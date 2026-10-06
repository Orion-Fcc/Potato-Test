"""Runnable checks for the per-step evidence wiring on a failed case.

Three defects this file exists to keep fixed, all found in the live DB on 2026-10-02:

1. **The result text was shifted by one step.** `execute_case` merged the browser's
   read-backs into the live timeline with `hist[i]`, where `i` was the enumerate index of
   `live_steps`. The two lists are not 0-aligned: the step callback gets a 1-BASED
   `step_no` (browser-use's `state.n_steps` starts at 1), while `_build_diagnostics`
   numbers its own entries `i + 1` over a 0-based `history.history`. In run_result #142
   the damage was visible: step 21 read `action=find_elements` with
   `result=Clicked button "保存规则"` — the PREVIOUS step's text. The failure narrative is
   fed this evidence, so 【操作步骤】/【实际结果】 were written from a log that was one step
   early.

2. **The two lists differ in length, so index pairing was never safe.** #142 stored
   `steps=51` (from `history.action_names()`) but `diagnostics=36`, and the timeline's
   own step numbers skip 7/30/38. Position-based pairing cannot survive a skip.

3. **The narrative was handed bare framework verbs.** `res.steps` is
   `['navigate','click','wait','input','evaluate']` — nothing to build 【操作步骤】 from.
   The business language ("进入资源审批配置") lives in the timeline's result text.

python -m pytest tests/test_failure_evidence.py
"""

from __future__ import annotations

import ast
import inspect
import re

from app.executor import _build_diagnostics, execute_case
from app.failure_narrative import build_prompt


def _make_history(entries: list[tuple[str, str]]) -> object:
    """A minimal stand-in for AgentHistoryList: [(action_name, read_back), ...].

    Deliberately 0-based, exactly like the real `history.history`.
    """

    class _Res:
        def __init__(self, content: str) -> None:
            self.extracted_content = content
            self.error = None

    class _Action:
        def __init__(self, name: str) -> None:
            self._name = name

        def model_dump(self, exclude_none: bool = True) -> dict:
            return {self._name: "x"}

    class _ModelOutput:
        def __init__(self, name: str) -> None:
            self.thinking = f"think-{name}"
            self.next_goal = None
            self.action = [_Action(name)]

    class _Item:
        def __init__(self, name: str, read_back: str) -> None:
            self.model_output = _ModelOutput(name)
            self.result = [_Res(read_back)] if read_back else []

    class _History:
        def __init__(self) -> None:
            self.history = [_Item(n, r) for n, r in entries]

        def screenshot_paths(self) -> list[str]:
            return []

    return _History()


def test_build_diagnostics_numbers_steps_from_one() -> None:
    """The live callback is 1-based, so this must be too — that is what makes keying work."""
    steps = _build_diagnostics(_make_history([("click", "a"), ("input", "b")]))

    assert [s["i"] for s in steps] == [1, 2], "step numbers must be 1-based to match n_steps"


def test_result_text_is_merged_by_step_number_not_list_position() -> None:
    """The regression: a skipped step must not shift every later result by one.

    Reproduces #142's shape — the live timeline is missing a step (7) — and asserts the
    merge still pairs each read-back with the step it actually came from.
    """
    # The browser's history is complete (4 steps); the live timeline lost its 3rd.
    hist = _build_diagnostics(
        _make_history([("click", "r1"), ("input", "r2"), ("wait", "r3"), ("done", "r4")])
    )
    live_steps = [
        {"i": 1, "action": "click", "result": "", "error": "", "screenshot": None},
        {"i": 2, "action": "input", "result": "", "error": "", "screenshot": None},
        # step 3 missing — the skipped step
        {"i": 4, "action": "done", "result": "", "error": "", "screenshot": None},
    ]

    # The fixed merge, mirrored from execute_case.
    by_step = {d.get("i"): d for d in hist if isinstance(d, dict)}
    for ls in live_steps:
        src = by_step.get(ls.get("i"))
        if src:
            ls["result"] = src.get("result", "")

    assert live_steps[0]["result"] == "r1"
    assert live_steps[1]["result"] == "r2"
    assert live_steps[2]["result"] == "r4", (
        "step 4 must get r4. Under the old enumerate pairing it got r3 — the skipped "
        "step shifted every later result one position early."
    )


def test_old_enumerate_pairing_would_have_misaligned() -> None:
    """Guards the shape of the bug: proves position-pairing is wrong for this data."""
    hist = _build_diagnostics(
        _make_history([("click", "r1"), ("input", "r2"), ("wait", "r3"), ("done", "r4")])
    )
    live_steps = [{"i": 1}, {"i": 2}, {"i": 4}]

    # What the old code did.
    for i, ls in enumerate(live_steps):
        if i < len(hist):
            ls["result"] = hist[i].get("result", "")

    assert live_steps[2]["result"] == "r3", "documents the wrong behaviour the fix removes"
    assert live_steps[2]["result"] != "r4", "…and it is genuinely different from correct"


def test_execute_case_merges_by_step_number() -> None:
    """The source itself must key on the step number, not an enumerate index.

    A behavioural test needs a browser; this asserts the invariant directly so a
    well-meaning refactor back to `enumerate` fails here instead of in production.
    """
    src = inspect.getsource(execute_case)

    assert "by_step" in src, "the merge must build a step-number index"
    assert re.search(r"by_step\.get\(", src), "the merge must look results up by step number"
    assert "enumerate(live_steps)" not in src, (
        "enumerate(live_steps) is the off-by-one: live_steps is 1-based and the history is "
        "0-based, so the index cannot be used to address history"
    )


def test_narrative_gets_step_numbered_business_evidence() -> None:
    """`res.steps` alone is bare verbs; the narrative needs the read-backs with step numbers."""
    src = inspect.getsource(execute_case)

    assert "narrative_actions" in src, "the narrative must be fed assembled evidence"
    assert "第{" in src or "第%d步" in src, (
        "each line should carry its step number so the model cannot reorder or skip steps"
    )

    # And the prompt must tell the model what the 【】 marker means.
    payload = build_prompt(
        case_name="c",
        task="t",
        expected="e",
        final_answer="f",
        actions=["第1步: 【click】Clicked button \"保存规则\""],
        evidence=["Clicked button \"保存规则\""],
    )
    assert "保存规则" in payload, "the UI text must survive into the prompt"


def test_narrative_prompt_forbids_framework_vocabulary() -> None:
    """The 【】 action names are for classification only, never to be printed."""
    from app.failure_narrative import _SYSTEM

    assert "->" in _SYSTEM, "the step format is a `->` breadcrumb"
    assert "业务语言" in _SYSTEM, "steps must be business language, not browser language"
    assert "不要把这个词写进结果" in _SYSTEM or "不要把这个词写进" in _SYSTEM, (
        "the prompt must forbid leaking 【click】/【input】 into the output"
    )


def test_failure_narrative_module_has_no_syntax_drift() -> None:
    """Cheap smoke: the writer stays importable and its contract intact."""
    import app.failure_narrative as fn

    for field in ("steps", "actual", "expected", "title", "severity"):
        assert field in fn.FailureNarrative.__dataclass_fields__, f"{field} is part of the contract"

    ast.parse(inspect.getsource(fn))


def test_casespec_exposes_a_name() -> None:
    """`CaseSpec` 必须带用例名 —— 这个字段曾经根本不存在。

    实测（2026-10-02）：executor 里写着 `case_name=spec.name`，而 CaseSpec 没有 name 字段，
    于是每遇到失败用例就抛 AttributeError，被 `except` 吞掉后只留下一行
    「失败用例描述生成异常」，**缺陷描述功能一直是坏的却没人发现**。
    这类"异常被兜底吞掉"的 bug 不会让测试变红，只能靠显式断言字段存在来防。
    """
    from app.executor import CaseSpec

    assert "name" in CaseSpec.__dataclass_fields__, (
        "CaseSpec 丢了 name：describe_failure(case_name=spec.name) 会抛 AttributeError，"
        "而它被 except 捕获，缺陷描述会静默失效"
    )
    spec = CaseSpec(case_id=7, prompt="p", name="资源审批配置-新建规则")
    assert spec.name == "资源审批配置-新建规则"
    assert spec.name or f"case{spec.case_id}" == "资源审批配置-新建规则"


def test_name_falls_back_when_absent() -> None:
    """没传 name 时不能变成空标题，取值处必须有 case<id> 兜底。"""
    from app.executor import CaseSpec

    spec = CaseSpec(case_id=8, prompt="p")
    assert spec.name == "", "默认应是空串，便于用 `or` 兜底"
    assert (spec.name or f"case{spec.case_id}") == "case8"


def test_execute_case_guards_the_name_lookup() -> None:
    """取值处必须带兜底，避免再次把空名字当标题传给缺陷描述。"""
    import inspect

    from app.executor import execute_case

    src = inspect.getsource(execute_case)
    assert "spec.name or" in src, (
        "case_name 必须写成 `spec.name or f\"case{spec.case_id}\"`，"
        "否则库里没名字的用例会得到空标题"
    )


def test_browser_reuse_awaits_page_apis() -> None:
    """复用浏览器的重置逻辑必须 await 页面 API —— 它们全是协程。

    实测教训（2026-10-02）：`get_pages()` / `get_current_page()` 是 async，
    漏掉 await 时拿到的是 coroutine 对象：`if current is None` 永远为假，
    紧接着 `coroutine.goto(...)` 抛 AttributeError，于是重置次次失败、
    **复用静默失效**（退回一用例一浏览器，每用例多花约 6.9s 冷启动）。
    这类"漏 await"不会让测试变红，只能靠源码断言防。
    """
    import inspect

    from app.executor import _reset_browser_for_reuse

    src = inspect.getsource(_reset_browser_for_reuse)
    assert "await browser.get_pages()" in src, "get_pages() 是协程，必须 await"
    assert "await browser.get_current_page()" in src, "get_current_page() 是协程，必须 await"
    assert "browser.get_pages()\n" not in src.replace("await browser.get_pages()", ""), (
        "不应存在未 await 的 get_pages() 调用"
    )


def test_browser_reuse_is_pooled_by_profile_dir() -> None:
    """池的 key 必须是 profile 目录 —— 那是"项目 + 并发槽位"的天然隔离边界。"""
    import inspect

    from app.executor import _acquire_browser, execute_case

    assert "_BROWSER_POOL" in inspect.getsource(_acquire_browser)
    src = inspect.getsource(execute_case)
    assert "lease.path if" in src and "browser_reuse" in src, (
        "池 key 应取 lease.path 且受 browser_reuse 开关控制"
    )
    # 关闭条件必须看"有没有进池"(_pool_key)，不能看"这一轮有没有复用"(reused)。
    # 写成 `not reused` 时：新建那轮 reused=False → 刚入池就被关掉 → 下一轮从池里
    # 拿到死对象 → "CDP client not initialized" → 池里永远是死的，复用从未生效
    # （实测 139 次重置失败）。这条断言就是防这个的。
    assert "not _pool_key" in src, (
        "关闭浏览器必须写成 `not _pool_key`；用 `not reused` 会把刚入池的浏览器当场关掉"
    )


def test_video_can_be_disabled_without_waiting() -> None:
    """关掉录像时必须跳过 _wait_for_video，否则每用例白等空目录的 4 秒。"""
    import inspect

    from app.executor import execute_case

    src = inspect.getsource(execute_case)
    assert "if _record_video else None" in src, (
        "应写成 `await _wait_for_video(...) if _record_video else None`"
    )
    assert "_record_video = (" in src and "spec.record_video" in src, (
        "_record_video 必须由 spec 优先解析，才能让用户在界面上控制录像"
    )


def test_live_shot_every_zero_really_disables_shots() -> None:
    """`live_shot_every=0` 必须真的不截图。

    踩过的坑：条件原来写成 `step_no % max(1, s.live_shot_every) == 0`，
    那个 `max(1, ·)` 把 0 变成了 1 —— **设 0 反而每步都截**，开关做成了反效果。
    速度优先时用户会靠这个开关关掉逐帧截图，所以它必须真的有效。
    """
    import inspect

    from app.executor import execute_case

    src = inspect.getsource(execute_case)
    assert "_shot_every > 0 and" in src, (
        "必须写成 `if _shot_every > 0 and step_no % _shot_every == 0:`；"
        "用 max(1, ·) 会让 0 变成『每步都截』"
    )
    # _shot_every 是"项目设置优先、否则全局默认"解析后的结果（见 execute_case 开头）
    assert "_shot_every = spec.shot_every" in src, (
        "_shot_every 必须由 spec 优先解析，才能让用户在界面上控制截图"
    )


def test_per_step_screenshots_are_patched_off() -> None:
    """browser-use 在 agent/service.py 里写死了每步 include_screenshot=True，必须被掐掉。

    profiler 证明每步耗时热点就是截图链
    （screenshot_watchdog.on_ScreenshotEvent → Page.captureScreenshot → await future），
    而每步 11.3s 里 DOM(0.3s)+LLM(0.5s) 只占 0.8s。
    """
    import app.executor as E  # noqa: F401 — 导入即安装 patch
    from browser_use.browser.session import BrowserSession

    fn = BrowserSession.get_browser_state_summary
    assert getattr(fn, "_tp_no_screenshot", False), (
        "get_browser_state_summary 应被替换成不带截图的版本（见 _disable_per_step_screenshots）"
    )


def test_speed_first_knobs_are_configured() -> None:
    """速度优先档位的几个开关。改动它们要有意识地改，别被顺手回退。"""
    from app.config import get_settings

    s = get_settings()
    # 证据采集：**默认全开**（用户要求"默认都要视频和截图"）。
    # 关于速度：在真实服务链路上做 A/B 测不出截图/录像的代价（总体 全开 61.1s vs
    # 全关 68.7s，反而全开更快），而直接调执行器那轮却测出 +26.6s —— 两次相反，
    # 说明被测页面加载波动盖过了这点开销。既然测不出明显代价，就不牺牲回放证据。
    assert s.case_record_video is True, "录像默认开启"
    assert s.live_shot_every >= 1, "逐步截图默认开启（1 = 每步）"
    assert s.browser_reuse is False, (
        "浏览器复用**默认关闭**：实测它与 browser-use 的 session/事件总线生命周期冲突，"
        "池里实例全判为不可用 → 丢弃重开 → 重开要 50+ 秒，比不复用的 6 秒更慢。"
        "代码保留但默认走稳妥路径，详见 config.py 的说明。"
    )
    # 批处理：允许"同一区域的连续操作"（2 个），**但点击永远独占一步**。
    # 这条不靠 prompt 自律，而是 `_install_safe_batching()` 的确定性保证 ——
    # 用户的底线是"不想点偏"（browser-use 自带的保护只比 URL 和焦点，
    # 点击弹出弹窗时两者都不变、检测不到，那正是"想点下一步却触发取消"的成因）。
    assert s.max_actions_per_step >= 2, (
        "允许批处理是为了减步数（真人填表也会连着填几个框）"
    )
    for script_tool in ("evaluate", "find_elements"):
        assert script_tool in s.excluded_agent_actions, (
            f"{script_tool} 是脚本捷径，真人做不到，必须从工具集里移除"
        )


def _fake_action(name: str):
    class _A:
        def __init__(self, n):
            self._n = n

        def model_dump(self, exclude_unset=True):  # noqa: ARG002
            return {self._n: {}}

    return _A(name)


def test_safe_batching_never_clicks_twice() -> None:
    """"一批里永不点两次"是硬保证，必须逐种组合钉死。

    用户的底线：**不想点偏**。browser-use 的 `multi_act` 只比较 URL 与焦点，
    点击弹出弹窗时两者都不变、检测不到 → 后续动作会落在挪位后的元素上。
    所以截断必须由代码保证，不能指望模型每次都听话。
    """
    from app.executor import _SAFE_TO_BATCH, _action_name, _truncate_for_safety

    cases = [
        # (输入, 期望输出, 是否发生截断)
        (["input", "input", "click", "click"], ["input", "input", "click"], True),
        (["click", "click"], ["click"], True),
        (["click", "input"], ["click"], True),
        (["input", "click", "input"], ["input", "click"], True),
        (["input", "input"], ["input", "input"], False),
        (["send_keys", "send_keys", "send_keys"], ["send_keys"] * 3, False),
        (["scroll", "click"], ["scroll", "click"], False),
        (["click"], ["click"], False),
    ]
    for names, want, want_trunc in cases:
        got, trunc = _truncate_for_safety([_fake_action(n) for n in names])
        got_names = [_action_name(x) for x in got]
        assert got_names == want, f"{names} → {got_names}，期望 {want}"
        assert trunc == want_trunc, f"{names} 的截断标志应为 {want_trunc}"

    # 核心不变量：任何输入组合下，输出里"会改页面的动作"至多一个
    for names in (
        ["click"] * 5,
        ["input"] * 3 + ["click"] * 3,
        ["click", "input", "click", "input"],
        ["scroll", "scroll", "click", "click", "click"],
    ):
        got, _ = _truncate_for_safety([_fake_action(n) for n in names])
        changing = [n for n in (_action_name(x) for x in got) if n not in _SAFE_TO_BATCH]
        assert len(changing) <= 1, f"{names} 截断后仍有 {len(changing)} 个改页面的动作"


def test_evidence_toggles_are_tri_state() -> None:
    """截图/录像开关是**三态**：没传=不改，传 null=跟随默认，传值=明确设置。

    这里最容易踩的坑是 `model_dump(exclude_none=True)`：它会顺手把 null 丢掉，
    于是"恢复默认"这个操作永远表达不出来（点了没反应）。所以后端改用
    `model_fields_set` 判断"调用方到底有没有传这个键"。
    """
    from app.schemas import ProjectPatch

    # 没传 -> 完全不动
    only_name = ProjectPatch(name="x")
    assert "case_record_video" not in only_name.model_fields_set
    assert "live_shot_every" not in only_name.model_fields_set

    # 显式传 null -> 必须被识别为"要改（改回默认）"
    explicit_null = ProjectPatch(case_record_video=None, live_shot_every=None)
    assert "case_record_video" in explicit_null.model_fields_set, (
        "显式 null 必须进 model_fields_set，否则『恢复默认』表达不出来"
    )
    assert "live_shot_every" in explicit_null.model_fields_set

    # false / 0 不能被 exclude_none 丢掉
    d = ProjectPatch(case_record_video=False, live_shot_every=0).model_dump(exclude_none=True)
    assert d["case_record_video"] is False, "关录像要能存下去"
    assert d["live_shot_every"] == 0, "不截图（0）要能存下去"


def test_evidence_toggles_project_overrides_global() -> None:
    """取值优先级：项目设过就用项目的，没设才回退全局默认。"""
    from app.executor import CaseSpec

    fallback = CaseSpec(case_id=1, prompt="p")
    assert fallback.record_video is None and fallback.shot_every is None

    custom = CaseSpec(case_id=2, prompt="p", record_video=False, shot_every=0)
    assert custom.record_video is False and custom.shot_every == 0

    # 复刻 execute_case 里的取值表达式
    def resolve(spec, gv, gs):
        return (
            spec.record_video if spec.record_video is not None else gv,
            spec.shot_every if spec.shot_every is not None else gs,
        )

    assert resolve(fallback, True, 1) == (True, 1), "未设时用全局默认"
    assert resolve(custom, True, 1) == (False, 0), "项目设置要覆盖全局"


def test_project_model_has_evidence_columns() -> None:
    """数据列必须存在，且默认 None（= 跟随默认），这样老项目升级后行为不变。"""
    from app.models import Project

    for col in ("case_record_video", "live_shot_every"):
        assert col in Project.__table__.columns, f"project 表缺少 {col}"
        assert Project.__table__.columns[col].nullable is True, (
            f"{col} 必须可空：None 表示该项目没设过，跟随服务器默认"
        )


def test_anti_thrash_rule_and_hard_stop() -> None:
    """卡死重试必须两头堵：prompt 里写规则 + 代码里有硬停止。

    实测依据：本项目最贵的两个用例（44 步、55 步）各自只是**一个交互被重试了十几次**，
    而且那些重试一次都没成功。仅靠 prompt 不够（模型不总会听），所以还要用
    browser-use 的 register_should_stop_callback 做代码层兜底。
    """
    import inspect

    from app.executor import _ANTI_WASTE_RULE, execute_case

    assert "STOP THRASHING" in _ANTI_WASTE_RULE, (
        "prompt 里必须有『同一交互失败两次就停』这一条"
    )

    src = inspect.getsource(execute_case)
    assert "register_should_stop_callback=_should_stop" in src, (
        "必须注册停止回调：光靠 prompt 挡不住死磕"
    )
    assert "_THRASH_LIMIT" in src, "要有明确的重复次数阈值"
    # 阈值不能太小（正常流程会连着做同类动作），也不能太大（那就白设了）
    import re

    m = re.search(r"_THRASH_LIMIT = (\d+)", src)
    assert m, "阈值应当是可读的常量"
    limit = int(m.group(1))
    assert 3 <= limit <= 6, f"阈值 {limit} 不合理：太小会误停，太大等于没设"


def test_thrash_detection_does_not_over_trigger() -> None:
    """"死磕"判定必须**既不误伤正常流程、又真能抓到卡死**。

    这两边都会出错，而且都不会让测试变红：
      * 判太松 -> 44/55 步那种用例照样跑满预算
      * 判太严 -> 正常用例被中途掐断，结果全错
    所以把各种情形都钉在这里。
    """
    from app.executor import _looks_like_thrashing as t

    def s(a, e="", r=""):
        return {"action": a, "error": e, "result": r}

    # --- 应当判定为死磕 ---
    assert t([s("click")] * 4, 4)[0] is True, "连续 4 步同一动作是死磕"
    assert t([s("click", "btn disabled")] * 4, 4)[0] is True, "同动作同错误是死磕"

    # --- 不应判定为死磕 ---
    assert t([s("click")] * 3, 4)[0] is False, "步数不够阈值不判"
    assert t([s("done")] * 4, 4)[0] is False, "done 是正常收尾"
    assert t([], 4)[0] is False, "空历史不判"
    # 动作相同但反馈在变 = 还在探索，不是卡死
    varying = [s("click", "", x) for x in ("a", "b", "c", "d")]
    assert t(varying, 4)[0] is False, "结果在变说明有进展"
    # 批处理的步动作名本来就不同
    batch = [s("click"), s("click, click"), s("input"), s("navigate")]
    assert t(batch, 4)[0] is False, "批处理的不同动作不是死磕"

    # 阈值参数本身要能生效
    assert t([s("click")] * 3, 3)[0] is True, "阈值 3 时应判"
    assert t([s("click")] * 3, 0)[0] is False, "阈值 0 视为关闭"
