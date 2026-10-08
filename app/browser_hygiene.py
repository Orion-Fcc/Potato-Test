"""浏览器卫生与就绪判定：在 agent 动手之前，让浏览器处于可用状态的那几件事。

为什么单独一个模块
==================
这些逻辑此前长在 `executor.py` 里，占掉 600 多行（executor 当时 3572 行），却被夹在
用例执行、判定、视频收尾之间 —— 想知道"到底往页面里注了什么"要翻半天。

它们的主题是统一的：**把浏览器收拾到能干活的状态**。
  * 拦掉内网够不着的分析/广告域名（否则 SPA 卡在"加载中"直到超时）；
  * 裁掉 Element Plus 的图标精灵（每步 20s+ 的隐藏成本）；
  * 控制每步截图开关（截图与录像都会拖慢每一步）；
  * 把一次多动作调用截断到安全长度（超长动作会让浏览器静默卡死）；
  * 判定"页面到底加载完没有"（早读一步就会把"暂无数据"当成"查不到"）。

这些不是产品功能，而是让产品能跑起来的地基。

依赖方向
--------
本模块只依赖 `app.config` 与标准库，且**绝不 import executor** —— 这个方向必须保持，
否则 executor 无法 import 它（循环）。
"""

from __future__ import annotations

import asyncio
import logging
import re

from app.config import get_settings

log = logging.getLogger("potato-test.hygiene")


def safe(fn):
    try:
        return fn()
    except Exception:
        return None


# a page whose URL looks like a login/auth screen — used to detect a dead restored
# session (P3) and a capture that didn't actually log in (P2.5). Apps typically redirect an
# unauthenticated visit to /login, so the URL trail is a reliable signal.
_LOGIN_URL_RE = re.compile(r"login|sign-?in|/auth\b", re.I)

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


def _last_url_is_login(history) -> bool:
    """True if the agent's final page looked like a login screen (read from history —
    robust, unlike probing the live browser after the run)."""
    urls = safe(lambda: history.urls()) or []
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
            out = await original(self, kept)
            # ★ 导航后自动等页面就绪（2026-10-06）。
            # 现场：result 3/4 只走 1 步就放弃 —— navigate 之后页面还在
            # 「正在加载中请稍后......」，agent 看到的就是占位，于是宣布无法访问。
            # 它不是想放弃，是**从来没机会看到第二眼**。
            #
            # 放在这一步之后而不是步回调里：只有这里知道"这批动作里有没有导航"，
            # 而 SPA 内部的路由切换（点菜单）不经过 navigate，那种情况由
            # 下一轮模型自己看到新页面解决。
            if any(_action_name(a) in ("navigate", "go_back") for a in kept):
                _st = await _wait_until_ready(self.browser_session)
                if not _st.startswith("ready"):
                    log.info("executor: 导航后页面未就绪（%s），已等待", _st)
            return out

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
# 注释说"反正很快"，但实测在真实视口下并不快：profiler 抓到每步热点就是
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


# ---------------------------------------------------------------------------
# 导航后自动等页面就绪（2026-10-06）
#
# 现场（21:19-21:23 那批）：result 3 / 4 **只走 1 步**就放弃 ——
# navigate 之后页面还在「正在加载中请稍后......」，agent 看到的就是一个加载占位，
# 于是宣布"无法访问目标页面"。它不是想放弃，是**从来没机会看到第二眼**：
# 下一步要等模型返回，而模型看到的就是占位。
#
# 为什么不能靠提示词：agent 的判断没错——它看到的东西确实是个加载占位。
# 要求它"多试几次"没有意义，它手里没有别的信息。**要给它时间**。
#
# 为什么不做成"让 agent 自己 wait"：那要消耗一个步数配额，而步数是硬预算
# （120 步）。这里走代码层：导航后自动轮询 DOM，等到可交互元素出现为止，
# 不消耗任何步数 —— 这也正好对应用户说的"多花点时间没关系，但是不要无效操作"。
# ---------------------------------------------------------------------------

# 页面级"还在加载"的判据。刻意只看**整页无交互元素 + 出现加载字样**：
# 只匹配文案会误伤（有页面在正文里写"加载中"这种说明），
# 只匹配 0 交互元素会误伤（空态页也是 0 交互）。
# 必须是 raw string：这段 JS 要原样送进浏览器，\. 在 Python 里若不r 开头
# 会先被当成无效转义序列（SyntaxWarning），送到浏览器时就成了裸的 "."，
# 正则含义从"字面三个点"变成"任意字符"。
_WAIT_READY_JS = r"""
() => {
  // Page text is returned RAW and judged on the Python side by app/page_gate.py.
  //
  // Why not judge in here: the needles are a setting (page_loading_texts), and a setting
  // that only exists inside a JS string literal cannot be unit-tested — the exact thing
  // that broke in the field (a hardcoded list that silently stopped matching when the
  // target switched UI frameworks, leaving a gate that always passed). Text is capped at
  // 400 chars because that is all page_gate.verdict looks at anyway; sending the whole
  // body of a 33k-node SPA over CDP on every poll is pure waste.
  const txt = (document.body?.innerText || '').slice(0, 400);
  const interactive = document.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select, textarea, ' +
    '[role="button"], [role="tab"], [role="combobox"], [role="menuitem"], [contenteditable="true"]'
  ).length;
  return JSON.stringify({ text: txt, interactive });
}
"""

# 轮询参数。首屏 SPA（Vue）实测 1-3 秒渲染完；默认给到 20 秒是给慢网络留余量，
# 而单步上限是 45s（见上面的慢步骤告警），所以这个等待不会成为新的瓶颈。
#
# 实测依据（2026-10-07，内网 SPA）：该系统首屏挂「正在加载中请稍后......」**30 秒+**
# 是常态，12 秒的旧默认值会有一半的导航被记成 timeout(last=loading)，而那不是失败 ——
# 只是没等够。这里让超时可配（page_ready_gate_seconds），但**不因为超时就报错**：
# 等不到就返回描述，让 agent 看到真实的页面状态自己判断（见 _wait_until_ready 的说明）。
_WAIT_READY_TIMEOUT_S = 20.0
_WAIT_READY_INTERVAL_S = 0.6


async def _wait_until_ready(browser, timeout_s: float | None = None) -> str:
    """轮询直到页面出现可交互元素（或超时）。返回就绪状态描述，供日志核对。

    刻意**不抛异常**：等不到就返回描述，让调用方继续走 —— agent 随后会自己看到
    那个占位并给出结论，而那份结论此时是合法的（页面确实没加载出来）。

    `timeout_s=None` 时读设置 page_ready_gate_seconds，于是"等多久"变成可调项而不是
    写死的常数 —— 内网慢环境和本地快环境对同一个数字的合理值差好几倍。
    """
    #★`time.sleep()` 是**同步**的，返回 None，不能 await ——
    # 我第一版写成 `await _time.sleep(...)`，结果每次导航都抛
    # "TypeError: object NoneType can't be used in 'await' expression"，
    # 整条用例直接崩（实测连挂 10 条）。异步版本是 `asyncio.sleep`。
    # 这个错只在真的等不到就绪、走到 sleep 那一行时才暴露 —— 页面秒开时测不出来。
    import asyncio as _aio
    import json as _json
    import time as _time

    from app import page_gate

    if timeout_s is None:
        timeout_s = _WAIT_READY_TIMEOUT_S
        try:
            timeout_s = float(get_settings().page_ready_gate_seconds) or _WAIT_READY_TIMEOUT_S
        except Exception as exc:  # noqa: BLE001 — 配置读不到就用默认值
            log.debug("executor: 读 page_ready_gate_seconds 失败，用默认 %s（%s）", _WAIT_READY_TIMEOUT_S, exc)
    needles = page_gate.parse_texts(
        safe(lambda: get_settings().page_loading_texts),
        page_gate.DEFAULT_LOADING_TEXTS,
    )

    try:
        cdp = await browser.get_or_create_cdp_session()
    except Exception as exc:  # noqa: BLE001 — 拿不到 CDP 就别拖住用例
        return f"skip(cdp:{type(exc).__name__})"

    deadline = _time.monotonic() + timeout_s
    last = "?"
    while _time.monotonic() < deadline:
        try:
            res = await cdp.cdp_client.send.Runtime.evaluate(
                params={"expression": _WAIT_READY_JS, "returnByValue": True},
                session_id=cdp.session_id,
            )
            raw = (res or {}).get("result", {}).get("value")
            if isinstance(raw, str):
                d = _json.loads(raw)
                if d.get("interactive", 0) > 0:
                    return f"ready({d['interactive']})"
                # 没有可交互元素时才去看文案 —— 顺序不能反。
                #
                # 反过来会误伤：Element Plus 的表格在刷新时**既有**loading 提示**又有**
                # 可点的刷新/分页按钮，于是"有交互元素"与"正在加载"同时成立。若把
                # loading 当最高优先级，一个正在正常刷新的列表会被判成还没好，然后
                # 白等到超时 —— 把一个健康页面报成没就绪。
                v = page_gate.verdict(
                    d.get("text", ""), target_present=False, texts=needles
                )
                last = "loading" if v == "loading" else "empty"
        except Exception:  # noqa: BLE001 — 中途失败就再试一轮，不放弃
            last = "err"
        await _aio.sleep(_WAIT_READY_INTERVAL_S)
    return f"timeout(last={last},texts={page_gate.describe_texts(needles)})"


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



