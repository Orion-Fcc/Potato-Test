"""Per-case executor: drive a real browser via browser-use (Playwright/CDP under the
hood), record video + trace, then judge pass/fail.

browser-use is imported lazily so `app.engine` can be imported (and unit-tested)
without the heavy browser stack installed.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from app.config import get_settings
from app.judge import judge

log = logging.getLogger("potato-test.executor")

# a page whose URL looks like a login/auth screen — used to detect a dead restored
# session (P3) and a capture that didn't actually log in (P2.5). Apps typically redirect an
# unauthenticated visit to /login, so the URL trail is a reliable signal.
_LOGIN_URL_RE = re.compile(r"login|sign-?in|/auth\b", re.I)

# Every element index in a step comes from the snapshot taken BEFORE the step's actions.
# A dropdown/popover's options don't exist in that snapshot, so an index planned for them
# in the same step points at unrelated page content — clicking it closes the popup without
# selecting, and the next snapshot shows it closed again. That loop cost a VRS run 20
# identical steps against a Base UI multi-select.
_POPUP_RULE = """
Opening a dropdown, select, combobox, date picker, autocomplete, menu or any other popover
MUST be the LAST action of the step. Its options do not exist yet in the page state you are
looking at, so any index you pick for them in the same step is a guess at unrelated content.
End the step after the opening click, read the next page state, then click the real option.
If a popup looks open but you cannot find its options in the page state, do NOT click a
nearby index — say so and try keyboard selection (type to filter, arrow keys, Enter) instead.
""".strip()

# The app under test is on a private network. When its URL failed to load the agent kept
# "looking for the site" on Google/Baidu, which cannot reach it either — that burned whole
# runs (VRS runs 28/29 timed out searching, run 39 searched for a localhost URL).
# Analytics / ad endpoints that the app under test loads but the private-network
# container cannot reach. The browser then blocks on the request and the SPA sits on its
# "loading..." splash forever: VRS's login page rendered 33k DOM nodes and 14 characters
# of text for 90+ s because hm.baidu.com never resolved. These hosts contribute nothing
# to a test, so they are refused at the network layer.
_BLOCKED_HOSTS = (
    "hm.baidu.com",
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "sentry.io",
    "umeng.com",
    "umengcloud.com",
    "cnzz.com",
    "51.la",
    "clarity.ms",
    "hotjar.com",
    "baidu.com/hm.js",
)

_SCOPE_RULE = """
You are testing ONE web application, reachable only at the URL you were started on. Never
navigate to a search engine, and never look for the application on the public internet: it
is on a private network and is not indexed anywhere. If the page fails to load
(ERR_CONNECTION_REFUSED, DNS error, timeout), do not go looking for an alternative address
— report the exact URL and the exact browser error and stop. That is the useful result.
""".strip()


def _last_url_is_login(history) -> bool:
    """True if the agent's final page looked like a login screen (read from history —
    robust, unlike probing the live browser after the run)."""
    urls = _safe(lambda: history.urls()) or []
    last = next((u for u in reversed(urls) if u), None)
    return bool(last and _LOGIN_URL_RE.search(last))


def _is_blocked_url(url: str) -> bool:
    """True for third-party analytics/ad beacons we refuse to let the page wait on."""
    u = (url or "").lower()
    return any(host in u for host in _BLOCKED_HOSTS)


async def _block_third_party_beacons(browser) -> None:
    """Fail analytics/ad requests fast via CDP instead of letting them hang.

    The app under test is on a private network; its page also embeds third-party
    beacons (hm.baidu.com etc.). Those never resolve from the container, the browser
    blocks on them, and a SPA that awaits its analytics bootstrap sits on its
    "loading..." splash forever — VRS's login page rendered 33k DOM nodes with 14
    characters of text for 90+ s, which is why every case hit its timeout.

    Failing the request at the network layer lets the page's own error path run so it
    renders. Best-effort: if interception can't be installed we lose speed, not the run.
    """
    try:
        await browser.start()
        cdp = await browser.get_or_create_cdp_session()
        lib = cdp.cdp_client

        def _handler(*args):
            evt = next((a for a in args if isinstance(a, dict)), None)
            if not evt:
                return
            req = evt.get("request") or {}
            url = req.get("url", "")
            rid = evt.get("requestId")
            if not rid:
                return
            if not _is_blocked_url(url):
                asyncio.create_task(
                    lib.send.Fetch.continueRequest(params={"requestId": rid}, session_id=cdp.session_id)
                )
                return
            log.debug("executor: blocking beacon %s", url[:120])
            asyncio.create_task(
                lib.send.Fetch.failRequest(
                    params={"requestId": rid, "errorReason": "Aborted"},
                    session_id=cdp.session_id,
                )
            )

        lib.register.Fetch.requestPaused(_handler)
        await lib.send.Fetch.enable(
            params={"patterns": [{"urlPattern": "*"}], "handleAuthRequests": False},
            session_id=cdp.session_id,
        )
        log.info("executor: third-party beacon blocking enabled")
    except Exception as exc:  # noqa: BLE001 — never fail a case over a speed tweak
        log.warning("executor: beacon blocking unavailable: %s", exc)


# ---------------------------------------------------------------------------
# Workspace-scoped persistent browser profile (the same idea as "browser state is
# carried between agent sessions, isolated per workspace").
#
# Why a POOL and not one directory per project:
#   Chromium takes an exclusive lock on --user-data-dir. Two concurrent cases on the
#   same directory do not "share" it — the second one dies with
#   "Failed to create a ProcessSingleton" / the profile silently resets. Since a
#   project runs several cases in parallel, we shard: N copies of the project's
#   profile, each leased by at most one case at a time and handed back when done.
#
# What is actually shared/carried over:
#   cookies | localStorage | sessionStorage | IndexedDB | HTTP cache — everything
#   Chromium persists in a user-data-dir, which is exactly the list asked for.
#   Per-project isolation falls out of the directory layout (profiles/project_<pid>/...).
#
# Cost model: the FIRST case of a project still pays a cold start (empty profile).
# Every case after it — and every re-run/regression — starts warm: the SPA bundle is
# in the HTTP cache and the session cookie is already valid, so cases typically skip
# the whole login form (which was 3-6 model steps each) and the cold asset fetch.
# ---------------------------------------------------------------------------

# process-local lease state, keyed by "<project_id>:<slot>".
_PROFILE_LOCKS: dict[str, asyncio.Lock] = {}

# Chromium refuses to run if the profile was last closed by a DIFFERENT chromium build
# (e.g. after an image upgrade). This sentinel file records the version we created the
# slot with; a mismatch makes us throw the slot away and re-create it.
_PROFILE_STAMP = ".tp_browser_stamp"


def _profile_root() -> str:
    return os.path.abspath(get_settings().profile_dir)


def _profile_slot_dir(project_id: int, slot: int) -> str:
    return os.path.join(_profile_root(), f"project_{project_id}", f"slot{slot}")


def _profile_slot_key(project_id: int, slot: int) -> str:
    return f"{project_id}:{slot}"


def pick_profile_slot(project_id: int, concurrency: int) -> int:
    """Cheap, lock-free slot pick: prefer a slot nobody currently holds.

    Not a correctness guarantee — the caller still takes the asyncio.Lock via
    acquire_profile_dir() — it just spreads concurrent cases across the shards instead
    of every case piling onto slot 0 and serialising."""
    n = max(1, concurrency)
    busy = {k for k in _PROFILE_LOCKS if k.startswith(f"{project_id}:") and _PROFILE_LOCKS[k].locked()}
    for slot in range(n):
        if _profile_slot_key(project_id, slot) not in busy:
            return slot
    return 0  # all busy → queue on slot 0


class ProfileLease:
    """Async context manager handing out a writable user-data-dir for one case.

    Usage:
        lease = ProfileLease(project_id, slot)
        async with lease:
            browser = Browser(user_data_dir=lease.path, ...)
            ...
        # released here; a new writer session (or session_bundle) is persisted next time
    """

    def __init__(self, project_id: int, slot: int) -> None:
        self.project_id = project_id
        self.slot = slot
        self.path = _profile_slot_dir(project_id, slot)
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self) -> ProfileLease:
        key = _profile_slot_key(self.project_id, self.slot)
        self._lock = _PROFILE_LOCKS.setdefault(key, asyncio.Lock())
        await self._lock.acquire()
        os.makedirs(self.path, exist_ok=True)
        _ensure_profile_health(self.path)
        _clear_stale_singleton(self.path)
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Nothing to flush here: Chromium writes the profile on graceful shutdown, and
        # the caller is responsible for actually closing the browser before we return.
        if self._lock is not None and self._lock.locked():
            self._lock.release()


def _ensure_profile_health(path: str) -> None:
    """Drop a profile stamped by a different browser build — Chromium hard-fails on it
    ("This profile was last used with a newer version") instead of migrating."""
    stamp = os.path.join(path, _PROFILE_STAMP)
    try:
        from browser_use.browser.profile import BrowserProfile  # noqa: F401

        want = _browser_build_key()
        have = None
        if os.path.exists(stamp):
            with open(stamp, encoding="utf-8") as f:
                have = f.read().strip()
        if have and have != want:
            log.info("executor: profile %s build %s != %s, recreating", path, have, want)
            shutil.rmtree(path, ignore_errors=True)
            os.makedirs(path, exist_ok=True)
        if not os.path.exists(stamp):
            with open(stamp, "w", encoding="utf-8") as f:
                f.write(want)
    except Exception as exc:  # noqa: BLE001 — never block a case on profile hygiene
        log.warning("executor: profile health check skipped: %s", exc)


def _browser_build_key() -> str:
    """Whatever identifies the browser binary we're about to launch with."""
    for var in ("PLAYWRIGHT_BROWSERS_PATH", "BROWSER_USE_VERSION"):
        v = os.environ.get(var)
        if v:
            return v
    return "default"


# Chromium claims a profile with three symlinks. It removes them on a clean exit, but a
# SIGKILLed worker (deploy, OOM, `docker compose down` mid-run) leaves them behind — and
# the next launch then REFUSES to start rather than stealing the lock:
#   "Browser process exited before CDP became available" / ProcessSingleton.
# Nothing else can be holding the lock: we are inside the pool's asyncio lock, and the
# only other writer would be a different container, which this project never does.
_SINGLETON_LINKS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_stale_singleton(path: str) -> None:
    for name in _SINGLETON_LINKS:
        p = os.path.join(path, name)
        try:
            if os.path.islink(p) or os.path.exists(p):
                os.unlink(p)
        except OSError as exc:
            log.warning("executor: could not clear %s: %s", p, exc)


async def _shutdown_browser(browser) -> None:
    """Close Chromium so it FLUSHES the profile to disk, then drop the session.

    browser-use's own stop()/close() tear the CDP session down and terminate the process
    without running Chromium's shutdown path, so cookies/localStorage/IndexedDB stay in
    memory and are LOST — measured: the profile's Cookies table was empty after a run that
    had set a cookie, and the next process started logged out. That would have made the
    whole persistent-profile feature a no-op.

    Sending Browser.close over CDP makes Chromium exit gracefully and commit its profile.
    Verified end-to-end: a cookie set in process A is present in fresh process B on the
    same user_data_dir. Falls back to the plain stop() when there is no live session
    (e.g. the browser already died, which is also the case where there is nothing to flush).
    """
    if browser is None:
        return
    try:
        cdp = await browser.get_or_create_cdp_session()
        await cdp.cdp_client.send.Browser.close(session_id=cdp.session_id)
        # Give Chromium a moment to finish writing before we hand the shard to the next case.
        await asyncio.sleep(1.5)
    except Exception as exc:  # noqa: BLE001 — a dead browser has nothing to flush
        log.debug("executor: CDP Browser.close unavailable (%s), falling back", exc)
    await _safe_async(lambda: browser.stop())


def reset_project_profiles(project_id: int) -> None:
    """Wipe a project's profile pool (admin button: "reset browser state")."""
    d = os.path.join(_profile_root(), f"project_{project_id}")
    shutil.rmtree(d, ignore_errors=True)


def browser_state_info(project_id: int) -> dict:
    """What the UI shows for "browser state / workspace persistence"."""
    root = os.path.join(_profile_root(), f"project_{project_id}")
    slots: list[dict] = []
    total = 0
    newest = 0.0
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            size = 0
            mtime = 0.0
            for dirpath, _dirs, files in os.walk(p):
                for f in files:
                    try:
                        st = os.stat(os.path.join(dirpath, f))
                    except OSError:
                        continue
                    size += st.st_size
                    mtime = max(mtime, st.st_mtime)
            # "warm" == this shard already holds a real session, not just a fresh skeleton.
            warm = os.path.exists(os.path.join(p, "Default", "Cookies"))
            slots.append(
                {"slot": name, "path": p, "bytes": size, "warm": warm, "mtime": int(mtime)}
            )
            total += size
            newest = max(newest, mtime)
    return {
        "project_id": project_id,
        "enabled": bool(get_settings().persistent_profile),
        "root": root,
        "slots": slots,
        "total_bytes": total,
        "last_used": int(newest),
    }


def effective_case_timeout(configured_s: int, max_steps: int, s=None) -> int:
    """Wall-clock safety net for one case, scaled to the work it is allowed to do.

    A fixed cap is the wrong shape: steps do not take equal time (a navigate is ~2s, a
    model round-trip on a heavy page is 10-20s), so any constant either aborts slow-but-
    healthy cases or is so large it never protects anything. Budget the STEPS and derive
    the clock from that: time = configured net, further widened by per_step_timeout_s for
    every step beyond what the configured net already covers.

    The result is always at least the configured value, so a per-project override still
    means what its author intended (it is a floor here, not a ceiling).
    """
    s = s or get_settings()
    base = max(1, int(configured_s or 0))
    per_step = max(0, int(getattr(s, "per_step_timeout_s", 0) or 0))
    if per_step <= 0:
        return base
    # Steps the base net already pays for at the per-step rate; anything above that adds time.
    steps_covered = base // per_step
    extra = max(0, int(max_steps or 0) - steps_covered) * per_step
    return base + extra


@dataclass(frozen=True)
class CaseSpec:
    case_id: int
    prompt: str
    expected: str = ""
    start_url: str | None = None
    login_state: str | None = None  # storage_state JSON string, or None
    login_username: str | None = None  # robot account for per-run prompt-login
    login_password: str | None = None
    timeout_s: int = 0  # 0 => fall back to settings.case_timeout_s
    max_steps: int = 0  # 0 => fall back to settings.case_max_steps
    # RunResult id — the artifact folder. Keying artifacts by case_id made every re-run of
    # a case overwrite the previous run's screenshots/video, so old reports silently showed
    # the newest run's frames. 0 falls back to the case id (nothing constructs a spec
    # without a result row today; the fallback just keeps paths well-formed).
    result_id: int = 0
    # Persistent-profile wiring (see ProfileLease). project_id selects the workspace;
    # concurrency sizes the shard pool; 0 / persistent_profile=False keeps the legacy
    # fresh-browser-per-case behaviour.
    project_id: int = 0
    concurrency: int = 1
    persistent_profile: bool = False


@dataclass
class ResultSpec:
    case_id: int
    status: str = "error"  # passed | failed | error
    video_url: str | None = None
    trace_url: str | None = None
    steps: list = field(default_factory=list)
    diagnostics: list = field(
        default_factory=list
    )  # per-step [{i,action,thought,result,error,screenshot}]
    judge_reason: str | None = None
    final_answer: str | None = None
    latency_ms: int = 0
    error: str | None = None
    auth_failed: bool = False  # restored session was dead (ended on a login page) → self-heal
    timed_out: bool = False  # hit the wall-clock cap → a re-attempt cannot change the outcome


def _read_b64(path: str | None) -> str | None:
    """A local file as base64, or None. Used to hand the judge the final frame."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode()
    except OSError:
        return None


def _newest(pattern: str) -> str | None:
    hits = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return hits[0] if hits else None


def _build_diagnostics(history) -> list[dict]:
    """Per-step timeline from a browser-use AgentHistoryList: what the model thought,
    the action it took, the result/error, and the local screenshot path (uploaded later).
    Reads history.history directly so it works even when a run is cut short by timeout."""
    items = list(getattr(history, "history", None) or [])
    paths = _safe(lambda: history.screenshot_paths()) or []
    steps: list[dict] = []
    for i, h in enumerate(items):
        mo = getattr(h, "model_output", None)
        thought = ""
        actions: list[str] = []
        if mo is not None:
            thought = (
                getattr(mo, "thinking", None) or getattr(mo, "next_goal", None) or ""
            ).strip()
            for a in getattr(mo, "action", None) or []:
                dumped = a.model_dump(exclude_none=True) if hasattr(a, "model_dump") else {}
                actions.extend(dumped.keys())
        results = list(getattr(h, "result", None) or [])
        error = next((r.error for r in results if getattr(r, "error", None)), None)
        content = next(
            (r.extracted_content for r in results if getattr(r, "extracted_content", None)), None
        )
        steps.append(
            {
                "i": i + 1,
                "action": ", ".join(actions) or "—",
                "thought": thought,
                "result": (content or "")[:600],
                "error": (error or "")[:600],
                "screenshot": paths[i] if i < len(paths) else None,  # local path; uploaded below
            }
        )
    return steps


def build_task(spec: CaseSpec, report_language: str) -> str:
    """The instruction handed to the agent. Split out so the login-fallback rule below is
    testable without a browser."""
    parts = []
    if spec.start_url:
        parts.append(f"First go to {spec.start_url}.")
    # ALWAYS hand over the credentials, restored session or not. An expired session lands
    # the agent on the login page, and withholding them there left it either giving up
    # ("缺少登录凭据") or inventing a password and tripping the app's 「请求过于频繁」
    # rate limit for every other case in the run.
    if spec.login_username:
        parts.append(
            f"If a login page is shown, log in with username '{spec.login_username}' and "
            f"password '{spec.login_password}', reading and answering any simple captcha; "
            "if a first-login password change is forced, set a new valid password and continue. "
            "Use exactly this username and password — never invent credentials, and if login "
            "is refused (rate limited, wrong password) stop and report it instead of retrying."
        )
    parts.append(f"Then perform this task: {spec.prompt}")
    parts.append(
        f"Write all of your thinking, reasoning, evaluation and the final answer in {report_language}."
    )
    return " ".join(parts)


async def execute_case(spec: CaseSpec, on_step=None, should_abort=None) -> ResultSpec:
    """Run one NL test case in a fresh browser. Never raises — errors are captured.

    on_step(live_steps): optional async callback invoked after every browser-use step
    with the running list of {i, action, thought, screenshot} — powers the live view.
    should_abort(): optional async predicate checked every step; True stops the agent
    (that is how cancelling a run reaches a case that is already driving a browser)."""
    from app.storage import upload

    s = get_settings()
    res = ResultSpec(case_id=spec.case_id)
    t0 = time.monotonic()
    art = f"runs/{spec.result_id or spec.case_id}"  # this attempt's artifact folder
    final_shot: str | None = None

    with tempfile.TemporaryDirectory(prefix=f"tp_case_{spec.case_id}_") as workdir:
        video_dir = os.path.join(workdir, "video")
        trace_dir = os.path.join(workdir, "trace")
        os.makedirs(video_dir, exist_ok=True)
        os.makedirs(trace_dir, exist_ok=True)

        browser = None
        agent = None
        live_steps: list[dict] = []
        # Set once the browser has been shut down, so the finally block does not do it twice.
        _closed = [False]
        timeout_s = effective_case_timeout(spec.timeout_s or s.case_timeout_s, spec.max_steps or s.case_max_steps, s)
        max_steps = spec.max_steps or s.case_max_steps
        # Workspace-scoped persistent profile: one shard per concurrency slot, leased for
        # the whole case. Kept outside the try so the lease is released even on setup error.
        use_profile = bool(spec.persistent_profile and s.persistent_profile and spec.project_id)
        lease = (
            ProfileLease(spec.project_id, pick_profile_slot(spec.project_id, spec.concurrency))
            if use_profile
            else None
        )
        warmed = False
        try:
            if lease is not None:
                await lease.__aenter__()
                # "warmed" = this shard already carries a real session (not a fresh dir).
                warmed = bool(os.path.exists(os.path.join(lease.path, "Default", "Cookies")))

            from browser_use import Agent, Browser

            from app.llm import browser_use_llm

            # ponytail: record_video_dir / traces_dir are the Playwright-context recording
            # knobs. Their exact names on Browser/BrowserProfile drift across browser-use
            # releases — verify against the pinned version at first install and adjust here only.
            # Speed knobs (all safe, none change the LLM):
            #  * highlight_elements=False — the per-step element-index overlay is a full
            #    extra DOM pass plus paint on a heavy SPA; the a11y tree already carries
            #    the indices the model needs.
            #  * record_video_dir / traces_dir cost real CPU (Playwright encodes every
            #    frame). Both are opt-out via settings so a fast local loop can skip them;
            #    enabled by default to keep the report's replay feature.
            browser = Browser(
                headless=True,
                # never fetch the default uBlock/cookie extensions — that download is a
                # blocking call with no internet in the container (it froze the API).
                enable_default_extensions=False,
                highlight_elements=s.browser_highlight_elements,
                # Persistent, project-isolated user-data-dir → cookies/localStorage/
                # sessionStorage/IndexedDB + HTTP cache survive between cases and runs.
                **({"user_data_dir": lease.path} if lease is not None else {}),
                **({"record_video_dir": video_dir} if s.case_record_video else {}),
                **({"traces_dir": trace_dir} if s.case_record_trace else {}),
            )
            await browser.start()
            await _block_third_party_beacons(browser)
            # Seed the captured session bundle ONLY into a cold shard. A warm shard
            # already holds a live session and a newer app state; re-injecting the old
            # bundle would overwrite tokens the app itself has since rotated.
            if spec.login_state and not warmed:
                await _safe_async(lambda: _restore_session(browser, spec.login_state))
            task = build_task(spec, s.report_language)

            async def _step_cb(browser_state, model_output, step_no):
                # stream each step live: model thought + action + current screenshot.
                # browser-use keeps screenshots in-memory (not on disk), so grab one via
                # CDP here rather than relying on browser_state.screenshot / screenshot_paths.
                # The capture + artifact upload is expensive (CDP round-trip + disk write),
                # so it runs every live_shot_every steps: the feed stays useful while the
                # per-step cost drops to near zero on the steps in between.
                try:
                    thought = (
                        getattr(model_output, "thinking", None)
                        or getattr(model_output, "next_goal", None)
                        or ""
                    ).strip()
                    actions: list[str] = []
                    for a in getattr(model_output, "action", None) or []:
                        d = a.model_dump(exclude_none=True) if hasattr(a, "model_dump") else {}
                        actions.extend(d.keys())
                    shot_url = None
                    if step_no % max(1, s.live_shot_every) == 0:
                        png = await _safe_async(lambda: browser.take_screenshot())
                        if png:
                            p = os.path.join(workdir, f"live-{step_no}.png")
                            data = png if isinstance(png, bytes) else base64.b64decode(png)
                            with open(p, "wb") as f:  # noqa: ASYNC230 — tiny one-shot write
                                f.write(data)
                            shot_url = await _swallow(upload(p, f"{art}/live-{step_no}.png"))
                    live_steps.append(
                        {
                            "i": step_no,
                            "action": ", ".join(actions) or "…",
                            "thought": thought,
                            "result": "",
                            "error": "",
                            "screenshot": shot_url,
                        }
                    )
                    if on_step is not None:
                        await on_step(list(live_steps))
                    if should_abort is not None and await should_abort():
                        res.error = "cancelled"
                        _safe(lambda: agent.stop())
                except Exception:
                    pass

            agent = Agent(
                task=task,
                llm=await browser_use_llm(),
                browser=browser,
                register_new_step_callback=_step_cb,
                extend_system_message=f"{_SCOPE_RULE}\n\n{_POPUP_RULE}",
                use_vision=False,  # 提速：不每步发整屏截图，改用无障碍树/DOM 文本
                # Speed: batch independent actions (e.g. several form fills) into one
                # model round-trip instead of one round-trip per action. This is the
                # single biggest lever on step count, and step count is the cost.
                max_actions_per_step=s.max_actions_per_step,
                # Speed: bound a single slow model call so it can't consume the whole
                # case budget while the run waits on it.
                step_timeout=s.step_timeout_s,
            )
            # nested so a timeout/agent error still lets us harvest agent.history below —
            # wait_for cancels the coroutine and never returns, so we must read agent.history
            # (built up in-place) rather than the run() return value.
            try:
                await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=timeout_s)
            except TimeoutError:
                res.error = f"timeout after {timeout_s}s"
                res.timed_out = True
            except Exception as exc:
                res.error = f"{type(exc).__name__}: {exc}"[:500]
        except Exception as exc:  # browser/agent setup failure
            res.error = res.error or f"{type(exc).__name__}: {exc}"[:500]
        finally:
            # Normal path closes the browser below (after the final screenshot). This only
            # catches the error paths that jump straight out of the try block.
            if browser is not None and not _closed[0]:
                await _shutdown_browser(browser)
                _closed[0] = True
            if lease is not None:
                await lease.__aexit__(None, None, None)

        # harvest diagnostics from the agent's history regardless of outcome (the whole
        # point: a timed-out/failed case still shows what the model thought and did).
        # live_steps already carry per-step screenshots (uploaded in the callback); the
        # history adds each step's result/error, which we merge in by index.
        history = getattr(agent, "history", None) if agent is not None else None
        if history is not None:
            res.final_answer = _safe(lambda: history.final_result()) or ""
            res.steps = _safe(lambda: history.action_names()) or []
            # P3: we restored a session but the agent ended on a login page → session dead.
            # Signal the engine to invalidate + re-capture + retry once.
            if spec.login_state:
                res.auth_failed = _last_url_is_login(history)
            hist = _safe(lambda: _build_diagnostics(history)) or []
            for i, ls in enumerate(live_steps):
                if i < len(hist):
                    ls["result"] = hist[i].get("result", "")
                    ls["error"] = hist[i].get("error", "")
            res.diagnostics = live_steps or hist
            history_path = os.path.join(workdir, "history.json")
            _safe(lambda: history.save_to_file(history_path))
        else:
            res.diagnostics = live_steps

        # A bare "timeout after 600s" reads the same whether the agent was one step from
        # done or stuck on step 3 — and those want opposite fixes (raise the budget vs.
        # fix the case). VRS run 45's seven timeouts had all reached step 21-29 of 30.
        #
        # The two cases are now NAMED, because the response to each is different and a
        # single "timeout" line sent people tuning the clock when the real limit was the
        # step budget (or vice versa):
        #   * steps exhausted  -> the case is too long for case_max_steps; it did not hang.
        #   * clock exhausted  -> genuinely slow/hung mid-step; the net is a real signal.
        if res.timed_out:
            reached = len(res.diagnostics)
            if reached >= max_steps:
                res.error = (
                    f"步数预算用尽：已用满 {max_steps} 步（用时 {timeout_s}s）。"
                    f"该用例比当前步数上限更长，请在项目设置里调高『最大步数』，"
                    f"或把用例拆成几条更小的。"
                )
            else:
                res.error = (
                    f"超时：{timeout_s}s 内只走到第 {reached}/{max_steps} 步。"
                    f"这一步本身卡住了（不是步数不够），通常是页面某元素一直等不到或网络慢；"
                    f"可适当调高『用例超时』，但更建议检查该步骤对应的页面。"
                )

        # The judge's only look at the page's end state. live-*.png is now sampled every
        # Nth step (speed), so grab a dedicated final frame rather than hoping the last
        # live shot happened to land on the last step.
        final_path = _newest(os.path.join(workdir, "live-*.png"))
        if browser is not None:
            try:
                png = await _safe_async(lambda: browser.take_screenshot())
                if png:
                    p = os.path.join(workdir, "final.png")
                    data = png if isinstance(png, bytes) else base64.b64decode(png)
                    with open(p, "wb") as f:
                        f.write(data)
                    final_path = p
            except Exception:
                pass
        # Screenshot done, so the browser can go. Everything read from it (video file,
        # live frames) is finished, and a persistent profile only reaches disk on a
        # graceful close — so this is the single point where we shut it down.
        if browser is not None and not _closed[0]:
            await _shutdown_browser(browser)
            _closed[0] = True

        # collect + upload artifacts (best-effort; missing artifacts don't fail the case).
        # AFTER the shutdown: the video file is only finalized when the browser closes.
        video = _newest(os.path.join(video_dir, "*"))
        history_file = os.path.join(workdir, "history.json")
        if video:
            res.video_url = await _swallow(upload(video, f"{art}/{os.path.basename(video)}"))
        if os.path.exists(history_file):
            res.trace_url = await _swallow(upload(history_file, f"{art}/history.json"))
        final_shot = _read_b64(final_path)

    res.latency_ms = int((time.monotonic() - t0) * 1000)

    if res.error:
        res.status = "error"
    else:
        verdict = await judge(
            spec.expected,
            res.final_answer or "",
            res.steps,
            task=spec.prompt,
            # the browser's own read-backs ("Clicked div role=option …", "Typed …", errors)
            evidence=[
                str(d.get("result") or d.get("error") or "") for d in (res.diagnostics or [])
            ],
            screenshot_b64=final_shot,
        )
        res.status = verdict.status
        res.judge_reason = verdict.reason
    return res


async def capture_session(base_url: str, username: str, password: str) -> str:
    """Log into base_url with a test account server-side and return the captured
    storage_state as a JSON string. The agent reads simple captchas. Raises on failure."""
    from browser_use import Agent, Browser

    from app.llm import browser_use_llm

    s = get_settings()
    # keep_alive so the CDP session survives after agent.run() — otherwise
    # export_storage_state() fails with "Root CDP client not initialized".
    browser = Browser(headless=True, keep_alive=True, enable_default_extensions=False)
    try:
        task = (
            f"Go to {base_url}. Log in with username '{username}' and password '{password}'. "
            "If a simple math or text captcha is shown, read it from the page and enter the answer. "
            "If a first-login password change is forced, set a new valid password and continue. "
            "Finish only once you are on a logged-in page (no longer on the login screen)."
        )
        agent = Agent(task=task, llm=await browser_use_llm(), browser=browser, use_vision=False)
        await asyncio.wait_for(agent.run(max_steps=s.case_max_steps), timeout=s.case_timeout_s)
        # P2.5: never store a garbage bundle — if login didn't actually complete (agent still
        # on a login page), fail loudly so the caller falls back to prompt-login instead of
        # caching a tokenless session that would make every future case fail.
        if _last_url_is_login(agent.history):
            raise RuntimeError("login did not complete — still on a login page after capture")
        # Full bundle: cookies + per-origin localStorage AND sessionStorage (CDP format).
        # Restored later via CDP (_restore_session), so sessionStorage-based auth
        # survives — unlike the Playwright storage_state format, which drops it.
        state = await browser._cdp_get_storage_state()  # noqa: SLF001 — private API, pinned to browser-use 0.13.x
        return json.dumps(state, ensure_ascii=False)
    finally:
        await _safe_async(lambda: browser.kill())


async def _restore_session(browser, bundle_json: str) -> None:
    """Restore a captured session bundle (from capture_session / _cdp_get_storage_state)
    so the agent starts logged in, WITHOUT the storage_state= launch path:
      - cookies via CDP Storage.setCookies
      - localStorage + sessionStorage seeded at document-start via
        Page.addScriptToEvaluateOnNewDocument, so they exist before the app's own JS runs
        (this is what makes sessionStorage-based auth survive).
    Best-effort: a restore failure just means the agent may see a login page."""
    bundle = json.loads(bundle_json)
    await browser.start()  # idempotent; needed so a CDP target exists before we inject
    # cookies best-effort on their own: a legacy (Playwright-format) bundle may not map
    # cleanly to CDP setCookies, but must NOT block the storage seed below (which is what
    # carries sessionStorage-based auth).
    cookies = bundle.get("cookies") or []
    if cookies:
        await _safe_async(lambda: browser._cdp_set_cookies(cookies))  # noqa: SLF001
    origins: dict[str, dict] = {}
    for o in bundle.get("origins") or []:
        ls = {i["name"]: i["value"] for i in (o.get("localStorage") or [])}
        ss = {i["name"]: i["value"] for i in (o.get("sessionStorage") or [])}
        if ls or ss:
            origins[o["origin"]] = {"localStorage": ls, "sessionStorage": ss}
    if origins:
        # seed each key ONLY if absent. This script runs on *every* new document, so an
        # unconditional setItem re-writes stale values the app just updated — e.g. an app that
        # compares localStorage.DVADMIN3_VERSION against /version-build and reload()s on a
        # mismatch, so re-seeding the old version reloaded the page ~19x/s forever (blank
        # DOM + hung CDP screenshots → the agent sees an empty page). sessionStorage
        # survives same-tab reloads, so auth still restores on the first document.
        seed = (
            "(function(){var D=" + json.dumps(origins, ensure_ascii=False) + ";try{"
            "var d=D[location.origin];if(d){"
            "for(var k in d.localStorage)"
            "if(localStorage.getItem(k)===null)localStorage.setItem(k,d.localStorage[k]);"
            "for(var k in d.sessionStorage)"
            "if(sessionStorage.getItem(k)===null)sessionStorage.setItem(k,d.sessionStorage[k]);"
            "}}catch(e){}})();"
        )
        await browser._cdp_add_init_script(seed)  # noqa: SLF001 — private, pinned to browser-use 0.13.x


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


async def _safe_async(fn):
    try:
        return await fn()
    except Exception:
        return None


async def _swallow(coro):
    try:
        return await coro
    except Exception:
        return None
