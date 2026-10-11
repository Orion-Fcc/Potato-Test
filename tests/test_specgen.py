"""从需规生成用例：解析层与质量防线（2026-10-09）。

## 为什么这组测试是重点，而不是"再补几条覆盖率"

这个功能最容易产出的就是**看起来对、实际是垃圾**的用例。一条不可判定的用例进了
回归集，代价是每次运行都红一次，而每次都"看起来像被测系统的问题"—— 没人能证明它错。
所以真正需要守的不是"能不能生成"，而是"**生成的垃圾有没有被拦下来**"：

1. 需规里写着"建议""可选""待确认"的内容**不是**可断言需求。
   把它写成"应当支持 X"，就制造了一条永远失败且无法反驳的用例。
2. `expected` 是"符合预期"这类**同义反复**时，等价于没有预期 ——
   但它在界面里看起来像填了，是审阅时最容易被放过的一类。
3. 模型很乐意"顺手补全"文档里没有的章节标题（尤其是它熟悉的领域）。
   那些标题下面没有原文，生成出来的用例就是纯编造 —— 所以规划层必须
   把不在清单里的标题**剔除**，而不是照单全收。
"""

from __future__ import annotations

from app.specgen import (
    MAX_CASES_PER_BATCH,
    DraftCase,
    group_titles_into_modules,
    parse_cases,
    parse_modules,
)


# ---------------------------------------------------------------------------
# 解析层：把模型输出变成可信的草稿
# ---------------------------------------------------------------------------


def _ok(name: str = "查询列表") -> dict:
    return {
        "name": name,
        "prompt": "打开资源审批配置列表，使用名称筛选后点击查询。",
        "expected": "列表只显示名称匹配的记录。",
    }


def test_a_complete_case_becomes_a_draft() -> None:
    cases, dropped = parse_cases([_ok()], module_hint="资源审批")
    assert len(cases) == 1
    assert not dropped
    assert cases[0].module == "资源审批", "没给 module 时应当回落成模块名"
    assert cases[0].type == "functional"
    assert cases[0].priority == "P2"


def test_case_without_a_prompt_is_dropped() -> None:
    """没有操作指令 = agent 不知道该干什么，这不是用例。"""
    bad = {**_ok("空指令"), "prompt": ""}
    cases, dropped = parse_cases([bad])
    assert cases == []
    assert any("操作指令" in d for d in dropped), dropped


def test_case_without_a_decidable_expectation_is_dropped() -> None:
    bad = {**_ok("无预期"), "expected": ""}
    cases, dropped = parse_cases([bad])
    assert cases == []
    assert any("不可判定" in d for d in dropped), dropped


def test_tautological_expectation_is_dropped() -> None:
    """★ "符合预期"不是预期 —— 它在界面上看起来像填了，是审阅最容易放过的一类。

    这类 expected 与"空"在语义上完全等价（没有任何可核对的东西），
    但因为它非空，任何"只判空"的校验都拦不住它。
    """
    for junk in ("符合预期", "功能正常。", "no error", "一切正常", "与预期一致"):
        cases, dropped = parse_cases([{**_ok("同义反复"), "expected": junk}])
        assert cases == [], f"「{junk}」这种同义反复不该通过校验"
        assert any("不可判定" in d for d in dropped), (junk, dropped)


def test_a_real_expectation_containing_a_tautology_word_survives() -> None:
    """反向守卫：只是**包含**"正常"这类词的实质断言不能被误杀。

    这条如果做错，会把大量真实用例静默丢掉 —— 比放过几条同义反复更糟。
    """
    good = {
        **_ok("含关键词的真断言"),
        "expected": "列表按创建时间倒序显示，新增记录出现在第一行。",
    }
    cases, _ = parse_cases([good])
    assert len(cases) == 1, "实质断言被误杀了"

    # 另一个容易误杀的形状：句子里带"正常"但另有可核对内容
    good2 = {**_ok("带正常二字"), "expected": "页面正常跳转到详情页，标题显示该记录名称。"}
    assert len(parse_cases([good2])[0]) == 1


def test_unknown_enum_values_fall_back_instead_of_leaking() -> None:
    """模型会造枚举值。放进去会让报告的按类型分组冒出一堆只出现一次的类目。"""
    row = {**_ok(), "type": "exploratory", "priority": "P9"}
    cases, _ = parse_cases([row])
    assert cases[0].type == "functional"
    assert cases[0].priority == "P2"


def test_known_enum_values_are_kept() -> None:
    row = {**_ok(), "type": "negative", "priority": "p0"}
    cases, _ = parse_cases([row])
    assert cases[0].type == "negative"
    assert cases[0].priority == "P0", "优先级应当大小写归一化"


def test_module_hint_is_used_when_the_model_omits_module() -> None:
    cases, _ = parse_cases([_ok()], module_hint="审批流")
    assert cases[0].module == "审批流"


def test_non_list_and_non_dict_items_are_reported_not_crashed() -> None:
    """模型偶尔回 `{"cases": "..."}` 或数组里混字符串 —— 要报告，不要抛。"""
    assert parse_cases(None) == ([], ["模型没有返回 cases 数组"])
    cases, dropped = parse_cases(["这不是对象", {"name": "x"}])
    assert cases == []
    assert any("不是对象" in d for d in dropped)
    assert any("没有标题" in d or "缺少" in d for d in dropped)


# ---------------------------------------------------------------------------
# 规划层：标题必须**来自文档**
# ---------------------------------------------------------------------------


def test_modules_may_only_use_titles_that_exist_in_the_document() -> None:
    """★ 模型最危险的行为：顺手补一个文档里没有的标题（通常是它熟悉的领域）。

    那个标题下面没有任何原文，生成出来的用例就是纯编造 —— 而且要等跑失败才发现。
    所以这里按"清单里真实存在"过滤，而不是照单全收。
    """
    real = ["### 资源审批配置", "### 培训资源管理"]
    raw = [
        {"name": "审批", "titles": ["### 资源审批配置", "### 用户登录"], "testable": True},
    ]
    mods = parse_modules(raw, real)
    assert len(mods) == 1
    assert mods[0]["titles"] == ["### 资源审批配置"], "文档里没有的标题必须被剔除"


def test_a_module_with_no_real_titles_is_dropped_entirely() -> None:
    raw = [{"name": "编造模块", "titles": ["### 完全不存在"], "testable": True}]
    assert parse_modules(raw, ["### 真实标题"]) == []


def test_a_title_is_never_reused_across_modules() -> None:
    """同一节被分到两个模块 → 同一批用例生成两遍，用户要审两遍。"""
    real = ["A", "B"]
    raw = [
        {"name": "模块1", "titles": ["A"], "testable": True},
        {"name": "模块2", "titles": ["A", "B"], "testable": True},
    ]
    mods = parse_modules(raw, real)
    assert mods[0]["titles"] == ["A"]
    assert mods[1]["titles"] == ["B"], "A 已被上一个模块用掉"


def test_testable_defaults_to_true() -> None:
    """默认必须是"可测"。反过来（默认 False）会因为模型漏填一个字段
    就把整个模块的用例都吞掉，而且用户完全看不出为什么少了一块。"""
    raw = [{"name": "m", "titles": ["A"]}]
    assert parse_modules(raw, ["A"])[0]["testable"] is True

    raw2 = [{"name": "m", "titles": ["A"], "testable": False}]
    assert parse_modules(raw2, ["A"])[0]["testable"] is False


def test_group_titles_fallback_is_one_module_per_title() -> None:
    """规划失败时的兜底：粒度更碎，但功能仍然可用。"""
    mods = group_titles_into_modules(["A", "", "B", "  "])
    assert [m["name"] for m in mods] == ["A", "B"]
    assert all(m["testable"] for m in mods)


def test_draft_case_is_json_serialisable() -> None:
    """草稿要经 API 回传前端，字段必须可序列化（曾经想过直接回 ORM 对象）。"""
    import json

    d = DraftCase(name="n", prompt="p", expected="e")
    json.dumps(d.as_dict())


def test_batch_cap_is_sane() -> None:
    """上限存在的意义是防模型对一节"自由发挥"出几十条同质用例。"""
    assert 5 <= MAX_CASES_PER_BATCH <= 100


# ---------------------------------------------------------------------------
# 标题与正文必须对得上（2026-10-09 修的真实缺陷）
# ---------------------------------------------------------------------------


def test_chunk_sections_are_split_so_every_heading_is_visible() -> None:
    """★ 缺陷复现：小节的标题会被"合并 chunk"吃掉。

    `knowledge.chunk_text` 会把小于 2000 字符的相邻小节合并进一个 chunk，
    而一个 chunk 只记**一个** heading。于是：

        # 甲
        ## 甲-1
        甲的内容。
        ## 甲-2
        甲的第二节。

    只产出一个 chunk、heading 是「甲」——`甲-2` 在标题清单里**完全不存在**，
    用户在界面上看不到这一节，也就永远生成不出它的用例。

    实测影响：真库 project 2 的标题数从 175（每个 chunk 一个，同一节被重复计数）
    变成 43（真实的不同小节数）—— 两个方向都错，切开之后才对得上。
    """
    from app.specgen import split_chunk_sections

    merged = "# 甲\n## 甲-1\n甲的内容。\n## 甲-2\n甲的第二节。"
    secs = split_chunk_sections(merged)
    titles = [h for h, _ in secs if h]
    assert titles == ["甲", "甲-1", "甲-2"], f"合并 chunk 里的标题没被切出来：{titles}"

    bodies = dict(secs)
    # ★ 关键：正文必须归到**它自己**的标题下，否则模型会把甲-2 的需求
    # 写成甲-1 的断言 —— 一条看起来合理、实际测错东西的用例。
    assert bodies["甲-1"] == "甲的内容。", bodies["甲-1"]
    assert bodies["甲-2"] == "甲的第二节。", bodies["甲-2"]


def test_chunk_sections_keeps_leading_text() -> None:
    """标题之前的文字（文档开头的说明）不能丢，但也不该被当成某一节的正文。"""
    from app.specgen import split_chunk_sections

    secs = split_chunk_sections("这是开头的说明。\n## 第一节\n正文。")
    assert secs[0][0] == "", "标题之前的文字应当以空标题返回"
    assert "开头的说明" in secs[0][1]
    assert dict(secs)["第一节"] == "正文。"


def test_chunk_sections_handles_no_headings_at_all() -> None:
    """纯文本文档没有任何标题 —— 返回一个空标题段，不能崩。"""
    from app.specgen import split_chunk_sections

    secs = split_chunk_sections("一整段没有标题的文字。")
    assert [h for h, _ in secs] == [""]


# ---------------------------------------------------------------------------
# 结构性模块必须自己判，不能只信模型（2026-10-09 实测缺陷）
# ---------------------------------------------------------------------------


def test_structural_sections_are_marked_untestable_even_if_the_model_says_otherwise() -> None:
    """★ 实测缺陷：模型把「需求说明书索引及联系人」标成 testable=True。

    那一节的内容是索引 + 版本清单 + 联系人，在浏览器里**没有任何可验证的需求**。
    于是：用户看到"可测模块"，点进去生成 → 0 条，而且**没有任何解释**，
    只能认为功能坏了。

    模型在任何单点判断上都可能错，而这类结构性标题是**代码一眼可辨**的，
    所以由代码兜底覆盖它的判断。
    """
    from app.specgen import parse_modules

    real = ["需求说明书", "版本清单", "联系人分配", "维护计划管理"]
    raw = [
        {"name": "索引及联系人", "idx": [1, 2, 3], "testable": True},
        {"name": "维护管理", "idx": [4], "testable": True},
    ]
    mods = {m["name"]: m for m in parse_modules(raw, real)}
    assert mods["索引及联系人"]["testable"] is False, "元信息模块被当成了可测模块"
    assert "元信息" in mods["索引及联系人"]["reason"]
    # 正常业务模块不受影响
    assert mods["维护管理"]["testable"] is True


def test_a_real_business_section_named_qingdan_is_not_skipped() -> None:
    """★ 反向守卫：判据是"每一节都像元信息"，不是"任一含关键词"。

    「订单清单」「出库清单」是**正常业务列表**。按关键词一刀切会把它们整块跳过 ——
    用户会发现自己最该测的列表页生成不出用例，而且不知道为什么。
    """
    from app.specgen import looks_structural

    assert looks_structural(["订单清单", "出库清单"]) is False
    assert looks_structural(["需求说明书", "版本清单", "联系人分配"]) is True
    # 混合模块：只要有一节是真实需求，整个模块就可测
    assert looks_structural(["维护计划管理", "版本清单"]) is False


def test_structural_detection_handles_the_edge_cases() -> None:
    from app.specgen import looks_structural

    assert looks_structural([]) is False, "空列表不该被当成结构性"
    assert looks_structural(["  "]) is False
    assert looks_structural(["版本记录"]) is True
    assert looks_structural(["术语表", "缩略语"]) is True
    assert looks_structural(["需求说明书索引及联系人"]) is True


def test_zero_cases_comes_with_an_explanation() -> None:
    """★ 0 条必须解释**为什么**。

    0 条有三种完全不同的原因（真没需求 / 模型判断错了 / 调用其实失败了），
    对应三种不同的下一步动作。不区分的话用户只能反复点同一个按钮。
    """
    from app.specgen import GenResult

    # 结构上：GenResult 必须带 note 字段，且默认空串（不干扰正常路径）
    r = GenResult()
    assert r.note == ""
    assert GenResult(cases=[], note="x").note == "x"


# ---------------------------------------------------------------------------
# 第一批必须是有用的那些（2026-10-09 实测的体验问题）
# ---------------------------------------------------------------------------


def test_ranking_puts_likely_requirements_before_metadata() -> None:
    """★ 用户打开面板看到的**第一批**决定他对这个功能的第一印象。

    实测那份 586 个标题的合并需规，文档最前面恰好全是元信息
    （版本清单 / 未导入 / 需求说明书 / 联系人分配）—— 用户看到的是
    "12 个模块、其中 6 个不可测"，而真正有需求的小节（`R-查询`、`U-提交`…）
    散在后面 500 多个标题里，要连点十次「规划下一批」才碰得到。

    这条守的是：排序之后，一眼可辨的元信息必须沉到最后，有需求信号的排到前面。
    """
    from app.specgen import rank_headings

    doc = [
        ("资源审批使用（合并文档）", 1),
        ("版本清单", 1),
        ("未导入（纯文本文档 / 扫描件）", 1),
        ("需求说明书", 1),
        ("联系人分配", 1),
        ("R-列表", 3),
        ("R-查询", 3),
        ("维护计划管理", 2),
        ("U-提交", 3),
        ("C-新增", 3),
        ("术语表", 2),
    ]
    ranked = rank_headings([(t, lvl, i) for i, (t, lvl) in enumerate(doc)])

    assert len(ranked) == len(doc), "★ 排序丢了标题 —— 排序绝不能变成过滤"
    assert ranked[0] in ("R-列表", "R-查询", "U-提交", "C-新增"), f"第一位是 {ranked[0]!r}"
    tail = set(ranked[-4:])
    assert {"版本清单", "术语表", "联系人分配"} <= tail, f"元信息没有被沉到最后：{ranked[-4:]}"


def test_ranking_is_stable_within_equal_scores() -> None:
    """同分内部保持文档顺序。

    否则同一份文档两次请求可能给出不同顺序 —— 而用户要的是"按批往下推"，
    顺序乱跳会让"我做到哪儿了"完全没法跟踪。
    """
    from app.specgen import rank_headings

    same = [("查询甲", 2), ("查询乙", 2), ("查询丙", 2)]
    ranked = rank_headings([(t, lvl, i) for i, (t, lvl) in enumerate(same)])
    assert ranked == ["查询甲", "查询乙", "查询丙"]


def test_ranking_prefers_deeper_headings_when_nothing_else_differs() -> None:
    """层级深通常更具体：`###` 是功能点，`#` 往往是文档标题本身。

    实测那批元信息标题恰好都是 `#` 一级 —— 这是"层级"这条信号有效的原因。
    """
    from app.specgen import rank_headings

    doc = [("甲功能", 1), ("乙功能", 3), ("丙功能", 2)]
    ranked = rank_headings([(t, lvl, i) for i, (t, lvl) in enumerate(doc)])
    assert ranked.index("乙功能") < ranked.index("丙功能") < ranked.index("甲功能")

