"""判定前的确定性校验 —— 抓"编造的数字"和"没验证就下结论"。

## 为什么必须在 LLM 判定之外再加一层

现场（run 12 / case 4「使用范围+状态同时选 → 点查询」，2026-10-06 20:00）：
判定器判`product_defect`，理由是"筛选未生效，查询后列表仍显示全部 11 条"。
逐步核对该次执行的 `thought`（意图）与 `result`（浏览器记录的实际动作）：

| 步 | thought 说要做 | result 实际做的 |
|---|---|---|
| 3 | 选择「课程资源」 | 点了 combobox 输入框 |
| 4 | 打开「状态」下拉 |点了 li「场地资源」|
| 5 | 选择「启用」 | 点了 combobox |
| 6 | 点「查询」 | 点了 span「启用」 |
| 7 | done（宣布完成）| 点了 button「查询」|

**筛选条件是在最后一步才真正生效的**，agent 却在同一步宣布完成——
它从头到尾**没有读过查询结果页**，却在 final_answer 里写：

> "查询前列表共 11 条……其中符合条件的仅有 3 条：场地资源审批23、…"

那 3 条是它**自己算的**，不是从页面上读的。而判定器把这份自述当成了证据，
于是报出一个**假的产品缺陷** —— 真缺陷率被污染，比假失败更难挽回。

## 现有提示词为什么没拦住

`app/judge.py` 的 `_SYSTEM` 里已经有"CRITICAL — do not invent anything"，
但那条约束的对象是"**动作与观察**"。而这里编造的是**结论里的数字**：
agent 说"共 11 条"时语气笃定、格式规整，判定器读起来就像一条真实观察。
提示词约束的是"别编动作"，没有覆盖"别编数据"。

## 本模块的定位

纯函数、零 IO、不调 LLM。只做一件事：**把"结论依赖了步骤记录里不存在的观察"这件事标出来**。
判不判失败仍由判定器决定——这里只提供一条它自己没有的能力：
**核对结论里的数字能不能在步骤记录里找到出处。**

★为什么不给"直接判失败"的权力：数字找不到出处可能是 agent 表述方式的差异
（例如把"共11 条"写在 thought 里而不是 result 里）。直接判失败会误伤，
而误伤判定比判定宽松更伤——它会让整个报告不可信。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ════════════════════════════════════════════════════════════════════
# 数字提取
# ════════════════════════════════════════════════════════════════════

# 中文语境里表示"计数"的数字。刻意**只认带量词的**：
# 「共11 条」「3 条记录」是页面会显示的计数；
# 光秃秃的 "11" 可能出现在 URL、版本号、时间里，追它只会误报。
_COUNT_RE = re.compile(
    r"(?:共|合计|总计|剩下|只有|仅|应该(?:是|为)?|预期(?:是|为)?|应为|"
    r"total|count|result_count|rows?)\s*"
    r"(\d{1,6})\s*(?:条|行|个|项|页|家|次|笔|张)"
    # 也抓「11条」这种没有前导词的（页面计数最常见的写法）
    r"|(\d{1,6})\s*(?:条记录|行记录|条数据|条规则|项记录)"
)

# 「N 条」通用形态：用于宽松比对（只比数字，不要求量词在原文出现）
_LOOSE_COUNT_RE = re.compile(r"(\d{1,6})\s*(?:条|行|个|项)")


def _counts_in(text: str) -> set[int]:
    """抽取文本里所有"像页面计数"的数字。"""
    if not text:
        return set()
    out: set[int] = set()
    for m in _COUNT_RE.finditer(text):
        for g in m.groups():
            if g:
                out.add(int(g))
    # 宽松形态只在小数字时收（<= 9999），避免把时间/ID 当计数
    for m in _LOOSE_COUNT_RE.finditer(text):
        n = int(m.group(1))
        if 0 < n <= 9999:
            out.add(n)
    return out


# ════════════════════════════════════════════════════════════════════
# 结论里的"未经核对"标记
# ════════════════════════════════════════════════════════════════════

# 出现在结论里、但**没在步骤记录里出现过**的数字。这个名字会挂到 reason 末尾。
_UNVERIFIED = "（该结论引用了步骤记录里没有的观察）"

# 这些词出现时，说明结论在**断言页面状态**——
# 那就必须有对应的页面观察作为依据。
_STATE_CLAIM_RE = re.compile(
    r"(?:查询后|筛选后|提交后|保存后|删除后|列表|结果显示|"
    r"共\s*\d+\s*条|未过滤|未生效|仍然显示|仍显示|实际显示)"
)


@dataclass(frozen=True)
class UnverifiedClaim:
    """agent 结论里有一处没有证据支撑的观察。"""

    numbers: tuple[int, ...]
    where: str          # 出现在结论的哪一句
    snippet: str


def find_unverified_claims(
    final_answer: str,
    evidence: list[str] | None,
    min_new_numbers: int = 1,
) -> list[UnverifiedClaim]:
    """找出 final_answer 里有、但步骤记录里找不到出处的计数。

    返回空列表 = 结论里的数字都有出处（正常情况）。
    非空 = 判定器应当知道这些数字不是从页面上读来的。
    """
    ev_text = "\n".join(e for e in (evidence or []) if e)
    ev_counts = _counts_in(ev_text)
    if not ev_text:
        # 没有任何步骤记录 → 结论里所有计数都无从核对
        counts = _counts_in(final_answer)
        if not counts:
            return []
        return [UnverifiedClaim(tuple(sorted(counts)), "final_answer", "（无步骤记录）")]

    out: list[UnverifiedClaim] = []
    # 按句切，便于告诉判定器"这句话里的数字没出处"
    for sentence in re.split(r"[。；\n]+", final_answer or ""):
        if not _STATE_CLAIM_RE.search(sentence):
            continue
        nums = _counts_in(sentence)
        novel = {n for n in nums if n not in ev_counts}
        if len(novel) >= min_new_numbers:
            out.append(UnverifiedClaim(
                numbers=tuple(sorted(novel)),
                where="final_answer",
                snippet=sentence.strip()[:120],
            ))
    return out


def unverified_note(claims: list[UnverifiedClaim]) -> str:
    """给判定器的一句话提示（可为空字符串）。

    ★ 必须**点名 product_defect**：系统提示词里那条"别编造"的约束对象是动作，
    判定器看到"共 11 条"这种带量词、语气笃定的表述时，会自然地当成一条真实观察。
    明确点名这个分类，它才会意识到该往agent_incomplete 上归。
    """
    if not claims:
        return ""
    nums = sorted({n for c in claims for n in c.numbers})
    sample = claims[0].snippet[:80]
    return (
        f"{_UNVERIFIED}：结论里出现 {nums} 这些计数，"
        f"但 STEP_RESULTS 里没有任何一条观察读到过它们。例句：{sample}。\n"
        f"这意味着 agent **没有读过那个页面**，数字是它自己算或想象的。"
        f"因此：绝对不要据此判 product_defect（系统无法与一个从未被观察到的"
        f"预期结果相矛盾）；正确归类是 agent_incomplete"
        f"（它在读到结果之前就停下了），或 evidence_insufficient。"
        f"若结论是「通过」，同样不成立——用未见过的数字支撑的通过也是编造。"
    )


# ════════════════════════════════════════════════════════════════════
# 执行没落位：宣布完成时操作还没真正生效
# ════════════════════════════════════════════════════════════════════

# ★ 这一层抓的不是"意图与动作不一致"（那个要靠猜 thought 的意图，误报率高），
# 而是**一个结构上无争议的事实**：action 已经是 done（agent 宣布完成），
# 可同一条记录的 result 显示它这一"步"其实还在执行某个操作。
#
# 现场（run 12 / case 4）：
#   [7] action=done
#       thought : 任务已完成核对并确认缺陷存在，直接以FAIL结论报告。
#       result  : Clicked button "查询"
# → 它点下「查询」的那一刻就宣布完成了，**从未读过查询结果页**，
#   却在自述里写出"查询后仍显示共 11 条"。那 11 条是它自己算的。
#
# 为什么这条比"意图错位"可靠：
#   - 意图错位要判断"它想点A 实际点了 B 算不算错"，而 A/B 有时都对（点开下拉
#     本身就是正确动作），判断依赖对用例语义的理解 → 误报。
#   - "done 却还有未落地的操作"是**时序事实**，不需要任何语义理解。
_DONE = re.compile(r"^\s*done\b", re.I)
# result 里表示"这一步真的执行了一个动作"的形态
_PERFORMED = re.compile(
    r"(?:Clicked|Sent keys|Input|Typed|Scrolled|Pressed|Selected)"
    r"|🔗\s*Navigated",
    re.I,
)


@dataclass(frozen=True)
class PrematureDone:
    """agent 宣布完成时，还有操作没真正落地 —— 结论因此不可信。"""

    step_index: int
    performed: str      # 宣布完成那一刻"还在做"的动作描述


def find_premature_done(evidence: list[str] | None) -> PrematureDone | None:
    """找出"宣布完成但操作未落地"的那一步。返回 None = 正常。

    evidence 每条形如 `第N步: 【action】result (意图: thought)`
    （见 executor 里 narrative_actions 的拼法），也兼容 JSON 形态。
    """
    entries = [e for e in (evidence or []) if e]
    if not entries:
        return None
    # 最后一条是决定性的：结论是在那一步宣布的
    last = entries[-1]
    action = ""
    m = re.search(r"【([^】]*)】", last)
    if m:
        action = m.group(1)
    else:
        m2 = re.search(r'"action"\s*:\s*"([^"]*)"', last)
        if m2:
            action = m2.group(1)
    if not _DONE.search(action or ""):
        return None
    # done 了，但这一步的结果里还有一个"正在执行的动作"
    m3 = re.search(r"(Clicked|Sent keys|Input|Typed|Scrolled|Pressed|Selected)[^\n）)]*", last)
    if not m3:
        return None
    # 取出步骤号（"第7步"）
    mi = re.search(r"第(\d+)\s*步", last) or re.search(r'"i"\s*:\s*(\d+)', last)
    step_index = int(mi.group(1)) if mi else len(entries)
    return PrematureDone(step_index=step_index, performed=m3.group(0)[:120])


def premature_done_note(pd: PrematureDone | None) -> str:
    """给判定器的提示（可为空）。"""
    if pd is None:
        return ""
    return (
        f"【执行未落位】第 {pd.step_index} 步的 action 已经是 done（agent 宣布完成），"
        f"但同一条记录的结果是「{pd.performed}」—— 也就是说它在操作真正生效**之前**"
        f"就宣布了完成。这说明它**没有看到操作之后的状态**。"
        f"因此：它的最终结论（包括任何『查询后共 N 条』这类计数）都不是观察到的结果，"
        f"而是推测。归类应为 agent_incomplete，不是 product_defect —— "
        f"系统无法与一个从未被观察到的预期结果相矛盾。"
    )


# ════════════════════════════════════════════════════════════════════
# 第三层：自述与判定直接矛盾（现场 2026-10-06 21:19-21:23，最新一批）
# ════════════════════════════════════════════════════════════════════

# 实测两条：
#   result 5：agent final_answer =「尝试访问 …，但页面显示『正在加载中请稍后……』，
#              未能加载出预期的培训…」（**这是放弃**）
#             判定器 reason     =「最终截图显示页面已加载…符合预期」→ **判了 passed**
#   result 2：agent final_answer = `Clicked button "查询"`（**根本没写结论**）
#             判定器 reason     =「结果表格共 5 条记录…与 EXPECTED 一致」→ **判了 passed**
#
# 第一层（编造数字）和第二层（未落位）都拦不住这两种：它们查的是
# "数字有没有出处"、"done 时操作有没有落地"，而这里是**判定结论本身**出了问题。
#
# 为什么这类最危险：passed 是会进报表的数字。假通过会同时污染通过率和真缺陷率，
# 而且用户从界面上看不出任何异常 —— 报告一片绿，绿得毫无根据。

# agent 自述里的"放弃/没做到"信号。刻意**只认明确��废**的表述，
# 因为误伤通过的代价（让真通过变失败）远大于漏过一次假通过。
_GAVE_UP_RE = re.compile(
    r"(?:未能|未能|无法|没能|没有)\s*(?:加载|访问|进入|打开|到达|完成|验证|核对|读取|获取)"
    r"|页面\s*(?:一直)?(?:显示|停留在?|是)?\s*[^。；\n]{0,12}"
    r"(?:正在加载中|加载中请稍后|加载失败|无法访问|打不开)"
    r"|加载\s*(?:失败|超时|不完整)"
    r"|仍停留在?[^。；\n]{0,10}(?:登录页|加载|空白)"
    r"|页面\s*(?:一直)?(?:没有|未)\s*(?:渲染|加载出|出现)"
)

# 判定理由里的"我看到了预期结果"信号。必须两者同时出现才判矛盾 ——
# 只看一边会产生大量误判（agent 说自己没做到、判定器认可，那是正常的）。
_JUDGE_SAW_PASS_RE = re.compile(
    r"(?:符合预期|与预期一致|满足预期|符合\s*EXPECTED|与\s*EXPECTED\s*一致"
    r"|已成功进入|已加载|已到达|显示正常|验证通过|核对无误|已达成预期)"
)


def find_contradiction(
    final_answer: str,
    judge_reason: str,
) -> tuple[str, str] | None:
    """自述说"没做到"而判定说"做到了" → 返回 (原因, 建议动作)，否则 None。

    ★只在**判定器判通过**时才有意义（passed 才需要翻案）。
      判定失败时 agent 说"没做到"完全正常，不算矛盾。
    """
    if not (final_answer or "").strip():
        return None
    if not _GAVE_UP_RE.search(final_answer):
        return None
    if not _JUDGE_SAW_PASS_RE.search(judge_reason or ""):
        return None
    return (
        "agent 自述明确表示未能完成（页面未加载/无法访问/未读到结果），"
        "而判定理由却称看到了预期结果",
        "failed",
    )


# ════════════════════════════════════════════════════════════════════
# 第四层：结论其实是工具回显（agent 根本没写结论）
# ════════════════════════════════════════════════════════════════════

# 现场 result 2：final_answer = `Clicked button "查询"` —— 19 个字符，
# 就是一行工具回显。判定器照样给了 passed 并写了一段像模像样的核对理由。
#
# 这类"空结论"不该被判通过：判定器是拿截图在推理，不是在读 agent 的观察。
# 它判passed 时的理由质量再高，也只是**从截图里猜的**，而截图可能来自
# 任何时刻（比如展开下拉的那一刻）。

# 单行动作回显的形态：`Clicked xxx` / `Sent keys: X` / `Input: x` / `🔗 Navigated to x`
_ECHO_ONLY_RE = re.compile(
    r"^(?:Clicked|Sent keys|Input|Typed|Pressed|Scrolled|Selected|Extracted)"
    r"[^\n]{0,120}$",
    re.I,
)
_NAV_ONLY_RE = re.compile(r"^(?:🔗\s*)?Navigated\s+to\s+\S{0,200}$", re.I)
# 正常的结论至少要说清"看到了什么"。
# ★ 门槛定25 而不是 40：实测「列名符合预期，通过。」只有 10 个字符却是合法的通过，
# 门槛太高会把这类短结论误杀。25 是个实测平衡点 —— 比"单行动作回显"长，
# 又比正常的核对结论短。
_MIN_ANSWER_CHARS = 25

# 结论性表述：出现了它就说明 agent 真的下了判断，而不是只回显了一个动作。
# 这类词在中文报告里很稳定（判定理由也在用同一套，见 _JUDGE_SAW_PASS_RE）。
_CONCLUSION_RE = re.compile(
    r"(?:符合预期|与预期一致|满足预期|一致|不符|不通过|通过|失败|成功|已完成|未完成"
    r"|确认|核对|结果|第\s*\d+\s*列|\d+\s*条|列名|列数|顺序)"
)


def looks_like_echo_only(final_answer: str) -> str:
    """final_answer 只是工具回显就返回其形态，否则返回空串。

    ★ 顺序有意：先判形态（回显），再判长度。形态判据是**结构性**的
    （整句就是一个 `Clicked xxx`），比长度可靠；长度只是兜底。
    """
    t = (final_answer or "").strip()
    if not t:
        return "empty"
    if _ECHO_ONLY_RE.match(t) or _NAV_ONLY_RE.match(t):
        return "tool_echo"
    # 长度兜底：单行且极短。**但要先看有没有结论性表述**——
    # 「列名符合预期，通过。」只有 10 个字符，可它确实给出了结论，
    # 按纯长度判会误杀（实测过）。所以只在"既短、又没有任何结论词"时才算空。
    if len(t) < _MIN_ANSWER_CHARS and "\n" not in t and not _CONCLUSION_RE.search(t):
        return "too_short"
    return ""


def echo_only_note(kind: str) -> str:
    """给判定器的提示。"""
    if not kind:
        return ""
    label = {
        "empty": "final_answer 为空",
        "tool_echo": "final_answer 只是工具回显（如一行 `Clicked button \"查询\"`），"
                     "agent 根本没有给出观察结论",
        "too_short": f"final_answer 单行且短于 {_MIN_ANSWER_CHARS} 字符，看不出它核对到了什么",
    }.get(kind, kind)
    return (
        f"【agent 未给出结论】{label}。此刻你唯一的依据是截图，而截图反映的是"
        f"**某一刻**的页面状态（可能是展开下拉、弹窗打开的中间态），"
        f"不是 agent 核对后的结果。判 passed 属于**无依据的通过**。"
        f"正确做法：failed + agent_incomplete，说明 agent 没有完成核对。"
    )


# ════════════════════════════════════════════════════════════════════
# 汇总闸门：闸门优先于判定器
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Gate:
    """一条硬闸门。命中即改判，不再采信判定器。"""

    gate: str
    reason: str
    root_cause: str


def override_gate(
    final_answer: str,
    evidence: list[str] | None,
    verdict_status: str,
    verdict_reason: str,
) -> Gate | None:
    """判定器出结果**之后**再跑一次：只抓"无依据的通过"。

    与 find_unverified_claims / find_premature_done 的区别：那两层是给判定器**提示**，
    本层是**直接改判** —— 因为它们对应的是"判定器被自己没看见的东西骗了"，
    提示它反而会被它自己反驳（它会坚持说"我确实看到了"）。

    ★只在 verdict_status == "passed" 时动手：假通过必须拦，假失败不拦
      （误伤通过的代价远大于漏过一次假通过）。
    """
    if verdict_status != "passed":
        return None

    # ① agent 压根没给结论 → 无依据的通过
    kind = looks_like_echo_only(final_answer)
    if kind:
        return Gate(
            gate="no_agent_conclusion",
            reason=echo_only_note(kind),
            root_cause="agent_incomplete",
        )

    # ② agent 说没做到，判定说做到了 → 直接矛盾
    c = find_contradiction(final_answer, verdict_reason)
    if c:
        why, action = c
        return Gate(
            gate="self_contradiction",
            reason=(
                f"【自述与判定矛盾】{why}。判定理由：{verdict_reason[:160]}。"
                f"两者的观察不可能同时成立 —— 以自述为准（它记录了 agent 实际看到什么），"
                f"判 failed。"
            ),
            root_cause="agent_incomplete",
        )
    return None
