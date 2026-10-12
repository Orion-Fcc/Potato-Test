r"""Tell apart "the system refused us" from "we never finished the setup".

Why this module exists
======================
Measured on a real internal SPA (2026-10-07), two failures looked identical from the
outside — the agent's final answer said some data was missing and the case failed —
and only one of them was a product defect:

1. A trainee was imported, the list showed``共 0 条``, and the agent concluded the
   trainee did not exist. **We** were wrong: the table was still fetching. Moments
   later the same page showed 36 rows with nothing changed.
2. An import was done, and the agent concluded the record was not saved. **We** were
   wrong again: the result page has both【保存】and【提交审批】, and the case only did
   the import — which is a draft. Nothing was submitted, so nothing was approved.

Both were reported as defects. Both were our own incompleteness. The cost is not the
false report itself: it is that a tester who learns to distrust the tool stops reading
its output, and a real defect sitting next to two fake ones gets missed.

So this module answers one question, from text only, before the narrative is written:
**who caused this failure?**

The four verdicts
-----------------
- ``system``     — the system said no, and the reason is a product behaviour.
- ``setup``      — we never completed a prerequisite. Retryable, **not** a defect.
- ``transient``  — the page was mid-render / mid-request. Retryable, not a defect.
- ``agent``      — the agent skipped, misread, or worked around something.
- ``unknown``    — genuinely undecidable from the available text.

Why "unknown" is a first-class answer
-------------------------------------
It is tempting to force every failure into one of the four. That would produce a
confident wrong label, and a wrong label here is worse than no label: it would file a
non-bug as a bug (or vice versa) with a straight face. ``unknown`` is returned when the
evidence is too thin, and the narrative prompt is told to write conservatively instead.

No I/O, no LLM
--------------
Same rule as :mod:`app.browser_binary`, :mod:`app.cdp_endpoint` and :mod:`app.page_gate`:
the classification must be testable without a browser, a network or a model. The LLM
sees the verdict as one more piece of context; it does not make the call.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("potato-test.attrib")

# ---------------------------------------------------------------------------
# Evidence signals
# ---------------------------------------------------------------------------
# Collected case-insensitively over the concatenated evidence text.

# A message the SYSTEM produced, addressed to the user. This is the strongest signal
# that the refusal was the system's own doing — a product usually validates and says so.
_SYSTEM_MESSAGE = re.compile(
    r"(系统提示|系统错误|错误提示|提示信息|操作失败|保存失败|提交失败|操作不允许|"
    r"不允许|权限不足|无权限|数据已存在|已存在|不能为空|必填|格式不正确|"
    r"校验失败|请输入|请选择|超出范围|不允许重复|重复提交)",
    re.IGNORECASE,
)

# The agent hitting a wall it never prepared for. Phrases the model uses when it tried
# something and the page refused it — a human writing a bug report does not say these.
_AGENT_STUCK = re.compile(
    r"(找不到|未找到|没有找到|没有该|不存在该|页面没有|看不到|找不到按钮|"
    r"未看到|没有显示|按钮不存在|元素不存在|无法定位|无法找到|"
    r"页面无响应|元素不可见)",
    re.IGNORECASE,
)

# The page was still working. Matches both the splash text and the classic
# virtual-table "no rows yet" state.
_TRANSIENT = re.compile(
    r"(正在加载中|正在加载|加载中请稍后|请稍后|loading\.\.\.|共\s*0\s*条|"
    r"暂无数据|无数据|数据加载中|请求中|处理中)",
    re.IGNORECASE,
)

# The agent narrating an action it skipped or invented. Kept separate from
# ``_AGENT_STUCK`` because the remedy differs: stuck → look harder; skipped → the
# case definition is at fault.
_AGENT_SKIPPED = re.compile(
    r"(没有点击|未点击|没有选择|未选择|没有填写|未填写|没有输入|未输入|"
    r"跳过|直接返回|提前返回|未等待|没有等待|未提交|没有提交|"
    r"改为|改用|绕过|跳过提交|仅做了导入|只做了导入)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# "agent 根本没给出结论" —— 2026-10-10 用户实测反馈后新增
# ---------------------------------------------------------------------------
#
# 用户发来的截图里有一条 P0：`新增: 名称留空 → 保存` 判 failed，理由是
#
#   「智能体只打开了『新增审批规则』弹窗并两次点击『新增规则』（中间点了一次取消），
#     始终没有点击『保存规则』，因此从未触发或观察到『规则名称』留空时的前端拦截
#     与高亮提示，最终截图仅显示空的弹窗，无任何校验信息。」
#
# 而它的 `final_answer` 是：**`Clicked button "新增规则"`** —— 一行原样的工具回显。
# 也就是说 agent 既没有做成这件事，也没有说出任何观察结论，
# 却在报告里和"被测系统真的有缺陷"看起来一模一样。用户的原话是：
#   「我不希望 agent 出现以下错误。」
#
# 这类结果必须单独归因。判据是**决定性**的、不看模型措辞：
#   `final_answer` 是"工具回显"而不是"观察结论"。
#
# 为什么这条要排在 _SYSTEM_MESSAGE 之前：这个形态下 ev（截图证据）往往是
# "弹窗是空的"这类文本，可能命中 不能为空/必填 等系统措辞，
# 于是被判成 system（被测系统的锅）—— 方向正好反了。
_TOOL_ECHO = re.compile(
    r"^\s*(?:"
    r"Clicked|Typed|Pressed|Selected|Navigated|Scrolled|Hovered|Uploaded|"
    r"点击了?|输入了?|选择了?|按下了?|导航到|滚动到|上传了?"
    r")",
    re.IGNORECASE,
)


def final_answer_is_tool_echo(final_answer: str) -> bool:
    """`final_answer` 是不是一行"工具回显"而不是观察结论。

    实测样例（都来自真库）::

        Clicked button "新增规则"
        Clicked button "查询"
        Typed "测试" into the search box

    为什么只认开头那个动词：工具回显的形状非常固定 —— 动词过去式开头 + 目标。
    而真正的结论会先说页面状态（"列表显示…"、"提示『名称不能为空』"）。
    这个判据是**决定性的**，不含任何模型措辞判断，所以不会随模型波动。
    """
    s = (final_answer or "").strip()
    if not s:
        return False
    # 多行的话只看第一行：工具回显永远是一行，而结论可能带换行。
    first = s.splitlines()[0].strip()
    # 太长的肯定不是单行回显（回显很少超过 80 字符）。
    if len(first) > 120:
        return False
    return bool(_TOOL_ECHO.match(first))


# 判定器明确说出"agent 没有给出结论/没有完成核对"。这是判定器拒收截图当证据时的措辞，
# 指的是 **agent 停了**，不是产品有问题。
_AGENT_NO_CONCLUSION = re.compile(
    r"(agent\s*(?:未|没有|并未)\s*(?:给出|提供)?\s*(?:结论|观察|核对)|"
    r"没有完成核对|未完成核对|没有给出观察结论|"
    r"最终回答(?:只是|仅为)工具回显|"
    r"agent\s*incomplete)",
    re.IGNORECASE,
)


# A write that reports success but never reaches a settled state.
# This is the shape of the measured false defect: the import dialog said
# 「导入成功 共 2 条」, the agent stopped there, and the list then showed nothing —
# so it filed "data was not saved". What was actually missing is the other half of
# that dialog's flow (【提交审批】), which the run never performed.
#
# "写成功了" + "结果是查不到" is the signature of an **unfinished** write, not a lost
# one: a genuinely dropped write shows the error toast or a success-with-nothing-added
# warning, and those land in ``_SYSTEM_MESSAGE`` first. Keyed on the pairing, not on
# either half, so a normal "saved and found" cannot match.
_WRITE_UNSETTLED = re.compile(
    r"(导入成功|新增成功|保存成功|提交成功|创建成功|写入成功)"
    r"[\s\S]{0,200}?"
    r"(查不到|看不到|没有|未生成|不存在|没保存|未保存|没有保存|列表为空|数据没)",
    re.IGNORECASE,
)

# The screenshot outvoted the text snapshot. Produced by app.shot_probe.evidence_line.
#
# ★ Matched on the 【截图与文本矛盾】 prefix rather than on message wording: the bracket
# tag is emitted only by `shot_probe`, so this cannot accidentally fire on an agent that
# happens to write something about screenshots, and it keeps the two modules' contract to
# a single agreed token.
#
# Its sibling 【截图未渲染】 is deliberately NOT matched here. That line means the frame
# drew nothing — which is also what a page that never finished loading produces — so it
# must never be read as the text being wrong. Matching both under one prefix is what made
# a blank frame behave like a contradiction.
_SHOT_CONTRADICTS_TEXT = re.compile(r"【截图与文本矛盾】", re.IGNORECASE)

# Explicit workflow vocabulary. In this system an import produces a DRAFT that only
# becomes a record after 【提交审批】 — the measured instance of a real
# "we never finished the setup" that read exactly like a data-loss defect.
#
# ★ These must be **state words**, not button names. An earlier version listed
# `提交审批` / `保存` plainly and misfired on the opposite case: "页面找不到『提交审批』
# 按钮" hit the word and got labelled `setup`, i.e. "we didn't finish the workflow" —
# when in fact the workflow was fine and the button was missing (a real defect). The
# distinguishing question is which side the sentence is on: "已提交审批/未提交审批"
# describes state, "找不到提交审批按钮" describes absence. So state forms only.
_SETUP_VOCAB = re.compile(
    r"(未提交审批|没有提交审批|提交审批后|审批未通过|审批被拒|审批中|"
    r"草稿态|仍是草稿|保存为草稿|待提交审批|"
    r"前置条件|先登录|重新登录|会话过期|登录已失效|token失效|未登录|"
    r"从未进入|未进入该页面|还在上一个页面)",
    re.IGNORECASE,
)

# An error the browser/automation layer raised, not the application. These are ours:
# a selector that no longer matches, a CDP timeout, a stalled navigation.
_AUTOMATION_ERROR = re.compile(
    r"(net::ERR_|CDP|Cannot read propert|is not defined|is not a function|"
    r"element is not|waiting for locator|Timeout.*exceeded|"
    r"Target closed|Browser closed|Connection refused)",
    re.IGNORECASE,
)


def classify(
    evidence_text: str,
    *,
    action_text: str = "",
    final_answer: str = "",
) -> str:
    """Return one of ``system`` / ``setup`` / ``transient`` / ``agent`` / ``unknown``.

    Deliberately conservative and ordered. The order below is the whole design: the
    first matching question is the most specific one, and a weaker signal never
    overrides a stronger one.

    1. An automation-layer error is always ours. A ``net::ERR_PROXY_CONNECTION_FAILED``
       says nothing about the product.
    2. A skip/self-narration signal means we did not do the work — the case failed for
       a reason the developer does not own.
    3. Setup vocabulary (审批 /登录失效 / 前置条件) means a prerequisite was missing.
    4. A loading/empty state means we read the page too early.
    5. A system message means the product answered, and its answer is the subject.
    6. "Can't find it" on its own is ambiguous, and only becomes ``agent`` when the
       text also shows we were still acting.
    """

    ev = (evidence_text or "")[:8000]
    act = (action_text or "")[:4000]
    fin = (final_answer or "")[:2000]
    # Evidence first: it is the browser's own read-back and therefore the most
    # trustworthy. Actions and the final answer are the agent talking about itself.
    hay = f"{ev}\n{act}\n{fin}"

    # 0. The screenshot contradicted the text.
    #
    # This has to sit above everything else, and it was a real bug before it did.
    # The evidence line says 「页面文本称查不到数据，但最终截图显示页面有内容 ——
    # 以文本快照为准的结论不可信」, but "查不到" itself still matched _AGENT_STUCK
    # below, so the run was attributed to the agent — i.e. the corroboration was
    # overruled by the very text it was corroborating against, and the signal did
    # nothing at all in the one case it was built for.
    #
    # It resolves to `agent` rather than `system` or `unknown` on purpose: the finding
    # is still not a product defect (we did not look at the right thing), but neither
    # is it simply the agent failing to act — the text snapshot lied. That is our bug,
    # so the remedy is the same bucket with a different reason string.
    if _SHOT_CONTRADICTS_TEXT.search(hay):
        return "agent"

    # 1. automation layer
    if _AUTOMATION_ERROR.search(hay):
        return "agent"

    # 1b. ★ agent 根本没给出结论（2026-10-10 用户实测反馈后新增）。
    #
    # `final_answer` 只是一行工具回显（`Clicked button "新增规则"`），
    # 说明 agent 既没做成这件事、也没说出任何观察结论。
    # 这种结果在报告里必须和"被测系统真的有缺陷"分开 ——
    # 否则用户拿到的 P0 红条全部是 agent 自己没做完，产品被冤枉。
    #
    # 位置说明：放在 _SYSTEM_MESSAGE 之前，因为这个形态下截图证据常写着
    # "弹窗是空的"，会命中 不能为空/必填 等系统措辞，被判成 system（方向正好反了）。
    # 放在 _AGENT_SKIPPED 之后：如果文本明确说了"没有点击保存"，
    # 那说明用例/流程定义有问题，归 setup 更准确。
    if final_answer_is_tool_echo(fin):
        return "agent"

    # 2. we said we skipped something
    if _AGENT_SKIPPED.search(hay):
        return "setup"

    # 2c. the same "no conclusion" shape, seen from the judge's side.
    #
    # The judge writes 「agent 未给出结论」/「说明 agent 没有完成核对」 when it refuses to
    # accept a screenshot as evidence. That wording means the *agent* stopped, not the
    # product — and without this the case falls through to `unknown`, which tells the
    # operator nothing actionable.
    if _AGENT_NO_CONCLUSION.search(ev):
        return "agent"

    # 2b. a write that reported success but never settled.
    #     Checked BEFORE _SYSTEM_MESSAGE: when a dialog says 「导入成功」 the literal
    #     word 成功 would otherwise trip a success-ish pattern, and when the follow-up
    #     says 「数据没有保存」 that trips the message patterns — the pair is what
    #     actually identifies an unfinished workflow.
    if _WRITE_UNSETTLED.search(hay):
        return "setup"

    # 3. a prerequisite was not in place
    if _SETUP_VOCAB.search(ev) or _SETUP_VOCAB.search(fin):
        return "setup"

    # 4. the page was still working
    if _TRANSIENT.search(ev):
        return "transient"

    # 5. the product answered, and its answer is the finding
    if _SYSTEM_MESSAGE.search(ev):
        return "system"

    # 6. "找不到" only counts against us when we were still moving. Alone it is
    #    just as likely to describe a genuine missing feature.
    if _AGENT_STUCK.search(hay) and (act.strip() or fin.strip()):
        return "agent"

    return "unknown"


# What each verdict means for the reader, and — more importantly — whether it may be
# filed as a defect. Used both in the log line and in the narrative prompt.
VERDICT_INFO: dict[str, tuple[str, bool, str]] = {
    # verdict: (中文标签, 是否可作为缺陷上报, 给叙述模型的一句话)
    "system": ("系统行为", True, "这是系统自身的表现，按实际现象描述即可。"),
    "setup": ("流程未走完", False, "这是前置条件/流程未走完造成的，**不是缺陷**。"),
    "transient": ("页面未就绪", False, "这是页面还没加载完造成的，**不是缺陷**。"),
    "agent": (
        "执行方问题",
        False,
        "这是执行侧的问题（选择器失效/未按流程操作/页面快照与实际渲染不一致），"
        "**不是缺陷**。",
    ),
    "unknown": ("待人工确认", False, "证据不足以判断，请保守描述、不要下结论。"),
}

# Extra sentence injected into the narrative prompt, keyed by verdict. The point is
# that the narrative writer must NOT be told "write a bug report" for a verdict that is
# not a defect — otherwise a ``setup`` failure gets polished into a confident fake bug.
PROMPT_HINT: dict[str, str] = {
    "system": (
        "【归因】判定为**系统行为**：可以把现象按实际看到的样子写清楚，"
        "这是有效缺陷线索。"
    ),
    "setup": (
        "【归因】判定为**流程未走完**，这不是产品缺陷。"
        "写【实际结果】时如实描述页面现象，但【预期结果】要写清楚正确的完整操作路径，"
        "**不要**写成产品应该怎样、否则会误导开发去改一个没坏的地方。"
    ),
    "transient": (
        "【归因】判定为**页面未就绪**（读得太早），这不是产品缺陷。"
        "如实描述当时看到的现象即可，**不要**写成产品数据丢失或功能缺失。"
    ),
    "agent": (
        "【归因】判定为**执行方问题**（自动化脚本侧），这不是产品缺陷。"
        "如实描述现象，并在【实际结果】里点明是自动化执行未能完成，"
        "**不要**写成产品功能有问题。"
    ),
    "unknown": (
        "【归因】**证据不足**，无法判断是产品问题还是执行问题。"
        "描述要保守，**不要**断言是缺陷，也不要断言不是。"
    ),
}


def describe(verdict: str) -> str:
    """One-line log wording, e.g. ``transient(页面未就绪，不可上报为缺陷)``."""
    label, fileable, _ = VERDICT_INFO.get(verdict, VERDICT_INFO["unknown"])
    tail = "可上报为缺陷" if fileable else "不可上报为缺陷"
    return f"{verdict}({label}，{tail})"


def prompt_hint(verdict: str) -> str:
    """The paragraph to splice into the narrative prompt for this verdict."""
    return PROMPT_HINT.get(verdict, PROMPT_HINT["unknown"])