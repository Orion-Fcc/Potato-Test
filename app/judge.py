"""LLM-as-judge: given the expected outcome and what the agent actually did,
decide pass / fail. Errs toward 'failed' when evidence is thin (design doc §5.4)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.llm import llm_config, openai_client

# 2026-10-08 补：下面"无依据的通过 → 翻案"那条分支在用它，但此前**从未定义** ——
# 也就是说 gate 一旦真的拦下一条假通过，这里就抛 NameError，翻案逻辑从来没生效过。
# 是 ruff 的 F821（未定义名字）把它翻出来的，不是覆盖率。
log = logging.getLogger("potato-test.judge")

_SYSTEM = (
    "You are a strict QA judge for an automated browser test. "
    "Given the test's EXPECTED outcome and the agent's FINAL result plus its action log, "
    "decide whether the test passed. Pass ONLY if the evidence clearly shows the expected "
    "outcome was achieved. If the evidence is missing, ambiguous, or the agent errored, "
    'return failed. Reply with JSON only: {"status": "passed"|"failed", "reason": "<one sentence>"}.'
    # Without these rules an empty `expected` made every case unpassable: there was no
    # criterion to match and nothing but the agent's own claim to match it against, so the
    # judge always answered "no expected outcome / only self-reported success".
    " When EXPECTED is empty or missing, judge against TASK instead: the test passes when "
    "the evidence shows the agent demonstrably did what TASK asked. Never fail a test merely "
    "because EXPECTED was not filled in. "
    "STEP_RESULTS are the browser's own observations — what was actually clicked or typed, "
    "the text it read back, and any errors. They are evidence. FINAL_ANSWER is only the "
    "agent's self-report; corroborate it against STEP_RESULTS rather than dismissing it. "
    # The screenshot is why a run that visibly created its record used to fail: the proof was
    # on screen and the judge could not see it, so it kept asking for "browser evidence".
    "The attached image is the FINAL SCREENSHOT of the page when the run ended — the page's "
    "own state, not a claim. It is the strongest evidence you have: read it. If it shows the "
    "expected outcome (the new row in the list, the success dialog, the error message, the "
    "disabled field), that settles it and you should pass. Judge on the evidence you were "
    "given; do not fail a run for lacking a particular form of proof it was never asked to "
    "produce. Note that the screenshot is the LAST frame only — an outcome that appeared "
    "earlier and was then dismissed will not be in it, so weigh STEP_RESULTS for those."
    # ── 加载占位不是"到达"（2026-10-06，实测两次假通过）────────────────────────
    # 现场（run 10 / result 187 与 result 116）：agent 自述"页面只显示
    # 『正在加载中请稍后......』、0 个可交互元素，我无法核对"，判定器两次都判 passed，
    # 理由分别是"截图显示已到达资源审批配置列表"和"截图显示已成功进入…展示 9 条数据"
    # —— 截图里根本没有列表。判定器是从agent 的一句话去反推"截图里应该有什么"，
    # 然后按想象补齐了结论。
    #
    # 这条约束是第一道防线的补充：确定性闸门（app/judge_gate.py）已经会拦下
    # "全篇无成功陈述 + 明确放弃"和"最终态仍卡在加载占位"两种情况，
    # 但闸门管不了"agent 嘴上说做到了、截图其实不对"的情形。
    "CRITICAL — a page still showing only a loading placeholder (e.g.『正在加载中请稍后』) "
    "or '0 interactive elements' has NOT loaded. You MUST NOT describe such a page as "
    "'reached the target page', 'the list is displayed', or any similar claim — that is "
    "the single most common way this judge has fabricated evidence. If the final screenshot "
    "shows a loading placeholder or an error page, the run FAILED regardless of what "
    "FINAL_ANSWER claims. "
    # ── agent 的自述与截图冲突时，以能核实的为准 ──────────────────────────────
    # 上面那条是本项目最贵的一课：判定器被训练成"要给出答案"，
    # 于是在证据不足时用想象补全，而不是承认证据不足。
    "If FINAL_ANSWER and the screenshot/STEP_RESULTS disagree, prefer the ones you can "
    "actually verify, and say plainly in the reason which source you relied on. "
    "When the page state is ambiguous, failed with an honest 'the evidence does not show it' "
    "is the correct answer — a confident but invented pass is the worst possible outcome, "
    "because it lets a real defect reach the report as green. "
    # ── 编造的"数据"（2026-10-06 现场，run 12 / case 4）─────────────────────
    # 实测：agent 声称"查询后仍显示共 11 条"，而"11"在 STEP_RESULTS 里**根本不存在** ——
    # 它点完「查询」就宣布完成，从没读过结果页，那 11 条是它自己算的。
    # 判定器采信了这份自述，于是报出 product_defect —— **一个并不存在的缺陷**。
    #
    # 为什么上面那条"do not invent anything"没拦住：它约束的是"别编动作"，
    # 而这里编的是**结论里的数字**。数字带着笃定的语气和规整格式，
    # 读起来就像一条真实观察。必须单独点名。
    "CRITICAL — a number in FINAL_ANSWER is a CLAIM, not evidence. Before you accept any "
    "count, row total, or '共 N 条', find it in STEP_RESULTS or the screenshot. If the agent "
    "quotes numbers that appear nowhere in the observed steps, it did not read that page — "
    "it computed or imagined the result. In that case you MUST NOT report "
    "product_defect: a system cannot contradict an expected result that was never observed. "
    "The correct verdict is failed with root_cause=agent_incomplete (it stopped before "
    "reading the result), and your reason must say the conclusion was unverified. "
    "Symmetrically, do not accept a passing verdict built on numbers that were never seen. "
    "When a message tagged [确定性校验发现] appears, treat every number it lists as UNVERIFIED. "
    # ── 执行未落位（同一现场的第二个信号）──────────────────────────────────
    # case 4 最后一步 action=done、result 却是 Clicked button「查询」——
    # 它在操作真正生效**之前**就宣布完成了。这比"数字对不上"更硬：
    # 那是不需要任何语义理解的**时序事实**。
    "SECOND TRAP — the agent declares completion (action=done) while the same step's result "
    "still shows an operation being performed. That means it announced SUCCESS before the "
    "operation took effect, so it NEVER SAW the post-operation state. Any conclusion it draws "
    "about that state is a guess. When you see this, the verdict is failed with "
    "root_cause=agent_incomplete, and your reason must say the agent finished before the "
    "operation landed — never product_defect, because nothing was observed to contradict "
    "anything. "
    # ── 反编造约束 ────────────────────────────────────────────────────────────
    # 实测抓到过判定器凭空补操作：某次 agent 的全部输出只有
    #   "Clicked input type=text role=combobox id=el-id-6768-51"
    # 步骤里根本没有点击「查询」，判定却写成"且点击「查询」后列表仍显示全部 10 条记录"
    # ——动作是编的，那个"10 条记录"的观察更是无从产生。
    # 危害比"结果不稳定"更大：报告里的失败理由是编的，人会照着它去查根本不存在的问题。
    "CRITICAL — do not invent anything. Your reason may ONLY describe actions and observations "
    "that literally appear in STEP_RESULTS, or things visible in the attached screenshot. "
    "If the agent never performed an action, say plainly that it did not, and stop there — "
    "do NOT describe what would have happened if it had. Never state a result, a row count, a "
    "page state or a validation message that no step recorded and the screenshot does not show. "
    "When evidence is thin, the correct answer is failed with a reason that says the evidence is "
    "missing — an honest 'it did not do this' is far more useful than a confident wrong story. "
    # 让它引用依据，便于事后核对
    "Also return \"evidence\": the 1-based indices of the STEP_RESULTS entries you relied on, "
    "so a human can check your reasoning. "
    # ── 失败类型（决定要不要重试）────────────────────────────────────────────────
    "Also return \"evidence_gap\": a boolean. Set it true ONLY when the run failed because the "
    "agent never actually got there — it stopped early, a control it needed never got used, the "
    "final screenshot shows it still on the previous screen, or there is simply nothing in the "
    "record to judge against. Set it false when the agent DID complete the work and the observed "
    "result genuinely differs from EXPECTED — that is a real finding and re-running it would "
    "only waste time. If you are unsure which, prefer false (do not re-run real failures). "
    # ── 失败根因分类（2026-10-04）──────────────────────────────────────────────
    # 目的：报告要按类分组，让测试工程师一眼分清"哪些该找开发、哪些是自己环境的问题"。
    # 互斥单选，不许猜 —— 猜错的分类会让真缺陷被归进"环境问题"里被忽略。
    "Also return \"root_cause\": pick EXACTLY ONE of these keys for WHY it failed. "
    "On a passed case, use an empty string. "
    "\n  product_defect        — the agent really did perform the intended steps and the "
    "system's behaviour genuinely contradicts EXPECTED. This is a real bug in the system "
    "under test. Choose this ONLY when the agent verifiably reached the state being judged. "
    "\n  agent_incomplete      — the agent itself did not finish: it stopped early, never "
    "used a control it needed, or the last screenshot shows it still on an earlier screen. "
    "\n  evidence_insufficient — the record contains nothing that could prove or disprove "
    "EXPECTED (e.g. the page never rendered, the snapshot is empty). You cannot tell whether "
    "the system is correct or not. "
    "\n  precondition_missing  — a precondition the task depended on was absent and was not "
    "set up (missing data, missing upstream record, missing required state). "
    "\n  auth_or_permission    — login failed, the account was unavailable, or the identity "
    "in use was not allowed to perform the action. "
    "\n  environment           — the page could not be reached, timed out, or the target "
    "system was unavailable (network, proxy, certificate, 5xx). "
    "\n  test_data             — the test data itself is unusable by the UI (wrong format, "
    "too long, rejected by a field's own validation). "
    "\n  test_case_issue       — the case itself is defective: EXPECTED is not decidable, "
    "the steps contradict each other, or it depends on a screen that no longer exists. "
    "\n  unclear               — you genuinely cannot tell which of the above it is. "
    "IMPORTANT: distinguish product_defect from agent_incomplete carefully. 'The agent did not "
    "get there' is agent_incomplete, NOT product_defect — reporting it as product_defect sends "
    "a non-existent bug to the developers. When you cannot show that the agent reached the "
    "judged state, do not use product_defect. "
    'Reply with JSON only: {"status": "passed"|"failed", "reason": "<one sentence>", '
    '"evidence": [<int>, ...], "evidence_gap": <true|false>, "root_cause": "<key>"}.'
)


@dataclass(frozen=True)
class Verdict:
    status: str  # passed | failed
    reason: str
    evidence: tuple[int, ...] = ()
    # 失败类型：true = "没做到位"（agent 没走完 / 证据不足），false = "结果确实不符"。
    #
    # 为什么要有这个字段：实测近 121 条失败里，**58% 是"没做到位/证据不足"**，
    # 只有 14% 是"结果确实与预期不符"。前者重试一次有很大机会变成通过
    # （配合经验记忆，第二次会少走弯路），后者重试只是白烧一遍时间。
    # 让判定器自己区分，比按关键词猜可靠得多。
    evidence_gap: bool = False
    # 2026-10-04 失败根因分类（用户要求"失败根因分类，降低假失败"）。
    #
    # 为什么在 evidence_gap 之外还要一个分类：evidence_gap 只回答"要不要重试"
    # （重试有没有救），回答不了测试工程师真正关心的问题 —— **这条失败该找谁处理**。
    # 实测痛点：一堆失败混在一起看不出哪些是真缺陷，于是只能一条条点开看，
    # 或者干脆全部重跑一遍；而"账号在忙""前置数据没造出来"这类假失败
    # 被当成真缺陷报上去，也消耗了开发的信任。
    # 分类之后，报告可以直接按类分组：真缺陷给开发，账号/环境问题给运维。
    #
    # 取值见 _ROOT_CAUSES。无法判断时必须填 "unclear"，**不允许猜** ——
    # 猜错的分类比不分类更糟，因为它会让真缺陷被归到"环境问题"里被忽略。
    root_cause: str = ""


# 根因分类表。key 是内部标识（落库用），值是给判定器看的中文说明。
#
# 分类是**互斥单选**而不是多选：报告要按它分组，一条失败落进两组就等于没分。
# 判定器被要求选"最主要的那一个"。
_ROOT_CAUSES: dict[str, str] = {
    "product_defect": "系统本身有缺陷：agent 确实按预期路径操作到位了，但系统给出的行为与预期不符",
    "agent_incomplete": "agent 自己没做完：中途停了、需要的控件没找到、关键步骤没走到",
    "evidence_insufficient": "证据不足以判定：记录里没有能证明或证伪预期的东西（例如页面没渲染出来）",
    "precondition_missing": "前置条件不具备且没能自动补建：缺少必要的数据/状态/上游单据",
    "auth_or_permission": "账号或权限问题：登录失败、账号被占、当前账号没有该操作的权限",
    "environment": "环境或网络问题：页面打不开、超时、代理/证书错误、系统不可用",
    "test_data": "测试数据问题：用例给的测试数据本身无效（格式错、超长、与界面校验不符）",
    "test_case_issue": "用例本身有问题：预期写得无法判定、步骤矛盾、依赖已不存在的界面",
    "unclear": "无法判断属于以上哪一类",
}

# 哪些分类算"真失败"（值得开发介入），哪些算"假失败"（重试或修环境即可）。
# 报告和统计要用它把两类分开，避免假失败污染真缺陷的统计口径。
#
# 判断依据是"这条失败反映了被测系统的真实问题吗"：
#   product_defect  → 是，系统确实不符预期
#   precondition_missing / test_data → 否，是用例侧的准备问题（但可能暴露用例质量）
#   agent_incomplete / evidence_insufficient / environment / auth_or_permission / unclear
#                    → 否，是执行侧问题
#   test_case_issue  → 不是产品问题，是用例维护问题
# 这一点必须显式写下来：默认全算"真失败"会让假失败长期污染缺陷率。
ROOT_CAUSE_IS_REAL_DEFECT: frozenset[str] = frozenset({"product_defect"})

# 重试有救的分类。engine 已经用 evidence_gap 决定重试；这里给出更细的依据，
# 让"为什么重试/为什么放弃"能对上具体原因，而不是只有一个布尔值。
ROOT_CAUSE_RETRYABLE: frozenset[str] = frozenset(
    {"agent_incomplete", "evidence_insufficient", "precondition_missing", "environment", "auth_or_permission"}
)

# 给界面/报告用的中文短标签。
# 为什么放在这里而不是前端：分类表只有这一份来源，前端再抄一份迟早会不一致 ——
# 新增一个分类时忘了改前端，报告里就会出现空白分组。
ROOT_CAUSE_LABELS_ZH: dict[str, str] = {
    "product_defect": "产品缺陷",
    "agent_incomplete": "执行未完成",
    "evidence_insufficient": "证据不足",
    "precondition_missing": "前置数据缺失",
    "auth_or_permission": "账号/权限",
    "environment": "环境/网络",
    "test_data": "测试数据问题",
    "test_case_issue": "用例本身问题",
    "unclear": "无法判定",
}


def _check_evidence(
    cited: Any, n_steps: int
) -> tuple[tuple[int, ...], str | None]:
    """校验判定器引用的步骤编号是否真实存在。

    这一层是给"反编造"兜底的：判定器现在被要求说明它依据了第几步。
    如果它引用了不存在的编号（越界、或压根没给），那它的理由很可能不是从实际
    步骤里读出来的 —— 这时在 reason 后面挂一条提示，让人一眼看出这条理由不可全信。
    **不改变判定结果**（宁可标疑点，也不越权改判 —— 改判就成了系统替测试做决定）。
    """
    if not isinstance(cited, list) or not cited:
        return (), "（未说明依据步骤）"
    idx: list[int] = []
    bad = False
    for v in cited:
        if isinstance(v, bool) or not isinstance(v, int):
            bad = True
            continue
        if 1 <= v <= n_steps:
            idx.append(v)
        else:
            bad = True
    if bad:
        return tuple(idx), "（引用的步骤编号不存在，该理由未经核对）"
    return tuple(idx), None


def build_prompt(
    expected: str,
    final_answer: str,
    actions: list[str],
    task: str = "",
    evidence: list[str] | None = None,
) -> str:
    """The judge's user message. Split out so the wiring is testable without an LLM call."""
    # STEP_RESULTS 必须带编号：判定器被要求在 reason 里说明依据了第几步，
    # 编号不对齐它就无从引用，反编造那层校验也就落空了。
    steps = [e for e in (evidence or []) if e][:80]
    return json.dumps(
        {
            "task": task,
            "expected": expected,
            "final_answer": final_answer,
            "actions": actions[:80],
            "step_results": [{"step": i, "observation": e} for i, e in enumerate(steps, 1)],
        },
        ensure_ascii=False,
    )


def build_content(user: str, screenshot_b64: str | None) -> Any:
    """Multimodal user content when there's a final screenshot, plain text otherwise."""
    if not screenshot_b64:
        return user
    return [
        {"type": "text", "text": user},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
        },
    ]


async def judge(
    expected: str,
    final_answer: str,
    actions: list[str],
    task: str = "",
    evidence: list[str] | None = None,
    screenshot_b64: str | None = None,
) -> Verdict:
    user = build_prompt(expected, final_answer, actions, task, evidence)
    s = get_settings()

    # ★ 确定性校验：结论里引用了步骤记录里没有的计数 → 明确告诉判定器。
    #
    # 现场（run 12 / case 4，2026-10-06）：agent 声称"查询后仍显示共 11 条"
    # 并据此报出product_defect，但"11"在 STEP_RESULTS 里根本不存在 ——
    # 它在最后一步点完「查询」就宣布完成，从没读过结果页，那 11 条是它自己算的。
    # 判定器把这份自述当证据，于是报出一个**并不存在的缺陷**，真缺陷率被污染。
    #
    # 这一层只提供判定器自己做不到的能力（核对数字出处），
    # **不替它改判** —— 判失败还是通过仍由判定器决定。
    # 理由见 app/judge_claims.py 的 docstring：误判比判松更伤。
    from app.judge_claims import (
        find_premature_done,
        find_unverified_claims,
        override_gate,
        premature_done_note,
        unverified_note,
    )

    # ① 执行未落位：宣布完成时操作还没真正生效
    #    现场 case 4：第 7 步 action=done，但 result 是 Clicked button "查询" ——
    #    它点下查询就宣布完成，从没读过结果页。这条比"编造数字"更硬：
    #    它是**时序事实**，不需要任何语义理解。
    _pd = find_premature_done(evidence)
    _pd_note = premature_done_note(_pd)
    if _pd_note:
        user = f"{user}\n\n[确定性校验发现]\n{_pd_note}"

    # ② 编造的数字：结论里的计数在步骤记录里找不到出处
    _claims = find_unverified_claims(final_answer, evidence)
    _claim_note = unverified_note(_claims)
    if _claim_note:
        user = f"{user}\n\n[确定性校验发现]\n{_claim_note}"

    system = f'{_SYSTEM} Write the "reason" in {s.report_language}.'
    cfg = await llm_config()
    client = await openai_client()

    async def ask(content: Any) -> Verdict:
        resp = await client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        status = "passed" if data.get("status") == "passed" else "failed"
        reason = str(data.get("reason", ""))[:500]
        # 证据编号校验：让"依据哪几步"可核对，抽到编造的理由时能看出来
        n_steps = len([e for e in (evidence or []) if e])
        ev, warn = _check_evidence(data.get("evidence"), n_steps)
        if warn:
            reason = f"{reason}{warn}"[:500]
        # 只有"失败 + 判定器确认为没做到位"才算可重试。默认 False 是安全的：
        # 宁可少重试，也不要把真实缺陷重跑一遍当成偶发。
        gap = status == "failed" and data.get("evidence_gap") is True
        # 根因分类：只接受白名单里的 key。
        # 模型偶尔会自己造一个听着合理的分类（"timeout"、"network_error"…），
        # 直接落库会让统计口径被污染 —— 报告按类分组时冒出十几个只出现一次的类目。
        # 认不出来就归 unclear，并在 reason 里留痕，便于事后发现提示词没被遵守。
        raw_cause = str(data.get("root_cause") or "").strip()
        cause = raw_cause if raw_cause in _ROOT_CAUSES else ""
        if status == "passed":
            cause = ""  # 通过的用例没有根因，模型若硬填一律清掉
        elif raw_cause and not cause:
            cause = "unclear"
            reason = f"{reason}（判定器给的分类「{raw_cause[:20]}」不在允许列表内，已归为 unclear）"[:500]
        elif not raw_cause:
            cause = "unclear"
        # 分类与 evidence_gap 的一致性：product_defect 意味着 agent 确实做到位了，
        # 那就绝不能同时被标成"可重试"。两者矛盾时以分类为准 —— 分类是更具体的判断，
        # 而矛盾会让 engine 重跑一条真实缺陷，白烧一轮时间。
        if cause == "product_defect":
            gap = False
        elif cause in ROOT_CAUSE_RETRYABLE and status == "failed":
            gap = True

        # ── 硬闸门：无依据的通过，一律翻案（2026-10-06 21:19-21:23 现场）─────
        # 抓到两条实测假通过：
        #   result 2  final_answer = `Clicked button "查询"`（agent 根本没写结论）
        #             判定理由 =「结果表格共 5 条…与 EXPECTED 一致」→ passed
        #   result 5  final_answer =「页面显示正在加载中，未能加载出预期的…」
        #             判定理由 =「最终截图显示页面已加载…符合预期」→ passed
        #
        # 为什么这里必须**直接改判**而不能只提示：上面两层（编造数字 / 执行未落位）
        # 是给判定器看的，而这两种情况里判定器**确信自己看到了**——它是从截图推理的。
        # 提示它"你的依据不成立"会被它自己反驳。所以只能在出口拦。
        #
        # ★只拦 passed，不动 failed：假通过必须拦（它污染通过率和真缺陷率，
        # 而且界面上看不出异常），而误伤真通过会把报表染红，代价更大。
        _gate = override_gate(final_answer, evidence, status, reason)
        if _gate is not None:
            log.warning(
                "judge: ★ 无依据的通过，已翻案（gate=%s）：%s",
                _gate.gate, reason[:120],
            )
            return Verdict(
                status="failed",
                reason=_gate.reason[:500],
                evidence=ev,
                evidence_gap=True,
                root_cause=_gate.root_cause,
            )
        return Verdict(
            status=status, reason=reason, evidence=ev, evidence_gap=gap, root_cause=cause
        )

    try:
        try:
            return await ask(build_content(user, screenshot_b64))
        except Exception:
            if not screenshot_b64:
                raise
            # a gateway model without vision must not cost us the verdict entirely
            return await ask(user)
    except Exception as exc:  # judge failure must not pass a test by default
        # 判定器自己挂了，属于执行侧问题而非产品缺陷 —— 归 environment，
        # 这样它不会混进"真缺陷"统计里，也会被 ROOT_CAUSE_RETRYABLE 判为可重试。
        return Verdict(
            status="failed",
            reason=f"judge_error: {type(exc).__name__}: {exc}"[:500],
            root_cause="environment",
        )
    finally:
        await client.close()
