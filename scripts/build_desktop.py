"""把 Potato Test 打成桌面版（PyInstaller）。

产物：`dist_desktop/PotatoTest/PotatoTest.exe`（双击开窗口，无控制台）。
前端 `web/dist`、后端 `app/`、Playwright 的 node driver 全部进包 —— 装到别的机器上
不需要 Python、不需要 Node、不需要 pnpm。

用法：
    .venv\\Scripts\\python.exe scripts\\build_desktop.py

## 为什么是这几个选择

* **onedir 而不是 onefile**：onefile 每次启动都要把上百 MB 解压到临时目录，首启慢 3~8 秒，
  而且"自解压"行为容易被杀软拦。装到用户目录后 onedir 启动快得多。
* **--windowed**：不弹黑框控制台；日志写 `logs/desktop.log`。
* **--add-data web/dist**：桌面版不依赖仓库目录，前端构建产物必须塞进包内
  （`app/config.py` 会从 `WEB_DIST` 或包内路径找它）。
* **--collect-all playwright**：它带一个 node driver，PyInstaller 的静态分析看不见。
* **--collect-all browser_use**：执行器用它，里面有大量动态 import。
* **hidden-import uvicorn.***：uvicorn 用字符串选 loop/protocol 实现，静态分析同样看不见。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist_desktop"
WORK = ROOT / "build" / "desktop"
NAME = "PotatoTest"


def main() -> int:
    dist = ROOT / "web" / "dist"
    if not (dist / "index.html").is_file():
        print("!! web/dist 不存在 —— 先 `cd web && pnpm build`")
        return 2

    try:
        import PyInstaller  # noqa: F401
    except Exception:
        print("!! 没装 PyInstaller：.venv\\Scripts\\python -m pip install pyinstaller")
        return 2

    sep = os.pathsep  # Windows 是 ';'
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        NAME,
        "--windowed",
        "--distpath",
        str(OUT),
        "--workpath",
        str(WORK),
        "--specpath",
        str(WORK),
        # 前端构建产物（app 找的是 web/dist）
        "--add-data",
        f"{dist}{sep}web/dist",
        # 静态分析看不见的：playwright 的 node driver / browser_use 的动态 import
        "--collect-all",
        "playwright",
        "--collect-all",
        "browser_use",
        # pywebview 的 Windows 后端（WebView2）
        "--collect-all",
        "webview",
        # OCR（2026-10-17）：模型文件是数据，静态分析看不到 —— 不 collect-all 的话
        # 装到别人机器上会"能跑但 OCR 报找不到模型"
        "--collect-all",
        "rapidocr",
        "--collect-all",
        "onnxruntime",
        "--collect-all",
        "pymupdf",
        "--hidden-import",
        "cv2",
        # uvicorn 按字符串选实现
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.loops.asyncio",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.http.h11_impl",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
        "--hidden-import",
        "uvicorn.lifespan.off",
        # app 里用到但可能被漏掉的
        "--hidden-import",
        "aiosqlite",
        "--hidden-import",
        "asyncpg",
        "--hidden-import",
        "pythonnet",
        "--hidden-import",
        "clr_loader",
        str(ROOT / "desktop.py"),
    ]

    print("== 开始打包 ==")
    print(" ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        print(f"!! PyInstaller 退出码 {rc}")
        return rc

    exe = OUT / NAME / f"{NAME}.exe"
    if not exe.is_file():
        print(f"!! 没找到产物 {exe}")
        return 3

    # 把 .env 模板一起带上：桌面版首次启动没有配置时，用户至少知道该填什么。
    example = ROOT / ".env.example"
    if example.is_file():
        shutil.copy2(example, OUT / NAME / ".env.example")

    size = sum(f.stat().st_size for f in (OUT / NAME).rglob("*") if f.is_file())
    print(f"== 完成：{exe} ==")
    print(f"   目录大小 {size / 1024 / 1024:.0f} MB")
    print("   下一步：用打包脚本/安装器把这整个目录装到用户目录（见 docs/desktop-build.md）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
