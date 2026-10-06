"""用 LLM 给**历史失败**补上 root_cause（回填），让清单能真正合并。

## 为什么需要回填

实测（2026-10-06）：119 条历史失败里 **root_cause 全是 NULL**，
而 10-04 之后跑的 6 条里 5 条有值。原因是分类功能 2026-10-04 才加，
之前的结果没人补。

不补的后果很直接：`app/failure_digest.py` 只能按确定性信号分组，
这 119 条全部落进 `singleton` → **119 条失败聚成 117 条，一条都合并不了**。
用户明确要求「相同原因的可以合并」，所以必须补。

## 为什么用 LLM 而不是规则

这批数据里确实藏着可判定的共同点（真缺陷、列名不符、状态被重置…），
但要靠规则把它们准确挑出来，得写几十条正则并逐条验证 ——
而**写错的规则会静默把不同根因合并**，那比不合并更糟。
LLM 在"读懂一句话里的因果"这件事上比正则可靠得多。

## 防错：LLM 只做分类，不做判断

1. **白名单强制**：不在 `_VALID_ROOT_CAUSES` 里的输出直接丢成 `unclear`，
   不让模型自造的分类污染清单口径（沿用 judge.py 的既有做法）。
2. **不给它合并的权限**：分类完仍由 `build_groups` 按 signal 分组。
   LLM 改措辞可以，改分组不行。
3. **要证据**：每条必须引用 `judge_reason` 里的原句，
   模型给不出原句就说明它在编，这类直接降级为 `unclear`。
4. **不阻塞清单生成**：LLM 不可用时清单照常出，只是没历史分类。
   （用户这条是硬要求：清单不能因为外部依赖挂了就出不来。）

## 成本

119 条，一次性。若分批（默认 20/批）便于观察中途输出。
每条只发judge_reason 摘要 + 用例名，不发截图 —— 分类不需要看图。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.judge import _ROOT_CAUSES

# 复用 judge.py 的白名单，不另立一套 —— 两处口径分裂比缺分类更麻烦。
VALID_CAUSES = frozenset(_ROOT_CAUSES)

_SYSTEM = (
    "You classify why automated browser test cases failed. "
    "The user message is a JSON **array**; classify EVERY element and return ONE entry per input. "
    "Pick EXACTLY ONE cause key per element. Judge only from that element's `reason` text — "
    "never guess beyond it.\n"
    + "\n".join(f"  {k:<22} — {v}" for k, v in _ROOT_CAUSES.items())
    + "\n\nReply with JSON only, in exactly this shape:\n"
    '{"items": [{"id": <the id you were given>, "cause": "<key>", '
    '"quote": "<a short verbatim fragment copied from that reason>"}]}\n'
    "Rules:\n"
    "- Keep every input `id` unchanged so results can be matched back.\n"
    "- The quote MUST be copied verbatim from that element's `reason`. "
    "If you cannot find a supporting fragment, use cause=\"unclear\" and an empty quote — "
    "a wrong classification sends a human to investigate something that is not the problem."
)

# 引用校验前要把标点与空白剥掉，否则模型在引文里改了标点就被判"编造"。
#
# ★踩过的坑：原来的正则写成 `[\w一-鿿、，。]+`（保留标点、意图保留正文），
# 结果把**中文正文全滤掉了** —— 因为那套写法在 Python 里字符类方向反了。
# 于是每条quote 归一后长度都是 0 → 触发"太短无法作为依据" → 全被降级成 unclear，
# 白白花掉一次 LLM 调用。教训：正则里"保留什么"要想清楚，别凭语感写。
# 现在明确剥掉标点空白、保留正文（含中文；Python 的 \w 本就含 CJK）。
_PUNCT_ONLY = re.compile(
    r"[\s，。：；、！？「」『』（）()《》〈〉【】\[\]{}“”‘’\"'"
    r"％%\-—…·|/\\]+"
)


def _strip_punct(text: str) -> str:
    return _PUNCT_ONLY.sub("", text or "")


def _quote_is_real(quote: str, reason: str) -> bool:
    """quote 必须能在原文里找到（允许标点与空白差异）。

    长度门槛 6 个正文字符：太短的片段（如"不符""未达成"）谁都写得出来，
    不足以证明分类有依据。
    """
    nq = _strip_punct(quote)
    if len(nq) < 6:
        return False
    return nq in _strip_punct(reason)


async def backfill_root_causes(
    rows: list[dict[str, Any]],
    batch_size: int = 20,
) -> dict[int, str]:
    """给 root_cause 为空的历史失败补分类。返回 {result_id: cause}。

    rows 至少需要 id / judge_reason / case_key / name。
    任何异常都不外抛 —— 清单必须在LLM 不可用时照常生成。
    """
    pending = [r for r in rows if not (r.get("root_cause") or "").strip()]
    if not pending:
        return {}

    try:
        from app.llm import llm_config, openai_client

        cfg = await llm_config()
        client = await openai_client()
    except Exception as exc:  # noqa: BLE001
        _log("backfill 跳过：LLM 客户端不可用（%s）", exc)
        return {}

    out: dict[int, str] = {}
    try:
        for i in range(0, len(pending), batch_size):
            batch = pending[i:i + batch_size]
            payload = [
                {
                    "id": r.get("id"),
                    "case": r.get("case_key") or r.get("name") or "",
                    "reason": (r.get("judge_reason") or r.get("error") or "")[:400],
                }
                for r in batch
            ]
            try:
                resp = await client.chat.completions.create(
                    model=cfg.model,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": json.dumps(
                            [{"id": p["id"], "case": p["case"], "reason": p["reason"]}
                             for p in payload], ensure_ascii=False)},
                    ],
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                data = json.loads(resp.choices[0].message.content or "{}")
            except Exception as exc:  # noqa: BLE001 — 单批失败不拖垮整体
                _log("backfill 第 %d 批失败：%s", i // batch_size + 1, exc)
                continue

            # 兼容 {"items":[...]} 与 {"id":..,"cause":..} 两种返回形状
            items = data.get("items")
            if not isinstance(items, list):
                items = [data] if data.get("cause") else []
            by_id = {r.get("id"): r for r in batch}
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                row = by_id.get(rid)
                if row is None:
                    continue
                cause = str(it.get("cause") or "").strip()
                quote = str(it.get("quote") or "").strip()
                if cause not in VALID_CAUSES:
                    cause = "unclear"
                elif cause != "unclear" and not _quote_is_real(quote, row.get("judge_reason") or ""):
                    #给不出真实引用的分类不可信 —— 这是模型在编依据
                    cause = "unclear"
                out[int(rid)] = cause
            _log("backfill 进度 %d/%d", min(i + batch_size, len(pending)), len(pending))
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _log(msg: str, *args) -> None:
    try:
        import logging

        logging.getLogger("potato-test.failure_digest").warning(msg, *args)
    except Exception:  # noqa: BLE001
        pass
