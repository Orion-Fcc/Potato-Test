"""用例页深链筛选（2026-10-12）。

## 为什么需要这组断言

项目概况面板里每行都挂着用例链接（"哪几条用例在测这条需求"）。
那些链接要真能用，需要三件事同时在位，而**任何一件缺失都不会报错**：

1. **URL 参数真的参与筛选。** `?refs=` / `?keys=` 少了任何一个，
   链接点进去就是"全部用例" —— 用户以为筛过了，实际没筛。
2. **界面说明"你只看到一部分"。** 不说的话，用户会以为用例库就剩这几条。
3. **大分组不能折叠、行窗口不能截断。** 这两个"性能优化"会把要找的用例
   静默藏起来。实测症状：矩阵写 357 条，点进去只渲染 315 行，
   而用户完全不知道为什么对不上。

前端没有测试运行器（`web/package.json` 只有 dev/build/preview），
所以按本仓库既有的做法（见 test_run_report_layout.py）做**源码级断言**。
它挡不住"逻辑写错"，但挡得住"有人顺手把参数名改了/把深链的豁免删了" ——
那正是这类功能最常见的坏法。
"""

from __future__ import annotations

import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "web" / "src"
CASES = WEB / "pages" / "CasesPage.tsx"
INSIGHTS = WEB / "components" / "InsightsPanel.tsx"


@pytest.fixture(scope="module")
def cases_src() -> str:
    if not CASES.exists():  # pragma: no cover
        pytest.skip(f"找不到 {CASES}")
    return CASES.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def insights_src() -> str:
    if not INSIGHTS.exists():  # pragma: no cover
        pytest.skip(f"找不到 {INSIGHTS}")
    return INSIGHTS.read_text(encoding="utf-8")


def test_the_page_reads_both_url_params(cases_src: str) -> None:
    """两个参数都要读：`refs`（按需求 ID 筛）和 `keys`（按编号精确筛）。"""
    assert 'params.get("refs")' in cases_src, "没读 ?refs=，按需求 ID 的深链会失效"
    assert 'params.get("keys")' in cases_src, "没读 ?keys=，按编号的深链会失效"


def test_both_params_actually_filter(cases_src: str) -> None:
    """读到参数还不够，必须真的用在 filter 里。

    "读了但没用"是一种很容易犯的坏法：变量定义了、lint 也不报
    （因为有 `onlyKeys.size > 0` 之类的用法），但筛选根本没生效。
    """
    body = re.search(r"const filtered = useMemo\((.*?)\n  \);", cases_src, re.S)
    assert body, "找不到 filtered 的计算"
    code = body.group(1)
    assert "onlyKeys.has(" in code, "?keys= 读了但没参与筛选"
    assert "onlyRefs.some(" in code, "?refs= 读了但没参与筛选"
    assert "c.references" in code, "?refs= 必须比对用例的 references 字段"


def test_the_banner_says_you_are_seeing_a_subset(cases_src: str) -> None:
    """必须明说"只显示了一部分"，否则用户以为用例库就剩这几条。"""
    assert "显示全部用例" in cases_src, "深链视图没有「显示全部」的出口"
    assert "只显示" in cases_src, "深链视图没有说明自己在筛选"


def test_the_banner_reports_the_actual_count(cases_src: str) -> None:
    """★ 报的必须是**实际筛出来的**条数，不是链接里声称的条数。

    两者不一致时界面会自相矛盾（实测 357 vs 315），而用户无从判断该信哪个。
    用 filtered.length 就从结构上消除了这种可能。
    """
    # 找到"按 refs 拼提示条"那一段（括号是多行的，用 DOTALL 跨行取）
    banner = re.search(r"\{onlyRefs\.length > 0\s*\n?\s*\?\s*t\((.*?)\n\s*: t\(", cases_src, re.S)
    assert banner, "找不到深链提示条的拼装"
    assert "filtered.length" in banner.group(1), (
        "提示条没报实际条数 —— 它可能与列表实际渲染的行数不一致"
    )


def test_deep_links_disable_the_two_hiding_optimizations(cases_src: str) -> None:
    """★ 大分组折叠与行窗口在深链时必须关掉。

    这两个优化本身是对的（370 行的表格全量挂载很重），但它们会**静默藏掉**
    深链要找的用例：分组默认折叠（>40 条）时目标行不在 DOM 里；
    每组只渲染前 50 行时，深链指向第 200 条就看不到。
    实测症状就是矩阵写 357、点进去只有 315。
    """
    assert "deepLinked" in cases_src, "没有深链状态的判定"
    # isOpen：深链时一律返回 true
    is_open = re.search(r"const isOpen = \(m: string, size: number\) => \{(.*?)\n  \};", cases_src, re.S)
    assert is_open, "找不到 isOpen"
    assert "if (deepLinked) return true;" in is_open.group(1), (
        "深链时大分组仍会折叠 —— 要找的用例不会出现在页面上"
    )
    # 行窗口：深链时不截断
    assert "deepLinked ? list.length" in cases_src, (
        "深链时行窗口仍在截断 —— 深链指向的用例可能被藏起来"
    )


def test_the_row_window_and_auto_collapse_still_exist(cases_src: str) -> None:
    """★ 反向断言：不能为了深链把这两个优化**整体删掉**。

    没有深链时它们是必要的（否则 370 行的表格全量挂载，页面会卡）。
    修法是"深链时豁免"，不是"取消优化"。
    """
    assert "AUTO_COLLAPSE_AT" in cases_src, "大分组折叠被删了 —— 页面会变重"
    assert "ROW_PAGE" in cases_src, "行窗口被删了 —— 页面会变重"


def test_the_insights_matrix_links_by_requirement_id(insights_src: str) -> None:
    """矩阵里的用例数要按**需求 ID** 链出去，不是把几百个编号塞进查询串。

    实测枚举 keys 的链接有 6000 字符：没法分享、可能被截断，也看不出筛了什么。
    """
    assert "/cases?refs=" in insights_src, "矩阵没有按需求 ID 深链"
    assert "r.cases.join" not in insights_src, (
        "矩阵把用例编号列表拼进了 URL —— 几百个编号会撑爆链接"
    )


def test_case_lists_still_link_by_key(insights_src: str) -> None:
    """规则清单/失败台账里的**具体用例编号**还要能精确链到那一条。

    这两处的列表本来就是短的（几条），精确链比按需求筛更准。
    """
    assert "/cases?keys=" in insights_src, "用例编号不再可点"


# ---------------------------------------------------------------------------
# 富文本编辑器要跟着外部内容走
# ---------------------------------------------------------------------------
EDITOR = WEB / "components" / "RichTextEditor.tsx"


@pytest.fixture(scope="module")
def editor_src() -> str:
    if not EDITOR.exists():  # pragma: no cover
        pytest.skip(f"找不到 {EDITOR}")
    return EDITOR.read_text(encoding="utf-8")


def test_the_editor_syncs_when_the_value_arrives_later(editor_src: str) -> None:
    """★ 用户报「没正常生成」的真根因（2026-10-12）。

    `useEditor({content: value})` 只在**创建时**读一次，而这个组件的 value 是
    异步来的：打开页面时文档还在请求中（先渲染空编辑器），点「AI 分析与生成」
    也是先渲染再拿到结果。内容到了却没人告诉编辑器，于是编辑区一片空白 ——
    而库里明明存着 24033 字符（12 个 h2、30 个 h3、4 张表）。

    必须有一个以 value 为依赖的同步效应。
    """
    assert "setContent(" in editor_src, (
        "编辑器没有在 value 变化时重设内容 —— 异步加载的文档会显示成空"
    )
    # 该效应必须依赖 value：只依赖 editor 的话内容变了也不会同步
    eff = re.search(r"useEffect\((.*?)\n  \}, \[([^\]]*)\]\);", editor_src, re.S)
    assert eff, "找不到同步用的 useEffect"
    assert "value" in eff.group(2), f"同步效应没有依赖 value：deps={eff.group(2)!r}"


def test_the_sync_guards_against_a_feedback_loop(editor_src: str) -> None:
    """★ 两个守卫缺一不可，否则打字就没法用。

    * 不比较 `getHTML() !== value` → 每次 onChange 都会 setContent，
      光标被重置到开头，一个字都打不进去；
    * 不用 `emitUpdate: false` → 程序性灌入反过来触发 onChange，
      把"外部内容到了"当成"用户编辑了"，dirty 标记一直是脏的。
    """
    assert "getHTML()" in editor_src, "没有比较当前内容就 setContent —— 会打断打字"
    assert "emitUpdate: false" in editor_src, (
        "程序性灌入会触发 onChange —— 会把非用户编辑记成用户编辑"
    )
