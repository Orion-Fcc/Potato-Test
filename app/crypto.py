"""Symmetric encryption for stored test credentials.

Secrets (login_state snapshots, robot-account passwords, API keys) are encrypted at rest
with a Fernet key resolved by app/key_store.py. Ciphertext is what lands in the DB;
plaintext only ever exists in memory inside the executor at injection time.

Key resolution, DPAPI protection, self-checking and recovery live in app/key_store.py.
This module only wraps the resolved key in a cached Fernet instance. It never generates
a key: when the key is missing while ciphertext exists, callers get SecretKeyMissing and
the app enters recovery mode instead of silently making old data unreadable.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from app.key_store import SecretKeyMissing, get_key

__all__ = ["SecretKeyMissing", "decrypt", "encrypt", "secret_configured"]


@lru_cache
def _fernet() -> Fernet:
    key = get_key()
    try:
        return Fernet(key.encode())
    except Exception as exc:  # noqa: BLE001
        # 把"密钥不合法"翻译成一句能照着修的话。
        # 现场踩过：某个测试/环境把 POTATO_SECRET_KEY 写成 "x" * 32
        # （32 个字符 ≠ 32 字节的 url-safe base64），而报错是从 encrypt() 里抛出来的
        # `ValueError: Fernet key must be 32 url-safe base64-encoded bytes` ——
        # 调用方完全看不出问题出在密钥上，更不知道该改哪个变量。
        raise SecretKeyMissing(
            "POTATO_SECRET_KEY / .potato-secret.key 不是合法的 Fernet 密钥"
            f"（需要 32 字节的 url-safe base64 字符串，当前长度 {len(key)}）。"
            f"生成一个：python -c \"from cryptography.fernet import Fernet;"
            f' print(Fernet.generate_key().decode())" —— 底层错误：{exc}'
        ) from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


def secret_configured() -> bool:
    """True when a usable credential key exists; False means recovery mode."""
    from app import key_store

    return key_store.secret_available()
