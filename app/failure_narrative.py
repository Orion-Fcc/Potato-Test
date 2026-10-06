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

    def as_dict(self) -> dict[str, str]:
        return {
            "steps": self.steps,
            "actual": self.actual,
            "expected": self.expected,
            "title": self.title,
            "severity": self.severity,
        }

    def is_empty(self) -> bool:
        return not (self.steps or self.actual or self.expected)


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
"""


def build_prompt(
    case_name: str,
    task: str,
    expected: str,
    final_answer: str,
    actions: list[str],
    evidence: list[str],
    judge_reason: str = "",
) -> str:
    """The narrative writer's user message. Split out so wiring is testable without an LLM."""
    return json.dumps(
        {
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
        },
        ensure_ascii=False,
    )


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
) -> FailureNarrative:
    """Generate the 操作步骤/实际结果/预期结果 narrative for one failed case.

    Never raises: a narrative is a nice-to-have attached to a failure, and losing it must
    not turn one failed case into a crashed run. Returns an empty narrative on any error.
    """
    if not get_settings().failure_narrative_enabled:
        return FailureNarrative(steps="", actual="", expected="")
    user = build_prompt(case_name, task, expected, final_answer, actions, evidence, judge_reason)
    cfg = await llm_config()
    client = await openai_client()
    try:
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
        return FailureNarrative(steps="", actual="", expected="")
    finally:
        await client.close()
