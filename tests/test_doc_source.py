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


def test_read_doc_source_needs_an_identifier(client):
    """不给 doc_id/title 要直接告诉模型怎么拿 —— 而不是抛异常。"""
    from app import assistant

    out = asyncio.run(assistant._read_doc_source(_pid(client), {}))
    assert "error" in out
    assert "read_project_docs" in str(out)
