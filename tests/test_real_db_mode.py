"""在**真实数据库**上跑，但写入会被回滚（2026-10-09）。

## 这组用例回答什么问题

"你能在真实数据上操作测试" —— 能，而且写不进去。这个文件本身就是在证明这条承诺：
它对着真库做读、做写、做只读校验，跑完之后用户的 `potato.db` 必须与跑之前**逐行一致**。

## 与 conftest 里两条隔离的关系

`conftest.py` 的 `_session_writes_go_to_a_temp_db` 让**默认**写入目标是临时库；
`_never_touch_the_real_db` 则是事后探测（真库被写就 fail）。
本文件用的是第三条路：`real_db` fixture —— 真库连接 + 外层事务 +
`join_transaction_mode="rollback_only"`，所以：

* 读到的是**真实数据**（不是造出来的样本）；
* 写只在事务内可见，永远不会落盘；
* 真库被意外改动时，`_never_touch_the_real_db` 仍然会把它抓出来。

## 为什么不用 tmp_path

本机沙箱会给 pytest 新建的 tmp 目录打一条拒绝项，于是 tmp_path 全报
PermissionError。这组用例刻意只用真库里的**既有**数据（不建临时文件）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import Project, TestCase


def _count(model):
    """返回一个"数这张表"的**协程**，交给 ``real_db.run()`` 执行。"""

    async def go():
        from app import db as dbmod

        async with dbmod.db_session() as s:
            return (await s.execute(select(func.count()).select_from(model))).scalar_one()

    return go()


# ★ 本文件是**唯一**有意读真库的地方。
# conftest 的 `_db_points_at_a_temp_db_before_every_test` 会先把 app.db 指到临时库；
# `real_db` fixture 显式依赖它，因此保证在其**之后**运行并把目标换成"真库 + 只读事务"。
# 这里只需要把这件事写清楚，不需要额外的 fixture —— 顺序由依赖保证。
def test_the_real_db_fixture_documented_its_ordering() -> None:
    """元测试：把"本文件为什么能读真库"钉在代码里。

    这条不是形式主义。真实踩过：real_db 一旦没有显式依赖那条 autouse fixture，
    pytest 会把临时库**最后**生效，于是六条用例一起读空库、报"真库里一个项目都没有"，
    而报错完全指不到是 fixture 顺序问题。
    """
    import inspect

    from tests.conftest import real_db

    params = inspect.signature(real_db.__wrapped__).parameters
    assert "_db_points_at_a_temp_db_before_every_test" in params, (
        "real_db 必须显式依赖 conftest 的逐测试临时库 fixture，"
        "否则它会被那条 autouse 覆盖掉，读到一个空临时库"
    )


def test_reads_the_real_database(real_db) -> None:
    """真库必须真的连上了，而且里面是用户的真数据。

    这条是整组用例的地基：它一旦成立，下面"写在事务里""回滚干净"才有意义。
    """
    projects = real_db.run(_count(Project))
    cases = real_db.run(_count(TestCase))

    assert projects > 0, "真库里一个项目都没有 —— 要么连错库，要么库是空的"
    assert cases > 0, "真库里一条用例都没有 —— 这不该发生在有真实项目的库上"
    assert cases >= projects, "用例总数不该少于项目数"


def test_runs_against_the_real_file_not_a_temp_copy(real_db) -> None:
    """★ 关键断言：连的必须是真库文件，不是某个临时库。

    没有这条，"真实数据库操作测试"就可能变成"对着临时库自说自话"，
    而所有断言仍然全绿 —— 这正是最该防的假绿。
    """
    from app import db as dbmod

    assert dbmod._engine is not None
    url = str(dbmod._engine.url)
    assert "potato.db" in url, f"连的不是真库：{url}"
    assert "_tp_" not in url, f"连到了临时库：{url}"


def test_can_read_a_real_project_and_its_cases(real_db) -> None:
    """按真实数据的关系读一遍：项目 → 它的用例。

    用真库里的**第一个**项目，而不是写死某个 id（id 会变，硬编码会让用例变脆）。
    """
    async def go():
        from app import db as dbmod

        async with dbmod.db_session() as s:
            proj = (await s.execute(select(Project).order_by(Project.id).limit(1))).scalar_one()
            cases = (
                await s.execute(
                    select(func.count())
                    .select_from(TestCase)
                    .where(TestCase.project_id == proj.id)
                )
            ).scalar_one()
            return proj.id, proj.name, cases

    pid, name, cases = real_db.run(go())
    assert pid > 0
    assert name, "项目名不该是空的"
    assert cases > 0, f"项目 {pid} 一条用例都没有 —— 真库的形状不该是这样"


def test_writes_inside_a_transaction_are_visible_then_rolled_back(real_db) -> None:
    """★ 本文件的核心：写进去看得见，跑完什么都没留下。

    这正是"安全地在真库上操作测试"的定义 —— 不是不能写，是写完不留痕。
    （`rollback_only` 让会话里的 commit 变成空操作：事务内可见，落盘无效。）
    """
    marker = "__real_db_mode_probe__"

    async def write_then_count():
        from app import db as dbmod

        async with dbmod.db_session() as s:
            before = (await s.execute(select(func.count()).select_from(Project))).scalar_one()
            s.add(Project(name=marker))
            await s.commit()  # 生产代码就是这么提交的；在 rollback_only 下它不会落盘
            inside = (await s.execute(select(func.count()).select_from(Project))).scalar_one()
            return before, inside

    before, inside = real_db.run(write_then_count())
    assert inside == before + 1, "事务内看不到自己刚写的行，说明 session 没绑在这条连接上"

    # 换一条**独立**连接去看：那条标记行必须不存在（rollback_only 已生效，
    # 真实 COMMIT 从未发生）。
    async def seen_from_another_connection():
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from tests.conftest import _real_db_url

        eng = create_async_engine(_real_db_url(), future=True, poolclass=NullPool)
        try:
            async with eng.connect() as c:
                return (
                    await c.execute(select(func.count()).select_from(Project).where(Project.name == marker))
                ).scalar_one()
        finally:
            await eng.dispose()

    leaked = real_db.run(seen_from_another_connection())
    assert leaked == 0, (
        "写进真库且真的落盘了！这说明 rollback_only 没生效 —— "
        "用户的真实数据已经被污染，必须立刻修 conftest 的 real_db fixture。"
    )


def test_write_rolls_back_even_without_an_explicit_rollback_call(real_db) -> None:
    """不显式 rollback 也不能落盘 —— 安全性不该依赖"测试作者记得收尾"。

    上面那条用例是"跑完再看"；这条是"故意什么都不做，直接结束"。
    两条一起才能说明：安全来自 fixture，而不是来自某个用例的自觉。
    """
    marker = "__real_db_mode_probe_no_cleanup__"

    async def just_write():
        from app import db as dbmod

        async with dbmod.db_session() as s:
            s.add(Project(name=marker))
            await s.commit()

    real_db.run(just_write())

    async def counted_from_fresh_engine():
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from tests.conftest import _real_db_url

        eng = create_async_engine(_real_db_url(), future=True, poolclass=NullPool)
        try:
            async with eng.connect() as c:
                return (
                    await c.execute(select(func.count()).select_from(Project).where(Project.name == marker))
                ).scalar_one()
        finally:
            await eng.dispose()

    assert real_db.run(counted_from_fresh_engine()) == 0


def test_real_db_supports_ordinary_queries_used_by_the_api(real_db) -> None:
    """真库上跑一遍界面会发的查询形状，确认真数据不会把它们弄崩。

    这比"造一条样本"更有价值：真数据里有 1200+ 用例、四种项目规模、
    各种 NULL / 空 JSON 列，边界都在真实数据里。
    """
    async def go():
        from app import db as dbmod

        async with dbmod.db_session() as s:
            rows = (
                await s.execute(
                    select(TestCase.project_id, func.count())
                    .group_by(TestCase.project_id)
                    .order_by(TestCase.project_id)
                )
            ).all()
            return [(int(pid), int(n)) for pid, n in rows]

    grouped = real_db.run(go())
    assert grouped, "按项目分组一条都没返回"
    assert all(n > 0 for _, n in grouped), f"出现了用例数为 0 的分组：{grouped}"
    assert sum(n for _, n in grouped) > 0


def test_the_real_db_is_not_left_as_the_default_engine() -> None:
    """真库用例跑完后，`app.db` 的默认引擎**绝不能**还指着真库。

    否则它会污染同进程里后面 900 多条用例 —— 那些用例以为自己在临时库上跑，
    实际写的是用户的真数据。这种故障的表现是"另一个文件里的用例突然开始写真库"，
    报错完全指不到这里。

    这条用例**故意不请求** real_db，所以看到的是别的测试留下的状态。

    ★ 断言为什么是"不是真库"而不是"必须是会话级临时库"：
      本仓库有若干测试自己用 tmp_path 建库并赋给 `dbmod._engine`，且不还原
      （实测：跑完整套之后引擎指向 `.../test_password_reset_loop0/tp.db`）。
      那是既有的测试卫生问题，不是 real_db fixture 的锅。
      这里要守的边界只有一条：**默认引擎不许是真库**。
    """
    from app import db as dbmod

    assert dbmod._engine is not None
    url = str(dbmod._engine.url)
    assert "potato.db" not in url, (
        f"默认引擎指着真库（{url}）—— real_db fixture 没还原干净，"
        f"后面的用例会直接写用户的真实数据"
    )


@pytest.mark.parametrize("_run", [1, 2])
def test_real_db_fixture_is_reusable_across_tests(real_db, _run: int) -> None:
    """同一个 fixture 连着用两次都要正常（后面那条会重新建连接与事务）。"""
    assert real_db.run(_count(Project)) > 0
