"""Runnable checks for the concurrency-1 speed levers.

Concurrency is pinned to 1 on this project, so a wasted step is not overlapped with
anything — it is pure serial dead time. Measured on the project's own run history
(2026-10-02): 44% of steps were `wait` / `scroll` / `search_page` / `find_elements` /
`evaluate`, i.e. steps that changed nothing and only re-asked a question the page had
already answered.

Two levers are locked in here:
  1. `wait` and `search_page` are removed from the agent's tool registry, so the model
     cannot call them. A prompt rule asks; removing the tool guarantees.
  2. The anti-waste rules are present in the system message.

Kept as tests because both are easy to lose in a refactor and the failure is silent —
the suite stays green while every case quietly gets 40% slower.

python -m pytest tests/test_speed_levers.py
"""

from __future__ import annotations

from app.config import get_settings
from app.executor import _ANTI_WASTE_RULE, _build_tools


def _registry_names() -> set[str]:
    tools = _build_tools(get_settings())
    assert tools is not None, "the tool registry must build (fallback is None = default set)"
    return set(tools.registry.registry.actions.keys())


def test_wait_and_search_page_are_not_available() -> None:
    """The two pure time-wasters must be physically absent, not merely discouraged."""
    names = _registry_names()

    assert "wait" not in names, (
        "`wait` re-observes a page the agent is handed a fresh snapshot of anyway — it "
        "cannot pay for its round-trip. Runs showed wait→wait→wait chains while the model "
        "noted 'DOM not captured yet'."
    )
    assert "search_page" not in names, (
        "`search_page` re-reads text already in the page state. Run #140 searched the same "
        "validation message twice in two wordings, burning two full round-trips."
    )


def test_useful_actions_survive_the_exclusion() -> None:
    """移除错的动作会搞坏真实用例 —— 守住"真人真的会做"的那批动作。

    注意这条测试**曾经断言 evaluate / find_elements 必须保留**（那时它们被当作
    "读长列表有用"而留下）。后来需求变成"要像真人一样操作"：真人不会执行 JS、
    也不会用脚本列元素，于是这两个被移出。所以这里守的是"人类动作"集合，
    而不是当初那份"有用"集合 —— 意图变了，断言也要跟着变。
    """
    names = _registry_names()

    for keep in ("click", "input", "navigate", "scroll", "done"):
        assert keep in names, f"{keep} 是真人会做的动作，必须保留"

    for banned in ("evaluate", "find_elements"):
        assert banned not in names, (
            f"{banned} 是跳过界面的脚本捷径，与『像真人一样操作』冲突，必须移除"
        )


def test_exclusion_list_is_configurable() -> None:
    """排除集合来自 settings，方便按部署环境调整。"""
    assert get_settings().excluded_agent_actions == [
        "wait",
        "search_page",
        "evaluate",
        "find_elements",
    ]


def test_build_tools_never_raises() -> None:
    """A registry API mismatch must cost the tweak, never the case.

    `None` is the documented fallback (it means "let browser-use use its default set"),
    so the contract under test is "does not raise", not "returns a Tools".
    """

    class _NoActions:
        excluded_agent_actions = ["does_not_exist_as_an_action"]

    # Unknown names are skipped, not fatal — and the rest of the exclusions still apply.
    tools = _build_tools(_NoActions())
    assert tools is not None, "an unusable name must not discard the whole registry"
    assert "wait" in tools.registry.registry.actions, (
        "with no valid exclusion the registry should be untouched, i.e. still complete"
    )

    class _Boom:
        @property
        def excluded_agent_actions(self):
            raise RuntimeError("boom")

    # An exploding settings object falls back to browser-use's default registry.
    tools = _build_tools(_Boom())
    assert tools is None, "on failure we return None = use browser-use defaults, not raise"


def test_anti_waste_rule_covers_each_waste_pattern() -> None:
    """Each measured waste pattern has an explicit counter-rule."""
    assert "CONFIRM ONCE" in _ANTI_WASTE_RULE, "repeated verification of one fact"
    assert "NEVER WAIT BLINDLY" in _ANTI_WASTE_RULE, "waiting for a state that arrives anyway"
    assert "RE-VERIFICATION" in _ANTI_WASTE_RULE, "re-clicking to be sure"
    assert "BATCH INDEPENDENT ACTIONS" in _ANTI_WASTE_RULE, (
        "67% of steps carried a single action; batching is the biggest step-count lever"
    )
    assert "STOP WHEN DONE" in _ANTI_WASTE_RULE, "final confirmation of a known conclusion"


def test_anti_waste_rule_yields_to_the_popup_rule() -> None:
    """Batching must not be read as licence to click a popover option in one step."""
    assert "popover" in _ANTI_WASTE_RULE.lower() or "dropdown" in _ANTI_WASTE_RULE.lower(), (
        "the batching rule must name the one exception, or it contradicts the popup rule "
        "and reintroduces the 20-identical-steps dropdown loop"
    )


def test_agent_gets_the_anti_waste_rule() -> None:
    """The rule only helps if it is actually attached to the agent's system message."""
    import inspect

    from app.executor import execute_case

    src = inspect.getsource(execute_case)
    assert "_ANTI_WASTE_RULE" in src, "the rule must be interpolated into extend_system_message"
    # 2026-10-04: the call became `_build_tools(s, spec)` (spec carries the multi-role
    # logins). The point of this assertion is that the agent gets the TRIMMED registry
    # built from the settings `s` — not a hard-coded default registry — so match on
    # `_build_tools(s` and stop there. Pinning the full string would break on any
    # future extra argument, which is how this assertion bit us once already.
    assert "tools=_build_tools(s" in src, "the agent must receive the trimmed registry"
