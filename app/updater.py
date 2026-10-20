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

import asyncio
import hashlib
from collections.abc import Callable
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
    """按"直连 → 系统代理"的顺序试，成功就记住这种方式。

    403/429/5xx 也算失败：GitHub 匿名 API 按出口 IP 限流（60 次/小时），
    直连出口被限流时换代理出口往往还能通。以前把 403 当成功返回，会把
    "直连"记成可用方式，后面下载大文件时连不上也不换路。
    """
    global _proxy_mode
    modes = [_proxy_mode] if _proxy_mode is not None else list(_TRUST_ENV_MODES)
    if _proxy_mode is not None:
        modes.append(not _proxy_mode)  # 记住的方式失败时，另一种兜底
    last: Exception | None = None
    fallback = None
    for mode in modes:
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, trust_env=mode
            ) as c:
                r = await c.get(url, headers=headers or {})
        except Exception as exc:  # noqa: BLE001 —— 换下一种方式再试
            last = exc
            continue
        if r.status_code in (403, 429) or r.status_code >= 500:
            fallback = r  # 限流/临时故障：换一种方式试，都不行再把这个响应交回去
            continue
        _proxy_mode = mode
        return r
    if fallback is not None:
        return fallback
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


# ★ 免费 GitHub 加速镜像（2026-10-20 实测：单连接约 105 KB/s，直连约 20 KB/s）。
#   镜像**只负责传 exe**：SHA-256 永远从 GitHub 官方地址取（`_fetch_checksum`
#   不走这里），否则镜像可以同时替换 exe 和校验值，等于没校验。
#   镜像失效/改地址不影响更新：列表最后一项永远是官方地址。
_UPDATE_MIRRORS = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
)


def expand_download_urls(url: str) -> list[str]:
    """GitHub 官方地址 → [镜像…, 官方]。白名单只认原始地址，镜像不扩大信任范围。"""
    official = validate_download_url(url)
    return [m + official for m in _UPDATE_MIRRORS] + [official]


async def _stream_download(
    url: str,
    tmp: Path,
    mode: bool,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """续传到 tmp。mode=True 表示走系统/环境代理。

    从已有的 .part 续传（Range 头）：这个网络到 GitHub 经常只有几十 KB/s，
    175 MB 要下一两个小时，中途抖一下就从头来等于永远下不完。
    服务端不认 Range（返回 200）时自动整包重下。
    120s 是**两次读之间**的上限，不是整包上限。
    """
    offset = tmp.stat().st_size if tmp.exists() else 0
    headers = {"User-Agent": f"PotatoTest/{APP_VERSION}"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=20.0),
        follow_redirects=True,
        trust_env=mode,
    ) as c:
        async with c.stream("GET", url, headers=headers) as r:
            if r.status_code == 416:
                # 已经下满（或旧分片过长）：交给 SHA-256 判断，别从头再下两小时。
                # 校验不过时 download_installer 会删掉它，下次自然重下。
                return
            if offset and r.status_code != 206:
                offset = 0  # 服务端没按 Range 返回：只能整包重下
            r.raise_for_status()
            length = r.headers.get("content-length")
            total = int(length) + offset if length else 0  # 分块传输时总长未知
            done = offset
            if on_progress is not None:
                on_progress(done, total)
            with tmp.open("ab" if offset else "wb") as fh:
                async for chunk in r.aiter_bytes(1 << 16):
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)


async def _download_resumable(
    urls: list[str], tmp: Path, on_progress: Callable[[int, int], None] | None = None
) -> None:
    """按"镜像 → 官方"逐个尝试，每个源再试"直连/代理"；断流保留 .part 续传。

    换源时丢弃旧分片：不同源的 .part 不能拼，哈希虽然能拦截，但白下一两个小时。
    """
    global _proxy_mode
    last: Exception | None = None
    modes = [False, True] if _proxy_mode is None else [_proxy_mode, not _proxy_mode]
    src = tmp.with_name(tmp.name + ".src")  # 记下 .part 属于哪个源
    for url in urls:
        if src.exists() and src.read_text(encoding="utf-8") != url:
            tmp.unlink(missing_ok=True)
        for mode in modes:
            src.write_text(url, encoding="utf-8")
            try:
                await _stream_download(url, tmp, mode, on_progress)
                _proxy_mode = mode
                src.unlink(missing_ok=True)
                return
            except httpx.HTTPStatusError as exc:
                # 4xx 跟连接方式无关（资产不存在/被拒），换方式没用，换下一个源。
                last = exc
                if exc.response.status_code < 500:
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
            log.warning(
                "更新包下载中断（%s，%s）：%s: %s",
                url.split("/")[2],
                "代理" if mode else "直连",
                type(last).__name__,
                last,
            )
            await asyncio.sleep(1)
    if last is None:
        raise RuntimeError("下载失败，且没有拿到具体原因")
    raise last


async def _fetch_checksum(url: str) -> str:
    """取安装包对应的 SHA-256。拿不到就当成校验失败（不能"没有哈希就放行"）。"""
    r = await _request(url, headers={"User-Agent": f"PotatoTest/{APP_VERSION}"})
    if r.status_code != 200:
        page = f"https://github.com/{GITHUB_REPO}/releases"
        raise ValueError(
            f"取不到更新包校验值（HTTP {r.status_code}），已拒绝安装。请到 {page} 手动下载。"
        )
    return r.text


async def download_installer(
    url: str,
    dest_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
    on_verify: Callable[[], None] | None = None,
) -> Path:
    """把安装包下到临时目录，**校验 SHA-256 通过后**才返回本地路径。"""
    url = validate_download_url(url)
    sha_url = checksum_url_for(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    dest = dest_dir / name
    tmp = dest.with_suffix(dest.suffix + ".part")
    # ★ 先取校验值再下大包：校验文件只有几百字节，拿不到就别浪费一两个小时下载。
    #   同时也让 _proxy_mode 先定下来，下载直接用能通的那条路。
    expected = _parse_sha256(await _fetch_checksum(sha_url))
    # 下载中断时保留 .part：下次点击"更新"从断点续传，而不是从零再来。
    # 先试免费镜像，最后回落 GitHub 官方；校验值始终来自官方。
    await _download_resumable(expand_download_urls(url), tmp, on_progress)
    if on_verify is not None:
        on_verify()  # 让界面从"下载中"切到"校验中"：大文件求哈希也要几秒
    # 175 MB 求哈希是纯 CPU：放线程里，别让界面在"校验中"卡住。
    actual = await asyncio.to_thread(_sha256_file, tmp)
    if actual != expected:
        tmp.unlink(missing_ok=True)  # 内容不对：绝不能留给后面的 launch
        tmp.with_name(tmp.name + ".src").unlink(missing_ok=True)
        raise ValueError(
            "更新包校验失败（SHA-256 不一致），已拒绝安装。"
            f"请到 https://github.com/{GITHUB_REPO}/releases 手动下载。"
        )
    tmp.replace(dest)  # 校验通过才改名：中途断了不会留下一个"看起来完整"的半个 exe
    return dest


# ---- 后台下载任务（2026-10-20）-------------------------------------------
# 安装包约 480 MB。原来 POST /app/update/install 同步等整个下载完成，而前端
# req() 默认 30 秒超时 —— 必然显示"request timed out after 30s"，下载其实还在跑。
# 现在 POST 只负责启动后台任务，前端轮询进度；HTTP 请求再也不会因为包大而超时。
_JOB_ACTIVE = ("downloading", "verifying", "installing")
_job: dict[str, object] = {
    "phase": "idle",
    "downloaded": 0,
    "total": 0,
    "percent": None,
    "error": None,
    "message": "",
}
_job_task: asyncio.Task | None = None


def install_status() -> dict:
    """当前/最近一次安装任务的状态（前端轮询用）。"""
    return dict(_job)


def _on_progress(done: int, total: int) -> None:
    _job["downloaded"] = done
    _job["total"] = total
    _job["percent"] = int(done * 100 / total) if total > 0 else None


async def _run_install(url: str, dest_dir: Path) -> None:
    global _job
    try:
        _job.update(phase="downloading", downloaded=0, total=0, percent=None, error=None)
        path = await download_installer(
            url, dest_dir, _on_progress, lambda: _job.update(phase="verifying")
        )
        _job.update(phase="installing", percent=100)
        launch_installer(path)
        _job.update(phase="done", message="更新包已下载，应用即将退出并自动安装")
    except Exception as exc:  # noqa: BLE001 —— 失败原因要回传给前端
        log.warning("下载更新包失败：%s: %s", type(exc).__name__, exc)
        _job.update(phase="error", error=str(exc))


def start_install(url: str, dest_dir: Path) -> dict:
    """启动后台下载。已在下载中则直接返回当前状态，避免重复下 480 MB。"""
    global _job, _job_task
    if str(_job.get("phase")) in _JOB_ACTIVE:
        return dict(_job)
    _job = {
        "phase": "downloading",
        "downloaded": 0,
        "total": 0,
        "percent": None,
        "error": None,
        "message": "",
    }
    _job_task = asyncio.create_task(_run_install(url, dest_dir))
    return dict(_job)


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
