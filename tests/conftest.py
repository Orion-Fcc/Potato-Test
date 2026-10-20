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
import uuid

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


@pytest.fixture(autouse=True)
def _reset_db_between_tests():
    """★ 每个测试结束后：把 `app.db` 的全局状态还原，**并**把任何指向真库的
    engine 换成临时库。2026-10-10 新增。

    ## 为什么需要它（这是一次真实事故的根治）

    真库里凭空多出 9 个项目（`reconcile-check` / `live-check` / `fk-check`，
    18 条 case、12 个 run、18 条 run_result）。追下去是**同一个模式**在 15 个
    测试文件里重复出现：

        db._engine = db._Session = None      # "先把上一个测试的 engine 丢掉"
        ...
        await db.init_db()                   # ← 于是按 settings 重建 = 连真库

    上半句看着人畜无害（"清干净"），下半句就把所有写入送到了用户的真库。
    这些文件写的时候大概都以为"反正 conftest 有一条会话级临时库兜着" ——
    但那条兜底只在**会话开始时**设一次，一旦被置 None 就再也回不来了。

    ## 为什么修在这里，而不是去改那 15 个文件

    已有的两条防线是"预防一次"和"事后探测"：

      * `_session_writes_go_to_a_temp_db`：会话开始时把默认目标换成临时库。
        **一次性**，被置 None 之后就失效了。
      * `_never_touch_the_real_db`：事后比对行数，能发现污染。
        但它是**事后**的 —— 数据已经写进去了，用户已经能看见了。

    两个都拦不住"中途把 engine 置 None"这个动作。这里补的是**每次测试之后
    都重设一遍**：只要 engine 是 None 或者指向真库，就换成一个临时库。
    这样那 15 个文件的写法从"静默写脏真库"变成"写在临时库上" ——
    它们的断言照常通过，用户的数据一个字都不动。

    为什么是"换成临时库"而不是"报错"：报错会当场弄红 24 条测试，
    而那些测试想测的根本不是数据库隔离（是重试策略、是向导勾选）。
    它们只是**恰好**用了这个坏模式。让它们继续工作、同时不再碰真库，
    才是这次要的结果。

    `_db_is_real` 那条断言留在最后：如果连这个兜底都没拦住，说明有测试
    **绕过了 app.db** 自己 create_async_engine(settings.database_url) ——
    那种情况必须炸出来，不能继续静默污染。
    """
    yield
    try:
        import importlib

        from app.config import get_settings

        dbmod = importlib.import_module("app.db")
        real = str(_REAL_DB)
        cur = str(getattr(dbmod._engine, "url", "") or "")
        # 已经指向某个临时库 -> 什么都不用做（绝大多数测试走这条）
        if dbmod._engine is not None and dbmod._Session is not None and real not in cur:
            return
        # engine 是 None，或指向真库 —— 换成一个会话内的临时库
        import asyncio
        import tempfile

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.models import Base

        target = pathlib.Path(tempfile.gettempdir()) / "_tp_rehydrated.db"
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

        assert real not in str(eng.url), (
            f"兜底失败：engine 仍然指向真库 {eng.url}。"
            "这个测试绕过了 app.db 自己建了 engine —— 必须改掉，否则会污染用户数据。"
        )
        # 让 settings 也指向它：有些代码自己读 settings.database_url 建连接。
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + target.as_posix()
        get_settings.cache_clear()
    except Exception:  # noqa: BLE001 — 兜底本身失败不该弄红一个无关的测试
        pass


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


def bind_temp_db(db_path) -> tuple[object, object]:
    """把 `app.db` 的 engine/sessionmaker 指向 `db_path`，并返回**原值**。

    给那些"想自己控制库文件"的测试用，替代它们原来的这段写法：

        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_db}"
        db._engine = db._Session = None      # ← 灾难来源
        await db.init_db()

    ★ 为什么最后一行的写法必须废掉：`_engine = None` 之后 `init_db()` 会按
    settings 重建 engine。而这个项目里 **settings 读的是 .env**，
    `os.environ` 的优先级在 pytest 下并不可靠（见 PROJECT_ISOLATION.md 第七节），
    实测结果就是"以为切到临时库、其实连的是用户真库" —— 真库里因此多出过
    9 个测试项目。直接构造 engine 才能确定。

    用法（前两步就够，不需要再 init_db —— 这里已经把表建好了）：

        eng, prev = bind_temp_db(tmp_path / "to.db")
        try:
            ...
        finally:
            restore_db(*prev)
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod
    from app.models import Base

    prev = (dbmod._engine, dbmod._Session)
    target = pathlib.Path(db_path)
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
    # 自证：没有这条断言，写错路径时是静默的，而症状出现在用户的真库里。
    assert _REAL_DB.name not in str(eng.url), f"竟然连到了真库：{eng.url}"
    return eng, prev


def restore_db(engine, session) -> None:
    """把 `bind_temp_db` 拿到的原值还原回去（**永远不要置 None**）。"""
    from app import db as dbmod

    if engine is not None and session is not None:
        dbmod._engine, dbmod._Session = engine, session


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


# ---------------------------------------------------------------------------
# ★ 在**真实数据库**上跑测试（2026-10-09）
# ---------------------------------------------------------------------------
#
# 为什么要有这条路径：临时库（上面那两条）保证不了"真库上也能跑对"。
# 真实数据有它的形状 —— 368+555+54+226 条用例、多角色、多环境、31 份凭据、
# 1295 条知识库切片 —— 用临时库测出来的"通过"不代表真库上通过。
# 而"直接跑真库"以前是**禁止**的，理由充分：2026-10-06 两次把测试数据写进了
# 用户的真库（多出 24 个空项目）。所以这里给的不是"放开写"，而是**写了也会回滚**。
#
# 机制（实测确认过，见 tests/test_real_db_mode.py）：
#   1. 建 engine 并**手动开一个外层事务**；
#   2. 把 sessionmaker 绑到这个 connection 上，用
#      `join_transaction_mode="rollback_only"` —— 这让会话里的每一次 commit
#      都变成空操作，写只在当前事务内可见，外层 rollback 一执行就全没了；
#   3. 测试结束后无条件 rollback。
#
# ★ 为什么是 rollback_only 而不是 create_savepoint —— 这是踩过的坑：
#   第一版用了 `join_transaction_mode="create_savepoint"`，结果**数据真的落库了**，
#   两条 `__ROLLBACK_PROBE__` 写进了用户的 potato.db（已清除）。
#   原因是 create_savepoint 会为会话开一个 SAVEPOINT，而 SAVEPOINT 的 RELEASE
#   在外层事务里是持久化的；再加上外层事务与真实 COMMIT 的时序，最终没被回滚。
#   实测三种模式的结论（在真库副本上跑）：
#       create_savepoint  -> LEAKED
#       default           -> ROLLBACK OK
#       rollback_only     -> ROLLBACK OK   ← 选它
#   选 rollback_only 而不是 default，是因为它的语义最直白：**这个会话里的 commit
#   不可能生效**。某个测试以为自己写下了一条记录、后面读得到，那只说明事务内可见，
#   不代表落库 —— 这正是我们想要的"安全地操作真库"。
#
# 用 `_session_writes_go_to_a_temp_db` 的临时库做底：本 fixture 在函数级覆盖
# `_engine`/`_Session`，结束时**还原**（不是置 None），和 tmp_db 一样。
# 这样会话级临时库在真库用例跑完之后自动回来，其它 900 多条用例不受影响。
# 真实库路径复用文件上方 _REAL_DB（_never_touch_the_real_db 用的同一个）。


def _real_db_url() -> str:
    """真实数据库的绝对 URL —— **直接指向仓库根的 potato.db**。

    ## 为什么不再去解析 `.env` / `get_settings().database_url`

    两个原因，都是实测踩出来的：

    1. **相对路径是按进程 cwd 解析的**，于是"真库"会随着从哪个目录跑测试
       而变成另一个文件。
    2. ★ **`DATABASE_URL` 会被别的测试留在环境变量里**。十几个测试文件用
       `os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path}"` 切库；
       只要有一个没清干净，后面 `real_db` 就会把**那个临时库**当成真库 ——
       表现是 `test_real_db_mode.py` 六条用例一起红，报"真库里一个项目都没有"，
       而报错完全指不到是环境变量泄漏。
       （实测：`test_feishu.py`、`test_isolation_and_policy.py`、`test_multi_role.py`、
       `test_credential_edit.py`、`test_root_cause_and_rag.py` 都会触发。）

    `real_db` 的语义是"连**用户的真库**"，那就该是一个确定的文件，
    不该受环境变量影响。precedence 上真库也确实是文件系统里那个 potato.db。
    """
    return f"sqlite+aiosqlite:///{_REAL_DB.as_posix()}"


@pytest.fixture
def real_db(_db_points_at_a_temp_db_before_every_test):
    """★ 在**真实** potato.db 上跑测试，所有写入都在事务里，结束回滚。

    给的是 ``run(coro)` `` —— 一个"在这条连接上跑这段协程"的执行器：

        def test_reads_real_data(real_db):
            n = real_db.run(_count_cases())
            assert n > 0

    为什么不直接 yield 库对象：本套件用 `asyncio_mode=auto`，测试体内再调
    `asyncio.run()` 会撞上"事件循环已在运行"。而 connection / engine / sessionmaker
    都是绑定事件循环的，不能跨 `asyncio.run()` 复用。所以由 fixture 用**一次**
    `asyncio.run()` 把所有东西建好并交给调用方，调用方也用它跑自己的协程。

    ★ 参数里显式依赖 `_db_points_at_a_temp_db_before_every_test` **不是装饰**：
    那条 autouse fixture 会把 app.db 指到临时库，本 fixture 必须**在它之后**执行，
    才能把目标换成"真库 + 只读事务"。写成隐式依赖的话，pytest 的 autouse 顺序
    会让临时库最后生效，于是"真库测试"其实读的是空临时库 —— 六条用例一起红，
    而报错说"真库里一个项目都没有"，完全指不到是这个顺序问题。
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod

    url = _real_db_url()
    if f"{_REAL_DB.name}" not in url and "sqlite" not in url:
        pytest.skip(f"真实库不可用（{url}）—— 无真库基线可测")
    if not _REAL_DB.is_file():
        pytest.skip(f"真实库不存在：{_REAL_DB} —— 干净环境无真库基线，非代码缺陷")

    state: dict = {}

    async def _setup():
        eng = create_async_engine(url, future=True, poolclass=NullPool)
        conn = await eng.connect()
        trans = await conn.begin()  # ← 外层事务，最后一定 rollback
        Session = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="rollback_only"
        )
        state.update(eng=eng, conn=conn, trans=trans, Session=Session)
        return Session

    asyncio.run(_setup())

    prev_engine, prev_session = dbmod._engine, dbmod._Session
    assert state["Session"] is not None
    dbmod._engine = state["eng"]
    dbmod._Session = state["Session"]

    class _RealDB:
        """真库句柄：``ctx.run(coro)`` 在这条连接上跑协程。"""

        def __init__(self, runner):
            self._runner = runner

        def run(self, coro):
            return self._runner(coro)

    try:
        yield _RealDB(asyncio.run)
    finally:
        dbmod._engine, dbmod._Session = prev_engine, prev_session

        async def _teardown():
            await state["trans"].rollback()
            await state["conn"].close()
            await state["eng"].dispose()

        asyncio.run(_teardown())


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
def _db_points_at_a_temp_db_before_every_test():
    """★ **每个测试开始之前**都确保 `app.db` 指向临时库，绝不指向真库。

    这是 2026-10-10 那起事故的正面修法。原来只有"会话开始时设一次"（
    `_session_writes_go_to_a_temp_db`）和"测试结束后兜底"（`_reset_db_between_tests`），
    但 15 个测试文件在**测试体内部**写了这个模式：

        db._engine = db._Session = None      # "先把上一个的丢掉"
        await db.init_db()                   # ← 按 settings 重建 = 连真库

    它们在测试**中途**把 engine 置空，于是"会话开始时设一次"失效了；
    等测试结束再兜底，数据早就写进真库了。实测后果：真库里多出 9 个项目。

    修在这里而不是去改那 15 个文件：那是 15 处重复的、抄来抄去的样板，
    改它们既容易漏、以后还会有人再写一遍。而"每个测试都从一个临时库开始"
    本来就该是整个套件的默认前提 —— 放在 conftest 里，一次说清。

    代价是每个测试建一个空库（几毫秒，SQLite 文件 + create_all），
    换来的是"测试不可能碰到真库"这条硬保证。
    """
    import asyncio
    import tempfile

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod
    from app.models import Base

    prev = (dbmod._engine, dbmod._Session)
    target = pathlib.Path(tempfile.gettempdir()) / f"_tp_per_test_{uuid.uuid4().hex[:8]}.db"
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
    # 自证：隔离失效时不报错的话，症状会出现在**用户的真库里**。
    assert _REAL_DB.name not in str(eng.url), f"测试库竟然指向真库：{eng.url}"

    yield

    # 还原成测试开始前的值（可能是会话级临时库，也可能是上一个测试留下的），
    # 让"会话级"那层语义在整个套件里保持成立。**绝不置 None**。
    dbmod._engine, dbmod._Session = prev
    try:
        asyncio.run(eng.dispose())
    except Exception:  # noqa: BLE001
        pass
    try:
        target.unlink(missing_ok=True)
    except OSError:
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


@pytest.fixture(autouse=True)
def _credential_key_is_local_to_each_test():
    """给每个测试一把独立、合法的凭据密钥。

    key_store 现在会记住初始化状态；不重置的话，上一个测试的密钥（或"缺少密钥"
    状态）会漏到下一个测试，表现为随机的加密/解密失败。测试不写任何密钥文件。
    """
    from cryptography.fernet import Fernet

    from app import key_store

    key_store.reset_for_tests(Fernet.generate_key().decode())
    yield
    key_store.reset_for_tests(None)
