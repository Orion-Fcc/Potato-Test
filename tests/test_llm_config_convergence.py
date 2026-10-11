"""LLM 配置只剩一个真相来源（2026-10-11）。

用户的提问是：「LLM 模型很多功能是不是重复了，包括有一个基层模型了，还有 LLM 池，
还有什么那个是不是冲突了？」

答案是：池（横向换 key）和用途（纵向换模型）是正交的、都要留；但**「基层模型」那张卡
确实是死的** —— `load_pool()` 在池非空时会整体取代它，于是：

  · 保存基层卡对运行零影响（改了模型发现没变，且不报错）；
  · `GET /admin/settings` 的「正在使用」面板读的就是它 → 显示一个没在跑的模型名；
  · `POST /admin/settings/llm/test` 测的也是它 → "测试通过"和"实际 401"可以同时成立。

这个文件钉住修复后的契约：

  1. `active_profile()` = 池的第 1 条（真正在跑的那套），不再是 env/旧单值字段；
  2. 单值字段（base_url/model/agent_model/provider/api_key）改为**合并进池第 1 条**，
     而不是写进那些永远不生效的 AppSetting；
  3. 池为空时 `GET .../llm/pool` 回一条 .env 的**回显**（seeded=True），
     不是空列表 —— 空列表会让界面显示"没配置"，而实际上有一套在跑；
  4. 池的来源（pool / env）如实回给界面；
  5. 拼不出完整条目时**拒绝写入** —— 半截条目会被 load_pool 忽略并回落 env，
     那正好又变回"保存了没生效"。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

import pytest


@pytest.fixture()
def client():
    """TestClient + 一个只属于本测试的临时库（写法照 tests/test_api_crud.py）。"""
    tmp = os.path.join(tempfile.gettempdir(), f"_tp_llm_cfg_{uuid.uuid4().hex[:8]}.db")
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

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

    from app import llm as llm_mod
    from app.config import get_settings
    from app.main import app

    # 管理接口在 auth_enabled=False 时不做鉴权；测试里显式钉住，别依赖本机 .env。
    prev_auth = get_settings().auth_enabled
    get_settings().auth_enabled = False
    llm_mod.invalidate_llm_cache()

    with TestClient(app) as c:
        yield c

    get_settings().auth_enabled = prev_auth
    llm_mod.invalidate_llm_cache()
    asyncio.run(eng.dispose())
    dbmod._engine = prev_engine
    dbmod._Session = prev_session
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def test_active_profile_follows_the_pool_not_the_dead_fields(client, monkeypatch) -> None:
    """★ 核心修正：`active_profile()` 必须等于池的第 1 条。

    以前它返回 env/旧单值字段，而池非空时那些字段根本不参与运行 ——
    于是所有"没传用途"的调用点（助手、飞书、能力探测）都在描述另一套配置。
    这里刻意把 env 设成一个不同的值，好区分"读的是 env 还是池"。
    """
    from app import llm as llm_mod
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "gateway_model", "env-model", raising=False)
    monkeypatch.setattr(get_settings(), "gateway_base_url", "https://env.example/v1", raising=False)

    client.put(
        "/api/admin/settings/llm",
        json={
            "pool": [
                {
                    "name": "real",
                    "base_url": "https://real.example/v1",
                    "model": "real-model",
                    "api_key": "k",
                }
            ]
        },
    )

    async def _run():
        return await llm_mod.active_profile(), await llm_mod.pool_origin()

    prof, origin = asyncio.run(_run())
    assert origin == "pool"
    assert prof.base_url == "https://real.example/v1", "还在读 env —— 池非空时 env 不生效"
    assert prof.model == "real-model"


def test_single_value_fields_are_merged_into_the_pool_head(client) -> None:
    """单值字段（老 UI 的「基层模型」）必须落到池第 1 条，而不是写进死字段。"""
    r = client.put(
        "/api/admin/settings/llm",
        json={
            "base_url": "https://gw.example/v1",
            "model": "cheap-model",
            "api_key": "sk-1",
            "provider": "openai",
        },
    )
    assert r.status_code == 200, r.text

    s = client.get("/api/admin/settings").json()["llm"]
    assert s["pool_source"] == "pool", "写了单值字段之后仍回落 env = 保存了没生效"
    assert s["model"] == "cheap-model"
    assert s["base_url"] == "https://gw.example/v1"

    pool = client.get("/api/admin/settings/llm/pool").json()
    assert pool["count"] == 1
    assert pool["source"] == "pool"
    assert pool["seeded"] is False


def test_merging_only_model_keeps_the_key_and_the_rest_of_the_pool(client) -> None:
    """只改模型不该丢掉钥匙，也不该动到做 429 兜底的其余条目。"""
    client.put(
        "/api/admin/settings/llm",
        json={
            "pool": [
                {"name": "a", "base_url": "https://a.example/v1", "model": "m-a", "api_key": "k-a"},
                {"name": "b", "base_url": "https://b.example/v1", "model": "m-b", "api_key": "k-b"},
            ]
        },
    )
    r = client.put("/api/admin/settings/llm", json={"model": "m-a2"})
    assert r.status_code == 200, r.text

    pool = client.get("/api/admin/settings/llm/pool").json()
    assert pool["count"] == 2, "改默认模型不该把兜底的那几套弄丢"
    assert [p["name"] for p in pool["pool"]] == ["a", "b"], "顺序即优先级，不能变"
    assert pool["pool"][0]["model"] == "m-a2"
    # 钥匙留空 = 保留已存的那把（界面只显示掩码，不可能让人重输）
    assert pool["pool"][0]["api_key_set"] is True
    assert pool["pool"][1]["model"] == "m-b"


def test_model_only_is_completed_with_the_env_gateway(client, monkeypatch) -> None:
    """只填一个模型名时，用 env 里的网关/钥匙把它补成一条完整条目。

    这是刻意的：用户的意图是"默认模型换成这个"，而平台已经从 .env 知道网关和钥匙。
    补全之后它就成为池的第 1 条 —— 设什么跑什么。反过来若拒绝写入，
    用户就又会遇到"我明明保存了却没生效"。
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "gateway_base_url", "https://env.example/v1", raising=False)
    monkeypatch.setattr(get_settings(), "gateway_api_key", "env-key", raising=False)

    r = client.put("/api/admin/settings/llm", json={"model": "only-a-model"})
    assert r.status_code == 200, r.text

    s = client.get("/api/admin/settings").json()["llm"]
    assert s["pool_source"] == "pool"
    assert s["model"] == "only-a-model"
    assert s["base_url"] == "https://env.example/v1", "网关该从 env 继承，不该丢"
    assert s["api_key_set"] is True, "钥匙该从 env 继承，否则这条根本跑不起来"


def test_incomplete_config_is_refused_instead_of_being_written_half(client, monkeypatch) -> None:
    """补不出完整条目（连 env 都没有网关/钥匙）时什么都不写。

    半截条目会被 `load_pool` 静默忽略、然后回落 env —— 那正好又变回"保存了没生效"。
    宁可什么都不做，让"池仍是空的"这个事实如实留在界面上（`pool_source == "env"`）。
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "gateway_base_url", "", raising=False)
    monkeypatch.setattr(get_settings(), "gateway_api_key", "", raising=False)

    r = client.put("/api/admin/settings/llm", json={"model": "only-a-model"})
    assert r.status_code == 200, r.text

    s = client.get("/api/admin/settings").json()["llm"]
    assert s["pool_source"] == "env", "缺 base_url/key 时不该凭空造出一个池条目"


def test_empty_pool_is_reported_as_env_with_one_readback_row(client) -> None:
    """池为空时回一条 .env 的回显 + seeded=True。

    回空列表会让界面显示"什么都没配"，而平台上明明有一套在跑 ——
    "显示为空"和"有一套在跑"是最容易把人带偏的那种不一致。
    """
    pool = client.get("/api/admin/settings/llm/pool").json()
    assert pool["source"] == "env"
    assert pool["seeded"] is True
    assert pool["count"] == 1, "至少要能看到当前实际在用的那一套"

    s = client.get("/api/admin/settings").json()["llm"]
    assert s["pool_source"] == "env"
    assert s["pool_count"] == 1


def test_per_purpose_models_still_win_over_the_pool_head(client) -> None:
    """用途覆盖的语义不能被这次收敛改掉：留空继承池第 1 条，填了以它为准。"""
    client.put(
        "/api/admin/settings/llm",
        json={
            "base_url": "https://gw.example/v1",
            "model": "base-model",
            "api_key": "sk-1",
        },
    )
    client.put("/api/admin/settings/llm", json={"roles": {"judge": {"model": "judge-model"}}})

    roles = client.get("/api/admin/settings").json()["llm"]["roles"]
    assert roles["judge"]["model"] == "judge-model"
    assert roles["judge"]["effective_model"] == "judge-model"
    # 没配的用途仍然继承池第 1 条 —— 这也是"默认"的唯一含义
    assert roles["agent"]["model"] == ""
    assert roles["agent"]["effective_model"] == "base-model"


def test_test_endpoint_uses_the_agent_role_set(client, monkeypatch) -> None:
    """连通性测试必须测 agent 真正用的那一套。

    以前它测 `llm_config()` + `active_profile()`（env/旧字段），配了池之后
    "测试通过"和"实际 401"可以同时成立，而用户会以为是平台坏了。
    """
    from app import llm as llm_mod

    seen: list[str] = []

    async def _fake_caps(profile, force=False):  # noqa: ANN001, ARG001
        seen.append(f"{profile.name}|{profile.base_url}|{profile.model}")
        raise RuntimeError("stop here — 我们只关心它测的是哪一套")

    monkeypatch.setattr(llm_mod, "capabilities_for", _fake_caps, raising=True)

    client.put(
        "/api/admin/settings/llm",
        json={"pool": [{"name": "real", "base_url": "https://real.example/v1",
                        "model": "real-model", "api_key": "k"}]},
    )
    r = client.post("/api/admin/settings/llm/test")
    assert r.status_code == 200
    assert seen == ["real|https://real.example/v1|real-model"], (
        f"测的不是池里那一套：{seen}"
    )
