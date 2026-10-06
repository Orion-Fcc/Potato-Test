"""2026-10-04 多角色执行：单元测试。

覆盖三条最容易回归的点：
  1. 归一化 —— 存量单角色用例必须解析成单元素列表（557 条存量零改动）
  2. 单角色用例的动作集/提示词**完全不变**（多角色功能不得污染存量路径）
  3. 多角色才注册 switch_account，且提示词说清何时切
"""

import sys

import pytest

sys.path.insert(0, "<PROJECT_DIR>")

from app.executor import CaseSpec, _add_switch_account, _build_tools, build_task  # noqa: E402
from app.schemas import _roles_of  # noqa: E402
from app.engine import _case_roles  # noqa: E402


class _Case:
    """最小 TestCase 替身：_case_roles 只读这两个属性。"""

    def __init__(self, role=None, roles=None):
        self.role = role
        self.roles = roles


# ---------------------------------------------------------------- 归一化


@pytest.mark.parametrize(
    "role,roles,want",
    [
        (None, [], []),
        ("admin", [], ["admin"]),  # ★存量 557 条全走这条
        (None, ["a", "b"], ["a", "b"]),
        ("a", ["a", "b"], ["a", "b"]),
        ("  admin  ", ["  admin  "], ["admin"]),
        ("a", ["a", "a", "b"], ["a", "b"]),
        ("", ["x"], ["x"]),
        (None, [""], []),
    ],
)
def test_roles_of_normalizes(role, roles, want):
    assert _roles_of(role, roles) == want


@pytest.mark.parametrize(
    "role,roles,want",
    [
        ("admin", None, ["admin"]),
        (None, ["a", "b"], ["a", "b"]),
        ("a", ["a", "b"], ["a", "b"]),
        ("a", ["b"], ["b", "a"]),  # roles 保序，role 补在后面
        (None, None, []),
        ("a", ["a", "a"], ["a"]),  # 去重：否则会重复租同一个账号
    ],
)
def test_case_roles_same_rules(role, roles, want):
    assert _case_roles(_Case(role, roles)) == want


def test_case_roles_legacy_case_is_unchanged():
    """存量单角色用例：解析结果只有一个身份，执行链路与改动前逐字节相同。"""
    assert _case_roles(_Case(role="sittrapply", roles=None)) == ["sittrapply"]


def test_case_roles_dedupes_to_avoid_self_deadlock():
    """重复角色会让 leasing 把同一个账号租两次，租约互相等待 → 用例自锁。"""
    assert _case_roles(_Case(role="a", roles=["a"])) == ["a"]


# ------------------------------------------- 单角色用例零影响（核心回归点）


def test_single_role_prompt_mentions_no_switching():
    """单角色用例的提示词不能出现 MULTIPLE / switch_account ——
    否则模型会尝试调用一个不存在的工具，白烧步数。"""
    spec = CaseSpec(case_id=1, prompt="提交申请", login_username="u", login_password="p")
    task = build_task(spec, "中文")
    assert "MULTIPLE" not in task
    assert "switch_account" not in task


def test_multi_role_prompt_explains_the_switch():
    spec = CaseSpec(
        case_id=2,
        prompt="提交后由他人审批",
        extra_role_logins=[
            {"role": "approver", "bundle": "{}", "user": None, "password": None, "label": "approver · A"},
        ],
    )
    task = build_task(spec, "中文")
    assert "MULTIPLE" in task
    assert "switch_account" in task
    assert "approver" in task


def test_extra_role_logins_defaults_empty():
    assert CaseSpec(case_id=1, prompt="x").extra_role_logins == []


# ------------------------------------------------------------ 工具注册


def _actions(tools):
    return set(tools.registry.registry.actions.keys())


def test_switch_account_not_registered_for_single_role():
    from browser_use import Tools

    t = Tools()
    _add_switch_account(t, CaseSpec(case_id=1, prompt="x"))
    assert "switch_account" not in _actions(t)


def test_switch_account_registered_for_multi_role():
    from browser_use import Tools

    t = Tools()
    _add_switch_account(
        t,
        CaseSpec(
            case_id=1,
            prompt="x",
            extra_role_logins=[
                {"role": "approver", "bundle": "{}", "user": None, "password": None, "label": "a"},
            ],
        ),
    )
    assert "switch_account" in _actions(t)


def test_build_tools_keeps_old_single_arg_call():
    """tests/test_speed_levers.py 直接调 _build_tools(settings)，签名不能被破坏。"""
    from app.config import get_settings

    s = get_settings()
    assert _build_tools(s) is not None
    assert _build_tools(s, None) is not None
    assert _build_tools(s, CaseSpec(case_id=1, prompt="x")) is not None


def test_register_failure_does_not_raise():
    """工具注册失败绝不能带崩用例（宁可没有切换功能）。"""

    class _Boom:
        def action(self, *a, **k):
            raise RuntimeError("no such API")

    # 不抛异常即通过
    _add_switch_account(_Boom(), CaseSpec(
        case_id=1, prompt="x",
        extra_role_logins=[{"role": "r", "bundle": "{}", "user": None, "password": None, "label": "l"}],
    ))


# ------------------------------------------- 接口层归一化（端到端抓过的两个 bug）


@pytest.fixture()
def client():
    """FastAPI TestClient + 临时库，不碰真实 potato.db。

    ★ 2026-10-04 踩过的坑：只设 DATABASE_URL 环境变量是**不够的**。
    app/db.py 的 _ensure() 会把 engine/sessionmaker 缓存在模块级全局变量里，
    一旦本次 pytest 进程里更早的测试已经触发过 _ensure()，engine 就绑死在
    真实 potato.db 上了 —— 之后再改环境变量毫无作用，测试建的用例会全部
    落进用户的真实库（实测污染了 6 个项目 / 6 条用例）。
    所以这里必须把缓存显式清掉，再改 URL，再 init_db()。
    """
    import os
    import tempfile

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    tmp = os.path.join(tempfile.gettempdir(), "_tp_multirole_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)

    import asyncio

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    # 直接构造 engine，不走环境变量 —— 见 test_isolation_and_policy.py 里
    # 那段长注释：环境变量 + _ensure() 缓存 + .env 优先级三者叠加，会让
    # "设了 DATABASE_URL 却仍连真实库"，静默污染用户数据。
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

    # 自证隔离有效：连错库就直接失败，绝不静默污染真实数据
    assert "_tp_multirole_test.db" in str(eng.url), (
        f"测试没连到临时库，而是 {eng.url} —— 拒绝继续，避免污染真实数据"
    )

    with TestClient(app) as c:
        yield c

    # 收尾：断开临时库，并把缓存清干净，免得污染后续测试
    asyncio.run(eng.dispose())
    dbmod._engine = None
    dbmod._Session = None
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def _mk(client, **kw):
    r = client.post("/api/projects", json={"name": "P", "base_url": " http://x.invalid/"})
    pid = r.json()["id"]
    return pid, client.post(
        f"/api/projects/{pid}/testcases", json={"name": "c", "prompt": "p", **kw}
    ).json()


def test_api_single_role_becomes_one_element_list(client):
    """存量形态：只传 role。响应里 roles 必须是 [role]，前端才不用特判。"""
    _, d = _mk(client, role="admin")
    assert d["roles"] == ["admin"]
    assert d["role"] == "admin"


def test_api_multi_role_preserves_order(client):
    _, d = _mk(client, roles=["applicant", "approver"])
    assert d["roles"] == ["applicant", "approver"]
    assert d["role"] == "applicant", "role must mirror roles[0]"


def test_api_update_to_multi_role_does_not_keep_old_role_first(client):
    """回归：把单角色用例改成多角色时，**旧 role 不得被排到最前**。

    这是端到端测出来的真 bug —— 旧实现把 c.role 当缺省值混进归一化，
    结果用户设的 [applicant, hr, admin] 被存成 [admin, applicant, hr]，
    执行顺序完全错。"""
    _, d = _mk(client, role="admin")
    cid = d["id"]
    r = client.put(f"/api/testcases/{cid}", json={"roles": ["applicant", "hr", "admin"]})
    assert r.json()["roles"] == ["applicant", "hr", "admin"]
    assert r.json()["role"] == "applicant"


def test_api_update_clearing_roles_falls_back_to_default_account(client):
    """回归：roles=[] 表示"回到默认账号"，之前因为回落到旧值而清不掉。"""
    _, d = _mk(client, roles=["applicant", "approver"])
    cid = d["id"]
    r = client.put(f"/api/testcases/{cid}", json={"roles": []})
    assert r.json()["roles"] == []
    assert r.json()["role"] is None


def test_api_update_leaves_roles_untouched_when_not_sent(client):
    """没传 roles/role 时不能动这两列（前端保存其它字段的常见路径）。"""
    _, d = _mk(client, roles=["a", "b"])
    cid = d["id"]
    r = client.put(f"/api/testcases/{cid}", json={"name": "renamed"})
    assert r.json()["roles"] == ["a", "b"]
    assert r.json()["role"] == "a"


def test_api_dedupes_repeated_roles(client):
    _, d = _mk(client, roles=["a", "a", "b"])
    assert d["roles"] == ["a", "b"]
