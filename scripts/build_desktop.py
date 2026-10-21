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


# 打包后删掉**确定用不到**的运行时文件。
# 业务代码、业务依赖、前端资源一律不动；只清缓存/调试符号/误打包的 UI 库。
_PRUNE_FILES = [
    (
        "**/cv2/opencv_videoio_ffmpeg*.dll",
        "cv2 的视频解码后端（约 29 MB）。OCR 只做图片解码，cv2 对它懒加载："
        "实测删掉后 import cv2 与 cvtColor 正常，只有 VideoCapture 会失败（本产品不读视频）",
    ),
    (
        "**/playwright/driver/package/**/*.js.map",
        "playwright 驱动的 JS source map（只给调试器用）",
    ),
    (
        "**/playwright/driver/package/**/*.d.ts",
        "playwright 驱动的 TypeScript 类型声明（编译期用）",
    ),
    ("**/*.pyc", "Python 字节码缓存（包内副本永远用不到，导入时会重新生成）"),
    ("**/*.pdb", "调试符号（发行版不需要）"),
    ("**/*.map", "Source Map（只给调试器用）"),
    ("**/Qt5*.dll", "Qt 运行库（本项目用 pywebview/WebView2，不用 Qt）"),
    ("**/Qt6*.dll", "Qt 运行库（本项目用 pywebview/WebView2，不用 Qt）"),
    ("**/_tkinter*.pyd", "Tk 绑定（本项目不用 Tk）"),
    ("**/tcl*.dll", "Tcl 运行库（本项目不用 Tk）"),
    ("**/tk*.dll", "Tk 运行库（本项目不用 Tk）"),
    ("**/web/dist/**/*.test.*", "前端测试文件（构建产物不应包含）"),
    ("**/web/dist/**/*.spec.*", "前端测试文件（构建产物不应包含）"),
]

_PRUNE_DIRS = {
    "__pycache__": "Python 字节码缓存目录",
    "PyQt5": "Qt 绑定（本项目不用 Qt）",
    "PyQt6": "Qt 绑定（本项目不用 Qt）",
    "PySide2": "Qt 绑定（本项目不用 Qt）",
    "PySide6": "Qt 绑定（本项目不用 Qt）",
    "shiboken2": "Qt 绑定的辅助库（本项目不用 Qt）",
    "shiboken6": "Qt 绑定的辅助库（本项目不用 Qt）",
    "tkinter": "Tk 标准库（本项目不用 Tk）",
    "tcl": "Tcl 运行库目录（本项目不用 Tk）",
    "node_modules": "前端依赖源码（构建产物不需要）",
}


def _prune_bundle(dist: Path) -> None:
    total = 0
    removed = 0
    for pat, why in _PRUNE_FILES:
        n = 0
        for f in dist.glob(pat):
            if not f.is_file():
                continue
            try:
                total += f.stat().st_size
                f.unlink()
                n += 1
            except Exception:  # noqa: BLE001
                pass
        removed += n
        print(f"  剔除 {pat} x{n}（{why}）")

    for name, why in _PRUNE_DIRS.items():
        n = 0
        for d in list(dist.rglob(name)):
            if not d.is_dir():
                continue
            try:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d)
                total += size
                n += 1
            except Exception:  # noqa: BLE001
                pass
        removed += n
        print(f"  剔除目录 {name} x{n}（{why}）")

    print(f"  瘦身合计 {removed} 项 / {total / 1024 / 1024:.1f} MB")


def _find_upx() -> str | None:
    """优先用仓库内便携版，其次 PATH。找不到就只告警（PyInstaller 会跳过 UPX）。"""
    local = sorted((ROOT / ".tools" / "upx").rglob("upx.exe"))
    if local:
        return str(local[0])
    return shutil.which("upx")


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


def _run_frontend_build() -> int:
    """打包前强制重建前端。

    `web/dist` 过期是最常见的发版事故：安装包里还是上一版界面，而且没有任何报错。
    统一在这里跑，避免依赖人工记得先 `npm run build`。
    """
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        print("!! 找不到 npm —— 先装 Node.js，或用 --skip-web 复用现有 web/dist")
        return 2
    print("== 构建前端（npm run build）==")
    rc = subprocess.call([npm, "run", "build"], cwd=str(ROOT / "web"))
    if rc != 0:
        print(f"!! 前端构建失败（退出码 {rc}）")
    return rc



def main() -> int:
    ap = argparse.ArgumentParser(description="打包 Potato Test 桌面端")
    ap.add_argument(
        "--fast",
        action="store_true",
        help="增量构建：跳过 PyInstaller --clean，仅供本机调试；发版仍用完整构建",
    )
    ap.add_argument(
        "--skip-web",
        action="store_true",
        help="跳过前端构建，直接复用现有 web/dist（仅供调试）",
    )
    args = ap.parse_args()

    if not args.skip_web:
        rc = _run_frontend_build()
        if rc != 0:
            return rc

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

    upx = _find_upx()
    if upx:
        os.environ["PATH"] = str(Path(upx).parent) + os.pathsep + os.environ.get("PATH", "")
        # UPX 要在 PyInstaller 的 bincache 里写临时文件；部分机器（杀软/受控文件夹访问）
        # 会拒绝在 %LOCALAPPDATA%\pyinstaller 下创建文件，导致全部压缩失败。
        # 把缓存放到仓库内，既避开该限制，也方便清理。
        os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(ROOT / ".tools" / "pyinstaller-cache"))
        print(f"== UPX：{upx} ==")
        print(f"== PyInstaller 缓存：{os.environ['PYINSTALLER_CONFIG_DIR']} ==")
    else:
        print("!! 未找到 UPX —— upx=True 会被静默跳过，本次不做二进制压缩")

    # 维护版 spec 里固定了 excludes / upx / upx_exclude；这里只传路径和清理开关。
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        *([] if args.fast else ["--clean"]),
        "--distpath",
        str(OUT),
        "--workpath",
        str(WORK),
        str(ROOT / "PotatoTest.spec"),
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
