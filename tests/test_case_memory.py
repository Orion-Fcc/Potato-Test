"""操作经验记忆的测试。重点是把"绝不记结论"这条红线钉死。

为什么值得单独写一组测试：记忆这个东西，让它变味只需要一行代码 ——
比如哪天有人觉得"把上次的判定结果也告诉模型，命中率会更高"，测试会立刻拦住。
它拦住的是**测试本身失去意义**这种最隐蔽的退化。
"""
import json

from app.case_memory import (
    _ALLOWED_KEYS,
    clear_case,
    fingerprint,
    is_fresh,
    note_for_case,
    render,
    sanitize,
)


class _Case:
    def __init__(self, **kw):
        self.name = kw.get("name", "用例")
        self.prompt = kw.get("prompt", "做某事")
        self.expected = kw.get("expected", "应该出现 X")
        self.test_data = kw.get("test_data", "")
        self.start_url = kw.get("start_url", "http://x/")
        self.steps = kw.get("steps", [])


def test_memory_never_keeps_a_verdict() -> None:
    """★ 核心红线：判定结果绝不能进记忆。

    一旦记了"上次通过/失败"，下次运行就变成背答案，测试失去意义。
    这里同时验证三道闸：白名单挡键、措辞过滤挡值、全脏则整份丢弃。
    """
    raw = {
        "navigation": ["进入培训资源管理 → 资源审批配置"],
        "page_notes": ["这条用例通过了，符合预期", "列表页加载约 6-8 秒"],
        "element_notes": ["用例判定为失败", "「下一步」与「取消」相邻"],
        # 白名单之外的键，必须被丢掉
        "verdict": "passed",
        "expected_met": True,
        "conclusion": "全部通过",
        "judge_reason": "满足预期",
    }
    out = sanitize(raw)
    assert out is not None
    # 只有白名单里的键能留下
    assert set(out) <= set(_ALLOWED_KEYS), f"出现了白名单外的键：{set(out) - set(_ALLOWED_KEYS)}"
    # 判定性措辞的条目必须被逐条剔除
    assert out["page_notes"] == ["列表页加载约 6-8 秒"], "含『通过/符合预期』的条目必须丢弃"
    assert out["element_notes"] == ["「下一步」与「取消」相邻"], "含『失败』的条目必须丢弃"

    # 整份都是判定内容 → 丢弃整份，宁缺毋滥
    assert sanitize({"page_notes": ["通过了"]}) is None
    assert sanitize({"verdict": "passed"}) is None
    # 非 dict 输入也不能炸
    assert sanitize("通过了") is None
    assert sanitize(None) is None


def test_verdict_token_list_covers_common_phrasings() -> None:
    """措辞过滤要能挡住模型常用的几种说法，否则红线形同虚设。"""
    from app.case_memory import _looks_like_verdict

    for bad in (
        "这条用例通过了", "判定为失败", "符合预期", "未达成预期",
        "可能是个 bug", "断言不成立", "PASSED", "expected_met=true",
    ):
        assert _looks_like_verdict(bad), f"『{bad}』应被判定为结论性内容"
    for good in (
        "列表页加载约 6-8 秒", "「新增规则」在页面右上角",
        "点「下一步」前先确认按钮文案", "进入培训资源管理 → 资源审批配置",
    ):
        assert not _looks_like_verdict(good), f"『{good}』是正常经验，不应被误杀"


def test_memory_dies_when_the_case_is_edited() -> None:
    """用例一改，记忆必须立即作废 —— 否则拿旧经验误导新用例。"""
    mem = {"navigation": ["A → B"]}
    fp = fingerprint(_Case())
    assert is_fresh(mem, fp, fp) is True
    assert render(mem, fp, fp) != ""

    for changed in (
        _Case(prompt="换了任务描述"),
        _Case(expected="换了预期"),
        _Case(name="换了名字"),
        _Case(steps=[{"action": "点X", "expected": "Y"}]),
        _Case(start_url="http://y/"),
    ):
        fp2 = fingerprint(changed)
        assert is_fresh(mem, fp2, fp) is False, "改动用例后记忆应失效"
        assert render(mem, fp2, fp) == "", "失效的记忆不能渲染出任何东西"

    # 只在元信息（tags/priority/owner）上改动，**不该**让经验作废
    same = fingerprint(_Case())
    assert same == fp, "指纹不应包含 priority/owner/tags 这类元信息"


def test_rendered_note_forbids_substituting_memory_for_observation() -> None:
    """注入文本必须包含"以本次实际观察为准"的声明。

    只给经验而不划清界限，模型很容易把"上次看到 X"当成"这次也是 X"，
    等于绕一圈又把结论喂回去了。
    """
    mem = {"navigation": ["A → B"], "page_notes": ["加载慢"]}
    fp = fingerprint(_Case())
    text = render(mem, fp, fp)
    assert "A → B" in text and "加载慢" in text
    assert "本次运行实际观察" in text
    assert "以本次观察为准" in text


def test_note_and_clear_tolerate_bad_input() -> None:
    """读取/清空路径永不抛异常：记忆是好东西，但不能搞死用例。"""
    import asyncio

    assert asyncio.run(note_for_case(0)) == ""
    assert asyncio.run(note_for_case(-1)) == ""
    # 不存在的 id 也只是返回空/False，不抛
    assert asyncio.run(note_for_case(10**9)) == ""
    assert asyncio.run(clear_case(10**9)) is False


def test_memory_schema_is_json_serialisable() -> None:
    """记忆要能存进 JSON 列，别塞进去不可序列化的东西。"""
    out = sanitize({"navigation": ["A → B"], "page_notes": ["加载约 5 秒"]})
    json.dumps(out, ensure_ascii=False)  # 不抛即通过
