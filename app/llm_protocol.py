"""LLM 端点「说什么协议、有哪些能力」的自适应层。

为什么需要它（2026-10-10 现场）：网关 api.example.com 突然开始拒绝
`response_format={"type":"json_schema"}`（400 `This response_format type is
unavailable now`），而 browser-use 的 ChatOpenAI 默认**每一步**都带这个字段 ——
结果 run17 里 TP-CCR-001/005 只导航成功，后面每一步全 400，用例被判"只导航、零动作"。
同一天，同一网关却仍然接受 `json_object`（判定器就是靠它跑通的）。

结论：**"这个端点支持什么"不能写死在代码里**。它随厂商、随厂商的临时变更而变。
所以这一层负责：把「协议」和「能力」探测出来、缓存起来，代码只读结论。

换模型 = 改配置（base_url / key / model / provider），不改代码：
- 所有 OpenAI 兼容网关（DeepSeek、通义、智谱、月之暗面、硅基流动、Groq、vLLM、LM Studio…）
  走 openai 协议；
- Claude 走 anthropic 协议；Gemini 走 google 协议；本地 Ollama 走 ollama 协议；
- provider 留空/auto 时**自动识别**，猜不出来就按 openai 试。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger("potato-test.llm")

# 我们真正实现了适配的协议。
PROTOCOLS = ("openai", "anthropic", "google", "ollama")

# provider 别名归一化：国产/第三方网关绝大多数是 OpenAI 兼容，只是名字不同，
# 让用户填 "deepseek"/"qwen"/"kimi" 这类熟悉的词也能落到正确协议上。
_PROVIDER_ALIASES: dict[str, str] = {
    "": "auto",
    "auto": "auto",
    "detect": "auto",
    # OpenAI 协议（含一切 OpenAI 兼容网关）
    "openai": "openai",
    "openai_compatible": "openai",
    "openai-compatible": "openai",
    "compatible": "openai",
    "deepseek": "openai",
    "groq": "openai",
    "openrouter": "openai",
    "siliconflow": "openai",
    "moonshot": "openai",
    "kimi": "openai",
    "zhipu": "openai",
    "glm": "openai",
    "qwen": "openai",
    "dashscope": "openai",
    "bailian": "openai",
    "mistral": "openai",
    "cerebras": "openai",
    "azure": "openai",
    "azure_openai": "openai",
    "vllm": "openai",
    "lmstudio": "openai",
    "oneapi": "openai",
    "newapi": "openai",
    # 原生协议
    "anthropic": "anthropic",
    "claude": "anthropic",
    "google": "google",
    "gemini": "google",
    "ollama": "ollama",
}

# 从 base_url 猜协议（只在 provider=auto 时用，命中就不必发探测请求）。
_HOST_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("anthropic.com", "/v1/messages"), "anthropic"),
    (("generativelanguage.googleapis.com", "aiplatform.googleapis.com"), "google"),
    ((":11434", "ollama"), "ollama"),
    (("openai.com", "/v1/chat/completions"), "openai"),
)


@dataclass(frozen=True)
class Capabilities:
    """一个 profile 的实测能力。所有字段都是"探测出来的事实"，不是假设。"""

    protocol: str  # openai | anthropic | google | ollama
    json_object: bool  # 支持 response_format={"type":"json_object"}
    json_schema: bool  # 支持 response_format={"type":"json_schema"}（browser-use 默认要发）
    tools: bool  # 支持 OpenAI 风格 function calling（我们的工具循环用）
    vision: bool  # 支持图片输入
    probed: bool = True  # False = 用协议默认值推断，没实测（显式指定 provider 且探测失败）

    @property
    def tolerant_structured_output(self) -> bool:
        """给 browser-use 用：不支持 json_schema 时必须关掉强制结构化输出。

        注意 `add_schema_to_system_prompt=True` **不能**替代它 —— browser-use 0.13
        里它只是把 schema 也写进 system prompt，`response_format` 照发不误
        （.venv/Lib/site-packages/browser_use/llm/openai/chat.py:225-256）。
        """
        return not self.json_schema


# 协议默认能力：显式指定 provider 但探测失败时的兜底（宁可乐观，运行期还有降级路径）。
_PROTOCOL_DEFAULTS: dict[str, Capabilities] = {
    "openai": Capabilities("openai", True, False, True, True, probed=False),
    "anthropic": Capabilities("anthropic", False, False, False, True, probed=False),
    "google": Capabilities("google", False, False, False, True, probed=False),
    "ollama": Capabilities("ollama", False, False, False, True, probed=False),
}


def normalize_provider(raw: str | None) -> str:
    """把用户填的 provider 收敛成协议名或 "auto"。未知值一律当 auto（不报错、不阻断）。"""
    v = (raw or "").strip().lower()
    if v in PROTOCOLS:
        return v
    return _PROVIDER_ALIASES.get(v, "auto")


def _hint_protocol(base_url: str) -> str | None:
    low = (base_url or "").lower()
    for needles, proto in _HOST_HINTS:
        if any(n in low for n in needles):
            return proto
    return None


# ---------------------------------------------------------------------------
# 探测期的 429：**协议是对的，只是配额用完了**
#
# 必须与"这个端点不是这个协议"（探测返回 None）严格分开。2026-10-11 的真实缺陷：
# 探测链路把任何 >=400 都当成"不是这个协议"，于是一个配额耗尽的 OpenAI 兼容端点
# 被判成 anthropic → google，最后用 Gemini 的 genai 去跟 DeepSeek 说话，报错完全
# 指不到真实原因（"按用途分模型"让这件事更容易踩到：便宜模型的日配额更容易打满）。
#
# 抛出去而不是吞掉，是为了让 chat_with_pool 按池语义换下一套 key。
# ---------------------------------------------------------------------------
class RateLimitedError(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"gateway rate-limited (HTTP {status}): {(text or '')[:200]}")
        # chat_with_pool 的 _is_rate_limited 看的就是这个属性
        self.status_code = status


# ---------------------------------------------------------------------------
# 缓存：能力随厂商变更而变，但没人希望每个用例都去探一遍（那是纯延迟开销）。
# in-process + TTL。TTL 内零成本；过期后下一次调用顺手重探一次。
# ---------------------------------------------------------------------------
_cache: dict[tuple[str, str, str], tuple[Capabilities, float]] = {}


def invalidate_capabilities() -> None:
    """配置变更后调用（admin 保存 LLM 设置），让下一次调用重新探测。"""
    _cache.clear()


def _cache_key(profile) -> tuple[str, str, str]:
    # key 只取前 8 位做隔离，避免把密钥写进内存字典的可读位置
    return (profile.base_url or "", profile.effective_model or "", (profile.api_key or "")[:8])


def _ttl() -> float:
    return float(getattr(get_settings(), "llm_capability_ttl_s", 21600) or 21600)


async def capabilities_for(profile, *, force: bool = False) -> Capabilities:
    """一个 profile 的实测能力（带 TTL 缓存）。探测不出来就按协议默认值。

    唯一的例外：**429**（配额耗尽）会原样抛出。它不是"探测不出"，而是"这套 key 现在
    不能用"—— 上层（chat_with_pool）要靠这个异常换下一套；吞掉它会让一个配额用完的
    端点被当成"换个协议也许行"。
    """
    key = _cache_key(profile)
    now = time.monotonic()
    if not force:
        hit = _cache.get(key)
        if hit is not None and now - hit[1] < _ttl():
            return hit[0]

    explicit = normalize_provider(getattr(profile, "provider", "") or "")
    try:
        caps = await _probe(profile, explicit)
    except RateLimitedError:
        raise
    except Exception as exc:  # noqa: BLE001 — 探测失败绝不能阻断真实调用
        log.warning("llm: 能力探测失败（按协议默认值继续）：%s: %s", type(exc).__name__, exc)
        proto = explicit if explicit != "auto" else (_hint_protocol(profile.base_url) or "openai")
        caps = _PROTOCOL_DEFAULTS[proto]
    _cache[key] = (caps, now)
    log.info(
        "llm: %s → protocol=%s json_object=%s json_schema=%s tools=%s vision=%s%s",
        profile.name, caps.protocol, caps.json_object, caps.json_schema, caps.tools, caps.vision,
        "" if caps.probed else "（未实测，按协议默认）",
    )
    return caps


async def _probe(profile, explicit: str) -> Capabilities:
    """真实探测。顺序：显式 provider > base_url 线索 > 依次试 openai/anthropic/google。"""
    order: list[str] = []
    if explicit != "auto":
        order.append(explicit)
    else:
        hint = _hint_protocol(profile.base_url)
        if hint:
            order.append(hint)
        for p in ("openai", "anthropic", "google", "ollama"):
            if p not in order:
                order.append(p)

    for proto in order:
        caps = await _probe_protocol(profile, proto)
        if caps is not None:
            return caps
        if explicit != "auto":
            # 用户指定了协议，探测失败就不再乱试别的协议 —— 尊重用户的判断，
            # 用协议默认能力继续（真跑不通时错误信息会说明问题）。
            log.warning("llm: provider=%s 探测未通过，按该协议默认能力继续", explicit)
            return _PROTOCOL_DEFAULTS[proto]
    # 全都不认：按 openai 兼容最乐观处理，交给真实调用报错
    return _PROTOCOL_DEFAULTS["openai"]


# ---- 各协议的探测 ----

async def _probe_protocol(profile, proto: str) -> Capabilities | None:
    """返回 None = "这个端点不是这个协议"（再去试下一个）；返回对象 = 命中。"""
    if proto == "openai":
        return await _probe_openai(profile)
    if proto == "anthropic":
        return await _probe_anthropic(profile)
    if proto == "google":
        return await _probe_google(profile)
    if proto == "ollama":
        return await _probe_ollama(profile)
    return None


def _http(timeout: float = 20.0) -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        verify=s.gateway_verify_ssl,
        timeout=httpx.Timeout(timeout),
        trust_env=not s.gateway_ignore_proxy,
    )


def _openai_chat_url(base_url: str) -> str:
    """base_url 是 SDK 语义（通常已含 /v1），探测时直接拼 /chat/completions。

    兼容两种写法：给了 ".../v1" 就补 "/chat/completions"；给了完整端点就原样用。
    """
    b = (base_url or "").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    return f"{b}/chat/completions"


def _rf_unsupported(text: str) -> bool:
    """从 400 的错误文案判断"这个 response_format 类型不被支持"。

    实测坑（2026-10-10，DeepSeek）：提示词里没有 "json" 字样时，用 json_object 会报
    `Prompt must contain the word 'json' in some form to use 'response_format' of type
    'json_object'` —— 文案里同时出现 response_format 和 400，但它**不是**能力问题，
    是提示词问题（真实调用点都写了"返回 JSON"，所以永远不会踩到）。
    只看"response_format"关键词会把一个支持 json_object 的端点判成不支持，
    于是判定器白白降级成提示词模式。所以这里要把这条排除掉。
    """
    t = (text or "").lower()
    if "response_format" not in t:
        return False
    if "must contain the word" in t or "word 'json'" in t or 'word "json"' in t:
        return False
    return True


async def _probe_openai(profile) -> Capabilities | None:
    """先打一发最小 completion；通了再分别探 json_object / json_schema。

    三次请求都是 max_tokens=1~16 级的小调用，且结果被 TTL 缓存 ——
    开销摊薄到进程级，不落在每个用例上。
    """
    url = _openai_chat_url(profile.base_url)
    headers = {"Authorization": f"Bearer {profile.api_key}"}
    model = profile.effective_model
    base_body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    # 探测 json_object 的提示词必须带 "json" 字样（见 _rf_unsupported 的注释）
    json_body = {
        "model": model,
        "messages": [{"role": "user", "content": 'Return JSON: {"ok": true}'}],
        "max_tokens": 16,
    }

    async with _http() as c:
        try:
            r = await c.post(url, json=base_body, headers=headers)
        except Exception:  # noqa: BLE001 — 网络层不通 → 不是这个协议（或该地址不可达）
            return None
        if r.status_code >= 400:
            if r.status_code == 429:
                # 配额耗尽 ≠ 协议不对：继续用 openai 协议，让上层换下一套 key。
                raise RateLimitedError(r.status_code, r.text)
            if r.status_code in (401, 403):
                # 路由在、鉴权有问题 → 是 OpenAI 协议但配置要修，照实返回（不阻断其余判断）
                return Capabilities("openai", True, False, True, True, probed=False)
            return None

        # ① json_object（判定器 / 三段式叙述 / 话术提炼依赖它）
        ro = await c.post(url, json={**json_body, "response_format": {"type": "json_object"}}, headers=headers)
        if ro.status_code == 429:
            # 这一发也限流：能力无从判断，按"兼容端点通常支持"继续，别因为限流把它记成"不支持"
            json_object = True
        else:
            json_object = not (ro.status_code >= 400 and _rf_unsupported(ro.text))
        # ② json_schema（browser-use 代理默认要发它 —— 就是 2026-10-10 崩在这）
        probe_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "agent_output",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        }
        rs = await c.post(url, json={**json_body, "response_format": probe_schema}, headers=headers)
        json_schema = not (rs.status_code >= 400 and _rf_unsupported(rs.text))

    return Capabilities("openai", json_object, json_schema, True, True)


async def _probe_anthropic(profile) -> Capabilities | None:
    b = (profile.base_url or "").strip().rstrip("/")
    url = b if b.endswith("/messages") else f"{b}/messages"
    headers = {
        "x-api-key": profile.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": profile.effective_model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    async with _http() as c:
        try:
            r = await c.post(url, json=body, headers=headers)
        except Exception:  # noqa: BLE001
            return None
    if r.status_code == 429:
        # 同上：限流不是『协议不对』，交给上层换 key
        raise RateLimitedError(r.status_code, r.text)
    if r.status_code in (401, 403):
        return Capabilities("anthropic", False, False, False, True, probed=False)
    if r.status_code >= 400:
        return None
    return Capabilities("anthropic", False, False, False, True)


async def _probe_google(profile) -> Capabilities | None:
    """Gemini：GET 模型元数据（比 generateContent 便宜，且能确认 key + model 存在）。"""
    b = (profile.base_url or "https://generativelanguage.googleapis.com/v1beta").strip().rstrip("/")
    url = f"{b}/models/{profile.effective_model}"
    async with _http() as c:
        try:
            r = await c.get(url, headers={"x-goog-api-key": profile.api_key})
        except Exception:  # noqa: BLE001
            return None
    if r.status_code == 429:
        # 同上：限流不是『协议不对』，交给上层换 key
        raise RateLimitedError(r.status_code, r.text)
    if r.status_code in (401, 403):
        return Capabilities("google", False, False, False, True, probed=False)
    if r.status_code >= 400:
        return None
    return Capabilities("google", False, False, False, True)


async def _probe_ollama(profile) -> Capabilities | None:
    """Ollama：/api/tags 列出本地模型。命中的话模型名要做存在性匹配。"""
    base = (profile.base_url or "http://127.0.0.1:11434").strip().rstrip("/")
    for suffix, path in (("/api/tags", "/api/tags"), ("/v1", "/api/tags")):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    async with _http() as c:
        try:
            r = await c.get(f"{base}/api/tags")
        except Exception:  # noqa: BLE001
            return None
    if r.status_code >= 400:
        return None
    return Capabilities("ollama", False, False, False, True)
