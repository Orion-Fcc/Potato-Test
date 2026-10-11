"""核心 REST 接口的端到端覆盖（2026-10-09 新增）。

## 为什么加这一组

`pyproject.toml` 的覆盖率注释里点名的短板就是 `api.py`：1514 条语句、覆盖率 28%，
是**全项目最低**的一个文件 —— 而它恰好是界面所有动作的唯一入口。风险最高的地方
测试最少，这个组合不能留着。

这组用例走真实的 FastAPI 栈（TestClient），断言的是**契约**（状态码 + 落库结果），
不是实现细节：换个持久化方式、重命名内部函数，这些用例都不该红。

## 隔离

用 conftest 里会话级的临时库（`_session_writes_go_to_a_temp_db`）打底，本文件再自己
构造一个 engine 覆盖它，并**自证连的是临时库**（断言见 `client` fixture）。
理由见 docs/PROJECT_ISOLATION.md 第七节：本项目的 `DATABASE_URL` 环境变量切库写法
会静默失效，直接构造 engine 才是可靠的。
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def client():
    """TestClient + 一个只剩本测试自己的临时库。

    ★ 直接构造 engine 并赋值给 dbmod._engine / _Session，**不要**用 DATABASE_URL。
    `_ensure()` 把 engine 缓存在模块级全局变量里，一旦本进程更早的测试触发过它，
    改环境变量就完全无效 —— 测试会静默写进用户的真实 potato.db。
    """
    import asyncio
    import uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    # 每个测试一个**唯一**库文件名：固定名在 Windows 上会撞"文件被另一个进程占用"
    # （上一个测试的 engine 还没 dispose 完），表现为一串与产品无关的 WinError 32。
    dbname = f"_tp_api_crud_{uuid.uuid4().hex[:8]}.db"
    tmp = os.path.join(tempfile.gettempdir(), dbname)
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    # ★ 记下会话级的值，teardown 必须**还原成它**，不能置 None。
    #
    # 这里原来是 `dbmod._engine = None`。后果不是"本文件出问题"，而是**污染后续文件**：
    # 置 None 之后下一个用到 db 的测试会走 `_ensure()` 重新按 settings 建 engine ——
    # 而 settings 解析出来的是项目的 potato.db。实测症状：
    #   `test_api_crud.py` 先跑，`test_stale_run_reconcile.py` 就有 3 条 ERROR
    #   （连到了真库、表结构对不上）；单独跑后者则全绿。
    # 这类"顺序依赖"的失败最费时间，因为它指向错误的方向。
    # 写法与 docs/PROJECT_ISOLATION.md 第七节、conftest.tmp_db 一致。
    prev_engine, prev_session = dbmod._engine, dbmod._Session
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    from app.main import app

    # 没有这条断言，隔离失效时是静默的，而症状出现在用户的项目列表里。
    # 注意 SQLAlchemy 会把 URL 里的 "_" 之类字符做百分号编码，所以比对时统一解码，
    # 否则这条断言会因为"文件名被转义"而假红。
    from urllib.parse import unquote

    assert dbname in unquote(str(eng.url)), (
        f"测试没连到临时库，而是 {eng.url} —— 拒绝继续，避免污染真实数据"
    )

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


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------


def test_create_list_and_patch_project(client) -> None:
    r = client.post("/api/projects", json={"name": " 训练资源 ", "base_url": "http://example.invalid"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["name"] == "训练资源", "首尾空白要清掉，否则列表里会出现'看不出来'的重名"
    assert p["base_url"] == "http://example.invalid"
    pid = p["id"]

    listed = client.get("/api/projects").json()
    assert any(x["id"] == pid for x in listed), "新建的项目必须立刻出现在列表里"

    r = client.put(f"/api/projects/{pid}", json={"name": "改名了"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "改名了"

    # 改名的效果用**列表**核对。不要 GET /api/projects/{pid} —— 那个端点不存在
    # （前端只用列表 + PUT/DELETE），而未知的 /api GET 现在会被兜底路由回 404。
    after = next(x for x in client.get("/api/projects").json() if x["id"] == pid)
    assert after["name"] == "改名了"


def test_project_name_is_required(client) -> None:
    assert client.post("/api/projects", json={"name": ""}).status_code == 422


def test_patch_project_rejects_out_of_range_numbers(client) -> None:
    """边界要在 API 这层就拦下 —— 落到执行层就是一个跑到一半才炸的用例。

    case_max_steps 的下限是 3（1~2 步根本完不成任何真实用例），下限被漏掉的话
    用户会配出一个"每条用例必然步数耗尽"的项目。
    """
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    assert client.put(f"/api/projects/{pid}", json={"case_max_steps": 1}).status_code == 422
    assert client.put(f"/api/projects/{pid}", json={"run_concurrency": 0}).status_code == 422
    assert client.put(f"/api/projects/{pid}", json={"case_timeout_s": 5}).status_code == 422


def test_patch_project_can_unset_evidence_flags_to_follow_default(client) -> None:
    """显式传 null = 跟随全局默认，与"没传这个键"必须能区分开。

    区分不了的话，"改回跟随默认"这个操作就表达不出来 —— 用户点完看起来没反应。
    """
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    client.put(f"/api/projects/{pid}", json={"case_record_video": False})

    def _flag() -> object:
        return next(x for x in client.get("/api/projects").json() if x["id"] == pid)[
            "case_record_video"
        ]

    assert _flag() is False

    client.put(f"/api/projects/{pid}", json={"case_record_video": None})
    assert _flag() is None


def test_delete_project(client) -> None:
    pid = client.post("/api/projects", json={"name": "要删的"}).json()["id"]
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    # 删完之后它的子资源必须消失。**不要**用 GET /api/projects/{pid} 探测 —— 那个
    # 端点本来就不存在（前端只用列表 + PUT/DELETE），未匹配的 GET 会被 SPA 的
    # `/{full_path:path}` 兜底路由接走并回 index.html（200 + HTML），
    # 于是"端点不存在"看起来像"项目还在"。
    assert client.get(f"/api/projects/{pid}/testcases").status_code == 404
    assert all(x["id"] != pid for x in client.get("/api/projects").json())


def test_missing_project_is_404_not_500(client) -> None:
    """不存在的项目要回 404；回 500 会让界面弹一个"服务器错误"，指向错误方向。"""
    assert client.get("/api/projects/999999/testcases").status_code == 404
    assert client.get("/api/projects/999999/credentials").status_code == 404


def test_unknown_api_get_is_404_not_html(client) -> None:
    """★ 未匹配的 /api GET 必须回 404，**不许**被 SPA 兜底接走。

    这是 2026-10-09 加这组测试时发现的真实缺陷：兜底路由 `/{full_path:path}` 会接走
    所有未匹配的 GET，包括拼错的 /api 路径，于是调用方拿到 **200 + index.html**，
    以为请求成功，然后在 `res.json()` 上炸出 "Unexpected token '<'" ——
    一个完全不指向真实原因的报错。实测触发点：`GET /api/projects/999999`
    （该端点根本不存在）返回的就是 index.html + 200。
    """
    for path in ("/api/projects/999999", "/api/nope", "/api/projects/1/nope"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} 没回 404，而是 {r.status_code}"
        assert not r.text.lstrip().startswith("<"), f"{path} 回了 HTML，调用方会在 JSON 解析处炸"


# ---------------------------------------------------------------------------
# 用例（含编号分配：本项目栽过的重号缺陷）
# ---------------------------------------------------------------------------


def _new_project(client, name: str = "P") -> int:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _new_case(client, pid: int, **kw) -> dict:
    body = {"name": "用例", "prompt": "打开页面并确认标题", **kw}
    r = client.post(f"/api/projects/{pid}/testcases", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_case_key_is_auto_assigned_and_increases(client) -> None:
    pid = _new_project(client)
    a = _new_case(client, pid)
    b = _new_case(client, pid)
    assert a["case_key"] == "TC-001"
    assert b["case_key"] == "TC-002"


def test_case_key_does_not_get_reused_after_a_middle_delete(client) -> None:
    """删掉中间一条之后，新用例**不许**捡回那个空出来的编号。

    旧实现用 COUNT(*) 当序号：TC-001..TC-005 里删掉 TC-003 之后总数变成 4，
    下一条又拿到 TC-005 —— 而 TC-005 已经被占用，直接撞唯一约束；即便没撞，
    两条完全不同的用例共用一个编号也让报告和缺陷单分不清谁是谁。
    这里断言"取已用编号的最大值 +1"，序号只增不减。

    ⚠️ 已知边界（**不是**本用例要守的东西）：删掉的如果恰好是**最大的**那个编号
    （如只剩 TC-001、删掉 TC-002 再新建），max+1 会重新给出 TC-002。
    这是"最大值+1"算法的固有行为，用户 2026-10-04 选的正是这个方案
    （另一方案是给每个项目存一个永久计数器，要加列）。见 app/api.py::_next_case_key。
    """
    pid = _new_project(client)
    for _ in range(5):
        _new_case(client, pid)  # TC-001 .. TC-005
    rows = client.get(f"/api/projects/{pid}/testcases").json()
    third = next(x for x in rows if x["case_key"] == "TC-003")
    assert client.delete(f"/api/testcases/{third['id']}").status_code == 200

    c = _new_case(client, pid)
    assert c["case_key"] == "TC-006", f"编号被回收复用了：{c['case_key']}"


def test_case_keys_are_per_project_not_global(client) -> None:
    """每个项目都从 TC-001 开始 —— 这是设计如此，不是重号缺陷。"""
    p1 = _new_project(client, "A")
    p2 = _new_project(client, "B")
    assert _new_case(client, p1)["case_key"] == "TC-001"
    assert _new_case(client, p2)["case_key"] == "TC-001", "跨项目必须允许重名"


def test_explicit_case_key_is_kept_and_still_advances_the_counter(client) -> None:
    """手工指定 TC-050 之后，下一条自动编号必须是 TC-051 而不是 TC-002。

    否则手工编号会和自动编号撞车 —— 这正是"最大值+1"要解决的问题。
    """
    pid = _new_project(client)
    _new_case(client, pid, case_key="TC-050")
    assert _new_case(client, pid)["case_key"] == "TC-051"


def test_duplicate_case_key_in_one_project_is_rejected(client) -> None:
    """项目内重号由数据库唯一约束强制，不能只靠约定。

    而且必须回 **409 + 人话**：这里原先直接抛 IntegrityError → 500，
    用户看到的是"服务器错误"，真实原因却是"这个编号已经有人在用" ——
    一个他自己就能改的问题被报成了系统故障。
    """
    pid = _new_project(client)
    _new_case(client, pid, case_key="TC-007")
    r = client.post(
        f"/api/projects/{pid}/testcases",
        json={"name": "撞号的", "prompt": "x", "case_key": "TC-007"},
    )
    assert r.status_code == 409, f"重号没有回 409，而是 {r.status_code}: {r.text[:200]}"
    assert "TC-007" in r.text, "409 里要说清是哪个编号撞了，否则用户还得自己猜"

    # 撞号失败后不能留下半条脏数据。
    keys = [x["case_key"] for x in client.get(f"/api/projects/{pid}/testcases").json()]
    assert keys == ["TC-007"]


def test_case_can_be_patched_and_disabled(client) -> None:
    pid = _new_project(client)
    c = _new_case(client, pid)
    r = client.put(
        f"/api/testcases/{c['id']}",
        json={"name": "改了", "priority": "P0", "enabled": False, "tags": ["冒烟"]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "改了"
    assert out["priority"] == "P0"
    assert out["enabled"] is False
    assert out["tags"] == ["冒烟"]


def test_case_rejects_an_unknown_priority(client) -> None:
    pid = _new_project(client)
    r = client.post(
        f"/api/projects/{pid}/testcases", json={"name": "x", "prompt": "y", "priority": "P9"}
    )
    assert r.status_code == 422


def test_role_and_roles_are_normalized_together(client) -> None:
    """role 与 roles 的合并规则是"role 领跑、roles 补齐、去重保序"。

    顺序有意义（agent 按这个顺序切换身份），所以这里断言的是**顺序**，不是集合。
    """
    pid = _new_project(client)
    c = _new_case(client, pid, role="主管", roles=["审核员", "主管", "财务"])
    assert c["roles"] == ["主管", "审核员", "财务"]
    assert c["role"] == "主管"


def test_empty_roles_list_clears_the_roles(client) -> None:
    """roles=[] 是**明确传了**的空列表 = 把角色清空（回落成"不需要特定身份"）。

    它与"没传这个键"必须能区分开 —— `exclude_none=True` 会留下 []（空列表不是 None），
    这正是让"清空"能表达出来的机制。以前多叠了一个 `body.roles is not None` 的回落
    分支，于是清空失效、角色又冒回来。
    """
    pid = _new_project(client)
    c = _new_case(client, pid, role="审核员")
    assert c["roles"] == ["审核员"]

    r = client.put(f"/api/testcases/{c['id']}", json={"roles": []})
    assert r.status_code == 200
    assert r.json()["roles"] == [], "明确的空列表被回落成了旧角色，清空失效"
    assert r.json()["role"] is None


def test_case_list_returns_every_case_of_the_project(client) -> None:
    """列表按 id 升序返回项目内全部用例，含已禁用的。

    `list_cases` 现在**不接任何筛选参数**（module / enabled 都不支持），
    筛选在前端做。这条把契约钉住：将来要加服务端筛选，就得一并改这里，
    而不是让前端继续以为参数生效、静默拿到全量。
    """
    pid = _new_project(client)
    _new_case(client, pid, name="甲", module="登录")
    _new_case(client, pid, name="乙", module="审批", enabled=False)

    rows = client.get(f"/api/projects/{pid}/testcases").json()
    assert [x["name"] for x in rows] == ["甲", "乙"], "列表要按 id 稳定升序"
    assert rows[1]["enabled"] is False, "已禁用的用例也要在列表里（前端要能改回来）"


def test_testcases_of_another_project_are_not_visible(client) -> None:
    """隔离底线：A 项目的用例列表里绝不能出现 B 项目的用例。"""
    p1 = _new_project(client, "A")
    p2 = _new_project(client, "B")
    _new_case(client, p1, name="A的用例")
    _new_case(client, p2, name="B的用例")

    names = [x["name"] for x in client.get(f"/api/projects/{p1}/testcases").json()]
    assert names == ["A的用例"]


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


def test_environment_crud_and_default_flag_is_exclusive(client) -> None:
    """is_default 只能有一个：把第二个设为默认时，第一个必须自动让位。"""
    pid = _new_project(client)
    e1 = client.post(
        f"/api/projects/{pid}/environments",
        json={"name": "测试", "base_url": "http://a.invalid", "is_default": True},
    ).json()
    e2 = client.post(
        f"/api/projects/{pid}/environments",
        json={"name": "预发", "base_url": "http://b.invalid", "is_default": True},
    ).json()

    rows = {x["id"]: x for x in client.get(f"/api/projects/{pid}/environments").json()}
    assert rows[e2["id"]]["is_default"] is True
    assert rows[e1["id"]]["is_default"] is False, "出现了两个默认环境"

    r = client.put(f"/api/environments/{e1['id']}", json={"base_url": "http://c.invalid"})
    assert r.status_code == 200
    assert r.json()["base_url"] == "http://c.invalid"

    assert client.delete(f"/api/environments/{e2['id']}").status_code == 200
    assert len(client.get(f"/api/projects/{pid}/environments").json()) == 1


# ---------------------------------------------------------------------------
# 凭据（密码必须加密落库）
# ---------------------------------------------------------------------------


def test_password_credential_is_stored_encrypted_and_never_echoed(client) -> None:
    """明文密码不能落库、也不能回显 —— 这是本项目唯一的"绝不能错"的地方。"""
    pid = _new_project(client)
    r = client.post(
        f"/api/projects/{pid}/credentials",
        json={"type": "password", "label": "审核员", "username": "u1", "secret": "hunter2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "hunter2" not in str(body), f"响应里回显了明文密码：{body}"
    cid = body["id"]

    listed = str(client.get(f"/api/projects/{pid}/credentials").json())
    assert "hunter2" not in listed, "列表接口泄露了明文密码"

    # 直接查库：存的是密文，且能解回原文。
    import asyncio

    from app import db as dbmod
    from app.crypto import decrypt
    from app.models import Credential

    async def _read():
        async with dbmod.db_session() as s:
            return (await s.get(Credential, cid)).secret

    stored = asyncio.run(_read())
    assert stored != "hunter2", "密码是明文落库的"
    assert decrypt(stored) == "hunter2", "加解密不是可逆的，登录会失败"


def test_credential_can_be_patched_without_retyping_the_password(client) -> None:
    """改标签不该逼用户重打密码（密码轮换是常规运维动作，不是"删了重建"）。"""
    pid = _new_project(client)
    c = client.post(
        f"/api/projects/{pid}/credentials",
        json={"type": "password", "label": "旧名", "username": "u1", "secret": "pw1"},
    ).json()

    r = client.patch(f"/api/credentials/{c['id']}", json={"label": "新名"})
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "新名"

    import asyncio

    from app import db as dbmod
    from app.crypto import decrypt
    from app.models import Credential

    async def _read():
        async with dbmod.db_session() as s:
            return (await s.get(Credential, c["id"])).secret

    assert decrypt(asyncio.run(_read())) == "pw1", "只改标签却把密码改掉了"


def test_credential_delete(client) -> None:
    pid = _new_project(client)
    c = client.post(
        f"/api/projects/{pid}/credentials",
        json={"type": "password", "label": "x", "username": "u", "secret": "p"},
    ).json()
    assert client.delete(f"/api/credentials/{c['id']}").status_code == 200
    assert client.get(f"/api/projects/{pid}/credentials").json() == []


def test_password_credential_requires_a_username(client) -> None:
    """没有用户名的密码账号是登录不了的，应当在这一层就拒掉。"""
    pid = _new_project(client)
    r = client.post(
        f"/api/projects/{pid}/credentials",
        json={"type": "password", "label": "x", "secret": "p"},
    )
    assert r.status_code in (400, 422), r.text


# ---------------------------------------------------------------------------
# 运行与结果
# ---------------------------------------------------------------------------


def test_create_run_records_the_case_set_and_trigger(client) -> None:
    pid = _new_project(client)
    c1 = _new_case(client, pid)
    c2 = _new_case(client, pid)

    r = client.post(
        f"/api/projects/{pid}/runs",
        json={"name": "回归", "case_ids": [c1["id"], c2["id"]], "concurrency": 1},
    )
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["total_count"] == 2
    assert sorted(run["case_ids"]) == sorted([c1["id"], c2["id"]])
    assert run["project_id"] == pid

    listed = client.get(f"/api/projects/{pid}/runs").json()
    assert any(x["id"] == run["id"] for x in listed)


def test_run_with_no_cases_is_rejected(client) -> None:
    """空运行的语义是"跑了个寂寞"，应在入口挡掉而不是产出一个 0/0 的报告。"""
    pid = _new_project(client)
    r = client.post(f"/api/projects/{pid}/runs", json={"case_ids": []})
    assert r.status_code in (400, 409, 422), r.text


def test_run_patch_and_delete(client) -> None:
    pid = _new_project(client)
    c = _new_case(client, pid)
    run = client.post(f"/api/projects/{pid}/runs", json={"case_ids": [c["id"]]}).json()

    r = client.patch(f"/api/runs/{run['id']}", json={"name": "改过的名字"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "改过的名字"

    assert client.delete(f"/api/runs/{run['id']}").status_code == 200
    assert client.get(f"/api/runs/{run['id']}").status_code == 404


def _seed_result(client, pid: int, run_id: int, case_id: int, status: str) -> int:
    """直接落一条 run_result（不跑浏览器），供改判/统计类用例使用。"""
    import asyncio

    from app import db as dbmod
    from app.models import Run, RunResult

    async def _go():
        async with dbmod.db_session() as s:
            run = await s.get(Run, run_id)
            run.passed_count = 1 if status == "passed" else 0
            run.failed_count = 1 if status == "failed" else 0
            row = RunResult(run_id=run_id, case_id=case_id, status=status, final_answer="agent 说做完了")
            s.add(row)
            await s.commit()
            return row.id

    return asyncio.run(_go())


def test_override_result_marks_it_as_human_and_keeps_the_ai_verdict(client) -> None:
    """改判要写进 status（报表/筛选自动跟着走），同时留一份 AI 原判供撤销。"""
    pid = _new_project(client)
    c = _new_case(client, pid)
    run = client.post(f"/api/projects/{pid}/runs", json={"case_ids": [c["id"]]}).json()
    rid = _seed_result(client, pid, run["id"], c["id"], "failed")

    r = client.patch(f"/api/results/{rid}", json={"status": "passed", "reason": "人工核对确实通过"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "passed"
    assert out["verdict_override"] == "passed"
    assert out["original_status"] == "failed"
    assert out["override_reason"] == "人工核对确实通过"


def test_override_can_be_cleared_back_to_the_ai_verdict(client) -> None:
    """撤销改判要回到 AI 的原结论 —— 而不是把结论留在一个编造的值上。"""
    pid = _new_project(client)
    c = _new_case(client, pid)
    run = client.post(f"/api/projects/{pid}/runs", json={"case_ids": [c["id"]]}).json()
    rid = _seed_result(client, pid, run["id"], c["id"], "error")

    client.patch(f"/api/results/{rid}", json={"status": "passed"})
    r = client.patch(f"/api/results/{rid}", json={"clear": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "error", "撤销后没有回到 AI 原判"
    assert out["verdict_override"] is None
    assert out["original_status"] is None


def test_override_rejects_a_bogus_status(client) -> None:
    pid = _new_project(client)
    c = _new_case(client, pid)
    run = client.post(f"/api/projects/{pid}/runs", json={"case_ids": [c["id"]]}).json()
    rid = _seed_result(client, pid, run["id"], c["id"], "failed")
    assert client.patch(f"/api/results/{rid}", json={"status": "maybe"}).status_code == 422


def test_results_are_listed_for_a_run(client) -> None:
    pid = _new_project(client)
    c = _new_case(client, pid)
    run = client.post(f"/api/projects/{pid}/runs", json={"case_ids": [c["id"]]}).json()
    _seed_result(client, pid, run["id"], c["id"], "passed")

    rows = client.get(f"/api/runs/{run['id']}/results").json()
    assert len(rows) == 1
    assert rows[0]["status"] == "passed"


def test_running_a_case_does_not_count_as_a_write_to_the_real_db(client) -> None:
    """这条是**元断言**：确认本文件自己的隔离仍然有效。

    它证明 client fixture 的 engine 确实指向临时库 —— 上面那些写操作
    一条都没有落到用户的 potato.db 上。
    """
    from urllib.parse import unquote

    from app import db as dbmod

    assert dbmod._engine is not None
    assert "_tp_api_crud_" in unquote(str(dbmod._engine.url))
