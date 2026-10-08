r"""Attach to a browser the human already started, instead of launching our own.

Why this module exists
======================
Running the case suite against an **internal-network** SPA (the kind that only answers
on 10.x.x.x) exposed a cost that had nothing to do with testing: every case that needs a
different role had to re-do the whole login ritual — tenant box, username, password, then
wait for the SPA to finish booting. Measured on the real system: the SPA sits on its
"正在加载中请稍后......" splash for 30s+ at times, and with 13 accounts and a suite where
most cases switch roles, login was dominating the run.

The login itself is not the interesting part. What is interesting is what the browser
already knows: the access token lives in ``localStorage``, the tenant id lives in
``localStorage``, and neither needs re-entering. A browser that is **already logged in**
carries all of it.

So: if the human starts the browser with ``--remote-debugging-port=9222`` and logs in
once, the suite can attach to that window and inherit the session.

Three rules this module must never break
========================================
1. **Never launch, never kill.** When an endpoint is configured we attach and nothing
   else. The human's browser is *their* session — closing it, or letting a run's cleanup
   path call ``stop()`` on it, would take their logged-in window down. Every function
   here is read-only with respect to the browser process.
2. **Never guess a port.** We only use an endpoint the human wrote down in settings.
   Probing ports would risk attaching to something that is not a browser.
3. **A missing endpoint is a normal state, not an error.** ``""`` (the default) means
   "launch our own", which is exactly how this ran before. The caller decides whether a
   failed attach should fall back or abort; see ``browser_cdp_fallback``.

Why pure functions
------------------
Same rule as ``browser_binary``: ``app/executor.py`` is the file most likely to trigger
approval prompts, and this decision must be testable with no browser and no network.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

log = logging.getLogger("potato-test.cdp")

# Browser's own default. Used only when the human wrote a bare port; the number is
# Chrome/Edge's documented default, not a guess.
DEFAULT_CDP_PORT = 9222

# A bare port is 1-5 digits with no leading zero. Deliberately stricter than int() so
# "9222abc" and "" both fall into the "not a port" branch instead of half-parsing.
_BARE_PORT_RE = re.compile(r"^\d{1,5}$")

# Loopback only. Attaching to a non-local endpoint would mean driving someone else's
# browser over the network, which is not something settings should be able to cause by
# accident — so it is refused here rather than passed to Playwright and hoped for.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def normalize_endpoint(raw: str | None) -> str | None:
    """Turn whatever is in settings into ``host:port`` — or ``None`` if unusable.

    Accepts the three shapes a human actually writes:

    - ``""`` / ``None``      → ``None`` (feature off; caller launches its own browser)
    - ``"9222"``             → ``"127.0.0.1:9222"``
    - ``"127.0.0.1:9222"``   → unchanged
    - ``"http://localhost:9222"`` → ``"localhost:9222"``

    Returns ``None`` — never raises — for the cases we must not silently accept:

    - a non-loopback host (see ``_ALLOWED_HOSTS``)
    - a port outside 1..65535
    - anything unparseable

    A bad value disabling the feature is deliberate: a typo in ``.env`` should degrade
    to the well-tested launch path, not take the whole suite down. The caller logs a
    warning with the offending value so the typo is still visible.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # Bare port.
    if _BARE_PORT_RE.match(s):
        # 单独命名，不与下面从 urlparse 取出的 `port` 共用一个变量：
        # 那个是 `int | None`（"localhost" 可以不带端口），这个是纯 int。
        # 共用一个名字会让类型推断锁死在 int 上，`port is None` 那条分支就变成死代码。
        bare_port = int(s)
        if 1 <= bare_port <= 65535:
            return f"127.0.0.1:{bare_port}"
        log.warning("cdp: 端口 %s 超出范围，已忽略（改为自启动浏览器）", s)
        return None

    # Anything with a scheme or a path still ends up parsed by urlparse; we only take
    # host and port from it.
    candidate = s if "//" in s else f"//{s}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        log.warning("cdp: 无法解析 BROWSER_CDP_ENDPOINT=%r，已忽略", raw)
        return None

    host = (parsed.hostname or "").strip()
    if not host:
        log.warning("cdp: BROWSER_CDP_ENDPOINT=%r 里没有主机名，已忽略", raw)
        return None
    if host not in _ALLOWED_HOSTS:
        # Not an error path we support. Say so loudly, because silently attaching to a
        # remote browser would be much worse than not attaching at all.
        log.warning(
            "cdp: 只允许 attach 回环地址（%s），收到 %r —— 已忽略，继续自启动浏览器。"
            "远程 attach 有 security implications，不从配置里开启。",
            "/".join(sorted(_ALLOWED_HOSTS)),
            host,
        )
        return None

    try:
        port = parsed.port
    except ValueError:
        log.warning("cdp: BROWSER_CDP_ENDPOINT=%r 的端口不合法，已忽略", raw)
        return None
    if port is None:
        # "localhost" with no port: Playwright would default to 9222, but being
        # explicit keeps the log line honest about where we think we are going.
        port = DEFAULT_CDP_PORT

    return f"{host}:{port}"


def describe(endpoint: str | None) -> str:
    """One line for the log, saying what we will do and why."""
    if not endpoint:
        return "自启动浏览器（BROWSER_CDP_ENDPOINT 未配置）"
    return f"attach 已开调试端口的浏览器 {endpoint}（不启动、不关闭）"
