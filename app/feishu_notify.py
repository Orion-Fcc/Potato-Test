"""Proactive Feishu pushes to a project's bound group chat. All best-effort —
never raise into the caller (run finalize / issue update)."""

from __future__ import annotations

import asyncio
import logging
import time

from app.models import Issue, Project, Run

log = logging.getLogger("potato-test.feishu.notify")

# run_id -> monotonic timestamp of the last progress heartbeat actually sent.
# Process-local: the run executes in whichever process owns it, and a stale entry only
# means "this process pushed recently", which is exactly what we want to rate-limit.
_last_heartbeat: dict[int, float] = {}

# run_id -> milestones (percent) already announced. Keeps "crossed 25%" from firing
# again on the next case; without it every case after 25% would send a milestone card.
_announced_milestones: dict[int, set[int]] = {}

# run_ids whose "first case did not pass" alert has already gone out. One alert per
# run: a suite with 200 failures must not produce 200 cards.
_first_failure_sent: set[int] = set()


def forget_run(run_id: int) -> None:
    """Drop a finished run's push state (called from finalize)."""
    _last_heartbeat.pop(run_id, None)
    _announced_milestones.pop(run_id, None)
    _first_failure_sent.discard(run_id)


def _due(run_id: int, interval_s: int, force: bool) -> bool:
    """Whether a heartbeat may be sent right now.

    Split out (instead of inlined) because this is the only part that has interesting
    edge cases: a 378-case run calls it 378 times and must emit roughly one card per
    interval, not 378 cards.
    """
    if force:
        return True
    if interval_s <= 0:
        return False  # heartbeat disabled
    last = _last_heartbeat.get(run_id)
    return last is None or time.monotonic() - last >= interval_s


async def _send_with_retry(client, chat_id: str, card: dict) -> None:
    """Send a card, retrying once on a transient failure.

    One retry is deliberate: the network to Feishu can blip (a rotating proxy, a brief
    DNS failure), and losing a progress card means the wait stretches to the next
    heartbeat. A long retry chain would instead hold up whoever is calling us.
    """
    try:
        await client.send_card(chat_id, card)
        return
    except Exception as exc:
        log.warning("feishu 推送失败，2 秒后重试一次 → %s", exc)
    await asyncio.sleep(2)
    await client.send_card(chat_id, card)  # let it raise on the final attempt


async def _client(session):
    """A configured FeishuClient, or None when the bot isn't configured."""
    from app.feishu import FeishuClient, resolve_config

    cfg = await resolve_config(session)
    if not (cfg["app_id"] and cfg["app_secret"]):
        return None
    return FeishuClient(cfg["app_id"], cfg["app_secret"], cfg["api_base"])


async def _client_and_chat(session, project_id: int):
    """Return (FeishuClient, chat_id, project_name) if the project is bound and the
    bot is configured, else (None, None, None)."""
    proj = await session.get(Project, project_id)
    if proj is None or not proj.feishu_chat_id:
        return None, None, None
    client = await _client(session)
    if client is None:
        return None, None, None
    return client, proj.feishu_chat_id, proj.name


async def _reporter(session, issue_id: int) -> tuple[str | None, str | None]:
    """(open_id, name) of whoever reported the issue in chat, or (None, None)."""
    from sqlalchemy import select

    from app.models import FeedbackItem

    fb = (
        (await session.execute(select(FeedbackItem).where(FeedbackItem.issue_id == issue_id)))
        .scalars()
        .first()
    )
    return (fb.sender_id, fb.sender_name) if fb else (None, None)


async def mirror_issue(
    issue_id: int,
    message_id: str | None = None,
    image_keys: list[str] | None = None,
) -> None:
    """Create the Bitable defect row for an Issue if not already mirrored (best-effort).

    When the feedback message carried screenshots (message_id + image_keys), they
    are copied into the row's 问题截图 attachment field.
    """
    from app.db import db_session
    from app import feishu_bitable

    try:
        async with db_session() as s:
            issue = await s.get(Issue, issue_id)
            if issue is None or issue.bitable_record_id:
                return  # gone, or already mirrored
            proj = await s.get(Project, issue.project_id)
            if proj is None or not (proj.feishu_bitable_app_token and proj.feishu_bitable_table_id):
                return
            client = await _client(s)
            if client is None:
                return
            tokens: list[str] = []
            for i, key in enumerate(image_keys or []):
                if message_id is None:
                    break
                data = await client.download_message_resource(message_id, key)
                if data is None:
                    continue
                tok = await feishu_bitable.upload_screenshot(
                    client, proj.feishu_bitable_app_token, data, f"issue{issue_id}_{i + 1}.png"
                )
                if tok:
                    tokens.append(tok)
            rec = await feishu_bitable.create_issue_record(
                client,
                proj.feishu_bitable_app_token,
                proj.feishu_bitable_table_id,
                issue,
                proj.name,
                *await _reporter(s, issue_id),
                screenshot_tokens=tokens,
            )
            if rec:
                issue.bitable_record_id = rec
    except Exception:
        log.exception("bitable mirror_issue failed issue=%s", issue_id)


async def sync_issue_to_bitable(issue_id: int) -> None:
    """Keep the Bitable row in sync with an Issue's status (create it if missing)."""
    from app.db import db_session
    from app import feishu_bitable

    try:
        async with db_session() as s:
            issue = await s.get(Issue, issue_id)
            if issue is None:
                return
            proj = await s.get(Project, issue.project_id)
            if proj is None or not (proj.feishu_bitable_app_token and proj.feishu_bitable_table_id):
                return
            client = await _client(s)
            if client is None:
                return
            app_token, table_id = proj.feishu_bitable_app_token, proj.feishu_bitable_table_id
            if issue.bitable_record_id is None:
                rec = await feishu_bitable.create_issue_record(
                    client, app_token, table_id, issue, proj.name, *await _reporter(s, issue_id)
                )
                if rec:
                    issue.bitable_record_id = rec
                return
            record_id, status = issue.bitable_record_id, issue.status
        await feishu_bitable.update_status(client, app_token, table_id, record_id, status)
    except Exception:
        log.exception("bitable issue-status sync failed issue=%s", issue_id)


async def _latest_failure_name(session, run_id: int) -> str | None:
    """Name of the most recent non-passing case in this run, for the alert card.

    Falls back to the row's error text when the case was deleted (the FK is to
    test_case, and a case can disappear mid-run if someone edits the project).
    """
    from sqlalchemy import select

    from app.models import RunResult, TestCase

    try:
        row = (
            await session.execute(
                select(RunResult, TestCase.name)
                .join(TestCase, TestCase.id == RunResult.case_id)
                .where(RunResult.run_id == run_id, RunResult.status != "passed")
                .order_by(RunResult.id.desc())
                .limit(1)
            )
        ).first()
    except Exception:
        return None
    if row is None:
        return None
    result, case_name = row
    return case_name or (result.error or None)


async def push_run_started(run_id: int) -> None:
    """Announce that a run started, so the phone hears about it in seconds.

    Fired from `run_suite`, deliberately after `prepare_run` so the total case count is
    already known and the card is not wrong.

    Off by default (`feishu_push_start`): the point of the milestone redesign is to
    stop sending cards that say nothing new, and "did it even start?" is now answerable
    on demand with `@bot 状态`.
    """
    from app.config import get_settings
    from app.db import db_session
    from app.feishu_cards import run_started_card

    if not get_settings().feishu_push_start:
        return

    try:
        async with db_session() as s:
            run = await s.get(Run, run_id)
            if run is None:
                return
            # 2026-10-08 删掉一行 `proj = await s.get(Project, run.project_id)`：
            # 取出来从没被用过（项目名由 _client_and_chat 一并返回），
            # 等于每次推送白做一次查询。ruff 的 F841 把它翻了出来。
            client, chat_id, project_name = await _client_and_chat(s, run.project_id)
            if client is None:
                return
            card = run_started_card(run, project_name)
        await _send_with_retry(client, chat_id, card)
        _last_heartbeat[run_id] = time.monotonic()
    except Exception:
        log.exception("feishu push_run_started failed run=%s", run_id)


async def push_run_progress(run_id: int, *, force: bool = False) -> None:
    """Push only when something worth reporting happened.

    Called after every finished case, but almost always sends nothing -- that is the
    point. Three independent triggers, any of which can fire on a given case:

    * milestone  -- progress crossed 25% / 50% / 75% (configurable); each fires once
    * first failure -- the first case that did not pass; once per run
    * timed heartbeat -- only when `feishu_progress_interval_sec` > 0 (off by default)

    The failure alert is deliberately NOT rate-limited together with the others: a
    suite whose 3rd case fails is exactly the signal that must not wait for 25%.
    """
    from app.config import get_settings
    from app.db import db_session
    from app.feishu import run_elapsed_s
    from app.feishu_cards import run_failure_card, run_milestone_card, run_progress_card

    settings = get_settings()
    interval = settings.feishu_progress_interval_sec
    milestones = settings.feishu_milestones

    client = None
    chat_id = None
    cards: list[dict] = []
    try:
        async with db_session() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status not in ("pending", "running"):
                return
            client, chat_id, project_name = await _client_and_chat(s, run.project_id)
            if client is None:
                return
            total = run.total_count or 0
            processed = run.processed_count or 0
            passed = run.passed_count or 0
            failed = processed - passed
            # run_elapsed_s pins the timezone: SQLite returns a NAIVE datetime holding a
            # UTC value, and `.timestamp()` would read it as local time (+8h here).
            elapsed = run_elapsed_s(run)

            if force or (interval > 0 and _due(run_id, interval, force)):
                cards.append(
                    run_progress_card(run, project_name, elapsed_s=elapsed, interval_s=interval)
                )

            if settings.feishu_push_first_failure and failed > 0 and run_id not in _first_failure_sent:
                _first_failure_sent.add(run_id)
                cards.append(
                    run_failure_card(
                        run,
                        project_name,
                        elapsed_s=elapsed,
                        case_name=await _latest_failure_name(s, run_id),
                    )
                )

            if total:
                pct = processed * 100 // total
                crossed = [
                    m for m in milestones if pct >= m and m not in _announced_milestones.setdefault(run_id, set())
                ]
                if crossed:
                    # Mark every crossed mark as announced, but push only the highest:
                    # a tiny run can jump 25→50→75 between two cases, and three cards
                    # saying "25%... 50%... 75%" in the same second is noise.
                    _announced_milestones[run_id].update(crossed)
                    cards.append(
                        run_milestone_card(run, project_name, elapsed_s=elapsed, percent=max(crossed))
                    )
    except Exception:
        log.exception("feishu push_run_progress failed run=%s", run_id)
        return

    for card in cards:
        try:
            await _send_with_retry(client, chat_id, card)
        except Exception:
            log.warning("feishu 推送失败（已重试一次，放弃）run=%s", run_id)
    if cards:
        _last_heartbeat[run_id] = time.monotonic()


async def push_run_result(run_id: int) -> None:
    """Push a run's result card to its project's bound chat."""
    from app.db import db_session
    from app.feishu_cards import run_result_card

    try:
        async with db_session() as s:
            run = await s.get(Run, run_id)
            if run is None:
                return
            client, chat_id, project_name = await _client_and_chat(s, run.project_id)
            if client is None:
                return
            card = run_result_card(run, project_name)
        await _send_with_retry(client, chat_id, card)
        forget_run(run_id)
    except Exception:
        log.exception("feishu push_run_result failed run=%s", run_id)


async def push_issue_update(issue_id: int) -> None:
    """Push an issue-status card to its project's bound chat."""
    from app.db import db_session
    from app.feishu_cards import issue_update_card

    try:
        async with db_session() as s:
            issue = await s.get(Issue, issue_id)
            if issue is None:
                return
            client, chat_id, project_name = await _client_and_chat(s, issue.project_id)
            if client is None:
                return
            card = issue_update_card(issue, project_name)
        await client.send_card(chat_id, card)
    except Exception:
        log.exception("feishu push_issue_update failed issue=%s", issue_id)
