"""A Chinese audit reason has to survive an HTTP header and then be readable again.

The two halves live in different files and either one alone is a defect:

* ``assistant._header_safe`` percent-encodes, because httpx encodes header values as ASCII and
  raises ``UnicodeEncodeError`` **before the request is sent**. Without it, every assistant turn
  that changes a case dies with HTTP 500 — and two of the three branches default to a Chinese
  string, so they failed on *every* call.
* ``api._decode_header_label`` decodes, because otherwise the audit table stores
  ``%E4%B8%8E%20TC-001%20...`` and the operator reads a wall of escapes instead of "与 TC-001 重复".

Encoding without decoding keeps the request legal and loses the only thing the column is for.
These tests pin both ends and, more importantly, the round trip — the property that actually
matters is what the operator reads, not that each function works alone.
"""

from __future__ import annotations

import inspect
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app import api, assistant  # noqa: E402

REASONS = [
    "助手停用：与 TC-001 重复",
    "助手删除",
    "与 TC-002 同名同目的，保留更成熟的一条",
    "冒烟用例改名",
    "50% done",  # a literal '%' from a human, not an escape sequence
    "",
]


def test_the_encode_half_passes_ascii_through():
    """Encoding unconditionally would make every audit row unreadable for no gain."""
    assert assistant._header_safe("assistant") == "assistant"
    assert assistant._header_safe("digest: 3 cases") == "digest: 3 cases"
    assert assistant._header_safe("") == ""


@pytest.mark.parametrize("reason", REASONS)
def test_the_round_trip_returns_the_original_text(reason):
    """The property that matters: what the operator reads in the UI.

    Encoded on the way out (so the request survives), decoded on the way in (so the column is
    legible). Testing the two halves separately would pass while the composition was broken.
    """
    wire = assistant._header_safe(reason)
    wire.encode("ascii")  # must not raise — this is the 500
    assert api._decode_header_label(wire) == reason


def test_the_round_trip_survives_the_200_char_truncation():
    """The header is truncated to 200 chars on both ends, so test what actually gets stored.

    Cutting percent-encoded text at 200 characters can slice an escape in half (``%E4%B``),
    which is why this is worth a test rather than an assumption: ``unquote`` must not raise or
    produce mojibake when it happens.
    """
    reason = "助手停用：" + "很长的理由" * 60
    wire = assistant._header_safe(reason)[:200]
    wire.encode("ascii")
    back = api._decode_header_label(wire)
    assert back.startswith("助手停用")
    assert isinstance(back, str)


def test_a_plain_percentage_is_not_mangled():
    """``%`` is legal in a human-written label. Only *encoded* text should be decoded.

    Decoding unconditionally turns "50% done" into "50%done" (a zero-width space) — silently
    corrupting the audit trail of a change some human made by hand.
    """
    assert api._decode_header_label("50% done") == "50% done"
    assert api._decode_header_label("完成度 100%") == "完成度 100%"


def test_an_incomplete_escape_is_left_alone():
    """Truncation can produce a dangling ``%E4``. It must not raise or corrupt."""
    for bad in ("%", "%E4", "%E4%B", "abc%ZZdef"):
        out = api._decode_header_label(bad)
        assert isinstance(out, str)


def test_decode_is_a_no_op_without_a_percent():
    """The common case costs nothing."""
    assert api._decode_header_label("") == ""
    assert api._decode_header_label("wiring-check") == "wiring-check"


def test_the_two_helpers_live_where_their_comment_claims():
    """Pins the wiring: the encoder is on the send path, the decoder on the audit path.

    If someone moves ``_header_safe`` out of ``_api`` the tests above still pass — the encoder
    simply stops being called. This checks it is still on the request path, and likewise that
    ``_audit_case_change`` still decodes what it reads.
    """
    api_src = inspect.getsource(assistant._api)
    assert "_header_safe" in api_src, (
        "_api 不再清洗 header —— 中文审计理由会重新变成 HTTP 500"
    )
    audit_src = inspect.getsource(api._audit_case_change)
    assert "_decode_header_label" in audit_src, (
        "审计不再解码 —— case_change.by_label 会存成 %E4%B8%8E 这样的转义串"
    )

# ── cross-project write guard ──────────────────────────────────────────────────
# The case-editing tools address /api/testcases/{id}, which carries no project number, so the
# path-based _scope_guard cannot see them. The check has to resolve the id through the DB.
#
# Observed on 2026-10-07: asked to re-enable TC-002 in a 百度 (pid=3) conversation, the assistant
# enabled case_id=2 — a case in 培训资源管理 (pid=1) — because it guessed an id instead of
# resolving the key. It was a no-op only because that case was already active.


@pytest.mark.parametrize("tool", ["set_case_status", "delete_case", "update_case"])
def test_the_case_edit_tools_are_wired_to_the_project_guard(tool):
    """All three must consult the guard, not just the two added most recently.

    ``update_case`` is the one most likely to be missed: it predates the guard and mutates the
    content of a case, which is worse than flipping its status.
    """
    src = inspect.getsource(assistant._run_tool)
    body = src.split(f'if name == "{tool}":', 1)[1]
    # Cut at the next branch so one tool's guard cannot satisfy another's assertion.
    nxt = body.find('if name == "', 1)
    branch = body[:nxt] if nxt > 0 else body
    assert "_case_scope_error" in branch, f"{tool} 没有接项目归属校验 —— 能跨项目改用例"


@pytest.fixture
def case_952_belongs_to_project_1(monkeypatch):
    """Make id=952 look like a case in project 1, without touching any real row.

    The first version of this test asserted against id=2 in the live database. It failed with
    "找不到用例 id=2" because the suite runs against a temporary DB — and the right fix is not to
    skip the test but to stop depending on rows the test does not own. A guard protecting against
    cross-project damage is exactly the kind of test that must work on a fresh checkout.
    """
    import contextlib

    import app.db as db
    from app.models import TestCase  # noqa: F401  (imported for the real path)

    class _Result:
        def first(self):
            return (1, "TP-SOME-002")  # project_id, case_key

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
    return 952


@pytest.mark.parametrize(
    "tool, args",
    [
        ("set_case_status", {"status": "active"}),
        ("delete_case", {}),
        ("update_case", {"name": "被助手改了"}),
    ],
)
def test_a_case_from_another_project_is_refused(tool, args, case_952_belongs_to_project_1):
    """End to end through the real ``_run_tool``: id=952 belongs to project 1, chat says 3."""
    import asyncio

    res = asyncio.run(
        assistant._run_tool(tool, {"case_id": 952, **args}, 3)
    )
    assert "error" in res, f"{tool} 竟跨项目改成了：{res}"
    assert "跨项目" in res["error"]
    # The message must name both projects, or the model cannot correct itself.
    assert "1" in res["error"] and "3" in res["error"]


def test_a_case_in_the_same_project_is_allowed(
    monkeypatch, case_952_belongs_to_project_1
):
    """The other half — a guard that refuses everything is not a guard."""
    import asyncio

    import contextlib

    import app.db as db
    import httpx

    class _Result:
        def first(self):
            return (3, "TC-002")  # same project as the conversation

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

    sent: list[str] = []

    class _Probe(httpx.AsyncClient):
        async def request(self, method, url, **kw):  # type: ignore[override]
            sent.append(f"{method} {url}")
            return httpx.Response(200, request=httpx.Request(method, url),
                                  json={"id": 952, "status": "deprecated"})

    original = assistant.httpx.AsyncClient
    assistant.httpx.AsyncClient = _Probe
    try:
        res = asyncio.run(
            assistant._run_tool(
                "set_case_status", {"case_id": 952, "status": "deprecated"}, 3
            )
        )
    finally:
        assistant.httpx.AsyncClient = original

    assert sent, "同项目的用例反而被拒了 —— 守卫把合法的写也挡了"
    assert res.get("status") == "deprecated"


def test_a_missing_case_id_is_refused_before_any_lookup():
    import asyncio

    res = asyncio.run(assistant._run_tool("set_case_status", {"status": "active"}, 3))
    assert "error" in res
    assert "case_id" in res["error"]


def test_the_refusal_never_sends_a_request(case_952_belongs_to_project_1):
    """A guard that logs the refusal but still performs the write is worse than no guard.

    Asserted structurally: the guard returns before ``_api`` is reached, so the DELETE cannot have
    been sent. For a permanent delete this is the assertion that matters most — there is no undo.
    """
    import asyncio

    import httpx

    calls: list[str] = []

    class _Tripwire(httpx.AsyncClient):
        async def request(self, method, url, **kw):  # type: ignore[override]
            calls.append(f"{method} {url}")
            return httpx.Response(400, request=httpx.Request(method, url), text="stub")

    original = assistant.httpx.AsyncClient
    assistant.httpx.AsyncClient = _Tripwire
    try:
        asyncio.run(
            assistant._run_tool("delete_case", {"case_id": 952, "reason": "跨项目删除"}, 3)
        )
    finally:
        assistant.httpx.AsyncClient = original

    assert not calls, f"被拒绝的删除请求仍然发出去了：{calls}"


# ── the generic API tool is a second door ──────────────────────────────────────
# Guarding set_case_status changed nothing in practice. On the retry the model took the generic
# route and the cross-project write completed:
#     PATCH /api/testcases/2/status  -> 404, retried
#     PUT   /api/testcases/2         -> 200, done
# Both times it was a no-op only because the target already happened to be in the wanted state.


@pytest.mark.parametrize(
    "method, path",
    [
        ("PUT", "/api/testcases/952"),
        ("PATCH", "/api/testcases/952/status"),
        ("POST", "/api/testcases/952/results"),
        ("DELETE", "/api/testcases/952"),
        ("PUT", "/api/testcases/952/results"),
    ],
)
def test_a_case_write_through_the_generic_tool_is_refused(
    method, path, case_952_belongs_to_project_1
):
    import asyncio

    res = asyncio.run(
        assistant._run_tool("potato-test_api", {"method": method, "path": path}, 3)
    )
    assert "error" in res, f"通用工具的 {method} {path} 竟通过了：{res}"
    assert "跨项目" in res["error"]


def test_a_case_read_through_the_generic_tool_is_still_allowed(
    case_952_belongs_to_project_1,
):
    """Reads are not the hazard, and refusing them would break legitimate work.

    The assistant routinely needs to look up a case by id. Locking reads would just teach the
    model to route around the guard, which is how the bypass happened in the first place.
    """
    import asyncio

    import httpx

    sent: list[str] = []

    class _Probe(httpx.AsyncClient):
        async def request(self, method, url, **kw):  # type: ignore[override]
            sent.append(f"{method} {url}")
            return httpx.Response(200, request=httpx.Request(method, url), json={"ok": True})

    original = assistant.httpx.AsyncClient
    assistant.httpx.AsyncClient = _Probe
    try:
        asyncio.run(
            assistant._run_tool("potato-test_api", {"method": "GET", "path": "/api/testcases/952"}, 3)
        )
    finally:
        assistant.httpx.AsyncClient = original

    assert sent, "连读取都被拦了 —— 模型会绕过守卫而不是服从它"


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/3/testcases",  # project in the path, checked by the other regex
        "/api/runs/5",
        "/api/projects/3/failure-digest",
    ],
)
def test_paths_without_a_bare_case_id_are_not_touched_by_this_guard(path):
    """The backstop must not fire on ordinary paths, or it becomes noise and gets ignored."""
    assert assistant._case_id_in_path(path) is None


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/api/testcases/952", 952),
        ("/api/testcases/2/status", 2),
        ("/api/testcases/962/results", 962),
        ("/api/testcases/abc", None),
        ("/api/projects/3/testcases", None),
    ],
)
def test_the_case_id_is_read_out_of_the_path(path, expected):
    assert assistant._case_id_in_path(path) == expected


def test_the_generic_tool_consults_the_case_guard():
    """Structural pin: the check must live in the generic branch.

    Its absence is silent — every other test in this file would still pass with the model simply
    choosing a different tool.
    """
    src = inspect.getsource(assistant._run_tool)
    head = src.split('if name == "potato-test_api":', 1)[1]
    branch = head.split('if name == "', 1)[0]
    assert "_case_id_in_path" in branch, "通用工具没接用例归属校验 —— 第二道门是敞开的"
    assert "await _case_scope_error" in branch
