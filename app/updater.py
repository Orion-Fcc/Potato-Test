"""检查 / 下载更新（2026-10-17）。

数据源：**GitHub Releases**（公开仓库，不需要 token）。
为什么不自己搭更新服务：一个人维护，多一个要部署的东西就多一个故障点；
Release 的 `latest` 接口天然给到"版本号 + 更新说明 + 资产下载地址"。

## 三条硬约束

1. **只有桌面版能一键安装**。网页版跑在服务器上 —— 它读不到用户的文件系统，
   也不该能往用户机器上装东西。`install_supported()` 用 `sys.frozen` 判断。
2. **只允许从本仓库的 Release 下载，且必须校验 SHA-256**。这个接口会
   "下载一个 exe 并执行"，如果不校验来源与内容，它就等于一个远程代码执行入口。
   所以下载地址必须匹配 `https://github.com/<本仓库>/releases/download/`，资产名
   必须是 `PotatoTest-Setup-*.exe`，而且同一次 Release 里必须有配套的
   `<安装包名>.sha256`（`scripts/release.py` 会生成并先上传它）。地址不对、校验
   文件缺失、哈希对不上 —— 一律拒绝，不做任何"尽力而为"。

   ★ 哈希清单和安装包走同一个下载通道，只能防"传输损坏/镜像被换"，**挡不住
   整包替换**。要挡那个，得给 exe 做 Authenticode 签名并校验签名 —— 当前未做。
3. **检查失败不算错误**。断网、GitHub 抽风、还没发过 Release —— 都不是用户的问题，
   返回 `error` 字段让界面温和提示即可，绝不抛 500 让应用显得坏了。
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import cast
from pathlib import Path

import httpx

from app.version import APP_VERSION, GITHUB_REPO

log = logging.getLogger("potato.updater")

API = "https://api.github.com"
_TIMEOUT = 8.0  # 检查更新是"顺手看一眼"，不能拖住界面
_CACHE_TTL = timedelta(minutes=10)
_ASSET_RE = re.compile(r"^PotatoTest-Setup-.*\.exe$", re.I)
# 校验文件与安装包**同名**，只多一个 .sha256 后缀（release.py 与本文件的约定）。
_CHECKSUM_RE = re.compile(r"^PotatoTest-Setup-.*\.exe\.sha256$", re.I)
# 下载白名单：只认本仓库 release 资产（GitHub 会 302 到 objects.githubusercontent.com，
# httpx 跟随重定向，但那已经是我们放行的地址之后的跳转，可以接受）
_ALLOWED_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"

_cache: dict[str, object] = {"at": None, "data": None}

# ★ 代理策略（2026-10-19，实测踩出来的坑）：**先直连，失败再信环境/系统代理**。
#
# 为什么不能像别处那样直接 trust_env=True：在 Windows 上，httpx 的 trust_env 会通过
# urllib 读**注册表**里的系统代理（HKCU\...\Internet Settings\ProxyServer）。
# 而用户机器上配的 127.0.0.1:7877 很可能**根本没在跑**（代理客户端没启动）——
# 于是每个请求都撞死在一个不存在的代理上，界面显示"连不上更新服务器（检查网络或代理）"，
# 但真相是直连明明通。
#
# 实测（同一台机器、同一时刻）：
#   trust_env=True  → ConnectError: All connection attempts failed  2.3s
#   trust_env=False → HTTP 200                                        0.7s
#
# 反过来也有需要代理的网络，所以做成"直连优先、代理兜底"，并记住本次进程里哪种可用。
_TRUST_ENV_MODES = (False, True)
_proxy_mode: bool | None = None


async def _request(url: str, headers: dict | None = None, timeout: float = _TIMEOUT):
    """按"直连 → 系统代理"的顺序试，成功就记住这种方式。"""
    global _proxy_mode
    modes = [_proxy_mode] if _proxy_mode is not None else list(_TRUST_ENV_MODES)
    last: Exception | None = None
    for mode in modes:
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, trust_env=mode
            ) as c:
                r = await c.get(url, headers=headers or {})
            _proxy_mode = mode
            return r
        except Exception as exc:  # noqa: BLE001 —— 换下一种方式再试
            last = exc
    if last is not None:
        raise last
    raise RuntimeError("没有可用的连接方式")


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
        return cast(dict, _cache["data"])

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
        r = await _request(
            f"{API}/repos/{GITHUB_REPO}/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                # GitHub 对**没有 User-Agent** 的请求返回 403。少了这个头，检查更新会
                # 显示"更新服务器返回 403"，看起来像仓库权限问题，其实是缺个头。
                "User-Agent": f"PotatoTest/{APP_VERSION}",
            },
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
        log.warning("检查更新失败（直连与代理都试过）：%s: %s", type(exc).__name__, exc)
        data["error"] = f"连不上更新服务器（{type(exc).__name__}）"

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


def checksum_url_for(installer_url: str) -> str:
    """安装包对应的 `.sha256` 资产地址（release.py 与本文件的约定：同名 + .sha256）。

    同样走白名单：校验文件也只能来自本仓库的 Release。
    """
    u = validate_download_url(installer_url) + ".sha256"
    if not _CHECKSUM_RE.match(u.rsplit("/", 1)[-1]):
        raise ValueError("校验文件地址不合法，已拒绝下载")
    return u


def _parse_sha256(text: str) -> str:
    """从校验文件里取 64 位十六进制。兼容 `sha256sum` 的 `<hex>  <name>` 格式。"""
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    if not m:
        raise ValueError("更新包的校验文件里没有 SHA-256 值，已拒绝安装")
    return m.group(1).lower()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _stream_download(url: str, tmp: Path) -> None:
    # 用 check() 定下来的方式（直连还是走代理），否则"检查得通、下载连不上"
    async with httpx.AsyncClient(
        timeout=120.0, follow_redirects=True, trust_env=bool(_proxy_mode)
    ) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                async for chunk in r.aiter_bytes(1 << 16):
                    fh.write(chunk)


async def _fetch_checksum(url: str) -> str:
    """取安装包对应的 SHA-256。拿不到就当成校验失败（不能"没有哈希就放行"）。"""
    r = await _request(url, headers={"User-Agent": f"PotatoTest/{APP_VERSION}"})
    if r.status_code != 200:
        page = f"https://github.com/{GITHUB_REPO}/releases"
        raise ValueError(
            f"取不到更新包校验值（HTTP {r.status_code}），已拒绝安装。请到 {page} 手动下载。"
        )
    return r.text


async def download_installer(url: str, dest_dir: Path) -> Path:
    """把安装包下到临时目录，**校验 SHA-256 通过后**才返回本地路径。"""
    url = validate_download_url(url)
    sha_url = checksum_url_for(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    dest = dest_dir / name
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        # 下载也放进 try：中途断网会留下半个 .part，同样是"未验证的文件"，一并清掉。
        await _stream_download(url, tmp)
        expected = _parse_sha256(await _fetch_checksum(sha_url))
        actual = _sha256_file(tmp)
        if actual != expected:
            raise ValueError(
                "更新包校验失败（SHA-256 不一致），已拒绝安装。"
                f"请到 https://github.com/{GITHUB_REPO}/releases 手动下载。"
            )
    except Exception:
        # 校验不通过/取不到校验值：删掉下载物，绝不把未验证的 exe 留给后面的 launch
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)  # 校验通过才改名：中途断了不会留下一个"看起来完整"的半个 exe
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
