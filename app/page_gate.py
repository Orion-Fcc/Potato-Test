r"""Decide whether a page has actually finished rendering, before touching it.

The problem this solves
=======================
Measured against the internal SPA (2026-10-07), two failures shared one root cause —
**we read the page too early**:

1. The recruit-student list is a ``vxe-table`` virtual table. While the data request is
   still in flight the page shows ``共 0 条 / 暂无数据`` and zero rows. An agent that
   reads that concludes "the trainee I just created does not exist" and fails the case.
   Moments later the same page, with nothing changed, shows 36 rows. The system was never
   wrong; we asked too early.
2. Element Plus dialogs keep their DOM subtree mounted while the close animation plays.
   A snapshot taken during that window still contains the dialog that the human can no
   longer see, so a follow-up action targets an element that is already gone.

Both look identical from the outside — "the expected thing isn't there" — and both are
invisible to a purely time-based wait, because how long the page takes is not knowable
in advance. So readiness has to be read off the page.

What "ready" means here
-----------------------
Not "blank" and not "loading" — specifically:

- no skeleton/splash text from :data:`DEFAULT_LOADING_TEXTS` (configurable), and
- the element the step is about to touch is actually attached.

The second half matters as much as the first: many SPAs drop their splash text early and
then spend seconds mounting the real tree.

Why the texts are configurable
------------------------------
A hardcoded list silently stops working when the app switches frameworks — Element Plus
says 正在加载中, antd says Loading... — and a gate that quietly always passes is worse
than no gate, because it looks like protection. Making it a setting means changing it is
a one-line edit rather than a code change, and ``describe_texts`` puts the active list in
the log so a stale value is visible instead of invisible.

No browser, no I/O
------------------
Same rule as ``browser_binary`` and ``cdp_endpoint``: this file must stay unit-testable.
Every function takes text and returns a verdict.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("potato-test.gate")

DEFAULT_LOADING_TEXTS: tuple[str, ...] = (
    "正在加载中",
    "加载中",
    "Loading",
    "loading...",
    "请稍后",
)

# Element Plus renders "正在加载中请稍后......" as one text node; the ellipsis varies
# (...... / … / ...), so we normalise it away before matching. Without this, a text that
# is "loading..." with three dots would not match a rule written with six.
_ELLIPSIS_RE = re.compile(r"[.…]{1,6}")


def parse_texts(raw: str | None, defaults: tuple[str, ...] = DEFAULT_LOADING_TEXTS) -> list[str]:
    """Split the comma-separated setting into a clean list of needles.

    Empty entries are dropped (``"加载中,,请稍后"`` is a typo, not a request to match the
    empty string — which would match everything and disable the gate by accident).
    """
    if raw is None:
        return list(defaults)
    parts = [p.strip() for p in str(raw).split(",")]
    kept = [p for p in parts if p]
    return kept or list(defaults)


def describe_texts(texts: list[str]) -> str:
    """Log line naming the active needles — a stale list should be *visible*."""
    return ", ".join(repr(t) for t in texts) or "(空)"


def normalize(text: str) -> str:
    """Collapse whitespace and unify ellipsis, so matching is about words not glyphs."""
    if not text:
        return ""
    t = _ELLIPSIS_RE.sub("...", str(text))
    return re.sub(r"\s+", "", t).lower()


def is_loading(page_text: str, texts: list[str]) -> bool:
    """True if the page still shows a loading splash.

    Matched on whitespace-stripped, lowercased text. The window is a small prefix
    check rather than a plain ``in`` because the splash is typically the first thing on
    screen; checking the first ~200 normalised chars keeps a page whose *data* happens
    to contain 加载中 from being mistaken for a splash.
    """
    if not page_text:
        return False
    hay = normalize(page_text)[:200]
    return any(normalize(t) in hay for t in texts if t)


def verdict(
    page_text: str,
    *,
    target_present: bool | None = None,
    texts: list[str] | None = None,
) -> str:
    """One word the caller can branch on: ``ready`` / ``loading`` / ``no_target``.

    - ``loading``   — a splash is still up. Wait; this is not an error.
    - ``no_target`` — no splash, but the element this step needs is missing. The page
      finished, and what we wanted genuinely isn't there (or hasn't mounted yet).
      Distinguished from ``loading`` because the two need opposite handling: one means
      "be patient", the other means "look harder".
    - ``ready``     — safe to act.

    ``target_present=None`` means the caller has no target to check, so only the splash
    test applies.
    """
    needles = texts if texts is not None else list(DEFAULT_LOADING_TEXTS)
    if is_loading(page_text, needles):
        return "loading"
    if target_present is False:
        return "no_target"
    return "ready"
