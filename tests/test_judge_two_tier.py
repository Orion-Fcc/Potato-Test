"""两级判定（2026-10-11）：便宜模型先判，只在真正需要时请强模型复核。

用户的原话是"能不能多个 LLM 互相合作，DeepSeek 的 token 太花钱"。
判定的账很好算：**每条用例固定一次、输入 3~4k、占整轮 token 的个位数百分比**，
所以这里不做"全都换便宜模型"（那会砸掉最容易出错的环节），
而是做成"便宜模型先判 + 有条件地请强模型复核"。

这个文件钉住的是**花钱的那几条决策**，因为每一条写错都只表现为"账单变贵"，
不会报错 —— 没有测试就永远发现不了：

  1. 初判 failed          → 必须复核（假失败会让人去查不存在的缺陷）
  2. 初判 passed 但闸门报警 → 必须复核（假通过的高发区）
  3. 初判 passed 且干净    → **绝不**复核（这是省钱的来源，也是回归风险最大的一条）
  4. 两层指向同一支模型    → 不复核（同一支模型再看一遍只是白花钱）
  5. 复核层自己挂了        → 保留初判（不能因为复核失败而没有结论）
  6. 初判层自己挂了        → 换复核层再试（换个模型可能就正常了）

以及账本：Verdict.usage 必须累计**两层之和**，否则"复核到底花了多少"无从复盘。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app import judge as judge_mod
from app import llm as llm_mod

# 触发"执行未落位"闸门的一步：action 已经是 done，同一条记录却在执行点击。
# （见 app/judge_claims.py::find_premature_done，现场是 run 12 / case 4）
_PREMATURE_DONE_STEP = "第3步: 【done】Clicked button「查询」(意图: 点查询后读结果)"


class _FakeChatResult:
    """app.llm_chat.ChatResult 的最小替身：只带 judge 真正读的那几个面。"""

    def __init__(self, payload: dict):
        self.text = json.dumps(payload, ensure_ascii=False)
        # 每次调用固定 10/5/2 —— 两层的累计值就能被断言成 20/10/4
        self.usage = {"prompt": 10, "completion": 5, "cached": 2, "total": 15, "calls": 1}
        self.model = "fake"
        self.protocol = "openai"

    def json(self, default=None):
        try:
            return json.loads(self.text)
        except Exception:
            return default


@dataclass
class _Prof:
    base_url: str
    model: str
    api_key: str = "sk-fake"
    provider: str = "openai"
    agent_model: str = ""


class _Harness:
    """替换掉 llm 门面的三个入口，并把"哪一层被调用"记下来。

    调用序列（self.calls）是这组测试的核心断言对象：钱是按调用次数花的。
    """

    def __init__(self, monkeypatch, *, two_tier=True, replies=None, profiles=None):
        self.two_tier = two_tier
        # 默认两层指向不同模型 —— 否则测试 1/2/3 会因为"同模型跳过复核"而提前返回，
        # 看起来像功能没生效，实际是另一条规则的正常行为（那条规则由测试 5 单独钉）。
        self.profiles = profiles or {
            "judge": _Prof("https://gateway.example", "cheap-model"),
            "judge_escalate": _Prof("https://gateway.example", "strong-model"),
        }
        self.replies = replies or {}
        self.calls: list[str] = []
        monkeypatch.setattr(llm_mod, "judge_two_tier_enabled", self._enabled, raising=True)
        monkeypatch.setattr(llm_mod, "role_profile", self._role_profile, raising=True)
        monkeypatch.setattr(llm_mod, "chat_with_pool", self._chat_with_pool, raising=True)

    async def _enabled(self) -> bool:
        return self.two_tier

    async def _role_profile(self, role: str):
        return self.profiles.get(role) or self.profiles["judge"]

    async def _chat_with_pool(self, **kwargs):
        role = kwargs.get("role") or "judge"
        self.calls.append(role)
        reply = self.replies.get(role)
        if reply is None:
            reply = {"status": "passed", "reason": "没有预期的结果，判定通过", "evidence": [1]}
        if isinstance(reply, BaseException):
            raise reply
        # 形状必须与 app.llm_chat.chat_with_pool 一致：**二元组**
        return _FakeChatResult(reply), None


async def _judge(evidence=None, final_answer="列表已按条件查询完成，结果与预期一致。"):
    return await judge_mod.judge(
        expected="查询后列表显示 1 条记录",
        final_answer=final_answer,
        actions=["打开列表页", "点击查询"],
        task="查询列表并按条件筛选",
        evidence=evidence if evidence is not None else ["第1步: 【navigate】Navigated to /list"],
    )


# ── 1. 初判 failed → 复核 ────────────────────────────────────────────────────


async def test_failed_first_verdict_escalates_to_review(monkeypatch):
    h = _Harness(
        monkeypatch,
        replies={
            "judge": {"status": "failed", "reason": "没看到列表", "evidence": [1],
                      "root_cause": "agent_incomplete", "evidence_gap": True},
            "judge_escalate": {"status": "passed", "reason": "截图里列表确实显示了 1 条", "evidence": [1]},
        },
    )
    v = await _judge()
    assert h.calls == ["judge", "judge_escalate"]
    # 复核改判：结论来自复核层，并且必须留下"它是被升级上来的"这条痕迹
    assert v.status == "passed"
    assert v.escalated is True
    assert v.escalated_from == "failed"


# ── 2. 初判 passed 且干净 → 绝不复核（省钱的那一条）────────────────────────


async def test_clean_pass_never_calls_the_review_model(monkeypatch):
    h = _Harness(monkeypatch, replies={"judge": {"status": "passed", "reason": "列表显示 1 条", "evidence": [1]}})
    v = await _judge()
    assert h.calls == ["judge"]  # ★ 只花一次钱
    assert v.status == "passed"
    assert v.escalated is False
    assert v.escalated_from == ""


# ── 3. 初判 passed 但确定性闸门已报警 → 复核 ────────────────────────────────


async def test_pass_with_gate_alarm_escalates(monkeypatch):
    """结论里引用了步骤记录中找不到的数字 —— 假通过的高发区。

    这里刻意让初判**判通过**（不是 failed），所以触发复核的只能是闸门报警本身；
    断言 escalated_from == "passed" 就是在证明这一点。
    """
    h = _Harness(
        monkeypatch,
        replies={
            # "11" 在证据里根本不存在（见 app/judge_claims.py::find_unverified_claims）
            "judge": {"status": "passed", "reason": "列表共 11 条，与预期一致", "evidence": [1]},
            "judge_escalate": {"status": "failed", "reason": "数字 11 在观察记录里找不到出处",
                               "evidence": [1], "root_cause": "agent_incomplete", "evidence_gap": True},
        },
    )
    v = await _judge(final_answer="查询后列表共 11 条记录，与预期一致。")
    assert h.calls == ["judge", "judge_escalate"]
    assert v.escalated is True
    assert v.escalated_from == "passed"
    assert v.status == "failed"


async def test_premature_done_alarm_also_escalates(monkeypatch):
    """另一条闸门信号：宣布完成时操作还没落位（时序事实，不依赖语义）。"""
    h = _Harness(
        monkeypatch,
        replies={"judge": {"status": "passed", "reason": "查询完成", "evidence": [1]},
                 "judge_escalate": {"status": "failed", "reason": "它在操作生效前就宣布完成",
                                    "evidence": [1], "root_cause": "agent_incomplete"}},
    )
    v = await _judge(evidence=["第1步: 【navigate】Navigated to /list", _PREMATURE_DONE_STEP])
    assert h.calls == ["judge", "judge_escalate"]
    assert v.escalated_from == "passed"


# ── 4. 两层同一支模型 → 跳过复核 ─────────────────────────────────────────────


async def test_same_model_on_both_tiers_skips_review(monkeypatch):
    """同一支模型再看一遍不会多出信息，只是白花钱 —— 必须跳过。

    这条规则的反面同样重要：跳过时**不能**把结论标成 escalated，
    否则统计里的"升级率"会被灌水，调阈值时看到的就是假数据。
    """
    same = {"judge": _Prof("https://gateway.example", "only-model"),
            "judge_escalate": _Prof("https://gateway.example", "only-model")}
    h = _Harness(
        monkeypatch,
        profiles=same,
        replies={"judge": {"status": "failed", "reason": "没看到列表", "evidence": [1],
                           "root_cause": "agent_incomplete"}},
    )
    v = await _judge()
    assert h.calls == ["judge"]
    assert v.status == "failed"
    assert v.escalated is False


# ── 5. 复核层挂了 → 保留初判 ────────────────────────────────────────────────


async def test_review_failure_keeps_the_first_verdict(monkeypatch):
    h = _Harness(
        monkeypatch,
        replies={
            "judge": {"status": "failed", "reason": "初判：没看到列表", "evidence": [1],
                      "root_cause": "agent_incomplete"},
            "judge_escalate": RuntimeError("复核层超时"),
        },
    )
    v = await _judge()
    assert h.calls == ["judge", "judge_escalate"]
    # 初判是有效结论，不能因为"复核失败"就变成没有结论
    assert v.status == "failed"
    assert "初判" in v.reason
    assert v.escalated is False  # 复核没给出结论，不算"升级后的结论"


# ── 6. 初判层挂了 → 换复核层再试 ────────────────────────────────────────────


async def test_first_tier_error_escalates_to_review(monkeypatch):
    h = _Harness(
        monkeypatch,
        replies={
            "judge": RuntimeError("初判层网关 500"),
            "judge_escalate": {"status": "passed", "reason": "复核：列表确实显示了 1 条", "evidence": [1]},
        },
    )
    v = await _judge()
    assert h.calls == ["judge", "judge_escalate"]
    assert v.status == "passed"
    assert v.escalated is True
    # 必须区分"因为初判挂了才升级"和"因为初判判失败才升级" ——
    # 前者说明便宜模型/网关不稳，后者说明判定分歧，处理方式完全不同
    assert v.escalated_from.startswith("error:")


async def test_both_tiers_error_reports_judge_error(monkeypatch):
    h = _Harness(
        monkeypatch,
        replies={"judge": RuntimeError("初判挂了"), "judge_escalate": RuntimeError("复核也挂了")},
    )
    v = await _judge()
    assert h.calls == ["judge", "judge_escalate"]
    assert v.status == "failed"
    assert v.reason.startswith("judge_error:")
    # 归 environment（可重试），绝不能混进"真缺陷"统计
    assert v.root_cause == "environment"


# ── 7. 关掉开关 = 回到单层，且不再花复核的钱 ───────────────────────────────


async def test_disabled_two_tier_never_calls_review(monkeypatch):
    h = _Harness(
        monkeypatch,
        two_tier=False,
        replies={"judge": {"status": "failed", "reason": "没看到列表", "evidence": [1],
                           "root_cause": "agent_incomplete"}},
    )
    v = await _judge()
    assert h.calls == ["judge"]
    assert v.escalated is False


# ── 8. 账本：两层之和 ───────────────────────────────────────────────────────


async def test_usage_accumulates_across_both_tiers(monkeypatch):
    """★ 不累计就等于"复核免费" —— 复盘成本时会把最贵的那部分漏掉。"""
    h = _Harness(
        monkeypatch,
        replies={"judge": {"status": "failed", "reason": "初判失败", "evidence": [1],
                           "root_cause": "agent_incomplete"},
                 "judge_escalate": {"status": "passed", "reason": "复核通过", "evidence": [1]}},
    )
    v = await _judge()
    assert len(h.calls) == 2
    assert v.usage["calls"] == 2
    assert v.usage["prompt"] == 20  # 单层 10 × 2
    assert v.usage["completion"] == 10
    assert v.usage["cached"] == 4
    assert v.usage["total"] == 30
