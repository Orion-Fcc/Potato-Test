"""Legacy plaintext login state must move into encrypted credentials.

These tests pin three rules:
1. Backfill encrypts existing `Project.login_state` values and is idempotent.
2. New projects never write the plaintext column again.
3. Importing a backup key immediately backfills, without waiting for a restart.
"""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app import crypto, key_store, login_state
from app.config import get_settings
from app.models import AppSetting, Credential, Project


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def key_env(tmp_path, monkeypatch):
    key_dir = tmp_path / "keys"
    monkeypatch.setenv("POTATO_KEY_DIR", str(key_dir))
    monkeypatch.delenv("POTATO_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    key_store.reset_for_tests(None)
    yield key_dir
    get_settings.cache_clear()
    key_store.reset_for_tests(None)


def _project(name: str, raw: str) -> Project:
    return Project(name=name, base_url="http://example.test", login_state=raw)


async def _init_key(db_session):
    async with db_session() as sess:
        return await key_store.initialize(sess)


def test_backfill_encrypts_and_is_idempotent(tmp_db, key_env):
    _, db_session, _ = tmp_db
    _run(_init_key(db_session))
    raw = '{"cookies": [{"name": "sid"}], "origins": []}'

    async def scenario():
        async with db_session() as s:
            s.add(_project("legacy", raw))
        async with db_session() as s:
            assert await login_state.backfill(s) == 1
        async with db_session() as s:
            assert await login_state.backfill(s) == 0
            creds = (
                (await s.execute(select(Credential).where(Credential.type == "storage_state")))
                .scalars()
                .all()
            )
            project = (await s.execute(select(Project).where(Project.name == "legacy"))).scalar_one()
            assert len(creds) == 1
            assert creds[0].is_active is True
            assert creds[0].role is None
            assert creds[0].environment_id is None
            assert crypto.decrypt(creds[0].secret) == raw
            # Kept for one release so a rollback can still read it.
            assert project.login_state == raw

    _run(scenario())


def test_backfill_skips_projects_that_already_have_a_storage_state(tmp_db, key_env):
    _, db_session, _ = tmp_db
    _run(_init_key(db_session))

    async def scenario():
        async with db_session() as s:
            p = _project("already-migrated", "plaintext")
            s.add(p)
            await s.flush()
            s.add(
                Credential(
                    project_id=p.id,
                    type="storage_state",
                    label="existing",
                    secret=crypto.encrypt("encrypted"),
                    is_active=False,
                )
            )
        async with db_session() as s:
            assert await login_state.backfill(s) == 0
        async with db_session() as s:
            count = len((await s.execute(select(Credential))).scalars().all())
            assert count == 1

    _run(scenario())


def test_backfill_does_nothing_without_a_key(tmp_db, key_env):
    _, db_session, _ = tmp_db

    async def scenario():
        async with db_session() as s:
            s.add(_project("no-key", "plaintext"))
        async with db_session() as s:
            assert await login_state.backfill(s) == 0
        async with db_session() as s:
            assert (await s.execute(select(Credential))).scalars().all() == []

    _run(scenario())


def test_create_project_stops_writing_the_plaintext_column(tmp_db, key_env):
    from fastapi.testclient import TestClient

    from app.main import app

    raw = '{"cookies": [], "origins": []}'
    with TestClient(app) as c:
        r = c.post(
            "/api/projects",
            json={"name": "new-project", "base_url": "http://example.test", "login_state": raw},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["has_login_state"] is True
        pid = body["id"]

    async def check():
        async with tmp_db[1]() as s:
            p = await s.get(Project, pid)
            assert p is not None and p.login_state is None
            cred = (
                await s.execute(select(Credential).where(Credential.project_id == pid))
            ).scalar_one()
            assert cred.type == "storage_state"
            assert crypto.decrypt(cred.secret) == raw

    _run(check())


def test_import_key_backfills_immediately(tmp_db, key_env):
    """Backup-key import must migrate plaintext without a restart."""
    from fastapi.testclient import TestClient

    _, db_session, _ = tmp_db
    right_key = Fernet.generate_key().decode()
    token = Fernet(right_key.encode()).encrypt(b"stored-secret").decode()
    raw = '{"cookies": [{"name": "sid"}], "origins": []}'

    async def seed():
        async with db_session() as s:
            s.add(AppSetting(key="probe-secret", value=token, secret=True))
            s.add(_project("legacy-during-recovery", raw))

    _run(seed())

    from app.main import app

    with TestClient(app) as c:
        status = c.get("/api/secret-key/status")
        assert status.json()["recovery_mode"] is True
        r = c.post("/api/secret-key/import", json={"key": right_key})
        assert r.status_code == 200, r.text

    async def check():
        async with db_session() as s:
            cred = (
                await s.execute(select(Credential).where(Credential.type == "storage_state"))
            ).scalar_one()
            assert crypto.decrypt(cred.secret) == raw

    _run(check())
