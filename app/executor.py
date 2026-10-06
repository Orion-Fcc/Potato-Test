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

from app import browser_binary
from app.config import get_settings
from app.failure_narrative import describe_failure
from app.judge import judge
from app.judge_gate import check_gates

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

# ---------------------------------------------------------------------------
# Anti-waste rules. Measured on this project's own run history (2026-10-02): 44% of all
# steps were `wait` / `scroll` / `search_page` / `find_elements` / `evaluate` — steps that
# change nothing on screen and only re-ask a question the page already answered.
#
# The pattern, verbatim from run_result #140 (case 11): the agent needed THREE real
# actions, spent NINE steps, and four of the extra ones were the same question asked
# again — `search_page "请选择审批节点"`, then `search_page "必填|请选择|审批节点"`, then
# `wait`, then `wait`, each one a full 26-67s model round-trip. It had the answer at the
# first search and kept re-confirming it.
#
# That case ran 336 s for ~3 actions of work. Since concurrency is pinned to 1, a wasted
# step is not overlapped with anything — it is pure serial dead time. Removing it is the
# only remaining speed lever that touches neither the model nor the parallelism.
_ANTI_WASTE_RULE = """
You have a limited step budget and every step costs real time. Do not spend steps
re-reading a page you have already read.

1. CONFIRM ONCE. If a page state already tells you the answer (a validation message is
   visible, a field shows an error, a row is present or absent), that is your evidence —
   finish and report it. Do NOT re-run a search to double-check the same text, and do not
   search twice with slightly different keywords for the same fact. If you already found
   "请选择审批节点", searching for "必填|请选择|审批节点" adds nothing.
2. NEVER WAIT BLINDLY. "Wait a few seconds to see if it loads" is a wasted step: the next
   snapshot you get is taken anyway after your action, so waiting changes nothing you
   would not already see. Only wait if you have a specific reason (e.g. you were told a
   known delay exists). If a page state came back empty, do not wait and retry the same
   thing — scroll, navigate, or conclude.
3. NO RE-VERIFICATION LOOPS. If your previous action produced the expected visible result,
   move to the NEXT step of the task. Re-clicking an element "to be sure", re-reading a
   list you already read, or re-opening a dialog to check it is still open are all wasted
   steps. Your own logged action + the page's read-back are the record; trust them.
4. ONE action per question. When you trigger a validation error, the error text and the
   still-open dialog are your answer, in the same snapshot. Report it; do not then search
   the page for the error text you can already see.
5. STOP WHEN DONE. The moment the task's goal is achieved or provably unachievable,
   call `done` with your conclusion. Do not take "one more look" first — final
   confirmation of a conclusion you already reached is the single most expensive habit
   you can have.
6. BATCH INDEPENDENT ACTIONS. You may emit several actions in ONE step. When the actions
   do not depend on each other's result — filling several separate form fields, ticking
   several checkboxes, clicking through a fixed sequence of menus — put them in the SAME
   step instead of one per step. This is the single biggest saving available: measured on
   this project, 67% of steps carried only one action, so each step paid a full
   think-and-observe round-trip to do one small thing.
   The one exception is opening a popover (see the dropdown rule above): that must end
   its step, so the next state can be read before choosing from it.
7. STOP THRASHING. If the SAME interaction fails twice, stop attempting it. Concretely:
   a button that stays disabled, a click that keeps landing on the wrong element, an
   option that will not select, a field that keeps rejecting a value — trying a third,
   fourth, tenth time with slightly different wording will not make the UI cooperate.
   On the second failure you already have your evidence: the UI does not behave as the
   case expects. Report exactly what you observed (that IS the defect) and call `done`.
   This is measured, not theoretical: on this project the most expensive cases (44 and
   55 steps) were each ONE interaction retried a dozen times — and not one of those
   retries ever succeeded. They only consumed the budget that a clear, early failure
   report would have cost a fraction of. A promptly-reported failure is a good result;
   a long thrash that ends in the same failure is a bad one.
   What you MAY still do: try ONE genuinely different route to the same goal (a different
   wording, a different entry point, satisfying a prerequisite you just discovered — see the
   adaptation rule). "Different route" and "same click again" are not the same thing.
""".strip()

# 2026-10-04 用户要求：「直接告诉 agent 使用最少的步数和最快的时间去完成，
# 但是并发数只能是 1（内网限制），运行的质量不能降低，不能为了快瞎整。」
#
# 这段规则就是为了把"快"和"准"的边界写清楚。为什么必须写清楚：
# 一旦告诉模型"要快"，它会走两个捷径 —— (a) 跳过验证直接说通过，
# (b) 猜一个结论填上。这两条都会让报告变成假数据，比慢更糟。
# 所以下面用**正反两面**写：既要它省步数，又明确禁止用降低证据标准的方式省。
#
# 剩下的速度手段（去掉固定步数上限）见 config.py 的 case_max_steps 注释。
_EFFICIENCY_RULE = """
Work at the smallest step count that still produces trustworthy evidence. Speed here
means "not wasting steps", NOT "lowering the bar for proof".

WHAT COUNTS AS GOING FAST (do these):
1. PLAN ONCE, THEN ACT. At the start, decide the shortest path to the goal and follow it.
   Do not explore the whole menu tree to "understand the system" — open only what the task
   actually needs. (This forbids touring the app. If the one route you planned is genuinely
   not there, a BOUNDED search for another route is allowed — see the adaptation rule.)
2. REUSE WHAT YOU ALREADY HAVE. If the session is already logged in, do not log in again.
   If a page already shows the data you need, read it instead of navigating to it again.
3. TAKE THE SHORTEST ROUTE TO THE SAME EVIDENCE. If a list page shows the row you need,
   that is as good as opening the detail page. Prefer one page that answers the question
   over three that build up to it.
4. FINISH THE MOMENT THE GOAL IS MET. Reaching the goal is the end of the case.

WHAT IS FORBIDDEN (this is the "不要瞎整" part):
5. NEVER trade evidence for steps. Every conclusion must point at something you actually
   observed on the page. "It probably worked", "the flow usually succeeds", "assuming the
   save succeeded" are not evidence — they are fabricated results, and a fabricated PASS is
   far worse than an honest FAIL or an honest "could not verify".
6. NEVER report an outcome you did not see. If you could not reach the state that proves
   or disproves the expected result, say exactly that and name the step where you got stuck.
   An honest "unverified" is a useful result; an invented "passed" is a defect in the test
   suite itself.
7. NEVER skip a step the task explicitly asks you to verify. If the expected result names
   3 fields to check, check 3. Cutting it to 1 to save steps changes the test, it does not
   speed it up.
8. NEVER rush an interaction that needs the page to settle. Saving 2 seconds by clicking
   before a form is ready costs a 30-step detour when it silently fails.
""".strip()

# 2026-10-06 包容性 / 自适应探索。用户原话：「ai 的操作太呆了吧，他不会自己探索一下，
# 然后包容性的进行操作吗」。
#
# 为什么会呆（这是上面几条规则合起来的副作用，不是模型的锅）：
# _EFFICIENCY_RULE 叫它别逛菜单，_ANTI_WASTE_RULE 叫它同一个动作失败两次就停。
# 单独看都对，但合起来把模型逼成了"只会照着用例文案逐字找元素"。于是只要页面文案
# 和用例文案不一致（"新增" vs "新建"）、或者目标藏在折叠面板/第二页/另一个入口下，
# 模型就宣布"元素不存在"。
#
# 那个"不存在"是**假失败**：它证明的是模型没找到，不是被测系统有缺陷。
# 而用例是人凭记忆写的，往往早于最近一次 UI 改动 —— 字面不一致是常态，不是异常。
#
# 所以这条规则只放宽"怎么走到目标"，不放宽"什么算证据"。这条边界必须写死在提示词里，
# 否则"包容性"会被理解成"差不多就算通过"，那比呆还糟糕。
_ADAPT_RULE = """
The task text was written by a person, from memory, often before the last UI change. Treat it
as the GOAL, not as literal UI strings. When what the task describes is not exactly what is on
screen, ADAPT — within the limits spelled out below.

ADAPT THE WORDS:
1. Match by meaning, not by exact characters. 新增 / 新建 / 添加 / 录入 / 创建 are the same
   button; 查询 / 搜索 / 筛选 the same action; 删除 / 移除 / 作废 the same intent. If the task
   says 新增 and the page says 新建, click 新建 — "the 新增 button does not exist" is a FALSE
   FAILURE, because what you actually proved is that you did not find it.
2. Quote the page's own wording when you report. A report reading "未找到新增按钮" on a page
   whose button reads 新建 sends the tester hunting for a defect that is not there.
3. Case, full/half-width, punctuation, and leading/trailing whitespace are not differences.

ADAPT THE ROUTE — only when the direct one is genuinely absent:
4. Before concluding that something is missing, spend at most 2-3 cheap steps on the mundane
   explanations: scroll the table or list (the row may be below the fold, or on page 2),
   expand a collapsed panel / accordion / 展开, switch to another tab or step bar on the same
   page, close a blocking dialog or drawer, and check whether a parent record (项目/组织/分类)
   must be selected before anything appears.
5. If a menu path named in the task does not exist, look for the same module ONE other way —
   the same words elsewhere in the navigation, or the application's own search box. This is
   bounded: two attempts, then stop. Do NOT walk the whole menu tree.
6. If a control is disabled or a field rejects a value, read what the page is asking for
   (required fields, a format hint, a validation message, an unselected prerequisite) and
   satisfy it. "The button is disabled" is not a verdict until you have read why.

WHERE ADAPTATION STOPS — a hard line:
7. Adaptation is about REACHING the thing you must test. It is NEVER about what counts as
   proof. Finding a button that "looks like" the one the task meant does not make the expected
   result true: the expected result must still be visibly present, exactly as the efficiency
   and self-check rules require. Being flexible about labels while strict about evidence is
   the whole point — reversing that produces confident, wrong passes.
8. If you genuinely cannot reach the target after the attempts above, say so plainly and name
   WHAT IS THERE instead — the actual menu items, the actual button labels, the actual page
   title. Naming what exists separates "the feature is missing" (a real defect, worth a bug
   report) from "I could not find it" (an execution gap, worth fixing the case). Repeating
   only what is absent leaves the tester unable to tell those two apart.
""".strip()


# 2026-10-04 自检节点（Evaluator 模式的最省形式）。
#
# 为什么需要它：这套系统的主要质量问题不是"跑得慢"，而是**判定不准** ——
# 看漏一个字段、跳过一个验证点、把"没报错"当成"通过"。上面 _EFFICIENCY_RULE
# 让模型求快，快和准天然冲突，所以必须在"宣布通过"这个动作上加一道自检。
#
# 为什么不另起一次 LLM 调用做 Evaluator：那会让每个用例多花一次模型往返
# （实测单次 26-67 秒），而且那正是用户不想要的"为了准确拖长时间"。
# 把自检做成**宣布结论前的强制自问**，代价接近 0，且能拦住绝大多数漏看。
#
# 只对"打算判通过"的情况要求自检：判失败时模型本来就会把证据摆出来。
_SELF_CHECK_RULE = """
BEFORE YOU CALL `done` WITH A PASSING VERDICT, run this self-check. It costs no extra
steps — just answer it to yourself from what is already on screen and in your log.

1. STATE THE EXPECTED RESULT, then point at the exact thing on the page that satisfies
   it. If you cannot name what you saw (which list, which field, which message, which
   row), you have not verified it — go back and look, or report it as unverified.
2. LIST THE CHECKS THE TASK ASKED FOR. If the expected result names several things
   (e.g. 3 fields, a status change AND a message), confirm you actually checked each
   one. "The main one worked" is not the same as "the case passed".
3. ASK WHETHER YOU SKIPPED A STEP. Compare what the task asked for against the steps in
   your log. A gap between them means the case is not verified, not passed.
4. DISTINGUISH "NO ERROR SHOWN" FROM "CORRECT BEHAVIOUR". A silent page is not evidence
   of success by itself — the state you expected must be visibly present.

If any answer above is "I did not actually confirm that", do NOT report a pass. Either
go back and confirm it (if you still have steps), or report it honestly as unverified
and say which part you could not confirm. An unverified case is a useful result; a pass
you cannot point at is a defect in the test suite.
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


_TOOLKIT_RULE = """
Use the RIGHT tool for each situation — several of them exist precisely because the obvious
approach fails. Concretely, on this application's UI:

- **Dropdowns / selects (the grey boxes that open a list): use `select_dropdown`.** Do NOT click
  the box and then click the option in the page. Hand-clicking a `<select>`-style control is
  the single most common way a run dies here: measured on case 4 ("查询：使用范围+状态同时选"),
  five runs alternated pass/fail, and every failure was the same shape — the first dropdown got
  selected, the second was clicked open, and then the run just ended without choosing an option
  or pressing 查询. Not sure what a dropdown contains? `dropdown_options` reads the options
  without guessing, then `select_dropdown` picks one.
- **A tooltip, placeholder or hint that only appears on hover: hover the element with `click`
  first** (there is no separate hover action in this build), and if that does not reveal it,
  move on rather than concluding the element is missing.
- **Uploading a file (e.g. a storage_state / auth.json): use `upload_file`; to swap an already
  uploaded file, `replace_file`.** Do not try to type a filesystem path into a text box.
- **Looking for a phrase on a long page: use `find_text`.** It scrolls to the match, which is
  what a person would do.
- **You navigated somewhere wrong: use `go_back`** instead of hunting through menus again.
- **Reading a long block of text off the page: use `extract`.** It is one call instead of
  scrolling and re-reading the same paragraph step after step.
- **An action opened a NEW TAB and you now cannot see your page: use `switch` to go back to
  the application tab.** A run that wanders into a blank new tab usually ends up stuck there.
- **You are done with a tab you opened: `close` it** so it does not confuse later steps.

Deliberately NOT taught here, and why:
  * `search` — it drives a web search engine. This application is on a private network and is
    not indexed; searching is a dead end (see _SCOPE_RULE).
  * `screenshot` — redundant. We capture a screenshot every single step ourselves.
  * `read_file` / `write_file` — for working with local files, not for driving a web UI.
  * `save_as_pdf` — nothing in a test verdict needs a PDF; it would add time and file churn.
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
# Prune the app's inline SVG icon sprite. This is THE per-step speed fix.
#
# Measured 2026-10-02 against 192.0.2.10:8600 (a Vue + Element-Plus admin):
#   - the page injects <svg id="__svg__icons__dom__" aria-hidden="true">, a hidden
#     icon REGISTRY (position:absolute;width:0;height:0 — it is not painted at all)
#   - it holds 79 <symbol>s / 32,979 <path>s, and ONE symbol
#     (`icon-building-floor-plan`, a floor-plan glyph) is 32,609 of them
#   - on the LOGIN page that symbol is referenced 0 times by any <use>
#   - `get_browser_state_summary()` took 20.7–27.3 s with the sprite present, and
#     0.80–0.93 s with the node removed — while the model-visible state was byte-for-byte
#     identical (29 interactive elements, 853 chars of prompt text, both times).
#   A/B for the same page:
#     sprite present         20.7 / 27.3 s
#     sprite display:none    16.4 s   ← hiding is NOT enough, CDP still walks the node
#     sprite removed          0.80 s
#   21 s of every single step was spent walking 33k decorative <path> elements. At the
#   observed 28–43 s/step that was ~96% of the DOM cost and the bulk of each step.
#
# Why prune SYMBOLS rather than delete the whole sprite: different pages use different
# icons (`icon-login-box-bg` etc. ARE used on the login page), so nuking the container
# would break real icons and change what the test sees. Only symbols that no <use>
# references are dropped, which is by construction invisible.
#
# Why a MutationObserver and not a one-shot pass: it is a SPA. Vue mounts, routes change,
# and the icon set can grow after our injection runs. Pruning on every mutation keeps the
# node count down for the whole case, not just at t=0. The pass is idempotent and cheap
# once there is nothing to drop (one querySelectorAll over <use>).
#
# Best-effort by design: if this fails we lose speed, never a run.
# ---------------------------------------------------------------------------

_SPRITE_PRUNE_JS = r"""
(() => {
  if (window.__tpSpritePrune) return 'already-installed';
  const SPRITE_IDS = ['__svg__icons__dom__'];

  const prune = () => {
    let dropped = 0;
    for (const id of SPRITE_IDS) {
      const svg = document.getElementById(id);
      if (!svg) continue;
      // Every symbol still referenced anywhere in the document.
      const used = new Set();
      for (const u of document.querySelectorAll('use')) {
        const href = u.getAttribute('href') || u.getAttribute('xlink:href') || '';
        if (href.startsWith('#')) used.add(href.slice(1));
      }
      // Symbols already kept in a previous pass are kept again: cheaper than
      // re-deciding, and a symbol can be referenced later by a newly-mounted view.
      for (const sym of Array.from(svg.querySelectorAll('symbol'))) {
        if (used.has(sym.id) || sym.dataset.tpKept === '1') continue;
        const n = sym.querySelectorAll('*').length;
        sym.remove();
        dropped += n + 1;
      }
    }
    return dropped;
  };

  // Mark the symbols that are in use right now so we never drop them later.
  const markUsed = () => {
    for (const id of SPRITE_IDS) {
      const svg = document.getElementById(id);
      if (!svg) continue;
      for (const u of document.querySelectorAll('use')) {
        const href = u.getAttribute('href') || u.getAttribute('xlink:href') || '';
        if (!href.startsWith('#')) continue;
        const sym = svg.querySelector('symbol[id="' + CSS.escape(href.slice(1)) + '"]');
        if (sym) sym.dataset.tpKept = '1';
      }
    }
  };

  const run = () => { try { markUsed(); return prune(); } catch (e) { return -1; } };

  window.__tpSpritePrune = { run };
  const first = run();

  // 观察目标必须是**任何时刻都存在**的节点。
  //
  // 这里此前写的是 document.documentElement，而本脚本是在**新文档创建时**执行的
  // （Page.addScriptToEvaluateOnNewDocument）—— 那一刻 documentElement 还不存在，
  // obs.observe(null) 直接抛异常并被 catch 吞掉。后果极其隐蔽：marker 装上了、
  // 一次性 pass 也跑了（但那时精灵还没被页面 JS 创建，什么也没删到），
  // **观察器却从未生效**，于是页面随后创建的精灵一直留着。
  // 实测（2026-10-02）：导航后 window.__tpSpritePrune 存在、而 path 数始终是 32979；
  // 紧接着 browser-use 在导航那一刻构建 DOM 树，CDP 报
  // "TimeoutError: CDP requests failed or timed out: dom_tree, device_pixel_ratio"
  // ——单步因此白等 31 秒。document 本身始终存在，是可靠的观察根。
  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    // debounce：重 SPA 的 DOM 变更极频繁，逐次跑全量 prune 会拖慢页面本身。
    setTimeout(() => { scheduled = false; run(); }, 50);
  };

  try {
    const obs = new MutationObserver(schedule);
    obs.observe(document, { childList: true, subtree: true });
    window.__tpSpritePrune.observerAttached = true;
  } catch (e) {
    window.__tpSpritePrune.observerAttached = false;
    window.__tpSpritePrune.observerError = String(e);
  }

  // 兜底：DOM 解析完成后再跑一次（有些 SPA 在 DOMContentLoaded 之后才注入精灵）。
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { run(); }, { once: true });
  }

  return 'installed, dropped ' + first + ', observer=' + !!window.__tpSpritePrune.observerAttached;
})()
"""


# ---------------------------------------------------------------------------
# 批处理的硬保证：**一批里永远不点两次**
#
# 背景：用户想要快一点，但明确说"如果有点偏的概率，我不建议这样做"。
# browser-use 的 `multi_act` 本身有两层防点偏保护：
#   1. 静态标记：navigate / search / go_back / switch 会中止后续队列
#   2. 运行时检测：每个动作后比较 URL 与焦点，变了就中止剩余队列
# 但**缺口**是它只查 URL 和焦点 —— 点击弹出一个弹窗时两者都不变，检测不到，
# 后续动作仍会落在已经挪位的元素上（"想点『下一步』却触发『取消』"就是这么来的）。
#
# 所以这里不靠 prompt 自律，而是在代码层做确定性保证：把一批动作截断到
# "至多一个会改变页面的动作"。规则是从头保留，遇到第一个非安全动作就把它包进来然后停止：
#     [input, input, click, click] -> [input, input, click]   两个输入照批，只留第一个点击
#     [click, click]               -> [click]
#     [input, input]               -> [input, input]          全是安全动作，原样保留
# 安全动作定义为**不改变页面结构**的那些：input / send_keys / scroll / extract。
# 它们同批执行不会让后续索引失效，而且真人填表时也确实会连着填几个框。
# ---------------------------------------------------------------------------
_SAFE_TO_BATCH = frozenset({"input", "send_keys", "scroll", "extract"})


def _action_name(action) -> str:  # noqa: ANN001
    """取出动作名（ActionModel 是 {动作名: 参数} 的单键结构）。"""
    try:
        data = action.model_dump(exclude_unset=True)
    except Exception:  # noqa: BLE001
        return ""
    return next(iter(data.keys()), "") if data else ""


def _truncate_for_safety(actions: list) -> tuple[list, bool]:
    """把一批动作截到"至多一个会改页面的动作"。返回 (新列表, 是否截断)。"""
    if len(actions) <= 1:
        return actions, False
    kept: list = []
    for a in actions:
        kept.append(a)
        if _action_name(a) not in _SAFE_TO_BATCH:
            break  # 第一个"可能改变页面"的动作：执行它，后面全部丢掉
    return kept, len(kept) < len(actions)


def _install_safe_batching() -> bool:
    """给 Agent.multi_act 打上截断补丁。成功返回 True。"""
    try:
        from browser_use.agent.service import Agent

        original = Agent.multi_act
        if getattr(original, "_tp_safe_batch", False):
            return True  # 已打过，别叠加包裹

        async def safe_multi_act(self, actions):  # noqa: ANN001
            kept, truncated = _truncate_for_safety(list(actions))
            if truncated:
                log.info(
                    "executor: 批处理含『会改变页面』的动作，已截断为 %d 个（原 %d 个）"
                    "—— 防止后续动作落在挪位后的元素上",
                    len(kept), len(actions),
                )
            return await original(self, kept)

        safe_multi_act._tp_safe_batch = True
        Agent.multi_act = safe_multi_act
        return True
    except Exception as exc:  # noqa: BLE001 — 加固项绝不能反过来搞死用例
        log.warning("executor: 批处理安全补丁安装失败（%s），将保守回退", exc)
        return False


_SAFE_BATCHING_INSTALLED = _install_safe_batching()


def _sprite_prune_js() -> str:
    """Kept as a function so tests can assert on the emitted script's shape."""
    return _SPRITE_PRUNE_JS


# ---------------------------------------------------------------------------
# 掐掉 browser-use 每步强制的截图
#
# agent/service.py 里这一步是**写死**的：
#     browser_state_summary = await self.browser_session.get_browser_state_summary(
#         include_screenshot=True,   # always capture even if use_vision=False ...
#     )
# 注释说"反正很快"，但实测在 2880x1800 的窗口上并不快：profiler 抓到每步热点就是
# `screenshot_watchdog.on_ScreenshotEvent → Page.captureScreenshot → await future`，
# 而每步 11.3s 里 DOM(0.3s)+LLM(0.5s) 只占 0.8s，其余基本都耗在这条链上。
#
# 我们**不需要 browser-use 自己那张图**：逐步证据由 `live_shot_every` 控制，
# 走的是 `_grab_shot` 的独立 CDP 调用（`browser.take_screenshot()`），
# 用例结束时那张「最终截图」也是独立的调用。
# 掐掉这一处是为了**避免同一步在 CDP 通道上截两次**（browser-use 一次 + 我们一次），
# 而不是为了取消截图 —— 用户已要求默认每步都截，截图照常存在。
# 想让 browser-use 自己那张也回来的话，删掉 `_disable_per_step_screenshots()` 的调用即可。
#
# 失败必须静默跳过：这是提速项，绝不能影响用例执行。
# ---------------------------------------------------------------------------
def _disable_per_step_screenshots() -> bool:
    """让所有 get_browser_state_summary 都不带截图。成功返回 True。"""
    try:
        from browser_use.browser.session import BrowserSession

        original = BrowserSession.get_browser_state_summary
        if getattr(original, "_tp_no_screenshot", False):
            return True  # 已经打过，别叠加包裹

        async def without_screenshot(
            self, include_screenshot: bool = True, cached: bool = False,
            include_recent_events: bool = False,
        ):
            # 参数原样收下再丢掉 include_screenshot，这样调用方（含关键字与位置传参）
            # 都不会因为签名变化而报错。
            return await original(
                self,
                include_screenshot=False,
                cached=cached,
                include_recent_events=include_recent_events,
            )

        without_screenshot._tp_no_screenshot = True
        BrowserSession.get_browser_state_summary = without_screenshot
        return True
    except Exception:  # noqa: BLE001
        return False


_NO_PER_STEP_SHOTS = _disable_per_step_screenshots()


# ---------------------------------------------------------------------------
# 【已实测否决，勿重开】把导航等待从 'load' 降级为 'domcontentloaded'
#
# 动机看起来成立：browser-use 的 NavigateToUrlEvent.wait_until 默认 'load'，等不到时的
# 超时是硬编码的（session.py: `timeout = 3.0 if same_domain else 8.0`），而被测前端是
# 6.5MB 的 SPA，日志里 "⚠️ Page readiness timeout" 累计出现 418 次 —— 很像是白等。
#
# 但**实测否决**（2026-10-03，同一批 URL 各导航一次）：
#     load            10.35s / 4.48s / 6.11s   合计 20.94s
#     domcontentloaded 11.21s / 9.08s / 10.54s  合计 30.84s   ← 反而更慢 9.9s
# 三次逐项都更慢，不是噪声。所以这个"优化"被回退了，只留下这段记录，
# 避免以后有人（包括我自己）再按同样的推理重做一遍。
#
# 结论：那 8 秒超时虽然看着浪费，但它是**和页面的真实加载行为绑在一起**的；
# 换档不会让页面更快就绪，只是换了等待方式。要动这个得先有更强的证据。
# ---------------------------------------------------------------------------


def _sprite_prune_enabled() -> bool:
    try:
        return bool(get_settings().browser_prune_icon_sprite)
    except Exception:  # noqa: BLE001 — a settings hiccup must not break execution
        return True


# Failure signatures already reported. The pruner is best-effort and runs every step, so
# a persistent failure would otherwise emit one warning per step; the FIRST one is the
# actionable one and must not be swallowed (it was `log.debug`, which meant a totally
# broken pruner looked identical to a working one in the log — that is how a 30 s/step
# DOM timeout went unnoticed).
_PRUNE_WARNED: set[str] = set()

# 注：这里曾有一个"_PRUNE_TARGET_WAIT_S 有界等待"，2026-10-05 已删除。
# 原因是方向错了：`get_page_targets()` 在 session_manager 未初始化时恒返回空，
# 等多久都没用。正确的做法是回落到 `get_or_create_cdp_session()`，
# 见 _install_prune_on_session 的说明。


def _warn_prune_once(key: str, msg: str, *args) -> None:
    if key in _PRUNE_WARNED:
        return
    _PRUNE_WARNED.add(key)
    log.warning(msg, *args)


async def _install_sprite_pruner(browser) -> None:
    """Install the icon-sprite pruner on every live page target.

    Why this is re-run rather than registered once: browser-use 0.13.10 does not expose
    a Playwright page here (`get_current_page()` returns its own CDP-backed `Page`, which
    has no `add_init_script`), and a browser-level
    `Page.addScriptToEvaluateOnNewDocument` is rejected with -32601 because `Page.*` is
    not routable at the browser target. A session-scoped registration is dropped as soon
    as the session is torn down — which happens on navigation. So the pruner is
    (re)installed per target, and `_ensure_sprite_pruner` re-checks it every agent step.
    """
    if not _sprite_prune_enabled():
        log.info("executor: 图标精灵裁剪已关闭（browser_prune_icon_sprite=false）")
        return
    # 2026-10-05：拿 target 的方式换了。
    # `get_page_targets()` 在 `session_manager` 还没初始化时**永远返回 []**（见其源码
    # 第一行 `if not self.session_manager: return []`）。browser-use 0.13 的 Browser 是
    # 懒加载的，capture_session 这条路径上预装时必然命中这个分支 —— 实测等满 12s 仍然
    # 报"找不到页面目标"，裁剪器整轮没装上。
    #
    # `get_or_create_cdp_session()` 不依赖 session_manager：它会自己建一个会话出来。
    # 所以两条路都试：先拿页面列表（能拿到就按 target 逐个装，语义更清楚），
    # 拿不到就直接用当前会话装 —— 后者才是让这条路径真正装上的那条路。
    targets: list = []
    try:
        targets = browser.get_page_targets() or []
    except Exception as exc:  # noqa: BLE001
        log.warning("executor: ★ 取页面目标失败（改用当前会话安装）：%s", exc)
    if not targets:
        try:
            cdp = await browser.get_or_create_cdp_session()
        except Exception as exc:  # noqa: BLE001
            _warn_prune_once(
                "nosession",
                "executor: ★ 拿不到 CDP 会话，图标精灵裁剪未安装 —— 本轮每步的 DOM 采集会慢 20s 以上：%s",
                exc,
            )
            return
        installed = await _install_prune_on_session(cdp)
        if installed:
            log.info("executor: 图标精灵裁剪已在当前 CDP 会话安装")
        return

    installed = 0
    for tgt in targets:
        tid = getattr(tgt, "target_id", None)
        if not tid:
            continue
        try:
            cdp = await browser.get_or_create_cdp_session(target_id=tid, focus=False)
            if await _install_prune_on_session(cdp):
                installed += 1
                log.info("executor: 图标精灵裁剪已装到 target=%s", str(tid)[:12])
        except Exception as exc:  # noqa: BLE001 — per-target, but must be loud
            log.warning(
                "executor: ★ 图标精灵裁剪装到 target=%s 失败：%s —— 本轮该页面每步可能慢 20s+",
                str(tid)[:12], exc,
            )
    if installed:
        log.info("executor: 图标精灵裁剪已在 %s 个页面上安装", installed)


async def _install_prune_on_session(cdp) -> bool:
    """在一个已经拿到的 CDP 会话上装裁剪器。成功返回 True，永不抛。

    抽成独立函数的原因：浏览器刚建好时 `session_manager` 还没初始化，
    `get_page_targets()` 恒返回空 —— 那时唯一能用的入口就是
    `get_or_create_cdp_session()` 直接给的这个会话。两条路都要能装。
    """
    try:
        # (a) 注册到该 target 的**未来每一个**文档。
        #     这一步才是让裁剪器熬过导航的关键：agent 随后的跳转会重建文档，
        #     只在当前文档里注入的话，一跳就没了。
        try:
            await cdp.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(
                params={"source": _sprite_prune_js()},
                session_id=cdp.session_id,
            )
        except Exception as exc:  # noqa: BLE001 — 下面的即时执行仍然有用
            _warn_prune_once(
                "addscript", "executor: 图标精灵裁剪无法注册到新文档（导航后会失效）：%s", exc
            )
        # (b) 对**已经存在**的文档立刻跑一遍。
        res = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": _sprite_prune_js(), "returnByValue": True},
            session_id=cdp.session_id,
        )
        got = (res or {}).get("result", {}).get("value")
        if isinstance(got, int) and got > 0:
            log.info("executor: 图标精灵裁剪即时清掉 %s 个节点", got)
        return True
    except Exception as exc:  # noqa: BLE001
        _warn_prune_once("onsession", "executor: ★ 图标精灵裁剪装到当前会话失败：%s", exc)
        return False


async def _ensure_sprite_pruner(browser) -> None:
    """Per-step re-arm: run the prune pass on the active page, re-inject if it was lost.

    Called from the agent's step callback. Two things can wipe the pruner between steps:
    a navigation (new document → `window.__tpSpritePrune` gone) and a rebuilt sprite. The
    expression below therefore re-injects the full script when the marker is missing and
    otherwise just re-runs the pass (idempotent).

    Never raises — but a failure is reported once at WARNING, because an unnoticed broken
    pruner is indistinguishable from a slow page except by the 30 s DOM timeout it causes.
    """
    if not _sprite_prune_enabled():
        return
    try:
        cdp = await browser.get_or_create_cdp_session()
        res = await cdp.cdp_client.send.Runtime.evaluate(
            params={
                "expression": (
                    # 返回带前缀的字符串，好区分"这次才补上"和"本来就在"。
                    # 只有 dropped>0 才打日志的话，一次成功但当时页面里还没有精灵的
                    # 补装会完全静默 —— 那就又回到"装没装上只能靠猜"的老问题。
                    "(() => { const fresh = !window.__tpSpritePrune; "
                    "if (fresh) { " + _sprite_prune_js() + " } "
                    "try { return (fresh ? 'fresh:' : 're:') + window.__tpSpritePrune.run(); } "
                    "catch (e) { return 'err:' + e; } })()"
                ),
                "returnByValue": True,
            },
            session_id=cdp.session_id,
        )
        got = (res or {}).get("result", {}).get("value")
        if isinstance(got, str) and got.startswith("fresh:"):
            # 补上了：这一步之前是裸的，说明上一步的 DOM 采集是慢的
            log.info("executor: 图标精灵裁剪本步才补上（此前失效），清掉 %s", got[6:])
        elif isinstance(got, str) and got.startswith("err:"):
            _warn_prune_once("ensure_err", "executor: ★ 图标精灵裁剪每步执行报错：%s", got)
        elif isinstance(got, str) and got.startswith("re:") and got != "re:0":
            log.debug("executor: 图标精灵裁剪例行清理 %s 个节点", got[3:])
    except Exception as exc:  # noqa: BLE001
        _warn_prune_once(
            "ensure", "executor: ★ 图标精灵裁剪每步补装失败：%s —— 后续每步可能慢 20s+", exc
        )



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
        # The asyncio lock only serialises cases INSIDE this process. A Chromium that
        # outlived a killed worker is invisible to it, and removing the Singleton links
        # without checking (the old behaviour) actively caused the reported failure.
        owners = await _wait_for_profile_free(self.path)
        if owners:
            # Still held after the grace period → it is an orphan, not a graceful exit.
            await asyncio.to_thread(_kill_orphans, owners, self.path)
            owners = await _wait_for_profile_free(self.path, timeout_s=10.0)
        if owners:
            log.warning(
                "executor: 槽位 %s 仍有 pid=%s 占用，本次用例很可能启动失败", key, owners
            )
        else:
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
# SIGKILLed worker (deploy, OOM, service restart mid-run) leaves them behind — and the
# next launch then REFUSES to start rather than stealing the lock:
#   "Browser process exited before CDP became available" / ProcessSingleton.
#
# 2026-10-02 — WHY THIS IS NOW GUARDED:
# Removing these links unconditionally made things WORSE in one specific case. Eight
# Chromium processes from a killed run were still alive and still holding
# profiles/project_1/slot0. Deleting the links removed the *only* signal left that the
# profile was busy: the next launch then started Chromium, Chromium saw the live lock,
# and exited immediately without ever opening its CDP port — which is exactly the
# reported error, and it repeated on every case because the zombies never went away.
# So: only clear the links when the profile has NO live owner (see _profile_owner_pids).
_SINGLETON_LINKS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_stale_singleton(path: str) -> None:
    """Drop stale Singleton links. Caller MUST have established nobody owns the profile."""
    for name in _SINGLETON_LINKS:
        p = os.path.join(path, name)
        try:
            if os.path.islink(p) or os.path.exists(p):
                os.unlink(p)
        except OSError as exc:
            log.warning("executor: could not clear %s: %s", p, exc)


def _list_processes() -> list[tuple[int, str]]:
    """[(pid, command_line)] for this machine, best effort. Read-only.

    Used to answer 'is some other Chromium still holding this profile?'. Windows has no
    /proc, so this shells out to PowerShell. Every failure mode (no PowerShell, timeout,
    unparseable output, non-Windows) returns an empty list, which the callers treat as
    'cannot prove there is an owner' — never as 'safe to delete'.
    """
    import subprocess  # noqa: PLC0415 — only needed on this fallback path

    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "ForEach-Object { $_.ProcessId.ToString() + '|' + ($_.CommandLine -replace \"`r?`n\",' ') }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=25,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("executor: process listing unavailable (%s)", exc)
        return []
    text = out.stdout.decode("utf-8", "replace") or out.stdout.decode("gbk", "replace")
    procs: list[tuple[int, str]] = []
    for line in text.splitlines():
        pid_s, sep, cmd = line.partition("|")
        if not sep:
            continue
        try:
            procs.append((int(pid_s.strip()), cmd))
        except ValueError:
            continue
    return procs


def _profile_owner_pids(path: str) -> list[int]:
    """PIDs of live Chromium processes launched with `--user-data-dir=<path>`.

    Deliberately matches on the user-data-dir argument rather than on the lock files:
    Windows releases the file handle as soon as the check ends, so probing is a race.
    The command line is stable for the process's whole lifetime.

    Matches children too (--type=gpu-process keeps the same --user-data-dir): they are
    exactly what keeps a profile locked after the browser process itself has exited.
    """
    target = os.path.normcase(os.path.abspath(path)).rstrip("\\")
    owners: list[int] = []
    for pid, cmd in _list_processes():
        if not _is_chromium(cmd):
            continue
        udd = _udd_of(cmd)
        if udd and os.path.normcase(os.path.abspath(udd)).rstrip("\\") == target:
            owners.append(pid)
    return owners


# Chromium spawns a tree: one browser process plus --type=gpu-process / utility /
# renderer children, ALL carrying the same --user-data-dir. Killing "the" process is
# meaningless — every level has to go, or a child keeps the profile locked.
_CHROME_EXE_RE = re.compile(r"[\\/](chrome|chromium|msedge|brave|vivaldi)\.exe", re.I)


def _udd_of(cmd: str) -> str | None:
    """The --user-data-dir value of a command line, unquoted. None when absent."""
    for tok in cmd.split():
        if tok.lower().startswith("--user-data-dir="):
            return tok.split("=", 1)[1].strip('"')
    return None


def _is_chromium(cmd: str) -> bool:
    """True for any Chromium-family process, main or child, on Windows or POSIX paths."""
    return bool(cmd) and (_CHROME_EXE_RE.search(cmd) is not None or "chrome" in cmd[:200].lower())


def _kill_orphans(pids: list[int], path: str) -> int:
    """Kill leftover Chromium processes holding a profile. Returns how many were killed.

    Safety: every PID is re-verified from a FRESH process listing before being killed, and
    must still be a Chromium whose --user-data-dir is exactly `path`. PIDs get recycled, so
    killing a remembered number blindly can take out an unrelated process — the same rule
    the desktop .bat follows for the bot.

    Children are killed too (`taskkill /T`), because a surviving `--type=utility` child
    keeps the profile's LOCK file open even after the browser process is gone — which is
    what made the first version of this function look like it had failed.
    """
    if not pids:
        return 0
    import subprocess  # noqa: PLC0415

    target = os.path.normcase(os.path.abspath(path)).rstrip("\\")
    by_pid = {pid: cmd for pid, cmd in _list_processes()}
    killed = 0
    for pid in pids:
        cmd = by_pid.get(pid, "")
        if not _is_chromium(cmd):
            log.warning("executor: pid %s 已不是浏览器进程，跳过", pid)
            continue
        udd = _udd_of(cmd)
        if udd is None or os.path.normcase(os.path.abspath(udd)).rstrip("\\") != target:
            log.warning("executor: pid %s 的 user-data-dir 不是 %s，跳过", pid, path)
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
                check=False,
            )
            killed += 1
            log.warning("executor: 清理僵尸浏览器 pid=%s（占用 %s）", pid, path)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("executor: 无法结束 pid %s: %s", pid, exc)
    return killed


def _kill_all_orphans_under(root: str) -> int:
    """Sweep EVERY Chromium under `root` in one pass, parents and children alike.

    Called after the targeted kill: killing a browser process can leave a detached child
    (or Chrome can re-spawn one) still holding the profile. Re-listing afterwards catches
    anything the first pass raced with.
    """
    import subprocess  # noqa: PLC0415

    root_n = os.path.normcase(os.path.abspath(root)).rstrip("\\")
    doomed: list[int] = []
    for pid, cmd in _list_processes():
        if not _is_chromium(cmd):
            continue
        udd = _udd_of(cmd)
        if udd and os.path.normcase(os.path.abspath(udd)).rstrip("\\").startswith(root_n):
            doomed.append(pid)
    killed = 0
    for pid in doomed:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
                check=False,
            )
            killed += 1
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("executor: 无法结束 pid %s: %s", pid, exc)
    return killed


def reap_orphan_browsers() -> dict:
    """Start-up hygiene: kill Chromium left over from a previous, killed process.

    A worker that is SIGKILLed (service restart, OOM, taskkill) never runs its shutdown
    path, so its Chromium children keep the profile locks and every later case dies with
    "Browser process exited before CDP became available". Nothing else will ever clean
    them up: they are not children of the new process.

    Only touches browsers whose --user-data-dir is inside our own profile root, so a
    person's own Chrome windows are never at risk.
    """
    root = _profile_root()
    root_n = os.path.normcase(os.path.abspath(root)).rstrip("\\")
    victims: list[int] = []
    for pid, cmd in _list_processes():
        if not _is_chromium(cmd):
            continue
        udd = _udd_of(cmd)
        if udd and os.path.normcase(os.path.abspath(udd)).rstrip("\\").startswith(root_n):
            victims.append(pid)

    # `_kill_orphans` needs an exact slot path per process; here we are sweeping the whole
    # tree, so go straight to the "everything under root" variant — calling the exact-match
    # one with the root would just log a skip for every PID, which reads like a bug.
    killed = _kill_all_orphans_under(root) if victims else 0
    return {"found": len(victims), "killed": killed}


async def _wait_for_profile_free(path: str, *, timeout_s: float = 30.0) -> list[int]:
    """Wait until no live Chromium owns `path`. Returns the surviving PIDs (empty = free).

    Must stay async: `_list_processes` shells out to PowerShell and takes ~0.5-1s, which
    would stall every other case on the loop if this were a blocking call. The process
    listing itself goes through asyncio.to_thread, and the pause is a real await.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        owners = await asyncio.to_thread(_profile_owner_pids, path)
        if not owners:
            return []
        if time.monotonic() >= deadline:
            return owners
        # A previous case's graceful shutdown (CDP Browser.close) needs a moment to exit.
        await asyncio.sleep(0.25)


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
        cdp = await asyncio.wait_for(browser.get_or_create_cdp_session(), timeout=10)
        # Bounded: on a hung/timeout run the CDP channel is exactly what may be stuck, and
        # an unbounded close here would burn the rest of the case budget after the agent
        # already gave up — losing the video we are trying to save.
        await asyncio.wait_for(
            cdp.cdp_client.send.Browser.close(session_id=cdp.session_id), timeout=15
        )
        # Give Chromium a moment to finish writing before we hand the shard to the next case.
        await asyncio.sleep(1.5)
    except Exception as exc:  # noqa: BLE001 — a dead browser has nothing to flush
        log.debug("executor: CDP Browser.close unavailable (%s), falling back", exc)
    await _safe_async(lambda: browser.stop())


# ---------------------------------------------------------------------------
# 跨用例复用浏览器进程
#
# 为什么值得做：实测 `Browser.start()` 每用例 6.93s（378 个用例 ≈ 43 分钟），而这笔钱
# 全花在"拉起一个新的 Chromium 进程"上 —— 上一个用例用完就被关掉了。
#
# 复用机制用 browser-use 自己的 `keep_alive`：设了它之后 stop() 不会真的杀进程，
# 只发一个事件就返回（session.py: `if keep_alive and not force: return`）。
# 所以 Agent 跑完后浏览器仍然活着，下一个用例把页面重置一下就能接着用。
#
# 池的 key 用 profile 目录（ProfileLease 的 path）——profile 本来就是按
# project + 并发槽位分片的，所以不同项目、不同槽位天然不会串用。
# 没有 lease（persistent_profile=False）时自动退回"一用例一浏览器"的老行为。
# ---------------------------------------------------------------------------

_BROWSER_POOL: dict[str, object] = {}


async def _reset_browser_for_reuse(browser) -> bool:
    """把复用的浏览器恢复成干净状态；失败返回 False，调用方应丢弃它重开。

    要清掉上一个用例留下的两类东西：
      * 多余的标签页 —— agent 可能自己开过新 tab，留着会让下一个用例在错误的页面上操作；
      * 当前页面的路由与 DOM —— 不重置的话新用例直接看到上一个用例的界面，
        agent 会以为自己"已经到过"某处，白白少走或走错步骤。
    只留一个标签页并导航到 about:blank 就够了。**cookie/localStorage 是故意保留的** ——
    持久化 profile 的意义正是让登录态跨用例复用。
    """
    try:
        pages = await browser.get_pages()
    except Exception as exc:  # noqa: BLE001
        log.warning("executor: 复用浏览器时取页面列表失败：%s", exc)
        return False
    try:
        for extra in pages[1:]:
            try:
                await browser.close_page(extra)
            except Exception:  # noqa: BLE001 — 关不掉某个 tab 不致命
                pass
        # 注意 get_current_page() 是**协程**（实测漏了 await 会让 current 变成 coroutine，
        # 然后 coroutine.goto 抛 AttributeError，重置次次失败、复用直接失效）。
        current = await browser.get_current_page()
        if current is None:
            await browser.new_page()
        else:
            await current.goto("about:blank")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("executor: 复用浏览器时重置页面失败：%s", exc)
        return False


async def _revive_pooled_browser(browser) -> bool:
    """把池里的浏览器从「进程活着但事件总线已停」恢复到可用状态。

    这是跨用例复用最关键的一环，踩过一次很贵的坑：
    `Agent.close()` 在 **keep_alive=True** 的分支里会
        await event_bus.stop(clear=False); event_bus.event_queue = None
    ——进程确实还在（keep_alive 保住了），但**事件总线被拆了**，于是 `start()` 连事件都发不出去，
    报
        Client is not started. Call start() first or use as async context manager.
    结果池里每个实例都判为不可用 → 丢弃重开 → 还因为旧进程占着 profile 导致重开要 40+ 秒，
    **比不复用还慢**（不复用只要 6 秒）。

    BrowserSession.stop() 的尾部本来就有重建逻辑（它的 docstring 也写明"计划稍后重连时用"），
    这里照做：重建事件总线 → 注册 watchdog → 再重连 CDP。
    """
    try:
        from browser_use.browser.session import ResilientEventBus

        # 无条件重建事件总线。
        #
        # 一开始想用 `event_queue is None` 当"总线已停"的判据，**实测不成立**：
        # agent.close() 把队列置 None 之后 bubus 内部可能又把它补回来，判据永远为假，
        # 于是重建从不发生 → 重置页面照样报 "Client is not started"。
        # 这里不猜状态，直接照 BrowserSession.stop() 尾部的做法重建再重连；
        # 重建对正常的总线也安全（handlers 会由 _register_session_event_handlers 重新挂上）。
        browser.event_bus = ResilientEventBus()
        register = getattr(browser, "_register_session_event_handlers", None)
        if callable(register):
            register()
        log.info("executor: 复用前已重建事件总线并重挂 watchdog")
        # 总线好了再重连 CDP（Chromium 进程本来就还在）
        await browser.start()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("executor: 复用浏览器复活失败：%s", exc)
        return False


async def _acquire_browser(pool_key: str | None, factory, *, reuse: bool):
    """取一个可用浏览器：命中池就复用，否则新建。返回 (browser, reused)。

    注意**不要**对池里那个已经在跑的实例再调 start()：实测在它已导航过之后调 start()
    会把 CDP WebSocket 弄断（日志出现 "CDP WebSocket message handler exited
    unexpectedly" + 重连），反而更慢。池里的实例由 keep_alive 保着，本来就活着。
    """
    if reuse and pool_key:
        pooled = _BROWSER_POOL.get(pool_key)
        if pooled is not None:
            # 先"复活"：Agent 结束时事件总线被拆了（见 _revive_pooled_browser 的说明），
            # 不修好这一步，后面的重置页面必然报 "Client is not started"。
            if await _revive_pooled_browser(pooled):
                if await _reset_browser_for_reuse(pooled):
                    return pooled, True
            # 复活或重置失败：别赌了，丢掉重开（避免脏状态进下一个用例）。
            log.warning("executor: 池中浏览器不可用，改为重开（%s）", pool_key)
            _BROWSER_POOL.pop(pool_key, None)
            try:
                await _shutdown_browser(pooled)
            except Exception:  # noqa: BLE001
                pass
    browser = factory()
    await browser.start()
    if reuse and pool_key:
        _BROWSER_POOL[pool_key] = browser
    return browser, False


async def _close_browser_pool() -> None:
    """服务退出时优雅关闭池里所有浏览器（Chromium 只有在正常退出时才把 profile 落盘）。"""
    for key in list(_BROWSER_POOL):
        browser = _BROWSER_POOL.pop(key, None)
        if browser is None:
            continue
        try:
            await _shutdown_browser(browser)
        except Exception as exc:  # noqa: BLE001
            log.debug("executor: 关闭池中浏览器失败（%s）：%s", key, exc)


async def _wait_for_video(
    video_dir: str, *, timeout_s: float = 20.0, empty_timeout_s: float = 4.0
) -> str | None:
    """Wait for the recording file to settle, then return it.

    Playwright finalizes the video asynchronously during teardown, so reading the directory
    the instant the browser closes can miss it entirely (or catch a partially-written file
    that plays as a 0-second clip). Poll until the size stops growing — that is the file
    the browser is done with.

    两段等待上限，因为这两种情况的正确等待时间差一个数量级：
      * 文件已经出现但还在增长 → 等它稳定（timeout_s，编码确实需要时间）
      * 目录里**从头到尾没有文件** → 说明这次根本没录，等 20s 也等不出来。
        此前两种情况共用同一个 deadline，于是"没录到视频"的用例每个都白等 20 秒；
        在浏览器用例只能串行跑的前提下，这笔固定开销会乘以用例总数。
    """
    if not video_dir or not os.path.isdir(video_dir):
        return None
    started = time.monotonic()
    deadline = started + timeout_s
    best: str | None = None
    last_size = -1
    stable = 0
    while time.monotonic() < deadline:
        cur = _newest(os.path.join(video_dir, "*"))
        if cur:
            try:
                size = os.path.getsize(cur)
            except OSError:
                size = -1
            if cur == best and size == last_size and size > 0:
                stable += 1
                if stable >= 2:  # unchanged across two polls → final
                    return cur
            else:
                stable = 0
            best, last_size = cur, size
        elif best is None and time.monotonic() - started >= empty_timeout_s:
            # 从来没有出现过文件：不要再等剩下的时间了。
            log.info("executor: %ss 内视频目录没有出现文件，本次无录像（不继续等待）", empty_timeout_s)
            return None
        await asyncio.sleep(0.5)
    if best:
        log.warning("executor: 视频文件在 %ss 内没有稳定下来，仍按当前内容上传：%s", timeout_s, best)
    return best


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
    # 用例名。缺陷描述（app/failure_narrative.py）要用它做标题，报告里也靠它辨认是哪条用例。
    # 之前 CaseSpec **没有这个字段**，而 executor 里写着 `case_name=spec.name` —— 每遇到
    # 失败用例就抛 AttributeError，被 except 吞掉后只留一行"失败用例描述生成异常"，
    # 于是这个功能**一直是坏的**（日志实测：'CaseSpec' object has no attribute 'name'）。
    # 默认空串是为了兼容既有调用点，取值处一律用 `spec.name or f"case{spec.case_id}"` 兜底。
    name: str = ""
    expected: str = ""
    start_url: str | None = None
    login_state: str | None = None  # storage_state JSON string, or None
    login_username: str | None = None  # robot account for per-run prompt-login
    login_password: str | None = None
    # 2026-10-04 多角色：roles[1:] 的登录态，供 switch_account 工具中途切换身份。
    # 每项形如 {"role": str, "bundle": str|None, "user": str|None,
    #           "password": str|None, "label": str|None}。
    # 单角色时为空列表 —— 工具根本不会被注册，所以单角色用例的提示词与
    # 可用动作跟改动前完全一致，存量 557 条用例不受任何影响。
    extra_role_logins: list[dict] = field(default_factory=list)
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
    # 证据采集开关（由 engine 从 project 设置里读进来，用户在界面上自己选）。
    # None = 跟随服务器全局默认；True/False 明确要不要录像；shot_every 0 = 不截逐步图。
    # 做在这里而不是 executor 里查库：engine 拿到 project 时一次读完，少一次查询。
    record_video: bool | None = None
    shot_every: int | None = None
    # 数据隔离提示（可选）。实测这批用例里很多"预期"写死了具体条数/行名，
    # 而环境数据会被别的用例改动 —— 于是同一个用例两次跑出不同结果，看起来像
    # 不稳定，其实是数据前置条件不成立。
    # 用例作者在这里写清"怎么看待环境里已有的数据"，原样拼进任务提示。
    # 见 app/data_hygiene.py 的默认规则。
    data_hygiene: str | None = None


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
    # AI-written bug description for failed cases: {steps, actual, expected, title, severity}
    failure_narrative: dict | None = None
    latency_ms: int = 0
    error: str | None = None
    auth_failed: bool = False  # restored session was dead (ended on a login page) → self-heal
    timed_out: bool = False  # hit the wall-clock cap → a re-attempt cannot change the outcome
    # 判定器认定"失败是因为没做到位"而非"结果确实不符"（见 app/judge.py）。
    # engine 用它决定要不要重试：前者重试有救，后者重烧时间。
    evidence_gap: bool = False
    # 2026-10-04 失败根因分类（取值见 app/judge.py 的 _ROOT_CAUSES）。
    # 空串 = 通过、或判定器没能分类；engine/报告据此分组。
    root_cause: str = ""
    # 判定器引用了第几步作为依据（1-based）。空 = 它没说明依据，理由可信度要打折。
    verdict_evidence: list = field(default_factory=list)


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


# 步骤时间线里唯一值得看的一行，是「这一步点了啥、干了啥」。
# 光有工具名（click_element_by_index）测试员读不出信息，带上关键参数才成一句话。
# 上限是硬截断：长文本输入、整段 URL 会把时间线撑成一堵墙。
_DETAIL_VALUE_MAX = 40
_DETAIL_MAX = 160


def _action_detail(dumped: dict) -> str:
    """把一个模型输出压成人话：click_element_by_index(index=12)。"""
    parts: list[str] = []
    for tool, params in dumped.items():
        if not isinstance(params, dict):
            parts.append(str(tool))
            continue
        bits: list[str] = []
        for k, v in list(params.items())[:3]:
            s = str(v).strip()
            if len(s) > _DETAIL_VALUE_MAX:
                s = s[: _DETAIL_VALUE_MAX - 1] + "…"
            bits.append(f"{k}={s}")
        parts.append(f"{tool}({', '.join(bits)})" if bits else str(tool))
    return ", ".join(parts)[:_DETAIL_MAX]


def _summarize_actions(model_output) -> tuple[list[str], list[str]]:
    """(工具名列表, 人话列表)。

    实时回调和最终诊断两处都用它 —— 分开写迟早会漂移（一边有 detail 一边没有），
    而时间线是失败叙述的输入源，口径不一致会直接写进缺陷单。
    """
    names: list[str] = []
    details: list[str] = []
    for a in getattr(model_output, "action", None) or []:
        try:
            dumped = a.model_dump(exclude_none=True) if hasattr(a, "model_dump") else {}
        except Exception:  # noqa: BLE001
            dumped = {}
        names.extend(dumped.keys())
        detail = _action_detail(dumped)
        if detail:
            details.append(detail)
    return names, details


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
        detail = ""
        if mo is not None:
            thought = (
                getattr(mo, "thinking", None) or getattr(mo, "next_goal", None) or ""
            ).strip()
            actions, details = _summarize_actions(mo)
            detail = ", ".join(details)
        results = list(getattr(h, "result", None) or [])
        error = next((r.error for r in results if getattr(r, "error", None)), None)
        content = next(
            (r.extracted_content for r in results if getattr(r, "extracted_content", None)), None
        )
        steps.append(
            {
                "i": i + 1,
                "action": ", ".join(actions) or "—",
                # 「点了啥」的人话版。UI 默认只显示这一行 + 结果，thought 收进开关。
                "detail": detail,
                "thought": thought,
                "result": (content or "")[:600],
                "error": (error or "")[:600],
                "screenshot": paths[i] if i < len(paths) else None,  # local path; uploaded below
            }
        )
    return steps


def _looks_like_thrashing(steps: list, limit: int) -> tuple[bool, str]:
    """最近 limit 步是不是在**重复同一个动作、且反馈也相同**？

    抽成纯函数是为了能直接测：这段判定藏在 execute_case 的闭包里时只能靠源码断言，
    而"什么算死磕"恰恰最需要被用例固定住 —— 判太严会误停正常流程，判太松等于没设。

    判定条件（三者同时成立）：
      * 步数够 limit
      * 最近 limit 步的 (动作, 错误, 结果) 三元组完全一致
      * 该动作非空、且不是 done（done 是正常收尾）

    返回 (是否死磕, 动作描述)。
    """
    if limit <= 0 or len(steps) < limit:
        return False, ""
    tail = [d for d in steps[-limit:] if isinstance(d, dict)]
    if len(tail) < limit:
        return False, ""
    sigs = {
        (
            (d.get("action") or "?"),
            (d.get("error") or "").strip()[:40],
            (d.get("result") or "").strip()[:40],
        )
        for d in tail
    }
    if len(sigs) != 1:
        return False, ""
    act = (tail[-1].get("action") or "").strip()
    if not act or act == "done":
        return False, ""
    return True, act


def _build_tools(s, spec: "CaseSpec | None" = None):
    """The browser-use tool registry, with the pure time-wasters removed.

    Measured on this project's own history (2026-10-02): 44% of steps were
    `wait`/`scroll`/`search_page`/`find_elements`/`evaluate` — steps that changed nothing
    and only re-asked a question the page had already answered. `wait` and `search_page`
    are the two with essentially no legitimate use here:

      * `wait` — the agent already receives a fresh snapshot after every step, so waiting
        buys nothing it would not see anyway. Runs showed chains like wait→wait→wait with
        the model's own note "DOM not captured yet". `scroll` and `navigate` can recover
        a genuinely bad state; waiting cannot.
      * `search_page` — it re-reads text already present in the page state. Run #140
        searched "请选择审批节点", then searched "必填|请选择|审批节点" for the same fact,
        burning two full round-trips on an answer it already had. A hard removal is what
        makes this stick: the prompt asks nicely, removing the tool guarantees it.

    `scroll` 和 `extract` **保留** —— 这两个是真人真的会做的：scroll 用于长列表，
    extract 是从页面**已渲染的文本**里归纳（不执行脚本）。

    `evaluate` / `find_elements` 见 config.excluded_agent_actions 的说明：
    它们是"跳过界面的脚本捷径"，与"像真人一样操作"的要求冲突，已一并移除。

    Defensive by construction: on any registry API mismatch this returns the default
    registry rather than failing the case. Losing the speed tweak is acceptable; losing
    the run is not.
    """
    try:
        from browser_use import Tools

        tools = Tools()
        excluded = []
        for name in (getattr(s, "excluded_agent_actions", None) or []):
            try:
                tools.exclude_action(name)
                excluded.append(name)
            except Exception as exc:  # noqa: BLE001 — unsupported name, skip it
                log.debug("executor: 无法排除动作 %s：%s", name, exc)
        if excluded:
            log.info("executor: 已禁用低价值动作 %s（每步都是完整模型回合）", ", ".join(excluded))
        _add_switch_account(tools, spec)
        return tools
    except Exception as exc:  # noqa: BLE001 — never fail a case over a speed tweak
        log.warning("executor: 精简动作集失败，改用默认动作集：%s", exc)
        return None


def _add_switch_account(tools, spec: CaseSpec) -> None:
    """Register the multi-role identity switch (2026-10-04). No-op for single-role cases.

    Only registered when the case actually declares extra roles, so a single-role case's
    action set — and therefore its prompt and every step it can take — is byte-identical
    to before this feature existed. That matters: 557 of the existing cases are
    single-role, and they must not start seeing a tool they have no use for.

    How the switch works: the browser is already running with the previous identity's
    cookies, so switching means (a) dropping that identity's cookies and (b) seeding the
    new one's. It is NOT a fresh login — apps that keep auth in sessionStorage or behind
    an SSO redirect may still bounce to a login page, which the agent handles with the
    credentials we hand it. Verified end-to-end on a two-role case; see the log line below
    for the observable signal that a switch happened.
    """
    logins = list(getattr(spec, "extra_role_logins", None) or [])
    if not logins:
        return
    by_role = {r["role"]: r for r in logins}
    roles_text = ", ".join(by_role)

    try:

        @tools.action(
            "Switch to a different user account for the rest of this task. Use this ONLY "
            "when the work you are about to do needs another person's identity (e.g. the "
            f"item you just created has to be approved by someone else). Available: {roles_text}. "
            "After switching, the page may need a reload before the new identity's view appears."
        )
        async def switch_account(role: str) -> str:
            r = by_role.get((role or "").strip())
            if r is None:
                return (
                    f"没有名为「{role}」的身份。可切换的身份只有：{roles_text}。"
                    "不要臆造身份名——换一个上面列出的名字。"
                )
            bundle = r.get("bundle")
            if not bundle:
                return (
                    f"角色「{r['role']}」没有可用的已捕获会话（它的账号需要交互式登录，"
                    "当前无法自动切换）。请改用当前身份完成任务，并在结果里说明这一点。"
                )
            ok, err = await _switch_identity(_ACTIVE_BROWSER.get(), bundle)
            if not ok:
                return f"切换到「{r['role']}」失败：{err}。请继续用当前身份，或先刷新页面重试一次。"
            log.info("executor: 已切换身份 -> %s", r.get("label") or r["role"])
            return (
                f"已切换到「{r['role']}」。请刷新页面（必要时重新进入相关菜单）确认你看到的是"
                "新身份的内容，然后继续任务。"
            )

    except Exception as exc:  # noqa: BLE001 — a missing tool must not fail the case
        log.warning("executor: switch_account 工具注册失败（多角色用例将无法切换身份）：%s", exc)


# The live browser for the case currently executing. Set in execute_case before the agent
# runs and read by the switch_account closure above. A module-level cell is used instead
# of a closure over the browser because the tool is registered on a Tools object that
# outlives the registration call; the executor is single-case-per-task, so there is only
# ever one live browser.
_ACTIVE_BROWSER: dict = {}


async def _switch_identity(browser, bundle_json: str) -> tuple[bool, str]:
    """Replace the browser's current identity with `bundle_json`'s. Best-effort.

    Returns (ok, error). Never raises: a failed switch must leave the case runnable
    on the previous identity rather than aborting it.
    """
    if browser is None:
        return False, "浏览器会话已结束"
    try:
        # Clear first. Seeding without clearing leaves the old identity's cookies in
        # place, so the app keeps showing the previous user's data — which looks like a
        # successful switch but is not one.
        #
        # NOTE: clearing cookies alone is not a full logout on apps that keep auth in
        # localStorage/sessionStorage; _restore_session's seeding only writes keys that
        # are ABSENT, so a stale localStorage token from the previous identity would
        # survive. The agent is told to reload after switching and is given the
        # credentials, so a bounce to the login page is recoverable. Verified on a
        # two-role case; see the "已切换身份" log line for the observable signal.
        try:
            await browser._cdp_clear_cookies()  # noqa: SLF001 — private, pinned to 0.13.x
        except Exception as exc:  # noqa: BLE001
            return False, f"清除当前身份失败：{exc}"
        await _restore_session(browser, bundle_json)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --- 身份守卫（2026-10-06）-------------------------------------------------------
#
# 背景（实测，不是推测）：持久 profile 里留着**上一个登录者**的会话。项目 2 的
# profile 里躺着 admin 的 user / loginForm / ACCESS_TOKEN / REFRESH_TOKEN，结果是
# 179 条用例全部以 admin 身份跑完 —— 用例"通过"了，但测的是 admin 的菜单和数据
# 权限，结论不可用。而且登录框还被"记住密码"预填成 admin，agent 就算走到登录页
# 也会照着预填值提交。
#
# 守卫策略：用例开始前，在被测系统 origin 上读一次当前身份；与本用例应使用的账号
# 不一致就清掉登录态 + 记住密码，让 agent 用下发到提示词里的正确账号登录。
# 只在"期望账号已知"时运行 —— 没有可比对象就不动手，避免误伤。

# 读取当前身份。两段取值都要：
#   user      -> 已登录会话（{v:{user:{username}}}）
#   loginForm -> 登录框的"记住密码"预填（{v:{username}}）
_IDENTITY_READ_JS = """
(function(){
  function unwrap(raw){
    if(!raw) return null;
    try{
      var o=JSON.parse(raw);
      return (o && typeof o.v === 'string') ? JSON.parse(o.v) : o;
    }catch(e){ return null; }
  }
  var who='';
  var u=unwrap(localStorage.getItem('user'));
  if(u && u.user && u.user.username) who=u.user.username;
  else if(u && u.username) who=u.username;
  var form='';
  var f=unwrap(localStorage.getItem('loginForm'));
  if(f && f.username) form=f.username;
  var tok=localStorage.getItem('ACCESS_TOKEN')||localStorage.getItem('REFRESH_TOKEN');
  return JSON.stringify({who:who, form:form, token: tok?'yes':'no'});
})()
"""

# 清掉"这一个身份"的全部痕迹。roleRouters 是**按用户下发**的菜单树，留着会让
# agent 看到上一个账号的菜单，所以一并清。tenantId 与主题/语言/图标缓存保留 ——
# 它们与身份无关，留着能保住 profile 的预热收益。
_IDENTITY_KEYS = (
    "user",
    "loginForm",
    "ACCESS_TOKEN",
    "REFRESH_TOKEN",
    "roleRouters",
)

_IDENTITY_PURGE_JS = (
    "(function(){var K="
    + json.dumps(list(_IDENTITY_KEYS))
    + ";var n=0;for(var i=0;i<K.length;i++){"
    "if(localStorage.getItem(K[i])!==null){localStorage.removeItem(K[i]);n++;}"
    "if(sessionStorage.getItem(K[i])!==null){sessionStorage.removeItem(K[i]);n++;}"
    "}return String(n);})()"
)


def _identity_guard_enabled() -> bool:
    try:
        return bool(get_settings().browser_identity_guard)
    except Exception:  # noqa: BLE001 — 配置缺失时按"开"处理，宁可多查一次
        return True


async def _read_identity(browser) -> str:
    """当前浏览器里登录着的账号名（localStorage.user.user.username）。

    读不到就返回空串 —— 空串表示"不知道"，调用方必须把它当成**无法确认**而不是
    "确认没问题"。永不抛异常。
    """
    try:
        cdp = await browser.get_or_create_cdp_session()
        res = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": _IDENTITY_READ_JS, "returnByValue": True},
            session_id=cdp.session_id,
        )
        raw = (res or {}).get("result", {}).get("value")
        if not isinstance(raw, str):
            return ""
        return (json.loads(raw).get("who") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def _enforce_identity(browser, expected_user: str | None, url: str | None) -> str:
    """Make sure the browser is (or will be) logged in as `expected_user`.

    Returns a one-line, human-readable verdict for the log — never raises, because a
    guard that aborts the case is worse than a guard that silently does nothing.

    Verdicts:
      skip:…                 — nothing to compare against (no account name known)
      ok(who)                — already the right identity, profile warmth kept
      no-session             — nobody logged in and no stale prefill; nothing to do
      purged(old->new, N)    — wiped another identity's session/prefill, agent will log in
      <reason>               — guard could not run (CDP/navigation failed); case proceeds

    Why this exists: the persistent profile carries the PREVIOUS case's identity into
    the next one. Measured 2026-10-06 — project 2's profile held admin's session, so
    /login redirected straight to /index and every one of 179 cases ran as admin while
    reporting the role account. A wrong identity is worse than a failed login: the case
    passes and the result is fiction.
    """
    if not expected_user or not url:
        return "skip(no expected account)"
    try:
        await browser.start()
    except Exception as exc:  # noqa: BLE001
        return f"browser-start-failed:{type(exc).__name__}"
    try:
        cdp = await browser.get_or_create_cdp_session()
    except Exception as exc:  # noqa: BLE001
        return f"no-cdp:{type(exc).__name__}"

    async def _eval(js: str):
        res = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": js, "returnByValue": True},
            session_id=cdp.session_id,
        )
        return (res or {}).get("result", {}).get("value")

    # localStorage 是**按 origin** 隔离的，所以必须先落在被测系统的页面上。
    # 顺带这也是一次预热导航：agent 的第一步本来就要去这里。
    try:
        await browser.navigate_to(url)
    except Exception as exc:  # noqa: BLE001
        return f"nav-failed:{type(exc).__name__}"
    try:
        raw = await _eval(_IDENTITY_READ_JS)
        info = json.loads(raw) if isinstance(raw, str) else {}
    except Exception as exc:  # noqa: BLE001
        return f"read-failed:{type(exc).__name__}"
    who = (info.get("who") or "").strip()
    form = (info.get("form") or "").strip()

    # 两种情况都要处理，它们是独立的：
    #   who  —— 已经以别人的身份登录（页面根本不给你填账号的机会）
    #   form —— 登录框被"记住密码"预填成别人（agent 会照着提交）
    bad_session = bool(who) and who != expected_user
    bad_prefill = bool(form) and form != expected_user
    if not (bad_session or bad_prefill):
        if who:
            return f"ok({who})"
        return "no-session" if not form else f"ok-prefill({form})"

    try:
        n = await _eval(_IDENTITY_PURGE_JS)
    except Exception as exc:  # noqa: BLE001
        return f"purge-failed:{type(exc).__name__}"
    # 清完必须重新进一次页面，否则当前这个文档仍停留在旧身份渲染出来的界面上。
    try:
        await browser.navigate_to(url)
    except Exception:  # noqa: BLE001 — 导航失败就交给 agent 自己处理
        pass
    old = who or form
    return f"purged({old}->{expected_user}, {n} keys)"


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
    # 2026-10-04 多角色：告诉 agent 有哪些身份可用、何时该切。
    # 切换是**agent 主动调工具**完成的，所以必须写清楚"什么时候切"，否则它不会切。
    if spec.extra_role_logins:
        names = [r["role"] for r in spec.extra_role_logins]
        parts.append(
            "This task spans MULTIPLE accounts. You start as the current account. "
            f"You can switch to: {', '.join(names)}. "
            "Switch with the switch_account tool at the point in the flow where the work "
            "needs that other identity (e.g. after you submit something, if approving it "
            "requires a different person). Do the work that needs the CURRENT identity first, "
            "then switch. Never guess an identity — switch only to a role listed above. "
            "After switching, the page may need a reload before the new identity's view appears."
        )
    if spec.data_hygiene:
        parts.append(spec.data_hygiene)
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
    # 关掉 browser-use 的"有新版本了吗"联网检查。
    #
    # 2026-10-06 实测：它每次启动 Agent 都 GET https://pypi.org/pypi/browser-use/json
    #（browser_use/utils.py::check_latest_browser_use_version，timeout=3.0），
    # 实测这一项就吃掉 3.4 秒，而且它发生在"启动浏览器"计时**之外** —— 即每条用例
    # 都要白付3.4 秒。它只打印一行升级提示，对执行结果零影响。
    #
    # 用官方环境变量而不是改库源码：browser-use 升级会覆盖 site-packages 里的改动，
    # 而 env 是它自己认的配置入口（config.py:184读BROWSER_USE_VERSION_CHECK）。
    # 不设置默认值 —— 用户若想看升级提示，自己在 .env 里显式设 true 即可。
    if s.browser_version_check is False:
        os.environ["BROWSER_USE_VERSION_CHECK"] = "false"
    # 阶段计时。日志级别默认是 WARNING（见 run_server.py），所以这些数据最后会用**一条
    # WARNING** 汇总输出 —— 否则"每用例 170 多秒固定开销"在 INFO 里根本不可见。
    # 背景：对 13 个已完成用例做线性回归得到「固定开销 ≈171s/用例、每步 ≈16.6s」，
    # 在浏览器用例只能串行跑的前提下，固定开销无法靠并发摊薄，必须逐项拆开看。
    _stage: dict[str, float] = {}
    art = f"runs/{spec.result_id or spec.case_id}"  # this attempt's artifact folder
    final_shot: str | None = None

    with tempfile.TemporaryDirectory(prefix=f"tp_case_{spec.case_id}_") as workdir:
        video_dir = os.path.join(workdir, "video")
        trace_dir = os.path.join(workdir, "trace")
        os.makedirs(video_dir, exist_ok=True)
        os.makedirs(trace_dir, exist_ok=True)
        # 证据开关：项目设置优先，没设就跟随服务器全局默认（.env）。
        # 用户在界面上勾/取消"截图""录像"走的正是这条路径。
        _record_video = (
            spec.record_video if spec.record_video is not None else s.case_record_video
        )
        _shot_every = spec.shot_every if spec.shot_every is not None else s.live_shot_every
        # 操作经验记忆：把这条用例以前跑出来的"过程知识"（导航路径、界面脾气）拼进系统提示，
        # 减少每次重新摸索，让运行路径更稳。**里面不含任何判定结果**（见 app/case_memory.py
        # 的白名单与过滤），拿不到就是空串，绝不影响执行。
        _memory_note = ""
        try:
            from app.case_memory import note_for_case, related_notes

            note = await note_for_case(spec.case_id)
            if note:
                _memory_note = "\n\n" + note
            # 2026-10-04 上下文按需检索：再补一份"同项目其他用例"的界面经验。
            # 首跑的用例没有任何自身记忆，这份跨用例经验就是它唯一能少走弯路的来源。
            # 检索查询用用例的任务+预期（而不是用例名）—— 名字往往很短，
            # 二元组重合度的区分度不够，会捞回一堆沾边但不相关的条目。
            _pq = f"{spec.prompt} {spec.expected}".strip()
            related = await related_notes(
                project_id=spec.project_id or 0,
                query=_pq,
                exclude_case_id=spec.case_id,
            )
            if related:
                _memory_note += "\n\n" + related
        except Exception as exc:  # noqa: BLE001
            log.debug("executor: 读取经验笔记失败（忽略）：%s", exc)

        browser = None
        agent = None
        live_steps: list[dict] = []
        # Per-step wall clock. The step callback fires AFTER each step, so the gap between
        # two callbacks is that step's true cost (DOM collection + model round-trip +
        # action). Without this the report only had a case-total, and a case-total cannot
        # tell you whether 190 s went into 4 slow steps or 1 hung one. Measured 2026-10-02:
        # this is what exposed a 30 s DOMWatchdog timeout masquerading as "slow model".
        _step_clock = [time.monotonic()]
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
                # Cookie 文件在这个 Chromium 版本里已经从 Default/Cookies 挪到了
                # Default/Network/Cookies（本机实测），两个路径都要认 ——
                # 认不出来就会把 `warmed` 判成 False，于是每条用例都往一个**已经有
                # 活会话**的 shard 里重新注入旧 bundle，等于用过期 token 覆盖新 token。
                warmed = any(
                    os.path.exists(os.path.join(lease.path, *parts))
                    for parts in (
                        ("Default", "Cookies"),
                        ("Default", "Network", "Cookies"),
                    )
                )

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
            #
            # 用哪个浏览器在这里一次性定好，而不是每步再算：探测只是文件存在性检查，
            # 很便宜，但每个用例重算一次会让日志里出现"同一用例前后选了不同浏览器"
            # 这种读起来像 bug 的行。整轮 run 用同一个值。
            _sys_browser = browser_binary.resolve_browser_executable(
                s.browser_executable, s.browser_candidates
            )

            def make_browser():
                extra: dict = {}
                if _record_video:
                    extra["record_video_dir"] = video_dir
                    # 录制规格**独立于视口**（video_width/height 默认 1280×800@10fps）。
                    # 这里原本注释写"默认跟着视口走、视口就是屏幕分辨率 2880x1800"——
                    # 两处都不准：默认给了固定值就永远不走"跟着视口"分支，
                    # 而视口已改为与用户手动测试对齐（1434×825，见 _viewport_kwargs）。
                    # 为什么录像要小一号：1440p@30fps 的逐帧编码是**每个用例固定付出**的
                    # CPU 成本，而回放只需看清步骤。1280×800 与 1434×825 宽高比接近，
                    # 不会拉伸变形。
                    if s.video_width and s.video_height:
                        extra["record_video_size"] = {
                            "width": s.video_width,
                            "height": s.video_height,
                        }
                    if s.video_framerate:
                        extra["record_video_framerate"] = s.video_framerate
                if s.case_record_trace:
                    extra["traces_dir"] = trace_dir
                return Browser(
                    headless=True,
                    # 跨用例复用：设了 keep_alive 之后 stop() 只发事件、不真杀进程，
                    # 浏览器留给下一个用例接着用（见 _BROWSER_POOL 的说明）。
                    # 不支持复用时不设它，保持"用完即关"的老行为。
                    **({"keep_alive": True} if s.browser_reuse and lease is not None else {}),
                    # never fetch the default uBlock/cookie extensions — that download is a
                    # blocking call with no internet in the container (it froze the API).
                    enable_default_extensions=False,
                    highlight_elements=s.browser_highlight_elements,
                    # 不让 Chromium 继承宿主机的 HTTP(S)_PROXY。
                    #
                    # 宿主机由外部工具注入透明代理（本机实测 HTTP_PROXY=http://127.0.0.1:63679，
                    # 端口每次都不同），Chromium 会读这两个环境变量，于是**连内网
                    # 192.0.2.10 的请求也被送去代理**。那个代理会失效，一旦失效浏览器
                    # 就全线瘫痪——日志里出现过：
                    #   RuntimeError: Navigation failed: net::ERR_PROXY_CONNECTION_FAILED
                    #   Failed to search duckduckgo: Navigation failed: net::ERR_PROXY_CONNECTION_FAILED
                    # 现象是页面永远停在「正在加载中请稍后......」（0 链接 / 0 可交互元素），
                    # 用例全判失败。被测系统在内网，本就该直连；第三方 beacon 另有 CDP 拦截。
                    # 注意这只影响浏览器进程，Python 侧调 LLM 网关走的是 app/llm.py 自己的
                    # GATEWAY_IGNORE_PROXY 开关，两者互不干扰。
                    args=["--no-proxy-server"],
                    # ★ 用系统浏览器（Edge/Chrome）而不是 browser-use 自带的 Chromium。
                    # 探测逻辑在 app/browser_binary.py（纯函数、可单测），这里只传结果。
                    # None = 没找到系统浏览器，browser-use 用自带 Chromium —— 那也是
                    # 完全能跑的配置，所以这不是错误路径，不需要兜底告警。
                    **({"executable_path": _sys_browser} if _sys_browser else {}),
                    # 视口与用户手动测试环境一致（原因见 _viewport_kwargs）
                    **_viewport_kwargs(),
                    # 拟人 + 安全的批处理节奏。
                    #
                    # MAX_ACTIONS_PER_STEP=2 允许"同一区域的连续操作"，但**点击永远独占一步**
                    # （由 _install_safe_batching 硬保证，见文件上方说明）。
                    #
                    # `wait_between_actions` 只在**同一批次**的第 2 个动作之前生效。
                    # 之前把它设 0.0 是因为配置是"一步一个动作"、它根本不会触发；
                    # 现在批处理回来了，给它 0.3s —— 两个连续输入之间留出页面反应时间，
                    # 真人填完一格再填下一格也是这个节奏。
                    wait_between_actions=0.3,
                    # 这两项是"等页面稳定"的硬性等待，每步/每次导航都要付，用户看不见，
                    # 所以压到很低（原值 0.25 / 0.5）。
                    minimum_wait_page_load_time=0.1,
                    wait_for_network_idle_page_load_time=0.1,
                    # Persistent, project-isolated user-data-dir → cookies/localStorage/
                    # sessionStorage/IndexedDB + HTTP cache survive between cases and runs.
                    **({"user_data_dir": lease.path} if lease is not None else {}),
                    **extra,
                )

            # 复用池：有持久化 profile 且开了开关才复用（key 就是 profile 目录）。
            _pool_key = lease.path if (lease is not None and s.browser_reuse) else None
            # 先给默认值：_acquire_browser 抛异常时下面的 finally 路径也要能安全引用它。
            reused = False

            try:
                browser, reused = await _acquire_browser(
                    _pool_key, make_browser, reuse=bool(_pool_key)
                )
                if reused:
                    log.info("executor: 复用已有浏览器（%s），省掉一次冷启动", _pool_key)
            except Exception as exc:
                # The classic cause is another Chromium still holding the profile — which
                # the lease check should have caught, but a browser can also be spawned by
                # a session-capture path that does not take a lease. Clean up and try once
                # more rather than burning the case: this used to be an instant 100%
                # failure of the whole run with a message nobody could act on.
                if lease is None:
                    raise
                log.warning("executor: 浏览器启动失败（%s），清理占用后重试一次", exc)
                owners = await _wait_for_profile_free(lease.path, timeout_s=0.1)
                if owners:
                    await asyncio.to_thread(_kill_orphans, owners, lease.path)
                    await _wait_for_profile_free(lease.path, timeout_s=10.0)
                _clear_stale_singleton(lease.path)
                # 重试时先确保池里没有半死的实例，否则会拿到上一次那个坏掉的浏览器。
                if _pool_key:
                    _BROWSER_POOL.pop(_pool_key, None)
                try:
                    browser, reused = await _acquire_browser(
                        _pool_key, make_browser, reuse=bool(_pool_key)
                    )
                except Exception as exc2:
                    res.error = (
                        f"浏览器启动失败：{type(exc2).__name__}: {exc2}"[:400]
                        + " —— 通常是上一次运行异常结束后残留的 Chrome 仍占用浏览器配置目录。"
                        "已在启动时自动清理；若持续出现，请在项目设置里执行一次"
                        "『重置浏览器状态』，或确认没有手动打开的 Chrome 在用同一配置。"
                    )
                    raise
            await _block_third_party_beacons(browser)
            # 2026-10-04 多角色：把活着的浏览器交给 switch_account 工具，
            # 让它在运行中能换身份。放在这里（agent 启动前）而不是注册时，
            # 因为注册发生在 _build_tools，那时还没有 browser。
            _ACTIVE_BROWSER["browser"] = browser
            # The single biggest per-step speed lever (measured 20.7s → 0.8s of DOM
            # serialisation per step, see _SPRITE_PRUNE_JS). Installed before the agent
            # starts so even step 1 pays the low price.
            await _install_sprite_pruner(browser)
            # Seed the captured session bundle ONLY into a cold shard. A warm shard
            # already holds a live session and a newer app state; re-injecting the old
            # bundle would overwrite tokens the app itself has since rotated.
            if spec.login_state and not warmed:
                await _safe_async(lambda: _restore_session(browser, spec.login_state))
            # 2026-10-06 身份守卫：必须在 agent 看到页面**之前**跑。
            # 持久 profile 里留的是上一个登录者（现场实测是 admin），不核对的话
            # 用例会在错误身份下"通过"，而报告里完全看不出来。
            if _identity_guard_enabled():
                _t_guard = time.monotonic()
                _verdict = await _enforce_identity(
                    browser, spec.login_username, spec.start_url
                )
                _guard_ms = int((time.monotonic() - _t_guard) * 1000)
                if _verdict.startswith("purged"):
                    # 用 WARNING：默认日志级别是 WARNING，这条必须能被看见 ——
                    # 它意味着"之前那些用例跑的不是这个角色"，结论要重新看。
                    log.warning(
                        "executor: ★ 身份不匹配，已清除他人登录态（%s），本用例将以 "
                        "'%s' 重新登录（耗时 %sms）",
                        _verdict,
                        spec.login_username,
                        _guard_ms,
                    )
                else:
                    log.info("executor: 身份守卫 %s（%sms）", _verdict, _guard_ms)
            _stage["browser_up"] = time.monotonic()
            task = build_task(spec, s.report_language)

            async def _grab_shot(step_no: int, browser_state=None) -> str | None:
                """One step's screenshot — 优先复用 browser-use 自己截好的那一张。

                browser-use 的 ScreenshotWatchdog 每一步本来就会截一张，并通过
                `browser_state.screenshot` 交给我们（base64 PNG，实测第 1 步 87 KB）。
                此前我们又调 `browser.take_screenshot()` 自己再截一张：**同一时刻同一条
                CDP 通道上走了两次截图**，两边互相阻塞 —— 日志里反复出现
                `ScreenshotWatchdog.on_ScreenshotEvent timed out after 15.0s`。
                复用已有那张：每步依然有一张图（报告需要的东西一个不少），
                截图工作量减半，两边都不再饿死。

                browser_state 没带图时才回退到 CDP 截取（例如恢复会话/异常路径）。
                永不抛异常：时间线是诊断证据，不是控制流。
                """
                try:
                    data: bytes | None = None
                    source = "browser-use"
                    b64 = getattr(browser_state, "screenshot", None)
                    if isinstance(b64, str) and b64:
                        try:
                            data = base64.b64decode(b64)
                        except Exception:  # noqa: BLE001 — 坏图就当没有，走回退
                            data = None
                    if data is None:
                        png = await _safe_async(lambda: browser.take_screenshot())
                        if not png:
                            return None
                        data = png if isinstance(png, bytes) else base64.b64decode(png)
                        source = "cdp"
                    p = os.path.join(workdir, f"live-{step_no}.png")
                    with open(p, "wb") as f:  # noqa: ASYNC230 — tiny one-shot write
                        f.write(data)
                    log.debug("executor: 第 %s 步截图来源=%s", step_no, source)
                    return await _swallow(upload(p, f"{art}/live-{step_no}.png"))
                except Exception as exc:  # noqa: BLE001
                    log.debug("executor: 第 %s 步截图失败（不影响用例）：%s", step_no, exc)
                    return None

            async def _step_cb(browser_state, model_output, step_no):
                # This step's true cost = the gap since the previous callback (step 1's
                # value therefore also includes browser start + any pre-step setup, which
                # is exactly the fixed overhead worth seeing).
                _now = time.monotonic()
                step_elapsed = round(_now - _step_clock[0], 1)
                _step_clock[0] = _now
                # stream each step live: model thought + action + current screenshot.
                # browser-use keeps screenshots in-memory (not on disk), so grab one via
                # CDP here rather than relying on browser_state.screenshot / screenshot_paths.
                #
                # Every step is captured (live_shot_every=1 by default): a failure is
                # usually explained by one specific frame — the error toast, the disabled
                # field, the missing row — and sampling skips exactly that frame.
                #
                # Also re-arm the icon-sprite pruner: a navigation rebuilds the sprite and
                # tears down the CDP session that carried the pruner, so a one-shot install
                # silently expires. This is where it matters most — the next thing that
                # happens is the DOM serialisation this pruner exists to make cheap.
                await _ensure_sprite_pruner(browser)
                thought = ""
                actions: list[str] = []
                details: list[str] = []
                try:
                    thought = (
                        getattr(model_output, "thinking", None)
                        or getattr(model_output, "next_goal", None)
                        or ""
                    ).strip()
                    actions, details = _summarize_actions(model_output)
                except Exception as exc:  # noqa: BLE001
                    log.debug("executor: 第 %s 步解析模型输出失败：%s", step_no, exc)

                shot_url = None
                # 0 = 完全不要逐帧截图（速度优先时的开关）。此前写的是
                # `step_no % max(1, s.live_shot_every)`，那个 max(1,·) 会让 0 被当成 1，
                # 也就是"设成 0 反而每步都截"——把开关做成了反效果。
                if _shot_every > 0 and step_no % _shot_every == 0:
                    # Recorded BEFORE the append so an upload failure cannot abort the
                    # bookkeeping: the step must still appear in the timeline without a shot.
                    # 传入 browser_state：优先复用 browser-use 已截好的那张，避免同一步
                    # 在 CDP 通道上出现两次截图（见 _grab_shot 的注释）。
                    shot_url = await _grab_shot(step_no, browser_state)
                live_steps.append(
                    {
                        "i": step_no,
                        "action": ", ".join(actions) or "…",
                        "detail": ", ".join(details),
                        "thought": thought,
                        "result": "",
                        "error": "",
                        "screenshot": shot_url,
                        # 本步墙钟（秒）。见 _step_clock 注释：没有它就无法区分"4 步各
                        # 慢 45s"和"1 步卡死"，而这两者的修法完全相反。
                        "elapsed_s": step_elapsed,
                    }
                )
                if step_elapsed >= s.step_timeout_s:
                    log.warning(
                        "executor: 第 %s 步耗时 %.1fs，已达/超过单步上限 %ss —— "
                        "通常是页面状态采集卡住（DOM 过大或某请求一直等），而不是模型慢",
                        step_no, step_elapsed, s.step_timeout_s,
                    )
                try:
                    if on_step is not None:
                        await on_step(list(live_steps))
                    if should_abort is not None and await should_abort():
                        res.error = "cancelled"
                        _safe(lambda: agent.stop())
                except Exception as exc:  # noqa: BLE001
                    log.debug("executor: 第 %s 步回调上报失败：%s", step_no, exc)

            # 防"死磕"的硬兜底。
            #
            # 实测：本项目最贵的两个用例（44 步、55 步）各自只是**一个交互被重试了十几次**
            # ——判决理由里写着"下一步按钮点不动"、"取消和下一步区域合并"、"使用范围选不中"，
            # 而那些重试**一次都没成功**。它们烧掉的是"早点报失败"本来只要花零头的预算。
            #
            # prompt 里已经加了 STOP THRASHING 规则，但模型不总会听；这里用 browser-use
            # 自己的 register_should_stop_callback 做代码层保证：连续 N 步发出**完全相同**
            # 的动作（且错误信息也一样）就主动停止，让用例以"已观察到的问题"作为结论结束。
            # 阈值取 4 而不是 2：正常流程里偶尔也会连着两步做同类动作（如逐个点选），
            # 但真到"同样的动作 + 同样的错误"连续四次，那就不是进展了。
            _THRASH_LIMIT = 4

            async def _should_stop() -> bool:
                try:
                    thrashing, act = _looks_like_thrashing(live_steps, _THRASH_LIMIT)
                    if thrashing:
                        log.warning(
                            "executor: 连续 %d 步重复同一动作（%s）且反馈相同 —— "
                            "判定为卡死重试，主动停止，把已观察到的现象作为结论",
                            _THRASH_LIMIT, act[:60],
                        )
                    return thrashing
                except Exception:  # noqa: BLE001 — 停止判定绝不能反过来搞死用例
                    return False

            agent = Agent(
                task=task,
                llm=await browser_use_llm(),
                browser=browser,
                register_new_step_callback=_step_cb,
                register_should_stop_callback=_should_stop,
                # 顺序有讲究：_ADAPT_RULE 紧跟在 _EFFICIENCY_RULE / _ANTI_WASTE_RULE 之后，
                # 因为它是对那两条"省步数"规则的例外说明。反过来放会让人（和模型）
                # 读到"先看省步数、再看可以逛菜单"，理解成后者覆盖前者。
                extend_system_message=(
                    f"{_SCOPE_RULE}\n\n{_TOOLKIT_RULE}\n\n{_POPUP_RULE}\n\n"
                    f"{_ANTI_WASTE_RULE}\n\n{_EFFICIENCY_RULE}\n\n{_ADAPT_RULE}\n\n"
                    f"{_SELF_CHECK_RULE}{_memory_note}"
                ),
                use_vision=False,  # 提速：不每步发整屏截图，改用无障碍树/DOM 文本
                # Speed: batch independent actions (e.g. several form fills) into one
                # model round-trip instead of one round-trip per action. This is the
                # single biggest lever on step count, and step count is the cost.
                max_actions_per_step=s.max_actions_per_step,
                # Speed: bound a single slow model call so it can't consume the whole
                # case budget while the run waits on it.
                step_timeout=s.step_timeout_s,
                # browser-use 自带的 judge 会在"完成"时多调一次 LLM（附最多 10 张截图），
                # 结论却不覆盖 agent 自述，而我们有 app/judge.py —— 纯重复，默认关。
                use_judge=s.agent_use_judge,
                # 单次 LLM 调用的墙钟上限；browser-use 默认 None（不限制）。
                llm_timeout=s.llm_timeout_s,
                # 输出侧 token（质量换速度，默认保守，见 config.py 注释）。
                use_thinking=s.agent_use_thinking,
                flash_mode=s.agent_flash_mode,
                # None 时不下传，保持 browser-use 自己的默认（不限制历史）。
                **(
                    {"max_history_items": s.agent_max_history_items}
                    if s.agent_max_history_items
                    else {}
                ),
                tools=_build_tools(s, spec),
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
            finally:
                # 异常路径也要记：卡死/超时的用例恰恰是最需要知道"卡在哪一段"的。
                _stage["agent"] = time.monotonic()
        except Exception as exc:  # browser/agent setup failure
            res.error = res.error or f"{type(exc).__name__}: {exc}"[:500]
        finally:
            # Do NOT tear the browser down here any more. The final screenshot below has to
            # be taken from a LIVE browser, and this `finally` runs BEFORE it — which meant
            # every timeout/error path lost its final frame, i.e. exactly the cases whose
            # end state matters most. The screenshot is now captured here instead, and the
            # single shutdown point moved after it (see below).
            #
            # Only the lease is released here: it is process-local bookkeeping and holding
            # it longer would block other cases from using the slot.
            if lease is not None:
                await lease.__aexit__(None, None, None)
                lease = None

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
            # Merge the browser's own result/error text into the live timeline BY STEP
            # NUMBER, never by list position.
            #
            # This used to be `hist[i]` where `i` was the enumerate index of live_steps —
            # which shifted every result by one step, because the two lists are not
            # 0-aligned: the step callback is handed a 1-based `step_no`, so live_steps
            # holds steps 1..N, while _build_diagnostics numbers its own entries `i + 1`
            # over a 0-based `history.history`. The visible damage in a real run
            # (run_result #142): step 21 read `action=find_elements` but
            # `result=Clicked button "保存规则"` — the previous step's text. The failure
            # narrative is fed exactly this evidence, so it was being told what happened
            # one step early, which is why a narrative could name a plausible but wrong
            # UI action.
            by_step = {d.get("i"): d for d in hist if isinstance(d, dict)}
            for ls in live_steps:
                src = by_step.get(ls.get("i"))
                if src:
                    ls["result"] = src.get("result", "")
                    ls["error"] = src.get("error", "")
                    # 实时那一步拿不到模型输出外的参数细节，最终诊断里有更完整的版本。
                    if not ls.get("detail"):
                        ls["detail"] = src.get("detail", "")
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

        # The judge's only look at the page's end state, and the last frame of the replay.
        # Grabbed from the still-live browser before the single shutdown below.
        final_path = _newest(os.path.join(workdir, "live-*.png"))
        final_url: str | None = None
        if browser is not None:
            try:
                png = await _safe_async(lambda: browser.take_screenshot())
                if png:
                    p = os.path.join(workdir, "final.png")
                    data = png if isinstance(png, bytes) else base64.b64decode(png)
                    with open(p, "wb") as f:
                        f.write(data)
                    final_path = p
                    final_url = await _swallow(upload(p, f"{art}/final.png"))
            except Exception as exc:  # noqa: BLE001
                log.debug("executor: 最终截图失败：%s", exc)

        # Backfill: any step whose screenshot did not make it (CDP hiccup, upload failed)
        # gets the final frame attached, so the report never shows a bare step for a case
        # that ran. The frame is mislabelled as that step's if the run ended much later —
        # acceptable, because the alternative is an empty slot with no picture at all.
        if final_url and live_steps:
            for ls in live_steps:
                if not ls.get("screenshot"):
                    ls.setdefault("screenshot", final_url)
            if res.diagnostics:
                for d in res.diagnostics:
                    if isinstance(d, dict) and not d.get("screenshot"):
                        d["screenshot"] = final_url
        # Screenshot done, so the browser can go — UNLESS it lives in the reuse pool.
        #
        # 判断依据必须是「**有没有进池**」（_pool_key），不是「这一轮有没有复用」（reused）。
        # 这里踩过一次：写成 `not reused` 时，用例 A 新建浏览器（reused=False）→ 正常入池，
        # 但紧接着就被这段关掉了；用例 B 从池里拿到的是**已死对象**，
        # 重置报 "CDP client not initialized - browser may not be connected yet"，
        # 于是丢弃重开 —— 池里永远是死对象，复用一次都没成功（实测 139 次重置失败）。
        #
        # 另外 keep_alive 拦不住这里：_shutdown_browser 里的 CDP Browser.close 是
        # 绕过 keep_alive 直接关 Chromium 的。
        # 非复用模式（没有 pool key）保持原样：优雅关闭，让 persistent profile 落盘。
        if browser is not None and not _closed[0] and not _pool_key:
            await _shutdown_browser(browser)
            _closed[0] = True
        # 多角色：浏览器生命周期结束，别让 _ACTIVE_BROWSER 攥着一个已关的对象。
        # 复用模式（_pool_key）下浏览器留给下个用例，所以不清——它还活着，
        # 而下一个用例启动时会重新赋值。
        if _closed[0]:
            _ACTIVE_BROWSER.pop("browser", None)
        _stage["close"] = time.monotonic()

        # collect + upload artifacts (best-effort; missing artifacts don't fail the case).
        # AFTER the shutdown: the video file is only finalized when the browser closes, and
        # it is finalized ASYNCHRONOUSLY — so wait for its size to settle rather than
        # reading the directory once and racing the encoder.
        # 没开录像就直接跳过等待 —— 否则 _wait_for_video 会为空目录白等 empty_timeout_s（4 秒/用例）。
        video = await _wait_for_video(video_dir) if _record_video else None
        _stage["video"] = time.monotonic()
        history_file = os.path.join(workdir, "history.json")
        if video:
            res.video_url = await _swallow(upload(video, f"{art}/{os.path.basename(video)}"))
        elif s.case_record_video:
            log.warning("executor: 本用例没有录到视频（video_dir=%s）", video_dir)
        if os.path.exists(history_file):
            res.trace_url = await _swallow(upload(history_file, f"{art}/history.json"))
        final_shot = _read_b64(final_path)

    res.latency_ms = int((time.monotonic() - t0) * 1000)

    # The browser's own read-backs ("Clicked div role=option …", "Typed …", errors) — used
    # by both the judge and the failure narrative, so build it once.
    evidence = [str(d.get("result") or d.get("error") or "") for d in (res.diagnostics or [])]

    # What the narrative writer gets as "what the tester did".
    #
    # `res.steps` alone is nearly useless for that job: it is `history.action_names()`,
    # i.e. bare framework verbs — ['navigate', 'click', 'wait', 'input', 'evaluate'] — with
    # no menu names, no field labels, nothing to build 【操作步骤】 from. The business
    # language the narrative needs ("进入资源审批配置", "点击新建规则") is in the timeline's
    # result text, which the browser writes as `Clicked button "保存规则"`. Pair each step's
    # action with that read-back and the model has real material AND a step number it can
    # cite, which is what keeps 【操作步骤】 from being invented.
    #
    # Falls back to plain res.steps when there is no timeline (e.g. a set-up failure), so
    # the narrative still gets whatever exists instead of nothing.
    narrative_actions: list[str] = []
    for d in res.diagnostics or []:
        if not isinstance(d, dict):
            continue
        act = str(d.get("action") or "").strip()
        res_txt = str(d.get("result") or d.get("error") or "").strip()
        thought = str(d.get("thought") or "").strip()
        bits = [b for b in (f"【{act}】" if act else "", res_txt, f"(意图: {thought})" if thought else "") if b]
        if bits:
            narrative_actions.append(f"第{d.get('i')}步: " + " ".join(bits))
    if not narrative_actions:
        narrative_actions = [str(a) for a in (res.steps or []) if a]

    if res.error:
        res.status = "error"
    else:
        # ★ 判定闸门先行：拦下"物理上不可能通过"的情况，再交给 LLM 判定器。
        #
        # 现场（run 10 / result 187，2026-10-06）：agent 明确说"无法完成核对，
        # 页面只有『正在加载中请稍后』、0 个可交互元素"，判定器却判passed，
        # 理由是"截图显示已到达资源审批配置列表"—— 截图里根本没有列表。
        # 根因是 judge.py 对模型输出的 status 没有任何校验，
        # 而提示词里"截图是最强证据"权重过高，能被一句话说服。
        #
        # 闸门在 judge() 之前执行，命中就不调 LLM：既省一次调用，
        # 也避免那个已经被证明会编造证据的判定器再开口。
        # 设计取舍与180 条真实数据回归结果见 app/judge_gate.py 顶部注释。
        _gate = check_gates(
            res.final_answer or "", res.steps, evidence
        )
        if _gate.blocked:
            res.status = "failed"
            res.judge_reason = _gate.reason
            # agent 自己都说没做到 → 属于"没做到位"，重试有意义。
            #根因归 environment/evidence_insufficient 取决于 agent 怎么描述，
            # 交给 LLM 判定器去分类没有意义（它已经被证明会编），这里按
            # 闸门类型直接给：页面没起来 = environment。
            res.evidence_gap = True
            res.root_cause = (
                "environment" if _gate.gate == "page_not_ready" else "agent_incomplete"
            )
            res.verdict_evidence = []
            log.warning(
                "executor: ★ 判定闸门拦下假通过（gate=%s）—— %s",
                _gate.gate,
                spec.name or f"case{spec.case_id}",
            )
        else:
            verdict = await judge(
                spec.expected,
                res.final_answer or "",
                res.steps,
                task=spec.prompt,
                evidence=evidence,
                screenshot_b64=final_shot,
            )
            res.status = verdict.status
            res.judge_reason = verdict.reason
            res.evidence_gap = verdict.evidence_gap
            res.root_cause = verdict.root_cause
            res.verdict_evidence = list(verdict.evidence)

    # Bug description for anything that did not pass. "error" counts: an errored case is
    # still a case the tester has to look at, and a narrative is what they read first.
    # Skipped for passed cases — there is no defect to describe.
    if res.status in ("failed", "error"):
        try:
            nar = await describe_failure(
                case_name=spec.name or f"case{spec.case_id}",
                task=spec.prompt,
                expected=spec.expected,
                final_answer=res.final_answer or "",
                actions=narrative_actions,
                evidence=evidence,
                judge_reason=res.judge_reason or res.error or "",
                screenshot_b64=final_shot,
            )
            if not nar.is_empty():
                res.failure_narrative = nar.as_dict()
        except Exception as exc:  # noqa: BLE001 — a narrative must never cost us the result
            log.warning("executor: 失败用例描述生成异常（不影响结果）：%s", exc)

    # 每用例一行耗时分解，用 WARNING 输出（默认日志级别下 INFO 不可见）。
    #
    # 为什么必须这样输出：对 13 个已完成用例做回归得到「固定开销 ≈171s/用例、
    # 每步 ≈16.6s」。浏览器用例只能串行跑，固定开销无法靠并发摊薄 —— 它才是
    # 378 个用例跑几十小时的主因，而此前报告里只有一个 latency 总数，看不出构成。
    # 分段含义：
    #   启动浏览器 = t0 → browser_up（含 profile 租约、浏览器拉起、beacon 拦截、精灵裁剪、会话注入）
    #   执行用例   = browser_up → agent（agent.run 的墙钟，含全部步）
    #   收尾       = agent → close（最终截图 + 优雅关浏览器以落盘 profile）
    #   等视频落盘 = close → video（Playwright 异步 finalize 录像）
    #   判定/描述  = 之后到函数返回（judge 每个用例一次；失败用例再加一次描述）
    try:
        total = time.monotonic() - t0
        seg = {
            "启动浏览器": _stage.get("browser_up", t0) - t0,
            "执行用例": _stage.get("agent", _stage.get("browser_up", t0))
            - _stage.get("browser_up", t0),
            "收尾": _stage.get("close", _stage.get("agent", t0)) - _stage.get("agent", t0),
            "等视频": _stage.get("video", _stage.get("close", t0)) - _stage.get("close", t0),
            "判定描述": total - (_stage.get("video", t0) - t0),
        }
        parts = " | ".join(f"{k} {v:.1f}s" for k, v in seg.items())
        fixed = seg["启动浏览器"] + seg["收尾"] + seg["等视频"]
        log.warning(
            "executor: case-timing[%s] 总 %.1fs | %s | 步数 %d | 固定开销(启动+收尾+等视频) %.1fs",
            (spec.name or f"case{spec.case_id}")[:24], total, parts,
            len(res.diagnostics or []), fixed,
        )
    except Exception as exc:  # noqa: BLE001 — 埋点绝不能影响用例结果
        log.debug("executor: 耗时分解输出失败：%s", exc)
    return res


def _capture_profile_dir(project_id: int | None) -> str:
    """隔离的 user-data-dir，专供会话捕获用。

    2026-10-04 修：原来 capture_session 建 Browser 时**完全没传 user_data_dir**，
    于是走 browser-use 的默认 profile 目录 —— 那是**所有项目共用**的一份。
    后果：给 B 项目捕获登录态时，浏览器里还留着 A 项目的 cookies，
    捕获出来的 bundle 可能带着 A 项目的会话，B 项目的用例就这么"用上了别人家的账号
    和缓存"。这正是用户报的现象。

    为什么用独立子目录而不是复用 `slot0`：捕获可能和某个正在跑的用例并发，
    共用同一个 Chromium profile 会撞"profile 被占用"，两边都起不来。
    单独一个 `_capture` 目录互不干扰；捕获完就关浏览器，不会长期占盘。

    project_id 为 None 时退化成 `_capture` 同级目录（不按项目分），
    这只会发生在极老的调用路径上 —— 但即使退化了也比"所有项目共用默认 profile"安全，
    因为默认 profile 里会混入所有项目的真实登录态。
    """
    root = _profile_root()
    if project_id:
        return os.path.join(root, f"project_{project_id}", "_capture")
    return os.path.join(root, "_capture_orphan")


class _StageTimer:
    """分段计时器 —— 只为把登录态捕获的耗时拆开，不参与任何控制逻辑。

    为什么需要它（实测run 10，2026-10-06）：
      UI 显示单条用例 2m29s，但 `case-timing` 日志只有 31.7s。
      差的 145s 花在**用例开始之前**的 `capture_session`（凭据会话过期 → 重登），
      而那一段没有任何日志，看起来像"系统在浪费时间"。

      没有分解就不知道该优化哪里。现在每段都会落一条日志：
      「capture-timing[user] 总 145.2s | 启浏览器 4.1s | 预导航 12.3s |
        裁剪器 0.8s | 身份守卫 3.4s | agent登录 118.2s | 导出 6.4s」

    用法：`with _stage_timer("capture", username) as t: ...` 然后 `t.mark("阶段名")`。
    刻意不做成 contextmanager 链式调用 —— 阶段名要写在业务代码旁边，
    自动推导反而看不出这一段到底做了什么。
    """

    __slots__ = ("_label", "_t0", "_last", "_marks", "_t_start")

    def __init__(self, label: str):
        self._label = label
        self._marks: list[tuple[str, float]] = []
        self._t0: float | None = None
        self._t_start: float | None = None
        self._last: float | None = None

    def start(self) -> "_StageTimer":
        """开始计时。返回 self 以便链式：`t = _StageTimer(x).start()`。"""
        self._t0 = time.monotonic()
        self._t_start = self._t0
        self._last = self._t0
        return self

    def mark(self, stage: str) -> None:
        """记一个阶段耗时（距上一个 mark 的增量）。"""
        if self._last is None:
            return
        now = time.monotonic()
        self._marks.append((stage, now - self._last))
        self._last = now

    def finish(self) -> None:
        """输出分段日志。必须显式调用（不靠 __exit__，理由见调用处注释）。"""
        if self._t0 is None:
            return
        total = time.monotonic() - self._t0
        parts = " | ".join(f"{n} {d:.1f}s" for n, d in self._marks)
        log.warning(
            "executor: capture-timing[%s] 总 %.1fs | %s",
            self._label,
            total,
            parts or "（无阶段记录）",
        )
        # 慢捕获要留痕：会话 TTL 有限，捕获 100s+ 意味着缓存期里一直在付这笔钱。
        if total >= _CAPTURE_SLOW_WARN_S:
            _ttl = get_settings().session_ttl_min
            log.warning(
                "executor: ★ 捕获登录态耗时 %.1fs（阈值 %.0fs）。会话 TTL %s 分钟，"
                "即缓存期有约 %.0f%% 的时间都在付这笔开销 —— "
                "批量跑用例时它是独立于用例本身的大头。",
                total,
                _CAPTURE_SLOW_WARN_S,
                _ttl,
                100.0 * total / (_ttl * 60.0),
            )


# 超过这个秒数就算"慢捕获"，值得单独提醒。
# 依据：实测 145s；缓存 TTL 30min，即缓存 20% 的时间都在付这笔钱。
_CAPTURE_SLOW_WARN_S = 60.0


def _viewport_kwargs() -> dict:
    """把配置里的视口宽高转成 Browser(**kwargs) 形状。

    与用户手动测试环境对齐的原因与取舍写在 config.py 的 browser_viewport_width 上，
    这里只做转换和校验，不含任何"提速"逻辑。

    两个都填 0 → 返回 {}，交回 browser-use 自己决定（headless 下等于屏幕物理分辨率）。
    只填一个 → 抛错，因为视口必须成对：半边视口会让页面比例失真，
    排查时看起来像"页面渲染坏了"，比直接回落到默认值更浪费时间。
    """
    s = get_settings()
    if s.browser_viewport_width and s.browser_viewport_height:
        return {
            "viewport": {
                "width": s.browser_viewport_width,
                "height": s.browser_viewport_height,
            }
        }
    if bool(s.browser_viewport_width) != bool(s.browser_viewport_height):
        raise ValueError(
            f"BROWSER_VIEWPORT_WIDTH/HEIGHT 必须成对设置，当前 "
            f"{s.browser_viewport_width}x{s.browser_viewport_height}。"
        )
    return {}


async def capture_session(
    base_url: str,
    username: str,
    password: str,
    project_id: int | None = None,
) -> str:
    """Log into base_url with a test account server-side and return the captured
    storage_state as a JSON string. The agent reads simple captchas. Raises on failure.

    project_id 决定 user-data-dir 落在哪个项目目录下（见 _capture_profile_dir）。
    调用方一定要传，否则捕获出来的会话可能混入别的项目的 cookies。
    """
    from browser_use import Agent, Browser

    from app.llm import browser_use_llm

    s = get_settings()
    profile = _capture_profile_dir(project_id)
    os.makedirs(profile, exist_ok=True)
    log.info(
        "executor: 捕获登录态（项目 %s）使用隔离 profile: %s",
        project_id if project_id else "未指定",
        profile,
    )
    # 分段计时：这一段每 30 分钟就要重跑一次，实测能占到整轮耗时的大头，
    # 之前却完全没有日志（详见 _StageTimer 的docstring）。
    #
    # ★ 刻意**不用** `with`、也不拆函数：那需要把整个函数体再缩进一层，
    # 而这个函数里有嵌套的 async def（_capture_step_cb）和多段 try/except +
    # 多个 return，大范围重排极易出错并留下不可达代码（已经踩过一次）。
    # 局部计时器 + 显式 mark 是改动面最小的做法。
    _t = _StageTimer(username).start()
    # keep_alive so the CDP session survives after agent.run() — otherwise
    # export_storage_state() fails with "Root CDP client not initialized".
    browser = Browser(
        headless=True,
        keep_alive=True,
        enable_default_extensions=False,
        # 视口必须与 execute_case 的 make_browser 完全一致。
        # 这条路径单独 new 了一个 Browser（不走 make_browser），如果只在一处接线，
        # 会出现"捕获登录态时看到的是 2880 宽的页面布局，真正执行时是 1434 宽"——
        # 捕获时能点到的元素，执行时未必在视口里，且这种不一致只在特定页面偶发，
        # 排查成本极高。抽成函数是为了让两处共用同一个真相来源。
        **_viewport_kwargs(),
        # 项目隔离的 profile：不同项目之间绝不共用 cookies/缓存
        user_data_dir=profile,
        # 同 make_browser：别让 Chromium 继承宿主机的透明代理，
        # 否则内网地址会被送去代理并全线失败（详见 make_browser 里的长注释）。
        args=["--no-proxy-server"],
    )
    await browser.start()
    _t.mark("启浏览器")
    # 2026-10-05 补装图标精灵裁剪。
    # 为什么这里必须单独装：这条路径自己 new 了一个 Agent（不走 execute_case），
    # 而裁剪器原先只装在 execute_case 里。漏装的后果实测很明确 ——
    # 登录页内联了 32,995 个 <path>，未裁剪时 DOM 有 33,915 个节点，
    # 采集一次 20-27s，于是 agent 拿到的是"页面还没有可交互元素"，
    # 开始 wait 3s → 再 wait → 干等循环（实测 element index 一路到 67346）。
    # 装上之后同一页面只剩 335 个节点，交互元素与 prompt 完全不变。
    #
    # 为什么先自己导航一次：browser-use 0.13 的 Browser 是**懒加载**的 ——
    # 不发生导航就一个页面 target 都没有，`_install_sprite_pruner` 等满 12s
    # 也会以"找不到页面目标"告警收场（实测过）。所以这里主动导航一次把 target
    # 拉起来，顺带让登录页在**裁剪器已注册**的前提下才开始解析。
    # agent 紧接着本来也要去同一个地址，多这一次导航不影响总时长。
    #
    # ★ 2026-10-06 补start()：现场日志「拿不到 CDP 会话，图标精灵裁剪未安装
    # —— Root CDP client not initialized」查到的就是这里。
    # `navigate_to()` 在浏览器未启动时**不会抛异常**：它 dispatch 一个事件，
    # 而 `event_result(raise_if_any=True, raise_if_none=False)` 里
    # `raise_if_none=False` 会把"事件根本没跑起来"静默当成成功。
    # 于是后面的 `_install_sprite_pruner` 撞上 `get_or_create_cdp_session()`
    # 里的 `assert self._cdp_client_root is not None`（session.py:1496）必炸，
    # 裁剪器整轮装不上 —— 而登录页内联 3.3 万个 <path>，未裁剪时每步 DOM 采集
    # 要20-27s（实测元素索引一路涨到 67346，agent 只能干等）。
    # 这一条是**必须先 start** 才成立：没有 CDP 根客户端，注入脚本无处可挂。
    # （start() 已挪到 Browser(...) 之后紧跟执行，见上方 _t.mark("启浏览器")。）
    try:
        await browser.navigate_to(base_url)
    except Exception as exc:  # noqa: BLE001 — 导航失败不该挡住后面的登录尝试
        _warn_prune_once(
            "prenav", "executor: 预导航 %s 失败（裁剪器可能装不上）：%s", base_url, exc
        )
    _t.mark("预导航")
    await _install_sprite_pruner(browser)
    _t.mark("装裁剪器")
    # 2026-10-06 身份守卫（捕获路径同样要守，而且这里是**最要命**的一处）。
    #
    # 捕获用的 profile 是长期复用的（profiles/project_N/_capture），里面也会残留
    # 上一次登录者的会话。后果比用例侧的污染更严重：agent 进来一看"已经登录了"，
    # 直接判定完成，于是**把别人的会话当成目标账号的会话存进凭据库** ——
    # 从那以后，这个项目所有用例恢复的都是那个别人的身份。
    # 现场就是这么中招的：项目 2 的凭据里躺着 admin 的 ACCESS_TOKEN。
    # 所以捕获前必须先把不相干的身份清掉，确保"这次捕获到的就是 username 本人"。
    if _identity_guard_enabled():
        verdict = await _enforce_identity(browser, username, base_url)
        if verdict.startswith("purged"):
            log.warning(
                "executor: ★ 捕获登录态前清掉了他人会话（%s），本次将为 '%s' 重新登录",
                verdict,
                username,
            )
        else:
            log.info("executor: 捕获登录态前身份守卫 %s", verdict)
    _t.mark("身份守卫")
    try:
        task = (
            f"Go to {base_url}. Log in with username '{username}' and password '{password}'. "
            "If a simple math or text captcha is shown, read it from the page and enter the answer. "
            "If a first-login password change is forced, set a new valid password and continue. "
            "Finish only once you are on a logged-in page (no longer on the login screen)."
        )
        # 每步补装裁剪器：上面的预装是在浏览器刚建、还没有页面 target 时做的，
        # 那次往往装不上（没有 target 可挂）。真正要命的是第 1 步 ——
        # 它正好赶在登录页解析完 3.3 万节点之后，所以从第 2 步起补装。
        #
        # 必须是 `async def`，不能图省事写 lambda：browser-use 用
        # `inspect.iscoroutinefunction` 决定是 await 还是直接调用，
        # lambda 返回的是协程对象但函数本身不是协程函数 → 走同步分支 →
        # 协程永不执行，还附赠一条 "coroutine was never awaited" 警告。
        # 参数按位置传三个，所以形参必须能收下。
        async def _capture_step_cb(_browser_state, _model_output, _step_no) -> None:
            await _ensure_sprite_pruner(browser)

        agent = Agent(
            task=task,
            llm=await browser_use_llm(),
            browser=browser,
            use_vision=False,
            register_new_step_callback=_capture_step_cb,
        )
        # 登录捕获用自己的小预算，**不要**复用用例的 80 步 / 600 秒：
        # 凭据失效时 agent 会开始猜账号密码，把整个预算烧光才失败，
        # 现场看起来就是"卡在登录"（实测空动作事件累计 526 次）。
        # 详见 config.py 里 login_capture_* 的说明。
        await asyncio.wait_for(
            agent.run(max_steps=s.login_capture_max_steps),
            timeout=s.login_capture_timeout_s,
        )
        _t.mark("agent登录")
        # P2.5: never store a garbage bundle — if login didn't actually complete (agent still
        # on a login page), fail loudly so the caller falls back to prompt-login instead of
        # caching a tokenless session that would make every future case fail.
        if _last_url_is_login(agent.history):
            # 2026-10-05：报错里带上步数与排查方向。
            # 原来只说"登录没完成"，而现场最常见的原因是**凭据本身失效**：
            # 服务端返回「登录失败，账号密码不正确」，但 agent 会继续猜账号密码，
            # 表面现象是"卡在登录"，很容易被误判成输入机制/前端组件的问题
            # （实测甚至被模型自己解释成"shadow passthrough 设不进值"，而真实原因
            #  只是密码不对）。把步数和这句提示写进报错，下次一眼可辨。
            steps_used = len(getattr(agent.history, "history", []) or [])
            raise RuntimeError(
                "login did not complete — still on a login page after capture "
                f"(用掉 {steps_used} 步 / 上限 {s.login_capture_max_steps})。"
                "排查顺序：① 到被测系统确认该账号密码是否仍然有效、是否被锁定 —— "
                "若服务端返回「账号密码不正确」，就是凭据问题，与输入机制无关；"
                "② 确认租户名正确（登录页有「请输入租户名称」一栏）；"
                "③ 再怀疑页面结构变化。"
            )
        # 2026-10-06 落库前再验一次身份。
        # 「不在登录页了」不等于「登录的是本人」—— 只要 profile 里残留着别人的会话，
        # agent 就会把"已经登录"当成任务完成，于是我们存下的是别人的 token。
        # 项目 2 的凭据里就这么混进了 admin 的 ACCESS_TOKEN，之后 179 条用例全部
        # 顶着 admin 的身份跑。这里拦一道：抓到的人不是目标账号就报错，宁可不存。
        if _identity_guard_enabled():
            who = await _read_identity(browser)
            if who and who != username:
                raise RuntimeError(
                    f"捕获到的登录态属于「{who}」，与目标账号「{username}」不一致 —— "
                    "已拒绝入库（存进去会让该项目后续所有用例跑在别人的身份下）。"
                    "常见原因：捕获用的 profile 里残留着上一次的会话，"
                    "agent 一进来就看到已登录状态，直接判定完成。"
                )
        # Full bundle: cookies + per-origin localStorage AND sessionStorage (CDP format).
        # Restored later via CDP (_restore_session), so sessionStorage-based auth
        # survives — unlike the Playwright storage_state format, which drops it.
        state = await browser._cdp_get_storage_state()  # noqa: SLF001 — private API, pinned to browser-use 0.13.x
        _t.mark("导出登录态")
        return json.dumps(state, ensure_ascii=False)
    finally:
        # finish 放在 finally：捕获失败（超时 / 凭据失效 / 被身份守卫拒绝）时
        # 恰恰最需要知道"时间花在哪一段"，成功路径反而次要。
        _t.finish()
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
