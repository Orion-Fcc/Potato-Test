"""测试隔离：回滚"直接赋值模块属性"的打桩。

背景（2026-10-02 实测到的真实故障）：

若干测试用**直接赋值**的方式打桩：

    executor.execute_case = fake_execute_case
    engine._ensure_bundle = fake_ensure_bundle

它们写的是 `def main(): ...` + `asyncio.run(main())` 的形式，没有用 pytest 的
`monkeypatch` fixture，所以**打桩不会被回滚**，会泄漏给同进程里后续跑的所有测试。

后果不是"某个测试报错"，而是**静默失效**，这更危险：
`tests/test_speed_levers.py` 和 `tests/test_failure_evidence.py` 用
`inspect.getsource(execute_case)` 断言"真实实现里有没有某段关键代码"。
一旦 `execute_case` 已被替换成 fake，读到的是 **fake 的源码**：
  * 断言失败 → 看起来像产品代码坏了（本次就是这样误导了一次排查）
  * 更糟：fake 里恰好也有同名字符串时断言通过 → 那条约束**根本没在测**，
    而套件仍然全绿。

这里用 autouse fixture 在**每个测试结束后**把被替换的属性还原。原始值在第一次
fixture setup（即第一个测试运行前、任何打桩发生前）捕获，所以不会把已污染的值
误当成"原始值"。
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest

# 被"直接赋值"污染的模块级属性。新增此类打桩时，请同时把名字加到这里，
# 或者干脆改用 monkeypatch fixture（那个会自动回滚，不需要登记）。
_GUARDED = (
    ("app.executor", "execute_case"),
    ("app.engine", "_ensure_bundle"),
)

_ORIGINALS: dict[tuple[str, str], object] = {}


def _capture_originals() -> None:
    import importlib

    for mod_name, attr in _GUARDED:
        key = (mod_name, attr)
        if key in _ORIGINALS:
            continue
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 — 模块导入失败不该让整个套件挂掉
            continue
        _ORIGINALS[key] = getattr(mod, attr, None)


@pytest.fixture(autouse=True)
def _rollback_module_stubs():
    import importlib

    # 捕获必须发生在任何测试体执行之前，否则 capture 到的就是被污染的值。
    _capture_originals()
    yield
    for (mod_name, attr), original in _ORIGINALS.items():
        if original is None:
            continue
        try:
            setattr(importlib.import_module(mod_name), attr, original)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 环境变量与进程级缓存的隔离（2026-10-06）。
#
# 这是上面那类"静默失效"的另一半。十几个测试文件直接写环境变量而不还原：
#
#     os.environ["POTATO_SECRET_KEY"] = "x" * 32      # 不是合法的 Fernet 密钥
#     os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_db}"
#
# 而 app 侧有两个进程级 lru_cache：`app.config.get_settings` 与 `app.crypto._fernet`。
# 缓存 + 未还原的环境变量 = 测试之间互相投毒。实测到的形态：
# tests/test_case_last_result.py 把密钥设成 "x"*32、自己 cache_clear 了 settings，
# 但没管 _fernet；于是之后**第一个真正调用 encrypt() 的测试**会拿到这个非法密钥，
# 报出 `ValueError: Fernet key must be 32 url-safe base64-encoded bytes` ——
# 一个跟它自己毫无关系、也不知道该去改哪里的报错。
#
# 实测数字：tests/test_credential_edit.py 单独跑 13 passed；
# 接在 test_case_last_result.py 后面跑 11 failed。失败与被测代码无关，
# 纯粹是套件内部的顺序依赖 —— 而这类失败最费时间，因为它指向错误的方向。
#
# 不逐个去修那十几个文件（改动面大，而且以后还会有人这么写），
# 在这里统一兜底：测前存档 + 清缓存，测后还原 + 清缓存。
_ENV_GUARDED = ("POTATO_SECRET_KEY", "DATABASE_URL", "AUTH_ENABLED")


@pytest.fixture(autouse=True)
def _no_real_feishu_worker(monkeypatch):
    """测试里绝不启动真的飞书轮询机器人。

    2026-10-06 起，机器人随 API 服务进程启动（app/main.py 的 lifespan 会调
    app.feishu_poll.serve_in_process）。而本仓库的端到端测试大量使用
    `TestClient(app)`，那是会真的跑 lifespan 的 —— 不拦的话，每建一个
    TestClient 就会：抢单实例锁、往 .feishu_ws.pid 写本进程 PID、
    并开始每 8 秒调一次 open.feishu.cn。

    后果不只是慢：锁被测试进程占住之后，用户手动启动的机器人会以
    "已有轮询实例在运行"静默退出 —— 一次 pytest 就能让群机器人再也不回话，
    而症状出现在完全无关的时间点。

    monkeypatch 会在测试结束时自动还原，不会污染后续用例。
    """
    monkeypatch.setenv("FEISHU_WORKER_IN_PROCESS", "false")


# ---------------------------------------------------------------------------
# ★ 真实数据库不许被测试写入
# ---------------------------------------------------------------------------
#
# 2026-10-06 实测事故：`tests/test_knowledge_chunks.py` 没做任何隔离，
# 每跑一次就往用户的真库里建 4 个项目（kb-roundtrip / kb-replace / kb-a / kb-b）。
# 当天我跑了 6 次全量套件 —— 真项目列表里就多了 24 个空壳项目。
#
# 为什么不能再靠"每个测试自己记得隔离"：
#   * 本仓库已经有 4 种不同的隔离写法，其中一种（设 DATABASE_URL 环境变量）
#     在这个环境里**会静默失效** —— app/db.py 把 engine 缓存在模块级全局变量里，
#     同一个 pytest 进程里只要更早的测试触发过 _ensure()，之后改环境变量毫无作用；
#     pydantic-settings 读 .env 与环境变量的优先级在这里也不可靠。
#   * 也就是说，"我以为我隔离了"是没有证据的。而这类故障的表现是
#     **用户的界面里凭空多出东西**，一眼就能看见，却极难定位到是哪个测试。
#
# 所以这里改成**事后可证伪**：每个测试前后各读一次真库的计数，不一致就 fail。
# 它不需要任何测试配合 —— 忘隔离的测试会自己把套件弄红，而红的那一刻
# 就是"你刚刚改了用户的真实数据"的证据。

_REAL_DB = pathlib.Path(__file__).resolve().parent.parent / "potato.db"

# 只盯这三张表：它们只在"人或测试创建/删除项目、凭据、用例"时变化。
# run / run_result 这类表会被正在运行的服务随时改动，盯它们只会误报。
_WATCHED_TABLES = ("project", "test_case", "credential")


def _real_db_counts() -> dict[str, int] | None:
    """真库当前各表的行数；打不开就返回 None（不阻塞套件）。"""
    if not _REAL_DB.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{_REAL_DB.as_posix()}?mode=ro", uri=True)
        try:
            return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _WATCHED_TABLES}
        finally:
            con.close()
    except sqlite3.Error:
        return None


@pytest.fixture(autouse=True)
def _never_touch_the_real_db():
    before = _real_db_counts()
    yield
    after = _real_db_counts()
    if before is None or after is None or before == after:
        return
    grew = {t: (before[t], after[t]) for t in _WATCHED_TABLES if before[t] != after[t]}
    detail = ", ".join(f"{t} {a} -> {b}" for t, (a, b) in grew.items())
    pytest.fail(
        f"这个测试写了真实数据库 {_REAL_DB}：{detail}。\n"
        "用户会在自己的项目列表里直接看到这些数据。\n"
        "修法：不要用 `os.environ['DATABASE_URL']` 切库（在本项目里会静默失效），\n"
        "      直接构造 engine 并赋值给 `app.db._engine` / `app.db._Session`，\n"
        "      并照 tests/test_isolation_and_policy.py::dep_project 的写法自证隔离。"
    )


@pytest.fixture(autouse=True, scope="session")
def _session_writes_go_to_a_temp_db():
    """★ 整个 pytest 会话里，**默认**写入目标是临时库，不是用户的真库。

    2026-10-06 的事故就是这么来的：`tests/test_knowledge_chunks.py` 里
    `await init_db()` + `db_session()` 没有任何隔离，每跑一次就在用户的
    `potato.db` 里建 4 个项目（kb-roundtrip / kb-replace / kb-a / kb-b）。
    那天我跑了 6 次全量套件 —— 用户的项目列表里就多了 24 个空壳。

    为什么这一条能一劳永逸：`app/db.py` 里**只有 `_ensure()`** 会建 engine，
    而且只在 `_Session is None` 时才建。所以会话一开始就把 `_engine` / `_Session`
    指向临时库，全套测试就再也没有别的入口能碰到真库 —— 不依赖每个测试作者记得隔离。

    和 `_never_touch_the_real_db` 的分工，**两条都要留**：
      * 本条是**预防**（让它没有入口）；
      * 那条是**探测**（万一将来出现绕过 `app.db` 的新入口，比如某处自己
        `create_async_engine(settings.database_url)`）。
    只有探测会先污染再报警；只有预防则在被绕过时静默失效。
    """
    import asyncio
    import tempfile

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod
    from app.models import Base

    target = pathlib.Path(tempfile.gettempdir()) / "_tp_pytest_session.db"
    target.unlink(missing_ok=True)
    eng = create_async_engine(
        "sqlite+aiosqlite:///" + target.as_posix().replace("\\", "/"),
        future=True,
        poolclass=NullPool,
    )
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    # 自证：这套"预防"本身没失效
    assert target.name in str(eng.url), f"会话库不是临时库：{eng.url}"
    assert _REAL_DB.name not in str(eng.url), "会话库竟然指向用户的真库"

    yield

    dbmod._engine = None
    dbmod._Session = None
    asyncio.run(eng.dispose())
    target.unlink(missing_ok=True)


@pytest.fixture
def tmp_db(tmp_path):
    """一个指向临时 SQLite 的 `(engine, db_session)`，并**自证**连的是临时库。

    给出 engine 而不是只给 sessionmaker：调用方有时还要 `create_all` 或 `dispose`。
    大多数测试其实不需要它 —— 会话级的 `_session_writes_go_to_a_temp_db` 已经把
    默认目标换成了临时库；需要**自己**控制库文件的测试才用它。
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod
    from app.models import Base

    target = tmp_path / "_tp_test.db"
    eng = create_async_engine(
        "sqlite+aiosqlite:///" + target.as_posix().replace("\\", "/"),
        future=True,
        poolclass=NullPool,
    )
    # 记下会话级的值：teardown 必须还原成它，而不是 None ——
    # 置 None 会让下一个测试走 _ensure()，而那才是真库。
    prev_engine, prev_session = dbmod._engine, dbmod._Session
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    # 这一行不能省：没有它，隔离失效时是静默的，而症状在用户的界面上。
    assert target.name in str(eng.url), f"没连到临时库：{eng.url}"

    yield eng, dbmod.db_session, dbmod

    # 还原成会话级的临时库，而不是 None —— 见上面 prev_engine 那行注释。
    dbmod._engine = prev_engine
    dbmod._Session = prev_session
    asyncio.run(eng.dispose())


def _clear_process_caches() -> None:
    """清掉读取环境变量的两处 lru_cache，让下一次调用重新解析。"""
    try:
        from app.config import get_settings

        get_settings.cache_clear()
    except Exception:  # noqa: BLE001 — 模块导入失败不该让整个套件挂掉
        pass
    try:
        from app import crypto

        crypto._fernet.cache_clear()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _rollback_env_and_caches():
    saved = {k: os.environ.get(k) for k in _ENV_GUARDED}
    # 测前就要清：上一个测试可能已经把非法密钥留在缓存里了。
    _clear_process_caches()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _clear_process_caches()
