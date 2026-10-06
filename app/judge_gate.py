"""判定闸门 —— 在 LLM 判定**之前**拦截那些不可能是"通过"的情况。

## 为什么需要它（不是提示词优化，是结构缺陷）

现场（run 10 / result 187，2026-10-06）三条记录互相矛盾：

| 来源 | 内容 |
|---|---|
| agent 的 final_answer | "页面只显示『正在加载中请稍后......』，0 个可交互元素……我无法核对" |
| 步骤记录 | 确实只有占位文本，没有任何可交互元素 |
| 判定器给的 status | **passed**，理由是"截图显示已到达资源审批配置列表" |

截图里根本没有列表。判定器不是"判断偏松"，是**编造了截图证据**：
它拿 agent 的一句话去反推"截图里应该有什么"，然后按想象补齐了结论。

`_SYSTEM` 里那条"截图是最强证据，看到了就 pass"权重太高，
而 `ask()` 对模型返回的 status 没有任何校验（`data.get("status") == "passed"` 就是通过）。
也就是说：**唯一的把关人是一个可以被一句话说服的模型**。

## ★ 第一版设计的教训（这版能通过 180 条真实数据回归，是修出来的）

第一版写了三条闸门，用 180 条历史 run_result 回归，**误伤 28 条**（15.6%）。
逐条看后的根因，以及本版的修法：

### 教训 1：闸门 2「页面在加载中」是彻底错误的判据 —— 26 条全误伤

「正在加载中请稍后......」是 **SPA 的中间态**，不是终态。
被测系统是个 Vue SPA，**几乎每一条正常用例的前几步都会经过它**
（先 `navigate`，SPA 还没渲染出 DOM）。实测 26 条 passed 的用例全都命中这条。

第一版把它当"硬阻塞"，等于把"正常跑起来的用例"和"卡死的用例"当成同一件事。

**修法**：不再看「有没有出现过加载占位」，只看**最终态**——
最后一步的观察里仍是加载占位，**且** agent 没有任何成功/完成陈述。
中间态出现过多少次完全不重要。

### 教训 2：闸门 1 不能只看「有没有放弃词」—— 要看「是不是唯一的结论」

误伤样本的共性（id 16/ 47 / 63 / 125 等）：agent 说的是
"**核心验证点已确认**，但未完整跑通 XXX 环节"。
这是合法的通过判定（次要分支没覆盖不影响主断言），不是放弃。

同一个词「未能完成」在两种语境里意思相反：
- "我无法完成核对，页面没加载出来"→ 真放弃
- "虽然没能跑通取消流程，但新增已被拦截"→ 部分完成，合法通过

**修法**：区分「**全盘放弃**」与「**部分完成**」。
判据是**有没有正面的成功陈述**（已成功/已核对/符合预期…）——
有成功陈述就不拦，因为"部分完成"和"证据不足"只有 agent 自己能分辨，
而它把话说得很清楚。**只拦那些通篇找不到任何成功陈述、又在明确放弃的**。

### 保留的原则

1. **只拦"物理上不可能通过"的情况**，不碰主观判断。
   判定器对"结果符不符合预期"有判断权，我们不越权替它做测试决策。
2. **纯函数、零 IO**，可在无浏览器、无 LLM 的环境下单测。
3. **失败要说清是哪条闸门拦的** —— 报告里要能一眼看出"这不是模型判的"。
4. 误伤方向是**宁可多拦**：把假失败变成"需人工看一眼"，
   比把真缺陷报成"通过"代价低得多（后者会让缺陷直接漏出去）。

## 回归基线（本文件的数据来自项目真库 run_result，非构造样本）

用`tests/test_judge_gate_regression.py::test_no_false_block_on_real_history`钉住：
180 条真实结果中，闸门只允许拦下原本就是 failed 的条目，**误伤必须为 0**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ════════════════════════════════════════════════════════════════════
# 正面成功陈述 —— 有这些就说明 agent 自认为做到了，不拦
# ════════════════════════════════════════════════════════════════════

# 判定的关键不是"有没有放弃词"，而是"**通篇有没有成功陈述**"。
# 少了这一层会把"核心点已验证、次要分支没跑完"这种合法的部分完成误杀成失败。
_SUCCESS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"已(?:经)?(?:成功|完成|确认|核对|验证|通过|进入|到达|执行|添加|删除|修改|停用|启用|保存)",
        r"成功(?:进入|到达|完成|添加|删除|保存|执行|核对|验证)",
        r"(?:核对|验证|确认|检查)(?:无误|正确|通过|完成|一致)",
        r"符合预期|符合期望|与预期(?:一致|相符|吻合)|满足预期|与期望一致",
        r"测试通过|核对通过|验证通过|结论.{0,4}通过",
        r"前端(?:已|确实)?(?:拦截|校验|提示)",
        r"\b(?:successfully|verified|confirmed|passed|validated)\b",
    )
)

# ════════════════════════════════════════════════════════════════════
# 全盘放弃陈述 —— 匹配时要求整段找不到任何成功陈述
# ════════════════════════════════════════════════════════════════════

# 收紧的**唯一**手段是"通篇无成功陈述"这一前置条件（见 check_gates），
# 这里的正则因此可以覆盖常见的放弃句式，不必强求"我"字头。
# ——第一版把 `(?:无法|未能)\s*(完成|核对|...)` 当判据，误伤了"部分完成"；
# 教训不在于放弃词本身，而在于**缺少否证机制**。
# 反过来只认"我…"字头又太窄：agent 常写成"无法完成核对。"（无主语），
# 那正是 run 10 的原句。所以两者结合：词形放宽，靠成功陈述把关。
_ABANDON_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # 中文：无法/不能/未能/没法 + 完成/核对/验证/确认/进入/打开/获取/访问/继续/进行/读取
        r"(?:无法|不能|未能|没法)\s*"
        r"(?:完成|核对|验证|确认|进入|打开|获取|访问|继续|进行|读取|定位|找到|看到)",
        # "该用例因……未能验证" / "本次测试因……无法完成"
        r"(?:该用例|本次(?:测试|运行)?|此用例)因[^。；]{0,40}(?:未能|无法|不能)"
        r"(?:验证|完成|核对|确认)",
        # "因页面/系统/网络/服务器……未能加载"
        r"因(?:页面|系统|网络|服务器|应用|接口)[^。；]{0,20}(?:未能|无法|不能)"
        r"(?:加载|访问|打开|响应|渲染|进入)",
        # "页面无法完成验证/加载"（主语是页面本身）
        r"(?:页面|系统|网络)无法(?:完成|进行)?\s*(?:验证|加载|访问|核对)",
        # 明确的整句定性
        r"任务(?:无法继续|失败|中断)",
        # 英文
        r"\bI\s+(?:could\s*n[o']?t|could\s+not|was\s+unable\s+to|am\s+unable\s+to|"
        r"failed\s+to)\s+(?:complete|verify|confirm|reach|access|open|proceed|check)\b",
        r"\b(?:could\s+not|couldn[o']t|unable\s+to)\s+"
        r"(?:complete|verify|confirm|reach|access)\s+the\b",
        r"\bnot\s+(?:verified|validated|confirmed)\s+because\b",
        r"\bunable\s+to\s+(?:complete|verify|confirm|reach|access)\b",
    )
)

# 加载占位文案（只用于判断**最终态**，见_is_final_state_blocked）
_LOADING_PLACEHOLDERS: tuple[str, ...] = (
    "正在加载中请稍后",
    "正在加载中",
    "加载中请稍候",
    "数据加载中",
    "正在加载",
    "loading...",
    "please wait while loading",
)


# ════════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════════


def _has_success_statement(text: str) -> bool:
    """agent 的自述里有没有正面的成功/完成陈述。"""
    return any(p.search(text) for p in _SUCCESS_PATTERNS)


def _abandon_statement(text: str) -> str | None:
    """全盘放弃陈述的命中片段，找不到返回 None。"""
    for pat in _ABANDON_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def _is_loading_placeholder(text: str) -> bool:
    low = text.lower()
    return any(ph in low for ph in _LOADING_PLACEHOLDERS)


def _final_state_blocked(evidence: list[str] | None) -> str | None:
    """判断**最终态**是否卡在加载占位。

    ★ 关键修正（第一版最大的错误）：只看**最后一条**观察，不是"有没有出现过"。
    SPA 的加载占位是中间态，正常用例几乎必然经历它；
    只有"到最后一步还停在这儿"才是阻塞。

    同时要求**没有任何可交互元素**佐证 —— 单有占位文案不够，
    因为有些页面标题栏/空态提示里也含"加载"字样。
    """
    ev = [e for e in (evidence or []) if e and e.strip()]
    if not ev:
        return None
    last = ev[-1]
    if not _is_loading_placeholder(last):
        return None
    # 最后一条里同时出现"0 个可交互元素"才算硬阻塞
    low = last.lower()
    zero_interactive = (
        "0 interactive" in low
        or "0 links" in low
        or "0 个可交互" in low
        or "0 interactive elements" in low
        or re.search(r"\b0\s*(?:interactive|clickable)\b", low) is not None
    )
    if zero_interactive:
        return last.strip()[:120]
    return None


# ════════════════════════════════════════════════════════════════════
# 统一入口
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class GateResult:
    """闸门裁决。blocked=False 时放行，交给 LLM 判定器。"""

    blocked: bool
    reason: str = ""
    gate: str = ""


def check_gates(
    final_answer: str,
    actions: list[str] | None,
    evidence: list[str] | None,
) -> GateResult:
    """在调用 LLM 判定器**之前**执行。返回 blocked=True 时该用例直接判失败。

    调用方必须把 reason 原样写进报告，并标记 gate 来源，
    让报告里能区分"模型判的失败" 与 "闸门拦下的假通过"。

    `actions` 目前只用于日志与将来的收紧规则，**不参与拦截**——
    刻意不把"只导航没点击"当拦截条件：「打开列表看一眼」这类用例
    本来就只需要一次导航，按动作过滤会误杀（第一版的教训 3）。
    """
    fa = final_answer or ""
    ev = [e for e in (evidence or []) if e and e.strip()]

    has_success = _has_success_statement(fa)

    # ── 闸门 1：全盘放弃（通篇无成功陈述 + 明确放弃）──
    # 有成功陈述就放行：那是"部分完成"，agent 比我更清楚哪些是必判项。
    if not has_success:
        frag = _abandon_statement(fa)
        if frag:
            return GateResult(
                blocked=True,
                gate="agent_declined",
                reason=(
                    f"【判定闸门】agent 全篇未给出任何成功陈述，却明确声明放弃"
                    f"（自述片段：「{frag[:60]}」），不存在可核对的证据，判为失败。"
                    f"这一条由确定性规则拦截，不采信判定器基于截图的推断。"
                ),
            )

        # ── 闸门 2：最终态卡在加载占位 ──
        # 只看最后一步，且要求同时出现"0 可交互元素"。
        final = _final_state_blocked(ev)
        if final:
            return GateResult(
                blocked=True,
                gate="page_not_ready",
                reason=(
                    f"【判定闸门】运行结束时页面仍停留在加载占位状态且无任何可交互元素"
                    f"（最后一步观察：「{final}」），此时任何「已到达目标页面」的结论"
                    f"都没有依据，判为失败。"
                ),
            )

    return GateResult(blocked=False)
