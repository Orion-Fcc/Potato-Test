"""Runnable check: a timed-out case is not re-attempted.

A timeout means the case wants more wall clock than case_timeout_s. Re-running it
burns another full budget for the same outcome — 10 more minutes at 600s. It used
to happen anyway: the timed-out attempt was cut off while the SPA still sat on a
/login-ish URL, the session self-heal read that as "restored session is dead",
re-captured the login bundle and ran the whole case again (run 29 on the VRS
project: attempts=2, 324s of wall clock for one 150s case).

python -m pytest tests/test_timeout_no_reattempt.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.conftest import bind_temp_db, restore_db

pytest.importorskip("aiosqlite")


def _run(tmp_db: str, timed_out: bool) -> dict:
    """Drive engine.execute_one_case with a stubbed executor that always reports auth_failed
    (so the self-heal wants to fire) and, per the flag, a timeout."""
    from cryptography.fernet import Fernet

    _teardown: list = []

    os.environ["POTATO_SECRET_KEY"] = Fernet.generate_key().decode()
    os.environ.pop("AUTH_ENABLED", None)

    import app.db as db
    import app.engine as engine
    import app.executor as executor
    from app.crypto import encrypt
    from app.models import Credential, Environment, Project, Run, RunResult, TestCase

    # ★ 用 bind_temp_db 而不是 `os.environ['DATABASE_URL'] = ...` + `_engine = None`。
    #
    # 原写法（2026-10-10 之前）是：
    #     os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_db}"
    #     get_settings.cache_clear(); db._engine = db._Session = None
    #     await db.init_db()
    # 它在本项目里**不可靠**：settings 读 .env，环境变量的优先级在 pytest 下
    # 并不稳定（PROJECT_ISOLATION.md 第七节），于是 init_db() 会连到用户的真库 ——
    # 真库里因此出现过 9 个测试项目。bind_temp_db 直接构造 engine，
    # 并且自带"没连真库"的自证。
    eng, prev = bind_temp_db(tmp_db)
    _teardown.append((eng, prev))

    calls: list[str] = []

    async def fake_execute_case(spec, on_step=None, **kw):  # noqa: ANN001
        calls.append("execute")
        return executor.ResultSpec(
            case_id=spec.case_id,
            status="error",
            error=f"timeout after {spec.timeout_s}s" if timed_out else "boom",
            auth_failed=True,  # the self-heal's trigger — must be ignored on a timeout
            timed_out=timed_out,
        )

    async def fake_ensure_bundle(*a, **kw):  # noqa: ANN002, ANN003
        calls.append("heal")
        return '{"cookies": []}'  # non-empty => the engine re-attempts

    async def main() -> dict:
        await db.init_db()
        executor.execute_case = fake_execute_case
        engine._ensure_bundle = fake_ensure_bundle

        async with db.db_session() as s:
            p = Project(name="VRS", roles=["admin"], case_timeout_s=600)
            s.add(p)
            await s.flush()
            env = Environment(
                project_id=p.id, name="test", base_url="http://app.example.com", is_default=True
            )
            s.add(env)
            await s.flush()
            s.add(
                Credential(
                    project_id=p.id,
                    type="password",
                    role="admin",
                    environment_id=env.id,
                    label="admin",
                    username="admin",
                    secret=encrypt("pw"),
                    is_active=True,
                )
            )
            case = TestCase(project_id=p.id, name="c1", prompt="fill the form", role="admin")
            s.add(case)
            await s.flush()
            run = Run(
                project_id=p.id,
                name="r",
                case_ids=[case.id],
                total_count=1,
                environment_id=env.id,
            )
            s.add(run)
            await s.flush()
            run_id, case_id = run.id, case.id

        final = await engine.execute_one_case(run_id, case_id)

        async with db.db_session() as s:
            from sqlalchemy import select

            row = (
                (await s.execute(select(RunResult).where(RunResult.run_id == run_id)))
                .scalars()
                .first()
            )
            return {"final": final, "attempts": row.attempts, "calls": list(calls)}

    try:
        return asyncio.run(main())
    finally:
        # 还原成会话级的库，并释放这次建的临时 engine —— 不能留着不换回去。
        for eng, prev in _teardown:
            restore_db(*prev)
            try:
                asyncio.run(eng.dispose())
            except Exception:  # noqa: BLE001
                pass


# the leading "heal" in both is the pre-run capture for a password role account, not a
# re-attempt — only a second "execute" costs another full timeout budget.
def test_a_timeout_is_not_re_attempted(tmp_path) -> None:
    o = _run(str(tmp_path / "to.db"), timed_out=True)
    assert o["calls"] == ["heal", "execute"], (
        f"a timed-out case must run exactly once, got {o['calls']} — "
        "the self-heal is burning a second full timeout budget"
    )
    assert o["attempts"] == 1
    assert o["final"] == "error"


def test_a_dead_session_still_heals(tmp_path) -> None:
    """The guard must not disarm the self-heal for its real case: an attempt that failed
    fast on a login page (no timeout) is exactly what re-capturing the session fixes."""
    o = _run(str(tmp_path / "heal.db"), timed_out=False)
    assert o["calls"] == ["heal", "execute", "heal", "execute"], (
        f"a non-timeout auth failure must still heal and retry once, got {o['calls']}"
    )
    assert o["attempts"] == 2
