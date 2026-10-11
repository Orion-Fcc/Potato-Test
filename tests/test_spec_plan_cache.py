"""模块规划缓存（2026-10-09）。

## 为什么这是一个**缺陷修复**，不是优化

实测（真网关、真文档、同一批 60 个标题）连续调四次，模型给出的分批：

    第1次: modules= 2  testable= 0
    第2次: modules=16  testable=14
    第3次: modules=18  testable=16
    第4次: modules=13  testable=12

三个具体后果，每一个都足以让功能不可用：

1. **用户看不懂。** 刚看到"16 个可测模块"，刷新一下变成"2 个、0 个可测"。
2. **白烧钱。** 每次刷新重跑一次规划调用 —— 用户明确要求降消耗。
3. **接不上。** 界面列的是第 N 次的分批，点"生成"时后端按第 N+1 次的分组找正文，
   模块名对不上就报"找不到正文"，而文档明明在那儿。

缓存把"同一次分批"固定下来；重算必须是用户主动点「重新分批」的结果。
"""

from __future__ import annotations

import pytest

from app import specgen
from app.specgen import PlanResult


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后都清缓存 —— 模块级 dict 会跨用例串。"""
    specgen.clear_plan_cache()
    yield
    specgen.clear_plan_cache()


def _fake_plan(monkeypatch, modules: list[dict], calls: list):
    """把真正的模型调用换掉，并记录被调了几次。"""

    async def fake(headings, doc_intro=""):
        calls.append(list(headings))
        return PlanResult(modules=modules, usage={"total": 2901, "calls": 1})

    monkeypatch.setattr(specgen, "plan_modules", fake)


MODS = [{"name": "甲", "titles": ["A"], "testable": True, "reason": ""}]


def test_repeated_calls_reuse_one_plan(monkeypatch) -> None:
    """★ 同一批连续调 → 只算一次。这是"刷新不会换一批模块"的机制。"""
    calls: list = []
    _fake_plan(monkeypatch, MODS, calls)

    import asyncio

    async def go():
        r1 = await specgen.plan_modules_cached(1, ["A", "B"], 0, 2)
        r2 = await specgen.plan_modules_cached(1, ["A", "B"], 0, 2)
        r3 = await specgen.plan_modules_cached(1, ["A", "B"], 0, 2)
        return r1, r2, r3

    r1, r2, r3 = asyncio.run(go())
    assert len(calls) == 1, f"模型被调了 {len(calls)} 次，缓存没生效"
    assert r1.cached is False and r1.usage, "第一次应当是真实调用（有 usage）"
    assert r2.cached is True and r3.cached is True, "后续应当是缓存命中"
    assert r2.usage == {}, "缓存命中不该记 token —— 否则报告会虚报消耗"
    # 内容必须完全一致，否则等于没缓存
    assert r2.modules == r1.modules == r3.modules


def test_different_batches_do_not_share_a_cache_entry(monkeypatch) -> None:
    """批边界进 key：换一批（start 不同）必须重新算，不能串。"""
    calls: list = []
    _fake_plan(monkeypatch, MODS, calls)

    import asyncio

    async def go():
        await specgen.plan_modules_cached(1, [f"h{i}" for i in range(120)], 0, 60)
        await specgen.plan_modules_cached(1, [f"h{i}" for i in range(120)], 60, 120)
        return calls

    got = asyncio.run(go())
    assert len(got) == 2, "两批不同起点却只算了一次 —— key 里漏了批边界"


def test_different_projects_do_not_share_a_cache_entry(monkeypatch) -> None:
    """项目隔离：A 项目的分批结果不能被 B 项目拿到。"""
    calls: list = []
    _fake_plan(monkeypatch, MODS, calls)

    import asyncio

    async def go():
        await specgen.plan_modules_cached(1, ["A"], 0, 1)
        await specgen.plan_modules_cached(2, ["A"], 0, 1)
        return calls

    assert len(asyncio.run(go())) == 2


def test_clear_cache_forces_a_recompute(monkeypatch) -> None:
    """这是界面「重新分批」按钮的机制 —— 只有用户主动点才会走到这里。"""
    calls: list = []
    _fake_plan(monkeypatch, MODS, calls)

    import asyncio

    async def go():
        await specgen.plan_modules_cached(1, ["A"], 0, 1)
        specgen.clear_plan_cache(1)
        r = await specgen.plan_modules_cached(1, ["A"], 0, 1)
        return r

    r = asyncio.run(go())
    assert len(calls) == 2, "清缓存后没有重算"
    assert r.cached is False


def test_clear_cache_only_affects_the_named_project(monkeypatch) -> None:
    calls: list = []
    _fake_plan(monkeypatch, MODS, calls)

    import asyncio

    async def go():
        await specgen.plan_modules_cached(1, ["A"], 0, 1)
        await specgen.plan_modules_cached(2, ["A"], 0, 1)
        specgen.clear_plan_cache(1)
        await specgen.plan_modules_cached(2, ["A"], 0, 1)  # 应当仍命中
        return calls

    assert len(asyncio.run(go())) == 2, "清项目 1 的缓存把项目 2 的也清了"


def test_a_failed_plan_is_not_cached(monkeypatch) -> None:
    """★ 失败**不能**被缓存半小时。

    否则用户修好网关（或补上配额）之后再点，看到的还是同一个错误，
    只能重启服务 —— 而错误信息里没有一句提示"这是缓存的旧结果"。
    """
    calls: list = []

    async def failing(headings, doc_intro=""):
        calls.append(list(headings))
        return PlanResult(error="AuthenticationError: 401")

    monkeypatch.setattr(specgen, "plan_modules", failing)

    import asyncio

    async def go():
        r1 = await specgen.plan_modules_cached(1, ["A"], 0, 1)
        r2 = await specgen.plan_modules_cached(1, ["A"], 0, 1)
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert len(calls) == 2, "失败结果被缓存了 —— 用户修好网关也看不到新结果"
    assert r1.error == r2.error


def test_a_degraded_plan_with_modules_is_cached(monkeypatch) -> None:
    """降级（逐标题兜底）但**有模块**时可以缓存 —— 它是可用结果，只是粒度碎。"""
    calls: list = []

    async def degraded(headings, doc_intro=""):
        calls.append(list(headings))
        return PlanResult(modules=MODS, degraded=True)

    monkeypatch.setattr(specgen, "plan_modules", degraded)

    import asyncio

    async def go():
        await specgen.plan_modules_cached(1, ["A"], 0, 1)
        r = await specgen.plan_modules_cached(1, ["A"], 0, 1)
        return r

    r = asyncio.run(go())
    assert len(calls) == 1, "降级但可用的结果应当缓存"
    assert r.degraded is True and r.cached is True


def test_ttl_is_bounded(monkeypatch) -> None:
    """TTL 要有限：永久缓存会让"文档换了但分批还是旧的"变成一个查不出来的问题。"""
    assert 60 <= specgen._PLAN_TTL_S <= 24 * 3600


# ---------------------------------------------------------------------------
# 分批推进：失败必须停下，不能假装还在前进（2026-10-09 实测）
# ---------------------------------------------------------------------------


def test_a_failed_batch_does_not_report_has_more() -> None:
    """★ 实测缺陷：一份 586 个标题的文档，第 3 批起网关持续 500。

    而接口仍然回 `has_more=true` —— 界面于是给用户一个「规划下一批」按钮，
    点一次失败一次，而且**永远推不动**。用户能做的只有反复点同一个键。

    失败时的正确表达是"这批没成功，可以重试"，而不是"后面还有很多"。
    """
    from app.api import spec_outline_progress

    failed = spec_outline_progress(total=586, lo=120, hi=180, failed=True)
    assert failed["has_more"] is False, "失败的批次不该说'后面还有'"
    assert failed["failed"] is True
    assert failed["planned_until"] == 120, "失败没有推进，进度就不该往前走"

    ok = spec_outline_progress(total=586, lo=120, hi=180, failed=False)
    assert ok["has_more"] is True
    assert ok["planned_until"] == 180
    assert ok["failed"] is False

    # 最后一批成功时也没有"更多"
    last = spec_outline_progress(total=586, lo=540, hi=586, failed=False)
    assert last["has_more"] is False
    assert last["planned_until"] == 586


# ---------------------------------------------------------------------------
# 窗口契约：传什么就规划什么（踩过一次的坑）
# ---------------------------------------------------------------------------


def test_plan_modules_cached_plans_exactly_the_window_it_is_given(monkeypatch) -> None:
    """★ `plan_modules_cached` 必须规划**传进来的那个窗口**，不能再按 lo/hi 切一次。

    踩过的坑：调用方先 `rank_window()` 排好序再把结果传进来，而函数内部又
    `headings[lo:hi]` 切了一遍 —— 于是窗口内重排被切坏，
    排序看起来"没生效"（实测第一批仍然全是元信息）。

    正确契约是：`window` 是已经切好、已经排好序的这一批；
    `lo`/`hi` **只用来做缓存键**。
    """
    seen: list = []

    async def fake(headings, doc_intro=""):
        seen.append(list(headings))
        return PlanResult(modules=MODS, usage={"total": 1, "calls": 1})

    monkeypatch.setattr(specgen, "plan_modules", fake)

    import asyncio

    # 传一个"只有 3 个元素"的窗口，而 lo/hi 指向原文的 60~120。
    # 如果内部又切一次，这 3 个元素会变成空列表。
    window = ["甲", "乙", "丙"]
    asyncio.run(specgen.plan_modules_cached(1, window, 60, 120))
    assert seen == [["甲", "乙", "丙"]], f"窗口被二次切片了：{seen}"


def test_rank_window_only_reorders_within_bounds() -> None:
    """排序只在窗口内发生，且**不增不减**。

    跨窗口重排会破坏 `planned_until` 的语义（"已规划前 60 个"到底指哪些标题？），
    而排序本身绝不能变成过滤 —— 丢一个标题就是丢一节需求。
    """
    all_heads = [f"标题{i}" for i in range(10)]
    specgen._HEADING_LEVELS[999] = {h: (3 if i % 2 else 1) for i, h in enumerate(all_heads)}
    got = specgen.rank_window(all_heads, 2, 6, 999)
    assert sorted(got) == sorted(all_heads[2:6]), "窗口内排序改变了集合内容"
    assert len(got) == 4
