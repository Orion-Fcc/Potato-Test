r"""Which browser binary actually runs the cases.

Motivation
----------
browser-use ships its own Chromium and launches that by default. The machine already
has a real Edge/Chrome, and using the one a human would use has two concrete benefits:
one less few-hundred-MB download, and a rendering engine that matches what the tester
sees by hand (same engine = the evidence in the report is the same pixels they would
have got).

Measured on this machine, browser-use 0.13.10 + system Edge: **1.4s** to launch and a
full navigate/read/decide loop. The bundled Chromium was 6-7s.

Why a separate module
---------------------
`app/executor.py` is the file most likely to trigger approval prompts when edited, and
this logic is exactly the kind that must be unit-testable without a browser. Keep it
here: pure functions, no imports from `app.*`.

Contract
--------
:func:`resolve_browser_executable` returns a **path or None**, never raises:

- ``None`` means "no system browser found, let browser-use use its bundled Chromium"
  — which is a perfectly working configuration, so it must NOT be an error.
- An explicit path in settings is returned as-is **without** checking existence.
  Playwright's own error names the exact problem; our invented "not found" would only
  be a guess with less information.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("potato-test.browser")

# Where each candidate lives on Windows. Checked per-arch because Edge on 64-bit
# Windows is still installed under Program Files (x86) — that is not a mistake in
# this table, it is what Microsoft actually ships.
_WINDOWS_PATHS: dict[str, tuple[str, ...]] = {
    "edge": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
    "chrome": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
    "brave": (
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ),
}

# Linux/macOS locations, same keys. Checked after the Windows ones so a Windows box
# never touches them (and vice versa).
_POSIX_PATHS: dict[str, tuple[str, ...]] = {
    "chrome": (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ),
    "brave": ("/usr/bin/brave-browser",),
}


def _playwright_chromium() -> str | None:
    """Locate the Chromium that Playwright already downloaded, if any.

    Why this exists
    ---------------
    Playwright installs its own Chromium under ``%LOCALAPPDATA%\\ms-playwright``
    (``chromium-<rev>/chrome-win64/chrome.exe`` on Windows). We **do not download it** —
    we only ask Playwright where it put the one it already has, via its own resolver.
    Asking beats globbing: the revision number changes with every Playwright upgrade
    (``chromium-1243`` today, something else after the next one), and a hand-written
    glob would silently resolve to a stale or half-removed directory. It is also the
    exact binary ``p.chromium.executable_path`` points at, so there is zero chance of
    the config naming one build while Playwright launches another.

    Why someone would *want* this rather than their daily browser
    -----------------------------------------------------------
    Measured on this machine, launching the bundled Chromium takes **0.63s** against
    Edge's 1.4s, and — more importantly — it is a completely separate browser with its
    own profile directory. So a run can be going on in it while the human keeps using
    Edge normally: two independent processes, two independent profiles, no shared
    state, nothing for either side to trip over.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
        return path if path and Path(path).is_file() else None
    except Exception as exc:  # noqa: BLE001 — a missing/odd Playwright is not an error
        log.debug("browser: 问 Playwright 要 Chromium 路径失败（%s）", exc)
        return None


# The sentinel that means "give up on a system browser, use the bundled Chromium".
# It is not a path and must never be probed on disk — a folder named "bundled" would
# otherwise be picked up on some machine.
BUNDLED = "bundled"

# Names that are resolved by asking a library rather than by probing a fixed path.
# Kept as a set (not a prefix match) so a future name like "chromium-nightly" does not
# accidentally get routed into the Playwright branch.
_RESOLVERS = {"playwright", "chromium"}


def candidate_names(raw: str) -> list[str]:
    """Turn the comma-separated config value into an ordered list of names.

    Unknown names are kept rather than dropped: if a user types ``BROWSER_CANDIDATES
    =edg``, silently reducing the probe to zero candidates would produce a confusing
    "no system browser found" message when the real issue is a typo. They simply match
    nothing and the next candidate wins.
    """
    return [part.strip().lower() for part in (raw or "").split(",") if part.strip()]


def _paths_for(name: str) -> tuple[str, ...]:
    if name in _RESOLVERS:
        return ()
    if sys.platform.startswith("win"):
        return _WINDOWS_PATHS.get(name, ())
    return _POSIX_PATHS.get(name, ())


def find_browser(name: str) -> str | None:
    """Absolute path of the first existing binary for ``name``, else None."""
    if name in _RESOLVERS:
        found = _playwright_chromium()
        if found:
            log.info("browser: 使用 Playwright 自带 Chromium → %s", found)
        return found
    for raw in _paths_for(name):
        # `EGO_LINUX_CHROME`-style overrides are honoured: an explicit env var beats
        # guessing, which is the same precedence rule the rest of the app follows.
        if name == "edge" and (env := os.environ.get("POTATO_EDGE_PATH")):
            if Path(env).is_file():
                return env
        if name == "chrome" and (env := os.environ.get("POTATO_CHROME_PATH")):
            if Path(env).is_file():
                return env
        if Path(raw).is_file():
            return raw
    return None


def resolve_browser_executable(
    configured: str = "", candidates: str = "chrome"
) -> str | None:
    """Decide which binary a case should run in.

    ``configured`` follows the contract documented in ``app/config.py``:
    empty → auto-probe, ``"bundled"`` → hand back to browser-use, a known name →
    resolve just that one, anything else → treated as an explicit path and returned
    untouched.

    ★ ``candidates`` defaults to ``"chrome"``, matching ``config.browser_candidates``.
    It used to be ``"edge,chrome"`` and that mismatch was a real trap, not a style
    question: a caller that omits the argument silently gets Edge even though the project
    was switched to Chrome, and the symptom is a report that says "Edge" while the user
    believes Chrome is running. It cost a real detour — a verification script that called
    this without arguments and concluded the switch hadn't taken effect, when the switch
    was fine and the script was wrong.

    The lesson generalises past this parameter: **a default that duplicates a value
    configured elsewhere will drift**, and it drifts silently, because each copy looks
    correct on its own. The fix is not to remove the default (callers legitimately rely on
    it) but to make the two agree and to have a test that fails when they stop agreeing.
    """
    value = (configured or "").strip()

    if value.lower() == BUNDLED:
        log.info("browser: 显式要求使用内置 Chromium（BROWSER_EXECUTABLE=bundled）")
        return None

    if value:
        # A bare name is a request for that one browser. Not finding it is worth a
        # warning but must not abort the run — silently falling back to a *different*
        # browser than the one the user pinned is how you get a report that looks fine
        # and was produced by the wrong engine.
        if (
            value.lower() in _WINDOWS_PATHS
            or value.lower() in _POSIX_PATHS
            or value.lower() in _RESOLVERS
        ):
            found = find_browser(value.lower())
            if found:
                log.info("browser: 使用指定的系统浏览器 %s（%s）", value, found)
                return found
            log.warning(
                "browser: 指定了 %r 但本机没找到可执行文件，回退自动探测（不会悄悄换浏览器）",
                value,
            )
        else:
            log.info("browser: 使用显式路径 %s", value)
            return value

    for name in candidate_names(candidates):
        found = find_browser(name)
        if found:
            log.info("browser: 自动探测到 %s → %s", name, found)
            return found

    log.info("browser: 本机没有可用的系统浏览器，回退 browser-use 自带 Chromium")
    return None


def describe(found: str | None) -> str:
    """One-line human description for logs and the UI.

    The wording distinguishes "runs in its own profile" from "shares your profile",
    because that is the thing that broke: a run that shares the daily browser's profile
    wipes its login state and vice versa, and the symptom ("我这边登录了，同一个电脑
    里面另一边就会出现故障") points nowhere near the cause.

    Note it must NOT claim isolation for a bare system browser. The isolation comes from
    the caller passing its own ``user_data_dir`` (the project's ``profiles/<project>``),
    not from which binary was picked — so this function can only report what it knows:
    the binary. ``describe`` is fed the path alone, with no profile in hand, so claiming
    "独立" here would be a guess that stops being true the day someone sets
    ``BROWSER_EXECUTABLE`` to a path and the profile logic changes.
    """
    if not found:
        return "Chromium（内置）"
    if _playwright_chromium() == found:
        return "Chromium（Playwright 自带，独立 profile）"
    name = os.path.basename(found.replace("\\", "/"))
    label = {
        "chrome.exe": "Chrome 正式版",
        "msedge.exe": "Edge 正式版",
        "brave.exe": "Brave",
    }.get(name.lower(), name)
    return f"{label}（跑在项目自己的 profile 目录，与你日常浏览器不共用）"