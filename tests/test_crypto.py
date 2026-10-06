"""Credential encryption round-trip. Ciphertext must differ from plaintext and
decrypt back exactly. Runs without a browser/DB."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet


def test_crypto_roundtrip() -> None:
    os.environ["POTATO_SECRET_KEY"] = Fernet.generate_key().decode()
    from app import crypto
    from app.config import get_settings

    get_settings.cache_clear()
    crypto._fernet.cache_clear()

    secret = '{"cookies":[{"name":"session","value":"s3cr3t"}]}'
    ct = crypto.encrypt(secret)
    assert ct != secret  # stored value is ciphertext, not plaintext
    assert "s3cr3t" not in ct
    assert crypto.decrypt(ct) == secret
    assert crypto.secret_configured() is True


def test_a_malformed_key_fails_with_an_actionable_error() -> None:
    """密钥不合法时，报错必须指向 POTATO_SECRET_KEY，而不是 cryptography 的裸异常。

    现场踩过（2026-10-06）：某个测试把密钥设成 "x" * 32 —— 32 个字符，不是
    32 字节的 url-safe base64（需要 44 个字符）。真正调用 encrypt() 的地方只看到
    `ValueError: Fernet key must be 32 url-safe base64-encoded bytes`，
    完全联想不到是环境变量写错了，也不知道该去改哪。

    把"读不懂的底层异常"翻译成"照着修就能好的一句话"，是这类密钥配置错误
    唯一值得付的成本。
    """
    import pytest

    os.environ["POTATO_SECRET_KEY"] = "x" * 32
    from app import crypto
    from app.config import get_settings

    get_settings.cache_clear()
    crypto._fernet.cache_clear()

    with pytest.raises(crypto.SecretKeyMissing) as ei:
        crypto.encrypt("anything")

    msg = str(ei.value)
    assert "POTATO_SECRET_KEY" in msg, "报错必须点名是哪个变量"
    assert "Fernet.generate_key" in msg, "报错必须给出生成合法密钥的命令"


if __name__ == "__main__":
    test_crypto_roundtrip()
    print("crypto self-check: OK")
