"""判定闸门的回归防线。

## 为什么这组测试用的是**真库数据**而不是构造样本

第一版闸门我写完用 180 条真实 `run_result` 一跑，**误伤 28 条（15.6%）**。
两个设计错误都是靠逐条读真实数据才发现的：

1. 「页面在加载中」当硬阻塞 → 26 条误伤。被测系统是 Vue SPA，
   "正在加载中请稍后......"是**中间态**，几乎每条正常用例早期都经过它。
2. 只看"有没有放弃词" → 误伤「核心点已验证、次要分支没跑完」这种合法部分完成。

构造样本看不出来，因为构造时我脑子里只有 run 10 那个案例。
**所以这里直接读真库，并断言"误伤必须为 0"** —— 这比任何构造样本都硬。

## 读真库的边界

`potato.db` 是用户的真库，测试**只读不写**。文件不存在时 skip 而不是 fail，
因为干净 clone（CI / 别人的机器）上跑不出回归基线，那是环境差异不是代码缺陷。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.judge_gate import (
    _abandon_statement,
    _final_state_blocked,
    _has_success_statement,
    check_gates,
)

_DB = Path(__file__).resolve().parents[1] / "potato.db"


def _load_real_results() -> list[dict]:
    if not _DB.exists():
        pytest.skip(f"真库不存在：{_DB}（干净环境无回归基线）")
    conn = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "select id, status, final_answer, steps, diagnostics from run_result"
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        ev: list[str] = []
        try:
            for d in json.loads(r["diagnostics"] or "[]"):
                if isinstance(d, dict):
                    ev.append(
                        f"{d.get('thought', '') or ''} {d.get('detail', '') or ''}"
                    )
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            steps = json.loads(r["steps"] or "[]")
        except (json.JSONDecodeError, TypeError):
            steps = []
        out.append(
            {
                "id": r["id"],
                "status": r["status"],
                "final_answer": r["final_answer"] or "",
                "steps": steps,
                "evidence": ev,
            }
        )
    return out


# ══════════════════════════════════════════════════════════════════
# 核心回归：真库 180 条，误伤必须为 0
# ══════════════════════════════════════════════════════════════════


# 已知的**历史误判**样本：判定器当年给了 passed，但闸门认定它是假通过。
# 187 = run 10（用户当面报告的那次），必须被拦 —— 所以它不算"误伤"。
# 回归时用这份名单区分"闸门抓对了"与"闸门杀错了"。
#
# 为什么不能简单断言"passed 一条都不许拦"：真库里混着当年的误判，
# 闸门存在的意义就是抓它们。真正要守的是"闸门不许杀那些**确实做对了**的用例"，
# 而"是否确实做对了"正是 187 这条误判本身无法自证的地方 ——
# 所以名单要显式维护，每条都写清为什么。
_KNOWN_FALSE_PASSES: dict[int, str] = {
    116: "agent 报告 ERR_EMPTY_RESPONSE /『无法访问此网站』，判定器却称"
        "『截图显示已成功进入…列表展示 9 条数据』—— 与 agent 自述直接矛盾",
    187: "run 10：agent 明确说『无法完成核对，页面只有加载占位、0 可交互元素』，"
        "判定器却判 passed 并称『截图显示已到达列表』—— 截图里没有列表",
}


def test_no_false_block_on_real_history():
    """闸门不许杀"确实做对了"的用例；已知的历史误判必须被拦。

    这是第一版 28 条误伤的直接对策。改动闸门逻辑后必须先跑这条。
    """
    results = _load_real_results()
    if not results:
        pytest.skip("真库里没有 run_result 记录")

    false_blocks: list[tuple[int, str]] = []
    blocked_ids: list[tuple[int, str]] = []
    for r in results:
        g = check_gates(r["final_answer"], r["steps"], r["evidence"])
        if not g.blocked:
            continue
        blocked_ids.append((r["id"], g.gate))
        if r["status"] == "passed" and r["id"] not in _KNOWN_FALSE_PASSES:
            false_blocks.append((r["id"], g.gate))

    assert not false_blocks, (
        f"闸门误伤了 {len(false_blocks)} 条真实通过用例：{false_blocks}。"
        f"被拦下的全部条目：{blocked_ids}。"
        f"误伤方向是错的——把真失败报成通过会让缺陷直接漏出去，代价远大于误报。"
    )


def test_known_false_passes_are_actually_blocked():
    """名单上的历史误判必须仍然被拦 —— 否则闸门退化成了摆设。"""
    results = {r["id"]: r for r in _load_real_results()}
    for rid, why in _KNOWN_FALSE_PASSES.items():
        if rid not in results:
            continue
        g = check_gates(
            results[rid]["final_answer"],
            results[rid]["steps"],
            results[rid]["evidence"],
        )
        assert g.blocked, (
            f"已知的假通过 {rid} 没被拦下（{why}）。"
            f"闸门一旦对这类样本失效，就回到了「只信模型」的老问题。"
        )


def test_real_history_baseline_is_meaningful():
    """基线本身要有效：样本里必须同时有 passed 和 failed，否则回归没有意义。

    样本不足时 **skip 而不是 fail** —— 与本文件开头的边界一致：`potato.db` 是用户
    的真库，一条都没跑过（新装、CI、别人的机器）属于环境差异，不是代码缺陷。
    这个用例的价值是"有历史数据时确认基线可信"；没数据时它什么也证明不了，
    报红只会训练人忽略红灯。真正缺样本的情况由上面的回归用例覆盖（它们同样 skip）。
    """
    results = _load_real_results()
    statuses = {r["status"] for r in results}
    if "passed" not in statuses or len(results) < 50:
        pytest.skip(
            f"真库历史样本不足（{len(results)} 条，statuses={sorted(statuses)}）——"
            "无回归基线可比，非代码缺陷"
        )


# ══════════════════════════════════════════════════════════════════
# 现场案例：run 10 / result 187（必须拦下）
# ══════════════════════════════════════════════════════════════════

# agent 原话节选：明确说做不到，页面卡在加载占位，0 个可交互元素。
# ★ 地址用 RFC 2606 保留域名（example.com），不要写真实内网地址 ——
#   本仓库是公开的，检查器会在 --head 阶段拦下真实标识。
_CASE_187_ANSWER = (
    "无法完成核对。访问目标地址 http://example.invalid/training/resource-management/config "
    "时页面未能真正加载：页面上只显示一条「正在加载中请稍后......」的占位文本，"
    "浏览器状态里没有任何可交互元素（0 links, 0 interactive），"
    "也没有出现登录框、菜单或资源审批配置列表。"
    "由于无法进入「培训资源管理 → 资源审批配置」列表，我无法核对列顺序"
    "以及第4列是否为「审批」。该用例因页面加载受阻而未能验证。"
)
_CASE_187_EVIDENCE = [
    "当前浏览器状态只有加载中占位文本，没有登录框、菜单或资源审批配置列表。",
    "0 links, 0 interactive — 页面仍显示「正在加载中请稍后......」，无任何可交互元素。",
]


def test_blocks_the_real_false_pass():
    """现场那条假通过必须被拦下（这是建闸门的原始动机）。"""
    g = check_gates(_CASE_187_ANSWER, ["navigate", "done"], _CASE_187_EVIDENCE)
    assert g.blocked, "现场假通过未被拦下，闸门失效"
    assert g.gate == "agent_declined"
    assert "判定闸门" in g.reason, "原因里必须标明是闸门拦的，便于报告区分"


# ══════════════════════════════════════════════════════════════════
# 关键否证：中间态不算阻塞（第一版 26 条误伤的根因）
# ══════════════════════════════════════════════════════════════════


def test_loading_placeholder_in_middle_is_not_blocking():
    """"正在加载中"出现在中间步骤时**不能**拦 —— SPA 的中间态而已。

    这条是第一版闸门 2 的直接对策。它拦下了 26 条真实的正常通过用例，
    因为 Vue SPA 导航后必然先出现这个占位。
    """
    answer = "已完成核对：列表加载完成，4 条记录的字段与预期一致，符合预期。"
    evidence = [
        "页面导航后显示「正在加载中请稍后......」，0 个可交互元素，等待渲染。",
        "我等待页面加载完成。",
        "已成功进入「资源审批配置」列表，表格显示 10 条记录。",
    ]
    g = check_gates(answer, ["navigate", "wait", "click", "done"], evidence)
    assert not g.blocked, "把 SPA 中间态当成阻塞了 —— 这是第一版的严重误伤"


def test_final_state_loading_placeholder_does_block():
    """反过来：最后一步仍卡在加载占位且 0 可交互元素，必须拦。"""
    answer = "无法完成核对。"  # 无成功陈述
    evidence = ["已完成导航。", "最终仍显示「正在加载中请稍后......」，0 interactive。"]
    g = check_gates(answer, ["navigate"], evidence)
    assert g.blocked
    assert g.gate == "agent_declined"


def test_loading_placeholder_alone_is_not_enough():
    """只有占位文案、没有"0 可交互元素"佐证时**不拦**。

    有些页面标题/空态里也含"加载"字样，单凭文案会误杀。
    """
    assert _final_state_blocked(["已加载完成，正在加载下一批数据 0 条结果"]) is None


# ══════════════════════════════════════════════════════════════════
# 关键否证：部分完成不算放弃（第一版闸门 1 的误伤根因）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "answer",
    [
        # 真实误伤样本 id 16 的形状：核心点已确认，次要分支没跑完
        "【已完成/可确认的部分】已成功进入资源审批配置。"
        "【核心验证点已确认】已配置项被置灰禁选。"
        "但未完整跑通『先建1条再核对』的完整链路（卡在审批节点选择）。",
        # 真实误伤样本 id 40 的形状：在确认某个东西"不存在"，这是合法核对
        "弹窗包含规则名称、使用范围、资源范围字段。"
        "关于『审批节点选择项』：弹窗内没有可操作的选择项，符合预期。",
        # 真实误伤样本 id 2 的形状：「不能与页面实际显示一致」是"无异常"陈述
        "核对结果（均与页面实际显示一致）：筛选后共 4 条记录。",
    ],
)
def test_partial_completion_is_not_blocked(answer):
    """部分完成 / 确认"不存在" 都是合法通过，不该拦。"""
    g = check_gates(answer, ["navigate", "click", "click"], ["观察1", "观察2"])
    assert not g.blocked, f"误伤了合法通过用例：{answer[:60]}"


# ══════════════════════════════════════════════════════════════════
# 单元级：成功陈述 / 放弃陈述的判别
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    [
        "已成功进入资源审批配置列表。",
        "核对结果：符合预期。",
        "已完成核对，字段与预期一致。",
        "The form validation successfully blocked the submission.",
    ],
)
def test_success_statement_detected(text):
    assert _has_success_statement(text), f"漏认成功陈述：{text}"


@pytest.mark.parametrize(
    "text",
    [
        "无法完成核对，页面没加载出来。",
        "我未能进入目标页面。",
        "该用例因页面加载受阻而未能验证。",
        "I could not verify the column order.",
    ],
)
def test_abandon_statement_detected(text):
    assert _abandon_statement(text), f"漏认放弃陈述：{text}"


def test_gates_pass_through_clean_run():
    """正常完成的用例必须零干扰放行。"""
    g = check_gates(
        "已完成核对：删除成功，列表由 12 条刷新为 11 条，符合预期。",
        ["navigate", "click", "click"],
        ["点击删除", "记录已移除，列表刷新为 11 条"],
    )
    assert not g.blocked and g.gate == ""


def test_empty_inputs_do_not_crash():
    """空输入不能让闸门崩 —— 崩了就等于所有用例判失败。"""
    g = check_gates("", [], [])
    assert not g.blocked
