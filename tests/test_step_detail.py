"""步骤时间线的「人话」摘要（detail 字段）。

背景：测试员的原话是「我不想看到 ai 的思考过程，我只需要每一步点击了啥，干了啥」。
隐藏 thought 只是做完一半 —— 把 thought 藏掉之后，步骤行剩下的是裸工具名
`click_element_by_index`，那同样回答不了"点了啥"。所以每一步必须带关键参数。

这里钉住三件事：
1. detail 长得像一句话（工具名 + 关键参数），不是工具名、也不是整坨 JSON。
2. 长值必须截断。URL、整段输入文本不截断会把 80 步的时间线撑成一堵墙，
   正好把"只看操作"这个目的推翻。
3. 模型输出脏数据（参数不是 dict、model_dump 抛异常）时不能炸。
   执行路径上这一步在浏览器运行时里，抛异常等于整条用例白跑。

python -m pytest tests/test_step_detail.py
"""

from __future__ import annotations

from app.executor import _action_detail, _build_diagnostics, _summarize_actions


class _Action:
    def __init__(self, dumped: dict) -> None:
        self._dumped = dumped

    def model_dump(self, exclude_none: bool = True) -> dict:  # noqa: ARG002
        return self._dumped


class _BoomAction:
    def model_dump(self, exclude_none: bool = True) -> dict:  # noqa: ARG002
        raise RuntimeError("pydantic went away")


class _Output:
    def __init__(self, actions: list) -> None:
        self.thinking = "我先想想"
        self.next_goal = None
        self.action = actions


def test_detail_shows_the_target_not_just_the_verb() -> None:
    """`click_element_by_index` alone tells the tester nothing; index=12 does."""
    out = _action_detail({"click_element_by_index": {"index": 12}})

    assert out == "click_element_by_index(index=12)", out


def test_detail_truncates_long_values() -> None:
    """A 500-char input string would otherwise eat the whole timeline row."""
    out = _action_detail({"input_text": {"index": 3, "text": "哈" * 200}})

    assert out.startswith("input_text(index=3, text=")
    assert len(out) <= 160, len(out)
    assert "…" in out, "truncation must be visible in the UI"


def test_detail_caps_how_many_args_are_shown() -> None:
    out = _action_detail({"go_to_url": {"url": "http://x", "a": 1, "b": 2, "c": 3, "d": 4}})

    assert out.count("=") <= 3, out


def test_detail_tolerates_non_dict_params() -> None:
    """Older/odd action shapes dump a bare value; must not crash into `TypeError`."""
    assert _action_detail({"done": "x"}) == "done"


def test_summarize_returns_names_and_details() -> None:
    names, details = _summarize_actions(
        _Output([_Action({"click_element_by_index": {"index": 5}}), _Action({"wait": {"seconds": 2}})])
    )

    assert names == ["click_element_by_index", "wait"]
    assert details == ["click_element_by_index(index=5)", "wait(seconds=2)"]


def test_summarize_survives_a_broken_action() -> None:
    """Runs happen inside a live browser; a parse hiccup must not kill the case."""
    names, details = _summarize_actions(_Output([_BoomAction()]))

    assert names == []
    assert details == []


def test_summarize_handles_a_missing_action_list() -> None:
    assert _summarize_actions(None) == ([], [])


def _history(actions: list):
    class _Res:
        extracted_content = "点了保存"
        error = None

    class _Item:
        def __init__(self, mo: object) -> None:
            self.model_output = mo
            self.result = [_Res()]

    class _Hist:
        def __init__(self) -> None:
            self.history = [_Item(_Output(actions))]

        def screenshot_paths(self) -> list[str]:
            return []

    return _Hist()


def test_diagnostics_carry_the_detail_through() -> None:
    steps = _build_diagnostics(_history([_Action({"click_element_by_index": {"index": 7}})]))

    assert steps[0]["detail"] == "click_element_by_index(index=7)"
    # thought 仍然落库 —— UI 只是默认不显示，失败叙述和排查还要用它。
    assert steps[0]["thought"] == "我先想想"
