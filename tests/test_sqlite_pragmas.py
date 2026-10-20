"""SQLite 并发 PRAGMA 的测试（2026-10-20）。

守的是一条很具体的故障：执行器每个用例一个浏览器并发跑，SSE 每秒查一次库，
而 `app/db.py` 用的是 NullPool（每条 session 一条新连接）。没有 WAL + busy_timeout
时，写锁一撞就是 `database is locked` —— 用户看到的是"执行到一半整批失败"。

★ 这里**不能**用 conftest 的 `tmp_db` / `bind_temp_db`：它们直接
`create_async_engine(...)`，绕过了 `db._create_engine()`，因此也绕过了 PRAGMA 监听。
用它们测会永远绿 —— 而线上那条路径是不是设上了，恰恰没人知道。
"""

from __future__ import annotations

import asyncio

import pytest

from app import db as dbmod


def _pragmas(url: str) -> dict[str, object]:
    """按生产路径建 engine，读一条**新连接**上的 PRAGMA。"""
    eng = dbmod._create_engine(url)

    async def _probe() -> dict[str, object]:
        async with eng.connect() as conn:
            return {
                "journal_mode": (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar(),
                "busy_timeout": (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar(),
                "synchronous": (await conn.exec_driver_sql("PRAGMA synchronous")).scalar(),
            }

    try:
        return asyncio.run(_probe())
    finally:
        asyncio.run(eng.dispose())


def test_engine_connections_use_wal_and_busy_timeout(tmp_path):
    url = "sqlite+aiosqlite:///" + (tmp_path / "_tp_pragma.db").as_posix()
    got = _pragmas(url)

    # WAL 是持久属性：设一次就写进库文件；读返回小写 "wal"
    assert str(got["journal_mode"]).lower() == "wal"
    # 撞锁最多等 5 秒，而不是立刻抛 "database is locked"
    assert got["busy_timeout"] == 5000
    # NORMAL = 1；WAL 下的常规选择（比 FULL 快、比 OFF 安全）
    assert got["synchronous"] == 1


def test_listener_is_only_attached_for_sqlite(monkeypatch):
    """别的数据库不认识这些 PRAGMA —— 监听只能在 sqlite 上加。

    环境里没有 pg 驱动，所以把 `create_async_engine` 换成一个不连接任何东西的假对象，
    只观察 `event.listen` 被调用的次数与参数。
    """
    from types import SimpleNamespace

    calls: list[tuple] = []
    monkeypatch.setattr(
        dbmod, "create_async_engine", lambda url, **kw: SimpleNamespace(sync_engine=url)
    )
    monkeypatch.setattr(dbmod.event, "listen", lambda *a, **kw: calls.append(a))

    dbmod._create_engine("postgresql+asyncpg://user:pw@127.0.0.1/none")
    assert calls == [], "非 SQLite 也挂了 PRAGMA 监听 —— 换库时会直接报语法错"

    dbmod._create_engine("sqlite+aiosqlite:///x.db")
    assert len(calls) == 1
    assert calls[0][1] == "connect"
    assert calls[0][2] is dbmod._set_sqlite_pragmas


def test_pragma_failure_is_visible_not_silent():
    """`_set_sqlite_pragmas` 出错必须冒泡 —— 静默吞掉等于回到"没设"的状态。"""

    class _BoomCursor:
        def execute(self, *_a, **_kw):
            raise RuntimeError("boom")

        def close(self):
            pass

    class _BoomConn:
        def cursor(self):
            return _BoomCursor()

    with pytest.raises(RuntimeError, match="boom"):
        dbmod._set_sqlite_pragmas(_BoomConn(), None)
