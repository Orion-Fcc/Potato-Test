"""身份守卫（browser_identity_guard）的行为契约。

背景（2026-10-06 实测）：持久 profile 里残留着上一个登录者的会话。项目 2 的
profile 里躺着 admin 的 user / loginForm / ACCESS_TOKEN，导致 179 条用例全部以
admin 身份执行 —— 用例"通过"了，但结论是假的。这组测试钉住三件事：

1. 身份不一致时**一定**清掉（含登录框的"记住密码"预填）
2. 身份一致时**绝不动**手（保住 profile 的预热收益）
3. 读不到身份时当成"无法确认"，不误清、也不谎报

守卫永不抛异常 —— 一个会让用例直接失败的守卫比没有守卫更糟。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.executor import (
    _IDENTITY_KEYS,
    _IDENTITY_PURGE_JS,
    _IDENTITY_READ_JS,
    _enforce_identity,
    _read_identity,
)


class _Runtime:
    """只实现 Runtime.evaluate，按脚本内容返回预设值。"""

    def __init__(self, client):
        self._client = client

    async def evaluate(self, params=None, session_id=None):
        expr = (params or {}).get("expression", "")
        client = self._client
        client.calls.append(expr)
        if client.fail:
            raise RuntimeError("cdp down")
        if expr == _IDENTITY_READ_JS:
            payload = json.dumps(
                {
                    "who": client.storage.get("who", ""),
                    "form": client.storage.get("form", ""),
                    "token": "yes" if client.storage.get("token") else "no",
                }
            )
        elif expr == _IDENTITY_PURGE_JS:
            # 真实语义：删掉存在的键，返回删除数量
            n = 0
            for k in _IDENTITY_KEYS:
                for area in ("ls", "ss"):
                    if client.storage.get(f"{area}:{k}"):
                        client.storage[f"{area}:{k}"] = False
                        n += 1
            payload = str(n)
        else:
            payload = ""
        return {"result": {"value": payload}}


class _Send:
    def __init__(self, client):
        self.Runtime = _Runtime(client)


class _FakeCdpClient:
    def __init__(self, storage: dict | None, fail: bool = False):
        self.storage = storage or {}
        self.fail = fail
        self.calls: list[str] = []
        self.send = _Send(self)


class _FakeSession:
    def __init__(self, client):
        self.cdp_client = client
        self.session_id = "sid"


class _FakeBrowser:
    def __init__(self, storage=None, start_ok=True, cdp_ok=True):
        self.client = _FakeCdpClient(storage, fail=not cdp_ok)
        self.start_ok = start_ok
        self.cdp_ok = cdp_ok
        self.navigations: list[str] = []

    async def start(self):
        if not self.start_ok:
            raise RuntimeError("cannot launch")

    async def get_or_create_cdp_session(self):
        if not self.cdp_ok:
            raise RuntimeError("no session")
        return _FakeSession(self.client)

    async def navigate_to(self, url):
        self.navigations.append(url)


def _storage(who="", form="", token=False, keys=("user", "loginForm")):
    st = {"who": who, "form": form, "token": token}
    for k in keys:
        st[f"ls:{k}"] = True
        st[f"ss:{k}"] = True
    return st


# ---- 1. 身份不一致时必须清掉 -------------------------------------------------


def test_purges_when_another_user_is_logged_in():
    """现场就是这条：profile 里是 admin，用例该用 role01。"""
    b = _FakeBrowser(_storage(who="admin", form="admin", token=True))
    verdict = asyncio.run(_enforce_identity(b, "role01", "http://x/login"))
    assert verdict.startswith("purged("), verdict
    assert "admin" in verdict and "role01" in verdict
    # 清完必须重新进一次页面，否则当前文档仍渲染着旧身份
    assert len(b.navigations) == 2


def test_purges_stale_prefill_even_without_a_session():
    """没有登录态，但登录框被「记住密码」预填成 admin —— 一样要清。

    否则 agent 走到登录页会照着预填值提交，结果还是 admin。
    """
    b = _FakeBrowser(_storage(who="", form="admin"))
    verdict = asyncio.run(_enforce_identity(b, "role01", "http://x/login"))
    assert verdict.startswith("purged("), verdict


def test_purge_removes_every_identity_key():
    """清就要清干净：user/loginForm/两个 token/菜单树，一个都不能留。"""
    for k in ("user", "loginForm", "ACCESS_TOKEN", "REFRESH_TOKEN", "roleRouters"):
        assert k in _IDENTITY_KEYS, f"{k} 必须在清理清单里 —— 留着就还是别人的身份"
    assert _IDENTITY_PURGE_JS.count("removeItem") == 2  # localStorage + sessionStorage


# ---- 2. 身份一致时绝不动手 ---------------------------------------------------


def test_keeps_a_matching_identity():
    """已经是本人 → 一个键都不删，profile 预热白赚。"""
    b = _FakeBrowser(_storage(who="role01", form="role01", token=True))
    verdict = asyncio.run(_enforce_identity(b, "role01", "http://x/login"))
    assert verdict == "ok(role01)", verdict
    # 只读了一次身份，没有执行清理脚本
    assert all(c != _IDENTITY_PURGE_JS for c in b.client.calls)


def test_no_session_and_no_prefill_is_a_no_op():
    b = _FakeBrowser(_storage(who="", form=""))
    verdict = asyncio.run(_enforce_identity(b, "role01", "http://x/login"))
    assert verdict == "no-session", verdict


def test_never_purges_when_only_the_prefill_matches():
    """登录态为空、但预填就是本人 → 视为正常，不折腾。"""
    b = _FakeBrowser(_storage(who="", form="role01"))
    verdict = asyncio.run(_enforce_identity(b, "role01", "http://x/login"))
    assert verdict == "ok-prefill(role01)", verdict


# ---- 3. 无法确认时：跳过 / 降级，不谎报也不误清 ------------------------------


def test_skips_without_an_expected_account():
    """纯 storage_state 账号没有用户名可比 —— 没得比就不动手。"""
    assert (
        asyncio.run(_enforce_identity(_FakeBrowser(), None, "http://x")).startswith("skip")
    )
    assert (
        asyncio.run(_enforce_identity(_FakeBrowser(), "role01", None)).startswith("skip")
    )


@pytest.mark.parametrize("broken", ["start", "cdp"])
def test_never_raises_when_the_browser_misbehaves(broken):
    """守卫挂了必须降级放行 —— 用例该跑还得跑，只是失去这层保护。"""
    b = _FakeBrowser(start_ok=(broken != "start"), cdp_ok=(broken != "cdp"))
    verdict = asyncio.run(_enforce_identity(b, "role01", "http://x/login"))
    assert "failed" in verdict or "no-cdp" in verdict, verdict


def test_unreadable_identity_is_reported_not_guessed():
    """读不到身份返回空串，调用方要当成「无法确认」，不能当成「没问题」。"""
    b = _FakeBrowser()
    b.client.fail = True
    assert _read_identity is not None
    assert asyncio.run(_read_identity(b)) == ""


def test_read_identity_parses_the_nested_shape():
    """真实的存储形状是 {v: "{...}"} 再套一层 {user:{username}}。"""
    b = _FakeBrowser(_storage(who="admin"))
    assert asyncio.run(_read_identity(b)) == "admin"


# ---- 4. 开关 -----------------------------------------------------------------


def test_guard_switch_defaults_to_on():
    from app.config import Settings

    assert Settings().browser_identity_guard is True
