"""按用途分模型（2026-10-11）：省钱的机制必须被钉死，因为它靠"量"换算成钱。

真库实测（46 条带埋点的结果）：444 万 token / 271 次调用，agent 每步调用占约 95%，
判定每条用例只 1 次、输入 3~4k token。所以省钱=执行用便宜模型、判定留强模型。

这里用本地 mock 网关验证三件事（不依赖任何外部厂商）：
1. role_profile：只换模型时其余字段继承默认；未配置的用途等于默认
2. pool_for_role：用途 profile 打头，池里其余条目仍是 429 兜底，且不重复试同一套
3. chat(role=...) 真的把请求发到该用途的模型；429 时确实换到池里的下一套
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

STATE: dict = {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        return self._send(200, {"name": "mock"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = json.loads(self.rfile.read(n) or b"{}")
        model = raw.get("model", "")
        STATE.setdefault("seen", []).append(model)
        # 按模型名决定行为：名字里带 "429" 的模型模拟配额耗尽
        if "429" in model:
            return self._send(429, {"error": {"message": "quota exhausted"}})
        return self._send(200, {
            "id": "cc_1", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })


@pytest.fixture()
def mock_gw():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    STATE.clear()
    STATE["seen"] = []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def patched_llm(monkeypatch, mock_gw):
    """把池与角色配置替换成测试数据（不碰真库、不碰 .env）。"""
    from app import llm

    pool = [
        llm.LLMProfile(name="primary", base_url=mock_gw, api_key="k1", model="default-model"),
        llm.LLMProfile(name="backup", base_url=mock_gw, api_key="k2", model="backup-model"),
    ]

    async def _pool():
        return list(pool)

    # 角色覆盖：judge 只换模型；agent 换模型 + 换网关（模拟"执行走另一家便宜厂商"）
    overrides = {
        "judge": llm.RoleOverride(model="judge-model"),
        "agent": llm.RoleOverride(model="cheap-agent-model", base_url=mock_gw, api_key="k3"),
        # narrative / specgen / assistant 故意不配 → 必须等于默认
    }

    async def _roles():
        return dict(overrides)

    monkeypatch.setattr(llm, "load_pool", _pool, raising=True)
    monkeypatch.setattr(llm, "load_roles", _roles, raising=True)
    llm.invalidate_llm_cache()
    return llm, pool


async def test_role_profile_inherits_everything_but_the_model(patched_llm):
    llm, pool = patched_llm
    p = await llm.role_profile("judge")
    assert p.model == "judge-model"
    # 只换了模型：网关/钥匙仍继承默认那套（否则用户得把钥匙再抄一遍）
    assert p.base_url == pool[0].base_url
    assert p.api_key == pool[0].api_key
    # 换了模型就不再沿用默认的 agent_model（那个名字在新模型上多半不存在）
    assert p.agent_model == ""


async def test_unconfigured_role_equals_default(patched_llm):
    llm, pool = patched_llm
    for role in ("narrative", "specgen", "assistant"):
        p = await llm.role_profile(role)
        assert p.model == pool[0].model, f"{role} 未配置时必须等于默认模型"


async def test_pool_for_role_keeps_fallback_and_dedups(patched_llm):
    llm, pool = patched_llm
    cands = await llm.pool_for_role("agent")
    assert cands[0].model == "cheap-agent-model"
    # 池里的兜底条目仍在（配额耗尽不能把整轮跑废），且同一套不会被试两遍
    models = [c.model for c in cands]
    assert "default-model" in models and "backup-model" in models
    assert len(models) == len(set(models))


async def test_chat_sends_the_roles_model(patched_llm):
    """判定用 judge-model、执行用 cheap-agent-model，请求体里必须是这两个名字。"""
    from app.llm_chat import chat

    STATE["seen"].clear()
    await chat(role="judge", system="s", user="u")
    assert STATE["seen"][-1] == "judge-model"

    await chat(role="agent", system="s", user="u")
    assert STATE["seen"][-1] == "cheap-agent-model"

    # 未配的用途 → 默认模型
    await chat(role="specgen", system="s", user="u")
    assert STATE["seen"][-1] == "default-model"


async def test_429_on_the_role_model_falls_over_to_the_pool(patched_llm, monkeypatch):
    """用途模型配额耗尽时仍要能换池 → 否则"省钱"会变成"跑不动"。"""
    from app import llm as llm_mod
    from app.llm_chat import chat_with_pool

    async def _roles():
        return {"judge": llm_mod.RoleOverride(model="judge-429-model")}

    monkeypatch.setattr(llm_mod, "load_roles", _roles, raising=True)
    llm_mod.invalidate_llm_cache()

    STATE["seen"].clear()
    res, prof = await chat_with_pool(role="judge", system="s", user="u")
    assert res.text == "ok"
    # 先试用途模型（429），再落到池里的下一套
    assert STATE["seen"][0] == "judge-429-model"
    assert prof.model == "default-model"


async def test_roles_for_ui_reports_effective_values(patched_llm):
    llm, pool = patched_llm
    ui = await llm.roles_for_ui()
    assert set(ui) == set(llm.ROLES)
    # 界面要能直接显示"实际生效的模型"，而不是让人自己推算继承
    assert ui["judge"]["effective_model"] == "judge-model"
    assert ui["specgen"]["effective_model"] == pool[0].model
    assert ui["judge"]["api_key_set"] is False
    assert ui["agent"]["api_key_set"] is True


async def test_probe_429_must_not_be_read_as_a_different_protocol(patched_llm, mock_gw):
    """配额耗尽 ≠ 协议不对（2026-10-11 真实缺陷）。

    探测链路以前把任何 >=400 都当成"这个端点不是这个协议"，于是一个**配额用完的
    OpenAI 兼容端点**会被判成 anthropic → google，最后拿 Gemini 的 genai 去跟它说话：
    报错完全指不到真实原因。这里钉死：探测遇到 429 必须抛 RateLimitedError
    （带 status_code=429，好让 chat_with_pool 换下一套 key）。
    """
    from app import llm as llm_mod
    from app.llm_protocol import RateLimitedError

    prof = llm_mod.LLMProfile(
        name="limited", base_url=mock_gw, api_key="k9", model="daily-quota-429"
    )
    with pytest.raises(RateLimitedError) as ei:
        await llm_mod.capabilities_for(prof, force=True)
    assert ei.value.status_code == 429
    # 关键：不能被记成别的协议（那就是"用 genai 跟 OpenAI 端点说话"的入口）
    assert "google" not in str(ei.value)
