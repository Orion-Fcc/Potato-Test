"""把解析出的纯文本排成**给人读**的 HTML（2026-10-19）。

## 为什么需要它

上传的 PDF / Word / Excel 会被 `docparse.extract` 拍成纯文本：PDF 基本是
"一个视觉行一个换行"，Word 是"一个段落一个空行"，Excel 是"一行一条用 | 隔开"。
原来的写法是 `text.split("\\n")` 后**每行包一个 `<p>`**，于是文档页里出现：

* 一句话被拆成三四段，每段之间空 0.6em，行尾参差不齐；
* `## 第 1 页`、`## 表格 1`、`2.1 组织架构管理` 原样显示成段落；
* `字段 | 含义` 的表格行变成一串普通文字；
* 用户反馈「正文的文字排布有很大的问题」。

知识库仍然用 `docparse` 的纯文本（检索需要连续文字）；这里只负责
**展示用的排版**：按空行分段、段内把被换行切断的句子接回去、识别标题 / 列表 / 表格。

## 为什么不用现成的 Markdown 渲染器

`docparse` 的输出只是"碰巧有点像 Markdown"（`## 第 N 页` 是我们自己加的），
并不是规范 Markdown。交给完整解析器会把正文里的 `*`、`|` 误当成格式。
所以这里只认**明确的**结构：`#` 标题、行首列表符号、连续成行的 `|` 表格，
其余一律当正文 —— 宁可少认，也不要把段落弄乱。
"""

from __future__ import annotations

import re
from html import escape

from app.md_html import join_wrapped_lines, markdown_to_html

_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+)$")
# PDF 的页码标记（`docparse` 自己加的）。它不是章节，排版上要弱于章节标题。
_PAGE_MARK = re.compile(r"^#{1,6}\s*第\s*\d+\s*页(?:（OCR 识别）)?\s*$")
# 中文章节：第 3 章 / 第二节 / 第三部分
_CN_HEAD = re.compile(r"^(第[一二三四五六七八九十百千0-9]+[章节篇部分])\s*(.*)$")
# 数字编号：2.1 组织架构 / 1、概述
_NUM_HEAD = re.compile(r"^(\d+(?:\.\d+){0,5})[、.．]?\s*(\S.*)$")
_BULLET = re.compile(r"^[-*+•·]\s+(.+)$")
# 有序列表。`\d+[.)、]` 后面必须跟空白 —— 否则 "2.1 组织架构" 会被当成
# "2." + "1 组织架构" 这个列表项，而它其实是章节编号。
_ORDERED = re.compile(r"^(?:\d+[.)、](?=\s)|[（(]\d+[）)]|[①-⑳])\s*(.+)$")

# Markdown 表格的分隔行（docparse 的表格没有，但 .md 上传会有）
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

# 按**行**有意义、不能把相邻行接回段落的格式（2026-10-19）。
# CSV/TSV 另有表格解析；这些格式忠实保留换行即可 —— 把日志的两条记录粘成
# 一句话，比原来的"每行一个 <p>"更难读。
_STRUCTURED_FMTS = frozenset({"json", "log", "yaml", "yml", "xml"})

# 标题的结束标点：句子不会以这些结尾，所以它们不是标题。
_SENTENCE_END = ("。", "！", "？", "；", "，", "、", ".", ",", ";", "!", "?")


def _heading_level(line: str) -> int:
    """这一行是标题吗？返回 HTML 标题级别（2~5），不是则 0。

    只认**短**且不以句末标点结尾的行 —— 需求文档里的编号标题通常符合，
    而正文里的"2026 年要实现……"不会。
    """
    s = line.strip()
    if not s or len(s) > 60 or s.endswith(_SENTENCE_END):
        return 0

    # 页码是**阅读定位**，不是章节：排成 h4，别压过真正的章节标题。
    if _PAGE_MARK.match(s):
        return 4

    m = _MD_HEAD.match(s)
    if m:
        # 正文在文档标题（h2）之下，所以 # 也落在 h2，避免和标题抢层级
        return min(5, max(2, len(m.group(1)) + 1))

    if _CN_HEAD.match(s):
        return 3

    m = _NUM_HEAD.match(s)
    if m:
        num, rest = m.group(1), m.group(2)
        # "2026 年……" 这种年份开头的正文不算标题；有小数点的编号（2.1）一定是层级。
        if rest and (("." in num) or len(num) <= 3):
            # 2.1 → h3；2.1.1 这种再深一层 → h4
            return 3 if num.count(".") <= 1 else 4
    return 0


def _next_nonempty_starts_numbered(lines: list[str], i: int) -> bool:
    """下一非空行是不是又一个编号项？

    "1. 安装依赖" 这种单级编号既可能是标题、也可能是有序列表的第一项。
    如果下一行还是编号，按列表处理；否则才是标题 —— 这样 "1. 甲 / 2. 乙"
    不会变成两个 h3。
    """
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j < len(lines) and _ORDERED.match(lines[j].strip()) is not None


def _table_cells(line: str) -> list[str] | None:
    """`a | b | c` → ["a", "b", "c"]；不是表格行则 None。"""
    t = line.strip()
    if "|" not in t:
        return None
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|"):
        t = t[:-1]
    cells = [c.strip() for c in t.split("|")]
    if len(cells) < 2 or not any(cells):
        return None
    return cells


def _render_table(rows: list[list[str]]) -> str:
    head, body = rows[0], rows[1:]
    th = "".join(f"<th>{escape(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _csv_to_doc_html(text: str, delimiter: str) -> str:
    """CSV / TSV → 真正的表格（2026-10-19）。

    它们和 PDF 不一样：换行是**行边界**，不是排版折行。按段落接回会把
    整张表粘成一句话。字段里的逗号/引号交给 `csv` 模块，别自己 split。
    """
    import csv
    import io

    try:
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    except csv.Error:
        return f"<pre>{escape(text)}</pre>"
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    if len(rows) == 1:
        return f"<p>{escape('，'.join(rows[0]))}</p>"
    width = max(len(r) for r in rows)
    return _render_table([r + [""] * (width - len(r)) for r in rows])


def text_to_doc_html(text: str, fmt: str = "") -> str:
    """把 `docparse.extract` 的文本变成可读的 HTML 片段。

    `fmt` 是 `docparse` 给出的格式（"pdf"/"docx"/"xlsx"/"md"/…）。
    真正的 Markdown 交给 `markdown_to_html`；其余走这里的轻量排版。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    # .md 是**规范 Markdown**，交给已经覆盖标题/列表/表格/代码的转换器；
    # 不把 PDF/Word 的文本也送进去 —— 它们只是"像"，不是规范 Markdown。
    if fmt in ("md", "markdown"):
        return markdown_to_html(text)

    # CSV/TSV 的换行是**行边界**，不是排版折行：交给表格解析（2026-10-19）。
    if fmt in ("csv", "tsv"):
        return _csv_to_doc_html(text, "," if fmt == "csv" else "\t")

    # JSON / 日志 / YAML / XML 同样逐行有意义，用 <pre> 忠实保留换行与缩进。
    if fmt in _STRUCTURED_FMTS:
        return f"<pre>{escape(text)}</pre>"

    lines = text.split("\n")
    out: list[str] = []
    para: list[str] = []
    i, n = 0, len(lines)

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(f"<p>{escape(join_wrapped_lines(para))}</p>")
            para = []

    while i < n:
        raw = lines[i]
        s = raw.strip()

        # 空行 = 段落结束
        if not s:
            flush_para()
            i += 1
            continue

        # 表格：连续 2 行以上、每行至少两列。单行里的 `|` 当普通文字。
        # 必须放在标题判断**之前**：Excel 表格的第一列常常是数字（"1 | 甲"），
        # 先判标题会把它当成编号标题。
        first_cells = _table_cells(s)
        if first_cells is not None:
            rows: list[list[str]] = []
            j = i
            while j < n:
                cells = _table_cells(lines[j])
                if cells is None:
                    break
                if _TABLE_SEP.match(lines[j].strip()) and rows:
                    j += 1
                    continue
                rows.append(cells)
                j += 1
            if len(rows) >= 2:
                flush_para()
                out.append(_render_table(rows))
                i = j
                continue

        # 标题（Markdown # / 第 X 章 / 2.1 …）
        # 单行 `1 | 甲` 不是标题：它更像一行表格，但没有第二行可成表，按正文处理。
        lvl = 0 if first_cells is not None else _heading_level(s)
        if lvl:
            # 单级编号（"1. xxx"）且下一行还是编号 → 这是一组有序列表，不是标题。
            m_num = _NUM_HEAD.match(s)
            if m_num and "." not in m_num.group(1) and _next_nonempty_starts_numbered(lines, i):
                lvl = 0
        if lvl:
            flush_para()
            m = _MD_HEAD.match(s)
            content = m.group(2).strip() if m else s
            out.append(f"<h{lvl}>{escape(content)}</h{lvl}>")
            i += 1
            continue

        # 列表：同一类型的连续行合成一个 ul / ol，缩进的续行接回上一条。
        m_ul = _BULLET.match(s)
        m_ol = _ORDERED.match(s)
        if m_ul or m_ol:
            flush_para()
            ordered = bool(m_ol)
            items: list[str] = []
            while i < n:
                cur = lines[i].strip()
                m = (_ORDERED if ordered else _BULLET).match(cur)
                if m:
                    items.append(m.group(1).strip())
                    i += 1
                elif items and lines[i].strip() and lines[i].startswith(("  ", "\t")):
                    items[-1] = join_wrapped_lines([items[-1], cur])
                    i += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            lis = "".join(f"<li>{escape(x)}</li>" for x in items)
            out.append(f"<{tag}>{lis}</{tag}>")
            continue

        para.append(s)
        i += 1

    flush_para()
    return "\n".join(out)
