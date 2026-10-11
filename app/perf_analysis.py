"""用例性能异常分析（2026-10-09）。

## 用户要的是什么

原话：「agent 也需要分析性能，比如该测试用例本不应该花那么长时间，但是他花了很长时间，
说明该用例也有轻微的问题，性能不够好」。

也就是说：**"跑对了"不等于"没问题"**。一条用例通过了但用了 15 分钟，
本身就是一条要报出去的信号 —— 要么被测系统有性能问题，要么这条用例写得太啰嗦
（每一步都要等页面、或者路线绕远）。而在此之前的判定只看"结果对不对"，看不到这个维度。

## 为什么不做"和历史基线比"

直觉上最合理的是"和这条用例自己上次的耗时比"，但**实测数据不支持**：
真库里 1203 条用例只有 133 条带耗时分解，绝大多数用例只跑过 0~1 次。
对只跑过一次的用例，"历史基线"就是它自己，比值恒等于 1 —— 报不出任何东西。

所以这里用**同一轮内部**的比较，它对数据量没有要求、立刻可用：

1. **离群**：某条用例的**每步耗时**明显高于同一轮其它用例。
   用每步而不是总耗时，是因为总耗时天然随步数变化 —— 一条 47 步的用例
   当然比 6 步的慢，那不是异常，那是它该干的活多。
2. **绝对慢**：单步超过一个很宽的天花板（实测 p90 是 19s，所以门限取 60s）。
   它抓的是"某一步卡了很久"这种形态 —— 步骤数不多、但时间全花在一步上。
3. **跑之外的开销异常**：启动/收尾/录像占比过高。这与用例难度无关，
   属于环境或机器问题，不该被当成被测系统的性能问题报出去。

## 为什么阈值取得保守

性能告警最容易变成噪音：报一次假性能问题，用户下次就不看了。
所以门限故意宽松（<3 条同类样本不判离群、倍数要 ≥3x），宁漏不误报。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field

# 判离群至少要有这么多条可比样本 —— 样本太少时"中位数"本身就是噪音。
MIN_SAMPLES = 3

# 每步耗时超过同一轮中位数的这个倍数才算离群。
# 取 3x：2x 在实测数据里会把大量正常波动报出来（per_step p50=11s、p90=19s，
# 本来就有 1.7 倍的天然离散度），3x 才是"确实不对劲"。
SLOW_FACTOR = 3.0

# 单步绝对天花板（毫秒）。实测 p90≈19s，60s 留给"慢页面/大表格"的正常空间，
# 超过它基本就是卡住了或者在某一步反复重试。
ABS_SLOW_STEP_MS = 60_000

# "跑之外"的开销占比超过这个数，算环境问题而不是被测系统性能问题。
# 实测 p50 是 3%、p90 是 49%，所以 70% 才是真的异常。
OVERHEAD_SHARE = 0.70

_OVERHEAD_KEYS = ("browser_up_ms", "wrap_up_ms", "video_ms")

# ── 成本的形状（2026-10-11 真库实测，45 条有 usage 的结果）────────────────────
#
#   总 prompt 469 万 token，缓存命中 318 万（67.9%）；未命中 150 万是全价部分。
#   单条 total：中位 7.3 万、均值 10.8 万、最大 60.8 万、最小 2.7 千。
#   单条 LLM 往返次数：中位 5、最高 33。
#
# 由此定下两条判断，脚本 scripts/measure_dom_state.py 也验过第一条：
#
#   ① **DOM 状态不是省钱的杠杆。** 把 browser-use 的 include_attributes 换成精简白名单，
#      在真实页面上省下 0% —— 因为这份属性表里真正在用的只有 id/name/type/value/
#      placeholder 那几个，砍掉它们 agent 就找不到控件了。唯一的 23% 需要用"只留 id"
#      换，不能接受。
#   ② **钱在"往返次数"上。** 最贵的那 8 条占了全轮一半 token，它们无一例外是
#      12~33 次的往返；而每次往返的输入量（约 1.6 万）在各条之间差不了多少。
#      所以"这条为什么贵"几乎总能翻译成"这条为什么跑了这么多步"。
#
# HEAVY_CALLS 取 15：中位数的 3 倍。抓的是真正拖账单的那几条，不是"比平均多一点"。
HEAVY_CALLS = 15

# 成本排行榜取前几条。取 5 是因为实测前 8 条就占了一半 —— 5 条足够看清形状，
# 排满了反而淹掉真正的信号。
COST_TOP_N = 5


@dataclass
class CostCase:
    """一条"贵"的用例：花了多少 token、跑了多少次往返、占本轮多少。"""

    case_id: int
    tokens: int
    calls: int
    share: float  # 占本轮总 token 的比例（0~1）

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "tokens": self.tokens,
            "calls": self.calls,
            "share": round(self.share * 100, 1),
        }


@dataclass
class PerfFlag:
    """一条用例的性能异常结论。"""

    case_id: int
    kind: str  # slow_outlier | slow_step | overhead | cost_heavy
    detail: str
    # 给报告/判定器看的一句话。**不写"测试失败"** —— 这条用例的结果很可能是对的，
    # 性能结论是叠加在结论之上的另一维信息，不是翻案。
    note: str = ""
    ratio: float = 0.0  # 相对同轮中位数的倍数（kind=slow_outlier 时有意义）

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "detail": self.detail,
            "note": self.note,
            "ratio": round(self.ratio, 2),
        }


@dataclass
class PerfReport:
    flags: list[PerfFlag] = field(default_factory=list)
    # 同一轮的基准，写进报告便于解释"为什么判它慢"
    baseline_per_step_ms: int = 0
    samples: int = 0
    note: str = ""
    # ---- token 与缓存（2026-10-11）------------------------------------------------
    # 为什么要有这一段：用户的诉求是"token 太花钱"。68% 的输入命中缓存意味着真正
    # 付全价的是**未命中的那部分**（每步新增的页面文本 + 冷启动的头）。
    # 只报"总 token"会让人以为省 token 就是省钱 —— 优化已命中的那部分几乎不省钱，
    # 所以这里把 cached 与 uncached 分开报，指哪打哪。
    token_prompt: int = 0
    token_cached: int = 0
    token_total: int = 0
    # LLM 往返总次数。这是**成本的真正驱动**：实测平均每次往返约 1.6 万输入 token，
    # 各条用例之间差别不大 —— 差别在次数上（中位 5 次，最高的 33 次）。
    token_calls: int = 0
    # 最费钱的几条（按本轮 token 降序取前 COST_TOP_N）。留着它才能回答
    # "要省该从哪儿下手" —— 面对 555 条用例，看排行榜比看总数有用得多。
    cost_cases: list[CostCase] = field(default_factory=list)
    escalated_cases: int = 0  # 走过两级判定复核的用例数（升级率）
    escalated_changed: int = 0  # 复核层把初判结论**改掉**的条数（改判率）

    @property
    def token_uncached(self) -> int:
        return max(0, self.token_prompt - self.token_cached)

    @property
    def cache_hit_pct(self) -> float:
        return (self.token_cached / self.token_prompt * 100) if self.token_prompt else 0.0

    @property
    def cost_top_share(self) -> float:
        """前 COST_TOP_N 条占本轮总 token 的比例（0~1）。

        这个数字比"总 token"有用：它回答"这笔钱是均匀花的还是被少数几条吃掉的"。
        实测是**高度集中**的 —— 少数几条占一半，那优化目标就不是"整体提速"，
        而是"把那几条揪出来看看为什么跑了 30 步"。
        """
        return sum(c.share for c in self.cost_cases)

    @property
    def by_case(self) -> dict[int, PerfFlag]:
        return {f.case_id: f for f in self.flags}

    def as_dict(self) -> dict:
        return {
            "baseline_per_step_ms": self.baseline_per_step_ms,
            "samples": self.samples,
            "flags": [f.as_dict() for f in self.flags],
            "note": self.note,
            "token_prompt": self.token_prompt,
            "token_cached": self.token_cached,
            "token_uncached": self.token_uncached,
            "token_total": self.token_total,
            "token_calls": self.token_calls,
            "cache_hit_pct": round(self.cache_hit_pct, 1),
            "cost_cases": [c.as_dict() for c in self.cost_cases],
            "cost_top_share": round(self.cost_top_share * 100, 1),
            "escalated_cases": self.escalated_cases,
            "escalated_changed": self.escalated_changed,
        }


def _load_timing(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        t = json.loads(raw)
        return t if isinstance(t, dict) else {}
    except (TypeError, ValueError):
        return {}


def analyse_run(results: list) -> PerfReport:
    """对**同一轮**的结果做性能异常分析。

    `results` 里每个元素只要有 `case_id` 和 `timing`（ORM 行或 dict 都行）。
    这一步是纯计算，不调模型 —— 性能结论不该再花一次 LLM 往返，
    而且确定性规则比"让模型看数字下判断"更稳定、可复现、可单测。
    """
    rows: list[tuple[int, dict]] = []
    for r in results:
        cid = r.get("case_id") if isinstance(r, dict) else getattr(r, "case_id", None)
        timing = _load_timing(r.get("timing") if isinstance(r, dict) else getattr(r, "timing", None))
        if cid is None or not timing:
            continue
        rows.append((int(cid), timing))

    rep = PerfReport(samples=len(rows))
    # token 账本与两级判定标记：纯读 timing + 最终状态，不调模型。
    _cost: list[tuple[int, int, int]] = []  # (case_id, total_tokens, calls)
    for _r in results:
        _t = _load_timing(_r.get("timing") if isinstance(_r, dict) else getattr(_r, "timing", None))
        if not _t:
            continue
        _u = _t.get("usage")
        if isinstance(_u, dict) and _u:
            _tok = int(_u.get("total") or 0)
            _calls = int(_u.get("calls") or 0)
            rep.token_prompt += int(_u.get("prompt") or 0)
            rep.token_cached += int(_u.get("cached") or 0)
            rep.token_total += _tok
            rep.token_calls += _calls
            _cid = _r.get("case_id") if isinstance(_r, dict) else getattr(_r, "case_id", None)
            if _cid is not None and _tok:
                _cost.append((int(_cid), _tok, _calls))
        if _t.get("judge_escalated"):
            rep.escalated_cases += 1
            # 改判 = 复核层把初判结论改掉了。初判结论存在 judge_escalated_from，
            # 最终结论就是这条结果的 status（复核是通过/失败已写回）。
            _from = str(_t.get("judge_escalated_from") or "")
            _final = str(
                _r.get("status") if isinstance(_r, dict) else getattr(_r, "status", "")
            ).lower()
            if _from in ("passed", "failed") and _final in ("passed", "failed"):
                if (_from == "passed") != (_final == "passed"):
                    rep.escalated_changed += 1

    # 成本排行榜（不依赖 timing 里的耗时字段，所以放在 `if not rows` 之前 ——
    # 有的轮次只有 token 账本、没有耗时分解，那种轮次同样该看得到钱花在哪儿）。
    if rep.token_total and _cost:
        top = sorted(_cost, key=lambda x: -x[1])[:COST_TOP_N]
        rep.cost_cases = [
            CostCase(case_id=cid, tokens=tok, calls=calls, share=tok / rep.token_total)
            for cid, tok, calls in top
        ]

    if not rows:
        rep.note = "这一轮没有可用的耗时分解（timing 为 NULL 的历史行），无法做性能分析。"
        return rep

    # ---- 基准：同一轮里"每步耗时"的中位数 ----
    per_step = [int(t.get("per_step_ms") or 0) for _, t in rows if t.get("per_step_ms")]
    if per_step:
        rep.baseline_per_step_ms = int(statistics.median(per_step))

    enough = len(per_step) >= MIN_SAMPLES
    if not enough:
        rep.note = (
            f"本轮只有 {len(per_step)} 条样本（少于 {MIN_SAMPLES} 条），"
            "不做离群判定 —— 样本太少时中位数本身就是噪音，报了也不可信。"
        )

    for cid, t in rows:
        total = int(t.get("total_ms") or 0)
        pstep = int(t.get("per_step_ms") or 0)
        steps = int(t.get("steps") or 0)

        # (1) 跑之外的开销异常 —— 与用例难度无关，属环境
        if total > 0:
            overhead = sum(int(t.get(k) or 0) for k in _OVERHEAD_KEYS)
            share = overhead / total
            if share >= OVERHEAD_SHARE and total >= 30_000:
                rep.flags.append(
                    PerfFlag(
                        case_id=cid,
                        kind="overhead",
                        detail=(
                            f"{total / 1000:.0f}s 里有 {overhead / 1000:.0f}s（{share * 100:.0f}%）"
                            "花在启动/收尾/录像上"
                        ),
                        note=(
                            "时间花在跑用例之外（浏览器启动 / 收尾 / 等录像），"
                            "**不是被测系统的性能问题**，要往环境或机器负载上找。"
                        ),
                        ratio=share,
                    )
                )
                continue  # 已经判定为环境问题，就不再报"慢"了，避免同一条用例报两遍

        # (2) 单步绝对慢 —— 步骤不多但卡在某一步
        if pstep >= ABS_SLOW_STEP_MS:
            rep.flags.append(
                PerfFlag(
                    case_id=cid,
                    kind="slow_step",
                    detail=f"每步平均 {pstep / 1000:.0f}s（共 {steps} 步，合计 {total / 1000:.0f}s）",
                    note=(
                        "单步耗时远超正常范围，通常是某一步在等页面/等接口，"
                        "或者在同一步里反复重试。值得看一眼那一步的页面响应。"
                    ),
                    ratio=pstep / max(1, rep.baseline_per_step_ms),
                )
            )
            continue

        # (3) 离群：每步耗时明显高于同轮中位数
        if enough and rep.baseline_per_step_ms > 0 and pstep > rep.baseline_per_step_ms * SLOW_FACTOR:
            ratio = pstep / rep.baseline_per_step_ms
            rep.flags.append(
                PerfFlag(
                    case_id=cid,
                    kind="slow_outlier",
                    detail=(
                        f"每步 {pstep / 1000:.1f}s，是本轮中位数 "
                        f"{rep.baseline_per_step_ms / 1000:.1f}s 的 {ratio:.1f} 倍"
                        f"（共 {steps} 步 / {total / 1000:.0f}s）"
                    ),
                    note=(
                        "这条用例的结果可能是对的，但**它比同轮其它用例慢得多**。"
                        "要么被测系统在这条路径上确实慢，要么这条用例写得绕"
                        "（多余的跳转、等待或重复操作）—— 两种都值得看一眼。"
                    ),
                    ratio=ratio,
                )
            )

    # (4) 成本异常：往返次数明显高于同轮水平（2026-10-11）。
    #
    # 为什么与上面三类**时间**异常分开：一条用例可以很快却很贵（反复小步试错），
    # 也可以很慢却不贵（单步等接口）。混在一起会让人以为"把速度调好就等于省钱"。
    #
    # 为什么只看次数不看 token：每次往返的输入量实测各条之间差别不大（约 1.6 万），
    # 所以 token 是被次数直接乘出来的。报"次数"比报"token"更接近可行动的原因 ——
    # 人能去查"这 30 步它到底在干嘛"，却没法对"60 万 token"做任何事。
    if _cost:
        _calls_all = [c for _, _, c in _cost if c]
        med_calls = int(statistics.median(_calls_all)) if _calls_all else 0
        for cid, tok, calls in _cost:
            if calls < HEAVY_CALLS:
                continue
            rep.flags.append(
                PerfFlag(
                    case_id=cid,
                    kind="cost_heavy",
                    detail=(
                        f"{calls} 次 LLM 往返、{tok / 1000:.0f}k token"
                        + (f"（本轮中位 {med_calls} 次）" if med_calls else "")
                    ),
                    note=(
                        "这条用例的**花费**远超同轮水平，而花费几乎完全由往返次数决定。"
                        "要省 token 就该看它：是步骤写得绕、是页面反复重试，"
                        "还是 agent 在原地打转 —— 这三种的修法完全不同。"
                    ),
                    ratio=calls / max(1, med_calls),
                )
            )

    if not rep.flags and rep.samples:
        rep.note = rep.note or f"{rep.samples} 条用例的耗时都在同一轮的正常范围内。"
    return rep


def perf_note_for(result, report: PerfReport) -> str:
    """取某条结果的性能结论（一句话），没有则空串。"""
    cid = result.get("case_id") if isinstance(result, dict) else getattr(result, "case_id", None)
    if cid is None:
        return ""
    f = report.by_case.get(int(cid))
    if f is None:
        return ""
    return f"[性能] {f.detail} —— {f.note}"
