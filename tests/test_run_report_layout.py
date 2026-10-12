"""运行报告表格的响应式对齐（2026-10-10 用户反馈"看不到所有界面"后新增）。

## 用户的问题

截图里运行报告的表格只有「用例」和状态两列能看见，其余列被推出屏幕外 ——
用户的原话是「第一点是看不到所有界面」。

原因是那张表有 **9 列**（用例/模块/优先级/状态/重试/耗时/Token/判定/操作），
最窄的也有 900px 以上，在普通窗口宽度下必然溢出。原来只有 `overflow-x-auto`，
所以想看某一列必须横向滚动 —— 而横向滚动时表头会跟着走，读到"判定"那一列时
已经看不到它上面写的是什么了。

## 修法与这里守的东西

按重要性逐列设断点：窄屏保「用例 + 状态 + 判定 + 操作」（读报告的最小集），
其余列在更宽的断点逐步放出。

★ 这里守的不是"有没有设断点"，而是**表头与表体的断点逐列一致**。
两者错位比看不到更糟：表头写着「优先级」而那一列显示的是状态数据。
而这类错误不会有任何运行时报错，只能靠机械比对发现。
"""

from __future__ import annotations

import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "web" / "src" / "pages" / "RunReport.tsx"


@pytest.fixture(scope="module")
def table() -> tuple[list[str], list[str]]:
    if not SRC.exists():  # pragma: no cover - the web tree is always present here
        pytest.skip(f"找不到 {SRC}")
    src = SRC.read_text(encoding="utf-8")
    thead = re.search(r"<thead.*?</thead>", src, re.S)
    tbody = re.search(r"<tbody>.*?</tbody>", src, re.S)
    assert thead and tbody, "RunReport 里找不到结果表"
    ths = re.findall(r"<th(?:\s[^>]*)?>", thead.group(0))
    first_row = re.search(r"<tr [^>]*>.*?</tr>", tbody.group(0), re.S)
    assert first_row, "结果表里找不到数据行"
    tds = re.findall(r"<td(?:\s[^>]*)?>", first_row.group(0))

    def bp(tag: str) -> str:
        m = re.search(r'className="([^"]*)"', tag)
        cls = m.group(1) if m else ""
        hit = re.search(r"\bhidden\b.*?([a-z]+):table-cell", cls)
        return hit.group(1) if hit else "always"

    return [bp(t) for t in ths], [bp(t) for t in tds]


def test_header_and_body_breakpoints_align(table) -> None:
    """★ 表头与表体每一列的断点必须一致。

    错位的后果不是"少一列"，而是**表头与数据对不上号**（表头写「优先级」、
    列里显示状态）。这类错误没有任何运行时报错，只能机械比对。
    """
    header, body = table
    assert len(header) == len(body), f"列数不一致：表头 {len(header)} vs 表体 {len(body)}"
    mismatch = [(i, h, b) for i, (h, b) in enumerate(zip(header, body)) if h != b]
    assert not mismatch, f"断点错位的列（列号, 表头, 表体）：{mismatch}"


def test_the_essential_columns_are_never_hidden(table) -> None:
    """读报告的最小集必须始终可见：用例、状态、判定、操作。

    这四列被隐藏的话，窄屏用户看到一张什么也判断不了的表格 ——
    比需要横向滚动更糟。其余列（模块/优先级/重试/耗时/Token）都是**补充信息**，
    可以让位。
    """
    header, _body = table
    always = [i for i, b in enumerate(header) if b == "always"]
    # 用例(0) 状态(3) 判定(7) 操作(8) —— 见 RunReport 的列顺序
    assert 0 in always, "「用例」列被隐藏了"
    assert 3 in always, "「状态」列被隐藏了"
    assert len(always) >= 4, f"始终可见的列太少（{always}），窄屏下报告会没法读"


def test_at_least_one_column_actually_responds_to_width(table) -> None:
    """反向守卫：这个测试的存在意义是"表格确实做了响应式处理"。

    如果所有列都是 always，说明有人把断点删了（回到横向滚动的老样子），
    而上面两条断言仍然会通过 —— 那种情况下这条会红。
    """
    header, _body = table
    responsive = [i for i, b in enumerate(header) if b != "always"]
    assert responsive, "没有任何一列设了断点 —— 表格又回到必须横向滚动的状态了"


# ---------------------------------------------------------------------------
# 操作列必须固定住（2026-10-10 用户**两次**反馈"看不到"）
# ---------------------------------------------------------------------------
#
# 第一次反馈：列太多，最右边的「用例/改判/回放」被推出屏幕。
# 我按断点收掉了次要列 —— 用户第二次反馈「依旧看不到」。
#
# 原因是我搞错了问题的形状：无论怎么按断点收列，**只要窗口不够宽，最右一列就会
# 滑出视口**。而这一列是唯一能操作那条结果的地方。所以正确的解法不是"让它更窄"，
# 而是把它钉在右侧（sticky right-0），横向滚动时一直可见。


def test_the_actions_column_is_pinned_to_the_right(table) -> None:
    """★ 操作列（最后一列）必须 sticky right-0，表头与表体都要。

    只钉住其中一个的话：表头固定而数据列滚动（或反之），
    滚动时表头会和数据错位 —— 那比看不到更难用。
    """
    src = SRC.read_text(encoding="utf-8")
    thead = re.search(r"<thead.*?</thead>", src, re.S).group(0)
    tbody = re.search(r"<tbody>.*?</tbody>", src, re.S).group(0)
    first_row = re.search(r"<tr [^>]*>.*?</tr>", tbody, re.S).group(0)

    for label, block in (("表头", thead), ("表体", first_row)):
        cells = re.findall(r"<t[hd](?:\s[^>]*)?>", block)
        last = cells[-1]
        assert "sticky" in last and "right-0" in last, (
            f"{label}的最后一列（操作列）没有 sticky right-0 —— "
            "窗口不够宽时「用例/改判/回放」会被推出视口，用户点不到。\n"
            f"实际：{last[:160]}"
        )


def test_the_pinned_column_has_an_opaque_background() -> None:
    """钉住的列必须有**不透明**背景，否则横向滚动时下面的内容会透过来。

    这类问题在代码里看不出来（类名都"对"），只在滚动时肉眼可见 ——
    所以用 CSS 断言把它钉住。
    """
    css = (SRC.parent.parent / "index.css").read_text(encoding="utf-8")
    assert ".tp-sticky-actions" in css, "缺少 .tp-sticky-actions 的不透明背景规则"
    block = css.split(".tp-sticky-actions", 1)[1][:400]
    assert "background" in block, "sticky 列没有设背景 —— 滚动时会透出下层内容"
    # hover 也要跟着变，否则鼠标划过时这一格颜色不动，看起来像卡住的一块
    assert ".tp-row:hover .tp-sticky-actions" in css, (
        "sticky 列在行 hover 时没有跟随变色 —— 视觉上会像一块卡住的区域"
    )


def test_the_table_has_a_minimum_width_so_scrolling_engages(table) -> None:
    """表格必须有 min-w 下限。

    没有它时表格会一直压缩列宽、靠内容换行消化，最终把判定列拉得很高，
    而最右列还是被挤出去 —— 横向滚动根本没触发。
    """
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"<table className=\"([^\"]*)\"", src)
    assert m, "找不到结果表的 className"
    cls = m.group(1)
    assert "min-w-[" in cls, f"结果表没有 min-w 下限（className={cls!r}），横向滚动不会生效"
