"""The assistant must be able to finish the job, not just describe it.

Measured on 2026-10-07: the user asked the built-in assistant to add a case and to deal with
a duplicate (TC-017 vs TC-018). It correctly diagnosed the duplicate and replied

    「建议：不要我删掉其中一条（建议删 TC-018，保留 TC-017），让清单更干净？」

— then stopped. The diagnosis was right; the hands were empty. The tool table had
``create_case`` and ``update_case`` (which changes six *content* fields) but nothing that
could disable or remove a case, and ``TestCase.status`` was not among the fields
``update_case`` passed through.

That failure mode is worth pinning precisely because it is invisible: the assistant still
looked competent, the reply was well-reasoned, and the user still had to do the work.

The tests below check three things:

1. the capability exists at all (a missing tool is the whole bug);
2. the *reversible* option is what the prompt steers toward, because
   ``DELETE /testcases/{id}`` cascades to ``run_result`` and destroys the history of what
   that case actually did on previous runs;
3. the guidance reaches the model as an instruction, not as a tool description — a tool
   nobody thinks to reach for does not help.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app import assistant


def _tool_names() -> set[str]:
    return {
        t["function"]["name"]
        for t in assistant._tools()
        if t.get("type") == "function"
    }


def _tool(name: str) -> dict:
    for t in assistant._tools():
        if t.get("type") == "function" and t["function"]["name"] == name:
            return t["function"]
    raise AssertionError(f"工具表里没有 {name}")


def _prompt() -> str:
    return assistant._system_prompt("catalog", "百度", 3)


# --------------------------------------------------------------- 1. capability exists


def test_the_assistant_can_disable_a_case():
    """Without this the assistant can only ever *suggest* de-duplication.

    Named test rather than a loop over a list because this is the single tool whose absence
    caused the reported complaint.
    """
    assert "set_case_status" in _tool_names()


def test_the_assistant_can_delete_a_case():
    """Permanent deletion is needed eventually — but see the steering tests below."""
    assert "delete_case" in _tool_names()


def test_set_case_status_declares_both_directions():
    """An enum of one value would make "re-enable" impossible through the tool.

    The whole reason to prefer disabling over deleting is that it can be undone, which means
    the tool has to be able to undo it too.
    """
    params = _tool("set_case_status")["parameters"]
    assert set(params["properties"]["status"]["enum"]) == {"active", "deprecated"}
    assert params["required"] == ["case_id", "status"]


def test_set_case_status_rejects_a_missing_case_id():
    """A model that omits the id should get an error, not a call to /api/testcases/None."""
    params = _tool("set_case_status")["parameters"]
    assert "case_id" in params["required"]


# ------------------------------------------------- 2. reversible is the steered default


def test_the_delete_tool_warns_that_it_is_not_reversible():
    """The description is what the model reads before choosing.

    ``DELETE`` cascades: api.delete_case removes the matching ``run_result`` rows first,
    because ``run_result.case_id`` is a non-cascading FK. So deleting is not just
    "removing a row" — it is removing the record of every run that case ever took part in.
    A model that does not know that will happily delete on a duplicate and take the history
    with it.
    """
    desc = _tool("delete_case")["description"]
    assert "永久" in desc or "不可逆" in desc
    assert "set_case_status" in desc, (
        "delete_case 的描述里必须点名 set_case_status —— "
        "只说「不可逆」模型仍可能选它，需要给出更省的替代"
    )


def test_the_prompt_prefers_disabling_over_deleting():
    """The instruction has to be in the prompt, not only in tool descriptions.

    Reasoning: the model picks a tool the same way a person picks a button — by the rule it
    was given, not by reading every tooltip. When it met a duplicate it asked permission to
    delete instead of acting, so the missing instruction was in the prompt.
    """
    p = _prompt()
    assert "停用优先于删除" in p


def test_the_prompt_tells_it_to_act_rather_than_only_advise():
    """The specific regression: it diagnosed correctly and then asked the user to do it.

    Asserting the rule exists prevents the same wording from being dropped in a later edit.
    """
    p = _prompt()
    assert "发现重复不要只给建议" in p
    assert "set_case_status" in p


def test_the_prompt_numbering_has_no_duplicates():
    """Rule numbers are load-bearing: the prompt tells the model which number to follow.

    Two rules numbered "7)" means one of them is unaddressable, and the lossy one is
    whichever the duplicate shadowed. Cheap to check, invisible when broken.

    ★ Scoped to the 工作规则 section only. The prompt has two independently-numbered lists
    — 「你的手段」 1-6 and 「工作规则」 1-13 — and the first attempt at this test flagged all
    of 1-6 as duplicates, which is exactly what a legitimately-numbered second list looks
    like. Asserting on the whole prompt would have pushed someone to "fix" a prompt that was
    correct, so the check starts at the section marker instead.
    """
    import re

    p = _prompt()
    marker = "工作规则："
    assert marker in p, "提示词结构变了：找不到『工作规则』小节，本测试需要跟着改"
    body = p.split(marker, 1)[1]

    nums = re.findall(r"(?:^|\n)\s*(\d+)\)", body)
    dupes = {n for n in nums if nums.count(n) > 1}
    assert not dupes, f"工作规则里有重复的规则编号：{sorted(dupes)}"

    # And they must be contiguous from 0 — a gap means a rule was lost in an edit.
    seen = sorted({int(n) for n in nums})
    assert seen == list(range(min(seen), min(seen) + len(seen))), (
        f"工作规则的编号不连续：{seen} —— 中间缺号说明编辑时丢了一条规则"
    )


# ------------------------------------------------------------ 3. it reaches the LLM


def test_both_tools_are_in_the_schema_sent_to_the_model():
    """Tools declared in prose but absent from the payload cannot be called at all."""
    payload = assistant._tools()
    # 直接查 payload 而不是转一手 _tool_names()：这条用例的名字说的就是
    # "发给模型的那份 schema 里有它们"，那就该拿发出去的那份断言。
    names = {t["function"]["name"] for t in payload if t.get("type") == "function"}
    assert {"set_case_status", "delete_case"} <= names
    # And they must be real function entries, not dicts with a typo'd schema.
    for name in ("set_case_status", "delete_case"):
        t = _tool(name)
        assert t["parameters"]["type"] == "object"
        assert t["description"].strip()


def test_module_parses_and_tools_are_json_serialisable():
    """Guards against the edits that broke the prompt string concatenation.

    The prompt is built from adjacent string literals; a stray quote turns the rest of the
    function into one long expression, which imports fine and then returns the wrong text.
    """
    import json

    ast.parse(inspect.getsource(assistant))
    json.dumps(assistant._tools(), ensure_ascii=False)


# ── header encoding ────────────────────────────────────────────────────────────
# Every case-editing branch puts the model's Chinese reason into an ``x-change-by`` header.
# httpx encodes header values as ASCII and raises *before sending*, so one Chinese character
# turned the entire assistant turn into HTTP 500. Two of the three branches even default to a
# Chinese string, so they failed on every call — not just when the model supplied a reason.


@pytest.mark.parametrize(
    "value",
    [
        "assistant",
        "",
        "助手停用：与 TC-001 重复",
        "助手停用/启用",  # the literal default in set_case_status
        "助手删除",  # the literal default in delete_case
        "a" * 300,
        "tab\tnewline\n",
        "emoji 😀 mixed 中文",
    ],
)
def test_a_header_value_never_breaks_the_request(value):
    """httpx must accept whatever comes out of this function."""
    import httpx

    safe = assistant._header_safe(value)
    safe.encode("ascii")  # raises if the function let something through
    httpx.Headers({"x-change-by": safe})  # raises if httpx still rejects it


def test_ascii_values_pass_through_untouched():
    """Encoding everything would make the audit log unreadable for no reason."""
    assert assistant._header_safe("assistant") == "assistant"
    assert assistant._header_safe("") == ""
    assert assistant._header_safe("digest: 3 cases") == "digest: 3 cases"


def test_the_audit_reason_survives_as_text():
    """Percent-encoding, not stripping.

    The reason is the entire reason the header exists — a ``case_change`` row that records who
    changed a case but not why is half a record. Stripping non-ASCII to make the header legal
    would produce exactly that.
    """
    from urllib.parse import unquote

    original = "助手停用：与 TC-001 重复"
    assert unquote(assistant._header_safe(original)) == original


@pytest.fixture
def case_962_is_in_project_3(monkeypatch):
    """Let the write actually reach the transport.

    Added after the cross-project guard: these tools now resolve the case's project before
    writing, and on the temporary test database id=962 does not exist — so the run returned
    "找不到用例" and never built a header. The header encoding is still what is under test, so the
    ownership lookup is stubbed rather than the test being dropped.
    """
    import contextlib

    import app.db as db

    class _Result:
        def first(self):
            return (3, "TC-002")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return _Result()

    @contextlib.asynccontextmanager
    async def _fake_db_session():
        yield _Session()

    monkeypatch.setattr(db, "db_session", _fake_db_session)


@pytest.mark.parametrize(
    "tool, args",
    [
        ("set_case_status", {"case_id": 962, "status": "deprecated"}),  # reason omitted -> Chinese default
        ("set_case_status", {"case_id": 962, "status": "deprecated", "reason": "与 TC-001 重复"}),
        ("delete_case", {"case_id": 962}),  # -> default "助手删除"
        ("delete_case", {"case_id": 962, "reason": "助手删错了，改为停用"}),
        ("update_case", {"case_id": 962, "name": "改名", "digest_signal": "3 条用例结果不一致"}),
    ],
    ids=[
        "set_case_status-默认中文理由",
        "set_case_status-模型给的中文理由",
        "delete_case-默认中文理由",
        "delete_case-模型给的中文理由",
        "update_case-中文摘要",
    ],
)
def test_every_branch_that_sets_a_chinese_default_can_still_send(tool, args, case_962_is_in_project_3):
    """The regression itself, at the layer it happened.

    Each of these branches writes ``x-change-by`` from model-supplied or Chinese-defaulted text.
    Driving the real ``_run_tool`` with such an argument must not raise. The transport is stubbed
    so this stays a unit test, but the header is built by the production code — which is the part
    that was broken.
    """
    import asyncio

    import httpx

    captured: dict = {}

    class _Probe(httpx.AsyncClient):
        async def request(self, method, url, **kw):  # type: ignore[override]
            captured.update(kw.get("headers") or {})
            # 400 rather than an exception: the point is that headers were *built*, and _api
            # must survive the response too.
            return httpx.Response(400, request=httpx.Request(method, url), text="stub")

    original = assistant.httpx.AsyncClient
    assistant.httpx.AsyncClient = _Probe
    try:
        asyncio.run(assistant._run_tool(tool, dict(args), 3))
    finally:
        assistant.httpx.AsyncClient = original

    assert captured, "请求根本没构造出来 —— 测试没测到东西"
    assert captured.get("x-change-source") == "assistant"
    # The exact assertion that used to blow up with UnicodeEncodeError:
    captured["x-change-by"].encode("ascii")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))