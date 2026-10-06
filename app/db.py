"""Async engine + session factory. create_all for MVP (Alembic added in Phase 2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Base

_engine = None
_Session: async_sessionmaker[AsyncSession] | None = None


def _ensure() -> async_sessionmaker[AsyncSession]:
    global _engine, _Session
    if _Session is None:
        # NullPool: never reuse a connection across event loops. Celery tasks each spin a
        # fresh loop via asyncio.run(), and pooled asyncpg connections bound to a prior
        # loop raise "another operation is in progress". A fresh connection per session is
        # cheap enough for this internal tool and kills that whole bug class.
        _engine = create_async_engine(get_settings().database_url, future=True, poolclass=NullPool)
        _Session = async_sessionmaker(_engine, expire_on_commit=False)
    return _Session


async def init_db() -> None:
    _ensure()
    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


# Columns added to tables that already exist in a deployed database. `create_all` only
# creates missing TABLES — it silently leaves an existing table alone — so a new column on
# run_result would never appear for anyone who already has a potato.db, and every insert
# would then fail with "no such column". Kept as (table, column, DDL type) and applied
# idempotently on every boot. New tables do NOT need an entry here.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # AI-written bug description for failed cases (see app/failure_narrative.py).
    ("run_result", "failure_narrative", "JSON"),
    # 2026-10-04 多角色执行：test_case.roles 是一个 JSON 数组，声明这条用例
    # 流程中会依次用到哪些角色（例：["applicant", "approver"]）。`role` 单值列
    # **保留不动** —— 378 条存量用例都带着它，删掉等于逼所有人重录一遍。
    # roles 为空时执行层回落到 role，所以老用例不迁移也能照常跑。
    ("test_case", "roles", "JSON"),
    # 2026-10-06 测试数据文件声明：让"先导入 Excel 再验证"这类用例把文件内容
    # 写进用例，执行前物化成真文件（见 app/testdata.py）。存量行为 NULL = 不声明文件。
    ("test_case", "data_files", "JSON"),
    # 2026-10-04 失败根因分类：让报告能区分"真缺陷"与"环境/账号/前置数据"类假失败。
    # 取值见 app/judge.py 的 _ROOT_CAUSES；存量行为 NULL（分类功能上线前的结果），
    # 报告侧要按"未分类"展示，不能当成 unclear —— 那会冤枉历史数据。
    ("run_result", "root_cause", "VARCHAR(30)"),
    # 判定器引用的步骤号（1-based），落库便于核对理由是否可追溯。
    ("run_result", "verdict_evidence", "JSON"),
    # 2026-10-06 人工改判：AI 判定会不准（用户明确反馈过），测试工程师必须能自己
    # 把一条用例改成"通过/不通过"。三列一起记录**这次改判本身**是谁在什么时候、
    # 因为什么做的 —— 没有这些，报告里的"通过"到底是 AI 判的还是人改的就分不清，
    # 统计口径会被污染（真缺陷率、通过率全都失真）。
    ("run_result", "verdict_override", "VARCHAR(20)"),   # passed|failed|NULL=未改判
    ("run_result", "override_reason", "VARCHAR(500)"),
    ("run_result", "override_by", "VARCHAR(200)"),
    ("run_result", "override_at", "VARCHAR(40)"),
    # 改判前的 AI 原判。**必须单独存一列**：改判是直接写 status 的（这样报表/筛选
    # 自动生效），原值就被覆盖了；没有它，"撤销改判"就只能瞎猜回去。
    ("run_result", "original_status", "VARCHAR(20)"),
)


def _add_missing_columns(sync_conn) -> None:
    """Add any column in _ADDITIVE_COLUMNS that the live table is missing.

    Uses the SQLAlchemy inspector rather than a dialect-specific PRAGMA so this keeps
    working if the database is ever moved off SQLite (the URL is configurable).
    """
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(sync_conn)
    existing_tables = set(insp.get_table_names())
    for table, column, ddl_type in _ADDITIVE_COLUMNS:
        if table not in existing_tables:
            continue  # create_all just made it complete; nothing to patch
        cols = {c["name"] for c in insp.get_columns(table)}
        if column in cols:
            continue
        sync_conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl_type}')


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    session = _ensure()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
