"""上传的**原件**（2026-10-16）。

用户原话：「我想全部看到原件，ai建议让他也看原件」。

起因是他传了一份 PDF 到「项目文档」，看到的却是纯文本（项目符号还变成了 □）。
查下来根因有两层：解析出的正文本来就是**有损**的（PDF 丢版面、docx 丢标题层级），
而**原件之前根本没留** —— 连"看回原版"的退路都没有。

这组测试守三件事：
1. 上传之后原件**真的存下来了**，而且服务端能读回来（助手就是靠这条）；
2. 助手用 `_read_doc_source` 读到的是**原件**的原文；
3. 没有原件的文档要**明说没有**，绝不拿解析出来的正文冒充。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient + 专属临时库 + **专属 artifact 目录**。

    artifact 目录必须隔离：原件是往磁盘写的，跑测试不该往项目的 ./artifacts 里丢文件。
    """
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("S3_ENDPOINT", "")  # 强制走本地盘，别让测试依赖对象存储
    from app.config import get_settings

    get_settings.cache_clear()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    dbname = f"_tp_docsrc_{uuid.uuid4().hex[:8]}.db"
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

    async def _create() -> None:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    from app.main import app

    with TestClient(app) as c:
        yield c

    asyncio.run(eng.dispose())
    dbmod._engine, dbmod._Session = prev_engine, prev_session
    get_settings.cache_clear()
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def _pid(client, name: str = "原件测试项目") -> int:
    return int(client.post("/api/projects", json={"name": name}).json()["id"])


def _upload(client, pid: int, filename: str, data: bytes, ctype: str = "text/plain"):
    return client.post(
        f"/api/projects/{pid}/docs/from-file",
        files={"file": (filename, data, ctype)},
        data={"ai_editable": "1"},
    )


# ---------------------------------------------------------------------------
# 1) 人：原件要留下来
# ---------------------------------------------------------------------------
def test_upload_keeps_the_original_and_reports_it(client):
    """上传后接口要把原件的信息回传出来 —— 界面就靠它显示「原件」入口。"""
    pid = _pid(client)
    body = "# 计财收费标准配置\n\n2.8.3.17.7 D-删除\n\n行为描述：参与者点击删除。\n"
    r = _upload(client, pid, "计费配置.md", body.encode("utf-8"))
    assert r.status_code == 200, r.text
    doc = r.json()["doc"]

    assert doc["source_name"] == "计费配置.md"
    assert doc["source_bytes"] == len(body.encode("utf-8"))
    # 本地模式是 /artifacts/... —— 浏览器能直接打开的地址
    assert doc["source_url"], "原件存下来了却没给出可打开的地址"


def test_original_bytes_survive_the_roundtrip(client):
    """★ 关键：存进去的字节要能**原样读回来**（助手读原件全靠这条）。"""
    from app import storage
    from app.config import get_settings
    from app.db import db_session
    from app.models import ProjectDoc
    from sqlalchemy import select

    pid = _pid(client)
    payload = "行一\n行二\n| 字段 | 含义 |\n| --- | --- |\n| a | 甲 |\n".encode("utf-8")
    doc = _upload(client, pid, "对照表.txt", payload).json()["doc"]

    async def _key() -> str:
        async with db_session() as s:
            row = (
                await s.execute(select(ProjectDoc).where(ProjectDoc.id == doc["id"]))
            ).scalars().one()
            return row.source_key or ""

    key = asyncio.run(_key())
    assert key, "库里没记下 source_key —— 服务端就再也读不回原件了"
    assert asyncio.run(storage.read_bytes(key)) == payload
    assert get_settings().artifact_dir  # 隔离用的目录确实生效了


# ---------------------------------------------------------------------------
# 2) 助手：读得到原件
# ---------------------------------------------------------------------------
def test_assistant_can_read_the_original(client):
    """助手读原件：返回**原文**，并且点明"这不是文档页里那份可能被改过的正文"。"""
    from app import assistant

    pid = _pid(client)
    body = "# 需求\n\n| 角色 | 权限 |\n| --- | --- |\n| 管理员 | 全量 |\n"
    doc = _upload(client, pid, "角色表.md", body.encode("utf-8")).json()["doc"]

    out = asyncio.run(assistant._read_doc_source(pid, {"doc_id": doc["id"]}))
    assert out.get("has_original") is True, out
    assert "管理员" in out.get("source_text", ""), out
    assert out.get("original_name") == "角色表.md"
    assert "原件" in out.get("note", "")


def test_reading_a_doc_without_an_original_says_so(client):
    """没有原件就**明说没有** —— 拿正文冒充会让模型以为自己在读原文。"""
    from app import assistant
    from app.db import db_session
    from app.models import ProjectDoc

    pid = _pid(client)

    async def _make() -> int:
        async with db_session() as s:
            row = ProjectDoc(project_id=pid, kind="custom", title="手写的文档", html="<p>正文</p>")
            s.add(row)
            await s.flush()
            return row.id

    did = asyncio.run(_make())
    out = asyncio.run(assistant._read_doc_source(pid, {"doc_id": did}))
    assert out.get("has_original") is False, out
    assert "没有存原件" in out.get("note", "")


# ---------------------------------------------------------------------------
# 3) 人：上传来的文档要能**手工改**
# ---------------------------------------------------------------------------
def test_uploaded_doc_can_be_edited_by_hand(client):
    """上传来的文档必须能被人改（2026-10-16 用户要求「人工都要可以手动修改」）。

    守的重点不是"接口返回 200"，而是**改到的是同一份**：同一份资料重复上传会出现
    多条 kind=spec_source，写入如果按 kind 找第一行，就会改串到别的资料上去 ——
    那种错用户很难发现（两份都还在，只是内容换了位）。
    """
    pid = _pid(client)
    a = _upload(client, pid, "甲资料.md", "# 甲\n\n甲的一行。\n".encode("utf-8")).json()["doc"]
    b = _upload(client, pid, "乙资料.md", "# 乙\n\n乙的一行。\n".encode("utf-8")).json()["doc"]
    assert a["id"] != b["id"]

    r = client.put(
        f"/api/projects/{pid}/docs",
        json={
            "id": b["id"],
            "kind": "spec_source",
            "title": "乙资料.md",
            "html": "<h2>乙资料.md</h2><p>我手工加的批注。</p>",
        },
    )
    assert r.status_code == 200, r.text

    docs = {d["id"]: d for d in client.get(f"/api/projects/{pid}/docs").json()["docs"]}
    assert "我手工加的批注" in docs[b["id"]]["html"], "改的不是点的那一份"
    assert "我手工加的批注" not in docs[a["id"]]["html"], "改串到另一份资料上去了"
    assert "甲的一行" in docs[a["id"]]["html"], "甲那份不该被动过"
    # 改完标记成 human —— 界面据此区分"AI 生成未过目"和"人改过"
    assert docs[b["id"]]["origin"] == "human"


def test_uploaded_markdown_is_rendered_as_rich_text(client):
    """上传的 .md 不能把 `#` / `-` / `|` 原样显示成正文（2026-10-19）。

    用户反馈「正文的文字排布有很大的问题」：原来解析出的文本是
    `text.split("\\n")` 每行一个 `<p>`，Markdown 标题、列表、表格全变成
    普通段落。这里守住"结构化内容真的被渲染成 HTML"。
    """
    pid = _pid(client)
    body = (
        "# 需求说明\n\n"
        "## 2.1 组织架构\n\n"
        "- 部门名称不能重复\n"
        "- 删除前必须转移人员\n\n"
        "| 字段 | 含义 |\n"
        "| --- | --- |\n"
        "| dept | 部门 |\n"
    )
    doc = _upload(client, pid, "需求.md", body.encode("utf-8")).json()["doc"]
    html = doc["html"]

    assert "<h2>需求说明</h2>" in html or "<h1>需求说明</h1>" in html
    assert "<h2>2.1 组织架构</h2>" in html
    assert "<ul>" in html and "<li>部门名称不能重复</li>" in html
    assert "<table>" in html and "<th>字段</th>" in html
    # 标记本身不该再出现在正文里
    assert "<p>#" not in html
    assert "<p>- " not in html
    assert "<p>|" not in html


def _set_doc_html(doc_id: int, html: str, parse_hash: str | None = None) -> None:
    """直接改库里的正文，模拟"旧版本排版写进去的文档"。"""
    from sqlalchemy import select

    from app.db import db_session
    from app.models import ProjectDoc

    async def _do() -> None:
        async with db_session() as s:
            row = (
                await s.execute(select(ProjectDoc).where(ProjectDoc.id == doc_id))
            ).scalars().one()
            row.html = html
            if parse_hash is not None:
                row.html_parse_hash = parse_hash

    asyncio.run(_do())


def test_reparse_old_layout_uses_the_new_typesetting(client):
    """已经上传过的旧文档可以按原件重排，不必重新找文件（2026-10-19）。"""
    from hashlib import sha256

    pid = _pid(client)
    body = "## 第 1 页\n2.1 组织架构\n组织架构采用三级结构，\n每个部门必须归属一个公司。\n"
    doc = _upload(client, pid, "需规.txt", body.encode("utf-8")).json()["doc"]

    # 旧版本写进库里的正文是"每行一个 <p>"。把指纹也改成它 —— 表示"没人改过"。
    old = (
        "<p>## 第 1 页</p><p>2.1 组织架构</p>"
        "<p>组织架构采用三级结构，</p><p>每个部门必须归属一个公司。</p>"
    )
    _set_doc_html(doc["id"], old, sha256(old.encode("utf-8")).hexdigest()[:32])

    r = client.post(f"/api/projects/{pid}/docs/{doc['id']}/reparse", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True

    html = out["doc"]["html"]
    assert "<h4>第 1 页</h4>" in html
    assert "<h3>2.1 组织架构</h3>" in html
    assert "##" not in html
    # 两行被折行的正文接回了一段
    assert html.count("<p>") == 1
    assert "三级结构，每个部门" in html


def test_reparse_refuses_to_overwrite_hand_edits_until_forced(client):
    """重新排版不能静默覆盖人工修改；界面确认后带 force=true 才覆盖。"""
    pid = _pid(client)
    doc = _upload(client, pid, "需规.txt", b"# 1\n\noriginal text\n").json()["doc"]

    _set_doc_html(doc["id"], "<p>我手工改的正文</p>")

    r = client.post(f"/api/projects/{pid}/docs/{doc['id']}/reparse", json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "edited"

    r2 = client.post(
        f"/api/projects/{pid}/docs/{doc['id']}/reparse", json={"force": True}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    html = r2.json()["doc"]["html"]
    assert "我手工改的正文" not in html
    assert "original text" in html


def test_editing_an_uploaded_doc_keeps_its_original(client):
    """手工改正文**不能**把原件弄丢 —— 原件是"原文到底怎么写的"的权威。"""
    pid = _pid(client)
    doc = _upload(client, pid, "规范.md", "# 规范\n\n原文。\n".encode("utf-8")).json()["doc"]
    client.put(
        f"/api/projects/{pid}/docs",
        json={"id": doc["id"], "kind": "spec_source", "title": "规范.md", "html": "<p>改过</p>"},
    )
    after = {d["id"]: d for d in client.get(f"/api/projects/{pid}/docs").json()["docs"]}[doc["id"]]
    assert after["source_url"], "改一次正文就把原件信息丢了"
    assert after["source_name"] == "规范.md"


def test_read_doc_source_needs_an_identifier(client):
    """不给 doc_id/title 要直接告诉模型怎么拿 —— 而不是抛异常。"""
    from app import assistant

    out = asyncio.run(assistant._read_doc_source(_pid(client), {}))
    assert "error" in out
    assert "read_project_docs" in str(out)


def test_reupload_does_not_clobber_hand_edits(client):
    """重传同一个文件**不能**把人手工改过的正文覆盖掉（2026-10-16）。

    用户可能已经把那份有损正文补全、加了批注；把文件重传一遍就抹掉等于白干，
    而且界面上不会有任何提示 —— 这是"人工可以手动修改"这条要求下面最容易被忽略的坑。
    """
    pid = _pid(client)
    fname = "规范.md"
    doc = _upload(client, pid, fname, "# 规范\n\n原文。\n".encode("utf-8")).json()["doc"]

    # 人手工补了一行
    client.put(
        f"/api/projects/{pid}/docs",
        json={
            "id": doc["id"],
            "kind": "spec_source",
            "title": fname,
            "html": "<h2>规范.md</h2><p>原文。</p><p>我补的一行。</p>",
        },
    )

    r = _upload(client, pid, fname, "# 规范\n\n原文。\n".encode("utf-8"))
    assert r.status_code == 200, r.text
    assert any("手工改过" in w for w in r.json()["warnings"]), "覆盖了人工修改却没告诉用户"
    after = {d["id"]: d for d in client.get(f"/api/projects/{pid}/docs").json()["docs"]}[doc["id"]]
    assert "我补的一行" in after["html"], "人工修改被静默覆盖了"


def test_reupload_still_replaces_when_nobody_edited(client):
    """没人改过的文档仍然被新解析结果替换 —— 这是重传的本意，别一起改掉。"""
    pid = _pid(client)
    fname = "规范.md"
    doc = _upload(client, pid, fname, "# 旧\n\n旧原文。\n".encode("utf-8")).json()["doc"]
    r = _upload(client, pid, fname, "# 新\n\n新原文。\n".encode("utf-8"))
    assert r.status_code == 200, r.text
    assert not r.json()["warnings"], r.json()["warnings"]
    after = {d["id"]: d for d in client.get(f"/api/projects/{pid}/docs").json()["docs"]}[doc["id"]]
    assert "新原文" in after["html"]
    assert "旧原文" not in after["html"]

