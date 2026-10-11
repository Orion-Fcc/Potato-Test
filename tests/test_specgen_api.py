"""从需规生成用例：API 层（2026-10-09）。

守两件事：

1. **草稿不会进回归集。** 生成的用例默认 `status=draft`，而"运行全部启用用例"
   只收 `enabled=true` 的 active 用例。这条边界一旦破掉，LLM 写的垃圾用例
   就会混进每一次回归 —— 而用户是在看到一片红之后才知道的。
2. **生成必须以文档为依据。** 章节列表来自知识库真实标题；生成时正文按标题取，
   取不到就明确报错，不能拿"没读到正文"去悄悄生成一批编造用例。

不测真正的 LLM 调用（那要真网关、要说钱）：`/spec/draft` 的**解析与落库**用假响应覆盖，
提示词本身由 tests/test_specgen.py 的纯函数测试与提示词常量断言守住。
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

    dbname = f"_tp_specgen_{uuid.uuid4().hex[:8]}.db"
    tmp = os.path.join(tempfile.gettempdir(), dbname)
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    # 记下会话级的值：teardown 必须还原成它，**不能置 None**（理由见
    # docs/PROJECT_ISOLATION.md 第七节，以及 test_api_crud.py 里同款注释）。
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
    # 还原成**会话级的临时库**，不是 None。
    dbmod._engine = prev_engine
    dbmod._Session = prev_session
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def _project(client, name: str = "P") -> int:
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_knowledge(pid: int, doc: str) -> None:
    """把一份带标题的文档塞进项目知识库（走真实的分块路径）。"""
    import asyncio

    from app import db as dbmod
    from app import knowledge

    async def go():
        async with dbmod.db_session() as s:
            await knowledge.replace_knowledge(s, pid, doc, source="test")

    asyncio.run(go())


DOC = """# 资源审批配置

## 3.1 列表查询
在资源审批配置列表中，可以按名称筛选。点击查询后列表只显示名称匹配的记录。
名称不匹配时列表为空，并显示"暂无数据"。

## 3.2 新建规则
点击新建规则，弹窗中包含规则名称、使用范围、资源范围三个字段。
三个字段都必填，缺任意一个点击保存会提示"请填写完整"。
建议支持从已有规则复制，但本期可以不实现。

## 9.1 性能指标
列表查询接口的响应时间应小于 200 毫秒，数据库主键必须唯一，日志需落盘 30 天。
"""


def test_outline_is_empty_without_knowledge(client) -> None:
    """没有文档时要明说怎么上传，而不是给一个空白页。"""
    pid = _project(client)
    body = client.get(f"/api/projects/{pid}/spec/outline").json()
    assert body["has_knowledge"] is False
    assert body["headings"] == []
    assert "上传" in body["note"]


def test_outline_lists_real_headings_without_calling_the_model(client) -> None:
    """plan=false 时必须零 LLM 调用 —— 它是"先看看有哪些章节"的廉价入口。"""
    pid = _project(client)
    _seed_knowledge(pid, DOC)

    body = client.get(f"/api/projects/{pid}/spec/outline", params={"plan": False}).json()
    assert body["has_knowledge"] is True
    assert body["planned"] is False
    assert "资源审批配置" in body["headings"]
    assert "3.1 列表查询" in body["headings"]
    assert len(body["modules"]) == len(body["headings"]), "兜底分组是每个标题一个模块"


def test_outline_headings_keep_document_order(client) -> None:
    """顺序决定界面上的推进顺序，也决定用户的阅读顺序。"""
    pid = _project(client)
    _seed_knowledge(pid, DOC)
    body = client.get(f"/api/projects/{pid}/spec/outline", params={"plan": False}).json()
    hs = body["headings"]
    assert hs.index("3.1 列表查询") < hs.index("3.2 新建规则") < hs.index("9.1 性能指标")


def test_section_text_returns_the_body_under_a_heading(client) -> None:
    """生成的前提：按标题能取到**正文**。取不到就必须报错，不能悄悄编。"""
    import asyncio

    from app import specgen

    pid = _project(client)
    _seed_knowledge(pid, DOC)
    text = asyncio.run(specgen.section_text(pid, ["3.2 新建规则"]))
    assert "三个字段都必填" in text
    assert "9.1 性能指标" not in text, "不该把别的章节也带进来"


def test_section_text_of_an_unknown_heading_is_empty(client) -> None:
    import asyncio

    from app import specgen

    pid = _project(client)
    _seed_knowledge(pid, DOC)
    assert asyncio.run(specgen.section_text(pid, ["不存在的章节"])) == ""


def test_draft_rejects_an_empty_title_list(client) -> None:
    pid = _project(client)
    r = client.post(f"/api/projects/{pid}/spec/draft", json={"module": "m", "titles": []})
    assert r.status_code == 400


def test_draft_rejects_headings_with_no_body(client) -> None:
    """★ 标题在、正文取不到时必须 404，而不是带着空正文去让模型编。

    这条防的是"文档被重新上传过、章节清单过期了"这种真实情况：
    如果这里放过去，用户会拿到一批**没有原文依据**的用例，而且看起来很正常。
    """
    pid = _project(client)
    _seed_knowledge(pid, DOC)
    r = client.post(
        f"/api/projects/{pid}/spec/draft",
        json={"module": "过期模块", "titles": ["这个标题已经不存在了"]},
    )
    assert r.status_code == 404
    assert "找不到正文" in r.text


# ---------------------------------------------------------------------------
# 落库：草稿的边界
# ---------------------------------------------------------------------------


def _accept(client, pid: int, cases: list[dict], **kw):
    return client.post(
        f"/api/projects/{pid}/spec/accept",
        json={"cases": cases, "module": "3.1 列表查询", **kw},
    )


def _good_case(name: str = "按名称筛选") -> dict:
    return {
        "name": name,
        "prompt": "打开资源审批配置列表，名称填「课程资源」后点击查询。",
        "expected": "列表只显示名称包含「课程资源」的记录。",
        "type": "functional",
        "priority": "P1",
    }


def test_accepted_drafts_land_as_draft_status(client) -> None:
    """★ 默认必须落 draft。这是"先审后启用"的机制保障，不是礼貌。"""
    pid = _project(client)
    r = _accept(client, pid, [_good_case()])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"
    assert r.json()["count"] == 1

    rows = client.get(f"/api/projects/{pid}/testcases", params={"status": "draft"}).json()
    assert len(rows) == 1
    assert rows[0]["name"] == "按名称筛选"
    assert rows[0]["case_key"], "生成的用例也要拿到项目内编号"


def test_generated_cases_carry_their_source(client) -> None:
    """来源必须可追溯：事后要能回答"这条是从哪一节生成的"。"""
    pid = _project(client)
    _accept(client, pid, [_good_case()])
    row = client.get(f"/api/projects/{pid}/testcases").json()[0]
    assert row["source"].startswith("spec:"), row["source"]
    assert "3.1 列表查询" in row["source"]


def test_manual_cases_have_no_spec_source(client) -> None:
    """人写的用例不能被标成生成的 —— 否则按来源筛选就失去意义。"""
    pid = _project(client)
    r = client.post(
        f"/api/projects/{pid}/testcases", json={"name": "人工写的", "prompt": "打开首页"}
    )
    assert r.json()["source"] == ""


def test_accept_can_activate_explicitly(client) -> None:
    pid = _project(client)
    r = _accept(client, pid, [_good_case()], activate=True)
    assert r.json()["status"] == "active"
    rows = client.get(f"/api/projects/{pid}/testcases", params={"status": "active"}).json()
    assert len(rows) == 1


def test_accept_drops_incomplete_cases_it_was_handed(client) -> None:
    """界面回传的是用户**编辑过**的用例，用户也可能把预期清空 ——
    入库前必须再过一次校验，不能因为"来自界面"就信任它。"""
    pid = _project(client)
    bad = {**_good_case("被清空的"), "expected": ""}
    r = _accept(client, pid, [bad])
    assert r.status_code == 400
    assert "不可判定" in r.text


def test_accept_rejects_an_empty_payload(client) -> None:
    pid = _project(client)
    assert _accept(client, pid, []).status_code == 400


def test_drafts_are_not_in_the_enabled_set_used_by_run_all(client) -> None:
    """★ 最关键的边界：草稿**不会**被"运行全部启用用例"选中。

    没有这条，"先生成后审阅"就只是界面上的一个说法 ——
    草稿会跟着每一次全量回归一起跑，而这正是我们要防的。
    """
    pid = _project(client)
    _accept(client, pid, [_good_case("草稿一"), _good_case("草稿二")])

    # 这个查询就是 run-all 的取数口径（enabled=true）
    rows = client.get(f"/api/projects/{pid}/testcases").json()
    enabled = [r for r in rows if r["enabled"] and r["status"] == "active"]
    assert enabled == [], f"草稿竟然进了启用集：{enabled}"


def test_bulk_status_change_activates_drafts(client) -> None:
    pid = _project(client)
    _accept(client, pid, [_good_case("甲"), _good_case("乙")])
    ids = [r["id"] for r in client.get(f"/api/projects/{pid}/testcases").json()]

    r = client.post(
        f"/api/projects/{pid}/testcases/status", json={"ids": ids, "status": "active"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 2

    rows = client.get(f"/api/projects/{pid}/testcases").json()
    assert all(x["status"] == "active" and x["enabled"] for x in rows)


def test_bulk_status_rejects_an_unknown_value(client) -> None:
    """批量写错一个值就是几十条一起错，所以白名单必须硬。"""
    pid = _project(client)
    _accept(client, pid, [_good_case()])
    ids = [r["id"] for r in client.get(f"/api/projects/{pid}/testcases").json()]
    r = client.post(
        f"/api/projects/{pid}/testcases/status", json={"ids": ids, "status": "deleted"}
    )
    assert r.status_code == 400


def test_bulk_status_cannot_touch_another_project(client) -> None:
    """★ 跨项目越权：ids 是调用方给的，必须逐条校验归属。

    少了这条，一个项目可以改另一个项目的用例状态 —— 而项目隔离是这个仓库
    反复强调的底线（见 docs/PROJECT_ISOLATION.md）。
    """
    a = _project(client, "A")
    b = _project(client, "B")
    _accept(client, a, [_good_case("属于 A 的")])
    ids = [r["id"] for r in client.get(f"/api/projects/{a}/testcases").json()]

    r = client.post(f"/api/projects/{b}/testcases/status", json={"ids": ids, "status": "active"})
    assert r.status_code == 403
    # 并且真的没改动
    still = client.get(f"/api/projects/{a}/testcases").json()[0]
    assert still["status"] == "draft"


# ---------------------------------------------------------------------------
# 后端接口探索（2026-10-09）
# ---------------------------------------------------------------------------


def _seed_endpoints(pid: int, eps: list[tuple[str, str, bool]]) -> None:
    """直接往接口表里放几条，模拟"跑过一轮用例之后"的状态。"""
    import asyncio

    from app import db as dbmod
    from app.models import ApiEndpoint

    async def go():
        async with dbmod.db_session() as s:
            for method, path, writable in eps:
                s.add(
                    ApiEndpoint(
                        project_id=pid,
                        method=method,
                        path=path,
                        sample=f"http://h{path}",
                        query={"page": "int"} if path.endswith("list") else {},
                        body_fields=["qty"] if writable else [],
                        hits=7,
                        writable=writable,
                        origin="http://h",
                    )
                )

    asyncio.run(go())


def test_endpoint_list_is_empty_before_any_run_and_says_why(client) -> None:
    """★ 空清单必须解释**为什么**是空的。

    这是"观察式发现"的固有代价：没跑过用例就学不到接口。
    如果这里只回一个空表，用户会以为功能坏了 —— 而实际上只需要先去跑一轮。
    """
    pid = _project(client)
    body = client.get(f"/api/projects/{pid}/api-endpoints").json()
    assert body["endpoints"] == []
    assert body["total"] == 0
    assert "跑一轮" in body["note"] or "跑" in body["note"]


def test_endpoint_list_marks_writable_endpoints(client) -> None:
    """写接口必须能被识别出来 —— 它们只能看，不能被用例直接调用。"""
    pid = _project(client)
    _seed_endpoints(
        pid,
        [("GET", "/api/menu", False), ("GET", "/api/user/list", False), ("POST", "/api/order", True)],
    )
    body = client.get(f"/api/projects/{pid}/api-endpoints").json()
    assert body["total"] == 3
    assert "只读" in body["note"]
    by_path = {e["path"]: e for e in body["endpoints"]}
    assert by_path["/api/order"]["writable"] is True
    assert by_path["/api/menu"]["writable"] is False


def test_endpoint_list_can_filter_by_writability(client) -> None:
    pid = _project(client)
    _seed_endpoints(pid, [("GET", "/api/menu", False), ("POST", "/api/order", True)])
    only_read = client.get(f"/api/projects/{pid}/api-endpoints?writable=0").json()
    only_write = client.get(f"/api/projects/{pid}/api-endpoints?writable=1").json()
    assert [e["path"] for e in only_read["endpoints"]] == ["/api/menu"]
    assert [e["path"] for e in only_write["endpoints"]] == ["/api/order"]


def test_probe_refuses_write_methods(client) -> None:
    """★ 用户选定的边界：「探索只读，写操作必须显式声明」。

    POST 一旦发出去就可能在你测试环境里建/删真数据 —— 那代价不是
    "少拿到一点信息"能比的，所以必须硬拒。
    """
    pid = _project(client)
    _seed_endpoints(pid, [("POST", "/api/order", True)])
    r = client.post(
        f"/api/projects/{pid}/api-endpoints/probe",
        json={"method": "POST", "path": "/api/order"},
    )
    assert r.status_code == 403
    assert "只读" in r.text


def test_probe_refuses_a_path_that_was_never_observed(client) -> None:
    """★ 这条把"探索"和"扫接口"彻底分开。

    否则这个端点就变成一个可以打任意路径的探针 —— 拿着项目的登录态，
    对着后端随便试。用户要的是"探索"（从真实流量里学），不是端口扫描。
    """
    pid = _project(client)
    client.post("/api/projects", json={"name": "x"})
    r = client.post(
        f"/api/projects/{pid}/api-endpoints/probe",
        json={"method": "GET", "path": "/api/not-observed"},
    )
    assert r.status_code == 404
    assert "没观察到" in r.text or "已发现" in r.text


def test_probe_rejects_an_empty_path(client) -> None:
    pid = _project(client)
    r = client.post(f"/api/projects/{pid}/api-endpoints/probe", json={"method": "GET"})
    assert r.status_code == 400


def test_probe_needs_a_base_url_to_know_the_backend_origin(client) -> None:
    """不知道后端在哪就不该乱发请求 —— 报错要说清缺什么。"""
    pid = _project(client)
    _seed_endpoints(pid, [("GET", "/api/menu", False)])
    r = client.post(
        f"/api/projects/{pid}/api-endpoints/probe",
        json={"method": "GET", "path": "/api/menu"},
    )
    assert r.status_code == 400
    assert "base_url" in r.text


def test_endpoints_are_scoped_to_their_project(client) -> None:
    """项目隔离：A 项目的接口不该出现在 B 项目的清单里。"""
    a = _project(client, "A")
    b = _project(client, "B")
    _seed_endpoints(a, [("GET", "/api/menu", False)])
    assert client.get(f"/api/projects/{a}/api-endpoints").json()["total"] == 1
    assert client.get(f"/api/projects/{b}/api-endpoints").json()["total"] == 0
