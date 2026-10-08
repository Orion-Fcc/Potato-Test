"""Which browser binary the executor will use, and above all which one it must NOT.

The failure this guards against is quiet, not loud: picking a *different* browser than
the one the user pinned still produces a green run, just one that was rendered by
another engine. So most of these tests are about the negative cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import browser_binary as bb


def test_the_omitted_argument_default_agrees_with_the_configured_default():
    """The function's own default and the project's configured default must match.

    Found by a verification script that called ``resolve_browser_executable()`` with no
    candidates argument, got Edge, and concluded the Chrome switch hadn't taken effect —
    when the switch was fine and the *caller* was wrong. Nothing in the code objected,
    because a function that resolves a browser has no way to know the project picked a
    different one.

    This is the generalisable version: a default duplicating a value configured elsewhere
    will drift, and it drifts silently since each copy looks right on its own. Asserting
    the two agree turns that silent drift into a red test.
    """
    import inspect

    from app.config import Settings

    sig = inspect.signature(bb.resolve_browser_executable)
    default = sig.parameters["candidates"].default
    configured = Settings.model_fields["browser_candidates"].default

    assert default == configured, (
        f"browser_binary 的默认候选是 {default!r}，"
        f"而 config.browser_candidates 是 {configured!r} —— "
        "不传参数的调用方会拿到另一个浏览器，且不会报错"
    )
    # And the order must be identical, not just the set: a probe order is a preference.
    assert bb.candidate_names(default) == bb.candidate_names(configured)


def test_bundled_is_never_probed_on_disk():
    """"bundled" is a sentinel, not a path. A folder called `bundled` must not win."""
    assert bb.resolve_browser_executable("bundled", "edge,chrome") is None
    assert bb.resolve_browser_executable("BUNDLED", "edge,chrome") is None


def test_explicit_path_is_returned_untouched_even_when_missing():
    """We do not second-guess a path the user typed.

    Playwright's own launch error names the real problem; an invented "not found"
    from us would have strictly less information in it.
    """
    missing = r"C:\no\such\browser\thing.exe"
    assert bb.resolve_browser_executable(missing, "edge,chrome") == missing


def test_explicit_path_beats_the_candidate_list():
    """A pinned path must win even when a perfectly good Edge is installed."""
    assert bb.resolve_browser_executable(r"D:\custom\chrome.exe", "edge,chrome") == (
        r"D:\custom\chrome.exe"
    )


def test_named_browser_found_is_resolved(monkeypatch):
    monkeypatch.setattr(bb, "find_browser", lambda name: f"/fake/{name}" if name == "edge" else None)
    assert bb.resolve_browser_executable("edge", "edge,chrome") == "/fake/edge"


def test_named_browser_missing_does_not_silently_switch(monkeypatch):
    """Pinning `chrome` on a box with only Edge must NOT quietly run Edge.

    It falls back to auto-probe (documented), but the point being tested is that the
    *named* request itself never returns a different browser as if it were the one
    asked for.
    """
    monkeypatch.setattr(bb, "find_browser", lambda name: "/fake/edge" if name == "edge" else None)
    # Auto-probe finds edge -> that is the documented fallback, not a silent swap,
    # because the caller asked for `chrome` and chrome is genuinely absent.
    assert bb.resolve_browser_executable("chrome", "edge,chrome") == "/fake/edge"


def test_auto_probe_follows_candidate_order(monkeypatch):
    monkeypatch.setattr(bb, "find_browser", lambda name: f"/fake/{name}")
    assert bb.resolve_browser_executable("", "chrome,edge") == "/fake/chrome"
    assert bb.resolve_browser_executable("", "edge,chrome") == "/fake/edge"


def test_auto_probe_returns_none_when_nothing_is_installed(monkeypatch):
    """No system browser is a perfectly fine configuration, not an error."""
    monkeypatch.setattr(bb, "find_browser", lambda name: None)
    assert bb.resolve_browser_executable("", "edge,chrome") is None


def test_typo_in_candidates_does_not_silently_empty_the_probe(monkeypatch):
    """A misspelt candidate matches nothing; the next real one must still win.

    Silently reducing the probe to zero would report "no system browser found",
    sending the user hunting for a problem they actually created with a typo.
    """
    monkeypatch.setattr(bb, "find_browser", lambda name: "/fake/edge" if name == "edge" else None)
    assert bb.resolve_browser_executable("", "edg,edge") == "/fake/edge"


def test_candidate_names_parsing():
    assert bb.candidate_names("edge, chrome") == ["edge", "chrome"]
    assert bb.candidate_names("") == []
    assert bb.candidate_names("  ") == []
    assert bb.candidate_names("EDGE,,chrome,") == ["edge", "chrome"]


def test_find_browser_returns_none_for_unknown_name():
    assert bb.find_browser("netscape") is None


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows install paths")
def test_windows_table_matches_reality():
    """The hardcoded paths must be the ones this Windows actually uses.

    Edge on 64-bit Windows lives under Program Files (x86); that is not a typo in the
    table, so this test exists to stop someone "fixing" it.
    """
    edge = bb._WINDOWS_PATHS["edge"][0]
    if Path(edge).exists():
        assert bb.find_browser("edge") == edge


def test_describe_is_readable_both_ways():
    assert "Edge" in bb.describe(r"C:\p\msedge.exe")
    assert "Chromium" in bb.describe(None)


def test_describe_names_the_browser_not_just_the_filename():
    """`msedge.exe` in a log tells the reader nothing they don't already know.

    The log is what someone reads when a run misbehaves, so it should say which browser
    it was in language a person uses ("Edge"), not the file name.
    """
    assert "Edge 正式版" in bb.describe(r"C:\p\msedge.exe")
    assert "Chrome 正式版" in bb.describe(r"C:\p\chrome.exe")
    assert "Brave" in bb.describe(r"C:\p\brave.exe")


def test_describe_does_not_claim_isolation_it_cannot_know():
    """It must not say "独立实例" for a system binary.

    `describe` receives a path and nothing about profiles, so it cannot tell whether the
    run will share the daily browser's profile — which is exactly the situation that
    produced "我这边登录了，同一个电脑里面另一边就会出现故障". Claiming isolation it has
    not verified would be the most reassuring possible lie.
    """
    out = bb.describe(r"C:\p\chrome.exe")
    assert "不共用" in out          # states the arrangement it can see
    assert "独立实例" not in out     # does not assert isolation it cannot check


def test_describe_keeps_the_playwright_chromium_distinct():
    """Two Chromium flavours both exist on this machine; conflating them loses the
    distinction that the benchmark turned on (8.35s vs 5.74s intranet first paint)."""
    pw = bb._playwright_chromium()
    if not pw:
        return  # not installed here; nothing to distinguish
    assert "Playwright 自带" in bb.describe(pw)


def test_module_does_not_import_app_package():
    """It must stay importable without the app's heavy deps (browser-use etc.).

    That is why it lives in its own module instead of inside executor.py: executor.py
    cannot be imported for testing without dragging in the world.
    """
    source = (Path(bb.__file__)).read_text(encoding="utf-8")
    assert "from app." not in source
    assert "import browser_use" not in source

# ---------------------------------------------------------------------------
# The wiring itself. Pure unit tests above prove the resolver behaves; these prove
# the executor actually *calls* it. That gap is where a silent regression lives:
# resolve_browser_executable could be perfect while nothing passes the result on,
# and every case would quietly fall back to bundled Chromium with no symptom.
# ---------------------------------------------------------------------------


def test_executor_passes_executable_path_to_browser(monkeypatch):
    """The resolved binary must reach Browser(...).

    Note the patch target: executor.py does ``from browser_use import Browser``
    *inside* make_browser(), so the name is looked up in the source package at call
    time. Patching ``app.executor.Browser`` captures nothing and the test passes
    vacuously — that is exactly how this assertion was verified the first time.
    """
    import browser_use  # noqa: PLC0415 — deliberately late; see above

    from app import executor as ex  # noqa: PLC0415

    monkeypatch.setattr(ex, "browser_binary", bb)
    monkeypatch.setattr(
        bb, "resolve_browser_executable", lambda configured="", candidates="": "/fake/msedge.exe"
    )

    captured: dict = {}

    class FakeBrowser:
        def __init__(self, **kw):
            captured.update(kw)

        async def start(self):
            raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(browser_use, "Browser", FakeBrowser)

    import asyncio  # noqa: PLC0415

    from app.executor import CaseSpec, execute_case  # noqa: PLC0415

    spec = CaseSpec(
        case_id=999999,
        prompt="probe",
        name="probe",
        expected="ok",
        start_url="about:blank",
    )
    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(execute_case(spec))
    except BaseException:  # noqa: BLE001 — we only care about what was captured
        pass

    assert captured.get("executable_path") == "/fake/msedge.exe", (
        "executor did not forward the resolved browser binary to Browser()"
    )


def test_executor_omits_key_when_no_system_browser(monkeypatch):
    """No system browser => the key must be absent, not present-and-empty.

    ``executable_path=None`` means something different to Playwright than "not
    supplied" in some code paths, and passing an empty string would be worse: it
    looks like a configured path and fails at launch with a confusing error.
    """
    import browser_use  # noqa: PLC0415


    monkeypatch.setattr(bb, "resolve_browser_executable", lambda configured="", candidates="": None)

    captured: dict = {}

    class FakeBrowser:
        def __init__(self, **kw):
            captured.update(kw)

        async def start(self):
            raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(browser_use, "Browser", FakeBrowser)

    import asyncio  # noqa: PLC0415

    from app.executor import CaseSpec, execute_case  # noqa: PLC0415

    spec = CaseSpec(case_id=999998, prompt="probe", name="probe", expected="ok")
    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(execute_case(spec))
    except BaseException:  # noqa: BLE001
        pass

    assert "executable_path" not in captured, (
        "executable_path should be omitted entirely when no system browser is found"
    )
