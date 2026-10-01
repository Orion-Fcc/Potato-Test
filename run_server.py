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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "logs" / "potato.log"
DEFAULT_PORT = 18080


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ]
    # 从 pythonw 启动时 stdout 是 None（没有控制台），加 StreamHandler 会直接报错。
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def main() -> int:
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    except ValueError:
        print(f"端口必须是数字，收到：{sys.argv[1]!r}", file=sys.stderr)
        return 2

    _setup_logging()
    log = logging.getLogger("potato-test.server")
    log.info("启动 Potato Test，端口 %s，工作目录 %s", port, ROOT)

    import uvicorn

    from app.main import app

    # log_config=None：保留上面配置的根 logger，否则 uvicorn 会用自己的配置覆盖掉文件输出。
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    log.info("服务已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
