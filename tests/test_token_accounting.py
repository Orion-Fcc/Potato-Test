"""token 账本的正确性（2026-10-09）。

## 为什么这组测试值得存在

在这之前，**没有任何地方记录一条用例花了多少 token** —— 用户唯一的感受是
"这个系统很烧 token"，而"烧在哪"完全靠猜：是步数多？是每步页面快照大？是系统提示词长？
是没有命中缓存？这几个方向的优化手段完全不同，猜错就白干一轮。

所以先让消耗**可见**：账本错了，"优化有效"就无从证明，甚至会把变贵当成变便宜。

实测基线（3 条真实列表/查询用例，走真网关）：
    prompt 121,715 / completion 3,774 / cached 70,656 / total 125,489 / 8 步
    -> ~15,686 token/步，其中约 12k 是"与页面无关的固定开销"
       （browser-use 自带提示词 ~6.0k + 本项目的规则 ~4.1k + 工具 schema ~2.0k）

## 这组测试守什么

1. `_usage_payload` 把 browser-use 的 UsageSummary 折成**固定键**的紧凑账本；
2. 没有任何 usage 时返回**空字典**（而不是一堆 0）—— 0 和"没测到"必须能区分，
   否则报告会把"网关不回 usage"画成"这条用例免费"；
3. 判定器的 usage 能带进 Verdict（它每条用例固定一次，~2.9k 的 system 提示词，
   不算进账就会出现"agent 看起来省、整体依然贵"的错觉）。
"""

from __future__ import annotations

from app.executor import _usage_payload
from app.judge import Verdict


class _Usage:
    total_prompt_tokens = 1000
    total_completion_tokens = 200
    total_prompt_cached_tokens = 640
    total_tokens = 1200
    entry_count = 3


def test_usage_payload_shape() -> None:
    u = _usage_payload(_Usage())
    assert u == {"prompt": 1000, "completion": 200, "cached": 640, "total": 1200, "calls": 3}


def test_usage_payload_missing_is_empty_not_zeros() -> None:
    """没拿到 usage 时必须是 {}，不能是 {prompt: 0, ...}。

    区别很重要：`None/{}` = "这次没测到"（网关不回 usage、埋点前的历史行），
    而 0 = "真的没花钱"。混在一起，报告会把未测量的用例画成 0 消耗，
    于是"总消耗"看起来比实际低 —— 一个会让人放心地继续烧钱的错误。
    """
    assert _usage_payload(None) == {}


def test_usage_payload_tolerates_a_changed_summary() -> None:
    """browser-use 是锁版本的，但字段仍可能改名 —— 缺字段退化，不要抛。"""

    class _Partial:
        total_prompt_tokens = 7

    u = _usage_payload(_Partial())
    assert u["prompt"] == 7
    assert u["completion"] == 0 and u["total"] == 0 and u["calls"] == 0


def test_verdict_carries_usage_and_defaults_to_empty() -> None:
    """判定器的 token 必须能带出来；不传时是空字典（= 没测到）。"""
    assert Verdict(status="passed", reason="ok").usage == {}
    v = Verdict(status="passed", reason="ok", usage={"prompt": 5, "total": 5, "calls": 1})
    assert v.usage["prompt"] == 5
    assert v.usage["calls"] == 1
