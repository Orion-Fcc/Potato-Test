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
