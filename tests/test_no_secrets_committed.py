"""公开仓库不能装着自己的配置与凭据。

这是**长期**护栏，不是一次性检查。仓库是公开的，而能泄漏的东西一次 `git add -A`
就够了：`.env`（LLM key、飞书凭据）、`potato.db`（加密的测试凭据 + 被测系统地址）、
`.potato-secret.key`（解开上面那些凭据的钥匙）、`profiles/`（浏览器登录态）、
`artifacts/`（录屏与截图）。这些一旦推上去就删不干净了 —— 历史是公开的。

前半部分是**对本仓库的真实检查**（跑在 `git ls-files` 上），
后半部分是给检查器自己的单元测试：一个永远报"OK"的检查器比没有更糟，
所以它的判据必须被钉住。

python -m pytest tests/test_no_secrets_committed.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_secrets", ROOT / "scripts" / "check_secrets.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, errors="replace"
    )
    if out.returncode != 0:
        pytest.skip("不在 git 工作区里")
    return [p for p in out.stdout.splitlines() if p.strip()]


# --------------------------------------------------- 对本仓库的真实检查


def test_no_dangerous_path_is_tracked() -> None:
    mod = _load()
    bad = mod.dangerous_tracked_paths(_tracked())

    assert not bad, (
        "这些文件被跟踪了，公开仓库会连配置和凭据一起发出去：" + ", ".join(bad)
    )


def test_env_example_is_tracked_but_env_is_not() -> None:
    """模板要在，真配置不能要 —— 这一对搞反了是最常见的形态。"""
    files = set(_tracked())

    assert ".env.example" in files, "没有 .env.example，别人不知道怎么配"
    assert ".env" not in files, ".env 被跟踪了 —— 里面是真实密钥"


def test_no_tracked_file_looks_like_a_secret() -> None:
    mod = _load()
    offenders = []
    for path in _tracked():
        # 与检查器口径一致：规则清单与本测试文件按定义含有那些串（见 SELF_EXEMPT）。
        # 这里不跟随就会和检查器结论不一致 —— 那才是真正的坏味道：
        # 两处判断不同，任何一次改动都可能让其中一个变成"永远红"或"永远绿"。
        if path in mod.SELF_EXEMPT:
            continue
        full = ROOT / path
        if not full.is_file():
            continue
        try:
            text = full.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        hits = mod.shape_hits(text) + mod.identifier_hits(text)
        if hits:
            offenders.append(f"{path}: {', '.join(hits)}")

    assert not offenders, "跟踪的文件里有像密钥或真实标识的内容：\n" + "\n".join(offenders)


def test_configured_env_values_are_not_in_any_tracked_file() -> None:
    """`.env` 里真正配着的值，不能出现在任何被跟踪的文件里。"""
    mod = _load()
    env = ROOT / ".env"
    if not env.is_file():
        pytest.skip("没有 .env（干净环境，无需检查）")

    values = mod.collect_env_values(env.read_text(encoding="utf-8", errors="replace"))
    if not values:
        pytest.skip(".env 里没有可用的敏感值")

    offenders = []
    for path in _tracked():
        full = ROOT / path
        if not full.is_file():
            continue
        text = full.read_bytes().decode("utf-8", errors="replace")
        hits = mod.value_hits(text, values)
        if hits:
            offenders.append(f"{path}: {', '.join(hits)}")

    assert not offenders, "已配置的密钥值出现在被跟踪的文件里：\n" + "\n".join(offenders)


# --------------------------------------------------- 检查器自身的单元测试


def test_low_entropy_values_are_skipped() -> None:
    """`POSTGRES_PASSWORD=potato` 与项目同名 —— 拿它去搜会命中几十个文件。

    这条来自真实踩坑：第一版审计用"值匹配"跑，报出 40 多个"泄漏"，
    全是 `potato` 这个词。一份会喊狼来了的报告，等于没有报告。
    """
    mod = _load()

    assert mod.is_low_entropy("potato") is True
    assert mod.is_low_entropy("changeme") is True
    assert mod.is_low_entropy("xxxxxxxxxxxxxxxx") is True
    assert mod.is_low_entropy("short") is True

    assert mod.is_low_entropy("sk-a1B2c3D4e5F6g7H8i9J0k1L2") is False
    assert mod.is_low_entropy("7f3a9c1e5b2d8f4a6c0e9b7d3f1a5c8e") is False


def test_dangerous_paths_cover_the_real_ones() -> None:
    mod = _load()

    for bad in (
        ".env",
        ".potato-secret.key",
        "potato.db",
        "potato.db.bak-before-defaults-align-20261004-160128",
        "profiles/project_2/slot0/Default/Cookies",
        "artifacts/run-26/case-7.mp4",
        ".workbuddy/backup/cases-project1.json",
        "logs/potato.log",
        "project_settings_backup.json",
        "web/node_modules/x/index.js",
    ):
        assert mod.dangerous_tracked_paths([bad]) == [bad], f"{bad} 应当被拦下"

    for ok in (".env.example", "app/main.py", "web/dist/index.html", "docs/USAGE.md"):
        assert mod.dangerous_tracked_paths([ok]) == [], f"{ok} 不该被拦"


def test_shape_scan_catches_real_looking_ids_and_ignores_placeholders() -> None:
    mod = _load()

    assert "飞书 App ID" in mod.shape_hits("FEISHU_APP_ID=cli_a1b2c3d4e5f6g7h8")
    assert "API key（sk- 前缀）" in mod.shape_hits("key = sk-abcdefghijklmnopqrstuvwx")
    assert "私钥文件内容" in mod.shape_hits("-----BEGIN RSA PRIVATE KEY-----")

    # 文档里的占位写法必须放过，否则 README 一写示例就红
    assert mod.shape_hits("FEISHU_APP_ID=cli_xxx") == []
    assert mod.shape_hits("GATEWAY_API_KEY=sk-xxx") == []


def test_env_value_collection_keeps_secrets_and_drops_placeholders() -> None:
    mod = _load()

    text = "\n".join(
        [
            "# 注释行",
            "POSTGRES_PASSWORD=potato",           # 占位值，跳过
            "GATEWAY_BASE_URL=https://x/v1",      # 名字不像凭据，跳过
            "GATEWAY_API_KEY=sk-a1B2c3D4e5F6g7H8i9J0",
            "FEISHU_APP_SECRET=Zx9Qw8Er7Ty6Ui5Op4As3Df2",
            "POTATO_SECRET_KEY=",                 # 空值，跳过
        ]
    )

    got = mod.collect_env_values(text)

    assert "GATEWAY_API_KEY" in got
    assert "FEISHU_APP_SECRET" in got
    assert "POSTGRES_PASSWORD" not in got, "占位值被当成密钥会让报告全是噪音"
    assert "GATEWAY_BASE_URL" not in got
    assert "POTATO_SECRET_KEY" not in got


def test_identifier_scan_catches_the_projects_own_strings() -> None:
    """网关地址与客户名不长得像密钥，只能靠显式清单守。

    这条来自一次真实的漏判：第一版审计只找"密钥形状"，结论是"干净"，
    却把 `api.example.com` 放过去了 —— 它不长得像密钥，但它说明了
    "谁在用哪家网关"，而且比密钥更难轮换。
    """
    mod = _load()

    hits = mod.identifier_hits("GATEWAY_BASE_URL=https://api.example.com/v1")
    assert any("网关" in h for h in hits), hits

    assert mod.identifier_hits("被测系统 192.0.2.10:8600") != []
    assert mod.identifier_hits("tenantName=示例培训") != []
    assert mod.identifier_hits("D:\\<WORK_DIR>\\Potato_Test") != []

    # 脱敏后的写法必须放过，否则脱敏完检查器还在报警，就没人信它了
    assert mod.identifier_hits("https://api.example.com/v1") == []
    assert mod.identifier_hits("被测系统 192.0.2.10:8600") == []
    assert mod.identifier_hits("MODEL=example-model") == []


def test_value_hits_finds_the_offending_keys() -> None:
    mod = _load()

    hits = mod.value_hits(
        'const key = "sk-a1B2c3D4e5F6g7H8i9J0";',
        {"GATEWAY_API_KEY": "sk-a1B2c3D4e5F6g7H8i9J0", "OTHER": "not-here"},
    )

    assert hits == ["GATEWAY_API_KEY"]


def test_the_guard_exempts_only_itself() -> None:
    """豁免必须窄到"只有规则文件与它的测试"。

    规则清单里按定义就写着那些串，不豁免的话这个护栏永远无法提交 ——
    一个提交不了的护栏等于没有。但豁免一旦写成"忽略 scripts/ 目录"，
    就会变成一个真正的洞：谁把凭据放进 scripts/ 就再也没人拦。
    """
    mod = _load()

    assert set(mod.SELF_EXEMPT) == {
        "scripts/check_secrets.py",
        "tests/test_no_secrets_committed.py",
    }
    for path in ("app/config.py", "scripts/deploy.sh", "docs/USAGE.md", ".env"):
        assert path not in mod.SELF_EXEMPT, f"{path} 不该被豁免"
