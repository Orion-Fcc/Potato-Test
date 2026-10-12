"""Runnable check: a run whose workers died still finishes.

Run 44 was cancelled with six cases stuck at 'running' and no finished_at, and stayed that
way for three days — a deploy recreated the celery worker mid-run, its case tasks were
already acked so nothing redelivered them, and the chord that finalizes a run only fires
once every case task returns. Nothing could ever have moved that run.

python -m pytest tests/test_stale_run_reconcile.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.engine import hold_limit, reconcile_stale_runs, stall_limit
from tests.conftest import bind_temp_db, restore_db

CASE_TIMEOUT = 600  # project 6 (VRS)


@pytest.fixture(autouse=True)
def _own_temp_db(tmp_path):
    """★ 这个文件里的用例会**真的写库**（建 project/run/run_result），必须自带隔离。

    没有它的时候，写入目标是 `app.db` 的全局 engine —— 而那个 globals 可能被
    别的测试置成了 None，于是 `init_db()` 按 settings 重建 = 连用户的真库。
    实测后果：真库里多出 3 组 `reconcile-check` / `live-check` / `fk-check`
    项目（18 条 case、12 个 run、18 条 run_result），而且症状是**别的文件报错**。

    conftest 的 `_reset_db_between_tests` 已经会在每个测试之后兜底，
    但这里主动给出自己的库更明确：这个文件的用例需要"空库 + 干净起点"，
    而不是"上一个测试剩下的临时库"。
    """
    eng, prev = bind_temp_db(tmp_path / "_tp_reconcile.db")
    try:
        yield
    finally:
        restore_db(*prev)
        try:
            asyncio.run(eng.dispose())
        except Exception:  # noqa: BLE001
            pass


def test_a_silent_run_is_only_declared_dead_after_a_full_case_budget() -> None:
    """The gap has to cover the quiet stretches a healthy case really has: waiting for a
    free account for its role, capturing a session bundle, then the case itself."""
    assert stall_limit(CASE_TIMEOUT) > timedelta(seconds=CASE_TIMEOUT * 2), (
        "a case can sit silent through an account wait and a session capture before its "
        "first browser step — that must not read as a dead worker"
    )
    # a cancelled run stops at the next step, so it must not need the same patience
    assert stall_limit(CASE_TIMEOUT, cancelled=True) < stall_limit(CASE_TIMEOUT)


def test_a_concurrency_slot_outlives_the_case_holding_it() -> None:
    """The run-concurrency lease and the reconciler measure the same thing — how long a
    case can legitimately hold on — so they must not drift apart. A lease shorter than the
    case frees the slot with a browser still on it and 并发 quietly climbs past its cap;
    VRS overrides case_timeout_s to 1200s against a global default of 150s, and the lease
    used to be sized off the global one (expiring at 600s mid-browser)."""
    from app.engine import _TERMINAL

    assert hold_limit(1200) >= stall_limit(1200), (
        "the slot lease must last at least as long as the reconciler's patience, else a "
        "slot frees before anything notices the case is gone"
    )
    # a case waits for an account, captures a session, then runs — three full budgets
    assert hold_limit(1200).total_seconds() > 3 * 1200
    # and a waiter has to give up on every terminal state, not just cancellation, or it
    # spins forever on a run the reconciler marked completed
    assert set(_TERMINAL) == {"completed", "failed", "cancelled"}


async def test_reconcile_finishes_a_run_whose_worker_died() -> None:
    """End to end against a real (sqlite) DB: rebuild run 44's shape — most cases done,
    six left at 'running', nothing beating — and check one reconcile pass ends it."""
    from app.db import db_session, init_db
    from app.models import Project, Run, RunResult, TestCase

    await init_db()
    long_ago = datetime.now(UTC) - timedelta(days=3)
    async with db_session() as s:
        project = Project(name="reconcile-check", base_url="http://x", case_timeout_s=CASE_TIMEOUT)
        s.add(project)
        await s.flush()
        cases = [TestCase(project_id=project.id, name=f"c{i}", prompt="p") for i in range(4)]
        for c in cases:
            s.add(c)
        await s.flush()
        run = Run(
            project_id=project.id,
            name="died mid-run",
            case_ids=[c.id for c in cases],
            status="running",
            total_count=4,
            processed_count=2,
            started_at=long_ago,
            created_at=long_ago,
        )
        s.add(run)
        await s.flush()
        for c, status in zip(cases[:3], ["passed", "failed", "running"], strict=True):
            s.add(
                RunResult(
                    run_id=run.id,
                    case_id=c.id,
                    status=status,
                    created_at=long_ago,
                    updated_at=long_ago,
                )
            )
        run_id, ids = run.id, [c.id for c in cases]

    assert await reconcile_stale_runs() == [run_id]

    async with db_session() as s:
        run = await s.get(Run, run_id)
        assert run.finished_at is not None, "a dead run must reach a terminal state"
        assert run.status == "completed"
        rows = {r.case_id: r for r in (await _results(s, run_id))}
        assert not any(r.status == "running" for r in rows.values())
        # the 4th case's task died before it could even open a row — it must still appear
        assert set(rows) == set(ids)
        assert run.processed_count == 4
        assert run.summary["passed"] == 1


async def test_an_orphan_row_under_a_finished_run_is_swept() -> None:
    """★ 父运行**已终态**、结果行却还挂着 running（2026-10-12 补的真漏洞）。

    上面那一轮扫描只查 `finished_at IS NULL` 的运行 —— 语义是"终结还没结束的运行"。
    但有一类行它永远够不着：运行已经 `failed`（终态），却因为崩溃/取消
    留下一条 running 的结果。

    实测：run 20 状态 `failed`，而 run_result 228 从 2026-10-11 09:09
    一直挂着 running —— 查不到、也修不掉，报告里就是一条永远"运行中"的行。

    判据必须"绝不误伤在跑的行"：只处理**父运行已终态**的，所以下面还要
    断言一个正常运行没被动。
    """
    from app.db import db_session, init_db
    from app.models import Project, Run, RunResult, TestCase

    await init_db()
    now = datetime.now(UTC)
    async with db_session() as s:
        project = Project(name="orphan-check", base_url="http://x")
        s.add(project)
        await s.flush()
        case = TestCase(project_id=project.id, name="c1", prompt="p")
        s.add(case)
        await s.flush()

        # ① 已失败（终态）的运行 + 一条没落定的结果 = 孤儿
        dead = Run(
            project_id=project.id,
            name="failed but left a running row",
            case_ids=[case.id],
            status="failed",
            total_count=1,
            processed_count=0,
            started_at=now,
            created_at=now,
            finished_at=now,
        )
        # ② 正在跑的运行 —— 它的 running 行**绝不能被扫掉**
        live = Run(
            project_id=project.id,
            name="still going",
            case_ids=[case.id],
            status="running",
            total_count=1,
            processed_count=0,
            started_at=now,
            created_at=now,
        )
        s.add_all([dead, live])
        await s.flush()
        orphan = RunResult(run_id=dead.id, case_id=case.id, status="running", created_at=now)
        in_flight = RunResult(run_id=live.id, case_id=case.id, status="running", created_at=now)
        s.add_all([orphan, in_flight])
        await s.flush()
        orphan_id, flight_id = orphan.id, in_flight.id

    await reconcile_stale_runs()

    async with db_session() as s:
        o = await s.get(RunResult, orphan_id)
        f = await s.get(RunResult, flight_id)
        assert o.status == "error", f"孤儿行没被收敛：{o.status}"
        assert o.error and "没有落定" in o.error, f"没说清原因：{o.error!r}"
        assert f.status == "running", "★ 误伤了正在跑的结果行"


async def test_reconcile_leaves_a_live_run_alone() -> None:
    """The whole risk of a watchdog: killing a run that is merely slow."""
    from app.db import db_session, init_db
    from app.models import Project, Run, RunResult, TestCase

    await init_db()
    now = datetime.now(UTC)
    async with db_session() as s:
        project = Project(name="live-check", base_url="http://x", case_timeout_s=CASE_TIMEOUT)
        s.add(project)
        await s.flush()
        case = TestCase(project_id=project.id, name="c", prompt="p")
        s.add(case)
        await s.flush()
        run = Run(
            project_id=project.id,
            name="slow but alive",
            case_ids=[case.id],
            status="running",
            total_count=1,
            started_at=now - timedelta(hours=2),
            created_at=now - timedelta(hours=2),
        )
        s.add(run)
        await s.flush()
        # two hours in, but the case wrote a step a minute ago
        s.add(
            RunResult(
                run_id=run.id,
                case_id=case.id,
                status="running",
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(minutes=1),
            )
        )
        run_id = run.id

    assert run_id not in await reconcile_stale_runs()
    async with db_session() as s:
        assert (await s.get(Run, run_id)).finished_at is None


async def test_a_run_referencing_a_deleted_case_does_not_block_the_others() -> None:
    """What the first live pass actually hit: an old run listed a case that had since been
    deleted, the foreign key rejected the placeholder row, and the rollback took every
    other stale run in the same transaction with it — nothing got reconciled at all."""
    from app.db import db_session, init_db
    from app.models import Project, Run, RunResult, TestCase

    await init_db()
    long_ago = datetime.now(UTC) - timedelta(days=3)
    async with db_session() as s:
        project = Project(name="fk-check", base_url="http://x", case_timeout_s=CASE_TIMEOUT)
        s.add(project)
        await s.flush()
        case = TestCase(project_id=project.id, name="survivor", prompt="p")
        s.add(case)
        await s.flush()
        # run A: lists a case id that does not exist. run B: perfectly ordinary.
        runs = [
            Run(
                project_id=project.id,
                name=name,
                case_ids=ids,
                status="running",
                total_count=len(ids),
                started_at=long_ago,
                created_at=long_ago,
            )
            for name, ids in (("deleted case", [999_999]), ("healthy", [case.id]))
        ]
        for r in runs:
            s.add(r)
        await s.flush()
        s.add(
            RunResult(
                run_id=runs[1].id,
                case_id=case.id,
                status="running",
                created_at=long_ago,
                updated_at=long_ago,
            )
        )
        bad_id, good_id = runs[0].id, runs[1].id

    finalized = await reconcile_stale_runs()
    assert good_id in finalized, "one unwritable run must not stop the rest of the backlog"

    async with db_session() as s:
        assert (await s.get(Run, good_id)).finished_at is not None
        assert all(r.status != "running" for r in await _results(s, good_id))
        # the deleted case simply gets no placeholder row — there is nothing to re-run
        assert bad_id in finalized
        assert await _results(s, bad_id) == []


async def _results(session, run_id: int):
    from sqlalchemy import select

    from app.models import RunResult

    return (
        (await session.execute(select(RunResult).where(RunResult.run_id == run_id))).scalars().all()
    )


if __name__ == "__main__":
    test_a_silent_run_is_only_declared_dead_after_a_full_case_budget()
    asyncio.run(test_reconcile_finishes_a_run_whose_worker_died())
    asyncio.run(test_reconcile_leaves_a_live_run_alone())
    asyncio.run(test_a_run_referencing_a_deleted_case_does_not_block_the_others())
    print("ok")
