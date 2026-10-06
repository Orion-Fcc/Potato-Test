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
