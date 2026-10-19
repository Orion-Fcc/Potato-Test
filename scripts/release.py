"""发布一个版本：改版本号 → 校验一致性 → 打 Release → 上传安装包。

## 为什么需要这个脚本

应用的「检查更新」读的是 **GitHub Releases 的 latest**。要让这个功能真的工作，
GitHub 上必须有一个**带 `PotatoTest-Setup-*.exe` 资产**的 Release。手工点网页很容易漏：
忘了改版本号、忘了传资产、资产名写错 —— 任何一处错，用户的"检查更新"就会一直说
"已是最新"（或者一直提示更新但下不了）。

## 用法

    set GITHUB_TOKEN=ghp_xxxxxxxx          &:: 需要 repo 权限的 token
    python scripts\\release.py --bump 1.0.1  &:: 只改版本号（三处一起改）
    python scripts\\release.py --publish     &:: 用当前版本发 Release + 传安装包
    python scripts\\release.py --publish --notes-file notes.md

## 三处版本号必须一致

`app/version.py` / `pyproject.toml` / `installer/potato.iss` ——
`tests/test_updater.py` 会检查，这里也检查一遍（发布前拦住比事后查好）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_PY = ROOT / "app" / "version.py"
PYPROJECT = ROOT / "pyproject.toml"
ISS = ROOT / "installer" / "potato.iss"
GITHUB_API = "https://api.github.com"


def _read_repo() -> str:
    text = VERSION_PY.read_text(encoding="utf-8")
    m = re.search(r'GITHUB_REPO\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("app/version.py 里找不到 GITHUB_REPO")
    return m.group(1)


def _read_version() -> str:
    text = VERSION_PY.read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("app/version.py 里找不到 APP_VERSION")
    return m.group(1)


def bump(new: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        raise SystemExit(f"版本号要写成 x.y.z，收到 {new!r}")
    for path, pattern, repl in (
        (VERSION_PY, r'(APP_VERSION\s*=\s*)"[^"]+"', rf'\g<1>"{new}"'),
        (PYPROJECT, r'(?m)^version = "[^"]+"$', f'version = "{new}"'),
        (ISS, r'(#define\s+AppVer\s+)"[^"]+"', rf'\g<1>"{new}"'),
    ):
        text = path.read_text(encoding="utf-8")
        text2, n = re.subn(pattern, repl, text, count=1)
        if n != 1:
            raise SystemExit(f"{path.name} 里没找到要替换的版本号")
        path.write_text(text2, encoding="utf-8")
        print(f"  {path.relative_to(ROOT)} → {new}")
    verify()


def verify() -> str:
    """三处一致才算过 —— 这是"检查更新"能不能正确工作的前提。"""
    v = _read_version()
    py = re.search(r'(?m)^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"))
    iss = re.search(r'#define\s+AppVer\s+"([^"]+)"', ISS.read_text(encoding="utf-8"))
    ok = py and iss and py.group(1) == v and iss.group(1) == v
    if not ok:
        raise SystemExit(
            f"版本号不一致：version.py={v} pyproject={py and py.group(1)} iss={iss and iss.group(1)}"
        )
    print(f"版本号一致：{v}")
    return v


def _api(method: str, url: str, token: str, data: bytes | None = None, ctype: str = "application/json"):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", ctype)
    req.add_header("User-Agent", "potato-release-script")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"GitHub API {e.code}: {detail}") from e


def publish(notes: str) -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN（需要 repo 权限的 token）")
    v = verify()
    repo = _read_repo()
    tag = f"v{v}"

    installer = ROOT / "dist_desktop" / "installer" / f"PotatoTest-Setup-{v}.exe"
    if not installer.is_file():
        raise SystemExit(
            f"找不到安装包 {installer}\n先跑：python scripts\\build_desktop.py && ISCC.exe installer\\potato.iss"
        )

    print(f"创建 Release {tag} …")
    try:
        rel = _api(
            "POST",
            f"{GITHUB_API}/repos/{repo}/releases",
            token,
            json.dumps(
                {
                    "tag_name": tag,
                    "name": f"Potato Test {v}",
                    "body": notes or f"Potato Test {v}",
                    "draft": False,
                    "prerelease": False,
                }
            ).encode(),
        )
    except SystemExit as exc:
        # 422 already_exists：上次创建成功但资产没传上去，重跑时应当继续传，而不是整条失败。
        if "422" not in str(exc) and "already_exists" not in str(exc):
            raise
        print(f"Release {tag} 已存在，复用它继续上传资产")
        rel = _api("GET", f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", token)
    upload_url = str(rel["upload_url"]).split("{")[0]

    print(f"上传 {installer.name}（{installer.stat().st_size // 1024 // 1024} MB）…")
    # ★ 用 curl 上传，不用 urllib：190MB+ 的包走 urllib 会 write timeout
    #（实测 5 分钟超时，而 curl 只要 20 多秒）。上次就是因为这个，旧资产删了、新的没传上去。
    # --noproxy '*'：传大文件直连更稳（走本地代理容易被限速或中途断）。
    up = f"{upload_url}?name={urllib.parse.quote(installer.name)}"
    r = subprocess.run(
        [
            # --ssl-no-revoke：本机的证书吊销列表服务器常常连不上
            # （curl 报 CRYPT_E_REVOCATION_OFFLINE），不跳过吊销检查会直接失败。
            "curl", "-sS", "--ssl-no-revoke", "--noproxy", "*", "-m", "1800", "-X", "POST",
            "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/octet-stream",
            "-H", "Accept: application/vnd.github+json",
            "--data-binary", f"@{installer}",
            up,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        raise SystemExit(f"上传失败（curl {r.returncode}）：{(r.stderr or '')[:300]}")
    try:
        asset = json.loads(r.stdout)
    except Exception:
        raise SystemExit(f"上传返回看不懂：{(r.stdout or r.stderr or '')[:300]}")
    if not asset.get("browser_download_url"):
        raise SystemExit(f"上传没有返回下载地址：{str(asset)[:300]}")
    print(f"完成：{rel.get('html_url')}")
    print(f"资产：{asset.get('browser_download_url')}")
    print("\n注意：版本号已改，但代码还没提交 —— 记得 git commit + push。")


def main() -> int:
    ap = argparse.ArgumentParser(description="Potato Test 发布脚本")
    ap.add_argument("--bump", metavar="X.Y.Z", help="改版本号（三处一起改）")
    ap.add_argument("--publish", action="store_true", help="发 Release 并上传安装包")
    ap.add_argument("--notes-file", help="更新说明（Markdown 文件，可选）")
    a = ap.parse_args()

    if a.bump:
        bump(a.bump)
    if a.publish:
        notes = ""
        if a.notes_file:
            notes = Path(a.notes_file).read_text(encoding="utf-8")
        publish(notes)
    if not a.bump and not a.publish:
        verify()
        print("（什么都没做：用 --bump x.y.z 改版本，或 --publish 发布）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
