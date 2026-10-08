"""Deciding a page has finished rendering.

Every case here is a real observation from the internal SPA on 2026-10-07, where the
failure mode was symmetrical and misleading: the page said "0 rows" *and* "your trainee
is missing", and both looked like the same failure until you waited five seconds.
"""

from __future__ import annotations

from app import page_gate as pg


def test_splash_text_is_detected():
    """The exact string Element Plus renders — this is what the real page showed."""
    assert pg.is_loading("正在加载中请稍后......", list(pg.DEFAULT_LOADING_TEXTS))


def test_ellipsis_length_does_not_matter():
    """One dot, three dots, six dots, or the unicode ellipsis: all the same splash.

    Skipping this made an earlier draft match only one specific rendering, so the gate
    silently passed on the others — which is worse than having no gate.
    """
    for text in ("正在加载中...", "正在加载中", "正在加载中请稍后……", "正在加载中请稍后......"):
        assert pg.is_loading(text, list(pg.DEFAULT_LOADING_TEXTS)), text


def test_whitespace_and_case_are_normalised():
    assert pg.is_loading("  Loading...  ", ["loading..."])
    assert pg.is_loading("LOADING...", ["Loading..."])


def test_real_content_is_not_mistaken_for_a_splash():
    """A finished list page must read as ready, or the gate blocks every run."""
    text = "培训管理数字平台 招飞学员库 展开筛选 重置 查询 导入 导出 共 36 条"
    assert not pg.is_loading(text, list(pg.DEFAULT_LOADING_TEXTS))


def test_empty_page_is_not_treated_as_loading():
    """Blank ≠ still loading. An empty body is the caller's problem, not the gate's.

    If blank counted as loading, every navigation would burn the full timeout before
    the first action.
    """
    assert not pg.is_loading("", list(pg.DEFAULT_LOADING_TEXTS))


def test_only_the_top_of_the_page_is_considered():
    """Data that happens to contain 加载中 must not look like a splash.

    The splash is the first thing on screen; a hit further down is content.
    """
    filler = "共 36 条 " * 40
    assert not pg.is_loading(filler + "加载批次", ["加载中"])


def test_verdict_waits_rather_than_failing_while_loading():
    """The whole point: loading is a reason to wait, not to report a defect."""
    assert pg.verdict("正在加载中请稍后......") == "loading"


def test_verdict_separates_missing_target_from_loading():
    """These two need opposite handling, so they must not collapse into one answer.

    loading  → be patient
    no_target → the page finished; look harder / fail honestly
    """
    texts = list(pg.DEFAULT_LOADING_TEXTS)
    assert pg.verdict("招飞学员库", target_present=False, texts=texts) == "no_target"
    assert pg.verdict("招飞学员库", target_present=True, texts=texts) == "ready"
    # Loading wins even when the target happens to be attached — a splash in front of
    # the element means clicks would land on the overlay, not the button.
    assert pg.verdict("正在加载中", target_present=True, texts=texts) == "loading"


def test_verdict_without_a_target_uses_only_the_splash_test():
    assert pg.verdict("招飞学员库", target_present=None) == "ready"


def test_configured_texts_replace_the_defaults_entirely():
    """A setting that replaced the list must actually replace it, not extend it."""
    texts = pg.parse_texts(" Espere")
    assert texts == ["Espere"]
    assert not pg.is_loading("正在加载中请稍后......", texts)
    assert pg.is_loading("Espere", texts)


def test_parse_drops_empty_entries():
    """Otherwise "a,,b" produces an empty needle that matches everything.

    An empty needle matches every page, which would disable the gate in exactly the
    situation it exists for — and look like it was working.
    """
    assert pg.parse_texts("加载中,,请稍后") == ["加载中", "请稍后"]


def test_parse_falls_back_to_defaults_when_everything_is_dropped():
    """A settings value of just commas must not yield an empty needle list."""
    assert pg.parse_texts(",,,") == list(pg.DEFAULT_LOADING_TEXTS)
    assert pg.parse_texts(None) == list(pg.DEFAULT_LOADING_TEXTS)


def test_describe_texts_puts_the_active_list_in_the_log():
    """A stale configured list should be visible in the log, not invisible."""
    assert "加载中" in pg.describe_texts(["加载中", "Loading"])
    assert pg.describe_texts([]) == "(空)"
