"""项目概况面板：把库里的事实算成看得懂的东西（2026-10-12）。

## 这组测试盯的是什么

这一层**不算、只查**（一个 LLM 调用都不发），所以错误形态不是"AI 胡说"，
而是**算错**——而算错的后果比胡说更隐蔽：胡说能被看出来，算错看起来像事实。

两条真实的算错（都在真数据上撞到过，见下），各自对应一组测试：

1. **需求原文取错了地方。** 同一个 ID 在文档里有三种出现方式：定义（规则表那一行）、
   引用（"依据 RULE-XXX"）、以及**记号说明**（"引用格式说明"那一节把它当例子列出来）。
   原来的实现"碰到第一个就返回"，命中的是记号说明，于是需求原文显示成
   「文档内的规则编号」——用户拿着这句无法核对任何东西。
2. **需求 ID 里混着版本号。** references 里有 `v1.38` 这种值。
   当成需求去统计，矩阵里就多一条永远"未被覆盖"的假需求，用户会去追一个不存在的问题。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.project_insights import (  # noqa: E402
    _is_legend,
    build_module_matrix,
    build_requirement_matrix,
    build_rules,
    clean_excerpt,
    excerpt_for,
    extract_refs,
    extract_rules,
)


class TestBackendCountMatchesWhatTheCaseListWouldShow:
    """★ 覆盖矩阵报的条数必须等于"按这个需求 ID 筛用例"能筛出来的条数。

    这是一次真实的不一致（2026-10-12）：矩阵里 DES-TRAINRES-001 写 **357 条**，
    点进去只看到 **315 行**。根因在前端（大分组默认折叠 + 每组只渲染前 50 行），
    但**用户看到的是一个自相矛盾的界面**，而他没有任何办法知道该信哪个数。

    前端那边已经改成"报实际筛出来的条数"。这里钉住后端这一侧的口径：
    矩阵计数与 `references` 的子串命中必须一致 —— 否则前端怎么改都对不上。
    """

    def test_count_equals_substring_hits(self):
        refs = {
            "TC-1": ["DES-A-1"],
            "TC-2": ["DES-A-1", "TST-B-2"],
            "TC-3": ["TST-B-2"],
        }
        got = {r.req_id: r.case_count for r in build_requirement_matrix(refs, [], {})}
        assert got["DES-A-1"] == 2
        # 用"前端会怎么做"的方式独立算一遍：references 里包含这个 ID
        raw_refs = {"TC-1": "DES-A-1", "TC-2": "DES-A-1 / TST-B-2", "TC-3": "TST-B-2"}
        for rid, count in got.items():
            frontend = sum(1 for v in raw_refs.values() if rid in v)
            assert frontend == count, f"{rid}: 矩阵说 {count}，按子串筛出 {frontend}"

    def test_a_case_referencing_two_requirements_counts_in_both(self):
        """一条用例可以验证多条需求 —— 两条都该把它算进去（不是二选一）。"""
        refs = {"TC-1": ["DES-A-1", "TST-B-2"]}
        got = {r.req_id: r.case_count for r in build_requirement_matrix(refs, [], {})}
        assert got == {"DES-A-1": 1, "TST-B-2": 1}

    def test_cases_list_is_capped_but_count_is_not(self):
        """列表字段有上限（60），计数必须仍是**全部**。

        用 len(cases) 当计数的话，一条被 357 条用例引用的需求会显示成 60 ——
        而 60 是个看起来完全合理的数字，没人会怀疑它错了。
        """
        refs = {f"TC-{i}": ["DES-A-1"] for i in range(200)}
        rc = build_requirement_matrix(refs, [], {})[0]
        assert rc.case_count == 200, "计数被列表上限截断了"
        assert len(rc.as_dict()["cases"]) == 60, "as_dict 里的列表该有上限（避免响应过大）"


# ---------------------------------------------------------------------------
# 摘出来的文本要能读
# ---------------------------------------------------------------------------
class TestCleanExcerpt:
    """需求文件是 Markdown/HTML 混排，摘出来必须清成可读文本。

    实测：不清的话界面上显示的是
    `...（申请、结算和数据库删除）。<br>**步骤**：填报页 → ...`
    —— 用户看到的是 `**步骤**` 和 `<br>`，正文反而读不出来。
    这些字符不是需求内容的一部分，去掉不损失信息。
    """

    def test_drops_html_tags(self):
        got = clean_excerpt("正文<br>下一行</p>")
        assert "<br>" not in got and "</p>" not in got
        assert "正文" in got and "下一行" in got

    def test_drops_markdown_emphasis(self):
        got = clean_excerpt("**步骤**：填报页 → 基本信息区")
        assert "**" not in got
        assert "步骤：填报页" in got

    def test_unescapes_quotes(self):
        assert '\\"' not in clean_excerpt('不出现整单\\"使用人数\\"')

    def test_decodes_entities(self):
        assert clean_excerpt("A &amp; B &lt;x&gt;") == "A & B <x>"

    def test_keeps_the_actual_content(self):
        """★ 清理不能把需要核对的内容也清掉 —— 那比不清更糟。"""
        src = "同一「使用范围+资源范围」不能配两条"
        assert clean_excerpt(src) == src

    def test_empty(self):
        assert clean_excerpt("") == ""


# ---------------------------------------------------------------------------
# 需求 ID 的抽取
# ---------------------------------------------------------------------------
class TestExtractRefs:
    def test_a_normal_reference_list(self):
        assert extract_refs("TST-TRAINRES-002 / DES-TRAINRES-001") == [
            "TST-TRAINRES-002",
            "DES-TRAINRES-001",
        ]

    def test_colon_form(self):
        assert "DES:1358" in extract_refs("DES:1358")

    def test_version_numbers_are_not_requirements(self):
        """★ 真实数据里 references 混着 `v1.38`。

        当成需求统计，矩阵里会多一条永远"未被覆盖"的假需求 ——
        用户会去追一个根本不存在的东西，而矩阵的其余部分因此失去可信度。
        """
        got = extract_refs("v1.38")
        assert got == [], f"版本号被当成需求 ID：{got}"
        assert extract_refs("§8.2") == []

    def test_dedupes(self):
        assert extract_refs("RULE-A-1 RULE-A-1") == ["RULE-A-1"]

    def test_empty_and_junk(self):
        assert extract_refs("") == []
        assert extract_refs("没有编号的一句话") == []
        assert extract_refs("A-1") == [], "太短的串不收"

    def test_mixed_keeps_the_real_ones(self):
        got = extract_refs("RULE-TRAINRES-116 / v1.38 / AC-TRAINRES-110")
        assert got == ["RULE-TRAINRES-116", "AC-TRAINRES-110"]


# ---------------------------------------------------------------------------
# 记号说明 vs 定义（最要紧的一条）
# ---------------------------------------------------------------------------
class TestLegendDetection:
    LEGEND_HEADING = "引用格式说明（怎么找到原文）"
    LEGEND_BODY = (
        "| `RULE-TRAINRES-116` | 文档内的**规则编号**（`DES` §4.5 配置规则表） |\n"
        "| `AC-TRAINRES-110` | 文档内的**验收标准编号**（`DES` §11） |\n"
        "| `DEC-TRAINRES-141` | 文档内的**决定编号**（`DES` §3 决定清单） |\n"
    )

    def test_heading_is_enough(self):
        assert _is_legend(self.LEGEND_HEADING, "随便什么内容")

    def test_body_pattern_is_enough(self):
        """只看标题不够：正文里反复出现"文档内的**XX编号**"更能说明它在解释记号。"""
        assert _is_legend("别的标题", self.LEGEND_BODY)

    def test_a_real_definition_chunk_is_not_a_legend(self):
        real = "| `RULE-TRAINRES-116` | 同配置类型、使用范围、资源范围只能被一条未删除规则占用；已配置项禁选 |"
        assert not _is_legend("4.5 配置", real)


class TestExcerptFor:
    """★ 需求原文必须取**定义**，不能取引用、更不能取记号说明。"""

    LEGEND = (
        "引用格式说明（怎么找到原文）",
        "| `RULE-TRAINRES-116` | 文档内的**规则编号**（`DES` §4.5 配置规则表） | "
        "| `AC-TRAINRES-110` | 文档内的**验收标准编号** |",
    )
    DEFINITION = (
        "4.5 配置",
        "| `RULE-TRAINRES-116` | 同配置类型、使用范围、资源范围只能被一条未删除规则占用；已配置项禁选 |",
    )
    CITATION = (
        "1. 资源审批配置",
        "该场景依据 `RULE-TRAINRES-116`「已配置项禁选」；若页面没拦住即判缺陷",
    )

    def test_prefers_the_definition_over_a_citation(self):
        got = excerpt_for("RULE-TRAINRES-116", [self.CITATION, self.DEFINITION])
        assert "只能被一条未删除规则占用" in got, got

    def test_skips_the_legend_even_if_it_comes_first(self):
        got = excerpt_for("RULE-TRAINRES-116", [self.LEGEND, self.DEFINITION])
        assert "文档内的" not in got, f"取到了记号说明：{got}"
        assert "只能被一条未删除规则占用" in got

    def test_truncates_at_the_next_table_cell(self):
        """定义是表格行，不截断会串到下一条规则去 —— 后半句是错的比只有半句更误导。"""
        got = excerpt_for("RULE-TRAINRES-116", [self.DEFINITION])
        assert "已配置项禁选" in got
        assert got.count("|") <= 1

    def test_returns_empty_when_only_the_legend_has_it(self):
        assert excerpt_for("RULE-TRAINRES-116", [self.LEGEND]) == ""

    def test_missing_id_returns_empty(self):
        assert excerpt_for("NOT-THERE-999", [self.DEFINITION]) == ""
        assert excerpt_for("", [self.DEFINITION]) == ""


# ---------------------------------------------------------------------------
# 规则抽取
# ---------------------------------------------------------------------------
class TestExtractRules:
    def test_picks_the_definition_not_the_first_mention(self):
        # 元组顺序是 (heading, text, id) —— 传反了会抛 ValueError（见 extract_rules）
        chunks = [
            ("引用格式说明", "| `RULE-A-1` | 文档内的**规则编号** |", 1),
            ("配置", "| `RULE-A-1` | 同一范围只能被一条未删除规则占用 |", 2),
        ]
        got = extract_rules(chunks)
        assert len(got) == 1
        assert "只能被一条未删除规则占用" in got[0]["text"], got
        assert "文档内的" not in got[0]["text"]

    def test_one_entry_per_rule_id(self):
        chunks = [("A", "`RULE-A-1` 定义甲", 1), ("B", "`RULE-A-1` 定义乙", 2)]
        assert len(extract_rules(chunks)) == 1

    def test_ignores_non_rule_prefixes(self):
        got = extract_rules([("A", "`OTHER-THING-1` 这不是规则", 1)])
        assert got == []

    def test_misordered_tuples_are_reported_clearly(self):
        """顺序传反要说清楚，不能让它烂在 `text.find(1)` 里。

        那种报错是 `TypeError: expected string or bytes-like object, got 'int'` ——
        完全指不到"顺序反了"，而这是很容易犯的错。
        """
        with pytest.raises(ValueError, match="heading, text, id"):
            extract_rules([(1, "heading", "text")])  # type: ignore[list-item]

    def test_build_rules_attaches_cases(self):
        rules = [{"req_id": "RULE-A-1", "text": "规则内容", "heading": "配置"}]
        got = build_rules(rules, {"TC-1": ["RULE-A-1"], "TC-2": ["RULE-A-1", "DES-B-2"]})
        assert got[0]["case_count"] == 2
        assert set(got[0]["cases"]) == {"TC-1", "TC-2"}
        assert got[0]["kind"] == "RULE"

    def test_a_rule_with_no_cases_is_reported_as_zero(self):
        """没有被任何用例验证的规则是最该被看见的一类 —— 不能被静默丢掉。"""
        got = build_rules([{"req_id": "RULE-A-1", "text": "x", "heading": ""}], {})
        assert got[0]["case_count"] == 0
        assert got[0]["cases"] == []


# ---------------------------------------------------------------------------
# 覆盖矩阵
# ---------------------------------------------------------------------------
class TestRequirementMatrix:
    def test_inverts_cases_into_requirements(self):
        refs = {"TC-1": ["RULE-A-1"], "TC-2": ["RULE-A-1", "DES-B-2"]}
        got = {r.req_id: r for r in build_requirement_matrix(refs, [], {})}
        assert got["RULE-A-1"].case_count == 2
        assert got["DES-B-2"].case_count == 1

    def test_verified_results_take_precedence_over_auto(self):
        """人工改判过了就不再计自动判定 —— 否则同一条会被算两次。"""
        refs = {"TC-1": ["RULE-A-1"]}
        outcomes = {"TC-1": {"verified": "passed", "auto": "failed"}}
        got = build_requirement_matrix(refs, [], outcomes)[0]
        assert got.verified_pass == 1
        assert got.auto_total == 0, "人工确认过的又被算进了自动判定"

    def test_auto_results_count_when_nobody_reviewed(self):
        refs = {"TC-1": ["RULE-A-1"]}
        got = build_requirement_matrix(refs, [], {"TC-1": {"verified": None, "auto": "failed"}})[0]
        assert got.auto_fail == 1
        assert got.verified_total == 0

    def test_uncovered_is_explicit(self):
        """★ "没人测"必须是一个显式状态，而不是"这行没出现"。"""
        got = build_requirement_matrix({}, [("H", "| `RULE-A-1` | 定义 |")], {})
        # 没有用例引用 → 这条需求根本不会出现在矩阵里（因为我们是从用例反推的）
        assert got == []

    def test_excerpt_is_attached(self):
        refs = {"TC-1": ["RULE-A-1"]}
        got = build_requirement_matrix(refs, [("H", "| `RULE-A-1` | 真实定义内容 |")], {})[0]
        assert "真实定义内容" in got.excerpt

    def test_sorted_by_usage(self):
        refs = {"TC-1": ["DES-B-2"], "TC-2": ["RULE-A-1"], "TC-3": ["RULE-A-1"]}
        got = [r.req_id for r in build_requirement_matrix(refs, [], {})]
        assert got[0] == "RULE-A-1"


class TestModuleMatrix:
    def test_groups_and_counts_no_refs(self):
        cases = [
            {"case_key": "TC-1", "module": "A", "has_refs": True},
            {"case_key": "TC-2", "module": "A", "has_refs": False},
        ]
        got = build_module_matrix(cases, {})
        assert got[0]["case_total"] == 2
        assert got[0]["with_refs"] == 1
        assert got[0]["no_refs"] == 1, "没有需求 ID 的用例数要能看到 —— 它衡量追溯粒度"

    def test_awaiting_review_counts_unreviewed(self):
        cases = [{"case_key": "TC-1", "module": "A", "has_refs": True}]
        got = build_module_matrix(cases, {"TC-1": {"verified": None, "auto": "passed"}})
        assert got[0]["awaiting_review"] == 1
        assert got[0]["auto_pass"] == 1

    def test_verified_rate_is_none_not_zero(self):
        """没有人工样本时必须是 None。

        0% 会被读成"全挂了"，而事实是"没人看过" —— 处置完全相反。
        """
        cases = [{"case_key": "TC-1", "module": "A", "has_refs": True}]
        got = build_module_matrix(cases, {})
        assert got[0]["verified_rate"] is None

    def test_blank_module_is_uncategorized(self):
        got = build_module_matrix([{"case_key": "T", "module": "", "has_refs": False}], {})
        assert got[0]["module"] == "未分类"
