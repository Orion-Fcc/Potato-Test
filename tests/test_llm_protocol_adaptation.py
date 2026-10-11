"""协议自适应的确定性验证：用本地 mock 网关，不依赖任何外部厂商。

为什么要专门为"换厂商"写测试（2026-10-10）：
用户的原话是"以后会换别的 LLM，不想每次都改代码，甚至可能不是 OpenAI 协议"。
这类需求的功能正确性**没法靠真网关回归** —— 真网关的行为会变（当天就变过一次：
突然拒 json_schema，把 run17 的 TP-CCR-001/005 打成"只导航、零动作"），
而且换厂商意味着"上次测通的那家这次不在了"。

所以这里自己起两种协议的端点，把下面这些结论钉死：
1. auto 识别：只有 /messages 的端点 → anthropic；只有 /chat/completions 的 → openai
2. 能力探测：拒 json_schema 的端点 → json_schema=False；拒 json_object 也如实反映
3. 代理工厂：拒 json_schema → dont_force_structured_output=True（run17 的修复点）
4. 门面：Anthropic 通道要把 system 提到顶层参数、图片翻成 base64 image block；
   OpenAI 通道图片原样透传
5. 显式指定 provider 但路由不存在 → 尊重用户，回退到协议默认能力，不乱试别的协议
6. 探测结果必须被缓存（否则每个用例都要多花一到两次调用）
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

STATE: dict = {}

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
           "AAH/q842iQAAAABJRU5ErkJggg==")


class _Handler(BaseHTTPRequestHandler):
    """最小的双协议 mock：OpenAI /chat/completions + Anthropic /messages。"""

    def log_message(self, *args):  # 静音测试输出
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "/models/" in self.path:
            return self._send(200, {"name": "mock-model"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = json.loads(self.rfile.read(n) or b"{}")

        if self.path.endswith("/messages"):
            STATE.setdefault("seen", []).append(("anthropic", raw))
            if not STATE.get("anthropic"):
                return self._send(404, {"error": "no such route"})
            return self._send(200, {
                "id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "anthropic ok"}],
                "usage": {"input_tokens": 11, "output_tokens": 7},
            })

        if not self.path.endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        STATE.setdefault("seen", []).append(("openai", raw))
        rf = (raw.get("response_format") or {}).get("type")
        if rf == "json_object" and not STATE.get("json_object"):
            return self._send(400, {"error": {"message": "response_format type is unavailable now"}})
        if rf == "json_schema" and not STATE.get("json_schema"):
            return self._send(
                400, {"error": {"message": "This response_format type is unavailable now"}}
            )
        text = '```json\n{"status": "passed"}\n```' if rf else "openai text ok"
        return self._send(200, {
            "id": "cc_1", "object": "chat.completion", "model": raw.get("model", "m"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30,
                      "prompt_tokens_details": {"cached_tokens": 5}},
        })


class _NoOpenAI(_Handler):
    """只保留 Anthropic 路由的端点：用来验证协议识别与显式 provider 的优先级。"""

    def do_POST(self):
        if self.path.endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        return super().do_POST()


@pytest.fixture()
def gateway():
    """真实监听在 127.0.0.1 上的 mock 网关 + 可切换的能力开关。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    STATE.clear()
    STATE.update(json_object=True, json_schema=False, anthropic=True, seen=[])
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _profile(base: str, **kw):
    from app import llm

    params = {"name": "probe", "base_url": base, "api_key": "k", "model": "mock-model"}
    params.update(kw)
    return llm.LLMProfile(**params)


async def test_auto_detects_openai_and_honours_rejected_json_schema(gateway):
    """run17 现场：端点认 openai，但拒 json_schema —— 代理必须自动关掉强制结构化输出。"""
    _srv, base = gateway
    STATE.update(json_schema=False)

    from app import llm

    prof = _profile(base)
    caps = await llm.capabilities_for(prof, force=True)
    assert caps.protocol == "openai"
    assert caps.json_object is True
    assert caps.json_schema is False
    assert caps.tolerant_structured_output is True

    agent = await llm._build_agent_llm(prof)
    assert type(agent).__name__ == "ChatOpenAI"
    assert agent.dont_force_structured_output is True


async def test_supported_json_schema_keeps_strict_mode(gateway):
    """厂商支持 json_schema 时不能白白降级 —— 自动回到严格模式。"""
    _srv, base = gateway
    STATE.update(json_schema=True)

    from app import llm

    prof = _profile(base)
    caps = await llm.capabilities_for(prof, force=True)
    assert caps.json_schema is True
    agent = await llm._build_agent_llm(prof)
    assert agent.dont_force_structured_output is False


async def test_auto_detects_anthropic_route_and_builds_chat_anthropic(gateway):
    """只有 /messages 的端点：识别成 anthropic，并给出对应的 browser-use 客户端。"""
    srv, base = gateway
    srv.RequestHandlerClass = _NoOpenAI

    from app import llm

    prof = _profile(base, model="claude-x")
    caps = await llm.capabilities_for(prof, force=True)
    assert caps.protocol == "anthropic"
    # 我们的工具循环是 OpenAI 形状 → 非 openai 协议必须自报不支持 tools，
    # 上层才能明确降级（而不是把工具调用失败当成故障暴露给用户）
    assert caps.tools is False
    agent = await llm._build_agent_llm(prof)
    assert type(agent).__name__ == "ChatAnthropic"


async def test_anthropic_facade_hoists_system_and_translates_image(gateway):
    """Anthropic 通道的 system 是顶层参数、图片是 base64 block —— 翻译错了就是 400。"""
    srv, base = gateway
    srv.RequestHandlerClass = _NoOpenAI

    from app import llm

    prof = _profile(base, model="claude-x")
    await llm.chat(prof, system="系统提示", user="问题", timeout=10)
    # 第一次调用会先触发能力探测（1-token 存活请求），取最后一条即可拿到真正的业务调用
    first = [x for x in STATE["seen"] if x[0] == "anthropic"][-1][1]
    assert first.get("system") == "系统提示", "system 没有被提到顶层参数"
    assert first["messages"][0]["role"] == "user"

    STATE["seen"].clear()
    await llm.chat(
        prof,
        content=[
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}},
        ],
        timeout=10,
    )
    blocks = [x for x in STATE["seen"] if x[0] == "anthropic"][-1][1]["messages"][0]["content"]
    assert [b.get("type") for b in blocks] == ["text", "image"]
    assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": PNG_B64}


async def test_openai_facade_passes_image_parts_through_unchanged(gateway):
    """OpenAI 通道不能动图片 part 的形状（动了老网关立刻 400）。"""
    _srv, base = gateway

    from app import llm

    prof = _profile(base, provider="openai")
    await llm.chat(
        prof,
        content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}},
        ],
        timeout=10,
    )
    sent = [x for x in STATE["seen"] if x[0] == "openai"]
    parts = sent[-1][1]["messages"][0]["content"]
    assert [p.get("type") for p in parts] == ["text", "image_url"]


async def test_explicit_provider_wins_and_does_not_wander(gateway):
    """显式 provider=openai 而端点只有 anthropic 路由：尊重用户，回退协议默认值。

    不能"猜不出来就到处试别的协议" —— 那会把一个配置错误变成一串难以解释的行为。
    """
    srv, base = gateway
    srv.RequestHandlerClass = _NoOpenAI

    from app import llm

    caps = await llm.capabilities_for(_profile(base, provider="openai"), force=True)
    assert caps.protocol == "openai"
    assert caps.probed is False, "探测失败时必须标明'未实测'，不能用默认值冒充实测结果"


async def test_openai_facade_parses_json_wrapped_in_code_fences(gateway):
    """JSON 模式下的容错：模型把 JSON 包进 ``` 代码块也要能解析（否则白丢一次成功调用）。"""
    _srv, base = gateway

    from app import llm

    prof = _profile(base, provider="openai")
    res = await llm.chat(prof, system="s", user="u", json_mode=True, timeout=10)
    assert res.json(default=None) == {"status": "passed"}
    assert res.usage["calls"] == 1 and res.usage["cached"] == 5


async def test_capability_probe_is_cached(gateway):
    """探测结果必须被缓存 —— 否则"每个用例多几次调用"就是性能倒退。"""
    _srv, base = gateway

    from app import llm

    prof = _profile(base, provider="openai")
    await llm.capabilities_for(prof, force=True)
    before = len(STATE["seen"])
    await llm.capabilities_for(prof)
    await llm.capabilities_for(prof)
    assert len(STATE["seen"]) == before, "缓存没生效：每次调用都在重新探测"
