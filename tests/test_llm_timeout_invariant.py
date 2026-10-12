"""单次 LLM 调用上限必须小于整步上限（2026-10-10 实测后新增）。

## 为什么这是一个"提速 + 质量"双输的配置错误

`.env` 里 `LLM_TIMEOUT_S` 和 `STEP_TIMEOUT_S` 是两个独立的键，而它们之间有一条
必须成立的不变量：**单次调用上限要小于整步上限**。破了它的代价不是"慢一点"：

    步在 45s 被 browser-use 砍掉（asyncio.wait_for 超时，该步作废）
    而那次模型调用还在网关那头跑到 90s
    → 这一次调用白烧掉
    → 并发位被占住（max_global_concurrency = 1）→ 整条队列停在那儿等一个作废的响应
    → 而且 consecutive_failures 会累加，踩到反空转规则就直接判失败

实测就是 `llm=35` / `step=45`（只差 10s），而每步耗时 p90 已经是 45.5s ——
也就是说正常步的上限和 LLM 上限**撞在一起**了。实测 13 次
"LLM call timed out after 35 seconds"，那些步总耗时 51~121s，
说明模型当时正在正常工作、只是慢。

## 这组测试守什么

1. 夹取函数在任何输入下都返回 `llm < step`。
2. **仓库里实际配置的默认值**满足这条不变量 —— 否则下一个人把 `.env` 删了
   或者改了 config 默认值，不变量又破了，而没有任何东西会提醒他。
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.executor import effective_llm_timeout

# 夹取函数保留给 DOM 采集与动作执行的余量（见函数注释）。
HEADROOM = 20


def test_llm_timeout_is_always_below_step_timeout():
    """★ 不变量：夹取之后 llm < step，任何输入组合都不能例外。"""
    for llm in (1, 10, 35, 90, 100, 120, 200, 1000):
        for step in (5, 30, 45, 60, 120, 300):
            got = effective_llm_timeout(llm, step, None)
            assert got < step, f"llm={llm} step={step} -> {got}，破了不变量"


def test_a_sane_pair_is_left_alone():
    """正常的组合不该被改动 —— 夹取只处理"太接近"的情况。"""
    assert effective_llm_timeout(90, 120, None) == 90
    assert effective_llm_timeout(30, 120, None) == 30
    assert effective_llm_timeout(60, 300, None) == 60


def test_a_too_close_pair_is_capped_with_headroom():
    """实测踩到的组合：35 / 45 只差 10s，必须被夹到留出余量。"""
    got = effective_llm_timeout(35, 45, None)
    assert got == 45 - HEADROOM, f"应当夹到 {45 - HEADROOM}，实际 {got}"


def test_llm_timeout_never_reaches_zero_or_negative():
    """极端配置下也要返回一个能用的正数 —— 0 会让每次调用立刻超时，
    表现成"全部用例都失败"，比配置错误本身更难排查。"""
    assert effective_llm_timeout(0, 10, None) >= 1
    assert effective_llm_timeout(999, 10, None) >= 1
    assert effective_llm_timeout(-5, 10, None) >= 1


def test_the_shipped_defaults_satisfy_the_invariant():
    """★ 仓库里**当前生效**的两个值必须满足不变量。

    这条比上面几条更重要：前面的测试用的都是我编的参数，
    而这条读的是真实配置 —— 如果有人把 `.env` 或 config 默认值改成
    llm >= step，这里会红，而不是等到线上跑完一轮才发现整步都在白烧。
    """
    s = get_settings()
    eff = effective_llm_timeout(s.llm_timeout_s, s.step_timeout_s, s)
    assert eff < s.step_timeout_s, (
        f"当前配置 llm_timeout_s={s.llm_timeout_s} / step_timeout_s={s.step_timeout_s} "
        f"夹取后 {eff} —— 仍不满足 llm < step"
    )
    # 也别夹得太狠：夹到太小会让正常的中等耗时调用被误杀，
    # 那是"用降低质量换一个假的稳定性"。
    assert eff >= 60, (
        f"生效的 LLM 上限只有 {eff}s。实测每步耗时 p90=45.5s、p99=98.7s，"
        "夹到 60s 以下会把正常的慢调用大量误杀。"
    )


@pytest.mark.parametrize("llm,step", [(35, 45), (44, 45), (45, 45), (50, 45)])
def test_boundary_cases_do_not_invert(llm: int, step: int):
    """边界：llm 等于或大于 step 时也必须夹住（不能出现负数或原样返回）。"""
    got = effective_llm_timeout(llm, step, None)
    assert 0 < got < step
