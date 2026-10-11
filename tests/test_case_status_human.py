"""人来变更用例状态：draft / blocked（2026-10-09）。

用户的原话：「用例也加上人可以变更状态的功能，因为有些用例 agent 可能执行不了」。

所以这组测试守的是**语义**，不是字段有没有存进去：

1. `blocked` 必须和 `deprecated` 分得开 —— deprecated 是"这条不要了"，
   blocked 是"这条**有效**，只是当前跑不了"。混在一起的话，看清单的人分不清
   "该删的"和"该想办法的"，而这两者的后续动作完全相反。
2. `blocked` 必须带原因。这个状态的**全部价值**就是那条原因；只标一个红点，
   下一个人只能把这条用例重新试一遍才知道它为什么被挡住。
3. `blocked` 必须**真的从回归集里出来**。否则"挡住"只是个标签，
   下一次全量运行照样跑到它、照样失败一次 —— 用户会以为这个功能没生效。
4. 离开 blocked 时必须清掉原因，否则已启用的用例还挂着过期的"为什么跑不了"。
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest


@pytest.fixture()
def client():
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    dbname = f"_tp_casestatus_{uuid.uuid4().hex[:8]}.db"
    tmp = os.path.join(tempfile.gettempdir(), dbname)
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    # 还原而不是置 None —— 见 docs/PROJECT_ISOLATION.md 第七节。
    prev_engine, prev_session = dbmod._engine, dbmod._Session
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    from app.main import app

    assert dbname in str(eng.url).replace("%5F", "_"), f"没连到临时库：{eng.url}"
    with TestClient(app) as c:
        yield c

    asyncio.run(eng.dispose())
    dbmod._engine, dbmod._Session = prev_engine, prev_session
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def _project(client, name: str = "P") -> int:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _case(client, pid: int, name: str = "用例") -> int:
    r = client.post(f"/api/projects/{pid}/testcases", json={"name": name, "prompt": "打开首页"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _get(client, pid: int, cid: int) -> dict:
    return next(c for c in client.get(f"/api/projects/{pid}/testcases").json() if c["id"] == cid)


# ---------------------------------------------------------------------------
# blocked 的语义
# ---------------------------------------------------------------------------


def test_a_human_can_block_a_case_the_agent_cannot_run(client) -> None:
    """★ 用户明确要求的能力：有些用例 agent 执行不了，人要能把它标出来。"""
    pid = _project(client)
    cid = _case(client, pid)
    r = client.post(
        f"/api/projects/{pid}/testcases/status",
        json={"ids": [cid], "status": "blocked", "reason": "需要人工先在 OA 里建好审批单"},
    )
    assert r.status_code == 200, r.text
    row = _get(client, pid, cid)
    assert row["status"] == "blocked"
    assert "OA" in row["blocked_reason"]


def test_blocking_without_a_reason_is_refused(client) -> None:
    """★ 只标状态不写原因 = 一个没有解释的红点。

    下一个人看到它只能把用例重新试一遍才知道为什么跑不了 ——
    而"重试一次"往往要花几分钟，这正是这个状态本该省掉的事。
    """
    pid = _project(client)
    cid = _case(client, pid)
    r = client.post(
        f"/api/projects/{pid}/testcases/status", json={"ids": [cid], "status": "blocked"}
    )
    assert r.status_code == 400
    assert "原因" in r.text
    assert _get(client, pid, cid)["status"] == "active", "被拒时不该有任何改动"


def test_blocked_is_not_the_same_as_deprecated(client) -> None:
    """★ 这两个状态回答的是不同的问题，必须分开。

    deprecated = 这条不要了（后续动作 = 删）
    blocked    = 这条有效但现在跑不了（后续动作 = 想办法）
    混成一个的话，看清单的人根本分不清该删哪条、该推哪条。
    """
    pid = _project(client)
    cid = _case(client, pid)
    client.post(
        f"/api/projects/{pid}/testcases/status",
        json={"ids": [cid], "status": "blocked", "reason": "依赖第三方系统"},
    )
    assert _get(client, pid, cid)["status"] == "blocked"
    assert _get(client, pid, cid)["status"] != "deprecated"


def test_a_blocked_case_leaves_the_regression_set(client) -> None:
    """★ 这条是"挡住"有没有真正生效的判据。

    如果只改 status 而不动 enabled，下一次全量运行照样会跑到它、照样失败一次 ——
    用户会以为这个功能没生效，然后回去手工一条条勾选。
    """
    pid = _project(client)
    cid = _case(client, pid)
    client.post(
        f"/api/projects/{pid}/testcases/status",
        json={"ids": [cid], "status": "blocked", "reason": "需要特定时段才能跑"},
    )
    row = _get(client, pid, cid)
    assert row["enabled"] is False, "被挡住的用例必须从「运行全部启用用例」里出去"

    # 这正是 run-all 的取数口径
    enabled_active = [
        r for r in client.get(f"/api/projects/{pid}/testcases").json() if r["enabled"]
    ]
    assert enabled_active == []


def test_unblocking_clears_the_stale_reason(client) -> None:
    """离开 blocked 必须清掉原因。

    否则一条已经启用的用例还挂着"为什么跑不了"，后面看报告的人会被这条过期信息误导。
    """
    pid = _project(client)
    cid = _case(client, pid)
    client.post(
        f"/api/projects/{pid}/testcases/status",
        json={"ids": [cid], "status": "blocked", "reason": "缺测试数据"},
    )
    client.post(f"/api/projects/{pid}/testcases/status", json={"ids": [cid], "status": "active"})
    row = _get(client, pid, cid)
    assert row["status"] == "active"
    assert row["blocked_reason"] == "", "启用之后还留着旧的阻挡原因"
    assert row["enabled"] is True


def test_single_case_patch_blocks_with_a_reason(client) -> None:
    """单条 PUT 也必须遵守同一套规则（界面上的"改状态"走的是这条）。"""
    pid = _project(client)
    cid = _case(client, pid)
    ok = client.put(
        f"/api/testcases/{cid}",
        json={"status": "blocked", "blocked_reason": "需要人工审批账号"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "blocked"

    cid2 = _case(client, pid, "第二条")
    bad = client.put(f"/api/testcases/{cid2}", json={"status": "blocked"})
    assert bad.status_code == 400
    assert "原因" in bad.text


def test_single_case_patch_unblocking_clears_the_reason(client) -> None:
    pid = _project(client)
    cid = _case(client, pid)
    client.put(f"/api/testcases/{cid}", json={"status": "blocked", "blocked_reason": "x"})
    back = client.put(f"/api/testcases/{cid}", json={"status": "active"})
    assert back.json()["blocked_reason"] == ""


# ---------------------------------------------------------------------------
# 与其它状态的交互
# ---------------------------------------------------------------------------


def test_a_draft_case_is_not_run_by_run_all(client) -> None:
    """★ 草稿不进回归集 —— 这条要在**真正执行的取数路径**上验证。

    2026-10-09 发现：`POST /projects/{pid}/runs`（不带 case_ids）原来只看
    `enabled=True`，而生成的草稿是 `status="draft", enabled=True` ——
    于是「草稿不会被跑到」这个承诺在 run-all 上是**假的**：
    用户点「运行全部」会把草稿一起跑掉，而且看起来完全正常。

    这条测试用 run-all 的实际结果来判，而不是用某个查询的形状 ——
    形状可以对、数据仍然被选中（上面那个 bug 就是这样）。
    """
    pid = _project(client)
    cid = _case(client, pid, "草稿用例")
    client.put(f"/api/testcases/{cid}", json={"status": "draft"})

    r = client.post(f"/api/projects/{pid}/runs", json={"name": "全量"})
    # 没有任何 active 用例 -> 应当拒绝（"no matching test cases"），
    # 而不是把这条草稿拿去跑
    assert r.status_code == 400, f"run-all 选中了草稿：{r.text}"
    assert "no matching" in r.text


def test_explicitly_naming_a_draft_case_still_runs_it(client) -> None:
    """反向守卫：勾选某条草稿再点运行，意思就是"我现在就要试这一条"。

    如果这条也被拦，用户就失去了审阅草稿的唯一手段（先跑一次看它行不行）。
    """
    pid = _project(client)
    cid = _case(client, pid, "草稿用例")
    client.put(f"/api/testcases/{cid}", json={"status": "draft"})
    r = client.post(f"/api/projects/{pid}/runs", json={"name": "试跑", "case_ids": [cid]})
    assert r.status_code in (200, 202), r.text
    assert cid in (r.json().get("case_ids") or [cid])


def test_bulk_block_requires_a_reason_for_every_target(client) -> None:
    """批量转 blocked 也必须带原因 —— 批量场景下漏原因的危害更大（几十条一起没解释）。"""
    pid = _project(client)
    ids = [_case(client, pid, f"c{i}") for i in range(3)]
    r = client.post(
        f"/api/projects/{pid}/testcases/status", json={"ids": ids, "status": "blocked"}
    )
    assert r.status_code == 400


def test_blocked_cases_are_still_visible_and_counted(client) -> None:
    """blocked **不是删除** —— 用例内容和历史结果都还在，列表里必须看得见。

    用户要的是"能标出来并知道原因"，不是"让它消失"。
    """
    pid = _project(client)
    cid = _case(client, pid, "被挡住的用例")
    client.post(
        f"/api/projects/{pid}/testcases/status",
        json={"ids": [cid], "status": "blocked", "reason": "依赖外部系统"},
    )
    all_rows = client.get(f"/api/projects/{pid}/testcases").json()
    assert any(r["id"] == cid for r in all_rows), "blocked 的用例从列表里消失了"
    only_blocked = client.get(f"/api/projects/{pid}/testcases?status=blocked").json()
    assert [r["id"] for r in only_blocked] == [cid]
