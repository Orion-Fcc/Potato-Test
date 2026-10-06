"""就地修改既有凭据（PATCH /api/credentials/{cid}）。

为什么要有这个文件：此前一条凭据只有「新建 / 激活 / 重新检测 / 删除」四条路，
**没有任何更新接口**，所以界面上根本没有"改密码"这个操作 —— 想改密码只能
先删掉再建一条。而删掉一条凭据会连带丢掉它的 role、environment、会话缓存
（`session_bundle`），还得重走一遍登录验证。

密码轮换是常规运维动作（当前就有 13 个 role 账号要统一改密码），
它必须是一条独立、可重复执行的操作。这个文件把它的行为钉住。

两条最容易被忽略、但必须守住的行为：
1. `model_fields_set` 语义 —— 没传的字段保持原值，传 null 才是清空。
   用 `exclude_none=True` 实现会把"清空 role"和"没提 role"混成一件事。
2. 改密码/改用户名**必须丢弃 session_bundle**。缓存里存的是上一个身份的登录态，
   留着就是让用例跑在别人的身份下 —— 与 _enforce_identity 修的是同一类污染。

python -m pytest tests/test_credential_edit.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest


@pytest.fixture()
def client():
    """FastAPI TestClient + 临时库。

    ★ 直接构造 engine 并赋值给 dbmod._engine / _Session，**不要**用 DATABASE_URL 环境变量。
    原因见 test_isolation_and_policy.py 那段长注释：_ensure() 会把 engine 缓存在模块级
    全局变量里，一旦本进程更早的测试触发过 _ensure()，改环境变量就完全无效 ——
    测试会静默写进用户的真实 potato.db。所以这里还断言了 URL 含临时库文件名。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    tmp = os.path.join(tempfile.gettempdir(), "_tp_cred_edit_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    from app.main import app

    assert "_tp_cred_edit_test.db" in str(eng.url), (
        f"测试没连到临时库，而是 {eng.url} —— 拒绝继续，避免污染真实数据"
    )

    with TestClient(app) as c:
        yield c

    asyncio.run(eng.dispose())
    dbmod._engine = None
    dbmod._Session = None
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def _snapshot(cid: int) -> dict | None:
    """把一条凭据的原始列（含密文与会话缓存）读出来，供断言使用。

    在 session 内取完值再返回纯 dict —— 出了 db_session 再读 ORM 属性会 DetachedInstanceError。
    """
    from app import db as dbmod
    from app.models import Credential

    async def go():
        async with dbmod._Session() as s:
            c = await s.get(Credential, cid)
            if c is None:
                return None
            return {
                "secret": c.secret,
                "session_bundle": c.session_bundle,
                "label": c.label,
                "username": c.username,
                "role": c.role,
                "environment_id": c.environment_id,
            }

    return asyncio.run(go())


def _seed(client, **over) -> tuple[int, int]:
    """建一个项目 + 一条角色密码账号，并伪造一个会话缓存（模拟"已经抓过一次会话"）。"""
    pid = client.post("/api/projects", json={"name": "P", "base_url": "http://x.invalid/"}).json()["id"]
    body = {
        "type": "password",
        "role": "role01",
        "label": "role01",
        "username": "role01",
        "secret": "old-password",
        "environment_id": None,
    }
    body.update(over)
    for k, v in list(body.items()):
        if v is None:
            body.pop(k)
    cid = client.post(f"/api/projects/{pid}/credentials", json=body).json()["id"]

    from app import db as dbmod
    from app.models import Credential

    async def fake_bundle():
        async with dbmod._Session() as s:
            c = await s.get(Credential, cid)
            c.session_bundle = '{"cookies": [{"name": "OLD_SESSION"}]}'
            await s.commit()

    asyncio.run(fake_bundle())
    return pid, cid


def test_patch_changes_the_password_in_place(client):
    """核心用例：改密码后，还是同一条凭据（id 不变），只是密文变了。"""
    from app.crypto import decrypt

    _, cid = _seed(client)
    r = client.patch(f"/api/credentials/{cid}", json={"secret": "admin123"})

    assert r.status_code == 200, r.text
    assert r.json()["id"] == cid, "必须是就地修改，不能是新建一条"
    assert decrypt(_snapshot(cid)["secret"]) == "admin123"
    assert r.json()["role"] == "role01", "没传的字段必须保持原值"


def test_response_never_leaks_the_secret(client):
    """密文/明文都不能出现在响应里。"""
    _, cid = _seed(client)
    body = client.patch(f"/api/credentials/{cid}", json={"secret": "admin123"}).json()

    assert "secret" not in body
    assert "admin123" not in str(body)


def test_patch_without_secret_keeps_the_password(client):
    """留空 = 不改密码。这是界面上"不填就不动"语义的服务端一半。"""
    from app.crypto import decrypt

    _, cid = _seed(client)
    client.patch(f"/api/credentials/{cid}", json={"label": "改名"})

    assert decrypt(_snapshot(cid)["secret"]) == "old-password"


def test_changing_the_password_drops_the_cached_session(client):
    """缓存的是"用旧密码登录下来的会话"，密码一换就必须作废。

    留着它的后果就是本轮修过的那类身份污染：用例恢复了一个属于别人
    （或根本无效）的登录态，报告里却完全看不出来。
    """
    _, cid = _seed(client)
    assert _snapshot(cid)["session_bundle"] is not None, "前置：先要有缓存才会被测到"

    client.patch(f"/api/credentials/{cid}", json={"secret": "admin123"})

    assert _snapshot(cid)["session_bundle"] is None


def test_changing_the_username_drops_the_cached_session(client):
    """换用户名 = 换了一个人，旧缓存必然属于上一个人。"""
    _, cid = _seed(client)
    client.patch(f"/api/credentials/{cid}", json={"username": "role02"})

    snap = _snapshot(cid)
    assert snap["username"] == "role02"
    assert snap["session_bundle"] is None


def test_editing_only_the_label_keeps_the_cached_session(client):
    """反向：只改标签不该白扔一个有效会话（代价是下次运行必须重新登录）。"""
    _, cid = _seed(client)
    client.patch(f"/api/credentials/{cid}", json={"label": "新标签"})

    assert _snapshot(cid)["session_bundle"] is not None


def test_explicit_null_clears_the_role_and_environment(client):
    """传 null = 清空。用 exclude_none 实现会把"清空"和"没提"混成一件事。"""
    pid, cid = _seed(client, role="role01", environment_id=None)

    r = client.patch(f"/api/credentials/{cid}", json={"role": None})

    assert r.status_code == 200, r.text
    assert _snapshot(cid)["role"] is None


def test_omitted_role_is_left_alone(client):
    """没传 role 时绝不能顺手清掉 —— 那会静默破坏多角色绑定。"""
    _, cid = _seed(client)
    client.patch(f"/api/credentials/{cid}", json={"secret": "admin123"})

    assert _snapshot(cid)["role"] == "role01"


def test_patch_can_move_the_account_to_another_role(client):
    """轮换账号用途（换绑角色）是常见诉求，必须支持。"""
    _, cid = _seed(client)
    r = client.patch(f"/api/credentials/{cid}", json={"role": "approver-train"})

    assert r.json()["role"] == "approver-train"
    assert _snapshot(cid)["role"] == "approver-train"


def test_username_cannot_be_emptied_on_a_password_account(client):
    """密码账号没有用户名就没法登录。拒绝，且**不能留下半截修改**。"""
    from app.crypto import decrypt

    _, cid = _seed(client)
    r = client.patch(f"/api/credentials/{cid}", json={"username": "", "secret": "admin123"})

    assert r.status_code == 400, r.text
    snap = _snapshot(cid)
    assert snap["username"] == "role01", "被拒的请求不能改到用户名"
    assert decrypt(snap["secret"]) == "old-password", "被拒的请求不能改到密码"


def test_patching_a_missing_credential_is_404(client):
    assert client.patch("/api/credentials/999999", json={"secret": "x"}).status_code == 404


def test_empty_password_is_rejected(client):
    """空串不是"改成空密码"，而是调用方写错了。min_length=1 拦在 schema 层。"""
    _, cid = _seed(client)
    assert client.patch(f"/api/credentials/{cid}", json={"secret": ""}).status_code == 422


def test_the_update_route_is_registered():
    """回归护栏：这条路由是本次新加的，别在后续重构里被删掉。

    删掉的症状很隐蔽 —— 界面上"编辑"按钮会静默变成 404，而不是编译报错。
    注意要查 `app.api.router.routes`：当前 FastAPI 把 `include_router` 实现成
    `_IncludedRouter` 包装，路由不会再被拍平进 `app.routes`（那里只有一个包装对象，
    path 是 None）。查 app.routes 会得到"路由不存在"的假结论。
    """
    from app.api import router

    found = {
        (r.path, m)
        for r in router.routes
        for m in getattr(r, "methods", set()) or set()
    }
    assert ("/api/credentials/{cid}", "PATCH") in found, "凭据更新接口不见了"
