"""Credential-key lifecycle: resolve, protect, verify, persist.

This module owns the Fernet key that encrypts credentials at rest. The old
implementation in app/crypto.py silently generated a new key whenever the key file
was missing, which made every existing ciphertext permanently undecryptable while
the process kept reporting success. That is the failure this module exists to
prevent.

Invariants:

1. If the database already contains ciphertext, the process NEVER generates or
   replaces a key. A missing key is a user-visible recovery state, not a silent
   reset.
2. A new key must pass a Fernet format check and one encrypt/decrypt round-trip
   before it is written to disk.
3. Source priority: POTATO_SECRET_KEY > DPAPI-protected file > legacy plaintext file.
4. The credential key is independent from the JWT signing key. Losing credentials
   must not prevent an admin from logging in to the recovery page.

On Windows the key file is protected with user-scoped DPAPI (CryptProtectData),
using only the standard library. Other platforms fall back to a 0600 plaintext file.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

DPAPI_FILE = ".potato-secret.key.dpapi"
PENDING_FILE = ".potato-secret.key.pending"
LEGACY_FILE = ".potato-secret.key"
JWT_FILE = ".potato-jwt.key.dpapi"

_DPAPI_MAGIC = b"POTATO-DPAPI-1:"
_ENTROPY = b"potato-test/credential-key/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_SELF_CHECK = "potato-key-self-check"

_ENV_HINT = (
    '生成一个：python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


class SecretKeyMissing(RuntimeError):
    """The credential key is unavailable, invalid, or does not match stored data."""


class KeyState(str, Enum):
    OK_ENV = "ok_env"
    OK_DPAPI = "ok_dpapi"
    OK_LEGACY_MIGRATED = "ok_legacy_migrated"
    OK_GENERATED = "ok_generated"
    MISSING = "missing"       # ciphertext exists, no key found
    MISMATCH = "mismatch"     # a key was found, but it cannot decrypt the sample
    INVALID = "invalid"       # key text is not a valid Fernet key


@dataclass(frozen=True)
class KeyStatus:
    state: KeyState
    source: str | None = None
    reason: str | None = None
    can_use_credentials: bool = False
    has_ciphertext: bool = False
    key_file: str | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "source": self.source,
            "reason": self.reason,
            "can_use_credentials": self.can_use_credentials,
            "has_ciphertext": self.has_ciphertext,
            "key_file": self.key_file,
        }


_key: str | None = None
_status: KeyStatus = KeyStatus(KeyState.MISSING, reason="密钥尚未初始化")
_initialized = False
_jwt_cache: str | None = None
_rotation_lock = asyncio.Lock()


@dataclass(frozen=True)
class _Candidate:
    key: str
    source: str
    path: Path | None
    state: KeyState


# --------------------------------------------------------------------------- paths


def key_dir() -> Path:
    """Directory holding key files. POTATO_KEY_DIR wins; otherwise the SQLite DB dir."""
    raw = (getattr(get_settings(), "key_dir", "") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()

    url = get_settings().database_url or ""
    if url.startswith("sqlite") and "///" in url:
        raw_url = url.split("///", 1)[1] or ""
        raw_url = raw_url.split("?", 1)[0]
        if raw_url and raw_url != ":memory:":
            return Path(raw_url).expanduser().resolve().parent
    return Path.cwd().resolve()


def key_file_paths() -> tuple[Path, Path]:
    """Return (primary DPAPI/plain file, legacy plaintext file)."""
    d = key_dir()
    return d / DPAPI_FILE, d / LEGACY_FILE


# --------------------------------------------------------------------------- DPAPI


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_supported() -> bool:
    return sys.platform == "win32"


def _make_blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, max(len(data), 1))
    blob = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    # The buffer must outlive the call; the caller keeps this tuple alive.
    return blob, buf


def protect(raw: bytes) -> bytes:
    """Encrypt bytes with the current Windows user's DPAPI. Returns raw on non-Windows."""
    if not raw or not _dpapi_supported():
        return raw

    from ctypes import wintypes

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    in_blob, in_buf = _make_blob(raw)
    ent_blob, ent_buf = _make_blob(_ENTROPY)
    out = _Blob()

    fn = crypt32.CryptProtectData
    fn.argtypes = [
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    fn.restype = wintypes.BOOL

    ok = fn(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(ent_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"CryptProtectData 失败（WinError {err}）")

    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def unprotect(blob: bytes) -> bytes:
    """Decrypt bytes with the current Windows user's DPAPI. Returns raw on non-Windows."""
    if not blob or not _dpapi_supported():
        return blob

    from ctypes import wintypes

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    in_blob, in_buf = _make_blob(blob)
    ent_blob, ent_buf = _make_blob(_ENTROPY)
    out = _Blob()

    fn = crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    fn.restype = wintypes.BOOL

    ok = fn(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(ent_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"CryptUnprotectData 失败（WinError {err}）")

    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


# --------------------------------------------------------------------------- files


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_key_file(path: Path, raw: bytes) -> None:
    if _dpapi_supported():
        payload = _DPAPI_MAGIC + protect(raw)
    else:
        payload = raw
    _atomic_write(path, payload)


def _read_key_file(path: Path) -> bytes:
    data = path.read_bytes()
    if data.startswith(_DPAPI_MAGIC):
        if not _dpapi_supported():
            raise SecretKeyMissing(
                f"{path.name} 由 Windows DPAPI 保护，当前平台无法解密。"
                "请在原 Windows 环境导出密钥后，用 /api/secret-key/import 导入。"
            )
        return unprotect(data[len(_DPAPI_MAGIC):])
    return data


def _valid_key(raw: str) -> bool:
    try:
        Fernet(raw.encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 - any parse failure means invalid
        return False


def _invalid_key_message(raw: str, where: str) -> str:
    return (
        f"{where} 不是合法的 Fernet 密钥"
        f"（需要 32 字节的 url-safe base64 字符串，当前长度 {len(raw)}）。{_ENV_HINT}"
    )


def _decrypts(key: str, token: str) -> bool:
    try:
        Fernet(key.encode("utf-8")).decrypt(token.encode("utf-8"))
        return True
    except (InvalidToken, ValueError, TypeError):
        return False
    except Exception:  # noqa: BLE001
        return False


def _self_check(key: str) -> None:
    fernet = Fernet(key.encode("utf-8"))
    blob = fernet.encrypt(_SELF_CHECK.encode("utf-8"))
    if fernet.decrypt(blob).decode("utf-8") != _SELF_CHECK:
        raise SecretKeyMissing("新生成的密钥未通过加解密自检，拒绝写入")


def _clear_crypto_cache() -> None:
    try:
        from app import crypto

        crypto._fernet.cache_clear()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- resolve


def _load_candidate() -> tuple[_Candidate | None, tuple[KeyState, str] | None]:
    """Pick a key without touching the DB. Returns (candidate, failure)."""
    env = (get_settings().secret_key or "").strip()
    if env:
        if _valid_key(env):
            return _Candidate(env, "env", None, KeyState.OK_ENV), None
        return None, (KeyState.INVALID, _invalid_key_message(env, "POTATO_SECRET_KEY"))

    failures: list[tuple[KeyState, str]] = []
    primary, legacy = key_file_paths()

    if primary.exists():
        try:
            raw = _read_key_file(primary).decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            failures.append((KeyState.MISSING, f"读取 {primary} 失败：{exc}"))
        else:
            if _valid_key(raw):
                return _Candidate(raw, "dpapi", primary, KeyState.OK_DPAPI), None
            failures.append((KeyState.INVALID, _invalid_key_message(raw, str(primary))))

    if legacy.exists():
        try:
            raw = _read_key_file(legacy).decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            failures.append((KeyState.MISSING, f"读取 {legacy} 失败：{exc}"))
        else:
            if _valid_key(raw):
                return _Candidate(
                    raw, "legacy_file", legacy, KeyState.OK_LEGACY_MIGRATED
                ), None
            failures.append((KeyState.INVALID, _invalid_key_message(raw, str(legacy))))

    if failures:
        # Prefer INVALID over MISSING: it is the more actionable diagnosis.
        for state, reason in failures:
            if state is KeyState.INVALID:
                return None, (state, reason)
        return None, failures[0]
    return None, None


def _migrate_legacy(candidate: _Candidate) -> str:
    """Re-protect a legacy plaintext key with DPAPI, keeping the old file as .bak."""
    primary = key_dir() / DPAPI_FILE
    try:
        _write_key_file(primary, candidate.key.encode("utf-8"))
        if _read_key_file(primary).decode("utf-8").strip() != candidate.key:
            raise RuntimeError("回读内容与写入内容不一致")
    except Exception as exc:  # noqa: BLE001
        return f"旧密钥仍可继续使用，但迁移到 DPAPI 失败：{exc}"

    backup = (
        candidate.path.with_name(candidate.path.name + ".bak")
        if candidate.path is not None
        else None
    )
    try:
        if candidate.path is not None and candidate.path.exists():
            if backup is not None and backup.exists():
                backup.unlink()
            candidate.path.rename(backup) if backup is not None else candidate.path.unlink()
    except Exception as exc:  # noqa: BLE001
        return f"密钥已迁移到 {primary.name}，但旧文件改名失败：{exc}"

    name = backup.name if backup is not None else LEGACY_FILE
    return f"旧明文密钥已迁移为 DPAPI 保护，原文件保留为 {name}。"


async def _probe(session) -> tuple[bool, tuple[str, str] | None]:
    """Find whether the DB holds ciphertext and return one decryptable sample."""
    from sqlalchemy import select

    from app.models import AppSetting, Credential

    for column, label in (
        (Credential.secret, "credential.secret"),
        (Credential.session_bundle, "credential.session_bundle"),
    ):
        try:
            row = (
                await session.execute(
                    select(column).where(column.is_not(None), column != "").limit(1)
                )
            ).first()
        except Exception:  # noqa: BLE001 - old DB without the column
            await session.rollback()
            continue
        if row and row[0]:
            return True, (str(row[0]), label)

    try:
        row = (
            await session.execute(
                select(AppSetting.value)
                .where(AppSetting.secret.is_(True), AppSetting.value != "")
                .limit(1)
            )
        ).first()
    except Exception:  # noqa: BLE001
        await session.rollback()
        return False, None
    if row and row[0]:
        return True, (str(row[0]), "app_setting")
    return False, None


# --------------------------------------------------------------------------- public API


async def initialize(session) -> KeyStatus:
    """Resolve the credential key once at startup. Never generates when ciphertext exists."""
    global _key, _status, _initialized

    has_ciphertext, sample = await _probe(session)
    candidate, failure = _load_candidate()

    # Crash recovery: a rotation commits the DB before it promotes the pending key.
    # If the primary key does not decrypt the stored ciphertext, the pending key may
    # be the one that does. Never do this when POTATO_SECRET_KEY is set: rotation
    # refuses to run in env mode, so a pending file cannot legitimately belong to it.
    env_configured = bool((get_settings().secret_key or "").strip())
    recovered_reason: str | None = None
    if (
        not env_configured
        and sample is not None
        and (candidate is None or not _decrypts(candidate.key, sample[0]))
    ):
        pending = _load_pending()
        if pending is not None and _decrypts(pending.key, sample[0]):
            recovered_reason = _promote_pending(pending, sample[0])
            candidate = _Candidate(
                pending.key,
                "pending_recovered",
                key_dir() / DPAPI_FILE,
                KeyState.OK_DPAPI,
            )

    if candidate is not None:
        if has_ciphertext and sample is not None and not _decrypts(candidate.key, sample[0]):
            _key = None
            _status = KeyStatus(
                state=KeyState.MISMATCH,
                source=candidate.source,
                reason=(
                    f"找到了密钥（来源：{candidate.source}），但它解不开库中已有的密文"
                    f"（样本：{sample[1]}）。请导入原密钥；不会覆盖现有密钥文件。"
                ),
                can_use_credentials=False,
                has_ciphertext=True,
                key_file=str(candidate.path) if candidate.path is not None else None,
            )
            _initialized = True
            _clear_crypto_cache()
            return _status

        # A pending key left here is from a rotation that never committed (or whose
        # promotion succeeded). Keep it: deleting it outside the rotation lock could
        # race with a concurrent rotation and destroy the only copy of its new key.
        reason = recovered_reason
        if reason is None and candidate.source == "legacy_file":
            reason = _migrate_legacy(candidate)
        _key = candidate.key
        _status = KeyStatus(
            state=candidate.state,
            source=candidate.source,
            reason=reason,
            can_use_credentials=True,
            has_ciphertext=has_ciphertext,
            key_file=str(candidate.path) if candidate.path is not None else None,
        )
        _initialized = True
        _clear_crypto_cache()
        return _status

    primary = key_dir() / DPAPI_FILE

    if has_ciphertext:
        state, reason = (
            failure
            if failure is not None
            else (
                KeyState.MISSING,
                "数据库里已经有加密凭据，但找不到任何密钥：未设置 POTATO_SECRET_KEY，"
                f"{primary.name} 和 {LEGACY_FILE} 都不存在。不会自动生成新密钥。",
            )
        )
        _key = None
        _status = KeyStatus(
            state=state,
            source=None,
            reason=reason,
            can_use_credentials=False,
            has_ciphertext=True,
            key_file=str(primary),
        )
        _initialized = True
        _clear_crypto_cache()
        return _status

    if failure is not None:
        # A key file/env var exists but is malformed. Do not overwrite it silently.
        state, reason = failure
        _key = None
        _status = KeyStatus(
            state=state,
            source=None,
            reason=reason,
            can_use_credentials=False,
            has_ciphertext=False,
            key_file=str(primary),
        )
        _initialized = True
        _clear_crypto_cache()
        return _status

    generated = Fernet.generate_key().decode()
    _self_check(generated)
    _write_key_file(primary, generated.encode("utf-8"))
    _key = generated
    _status = KeyStatus(
        state=KeyState.OK_GENERATED,
        source="generated",
        reason=None,
        can_use_credentials=True,
        has_ciphertext=False,
        key_file=str(primary),
    )
    _initialized = True
    _clear_crypto_cache()
    return _status


def get_status() -> KeyStatus:
    return _status


def _resolve_without_generating() -> str | None:
    env = (get_settings().secret_key or "").strip()
    if env:
        if not _valid_key(env):
            raise SecretKeyMissing(_invalid_key_message(env, "POTATO_SECRET_KEY"))
        return env

    primary, legacy = key_file_paths()
    for path in (primary, legacy):
        if not path.exists():
            continue
        try:
            raw = _read_key_file(path).decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            raise SecretKeyMissing(f"读取密钥文件 {path} 失败：{exc}") from exc
        if not _valid_key(raw):
            raise SecretKeyMissing(_invalid_key_message(raw, str(path)))
        return raw
    return None


def get_key() -> str:
    """Return the credential key. Never generates a replacement lazily."""
    # initialize() already decided this install cannot use credentials (MISSING /
    # MISMATCH / INVALID). Do not let a lazy re-read of the environment turn that
    # decision into a false "available" -- the key might parse but still be wrong.
    if _initialized and not _status.can_use_credentials:
        raise SecretKeyMissing(_status.reason or "凭据加密密钥不可用")
    global _key
    if _key:
        return _key

    raw = _resolve_without_generating()
    if raw is None:
        raise SecretKeyMissing(
            "凭据加密密钥不可用：既没有设置 POTATO_SECRET_KEY，也没有找到密钥文件。"
            "如果本机没有历史密文，先运行一次服务即可自动生成；"
            "否则请在恢复页导入原密钥。"
        )
    _key = raw
    return raw


def secret_available() -> bool:
    try:
        get_key()
        return True
    except (SecretKeyMissing, OSError):
        return False


def export_key() -> str:
    """Return the in-memory key for one-time display. Never re-reads the disk."""
    return get_key()


async def import_key(raw: str, session) -> KeyStatus:
    """Validate a user-supplied key against stored ciphertext, then persist it."""
    global _key, _status, _initialized

    key = (raw or "").strip()
    primary = key_dir() / DPAPI_FILE
    has_ciphertext, sample = await _probe(session)

    if not _valid_key(key):
        return KeyStatus(
            state=KeyState.INVALID,
            source="import",
            reason=_invalid_key_message(key, "导入的密钥"),
            can_use_credentials=False,
            has_ciphertext=has_ciphertext,
            key_file=str(primary),
        )

    if has_ciphertext and sample is not None and not _decrypts(key, sample[0]):
        return KeyStatus(
            state=KeyState.MISMATCH,
            source="import",
            reason=f"导入的密钥无法解开库中已有的密文（样本：{sample[1]}）。原密钥文件未被修改。",
            can_use_credentials=False,
            has_ciphertext=True,
            key_file=str(primary),
        )

    try:
        _write_key_file(primary, key.encode("utf-8"))
        if _read_key_file(primary).decode("utf-8").strip() != key:
            raise RuntimeError("回读内容与写入内容不一致")
    except Exception as exc:  # noqa: BLE001
        return KeyStatus(
            state=KeyState.MISSING,
            source="import",
            reason=f"写入密钥文件失败：{exc}",
            can_use_credentials=False,
            has_ciphertext=has_ciphertext,
            key_file=str(primary),
        )

    _key = key
    _initialized = True
    env = (get_settings().secret_key or "").strip()
    reason = None
    if env and env != key:
        reason = (
            "密钥已导入并落盘，但 POTATO_SECRET_KEY 仍然优先生效；"
            "请修改或删除该环境变量后重启。"
        )
    _status = KeyStatus(
        state=KeyState.OK_DPAPI,
        source="import",
        reason=reason,
        can_use_credentials=True,
        has_ciphertext=has_ciphertext,
        key_file=str(primary),
    )
    _clear_crypto_cache()
    return _status


# --------------------------------------------------------------------------- rotation


def _pending_path() -> Path:
    return key_dir() / PENDING_FILE


@contextlib.contextmanager
def _rotation_file_lock():
    """Cross-process guard: two workers must never promote different keys."""
    path = key_dir() / ".potato-rotation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")  # noqa: SIM115 - held for the rotation, closed in finally
    try:
        if sys.platform == "win32":
            import msvcrt

            _seed_lock_byte(fh)
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _load_pending() -> _Candidate | None:
    path = _pending_path()
    if not path.exists():
        return None
    try:
        raw = _read_key_file(path).decode("utf-8").strip()
    except Exception:  # noqa: BLE001 - an unreadable pending key is treated as absent
        return None
    if not _valid_key(raw):
        return None
    return _Candidate(raw, "pending", path, KeyState.OK_DPAPI)


def _discard_pending() -> None:
    try:
        _pending_path().unlink(missing_ok=True)
    except OSError:
        pass


def _promote_pending(pending: _Candidate, sample_token: str | None = None) -> str:
    """Make the pending key primary. Returns a human-readable outcome."""
    try:
        with _rotation_file_lock():
            if sample_token is not None and not _decrypts(pending.key, sample_token):
                return (
                    "检测到未完成的密钥轮换，但 .pending 中的密钥已不匹配当前密文，"
                    "未提升为主密钥。请导入原密钥。"
                )
            primary = key_dir() / DPAPI_FILE
            try:
                _write_key_file(primary, pending.key.encode("utf-8"))
                if _read_key_file(primary).decode("utf-8").strip() != pending.key:
                    raise RuntimeError("回读内容与写入内容不一致")
            except Exception as exc:  # noqa: BLE001
                return (
                    "检测到未完成的密钥轮换：当前使用 .pending 中的新密钥，但写入主密钥文件"
                    f"失败（{exc}）。请勿删除 {PENDING_FILE}，修复后重启会自动重试。"
                )
            _discard_pending()
            return "检测到上次密钥轮换未完成，已恢复新密钥并提升为主密钥。"
    except OSError as exc:
        return (
            f"检测到未完成的密钥轮换，但无法获取轮换锁（{exc}）；"
            f"当前暂用 .pending 中的密钥。请勿删除 {PENDING_FILE}。"
        )


def _seed_lock_byte(fh) -> None:
    """msvcrt.locking needs a real byte to lock, even if the file was just created."""
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"x")
        fh.flush()


def _primary_holds(key: str) -> bool:
    primary = key_dir() / DPAPI_FILE
    try:
        return primary.exists() and (
            _read_key_file(primary).decode("utf-8").strip() == key
        )
    except Exception:  # noqa: BLE001
        return False


def _ensure_primary(key: str) -> None:
    """Make sure the key currently used for the DB is durable before we replace it."""
    if _primary_holds(key):
        return
    primary = key_dir() / DPAPI_FILE
    _write_key_file(primary, key.encode("utf-8"))
    if _read_key_file(primary).decode("utf-8").strip() != key:
        raise SecretKeyMissing("当前密钥写入主密钥文件后回读不一致，已取消轮换")


def _reencrypt_value(old: Fernet, new: Fernet, value: str, label: str) -> str:
    try:
        plain = old.decrypt(value.encode("utf-8"))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise SecretKeyMissing(
            f"轮换中止：{label} 无法用当前密钥解密，数据库未修改。"
        ) from exc
    return new.encrypt(plain).decode("utf-8")


async def _reencrypt_all(session, old_key: str, new_key: str) -> int:
    from sqlalchemy import select

    from app.models import AppSetting, Credential

    old = Fernet(old_key.encode("utf-8"))
    new = Fernet(new_key.encode("utf-8"))
    changed = 0

    rows = (await session.execute(select(Credential))).scalars().all()
    for row in rows:
        for field in ("secret", "session_bundle"):
            value = getattr(row, field)
            if not value:
                continue
            setattr(
                row,
                field,
                _reencrypt_value(
                    old, new, str(value), f"credential.{field}（id={row.id}）"
                ),
            )
            changed += 1

    settings = (
        await session.execute(
            select(AppSetting).where(AppSetting.secret.is_(True))
        )
    ).scalars().all()
    for row in settings:
        if not row.value:
            continue
        row.value = _reencrypt_value(
            old, new, str(row.value), f"app_setting.{row.key}"
        )
        changed += 1
    return changed


async def rotate(session) -> tuple[KeyStatus, str]:
    """Re-encrypt every credential with a fresh key and return (status, new key).

    Ordering is deliberate: (1) make the current key durable, (2) park the new key in
    PENDING_FILE, (3) re-encrypt and commit the DB, (4) promote pending to primary.
    A crash before commit leaves the old key usable; a crash after commit leaves the
    new key recoverable from PENDING_FILE at the next startup.
    """
    global _key, _status, _initialized

    env = (get_settings().secret_key or "").strip()
    if env:
        raise SecretKeyMissing(
            "当前密钥来自 POTATO_SECRET_KEY。在线轮换无法在重启后继续生效："
            "新密钥会写入密钥文件，但环境变量仍然优先。"
            "请删除该环境变量并重启后再轮换。"
        )

    async with _rotation_lock:
        with _rotation_file_lock():
            old_key = get_key()
            _ensure_primary(old_key)

            new_key = Fernet.generate_key().decode()
            _self_check(new_key)

            pending = _pending_path()
            try:
                _write_key_file(pending, new_key.encode("utf-8"))
                if _read_key_file(pending).decode("utf-8").strip() != new_key:
                    raise RuntimeError("回读内容与写入内容不一致")
                await _reencrypt_all(session, old_key, new_key)
                await session.commit()
            except Exception:
                await session.rollback()
                _discard_pending()
                raise

            primary = key_dir() / DPAPI_FILE
            reason = None
            key_file = str(primary)
            try:
                _write_key_file(primary, new_key.encode("utf-8"))
                if _read_key_file(primary).decode("utf-8").strip() != new_key:
                    raise RuntimeError("回读内容与写入内容不一致")
                _discard_pending()
            except Exception as exc:  # noqa: BLE001
                reason = (
                    "数据库已用新密钥重加密，但主密钥文件写入失败"
                    f"（{exc}）。新密钥保留在 {PENDING_FILE}，请勿删除；重启会自动恢复。"
                )
                key_file = str(pending)

            _key = new_key
            _initialized = True
            _status = KeyStatus(
                state=KeyState.OK_DPAPI,
                source="rotation",
                reason=reason,
                can_use_credentials=True,
                has_ciphertext=True,
                key_file=key_file,
            )
            _clear_crypto_cache()
            return _status, new_key


# --------------------------------------------------------------------------- JWT


def jwt_key() -> str:
    """Independent HS256 signing key, generated on demand.

    It deliberately does not check credential ciphertext: login must keep working while
    the credential key is in recovery mode.
    """
    global _jwt_cache
    if _jwt_cache:
        return _jwt_cache

    path = key_dir() / JWT_FILE
    if path.exists():
        try:
            raw = _read_key_file(path).decode("utf-8").strip()
            if _valid_key(raw):
                _jwt_cache = raw
                return raw
        except Exception:  # noqa: BLE001 - corrupt file: regenerate, sessions may reset
            pass

    raw = Fernet.generate_key().decode()
    _self_check(raw)
    _write_key_file(path, raw.encode("utf-8"))
    _jwt_cache = raw
    return raw


# --------------------------------------------------------------------------- tests


def reset_for_tests(raw_key: str | None = None) -> None:
    """Reset module state. Tests call this so one test cannot leak a key into another."""
    global _key, _status, _initialized, _jwt_cache
    _key = raw_key
    _jwt_cache = None
    _initialized = raw_key is not None
    if raw_key is not None:
        _status = KeyStatus(
            state=KeyState.OK_ENV,
            source="test",
            can_use_credentials=True,
            has_ciphertext=False,
        )
    else:
        _status = KeyStatus(KeyState.MISSING, reason="密钥尚未初始化")
    _clear_crypto_cache()
