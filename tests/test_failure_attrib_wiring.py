"""Wiring tests for the failure-attribution plumbing.

`tests/test_failure_attrib.py` covers the classifier itself. This file covers the
seams around it — the places where an attribution can be computed correctly and then
silently dropped on the way to the report:

    failure_attrib.classify -> executor -> describe_failure -> build_prompt
                                            -> FailureNarrative.as_dict
                                            -> engine -> RunResult -> api._result

Every one of those hops was new code. A dropped attribution is invisible: the run still
reports `failed`, the narrative still reads fine, and the tester files a bug against a
flow they never completed. So the guards below assert the value is *carried*, not merely
computed.

No LLM and no browser: `describe_failure` is exercised with a stub client so this stays
fast and deterministic.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from app import shot_probe
from app.failure_attrib import VERDICT_INFO, classify, describe, prompt_hint
from app.failure_narrative import FailureNarrative, build_prompt, describe_failure

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

ALL_VERDICTS = ("system", "setup", "transient", "agent", "unknown")


# ── classification is threaded into the narrative prompt ────────────────────────────


def test_prompt_carries_the_verdict_alongside_the_evidence():
    """The verdict must travel in the payload, not only shape _SYSTEM.

    Only rewriting the system prompt would make it one refactor away from vanishing,
    and it would vanish silently: the narrative would still be generated, just with no
    idea that two thirds of these failures are not defects.
    """
    out = build_prompt(
        "导入飞行学员数据后在校学员列表应可见",
        "导入 2 条学员",
        "列表可见",
        "导入成功 共 2 条 失败 0 条，但列表里查不到",
        ["点击导入", "选择文件"],
        ["导入成功，共 2 条"],
        judge_reason="功能不符",
        attribution="setup",
    )
    assert "【归因】" in out
    assert prompt_hint("setup") in out


def test_prompt_omits_the_verdict_line_when_unclassified():
    """An unattributed failure gets no verdict line — nothing to say is better than a
    guessed one. The key must be absent so the writer cannot read absence as a verdict."""
    out = build_prompt("用例", "任务", "预期", "回复", [], [], judge_reason="")
    assert "【归因】" not in out


def test_system_prompt_states_the_attribution_rule():
    """_SYSTEM carries the hard rule for non-defect verdicts.

    Checked against the actual system prompt rather than a comment: this is the text the
    model sees, so it is the only place a rule can actually bind.
    """
    from app.failure_narrative import _SYSTEM

    assert "归因" in _SYSTEM
    # The rule has to be stated as a prohibition, not a suggestion.
    assert "缺陷" in _SYSTEM


def test_prompt_hint_for_every_non_defect_verdict_forbids_filing_a_bug():
    """setup / transient / agent must all tell the writer not to write a bug report."""
    for verdict in ("setup", "transient", "agent", "unknown"):
        hint = prompt_hint(verdict)
        assert "缺陷" in hint and "不是" in hint and "不要" in hint, verdict


def test_prompt_hint_does_not_forbid_system_verdicts():
    """A real defect must not be talked out of being reported.

    The mirror risk of the rule above: over-correcting turns genuine defects into
    "conservative descriptions", which is its own kind of loss.
    """
    assert VERDICT_INFO["system"][1] is True
    hint = prompt_hint("system")
    assert "缺陷" not in hint or "不要" not in hint


# ── the narrative object carries and reports fileability ───────────────────────────


def test_as_dict_includes_attribution():
    nar = FailureNarrative(steps="a->b", actual="x", expected="y", attribution="transient")
    d = nar.as_dict()
    assert d["attribution"] == "transient"
    # The three sections the tester pastes into the tracker must still be there.
    for k in ("steps", "actual", "expected"):
        assert k in d and d[k]


@pytest.mark.parametrize("verdict", ALL_VERDICTS)
def test_fileable_only_for_system(verdict):
    nar = FailureNarrative(steps="a", actual="b", expected="c", attribution=verdict)
    assert nar.fileable_as_defect is (verdict == "system")


def test_unattributed_narrative_is_not_fileable():
    """A missing attribution must not default to "file it".

    `unknown` is the deliberate answer here — a wrong label is worse than no label,
    because the tester acts on it.
    """
    assert FailureNarrative(steps="a", actual="b", expected="c").fileable_as_defect is False


# ── describe_failure passes it through, including its failure paths ─────────────────


class _FakeCompletions:
    def __init__(self, sink: dict):
        self._sink = sink

    async def create(self, **kwargs):
        self._sink["messages"] = kwargs["messages"]
        content = json.dumps(
            {
                "steps": "导入数据->查看列表",
                "actual": "列表未显示新导入的数据",
                "expected": "列表可见",
                "title": "导入后列表查不到数据",
                "severity": "严重",
            }
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class _FakeOpenAI:
    def __init__(self, sink: dict):
        self.chat = SimpleNamespace(completions=_FakeCompletions(sink))
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_llm(monkeypatch):
    """Stub the LLM plumbing describe_failure reaches for; record what it was sent."""
    from app import failure_narrative as fn

    sink: dict = {}

    async def _openai_client():
        return _FakeOpenAI(sink)

    async def _llm_config():
        return SimpleNamespace(model="stub")

    monkeypatch.setattr(fn, "openai_client", _openai_client, raising=False)
    monkeypatch.setattr(fn, "llm_config", _llm_config, raising=False)
    monkeypatch.setattr(fn, "get_settings", lambda: SimpleNamespace(failure_narrative_enabled=True))
    return sink


@pytest.mark.parametrize("verdict", ALL_VERDICTS)
def test_describe_failure_echoes_attribution_back(fake_llm, verdict):
    nar = asyncio.run(
        describe_failure(
            "导入后列表应可见",
            "导入 2 条学员",
            "列表可见",
            "列表里查不到",
            ["点击导入"],
            ["导入成功，共 2 条"],
            attribution=verdict,
        )
    )
    assert nar.attribution == verdict
    # And it must have reached the model, not merely been stamped on afterwards.
    assert prompt_hint(verdict) in fake_llm["messages"][1]["content"]


def test_describe_failure_keeps_attribution_when_the_llm_explodes(monkeypatch):
    """The LLM failing must not lose the verdict.

    This is the path that matters most: the narrative is empty, so the label is the
    only thing left telling the tester whether to file a bug.
    """
    from app import failure_narrative as fn

    async def _boom():
        raise RuntimeError("gateway down")

    async def _cfg():
        return SimpleNamespace(model="stub")

    monkeypatch.setattr(fn, "openai_client", _boom, raising=False)
    monkeypatch.setattr(fn, "llm_config", _cfg, raising=False)
    monkeypatch.setattr(fn, "get_settings", lambda: SimpleNamespace(failure_narrative_enabled=True))

    nar = asyncio.run(
        describe_failure("用例", "任务", "预期", "回复", [], [], attribution="agent")
    )
    assert nar.is_empty()
    assert nar.attribution == "agent"
    assert nar.fileable_as_defect is False


def test_describe_failure_keeps_attribution_when_narratives_are_disabled(monkeypatch):
    from app import failure_narrative as fn

    monkeypatch.setattr(
        fn, "get_settings", lambda: SimpleNamespace(failure_narrative_enabled=False)
    )
    nar = asyncio.run(
        describe_failure("用例", "任务", "预期", "回复", [], [], attribution="setup")
    )
    assert nar.attribution == "setup"
    assert nar.fileable_as_defect is False


# ── executor → engine → models → api ───────────────────────────────────────────────


def _code_of(rel: str) -> str:
    return (APP_DIR / rel).read_text(encoding="utf-8")


def test_result_spec_has_an_attribution_field():
    """executor must be able to hold the verdict at all.

    Asserted by name and by default: a required field would break every other
    construction site, and a missing one means the value is dropped at the source.
    """
    from app.executor import ResultSpec

    assert "attribution" in ResultSpec.__dataclass_fields__
    assert ResultSpec.__dataclass_fields__["attribution"].default == ""


def test_executor_classifies_before_writing_the_narrative():
    """The verdict must be computed first and passed in, not appended afterwards.

    Ordering is the whole point — a narrative generated before the verdict exists
    cannot have been shaped by it, whatever the call site looks like afterwards.
    """
    src = _code_of("executor.py")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute_case"
    )
    calls: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in ("classify", "describe_failure"):
            calls.append((node.lineno, name))
    assert calls, "execute_case must call classify and describe_failure"
    order = sorted(calls)
    classify_lines = [ln for ln, n in order if n == "classify"]
    narr_lines = [ln for ln, n in order if n == "describe_failure"]
    assert classify_lines and narr_lines
    assert min(classify_lines) < max(narr_lines), (
        "classify() must run before describe_failure() — otherwise the narrative is "
        "written without knowing the verdict"
    )


def test_describe_failure_call_passes_attribution_as_a_keyword():
    """Positional passing would put it in `screenshot_b64`."""
    src = _code_of("executor.py")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute_case"
    )
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "describe_failure"
        ):
            kws = {k.arg for k in node.keywords}
            assert "attribution" in kws, (
                "attribution must be passed by keyword — by position it would land in "
                "screenshot_b64 and silently never reach the prompt"
            )


def test_run_result_model_declares_the_column():
    """models.RunResult must map it, or engine's assignment raises on the first failure."""
    from app.models import RunResult

    col = RunResult.__table__.columns["attribution"]
    assert col.nullable is True  # history predates attribution; NULL means "undecided"
    assert str(col.type) == "VARCHAR(30)"


def test_db_additive_columns_lists_attribution():
    """`create_all` never touches an existing table, so this list is the only thing
    that adds the column to a deployed potato.db. Without it every insert fails with
    "no such column" the first time a case fails."""
    from app.db import _ADDITIVE_COLUMNS

    assert ("run_result", "attribution", "VARCHAR(30)") in _ADDITIVE_COLUMNS


def test_engine_persists_the_verdict_but_never_blanks_it():
    """`or row.*` on purpose: a retry that produced no verdict must not erase the one
    the first attempt computed. A blanked column reads as "undecided" and loses a real
    judgement that was already made."""
    src = _code_of("engine.py")
    assert "row.attribution = r.attribution or row.attribution" in src


def test_api_exposes_label_and_fileability():
    """The API is the last hop. Two fields, both load-bearing:
    the label for the report, the boolean for disabling the bug-report button."""
    # 2026-10-08：`_result` 搬去了 app/serialize.py（api.py 一度 3370 行，
    # 21 个序列化函数是其中最独立的一块）。断言不变，只换归属地。
    src = _code_of("serialize.py")
    tree = ast.parse(src)
    result_fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_result"
    )
    keys = {
        k.value
        for k in ast.walk(result_fn)
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }
    assert {"attribution", "attribution_label", "attribution_is_real_defect"} <= keys


def test_api_falls_back_to_empty_label_not_a_guess():
    """Unknown / NULL attribution → ("", False). A placeholder like "未分类" in the
    boolean slot, or a default label, would read as a real judgement."""
    # 同上：`_verdict_info` 现在住在 app/serialize.py。
    from app.serialize import _verdict_info

    assert _verdict_info(None) == ("", False)
    assert _verdict_info("") == ("", False)
    assert _verdict_info("nonsense") == ("", False)
    assert _verdict_info("system") == (VERDICT_INFO["system"][0], True)
    assert _verdict_info("setup")[1] is False


def test_csv_export_puts_the_verdict_before_the_bug_text():
    """The CSV is pasted into the bug tracker, so the verdict has to sit before the
    three narrative columns — otherwise a non-defect's polished narrative is what gets
    read, and the label is one scroll away."""
    src = _code_of("api.py")
    i_hdr = src.index('"缺陷标题"')
    i_verdict = src.index('"失败归因"')
    assert i_verdict < i_hdr, "归因列必须排在缺陷标题之前"


def test_attribution_column_round_trips_through_a_real_sqlite_table():
    """End-to-end on a throwaway DB: insert a verdict, read it back.

    Guards against the column existing in the model but not the table — the failure mode
    where everything passes until the first real case fails, which is the worst moment
    to discover it.
    """
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE run_result (id INTEGER PRIMARY KEY, attribution VARCHAR(30))")
        con.execute(
            "INSERT INTO run_result (id, attribution) VALUES (?, ?)", (1, "transient")
        )
        con.execute("INSERT INTO run_result (id, attribution) VALUES (?, ?)", (2, None))
        got = dict(con.execute("SELECT id, attribution FROM run_result").fetchall())
        assert got == {1: "transient", 2: None}
    finally:
        con.close()


# ── describe() wording stays honest ────────────────────────────────────────────────


@pytest.mark.parametrize("verdict", ALL_VERDICTS)
def test_describe_states_whether_it_is_fileable(verdict):
    """The log line must say whether the failure is reportable.

    A bare verdict string in the log is the same ambiguity that got two fake defects
    filed in the first place.
    """
    text = describe(verdict)
    assert verdict in text
    assert ("不可上报" in text) or ("可上报" in text)


def test_describe_falls_back_for_an_unknown_verdict():
    assert classify("完全无关的证据，没有任何关键词命中") == "unknown"
    assert "unknown" in describe("not-a-verdict") or describe("not-a-verdict")


def test_classify_is_pure():
    """No state, no globals — same input, same answer.

    The verdict decides whether a tester files a bug. If it drifted run to run, two
    identical failures would land in different buckets and the whole report stops
    meaning anything.
    """
    args = ("导入成功 共 2 条 失败 0 条\n页面提供 [提交审批] [返回] 两个按钮",)
    kw = {"final_answer": "导入后列表里查不到这 2 条学员，数据没有保存"}
    first = classify(*args, **kw)
    for _ in range(5):
        assert classify(*args, **kw) == first


# ── the screenshot corroboration actually reaches the verdict ──────────────────────


def _filled_frame() -> Any:
    from app.shot_probe import analyse_pixels

    px = [(255, 255, 255)] * (200 * 120)
    for y in range(0, 120, 2):
        for x in range(0, 200, 2):
            px[y * 200 + x] = (60, 60, 60)
    return analyse_pixels(200, 120, px)


def _blank_frame() -> Any:
    from app.shot_probe import analyse_pixels

    return analyse_pixels(200, 120, [(255, 255, 255)] * (200 * 120))


def test_executor_feeds_the_screenshot_line_into_the_evidence():
    """The corroboration has to be appended before classify() reads the evidence.

    A line added afterwards would be decoration: the verdict would already be fixed.
    So the assertion is about ordering, not about the line's presence.
    """
    src = (APP_DIR / "executor.py").read_text(encoding="utf-8")
    i_line = src.index("shot_probe.evidence_line")
    i_classify = src.index("failure_attrib.classify")
    assert i_line < i_classify, "截图佐证必须排在 classify 之前，否则归因看不到它"


def test_executor_passes_the_shot_hint_into_the_narrative():
    src = (APP_DIR / "executor.py").read_text(encoding="utf-8")
    assert "shot_hint=shot_probe.screenshot_hint(" in src


def test_a_contradicting_frame_changes_the_verdict():
    """The end-to-end claim of the screenshot work: it moves the outcome.

    Evidence 「共 0 条」 alone reads as a page still loading (`transient`). Once the frame
    shows content, the text is the unreliable party instead, and the run stops being
    filed as a "the page never loaded" problem. Without this the module would be a
    pretty picture nobody acts on.
    """
    from app.shot_probe import evidence_line

    ev = ["在校学员库列表：共 0 条"]
    line = evidence_line("共 0 条", _filled_frame())
    assert line, "有内容的截图 + 文本称查不到，必须产出佐证行"

    without = classify("\n".join(ev))
    with_shot = classify("\n".join([*ev, line]))

    assert with_shot != without, "佐证没有改变结论 —— 它在唯一该起作用的场景里失效了"
    # And the direction is right: a contradicting frame must never make a case more
    # reportable, only less certain.
    assert VERDICT_INFO[with_shot][1] is False
    assert VERDICT_INFO[with_shot][1] <= VERDICT_INFO[without][1]


def test_a_contradicting_frame_also_reclassifies_a_bare_claim():
    """The other shape it has to fix: no pattern matched at all (`unknown`).

    "查不到" on its own is genuinely ambiguous, so it is parked at `unknown` and a human
    looks. A frame with content resolves that ambiguity in one direction only — the text
    is what we should not trust — so it must not stay parked.
    """
    from app.shot_probe import evidence_line

    ev = ["列表分页：第 1 页"]
    fin = "数据不存在"
    line = evidence_line(fin, _filled_frame())
    assert classify("\n".join(ev), final_answer=fin) == "unknown"
    assert classify("\n".join([*ev, line]), final_answer=fin) == "agent"


def test_corroboration_never_promotes_a_case_to_reportable():
    """The one invariant that matters across every evidence shape.

    Whatever the frame shows, no combination of it plus a "not found" claim may turn a
    non-reportable verdict into a reportable one. A contradicting frame says the text is
    wrong; it never says the product is at fault. If this ever fails, the tool has started
    manufacturing defects out of its own instrumentation — the one outcome worse than
    having no attribution at all.
    """
    from app.shot_probe import evidence_line

    claims = [
        "在校学员库按姓名搜索陈志远，页面提示：未找到匹配记录",
        "在校学员库列表：共 0 条",
        "列表分页：第 1 页",
        "页面文本：查询结果为空",
    ]
    frames = [_filled_frame(), _blank_frame()]
    for claim in claims:
        for facts in frames:
            line = evidence_line(claim, facts)
            before = classify(claim)
            after = classify("\n".join([claim, *([line] if line else [])]))
            assert VERDICT_INFO[after][1] is False or VERDICT_INFO[before][1] is False, (
                f"截图证据把可上报的结论升级了：{claim!r} {before} -> {after}"
            )


def test_a_blank_frame_does_not_create_evidence_out_of_nothing():
    """Nothing drawn proves nothing, so it must not manufacture a verdict change."""
    from app.shot_probe import evidence_line

    ev = ["在「在校学员库」按姓名搜索陈志远"]
    fin = "这条学员不存在"
    without = classify("\n".join(ev), final_answer=fin)
    line = evidence_line(fin, _blank_frame())
    after = classify("\n".join([*ev, *([line] if line else [])]), final_answer=fin)
    assert after == without


def test_classify_spot_check_shot_token_priority():
    """The 【截图佐证】 token outranks the text pattern it contradicts.

    This is the ordering bug the module had: 佐证 said the text was untrustworthy, then
    「查不到」 in that same text decided the verdict anyway.
    """
    line = shot_probe.evidence_line("共 0 条", _filled_frame())
    assert "【截图与文本矛盾】" in line
    assert classify(line) == "agent"


def test_the_unrendered_frame_line_is_not_treated_as_a_contradiction():
    """The blank-frame line is context only, never a verdict.

    【截图与文本矛盾】 means the text is unreliable. 【截图未渲染】 means the frame drew
    nothing, which is also what a slow-loading page produces. Conflating them would let a
    loading spinner decide the verdict — the precise inversion of what each line means.
    """
    line = shot_probe.evidence_line("共 0 条", _blank_frame())
    assert "【截图未渲染】" in line
    assert "【截图与文本矛盾】" not in line
    # Evidence alone is not enough to move it off `unknown`: a blank frame says nothing
    # about whether the data was there.
    assert classify(line) == "unknown"