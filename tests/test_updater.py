"""应用更新（`app/updater.py`）的测试（2026-10-17）。

这组守的是三件事，缺一条这个功能就会变成风险：

1. **版本号只有一个源** —— 三处（`app/version.py` / `pyproject.toml` /
   `installer/potato.iss`）不一致，"检查更新"就会永远判断错；
2. **下载白名单不能漏** —— 这个接口会下载并执行一个 exe，白名单漏了就是远程代码执行；
3. **查不到更新不能算错误** —— 断网 / 还没发 Release 都是正常状态，不该让应用显得坏了。
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import updater  # noqa: E402
from app.version import APP_VERSION, GITHUB_REPO  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """模块级缓存，测试之间必须清掉，否则第一条的结果会污染后面几条。"""
    updater._cache["at"] = None
    updater._cache["data"] = None
    yield


# ---- 1. 版本号单一来源 ------------------------------------------------------
def test_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', text, flags=re.M)
    assert m, "pyproject.toml 里没有 version"
    assert m.group(1) == APP_VERSION, "pyproject 与 app/version.py 版本不一致"


def test_version_matches_installer():
    text = (ROOT / "installer" / "potato.iss").read_text(encoding="utf-8")
    m = re.search(r'#define\s+AppVer\s+"([^"]+)"', text)
    assert m, "potato.iss 里没有 #define AppVer"
    assert m.group(1) == APP_VERSION, "安装包与 app/version.py 版本不一致"


# ---- 2. 版本比较 ------------------------------------------------------------
@pytest.mark.parametrize(
    "latest,current,expect",
    [
        ("1.0.1", "1.0.0", True),
        ("1.10.0", "1.9.9", True),  # 按数字比，不是按字符串
        ("2.0.0", "1.99.99", True),
        ("1.0.0", "1.0.0", False),
        ("0.9.9", "1.0.0", False),
    ],
)
def test_is_newer(latest, current, expect):
    assert updater.is_newer(latest, current) is expect


# ---- 3. 下载白名单 ----------------------------------------------------------
def test_download_whitelist_accepts_own_release():
    url = f"https://github.com/{GITHUB_REPO}/releases/download/v1.0.1/PotatoTest-Setup-1.0.1.exe"
    assert updater.validate_download_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/PotatoTest-Setup-1.0.1.exe",  # 别的域名
        f"https://github.com/someone/{GITHUB_REPO}/releases/download/v1/PotatoTest-Setup-1.exe",
        f"https://github.com/{GITHUB_REPO}/releases/download/v1.0.1/payload.exe",  # 名字不对
        f"https://github.com/{GITHUB_REPO}/releases/download/v1.0.1/notes.txt",
        "",
    ],
)
def test_download_whitelist_rejects_anything_else(url):
    """★ 这个接口会执行下载下来的 exe —— 白名单必须只认本仓库的 Setup 包。"""
    with pytest.raises(ValueError):
        updater.validate_download_url(url)


def test_pick_asset_ignores_unrelated_files():
    assets = [
        {"name": "checksums.txt", "browser_download_url": "x"},
        {"name": "PotatoTest-Setup-1.0.1.exe", "browser_download_url": "y"},
        {"name": "source.zip", "browser_download_url": "z"},
    ]
    assert updater._pick_asset(assets)["name"] == "PotatoTest-Setup-1.0.1.exe"
    assert updater._pick_asset([{"name": "a.txt"}]) is None


# ---- 4. 查不到更新 = 正常状态 -----------------------------------------------
class _FakeResp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._p = payload or {}

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **kw):
        return self._resp


def _patch_client(monkeypatch, resp):
    monkeypatch.setattr(updater.httpx, "AsyncClient", lambda *a, **kw: _FakeClient(resp))


async def _run_check():
    return await updater.check()


def test_no_release_yet_is_not_an_error(monkeypatch):
    _patch_client(monkeypatch, _FakeResp(404))
    data = updater.check.__wrapped__ if hasattr(updater.check, "__wrapped__") else None
    import asyncio

    out = asyncio.run(_run_check())
    assert out["has_update"] is False
    assert "还没有发布过版本" in (out["error"] or "")
    assert data is None or True


def test_newer_release_is_detected(monkeypatch):
    _patch_client(
        monkeypatch,
        _FakeResp(
            200,
            {
                "tag_name": "v9.9.9",
                "body": "修了几个 bug",
                "html_url": "https://github.com/x/y/releases/tag/v9.9.9",
                "assets": [
                    {
                        "name": "PotatoTest-Setup-9.9.9.exe",
                        "browser_download_url": f"https://github.com/{GITHUB_REPO}/releases/download/v9.9.9/PotatoTest-Setup-9.9.9.exe",
                    }
                ],
            },
        ),
    )
    import asyncio

    out = asyncio.run(_run_check())
    assert out["has_update"] is True
    assert out["latest"] == "9.9.9"
    assert out["download_url"].endswith(".exe")
    assert out["error"] is None


def test_network_failure_is_soft(monkeypatch):
    class _Boom(_FakeClient):
        async def get(self, *a, **kw):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(updater.httpx, "AsyncClient", lambda *a, **kw: _Boom(None))
    import asyncio

    out = asyncio.run(_run_check())
    assert out["has_update"] is False
    assert out["error"], "断网时必须有可读的提示，而不是抛异常"
