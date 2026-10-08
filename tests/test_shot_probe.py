"""Tests for :mod:`app.shot_probe` — layout facts from pixels, cross-checked with DOM text.

Frames are synthesised rather than captured: a PNG on disk would make this a browser
test, and the point of the module is that its verdict comes from measurable geometry, so
the geometry can be handed to it directly. Two of the cases reproduce failures actually
observed on 2026-10-07.
"""

from __future__ import annotations

import io

import pytest

from app.shot_probe import (
    LAYOUT_BLANK,
    LAYOUT_FILLED,
    LAYOUT_SPARSE,
    analyse,
    analyse_pixels,
    corroborate,
    describe,
    evidence_line,
    screenshot_hint,
)

W, H = 200, 120
WHITE = (255, 255, 255)
DARK = (60, 60, 60)


def _frame(
    *,
    ink_rows: range | None = None,
    ink_cols: range | None = None,
    bg: tuple[int, int, int] = WHITE,
    ink: tuple[int, int, int] = DARK,
    width: int = W,
    height: int = H,
) -> list[tuple[int, int, int]]:
    """A background-coloured frame with a block of `ink` drawn on it."""
    px = [bg] * (width * height)
    rows = ink_rows if ink_rows is not None else range(0, height, 2)
    cols = ink_cols if ink_cols is not None else range(0, width, 2)
    for y in rows:
        for x in cols:
            px[y * width + x] = ink
    return px


# ── geometry ───────────────────────────────────────────────────────────────────────


def test_a_flat_frame_is_blank():
    facts = analyse_pixels(W, H, [WHITE] * (W * H))
    assert facts.layout == LAYOUT_BLANK
    assert facts.is_blank
    assert facts.readable


def test_a_frame_with_content_is_filled():
    facts = analyse_pixels(W, H, _frame())
    assert facts.layout == LAYOUT_FILLED
    assert not facts.is_blank
    # A checkerboard over every other row and column is a quarter of the frame.
    assert facts.ink_ratio == pytest.approx(0.25, abs=0.02)


def test_shell_rendered_body_empty_is_sparse():
    """The 「首屏卡在正在加载中」 shape: header drawn, body never painted.

    This is the layout that page_gate cannot see — the DOM has text ("正在加载中"), so
    the gate reports loading, but the visual proof that nothing rendered is here.
    """
    px = [WHITE] * (W * H)
    for y in range(0, 14):  # header band only, top 12% of the frame
        for x in range(W):
            px[y * W + x] = DARK
    facts = analyse_pixels(W, H, px)
    assert facts.layout == LAYOUT_SPARSE
    assert facts.bottom_empty


def test_bottom_empty_needs_the_top_to_have_ink():
    """An entirely blank frame is not "shell without body".

    Otherwise every blank page would report sparse as well as blank, and the distinction
    that makes the blank verdict useful would be lost.
    """
    facts = analyse_pixels(W, H, [WHITE] * (W * H))
    assert facts.bottom_empty is False


def test_tiny_frames_are_unreadable_not_blank():
    """A 4-pixel image is not a blank page, it is a broken capture.

    Marking it blank would let a decode failure look like an empty system, which is the
    one conclusion this module exists to avoid.
    """
    facts = analyse_pixels(2, 2, [WHITE] * 4)
    assert facts.readable is False
    assert facts.layout == LAYOUT_FILLED
    assert facts.is_blank is False


def test_short_pixel_list_is_unreadable():
    facts = analyse_pixels(W, H, [WHITE] * 10)
    assert facts.readable is False


def test_zero_sized_frame_is_unreadable():
    facts = analyse_pixels(0, 0, [])
    assert facts.readable is False
    assert facts.is_blank is False


def test_ratios_are_between_zero_and_one():
    px = _frame(ink=DARK)
    facts = analyse_pixels(W, H, px)
    for v in (facts.unique_ratio, facts.ink_ratio, facts.top_ink_ratio, facts.bottom_ink_ratio):
        assert 0.0 <= v <= 1.0


def test_top_and_bottom_add_up_to_the_whole_frame():
    """Half-height splits are even here; the halves must reconcile with the total."""
    facts = analyse_pixels(W, H, _frame())
    mid_ink = (facts.top_ink_ratio + facts.bottom_ink_ratio) / 2
    assert mid_ink == pytest.approx(facts.ink_ratio, abs=0.01)


# ── PNG decoding ───────────────────────────────────────────────────────────────────


def _png(pixels, w=W, h=H) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (w, h))
    im.putdata(pixels)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_analyse_reads_a_real_png():
    facts = analyse(_png(_frame()))
    assert facts.readable
    assert facts.layout == LAYOUT_FILLED


def test_analyse_a_real_blank_png():
    facts = analyse(_png([WHITE] * (W * H)))
    assert facts.is_blank


def test_corrupt_bytes_yield_an_unreadable_frame_not_a_blank_one():
    facts = analyse(b"not a png at all")
    assert facts.readable is False
    assert facts.is_blank is False


def test_missing_bytes_yield_an_unreadable_frame():
    assert analyse(None).readable is False
    assert analyse(b"").readable is False


def test_large_frames_are_downsampled_not_skipped():
    """A full-HD frame must still be judged, just on a cheaper grid."""
    from PIL import Image

    w, h = 1920, 1080
    im = Image.new("RGB", (w, h), WHITE)
    px = list(im.getdata())
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            px[y * w + x] = DARK
    im.putdata(px)
    buf = io.BytesIO()
    im.save(buf, format="PNG")

    facts = analyse(buf.getvalue())
    assert facts.readable
    assert facts.layout == LAYOUT_FILLED
    # Downsampled, so the reported size is smaller — what matters is that it judged.
    assert facts.width < w


def test_downsampling_must_not_wipe_out_sparse_content():
    """Regression: a full page judged blank because resampling averaged the ink away.

    A 1920x1080 grid of dark dots on white is a page of table rows. Downsampled with
    the default resampling, those dots average into a uniform near-white wash and the
    frame reads `blank` — the module then contradicts a perfectly healthy page, which is
    the exact failure it was written to prevent. So the downsample must be nearest-neighbour.
    """
    from PIL import Image

    from app.shot_probe import _MAX_PIXELS

    w, h = 1920, 1080
    assert w * h > _MAX_PIXELS, "this case only means something if it exceeds the cap"
    im = Image.new("RGB", (w, h), WHITE)
    px = list(im.getdata())
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            px[y * w + x] = DARK
    im.putdata(px)
    buf = io.BytesIO()
    im.save(buf, format="PNG")

    facts = analyse(buf.getvalue())
    assert not facts.is_blank, "downsampling erased the content; use Image.NEAREST"
    assert facts.ink_ratio > 0.05


# ── corroboration against the DOM text ─────────────────────────────────────────────


def test_text_says_empty_but_frame_has_content_is_contradicted():
    """The expensive mistake: filing "data loss" because a text snapshot missed it."""
    facts = analyse_pixels(W, H, _frame())
    assert corroborate("查询结果：共 0 条，暂无数据", facts) == "contradicted"


def test_text_says_missing_but_frame_has_content_is_contradicted():
    facts = analyse_pixels(W, H, _frame())
    assert corroborate("我在列表里查不到这个学员", facts) == "contradicted"


def test_a_blank_frame_never_corroborates_an_empty_claim():
    """No "agrees" outcome exists, and this is why.

    "The text says missing AND the frame shows nothing" looks like two independent
    confirmations. It is not — a page that never finished rendering looks identical.
    Treating it as agreement is exactly how a loading spinner becomes a filed bug.
    """
    facts = analyse_pixels(W, H, [WHITE] * (W * H))
    assert corroborate("共 0 条", facts) == "neutral"
    assert corroborate("查不到这条数据", facts) == "neutral"


def test_a_blank_frame_does_not_corroborate_anything():
    """A frame with nothing in it proves nothing — it may simply not have rendered.

    Treating it as agreement would manufacture a defect out of a loading spinner.
    """
    facts = analyse_pixels(W, H, [WHITE] * (W * H))
    assert corroborate("列表里查不到这条数据", facts) == "neutral"


def test_unreadable_frame_is_always_neutral():
    facts = analyse_pixels(2, 2, [WHITE] * 4)
    assert corroborate("共 0 条", facts) == "neutral"
    assert corroborate("查不到", facts) == "neutral"


def test_text_making_no_emptiness_claim_is_neutral():
    facts = analyse_pixels(W, H, _frame())
    assert corroborate("学员姓名：张三，证件号：110101199001011234", facts) == "neutral"


def test_normal_page_text_is_neutral():
    facts = analyse_pixels(W, H, _frame())
    assert corroborate("共 25 条 第 1/3 页", facts) == "neutral"


def test_a_sparse_frame_does_not_count_as_content_contradicting_the_text():
    """Header-only frames are not evidence that data is there."""
    px = [WHITE] * (W * H)
    for y in range(0, 14):
        for x in range(W):
            px[y * W + x] = DARK
    facts = analyse_pixels(W, H, px)
    assert facts.layout == LAYOUT_SPARSE
    assert corroborate("共 0 条", facts) != "contradicted"


# ── evidence for the attribution classifier ────────────────────────────────────────


def test_evidence_line_is_emitted_on_contradiction():
    facts = analyse_pixels(W, H, _frame())
    line = evidence_line("共 0 条", facts)
    # The 【截图与文本矛盾】 prefix is the contract with app.failure_attrib. Changing it
    # disables the feature silently, so assert the exact token.
    assert "【截图与文本矛盾】" in line
    assert "不可信" in line


def test_evidence_line_mentions_a_blank_frame_when_data_was_claimed_missing():
    facts = analyse_pixels(W, H, [WHITE] * (W * H))
    line = evidence_line("列表里查不到", facts)
    # A distinct token, deliberately: this line says the frame proves nothing, which is
    # the opposite of the contradicting one.
    assert "【截图未渲染】" in line
    assert "尚未渲染" in line


def test_the_two_evidence_lines_never_share_a_token():
    """They point opposite ways; one token would make them read the same."""
    filled = analyse_pixels(W, H, _frame())
    blank = analyse_pixels(W, H, [WHITE] * (W * H))
    a = evidence_line("共 0 条", filled)
    b = evidence_line("共 0 条", blank)
    assert a and b
    assert a.split("】")[0] + "】" != b.split("】")[0] + "】"


def test_evidence_line_is_empty_when_there_is_nothing_to_report():
    """Silence is the default. Extra neutral lines dilute the classifier's evidence."""
    filled = analyse_pixels(W, H, _frame())
    assert evidence_line("共 25 条", filled) == ""
    assert evidence_line("共 0 条", filled) != ""  # only the contradicting case speaks
    blank = analyse_pixels(W, H, [WHITE] * (W * H))
    # A blank frame DOES speak, but as "unrendered" — context, not a verdict.
    assert evidence_line("共 0 条", blank) == evidence_line("共 0 条", blank)
    assert evidence_line("共 0 条", blank) != ""
    # And with no emptiness claim at all, nothing is said whatever the frame looks like.
    assert evidence_line("共 25 条", blank) == ""


def test_blank_frame_line_fires_on_a_system_zero_result_too():
    """"共 0 条" with an entirely blank frame is the strongest unrendered signal there is.

    A genuinely empty list draws an empty-state ("暂无数据") and the filter form above it,
    so there would be ink. Keying only on the agent's own 「查不到」 phrasing would miss
    exactly the measured case this line exists for — the table had not fetched yet and the
    system reported zero results.
    """
    blank = analyse_pixels(W, H, [WHITE] * (W * H))
    assert "【截图未渲染】" in evidence_line("共 0 条", blank)
    assert "【截图未渲染】" in evidence_line("暂无数据", blank)
    assert "【截图未渲染】" in evidence_line("列表里查不到", blank)


def test_evidence_line_is_empty_for_an_unreadable_frame():
    facts = analyse_pixels(2, 2, [WHITE] * 4)
    assert evidence_line("共 0 条", facts) == ""


def test_evidence_line_never_mentions_a_defect():
    """The line must not push either verdict — the classifier decides, this only reports."""
    filled = analyse_pixels(W, H, _frame())
    blank = analyse_pixels(W, H, [WHITE] * (W * H))
    for text in ("共 0 条", "查不到"):
        for facts in (filled, blank):
            line = evidence_line(text, facts)
            if line:
                assert "缺陷" not in line


# ── prompt hint ────────────────────────────────────────────────────────────────────


def test_blank_frame_hint_forbids_declaring_a_defect():
    facts = analyse_pixels(W, H, [WHITE] * (W * H))
    hint = screenshot_hint(facts)
    assert "空白" in hint
    assert "不要" in hint and "缺陷" in hint


def test_sparse_frame_hint_forbids_declaring_data_loss():
    px = [WHITE] * (W * H)
    for y in range(0, 14):
        for x in range(W):
            px[y * W + x] = DARK
    hint = screenshot_hint(analyse_pixels(W, H, px))
    assert "不要" in hint


def test_filled_frame_gets_no_hint():
    """A hint here would push the writer to describe the UI instead of the behaviour."""
    assert screenshot_hint(analyse_pixels(W, H, _frame())) == ""


def test_unreadable_frame_gets_no_hint():
    assert screenshot_hint(analyse_pixels(2, 2, [WHITE] * 4)) == ""


# ── logging ────────────────────────────────────────────────────────────────────────


def test_describe_states_the_verdict():
    assert "空白页" in describe(analyse_pixels(W, H, [WHITE] * (W * H)))
    assert "有内容" in describe(analyse_pixels(W, H, _frame()))


def test_describe_says_unreadable_rather_than_guessing():
    assert "不可读" in describe(analyse_pixels(2, 2, [WHITE] * 4))


# ── purity ─────────────────────────────────────────────────────────────────────────


def test_analysis_is_deterministic():
    """Two reads of the same frame must agree.

    This verdict decides whether a tester files a bug; drift between reads of an
    identical frame would make the report meaningless.
    """
    px = _frame()
    a = analyse_pixels(W, H, px)
    b = analyse_pixels(W, H, list(px))
    assert a == b


def test_as_dict_is_json_serialisable():
    import json

    d = analyse_pixels(W, H, _frame()).as_dict()
    assert json.loads(json.dumps(d))["layout"] == LAYOUT_FILLED


def test_facts_is_frozen():
    facts = analyse_pixels(W, H, _frame())
    with pytest.raises(Exception):
        facts.layout = LAYOUT_BLANK  # type: ignore[misc]