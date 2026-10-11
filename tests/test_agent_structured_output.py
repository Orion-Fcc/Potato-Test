"""钉住「换模型不改代码」这条约定：协议自动识别、能力自适应、结构化输出降级。

背景（2026-10-10，run 17 / TP-CCR-001 + TP-CCR-005）：网关拒
`response_format={"type":"json_schema"}`（400 `This response_format type is
unavailable now`），而 browser-use 的 ChatOpenAI 默认**每一步**都发这个字段 ——
导航成功后每一步全 400，用例被判"只导航、零动作"。

同一时期的第二层诉求："以后会换别的 LLM，甚至可能不是 OpenAI 协议，
不想每次都改代码"。于是把"这个端点说什么协议、支持什么能力"变成**实测+缓存**的事实：

- 协议：auto 识别（base_url 线索 → openai → anthropic → google → ollama）
- 能力：json_object / json_schema / tools / vision
- 代码里只读结论，不再写死假设

这几个断言的价值在于：它们都是"看起来可以顺手删掉"的间接层，
但删掉任何一处，换厂商就又要改代码（或者更糟：静默把每次调用都打 400）。
"""

from __future__ import annotations

import inspect

from app import llm
from app.config import get_settings
from app.llm_chat import extract_json
from app.llm_protocol import (
    _rf_unsupported,  # noqa: PLC2701 — 直接钉住这条判定，它是今天误判的来源
    Capabilities,
    normalize_provider,
)


def test_structured_output_is_auto_by_default():
    """三态默认 auto：靠实测决定，而不是人工开关（人工开关就是"每次都要改代码"）。"""
    assert str(get_settings().agent_json_schema).lower() == "auto"


def test_agent_factory_is_protocol_aware_and_capability_driven():
    """代理客户端由协议分发，结构化输出由能力决定。

    `browser_use_llm` 是单套配置入口，`agent_llm_for` 是多套池入口，
    两者都必须落到同一个构建器 —— 只改一个就会留下"某条路径每步 400"的坑。
    """
    for fn in (llm.browser_use_llm, llm.agent_llm_for):
        src = inspect.getsource(fn)
        assert "_build_agent_llm" in src, f"{fn.__name__} 绕过了协议分发"

    builder = inspect.getsource(llm._build_agent_llm)
    # 四种协议各有对应实现
    for cls in ("ChatAnthropic", "ChatGoogle", "ChatOllama", "ChatOpenAI"):
        assert cls in builder, f"构建器缺少 {cls} 分支"
    # 结构化输出跟着能力走，而不是写死 true/false
    assert "caps.json_schema" in builder
    assert "dont_force_structured_output" in builder
    # 429 不交给传输层盲目重试（browser-use 默认 5 次退避，实测是最大的时间黑洞）
    assert "max_retries=s.llm_max_retries" in builder


def test_tolerant_mode_when_gateway_rejects_json_schema():
    """网关不支持 json_schema 时必须关掉强制结构化输出（今天的现场）。"""
    caps = Capabilities("openai", json_object=True, json_schema=False, tools=True, vision=True)
    assert caps.tolerant_structured_output is True
    caps_ok = Capabilities("openai", json_object=True, json_schema=True, tools=True, vision=True)
    assert caps_ok.tolerant_structured_output is False


def test_provider_aliases_land_on_a_protocol():
    """用户填的是熟悉的厂商名，不是协议名 —— 别名要能落到正确协议。"""
    assert normalize_provider("") == "auto"
    assert normalize_provider("auto") == "auto"
    for alias in ("deepseek", "kimi", "qwen", "zhipu", "glm", "openrouter", "siliconflow", "vllm"):
        assert normalize_provider(alias) == "openai", alias
    assert normalize_provider("claude") == "anthropic"
    assert normalize_provider("GEMINI") == "google"
    assert normalize_provider("ollama") == "ollama"
    # 认不出来的一律 auto（不报错、不阻断），交给识别流程
    assert normalize_provider("某个没见过的网关") == "auto"


def test_prompt_word_error_is_not_a_capability_verdict():
    """DeepSeek 的 'prompt must contain the word json' 不算"不支持 json_object"。

    误判的代价是真实的：判定器会白白从原生 JSON 模式降级成提示词模式，
    而它其实完全可以用原生模式。今天真的踩到了（探测把 json_object 报成 False）。
    """
    prompt_issue = (
        'Error code: 400 - {"error":{"message":"Prompt must contain the word \'json\' '
        'in some form to use \'response_format\' of type \'json_object\'."}}'
    )
    assert _rf_unsupported(prompt_issue) is False
    unavailable = (
        '{"error":{"message":"This response_format type is unavailable now '
        '(request_id: 9fc3637d)"}}'
    )
    assert _rf_unsupported(unavailable) is True
    assert _rf_unsupported("some unrelated 400") is False


def test_json_extraction_survives_fences_and_prose():
    """非 OpenAI 协议没有原生 JSON 模式，模型会把 JSON 包进代码块或加一句开场白。

    直接 json.loads 会让一次成功的调用被记成失败 —— 内容其实就在那儿。
    """
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('好的，结果如下：{"a": [1, 2]} 完成') == {"a": [1, 2]}
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json("完全不是 JSON", default={"fallback": True}) == {"fallback": True}
    assert extract_json("") is None


def test_chat_facade_is_reachable_from_the_llm_module():
    """调用方只 import app.llm —— 门面必须在同一个入口暴露，否则又会散回去。"""
    for name in ("chat", "chat_with_pool", "ChatResult", "capabilities_for", "active_profile"):
        assert hasattr(llm, name), name
