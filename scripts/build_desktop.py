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

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# CI 的 Windows 运行器 stdout 默认 cp1252，打印中文会 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist_desktop"
WORK = ROOT / "build" / "desktop"
NAME = "PotatoTest"


# 打包后删掉**确定用不到**的运行时文件（2026-10-19）。
# 每条都要能说清"为什么删了不会坏" —— 删错会让应用启动即崩，所以只挑有测试验证过的。
_PRUNE = [
    (
        "**/cv2/opencv_videoio_ffmpeg*.dll",
        "cv2 的视频解码后端（约 29 MB）。OCR 只做图片解码，cv2 对它是**懒加载**："
        "实测删掉后 import cv2 与 cvtColor 都正常，只有 VideoCapture 会失败（本产品不读视频）",
    ),
    (
        "**/playwright/driver/package/**/*.js.map",
        "playwright 驱动的 JS source map（只给调试器用，运行时不需要）",
    ),
    (
        "**/playwright/driver/package/**/*.d.ts",
        "playwright 驱动的 TypeScript 类型声明（编译期用）",
    ),
]


def _prune_bundle(dist: Path) -> None:
    total = 0
    for pat, why in _PRUNE:
        for f in dist.glob(pat):
            try:
                total += f.stat().st_size
                f.unlink()
            except Exception:  # noqa: BLE001
                pass
        print(f"  剔除 {pat}（{why}）")
    print(f"  瘦身合计 {total / 1024 / 1024:.1f} MB")


def _assert_embedding_model() -> None:
    """★ 向量模型必须在场（2026-10-19）。

    为什么这里硬失败：`app/embeddings.available()` 设计成"模型缺失就静默退回关键词" ——
    对运行期是对的（不能让检索整个挂掉），但对**打包**是灾难：会出一个"看起来正常、
    实际检索质量退回关键词"的安装包，没有报错，只能靠人工发现。
    """
    d = ROOT / "app" / "embedding_models" / "bge-small-zh-v1.5"
    missing = [f for f in (d / "tokenizer.json", d / "onnx" / "model_quantized.onnx") if not f.is_file()]
    if missing:
        raise SystemExit(
            "缺少向量模型文件，拒绝打包：\n  " + "\n  ".join(str(m) for m in missing)
            + "\n（见 app/embeddings.py 文件头；从 hf-mirror 拉 Xenova/bge-small-zh-v1.5 的 "
            "tokenizer.json + onnx/model_quantized.onnx）"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 Potato Test 桌面端")
    ap.add_argument(
        "--fast",
        action="store_true",
        help="增量构建：跳过 PyInstaller --clean，仅供本机调试；发版仍用完整构建",
    )
    args = ap.parse_args()

    _assert_embedding_model()  # 先查模型，别白跑 4 分钟打包
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
        *([] if args.fast else ["--clean"]),
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
        # ★ 排除 imageio_ffmpeg（2026-10-19）：它自带一个 ffmpeg 二进制，占 **83 MB**，
        # 而本项目、browser_use、playwright 都**没有引用它**（grep 确认）。
        # 视频录制走 playwright 自己，用不到这个可执行文件。
        "--exclude-module",
        "imageio_ffmpeg",
        "--exclude-module",
        "imageio",
        # 向量模型（2026-10-19）：bge-small-zh-v1.5 的 ONNX 文件是**数据**，
        # PyInstaller 静态分析看不见 —— 不加 --add-data 的话，装出来的应用
        # `embeddings.available()` 永远 False，检索会静默退回关键词（不报错，很难发现）。
        "--add-data",
        f"{ROOT / 'app' / 'embedding_models'}{os.pathsep}app/embedding_models",
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
        "pythonnet",
        "--hidden-import",
        "clr_loader",
        str(ROOT / "desktop.py"),
    ]

    print("== 开始打包 ==" + ("（增量 --fast）" if args.fast else "（完整 clean）"))
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

    # ★ 打包后瘦身（2026-10-19）：见 _PRUNE 的说明，只删有验证过的无用文件
    _prune_bundle(OUT / NAME)

    size = sum(f.stat().st_size for f in (OUT / NAME).rglob("*") if f.is_file())
    print(f"== 完成：{exe} ==")
    print(f"   目录大小 {size / 1024 / 1024:.0f} MB")
    print("   下一步：用打包脚本/安装器把这整个目录装到用户目录（见 docs/desktop-build.md）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
