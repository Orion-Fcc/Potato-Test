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


def test_unused_file_tools_are_removed() -> None:
    """★ 2026-10-09：没人用的文件工具必须物理消失。

    为什么值得单独一条：工具 schema 会出现在**每一次** LLM 调用里，实测
    `_build_tools` 出来的 20 个工具合计约 2055 token/次调用。所以排除一个用不到的
    工具，省的不是一次调用，而是每一步。

    依据是拿真库 1203 条用例检索过：`write_file` / `read_file` / `replace_file`
    命中 **0 条**（用例要的是"在界面上操作"，没有一条让 agent 去写/读工作区文件）。
    """
    names = _registry_names()
    for gone in ("write_file", "read_file", "replace_file"):
        assert gone not in names, (
            f"{gone} 没有任何用例依赖它，却每次调用都在付费（schema 进 prompt）"
        )


def test_tools_with_real_dependents_survive_the_trim() -> None:
    """★ 反向守卫：把工具精简**做过头**会砍掉真实用例的能力。

    这三条都是对着真库检索过、确认有用例依赖的，别顺手删：

      * `upload_file` —— 38 条用例靠它把测试数据文件传进被测系统（见 app/testdata.py）。
      * `save_as_pdf` —— 检索到 94 条命中"PDF/打印"，逐条看上下文后确认它们测的是
        **被测系统自己的** PDF 功能（"打开PDF""PDF 排版""PDF A4 格式""跨部门打印"），
        而"打印/导出为"最终会走到浏览器 PDF 查看器 —— 这个工具可能是查看证据的唯一途径。
      * `extract` —— 从已渲染文本里归纳，不执行脚本，是真人会做的事。
    """
    names = _registry_names()
    for keep in ("upload_file", "save_as_pdf", "extract"):
        assert keep in names, f"{keep} 有真实用例依赖，不能为了省 token 被删掉"


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


# ---------------------------------------------------------------------------
# token 预算护栏（2026-10-09）
# ---------------------------------------------------------------------------

# 这套规则每**一步**都要重发一遍，所以它的长度直接乘在步数上。
# 实测（真网关，3 条真实用例）：每步约 15,686 token，其中
#   browser-use 自带基础提示 ≈ 6,026
#   本项目这套规则       ≈ 3,788
#   工具 schema          ≈ 1,816
#   ——— 固定开销合计 ≈ 11.6k，其余才是页面状态与历史。
#
# ★ 为什么门槛定在这里而不是"越小越好"：
# 这些规则每一条都对应一个**实测过的失败模式**（见各块上方的注释），
# 删规则 = 把那个失败模式放回来。所以护栏的目的不是逼它变短，
# 而是防止它**悄悄变长** —— 一段为维护者写的说明文字加了进去，
# 每步多付一次、全库 1000+ 条用例地付。
_MAX_RULE_CHARS = 17_000


def test_prompt_rules_dont_grow_without_a_measurement() -> None:
    """★ 提示词规则的体量护栏。

    为什么需要它：这套规则是**每步重发**的固定开销。给维护者看的解释性文字
    （"实测 44 步那条用例是把同一个动作重试了十几次…"）很有价值，但它应该写在
    **代码注释**里，而不是塞进提示词 —— 模型不需要被说服，它只需要被告知。

    2026-10-09 就清掉了一批这类文字（_ANTI_WASTE_RULE 少了约 190 字符）。
    这条测试保证它不会以同样的方式长回去：**加了长度就要在这里改数字并说明理由**，
    而"说明理由"这个动作本身就会让人先量一下值不值。
    """
    from app import executor

    blocks = {
        "_SCOPE_RULE": executor._SCOPE_RULE,
        "_TOOLKIT_RULE": executor._TOOLKIT_RULE,
        "_POPUP_RULE": executor._POPUP_RULE,
        "_ANTI_WASTE_RULE": executor._ANTI_WASTE_RULE,
        "_EFFICIENCY_RULE": executor._EFFICIENCY_RULE,
        "_ADAPT_RULE": executor._ADAPT_RULE,
        "_PERSIST_RULE": executor._PERSIST_RULE,
        "_SELF_CHECK_RULE": executor._SELF_CHECK_RULE,
    }
    total = sum(len(v or "") for v in blocks.values())
    assert total <= _MAX_RULE_CHARS, (
        f"提示词规则已涨到 {total} 字符（上限 {_MAX_RULE_CHARS}）。"
        "这套规则每步都重发一次，长度 × 步数 = 真实成本。\n"
        "新增规则前先确认它对应一个**实测过的失败模式**；"
        "如果只是想把道理讲清楚，请写进代码注释而不是提示词。\n"
        "确实需要放宽上限时，请同时更新这条测试里的数字与理由。"
    )


def test_every_rule_block_still_has_its_counter_measure() -> None:
    """压缩规则时**最怕**的是顺手删掉某条真正的约束。

    这条按"每条规则必须点出它治的那个具体毛病"来查 —— 只要关键短语还在，
    说明那块约束没被压缩掉。短语清单来自各块上方的实测注释。
    """
    from app import executor

    required = {
        # 每块至少要保留的"这条规则存在的原因"
        "_ANTI_WASTE_RULE": ["CONFIRM ONCE", "BATCH INDEPENDENT ACTIONS", "STOP THRASHING"],
        "_EFFICIENCY_RULE": ["NEVER trade evidence for steps", "smallest step count"],
        "_PERSIST_RULE": ["GIVING UP IS THE LAST OPTION", "Time is not the constraint"],
        "_SELF_CHECK_RULE": ["point at the exact thing on the page"],
        "_ADAPT_RULE": ["WHERE ADAPTATION STOPS"],
    }
    for name, needles in required.items():
        body = getattr(executor, name)
        for needle in needles:
            assert needle in body, (
                f"{name} 里的「{needle}」不见了 —— 压缩规则时把真正的约束删掉了？"
            )
