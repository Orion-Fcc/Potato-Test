"""Read layout facts out of a screenshot, and cross-check them against the DOM text.

Why this exists
---------------
The tool files two kinds of bug report that are not bugs, and both times the *only*
evidence was "I can't find it":

  * a trainee list read mid-request showed 「共 0 条」 — the record was there, the table
    was still fetching;
  * an import that was never submitted showed an empty list — the data was there, in a
    draft.

In both cases the agent said "查不到". It was not lying, it was reporting what the page
text said. The problem is that page text alone cannot separate three very different
worlds that all look identical in text:

    the thing is genuinely absent   -> a real defect, file it
    the thing is there but the text snapshot missed it -> our problem, do not file
    the page never rendered         -> still loading, do not file

Screenshots discriminate between them. A blank viewport has a very different pixel
signature from a populated table, and a shell of chrome with an empty body has a
different signature again from a fully blank page.

What this module deliberately does NOT do
-----------------------------------------
It does not OCR, and it does not ask a model to look at the picture. Both were rejected
on purpose:

  * OCR on a Chinese enterprise SPA renders field labels and button captions wrong often
    enough that a confident wrong reading is worse than no reading — and this module's
    entire value is that it cannot be confidently wrong;
  * a vision model call per step costs more than the step itself, and the agent already
    gets the screenshot when it needs one.

So this reads *layout*, not *content*: how many distinct colours, how much ink, whether
the ink is all up top. Those are measurable properties of any UI, which is what makes
them testable without a browser and without a model.

Design notes
------------
Pure functions over decoded pixels, no browser and no LLM — same rule as
:mod:`app.page_gate`. Every verdict here is ``layout``, never ``content``: this module
answers "does this frame show anything at all", and refuses to answer "what does it say".
Mixing those two is how you get a confident bug report about a spinner.

The thresholds are deliberately coarse. This is a corroborating signal, not a detector —
it is meant to overturn "查不到" when the frame obviously contradicts it, and to stay
quiet otherwise. A page that is 88% ink is not more "defective" than one that is 84%.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

# Quantise to 5 bits per channel before counting distinct colours. 8 bits would count
# anti-aliasing noise as hundreds of "colours" and every gradient would look like content.
_QUANT_BITS = 5
_QUANT_SHIFT = 8 - _QUANT_BITS

# A pixel counts as ink when it differs from the frame's dominant colour by more than
# this on the summed channel distance. Below it, gradient banding and text anti-aliasing
# are treated as background — they carry no layout information.
_INK_MIN_DISTANCE = 36

# unique_ratio < this  ->  effectively one colour; the viewport is blank or a flat wash.
_BLANK_UNIQUE_RATIO = 0.004
# ink_ratio < this     ->  nothing is drawn regardless of how many colours there are.
_BLANK_INK_RATIO = 0.015
# ...but only if the frame is also large enough to judge. A 4-pixel image is not blank,
# it is broken.
_MIN_JUDGEABLE_PIXELS = 4096
# bottom_ink vs top_ink below this ratio -> the lower half is materially emptier.
# 0.12 rather than 0.5 because real pages often have a short list under a tall filter
# form; the signal is meant to catch "nothing rendered below the header", not "sparse page".
_BOTTOM_TOP_RATIO = 0.12
# Both halves must have some ink before the comparison means anything — an all-blank
# frame would otherwise look maximally "bottom empty".
_HALF_MIN_INK = 0.004

# Frames larger than this are sampled down before measuring. A 4K frame is 8M pixels
# and the layout facts saturate long before that.
_MAX_PIXELS = 400_000

LAYOUT_BLANK = "blank"
LAYOUT_SPARSE = "sparse"
LAYOUT_FILLED = "filled"

LAYOUT_LABELS_ZH: dict[str, str] = {
    LAYOUT_BLANK: "空白页",
    LAYOUT_SPARSE: "有壳无内容",
    LAYOUT_FILLED: "有内容",
}

_EMPTY_TEXT_MARKERS = (
    "共 0 条",
    "共0条",
    "暂无数据",
    "无数据",
    "没有数据",
    "暂无",
    "空空如也",
    "查询无结果",
    "未找到相关",
    "total 0",
)
_MISSING_TEXT_MARKERS = (
    "查不到",
    "找不到",
    "未找到",
    "没有找到",
    "看不到",
    "没有显示",
    "不存在",
    "列表为空",
)

# Line prefixes that app.failure_attrib keys on. Two distinct claims, deliberately not
# one token — they point in opposite directions and must not be read the same way:
#
#   CONTRADICTED: the frame has content, so the text snapshot is the unreliable one.
#                  The classifier acts on this.
#   UNRENDERED:   the frame drew nothing, so the frame itself proves nothing. The
#                  classifier must NOT treat this as agreement — a page that never
#                  finished loading produces exactly this frame. It rides along as
#                  context only.
#
# Sharing one prefix made a blank frame silently behave like a contradicting one, which
# is the exact inversion of what each one means.
_TOKEN_CONTRADICTED = "【截图与文本矛盾】"
_TOKEN_UNRENDERED = "【截图未渲染】"


@dataclass(frozen=True)
class ShotFacts:
    """Layout measurements for one frame. Every field is scale-free (0..1 unless noted).

    ``unique_ratio`` and ``ink_ratio`` are the two that matter. ``bottom_ink_ratio`` is
    the one that catches an SPA whose shell rendered and whose body did not — the exact
    shape of the "首屏卡在正在加载中" failure.
    """

    width: int
    height: int
    unique_ratio: float
    ink_ratio: float
    top_ink_ratio: float
    bottom_ink_ratio: float
    layout: str
    readable: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_blank(self) -> bool:
        return self.layout == LAYOUT_BLANK

    @property
    def bottom_empty(self) -> bool:
        """True when the top of the frame has content and the bottom has none."""
        return (
            self.readable
            and self.top_ink_ratio >= _HALF_MIN_INK
            and self.bottom_ink_ratio <= self.top_ink_ratio * _BOTTOM_TOP_RATIO
        )


def _quantise(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return ((r >> _QUANT_SHIFT) << 10) | ((g >> _QUANT_SHIFT) << 5) | (b >> _QUANT_SHIFT)


def _distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _dominant(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    """Most common exact colour. Exact, not quantised: the background of an enterprise
    SPA is a flat fill, and finding it exactly is what makes the ink test stable."""
    counts: dict[tuple[int, int, int], int] = {}
    for p in pixels:
        counts[p] = counts.get(p, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def analyse_pixels(width: int, height: int, pixels: list[tuple[int, int, int]]) -> ShotFacts:
    """Measure a frame from its raw RGB pixels.

    Split out from :func:`analyse` so the geometry can be tested with synthetic frames
    and no PNG decoder. ``pixels`` must be ``width * height`` long, row-major.
    """
    total = width * height
    if width <= 0 or height <= 0 or len(pixels) < total:
        # An unreadable frame is not evidence of anything. Marking it `filled` keeps it
        # from vetoing a DOM-based finding; `readable=False` stops it being used as one.
        return ShotFacts(
            width=max(width, 0),
            height=max(height, 0),
            unique_ratio=0.0,
            ink_ratio=1.0,
            top_ink_ratio=1.0,
            bottom_ink_ratio=1.0,
            layout=LAYOUT_FILLED,
            readable=False,
        )

    bg = _dominant(pixels)
    seen: set[int] = set()
    ink = 0
    mid = height // 2
    top_ink = 0
    top_n = 0
    bottom_ink = 0
    bottom_n = 0

    for y in range(height):
        row_start = y * width
        is_top = y < mid
        for x in range(width):
            p = pixels[row_start + x]
            seen.add(_quantise(p))
            is_ink = _distance(p, bg) > _INK_MIN_DISTANCE
            if is_ink:
                ink += 1
                if is_top:
                    top_ink += 1
                else:
                    bottom_ink += 1
            if is_top:
                top_n += 1
            else:
                bottom_n += 1

    unique_ratio = len(seen) / total
    ink_ratio = ink / total
    top_ratio = (top_ink / top_n) if top_n else 0.0
    bottom_ratio = (bottom_ink / bottom_n) if bottom_n else 0.0

    readable = total >= _MIN_JUDGEABLE_PIXELS
    if not readable:
        layout = LAYOUT_FILLED
    elif unique_ratio < _BLANK_UNIQUE_RATIO and ink_ratio < _BLANK_INK_RATIO:
        layout = LAYOUT_BLANK
    elif bottom_ratio <= top_ratio * _BOTTOM_TOP_RATIO and top_ratio >= _HALF_MIN_INK:
        layout = LAYOUT_SPARSE
    else:
        layout = LAYOUT_FILLED

    return ShotFacts(
        width=width,
        height=height,
        unique_ratio=round(unique_ratio, 5),
        ink_ratio=round(ink_ratio, 5),
        top_ink_ratio=round(top_ratio, 5),
        bottom_ink_ratio=round(bottom_ratio, 5),
        layout=layout,
        readable=readable,
    )


def analyse(png_bytes: bytes | None) -> ShotFacts:
    """Measure a PNG. Any failure yields an unreadable ``filled`` result.

    Never raises: a screenshot that cannot be decoded must not be the reason a case
    fails. An unreadable frame is deliberately *not* evidence of a blank page — the
    absence of a picture is not the presence of an empty one.
    """
    if not png_bytes:
        return analyse_pixels(0, 0, [])
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(png_bytes)) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            # Bound the work. A 4K frame is 8M pixels; the layout facts saturate long
            # before that, and this runs on every failing case's final frame.
            if w * h > _MAX_PIXELS:
                # NEAREST, not the default resampling. This is not a cosmetic choice:
                # with bilinear/bicubic, a sparse grid (a table of rows on white) gets
                # averaged into a uniform near-white wash, the ink disappears, and a page
                # that is plainly full is reported `blank` — measured on a 1920x1080
                # grid, exactly the false "this page is empty" signal the module exists
                # to prevent. Nearest keeps a sample of what was actually drawn.
                step = int((w * h / _MAX_PIXELS) ** 0.5) + 1
                small = rgb.resize(
                    (max(w // step, 1), max(h // step, 1)), Image.NEAREST
                )
                w, h = small.size
                pixels = list(small.getdata())
            else:
                pixels = list(rgb.getdata())
            return analyse_pixels(w, h, pixels)
    except Exception:  # noqa: BLE001
        return analyse_pixels(0, 0, [])


def describe(facts: ShotFacts) -> str:
    """One-line, log-safe summary."""
    if not facts.readable:
        return "截图不可读（不作为判定依据）"
    label = LAYOUT_LABELS_ZH.get(facts.layout, facts.layout)
    return (
        f"截图判定 {label}：{facts.width}x{facts.height} "
        f"色数占比={facts.unique_ratio:.3f} 墨迹占比={facts.ink_ratio:.3f} "
        f"上半={facts.top_ink_ratio:.3f} 下半={facts.bottom_ink_ratio:.3f}"
    )


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(m in low for m in markers)


def corroborate(page_text: str, facts: ShotFacts) -> str:
    """Cross-check a "not there" claim from the DOM against what the frame shows.

    Returns ``"contradicted"`` or ``"neutral"``. Only two outcomes, on purpose.

    ``"contradicted"``
        The page text claimed empty/missing while the frame plainly shows content. The
        text snapshot is what was wrong, so this is not a product defect and must not be
        filed as one. This is the case worth catching — it is the one that costs a
        developer an afternoon.

    ``"neutral"``
        Everything else, including the tempting cases.

    There is deliberately no ``"agrees"``. It looks like the symmetric third outcome and
    it would be a trap: a blank or header-only frame is exactly what a page that never
    finished rendering produces, so "the text says missing AND the frame shows nothing"
    is **not** two independent confirmations — it is one observation plus one non-observation.
    Emitting "agrees" there is precisely how a loading spinner becomes a filed bug report.

    The limitation worth stating plainly: a frame that is `filled` because a tall filter
    form is on screen, above a list that really is empty, reads as `contradicted` too.
    The evidence line therefore says the text-based conclusion is *not trustworthy* and
    asks for a re-check — it never asserts that the data is present. The classifier
    weighs it alongside everything else; nothing auto-files on this signal.
    """
    if not facts.readable:
        return "neutral"
    claims_empty = _has_marker(page_text, _EMPTY_TEXT_MARKERS) or _has_marker(
        page_text, _MISSING_TEXT_MARKERS
    )
    if not claims_empty:
        return "neutral"
    # Only a frame with real content can contradict the text. A blank or header-only
    # frame has nothing to say about whether the data was there.
    if facts.layout == LAYOUT_FILLED and not facts.bottom_empty:
        return "contradicted"
    return "neutral"


def evidence_line(page_text: str, facts: ShotFacts) -> str:
    """A line for the failure-attribution classifier to read.

    ★ The two bracket prefixes are a **contract with app.failure_attrib**:

      * 【截图与文本矛盾】 — matched by its ``_SHOT_CONTRADICTS_TEXT``, checked above every
        text pattern there, and it *moves* the verdict.
      * 【截图未渲染】 — deliberately not matched there. Context only.

    Renaming or rewording either prefix silently disables that half of the feature: the
    line still gets emitted and still shows up in the log, while nothing changes. Both
    started life as one 【截图佐证】, which made a blank frame behave like a contradiction
    — the exact inversion of what it means — so they are separate tokens now.

    Only emitted when the frame says something: it contradicts the text, or it drew
    nothing while the text claimed the data was absent. Neutral frames produce no line at
    all, because extra observations dilute the classifier's evidence for no gain.
    """
    claims_empty = _has_marker(page_text, _EMPTY_TEXT_MARKERS) or _has_marker(
        page_text, _MISSING_TEXT_MARKERS
    )
    verdict = corroborate(page_text, facts)
    if verdict == "contradicted":
        return (
            f"{_TOKEN_CONTRADICTED}页面文本称查不到数据，但最终截图显示页面有内容"
            f"（{describe(facts)}）—— 以文本快照为准的结论不可信"
        )
    # Both marker families count here, not just "查不到". A page that reports 「共 0 条」
    # while the frame is entirely blank is the strongest possible sign it never rendered:
    # a genuinely empty list draws an empty-state ("暂无数据") and the filter form above
    # it, so there would be ink. Requiring the agent's own "查不到" phrasing would miss
    # exactly the case this line exists for — the measured 「首屏卡在加载中」 failure,
    # where the system said zero results because the table had not fetched yet.
    if facts.is_blank and claims_empty:
        return f"{_TOKEN_UNRENDERED}页面称没有数据/查不到，但最终截图是空白页 —— 页面可能尚未渲染完成"
    return ""


def screenshot_hint(facts: ShotFacts) -> str:
    """Prompt-side instruction for the narrative writer, or "" when there is nothing to say.

    Only non-defect-shaped frames get a hint. A filled frame needs none: telling the
    writer "the screenshot has content" would just invite it to describe the UI instead
    of the behaviour.
    """
    if not facts.readable:
        return ""
    if facts.is_blank:
        return (
            "【截图】最终截图是空白页（几乎没有绘制内容）。请在【实际结果】里"
            "如实写明这一点，但**不要**据此判定功能缺陷 —— 页面可能尚未加载完成。"
        )
    if facts.bottom_empty:
        return (
            "【截图】最终截图只有页头/导航有内容，下方主体区域是空的。"
            "请据实描述，但**不要**据此判定数据丢失类缺陷。"
        )
    return ""