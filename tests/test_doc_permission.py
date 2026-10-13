"""「只有我能改」+「上传成文档」的端到端测试（2026-10-12）。

## 守的是什么

用户两句话：
  1. 「ai 可以阅读那里面所有文档，但是有些文档 ai 无法修改，
      我上传文档的时候可以加个选择，也就是只有我自己能修改」
  2. 「上传的需归文档也无法查看修改」

第 2 句查出来的真相：上传原来只进**知识库**（AI 检索用的分块），
文档页里根本没有对应条目 —— 所以看不见、也改不了。这里测的是新加的
`/docs/from-file` 让上传**同时**变成一份可读可改的文档。

## 为什么权限要测三层

`ai_editable` 有三个入口，漏一个就等于没拦：
  * 助手工具 `write_project_doc`（真正的门）；
  * `PATCH /docs/{id}/permission`（用户设置）；
  * `PUT /docs`（保存正文，不能顺手把权限改回去）。
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
def client():
    """TestClient + 本测试专属临时库（理由见 docs/PROJECT_ISOLATION.md 第七节）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    dbname = f"_tp_perm_{uuid.uuid4().hex[:8]}.db"
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
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def _project(client, name: str) -> int:
    return int(client.post("/api/projects", json={"name": name}).json()["id"])


# ---------------------------------------------------------------------------
# 上传成文档
# ---------------------------------------------------------------------------
class TestUploadBecomesDocument:
    def test_upload_creates_a_viewable_editable_document(self, client):
        """★ 用户反馈「上传的需归文档无法查看修改」—— 这条就是那个修复。

        上传必须**同时**产出一份文档：只在知识库里，用户在文档页就看不见它。
        必须断言**正文内容**：只断言 created=True 会漏掉
        "创建了一个空文档"这种坏法 —— 而那恰好就是用户报的症状。
        """
        pid = _project(client, "upload-doc")
        r = client.post(
            f"/api/projects/{pid}/docs/from-file",
            files={"file": ("需规.txt", "# 规则\n\n同一范围不能配两条。".encode(), "text/plain")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] is True
        assert body["doc"]["kind"] == "spec_source"
        assert body["doc"]["title"] == "需规.txt"
        assert "同一范围不能配两条" in body["doc"]["html"], "建了一份空文档"

        # 文档列表里能查到（这就是"查看"）
        docs = client.get(f"/api/projects/{pid}/docs").json()["docs"]
        got = next(d for d in docs if d["kind"] == "spec_source")
        assert "同一范围不能配两条" in got["html"]

    def test_the_uploader_chooses_whether_the_assistant_may_edit(self, client):
        """★ 上传处那个「允许助手修改」勾选决定权限（2026-10-12 用户要求）。

        用户原话：「上传的文件我可以选择 ai 是否可以改」。
        所以上传**必须**能选，而且这个选择要真的落到 `ai_editable` 上。

        默认勾上（可改）：项目文档的主要用法是"助手起稿、人改"，
        默认锁上会让人以为功能坏了。上传**外部原件**（客户给的说明书、
        别人写的需规）的人会主动取消勾选 —— 那是别人给的东西，改了就对不上。
        """
        pid = _project(client, "upload-choice")

        # ① 勾上（默认）→ 助手可改
        ok = client.post(
            f"/api/projects/{pid}/docs/from-file",
            files={"file": ("可以改.txt", "内容甲".encode(), "text/plain")},
            data={"ai_editable": "1"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["doc"]["ai_editable"] is True

        # ② 取消勾选 → 只有人能改
        locked = client.post(
            f"/api/projects/{pid}/docs/from-file",
            files={"file": ("锁住.txt", "内容乙".encode(), "text/plain")},
            data={"ai_editable": "0"},
        )
        assert locked.status_code == 200, locked.text
        assert locked.json()["doc"]["ai_editable"] is False
        assert locked.json()["doc"]["id"] != ok.json()["doc"]["id"], "两份文件应当是两条文档"

    def test_the_lock_actually_stops_the_assistant(self, client):
        """选了"只有我能改"就必须真的拦得住 —— 否则那个勾选只是装饰。"""
        pid = _project(client, "upload-locked-really")
        r = client.post(
            f"/api/projects/{pid}/docs/from-file",
            files={"file": ("外部资料.txt", "原件内容XYZ".encode(), "text/plain")},
            data={"ai_editable": "0"},
        )
        assert r.json()["doc"]["ai_editable"] is False

        from app.assistant import _read_project_docs, _write_project_doc

        # 能读（用户要求「ai 可以阅读那里面所有文档」）
        rd = asyncio.run(_read_project_docs(pid, {"kind": "spec_source"}))
        assert "原件内容XYZ" in rd.get("readable", ""), f"助手读不到：{rd}"
        # 不能写
        wr = asyncio.run(
            _write_project_doc(pid, {"kind": "spec_source", "markdown": "助手篡改的内容"})
        )
        assert "error" in wr, f"助手改写了锁住的原件：{wr}"
        after = next(
            d
            for d in client.get(f"/api/projects/{pid}/docs").json()["docs"]
            if d["kind"] == "spec_source"
        )
        assert "助手篡改的内容" not in after["html"]

    def test_reuploading_the_same_file_updates_instead_of_duplicating(self, client):
        """重传同一个文件应当**替换**，不是越追加越长。

        用户重传通常正是因为上次解析得不理想 —— 追加会让正文里出现两份矛盾的原文。
        """
        pid = _project(client, "upload-again")
        for text in ("第一版内容", "第二版内容"):
            client.post(
                f"/api/projects/{pid}/docs/from-file",
                files={"file": ("规格.txt", text.encode(), "text/plain")},
            )
        docs = [
            d
            for d in client.get(f"/api/projects/{pid}/docs").json()["docs"]
            if d["kind"] == "spec_source"
        ]
        assert len(docs) == 1, f"重传产生了 {len(docs)} 份文档"
        assert "第二版内容" in docs[0]["html"]
        assert "第一版内容" not in docs[0]["html"]

    def test_different_files_become_separate_documents(self, client):
        """不同文件各自成篇 —— 否则第二份上传会覆盖第一份。"""
        pid = _project(client, "upload-two")
        for nm, txt in (("甲.txt", "甲的正文"), ("乙.txt", "乙的正文")):
            client.post(
                f"/api/projects/{pid}/docs/from-file",
                files={"file": (nm, txt.encode(), "text/plain")},
            )
        docs = [
            d
            for d in client.get(f"/api/projects/{pid}/docs").json()["docs"]
            if d["kind"] == "spec_source"
        ]
        assert len(docs) == 2, f"两份文件变成了 {len(docs)} 份文档"
        joined = " ".join(d["html"] for d in docs)
        assert "甲的正文" in joined and "乙的正文" in joined

    def test_unsupported_format_is_refused_with_a_reason(self, client):
        pid = _project(client, "upload-bad")
        r = client.post(
            f"/api/projects/{pid}/docs/from-file",
            files={"file": ("老件.doc", b"\xd0\xcf\x11\xe0 legacy", "application/msword")},
        )
        assert r.status_code == 400
        assert "docx" in r.text


# ---------------------------------------------------------------------------
# 只有我能改
# ---------------------------------------------------------------------------
class TestOnlyICanEdit:
    def test_the_assistant_write_is_blocked(self, client):
        """★ 这是真正的门。

        助手是通过**工具**写文档的，不经过界面 —— 只在界面上拦等于没拦。
        """
        pid = _project(client, "lock-assistant")
        client.put(
            f"/api/projects/{pid}/docs",
            json={"kind": "overview", "title": "项目总览", "html": "<p>人的内容</p>", "ai_editable": False},
        )
        from app.assistant import _write_project_doc

        r = asyncio.run(
            _write_project_doc(pid, {"kind": "overview", "markdown": "AI 想写的东西"})
        )
        assert "error" in r, f"助手写进去了：{r}"
        assert r.get("blocked_by") == "ai_editable=false"

        # 正文必须**一点没变**
        ov = next(
            d
            for d in client.get(f"/api/projects/{pid}/docs").json()["docs"]
            if d["kind"] == "overview"
        )
        assert "人的内容" in ov["html"]
        assert "AI 想写的东西" not in ov["html"]

    def test_the_assistant_can_still_read_a_locked_document(self, client):
        """★ 「ai 可以阅读那里面所有文档」—— 读不受限制。

        这条很重要：锁的是**写**。如果连读都拦掉，"有些文档 AI 不能改"
        就会变成"AI 对这些文档一无所知"，而用户要的是前者。
        """
        pid = _project(client, "lock-read")
        client.put(
            f"/api/projects/{pid}/docs",
            json={
                "kind": "overview",
                "title": "项目总览",
                "html": "<p>锁住的内容ABC</p>",
                "ai_editable": False,
            },
        )
        from app.assistant import _read_project_docs

        r = asyncio.run(_read_project_docs(pid, {"kind": "overview"}))
        assert "锁住的内容ABC" in r.get("readable", ""), f"助手读不到锁住的文档：{r}"

    def test_unlocking_lets_the_assistant_write_again(self, client):
        pid = _project(client, "unlock")
        r = client.put(
            f"/api/projects/{pid}/docs",
            json={"kind": "overview", "title": "项目总览", "html": "<p>甲</p>", "ai_editable": False},
        )
        doc_id = r.json()["id"]

        pr = client.patch(
            f"/api/projects/{pid}/docs/{doc_id}/permission", json={"ai_editable": True}
        )
        assert pr.status_code == 200, pr.text
        assert pr.json()["ai_editable"] is True

        from app.assistant import _write_project_doc

        w = asyncio.run(_write_project_doc(pid, {"kind": "overview", "markdown": "AI 现在能写了"}))
        assert w.get("ok") is True, w

    def test_saving_the_body_does_not_silently_reset_the_lock(self, client):
        """★ 保存正文不能顺手把权限改回可改。

        界面每次保存都 PUT 一次 `/docs`；如果那里无脑把 ai_editable 设成默认值，
        用户设好的锁会被他自己的一次保存悄悄打开 —— 而他完全看不出来。
        """
        pid = _project(client, "lock-survives-save")
        r = client.put(
            f"/api/projects/{pid}/docs",
            json={"kind": "overview", "title": "项目总览", "html": "<p>甲</p>", "ai_editable": False},
        )
        doc_id = r.json()["id"]
        # 不带 ai_editable 的保存（界面上的普通「保存」就是这样）
        r2 = client.put(
            f"/api/projects/{pid}/docs",
            json={"id": doc_id, "kind": "overview", "title": "项目总览", "html": "<p>乙</p>"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["ai_editable"] is False, "保存把锁悄悄打开了"

    def test_a_new_document_is_editable_by_default(self, client):
        """默认必须可改：项目文档的主要用法就是"助手起稿、人改"。

        默认锁上会让升级后的助手突然"什么都不能改"，而用户没做任何操作。
        """
        pid = _project(client, "default-editable")
        r = client.put(
            f"/api/projects/{pid}/docs",
            json={"kind": "custom", "title": "新文档", "html": "<p>x</p>"},
        )
        assert r.json()["ai_editable"] is True

    def test_placeholders_report_editable_not_missing(self, client):
        """占位文档（还没建）要报 ai_editable=True。

        报 None/undefined 的话前端会把它显示成"被锁住"——
        用户看到新建的总览是锁的，会以为权限功能坏了。
        """
        pid = _project(client, "placeholder")
        docs = client.get(f"/api/projects/{pid}/docs").json()["docs"]
        for d in docs:
            assert d["ai_editable"] is True, f"{d['kind']} 的占位不是可改：{d}"


def test_missing_document_permission_returns_404(client):
    pid = _project(client, "perm-404")
    assert (
        client.patch(f"/api/projects/{pid}/docs/999999/permission", json={"ai_editable": False}).status_code
        == 404
    )
