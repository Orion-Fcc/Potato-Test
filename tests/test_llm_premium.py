"""平台级「高端模型」（贵、聪明，**可选**）——2026-10-11 设计，2026-10-12 改为「从模型池选一套」。

用户的原话是：「我的建议是可以自己选择是否加高端 LLM（贵和聪明的模型）」。
落地方案是一个平台级槽位：**开不开**一支高端模型，开了再勾它管哪几个环节。
不开就全部回落到便宜的池第 1 套（一分钱不多花）。

2026-10-12 改：高端模型**不再自己存连接信息**（网关/钥匙/协议），而是从「模型池」里
选一套（`pool_name` 指向池里某个条目的 name）。模型的所有连接信息只在模型池里填 ——
这样「哪套模型」只有一处真相，避免 premium 和池各写一份、改了池却不知道 premium 还在用旧的。

这个文件钉住的契约，是这一层「说了就一定算」的四条：

  1. 关着的时候行为**逐字节不变**（默认关 = 省钱优先，与两级判定开关同一条原则）；
  2. 勾了的用途确实换到高端模型（连接信息取自模型池里那套），没勾的用途一个字都不动；
  3. 「按用途分模型」那张**更具体**的卡盖过这一层 —— 并且界面能说出生效值来自哪一层
     （`role_source`），否则用户会觉得高端模型「配了没生效」；
  4. 配置不完整（没选池里的某套 / 一个用途都没勾 / 开关关着）时**老实回落**，
     不能「看起来配了其实没用」。

另外单独钉一条容易漏的：高端模型挂到 `agent` 上时必须清掉 `agent_model` ——
否则 executor 会继续用池里那个旧模型名，表现为「我明明把执行步换成高端模型了却没变」。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

import pytest

# 便宜的那套（池第 1 套，默认全平台都在用）。
POOL_A = {
    "name": "cheap",
    "base_url": "https://cheap.example/v1",
    "model": "cheap-model",
    "api_key": "k-cheap",
    "provider": "openai",
}

# 贵的、聪明的那套 —— 高端模型从池里选的，就是它（连接信息只在这里填一次）。
POOL_B = {
    "name": "smart",
    "base_url": "https://smart.example/v1",
    "model": "smart-model",
    "api_key": "k-smart",
    "provider": "openai",
}


@pytest.fixture()
def client():
    """TestClient + 一个只属于本测试的临时库（写法照 tests/test_api_crud.py）。"""
    tmp = os.path.join(tempfile.gettempdir(), f"_tp_premium_{uuid.uuid4().hex[:8]}.db")
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


def _set_pool(client, *entries: dict) -> None:
    r = client.put("/api/admin/settings/llm", json={"pool": list(entries)})
    assert r.status_code == 200, r.text


def _set_premium(client, **kw) -> None:
    r = client.put("/api/admin/settings/llm", json={"premium": kw})
    assert r.status_code == 200, r.text


def _prof(role: str):
    from app import llm as llm_mod

    return asyncio.run(llm_mod.role_profile(role))


def _source(role: str) -> str:
    from app import llm as llm_mod

    return asyncio.run(llm_mod.role_source(role))


def _key(prof) -> tuple:
    return (prof.base_url, prof.model, prof.api_key)


def _premium_ui(client) -> dict:
    return client.get("/api/admin/settings").json()["llm"]["premium"]


# ── 1. 关着的时候一个字都不变 ───────────────────────────────────────────────


def test_default_is_off_and_changes_nothing(client) -> None:
    """默认必须关，且关着时每个用途都还是池第 1 套。

    这是「省钱优先」的底座：装上就该是便宜的那一套，高端模型要人**主动**开。
    """
    _set_pool(client, POOL_A)

    ui = _premium_ui(client)
    assert ui["enabled"] is False
    assert ui["usable"] is False
    assert ui["roles"] == []

    for role in ("agent", "judge", "judge_escalate", "specgen", "narrative", "assistant"):
        assert _prof(role).model == "cheap-model", f"{role} 在没开高端模型时被换掉了"
        assert _source(role) == "pool"


# ── 2. 勾了的用途确实换过去，没勾的不动 ──────────────────────────────────────


def test_selected_purpose_switches_to_the_premium_model(client) -> None:
    """高端模型 = 模型池里的某套（smart）。勾了的用途换过去，连接信息取自那套。"""
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["judge_escalate", "specgen"])

    esc = _prof("judge_escalate")
    assert _key(esc) == (POOL_B["base_url"], POOL_B["model"], POOL_B["api_key"])
    assert _source("judge_escalate") == "premium"

    assert _prof("specgen").model == "smart-model"

    # 没勾的用途必须原样 —— 「开了高端模型」不该顺手把所有环节都换贵。
    for role in ("agent", "judge", "narrative", "assistant"):
        assert _key(_prof(role)) == (POOL_A["base_url"], POOL_A["model"], POOL_A["api_key"])
        assert _source(role) == "pool"


def test_two_tier_judging_becomes_meaningful_when_only_the_reviewer_is_premium(client) -> None:
    """两级判定的复核层被高端模型接管后，两层不再是同一个模型。

    这一条是「能不能真省到钱」的关键：judge() 在两层解析成同一个
    (base_url, model, api_key) 时会**跳过复核**（同一个人再看一遍不算复核）。
    所以高端模型挂到 judge_escalate 上必须真的改变两层的指纹 —— 而连接信息来自
    模型池里那套（smart）。
    """
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["judge_escalate"])

    first, review = _prof("judge"), _prof("judge_escalate")
    assert _key(first) != _key(review), "两层仍是同一个模型 ⇒ 复核会被静默跳过"
    assert first.model == "cheap-model"
    assert review.model == "smart-model"
    assert review.base_url == POOL_B["base_url"], "高端模型的连接信息应来自模型池里的 smart 那套"


# ── 3. 与「按用途分模型」的优先级 ────────────────────────────────────────────


def test_per_purpose_override_beats_the_premium_slot(client) -> None:
    """更具体的那张卡赢 —— 但生效来源必须能说清楚，否则像是「高端模型没生效」。"""
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["judge", "specgen"])
    client.put(
        "/api/admin/settings/llm",
        json={"roles": {"judge": {"model": "handpicked-judge"}}},
    )

    assert _prof("judge").model == "handpicked-judge"
    assert _source("judge") == "override"
    # 同一个高端模型层里没被单独指名的用途，仍然老老实实走高端模型。
    assert _prof("specgen").model == "smart-model"
    assert _source("specgen") == "premium"


def test_roles_for_ui_reports_where_each_effective_model_comes_from(client) -> None:
    """界面靠 source 说清「我开的高端模型到底管到了哪几个用途」。"""
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["specgen"])
    client.put("/api/admin/settings/llm", json={"roles": {"judge": {"model": "hand-made"}}})

    roles = client.get("/api/admin/settings").json()["llm"]["roles"]
    assert roles["specgen"]["source"] == "premium"
    assert roles["specgen"]["effective_model"] == "smart-model"
    assert roles["judge"]["source"] == "override"
    assert roles["judge"]["effective_model"] == "hand-made"
    assert roles["agent"]["source"] == "pool"
    assert roles["agent"]["effective_model"] == "cheap-model"


# ── 4. 配置不完整时必须老实回落 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "premium, why",
    [
        ({"enabled": True, "roles": ["judge"]}, "开了却没从池里选哪套"),
        ({"enabled": True, "pool_name": "smart"}, "选了池但一个用途都没勾"),
        ({"enabled": False, "pool_name": "smart", "roles": ["judge"]}, "开关是关的"),
    ],
)
def test_incomplete_premium_is_not_applied(client, premium: dict, why: str) -> None:
    """三种「看起来配了其实没用」的情形都必须回落，并且如实告诉界面。"""
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, **premium)

    assert _prof("judge").model == "cheap-model", f"不该生效：{why}"
    assert _source("judge") == "pool"

    ui = _premium_ui(client)
    assert ui["usable"] is False, f"界面必须看得出没生效：{why}"
    assert ui["applies_to"] == []


def test_premium_on_agent_clears_the_stale_agent_model(client) -> None:
    """高端模型挂到 agent 上时，必须清掉池里那个旧 agent_model。

    不清的话 executor 会继续用旧模型名（`agent_model or model`），
    表现为「我明明把执行步换成高端模型了，实际一步都没变」。
    """
    _set_pool(client, {**POOL_A, "agent_model": "cheap-agent-model"}, POOL_B)
    assert _prof("agent").effective_model == "cheap-agent-model"  # 改之前的基线

    _set_premium(client, enabled=True, pool_name="smart", roles=["agent"])

    agent = _prof("agent")
    assert agent.model == "smart-model"
    assert agent.agent_model == "", "旧 agent_model 没清掉 ⇒ 换了模型也不生效"
    assert agent.effective_model == "smart-model"

    from app import llm as llm_mod

    # 429 兜底序列也要以高端模型打头，否则第一步就会退回便宜模型。
    assert asyncio.run(llm_mod.pool_for_role("agent"))[0].model == "smart-model"


# ── 5. 持久化细节 ────────────────────────────────────────────────────────────


def test_re_save_keeps_the_pool_selection(client) -> None:
    """高端模型只存 pool_name + 用途名单：再存一次时用途勾选变了、但选中的那套没变
    （界面始终带完整对象），选中的那套不应被抹掉。
    """
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["judge"])

    # 再存一次，用途勾选变了、模型池选择保持不变（这是界面「保存」实际发送的内容）
    _set_premium(client, enabled=True, pool_name="smart", roles=["judge", "specgen"])

    from app import llm as llm_mod

    prem = asyncio.run(llm_mod.load_premium())
    assert prem.pool_name == "smart", "不带 pool_name 的保存不该把它抹掉"
    assert prem.roles == ("judge", "specgen")

    ui = _premium_ui(client)
    assert ui["pool_name"] == "smart"
    assert ui["resolved_model"] == "smart-model"


def test_unknown_purpose_names_are_dropped(client) -> None:
    """拼错的用途名不能被存下来 —— 否则界面会显示它「已生效」而实际从不生效。"""
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["judge_escalate", "judge_escalte"])

    from app import llm as llm_mod

    assert asyncio.run(llm_mod.load_premium()).roles == ("judge_escalate",)


def test_purpose_order_is_normalised_not_taken_from_the_request(client) -> None:
    """勾选顺序不该泄漏进存储内容（否则两条等价配置的 diff 看起来不一样）。"""
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["narrative", "judge_escalate"])

    from app import llm as llm_mod

    assert asyncio.run(llm_mod.load_premium()).roles == ("judge_escalate", "narrative")


def test_turning_premium_off_takes_effect_at_once_and_keeps_the_selection(client) -> None:
    """关掉 ≠ 删配置：立刻失效，但选中的那套（pool_name）+ 用途名单刻意留着。

    为什么留：界面只显示「选了哪套」，用户**没法把选择再输一遍**——如果「关掉」顺手把
    pool_name 清了，下次重开就是「没选模型」，而且看不出原因。

    这条也钉住「关」和「没配过」的区别：`enabled=False` 且用途清空 ⇒ `usable=False`，
    所以关掉之后必须立刻回落池第 1 套。
    """
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["judge"])
    assert _prof("judge").model == "smart-model"  # 先确认它是真生效的

    # 关掉时仍把 pool_name + 用途带回去（模拟界面「只拨开关」）
    r = client.put(
        "/api/admin/settings/llm",
        json={"premium": {"enabled": False, "pool_name": "smart", "roles": ["judge"]}},
    )
    assert r.status_code == 200, r.text

    from app import llm as llm_mod

    prem = asyncio.run(llm_mod.load_premium())
    assert prem.enabled is False
    assert prem.usable is False
    assert prem.pool_name == "smart", "关掉不该把选中的那套清掉（界面没法让用户重选）"
    assert prem.roles == ("judge",), "关掉了还留着用途勾选 ⇒ 重开时行为会突然和关之前一样"

    assert _prof("judge").model == "cheap-model", "关掉之后没有立刻回落"
    assert _source("judge") == "pool"

    ui = _premium_ui(client)
    assert ui["enabled"] is False
    assert ui["usable"] is False
    assert ui["applies_to"] == []


def test_re_enabling_after_being_turned_off_needs_only_the_switch(client) -> None:
    """重开只需把开关拨回来（选择还在），不需要重选模型池 —— 这是上一条的实际收益。"""
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["judge"])
    client.put(
        "/api/admin/settings/llm",
        json={"premium": {"enabled": False, "pool_name": "smart", "roles": ["judge"]}},
    )

    client.put(
        "/api/admin/settings/llm",
        json={"premium": {"enabled": True, "pool_name": "smart", "roles": ["judge"]}},
    )

    assert _prof("judge").model == "smart-model", "重开时选中的那套丢了 ⇒ 用户只能看到便宜模型"
    assert _source("judge") == "premium"


def test_premium_applies_to_lists_purposes_in_a_stable_order(client) -> None:
    """applies_to 用固定顺序回，界面渲染才稳定。"""
    _set_pool(client, POOL_A, POOL_B)
    _set_premium(client, enabled=True, pool_name="smart", roles=["assistant", "judge_escalate"])

    ui = _premium_ui(client)
    assert ui["usable"] is True
    assert ui["applies_to"] == ["judge_escalate", "assistant"]
