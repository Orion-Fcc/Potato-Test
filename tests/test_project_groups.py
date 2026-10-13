"""「大系统」容器（2026-10-13）。

用户原话：「我的系统是一个很大的系统，我现在上传的都是小项目……你可以建一个大系统的
分类模式，里面放我这些小模块吗」。这组测试守两件事：

1. **归类**：项目能挂到某个大系统下、能移出、删大系统不删项目；
2. ★ **共享需规**：大系统里那份资料，**它的子项目都检索得到**（传一次、全模块可用），
   而且**不会串到别的大系统**去。
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

    dbname = f"_tp_group_{uuid.uuid4().hex[:8]}.db"
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


def _group(client, name: str) -> int:
    r = client.post("/api/project-groups", json={"name": name})
    assert r.status_code == 200, r.text
    return int(r.json()["id"])


def _assign(client, pid: int, gid: int | None) -> None:
    r = client.put(f"/api/projects/{pid}", json={"group_id": gid})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 归类
# ---------------------------------------------------------------------------
def test_create_and_list_groups(client):
    gid = _group(client, "民航培训管理系统")
    rows = client.get("/api/project-groups").json()
    assert any(g["id"] == gid and g["name"] == "民航培训管理系统" for g in rows)
    assert next(g for g in rows if g["id"] == gid)["projects"] == 0


def test_project_is_assigned_and_unassigned(client):
    gid = _group(client, "大系统甲")
    pid = _project(client, "小模块A")
    # 建完之后它是平的
    assert client.get(f"/api/projects/{pid}").json()["group_id"] is None
    # 挂上去
    _assign(client, pid, gid)
    assert client.get(f"/api/projects/{pid}").json()["group_id"] == gid
    assert next(
        g for g in client.get("/api/project-groups").json() if g["id"] == gid
    )["projects"] == 1
    # 显式传 null = 移出（这就是 ProjectPatch 要按 model_fields_set 判断的原因）
    _assign(client, pid, None)
    assert client.get(f"/api/projects/{pid}").json()["group_id"] is None


def test_deleting_a_group_keeps_its_projects(client):
    """删大系统**不能连带删项目** —— 项目里是用例库，是真正的资产。"""
    gid = _group(client, "待删大系统")
    pid = _project(client, "要保住的小模块")
    _assign(client, pid, gid)

    assert client.delete(f"/api/project-groups/{gid}").status_code == 200
    # 项目还在，只是退回平铺
    assert pid in [p["id"] for p in client.get("/api/projects").json()]
    assert client.get(f"/api/projects/{pid}").json()["group_id"] is None


# ---------------------------------------------------------------------------
# ★ 共享需规
# ---------------------------------------------------------------------------
def test_group_knowledge_is_searchable_by_its_sub_projects(client):
    """大系统里传一次需规，挂在上面的每个小项目都检索得到。"""
    from app import knowledge

    gid = _group(client, "民航培训管理系统")
    pid = _project(client, "培训资源管理")
    _assign(client, pid, gid)

    client.put(
        f"/api/project-groups/{gid}/knowledge",
        json={"text": "# 系统级规则\n\n同一资源范围不能配两条审批线。"},
    )
    # 项目自己一个字都没传 —— 但它搜得到大系统那份
    got = asyncio.run(knowledge.search(pid, "审批线"))
    assert got["hits"], "子项目检索不到大系统的共享需规"
    assert "不能配两条审批线" in got["hits"][0]["text"]


def test_group_knowledge_does_not_leak_to_other_groups(client):
    """共享需规只对**本大系统**的子项目可见 —— 别串到别的系统去。"""
    from app import knowledge

    gid_a = _group(client, "大系统A")
    gid_b = _group(client, "大系统B")
    pid_a = _project(client, "A 的模块")
    pid_b = _project(client, "B 的模块")
    _assign(client, pid_a, gid_a)
    _assign(client, pid_b, gid_b)

    client.put(f"/api/project-groups/{gid_a}/knowledge", json={"text": "# A 独有规则\n\n只有甲方才有的校验。"})

    assert asyncio.run(knowledge.search(pid_a, "甲方"))["hits"], "本系统项目应当搜得到"
    assert not asyncio.run(knowledge.search(pid_b, "甲方"))["hits"], "串到别的大系统了"


def test_replacing_group_knowledge_does_not_touch_project_own_text(client):
    """换大系统的共享需规**不能**动到项目自己传的资料 —— 两份是分开存的。"""
    from app import knowledge

    gid = _group(client, "大系统")
    pid = _project(client, "小模块")
    _assign(client, pid, gid)
    client.put(f"/api/projects/{pid}/knowledge", json={"text": "# 项目自己的资料\n\n这个模块独有的东西。"})

    client.put(f"/api/project-groups/{gid}/knowledge", json={"text": "# 大系统共享\n\n系统级的东西。"})

    own = asyncio.run(knowledge.search(pid, "这个模块独有"))
    shared = asyncio.run(knowledge.search(pid, "系统级"))
    assert own["hits"], "项目自己的资料被共享那份挤掉了"
    assert shared["hits"]
