"""2026-10-06 人工改判：后端契约。

用户原话：「我想自己可以改变用例是否通过，因为有时候ai弄得确实不准」。

钉住四条边界：
  1. 改判直接写 status —— 报表/KPI/重跑全都自动跟着走，不需要每个查询判断
  2. 改判必须留痕（谁/何时/为什么），且界面要能区分"人改的"和"AI 判的"
  3. 首次改判要把 AI 原判存下来，撤销才有得还原
  4. 只允许 passed/failed —— error/running 是执行层状态，不是"结论"
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, "<PROJECT_DIR>")

pytest.importorskip("aiosqlite")


def _fresh_db():
    """直接构造 engine，绕开环境变量/.env 解析（见 test_isolation_and_policy 的长注释）。

    这条**必须**断言连的是临时库 —— 不断言的话，隔离失效会静默写进用户真库。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod
    from app.models import Base

    tmp = os.path.join(tempfile.gettempdir(), "_tp_override_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)
    assert "_tp_override_test.db" in str(eng.url), f"没连到临时库：{eng.url}"

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(dbmod._add_missing_columns)

    asyncio.run(_create())
    return dbmod


def _seed(dbmod):
    from app.models import Project, Run, RunResult, TestCase

    async def go():
        async with dbmod.db_session() as s:
            p = Project(name="P", run_concurrency=1)
            s.add(p)
            await s.flush()
            c = TestCase(project_id=p.id, name="c1", prompt="x")
            s.add(c)
            await s.flush()
            run = Run(project_id=p.id, name="r", case_ids=[c.id], status="completed", total_count=1)
            s.add(run)
            await s.flush()
            # AI 判定为 failed，但其实是误判
            row = RunResult(run_id=run.id, case_id=c.id, status="failed", root_cause="product_defect")
            s.add(row)
            await s.flush()
            return row.id, run.id

    return asyncio.run(go())


def _client(dbmod):
    import app.api as api
    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    api._launch = lambda run_id: asyncio.sleep(0)
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t")


# ---------------------------------------------------------------- 1. 改判写 status


def test_override_writes_status_not_a_side_field():
    """改判后的结论必须就在 status 上 —— 否则每个统计口径都要多判断一次。"""
    dbmod = _fresh_db()
    rid, run_id = _seed(dbmod)

    async def go():
        async with _client(dbmod) as ac:
            r = await ac.patch(
                f"/api/results/{rid}", json={"status": "passed", "reason": "AI 误判，功能其实是好的"}
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "passed", body
            assert body["verdict_override"] == "passed"
            assert body["override_reason"] == "AI 误判，功能其实是好的"
            # AI 原判留下来了，撤销才有得还原
            assert body["original_status"] == "failed", body
            return body

    asyncio.run(go())


def test_overridden_result_is_visible_in_the_list():
    """改完再拉列表，看到的必须是改后的结论 —— 否则界面上改了跟没改一样。"""
    dbmod = _fresh_db()
    rid, run_id = _seed(dbmod)

    async def go():
        async with _client(dbmod) as ac:
            await ac.patch(f"/api/results/{rid}", json={"status": "passed"})
            r = await ac.get(f"/api/runs/{run_id}/results")
            assert r.status_code == 200, r.text
            rows = {x["id"]: x for x in r.json()}
            assert rows[rid]["status"] == "passed", rows[rid]
            # 界面要能看出这条是人改的，不是 AI 判的
            assert rows[rid]["verdict_override"] == "passed", rows[rid]

    asyncio.run(go())


# ---------------------------------------------------------------- 2. 留痕


def test_override_records_who_and_when():
    """没有审计痕迹的改判等于黑箱 —— 报告里的"通过"是谁定的必须能查到。"""
    dbmod = _fresh_db()
    rid, run_id = _seed(dbmod)

    async def go():
        async with _client(dbmod) as ac:
            r = await ac.patch(f"/api/results/{rid}", json={"status": "passed", "reason": "x"})
            body = r.json()
            assert body["override_by"], "没记录是谁改的"
            assert body["override_at"], "没记录改判时间"

    asyncio.run(go())


# ---------------------------------------------------------------- 3. 撤销


def test_clear_restores_the_ai_verdict():
    """撤销必须回到 AI 的原判，而不是随便挑一个值。"""
    dbmod = _fresh_db()
    rid, run_id = _seed(dbmod)

    async def go():
        async with _client(dbmod) as ac:
            await ac.patch(f"/api/results/{rid}", json={"status": "passed", "reason": "先改一下"})
            r = await ac.patch(f"/api/results/{rid}", json={"clear": True})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "failed", body          # 回到 AI 原判
            assert body["verdict_override"] is None, body    # 改判标记清干净
            assert body["original_status"] is None, body

    asyncio.run(go())


def test_second_override_does_not_lose_the_ai_verdict():
    """连改两次，AI 原判不能被第二次覆盖 —— 否则撤销就撤不回去了。"""
    dbmod = _fresh_db()
    rid, run_id = _seed(dbmod)

    async def go():
        async with _client(dbmod) as ac:
            await ac.patch(f"/api/results/{rid}", json={"status": "passed"})
            r = await ac.patch(f"/api/results/{rid}", json={"status": "failed"})
            assert r.json()["original_status"] == "failed", r.json()

    asyncio.run(go())


# ---------------------------------------------------------------- 4. 取值约束


@pytest.mark.parametrize("bad", ["error", "running", "pending", "whatever"])
def test_only_passed_and_failed_are_accepted(bad):
    """error/running 是执行层状态，不是"结论" —— 人能拍板的只有通过与不通过。"""
    dbmod = _fresh_db()
    rid, run_id = _seed(dbmod)

    async def go():
        async with _client(dbmod) as ac:
            r = await ac.patch(f"/api/results/{rid}", json={"status": bad})
            assert r.status_code == 422, f"{bad} 不该被接受：{r.status_code}"

    asyncio.run(go())


def test_missing_result_is_404():
    dbmod = _fresh_db()

    async def go():
        async with _client(dbmod) as ac:
            r = await ac.patch("/api/results/999999", json={"status": "passed"})
            assert r.status_code == 404

    asyncio.run(go())


# ---------------------------------------------------------------- 5. 迁移


def test_new_columns_are_registered_for_existing_databases():
    """create_all 只建表不加列 —— 已部署的库必须靠 _ADDITIVE_COLUMNS 补上。

    漏登记的后果是所有 insert 直接 "no such column" 崩掉。
    """
    from app.db import _ADDITIVE_COLUMNS

    cols = {(t, c) for t, c, _ in _ADDITIVE_COLUMNS}
    for need in (
        "verdict_override",
        "override_reason",
        "override_by",
        "override_at",
        "original_status",
    ):
        assert ("run_result", need) in cols, f"{need} 没登记，老库会缺列"
