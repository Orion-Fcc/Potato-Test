"""编造数据与过早放弃的防线。

## 两道防线分别治什么

**A. agent 过早放弃**（`_PERSIST_RULE`）
现场 run 12 / case 4：第 3-6 步 thought（意图）与 result（实际动作）**连续错位**，
agent 在第 7 步点完「查询」就宣布完成，从没读过结果页。
而`_EFFICIENCY_RULE` 第 6 条写着"做不到就照实说"—— 那给了它一条体面的退路。
用户要求「不希望 agent 那么喜欢放弃，可以思考怎么才能进行该用例，
多花点时间没关系，但是不要无效操作」。

**B. 判定器采信编造的数字**（`app/judge_claims.py`）
那次 agent 声称"查询后仍显示共 11 条"，而"11"在 STEP_RESULTS 里根本不存在。
判定器采信了，报出 product_defect —— **一个并不存在的缺陷**，真缺陷率被污染。

★这两道必须配套：有 A（agent 别轻易放弃）才走得完；
有 B（编的数字不算证据）才不会因为A 让它多跑几步就报出假缺陷。
"""

from __future__ import annotations

import inspect

import pytest

from app.judge_claims import (
    PrematureDone,
    find_premature_done,
    find_unverified_claims,
    premature_done_note,
    unverified_note,
)


# ══════════════════════════════════════════════════════════════════
# B. 编造的数字
# ══════════════════════════════════════════════════════════════════

_FABRICATED_ANSWER = (
    "## 测试结论：FAIL\n"
    "查询前（未筛选）列表共 **11条** 规则，其中符合条件的仅有 **3条**。\n"
    "查询后列表仍显示 **共11条**，第1页实际展示了以下规则：…\n"
    "结论：筛选未生效，仍显示全部11条记录。"
)

# 真实步骤里 agent 从没读过结果页——只有"点了查询"，
# 没有任何一条观察提到过 11 或 3。
_EVIDENCE_NO_RESULT_READ = [
    "当前浏览器状态只有加载占位文本，没有列表。",
    "Clicked input type=text role=combobox",
    "Clicked li role=option \"场地资源\"",
    "Clicked span \"启用\"",
    "Clicked button \"查询\"",
]


def test_fabricated_counts_are_caught():
    """★核心用例：结论里的 11 / 3 在步骤记录里找不到出处，必须被标出来。"""
    claims = find_unverified_claims(_FABRICATED_ANSWER, _EVIDENCE_NO_RESULT_READ)
    assert claims, "编造的数字没被抓到 —— 判定器会继续采信它"
    nums = {n for c in claims for n in c.numbers}
    assert 11 in nums, f"没抓到关键的 11（实际抓到 {nums}）"


def test_verified_counts_pass():
    """数字在步骤记录里确实出现过 → 不该被误伤。

    这条同样重要：校验器太敏感会把所有结论都标成"可疑"，
    判定器就会对每条都打折扣，等于没加。
    """
    answer = "查询后列表仍显示共 11 条记录，未按条件过滤。"
    evidence = [
        "点击查询后，页面列表区显示「共 11 条」，逐行为SIT手测-其他审批等。",
        "已核对 11 条均非场地资源。",
    ]
    assert find_unverified_claims(answer, evidence) == [], "正常结论被误伤了"


def test_no_evidence_marks_everything():
    """完全没有步骤记录时，结论里的计数全部标为无出处。"""
    claims = find_unverified_claims("列表共 5 条。", [])
    assert claims and 5 in claims[0].numbers


def test_claim_free_answer_is_silent():
    """没有数字的结论不该被标 —— 绝大多数用例的结论里没有页面计数。"""
    assert find_unverified_claims("已完成核对，弹窗按预期关闭。", ["点了保存", "弹窗已关闭"]) == []
    assert unverified_note([]) == ""


def test_note_explains_it_is_not_a_defect():
    """提示必须点名 product_defect 并给出正确归类 —— 否则判定器仍会照旧上报缺陷。"""
    note = unverified_note(find_unverified_claims(_FABRICATED_ANSWER, _EVIDENCE_NO_RESULT_READ))
    assert "product_defect" in note, "没点名 product_defect，判定器会继续上报缺陷"
    assert "agent_incomplete" in note, "没给出正确归类"
    # 必须说清"没有读过那个页面"这个事实，而不是笼统说"证据不足"
    assert "没有读过" in note or "从未被观察" in note
    # 反向也要管：编造的数字支撑"通过"同样是编造
    assert "通过" in note


def test_wiring_into_judge():
    """校验必须真的接在 judge() 上，且在拿到 LLM 响应之前。"""
    from app import judge

    src = inspect.getsource(judge.judge)
    assert "find_unverified_claims" in src
    # 注入到 user 消息里（而不是只在注释里提到）
    assert "确定性校验发现" in src


def test_system_prompt_names_the_trap():
    """系统提示词必须单独点名"编造数字"，不能只靠注入的提示。

    提示词里原有那条 "do not invent anything" 约束的是**动作**，
    实测没拦住编造的**计数** —— 所以必须有一条专门讲数字的。
    """
    from app import judge

    assert "a number in FINAL_ANSWER is a CLAIM" in judge._SYSTEM
    assert "product_defect" in judge._SYSTEM


# ══════════════════════════════════════════════════════════════════
# A. 过早放弃
# ══════════════════════════════════════════════════════════════════


def test_persist_rule_exists():
    """治"过早放弃"的规则必须在。"""
    from app import executor

    assert hasattr(executor, "_PERSIST_RULE")
    r = executor._PERSIST_RULE
    assert "GIVING UP IS THE LAST OPTION" in r
    # 必须区分"真尝试"与"空转"——用户原话是"不要无效操作"
    assert "WHAT COUNTS AS A REAL ATTEMPT" in r
    assert "BAD:" in r and "GOOD:" in r


def test_persist_rule_allows_more_time():
    """规则要明确"时间不是约束" —— 用户说"多花点时间没关系"。"""
    from app import executor

    r = executor._PERSIST_RULE
    assert "Time is not the constraint" in r
    assert "repeating yourself" in r


def test_persist_rule_order_after_efficiency():
    """★顺序：_PERSIST_RULE 必须排在 _EFFICIENCY_RULE 之后。

    记忆里的硬约束：省步数类规则是主体，例外/放宽类必须排在它之后，
    否则会被前面"求快"的措辞压过去 —— 而那正是放弃的来源。

    ★定位要精确到**拼接语句**而不是规则定义处：`find('_PERSIST_RULE')`
    命中的是文件顶度的规则定义（`_PERSIST_RULE = ` + 三引号），顺序永远是
    EFFICIENCY < PERSIST < SELF_CHECK，测不出拼接有没有排错。
    这个测试第一版就栽在这里（假通过）。
    """
    from app import executor

    src = inspect.getsource(executor)
    i = src.find("extend_system_message=(")
    assert i != -1, "找不到 extend_system_message 拼接语句"
    concat = src[i:i + 700]          # 拼接语句足够短，截一段足够覆盖
    i_persist = concat.find("_PERSIST_RULE")
    i_eff = concat.find("_EFFICIENCY_RULE")
    i_self = concat.find("_SELF_CHECK_RULE")
    assert i_eff != -1, "拼接里缺 _EFFICIENCY_RULE"
    assert i_persist != -1, "拼接里缺 _PERSIST_RULE"
    assert i_self != -1, "拼接里缺 _SELF_CHECK_RULE"
    assert i_eff < i_persist, "_PERSIST_RULE 必须在 _EFFICIENCY_RULE 之后"
    assert i_persist < i_self, "_SELF_CHECK_RULE 必须在最后（它是最后一道闸）"


def test_persist_rule_does_not_loosen_evidence():
    """★红线：这条规则只放宽"怎么走到目标"，绝不放宽"什么算证据"。

    否则"多试几次"会被理解成"差不多就算通过"。
    """
    from app import executor

    r = executor._PERSIST_RULE
    # 必须仍然要求点名页面真实内容
    assert "what the page actually showed" in r
    # 必须保留"实在不行就说"的出口，否则会变成硬撑到底的无效操作
    assert "then say so" in r


# ══════════════════════════════════════════════════════════════════
# 意图与动作错位（只统计，不独立改判）
# ══════════════════════════════════════════════════════════════════


def test_premature_done_detected():
    """★核心用例：最后一步 action=done 但 result 还在执行动作 —— 必须抓到。

    这是现场 case 4 的真实形态：它点下「查询」就宣布完成，从没读过结果页。
    """
    ev = [
        "第1步: 【navigate】🔗 Navigated to http://x/config",
        "第2步: 【click】Clicked input type=text role=combobox",
        "第3步: 【done】Clicked button \"查询\" (意图: 任务已完成核对并确认缺陷存在)",
    ]
    pd = find_premature_done(ev)
    assert pd is not None, "未落位的完成没被抓到"
    assert pd.step_index == 3
    assert "查询" in pd.performed


def test_premature_done_not_triggered_on_normal_finish():
    """正常收尾不该被误伤 —— done 且结果里没有待执行动作。"""
    ev = [
        "第1步: 【navigate】🔗 Navigated to http://x",
        "第2步: 【click】Clicked button \"查询\"",
        "第3步: 【done】Success: 列表已刷新，显示 3 条记录",
    ]
    assert find_premature_done(ev) is None, "正常收尾被误判成未落位"


def test_premature_done_ignores_middle_done():
    """只有**最后一步**的 done 才有决定意义 —— 中途出现 done 不算。"""
    ev = [
        "第1步: 【done】Clicked button \"保存\"",
        "第2步: 【click】Clicked button \"查询\"",
        "第3步: 【click】列表显示 3 条",
    ]
    assert find_premature_done(ev) is None, "把中间步骤的 done 当成了收尾"


def test_premature_done_note_names_correct_cause():
    """提示必须点名 agent_incomplete 而非 product_defect。"""
    note = premature_done_note(PrematureDone(step_index=7, performed="Clicked button 查询"))
    assert "agent_incomplete" in note
    assert "product_defect" in note, "要明确说不要报 product_defect"
    assert "没有看到" in note or "从未被观察到" in note


def test_premature_done_handles_empty():
    assert find_premature_done([]) is None
    assert find_premature_done(None) is None
    assert premature_done_note(None) == ""


def test_both_checks_wired_into_judge():
    """两层校验都必须接在 judge() 上（第一层=数字、第二层=未落位）。"""
    from app import judge

    src = inspect.getsource(judge.judge)
    assert "find_unverified_claims" in src, "第一层（编造数字）没接"
    assert "find_premature_done" in src, "第二层（未落位）没接"
    assert src.count("确定性校验发现") >= 2, "两处提示都要注入"


def test_evidence_carries_action_prefix():
    """★executor 传给 judge 的 evidence 必须带 【action】 前缀。

    第二层靠 action=done 识别"未落位"，而原来 evidence 只取 result——
    没有 action 就查不出来。这个测试防的是"以后有人又把 action 删掉"。
    """
    import inspect as _i

    from app import executor

    src = _i.getsource(executor)
    # 找到构造 evidence 的那段
    i = src.find("evidence = []")
    assert i != -1, "找不到 evidence 构造"
    seg = src[i:i + 500]
    assert "【" in seg and "action" in seg, "evidence 没有带 action 前缀"
