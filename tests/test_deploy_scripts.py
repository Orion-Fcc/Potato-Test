"""部署脚本本身的约束。

为什么给脚本写测试：README 里那句「一条命令就装好」是**对外承诺**，而破坏它的
方式都不报错，只是让下一个人多花半小时：

1. `deploy.sh` 没有可执行位 → `./scripts/deploy.sh` 直接 `Permission denied`。
   在 Windows 上开发根本感觉不到，因为大部分人用 `bash scripts/deploy.sh` 或
   `sh scripts/deploy.sh`，只有别人（macOS/Linux）才会踩到。
2. `deploy.sh` 变成 CRLF → bash 在某些环境下报
   `bad interpreter: /usr/bin/env bash^M`，错误信息完全指不到真正的原因。
3. `deploy.bat` 不是 GBK 或用了 LF → Windows 上中文全是乱码，甚至整行不执行。
   本仓库的 .bat 一律 GBK + CRLF（见 docs/deployment.md），这条很容易在
   "用编辑器随手改一下" 之后悄悄破掉。

这些断言只在 git 工作区里成立（`git ls-files -s`），所以在导出成 tarball
或非 git 目录里跑测试时会自动跳过，不会误报。

python -m pytest tests/test_deploy_scripts.py
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _index_mode(relpath: str) -> str | None:
    """git 索引里记录的文件模式；不在 git 工作区里返回 None。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-s", relpath],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.split()[0]


def test_deploy_sh_is_executable() -> None:
    """README 写的是 `./scripts/deploy.sh`，没有可执行位这句话就是错的。"""
    mode = _index_mode("scripts/deploy.sh")
    if mode is None:
        pytest.skip("不在 git 工作区里（可能是导出的 tarball）")
    assert mode == "100755", (
        f"scripts/deploy.sh 的 git 模式是 {mode}，不是 100755 —— "
        "别人执行 ./scripts/deploy.sh 会 Permission denied。"
        "修法：git update-index --chmod=+x scripts/deploy.sh"
    )


def test_deploy_sh_uses_lf_only() -> None:
    """CRLF 的 shell 脚本在 Linux/macOS 上会以 bad interpreter 收场。"""
    data = (ROOT / "scripts" / "deploy.sh").read_bytes()

    assert b"\r\n" not in data, "deploy.sh 里有 CRLF；.gitattributes 的 *.sh eol=lf 保证 checkout 是 LF"
    assert data.startswith(b"#!"), "缺少 shebang，无法直接执行"


def test_deploy_bat_is_gbk_and_crlf() -> None:
    """Windows 批处理必须 GBK + CRLF，否则中文乱码、行尾错乱。"""
    data = (ROOT / "scripts" / "deploy.bat").read_bytes()

    assert b"\r\n" in data, "deploy.bat 必须是 CRLF —— 记事本遇到纯 LF 会把它显示成一行"
    try:
        text = data.decode("gbk")
    except UnicodeDecodeError as exc:  # pragma: no cover - 只在编码被破坏时触发
        raise AssertionError(f"deploy.bat 不是 GBK：{exc}") from exc
    # 反向确认它**不是** UTF-8 中文（那才是乱码的真正来源）
    assert "一键部署" in text


def _bat_commands(text: str) -> list[str]:
    """批处理里真正会被执行的行（去掉注释与空行）。

    断言必须只看命令行：本文件的注释里就写着「不写 chcp」，用整篇文本去搜
    会把自己的说明文字当成违规 —— 这条测试第一版就是这么假失败了一次。
    """
    commands = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("rem") or stripped.startswith("::"):
            continue
        commands.append(stripped)
    return commands


def test_deploy_bat_never_calls_chcp() -> None:
    """chcp 会把控制台代码页切走，中文反而变乱码；仓库约定是不写它。"""
    text = (ROOT / "scripts" / "deploy.bat").read_bytes().decode("gbk")

    offenders = [c for c in _bat_commands(text) if "chcp" in c.lower()]
    assert not offenders, f"批处理里出现了 chcp：{offenders}"


def test_launcher_bat_derives_its_path(tmp_path) -> None:
    """启动器不能再硬编码项目路径 —— 项目搬一次家它就废了。"""
    text = (ROOT / "scripts" / "desktop" / "PotatoTest.bat").read_bytes().decode("gbk")

    assert "%~dp0" in text, "PROJ 应当从 %~dp0 推算，而不是写死"
    assert "PROJ_FALLBACK" in text, "桌面副本推不出来时要有一个显式的回落常量"


def test_secret_key_script_skips_an_existing_key(tmp_path) -> None:
    """生成器**绝不能**覆盖已有密钥。

    它会静默生效，代价却很重：已有测试凭据是拿旧密钥加密的，换了新密钥之后
    全部变成解不开的密文，而报错要等到下次用到那条凭据时才出现。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_secret_key", ROOT / "scripts" / "gen_secret_key.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    original = "POTATO_SECRET_KEY=" + "A" * 44
    assert mod.has_key(original) is True
    assert mod.has_key("POTATO_SECRET_KEY=") is False
    assert mod.has_key("POTATO_SECRET_KEY=   ") is False
    assert mod.has_key("# POTATO_SECRET_KEY=x") is False


def test_secret_key_script_keeps_the_file_shape(tmp_path) -> None:
    """写入时只改那一行的值：.env 是用户手写的文件，不能重排。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_secret_key", ROOT / "scripts" / "gen_secret_key.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out = mod.write_key("A=1\nPOTATO_SECRET_KEY=\nB=2\n", "NEWKEY")
    assert out == "A=1\nPOTATO_SECRET_KEY=NEWKEY\nB=2\n"

    appended = mod.write_key("A=1\n", "NEWKEY")
    assert appended == "A=1\nPOTATO_SECRET_KEY=NEWKEY\n"
