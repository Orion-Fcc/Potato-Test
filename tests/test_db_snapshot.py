"""Migration snapshots: a recoverable DB copy must exist before any DDL runs."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app import db as dbmod
from tests.conftest import bind_temp_db, restore_db


def _snapshots(db_path) -> list:
    return sorted((db_path.parent / dbmod._SNAPSHOT_DIRNAME).glob("potato-*.db"))


def _seed_marker(db_path, value: str) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS _snapshot_marker (value TEXT)")
        con.execute("DELETE FROM _snapshot_marker")
        con.execute("INSERT INTO _snapshot_marker VALUES (?)", (value,))
        con.commit()
    finally:
        con.close()


def test_init_db_creates_a_readable_snapshot(tmp_path):
    db_path = tmp_path / "snap.db"
    eng, prev = bind_temp_db(db_path)
    try:
        _seed_marker(db_path, "before-init")
        asyncio.run(dbmod.init_db())
        snaps = _snapshots(db_path)
        assert len(snaps) == 1
        con = sqlite3.connect(str(snaps[0]))
        try:
            row = con.execute("SELECT value FROM _snapshot_marker").fetchone()
        finally:
            con.close()
        assert row == ("before-init",)
    finally:
        restore_db(*prev)
        asyncio.run(eng.dispose())


def test_only_three_snapshots_are_kept(tmp_path):
    db_path = tmp_path / "retain.db"
    eng, prev = bind_temp_db(db_path)
    try:
        for i in range(4):
            _seed_marker(db_path, str(i))
            asyncio.run(dbmod.init_db())
            time.sleep(0.002)
        snaps = _snapshots(db_path)
        assert len(snaps) == 3
        # Newest snapshot must contain the last value written before its run.
        con = sqlite3.connect(str(snaps[-1]))
        try:
            row = con.execute("SELECT value FROM _snapshot_marker").fetchone()
        finally:
            con.close()
        assert row == ("3",)
    finally:
        restore_db(*prev)
        asyncio.run(eng.dispose())


def test_memory_database_is_skipped(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    prev = (dbmod._engine, dbmod._Session)
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:", future=True, poolclass=NullPool
    )
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        assert dbmod._snapshot_target() is None
        asyncio.run(dbmod.init_db())
    finally:
        dbmod._engine, dbmod._Session = prev
        asyncio.run(eng.dispose())


def test_snapshot_failure_aborts_before_ddl(tmp_path):
    db_path = tmp_path / "broken.db"
    eng, prev = bind_temp_db(db_path)
    try:
        backup_path = db_path.parent / dbmod._SNAPSHOT_DIRNAME
        # A file where the snapshot directory should be makes mkdir fail.
        backup_path.write_text("not a directory", encoding="utf-8")
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(dbmod.init_db())
        msg = str(exc.value)
        assert "快照失败" in msg
        assert "未执行任何迁移" in msg
    finally:
        restore_db(*prev)
        asyncio.run(eng.dispose())
