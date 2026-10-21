"""发布一个版本：改版本号 → 校验一致性 → 打 Release → 上传安装包。

## 为什么需要这个脚本

应用的「检查更新」读的是 **GitHub Releases 的 latest**。要让这个功能真的工作，
GitHub 上必须有一个**带 `PotatoTest-Setup-*.msi` 资产**的 Release。手工点网页很容易漏：
忘了改版本号、忘了传资产、资产名写错 —— 任何一处错，用户的"检查更新"就会一直说
"已是最新"（或者一直提示更新但下不了）。

## 用法

    set GITHUB_TOKEN=ghp_xxxxxxxx          &:: 需要 repo 权限的 token
    python scripts\\release.py --bump 1.0.1  &:: 只改版本号（四处一起改）
    python scripts\\release.py --publish     &:: 用当前版本发 Release + 传安装包
    python scripts\\release.py --publish --notes-file notes.md
    python scripts\\release.py --check           &:: 只做发布前检查（token / 仓库 / Release / 资产）
    python scripts\\release.py --publish --replace  &:: 覆盖 Release 中已有同名资产

## 每次发布上传两个资产

`PotatoTest-Setup-x.y.z.msi` + 同名 `.sha256`。后者不是附赠品：`app/updater.py`
拿到安装包后**必须**比对哈希，校验文件缺失或对不上就拒绝执行。`--publish` 会
按"先校验文件、后安装包"的顺序上传，避免中途断掉留下"有包无哈希"的状态。

## 四处版本号必须一致

`app/version.py` / `pyproject.toml` / `installer/potato.nsi` ——
`tests/test_updater.py` 会检查，这里也检查一遍（发布前拦住比事后查好）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_PY = ROOT / "app" / "version.py"
PYPROJECT = ROOT / "pyproject.toml"
NSI = ROOT / "installer" / "potato.nsi"
WEB_PKG = ROOT / "web" / "package.json"
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
        (NSI, r'(!define\s+AppVer\s+)"[^"]+"', rf'\g<1>"{new}"'),
        (WEB_PKG, r'("version"\s*:\s*)"[^"]+"', rf'\g<1>"{new}"'),
    ):
        text = path.read_text(encoding="utf-8")
        text2, n = re.subn(pattern, repl, text, count=1)
        if n != 1:
            raise SystemExit(f"{path.name} 里没找到要替换的版本号")
        path.write_text(text2, encoding="utf-8")
        print(f"  {path.relative_to(ROOT)} → {new}")
    verify()


def verify() -> str:
    """四处一致才算过 —— 这是"检查更新"能不能正确工作的前提。"""
    v = _read_version()
    py = re.search(r'(?m)^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"))
    nsi = re.search(r'!define\s+AppVer\s+"([^"]+)"', NSI.read_text(encoding="utf-8"))
    pkg = re.search(r'"version"\s*:\s*"([^"]+)"', WEB_PKG.read_text(encoding="utf-8"))
    ok = py and nsi and pkg and py.group(1) == v and nsi.group(1) == v and pkg.group(1) == v
    if not ok:
        raise SystemExit(
            f"版本号不一致：version.py={v} pyproject={py and py.group(1)} "
            f"nsi={nsi and nsi.group(1)} web/package.json={pkg and pkg.group(1)}"
        )
    print(f"版本号一致：{v}")
    return v


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_checksum(installer: Path) -> Path:
    """写 `<安装包名>.sha256`（sha256sum 格式）。

    ★ 必须有：`app/updater.py` 现在**没有校验值就拒绝安装**。这不是可选项 ——
    漏传这个资产，用户的"一键更新"会直接报"取不到更新包校验值"。
    """
    digest = _sha256_of(installer)
    out = installer.with_name(installer.name + ".sha256")
    out.write_text(f"{digest}  {installer.name}\n", encoding="utf-8")
    print(f"  SHA-256 {digest}")
    return out


def _upload_asset(upload_url: str, token: str, path: Path) -> dict:
    """上传单个资产（curl）。返回 GitHub 的 asset 对象。

    ★ 用 curl 不上 urllib：190MB+ 的包走 urllib 会 write timeout
    （实测 5 分钟超时，而 curl 只要 20 多秒）。上次就是因为这个，旧资产删了、
    新的没传上去。
    """
    up = f"{upload_url}?name={urllib.parse.quote(path.name)}"
    r = subprocess.run(
        [
            # --ssl-no-revoke：本机的证书吊销列表服务器常常连不上
            # （curl 报 CRYPT_E_REVOCATION_OFFLINE），不跳过吊销检查会直接失败。
            "curl", "-sS", "--ssl-no-revoke", "--noproxy", "*", "-m", "1800", "-X", "POST",
            "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/octet-stream",
            "-H", "Accept: application/vnd.github+json",
            "--data-binary", f"@{path}",
            up,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        raise SystemExit(f"上传 {path.name} 失败（curl {r.returncode}）：{(r.stderr or '')[:300]}")
    try:
        asset = json.loads(r.stdout)
    except Exception:
        raise SystemExit(f"上传 {path.name} 返回看不懂：{(r.stdout or r.stderr or '')[:300]}")
    if not asset.get("browser_download_url"):
        raise SystemExit(f"上传 {path.name} 没有返回下载地址：{str(asset)[:300]}")
    return asset


def _api(
    method: str, url: str, token: str, data: bytes | None = None, ctype: str = "application/json"
):
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


def _token() -> str:
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN（需要 repo 权限的 token）")
    return token


def _release_assets(repo: str, token: str, rel: dict) -> list[dict]:
    return _api(
        "GET",
        f"{GITHUB_API}/repos/{repo}/releases/{rel['id']}/assets?per_page=100",
        token,
    )


def _default_notes(v: str) -> str:
    """发布说明：优先 RELEASE_NOTES.md，否则用上一个 tag 之后的 git log。"""
    notes_file = ROOT / "RELEASE_NOTES.md"
    if notes_file.is_file():
        print(f"使用发布说明：{notes_file.name}")
        return notes_file.read_text(encoding="utf-8").strip()

    prev = ""
    log = None
    try:
        tags = subprocess.run(
            ["git", "tag", "--sort=-v:refname"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for name in tags.stdout.split():
            if name.startswith("v") and name != f"v{v}":
                prev = name
                break
        rng = f"{prev}..HEAD" if prev else "-30"
        log = subprocess.run(
            ["git", "log", rng, "--no-merges", "--pretty=format:- %s (%h)"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        log = None

    if log is None or log.returncode != 0 or not log.stdout.strip():
        return f"Potato Test {v}"

    title = f"## 更新内容（{prev} → v{v}）" if prev else "## 更新内容"
    print(f"自动生成发布说明{('：' + prev + ' → v' + v) if prev else ''}")
    return f"{title}\n\n{log.stdout.strip()}\n"


def preflight(require_installer: bool = False) -> dict:
    """发布前检查：版本一致、token 有效、仓库可访问、Release/资产状态。

    在打包**之前**跑一遍，避免等几分钟打包完才发现 token 过期或资产冲突。
    """
    v = verify()
    repo = _read_repo()
    token = _token()

    print(f"检查 GitHub 仓库 {repo} …")
    try:
        data = _api("GET", f"{GITHUB_API}/repos/{repo}", token)
    except SystemExit as exc:
        raise SystemExit(
            f"仓库访问失败：{exc}\n请确认 token 未过期、有 repo 权限、仓库名正确。"
        ) from exc
    print(f"  仓库可访问（私有：{'是' if data.get('private') else '否'}）")

    tag = f"v{v}"
    rel = None
    try:
        rel = _api("GET", f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", token)
    except SystemExit as exc:
        # 404 = 还没建 Release，这是正常状态
        if "404" not in str(exc):
            raise

    assets = _release_assets(repo, token, rel) if rel else []
    if rel:
        print(f"  Release {tag} 已存在：{rel.get('html_url')}")
        if assets:
            print("  已有资产：" + "、".join(str(a.get("name")) for a in assets))
            print("  （同名资产需用 --replace 覆盖，否则发布会失败）")
    else:
        print(f"  Release {tag} 尚未创建")

    installer = ROOT / "dist_desktop" / "installer" / f"PotatoTest-Setup-{v}.msi"
    if require_installer:
        if not installer.is_file():
            raise SystemExit(f"找不到安装包：{installer}\n先打包，或确认当前版本号是 {v}。")
        print(f"  安装包：{installer.name}（{installer.stat().st_size // 1024 // 1024} MB）")

    return {
        "version": v,
        "repo": repo,
        "token": token,
        "tag": tag,
        "release": rel,
        "assets": assets,
        "installer": installer,
    }


def publish(notes: str, replace: bool = False) -> None:
    info = preflight(require_installer=True)
    v = info["version"]
    repo = info["repo"]
    token = info["token"]
    tag = info["tag"]
    installer = info["installer"]
    rel = info["release"]

    if rel is None:
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
            # 422 already_exists：上次创建成功但资产没传完，接着传资产即可。
            if "422" not in str(exc) and "already_exists" not in str(exc):
                raise
            print(f"Release {tag} 已存在，继续上传资产")
            rel = _api("GET", f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", token)
    upload_url = str(rel["upload_url"]).split("{")[0]

    # 先写校验文件再传安装包：顺序不能反。安装包一直在变，中途断掉会出现
    # “有安装包、没校验值”的 Release —— 新版 updater 遇到这种状态会拒绝执行。
    checksum = _write_checksum(installer)
    wanted = {checksum.name, installer.name}
    current = {a.get("name"): a for a in _release_assets(repo, token, rel)}
    clash = [name for name in wanted if name in current]
    if clash:
        if not replace:
            raise SystemExit(
                "Release 已有同名资产：" + "、".join(clash)
                + "\n用 --replace 覆盖（先删除旧资产再上传），或先改版本号。"
            )
        for name in clash:
            print(f"删除旧资产 {name} …")
            _api(
                "DELETE",
                f"{GITHUB_API}/repos/{repo}/releases/assets/{current[name]['id']}",
                token,
            )

    print(f"上传 {checksum.name} …")
    _upload_asset(upload_url, token, checksum)

    print(f"上传 {installer.name}（{installer.stat().st_size // 1024 // 1024} MB）…")
    asset = _upload_asset(upload_url, token, installer)
    print(f"完成：{rel.get('html_url')}")
    print(f"资产：{asset.get('browser_download_url')}")
    print(f"校验：{checksum.name} · {_sha256_of(installer)}")
    print("\n注意：版本号已改，但代码还没提交 —— 记得 git commit + push。")


def main() -> int:
    ap = argparse.ArgumentParser(description="Potato Test 发布脚本")
    ap.add_argument("--bump", metavar="X.Y.Z", help="改版本号（四处一起改）")
    ap.add_argument("--check", action="store_true", help="只做发布前检查（token、仓库、Release、资产）")
    ap.add_argument("--publish", action="store_true", help="发 Release 并上传安装包")
    ap.add_argument("--replace", action="store_true", help="覆盖 Release 里已有的同名资产")
    ap.add_argument("--notes-file", help="更新说明（Markdown 文件，可选）")
    a = ap.parse_args()

    if a.bump:
        bump(a.bump)
    if a.check:
        preflight(require_installer=False)
    if a.publish:
        if a.notes_file:
            notes = Path(a.notes_file).read_text(encoding="utf-8")
        else:
            notes = _default_notes(_read_version())
        publish(notes, replace=a.replace)
    if not (a.bump or a.check or a.publish):
        verify()
        print("什么都没做。用 --bump x.y.z、--check 或 --publish。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
