"""助手读写项目文档 + 对话附件（2026-10-12）。

## 这组测试守的是什么

用户提了三件事：
  1. 「助手有阅读并分析项目总览那里所有功能的能力」
  2. 「给一个本地文件夹 / 文件地址 / 文件给他阅读」
  3. 「和其他 agent 一样有粘贴文件在对话框的能力」

第 2 件本来就有（`import_file_to_knowledge` / `list_files`），所以这里测的是新增的
第 1、3 件，以及它们共用的两块地基：

* **Markdown → HTML**：助手写文档的自然输出是 Markdown，而项目文档存 HTML。
  转换错了内容会变形甚至消失，而用户只会看到"助手写的东西不对"。
* **HTML → 可读文本**：助手要"阅读"项目文档。这里最容易踩的坑是**图表源码**——
  普通去标签会把 Mermaid 的换行吃掉，模型就认不出那是一张图。

第 3 件（附件）的关键契约是**失败要说清楚**：格式不支持必须报错，
不能回一段空文本 —— 那会变成"上传成功但没内容"这种最难查的故障。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.doc_tools import (  # noqa: E402
    clamp,
    default_title,
    guessed_kind,
    html_to_readable,
    summarize_doc_row,
)
from app.md_html import markdown_to_html  # noqa: E402


@pytest.fixture()
def client():
    """TestClient + 只属于本测试的临时库（理由见 docs/PROJECT_ISOLATION.md 第七节）。"""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    dbname = f"_tp_docassist_{uuid.uuid4().hex[:8]}.db"
    tmp = os.path.join(tempfile.gettempdir(), dbname)
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    prev_engine, prev_session = dbmod._engine, dbmod._Session
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    from app.main import app

    assert dbname in str(eng.url).replace("%5F", "_"), f"没连到临时库：{eng.url}"
    with TestClient(app) as c:
        yield c

    asyncio.run(eng.dispose())
    dbmod._engine, dbmod._Session = prev_engine, prev_session
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Markdown → HTML
# ---------------------------------------------------------------------------
class TestMarkdownToHtml:
    def test_headings_and_inline(self):
        got = markdown_to_html("# 标题\n\n这是**粗**和*斜*和`码`。")
        assert "<h1>标题</h1>" in got
        assert "<strong>粗</strong>" in got
        assert "<em>斜</em>" in got
        assert "<code>码</code>" in got

    def test_lists(self):
        assert "<ul><li>甲</li><li>乙</li></ul>" in markdown_to_html("- 甲\n- 乙")
        assert "<ol><li>一</li><li>二</li></ol>" in markdown_to_html("1. 一\n2. 二")

    def test_table(self):
        got = markdown_to_html("| 名称 | 值 |\n|---|---|\n| a | 1 |")
        assert "<th>名称</th>" in got
        assert "<td>a</td>" in got

    def test_fenced_code_becomes_pre(self):
        got = markdown_to_html("```\nflowchart TD\n  A --> B\n```")
        assert "<pre>" in got
        assert "flowchart TD" in got

    def test_html_in_the_source_is_escaped(self):
        """★ 助手写的东西里可能有 `<`，不能让它变成标签。

        这是一个**注入面**：模型输出会经过这里再入库，若原样放行，
        一次输出就能往页面里塞标签。转义之后由 doc_sanitize 再把关。
        """
        got = markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in got
        assert "&lt;script&gt;" in got

    def test_plain_less_than_is_escaped(self):
        assert "&lt;" in markdown_to_html("5 < 6")

    def test_blockquote_and_hr(self):
        got = markdown_to_html("> 引用\n\n---")
        assert "<blockquote>" in got and "<hr>" in got

    def test_paragraphs_split_on_blank_lines(self):
        got = markdown_to_html("第一段\n\n第二段")
        assert got.count("<p>") == 2

    def test_output_survives_the_sanitizer(self):
        """★ 转换结果要能过净化 —— 过不去的话用户拿到的是被啃掉一半的文档。"""
        from app.doc_sanitize import sanitize

        md = "# 标题\n\n正文**加粗**。\n\n- 甲\n- 乙\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> 引用\n"
        html = sanitize(markdown_to_html(md))
        for want in ("标题", "正文", "加粗", "甲", "乙", "引用"):
            assert want in html, f"净化后丢了：{want}"

    def test_empty(self):
        assert markdown_to_html("") == ""


# ---------------------------------------------------------------------------
# HTML → 可读文本
# ---------------------------------------------------------------------------
class TestHtmlToReadable:
    def test_headings_become_markdown_ish(self):
        assert "## 小节" in html_to_readable("<h2>小节</h2><p>正文</p>")

    def test_list_items(self):
        got = html_to_readable("<ul><li>甲</li><li>乙</li></ul>")
        assert "- 甲" in got and "- 乙" in got

    def test_table_rows_keep_cells_apart(self):
        got = html_to_readable("<table><tr><td>a</td><td>b</td></tr></table>")
        assert "a" in got and "b" in got
        assert "|" in got

    def test_mermaid_source_is_preserved_verbatim(self):
        """★ 这是这一块最容易踩的坑。

        普通"去标签"会把图表源码的**换行**吃掉（`flowchart TD A --> B`），
        模型于是认不出那是一张图的定义，也就谈不上理解或修改它。
        """
        html = '<p>说明</p><pre class="tp-mermaid">flowchart TD\n  A["甲"] --> B["乙"]</pre>'
        got = html_to_readable(html)
        assert "[流程图源码]" in got
        assert "flowchart TD\n" in got, f"换行被吃掉了：{got!r}"
        assert 'A["甲"] --> B["乙"]' in got

    def test_entities_are_decoded(self):
        assert "a < b & c" in html_to_readable("<p>a &lt; b &amp; c</p>")

    def test_empty(self):
        assert html_to_readable("") == ""


class TestSummarize:
    def test_detects_a_diagram(self):
        row = summarize_doc_row("flow", "流程图", '<pre class="tp-mermaid">flowchart TD\nA-->B</pre>')
        assert row["has_diagram"] is True
        assert row["empty"] is False

    def test_detects_an_empty_doc(self):
        assert summarize_doc_row("overview", "项目总览", "")["empty"] is True


class TestHelpers:
    def test_clamp_discloses_truncation(self):
        t, cut = clamp("x" * 100, 10)
        assert len(t) == 10 and cut is True
        t2, cut2 = clamp("short", 100)
        assert t2 == "short" and cut2 is False

    def test_guessed_kind_is_conservative(self):
        assert guessed_kind("往流程图里加") == "flow"
        assert guessed_kind("项目总览加一段") == "overview"
        assert guessed_kind("需求分析补一节") == "analysis"
        # 猜不出就返回空，让调用方决定 —— 猜错会把内容放错文档，用户很难发现
        assert guessed_kind("随便写点什么") == ""

    def test_default_titles(self):
        assert default_title("overview") == "项目总览"
        assert default_title("flow") == "流程图"


# ---------------------------------------------------------------------------
# 端到端：助手写进项目文档
# ---------------------------------------------------------------------------
def test_writing_appends_by_default(client):
    pid = client.post("/api/projects", json={"name": "assist-write"}).json()["id"]
    # 先手写一份，模拟"人已经改过"
    client.put(
        f"/api/projects/{pid}/docs",
        json={"kind": "overview", "title": "项目总览", "html": "<p>人写的内容</p>"},
    )
    # 再让助手写（走真实工具分发）
    import asyncio

    from app.assistant import _write_project_doc

    r = asyncio.run(
        _write_project_doc(pid, {"kind": "overview", "markdown": "## 助手补充\n\n这一段是助手写的。"})
    )
    assert r.get("ok") is True, r
    assert r["mode"] == "append"

    docs = client.get(f"/api/projects/{pid}/docs").json()["docs"]
    ov = next(d for d in docs if d["kind"] == "overview")
    assert "人写的内容" in ov["html"], "追加把原有内容覆盖了"
    assert "助手补充" in ov["html"]
    assert ov["origin"] == "ai", "助手写的应当标 ai（还没被人看过）"


def test_writing_with_replace_overwrites(client):
    pid = client.post("/api/projects", json={"name": "assist-replace"}).json()["id"]
    client.put(
        f"/api/projects/{pid}/docs",
        json={"kind": "overview", "title": "项目总览", "html": "<p>旧内容</p>"},
    )
    import asyncio

    from app.assistant import _write_project_doc

    r = asyncio.run(
        _write_project_doc(
            pid, {"kind": "overview", "markdown": "新内容", "mode": "replace"}
        )
    )
    assert r.get("ok") is True, r
    ov = next(
        d for d in client.get(f"/api/projects/{pid}/docs").json()["docs"] if d["kind"] == "overview"
    )
    assert "新内容" in ov["html"]
    assert "旧内容" not in ov["html"]


def test_writing_rejects_unknown_kind(client):
    pid = client.post("/api/projects", json={"name": "assist-badkind"}).json()["id"]
    import asyncio

    from app.assistant import _write_project_doc

    r = asyncio.run(_write_project_doc(pid, {"kind": "nope", "markdown": "x"}))
    assert "error" in r


def test_writing_rejects_empty_markdown(client):
    pid = client.post("/api/projects", json={"name": "assist-empty"}).json()["id"]
    import asyncio

    from app.assistant import _write_project_doc

    assert "error" in asyncio.run(_write_project_doc(pid, {"kind": "overview", "markdown": "  "}))


def test_reading_a_missing_document_says_so(client):
    """没有文档时要如实说，并给出下一步 —— 不要回一个空字符串让模型自己猜。"""
    pid = client.post("/api/projects", json={"name": "assist-nodoc"}).json()["id"]
    import asyncio

    from app.assistant import _read_project_docs

    r = asyncio.run(_read_project_docs(pid, {}))
    assert r["count"] == 0
    assert "项目文档" in r["note"]


# ---------------------------------------------------------------------------
# 对话附件
# ---------------------------------------------------------------------------
def test_parsing_an_attachment_returns_text(client):
    pid = client.post("/api/projects", json={"name": "att"}).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/assistant/parse",
        files={"file": ("需求.md", "# 规则\n\n不能配两条。".encode(), "text/markdown")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "需求.md", "文件名要回填 —— 对话里得能引用是哪一份"
    assert "不能配两条" in body["text"]
    assert body["truncated"] is False


def test_parsing_rejects_unsupported_format_with_a_reason(client):
    """★ 老格式要**明确报错并说怎么改**。

    回一段空文本的后果是"上传成功但没内容"——用户完全不知道该做什么，
    而这种故障最难查。
    """
    pid = client.post("/api/projects", json={"name": "att-bad"}).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/assistant/parse",
        files={"file": ("老件.doc", b"\xd0\xcf\x11\xe0 legacy", "application/msword")},
    )
    assert r.status_code == 400, r.text
    assert "docx" in r.text, f"报错里要给出补救办法：{r.text}"


def test_attachments_reach_the_model_message():
    """★ 附件正文必须真的进到发给模型的消息里。

    只把附件存在请求体里、没并进消息，是这类功能最常见的"看起来接好了"的坏法 ——
    界面上附件 chip 在，模型却完全不知道有这个东西。

    这里直接测拼装函数（而不是 mock HTTP 客户端）：早先我试过 monkeypatch 一个
    不存在的 `_complete`，测试"通过"了但**什么都没验证** ——
    mock 的名字一旦写错，测试就变成空的。测纯函数不会有这个问题。
    """
    from app.assistant import _compose_user_message

    body = _compose_user_message(
        "看附件",
        [{"filename": "需求.md", "text": "附件里的独特内容XYZ", "chars": 10}],
    )
    assert "附件里的独特内容XYZ" in body, "附件内容没有进消息"
    assert "需求.md" in body, "附件文件名没有进消息"
    assert "看附件" in body, "用户的话被丢了"


def test_truncated_attachments_are_disclosed():
    """截断了必须说明 —— 否则模型以为读全了，然后对没读到的部分照样下结论。"""
    from app.assistant import _compose_user_message

    body = _compose_user_message("x", [{"filename": "大.md", "text": "开头", "truncated": True}])
    assert "只附了开头部分" in body


def test_many_attachments_are_capped_and_disclosed():
    """一次挂十几个附件会把上下文挤爆；要限量并说明还剩几个。"""
    from app.assistant import _compose_user_message

    atts = [{"filename": f"f{i}.md", "text": f"内容{i}"} for i in range(9)]
    body = _compose_user_message("看", atts)
    assert "内容0" in body and "内容4" in body
    assert "内容8" not in body
    assert "另有 4 个附件未附上" in body


def test_empty_attachments_leave_the_message_alone():
    from app.assistant import _compose_user_message

    assert _compose_user_message("只有话", []) == "只有话"
    assert _compose_user_message("只有话", None) == "只有话"
    # 解析失败的附件（text 为空）不该在消息里留一个空壳标题
    assert _compose_user_message("只有话", [{"filename": "空的.md", "text": ""}]) == "只有话"
