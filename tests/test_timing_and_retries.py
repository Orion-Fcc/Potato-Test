"""耗时分解落库 + per-run 重试开关。

两个功能各解决一个"数据看不见"的问题，所以测试也按这两条线组织：

* **耗时分解。** 执行器早就在每条用例结束时算好 5 段墙钟，但只打成一行 WARNING。
  于是"这轮为什么慢"只能翻日志，报告页看不到、也没法按阶段汇总。这里钉住的是
  **折算法本身**（`_timing_payload`）—— 段不重叠、缺段不瞎补、单位是 ms。

* **per-run 重试。** 此前 `CASE_RETRIES` 只能全局一刀切。per-run 值最容易踩的坑是
  "0 当成了没设置"：一旦写成 `run.retries or global`，用户在界面上选的"不重试"就会被
  全局值顶掉，而且悄无声息。`effective_retries` 的测试就是钉死这条语义。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.engine import effective_retries  # noqa: E402
from app.executor import _timing_payload  # noqa: E402


class _Settings:
    def __init__(self, case_retries: int = 0) -> None:
        self.case_retries = case_retries


class _Run:
    def __init__(self, retries=None) -> None:
        self.retries = retries


# ── 耗时分解 ──────────────────────────────────────────────────────────────────


def test_the_five_segments_are_contiguous_and_sum_to_the_total():
    """五段必须首尾相接、加起来等于总数。断了一截就说明有段时间没人认领。"""
    stage = {"browser_up": 10.0, "agent": 40.0, "close": 45.0, "video": 47.0}
    p = _timing_payload(stage, 0.0, 50.0, 8)
    assert p["browser_up_ms"] == 10_000
    assert p["agent_ms"] == 30_000
    assert p["wrap_up_ms"] == 5_000
    assert p["video_ms"] == 2_000
    assert p["judge_ms"] == 3_000
    assert p["total_ms"] == 50_000
    seg_sum = (
        p["browser_up_ms"] + p["agent_ms"] + p["wrap_up_ms"] + p["video_ms"] + p["judge_ms"]
    )
    assert seg_sum == p["total_ms"]


def test_a_missing_middle_stage_collapses_to_zero_rather_than_inflating_a_neighbour():
    """中间某段没打点时，不能把它算到相邻段头上。

    否则"agent 很慢"这种结论会被带偏 —— 而那正是要看这份数据去回答的问题。
    没被认领的时间一律落到最后一段（judge）：它的定义就是"video 之后到返回"，
    时间轴已经走到头，没有下一段能接。所以该段记 0，总和仍等于 total。
    """
    p = _timing_payload({"browser_up": 10.0}, 0.0, 50.0, 3)
    assert p["browser_up_ms"] == 10_000
    assert p["agent_ms"] == 0
    assert p["wrap_up_ms"] == 0
    assert p["video_ms"] == 0
    assert p["judge_ms"] == 40_000
    assert p["total_ms"] == 50_000


def test_per_step_ms_is_zero_when_no_steps_ran():
    """0 步不能除零。用例在第一步之前就挂了（比如浏览器起不来）时正是这个形状。"""
    p = _timing_payload({}, 0.0, 12.0, 0)
    assert p["steps"] == 0
    assert p["per_step_ms"] == 0
    assert p["total_ms"] == 12_000


def test_nothing_ran_yet_is_all_zero_not_garbage():
    """一条都还没跑（时长 0、无任何阶段）时，各段应为 0 而不是负数或异常。

    注意 total 参数是**时长**（time.monotonic() - t0），不是绝对时间戳 ——
    传绝对时间戳进来会把 total 放大成开机以来的毫秒数。
    """
    p = _timing_payload({}, 100.0, 0.0, 0)
    assert p["browser_up_ms"] == 0
    assert p["agent_ms"] == 0
    assert p["judge_ms"] == 0
    assert p["total_ms"] == 0


# ── per-run 重试 ─────────────────────────────────────────────────────────────


def test_retries_are_inherited_from_the_global_setting_when_unset():
    """新建的 run 不显式指定时，必须沿用 .env 的全局值。

    这条是"为什么用 None 而不是 0 当未设置"的理由：若默认成 0，运维在 .env 里开了
    重试也永远不生效。
    """
    assert effective_retries(_Run(None), _Settings(case_retries=2)) == 2
    assert effective_retries(_Run(None), _Settings(case_retries=0)) == 0


def test_an_explicit_zero_is_respected_and_not_treated_as_unset():
    """显式 0 = "这一轮就是不重试"，不能被全局值顶掉。

    这是本功能最容易写错的地方：`run.retries or global` 会让 0 掉进回落分支。
    """
    assert effective_retries(_Run(0), _Settings(case_retries=2)) == 0


def test_an_explicit_value_overrides_the_global_setting():
    assert effective_retries(_Run(3), _Settings(case_retries=0)) == 3


def test_a_negative_or_bogus_value_cannot_go_below_zero():
    """负数重试没有意义，且会让循环条件变得诡异；夹到 0。"""
    assert effective_retries(_Run(-5), _Settings(case_retries=0)) == 0


def test_a_run_object_without_the_attribute_falls_back_to_global():
    """防御性：老对象（或某些测试替身）没有 retries 属性时走全局，不能 AttributeError。

    这条覆盖的是升级路径 —— 库里的 run 行在加列之前就存在，反序列化后该属性是 None，
    所以真实场景里走的是上面第一条；这里挡的是"对象压根没有这个属性"的形状。
    """

    class _Legacy:
        pass

    assert effective_retries(_Legacy(), _Settings(case_retries=1)) == 1


# ---------------------------------------------------------------------------
# 回调分段计时（2026-10-10）
# ---------------------------------------------------------------------------
#
# ## 为什么加这个
#
# 实测每步 17.8 秒，而**同样大小**的请求直测网关只要 4.8 秒
# （12,719 prompt token + 253 输出，约 2,660 tok/s）。
# 也就是每步约 13 秒花在模型调用之外 —— 但原有埋点里 `step_elapsed` 是
# "两次回调之间的墙钟"，把模型、DOM、动作、页面就绪、截图全揉成一个数，
# 分不出该往哪砍。这组测试守的是"这几项确实被分开记了、且没有重复计数"。


def _stage_and_phase():
    stage = {"browser_up": 3.0, "agent": 100.0, "close": 101.0, "video": 102.0}
    phase = {"prune_ms": 800.0, "shot_ms": 4200.0, "cb_ms": 6000.0, "steps": 10}
    return stage, phase


def test_phase_breakdown_is_recorded():
    """prune / shot / callback 三项都要落库，否则下次还是只能猜。"""
    stage, phase = _stage_and_phase()
    p = _timing_payload(stage, 0.0, 110.0, 10, phase)
    assert p["prune_ms"] == 800
    assert p["shot_ms"] == 4200
    assert p["callback_ms"] == 6000
    assert p["callback_per_step_ms"] == 600  # 6000ms / 10 步


def test_phase_breakdown_does_not_double_count():
    """★ 关键：分段是**拆分**不是**新增**。

    回调耗时本来就含在 agent_ms 里，如果再把它加进 total_ms，
    就会凭空多出一段并不存在的时间 —— 而"五段之和 == total"正是
    判断"有没有哪段没测到"的依据，破了它就等于把这套埋点废掉。
    """
    stage, phase = _stage_and_phase()
    p = _timing_payload(stage, 0.0, 110.0, 10, phase)
    segs = p["browser_up_ms"] + p["agent_ms"] + p["wrap_up_ms"] + p["video_ms"] + p["judge_ms"]
    assert segs == p["total_ms"], f"五段之和不等于 total：{segs} != {p['total_ms']}"


def test_the_breakdown_is_optional():
    """不带 phase 时必须照常工作 —— 老调用点（capture_session 等）不传这个参数。"""
    stage, _ = _stage_and_phase()
    p = _timing_payload(stage, 0.0, 110.0, 10)
    assert p["total_ms"] == 110000
    assert "prune_ms" not in p, "没传 phase 却出现了分段键"


def test_a_zero_step_case_does_not_divide_by_zero():
    """0 步的用例（启动就失败）也要能算出分解，不能 ZeroDivisionError。"""
    p = _timing_payload({"browser_up": 1.0, "agent": 2.0}, 0.0, 3.0, 0, {"cb_ms": 0.0})
    assert p["steps"] == 0
    assert p["per_step_ms"] == 0
    assert p["callback_per_step_ms"] == 0
