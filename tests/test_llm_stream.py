"""流式门面的回归测试（2026-10-20）。

不测具体 SDK：那需要真实网关。这里钉住三件容易写错的事：
1. 首字之前 429 才换 profile（首字之后换会把两个模型的文字拼在一起）；
2. 非 429 错误直接抛，不在池里乱跳；
3. `chat_stream` 按协议分派，文本块按到达顺序原样给出。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import llm_chat


class _RateLimited(Exception):
    pass


def _profile(name: str):
    return SimpleNamespace(name=name, effective_model=name, base_url=None, api_key="k")


async def test_pool_switches_only_before_first_token(monkeypatch):
    profiles = [_profile("a"), _profile("b")]
    monkeypatch.setattr(llm_chat, "_pool", None, raising=False)
    monkeypatch.setattr(
        llm_chat, "chat_stream", _fake_stream_factory({"a": _RateLimited("429"), "b": "ok"})
    )

    def _is_rate_limited(exc: Exception) -> bool:
        return isinstance(exc, _RateLimited)

    # chat_stream_with_pool 内部通过 app.llm 访问池和判定
    from app import llm as llm_mod

    async def _pool_for_role(role):
        return profiles

    monkeypatch.setattr(llm_mod, "pool_for_role", _pool_for_role)
    monkeypatch.setattr(llm_mod, "_is_rate_limited", _is_rate_limited, raising=False)

    out = [ev.text async for ev in llm_chat.chat_stream_with_pool(role="docgen") if ev.text]
    assert out == ["ok"]


async def test_pool_does_not_switch_after_text(monkeypatch):
    profiles = [_profile("a"), _profile("b")]

    async def _stream(profile, **kwargs):
        yield llm_chat.ChatStreamEvent(text="半句话")
        raise _RateLimited("429 mid-stream")

    from app import llm as llm_mod

    async def _pool_for_role(role):
        return profiles

    def _is_rate_limited(exc: Exception) -> bool:
        return isinstance(exc, _RateLimited)

    monkeypatch.setattr(llm_chat, "chat_stream", _stream)
    monkeypatch.setattr(llm_mod, "pool_for_role", _pool_for_role)
    monkeypatch.setattr(llm_mod, "_is_rate_limited", _is_rate_limited, raising=False)

    got: list[str] = []
    with pytest.raises(_RateLimited):
        async for ev in llm_chat.chat_stream_with_pool(role="docgen"):
            if ev.text:
                got.append(ev.text)
    assert got == ["半句话"], "已经吐字后不能再换模型续写"


async def test_chat_stream_dispatches_by_protocol_and_forwards_text(monkeypatch):
    async def _openai_stream(*args, **kwargs):
        yield llm_chat.ChatStreamEvent(text="flowchart TD\n")
        yield llm_chat.ChatStreamEvent(text="A-->B")
        yield llm_chat.ChatStreamEvent(done=True, usage={"calls": 1})

    async def _caps(profile):
        return SimpleNamespace(protocol="openai")

    monkeypatch.setattr(llm_chat, "capabilities_for", _caps)
    monkeypatch.setattr(llm_chat, "_chat_openai_stream", _openai_stream)

    chunks = [
        ev.text async for ev in llm_chat.chat_stream(_profile("m"), system="s", user="u")
    ]
    assert chunks == ["flowchart TD\n", "A-->B", ""]
    assert "".join(chunks) == "flowchart TD\nA-->B"


def _fake_stream_factory(script: dict[str, str | Exception]):
    async def _stream(profile, **kwargs):
        step = script.get(profile.name)
        if isinstance(step, Exception):
            raise step
        yield llm_chat.ChatStreamEvent(text=step or "")
        yield llm_chat.ChatStreamEvent(done=True)

    return _stream
