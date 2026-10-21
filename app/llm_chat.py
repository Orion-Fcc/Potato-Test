"""统一 LLM 调用门面：一处收口，换厂商不改调用方代码。

背景（2026-10-10）：平台里散着十几处 `client.chat.completions.create(...)`，
每一处都默认三件事：OpenAI 协议、`response_format={"type":"json_object"}`、
OpenAI 形状的返回体。于是"换个厂商"就变成"改十四个文件" —— 这不是配置问题，
是架构问题。本模块把这些调用收进一个 `chat()`：

- 输入：system / user 文案（或 OpenAI 风格 messages，含图片 part）；
- 输出：`ChatResult(text, usage, model, protocol, raw)`，**协议无关**；
- JSON 模式：能力支持就发原生 response_format，不支持就走提示词 + 容错解析；
- 视觉输入：OpenAI 的 image_url part 会被翻译成 Anthropic 的 image block /
  Gemini 的 Part.from_bytes。

调用方只关心"给我文字/给我 JSON"，不关心对面是谁。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings
from app.llm_protocol import Capabilities, capabilities_for

log = logging.getLogger("potato-test.llm")


def _http_for(profile, timeout: float) -> httpx.AsyncClient:
    """按 profile 的代理策略建客户端（系统 / 直连 / 自定义代理）。"""
    from app import llm as _llm  # 延迟导入，避免循环依赖
    return _llm.http_client_for(profile, timeout=timeout)


@dataclass
class ChatResult:
    """协议无关的回复。usage 用统一键名，跟平台里的 token 账本对齐。"""

    text: str
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    protocol: str = ""
    raw: Any = None

    def json(self, default: Any = None) -> Any:
        """把 text 当 JSON 解析（容错：去代码块、截取第一个完整对象）。"""
        return extract_json(self.text, default=default)


@dataclass
class ChatStreamEvent:
    """One chunk of a streaming reply.

    The event with ``done=True`` carries usage/model/protocol only. Text always
    arrives in ``text``; concatenating all text chunks gives the full reply.
    """

    text: str = ""
    done: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    protocol: str = ""


def empty_usage() -> dict[str, int]:
    return {"prompt": 0, "completion": 0, "cached": 0, "total": 0, "calls": 0}


def merge_usage(dst: dict[str, int], src: dict[str, int]) -> dict[str, int]:
    for k in ("prompt", "completion", "cached", "total", "calls"):
        dst[k] = int(dst.get(k, 0)) + int(src.get(k, 0))
    return dst


_JSON_ONLY_HINT = (
    "\n\n[输出格式] 只输出一个合法 JSON 对象，不要任何解释文字、前后缀或代码块标记。"
)


def extract_json(text: str, default: Any = None) -> Any:
    """从模型输出里抠出 JSON。

    有些厂商（尤其被要求"返回 JSON"但走提示词而非原生 response_format 时）会
    包上 ```json 代码块，或前后带一句"好的，这是结果："。直接 json.loads 会炸，
    于是把整次调用判成失败 —— 这不合理，能救就救。
    """
    if not text:
        return default
    s = text.strip()
    if s.startswith("```"):
        # 去围栏：```json ... ``` / ``` ... ```
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001 — 继续尝试截取
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch not in "{[":
            continue
        try:
            obj, _ = dec.raw_decode(s[i:])
            return obj
        except Exception:  # noqa: BLE001
            continue
    return default


# ---------------------------------------------------------------------------
# 消息与图片的协议翻译
# ---------------------------------------------------------------------------

def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """把 OpenAI 风格 messages 拆成 (system 文案, 其余消息)。"""
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str):
                system_parts.append(c)
        else:
            rest.append(m)
    return "\n\n".join(p for p in system_parts if p), rest


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content or "")


def _data_url(b64: str, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{b64}"


def _image_parts(content: Any) -> list[dict]:
    """抽出 OpenAI 风格的图片 part：[{"data": b64, "mime": "image/png"}]。"""
    out: list[dict] = []
    if not isinstance(content, list):
        return out
    for p in content:
        if not isinstance(p, dict) or p.get("type") != "image_url":
            continue
        url = (p.get("image_url") or {}).get("url") or ""
        if not url.startswith("data:"):
            out.append({"data": None, "uri": url, "mime": "image/png"})
            continue
        header, _, b64 = url.partition(",")
        mime = header[5:].split(";")[0] or "image/png"
        out.append({"data": b64, "uri": None, "mime": mime})
    return out


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------

async def chat(
    profile=None,
    *,
    role: str | None = None,
    messages: list[dict] | None = None,
    system: str | None = None,
    user: Any = None,
    content: Any = None,
    json_mode: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 60.0,
) -> ChatResult:
    """协议无关的一发 completion。

    profile=None 时按 `role` 取该用途的模型（agent/judge/narrative/specgen/assistant），
    没给 role 就用"平台默认模型"。显式传 profile 时 role 被忽略（调用方已经决定了）。
    `content`/`user` 都可以是字符串或 OpenAI 风格 part 列表（含 image_url）。
    """
    from app import llm as _llm  # 延迟导入，避免循环依赖

    if profile is None:
        profile = await _llm.role_profile(role) if role else await _llm.active_profile()
    caps = await capabilities_for(profile)

    if messages is None:
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        body = content if content is not None else user
        msgs.append({"role": "user", "content": body if body is not None else ""})
        messages = msgs

    if caps.protocol == "anthropic":
        return await _chat_anthropic(profile, caps, messages, json_mode, temperature, max_tokens, timeout)
    if caps.protocol == "google":
        return await _chat_google(profile, caps, messages, json_mode, temperature, max_tokens, timeout)
    if caps.protocol == "ollama":
        return await _chat_openai(profile, caps, messages, json_mode, temperature, max_tokens, timeout,
                                  force_base_url=_ollama_openai_base(profile.base_url))
    return await _chat_openai(profile, caps, messages, json_mode, temperature, max_tokens, timeout)


async def chat_with_pool(
    *,
    role: str | None = None,
    messages: list[dict] | None = None,
    system: str | None = None,
    user: Any = None,
    content: Any = None,
    json_mode: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 60.0,
) -> tuple[ChatResult, Any]:
    """按池优先级调用，429（配额耗尽）自动换下一套。

    role 给定时，候选序列以**该用途的模型**打头（见 llm.pool_for_role），
    池里其余条目仍作为 429 兜底。

    与 judge/specgen 原有的语义一致：非 429 的错误（401 错 key、404 错模型）
    是配置问题，直接抛出，不在池里乱跳 —— 跳了也一样失败，只会把报错搞模糊。
    """
    from app import llm as _llm

    pool = await (_llm.pool_for_role(role) if role else _llm.load_pool())
    # 自动降级：开关关掉时退回旧语义（只换 429），避免用户关不掉。
    routing = await _llm.load_routing()
    last: Exception | None = None
    for prof in pool:
        try:
            return await chat(
                prof, messages=messages, system=system, user=user, content=content,
                json_mode=json_mode, temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            ), prof
        except Exception as exc:  # noqa: BLE001
            switchable = _llm._is_switchable_error(exc) if routing.failover else _llm._is_rate_limited(exc)
            if switchable:
                log.warning("llm: %s 失败（%s），自动降级到下一条", prof.name, type(exc).__name__)
                last = exc
                continue
            raise
    if last is not None:
        raise last
    raise RuntimeError("llm: 池里没有任何可用 profile")


# ---------------------------------------------------------------------------
# Streaming facade (2026-10-20)
# ---------------------------------------------------------------------------
#
# Kept separate from chat(): a stream yields many chunks, a non-stream call
# yields one result; forcing both through one return type would make every
# existing caller test "is this a stream?". Both share the same message and
# protocol selection logic.

async def chat_stream(
    profile=None,
    *,
    role: str | None = None,
    messages: list[dict] | None = None,
    system: str | None = None,
    user: Any = None,
    content: Any = None,
    json_mode: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 60.0,
) -> AsyncIterator[ChatStreamEvent]:
    """Protocol-agnostic streaming completion.

    Failure is raised **during iteration**, not at call time. Callers must wrap
    the ``async for`` in try/except.
    """
    from app import llm as _llm

    if profile is None:
        profile = await _llm.role_profile(role) if role else await _llm.active_profile()
    caps = await capabilities_for(profile)

    if messages is None:
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        body = content if content is not None else user
        msgs.append({"role": "user", "content": body if body is not None else ""})
        messages = msgs

    if caps.protocol == "anthropic":
        gen = _chat_anthropic_stream(profile, caps, messages, json_mode, temperature, max_tokens, timeout)
    elif caps.protocol == "google":
        gen = _chat_google_stream(profile, caps, messages, json_mode, temperature, max_tokens, timeout)
    elif caps.protocol == "ollama":
        gen = _chat_openai_stream(
            profile, caps, messages, json_mode, temperature, max_tokens, timeout,
            force_base_url=_ollama_openai_base(profile.base_url),
        )
    else:
        gen = _chat_openai_stream(profile, caps, messages, json_mode, temperature, max_tokens, timeout)
    async for ev in gen:
        yield ev


async def chat_stream_with_pool(
    *,
    role: str | None = None,
    messages: list[dict] | None = None,
    system: str | None = None,
    user: Any = None,
    content: Any = None,
    json_mode: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 60.0,
) -> AsyncIterator[ChatStreamEvent]:
    """Stream through the profile pool; switch profiles only before first text.

    Switching after text has been sent would splice two models' output together,
    which reads worse than an error. So only a pre-first-token 429 rotates.
    """
    from app import llm as _llm

    pool = await (_llm.pool_for_role(role) if role else _llm.load_pool())
    routing = await _llm.load_routing()
    last: Exception | None = None
    for prof in pool:
        started = False
        try:
            async for ev in chat_stream(
                prof, messages=messages, system=system, user=user, content=content,
                json_mode=json_mode, temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            ):
                if ev.text:
                    started = True
                yield ev
            return
        except Exception as exc:  # noqa: BLE001
            switchable = _llm._is_switchable_error(exc) if routing.failover else _llm._is_rate_limited(exc)
            if not started and switchable:
                log.warning("llm: %s 流式失败（%s），自动降级到下一条", prof.name, type(exc).__name__)
                last = exc
                continue
            raise
    if last is not None:
        raise last
    raise RuntimeError("llm: 池里没有任何可用 profile")


# ---------------------------------------------------------------------------
# 各协议实现
# ---------------------------------------------------------------------------

async def _chat_openai(
    profile, caps: Capabilities, messages: list[dict], json_mode: bool,
    temperature: float | None, max_tokens: int | None, timeout: float,
    force_base_url: str | None = None,
) -> ChatResult:
    from openai import AsyncOpenAI

    s = get_settings()
    msgs = [dict(m) for m in messages]
    rf: dict | None = None
    if json_mode:
        if caps.json_schema:
            # 有原生 json_schema 就用（最稳），但我们的调用方只需要"合法 JSON"，
            # 不强加具体 schema，避免收紧过头把可选字段卡掉。
            rf = {"type": "json_object"} if caps.json_object else None
        elif caps.json_object:
            rf = {"type": "json_object"}
        if rf is None:
            # 都不支持：走提示词，靠 extract_json 容错解析
            _append_hint(msgs, _JSON_ONLY_HINT)
    else:
        msgs = msgs

    body: dict[str, Any] = {
        "model": profile.effective_model,
        "messages": msgs,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if rf is not None:
        body["response_format"] = rf

    http = _http_for(profile, timeout)
    client = AsyncOpenAI(
        base_url=force_base_url or profile.base_url,
        api_key=profile.api_key or "not-needed",
        http_client=http,
        max_retries=s.llm_max_retries,
    )
    try:
        resp = await client.chat.completions.create(**body)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        return ChatResult(
            text=text, usage=_usage_from_openai(resp), model=profile.effective_model,
            protocol="openai", raw=resp,
        )
    finally:
        await client.close()


def _ollama_openai_base(base_url: str) -> str:
    """Ollama 的 OpenAI 兼容端点在 /v1 下。用户可能填 http://127.0.0.1:11434 或带 /v1。"""
    b = (base_url or "http://127.0.0.1:11434").strip().rstrip("/")
    if b.endswith("/v1"):
        return b
    return f"{b}/v1"


def _append_hint(messages: list[dict], hint: str) -> None:
    """把"只输出 JSON"的要求塞进第一条 system（没有就在最前面补一条）。"""
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            m["content"] = m["content"] + hint
            return
    messages.insert(0, {"role": "system", "content": hint.strip()})


def _usage_from_openai(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return empty_usage()
    d = getattr(u, "prompt_tokens_details", None)
    return {
        "prompt": int(getattr(u, "prompt_tokens", 0) or 0),
        "completion": int(getattr(u, "completion_tokens", 0) or 0),
        "total": int(getattr(u, "total_tokens", 0) or 0),
        "cached": int(getattr(d, "cached_tokens", 0) or 0) if d else 0,
        "calls": 1,
    }


async def _chat_anthropic(
    profile, caps: Capabilities, messages: list[dict], json_mode: bool,
    temperature: float | None, max_tokens: int | None, timeout: float,
) -> ChatResult:
    from anthropic import AsyncAnthropic

    s = get_settings()
    system, rest = _split_system(messages)
    if json_mode:
        system = (system + _JSON_ONLY_HINT).strip()
    conv: list[dict] = []
    for m in rest:
        conv.append({"role": m.get("role", "user"), "content": _to_anthropic_content(m.get("content"))})
    kwargs: dict[str, Any] = {
        "model": profile.effective_model,
        # Anthropic 的 max_tokens 是必填项 —— 给个能装下 JSON 的默认值
        "max_tokens": max_tokens or get_settings().gateway_max_tokens or 4096,
        "messages": conv,
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature

    http = _http_for(profile, timeout)
    client = AsyncAnthropic(
        api_key=profile.api_key, base_url=profile.base_url or None,
        http_client=http, max_retries=s.llm_max_retries,
    )
    try:
        resp = await client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        u = getattr(resp, "usage", None)
        return ChatResult(
            text=text,
            usage={
                "prompt": int(getattr(u, "input_tokens", 0) or 0),
                "completion": int(getattr(u, "output_tokens", 0) or 0),
                "total": int(getattr(u, "input_tokens", 0) or 0) + int(getattr(u, "output_tokens", 0) or 0),
                "cached": int(getattr(u, "cache_read_input_tokens", 0) or 0) if u else 0,
                "calls": 1,
            },
            model=profile.effective_model, protocol="anthropic", raw=resp,
        )
    finally:
        await client.close()


def _to_anthropic_content(content: Any) -> Any:
    """OpenAI 风格 content → Anthropic content blocks（图片走 base64 source）。"""
    if isinstance(content, str):
        return content
    blocks: list[dict] = []
    for p in content if isinstance(content, list) else []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            blocks.append({"type": "text", "text": p.get("text", "")})
        elif p.get("type") == "image_url":
            img = _image_parts([p])
            if img and img[0].get("data"):
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": img[0]["mime"], "data": img[0]["data"]},
                })
    return blocks or [{"type": "text", "text": ""}]


async def _chat_google(
    profile, caps: Capabilities, messages: list[dict], json_mode: bool,
    temperature: float | None, max_tokens: int | None, timeout: float,
) -> ChatResult:
    from google import genai
    from google.genai import types as gtypes

    s = get_settings()
    system, rest = _split_system(messages)
    contents: list[Any] = []
    for m in rest:
        parts: list[Any] = []
        content = m.get("content")
        if isinstance(content, str):
            parts.append(gtypes.Part.from_text(text=content))
        else:
            for p in content if isinstance(content, list) else []:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append(gtypes.Part.from_text(text=p.get("text", "")))
                elif p.get("type") == "image_url":
                    imgs = _image_parts([p])
                    if imgs and imgs[0].get("data"):
                        import base64

                        parts.append(
                            gtypes.Part.from_bytes(
                                data=base64.b64decode(imgs[0]["data"]), mime_type=imgs[0]["mime"]
                            )
                        )
        contents.append(gtypes.Content(role="user", parts=parts or [gtypes.Part.from_text(text="")]))

    cfg_kwargs: dict[str, Any] = {}
    if system:
        cfg_kwargs["system_instruction"] = system
    if temperature is not None:
        cfg_kwargs["temperature"] = temperature
    if max_tokens is not None:
        cfg_kwargs["max_output_tokens"] = max_tokens
    if json_mode:
        cfg_kwargs["response_mime_type"] = "application/json"

    http = _http_for(profile, timeout)
    base = (profile.base_url or "").strip().rstrip("/")
    http_options = gtypes.HttpOptions(
        httpx_async_client=http, timeout=int(timeout * 1000),
        base_url=base or None,
    )
    client = genai.Client(api_key=profile.api_key, http_options=http_options)
    try:
        resp = await client.aio.models.generate_content(
            model=profile.effective_model,
            contents=contents,
            config=gtypes.GenerateContentConfig(**cfg_kwargs),
        )
        text = getattr(resp, "text", None)
        if text is None:
            cands = getattr(resp, "candidates", None) or []
            text = ""
            for c in cands:
                for part in getattr(getattr(c, "content", None), "parts", None) or []:
                    text += getattr(part, "text", "") or ""
        um = getattr(resp, "usage_metadata", None)
        prompt = int(getattr(um, "prompt_token_count", 0) or 0) if um else 0
        completion = int(getattr(um, "candidates_token_count", 0) or 0) if um else 0
        return ChatResult(
            text=text,
            usage={
                "prompt": prompt, "completion": completion, "total": prompt + completion,
                "cached": int(getattr(um, "cached_content_token_count", 0) or 0) if um else 0,
                "calls": 1,
            },
            model=profile.effective_model, protocol="google", raw=resp,
        )
    finally:
        try:
            await client.aio.aclose()
        except Exception:  # noqa: BLE001 — 关闭失败不影响结果
            pass
        await http.aclose()


# ---------------------------------------------------------------------------
# Streaming implementations per protocol
# ---------------------------------------------------------------------------

async def _chat_openai_stream(
    profile, caps: Capabilities, messages: list[dict], json_mode: bool,
    temperature: float | None, max_tokens: int | None, timeout: float,
    force_base_url: str | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    from openai import AsyncOpenAI

    s = get_settings()
    msgs = [dict(m) for m in messages]
    rf: dict | None = None
    if json_mode:
        if caps.json_schema:
            rf = {"type": "json_object"} if caps.json_object else None
        elif caps.json_object:
            rf = {"type": "json_object"}
        if rf is None:
            _append_hint(msgs, _JSON_ONLY_HINT)

    body: dict[str, Any] = {"model": profile.effective_model, "messages": msgs}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if rf is not None:
        body["response_format"] = rf

    http = _http_for(profile, timeout)
    client = AsyncOpenAI(
        base_url=force_base_url or profile.base_url,
        api_key=profile.api_key or "not-needed",
        http_client=http,
        max_retries=s.llm_max_retries,
    )
    usage = empty_usage()
    try:
        # No stream_options: many compatible gateways reject it with 400.
        # Missing usage only zeroes the token ledger; text is unaffected.
        stream = await client.chat.completions.create(**body, stream=True)
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                u = getattr(chunk, "usage", None)
                if u is not None and not usage.get("calls"):
                    usage = _usage_from_openai(chunk)
                continue
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None) if delta is not None else None
            if text:
                yield ChatStreamEvent(text=text)
        yield ChatStreamEvent(
            done=True, usage=usage, model=profile.effective_model, protocol="openai",
        )
    finally:
        await client.close()


async def _chat_anthropic_stream(
    profile, caps: Capabilities, messages: list[dict], json_mode: bool,
    temperature: float | None, max_tokens: int | None, timeout: float,
) -> AsyncIterator[ChatStreamEvent]:
    from anthropic import AsyncAnthropic

    s = get_settings()
    system, rest = _split_system(messages)
    if json_mode:
        system = (system + _JSON_ONLY_HINT).strip()
    conv: list[dict] = []
    for m in rest:
        conv.append({"role": m.get("role", "user"), "content": _to_anthropic_content(m.get("content"))})
    kwargs: dict[str, Any] = {
        "model": profile.effective_model,
        "max_tokens": max_tokens or s.gateway_max_tokens or 4096,
        "messages": conv,
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature

    http = _http_for(profile, timeout)
    client = AsyncAnthropic(
        api_key=profile.api_key, base_url=profile.base_url or None,
        http_client=http, max_retries=s.llm_max_retries,
    )
    try:
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    yield ChatStreamEvent(text=text)
            final = await stream.get_final_message()
        u = getattr(final, "usage", None)
        prompt = int(getattr(u, "input_tokens", 0) or 0) if u else 0
        completion = int(getattr(u, "output_tokens", 0) or 0) if u else 0
        yield ChatStreamEvent(
            done=True,
            usage={
                "prompt": prompt, "completion": completion, "total": prompt + completion,
                "cached": int(getattr(u, "cache_read_input_tokens", 0) or 0) if u else 0,
                "calls": 1,
            },
            model=profile.effective_model, protocol="anthropic",
        )
    finally:
        await client.close()


def _google_chunk_text(chunk: Any) -> str:
    text = getattr(chunk, "text", None)
    if isinstance(text, str):
        return text
    out = ""
    for c in getattr(chunk, "candidates", None) or []:
        for part in getattr(getattr(c, "content", None), "parts", None) or []:
            out += getattr(part, "text", "") or ""
    return out


async def _chat_google_stream(
    profile, caps: Capabilities, messages: list[dict], json_mode: bool,
    temperature: float | None, max_tokens: int | None, timeout: float,
) -> AsyncIterator[ChatStreamEvent]:
    from google import genai
    from google.genai import types as gtypes

    s = get_settings()
    system, rest = _split_system(messages)
    contents: list[Any] = []
    for m in rest:
        parts: list[Any] = []
        content = m.get("content")
        if isinstance(content, str):
            parts.append(gtypes.Part.from_text(text=content))
        else:
            for p in content if isinstance(content, list) else []:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append(gtypes.Part.from_text(text=p.get("text", "")))
                elif p.get("type") == "image_url":
                    imgs = _image_parts([p])
                    if imgs and imgs[0].get("data"):
                        import base64

                        parts.append(
                            gtypes.Part.from_bytes(
                                data=base64.b64decode(imgs[0]["data"]), mime_type=imgs[0]["mime"]
                            )
                        )
        contents.append(gtypes.Content(role="user", parts=parts or [gtypes.Part.from_text(text="")]))

    cfg_kwargs: dict[str, Any] = {}
    if system:
        cfg_kwargs["system_instruction"] = system
    if temperature is not None:
        cfg_kwargs["temperature"] = temperature
    if max_tokens is not None:
        cfg_kwargs["max_output_tokens"] = max_tokens
    if json_mode:
        cfg_kwargs["response_mime_type"] = "application/json"

    http = _http_for(profile, timeout)
    base = (profile.base_url or "").strip().rstrip("/")
    http_options = gtypes.HttpOptions(
        httpx_async_client=http, timeout=int(timeout * 1000), base_url=base or None,
    )
    client = genai.Client(api_key=profile.api_key, http_options=http_options)
    usage = empty_usage()
    try:
        stream = await client.aio.models.generate_content_stream(
            model=profile.effective_model,
            contents=contents,
            config=gtypes.GenerateContentConfig(**cfg_kwargs),
        )
        async for chunk in stream:
            text = _google_chunk_text(chunk)
            if text:
                yield ChatStreamEvent(text=text)
            um = getattr(chunk, "usage_metadata", None)
            if um is not None:
                prompt = int(getattr(um, "prompt_token_count", 0) or 0)
                completion = int(getattr(um, "candidates_token_count", 0) or 0)
                usage = {
                    "prompt": prompt, "completion": completion, "total": prompt + completion,
                    "cached": int(getattr(um, "cached_content_token_count", 0) or 0),
                    "calls": 1,
                }
        yield ChatStreamEvent(
            done=True, usage=usage, model=profile.effective_model, protocol="google",
        )
    finally:
        try:
            await client.aio.aclose()
        except Exception:  # noqa: BLE001 — close failure must not mask the result
            pass
        await http.aclose()
