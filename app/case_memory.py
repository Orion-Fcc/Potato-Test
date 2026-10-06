"""操作经验记忆：让同一条用例越跑越稳，但**绝不允许背答案**。

问题：同一条用例两次运行结果不一样。原因是每次都要重新摸索界面 ——
从哪进、哪个按钮在哪、页面加载要多久、哪个弹窗里的按钮挨得近容易点错。
人做第二遍时会记住这些，机器却每次都从零开始。

所以给每条用例存一份"经验笔记"：**只记过程和界面知识**，下次注入提示词里。

────────────────────────────────────────────────────────────────────────
红线（这是这个模块存在的意义，也是它最容易变质的地方）
────────────────────────────────────────────────────────────────────────
**绝不记录判定结果**：不记"通过/失败"、不记"是否符合预期"、不记"结论"。
一旦把结论写进记忆，下次运行就变成"背答案"，测试本身就失去意义了。

这条不靠自觉，由三层机制保证：
  1. **字段白名单**（`_ALLOWED_KEYS`）：只有 navigation / page_notes / element_notes
     三类能进记忆，模型多输出的一律丢弃；
  2. **文案过滤**（`_looks_like_verdict`）：任何字段值里出现判定性措辞就整条丢弃；
  3. **注入时再声明一次**：明确告诉模型"本次是否达成预期，只能依据本次实际观察"。
   —— 三层里任何一层单独都不够，叠起来才挡得住模型想走捷径的倾向。

另外 `memory_fingerprint` 保证**用例一改、记忆立即作废**，不会拿旧经验误导新用例。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("potato-test.case_memory")

# 记忆里只允许这三类内容。模型若返回别的键，一律丢弃。
_ALLOWED_KEYS = ("navigation", "page_notes", "element_notes")

# 每类最多留几条、每条多长（防止记忆无限膨胀，也防止它变成变相的日志回放）
_MAX_ITEMS = 8
_MAX_CHARS = 160

# 判定性措辞。出现在记忆值里说明模型在偷偷记结论 —— 整条丢弃，宁缺毋滥。
_VERDICT_TOKENS = (
    "通过", "不通过", "失败", "成功", "符合预期", "不符合预期", "达成", "未达成",
    "pass", "passed", "fail", "failed", "verdict", "expected_met", "not met",
    "bug", "缺陷", "断言", "assert",
)


def fingerprint(case: Any) -> str:
    """用例内容指纹。用例被编辑后指纹变化 → 记忆作废。

    只把**真正影响执行方式**的字段纳入指纹：名称、任务、步骤、预期、测试数据、起始地址。
    刻意不含 priority/owner/tags 这类元信息 —— 改个标签不该让积累的经验全废掉。
    """
    parts = []
    for field in ("name", "prompt", "expected", "test_data", "start_url"):
        parts.append(str(getattr(case, field, "") or ""))
    # steps 是结构化列表，序列化时排序保证稳定
    steps = getattr(case, "steps", None) or []
    try:
        parts.append(json.dumps(steps, ensure_ascii=False, sort_keys=True))
    except Exception:  # noqa: BLE001
        parts.append(str(steps))
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _looks_like_verdict(text: str) -> bool:
    low = text.lower()
    return any(tok in low for tok in _VERDICT_TOKENS)


def sanitize(raw: Any) -> dict | None:
    """把模型输出洗成合法记忆。任何可疑内容直接丢弃该条，整份洗不出东西就返回 None。"""
    if not isinstance(raw, dict):
        return None
    out: dict[str, list[str]] = {}
    for key in _ALLOWED_KEYS:
        val = raw.get(key)
        if val is None:
            continue
        items = val if isinstance(val, list) else [val]
        kept: list[str] = []
        for it in items:
            s = str(it).strip()
            if not s:
                continue
            if _looks_like_verdict(s):
                log.warning("case_memory: 丢弃含判定措辞的条目（不记结论是硬要求）：%s", s[:60])
                continue
            kept.append(s[:_MAX_CHARS])
        if kept:
            out[key] = kept[:_MAX_ITEMS]
    return out or None


def is_fresh(memory: Any, fp: str, stored_fp: str | None) -> bool:
    """记忆是否可用于本次运行：指纹必须一致。"""
    return bool(memory) and bool(stored_fp) and stored_fp == fp


def render(memory: dict | None, fp: str, stored_fp: str | None) -> str:
    """把记忆渲染成注入提示词的文本。不可用/为空时返回空串。

    最后那段"使用方式"是必要的：只给经验而不划清界限，模型很容易把
    "上次看到 X" 当成 "这次也是 X"，那等于绕一圈又把结论喂回去了。
    """
    if not is_fresh(memory, fp, stored_fp):
        return ""
    labels = {
        "navigation": "上次的导航路径",
        "page_notes": "页面特性",
        "element_notes": "元素注意事项",
    }
    lines: list[str] = []
    for key in _ALLOWED_KEYS:
        items = (memory or {}).get(key) or []
        if items:
            lines.append(f"- {labels[key]}：" + "；".join(str(x) for x in items))
    if not lines:
        return ""
    return (
        "【你的经验笔记 —— 来自你之前执行这条用例时的观察】\n"
        + "\n".join(lines)
        + "\n【怎么用这份笔记】它只帮你少走弯路、少重新摸索（比如知道从哪进、哪个按钮挨得近容易点错）。"
        "**本次是否达成预期，必须完全依据你本次运行实际观察到的东西来判断。**"
        "不要因为笔记里提到过某个界面或某条路径，就假定这次也是那样 —— 页面可能已经改了。"
        "笔记与本次实际观察冲突时，一律以本次观察为准。"
    )


async def note_for_case(case_id: int, timeout_s: float = 10.0) -> str:
    """给某条用例取出可直接拼进提示词的经验笔记。**永不抛异常**，拿不到就返回空串。

    这是 executor 唯一需要调用的入口 —— 把"读库 + 校验指纹 + 渲染"都收在这里，
    executor 那边只加一行 `+ await note_for_case(spec.case_id)`，
    避免让记忆逻辑散落到执行流程里（也避免它有任何机会影响控制流）。
    """
    if not case_id:
        return ""
    try:
        from sqlalchemy import select

        from app.db import db_session
        from app.models import TestCase

        async with asyncio.timeout(timeout_s):
            async with db_session() as db:
                case = await db.get(TestCase, case_id)
                if case is None:
                    return ""
                fp = fingerprint(case)
                return render(case.memory, fp, case.memory_fingerprint)
    except Exception as exc:  # noqa: BLE001
        log.warning("case_memory: 读取经验笔记失败（不影响执行）：%s: %s", type(exc).__name__, exc)
        return ""


async def remember_case(case_id: int, steps: list[dict], timeout_s: float = 45.0) -> bool:
    """把本次运行的过程提炼成经验并写回该用例。成功返回 True。

    调用时机：一条用例跑完之后（engine 侧）。失败只是没有积累到经验，绝不影响结果。
    指纹按**当前**用例内容重新算并存下 —— 之后用户若改了用例，指纹就对不上了。
    """
    if not case_id or not steps:
        return False
    try:
        from app.db import db_session
        from app.models import TestCase

        async with db_session() as db:
            case = await db.get(TestCase, case_id)
            if case is None:
                return False
            case_name = case.name or ""
            new_memory = await summarize(case_name, steps, timeout_s=timeout_s)
            if not new_memory:
                return False
            case.memory = new_memory
            case.memory_fingerprint = fingerprint(case)
            case.memory_updated_at = datetime.now(UTC)
            await db.flush()
        log.info("case_memory: 已更新 case %s 的经验笔记（%d 类）", case_id, len(new_memory))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("case_memory: 写入经验笔记失败（不影响结果）：%s: %s", type(exc).__name__, exc)
        return False


async def clear_case(case_id: int) -> bool:
    """清空某条用例的记忆（用例大改后想让 agent 完全从零开始时用）。"""
    try:
        from app.db import db_session
        from app.models import TestCase

        async with db_session() as db:
            case = await db.get(TestCase, case_id)
            if case is None:
                return False
            case.memory = None
            case.memory_fingerprint = None
            case.memory_updated_at = None
            await db.flush()
        return True
    except Exception:  # noqa: BLE001
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 2026-10-04 跨用例经验检索（用户要求"上下文按需检索(RAG)"）
# ─────────────────────────────────────────────────────────────────────────────
#
# 问题：现有的经验笔记是**按用例**存的，一条用例第一次跑时手上什么都没有 ——
# 而它要摸索的东西（这个系统页面加载慢、这两个按钮挨得近容易点错）其实
# 隔壁那条用例早就记下来过。563 条用例里大量这种重复摸索。
#
# 为什么不干脆把所有记忆都塞进提示词：563 条用例的记忆合起来远超上下文，
# 而且绝大部分与当前用例无关，塞进去既慢又稀释注意力 —— 这正是"按需检索"要解决的。
#
# 三条设计约束（都很关键）：
#  1. **只检索同一个项目**。跨项目检索会让用例重新耦合，与项目隔离要求冲突。
#  2. **只共享 page_notes / element_notes，不共享 navigation**。
#     navigation 是"这条用例从哪进到哪"的专属路径；拿到另一条用例身上是错的
#     （不同用例入口不同、也可能绕过中间页），会把 agent 带偏。
#     page_notes（页面脾气）和 element_notes（元素坑）才是可迁移的界面知识。
#  3. **只用新鲜记忆**。指纹对不上的记忆是用例改版前的残留，用它等于喂错信息。
#
# 检索方式用**字符二元组重合度**，不是按空格分词 —— 中文没有词边界，
# 按空格分会把"资源审批配置"整段当一个 token，任何改写都对不上。
# 二元组对中文的容错好得多，且不需要引入分词库。

_PAGE_KEYS = ("page_notes", "element_notes")


def _bigrams(text: str) -> set[str]:
    """字符二元组集合。中文用它代替分词。

    先去掉标点与空白，再取相邻两字的组合；单字串退化为它自己
    （否则长度 1 的文本会得到空集合，永远匹配不上）。
    """
    s = "".join(ch for ch in (text or "") if ch.isalnum())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def score_relevance(query: str, text: str) -> float:
    """一条候选经验与查询文本的相关度，0..1。

    用「命中查询的比例」而不是 Jaccard：候选句子通常比查询短，
    Jaccard 会被长度差异严重压低 —— 一句精准命中关键词的短提示
    反而得分低于一句冗长但只沾边的，那是错的排序。
    """
    q = _bigrams(query)
    if not q:
        return 0.0
    t = _bigrams(text)
    if not t:
        return 0.0
    return len(q & t) / len(q)


async def related_notes(
    project_id: int,
    query: str,
    exclude_case_id: int | None = None,
    k: int = 6,
    min_score: float = 0.12,
    timeout_s: float = 10.0,
) -> str:
    """从同项目其他用例的经验里，取与本用例最相关的若干条，渲染成可注入的文本。

    **永不抛异常**：检索失败只是没有额外经验，绝不能影响执行。

    为什么要有 min_score：相关度太低的经验就是噪音。宁可什么都不给
    （agent 照常自己摸索），也不要塞一堆无关的"注意事项"让它分心。
    """
    if not project_id or not (query or "").strip():
        return ""
    try:
        from sqlalchemy import select

        from app.db import db_session
        from app.models import TestCase

        async with asyncio.timeout(timeout_s):
            async with db_session() as db:
                rows = (
                    (
                        await db.execute(
                            select(TestCase).where(
                                TestCase.project_id == project_id,
                                TestCase.memory.is_not(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

        # 打分：把每条候选笔记单独算分，而不是按用例整体算 ——
        # 一条用例里可能只有一句与本用例相关，整条算分会被无关句子拉低。
        scored: list[tuple[float, str]] = []
        for c in rows:
            if exclude_case_id and c.id == exclude_case_id:
                continue  # 自己的记忆由 note_for_case 单独注入，不在这里重复
            if not is_fresh(c.memory, fingerprint(c), c.memory_fingerprint):
                continue  # 指纹过期 = 用例改过，旧经验不可信
            mem = c.memory or {}
            for key in _PAGE_KEYS:
                for item in mem.get(key) or []:
                    text = str(item).strip()
                    if not text:
                        continue
                    # 二次防线：万一历史记忆里混进了判定性措辞（旧版本写入的），
                    # 这里再挡一次。记忆系统的红线是"绝不背答案"。
                    if _looks_like_verdict(text):
                        continue
                    s = score_relevance(query, text)
                    if s >= min_score:
                        scored.append((s, text))

        if not scored:
            return ""

        # 同分时按文本排序，保证输出稳定（否则提示词每次都变，缓存与复现都受影响）
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = [t for _s, t in scored[:k]]

        # 逐条去重：不同用例可能记了同一句话
        seen: set[str] = set()
        uniq: list[str] = []
        for t in picked:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        if not uniq:
            return ""

        return (
            "【同一项目其他用例积累的界面经验（按相关度挑选，可能与你这条用例无关，自行判断）】\n"
            + "\n".join(f"- {t}" for t in uniq)
            + "\n【怎么用】这些只是别人踩过的界面坑，用来少走弯路。"
            "**它们描述的不是你这次要测的对象，也不是任何执行结果。**"
            "与你本次实际观察冲突时，一律以本次观察为准；无用就忽略。"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("case_memory: 跨用例检索失败（不影响执行）：%s: %s", type(exc).__name__, exc)
        return ""


_SUMMARY_SYSTEM = """你在帮一个自动化测试 Agent 积累"操作经验"。

给你的是它刚才执行某条测试用例的过程记录（每步的思考、动作、页面返回）。
请提炼出**下次能少走弯路**的经验，输出一个 JSON 对象：

{
  "navigation": ["从哪进到哪的路径，按顺序，用界面上的中文菜单名，例如：进入培训资源管理 → 资源审批配置"],
  "page_notes": ["这个页面有什么脾气，例如：列表页加载约 6-8 秒，期间只显示「正在加载中」"],
  "element_notes": ["元素上的坑，例如：「下一步」与「取消」相邻且样式接近，点击前先确认按钮文案"]
}

**极其重要的限制 —— 违反则整份作废**：
1. **绝对不要写这条用例是否通过、是否成功、是否符合预期、有没有缺陷。**
   也不要用"通过/失败/符合/达成/缺陷/断言"这类词。
   你的任务只是记"路怎么走、界面什么样"，不是记"结果对不对"。
2. 不要复述整个过程，只留**有复用价值**的结论性经验。每类最多 4 条，每条不超过 60 字。
3. 只写你**确实观察到**的东西。没把握就不写，宁可少写也不要编。
4. 界面原文（按钮名、提示文案）要照抄，不要改写。
"""


async def summarize(
    case_name: str,
    steps: list[dict],
    timeout_s: float = 45.0,
) -> dict | None:
    """从本次运行的过程记录里提炼经验。**永不抛异常** —— 记忆是好东西，但不能搞死用例。

    显式超时：外部 LLM 调用必须有上限（这是硬约束，不是可选项）。
    """
    if not steps:
        return None
    try:
        from app.llm import llm_config, openai_client

        trace_lines: list[str] = []
        for d in steps:
            if not isinstance(d, dict):
                continue
            bits = []
            for label, key in (("思考", "thought"), ("动作", "action"), ("结果", "result")):
                v = str(d.get(key) or "").strip()
                if v:
                    bits.append(f"{label}={v[:200]}")
            if bits:
                trace_lines.append(f"第{d.get('i')}步: " + " | ".join(bits))
        if not trace_lines:
            return None
        user = f"用例名称：{case_name}\n本次执行过程：\n" + "\n".join(trace_lines[:60])

        cfg = await llm_config()
        client = await openai_client()
        try:
            async def ask() -> Any:
                resp = await client.chat.completions.create(
                    model=cfg.model,
                    messages=[
                        {"role": "system", "content": _SUMMARY_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                return json.loads(resp.choices[0].message.content or "{}")

            raw = await asyncio.wait_for(ask(), timeout=timeout_s)
        finally:
            await client.close()
        return sanitize(raw)
    except asyncio.TimeoutError:
        log.warning("case_memory: 记忆提炼超时（%.0fs），本条跳过", timeout_s)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("case_memory: 记忆提炼失败（不影响用例结果）：%s: %s", type(exc).__name__, exc)
        return None
