"""失败清单的分组器 —— 把散落的失败结果聚成"能据以行动"的清单条目。

## 为什么不能只靠 LLM 聚类

用户要的是「相同原因可以合并，但要讲清楚，不能讲废话」。这两句有张力：
LLM 语义聚类能把条目数压到最小，但它**会把不同的根因合并**——
实测踩过的坑：`judge_reason` 里"代理"出现 15 次，可那里的"代理"
指的是**测试 agent**（"代理未能勾选多条规则"），不是网络代理。
按关键词或Embedding 聚类，这两类会被混成一条，
而人照着它去查网络代理问题 —— 这就是"废话"的来源，且比没有清单更糟。

所以本模块的设计是：**确定性信号负责"能不能合并"，LLM 只负责"讲清楚"。**

## 分组信号（按可信度降序，全部可复现）

1. `gate:<gate名>` —— 判定闸门拦截。规则触发，**100% 确定**。
2. `timeout` —— error 列有明确"超时：Ns内只走到第 X/Y 步"。
3. `root_cause:<key>` —— 分类器给出的白名单分类（仅最近数据有值）。
4. `singleton` —— 都不匹配时**单列**，绝不猜。

★ 合并方向是**单向收敛**的：只有信号完全相同才合并。
宁可 20 条单列，也不合并错 1 条 —— 后者的代价（人去查不存在的问题）
远大于前者（清单长一点）。

## LLM 在哪一层介入

`explain_group()`：把组内证据交给 LLM 生成"该改什么"的一句话建议。
它**只能改措辞，不能改分组** —— 分组已在上游完成。
这样即使 LLM 不可用或胡说，清单仍然正确可用。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ════════════════════════════════════════════════════════════════════
# 信号提取
# ════════════════════════════════════════════════════════════════════

# 判定闸门的两种落库形态：
#   1. judge_reason 前缀「【判定闸门】…（gate 名由 reason 内文案决定）」
#   2. 闸门命中时 executor 写入的 "★ 判定闸门拦下假通过（gate=xxx）"
# 前者进了库、后者只在日志里，所以要从 reason 文本里认。
_GATE_RE = re.compile(r"【判定闸门】")
# ★探测串必须用 judge_gate.py 实际写进 reason 的**中文原文**，
# 不能拿 key 名（agent_declined）去搜 —— 库里根本没有那个词。
# 踩过：初版写成 _GATE_KINDS = {"agent_declined": "agent_declined"}，
# 结果所有闸门条目都落进 gate:other，合并失效还不报错（静默降级）。
_GATE_KIND_PROBES: tuple[tuple[str, str], ...] = (
    ("全篇未给出任何成功陈述", "agent_declined"),
    ("仍停留在加载占位状态", "page_not_ready"),
)

# 超时：error 列形如「超时：480s 内只走到第 18/40 步。」
_TIMEOUT_RE = re.compile(r"超时：(\d+)s\s*内只走到第\s*(\d+)/(\d+)\s*步")
_CANCELLED_RE = re.compile(r"^cancelled$", re.I)

# 根因白名单与 judge.py 的 _ROOT_CAUSES 保持一致。
# 不在白名单里的值一律降级为singleton —— 模型偶尔会造出听着合理的分类，
# 让它进清单会让人按不存在的分类去排查。
_VALID_ROOT_CAUSES = frozenset({
    "product_defect", "agent_incomplete", "evidence_insufficient",
    "precondition_missing", "auth_or_permission", "environment",
    "test_data", "test_case_issue", "unclear",
})

# root_cause → 该不该找开发。分错方向的代价是让人白跑一趟。
_ACTION_BY_CAUSE = {
    "product_defect": "report_to_dev",
    "test_case_issue": "fix_case",
    "test_data": "fix_data",
    "precondition_missing": "fix_env",
    "auth_or_permission": "fix_env",
    "environment": "fix_env",
    "agent_incomplete": "rerun",
    "evidence_insufficient": "inspect",
    "unclear": "inspect",
}
ACTION_LABELS = {
    "fix_case": "改用例",
    "fix_data": "改测试数据",
    "fix_env": "改环境/凭据",
    "report_to_dev": "提缺陷给开发",
    "rerun": "重跑观察",
    "inspect": "需人工看一眼",
}


def group_signal(row: dict[str, Any]) -> tuple[str, str]:
    """返回 (信号键, 人类可读的原因)。信号键相同才允许合并。

    顺序即优先级 —— 第一个命中的胜出，不做多信号交叉。
    """
    reason = (row.get("judge_reason") or "").strip()
    error = (row.get("error") or "").strip()
    cause = (row.get("root_cause") or "").strip()

    # 1) 判定闸门：规则触发，最确定
    if _GATE_RE.search(reason):
        for probe, key in _GATE_KIND_PROBES:
            if probe in reason:
                return f"gate:{key}", f"判定闸门拦截（{key}）"
        # 闸门文案变了不许静默降级成同一个桶 —— 那会让不同根因被合并。
        # 带上 reason 的前若干字作为信号的一部分，宁可多一条也不合并错。
        sig = "gate:other:" + re.sub(r"\W+", "", reason)[:40]
        return sig, "判定闸门拦截（闸门文案未识别，需人工确认是哪一类）"

    # 2) 执行超时/取消：error 列有可核对的具体数字
    m = _TIMEOUT_RE.search(error)
    if m:
        return "timeout", f"执行超时（{m.group(1)}s 只走到第 {m.group(2)}/{m.group(3)} 步）"
    if _CANCELLED_RE.match(error):
        return "cancelled", "运行被取消"

    # 3) error 里的其它非空值：按错误类型粗分（同一类错误才合并）
    if error:
        kind = re.sub(r"\d+", "N", error)[:60]
        return f"error:{kind}", f"执行报错：{error[:80]}"

    # 4) 根因分类：仅当在白名单内
    if cause in _VALID_ROOT_CAUSES:
        return f"cause:{cause}", f"判定器归类：{cause}"

    # 5) 兜底：单列。**绝不用 judge_reason 的关键词聚类**（见模块 docstring）
    return f"singleton:{row.get('id')}", "原因待人工确认"


# ════════════════════════════════════════════════════════════════════
# 分组
# ════════════════════════════════════════════════════════════════════


@dataclass
class Group:
    """一条清单项 = 一组同因失败。"""

    signal: str
    summary: str                    # 原因，一句话
    action: str                     # fix_case / report_to_dev / ...
    action_label: str
    cases: list[dict] = field(default_factory=list)   # 去重后的用例
    result_ids: list[int] = field(default_factory=list)
    latest_at: str = ""
    # 同组内若存在 root_cause 冲突（如一条 product_defect 一条 agent_incomplete），
    # 说明分类器与证据不一致 —— 清单必须让人看见这件事，不能默默按多数决。
    cause_conflict: bool = False
    causes_seen: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "summary": self.summary,
            "action": self.action,
            "action_label": self.action_label,
            "case_count": len(self.cases),
            "result_count": len(self.result_ids),
            "cases": self.cases,
            "result_ids": self.result_ids,
            "latest_at": self.latest_at,
            "cause_conflict": self.cause_conflict,
            "causes_seen": self.causes_seen,
        }


def build_groups(rows: list[dict[str, Any]]) -> list[Group]:
    """把失败结果聚成清单。rows 里每条必须有 id/case_id/case_key/name/root_cause。

    排序：受影响用例数多的在前 —— 那是最该先解决的。
    同数时按最近失败时间倒序，保证"刚出的问题"浮到上面。
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    metas: dict[str, tuple[str, str]] = {}   # signal -> (summary, action)
    for r in rows:
        sig, summary = group_signal(r)
        buckets[sig].append(r)
        # 同一信号下summary 取第一个（group_signal 对同信号返回同文案）
        metas.setdefault(sig, (summary, ""))

    groups: list[Group] = []
    for sig, items in buckets.items():
        summary, _ = metas[sig]
        causes = sorted({(i.get("root_cause") or "") for i in items if i.get("root_cause")})
        # 单组内根因冲突 = 分类器与证据打架，必须显式暴露
        conflict = len(causes) > 1

        # 动作：冲突时降级为"人工看一眼"，绝不按多数决猜一个
        if conflict:
            action = "inspect"
        elif causes and causes[0] in _ACTION_BY_CAUSE:
            action = _ACTION_BY_CAUSE[causes[0]]
        elif sig.startswith("gate:"):
            # 闸门拦截 = agent 自己说做不到 → 先重跑观察，别急着改用例
            action = "rerun"
        elif sig in ("timeout", "cancelled"):
            action = "rerun"
        else:
            action = "inspect"

        # 用例去重（同一 case 多次失败只列一次，附失败次数）
        by_case: dict[int, dict] = {}
        for i in items:
            cid = i.get("case_id")
            entry = by_case.setdefault(cid, {
                "case_id": cid,
                "case_key": i.get("case_key") or "",
                "name": i.get("name") or "",
                "module": i.get("module") or "",
                "fail_count": 0,
                "latest_reason": (i.get("judge_reason") or "")[:300],
                "latest_error": (i.get("error") or "")[:200],
            })
            entry["fail_count"] += 1
            if (i.get("created_at") or "") > (entry.get("latest_at") or ""):
                entry["latest_at"] = i.get("created_at") or ""
                entry["latest_reason"] = (i.get("judge_reason") or "")[:300]
                entry["latest_error"] = (i.get("error") or "")[:200]

        groups.append(Group(
            signal=sig,
            summary=summary,
            action=action,
            action_label=ACTION_LABELS.get(action, action),
            cases=sorted(by_case.values(), key=lambda c: -c["fail_count"]),
            result_ids=[i["id"] for i in items],
            latest_at=max((i.get("created_at") or "") for i in items),
            cause_conflict=conflict,
            causes_seen=causes,
        ))

    groups.sort(key=lambda g: (-len(g.cases), g.latest_at), reverse=False)
    groups.sort(key=lambda g: -len(g.cases))
    return groups
