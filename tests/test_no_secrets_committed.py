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
import os
import pathlib
import subprocess
import tempfile

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


def _load_with_rules(tmp_path, monkeypatch):
    """在 tmp 里放一份清单并切 cwd，让检查器加载**夹具**规则。

    为什么必须用夹具、而不是开发者本机那份 .secrets.local.json：
    依赖未跟踪文件的测试，在别人的新克隆里会静默失去意义（变成"什么都没查"），
    而这种测试全绿的时候恰恰最危险 —— 它看起来在保护你，实际没有。
    """
    import importlib.util
    import json

    (tmp_path / ".secrets.local.json").write_text(
        json.dumps(
            {
                "redactions": [
                    ["real-gw.internal.example", "api.example.com", "网关主机名"],
                    ["real-model-x", "example-model", "模型名"],
                    ["10.9.8.7", "192.0.2.10", "内网地址"],
                    ["示例客户", "示例", "客户名"],
                ],
                "extra_identifiers": [["192.168.1.", "常见内网段"]],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "cs_fixture", ROOT / "scripts" / "check_secrets.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_repo_ships_no_identifier_list_of_its_own() -> None:
    """★ 仓库本身不许携带本项目的真实标识清单。

    这条来自一次真实事故：清单原本写在 `check_secrets.py` 里，于是自动脱敏把
    清单里的真实串一起换成了占位符 —— 检查器开始用 `role`、`示例`、
    `api.example.com` 这类**占位符和常用词**去搜正常文档，实测 190 处假警报。
    一个永远报红的检查器等于没有检查器。

    所以清单移到 .secrets.local.json（gitignore），仓库只提供机制。
    """
    import importlib.util

    with tempfile.TemporaryDirectory() as empty:
        spec = importlib.util.spec_from_file_location(
            "cs_clean", ROOT / "scripts" / "check_secrets.py"
        )
        clean = importlib.util.module_from_spec(spec)
        cwd = os.getcwd()
        try:
            os.chdir(empty)  # 空目录 => 没有任何本机清单
            spec.loader.exec_module(clean)
        finally:
            os.chdir(cwd)

    assert clean.REDACTIONS == [], "干净环境里不该凭空长出替换规则"
    assert clean.KNOWN_IDENTIFIERS == []
    assert clean.identifier_hits("host=whatever-gateway.example") == []


def test_the_local_rules_file_is_never_tracked() -> None:
    """本机清单与它的模板：模板要入库，清单绝不能。"""
    files = set(_tracked())

    assert ".secrets.local.json" not in files, "本机标识清单被跟踪了 —— 它正是不能公开的东西"
    assert "scripts/secrets.local.example.json" in files, "缺少模板，别人不知道怎么配"


def test_identifier_scan_uses_the_local_rules(tmp_path, monkeypatch) -> None:
    mod = _load_with_rules(tmp_path, monkeypatch)

    assert mod.identifier_hits("GATEWAY_BASE_URL=https://real-gw.internal.example/v1") != []
    assert mod.identifier_hits("被测系统 10.9.8.7:8600") != []
    assert mod.identifier_hits("tenant=示例客户") != []

    # 脱敏后的占位写法必须放过，否则脱敏完检查器还在报警，就没人信它了
    assert mod.identifier_hits("https://api.example.com/v1") == []
    assert mod.identifier_hits("被测系统 192.0.2.10:8600") == []
    assert mod.identifier_hits("MODEL=example-model") == []


def test_extra_identifiers_are_checked_but_not_replaced(tmp_path, monkeypatch) -> None:
    mod = _load_with_rules(tmp_path, monkeypatch)

    assert mod.identifier_hits("网关在 192.168.1.5 上") != []
    _out, applied = mod.redact_bytes(b"host 192.168.1.5")
    assert applied == [], "只查项不该被自动替换（它需要人工确认）"


def test_redaction_rules_are_ordered_by_length_when_applied(tmp_path, monkeypatch) -> None:
    """长规则必须先生效，否则会把长串截成半截。

    `real-model-x` 先跑，`real-model-xyz` 就变成 `SHORTyz`。应用时按长度降序。
    """
    mod = _load_with_rules(tmp_path, monkeypatch)
    mod.REDACTIONS[:] = [("real-model-x", "SHORT", "短的"), ("real-model-xyz", "LONG", "长的")]

    out, _ = mod.redact_bytes(b"model=real-model-xyz")
    assert out == b"model=LONG", out


def test_redaction_handles_gbk_encoded_blobs(tmp_path, monkeypatch) -> None:
    """历史里的 .bat 是 GBK；只按 UTF-8 替换，中文词会原样留下。"""
    mod = _load_with_rules(tmp_path, monkeypatch)

    out, applied = mod.redact_bytes("示例客户的租户".encode("gbk"))

    assert out == "示例的租户".encode("gbk"), out
    assert applied and "gbk" in applied[0]


def test_redact_history_refuses_to_run_without_rules(tmp_path, monkeypatch) -> None:
    """没有规则时必须**拒绝**运行，而不是"成功地什么都没做"。

    空跑是这里最坏的失败形态：filter-branch 会照常重写所有提交、输出漂亮的进度、
    推出一个什么都没变的分支，而人以为已经脱敏了。
    """
    import importlib.util

    monkeypatch.chdir(tmp_path)  # 空目录 => 没有 .secrets.local.json
    spec = importlib.util.spec_from_file_location(
        "redact_history", ROOT / "scripts" / "redact_history.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.main(["--print-rules"]) == 2
    assert mod.main(["--index-filter"]) == 2
    assert mod.main(["--msg-filter"]) == 2


def test_known_identifiers_is_derived_not_duplicated(tmp_path, monkeypatch) -> None:
    """KNOWN_IDENTIFIERS 必须由 REDACTIONS 推导，不能再单独维护一份。

    两份清单不一致时不会报错 —— 只会表现为"检查说干净、实际没抹掉"，
    而这种问题要等到有人真的去翻历史才会发现。所以这里把它钉成一条不变式。
    """
    mod = _load_with_rules(tmp_path, monkeypatch)

    derived = [old for old, _new, _why in mod.REDACTIONS] + [
        n for n, _why in mod.EXTRA_IDENTIFIERS
    ]
    assert mod.KNOWN_IDENTIFIERS == derived, (
        "KNOWN_IDENTIFIERS 与 REDACTIONS 脱钩了 —— 检查器会漏掉一部分真实标识"
    )


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
