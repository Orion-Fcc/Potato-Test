"""性能异常分析（2026-10-09）。

用户的需求原话：「agent 也需要分析性能，比如该测试用例本不应该花那么长时间，
但是他花了很长时间，说明该用例也有轻微的问题，性能不够好」。

也就是说 "跑对了" ≠ "没问题"。这组测试守的是**这一维信息**：

1. 它必须能抓出真实的慢用例（用真库里的实测形态构造：1 步却耗时 285s）；
2. 它**不能**把"步数多所以总耗时大"误报成性能问题 ——
   47 步的用例当然比 6 步慢，那是它该干的活多，不是异常；
3. 样本太少时不许判离群（"中位数"本身在 2 条样本上就是噪音）；
4. "时间花在跑用例之外"要归到环境，不能报成被测系统的性能问题 ——
   否则用户会拿着一条环境告警去质问开发。
"""

from __future__ import annotations

import json

from app.perf_analysis import (
    ABS_SLOW_STEP_MS,
    MIN_SAMPLES,
    SLOW_FACTOR,
    analyse_run,
    perf_note_for,
)


def _t(
    total: int,
    steps: int,
    *,
    browser_up: int = 2500,
    video: int = 1000,
    wrap: int = 0,
    judge: int = 3600,
) -> str:
    """造一条 timing JSON（形态照 executor._timing_payload 的真实产物）。"""
    agent = max(0, total - browser_up - video - wrap - judge)
    per_step = int(agent / steps) if steps else 0
    return json.dumps(
        {
            "browser_up_ms": browser_up,
            "agent_ms": agent,
            "wrap_up_ms": wrap,
            "video_ms": video,
            "judge_ms": judge,
            "total_ms": total,
            "steps": steps,
            "per_step_ms": per_step,
        }
    )


def _run(specs: list[tuple[int, str]]) -> list[dict]:
    return [{"case_id": cid, "timing": t} for cid, t in specs]


def test_catches_a_case_that_took_minutes_for_one_step() -> None:
    """★ 真库里的实测形态：1 步却花了 285 秒。

    case 1089 每步 271s，而同轮中位数是 11.4s —— 23 倍。
    这正是用户说的"本不应该花那么长时间"。
    """
    rows = _run(
        [
            (1, _t(60_000, 6)),
            (2, _t(70_000, 6)),
            (3, _t(65_000, 6)),
            (4, _t(80_000, 7)),
            (1089, _t(285_000, 1)),
        ]
    )
    rep = analyse_run(rows)
    f = rep.by_case[1089]
    assert f.kind == "slow_step", f.kind
    assert f.ratio > 10, f.ratio
    assert "285" in f.detail or "271" in f.detail


def test_a_long_case_is_not_flagged_just_for_taking_long() -> None:
    """★ 反向守卫：步数多 → 总耗时长，**不是**性能异常。

    这条如果做错，47 步的正常长用例会被报成性能问题，
    用户会去查一个根本不存在的缺陷。
    """
    rows = _run(
        [
            (1, _t(60_000, 6)),  # 每步 ~9s
            (2, _t(65_000, 6)),
            (3, _t(70_000, 7)),
            (4, _t(600_000, 47)),  # 总耗时 10 倍，但每步仍然 ~12s
        ]
    )
    rep = analyse_run(rows)
    assert 4 not in rep.by_case, "长用例被误报成性能问题了"


def test_outlier_needs_enough_samples() -> None:
    """样本太少时**不判"离群"**：2 条样本的"中位数"没有意义。

    注意这里守的是 `slow_outlier`（相对同轮中位数），不是 `slow_step`
    （单步绝对耗时）—— 后者是绝对门限，1 条样本也能判，而且**应该**判：
    某一步真的花了 100 秒，跟有几条样本无关。
    这两条规则的关系容易搞混，所以测试要分开写清楚。
    """
    rows = _run([(1, _t(60_000, 6)), (2, _t(600_000, 6))])
    rep = analyse_run(rows)
    outliers = [f for f in rep.flags if f.kind == "slow_outlier"]
    assert outliers == [], f"样本不足却报了离群：{outliers}"
    assert str(MIN_SAMPLES) in rep.note


def test_absolute_slow_step_is_judged_even_with_one_sample() -> None:
    """单步绝对慢与样本数无关 —— 某一步花了 100 秒，不需要别人作参照。"""
    rep = analyse_run(_run([(1, _t(400_000, 4, judge=0))]))  # 每步约 100s
    assert [f.kind for f in rep.flags] == ["slow_step"]


def test_overhead_is_attributed_to_the_environment_not_the_product() -> None:
    """★ 时间花在"跑之外"时，结论必须是环境问题。

    报成被测系统的性能问题，用户就会拿着一条环境告警去质问开发 ——
    这种错误会直接摧毁性能告警的可信度。所以这条用例的时间构成做成**只可能是开销**：
    每步很短（不会触发 slow_step），但启动+录像占了绝大多数。
    """
    rows = _run(
        [
            (1, _t(60_000, 6)),
            (2, _t(60_000, 6)),
            (3, _t(60_000, 6)),
            # 8 步、每步约 6s（远低于绝对门限），但启动 40s + 录像 20s 占了 60/63
            (4, _t(63_000, 8, browser_up=40_000, video=20_000, judge=0)),
        ]
    )
    rep = analyse_run(rows)
    f = rep.by_case[4]
    assert f.kind == "overhead", f.kind
    assert "不是被测系统" in f.note
    # 同一轮里不该对同一条用例既报环境又报慢
    assert sum(1 for x in rep.flags if x.case_id == 4) == 1


def test_missing_timing_reports_why_instead_of_silently_passing() -> None:
    """没有 timing（埋点前的历史行）要说清**为什么**没结论。

    静默返回空报告会让人以为"这一轮性能都正常"，而实际是"根本没测"。
    """
    rep = analyse_run([{"case_id": 1, "timing": None}, {"case_id": 2, "timing": ""}])
    assert rep.flags == []
    assert "无法做性能分析" in rep.note


def test_clean_run_says_so() -> None:
    rows = _run([(i, _t(60_000 + i * 1000, 6)) for i in range(1, 6)])
    rep = analyse_run(rows)
    assert rep.flags == []
    assert "正常范围" in rep.note


def test_analysis_accepts_both_dicts_and_objects() -> None:
    """API 传 dict、engine 传 ORM 行 —— 两种都要能收。"""

    class Row:
        def __init__(self, cid, timing):
            self.case_id = cid
            self.timing = timing

    rows = [Row(1, _t(60_000, 6)), Row(2, _t(60_000, 6)), Row(3, _t(60_000, 6))]
    rep = analyse_run(rows)
    assert rep.samples == 3
    assert rep.baseline_per_step_ms > 0


def test_timing_may_be_a_dict_already() -> None:
    """JSON 列在某些驱动下已经是 dict 了 —— 不能再 json.loads 一次。"""
    rows = [{"case_id": 1, "timing": json.loads(_t(60_000, 6))}]
    rep = analyse_run(rows)
    assert rep.samples == 1


def test_perf_note_is_empty_without_a_flag() -> None:
    rows = _run([(1, _t(60_000, 6)), (2, _t(60_000, 6)), (3, _t(60_000, 6))])
    rep = analyse_run(rows)
    assert perf_note_for({"case_id": 1}, rep) == ""
    assert perf_note_for({"case_id": 999}, rep) == ""


def test_perf_note_never_frames_it_as_a_test_failure() -> None:
    """性能结论是**叠加**在结论上的另一维信息，不是翻案。

    如果措辞像"失败"，用户会以为这条用例判错了 ——
    而它很可能结果完全正确，只是慢。
    """
    rows = _run(
        [
            (1, _t(60_000, 6)),
            (2, _t(62_000, 6)),
            (3, _t(64_000, 6)),
            (9, _t(285_000, 1)),
        ]
    )
    rep = analyse_run(rows)
    note = perf_note_for({"case_id": 9}, rep)
    assert note.startswith("[性能]")
    for bad in ("失败", "不通过", "判失败"):
        assert bad not in note, f"性能结论的措辞不该像判定结果：{note}"


def test_thresholds_are_conservative() -> None:
    """门限必须偏保守：一次假性能告警，用户下次就不看这类告警了。"""
    assert SLOW_FACTOR >= 2.5, "倍数门限太低会把正常波动报成异常"
    assert ABS_SLOW_STEP_MS >= 30_000, "绝对门限太低会把慢页面报成异常"
    assert MIN_SAMPLES >= 3
