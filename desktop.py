"""Potato Test 桌面版入口（2026-10-17）。

把现有的 FastAPI + 前端 SPA 装进一个**独立窗口**，不再依赖浏览器标签页。
网页版完全不受影响 —— 同一个服务、同一份数据库，只是多了一个窗口壳，两条入口并存。

用法：
    .venv\\Scripts\\pythonw.exe desktop.py      # 从桌面快捷方式启动（无控制台）
    .venv\\Scripts\\python.exe  desktop.py      # 前台启动（日志同时打到屏幕）

窗口的两种实现（自动选）：
  1. **pywebview**（首选）—— Windows 上用系统自带的 Edge WebView2，真正原生的窗口，
     任务栏里是独立应用，没有地址栏。
  2. **Edge `--app`**（兜底）—— 没装 pywebview 时，用独立 profile 的 Edge 应用窗口。
     同样没有地址栏/标签页；关窗即退出。

## 为什么是"壳"而不是重写一套桌面 UI

界面、执行器、数据全在现有的服务里。桌面版要解决的是"怎么把它当成一个应用打开"，
不是"再用 Qt 写一遍界面"。所以：**同一个进程里跑 uvicorn，窗口指向 127.0.0.1**。
好处是两条入口共享同一份代码与数据，不存在"桌面版行为和网页版不一致"。

## 三个必须处理的坑（都踩过）

* **工作目录**：从桌面/开始菜单双击时 cwd 不是项目根，`.env` 和相对路径的数据库都会找不到 ——
  症状是"能启动但配置全丢"（踩过：双击启动时配置全丢，看起来像数据库被清空了）。所以导入 `app` 之前先 chdir。
* **端口**：写死 18081 会和已经开着的网页版实例打架，启动失败却只写进日志。默认自动换空闲端口。
* **退出**：关窗口必须停掉 uvicorn 与浏览器复用池，否则进程留在后台继续占着 profile。
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_NAME = "desktop.log"
DEFAULT_PORT = 18081
APP_NAME = "Potato Test"


def _root_dir() -> Path:
    """数据/配置的落点。

    ★ 打包后（PyInstaller）必须用 **exe 所在目录**：`__file__` 指向的是 `_internal/` 里面，
      把数据库写在那儿"能用"，但那是程序目录 —— 重装/升级一次数据就没了。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT


def _bundled_web_dist() -> Path | None:
    """打包进包内的前端产物（--add-data 把 web/dist 放在 _MEIPASS 下）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "web" / "dist"
        if (p / "index.html").is_file():
            return p
    return None


def _setup_logging() -> logging.Logger:
    # pythonw 下 sys.stdout 是 None，无条件加 StreamHandler 会直接抛异常
    # （pythonw 下没有控制台，加 StreamHandler 会直接抛异常）。
    log_path = _root_dir() / "logs" / LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("potato.desktop")


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port() -> int:
    """默认 18081；被占用就换一个空闲端口（允许 POTATO_DESKTOP_PORT 固定）。"""
    want = os.environ.get("POTATO_DESKTOP_PORT", "").strip()
    if want.isdigit():
        return int(want)
    if _port_free(DEFAULT_PORT):
        return DEFAULT_PORT
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(port: int, timeout: float = 60.0) -> bool:
    """等端口真的能连上再开窗 —— 否则窗口先弹出来、显示"无法访问"。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _edge_exe() -> str | None:
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(p).is_file():
            return p
    return None


def _run_window(url: str, log: logging.Logger) -> None:
    """开窗口，并**阻塞到用户把它关掉**。"""
    try:
        import webview  # pywebview
    except Exception:  # noqa: BLE001 — 没装就走兜底
        webview = None

    if webview is not None:
        log.info("窗口后端：pywebview（WebView2）")
        try:
            webview.create_window(APP_NAME, url, width=1440, height=900, min_size=(1100, 700))
            webview.start()  # 阻塞到最后一个窗口关闭
            return
        except Exception as exc:  # noqa: BLE001
            # ★ 别人的电脑上可能**没装 WebView2**（Win10 没跟着 Edge 更新过）。
            # pywebview 装得上、但 start() 起不来 —— 以前这里直接冒泡，结果是
            # "双击没反应、只有日志里有一行异常"。现在降级到 Edge 应用窗口，再不行退浏览器。
            log.warning(
                "pywebview 起不来（%s: %s）—— 多半是缺 WebView2 运行时，改用 Edge 兜底",
                type(exc).__name__,
                exc,
            )

    edge = _edge_exe()
    if edge:
        # 兜底：独立 profile 的 Edge 应用窗口。
        # ★ 必须给独立 user-data-dir：否则 `--app` 会挂到已经开着的 Edge 上，
        #   启动它的进程立刻退出 —— 我们就没法知道"用户什么时候关了窗口"。
        prof = _root_dir() / "profiles" / "desktop-shell"
        prof.mkdir(parents=True, exist_ok=True)
        log.info("窗口后端：Edge --app（未安装 pywebview，使用兜底方案）")
        proc = subprocess.Popen(
            [
                edge,
                f"--user-data-dir={prof}",
                f"--app={url}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )
        proc.wait()
        return

    # 连 Edge 都没有：退回默认浏览器。至少能用，但没法感知关窗。
    import webbrowser

    log.warning("既没有 pywebview 也没有 Edge，退回默认浏览器")
    webbrowser.open(url)
    log.warning("服务继续运行；在任务管理器结束 pythonw.exe 才会停止。")
    threading.Event().wait()


def main() -> int:
    log = _setup_logging()
    root = _root_dir()

    # ★ 导入 app 之前切目录（.env / 相对路径数据库都以 cwd 为基准）。
    if Path.cwd() != root:
        os.chdir(root)
        log.info("工作目录切到 %s", root)

    # 打包后的数据默认落在 exe 旁边（用户目录，可写）：
    # 不设的话 app/config.py 会把相对路径解析到 `_internal/` —— 装到 Program Files 时
    # 那边**不可写**，表现为"能启动、一存东西就报错"。
    # 只在冻结时设：源码运行时 .env 里的路径优先级更高，不能被这里顶掉。
    if getattr(sys, "frozen", False):
        os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(root / 'potato.db').as_posix()}")
        os.environ.setdefault("ARTIFACT_DIR", str(root / "artifacts"))
        os.environ.setdefault("PROFILE_DIR", str(root / "profiles"))

    # 关掉 browser_use 的匿名遥测（它会往 eu.i.posthog.com POST，在代理环境里只会超时重试，拖慢启动）。
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")

    port = _pick_port()
    os.environ.setdefault("POTATO_PORT", str(port))

    import uvicorn

    from app.config import get_settings
    from app.main import app as fastapi_app

    s = get_settings()
    if not (s.web_dist or "").strip():
        # 打包后前端在 _MEIPASS 里；源码运行在仓库的 web/dist。
        cand = _bundled_web_dist() or (root / "web" / "dist")
        if (cand / "index.html").is_file():
            s.web_dist = str(cand)

    # ★ 桌面版的窗口是**唯一入口**（2026-10-17 用户要求「系统里面也不需要网页版」）：
    # 每次启动生成一次性令牌，只有带着它的窗口能访问；浏览器直接开这个端口会被
    # `app/desktop_gate.py` 拦成 403。令牌不落盘、不写日志，关掉应用就作废。
    #
    # 为什么不能干脆不起服务：窗口就是用 WebView2 加载这个服务上的界面 ——
    # 服务是"渲染管道"，不是"网页版入口"。删掉它窗口就白了。
    token = secrets.token_urlsafe(24)
    os.environ["POTATO_DESKTOP_TOKEN"] = token

    display_url = f"http://127.0.0.1:{port}/"  # 日志里只写这个，别把令牌写进文件
    url = f"{display_url}?__t={token}"
    log.info("启动服务 %s（数据库 %s）", display_url, s.database_url)

    server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app, host="127.0.0.1", port=port, log_config=None, access_log=False
        )
    )
    t = threading.Thread(target=server.run, name="potato-uvicorn", daemon=True)
    t.start()

    if not _wait_ready(port):
        log.error("服务 60 秒内没起来，窗口不打开（详情见 logs/desktop.log）")
        return 1

    # 自检模式：起服务、不开窗，检查完自己退出。
    # 用途是"验证桌面入口的启动链路"（端口选择 / 配置加载 / SPA 挂载），
    # 在没有图形界面的环境（CI、远程会话）里也能跑。
    if os.environ.get("POTATO_DESKTOP_HEADLESS") == "1":
        log.info("HEADLESS：服务已就绪于 %s，15 秒后退出自检", display_url)
        time.sleep(15)
        server.should_exit = True
        t.join(timeout=10)
        return 0

    try:
        _run_window(url, log)
    finally:
        log.info("窗口已关闭，正在停止服务")
        server.should_exit = True
        t.join(timeout=15)
        # 复用池里的浏览器要收掉，否则变孤儿、继续占 profiles（它们会继续占住 profiles 下的目录，下次启动就是"浏览器启动失败"）。
        try:
            import asyncio

            from app.executor import _close_browser_pool

            asyncio.run(_close_browser_pool())
        except Exception:  # noqa: BLE001 — 退出路径不能因为清理失败而崩
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
