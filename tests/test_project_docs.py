"""项目文档：净化、AI 生成、存取（2026-10-12）。

## 为什么这三块要一起测

用户要的是「一栏里放需求分析/总览，能像 Word 一样编辑」。这条链路上：
`AI 生成 → 落库 → 读出来 → 渲染`，任何一环出问题，用户看到的都是"内容不对"，
但根因完全不同：

* **净化**：用户从 Word/网页粘进来的东西带 `<script>`。这里漏了就是存储型 XSS，
  而且是**每个打开这个页面的同事**都会中招 —— 所以净化的测试按攻击面写，不按覆盖率写。
* **AI 生成**：必须标出处、必须不编造、失败必须如实说。
  测试盯的是"模型乱答时我们怎么收口"，而不是"模型答得好不好"。
* **存取**：一个项目的"总览"只能有一份。允许出现两份互相矛盾的总览，
  用户就不知道该信哪个 —— 这是数据模型层面的不变量，必须钉住。
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.doc_sanitize import html_to_text, sanitize  # noqa: E402
from app.spec_overview import (  # noqa: E402
    OverviewSection,
    _salvage_mermaid_from_text,
    build_plan_prompt,
    match_sources,
    mermaid_balance_ok,
    mermaid_looks_renderable,
    normalize_mermaid,
    parse_sources,
    sections_from_payload,
    sections_to_html,
    trim_unbalanced_tail,
)


@pytest.fixture()
def client():
    """TestClient + 一个只剩本测试自己的临时库。

    和本目录其它 API 测试同一套写法（见 docs/PROJECT_ISOLATION.md 第七节）：
    ★ 直接构造 engine 并赋值给 dbmod._engine / _Session，**不要**用 DATABASE_URL ——
    `_ensure()` 把 engine 缓存在模块级全局变量里，改环境变量完全无效，
    测试会静默写进用户的真实 potato.db（真发生过：真库里多出 9 个项目）。
    拆的时候**还原**而不是置 None，理由同上。
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    dbname = f"_tp_docs_{uuid.uuid4().hex[:8]}.db"
    tmp = os.path.join(tempfile.gettempdir(), dbname)
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    prev_engine, prev_session = dbmod._engine, dbmod._Session
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    from app.main import app

    assert dbname in str(eng.url).replace("%5F", "_"), f"没连到临时库：{eng.url}"
    with TestClient(app) as c:
        yield c

    asyncio.run(eng.dispose())
    dbmod._engine, dbmod._Session = prev_engine, prev_session
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 净化：按攻击面写
# ---------------------------------------------------------------------------
class TestSanitizeStripsScripts:
    """脚本类内容必须**连内容一起**去掉。

    只剥标签不够：`<script>alert(1)</script>` 剥完会剩下可见文本 `alert(1)`，
    页面上多出一段莫名其妙的文字，而攻击面只是变小、没有消失。
    """

    @pytest.mark.parametrize(
        "evil",
        [
            "<script>alert(1)</script>",
            "<SCRIPT>alert(1)</SCRIPT>",
            '<script src="http://evil/x.js"></script>',
            "<iframe src='http://evil'></iframe>",
            "<object data='x'></object>",
            "<embed src='x'>",
            "<svg onload=alert(1)></svg>",
            "<math><mtext></mtext></math>",
            "<style>body{display:none}</style>",
            "<form action='http://evil'><input name=p></form>",
            "<noscript><p>x</p></noscript>",
            "<template><p>x</p></template>",
        ],
    )
    def test_dangerous_tags_are_removed_with_content(self, evil):
        out = sanitize(evil)
        low = out.lower()
        for bad in ("<script", "<iframe", "<object", "<embed", "<svg", "<math", "<style"):
            assert bad not in low, f"{bad} 漏过了：{out!r}"
        assert "alert(1)" not in out, f"脚本正文被留成了可见文本：{out!r}"


class TestSanitizeStripsEventHandlers:
    """`on*` 属性一律丢 —— 它们是最短的一条 XSS 路径。"""

    @pytest.mark.parametrize(
        "src",
        [
            '<p onclick="evil()">x</p>',
            '<img src="a.png" onerror="evil()">',
            '<div onmouseover="evil()">x</div>',
            '<a href="https://a.cn" onfocus="evil()">x</a>',
            "<p ONCLICK='evil()'>x</p>",  # 大小写变体
        ],
    )
    def test_no_on_attributes_survive(self, src):
        out = sanitize(src).lower()
        assert "onclick" not in out
        assert "onerror" not in out
        assert "onmouseover" not in out
        assert "onfocus" not in out


class TestSanitizeUrlSchemes:
    """`javascript:` 是经典载体；`data:` 只对图片放行。"""

    @pytest.mark.parametrize(
        "src",
        [
            '<a href="javascript:alert(1)">x</a>',
            '<a href="JavaScript:alert(1)">x</a>',
            '<a href="  javascript:alert(1)">x</a>',
            '<a href="vbscript:x">x</a>',
            '<img src="data:text/html;base64,PHNjcmlwdD4=">',
            '<img src="data:application/javascript;base64,eA==">',
        ],
    )
    def test_dangerous_schemes_dropped(self, src):
        out = sanitize(src).lower()
        assert "javascript:" not in out
        assert "vbscript:" not in out
        assert "data:text/html" not in out
        assert "data:application" not in out

    def test_images_keep_http_and_data_and_relative(self):
        for src in (
            '<img src="https://a.cn/x.png">',
            '<img src="/artifacts/x.png">',
            '<img src="x.png">',
            '<img src="data:image/png;base64,iVBORw0KGgo=">',
        ):
            assert "<img" in sanitize(src), f"合法图片被误删：{src}"

    def test_relative_image_paths_survive(self):
        """相对路径不能被当成危险值丢掉。

        这是**可用性**问题：富文本里贴图是常态，而编辑器常给出不带前导 `/`
        的路径。丢掉的症状是"图片莫名其妙不见了"，很难查到净化头上。
        """
        out = sanitize('<img src="logo.png" alt="图">')
        assert 'src="logo.png"' in out
        assert 'alt="图"' in out


class TestSanitizeStyleAllowlist:
    """用户要的"字体颜色/字号"就是 style；但 style 也是攻击面。"""

    def test_color_and_font_size_survive(self):
        out = sanitize('<span style="color:#f00;font-size:18px">红</span>')
        assert "color: #f00" in out
        assert "font-size: 18px" in out

    @pytest.mark.parametrize(
        "style",
        [
            "width: expression(alert(1))",
            "background: url(javascript:alert(1))",
            "background-image: url(http://evil/x.png)",  # 外带请求
            "behavior: url(x.htc)",
            "position: fixed",  # 不在白名单 → 丢
        ],
    )
    def test_dangerous_style_values_dropped(self, style):
        out = sanitize(f'<p style="{style}">x</p>').lower()
        assert "expression(" not in out
        assert "url(" not in out
        assert "behavior:" not in out

    def test_unknown_props_are_dropped(self):
        out = sanitize('<span style="color:red;position:fixed">x</span>')
        assert "color: red" in out
        assert "position" not in out


class TestSanitizeOutputShape:
    """输出必须是良构 HTML —— 否则每次回填编辑器都要让解析器猜一次嵌套。"""

    def test_unclosed_tags_are_closed(self):
        assert sanitize("<p>未闭合") == "<p>未闭合</p>"
        assert sanitize("<div><p>x") == "<div><p>x</p></div>"

    def test_stray_close_tag_does_not_corrupt_nesting(self):
        out = sanitize("<p>a</p></div><p>b</p>")
        assert out.count("<p>") == out.count("</p>")

    def test_text_is_escaped(self):
        assert sanitize("<p>a & b < c</p>") == "<p>a &amp; b &lt; c</p>"

    def test_empty_and_blank_inputs(self):
        assert sanitize("") == ""
        assert sanitize("   ") == ""
        assert sanitize(None) == ""  # type: ignore[arg-type]

    def test_input_is_length_capped(self):
        """超长输入要被截断，而不是把几百 MB 写进库里。"""
        out = sanitize("<p>" + "x" * 1000 + "</p>", max_chars=50)
        assert len(out) < 200

    def test_tables_and_lists_survive(self):
        t = '<table><tr><td colspan="2">x</td></tr></table>'
        assert "colspan" in sanitize(t)
        assert "<ul><li>a</li></ul>" in sanitize("<ul><li>a</li></ul>")

    def test_external_link_gets_noopener(self):
        """`target=_blank` 不带 rel 是 tabnabbing —— 自动补上。"""
        out = sanitize('<a href="https://a.cn" target="_blank">x</a>')
        assert "noopener" in out

    def test_html_to_text(self):
        assert html_to_text("<p>a</p><p>b</p>") == "a b"
        assert html_to_text("<h2>标题</h2>") == "标题"


# ---------------------------------------------------------------------------
# AI 生成的收口逻辑（不打网络）
# ---------------------------------------------------------------------------
class TestSectionsFromPayload:
    """模型很爱漏字段或给变体，收口必须容错。

    一个 KeyError 会让整份概览拿不到 —— 而用户看到的是"AI 什么都没生成"，
    根本猜不到是解析炸了。
    """

    def test_normal_payload(self):
        got = sections_from_payload(
            {"overview": "<p>这是一个审批系统</p>", "sources": {"overview": ["1. 概述"]}}
        )
        assert len(got) == 1
        assert got[0].key == "overview"
        assert got[0].sources == ["1. 概述"]

    def test_missing_fields_are_tolerated(self):
        assert sections_from_payload({}) == []
        assert sections_from_payload({"overview": ""}) == []

    def test_bogus_types_do_not_crash(self):
        for bad in (
            {"overview": 123},
            {"overview": None},
            {"overview": "<p>x</p>", "sources": "不是字典"},
            {"overview": "<p>x</p>", "sources": {"overview": "不是列表"}},
            {"overview": [1, 2]},
        ):
            sections_from_payload(bad)  # 不抛即通过

    def test_empty_sections_are_skipped(self):
        """完全空的一节不显示 —— 一个标题配一片空白比没有更让人困惑。"""
        got = sections_from_payload({"overview": "", "modules": "<p>有内容</p>"})
        assert [s.key for s in got] == ["modules"]

    def test_sources_are_trimmed_and_cleaned(self):
        got = sections_from_payload(
            {"overview": "<p>x</p>", "sources": {"overview": ["  a  ", "", None, 5]}}
        )
        assert got[0].sources == ["a"], "非字符串的 source 不该变成字面量 'None'/'5'"


class TestSectionsToHtml:
    def test_includes_sources_by_default(self):
        """依据默认必须带上 —— 那是用户核对 AI 的唯一手段。

        标签是「依据（原文里的章节或条目）」而不是「依据章节」：
        标题清单本身是混的（586 条里有 63 条是角色名这类条目，不是章节），
        写"章节"会让用户去目录里找一个不存在的章节。
        """
        html = sections_to_html([OverviewSection("overview", "项目总览", "<p>x</p>", ["1. 概述"])])
        assert "依据" in html
        assert "章节或条目" in html
        assert "1. 概述" in html

    def test_sources_can_be_suppressed(self):
        html = sections_to_html(
            [OverviewSection("overview", "项目总览", "<p>x</p>", ["1. 概述"])], with_sources=False
        )
        assert "依据" not in html

    def test_empty_section_says_so(self):
        """空节要明说"AI 没找到内容"，而不是留一片空白。"""
        html = sections_to_html([OverviewSection("flow", "主要流程", "", [])])
        assert "没有找到" in html

    def test_titles_are_escaped(self):
        html = sections_to_html([OverviewSection("x", "<script>坏</script>", "<p>a</p>", [])])
        assert "<script>" not in html

    def test_output_survives_the_sanitizer(self):
        """生成的内容会过净化 —— 净化后不能变成空。

        这条防的是"我们自己生成的 HTML 用了白名单外的标签"，
        那样用户拿到手的会是一份被啃掉一半的文档。
        """
        html = sections_to_html(
            [
                OverviewSection("overview", "项目总览", "<p>正文</p><ul><li>a</li></ul>", ["1. 概述"]),
                OverviewSection("flow", "主要流程", "<p>步骤</p>", ["2. 流程"]),
            ]
        )
        out = sanitize(html)
        assert "项目总览" in out and "正文" in out and "主要流程" in out
        assert "1. 概述" in out


class TestBuildPlanPrompt:
    """提示词要真的把"骨架"喂进去，否则 AI 只能瞎猜。"""

    def test_headings_are_numbered_and_included(self):
        p = build_plan_prompt(["一、总述", "二、登录"], "开头内容")
        assert "1. 一、总述" in p
        assert "2. 二、登录" in p
        assert "开头内容" in p

    def test_huge_heading_lists_are_capped_and_disclosed(self):
        p = build_plan_prompt([f"标题{i}" for i in range(2000)], "")
        # 两条截断路径文案不同（条数上限 / 字符上限），但都必须**说明被截了**并给出总数。
        assert "只列了前" in p or "标题清单过长" in p, "截断必须说明，否则 AI 以为那就是全部"
        assert "共 2000 个标题" in p

    def test_intro_is_capped(self):
        p = build_plan_prompt(["a"], "x" * 100_000)
        assert len(p) < 20_000


class TestMermaidNormalize:
    """流程图源码的规范化。

    ## 这一组为什么这么细

    用户的原话是「我想让的是 ai 给我画流程图」—— 而我的第一版生成的是**文字描述**。
    改成真图之后，问题全在"mermaid 的语法细节"上，而**结构检查挡不住语法细节错**：
    实测 `subgraph SG1["申请与审批"].`（多一个句点）过了所有结构检查
    （有 flowchart 头、有连线、节点数够），而真 mermaid 直接 Parse error。

    所以下面的判据都用**真浏览器**验过（见 _check_mermaid.py，8/8 一致），
    用例就是当时踩到的那些形状。
    """

    def test_chinese_node_ids_become_ascii(self):
        """中文 ID 与带标签的边连用时会出错，统一换 ASCII 别名。"""
        got = normalize_mermaid('flowchart TD\n  提交["甲"] --> 审批{"乙"}')
        assert "提交" not in got and "审批" not in got
        assert re.search(r'n1\["甲"\]', got)
        assert "n2" in got

    def test_chinese_id_after_a_labeled_edge(self):
        """★ 带标签的边 `-->|"通过"|` 后面的中文 ID 也要换。

        我第一版的判据是"前面是 `-->`"，于是这种漏了 —— 而它恰恰是最常见的写法。
        """
        got = normalize_mermaid('flowchart TD\n  A["甲"] -->|"通过"| 结算["乙"]')
        assert "结算" not in got

    def test_a_later_reference_to_the_same_id_is_also_renamed(self):
        """★ 节点第二次出现只有名字（没有定义）。

        只换"名字后面跟着括号"的那种，第二处就漏了 —— 图上会同时存在
        `审批` 和它的别名，mermaid 把它们当成两个框，画出两个节点。
        """
        got = normalize_mermaid(
            'flowchart TD\n  提交["甲"] --> 审批{"乙"}\n  审批 -->|"通过"| 结算["丙"]'
        )
        assert "审批" not in got, f"后续引用没被改名：{got}"
        assert got.count("n2") >= 2, got

    def test_edge_labels_are_preserved(self):
        """★ 改名时**不能弄丢连线上的文字**。

        实测踩到：替换时把 `|"通过"|` 一起吃掉，图上少了一条边的说明，
        而图看着仍然"正常"，很难发现。
        """
        got = normalize_mermaid('flowchart TD\n  提交["甲"] -->|"通过"| 审批["乙"]')
        assert '"通过"' in got, f"连线标签被吃掉了：{got}"

    def test_label_parentheses_are_removed_not_the_label(self):
        """★ 标签里的括号要去掉，但**标签内容不能丢**。

        实测踩到的坑：正则把 `A[提交申请(含附件)]` 里的 `提交申请` 当成节点 ID，
        整个标签被换成 `A["n1"]` —— 用户看到个叫 n1 的框，内容没了。
        """
        got = normalize_mermaid("flowchart TD\n  A[提交申请(含附件)] --> B[审批]")
        assert "提交申请含附件" in got, got

    def test_style_directives_are_removed_whole(self):
        """整行去掉，不能只去关键字 —— 残留的 `x fill:#f00` 会让整张图炸。"""
        got = normalize_mermaid(
            'flowchart LR\n  A["甲"] --> B["乙"]\n  classDef x fill:#f00\n  style A fill:#0f0'
        )
        assert "fill:" not in got and "classDef" not in got
        assert 'A["甲"]' in got

    def test_trailing_punctuation_after_a_node_is_removed(self):
        """★ 实测最坑的一处：`subgraph SG1["甲"].` 多一个句点。

        真 mermaid 报 `Parse error on line 2`，而它逃过了所有结构检查。
        """
        got = normalize_mermaid(
            'flowchart TD\n  subgraph SG1["甲"].\n    A["乙"]\n  end\n  A --> B["丙"]'
        )
        assert '"].' not in got, got

    def test_br_tags_are_stripped(self):
        """模型会把 HTML 思维带进来写 `<br/>`，mermaid 不认它。"""
        got = normalize_mermaid('flowchart TD\n  A["甲<br/>乙"] --> B["丙"]')
        assert "<br" not in got
        assert "甲" in got and "乙" in got

    def test_subgraph_chinese_id_is_renamed(self):
        """★ `subgraph 门禁["门禁管理"]` 里的中文 ID 也要换。

        它在 `subgraph` 关键字后面 —— 既不在行首（前面有 subgraph），
        也不在连接符后面，所以前两条位置规则都盖不到。真 mermaid 对中文子图 ID
        报 `Parse error on line 28 … got '1'`（实测截图里就是这个错）。
        """
        got = normalize_mermaid(
            'flowchart TD\n  subgraph 维护["设施设备维护"]\n    A["甲"] --> B["乙"]\n  end'
        )
        assert "subgraph 维护" not in got, got
        assert "设施设备维护" in got, "子图标题不能丢"

    def test_comments_are_removed(self):
        got = normalize_mermaid('flowchart TD\n  %% 注释\n  A["甲"] --> B["乙"]')
        assert "%%" not in got

    def test_empty(self):
        assert normalize_mermaid("") == ""


class TestMermaidRenderable:
    def test_good_diagram(self):
        ok, why = mermaid_looks_renderable('flowchart TD\n  A["甲"] --> B["乙"]')
        assert ok, why

    def test_missing_declaration(self):
        ok, why = mermaid_looks_renderable('A["甲"] --> B["乙"]')
        assert not ok and "flowchart" in why

    def test_no_edges(self):
        ok, why = mermaid_looks_renderable('flowchart TD\n  A["甲"]\n  B["乙"]')
        assert not ok and "连线" in why

    def test_one_node(self):
        ok, _ = mermaid_looks_renderable('flowchart TD\n  A["甲"] --> A')
        assert not ok

    def test_unbalanced_curly(self):
        """★ 来自真实的截断形状。

        `A1["甲"] --> B1{"乙"` —— 引号 4 个（偶数，数引号看不出问题），
        但 `{` 没有配对的 `}`，因为收尾的 `"}` 一起被截掉了。
        """
        ok, why = mermaid_looks_renderable('flowchart TD\n  A1["甲"] --> B1{"乙"')
        assert not ok and "花括号" in why

    def test_unbalanced_quote(self):
        ok, why = mermaid_looks_renderable('flowchart TD\n  A["甲] --> B["乙"]')
        assert not ok, why

    def test_empty(self):
        ok, _ = mermaid_looks_renderable("")
        assert not ok


class TestMermaidBalance:
    def test_ignores_brackets_inside_quotes(self):
        """标签里合法地含括号时不能误判 —— 判据必须跳过引号内的内容。"""
        ok, why = mermaid_balance_ok('A["甲[乙]"] --> B["丙"]')
        assert ok, why

    def test_detects_missing_close(self):
        ok, _ = mermaid_balance_ok('A["甲"] --> B{"乙"')
        assert not ok

    def test_detects_stray_close(self):
        ok, _ = mermaid_balance_ok('A["甲"]] --> B["乙"]')
        assert not ok


class TestTrimUnbalancedTail:
    """截断的图要从末尾退到配平，而不是整张丢掉。"""

    def test_trims_to_the_last_intact_line(self):
        src = 'flowchart TD\n  A["甲"] --> B["乙"]\n  B --> C["丙"]\n  C --> D{"丁'
        got = trim_unbalanced_tail(src)
        assert got.endswith('C["丙"]')
        assert mermaid_looks_renderable(got)[0]

    def test_drops_a_dangling_edge(self):
        """截断常落在连线中间：`L1["丁"] --> L`。

        括号是配平的（balance 检查说没问题），而 mermaid 报
        `Parse error … got '1'`。这类残句只能按形态去掉。
        """
        src = 'flowchart TD\n  A["甲"] --> B["乙"]\n  L1["丁"] --> L'
        got = trim_unbalanced_tail(src)
        assert "L1" not in got, got
        assert mermaid_looks_renderable(got)[0]

    def test_returns_empty_when_nothing_survives(self):
        """只剩声明行 = 没抢救出来，返回空串（一个表头没有意义）。"""
        assert trim_unbalanced_tail('flowchart TD\n  A["半') == ""

    def test_leaves_a_good_diagram_alone(self):
        src = 'flowchart TD\n  A["甲"] --> B["乙"]\n  B -->|"通过"| C["丙"]'
        assert trim_unbalanced_tail(src) == src


class TestSalvageMermaid:
    """从**残缺的** JSON 里抠出图 —— 实测这样救回过好几次。"""

    def test_salvages_from_truncated_json(self):
        raw = '{"mermaid": "flowchart TD\\n  A[\\"甲\\"] --> B[\\"乙\\"]'
        got = _salvage_mermaid_from_text(raw)
        assert got.startswith("flowchart TD")
        assert '"甲"' in got

    def test_salvages_from_complete_json_too(self):
        raw = '{"mermaid": "flowchart TD\\n  A[\\"甲\\"] --> B[\\"乙\\"]", "note": "x"}'
        got = _salvage_mermaid_from_text(raw)
        assert got.endswith('B["乙"]'), got

    def test_unescapes_json_escapes(self):
        assert "\n" in _salvage_mermaid_from_text('{"mermaid": "a\\nb"')

    def test_no_mermaid_field(self):
        assert _salvage_mermaid_from_text('{"html": "x"}') == ""
        assert _salvage_mermaid_from_text("") == ""


class TestMatchSources:
    """出处匹配必须是**纯代码**，而且判据要严。

    ★ 为什么这条如此重要：出处是用户核对 AI 的唯一手段。原来这里是"再发一次
    LLM 调用让它挑"，实测网关一抖就整批为空 —— 正文六节都写出来了、出处六节全空，
    而那时用户毫无办法核对。改成字符串匹配之后它确定性、免费、不会失败。

    ★ 为什么判据必须严（2026-10-12 用户反馈「没正常生成」）：
    第一版只做"去掉空白后包含"，于是在真数据上几乎全错 —— 需求文档的标题清单里
    混着 63 个**角色名**（`场地预占审批人`），它们会作为子串出现在正文里
    （"（场地预占审批人）"），于是每节都匹配满 25 条。用户看到的是一串又长又没用的
    清单，而它的本意是"你可以核对这几节"。
    """

    def test_quoted_mention_counts(self):
        heads = ["干系人分析", "资源审批配置"]
        html = '<p>文档以「干系人分析」列出角色。</p>'
        got = match_sources(html, heads)
        assert "干系人分析" in got
        assert "资源审批配置" not in got, "正文没提到的章节不该被标成出处"

    def test_whole_block_counts(self):
        """独占一个列表项的条目算引用 —— 生成的分节正文就是这么列出的。"""
        heads = ["教学设备采购记录"]
        html = "<ul><li>教学设备采购记录</li><li>别的东西</li></ul>"
        assert match_sources(html, heads) == heads

    def test_a_mere_substring_does_not_count(self):
        """★ 这是那次误报的根因：名字顺带出现在正文中间不算引用。"""
        heads = ["场地预占审批人"]
        html = "<p>还涉及计划管理科室项目经理（场地预占审批人），以及单位高级经理。</p>"
        assert match_sources(html, heads) == [], "顺带提到的名字被算成了依据"

    def test_delimited_ids_may_appear_as_substring(self):
        """带分隔符的 ID 形态很特异，允许作为子串出现（收得宽是故意的）。"""
        heads = ["RULE-TRAINRES-116"]
        html = "<p>据此（见 RULE-TRAINRES-116 与 AC-TRAINRES-110）应禁止重复配置。</p>"
        assert match_sources(html, heads) == heads

    def test_short_headings_are_ignored(self):
        """两三个字的标题碰巧出现太容易了。"""
        assert match_sources("<p>前言 概述 目录</p>", ["前言", "概述", "目录"]) == []

    def test_no_invention_when_nothing_matches(self):
        assert match_sources("<p>完全无关的内容</p>", ["甲章节名称", "乙章节名称"]) == []

    def test_order_follows_the_heading_list_not_length(self):
        heads = ["中等章节名称", "很长的章节名称在这里"]
        html = "<p>「很长的章节名称在这里」和「中等章节名称」</p>"
        got = match_sources(html, heads)
        order = {h: i for i, h in enumerate(heads)}
        assert got == sorted(got, key=lambda x: order[x])

    def test_markup_and_whitespace_do_not_break_matching(self):
        heads = ["教学设备管理"]
        html = "<p>参见\n  「教学设备管理」\n一节</p>"
        assert match_sources(html, heads) == heads

    def test_result_is_capped(self):
        heads = [f"章节名称{i}" for i in range(200)]
        html = "<ul>" + "".join(f"<li>{h}</li>" for h in heads) + "</ul>"
        assert len(match_sources(html, heads, limit=10)) <= 10

    def test_empty_inputs(self):
        assert match_sources("", ["章节名称甲"]) == []
        assert match_sources("<p>x</p>", []) == []
        assert match_sources("<p>x</p>", ["", "  "]) == []


class TestParseSources:
    """兼容旧形状（模型直接给编号）。越界编号必须丢。

    模型会凭空给出清单里不存在的编号。照抄下来，"依据章节"里就会出现
    一条文档里根本没有的标题 —— 那比不给依据更糟，因为看起来像是核对过了。
    """

    def test_indexes_resolve_to_headings(self):
        assert parse_sources({"idx": [1, 3]}, ["甲", "乙", "丙"]) == ["甲", "丙"]

    def test_out_of_range_indexes_are_dropped(self):
        assert parse_sources({"idx": [1, 99, 0, -1]}, ["甲", "乙"]) == ["甲"]

    def test_bogus_types_are_dropped(self):
        assert parse_sources({"idx": ["x", None, {}, 2]}, ["甲", "乙"]) == ["乙"]
        assert parse_sources({}, ["甲"]) == []
        assert parse_sources("不是字典", ["甲"]) == []
        assert parse_sources({"idx": "不是列表"}, ["甲"]) == []

    def test_duplicates_collapse(self):
        assert parse_sources({"idx": [1, 1, 2]}, ["甲", "乙"]) == ["甲", "乙"]


# ---------------------------------------------------------------------------
# 存取不变量：一个项目一种内置文档只有一份
# ---------------------------------------------------------------------------
def test_a_project_has_only_one_overview(client):
    """★ 内置文档按 (project, kind) 唯一。

    允许两份"总览"并存的话，界面上会出现两个看起来都对的答案，
    而用户不知道该信哪个 —— 这正是这个功能最不该有的状态。
    """
    c = client
    pid = c.post("/api/projects", json={"name": "doc-uniq"}).json()["id"]
    a = c.put(f"/api/projects/{pid}/docs", json={"kind": "overview", "title": "A", "html": "<p>1</p>"})
    assert a.status_code == 200, a.text
    b = c.put(f"/api/projects/{pid}/docs", json={"kind": "overview", "title": "B", "html": "<p>2</p>"})
    assert b.status_code == 200, b.text
    docs = [d for d in c.get(f"/api/projects/{pid}/docs").json()["docs"] if d["kind"] == "overview"]
    assert len(docs) == 1, f"出现了多份总览：{docs}"
    assert docs[0]["title"] == "B", "第二次保存没有更新原来那一份"


def test_saving_a_doc_sanitizes_the_body(client):
    """写进去的内容必须是净化后的 —— 这是唯一的防线，不能只靠界面。"""
    c = client
    pid = c.post("/api/projects", json={"name": "doc-xss"}).json()["id"]
    r = c.put(
        f"/api/projects/{pid}/docs",
        json={
            "kind": "overview",
            "title": "t",
            "html": '<p>正常</p><script>alert(1)</script><img src=x onerror=alert(1)>',
        },
    )
    assert r.status_code == 200, r.text
    html = r.json()["html"]
    assert "<script" not in html.lower()
    assert "onerror" not in html.lower()
    assert "正常" in html


def test_dropped_content_is_reported(client):
    """净化丢了东西必须告诉用户，否则他以为"保存成功但内容不见了"。"""
    c = client
    pid = c.post("/api/projects", json={"name": "doc-drop"}).json()["id"]
    r = c.put(
        f"/api/projects/{pid}/docs",
        json={"kind": "overview", "title": "t", "html": "<p>a</p>" + "<script>x</script>" * 40},
    )
    assert r.status_code == 200, r.text
    assert r.json()["dropped_content"] is True


def test_builtin_docs_cannot_be_deleted(client):
    """内置三种删掉了左侧栏会缺一项，用户会以为是权限问题。"""
    c = client
    pid = c.post("/api/projects", json={"name": "doc-del"}).json()["id"]
    row = c.put(f"/api/projects/{pid}/docs", json={"kind": "overview", "title": "t", "html": "<p>x</p>"}).json()
    r = c.delete(f"/api/projects/{pid}/docs/{row['id']}")
    assert r.status_code == 400, r.text


def test_custom_docs_can_be_added_and_deleted(client):
    c = client
    pid = c.post("/api/projects", json={"name": "doc-custom"}).json()["id"]
    a = c.put(f"/api/projects/{pid}/docs", json={"kind": "custom", "title": "甲", "html": "<p>1</p>"}).json()
    b = c.put(f"/api/projects/{pid}/docs", json={"kind": "custom", "title": "乙", "html": "<p>2</p>"}).json()
    assert a["id"] != b["id"], "custom 文档被错误地合并成一份"
    assert c.delete(f"/api/projects/{pid}/docs/{b['id']}").status_code == 200
    kinds = [d["kind"] for d in c.get(f"/api/projects/{pid}/docs").json()["docs"]]
    assert kinds.count("custom") == 1


def test_new_project_lists_three_builtin_placeholders(client):
    """左侧栏永远是同一组入口 —— 缺哪一项会让用户以为是权限问题。"""
    c = client
    pid = c.post("/api/projects", json={"name": "doc-empty"}).json()["id"]
    docs = c.get(f"/api/projects/{pid}/docs").json()["docs"]
    assert {d["kind"] for d in docs} >= {"overview", "flow"}
    assert all(d["kind"] != "analysis" for d in docs), "需求分析已并入总览"
    assert all(d["id"] == 0 for d in docs), "空项目不该有已保存的行"


def test_bad_kind_is_rejected(client):
    c = client
    pid = c.post("/api/projects", json={"name": "doc-bad"}).json()["id"]
    r = c.put(f"/api/projects/{pid}/docs", json={"kind": "不存在的类型", "title": "t", "html": ""})
    assert r.status_code == 400


def test_analyze_reports_failure_instead_of_empty_doc(client, monkeypatch):
    """AI 挂了要**如实报**，不能回一份空文档让用户以为"这个项目没内容"。"""
    c = client
    pid = c.post("/api/projects", json={"name": "doc-fail"}).json()["id"]

    async def boom(_pid: int, **_kw):
        from app.spec_overview import OverviewResult

        return OverviewResult(error="AGNES_API_KEY 未配置")

    monkeypatch.setattr("app.spec_overview.analyze", boom)
    r = c.post(f"/api/projects/{pid}/docs/analyze", json={"kind": "overview"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "AGNES_API_KEY" in body["error"]


def test_analyze_stores_editable_doc_with_sources(client, monkeypatch):
    """★ 生成的结果要落成**可编辑文档**，并带上出处。

    用户要的是"能像 Word 一样编辑"+"AI 分析并截取过"，
    所以这条测试同时钉住两件事：存成了文档、且来源没丢。
    """
    c = client
    pid = c.post("/api/projects", json={"name": "doc-ok"}).json()["id"]
    seen: dict = {}

    async def fake(_pid: int, **kw):
        from app.spec_overview import OverviewResult, OverviewSection

        seen.update(kw)
        return OverviewResult(
            sections=[
                OverviewSection("overview", "项目总览", "<p>业务描述</p>", ["1. 概述"]),
                OverviewSection("flow", "主要流程", "<p>提交→审批</p>", ["2. 流程"]),
            ],
            usage={"total": 1234, "calls": 1},
            headings_used=586,
            doc_chars=1500,
        )

    monkeypatch.setattr("app.spec_overview.analyze", fake)
    r = c.post(f"/api/projects/{pid}/docs/analyze", json={"kind": "overview"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["headings_used"] == 586
    # ★ 需求分析已并入总览（2026-10-20）：总览生成概况/模块/角色/规则/名词表，流程图单独生成。
    assert seen.get("keys") == ("overview", "modules", "roles", "rules", "glossary")
    doc = body["doc"]
    assert "业务描述" in doc["html"]
    assert doc["origin"] == "ai", "AI 生成的内容要能被人一眼看出还没改过"
    assert "1. 概述" in doc["sources"]

    # 再读一次，确认真的落库了（而不是只在响应里）
    again = c.get(f"/api/projects/{pid}/docs").json()["docs"]
    ov = [d for d in again if d["kind"] == "overview"][0]
    assert "业务描述" in ov["html"]


def test_overview_absorbs_analysis_and_kind_is_gone(client, monkeypatch):
    """「需求分析」并入「项目总览」（2026-10-20 用户要求）。

    合并后总览拿概况/模块/角色/规则/名词表；旧的 analysis kind 不再生成，
    避免同一批内容出现在两份文档里。
    """
    c = client
    pid = c.post("/api/projects", json={"name": "doc-merged"}).json()["id"]
    seen: dict = {}

    async def fake(_pid: int, **kw):
        from app.spec_overview import OverviewResult, OverviewSection

        seen.update(kw)
        return OverviewResult(
            sections=[
                OverviewSection("overview", "项目总览", "<p>项目背景介绍</p>", []),
                OverviewSection("modules", "功能模块", "<p>甲模块</p>", []),
                OverviewSection("roles", "角色", "<p>管理员</p>", []),
                OverviewSection("rules", "规则", "<p>同一范围不能重复配置</p>", []),
                OverviewSection("glossary", "名词表", "<p>甲：示例术语</p>", []),
                OverviewSection("flow", "主要流程", "<p>提交→审批</p>", []),
            ],
        )

    monkeypatch.setattr("app.spec_overview.analyze", fake)
    r = c.post(f"/api/projects/{pid}/docs/analyze", json={"kind": "overview"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert seen.get("keys") == ("overview", "modules", "roles", "rules", "glossary")
    html = body["doc"]["html"]
    for text in ("项目背景介绍", "甲模块", "管理员", "同一范围不能重复配置", "示例术语"):
        assert text in html
    assert "提交→审批" not in html, "流程图仍归独立的流程图文档"

    # 旧 kind 不再接受，防止前端/助手继续写出一份重复文档。
    r2 = c.post(f"/api/projects/{pid}/docs/analyze", json={"kind": "analysis"})
    assert r2.status_code == 400


def test_regenerating_appends_by_default(client, monkeypatch):
    """默认追加，不覆盖 —— 用户已经改过的地方不该被一次重新生成抹掉。"""
    c = client
    pid = c.post("/api/projects", json={"name": "doc-append"}).json()["id"]
    c.put(f"/api/projects/{pid}/docs", json={"kind": "overview", "title": "t", "html": "<p>我手写的内容</p>"})

    async def fake(_pid: int, **_kw):
        from app.spec_overview import OverviewResult, OverviewSection

        return OverviewResult(sections=[OverviewSection("overview", "项目总览", "<p>AI 内容</p>", [])])

    monkeypatch.setattr("app.spec_overview.analyze", fake)
    r = c.post(f"/api/projects/{pid}/docs/analyze", json={"kind": "overview"})
    assert r.json()["appended"] is True
    html = r.json()["doc"]["html"]
    assert "我手写的内容" in html, "手写内容被覆盖了"
    assert "AI 内容" in html


def test_replace_flag_overwrites(client, monkeypatch):
    c = client
    pid = c.post("/api/projects", json={"name": "doc-replace"}).json()["id"]
    c.put(f"/api/projects/{pid}/docs", json={"kind": "overview", "title": "t", "html": "<p>旧内容</p>"})

    async def fake(_pid: int, **_kw):
        from app.spec_overview import OverviewResult, OverviewSection

        return OverviewResult(sections=[OverviewSection("overview", "总览", "<p>新内容</p>", [])])

    monkeypatch.setattr("app.spec_overview.analyze", fake)
    r = c.post(f"/api/projects/{pid}/docs/analyze", json={"kind": "overview", "replace": True})
    html = r.json()["doc"]["html"]
    assert "新内容" in html
    assert "旧内容" not in html


# ---------------------------------------------------------------------------
# A/B：检索式 grounding + 逐句溯源（2026-10-13，用户「确保 AI 依据需规」）
# ---------------------------------------------------------------------------
def test_recalled_source_is_fed_into_the_prompt():
    """A：每节的提示词要带**检索到的原文片段**，而不只是标题清单。"""
    from app.spec_overview import build_section_prompt

    p = build_section_prompt(
        "rules", ["甲", "乙"], "这是需规开头", "### 甲-规则\n同一范围不能配两条。"
    )
    assert "同一范围不能配两条" in p, "检索到的原文没进提示词"
    assert "只依据这些原文写" in p


def test_provenance_flags_sentences_with_no_source():
    """B：逐句溯源 —— 有据的句子 vs 找不到依据的句子都要能被标出来。"""
    from app.spec_overview import OverviewSection, sections_to_html

    sec = OverviewSection("rules", "规则", "")
    sec.recalled = "同一个资源范围不能重复配置审批人。"
    sec.html = (
        "<p>同一个资源范围不能重复配置审批人，所以提交时要校验。</p>"
        "<p>系统通常还会自动发短信通知相关角色，属于凭空推断的一句。</p>"
    )
    html = sections_to_html([sec])
    assert "溯源" in html
    assert "在需规原文里找得到依据" in html
    assert "凭空推断" in html, "找不到依据的句子没被标出来"


def test_recalled_is_not_leaked_into_the_api_dict():
    """recalled 是生成过程的输入，不该进 as_dict()（会把接口响应撑大）。"""
    from app.spec_overview import OverviewSection

    sec = OverviewSection("x", "t", "<p>a</p>", [])
    sec.recalled = "一大堆原文"
    assert "recalled" not in sec.as_dict()


def test_section_prompt_stays_under_the_gateway_limit():
    """★ 每节的提示词必须留在网关的字符上限内（约 4.5~5k）。

    否则 `specgen._call_json` 会"截尾重试"（缩到 70% / 45%），把**排在尾部的
    「需规原文片段」和输出格式说明整块截掉** —— 等于"喂了原文却没到模型手上"。
    2026-10-13 实测：586 标题的需规下原提示词 8486 字符，缩到 45% 后检索原文全没了。
    """
    from app.spec_overview import build_section_prompt

    headings = [f"模块{i}-需求说明{i}" for i in range(586)]
    intro = "背景与范围说明内容。" * 40
    recalled = "这是从需规检索出来的原文片段内容。" * 200  # 远超上限
    for key in ("overview", "flow", "rules", "glossary"):
        p = build_section_prompt(key, headings, intro, recalled)
        assert len(p) <= 4500, f"{key} 的提示词 {len(p)} 字符，超过网关上界"
        assert "只依据这些原文写" in p, f"{key} 的检索原文被标题清单挤掉了"


def test_full_mode_uses_its_own_source_label():
    """「通读全文」模式的提示词要说清"这是通读抽出的要点"，别让人以为仍是原文摘录。"""
    from app.spec_overview import _SOURCE_LABEL_FULL, build_section_prompt

    p = build_section_prompt("rules", ["甲"], "背景", "- 同一范围不能配两条。", _SOURCE_LABEL_FULL)
    assert "需规通读要点" in p
    assert "只依据这些原文写" in p


def test_image_upload_rejects_non_images(client):
    """上传端点会把文件存到公开可读路径，所以"任意文件"等于开了个静态托管。"""
    c = client
    pid = c.post("/api/projects", json={"name": "doc-img"}).json()["id"]
    r = c.post(
        f"/api/projects/{pid}/docs/image",
        files={"file": ("evil.html", b"<script>alert(1)</script>", "text/html")},
    )
    assert r.status_code == 400, r.text
