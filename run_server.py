"""后台启动 uvicorn，并把日志写进 logs/potato.log。

为什么需要这个文件，而不是在 .bat 里写 `pythonw -m uvicorn ... > log 2>&1`：

1. pythonw.exe 没有控制台，想重定向就必须套一层 `cmd /c`，而那个 cmd 会随启动它的
   窗口一起被结束，服务也就跟着死了 —— 这正是"关掉终端就断线"的根因之一。
2. 用 PowerShell 的 Start-Process 反而更脆：它复制整个环境块，而本机同时存在
   HTTP_PROXY / http_proxy 这类只差大小写的变量（Windows 环境变量不区分大小写），
   Start-Process 会直接抛 ArgumentException 拒绝启动。
3. 让服务自己配置日志，就没有任何需要转义的引号，也不必依赖外部启动器。

用法：
    .venv\\Scripts\\pythonw.exe run_server.py 18080     # 后台常驻
    .venv\\Scripts\\python.exe  run_server.py 18080     # 前台，日志同时打到屏幕
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "logs" / "potato.log"
DEFAULT_PORT = 18080


def _setup_logging() -> None:
    # 默认只写 WARNING 及以上。
    #
    # INFO 那一档会记录网关地址、数据库路径、代理环境变量、本机监听地址等运行细节，
    # 而这个工具本来就只本机访问，留这些既没必要也增加外泄面；排错时临时把级别调回
    # INFO 就够了：`set POTATO_LOG_LEVEL=INFO` 再启动一次。
    # 注意这里只读 os.environ，因为此刻 .env 还没被加载。
    level_name = os.environ.get("POTATO_LOG_LEVEL", "WARNING").strip().upper()
    level = getattr(logging, level_name, logging.WARNING)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ]
    # 从 pythonw 启动时 stdout 是 None（没有控制台），加 StreamHandler 会直接报错。
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _register_browser_pool_cleanup() -> None:
    """退出时优雅关闭复用池里的浏览器。

    为什么需要：跨用例复用（keep_alive=True）会把 Chromium 一直留着，如果服务被直接
    杀掉，这些 Chromium 会变成**孤儿进程** —— 它们继续占着 profiles/<project>/slotN，
    下一次启动就会"浏览器启动失败"。这里尽力关一次；真成了孤儿也有启动时的
    reap_orphan_browsers() 兜底，两层防护。
    """
    import atexit

    def _close() -> None:
        try:
            import asyncio

            from app.executor import _close_browser_pool

            asyncio.run(_close_browser_pool())
        except Exception:  # noqa: BLE001 — 退出路径绝不能因为清理失败而报错
            pass

    atexit.register(_close)


def main() -> int:
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    except ValueError:
        print(f"端口必须是数字，收到：{sys.argv[1]!r}", file=sys.stderr)
        return 2

    _setup_logging()
    log = logging.getLogger("potato-test.server")

    # 复用池里的 Chromium 需要在退出时优雅关闭（否则变孤儿、占住 profile）。
    _register_browser_pool_cleanup()

    # 让 app 知道自己在哪个端口上，助手要回调自己的 REST 接口（读 /openapi.json）。
    # 硬编码 8000 会在换端口后让助手彻底失去 API 目录，且报错与真实原因毫无关联。
    os.environ.setdefault("POTATO_PORT", str(port))

    # 关掉 browser_use 的匿名遥测。
    #
    # 为什么必须在这里设、而不是写进 .env：.env 只喂给 app/config.py 的 pydantic Settings
    # （`SettingsConfigDict(env_file=".env")`），它**不会**把键写进 os.environ；
    # 而 browser_use 是直接 `os.getenv('ANONYMIZED_TELEMETRY', 'true')` 读的
    # （browser_use/config.py:59）。所以只改 .env 完全无效，必须在导入 app 之前
    # setdefault 到进程环境里。
    #
    # 为什么值得关：它每次运行都 POST 到 eu.i.posthog.com。宿主机由外部工具注入了
    # 透明代理，那条出网路径会 read timeout（日志里实测到
    # `backoff: HTTPSConnectionPool(host='eu.i.posthog.com', port=443): Read timed out
    # (read timeout=15)`）。这个请求与测试无关，却占用一个连接并在退避重试。
    # BROWSER_USE_CLOUD_SYNC 默认跟随 ANONYMIZED_TELEMETRY（config.py:63），
    # 一起关掉即可，两者都不是本项目依赖的能力。
    #
    # 为什么用 setdefault：允许外部（桌面 BAT、CI）显式覆盖，比如临时想开回来做对比。
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")

    # 必须切到项目根：`app/config.py` 用 env_file=".env"（相对当前工作目录）读取配置。
    # 如果本脚本是通过快捷方式/桌面图标启动的，工作目录会是那个图标所在的位置，
    # 于是 .env 根本找不到 —— 表现为静默用默认值：数据库变成默认的 potato-test.db，
    # 而不是 .env 里配的库；LLM 网关地址等配置也全部丢失。
    # 这类"能启动但配置全不对"的故障最难查，所以在导入 app 之前就切好目录。
    if Path.cwd() != ROOT:
        log.info("工作目录 %s 不是项目根，已切换到 %s", Path.cwd(), ROOT)
        os.chdir(ROOT)

    # 显式汇报 .env 到底有没有被读到。配置读不到时 app 不会报错，只会安静地用默认值
    # （默认 LLM 网关是 api.openai.com / gpt-4o，默认数据库是另一个文件名），
    # 症状是"界面能开、但设置看起来不对"，很难查。这里至少留下一条明确记录。
    env_file = ROOT / ".env"
    if env_file.is_file():
        log.info("已加载配置文件 %s", env_file)
    else:
        log.warning(
            "没有找到 %s —— 全部配置将使用内置默认值（LLM 会指向 api.openai.com 且无密钥）。"
            "请从 .env.example 复制一份并填好。",
            env_file,
        )

    import uvicorn

    from app.config import get_settings
    from app.main import app

    s = get_settings()
    log.info(
        "LLM 网关 %s / 模型 %s （密钥%s）",
        s.gateway_base_url,
        s.agent_model or s.gateway_model,
        "已设置" if s.gateway_api_key else "未设置",
    )
    log.info("数据库 %s", s.database_url)

    # 继承来的代理变量是"能启动、但所有 LLM 调用都报 APIConnectionError"的头号原因：
    # httpx 默认 trust_env=True，会把请求发给启动时存在、之后已经死掉的代理端口。
    # 出网本来就不需要代理时它只是噪音，需要时又必须留着，所以两种都明确记下来。
    _proxy = {k: os.environ[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY") if os.environ.get(k)}
    if _proxy:
        log.info(
            "检测到代理环境变量 %s —— 已按 gateway_ignore_proxy=%s 处理（%s）",
            _proxy,
            s.gateway_ignore_proxy,
            "忽略，直连网关" if s.gateway_ignore_proxy else "沿用，网关需经代理才能访问",
        )

    # 上一次如果是被强杀的（重启 BAT、任务管理器结束进程、崩掉），它启动的 Chrome 不会
    # 跟着退出，而是继续占用 profiles/ 下的浏览器配置目录。之后每个用例启动浏览器都会
    # 立刻失败：「Browser process exited before CDP became available」。
    # 这些 Chrome 已经不是新进程的子进程，没人会替它们收尸，所以开机清一次。
    # 只扫 user-data-dir 落在本项目 profiles/ 里的浏览器，不会碰用户自己开的 Chrome。
    try:
        from app.executor import reap_orphan_browsers

        reaped = reap_orphan_browsers()
        if reaped["found"]:
            log.warning(
                "清理上次残留的浏览器进程：发现 %s 个，已结束 %s 个",
                reaped["found"],
                reaped["killed"],
            )
    except Exception as exc:  # noqa: BLE001 — 清理失败不能挡住服务启动
        log.warning("残留浏览器清理跳过：%s", exc)

    # log_config=None：保留上面配置的根 logger，否则 uvicorn 会用自己的配置覆盖掉文件输出。
    # 监听地址写死 127.0.0.1，不开放局域网：见 app/config.py 里的说明。
    host = "127.0.0.1"
    log.info("已启动，仅本机可访问：http://%s:%s", host, port)

    # 只在真的配了对外地址时才打这一行，避免把内网地址写进日志。
    if (s.public_base_url or "").strip():
        log.info("对外地址 PUBLIC_BASE_URL = %s", s.public_base_url)

    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
    log.info("服务已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
