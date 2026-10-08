"""Write a human-readable bug report for a FAILED case.

The user's ask, verbatim, was for output shaped like this:

    【操作步骤】：登录admin->进入培训资源管理->进入资源审批配置->编辑修改列表中已经存在的
                 审批规则在审批规则中改写资源配置->点击新建规则->选择已有的资源配置
                 (刚改写的那条规则所占用的资源范围)
    【实际结果】：可以选择已占用的资源范围，虽不可配置，但是报错也为明显指出具体错误
    【预期结果】：本来应该显示已配置或者如果可以点击，提示报错应该说明清楚

What makes that example good, and what this module has to reproduce:
  1. 【操作步骤】 is a `->`-separated breadcrumb of what the tester DID, in business
     language ("进入资源审批配置"), not browser language ("Clicked div role=tab"). The
     agent's action log is full of the latter, so translating between the two is the
     whole job. Menu paths, field names and button labels come from the step log; the
     connective tissue does not.
  2. 【实际结果】 describes what the SYSTEM did, including the defect: "虽不可配置，但是
     报错也为明显指出具体错误". It is a description, not a verdict — no "测试失败".
  3. 【预期结果】 states what SHOULD have happened, inferred from the case's own intent.

Two failure modes this prompt has to defend against, both observed with real step logs:

  * **Hallucinated steps.** A 4-step run whose agent wandered produces a confident
    8-step narrative if the model is allowed to infer intent. Every step must be
    traceable to a logged action; the prompt says so and the schema asks for the log
    line each step came from.
  * **Written from the judge's verdict.** The verdict says "failed", so the model writes
    "the test failed because…" instead of describing the page. The actual result must
    describe observable behaviour only.

Only failed/errored cases get a narrative — a passed case needs no bug report, and
generating one wastes a model call per case.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.failure_attrib import prompt_hint
from app.llm import llm_config, openai_client

# The output contract. Kept as a dataclass so the executor can store it as JSON and the
# frontend can render three labelled sections without parsing prose.
@dataclass(frozen=True)
class FailureNarrative:
    steps: str  # 操作步骤 — `->`-separated path
    actual: str  # 实际结果
    expected: str  # 预期结果
    title: str = ""  # one-line summary, useful as a bug-list headline
    severity: str = ""  # 阻塞 | 严重 | 一般 | 轻微  (best-effort)
    # Who caused it — "system" / "setup" / "transient" / "agent" / "unknown".
    #
    # ★ This is what stops the tool from filing a failure the developer must triage.
    # Measured twice on 2026-10-07, both non-defects:
    #   * a list read mid-request showed 「共 0 条」 → the trainee "didn't exist";
    #   * an import that was never submitted for approval → "data was not saved".
    # Both read exactly like data-loss defects in the bug list. A tester who cannot
    # tell them apart stops trusting the tool and starts re-checking everything, which
    # is worse than having no report at all — and a real defect sitting beside two fake
    # ones is the one that gets missed.
    attribution: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "steps": self.steps,
            "actual": self.actual,
            "expected": self.expected,
            "title": self.title,
            "severity": self.severity,
            "attribution": self.attribution,
        }

    def is_empty(self) -> bool:
        return not (self.steps or self.actual or self.expected)

    @property
    def fileable_as_defect(self) -> bool:
        """Only a ``system`` attribution may be filed as a product defect."""
        from app.failure_attrib import VERDICT_INFO

        return VERDICT_INFO.get(self.attribution, VERDICT_INFO["unknown"])[1]


_SYSTEM = """你是一名资深测试工程师，负责为**失败的**自动化测试用例撰写缺陷描述，供开发同学直接阅读。

严格按照下面的格式输出，只输出一个 JSON 对象：

{
  "title": "一句话概括缺陷（不超过40字，不要包含『测试失败』这类字样）",
  "severity": "阻塞|严重|一般|轻微",
  "steps": "操作步骤，用 -> 连接，写成业务语言",
  "actual": "实际结果",
  "expected": "预期结果"
}

【篇幅要求 —— 请严格遵守】
测试员要把这三段**直接粘进缺陷单**，所以越短越好，长了解释没人看。
- steps：**一句话**，不超过 60 字，只用 `->` 连接关键节点，不要加括号补充说明。
- actual：**一句话**，不超过 60 字，直接说现象本身。
- expected：**一句话**，不超过 60 字，直接说本该怎样。
- 禁止出现「经排查」「整体来看」「综上」「疑似」「可能是由于」这类过渡/推理语气词。
- 禁止复述日志、禁止解释你是怎么得出结论的、禁止罗列多条编号结论。
- 示例（这就是期望的长度）：
  steps:    登录admin->进入培训资源管理->进入资源结算配置->点击批量停用
  actual:   这两个按钮直接处于禁用状态
  expected: 未勾选数据时应提示用户先勾选，而不是把按钮直接禁用

【操作步骤】的写法（最重要，请仔细遵守）：
- 用 `->` 连接，从登录开始，例如：登录admin->进入培训资源管理->进入资源审批配置->点击新建规则
- 必须是**业务语言**：菜单名、页面名、字段名、按钮名。不要出现 "Clicked"、"div"、"role=button"、
  "index=3"、"Typed" 这类浏览器/框架词汇，要把它们翻译成用户看到的中文界面文字。
- 只能写**日志里真实发生过**的步骤。绝对不要根据用例标题去补想步骤、不要推断"应该是先进入哪里"。
  如果日志里只有 4 步，就只写 4 步。宁可少写，也绝不能编造。
- 如果某一步日志中提到了弹窗、下拉框、选项名称，要把名称写进去（如：选择已有的资源配置）。
- 日志的每一条都带 `第N步:` 前缀，必须按 N 的顺序写，不要跳步、不要重排。
- `【】` 里是浏览器动作名（如 click、input），只用来判断这一步是"点击"还是"输入"，
  **不要把这个词写进结果**；要写的是同一条里的界面文字（引号里的按钮名、菜单名、字段名）。

【实际结果】的写法：
- 描述**系统实际表现出的行为**，尤其是缺陷现象本身。
- 只描述可观察到的现象，不要写"测试失败"、"断言不通过"、"预期不符"这类结论性的话。
- 如果日志或截图里有报错信息、提示文案，把它引用出来（有原文就写原文）。
- 如果现象是"可以操作但没有校验"、"报错不明确"、"按钮可点但无效"这类，要准确表达出来。

【预期结果】的写法：
- 写**系统本该有的正确行为**，从用例的意图和业务常识推断。
- 同样用业务语言，不要写"应该返回200"这类技术描述，除非用例本身就是接口测试。

【重要】如果提供的证据不足以判断，就把不确定的地方写得保守、概括，
**不要编造具体的报错文案或页面元素**。宁可写得笼统，也不要虚构细节。

【★ 归因 —— 比措辞更重要的一条】
用户消息里会带一行【归因】。它已经由代码判定好了这次失败**是谁造成的**：
- 「系统行为」：可以按缺陷线索写，这是产品自己的表现。
- 「流程未走完」「页面未就绪」「执行方问题」：**这不是产品缺陷**。开发没有东西可改。
  你要如实描述现象，但绝不能把它写成产品应该怎样——那会让开发去改一个没坏的
  地方，浪费一次排查，并且把真的缺陷挤下去。
- 「待人工确认」：证据不足，描述保持保守，**不要断言是缺陷，也不要断言不是**。
**无论哪种归因，都不要在输出里提「归因」「执行方」「自动化脚本」这些词**——
写给开发看的是现象，分类是我们内部的事。
"""


def build_prompt(
    case_name: str,
    task: str,
    expected: str,
    final_answer: str,
    actions: list[str],
    evidence: list[str],
    judge_reason: str = "",
    attribution: str = "",
    shot_hint: str = "",
) -> str:
    """The narrative writer's user message. Split out so wiring is testable without an LLM.

    ``attribution`` is the pre-computed verdict from :mod:`app.failure_attrib`. It is
    spliced in as a line of the payload rather than only used to shape the system
    prompt, so the verdict travels with the evidence even if a future refactor stops
    rewriting ``_SYSTEM``.

    ``shot_hint`` is the caller-supplied observation about the final frame (see
    :func:`app.shot_probe.screenshot_hint`). It rides in the payload for the same
    reason: the writer gets the image, but an image it cannot trust, and a blank frame
    that failed to load is indistinguishable from a loaded one by looking alone.
    """
    payload: dict[str, Any] = {
        "用例名称": case_name,
        "用例任务": task,
        "用例预期": expected,
        "智能体最终回复": final_answer,
        "判定理由": judge_reason,
        # The raw material for 【操作步骤】. Capped: a 200-step log does not improve the
        # narrative and does cost tokens on every failed case.
        "动作日志": [a for a in actions if a][:100],
        # The browser's own observations — the only trustworthy source of UI text.
        "步骤观察": [e for e in evidence if e][:100],
    }
    if attribution:
        payload["【归因】"] = prompt_hint(attribution)
    if shot_hint:
        payload["【截图】"] = shot_hint
    return json.dumps(payload, ensure_ascii=False)


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


async def describe_failure(
    case_name: str,
    task: str,
    expected: str,
    final_answer: str,
    actions: list[str],
    evidence: list[str],
    judge_reason: str = "",
    screenshot_b64: str | None = None,
    attribution: str = "",
    shot_hint: str = "",
) -> FailureNarrative:
    """Generate the 操作步骤/实际结果/预期结果 narrative for one failed case.

    Never raises: a narrative is a nice-to-have attached to a failure, and losing it must
    not turn one failed case into a crashed run. Returns an empty narrative on any error.

    ``attribution`` — computed by the caller via :func:`app.failure_attrib.classify` —
    rides along into the prompt so the writer does not polish a non-defect into a
    confident fake bug. It is echoed back on the result so the report can label it.

    ``shot_hint`` — one line from :func:`app.shot_probe.screenshot_hint`, e.g. that the
    final frame is blank. Kept as text rather than left to the writer's reading of the
    image: a vision-capable model looking at a white screenshot will happily describe a
    working page in confident detail, and it has no way to know the frame failed to load.
    """
    if not get_settings().failure_narrative_enabled:
        return FailureNarrative(steps="", actual="", expected="", attribution=attribution)
    user = build_prompt(
        case_name,
        task,
        expected,
        final_answer,
        actions,
        evidence,
        judge_reason,
        attribution,
        shot_hint,
    )
    # 从这里开始的任何失败（含建客户端本身）都不能抛出去。
    #
    # 这两行原来在 try 外面，于是网关连不上时——正是最需要叙述的那天——
    # describe_failure 会带着 RuntimeError 直接抛出，docstring 里写的
    # "Never raises" 变成一句空话，整个用例执行被叙述功能带崩。
    # 失败叙事是挂在失败结果上的附加物，丢它不能反过来毁掉这次执行。
    client: Any = None
    try:
        cfg = await llm_config()
        client = await openai_client()
        content: Any = user
        if screenshot_b64:
            # The screenshot is what makes 【实际结果】 concrete — it is where the error
            # toast / disabled field / empty list is actually visible. Passed last because
            # a gateway without vision must still get a usable text-only attempt.
            content = [
                {"type": "text", "text": user},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                },
            ]

        async def ask(c: Any) -> FailureNarrative:
            resp = await client.chat.completions.create(
                model=cfg.model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": c},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            return FailureNarrative(
                steps=_clip(str(data.get("steps", "")), 1200),
                actual=_clip(str(data.get("actual", "")), 800),
                expected=_clip(str(data.get("expected", "")), 800),
                title=_clip(str(data.get("title", "")), 200),
                severity=_clip(str(data.get("severity", "")), 20),
                attribution=attribution,
            )

        try:
            return await ask(content)
        except Exception:
            if not screenshot_b64:
                raise
            # Vision-less gateway: retry as text rather than losing the narrative.
            return await ask(user)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "failure narrative 生成失败（不影响用例结果）：%s: %s", type(exc).__name__, exc
        )
        return FailureNarrative(steps="", actual="", expected="", attribution=attribution)
    finally:
        # 建客户端就失败时 client 是 None —— 直接 client.close() 会 AttributeError，
        # 把上面刚吞掉的异常重新抛出来，等于白救。
        if client is not None:
            await client.close()
