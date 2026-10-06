"""提示词里提到的动作必须真实存在。

这条看着琐碎，但它防的是一个真实发生过的错误：我在工具规则里写了
`get_dropdown_options`（"先读出下拉框有哪些选项"），而**这个动作在当前
browser-use 版本里根本不存在**。提示词指向不存在的动作比不写更糟 ——
模型会尝试一个工具说明里没有的能力，然后浪费时间或直接放弃。

同理，"用上 agent 的所有功能"这件事本身也依赖清单是准的：
browser-use 提供了 17 个动作，我们排除了 4 个（wait/search_page/evaluate/find_elements），
其余的**必须真的能调**，否则引导就是空谈。
"""
import re

import pytest

from app.executor import _ANTI_WASTE_RULE, _POPUP_RULE, _SCOPE_RULE, _TOOLKIT_RULE

_ALL_RULES = "\n".join([_SCOPE_RULE, _TOOLKIT_RULE, _POPUP_RULE, _ANTI_WASTE_RULE])

# 反引号包起来的标识符，就是我们让模型去调用的动作名
_NAMED = set(re.findall(r"`([a-z_]{3,30})`", _ALL_RULES))

# 本项目主动从工具集里移除的动作（见 app/config.py 的说明）
_EXCLUDED = {"wait", "search_page", "evaluate", "find_elements"}


def _real_actions() -> set[str]:
    """browser-use 实际注册的动作名。

    不能只扫 `@self.registry.action(` 那一处 —— 实测 `extract` 就注册在别处，
    只扫一种写法会误判它不存在（而误判成"不存在"会让我们把真工具从规则里删掉）。
    """
    import inspect
    import re as _re

    import browser_use.tools.service as svc

    src = inspect.getsource(svc)
    return set(_re.findall(r"@self\.registry\.action\([^)]*\)\s*\n\s*async def ([a-z_]+)", src)) | set(
        _re.findall(r"@self\.registry\.action\([^)]*\)\s*\n\s*def ([a-z_]+)", src)
    ) | set(_re.findall(r"\n\t\tasync def ([a-z_]+)\(", src))


def test_named_tools_actually_exist() -> None:
    """★ 规则里提到的每个动作都必须在 browser-use 里真实存在。"""
    real = _real_actions()
    missing = {n for n in _NAMED if n not in real}
    assert not missing, (
        f"提示词引用了不存在的动作：{sorted(missing)}。"
        f" 模型会尝试一个工具说明里没有的能力 —— 比不写更糟。"
        f" 当前真实可用：{sorted(real)}"
    )


def test_rules_do_not_ask_for_excluded_tools() -> None:
    """不能一边移除某个动作、一边又在规则里教模型用它。"""
    bad = _NAMED & _EXCLUDED
    assert not bad, f"这些动作已被移出工具集，规则里不该再提：{sorted(bad)}"


def test_toolkit_rule_covers_the_confirmed_gap() -> None:
    """工具规则必须覆盖那几个"真实存在但从没被引导"的动作。

    背景：browser-use 提供了 17 个动作，我们只引导过 click/input/scroll/navigate/send_keys/
    extract。`select_dropdown` 尤其关键 —— case 4（使用范围+状态同时选）五次运行
    pass/fail 交替，每次失败都是"第二个下拉框点开了却没选"，而 `select_dropdown`
    正是为这种控件准备的，从没被告诉过模型。
    """
    for tool in ("select_dropdown", "dropdown_options", "upload_file", "replace_file", "find_text", "go_back", "extract"):
        assert tool in _TOOLKIT_RULE, f"工具规则漏了 {tool}"


def test_toolkit_rule_cites_a_real_failure() -> None:
    """规则要给依据，不能空喊"请用专用工具"。

    每条引导都要挂得上实测现象，否则模型只会当成又一条泛泛的注意事项。
    """
    assert "case 4" in _TOOLKIT_RULE, "下拉框那条应引用实测证据（case 4 五次交替失败）"
