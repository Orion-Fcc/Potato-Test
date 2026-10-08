"""2026-10-04 数据隔离 / 前置条件 / 速度策略 的测试。

覆盖四组：
  1. 会话捕获按项目隔离（用户报的"别的项目能用另一个项目的缓存和账号"）
  2. 用例编号：项目内不重号、跨项目允许重名、唯一约束存在
  3. 前置条件：依赖解析（拓扑序 + 环保护 + 只在项目内解析）
  4. 速度策略：并发锁 1、步数/超时是兵底、提示词里有效率规则和质量红线
"""

import os
import sys

import pytest

sys.path.insert(0, "<PROJECT_DIR>")


# ---------------------------------------------------------------- 隔离：捕获 profile


def test_capture_profile_is_per_project():
    """不同项目必须落在不同的捕获目录 —— 这是串缓存的根因。"""
    from app.executor import _capture_profile_dir

    p1 = _capture_profile_dir(1)
    p2 = _capture_profile_dir(2)
    assert p1 != p2, "两个项目拿到了同一个捕获 profile，会串 cookies"
    assert "project_1" in p1.replace("\\", "/")
    assert "project_2" in p2.replace("\\", "/")


def test_capture_profile_never_returns_empty():
    """project_id 缺失时也必须给一个目录，绝不能返回 None。

    返回 None 会让 Browser 退回 browser-use 的**全局默认** profile ——
    那里面混着所有项目真实登录态，是最糟的结果。宁可给个 orphan 目录。
    """
    from app.executor import _capture_profile_dir

    for pid in (None, 0):
        p = _capture_profile_dir(pid)
        assert p and isinstance(p, str)
        assert "_capture" in p.replace("\\", "/")


def test_capture_session_always_passes_user_data_dir():
    """源码级断言：capture_session 必须把 user_data_dir 传给 Browser。

    这条是防回归的核心 —— 只要有人把 user_data_dir 删掉，
    浏览器就退回全局默认 profile，用户报的问题会原样复发。
    """
    import inspect

    from app.executor import capture_session

    src = inspect.getsource(capture_session)
    assert "user_data_dir=" in src, "capture_session 没传 user_data_dir，会退回全局默认 profile"
    assert "_capture_profile_dir" in src, "capture_session 没用项目隔离的目录"


def test_capture_session_has_project_id_param():
    """调用方必须能传项目；没有这个参数就没法隔离。"""
    import inspect

    from app.executor import capture_session

    params = inspect.signature(capture_session).parameters
    assert "project_id" in params


# ---------------------------------------------------------------- 隔离：用例编号


def test_next_case_key_ignores_free_text_keys():
    """导入的 Excel 可能有自由文本编号（REQ-12），不能让它把序号计算搞崩。

    int('REQ-12') 会抛 ValueError，整批导入会因此中断。
    """
    import re as _re

    # 直接验证正则本身：只认 TC-<数字>
    pat = _re.compile(r"TC-(\d+)")
    assert pat.fullmatch("TC-001")
    assert pat.fullmatch("REQ-12") is None
    assert pat.fullmatch("TC-abc") is None


def test_case_key_unique_constraint_declared():
    """必须有 (project_id, case_key) 复合唯一约束。

    单列唯一会让项目 2 建不了自己的 TC-001；完全没约束则删过用例后
    会发出重号（旧 COUNT 实现的缺陷）。
    """
    from app.models import TestCase

    names = [c.name for c in TestCase.__table__.constraints if c.name]
    assert "uq_testcase_project_casekey" in names, (
        "缺少 (project_id, case_key) 唯一约束，TC-001 可能在同一项目内重复"
    )
    uq = next(c for c in TestCase.__table__.constraints if c.name == "uq_testcase_project_casekey")
    cols = sorted(col.name for col in uq.columns)
    assert cols == ["case_key", "project_id"], f"约束列不对：{cols}"


def test_case_key_is_per_project_not_global():
    """跨项目重名是**允许**的（每个项目从 TC-001 开始），不能改成全局唯一。"""
    from app.models import TestCase

    uq = next(
        c for c in TestCase.__table__.constraints
        if c.name == "uq_testcase_project_casekey"
    )
    assert len(uq.columns) == 2, "改成单列唯一会让其它项目建不了同编号用例"


# ---------------------------------------------------------------- 前置条件：依赖解析


@pytest.fixture()
def dep_project():
    """临时库 + 一个项目，用于测依赖解析。

    ★ 为什么不用「设 DATABASE_URL 环境变量」这套：
    本仓库其它测试普遍这么写，但它在两处会静默失效：
      1) app/db.py 的 _ensure() 把 engine/sessionmaker 缓存在模块级全局变量里。
         本次 pytest 进程里只要更早的测试触发过 _ensure()，engine 就绑死在
         真实 potato.db 上，之后再改环境变量毫无作用。
      2) pydantic-settings 读 .env 与环境变量的优先级在这个环境里并不可靠
         （实测：单独跑 `python -c` 时环境变量生效，在 pytest 里同一个赋值
         却被 .env 里的值盖掉，解析出来仍是真实库路径）。
    结果就是测试造的数据全部写进用户的真库 —— 本文件第一次跑就往真实库里
    塞了 12 个 DEP 项目。

    所以这里**直接构造 engine/`sessionmaker`**，完全绕开环境变量与 .env 解析，
    并在构造后断言连的是临时库。确定性优先于"和别的测试写法一致"。
    """
    import asyncio
    import tempfile

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    tmp = os.path.join(tempfile.gettempdir(), "_tp_dep_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)

    from app import db as dbmod
    from app.models import Base, Project, TestCase

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    # 自证隔离有效 —— 绝不静默写真实数据
    assert "_tp_dep_test.db" in str(eng.url), f"没连到临时库：{eng.url}"

    db_session = dbmod.db_session

    async def setup(cases):
        """cases: [(case_key, preconditions)]"""
        async with db_session() as s:
            p = Project(name="DEP")
            s.add(p)
            await s.flush()
            pid = p.id
            out = []
            for key, pre in cases:
                c = TestCase(project_id=pid, case_key=key, name=key, prompt="x", preconditions=pre or "")
                s.add(c)
                await s.flush()
                out.append(c.id)
            return pid, out

    yield setup, db_session, dbmod

    asyncio.run(eng.dispose())
    dbmod._engine = None
    dbmod._Session = None
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def test_dependency_pulls_referenced_case_first(dep_project):
    """TC-002 的前置条件引用 TC-001 → 解析结果里 TC-001 必须排在前面。"""
    import asyncio

    setup, db_session, _ = dep_project
    pid, ids = asyncio.run(setup([
        ("TC-001", ""),
        ("TC-002", "已存在 TC-001 创建的申请单"),
    ]))
    from app.engine import resolve_case_dependencies

    async def run():
        async with db_session() as s:
            return await resolve_case_dependencies(s, pid, [ids[1]])

    got = asyncio.run(run())
    assert got == [ids[0], ids[1]], f"依赖没被前置：{got}"


def test_dependency_closure_is_transitive(dep_project):
    """TC-003 -> TC-002 -> TC-001：三个都要跑，且顺序是 1,2,3。"""
    import asyncio

    setup, db_session, _ = dep_project
    pid, ids = asyncio.run(setup([
        ("TC-001", ""),
        ("TC-002", "需要 TC-001"),
        ("TC-003", "需要 TC-002"),
    ]))
    from app.engine import resolve_case_dependencies

    async def run():
        async with db_session() as s:
            return await resolve_case_dependencies(s, pid, [ids[2]])

    got = asyncio.run(run())
    assert got == ids, f"传递依赖没展开或顺序错：{got}"


def test_dependency_ignores_other_projects_keys(dep_project):
    """只解析本项目编号 —— 跨项目指依赖会让项目重新耦合，这正是要拆开的。"""
    import asyncio

    setup, db_session, dbmod = dep_project
    pid, ids = asyncio.run(setup([
        ("TC-001", ""),
        ("TC-002", "需要 TC-001"),
    ]))

    # 另建一个项目，它也有 TC-001
    from app.db import db_session as dbs
    from app.models import Project

    async def other():
        async with dbs() as s:
            p = Project(name="OTHER")
            s.add(p)
            await s.flush()
            return p.id

    other_pid = asyncio.run(other())
    assert other_pid != pid

    from app.engine import resolve_case_dependencies

    async def run():
        async with db_session() as s:
            return await resolve_case_dependencies(s, pid, [ids[1]])

    got = asyncio.run(run())
    # 命中的必须是本项目的 TC-001（ids[0]），不能是另一个项目的同编号用例
    assert got == [ids[0], ids[1]]


def test_dependency_cycle_does_not_hang(dep_project):
    """A 依赖 B、B 依赖 A：必须能返回，不能死循环。"""
    import asyncio

    setup, db_session, _ = dep_project
    pid, ids = asyncio.run(setup([
        ("TC-001", "需要 TC-002"),
        ("TC-002", "需要 TC-001"),
    ]))
    from app.engine import resolve_case_dependencies

    async def run():
        async with db_session() as s:
            return await resolve_case_dependencies(s, pid, [ids[0]])

    got = asyncio.run(run())
    assert set(got) == set(ids), f"环里的用例没跑全：{got}"
    assert len(got) == len(set(got)), "结果里有重复用例"


def test_dependency_self_reference_is_ignored(dep_project):
    """用例在自己的前置条件里提到自己 → 不能把自己当依赖（会导致重复跑）。"""
    import asyncio

    setup, db_session, _ = dep_project
    pid, ids = asyncio.run(setup([("TC-001", "本用例 TC-001 需要先登录")]))
    from app.engine import resolve_case_dependencies

    async def run():
        async with db_session() as s:
            return await resolve_case_dependencies(s, pid, [ids[0]])

    got = asyncio.run(run())
    assert got == [ids[0]], f"自引用产生了多余条目：{got}"


def test_dependency_keeps_user_order_for_independent_cases(dep_project):
    """没有依赖关系的用例，用户选的顺序必须原样保留。"""
    import asyncio

    setup, db_session, _ = dep_project
    pid, ids = asyncio.run(setup([("TC-001", ""), ("TC-002", ""), ("TC-003", "")]))
    from app.engine import resolve_case_dependencies

    async def run():
        async with db_session() as s:
            # 故意反序选
            return await resolve_case_dependencies(s, pid, [ids[2], ids[0], ids[1]])

    got = asyncio.run(run())
    assert got == [ids[2], ids[0], ids[1]], f"无依赖时顺序被改：{got}"


# ---------------------------------------------------------------- 前置条件：提示词


def test_prompt_tells_agent_to_create_missing_preconditions():
    from app.engine import _effective_prompt

    class C:
        preconditions = "已存在一条待审核的申请单"
        prompt = "审批该申请单"
        test_data = ""

    out = _effective_prompt(C())
    assert "CREATE IT YOURSELF" in out, "没授权 agent 自己补建前置数据"
    assert "CONFIRM it exists" in out, "没要求验证补建结果，会产生假通过"


def test_prompt_forbids_pretending_precondition_met():
    """必须明确禁止"假装前置已满足" —— 否则假失败会变成更糟的假通过。"""
    from app.engine import _effective_prompt

    class C:
        preconditions = "已有一条申请单"
        prompt = "审批"
        test_data = ""

    out = _effective_prompt(C())
    assert "blocked/unverified" in out
    assert "Do NOT" in out and "pretend" in out


def test_prompt_without_preconditions_is_unchanged():
    """没有前置条件的用例，提示词里不该出现这段授权（避免无谓的上下文占用）。"""
    from app.engine import _effective_prompt

    class C:
        preconditions = ""
        prompt = "打开首页检查标题"
        test_data = ""

    out = _effective_prompt(C())
    assert "CREATE IT YOURSELF" not in out
    assert out == "打开首页检查标题"


# ---------------------------------------------------------------- 速度策略


def test_concurrency_is_pinned_to_one():
    """内网只有一条通道：并发必须是 1，且是硬上限。"""
    from app.config import get_settings

    s = get_settings()
    assert s.run_concurrency == 1
    assert s.default_concurrency == 1
    assert s.max_global_concurrency == 1, "全局并发上限必须也是 1，否则能绕过"


def test_step_and_time_are_generous_safety_nets():
    """去掉 160/40 之后应改成宽裕兵底，而不是更小的预算。

    这两个值的作用是"拦住真正卡死的用例"，不是"限制正常用例"，
    所以必须比原来大 —— 变小就等于把长流程用例重新砍断。
    """
    from app.config import get_settings

    s = get_settings()
    assert s.case_max_steps >= 80, f"步数兵底太小（{s.case_max_steps}），长流程用例会被砍断"
    assert s.case_timeout_s >= 600, f"超时兵底太小（{s.case_timeout_s}），长流程用例会被砍断"


def test_efficiency_rule_is_attached_to_the_agent():
    """效率规则必须真的挂在 agent 的 system message 上，否则等于没写。"""
    import inspect

    from app.executor import execute_case

    src = inspect.getsource(execute_case)
    assert "_EFFICIENCY_RULE" in src, "效率规则没被插进 system message"


def test_efficiency_rule_demands_speed():
    from app.executor import _EFFICIENCY_RULE

    t = _EFFICIENCY_RULE.lower()
    assert "smallest step count" in t or "minimum" in t
    assert "shortest" in t


def test_efficiency_rule_protects_quality():
    """用户原话：'不能为了快，瞎整'。必须用硬措辞禁止为省步数而放弃证据。"""
    from app.executor import _EFFICIENCY_RULE

    t = _EFFICIENCY_RULE
    assert "NEVER trade evidence for steps" in t
    assert "NEVER report an outcome you did not see" in t
    assert "fabricated" in t.lower() or "invented" in t.lower()


def test_efficiency_rule_forbids_fake_pass_explicitly():
    """必须点名"假通过比诚实失败更糟" —— 这是最容易出现的走捷径方式。"""
    from app.executor import _EFFICIENCY_RULE

    t = _EFFICIENCY_RULE
    assert "fabricated PASS" in t or "fabricated pass" in t
    assert "unverified" in t


# ------------------------------------------------- 自检节点（Evaluator 最省形式）


def test_self_check_rule_is_attached_to_the_agent():
    """自检规则必须真的挂上，否则就是一段没人读的注释。"""
    import inspect

    from app.executor import execute_case

    src = inspect.getsource(execute_case)
    assert "_SELF_CHECK_RULE" in src, "自检规则没被插进 system message"


def test_self_check_requires_pointing_at_evidence():
    """自检的核心是"指得出证据" —— 指不出就不算通过。"""
    from app.executor import _SELF_CHECK_RULE

    t = _SELF_CHECK_RULE
    assert "point at the exact thing on the page" in t
    assert "cannot name what you saw" in t


def test_self_check_catches_skipped_steps():
    """漏跑步骤是"看着通过其实没测"的主要来源，必须明确检查。"""
    from app.executor import _SELF_CHECK_RULE

    assert "SKIPPED A STEP" in _SELF_CHECK_RULE


def test_self_check_rejects_silence_as_success():
    """系统不报错 ≠ 行为正确。这条最容易被误判成通过。"""
    from app.executor import _SELF_CHECK_RULE

    t = _SELF_CHECK_RULE
    assert "NO ERROR SHOWN" in t
    assert "silent page is not evidence" in t


# ------------------------------------------------- 包容性探索（2026-10-06）


def test_adapt_rule_is_attached_to_the_agent():
    """同样的坑：写了不挂 = 没人读。"""
    import inspect

    from app.executor import execute_case

    src = inspect.getsource(execute_case)
    assert "_ADAPT_RULE" in src, "包容性规则没被插进 system message"


def test_adapt_rule_is_shown_after_the_efficiency_rules():
    """顺序必须是"先省步数、再给例外"。

    倒过来会被读成"可以随便逛菜单"，把提速的成果推翻。
    """
    from app.executor import execute_case
    import inspect

    src = inspect.getsource(execute_case)
    # 只在拼装那一段里比顺序：源码注释里也会提到这些名字，全文件搜会误判。
    chunk = src[src.index("extend_system_message") :]
    chunk = chunk[: chunk.index("{_memory_note}")]
    assert chunk.index("_EFFICIENCY_RULE") < chunk.index(
        "_ADAPT_RULE"
    ), "包容性规则必须排在效率规则之后（作为它的例外）"


def test_adapt_rule_matches_by_meaning_not_by_characters():
    """用例文案 ≠ 页面文案。逐字匹配会造出"元素不存在"的假失败。"""
    from app.executor import _ADAPT_RULE

    t = _ADAPT_RULE
    assert "新增" in t and "新建" in t, "必须给出中文近义词的具体例子，光说'同义词'模型不会照做"
    assert "FALSE" in t and "FAILURE" in t, "必须点名这是假失败"


def test_adapt_rule_bounds_the_exploration():
    """探索必须封顶，否则就是拿"包容性"给逛菜单开后门。"""
    from app.executor import _ADAPT_RULE

    t = _ADAPT_RULE
    assert "2-3 cheap steps" in t
    assert "two attempts" in t
    assert "Do NOT walk the whole menu tree" in t


def test_adapt_rule_keeps_the_evidence_bar():
    """最容易越界的地方：把"包容"理解成"差不多就算通过"。

    这条是整个规则的命门 —— 找不到按钮可以换个说法再试，
    但预期结果仍然必须**看得见地**成立。测试必须钉住这个边界。
    """
    from app.executor import _ADAPT_RULE

    t = _ADAPT_RULE
    assert "WHERE ADAPTATION STOPS" in t
    assert "NEVER about what counts as" in t or "NEVER about what counts as proof" in t
    assert "looks like" in t, "必须点名'看着像'不算证据"


def test_adapt_rule_asks_for_what_is_there_when_giving_up():
    """放弃时要报"页面上实际有什么"，才能区分真缺陷和执行问题。"""
    from app.executor import _ADAPT_RULE

    t = _ADAPT_RULE
    assert "WHAT IS THERE" in t
    assert "real defect" in t and "execution gap" in t


def test_efficiency_and_thrash_rules_point_at_the_exception():
    """两条"省步数"规则必须自己提到例外，否则三者互相矛盾、模型会随机挑一条执行。"""
    from app.executor import _ANTI_WASTE_RULE, _EFFICIENCY_RULE

    assert "adaptation rule" in _EFFICIENCY_RULE
    assert "adaptation rule" in _ANTI_WASTE_RULE


def test_self_check_costs_no_extra_model_call():
    """自检必须是提示词级的强制自问，不能另起一次 LLM 调用。

    另起调用会让每个用例多一次模型往返（实测 26-67 秒），
    与用户"用最快时间完成"的要求直接冲突。
    """
    from app.executor import _SELF_CHECK_RULE

    assert "costs no extra" in _SELF_CHECK_RULE and "steps" in _SELF_CHECK_RULE
