"""Credential-key lifecycle: no silent key replacement, ever.

These tests exercise app/key_store.py against a temporary SQLite DB and a temporary
key directory. The important case is the one the old implementation got wrong: when
ciphertext exists and the key is gone, initialization must report MISSING and must not
create a replacement key.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from cryptography.fernet import Fernet

from app import crypto, key_store
from app.config import get_settings
from app.models import AppSetting, Credential, Project


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def key_env(tmp_path, monkeypatch):
    """Point key_store at a temp key dir and clear cached settings/module state."""
    key_dir = tmp_path / "keys"
    monkeypatch.setenv("POTATO_KEY_DIR", str(key_dir))
    monkeypatch.delenv("POTATO_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    key_store.reset_for_tests(None)
    yield key_dir
    get_settings.cache_clear()
    key_store.reset_for_tests(None)


async def _initialize(db_session):
    async with db_session() as sess:
        return await key_store.initialize(sess)


async def _import(db_session, raw: str):
    async with db_session() as sess:
        return await key_store.import_key(raw, sess)


async def _rotate(db_session):
    async with db_session() as sess:
        return await key_store.rotate(sess)


async def _seed_ciphertext(db_session, key: str) -> str:
    token = Fernet(key.encode()).encrypt(b"stored-secret").decode()
    async with db_session() as sess:
        sess.add(AppSetting(key="probe-secret", value=token, secret=True))
    return token


def test_fresh_install_generates_a_key(tmp_db, key_env):
    _, db_session, _ = tmp_db

    status = _run(_initialize(db_session))

    assert status.state is key_store.KeyState.OK_GENERATED
    assert status.can_use_credentials is True
    assert status.has_ciphertext is False
    assert (key_env / key_store.DPAPI_FILE).exists()
    assert key_store.secret_available() is True
    assert crypto.decrypt(crypto.encrypt("hello")) == "hello"


def test_environment_variable_wins_over_key_file(tmp_db, key_env):
    _, db_session, _ = tmp_db
    file_key = Fernet.generate_key().decode()
    env_key = Fernet.generate_key().decode()
    key_env.mkdir(parents=True, exist_ok=True)
    (key_env / key_store.DPAPI_FILE).write_text(file_key, encoding="utf-8")

    import os

    os.environ["POTATO_SECRET_KEY"] = env_key
    get_settings.cache_clear()
    key_store.reset_for_tests(None)

    status = _run(_initialize(db_session))

    assert status.state is key_store.KeyState.OK_ENV
    assert status.source == "env"
    assert key_store.export_key() == env_key


def test_ciphertext_without_a_key_is_missing_and_creates_nothing(tmp_db, key_env):
    _, db_session, _ = tmp_db
    orphan_key = Fernet.generate_key().decode()
    _run(_seed_ciphertext(db_session, orphan_key))

    status = _run(_initialize(db_session))

    assert status.state is key_store.KeyState.MISSING
    assert status.has_ciphertext is True
    assert status.can_use_credentials is False
    assert not (key_env / key_store.DPAPI_FILE).exists()
    assert not (key_env / key_store.LEGACY_FILE).exists()
    assert key_store.secret_available() is False
    with pytest.raises(key_store.SecretKeyMissing):
        crypto.encrypt("must not encrypt with a brand-new key")


def test_wrong_key_is_mismatch_and_does_not_overwrite(tmp_db, key_env):
    _, db_session, _ = tmp_db
    right_key = Fernet.generate_key().decode()
    wrong_key = Fernet.generate_key().decode()
    _run(_seed_ciphertext(db_session, right_key))

    import os

    os.environ["POTATO_SECRET_KEY"] = wrong_key
    get_settings.cache_clear()
    key_store.reset_for_tests(None)

    status = _run(_initialize(db_session))

    assert status.state is key_store.KeyState.MISMATCH
    assert status.can_use_credentials is False
    assert key_store.secret_available() is False
    with pytest.raises(key_store.SecretKeyMissing):
        crypto.decrypt("anything")


def test_legacy_plaintext_key_is_migrated(tmp_db, key_env):
    _, db_session, _ = tmp_db
    raw_key = Fernet.generate_key().decode()
    token = _run(_seed_ciphertext(db_session, raw_key))
    key_env.mkdir(parents=True, exist_ok=True)
    legacy = key_env / key_store.LEGACY_FILE
    legacy.write_text(raw_key, encoding="utf-8")

    status = _run(_initialize(db_session))

    assert status.state is key_store.KeyState.OK_LEGACY_MIGRATED
    assert status.can_use_credentials is True
    assert (key_env / key_store.DPAPI_FILE).exists()
    assert (key_env / (key_store.LEGACY_FILE + ".bak")).exists()
    assert not legacy.exists()
    assert crypto.decrypt(token) == "stored-secret"


def test_wrong_import_is_rejected_without_touching_the_file(tmp_db, key_env):
    _, db_session, _ = tmp_db
    original_key = Fernet.generate_key().decode()
    _run(_seed_ciphertext(db_session, original_key))
    status = _run(_initialize(db_session))
    # Fresh install path first: generate, then seed ciphertext under the generated key.
    if status.state is key_store.KeyState.MISSING:
        # Ciphertext existed before generation; import the right key instead.
        _run(_import(db_session, original_key))
    else:
        generated = key_store.export_key()
        _run(_seed_ciphertext(db_session, generated))
        original_key = generated

    primary = key_env / key_store.DPAPI_FILE
    before = primary.read_bytes()

    status = _run(_import(db_session, Fernet.generate_key().decode()))

    assert status.state is key_store.KeyState.MISMATCH
    assert status.can_use_credentials is False
    assert primary.read_bytes() == before


def test_correct_import_recovers_access(tmp_db, key_env):
    _, db_session, _ = tmp_db
    right_key = Fernet.generate_key().decode()
    token = _run(_seed_ciphertext(db_session, right_key))
    missing = _run(_initialize(db_session))
    assert missing.state is key_store.KeyState.MISSING

    status = _run(_import(db_session, right_key))

    assert status.state is key_store.KeyState.OK_DPAPI
    assert status.can_use_credentials is True
    assert (key_env / key_store.DPAPI_FILE).exists()
    assert crypto.decrypt(token) == "stored-secret"


def test_invalid_environment_key_is_actionable(tmp_db, key_env):
    _, db_session, _ = tmp_db

    import os

    os.environ["POTATO_SECRET_KEY"] = "x" * 32
    get_settings.cache_clear()
    key_store.reset_for_tests(None)

    status = _run(_initialize(db_session))

    assert status.state is key_store.KeyState.INVALID
    assert status.can_use_credentials is False
    assert "POTATO_SECRET_KEY" in (status.reason or "")
    assert "Fernet.generate_key" in (status.reason or "")
    assert not (key_env / key_store.DPAPI_FILE).exists()


def test_jwt_key_survives_missing_credential_key(tmp_db, key_env):
    _, db_session, _ = tmp_db
    _run(_seed_ciphertext(db_session, Fernet.generate_key().decode()))

    status = _run(_initialize(db_session))
    assert status.state is key_store.KeyState.MISSING

    jwt = key_store.jwt_key()

    assert jwt
    assert (key_env / key_store.JWT_FILE).exists()
    assert key_store.get_status().state is key_store.KeyState.MISSING



# --------------------------------------------------------------------------- rotation


def test_rotate_reencrypts_every_encrypted_field(tmp_db, key_env):
    _, db_session, _ = tmp_db
    assert _run(_initialize(db_session)).can_use_credentials
    old_key = key_store.export_key()

    async def seed():
        async with db_session() as s:
            project = Project(name="rotate")
            s.add(project)
            await s.flush()
            s.add(
                Credential(
                    project_id=project.id,
                    type="password",
                    label="robot",
                    secret=crypto.encrypt("pw-value"),
                    session_bundle=crypto.encrypt("bundle-value"),
                    is_active=False,
                )
            )
            s.add(
                AppSetting(
                    key="secret-setting",
                    value=crypto.encrypt("setting-value"),
                    secret=True,
                )
            )
            s.add(AppSetting(key="plain-setting", value="plain", secret=False))

    _run(seed())

    async def snapshot():
        from sqlalchemy import select

        async with db_session() as s:
            cred = (await s.execute(select(Credential))).scalars().one()
            setting = await s.get(AppSetting, "secret-setting")
            plain = await s.get(AppSetting, "plain-setting")
            return cred.secret, cred.session_bundle, setting.value, plain.value

    old_tokens = _run(snapshot())
    status, new_key = _run(_rotate(db_session))

    assert new_key != old_key
    assert status.can_use_credentials is True
    assert status.source == "rotation"
    assert key_store.export_key() == new_key
    assert key_store.get_status().can_use_credentials is True

    new_tokens = _run(snapshot())
    assert all(a != b for a, b in zip(old_tokens[:3], new_tokens[:3]))
    assert crypto.decrypt(new_tokens[0]) == "pw-value"
    assert crypto.decrypt(new_tokens[1]) == "bundle-value"
    assert crypto.decrypt(new_tokens[2]) == "setting-value"
    assert new_tokens[3] == "plain"

    for token in new_tokens[:3]:
        with pytest.raises(Exception):
            Fernet(old_key).decrypt(token.encode())

    primary = key_env / key_store.DPAPI_FILE
    assert key_store._read_key_file(primary).decode("utf-8").strip() == new_key
    assert not (key_env / key_store.PENDING_FILE).exists()


def test_rotate_aborts_cleanly_when_a_row_cannot_be_decrypted(tmp_db, key_env):
    from sqlalchemy import select

    _, db_session, _ = tmp_db
    _run(_initialize(db_session))
    old_key = key_store.export_key()
    good_token = _run(_seed_ciphertext(db_session, old_key))

    async def add_bad_row():
        async with db_session() as s:
            s.add(AppSetting(key="broken-secret", value="not-a-fernet-token", secret=True))

    _run(add_bad_row())

    with pytest.raises(key_store.SecretKeyMissing):
        _run(_rotate(db_session))

    assert key_store.export_key() == old_key
    assert not (key_env / key_store.PENDING_FILE).exists()
    assert key_store._read_key_file(key_env / key_store.DPAPI_FILE).decode().strip() == old_key

    async def values():
        async with db_session() as s:
            rows = {
                row.key: row.value
                for row in (await s.execute(select(AppSetting))).scalars().all()
            }
            return rows

    rows = _run(values())
    assert rows["probe-secret"] == good_token
    assert rows["broken-secret"] == "not-a-fernet-token"


def test_rotate_refuses_environment_key(tmp_db, key_env):
    _, db_session, _ = tmp_db
    env_key = Fernet.generate_key().decode()
    _run(_seed_ciphertext(db_session, env_key))

    import os

    os.environ["POTATO_SECRET_KEY"] = env_key
    get_settings.cache_clear()
    key_store.reset_for_tests(None)
    assert _run(_initialize(db_session)).state is key_store.KeyState.OK_ENV

    with pytest.raises(key_store.SecretKeyMissing) as exc:
        _run(_rotate(db_session))

    assert "POTATO_SECRET_KEY" in str(exc.value)
    assert key_store.export_key() == env_key
    assert not (key_env / key_store.PENDING_FILE).exists()


def test_pending_key_recovers_a_committed_rotation(tmp_db, key_env):
    from sqlalchemy import select

    _, db_session, _ = tmp_db
    _run(_initialize(db_session))
    old_key = key_store.export_key()
    _run(_seed_ciphertext(db_session, old_key))

    new_key = Fernet.generate_key().decode()
    key_store._write_key_file(key_env / key_store.PENDING_FILE, new_key.encode("utf-8"))

    async def move_to_new_key():
        async with db_session() as s:
            row = (await s.execute(select(AppSetting))).scalars().one()
            plain = Fernet(old_key.encode()).decrypt(row.value.encode())
            row.value = Fernet(new_key.encode()).encrypt(plain).decode()

    _run(move_to_new_key())

    # Simulate a restart after the DB commit, before the pending key was promoted.
    key_store.reset_for_tests(None)
    status = _run(_initialize(db_session))

    assert status.can_use_credentials is True
    assert status.source == "pending_recovered"
    assert "轮换" in (status.reason or "")
    assert key_store.export_key() == new_key
    assert not (key_env / key_store.PENDING_FILE).exists()
    assert key_store._read_key_file(key_env / key_store.DPAPI_FILE).decode().strip() == new_key


def test_stale_pending_key_does_not_override_the_primary(tmp_db, key_env):
    _, db_session, _ = tmp_db
    _run(_initialize(db_session))
    old_key = key_store.export_key()
    token = _run(_seed_ciphertext(db_session, old_key))

    key_store._write_key_file(
        key_env / key_store.PENDING_FILE, Fernet.generate_key()
    )
    key_store.reset_for_tests(None)
    status = _run(_initialize(db_session))

    assert status.can_use_credentials is True
    assert key_store.export_key() == old_key
    # Kept on purpose: deleting it outside the rotation lock could race with a
    # rotation in another worker. It is only ever promoted if it decrypts the DB.
    assert (key_env / key_store.PENDING_FILE).exists()
    assert crypto.decrypt(token) == "stored-secret"



# --------------------------------------------------------------------------- API


@pytest.fixture
def ready_client(tmp_db, key_env):
    """Started app with a freshly generated key (no ciphertext yet)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def recovery_client(tmp_db, key_env):
    """Started app whose DB has ciphertext but whose key dir is empty."""
    from fastapi.testclient import TestClient

    from app.main import app

    _, db_session, _ = tmp_db
    right_key = Fernet.generate_key().decode()
    token = _run(_seed_ciphertext(db_session, right_key))
    with TestClient(app) as c:
        yield c, right_key, token


def test_secret_key_status_and_export_when_ready(ready_client):
    r = ready_client.get("/api/secret-key/status")
    assert r.status_code == 200
    body = r.json()
    assert body["can_use_credentials"] is True
    assert body["recovery_mode"] is False
    assert body["state"] == key_store.KeyState.OK_GENERATED.value

    r = ready_client.post("/api/secret-key/export")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    assert r.json()["key"] == key_store.export_key()


def test_recovery_mode_status_does_not_leak_key_and_blocks_export(recovery_client):
    client, _, _ = recovery_client

    r = client.get("/api/secret-key/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == key_store.KeyState.MISSING.value
    assert body["can_use_credentials"] is False
    assert body["recovery_mode"] is True
    assert body["has_ciphertext"] is True

    r = client.post("/api/secret-key/export")
    assert r.status_code == 409


def test_import_wrong_key_is_rejected_then_right_key_recovers(recovery_client):
    client, right_key, token = recovery_client
    primary = key_store.key_dir() / key_store.DPAPI_FILE

    r = client.post(
        "/api/secret-key/import", json={"key": Fernet.generate_key().decode()}
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["status"]["state"] == key_store.KeyState.MISMATCH.value
    assert detail["status"]["can_use_credentials"] is False
    assert not primary.exists()

    r = client.post("/api/secret-key/import", json={"key": right_key})
    assert r.status_code == 200
    body = r.json()
    assert body["can_use_credentials"] is True
    assert body["recovery_mode"] is False
    assert body["restart_required"] is True
    assert primary.exists()

    r = client.get("/api/secret-key/status")
    assert r.json()["recovery_mode"] is False
    assert crypto.decrypt(token) == "stored-secret"


def test_rotate_endpoint_returns_the_new_key(ready_client):
    old_key = key_store.export_key()

    r = ready_client.post("/api/secret-key/rotate")

    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    body = r.json()
    assert body["can_use_credentials"] is True
    assert body["recovery_mode"] is False
    assert body["restart_required"] is False
    assert body["key"] != old_key
    assert body["key"] == key_store.export_key()

    r = ready_client.get("/api/secret-key/status")
    assert r.status_code == 200
    assert r.json()["can_use_credentials"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_roundtrip():
    raw = b"dpapi-roundtrip-check"
    blob = key_store.protect(raw)
    assert blob != raw
    assert key_store.unprotect(blob) == raw
