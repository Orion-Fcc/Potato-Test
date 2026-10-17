"""检查 / 下载更新（2026-10-17）。

数据源：**GitHub Releases**（公开仓库，不需要 token）。
为什么不自己搭更新服务：一个人维护，多一个要部署的东西就多一个故障点；
Release 的 `latest` 接口天然给到"版本号 + 更新说明 + 资产下载地址"。

## 三条硬约束

1. **只有桌面版能一键安装**。网页版跑在服务器上 —— 它读不到用户的文件系统，
   也不该能往用户机器上装东西。`install_supported()` 用 `sys.frozen` 判断。
2. **只允许从本仓库的 Release 下载**。这个接口会"下载一个 exe 并执行"，
   如果不校验来源，它就等于一个远程代码执行入口。所以下载地址必须匹配
   `https://github.com/<本仓库>/releases/download/`，且资产名必须是
   `PotatoTest-Setup-*.exe`。校验失败直接拒绝，不做任何"尽力而为"。
3. **检查失败不算错误**。断网、GitHub 抽风、还没发过 Release —— 都不是用户的问题，
   返回 `error` 字段让界面温和提示即可，绝不抛 500 让应用显得坏了。
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.version import APP_VERSION, GITHUB_REPO

log = logging.getLogger("potato.updater")

API = "https://api.github.com"
_TIMEOUT = 8.0  # 检查更新是"顺手看一眼"，不能拖住界面
_CACHE_TTL = timedelta(minutes=10)
_ASSET_RE = re.compile(r"^PotatoTest-Setup-.*\.exe$", re.I)
# 下载白名单：只认本仓库 release 资产（GitHub 会 302 到 objects.githubusercontent.com，
# httpx 跟随重定向，但那已经是我们放行的地址之后的跳转，可以接受）
_ALLOWED_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"

_cache: dict[str, object] = {"at": None, "data": None}


def _ver_tuple(v: str) -> tuple[int, ...]:
    """`1.2.10` → (1, 2, 10)。只比前三位，剩下的忽略（够用且不会因为后缀炸掉）。"""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def is_newer(latest: str, current: str = APP_VERSION) -> bool:
    return _ver_tuple(latest) > _ver_tuple(current)


def install_supported() -> bool:
    """只有打包后的桌面版允许自更新（源码跑的开发环境不该被"更新"掉）。"""
    return bool(getattr(sys, "frozen", False))


def _pick_asset(assets: list[dict]) -> dict | None:
    """从 release 资产里挑安装包。挑不到就返回 None（不是错误）。"""
    for a in assets or []:
        name = str(a.get("name") or "")
        if _ASSET_RE.match(name):
            return a
    return None


async def check(force: bool = False) -> dict:
    """查最新版本。10 分钟内重复调用直接给缓存，避免用户连点把 GitHub 限流。"""
    now = datetime.now(UTC)
    if not force and _cache["data"] and _cache["at"] and now - _cache["at"] < _CACHE_TTL:  # type: ignore[operator]
        return dict(_cache["data"])  # type: ignore[arg-type]

    data: dict = {
        "current": APP_VERSION,
        "latest": None,
        "has_update": False,
        "notes": "",
        "page_url": f"https://github.com/{GITHUB_REPO}/releases",
        "download_url": None,
        "asset_name": None,
        "install_supported": install_supported(),
        "checked_at": now.isoformat(timespec="seconds"),
        "error": None,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(
                f"{API}/repos/{GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
        if r.status_code == 404:
            # 还没发过 Release —— 对用户是"暂时没有更新"，不是故障
            data["error"] = "这个项目还没有发布过版本"
        elif r.status_code != 200:
            data["error"] = f"更新服务器返回 {r.status_code}"
        else:
            rel = r.json()
            tag = str(rel.get("tag_name") or rel.get("name") or "").lstrip("vV")
            asset = _pick_asset(rel.get("assets") or [])
            data["latest"] = tag or None
            data["notes"] = str(rel.get("body") or "")[:4000]
            data["page_url"] = rel.get("html_url") or data["page_url"]
            if asset:
                data["download_url"] = asset.get("browser_download_url")
                data["asset_name"] = asset.get("name")
            data["has_update"] = bool(tag) and is_newer(tag)
    except Exception as exc:  # noqa: BLE001 — 断网/超时都只当"查不到"
        log.info("检查更新失败：%s", exc)
        data["error"] = "连不上更新服务器（检查网络或代理）"

    _cache["at"], _cache["data"] = now, data
    return dict(data)


def validate_download_url(url: str) -> str:
    """★ 下载白名单。这里放行 = 允许在用户机器上执行一个 exe，所以只认死地址。"""
    u = (url or "").strip()
    if not u.startswith(_ALLOWED_PREFIX):
        raise ValueError("更新包地址不在本仓库的 Release 里，已拒绝下载")
    if not _ASSET_RE.match(u.rsplit("/", 1)[-1]):
        raise ValueError("更新包文件名不是 PotatoTest-Setup-*.exe，已拒绝下载")
    return u


async def download_installer(url: str, dest_dir: Path) -> Path:
    """把安装包下到临时目录。返回本地路径。"""
    url = validate_download_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    dest = dest_dir / name
    tmp = dest.with_suffix(dest.suffix + ".part")
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                async for chunk in r.aiter_bytes(1 << 16):
                    fh.write(chunk)
    tmp.replace(dest)  # 下完才改名：中途断了不会留下一个"看起来完整"的半个 exe
    return dest


def launch_installer(path: Path) -> None:
    """静默安装，并**把时间留给应用自己退出**。

    为什么延迟 6 秒：Windows 锁着运行中的 exe，安装器直接覆盖会失败。
    这 6 秒里前端会关掉窗口 → 进程退出 → 安装器才动手。
    `DETACHED_PROCESS` 保证应用退出后安装器不被一起回收。
    """
    cmd = (
        f'timeout /t 6 /nobreak >nul & "{path}" '
        "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS"
    )
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(["cmd", "/c", cmd], creationflags=flags, close_fds=True)  # noqa: S603,S607
