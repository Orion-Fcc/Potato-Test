"""failure_attrib 的归因测试。

素材来自 2026-10-07 内网 SPA 上的两次**真实误判** —— 两者都差点被报成产品缺陷，
而两者都不是缺陷。这两个案例是这个模块存在的全部理由，所以直接写进测试：

* **案一**：导入 2 条大改驾学员后，列表页显示「共 0 条 / 暂无数据」，agent 判"查不到"。
  实为vxe-table 还在取数，点重置后恢复 36 条。
* **案二**：导入后未点【提交审批】，agent 判"数据没保存"。实为结果页有【保存】和
  【提交审批】两个按钮，只做导入是草稿态，未提交审批自然不入库。

还有一个案三来自更早：内网 SPA 首屏「正在加载中请稍后......」持续 30s+，agent
只走 1 步就宣布"无法访问目标页面"。

写成测试而不是注释，是因为这三条判据一旦被后人"顺手简化"（比如把 `共 0 条` 从
`_TRANSIENT` 里删掉，因为"它就是空列表"），没有人会想到这三条各对应一次真实误报。
"""

from __future__ import annotations

from app.failure_attrib import classify, describe, prompt_hint


# ── 案一：渲染中间态被当成"数据不存在" ─────────────────────────────────
CASE_LIST_MID_LOAD = """
【click】Clicked span "查询"
result: 共 0 条 暂无数据
error:
"""
CASE_LIST_AFTER = """
result: 共 36 条 暂无数据
"""


# ── 案二：只做导入、未提交审批，被当成"没保存" ─────────────────────────
# 真实的 evidence 里能看到页面上有【提交审批】按钮（DOM 快照/截图里都有它），
# 而动作日志里没有点它。这个"按钮在、没点它"的落差就是本模块要抓的信号。
CASE_DRAFT_NOT_SUBMITTED = """
【click】Clicked button "保存"
result: 导入成功 共 2 条 失败 0 条 页面提供 [提交审批] [返回] 两个按钮
【done】Done
"""


# ── 案三：首屏 splash 未撤 ─────────────────────────────────────────────
CASE_SPLASH = """
result: 正在加载中请稍后......
"""


def test_case1_loading_empty_list_is_transient_not_missing_data():
    """案一：vxe-table 取数中的「共 0 条 / 暂无数据」= transient，绝不能是 agent。

    这是最容易改坏的一条：如果把 `共 0 条` 从 _TRANSIENT 去掉，它就会掉到
    `agent`（找不到东西），于是每次列表页刷新都被算成执行方问题。
    """
    v = classify(CASE_LIST_MID_LOAD, final_answer="查不到刚导入的学员")
    assert v == "transient", f"案一应判transient，实际 {v}"
    assert v != "agent", "列表加载中绝不能算执行方问题"


def test_case1_still_transient_when_final_answer_says_missing():
    """即使 agent 的结论里写着"查不到"，也不能盖过页面证据。

    agent 的自我陈述在这里是**错的**（它就是读早了）。证据优先级高于结论 ——
    这正是证据来自浏览器回读、而非模型自述的原因。
    """
    v = classify(CASE_LIST_MID_LOAD, final_answer="该学员不存在于系统中")
    assert v == "transient"


def test_case2_draft_without_approval_is_setup():
    """案二：只做导入、未提交审批 = setup（流程没走完），不是系统丢数据。"""
    v = classify(CASE_DRAFT_NOT_SUBMITTED, final_answer="导入后列表里查不到这2 条学员，数据没有保存")
    assert v == "setup", f"案二应判 setup，实际 {v}"


def test_case2_state_words_are_what_carry_it():
    """真正把案二定成setup 的是**状态词**，不是按钮名。

    对照两句话，结论相反：
      「页面提供 [提交审批] [返回] 两个按钮」      → 不定 setup（按钮在，只是我们没点）
      「本条记录仍未提交审批」                    → 定setup（状态就是没提交）
    所以下面的断言用第二句。若哪天实现退化成"见到提交审批就判 setup"，
    `test_missing_feature_with_no_excuse_is_system` 会先炸。
    """
    v = classify(
        'result: 导入成功 共 2 条 该记录仍为草稿态，未提交审批',
        final_answer="查不到这2 条学员",
    )
    assert v == "setup", f"案二应判 setup，实际 {v}"


def test_case3_splash_is_transient():
    """案三：首屏 splash = transient。"""
    assert classify(CASE_SPLASH, final_answer="无法访问目标页面") == "transient"


def test_session_expired_is_setup_not_product_bug():
    """会话失效要判setup —— 否则每一条用例都会因一次 token 过期报一个假缺陷。"""
    v = classify("error: 登录已失效，请重新登录", final_answer="页面跳回登录页")
    assert v == "setup", f"登录失效应判 setup，实际 {v}"


# ── 反向：真缺陷不能被误判成"我们的问题" ─────────────────────────────


def test_real_validation_rejection_is_system():
    """系统的校验提示必须判成 system，否则真缺陷全被当成执行问题。"""
    v = classify('result: 错误提示 该身份证号已存在招飞学员记录')
    assert v == "system", f"系统校验提示应判 system，实际 {v}"


def test_permission_error_is_system():
    v = classify("result: 系统错误 权限不足，无法执行此操作")
    assert v == "system"


def test_required_field_error_is_system():
    v = classify('result: 提示信息 招飞时间不能为空')
    assert v == "system"


def test_missing_feature_with_no_excuse_is_system():
    """「按钮不存在」且没有任何前置词/加载态 → system（真缺陷的典型形态）。

    这条与案二成对：都是"提交审批"，但方向相反 ——
      *案二*：按钮**在**页面上、我们**没点**它      → setup
      *案三*：按钮**就是不存在**、页面上压根没有    → 真缺陷线索
    一字之差，结论相反。所以 `提交审批` 不能作为 setup 的关键词（见 _SETUP_VOCAB
    注释）—— 那会把本条误判成"流程没走完"，等于把真缺陷洗成执行问题。
    """
    v = classify('result: 页面上找不到"提交审批"按钮', final_answer="该功能缺失")
    assert v not in ("setup", "transient"), f"真缺陷被误判为 {v}"


# ── 执行侧问题 ─────────────────────────────────────────────────────────


def test_browser_layer_error_is_agent():
    """CDP / net::ERR 这类是自动化层的锅，与产品无关。"""
    v = classify("error: net::ERR_PROXY_CONNECTION_FAILED", final_answer="无法打开页面")
    assert v == "agent"


def test_selector_failure_is_agent():
    v = classify(
        "error: Timeout 30000ms exceeded waiting for locator",
        final_answer="按钮找不到",
    )
    assert v == "agent"


def test_self_reported_skip_is_setup():
    """agent 自己说"没有点击 X" → setup。这是它主动交代的，最可信。"""
    v = classify('result: 提示信息 请先勾选数据', final_answer="我没有点击确定，因为不确定是否生效")
    assert v == "setup"


# ── 保守：证据不足就说不足 ─────────────────────────────────────────────


def test_thin_evidence_is_unknown():
    """信息太少时不能硬给标签 —— 错标签比没标签更糟。"""
    v = classify("result: 页面打开了", final_answer="操作完成")
    assert v == "unknown", f"证据不足应判 unknown，实际 {v}"


def test_empty_evidence_is_unknown():
    assert classify("", final_answer="") == "unknown"
    assert classify("", action_text="", final_answer="") == "unknown"


# ── 优先级：强的信号不能被弱的覆盖 ─────────────────────────────────────


def test_setup_beats_transient():
    """同时命中 setup 词与加载态时，setup 优先。

    理由：加载态只是"现在还看不到"，setup 是"这一步压根没做"。后者更接近根因，
    报成 transient 会让人以为"再等等就好"，而实际上重试也还是这个结果。
    """
    v = classify("result: 正在加载中请稍后 提交审批 草稿态")
    assert v == "setup", f"setup 应优先于 transient，实际 {v}"


def test_automation_error_beats_everything():
    """自动化层报错优先于一切业务信号 —— 那时候页面上的话都是不可信的。"""
    v = classify("error: net::ERR_CONNECTION_RESET 错误提示 数据已存在")
    assert v == "agent", "自动化层错误应覆盖业务信号"


# ── 叙述提示：非缺陷的归因，绝不能写成缺陷 ─────────────────────────────


def test_prompt_hint_for_non_defect_verdicts_forbids_filing_a_bug():
    """setup / transient / agent 三个归因，提示里必须明说"不是缺陷"。"""
    for v in ("setup", "transient", "agent"):
        hint = prompt_hint(v)
        # 措辞容许不同（"不是缺陷"/"不是产品缺陷"），但必须出现"不是…缺陷"。
        assert "缺陷" in hint and "不是" in hint, f"{v} 的提示没声明不是缺陷：{hint}"
        assert "不要" in hint, f"{v} 的提示必须给出明确的写作禁令：{hint}"
    # unknown 更严格：既要声明不能断言，也要说明不能断言的方向。
    h = prompt_hint("unknown")
    assert "不要" in h and "缺陷" in h


def test_prompt_hint_for_system_allows_filing():
    assert "有效缺陷线索" in prompt_hint("system")


def test_unknown_verdict_gets_a_hint():
    assert prompt_hint("unknown"), "unknown 也必须有提示，否则会退回默认写法"


# ── describe：日志里要能一眼看出能不能上报 ─────────────────────────────


def test_describe_states_fileability():
    assert "可上报为缺陷" in describe("system")
    for v in ("setup", "transient", "agent", "unknown"):
        assert "不可上报为缺陷" in describe(v), f"{v} 的日志应标明不可上报"


def test_describe_handles_unknown_key():
    """未知key 不能抛 —— 标签会随版本变，describe 必须容错。"""
    assert describe("something_new")