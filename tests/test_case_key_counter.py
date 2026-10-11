"""用例编号的**永久计数器** + 单项目详情端点（2026-10-09）。

## 这组用例守的是什么

### 1. 编号只增不减（`project.case_seq`）
编号缺陷修过两次，这组是第三次、也是最后一次的防线：

| 朝代 | 算法 | 缺陷 |
|---|---|---|
| 最初 | `COUNT(*)` | 删掉任意一条 → 下一条重发它的号（撞号） |
| 2026-10-04 | 现有最大编号 +1 | 删掉**最大**编号 → 下一条重发它 |
| 2026-10-09 | 项目永久计数器 | —— |

前两版的共同根因：把"下一个号"当成能从当前数据推导出来的东西。它不是 ——
它是"发行过就不回收"的事实，必须自己存。所以这些用例断言的是**行为**：
**删掉用例之后，那个号再也不出现**。

### 2. `GET /api/projects/{pid}`
补上这个端点之前的坑：它没有路由，未匹配的 GET 被 SPA 兜底接走，
调用方拿到 **200 + HTML**，然后在 `res.json()` 上炸出
"Unexpected token '<'" —— 一个完全不指向真实原因的报错。
这里同时钉住"返回形状与列表逐字段一致"，否则前端要写两套解析。
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest


@pytest.fixture()
def client():
    """TestClient + 一个只属于本测试的临时库。

    隔离写法（直接构造 engine 并赋值）的理由见 docs/PROJECT_ISOLATION.md 第七节：
    本项目的 `DATABASE_URL` 环境变量切库会静默失效。
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    dbname = f"_tp_casekey_{uuid.uuid4().hex[:8]}.db"
    tmp = os.path.join(tempfile.gettempdir(), dbname)
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    # 记下会话级的值：teardown 必须**还原成它**，不能置 None。
    # 置 None 会让下一个用到 db 的测试走 _ensure() 重建 engine（连到真库）——
    # 表现为"另一个文件里的用例突然报错"，而报错完全指不到这里。
    # 写法见 docs/PROJECT_ISOLATION.md 第七节。
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
    # 还原成**会话级的临时库**，不是 None —— 见上面 prev_engine 那段注释。
    dbmod._engine = prev_engine
    dbmod._Session = prev_session
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def _mk_project(client, name: str = "P") -> int:
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_case(client, pid: int, name: str = "c", **kw) -> dict:
    r = client.post(f"/api/projects/{pid}/testcases", json={"name": name, "prompt": "x", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _keys(client, pid: int) -> list[str]:
    rows = client.get(f"/api/projects/{pid}/testcases").json()
    return [r["case_key"] for r in rows]


# ---------------------------------------------------------------------------
# 永久计数器
# ---------------------------------------------------------------------------


def test_a_new_project_starts_at_tc_001(client) -> None:
    pid = _mk_project(client)
    assert _mk_case(client, pid)["case_key"] == "TC-001"
    assert _mk_case(client, pid)["case_key"] == "TC-002"


def test_deleting_the_newest_case_does_not_recycle_its_number(client) -> None:
    """★ 这条就是加计数器要修的那个缺陷。

    "现有最大编号 +1" 在删掉**最大**编号时会把号重发出去：
    建 TC-001/TC-002，删掉 TC-002，下一条又是 TC-002 ——
    用户看到的是"我删掉的那条又回来了"，而报告和缺陷单里两条不同用例共用一个编号。
    """
    pid = _mk_project(client)
    _mk_case(client, pid)  # TC-001
    second = _mk_case(client, pid)  # TC-002
    assert second["case_key"] == "TC-002"
    assert client.delete(f"/api/testcases/{second['id']}").status_code == 200

    fresh = _mk_case(client, pid)
    assert fresh["case_key"] == "TC-003", (
        f"被删掉的 TC-002 又被发了出去（拿到 {fresh['case_key']}）—— 计数器没生效"
    )
    assert "TC-002" not in _keys(client, pid)


def test_deleting_every_case_still_advances_the_counter(client) -> None:
    """全删光也不能从 TC-001 重来：编号是发行记录，不是"当前有几条"。"""
    pid = _mk_project(client)
    ids = [_mk_case(client, pid)["id"] for _ in range(3)]  # TC-001..TC-003
    for cid in ids:
        assert client.delete(f"/api/testcases/{cid}").status_code == 200
    assert _keys(client, pid) == []

    assert _mk_case(client, pid)["case_key"] == "TC-004", "删光之后编号又从 TC-001 开始了"


def test_deleting_a_middle_case_does_not_reuse_that_number(client) -> None:
    """中间挖洞（2026-10-04 修过的那半），别被这次改动弄回去。"""
    pid = _mk_project(client)
    for _ in range(5):
        _mk_case(client, pid)  # TC-001..TC-005
    rows = client.get(f"/api/projects/{pid}/testcases").json()
    third = next(r for r in rows if r["case_key"] == "TC-003")
    assert client.delete(f"/api/testcases/{third['id']}").status_code == 200

    assert _mk_case(client, pid)["case_key"] == "TC-006"
    assert "TC-003" not in _keys(client, pid)


def test_counters_are_per_project(client) -> None:
    """每个项目都从 TC-001 开始（设计如此）。A 的计数器不许影响 B。"""
    a = _mk_project(client, "A")
    b = _mk_project(client, "B")
    for _ in range(3):
        _mk_case(client, a)  # A: TC-001..003
    assert _mk_case(client, b)["case_key"] == "TC-001", "B 被 A 的计数器带跑了"


def test_an_existing_project_without_a_counter_backfills(client) -> None:
    """★ 老库升级路径：`case_seq` 是后加的列，存量行会拿到 0（= 尚未回填）。

    发第一条号时必须先从**现有用例**把计数器补到最大编号，
    否则它会从 TC-001 开始发，直接撞上存量用例的编号。
    """
    import asyncio

    from sqlalchemy import select

    from app import db as dbmod
    from app.models import Project, TestCase

    pid = _mk_project(client)

    async def _seed_and_zero():
        async with dbmod.db_session() as s:
            # 造 3 条存量用例（模拟计数器上线前就存在的项目）
            for i in (1, 2, 3):
                s.add(
                    TestCase(
                        project_id=pid,
                        case_key=f"TC-{i:03d}",
                        name=f"legacy {i}",
                        prompt="x",
                    )
                )
            # 把计数器打回 0，模拟"这一列刚加上、还没回填"
            proj = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
            proj.case_seq = 0

    asyncio.run(_seed_and_zero())

    fresh = _mk_case(client, pid)
    assert fresh["case_key"] == "TC-004", (
        f"老项目没有从现有编号回填，发出了 {fresh['case_key']}（应当从 TC-004 起）"
    )


def test_a_hand_picked_number_raises_the_counter(client) -> None:
    """手工指定 TC-050 之后，自动编号必须从 51 起 —— 不能一路发到 50 上撞车。"""
    pid = _mk_project(client)
    _mk_case(client, pid, case_key="TC-050")
    assert _mk_case(client, pid)["case_key"] == "TC-051"


def test_a_lower_hand_picked_number_does_not_lower_the_counter(client) -> None:
    """计数器只增不减：手工填个小号不能把计数器压回去（否则又会重发）。"""
    pid = _mk_project(client)
    _mk_case(client, pid)  # TC-001
    _mk_case(client, pid)  # TC-002
    _mk_case(client, pid, case_key="TC-010")  # 抬高到 10
    # 再手工填一个更小的号：不该把计数器降到 3
    _mk_case(client, pid, case_key="TC-003")
    assert _mk_case(client, pid)["case_key"] == "TC-011", "手工小号把计数器压低了"


def test_editing_a_case_to_a_big_number_raises_the_counter(client) -> None:
    """改编号（PUT）也要计入计数器，理由同新建。"""
    pid = _mk_project(client)
    c = _mk_case(client, pid)  # TC-001
    r = client.put(f"/api/testcases/{c['id']}", json={"case_key": "TC-099"})
    assert r.status_code == 200, r.text
    assert _mk_case(client, pid)["case_key"] == "TC-100"


def test_free_text_case_keys_do_not_break_the_counter(client) -> None:
    """Excel 允许自由文本编号（如 REQ-12）—— 它不参与计数，也不能把发号搞崩。"""
    pid = _mk_project(client)
    _mk_case(client, pid, case_key="REQ-12")
    assert _mk_case(client, pid)["case_key"] == "TC-001", "自由文本编号污染了计数器"


def test_duplicate_number_collision_still_returns_409(client) -> None:
    """计数器不取代唯一约束：真撞号仍然要回 409 + 人话。"""
    pid = _mk_project(client)
    _mk_case(client, pid, case_key="TC-007")
    r = client.post(
        f"/api/projects/{pid}/testcases",
        json={"name": "x", "prompt": "y", "case_key": "TC-007"},
    )
    assert r.status_code == 409
    assert "TC-007" in r.text


# ---------------------------------------------------------------------------
# GET /api/projects/{pid}
# ---------------------------------------------------------------------------


def test_get_single_project_returns_json_not_html(client) -> None:
    """★ 这条端点的存在性本身要钉住。

    没有它时，未匹配的 GET 会落到 SPA 兜底路由上，回 **200 + index.html**；
    调用方以为成功，然后在 `res.json()` 上炸 "Unexpected token '<'"。
    所以除了状态码，还要断言"真的不是 HTML"。
    """
    pid = _mk_project(client, "单取项目")
    r = client.get(f"/api/projects/{pid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json"), r.headers["content-type"]
    assert not r.text.lstrip().startswith("<"), "回了 HTML —— 说明又落到 SPA 兜底上了"
    assert r.json()["name"] == "单取项目"


def test_single_project_shape_matches_the_list_entry(client) -> None:
    """单取与列表**逐字段一致**，否则前端得写两套解析。"""
    pid = _mk_project(client, "形状一致")
    _mk_case(client, pid)

    single = client.get(f"/api/projects/{pid}").json()
    listed = next(x for x in client.get("/api/projects").json() if x["id"] == pid)

    assert set(single) == set(listed), (
        f"字段集合不一致：单取多 {set(single) - set(listed)}，"
        f"列表多 {set(listed) - set(single)}"
    )
    assert single == listed, "同一个项目的两个端点返回的内容不同"
    assert single["case_count"] == 1


def test_single_project_404_for_unknown_id(client) -> None:
    assert client.get("/api/projects/999999").status_code == 404


def test_single_project_404_after_delete(client) -> None:
    pid = _mk_project(client, "待删")
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_single_project_includes_last_run(client) -> None:
    """`last_run` 是列表页用的派生字段，单取时也要有（同一个 helper 算出来的）。"""
    pid = _mk_project(client)
    c = _mk_case(client, pid)
    run = client.post(f"/api/projects/{pid}/runs", json={"case_ids": [c["id"]]}).json()
    body = client.get(f"/api/projects/{pid}").json()
    assert body["last_run"]["id"] == run["id"]
