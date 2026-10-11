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


@dataclass
class PerfFlag:
    """一条用例的性能异常结论。"""

    case_id: int
    kind: str  # slow_outlier | slow_step | overhead
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

    @property
    def by_case(self) -> dict[int, PerfFlag]:
        return {f.case_id: f for f in self.flags}

    def as_dict(self) -> dict:
        return {
            "baseline_per_step_ms": self.baseline_per_step_ms,
            "samples": self.samples,
            "flags": [f.as_dict() for f in self.flags],
            "note": self.note,
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
