"""扫描 PyInstaller onedir 产物，生成 WiX Components.wxs（2026-10-21）。

为什么自己生成而不装 heat：
  * 这次只用 WiX 核心功能，不想为一次性 harvest 再引一个工具；
  * 需要稳定的 Component GUID —— 每次构建 GUID 都变会让升级留下旧文件；
  * 需要排除运行时数据（potato.db / logs / .env …），这些绝不能进安装包。

GUID 用 uuid5(命名空间, 相对路径) 生成：同一路径永远同一 GUID，升级可覆盖。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "dist_desktop" / "PotatoTest"
DEFAULT_OUT = ROOT / "installer" / "Components.wxs"

# 相对路径（/ 分隔）匹配即排除。只排运行时产物，不排程序文件。
_EXCLUDE_DIRS = {"logs", "artifacts", "profiles", "__pycache__", ".pytest_cache"}
_EXCLUDE_FILES = {"potato.db", ".env"}
_EXCLUDE_GLOBS = ("potato.db-*", "*.pyc", "uninstall.exe")
_NS = uuid.UUID("0d6f5b7e-6b1a-5c2e-9a55-3f5f0f7c9d21")


def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in _EXCLUDE_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in _EXCLUDE_FILES:
        return True
    return any(Path(name).match(g) for g in _EXCLUDE_GLOBS)


def _ident(prefix: str, rel: str) -> str:
    return prefix + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]


def _guid(rel: str) -> str:
    return "{" + str(uuid.uuid5(_NS, rel)).upper() + "}"


def _xml_attr(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _dir_name(raw: str) -> str:
    # WiX 的 Name 是实际落盘名，必须原样保留；只做 XML 转义。
    return _xml_attr(raw)


def _walk(src: Path, out: list[str], rel_dir: str, depth: int, component_refs: list[str]) -> None:
    base = src / rel_dir if rel_dir else src
    pad = "  " * depth

    dirs = sorted(p for p in base.iterdir() if p.is_dir() and not _excluded((rel_dir + "/" + p.name).lstrip("/")))
    files = sorted(p for p in base.iterdir() if p.is_file() and not _excluded((rel_dir + "/" + p.name).lstrip("/")))

    for f in files:
        rel = (rel_dir + "/" + f.name).lstrip("/")
        cid = _ident("cmp_", rel)
        fid = _ident("fil_", rel)
        out.append(f'{pad}<Component Id="{cid}" Guid="{_guid(rel)}">')
        out.append(
            f'{pad}  <File Id="{fid}" Source="{_xml_attr(str(f.resolve()))}" KeyPath="yes" />'
        )
        out.append(f"{pad}</Component>")
        component_refs.append(cid)

    for d in dirs:
        rel = (rel_dir + "/" + d.name).lstrip("/")
        did = _ident("dir_", rel)
        out.append(f'{pad}<Directory Id="{did}" Name="{_dir_name(d.name)}">')
        _walk(src, out, rel, depth + 1, component_refs)
        out.append(f"{pad}</Directory>")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    src = args.src.resolve()
    if not (src / "PotatoTest.exe").is_file():
        raise SystemExit(f"找不到 {src / 'PotatoTest.exe'}，先运行 scripts/build_desktop.py")

    body: list[str] = []
    refs: list[str] = []
    _walk(src, body, "", 3, refs)

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">',
        "  <Fragment>",
        '    <DirectoryRef Id="INSTALLDIR">',
        *body,
        "    </DirectoryRef>",
        "  </Fragment>",
        "  <Fragment>",
        '    <ComponentGroup Id="PayloadGroup">',
        *[f'      <ComponentRef Id="{r}" />' for r in refs],
        "    </ComponentGroup>",
        "  </Fragment>",
        "</Wix>",
        "",
    ]
    args.out.write_text("\n".join(lines), encoding="utf-8")
    total = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
    print(f"harvest: {len(refs)} 个文件 -> {args.out}（{total / 1024 / 1024:.0f} MB 源目录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
