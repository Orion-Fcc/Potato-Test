"""桌面版访问闸门（`app/desktop_gate.py`）的测试（2026-10-17）。

用户要求「系统里面也不需要网页版」。桌面窗口用的那个本地服务**删不掉**（窗口就是靠它
加载界面的），所以落地方式是：**只有带启动令牌的窗口能访问** —— 浏览器直接敲
`127.0.0.1:18081` 得到 403 和一页说明。这组测试守的就是这条边界。
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.desktop_gate import COOKIE, ENV_TOKEN, HEADER, DesktopGate  # noqa: E402

TOKEN = "test-token-123"


def _app() -> FastAPI:
    a = FastAPI()
    a.add_middleware(DesktopGate)

    @a.get("/")
    def index():  # noqa: ANN202
        return PlainTextResponse("ui")

    @a.get("/api/projects")
    def projects():  # noqa: ANN202
        return {"ok": True}

    return a


def test_no_token_env_means_dev_mode(monkeypatch):
    """没设令牌就不拦 —— 测试环境靠这条跑（否则每个 TestClient 都要先握手）。"""
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    c = TestClient(_app())
    assert c.get("/").status_code == 200


def test_browser_without_token_is_blocked(monkeypatch):
    """★ 核心断言：浏览器直接访问 → 403（页面给人看说明，API 给 JSON）。"""
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    c = TestClient(_app())
    page = c.get("/")
    assert page.status_code == 403
    assert "桌面版" in page.text
    api = c.get("/api/projects")
    assert api.status_code == 403
    assert "桌面版" in api.json()["detail"]


def test_token_in_query_sets_cookie_and_strips_url(monkeypatch):
    """首次带令牌进来：种 cookie + 303 回干净地址（令牌不能留在地址栏）。"""
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    c = TestClient(_app())
    r = c.get(f"/?__t={TOKEN}", follow_redirects=False)
    assert r.status_code == 303
    assert COOKIE in r.cookies
    assert "__t" not in r.headers["location"]


def test_after_handshake_the_app_works(monkeypatch):
    """握手之后，页面和 API 都正常 —— 别把自己人也挡在外面。"""
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    c = TestClient(_app())
    c.get(f"/?__t={TOKEN}")  # 跟随 303，cookie 落到 session 里
    assert c.get("/").status_code == 200
    assert c.get("/api/projects").json() == {"ok": True}


def test_wrong_token_is_blocked(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    c = TestClient(_app())
    assert c.get("/?__t=wrong-token").status_code == 403


def test_app_factory_actually_installs_it():
    """闸门必须真的挂进 create_app() —— 这个文件写了但没挂，等于没做。"""
    from app.main import create_app

    app = create_app()
    assert any(m.cls is DesktopGate for m in app.user_middleware), "create_app 没装 DesktopGate"


def test_token_header_is_accepted(monkeypatch):
    """同进程调用（助手）带 token 头 → 放行，不需要先做 cookie 握手。"""
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    c = TestClient(_app())
    r = c.get("/api/projects", headers={HEADER: TOKEN})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_wrong_token_header_is_blocked(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    c = TestClient(_app())
    assert c.get("/api/projects", headers={HEADER: "wrong-token"}).status_code == 403


def test_assistant_self_headers_follow_env(monkeypatch):
    from app import assistant

    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    assert assistant._desktop_headers() == {HEADER: TOKEN}
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    assert assistant._desktop_headers() == {}


async def test_assistant_api_sends_desktop_token(monkeypatch):
    """_api() 必须把 token 放进请求头，否则桌面版会 403。"""
    from app import assistant

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": True}

    captured = {}

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, json=None, headers=None):
            captured.update(headers or {})
            return _Resp()

    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    monkeypatch.setattr(assistant.httpx, "AsyncClient", _Client)
    assert await assistant._api("GET", "/api/projects") == {"ok": True}
    assert captured == {HEADER: TOKEN}
