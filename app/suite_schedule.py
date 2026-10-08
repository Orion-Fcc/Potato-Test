"""Suite cadence → automatic execution.

Why this exists
===============
``TestSuite`` has carried ``cadence`` (none|daily|weekly|biweekly|monthly), ``due_at``
and ``runner_user_id`` since suites were introduced, and ``celery_app.scan_suite_reminders``
already emails the runner when ``due_at`` passes. But that task's own docstring is blunt
about the gap: **"Does NOT auto-run."**

A cadence that only sends mail is a to-do list, not a schedule. The reason to record
"this suite is due weekly" is that the weekly run happens — otherwise the记录 buys you a
reminder that a human then has to action by hand, which is exactly the manual work the
field was supposed to remove.

Two runners, one guarantee
==========================
Suites are swept both by Celery beat (server deployment) and by an in-process loop
(local single-user, where there is no broker). That is deliberate — the local user is
the one who most needs it — but it means two runners can look at the same due suite.

The guard is a compare-and-swap on ``due_at``, not a lock: each candidate suite is
claimed with ``UPDATE ... WHERE id = ? AND due_at = <the value we read>``, and only the
writer whose read still matches advances it. ``rowcount != 1`` means someone else got
there first and we skip. That gives at-most-once without a lock service, and it also
covers the "beat container restarted mid-sweep" case for free.

One more rule worth stating because it is a judgement call: a suite whose previous run is
**still going** is skipped, not stacked. A suite that takes 40 minutes on a 30-minute
cadence would otherwise pile up runs faster than it can finish them.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger("potato-test.schedule")

CADENCE_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "biweekly": 14}
# How often the in-process sweeper wakes up. Cadences are day-granularity, so this only
# decides how late a due suite can start — not how many times it runs (the CAS handles
# that). 5 minutes keeps a "due at 09:00" suite from starting at 09:40 without spinning.
SWEEP_INTERVAL_S = 300.0
# Cap per sweep: a backlog of hundreds of due suites must not launch hundreds of browsers
# in one tick (concurrency is 1 on this deployment). The rest stay due and go next tick.
MAX_PER_SWEEP = 3


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even for timezone=True columns.

    Comparing a naive value against an aware ``now`` raises TypeError, so every value that
    crosses the DB boundary gets coerced to UTC-aware first. Without this the whole sweep
    dies on the first row and looks like "scheduling silently does nothing".
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _add_months(dt: datetime, n: int) -> datetime:
    """Calendar-aware month addition.

    ``+ 30 days`` would drift: a suite due Jan 31 "monthly" would land on Mar 2 and keep
    walking. Clamping the day instead (Feb 28) keeps the due date in the right month,
    which is what someone writing "monthly" means.
    """
    month_index = dt.month - 1 + n
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_due(cadence: str | None, base: datetime) -> datetime | None:
    """When this suite is next due, counting forward from ``base``.

    ``None`` means "no cadence" — the caller must NOT treat that as "already due".
    Always anchored to ``base`` (normally "now"), not to the previous due date: a suite
    that has been un-run for a month should not fire five times to catch up. Missing a
    period is information, not a debt.
    """
    c = (cadence or "none").strip().lower()
    if c in CADENCE_DAYS:
        return base + timedelta(days=CADENCE_DAYS[c])
    if c == "monthly":
        return _add_months(base, 1)
    return None


def is_due(suite: Any, now: datetime) -> bool:
    """Whether this suite should run now.

    Requires all three: a real cadence, a due_at that has been set, and that due_at having
    passed. A suite with a cadence but no due_at is one whose first run has not been
    scheduled yet — inventing a due date for it here would start suites that nobody
    scheduled, so it is left alone.
    """
    if (getattr(suite, "cadence", None) or "none") == "none":
        return False
    due = _aware(getattr(suite, "due_at", None))
    if due is None:
        return False
    return due <= now


async def _claim(s: Any, suite: Any, now: datetime) -> bool:
    """Atomically take ownership of a due suite. True = we own it, go create the run.

    The CAS is on ``due_at`` so a concurrent sweeper that read the same value loses.
    """
    from sqlalchemy import update

    from app.models import TestSuite

    expected = _aware(suite.due_at)
    new_due = next_due(suite.cadence, now)
    res = await s.execute(
        update(TestSuite)
        .where(TestSuite.id == suite.id, TestSuite.due_at == expected)
        .values(due_at=new_due)
    )
    return res.rowcount == 1


async def create_run_for_suite(
    s: Any,
    suite: Any,
    *,
    trigger: str,
    ran_by_user_id: int | None = None,
    created_by: str | None = None,
    name: str | None = None,
) -> Any:
    """Build (but do not launch) a Run for a suite. None when it has no runnable cases.

    Shared by the manual "run this suite now" endpoint and the scheduler. Keeping one
    implementation is the point: a scheduled run that picks a different case set or a
    different environment than the button does is a bug you only find by comparing two
    run reports weeks apart.
    """
    from app.api import _default_env_id, _resolve_suite_case_ids
    from app.models import Project, Run

    case_ids = await _resolve_suite_case_ids(s, suite)
    if not case_ids:
        return None
    project = await s.get(Project, suite.project_id)
    run = Run(
        project_id=suite.project_id,
        name=name or suite.name,
        case_ids=case_ids,
        concurrency=(project.run_concurrency if project else None)
        or get_settings().run_concurrency,
        total_count=len(case_ids),
        suite_id=suite.id,
        ran_by_user_id=ran_by_user_id,
        trigger=trigger,
        environment_id=suite.environment_id or await _default_env_id(s, suite.project_id),
        created_by=created_by,
    )
    s.add(run)
    await s.flush()
    return run


async def sweep_due_suites(*, now: datetime | None = None, limit: int = MAX_PER_SWEEP) -> list[int]:
    """Create one run for every suite that is due, and launch it. Returns the new run ids.

    Never raises: this runs from a background loop where an exception would otherwise kill
    the sweeper for the rest of the process's life, and a broken schedule is invisible
    until someone notices no suites ran.
    """
    now = now or datetime.now(UTC)
    launched: list[int] = []
    try:
        from sqlalchemy import select

        from app.api import _launch
        from app.db import db_session
        from app.models import Run, TestSuite

        async with db_session() as s:
            suites = (
                (await s.execute(select(TestSuite).where(TestSuite.cadence != "none")))
                .scalars()
                .all()
            )
            for suite in suites:
                if len(launched) >= limit:
                    break
                if not is_due(suite, now):
                    continue
                if not await _claim(s, suite, now):
                    # Another sweeper advanced due_at between our read and this write.
                    continue
                pending = (
                    await s.execute(
                        select(Run.id).where(
                            Run.suite_id == suite.id,
                            Run.status.in_(("pending", "running")),
                        )
                    )
                ).first()
                if pending is not None:
                    # Previous auto-run still going: the cadence already advanced, so this
                    # period is skipped rather than stacked.
                    log.info("suite %s 上一轮仍在运行，跳过本次排期", suite.id)
                    continue
                run = await create_run_for_suite(
                    s,
                    suite,
                    trigger="schedule",
                    ran_by_user_id=suite.runner_user_id,
                    # 没有 created_by：不是谁触发的，这正是"自动"的含义。
                    created_by=None,
                    name=f"{suite.name} · {now:%Y-%m-%d}",
                )
                if run is None:
                    log.warning("suite %s 到期但没有可运行用例（可能已被删除/停用）", suite.id)
                    continue
                launched.append(run.id)
        # Launch outside the session: _launch fans out Celery tasks / spawns work, and
        # holding a transaction open across that would pin the SQLite writer.
        for run_id in launched:
            try:
                await _launch(run_id)
            except Exception as exc:  # noqa: BLE001 — one bad run must not stop the rest
                log.warning("suite 排期启动 run %s 失败：%s", run_id, exc)
    except Exception as exc:  # noqa: BLE001 — a sweeper must outlive its own bugs
        log.warning("套件排期扫描失败（下一轮会重试）：%s", exc)
    return launched


def get_settings() -> Any:
    """Local import shim — keeps this module importable without loading app.config eagerly."""
    from app.config import get_settings as _gs

    return _gs()


async def sweep_forever(interval_s: float | None = None) -> None:
    """In-process sweeper for deployments without a broker.

    Skipped entirely when Redis is configured: there Celery beat owns the schedule, and
    running both would be two sweepers for one due_at. (Even so the CAS would keep it
    correct — this just avoids the pointless DB churn.)
    """
    s = get_settings()
    if s.redis_url:
        log.info("已配置 Redis，套件排期交由 Celery beat 负责，进程内扫描不启动")
        return
    every = float(interval_s or getattr(s, "suite_sweep_interval_s", SWEEP_INTERVAL_S) or SWEEP_INTERVAL_S)
    log.info("套件排期扫描已启动（进程内，每 %.0fs 一次）", every)
    while True:
        await asyncio.sleep(every)
        ids = await sweep_due_suites()
        if ids:
            log.warning("套件排期自动启动了 %d 个运行：%s", len(ids), ids)
