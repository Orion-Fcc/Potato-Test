r"""构建 Potato Test 的 MSI（2026-10-21）。

流程：读取版本 → 校验 PyInstaller 产物 → harvest 生成 Components.wxs → wix build → .sha256。

用法：
    .venv\Scripts\python.exe scripts/build_msi.py
    .venv\Scripts\python.exe scripts/build_msi.py --skip-harvest
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dist_desktop" / "PotatoTest"
OUT_DIR = ROOT / "dist_desktop" / "installer"
WXS = ROOT / "installer" / "PotatoTest.wxs"
COMPONENTS = ROOT / "installer" / "Components.wxs"
DOTNET_ROOT = ROOT / ".tools" / "dotnet"
WIX = ROOT / ".tools" / "wix" / "wix.exe"
TEMP_DIR = ROOT / ".tools" / "tmp"


def read_version() -> str:
    text = (ROOT / "app" / "version.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("app/version.py 里找不到 APP_VERSION")
    return m.group(1)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-harvest", action="store_true")
    args = ap.parse_args()

    version = read_version()
    if not (SRC / "PotatoTest.exe").is_file():
        raise SystemExit(f"找不到 {SRC / 'PotatoTest.exe'}，先运行 scripts/build_desktop.py")
    if not WIX.is_file():
        raise SystemExit(f"找不到 {WIX}，先安装 WiX v5（见 docs/desktop-build.md）")

    if not args.skip_harvest:
        rc = subprocess.call([sys.executable, str(ROOT / "installer" / "harvest.py")])
        if rc != 0:
            return rc

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"PotatoTest-Setup-{version}.msi"

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["DOTNET_ROOT"] = str(DOTNET_ROOT)
    env["TEMP"] = str(TEMP_DIR)
    env["TMP"] = str(TEMP_DIR)

    cmd = [
        str(WIX),
        "build",
        "-arch", "x64",
        "-ext", "WixToolset.UI.wixext",
        "-culture", "zh-CN",
        "-o", str(out),
        "-d", f"Version={version}",
        "-d", f"SourceDir={SRC}",
        str(WXS),
        str(COMPONENTS),
    ]
    print("== wix build ==")
    print(" ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT), env=env)
    if rc != 0:
        print(f"!! wix 退出码 {rc}")
        return rc
    if not out.is_file():
        print(f"!! 没生成 {out}")
        return 3

    digest = sha256_of(out)
    (OUT_DIR / (out.name + ".sha256")).write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    print(f"== 完成：{out} ==")
    print(f"   大小 {out.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"   SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
