"""协同工作区流式接口的契约测试（2026-10-20）。

钉住三件事：
1. 助手流把模型的增量文本原样转成 `token` 事件，最后发 `done`；
2. 流程图流在最后只给出通过校验的 Mermaid 源码；
3. 没有需规内容时发中文 `error`，而不是让前端一直转圈。

不调真实模型：`chat_stream_with_pool` 由测试替换。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client():
    """TestClient + 只属于本测试的临时库（隔离理由见 docs/PROJECT_ISOLATION.md）。"""
    import asyncio
    import os
    import tempfile
    import uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod
    from app.models import Base

    tmp = os.path.join(tempfile.gettempdir(), f"_tp_ws_stream_{uuid.uuid4().hex[:8]}.db")
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
    from fastapi.testclient import TestClient

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


def _fake_stream(monkeypatch, chunks: list[str], seen: dict | None = None):
    from app import llm_chat

    async def _gen(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        for text in chunks:
            yield llm_chat.ChatStreamEvent(text=text)
        yield llm_chat.ChatStreamEvent(done=True, model="fake-model")

    monkeypatch.setattr(llm_chat, "chat_stream_with_pool", _gen)


def _stub_context(monkeypatch, headings: list[str], intro: str = "示例系统背景"):
    from app import spec_overview, specgen

    async def _headings(_pid: int) -> list[str]:
        return headings

    async def _intro(_pid: int, _chars: int = 2000) -> str:
        return intro

    async def _recall(_pid: int, _key: str, **_kw) -> str:
        return "用户提交申请后由主管审批。"

    monkeypatch.setattr(specgen, "list_headings", _headings)
    monkeypatch.setattr(specgen, "document_intro", _intro)
    monkeypatch.setattr(spec_overview, "_recall_text", _recall)


def test_assistant_stream_emits_tokens_and_done(client, monkeypatch):
    _stub_context(monkeypatch, ["申请", "审批"])
    seen: dict = {}
    _fake_stream(monkeypatch, ["审批流程", "包括提交与通过。"], seen)

    with client.stream(
        "POST",
        "/api/projects/1/assistant/stream",
        json={
            "message": "帮我写审批流程",
            "doc": {"kind": "overview", "title": "项目总览"},
            "content": "<p>旧内容</p>",
            "selection": "选中一句话",
        },
    ) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())

    assert "event: token" in body
    assert "审批流程" in body
    assert "event: done" in body
    user_msg = seen["messages"][-1]["content"]
    assert "审批流程" in user_msg
    assert "选中一句话" in user_msg
    assert seen["role"] == "assistant"


def test_flow_stream_returns_validated_mermaid(client, monkeypatch):
    _stub_context(monkeypatch, ["申请", "审批", "结束"])
    _fake_stream(
        monkeypatch,
        [
            "```mermaid\n",
            "flowchart TD\n",
            '  A["提交申请"] --> B{"是否通过"}\n',
            '  B -->|"通过"| C["结束"]\n',
            "```\n",
        ],
    )

    with client.stream(
        "POST", "/api/projects/1/docs/flow/stream", json={"instruction": "画主流程"}
    ) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())

    assert "event: done" in body
    assert "flowchart TD" in body
    assert "```" not in body.split("event: done")[-1]


def test_flow_stream_without_headings_reports_chinese_error(client, monkeypatch):
    _stub_context(monkeypatch, [])
    _fake_stream(monkeypatch, ["不该被调用"])

    with client.stream("POST", "/api/projects/1/docs/flow/stream", json={}) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())

    assert "event: error" in body
    assert "还没有可用的需规内容" in body
    assert "event: done" not in body
