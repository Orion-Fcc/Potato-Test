"""解析文本 → 文档正文 HTML 的排版（2026-10-19）。

用户反馈「项目文档 → 正文」的文字排布有问题。根因是上传时每行都包一个 `<p>`：
PDF 的折行被当成段落、Markdown 标题和表格标记原样显示。这组测试守三件事：

1. PRD/PDF 里被视觉换行切断的句子要**接回去**，不能一行一段；
2. `## 第 N 页` / `2.1 组织架构` 要变成标题，不能显示成正文；
3. `a | b` 的连续行要变成真正的表格。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.doc_text_html import text_to_doc_html  # noqa: E402


def test_pdf_line_wraps_are_rejoined_into_paragraphs():
    text = (
        "## 第 1 页\n"
        "2.1 组织架构管理\n"
        "组织架构采用公司、部门、岗位三级结构。每个部门必须归属于一个公司，\n"
        "每个岗位必须归属于一个部门。部门负责人可以管理本部门及下级部门的\n"
        "人员与资源，但不能跨部门操作。\n"
    )
    html = text_to_doc_html(text, "pdf")

    # 标题不再带 `##` / 编号也不再是普通段落；页码要弱于章节标题
    assert "<h4>第 1 页</h4>" in html
    assert "<h3>2.1 组织架构管理</h3>" in html
    # 三行正文合成一段，且中文折行处没有被塞进空格
    assert html.count("<p>") == 1
    assert "公司，每个岗位" in html
    assert "部门的\n人员" not in html
    assert "##" not in html


def test_pipe_rows_become_a_real_table():
    text = "## 工作表：Sheet1\n字段 | 含义\ndept | 部门\nrole | 角色\n"
    html = text_to_doc_html(text, "xlsx")

    assert "<h3>工作表：Sheet1</h3>" in html
    assert "<table>" in html
    assert "<th>字段</th><th>含义</th>" in html
    assert "<td>dept</td><td>部门</td>" in html
    # 表格行不该再作为普通段落出现
    assert "<p>字段 | 含义</p>" not in html


def test_plain_text_lists_are_rendered_as_lists():
    text = "注意事项：\n- 部门名称不能重复\n- 删除前必须转移人员\n"
    html = text_to_doc_html(text, "txt")

    assert "<p>注意事项：</p>" in html
    assert "<ul><li>部门名称不能重复</li><li>删除前必须转移人员</li></ul>" in html


def test_numeric_first_column_is_a_table_not_a_heading():
    """Excel 表格第一列是编号（"1 | 甲"）时，不能被当成编号标题。"""
    text = "序号 | 名称\n1 | 甲\n2 | 乙\n"
    html = text_to_doc_html(text, "xlsx")

    assert "<h" not in html
    assert "<th>序号</th><th>名称</th>" in html
    assert "<td>1</td><td>甲</td>" in html


def test_single_level_numbers_become_a_list_not_headings():
    """`1. 甲 / 2. 乙` 是列表，不能变成两个 h3；`1. 概述` 后面跟正文才是标题。"""
    text = "步骤：\n1. 打开页面\n2. 输入账号\n3. 点击登录\n\n1. 概述\n这里是正文。\n"
    html = text_to_doc_html(text, "pdf")

    assert "<ol><li>打开页面</li><li>输入账号</li><li>点击登录</li></ol>" in html
    assert "<h3>1. 概述</h3>" in html


def test_markdown_format_still_uses_the_markdown_converter():
    text = "# 需求\n\n正文第一行，\n正文第二行。\n\n- 甲\n- 乙\n"
    html = text_to_doc_html(text, "md")

    assert "<h1>需求</h1>" in html
    # 中文折行接回去时不该多出空格
    assert "<p>正文第一行，正文第二行。</p>" in html
    assert "<ul><li>甲</li><li>乙</li></ul>" in html


def test_english_line_wrap_keeps_a_space():
    text = "This sentence was wrapped\nacross two lines."
    html = text_to_doc_html(text, "pdf")

    assert "<p>This sentence was wrapped across two lines.</p>" in html


def test_csv_rows_become_a_table_not_one_paragraph():
    """CSV 的换行是行边界，不能像 PDF 折行那样接回一段（2026-10-19）。"""
    text = 'name,role\n"Alice, A.",admin\nBob,user\n'
    html = text_to_doc_html(text, "csv")

    assert "<table>" in html
    assert "<th>name</th><th>role</th>" in html
    # 带逗号的字段由 csv 模块解析，不会被拆成额外一列
    assert "<td>Alice, A.</td><td>admin</td>" in html
    assert "<p>" not in html


def test_tsv_uses_tabs_as_separators():
    text = "字段\t含义\ndept\t部门\n"
    html = text_to_doc_html(text, "tsv")

    assert "<th>字段</th><th>含义</th>" in html
    assert "<td>dept</td><td>部门</td>" in html


def test_structured_text_keeps_its_line_breaks():
    """日志/JSON 这类逐行文本不能被折行拼接逻辑粘成一段。"""
    log = "2026-10-19 10:00 INFO start\n2026-10-19 10:01 WARN slow\n"
    html = text_to_doc_html(log, "log")
    assert html.startswith("<pre>") and html.endswith("</pre>")
    assert "INFO start\n2026-10-19 10:01 WARN slow" in html

    js = '{\n  "a": 1,\n  "b": 2\n}\n'
    html = text_to_doc_html(js, "json")
    assert html.startswith("<pre>")
    assert "\n  &quot;a&quot;: 1" in html or '\n  "a": 1' in html
