"""judge 必须正确解包 chat_with_pool 的二元组返回（2026-10-11 真实事故回归）。

事故：judge._call 把 `chat_with_pool` 返回的 `(ChatResult, profile)` 元组当成
ChatResult 直接返回，`_tally` 取 `.usage` 时炸
`AttributeError: 'tuple' object has no attribute 'usage'`，
整轮运行的每一条判定都落成 judge_error（用户截图 2026-10-11）。

specgen.py 一直是对的（`res, _prof = await ...`），所以这题的教训是：
**门面的池化入口返回二元组这件事，每个新调用点都要被测试钉住。**
"""

from __future__ import annotations

import json

from app import judge as judge_mod
from app.config import get_settings


class _FakeChatResult:
    """app.llm_chat.ChatResult 的最小替身：只带 .text / .usage / .json()。"""

    def __init__(self, text: str):
        self.text = text
        self.usage = {"prompt": 1, "completion": 1, "cached": 0, "total": 2, "calls": 1}
        self.model = "fake"
        self.protocol = "openai"

    def json(self, default=None):
        try:
            return json.loads(self.text)
        except Exception:
            return default


async def _fake_chat_with_pool(**kwargs):
    # 形状必须与 app.llm_chat.chat_with_pool 一致：**二元组**
    return _FakeChatResult(json.dumps({"status": "passed", "reason": "ok", "evidence": []})), None


async def test_judge_unpacks_chat_with_pool_tuple(monkeypatch):
    """judge 全流程走通即证明：拿到的是 ChatResult，不是元组。

    若有人把解包改回直接返回元组，这里会炸
    'tuple' object has no attribute 'usage' / 'json' —— 正是线上那两个症状。
    """
    from app import llm as llm_mod

    monkeypatch.setattr(llm_mod, "chat_with_pool", _fake_chat_with_pool, raising=True)
    monkeypatch.setattr(judge_mod, "get_settings", lambda: get_settings(), raising=False)

    verdict = await judge_mod.judge(
        expected="列表显示 10 条",
        final_answer="列表显示了 10 条记录",
        actions=["打开页面", "查询"],
        task="查询列表",
        evidence=[],
    )
    assert verdict.status == "passed"
    # 判定 token 账本也应从 ChatResult.usage 正常累计，而不是 0/缺失
    assert verdict.usage.get("calls") == 1
