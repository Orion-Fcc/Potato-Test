"""Async engine + session factory. create_all for MVP (Alembic added in Phase 2)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Base

log = logging.getLogger("potato-test.db")

_engine = None
_Session: async_sessionmaker[AsyncSession] | None = None


def _ensure() -> async_sessionmaker[AsyncSession]:
    global _engine, _Session
    if _Session is None:
        # NullPool: never reuse a connection across event loops. Celery tasks each spin a
        # fresh loop via asyncio.run(), and pooled asyncpg connections bound to a prior
        # loop raise "another operation is in progress". A fresh connection per session is
        # cheap enough for this internal tool and kills that whole bug class.
        url = get_settings().database_url
        if url is None:
            url = ""
        # ★ 2026-10-10 哨兵：pytest 里重新按 settings 建 engine 时，**只允许连临时库**。
        #
        # 为什么要它（这是一次真实事故）：测试会在函数级把 `_engine`/`_Session` 换成
        # 自己的库，**结束时置 None**（而不是还原）。于是下一个用到 db 的测试走到这里，
        # 按 settings（= .env 里的 potato.db）重建 engine —— 测试数据直接写进用户的真库。
        # 实测后果：真库多出 9 个项目（reconcile-check / live-check / fk-check，
        # 18 条 case、12 个 run、18 条 run_result），而症状是**另一个文件里的测试报错**，
        # 完全指不到成因。
        #
        # 注意这里**不是**"pytest 里禁止重建"：确实有测试（如 test_run_concurrency）
        # 故意把自己的 tmp_path 库写进 os.environ["DATABASE_URL"] 再重建，那是对的、
        # 也必须允许。所以判据是**目标 URL**：只有它指着真库才拒绝。
        # 用 potato.db 的文件名判断有点粗，但在本项目里够准 —— 真库就叫 potato.db，
        # 而临时库一律带 _tp_ / tmp_path 前缀。
        if (os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_VERSION")) and (
            "potato.db" in url
        ):
            raise RuntimeError(
                "pytest 环境里 app.db 正准备重建 engine 并连上**真实数据库**："
                f"{url}\n"
                "这通常意味着某个测试把 db._engine / db._Session 置成了 None，"
                "于是这里按 settings（.env）重建 —— 那会把测试数据写进用户的数据。\n"
                "正确写法：直接构造 engine 并赋值给 db._engine / db._Session，"
                "结束时**还原**（不要置 None）。见 docs/PROJECT_ISOLATION.md 第七节、"
                "tests/conftest.py 的 bind_temp_db / tmp_db。\n"
                "拒绝继续：再走下去就会污染用户的真实数据库。"
            )
        _engine = create_async_engine(url, future=True, poolclass=NullPool)
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
    # 2026-10-07 失败归因：这次失败是谁的锅（取值见 app/failure_attrib.py 的
    # VERDICT_INFO）。与 root_cause 分工不同 —— 那个是"用例为什么判失败"，这个是
    # "能不能算被测系统的缺陷"。今天实测抓到的两条假缺陷 root_cause 都长得像正常的
    # "功能不符"，只有归因能认出它们其实是页面没加载完 / 流程没走完。
    # 存量行为 NULL = 归因功能上线前的结果，报告侧要显示"未判定"而不是"系统行为"。
    ("run_result", "attribution", "VARCHAR(30)"),
    # 2026-10-09 用例被人为挡住的原因（见 app/schemas.py::CaseStatus）。
    # 用户要求"人可以变更用例状态，因为有些用例 agent 执行不了"——
    # 状态枚举之外必须有原因，否则一个月后没人知道它为什么被挡住。
    # 默认空串而不是 NULL：调用方不必区分"没填"和"NULL"两种情况。
    ("test_case", "blocked_reason", "TEXT NOT NULL DEFAULT ''"),
    # 2026-10-12 用例优化提案要带"为什么"的两样证据：相关流程图与账号。
    # 用户要求「加上相关流程图告诉我为啥要加这个用例或者修改」以及「附上账号」。
    # 都用 NOT NULL DEFAULT ''：老提案（这两列之前就存在）不必回填，
    # 空串在界面上显示成"没有图/未指定账号"，比 NULL 少一种分支。
    ("case_proposal", "mermaid", "TEXT NOT NULL DEFAULT ''"),
    ("case_proposal", "role", "VARCHAR(120) NOT NULL DEFAULT ''"),
    # 2026-10-12「只有我能改」的文档（见 models.ProjectDoc.ai_editable）。
    # ★ 默认 1：老文档（这一列之前就存在）行为不变 —— 它们本来就是 AI 可改的。
    # 若默认 0，升级之后助手会突然"什么都改不了"，而用户没做任何操作。
    ("project_doc", "ai_editable", "BOOLEAN NOT NULL DEFAULT 1"),
    # 2026-10-07 需规漂移信号（见 app/spec_drift.py）。落在 run 上而不是 run_result 上，
    # 因为它**不是**某条用例的属性，而是一次执行的结论：这批用例 collectively 断言了
    # 互相矛盾的行为。挂到用例上会让「哪条用例漂移了」看起来像能逐条修，而实际要么是
    # 需规改了、要么是实现回退了 —— 是同一个决定。
    #
    # 落库而不是只打日志：漂移的处置动作是「停下来改文档」，发生在跑完之后几小时，
    # 那时能重看的只有库。NULL = 该次执行没跑漂移检查（功能上线前的历史 run）。
    ("run", "drift_signals", "JSON"),
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
    # 2026-10-08 耗时分解（执行器 5 段墙钟 + 步数）。埋点本来就在，缺的是落库：
    # 只有日志的话，"这轮为什么慢"没法在报告页按阶段汇总，只能翻日志。
    ("run_result", "timing", "JSON"),
    # 2026-10-08 per-run 重试次数。空值语义 = 0（不重试），与模型默认一致，
    # 所以历史 run 不需要回填。
    ("run", "retries", "INTEGER"),
    # 2026-10-09 用例编号的永久计数器（见 models.Project.case_seq 的说明）。
    # 必须在这里加：`create_all` 只建**缺的表**，对已存在的 project 表一个字段都不动 ——
    # 于是老库永远没有这一列，而代码每次发号都要读它 → 每条新用例都报 "no such column"。
    # 存量行会由这一句填上默认值 0，含义是"尚未回填"，首次发号时从现有编号推算，见
    # app/api.py::_next_case_key。
    ("project", "case_seq", "INTEGER NOT NULL DEFAULT 0"),
    # 2026-10-09 用例来源溯源（人写的 / 从需规哪一节生成的），见 models.TestCase.source。
    ("test_case", "source", "VARCHAR(300)"),
    # 2026-10-13「大系统」：项目归属 + 大系统共享需规块（见 models.ProjectGroup）。
    # 都是可空列，存量行 = NULL，行为不变（项目仍平铺、知识块仍只属于它自己）。
    ("project", "group_id", "INTEGER"),
    ("knowledge_chunk", "group_id", "INTEGER"),
    # 2026-10-16 上传文档的**原件**（见 models.ProjectDoc.source_*）。
    # 四条都可空：上线前导入的文档没有原件，界面按"没有原件"显示即可。
    ("project_doc", "source_key", "VARCHAR(400)"),
    ("project_doc", "source_url", "VARCHAR(1000)"),
    ("project_doc", "source_name", "VARCHAR(300)"),
    ("project_doc", "source_bytes", "INTEGER"),
    # 上次解析产物的指纹（见 models.ProjectDoc.html_parse_hash）——
    # 重传时用来判断"正文是不是已经被人工改过"，别把人的改动覆盖掉。
    ("project_doc", "html_parse_hash", "VARCHAR(64)"),
)

# 后加的**索引**（`_add_missing_columns` 只加列，索引得单独建）。
# 用 IF NOT EXISTS，幂等；表不存在就跳过（新库由 create_all 建好索引）。
_ADDITIVE_INDEXES: tuple[tuple[str, str, str], ...] = (
    # (表, 索引名, 列定义)
    ("project", "ix_project_group_id", '"group_id"'),
    ("knowledge_chunk", "ix_knowledge_chunk_group_id", '"group_id"'),
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
    for table, index_name, cols in _ADDITIVE_INDEXES:
        if table not in existing_tables:
            continue
        sync_conn.exec_driver_sql(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table}" ({cols})'
        )


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
