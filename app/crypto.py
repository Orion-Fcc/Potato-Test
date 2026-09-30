"""Symmetric encryption for stored test credentials.

Secrets (login_state snapshots, robot-account passwords, API keys) are encrypted at rest
with a Fernet key from POTATO_SECRET_KEY. Ciphertext is what lands in the DB; plaintext
only ever exists in memory inside the executor at injection time.

If POTATO_SECRET_KEY is blank the key is auto-generated on first use and persisted next to
the SQLite DB (``.potato-secret.key``), so a fresh clone runs with zero setup. Set the env
var explicitly for any deployment that needs the ciphertext to survive machine moves.

Generate a key by hand:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import get_settings

_KEYFILE = ".potato-secret.key"


class SecretKeyMissing(RuntimeError):
    pass


def _autogen_keyfile() -> Path:
    """Where an auto-generated key lives. Kept beside the SQLite DB when we can find it."""
    url = get_settings().database_url or ""
    if url.startswith("sqlite") and "///" in url:
        raw = url.split("///", 1)[1] or ""
        raw = raw.split("?", 1)[0]
        if raw and raw != ":memory:":
            return Path(raw).resolve().parent / _KEYFILE
    return Path.cwd() / _KEYFILE


def _resolve_key() -> str:
    """Env var wins; otherwise fall back to (or create) the on-disk auto-generated key."""
    key = (get_settings().secret_key or "").strip()
    if key:
        return key

    path = _autogen_keyfile()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    generated = Fernet.generate_key().decode()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0600 where the OS honours it; Windows ignores the mode bits, which is acceptable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(generated)
    return generated


@lru_cache
def _fernet() -> Fernet:
    return Fernet(_resolve_key().encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


def secret_configured() -> bool:
    """Always true now — a missing key is auto-generated rather than disabling storage."""
    return True

