"""桌面版访问闸门（2026-10-17）。

## 为什么需要

桌面版的窗口是用 WebView2 加载 `http://127.0.0.1:<端口>/` —— **这个服务删不掉**，
删了窗口就是白的。但"服务在"不等于"网页版在"：这个闸门让**只有带启动令牌的窗口**
能访问，用浏览器直接打开 `http://127.0.0.1:18081` 只会看到一页说明。

令牌每次启动随机生成（`secrets.token_urlsafe`），只活在进程内存和 cookie 里，不落盘、
不写日志 —— 关掉应用就作废。

## 为什么用 cookie 而不是让每个请求都带 query

窗口首次加载是 `/?__t=<令牌>`，服务端校验后**种一个 HttpOnly cookie 再 303 回干净的地址**。
之后的静态资源、API、SSE 都会自动带 cookie。这样地址栏里不会一直挂着令牌
（用户截图、录屏时不会把令牌带出去）。

## 什么时候不启用

没设 `POTATO_DESKTOP_TOKEN` 就不拦 —— 开发时 `run_server.py` 照旧能直接用浏览器调试。
只有 `desktop.py` 会设这个变量，也就是**只有桌面版会开启闸门**。
"""

from __future__ import annotations

import hmac
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "tp_desktop"
ENV_TOKEN = "POTATO_DESKTOP_TOKEN"

# 纯内联样式（不引用任何外部资源）：被拦的是浏览器而不是窗口，此时还没有前端可加载。
_BLOCKED_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Potato Test</title>
<style>
html,body{height:100%;margin:0;background:#f7f7f8;color:#1f2328;
font:15px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{height:100%;display:flex;align-items:center;justify-content:center;padding:24px}
.card{max-width:460px;padding:28px 30px;background:#fff;border:1px solid #e3e5e8;border-radius:12px}
h1{margin:0 0 10px;font-size:17px;font-weight:600}
p{margin:6px 0 0;color:#57606a;font-size:13px}
</style></head><body><div class="wrap"><div class="card">
<h1>请从 Potato Test 桌面版打开</h1>
<p>这个地址只在应用内部使用。请用开始菜单或桌面上的 Potato Test 图标打开。</p>
</div></div></body></html>"""


def _strip_token(url: str) -> str:
    """把 `__t` 从地址里去掉 —— 种完 cookie 后不该让令牌继续挂在 URL 上。"""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "__t"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


class DesktopGate(BaseHTTPMiddleware):
    """没带令牌的请求一律拒掉（API 给 JSON，页面给说明页）。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        token = (os.environ.get(ENV_TOKEN) or "").strip()
        if not token:
            return await call_next(request)  # 开发模式：不拦

        cookie = request.cookies.get(COOKIE, "")
        if cookie and hmac.compare_digest(cookie, token):
            return await call_next(request)

        given = request.query_params.get("__t", "")
        if given and hmac.compare_digest(given, token):
            # 首次带令牌进来：种 cookie，然后把地址清干净
            resp = RedirectResponse(_strip_token(str(request.url)), status_code=303)
            resp.set_cookie(
                COOKIE,
                token,
                httponly=True,
                samesite="lax",
                path="/",
                # 不加 max_age：关掉应用进程就是新建的令牌，旧 cookie 自然失效
            )
            return resp

        if request.url.path.startswith("/api"):
            return JSONResponse(
                {"detail": "这个地址只能在 Potato Test 桌面版里访问"}, status_code=403
            )
        return HTMLResponse(_BLOCKED_HTML, status_code=403)


def install(app) -> None:  # type: ignore[no-untyped-def]
    """在 create_app() 里调用。**最后加** —— 让它成为最外层，把静态资源和 API 一起罩住。"""
    app.add_middleware(DesktopGate)
