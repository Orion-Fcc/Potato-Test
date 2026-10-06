"""2026-10-04 剩余三项的测试：根因分类 / 结构化判定落库 / 跨用例经验检索。

重点守三类回归：
  1. 根因分类的白名单与一致性（分类不能被模型编造，且不能与重试标志冲突）
  2. 分类表只有一份来源（改动时不会漏同步中文标签）
  3. 跨用例检索的三条红线：同项目、不含 navigation、不用过期记忆
"""

import os
import sys

import pytest

sys.path.insert(0, "<PROJECT_DIR>")


# ------------------------------------------------------- 根因分类：表结构完整性


def test_every_root_cause_has_chinese_label():
    """新增分类时漏加中文标签，报告里就会出现空白分组。"""
    from app.judge import _ROOT_CAUSES, ROOT_CAUSE_LABELS_ZH

    assert set(_ROOT_CAUSES) == set(ROOT_CAUSE_LABELS_ZH), (
        "分类表与标签表不一致：%s" % (set(_ROOT_CAUSES) ^ set(ROOT_CAUSE_LABELS_ZH))
    )


def test_real_defect_set_is_conservative():
    """只有 product_defect 算真缺陷 —— 放宽它会让假失败污染缺陷率统计。"""
    from app.judge import ROOT_CAUSE_IS_REAL_DEFECT

    assert ROOT_CAUSE_IS_REAL_DEFECT == frozenset({"product_defect"})


def test_retryable_set_excludes_real_defects():
    """真缺陷绝不能被判成可重试 —— 那会白烧一轮时间重跑同一个 bug。"""
    from app.judge import ROOT_CAUSE_RETRYABLE, ROOT_CAUSE_IS_REAL_DEFECT

    assert not (ROOT_CAUSE_RETRYABLE & ROOT_CAUSE_IS_REAL_DEFECT)
    assert "product_defect" not in ROOT_CAUSE_RETRYABLE


def test_unclear_is_not_retryable():
    """unclear 不能自动重试：连原因都不知道，重试多半还是同样结果。"""
    from app.judge import ROOT_CAUSE_RETRYABLE

    assert "unclear" not in ROOT_CAUSE_RETRYABLE


def test_verdict_carries_root_cause():
    from app.judge import Verdict

    v = Verdict(status="failed", reason="r", root_cause="product_defect")
    assert v.root_cause == "product_defect"
    assert Verdict(status="passed", reason="r").root_cause == ""


# ------------------------------------------------------- 根因分类：判定器行为


class _FakeJudge:
    """把 judge() 里的解析逻辑复现出来，不调 LLM。

    直接测 `judge()` 需要 mock 网络；这里只验证「模型返回什么 → 落库什么」
    这段纯逻辑，把那几条一致性规则钉死。
    """

    @staticmethod
    def parse(data: dict, n_steps: int = 5):
        from app.judge import _ROOT_CAUSES

        status = "passed" if data.get("status") == "passed" else "failed"
        gap = status == "failed" and data.get("evidence_gap") is True
        raw = str(data.get("root_cause") or "").strip()
        cause = raw if raw in _ROOT_CAUSES else ""
        if status == "passed":
            cause = ""
        elif raw and not cause:
            cause = "unclear"
        elif not raw:
            cause = "unclear"
        if cause == "product_defect":
            gap = False
        elif cause in ("agent_incomplete", "evidence_insufficient", "precondition_missing",
                       "environment", "auth_or_permission") and status == "failed":
            gap = True
        return status, gap, cause


def test_passed_case_has_no_root_cause():
    """通过的用例不该带根因；模型硬填也要清掉，否则报告里会出现"通过的产品缺陷"。"""
    status, gap, cause = _FakeJudge.parse(
        {"status": "passed", "root_cause": "product_defect"}
    )
    assert status == "passed"
    assert cause == ""
    assert gap is False


def test_hallucinated_root_cause_falls_back_to_unclear():
    """模型编一个听着合理的分类（timeout/dns_error），必须归 unclear 而不是落库。"""
    for bogus in ("timeout", "network_error", "bug", "PRODUCT_DEFECT", "  "):
        _s, _g, cause = _FakeJudge.parse(
            {"status": "failed", "root_cause": bogus, "evidence_gap": False}
        )
        assert cause in ("unclear", ""), f"{bogus!r} 被当成合法分类了"


def test_product_defect_cancels_retryable():
    """分类与 evidence_gap 冲突时以分类为准 —— 否则真缺陷会被重跑一遍。

    这是实测里最容易出现的矛盾：模型一边说"确实是产品缺陷"，
    一边把 evidence_gap 标成 true（因为它看到 agent 没走完就下了结论）。
    """
    _s, gap, cause = _FakeJudge.parse(
        {"status": "failed", "root_cause": "product_defect", "evidence_gap": True}
    )
    assert cause == "product_defect"
    assert gap is False, "真缺陷被标成可重试，会白烧一轮"


def test_retryable_cause_enables_retry_even_if_model_says_false():
    """反过来同理：agent 没做完是可重试的，模型漏标 evidence_gap 也要纠正过来。"""
    _s, gap, cause = _FakeJudge.parse(
        {"status": "failed", "root_cause": "agent_incomplete", "evidence_gap": False}
    )
    assert cause == "agent_incomplete"
    assert gap is True


def test_missing_root_cause_defaults_to_unclear():
    _s, _g, cause = _FakeJudge.parse({"status": "failed", "reason": "x"})
    assert cause == "unclear", "没给分类时应落到 unclear，不能留空让报告无法分组"


# ------------------------------------------------------------ 结构化判定：落库


def test_run_result_has_classification_columns():
    from app.models import RunResult

    cols = RunResult.__table__.columns.keys()
    assert "root_cause" in cols
    assert "verdict_evidence" in cols


def test_additive_columns_registered():
    """新列必须登记进幂等迁移，否则老库启动会报 no such column。"""
    from app.db import _ADDITIVE_COLUMNS

    pairs = {(t, c) for t, c, _d in _ADDITIVE_COLUMNS}
    assert ("run_result", "root_cause") in pairs
    assert ("run_result", "verdict_evidence") in pairs


def test_result_payload_exposes_classification():
    """接口要给出分类标识、中文标签、以及"是否真缺陷" —— 前端不该自己维护分类表。"""
    import inspect

    from app.api import _result

    src = inspect.getsource(_result)
    assert "root_cause" in src
    assert "root_cause_label" in src
    assert "is_real_defect" in src


# ---------------------------------------------------- 跨用例检索：打分与红线


def test_bigram_scoring_ranks_relevant_above_irrelevant():
    """要断言的是**排序**，不是某个绝对阈值。

    一开始我在这里写死了 `> 0.5`，实测得 0.4545 就红了 —— 但 0.45 对
    「共享『资源审批配置』6 个字、开头还多个『进入』」的候选来说是完全合理的分数。
    绝对阈值取决于查询长度（二元组个数），写死它只会让测试随用例文案变化而假红。
    检索真正依赖的性质是：相关的必须排在无关的前面，且差距要够大才不会被
    min_score 一并滤掉。
    """
    from app.case_memory import score_relevance

    q = "进入资源审批配置新建规则"
    strong = score_relevance(q, "资源审批配置页面加载较慢")   # 共享完整菜单名（6 字）
    weak = score_relevance(q, "配置项说明按钮位置")           # 只共享「配置」一个二元组
    none = score_relevance(q, "首页标题显示正确")             # 完全不沾边

    assert strong > weak > none, f"排序不对: strong={strong} weak={weak} none={none}"
    assert none == 0.0
    # 这两条断言钉住 min_score 的设计意图：强相关必须过得去，
    # 只共享一个词的弱相关必须被滤掉 —— 否则提示词里会塞满噪音。
    assert strong >= 0.12, "强相关的分数低于 min_score，真正的经验会被误滤"
    assert weak < 0.12, "弱相关能过 min_score，说明门槛形同虚设"


def test_bigram_scoring_handles_short_text():
    """长度 1 的文本不能返回空集合，否则永远匹配不上。"""
    from app.case_memory import score_relevance, _bigrams

    assert _bigrams("查") == {"查"}
    assert _bigrams("") == set()
    assert score_relevance("查", "查") == 1.0


def test_bigram_scoring_ignores_punctuation_and_space():
    from app.case_memory import score_relevance

    assert score_relevance("资源审批", "资源，审批。") == 1.0


@pytest.fixture()
def mem_project():
    """临时库：直接构造 engine，绕开环境变量（见 test_isolation_and_policy.py 的说明）。"""
    import asyncio
    import tempfile

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    tmp = os.path.join(tempfile.gettempdir(), "_tp_rag_test.db")
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
    assert "_tp_rag_test.db" in str(eng.url), f"没连到临时库：{eng.url}"

    from app.case_memory import fingerprint

    async def add_case(project_id, name, prompt, memory=None, fp_override="__auto__"):
        async with dbmod.db_session() as s:
            c = TestCase(project_id=project_id, name=name, prompt=prompt, case_key=name)
            c.memory = memory
            if memory is not None:
                # 指纹默认取"当前内容"，这样记忆算新鲜；需要测过期时传别的值
                c.memory_fingerprint = fingerprint(c) if fp_override == "__auto__" else fp_override
            s.add(c)
            await s.flush()
            return c.id

    async def add_project(name="RAG"):
        async with dbmod.db_session() as s:
            p = Project(name=name)
            s.add(p)
            await s.flush()
            return p.id

    yield add_project, add_case, dbmod.db_session

    asyncio.run(eng.dispose())
    dbmod._engine = None
    dbmod._Session = None
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def test_retrieval_returns_relevant_note(mem_project):
    import asyncio

    add_project, add_case, _ = mem_project

    async def run():
        pid = await add_project()
        await add_case(pid, "TC-1", "进入资源审批配置", {
            "page_notes": ["资源审批配置列表加载约 8 秒，期间只显示正在加载中"],
            "element_notes": [],
            "navigation": [],
        })
        await add_case(pid, "TC-2", "检查首页标题")  # 查询目标
        from app.case_memory import related_notes

        return await related_notes(pid, "资源审批配置 新建规则", exclude_case_id=None)

    out = asyncio.run(run())
    assert "资源审批配置" in out
    assert "加载" in out


def test_retrieval_only_shares_page_and_element_notes(mem_project):
    """navigation 是这条用例的专属路径，共享给别的用例会把 agent 带偏。"""
    import asyncio

    add_project, add_case, _ = mem_project

    async def run():
        pid = await add_project()
        await add_case(pid, "TC-1", "资源审批配置", {
            "page_notes": ["资源审批配置页面加载慢"],
            "element_notes": [],
            "navigation": ["独有导航路径标识ZZZ"],
        })
        from app.case_memory import related_notes

        return await related_notes(pid, "资源审批配置 新建", exclude_case_id=None)

    out = asyncio.run(run())
    assert "加载慢" in out
    assert "ZZZ" not in out, "navigation 被跨用例共享了"


def test_retrieval_never_crosses_projects(mem_project):
    """跨项目检索会让用例重新耦合，与项目隔离要求冲突。"""
    import asyncio

    add_project, add_case, _ = mem_project

    async def run():
        p1 = await add_project("A")
        p2 = await add_project("B")
        await add_case(p1, "TC-1", "资源审批配置", {
            "page_notes": ["来自项目A的独有经验AAA"],
            "element_notes": [],
            "navigation": [],
        })
        from app.case_memory import related_notes

        # 在项目 B 里检索，不该看到项目 A 的经验
        return await related_notes(p2, "资源审批配置", exclude_case_id=None)

    out = asyncio.run(run())
    assert "AAA" not in out, "跨项目检索到了别的项目的经验"


def test_retrieval_skips_stale_memory(mem_project):
    """指纹对不上 = 用例改过版，旧经验不可信，必须跳过。"""
    import asyncio

    add_project, add_case, _ = mem_project

    async def run():
        pid = await add_project()
        await add_case(
            pid, "TC-1", "资源审批配置",
            {"page_notes": ["过期经验EEE"], "element_notes": [], "navigation": []},
            fp_override="stale-fingerprint",
        )
        from app.case_memory import related_notes

        return await related_notes(pid, "资源审批配置", exclude_case_id=None)

    out = asyncio.run(run())
    assert "EEE" not in out, "用了指纹过期的记忆"


def test_retrieval_excludes_own_case(mem_project):
    """自己的记忆由 note_for_case 单独注入，检索里不该重复出现。"""
    import asyncio

    add_project, add_case, _ = mem_project

    async def run():
        pid = await add_project()
        cid = await add_case(pid, "TC-1", "资源审批配置", {
            "page_notes": ["自己的记忆SSS"],
            "element_notes": [],
            "navigation": [],
        })
        from app.case_memory import related_notes

        return await related_notes(pid, "资源审批配置", exclude_case_id=cid)

    out = asyncio.run(run())
    assert "SSS" not in out, "自己的记忆在跨用例检索里重复注入了"


def test_retrieval_filters_verdict_like_text(mem_project):
    """记忆系统的红线是绝不背答案：历史记忆里若混进判定性措辞，检索时再挡一次。"""
    import asyncio

    add_project, add_case, _ = mem_project

    async def run():
        pid = await add_project()
        await add_case(pid, "TC-1", "资源审批配置", {
            "page_notes": ["该用例通过，资源审批配置正确"],
            "element_notes": [],
            "navigation": [],
        })
        from app.case_memory import related_notes

        return await related_notes(pid, "资源审批配置", exclude_case_id=None)

    out = asyncio.run(run())
    assert "通过" not in out, "判定性措辞漏进了跨用例检索"


def test_retrieval_is_safe_on_empty_project(mem_project):
    """项目里没有任何记忆时要返回空串，不能抛异常打断用例。"""
    import asyncio

    add_project, _add_case, _ = mem_project

    async def run():
        pid = await add_project()
        from app.case_memory import related_notes

        return await related_notes(pid, "随便什么查询", exclude_case_id=None)

    assert asyncio.run(run()) == ""


def test_retrieval_handles_zero_project_id():
    """project_id 为 0（未设置）时直接返回空串，不能去查全库。"""
    import asyncio

    from app.case_memory import related_notes

    assert asyncio.run(related_notes(0, "任何查询")) == ""


def test_executor_wires_related_notes_into_prompt():
    """检索结果必须真的拼进 system message，否则等于没做。"""
    import inspect

    from app.executor import execute_case

    src = inspect.getsource(execute_case)
    assert "related_notes" in src, "跨用例检索没接到执行流程里"
    assert "_memory_note" in src
