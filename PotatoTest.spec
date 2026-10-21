# -*- mode: python ; coding: utf-8 -*-
"""Potato Test 桌面版 PyInstaller 配置（维护版，1.0.7）。

由 scripts/build_desktop.py 调用：
    python -m PyInstaller --noconfirm --clean --distpath dist_desktop --workpath build/desktop PotatoTest.spec

原则：不排除任何业务依赖。excludes 只保留已确认无引用的 imageio/imageio_ffmpeg。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "web" / "dist"), "web/dist"),
    (str(ROOT / "app" / "embedding_models"), "app/embedding_models"),
]
binaries = []
hiddenimports = [
    "cv2",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "aiosqlite",
    "pythonnet",
    "clr_loader",
]

# 这些包的资源/动态导入 PyInstaller 静态分析看不见，必须整体收集。
for _pkg in ("playwright", "browser_use", "webview", "rapidocr", "onnxruntime", "pymupdf"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h


a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 只保留经过 grep 确认无引用的两项；不再盲猜排除 numpy/pandas 等。
    excludes=["imageio_ffmpeg", "imageio"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PotatoTest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    # 系统核心库不压缩：UPX 外壳可能破坏它们的加载/TLS，导致启动即崩。
    upx_exclude=[
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
        "msvcp140_atomic_wait.dll",
        "concrt140.dll",
        "python3.dll",
        "python3*.dll",
        "ucrtbase.dll",
    ],
    name="PotatoTest",
)
