"""AI 项目分析：把整份需规读成"人看得懂的项目概况"（2026-10-12）。

## 用户要的是什么

原话：「我希望你加上一栏，里面放上需求分析或者该项目的总览……
最好是和我上传的大需规一样，但是你那个要是 **AI 分析并截取过的**，
因为我上传的大需规可能不止这一个模块」。

区别就在"截取"两个字：

* 知识库（`KnowledgeChunk`）里是**原件**：46 万字、586 个标题，用于检索；
* 这里要的是**提炼**：这个项目有哪些模块、流程怎么走、角色怎么分、规则是什么。

所以它不是"把文档再显示一遍"，而是**有损压缩**——丢掉细节，留下骨架。

## 三条设计约束

1. **必须标出处。** 每一节都带上它依据的章节标题。用户能核对，就能信任；
   核对不了，这就是一段看起来很像真的文字。这是"AI 分析"和"AI 编造"的唯一区别。
2. **不编造。** 提示词里写死：文档没写的不要补。宁可留空，也不要写一句
   "通常这类系统会……"——那种句子读起来最顺，也最假。
3. **绝不抛异常。** 生成失败要在界面上说清楚（网关 401、超时），
   而不是回一份空文档让用户以为"这个项目没内容"。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("potato-test.spec_overview")

# 生成用的文档字符上限。真实需求文档远远超过这个数，所以这里是"有损"的第一道：
# 先给文档开头（判领域）+ 全部标题清单（骨架），再让模型按需索取。
# 为什么不把正文全塞进去：46 万字 ≈ 30 万 token，一次调用就爆了，而且绝大部分是细节，
# 概览用不到。**标题就是骨架**，这正是"截取"的关键手段。
# ★ 这几个上限是按**网关约束反推**的，不是随便定的（2026-10-13 重算）。
#
# 这个网关 user 提示词超过约 4.5~5k 字符就返回**空回复**（见 specgen._call_json），
# 而它的兜底是"截尾重试"（缩到 70% / 45%）—— 提示词一超限，**排在尾部的
# 「需规原文片段」和输出格式说明会被直接截掉**。
# 实测（586 标题的需规）：原来的 intro1500 + 标题清单240条 + 原文3000 = **8486 字符**，
# 缩到 45% 后检索原文整块消失 —— 等于"喂了原文，却没到模型手上"。
# 所以整条提示词必须压到 ≤ ~4200 字符；下面几个上限就是按这个总额分配的。
_MAX_INTRO_CHARS = 400  # 「这是什么系统」用不着 1500 字，400 够判断领域

# 一次生成里最多引用多少个章节标题（给模型当骨架看）。
_MAX_HEADINGS = 240
# 标题清单的**字符**上限（光限条数挡不住长标题）。清单只是骨架，不该挤掉原文。
_MAX_LISTING_CHARS = 1000

# 检索到的"需规原文片段"最多多少字 —— 这才是模型真正要依据的内容。
_SECTION_CHARS = 1800

# 提示词里"依据内容"那段的标题。检索模式 / 通读模式文案不同 ——
# 一个是原文摘录、一个是通读后抽出的要点，别让人（和模型）以为都是原文。
_SOURCE_LABEL_RETRIEVED = "需规原文片段（**只依据这些原文写**；这是从项目需规里检索出的相关段落）"
_SOURCE_LABEL_FULL = "需规通读要点（**只依据这些原文写**；这是把整份需规通读一遍后抽出的相关事实）"

# ---- 「通读全文」模式（2026-10-13 用户要求"是否检索全文"开关）----------------
#
# 网关单次 user 提示词超过约 4.5~5k 字符就返回空，一份 3 万字的需规**塞不进一次调用**，
# 所以"全文"只能靠**分块多次调用**：把整份需规分段读一遍、抽成"按节归类的事实条目"，
# 再按这些笔记写各节。慢（几十次调用）、贵，但真读到了全文 —— 由用户开关决定。
_FULL_CHUNK_CHARS = 2200  # 每次通读喂进去的原文上限
_FULL_MAX_CHUNKS = 30  # 通读调用次数上限（防跑飞）
_FULL_NOTE_CAP = 80  # 每节保留的笔记条数上限

_FULL_SYSTEM = """你是资深需求分析师。用户会给你**需求文档的一段原文**。
请只抽取原文里**确实写了的事实**，按下面六类归好；原文没写的**一律不写**，不要推断、不要补常识。
用中文，只输出 JSON。

六类：overview（项目总览 / 背景 / 目标 / 范围）、modules（模块与功能）、flow（流程 / 状态流转 / 审批）、
roles（角色 / 权限 / 岗位）、rules（规则 / 校验 / 约束）、glossary（名词 / 术语）。"""

# 每节用一组"检索词"去知识库取最相关的**原文片段**（2026-10-13，用户要求
# 「确保 AI 是依据需规来改文档」）。只喂标题等于只给骨架，正文才给事实 ——
# 检索片段让模型手里有原文，显著压低"照着标题名编内容"的幻觉。
_SECTION_QUERY = {
    "overview": "系统 背景 目标 范围 总体 概述 说明",
    "modules": "模块 功能 菜单 清单 组成",
    "flow": "流程 申请 提交 审批 状态 流转 驳回",
    "roles": "角色 权限 用户 账号 岗位",
    "rules": "规则 校验 约束 限制 必填 条件",
    "glossary": "名词 术语 定义 简称",
}


@dataclass
class OverviewSection:
    """生成出来的一节内容。"""

    key: str          # overview | modules | flow | roles | rules | glossary
    title: str
    html: str = ""
    # 依据哪些章节 —— 界面上直接显示，用户据此核对
    sources: list[str] = field(default_factory=list)
    # 「主要流程」这一节的 Mermaid 源码（**不是 HTML**）。
    # 存源码而不是渲染结果，是为了让用户能改：图是文本画出来的。
    mermaid: str = ""
    # 画不出来时的原因（界面上要如实说明，而不是留一片空白）
    mermaid_error: str = ""
    # 本节的**依据原文**（从知识库检索到的片段，2026-10-13）。
    # 不进 as_dict()：它是生成过程的输入，接口不需要回传（回传会撑大响应）。
    # 用途：`sections_to_html` 拿它做「逐句溯源」。
    recalled: str = field(default="", repr=False)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "html": self.html,
            "sources": self.sources,
            "mermaid": self.mermaid,
            "mermaid_error": self.mermaid_error,
        }


@dataclass
class OverviewResult:
    sections: list[OverviewSection] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str = ""
    # 用了多少标题、文档多少字符 —— 让用户知道"AI 到底看了多少"
    headings_used: int = 0
    doc_chars: int = 0
    # 本轮**真正喂给模型的需规原文**合计多少字（各节检索片段之和，2026-10-13）。
    # 这才是"AI 读了多少正文"的诚实数字 —— doc_chars 只是那 400 字的开头。
    recalled_chars: int = 0


_PLAN_SYSTEM = """你是资深需求分析师。用户会给你一份软件项目的需求文档（可能只是片段）
和它的章节标题清单。你的任务是产出一份**给人看的项目概况**。

## 铁律

1. **只写文档里有的。** 文档没提到的一律不写。宁可写空，
   也不要写"通常这类系统会……"这种推断句 —— 那种句子读起来最顺、也最假。
2. **标出处。** 每一节都要给出它依据的章节标题（`sources`），逐字照抄清单里的标题。
   标不出出处的结论就不要写。
3. 用**中文**输出。只输出 JSON，不要任何解释文字。

`html` 字段里可以用这些标签让内容更好读：`<p>` `<ul><li>` `<strong>` `<em>`
`<h3>` `<table><tr><td>`。不要用 `<script>`/`<style>`/`on*` 属性。"""

# 「主要流程」这一节改画**流程图**（2026-10-12 用户要求："我想让的是 ai 给我画流程图"）。
#
# 上一版这里生成的是**文字描述**流程（"谁先做什么 → 进入什么状态 →"），
# 而用户要的是一张图。所以这一节改成让模型输出 **Mermaid flowchart 源码**，
# 前端用 mermaid 渲染成真正的图。
#
# 为什么让模型直接写 Mermaid 而不是输出 JSON 再自己画：
#   * 流程图布局（分层、避让、连线走位）是**有难度的算法**，自己写必然很丑；
#   * Mermaid 是文本格式，用户能看懂、能改 —— 存进文档的也是文本，
#     不像图片那样改不了；
#   * 它已经是这个领域的事实标准。
_FLOW_SYSTEM = """你是资深需求分析师。用户给你一份软件项目的需求文档片段和章节标题清单。
你的任务是画出一张 **Mermaid 流程图**，讲清这个系统的主要业务流程。

## 铁律

1. **只画文档里写了的。** 文档没说"驳回"就不要画驳回分支。
   宁可少画两条线，也不要补一个文档里不存在的状态 —— 那种图看起来最专业、也最假。
2. 只输出 JSON：`{"mermaid": "<mermaid 源码>", "note": "<一句话说明这张图画的是什么，可空>"}`
3. 用**中文**标注节点。

## Mermaid 写法要求（很重要，写错就渲染不出来）

* 第一行必须是 `flowchart TD`（从上到下）或 `flowchart LR`（从左到右）。
* 节点 ID 只用 **ASCII 字母数字**（`A`、`B1`、`step1`），中文只能出现在标签里。
* 标签一律用方括号并加引号：`A["提交申请"]`。**不要**在标签里用 `()`、`[]`、`{}`
  这些符号，它们会和 Mermaid 语法冲突；需要说明就写进标签文字里。
* 判断节点用花括号：`B{"是否通过"}`。
* 连线：`A --> B`；带标签的连线 `A -->|"通过"| B`；
  驳回这类回边 `B -->|"驳回"| A`。
* 需要时用子图分组：`subgraph 申请人` … `end`。
* **不要**写 `%%` 注释、不要写 `classDef`/`style`/`linkStyle`（配色由系统统一处理）。
* 节点总数控制在 **6~18 个**：太少说不清，太多看不清。
* 一张图只画**一条主线**（申请—审批—结算这类）。有多条主线时，
  选最主要的一条画，其他在 note 里说明。"""

# 「主要流程」节的提示词要单独拼：它要的是 Mermaid 源码，不是 HTML。


# ★ 一节一次调用，而不是一次要六节（2026-10-12）。
#
# 为什么改：原来一次要模型把六节全写进一个 JSON，实测**被硬截断** ——
# 输出在 2448 字符处断掉、JSON 没收尾，整份概览一个都拿不到。
# 而实测网关的 max_tokens **没有被夹**（能拿到 3432 个 completion，
# finish_reason=stop 时 JSON 是完整的），所以问题不是上限太小，
# 而是"一次要的东西太多、必然撞上截断"。
#
# 拆开之后每个请求的输出都远小于安全线，而且**一节挂了不影响其它节** ——
# 六节里坏一节，用户拿到的是五节，而不是零节。
_SECTION_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "overview",
        "项目总览",
        "写 3~6 个段落，说清：这个系统是干什么的、给谁用、核心业务对象是什么、"
        "解决什么问题。要让一个没读过需求文档的人看完就有整体印象。",
    ),
    (
        "modules",
        "功能模块",
        "把功能分成 4~12 个模块。每个模块写成一段：**模块名** —— 它管什么、"
        "主要入口/页面是什么。不要罗列每个按钮。",
    ),
    (
        "flow",
        "主要流程",
        "画一张 Mermaid 流程图，讲清主要业务流程：谁先做什么 → 进入什么状态 → 最后什么结果。"
        "要能看出角色和状态流转。这张图会直接渲染给用户看。",
    ),
    (
        "roles",
        "角色与权限",
        "有哪些角色/权限组，各自能做什么、不能做什么。"
        "如果文档里写了具体账号或岗位，一并列出。",
    ),
    (
        "rules",
        "关键业务规则",
        "关键业务规则：校验、约束、计算口径、禁选/互斥条件。"
        "一条一句，写成能当断言用的句子（例如「同一『使用范围+资源范围』不能配两条」）。",
    ),
    (
        "glossary",
        "名词表",
        "文档里出现的专有名词、缩写、编号前缀（如 R-/C-/U-/D-）及其含义。"
        "没有就返回空。",
    ),
)


def build_section_prompt(
    key: str,
    headings: list[str],
    intro: str,
    extra: str = "",
    source_label: str = _SOURCE_LABEL_RETRIEVED,
) -> str:
    """针对**某一节**拼提示词。抽成函数是为了可单测（不必真调模型）。"""
    hs = [h.strip() for h in headings if h and h.strip()]
    used = hs[:_MAX_HEADINGS]
    listing = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(used))
    more = ""
    if len(listing) > _MAX_LISTING_CHARS:
        # 按行截，别把最后一条标题截成半截。
        listing = listing[:_MAX_LISTING_CHARS].rsplit("\n", 1)[0]
        more = f"\n（标题清单过长，这里只列了开头一部分，共 {len(hs)} 个标题。）"
    elif len(hs) > len(used):
        more = f"\n（清单过长，这里只列了前 {len(used)} 个，共 {len(hs)} 个标题。）"
    task = next((d for k, _t, d in _SECTION_SPECS if k == key), "")
    parts = [
        f"## 本次只写这一节：{dict((k, t) for k, t, _d in _SECTION_SPECS).get(key, key)}",
        f"要求：{task}",
        f"\n## 文档开头（判断这是什么系统）\n{(intro or '').strip()[:_MAX_INTRO_CHARS]}\n",
        f"## 章节标题清单（共 {len(hs)} 个）\n{listing}{more}\n",
    ]
    if extra.strip():
        parts.append(f"## {source_label}\n{extra.strip()[:_SECTION_CHARS]}\n")
    if key == "flow":
        # 流程这一节要的是 Mermaid 源码，不是 HTML —— 输出格式完全不同
        parts.append(
            '只输出这个 JSON：{"mermaid": "<Mermaid flowchart 源码>", "note": "<可选：一句话说明>"}\n'
            '画不出流程时输出 {"mermaid": "", "note": "文档里没有可画的主线流程"}。'
            "不要输出 HTML，不要解释。"
        )
    else:
        parts.append(
            '只输出这个 JSON：{"html": "<这一节的正文>"}\n'
            "没有可写的内容时输出 {\"html\": \"\"}。"
            "**不要**在 JSON 里列依据章节（那会另算），不要输出其它节，不要解释。"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Mermaid 流程图的校验（2026-10-12 用户要求"ai 给我画流程图"）
# ---------------------------------------------------------------------------
#
# ## 为什么服务端也要校验
#
# 前端用 mermaid 渲染，而 mermaid 对语法**很挑**：一个没加引号的括号、
# 一个中文节点 ID、一个 `%%` 注释，都会让整张图渲染成一块红色的报错文字。
# 而模型恰好爱写这些东西（它没见过我们的渲染约束）。
#
# 所以这里做两道事：
#   1. **规范化**：能修的顺手修（中文节点 ID、缺引号的标签）；
#   2. **判可信**：修不好的宁可不存图，也不要存一张渲染必炸的源码 ——
#      用户在页面上看到一片红字，只会以为功能坏了。
#
# 校验只做"够不够安全地交给 mermaid"，不做完整的语法解析（那是 mermaid 自己的事）。

# 节点 ID 必须是 ASCII：`A`、`B1`、`step1`。mermaid 允许中文 ID 但很脆，
# 而且和高亮/主题有兼容问题，所以统一要求 ASCII。
_FLOW_HEAD = re.compile(r"^\s*(flowchart|graph)\s+(TD|TB|LR|RL|BT)\s*$", re.I | re.M)
# 方括号内的标签若没被引号包住、且含这些字符，mermaid 会解析失败
_LABEL_RISKY = re.compile(r"\[([^\]\"]*?[\(\)\{\}\[\];][^\]\"]*?)\]")
# 中文节点 ID 的位置：**行首**，或**连接符之后**（`-->` / `---` / `--|标签|` / `|`）。
#
# ★ 为什么必须限定位置（2026-10-12 实测踩到两次）：
#   * 写成"任何中文字后面跟 `[`"→ `A[提交申请(含附件)]` 里的 `提交申请` 被当成 ID，
#     整个标签变成 `A["n1"]`，用户看到个叫 n1 的框；
#   * 写成"前面是 `-->`"漏掉了**行首**的中文 ID（`审批 -->|"通过"| n3[...]` 这种，
#     `审批` 在行首，没有前导连接符）。
# 两个坑都指向同一件事：**ID 在连接符旁边，标签在方括号里**。按位置判才准。
#
# 前缀用捕获组保留原空白，否则替换时会把 `-->` 前后的空格吃掉（也踩过）。
# 连接符要**整段**吃掉：`-->` 是一种，`-->|"通过"|`（带标签的边）是另一种。
# 只匹配 `-->` 的话，行首/带标签边后面的中文 ID 会漏掉 —— 实测漏过两次。
#
# ★ 中间的 `(?:.*?\|)?` 不能用 `(?:[^|\n]*\|)?`（2026-10-12 第三次修）：
# `[^|]*` 遇到标签里的**双引号**会提前失配 —— `-->|"通过"| 结算[` 匹配不上，
# 而那恰恰是最常见的一种写法。用非贪婪 `.*?` 一路退到最近的 `|` 才对。
#
# ★ 标签那一段必须是**捕获组**（`(.*?\|)?`）而不是非捕获组：替换时要把它原样写回，
# 否则 `-->|"通过"| 结算[` 会被替换成 `--> n3[` —— **连线上的"通过"没了**。
# 实测踩到：图上少了一条边的文字说明，而图看着仍然"正常"，很难发现。
_BAD_ID_AT_START = re.compile(r"^(\s*)([\u4e00-\u9fff][\w\u4e00-\u9fff]*)(\s*[\[\{\(])", re.M)
# ★ `subgraph <中文ID>["标题"]` 也要换（2026-10-12 实测漏过）。
# 它是**子图**的声明，位置在 `subgraph` 关键字后面 —— 既不在行首（前面有 `subgraph`），
# 也不在连接符后面，前两条规则都盖不到。真 mermaid 对中文子图 ID 报
# `Parse error on line 28 … got '1'`。
_BAD_SUBGRAPH_ID = re.compile(
    r"(\bsubgraph\s+)([\u4e00-\u9fff][\w\u4e00-\u9fff]*)(\s*[\[\{\(])"
)
_BAD_ID_AFTER_EDGE = re.compile(
    r"(-->|---|--\|)(.*?\|)?(\s*)([\u4e00-\u9fff][\w\u4e00-\u9fff]*)(\s*[\[\{\(])"
)
# 配色/交互指令：整行去掉。原来只去掉关键字，留下一行 `x fill:#f00` 让整张图炸掉。
_STYLE_DIRECTIVES = re.compile(
    r"^\s*(?:classDef|class|style|linkStyle|click)\b[^\n]*$", re.I | re.M
)
_COMMENTS = re.compile(r"^\s*%%.*$", re.M)

# 节点/子图定义后面多余的标点：`subgraph SG1["申请与审批"].` → 去掉那个句点。
#
# 用真的 mermaid 验证过：带尾点时整张图 Parse error on line 2（2026-10-12）。
# 只清"定义结尾"的标点，不动标签**内部**的标点（那可能是内容的一部分）。
_TRAILING_JUNK = re.compile(r"(\]|\})[ \t]*[.,;:、。，；：]+[ \t]*$", re.M)


def normalize_mermaid(src: str) -> str:
    """把模型给的 Mermaid 源码清成"大概率能渲染"的形状。纯函数。

    ## 处理顺序很关键

    必须**先修标签、再换中文 ID**。反过来的话，标签里的中文会被当成节点 ID
    （`A[提交申请(含附件)]` → `A["n1"]`），框里就只剩一个 n1 —— 实测踩过这个坑。

    只做低风险规范化，不做猜测性改写：
      * 去掉 `%%` 注释与 `classDef`/`style`/`linkStyle`/`click` **整行**（配色由系统统一）；
      * 标签里的危险符号去掉并补上引号；
      * 中文节点 ID 换成 ASCII 别名 `n1`/`n2`（中文 ID 与 `-->|标签|` 连用时会出错）。
    """
    if not src:
        return ""
    out = _COMMENTS.sub("", src)
    out = _STYLE_DIRECTIVES.sub("", out)
    lines = [ln.rstrip() for ln in out.replace("\r\n", "\n").split("\n")]
    out = "\n".join(ln for ln in lines if ln.strip())

    # ★ `<br/>` 在 mermaid 标签里不是换行，是**语法垃圾**（2026-10-12 实测：
    #   模型会写 `A1["…使用人<br/>提交巡检任务"]`，那是它从 HTML 思维带过来的）。
    #   mermaid 不认它，会渲染成一串字面量或者直接报错，所以清成空格。
    out = re.sub(r"<br\s*/?>", " ", out)

    # ① 先修简单标签：`A[提交申请(含附件)]` → `A["提交申请含附件"]`
    out = _LABEL_RISKY.sub(
        lambda m: '["' + re.sub(r"[\(\)\{\}\[\];]", "", m.group(1)).strip() + '"]',
        out,
    )

    # ①.5 去掉节点/子图定义**后面**多余的标点。
    #
    # ★ 这是实测最坑的一处（2026-10-12）：模型会写成
    #     `subgraph SG1["申请与审批"].`
    #   多一个句点。用**真的 mermaid** 验证过：那一行直接 Parse error on line 2。
    #   而它逃过了所有结构检查（有 flowchart 头、有连线、节点数够）——
    #   于是界面上是一块红字报错，用户以为功能坏了。
    #   结构检查挡不住这种"语法细节错"，所以这里按形态直接清掉。
    out = _TRAILING_JUNK.sub(lambda m: m.group(1), out)

    # ② 再把中文节点 ID 换成 ASCII 别名。
    #
    # ★ 两趟处理，而不是一路 global 替换（2026-10-12 实测）：
    # 节点第一次出现带定义（`审批{...}`），**之后再出现就只有名字**
    # （`审批 -->|"通过"| 结算[...]`）。只改"名字后面跟着括号"的那种，
    # 第二处引用就漏了 —— 图上会同时存在 `审批` 和它的别名，mermaid 把它们
    # 当成两个不同的框，画出两个"审批"节点。
    #
    # 所以：第一趟按位置**收集**"哪些名字是节点"，第二趟把所有出现都替换掉。
    # 先收集不替换，是因为替换会改变字符串，让后续的位置判断失准。
    mapping: dict[str, str] = {}
    counter = [0]

    def _alias(name: str) -> str:
        if name not in mapping:
            counter[0] += 1
            mapping[name] = f"n{counter[0]}"
        return mapping[name]

    # 第一趟：识别（只登记，不改动）
    for m in _BAD_ID_AT_START.finditer(out):
        _alias(m.group(2))
    for m in _BAD_ID_AFTER_EDGE.finditer(out):
        _alias(m.group(4))
    for m in _BAD_SUBGRAPH_ID.finditer(out):
        _alias(m.group(2))

    # 第二趟：按位置替换，保留连接符与标签
    out = _BAD_ID_AT_START.sub(
        lambda m: f"{m.group(1)}{mapping.get(m.group(2), m.group(2))}{m.group(3)}", out
    )
    out = _BAD_ID_AFTER_EDGE.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2) or ''}{m.group(3)}"
            f"{mapping.get(m.group(4), m.group(4))}{m.group(5)}"
        ),
        out,
    )
    out = _BAD_SUBGRAPH_ID.sub(
        lambda m: f"{m.group(1)}{mapping.get(m.group(2), m.group(2))}{m.group(3)}", out
    )
    # 第三趟：把仍然**裸露**出现的中文节点名（同一节点的后续引用）也换掉。
    # 用词边界防止误伤标签文字：只换"独立成词"的出现。
    for cn, alias in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(
            r"(?<![\w\u4e00-\u9fff\"])" + re.escape(cn) + r"(?![\w\u4e00-\u9fff\"])",
            alias,
            out,
        )
    return out.strip()


def mermaid_balance_ok(src: str) -> tuple[bool, str]:
    """引号与括号是否配平（**忽略引号内的字符**）。纯函数。

    ## 为什么不能只数引号（2026-10-12 实测）

    抢救出来的那段源码长这样：

        A1["甲人 提交"] --> B1{"乙人 审批"

    引号是 4 个（偶数，看着没问题），但 `{` 没有对应的 `}` ——
    因为**收尾的 `"}` 一起被截掉了**。只数引号会放过它，mermaid 却报
    `Expecting 'SQE', 'DOUBLECIRCLEEND', 'PE' …`。

    所以判据要升级成"配对"：先跳过引号内的内容，再检查 `{`/`[` 有没有配对。
    这比数总数准，而且不会因为标签里合法地含有一个引号就误判。
    """
    depth_curly = depth_square = 0
    in_quote = False
    i = 0
    while i < len(src):
        ch = src[i]
        if in_quote:
            if ch == "\\" and i + 1 < len(src):
                i += 2
                continue
            if ch == '"':
                in_quote = False
            i += 1
            continue
        if ch == '"':
            in_quote = True
        elif ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
        if depth_curly < 0 or depth_square < 0:
            return False, "括号多余闭合"
        i += 1
    if in_quote:
        return False, "标签引号没有收尾"
    if depth_curly != 0:
        return False, f"花括号没配平（差 {depth_curly} 个}}）"
    if depth_square != 0:
        return False, f"方括号没配平（差 {depth_square} 个]）"
    return True, ""


def mermaid_looks_renderable(src: str) -> tuple[bool, str]:
    """这张图值不值得存？返回 (是否可以, 原因)。

    ★ 刻意的取舍：**不能确定能渲染时就不存**。存一张渲染必炸的源码，
    用户看到的是页面上的一块红字报错 —— 那比"这一节没画出来"更让人以为功能坏了。
    """
    s = (src or "").strip()
    if not s:
        return False, "模型没有给出流程图源码"
    if not _FLOW_HEAD.search(s):
        return False, "缺少 flowchart 声明（第一行应当是 flowchart TD 或 flowchart LR）"
    # 至少要有两个节点和一条连线，否则"图"没有意义
    if "-->" not in s and "---" not in s:
        return False, "没有任何连线（-->）"
    # 数**唯一节点 ID**，而不是数行。
    #
    # ★ 为什么不能数行（2026-10-12）：`flowchart TD\n A[…] --> B[…]` 只有 2 行，
    # 数行算出来是 1 个"节点行"，于是被判成"节点太少" —— 而它是一张完全正常的图。
    # 判据要对着"图里有多少个框"，不是"文本有多少行"。
    ids = set(re.findall(r"(?<![\w\"])([A-Za-z_][\w]*)\s*[\[\{\(]", s))
    if len(ids) < 2:
        return False, f"节点太少（只有 {len(ids)} 个框）"
    if len(ids) > 40:
        return False, f"节点太多（{len(ids)} 个框），一张图看不清"
    # 引号/括号必须配平（**忽略引号内的字符**）。
    # 只数引号不够 —— 见 mermaid_balance_ok 的注释：截断时会把收尾的 `"}` 一起切掉，
    # 引号数看着是偶数，而 `{` 其实没配上。
    bal_ok, bal_why = mermaid_balance_ok(s)
    if not bal_ok:
        return False, bal_why
    # 中文 ID 残留 = 规范化没覆盖到，交给 mermaid 很可能炸。
    # 用"行首/连接符旁的中文字后跟括号"这个判据，和规范化用的是同一套位置规则。
    if _BAD_ID_AT_START.search(s) or _BAD_ID_AFTER_EDGE.search(s) or _BAD_SUBGRAPH_ID.search(s):
        return False, "仍有中文节点 ID（mermaid 渲染不稳定）"
    return True, ""


# 出处单独一次调用。
#
# ★ 为什么把 sources 从正文那次调用里拆出来（2026-10-12 实测）：
#   原来一次要求 `{"html": ..., "sources": [...]}`，而模型会**逐字抄写**几十个
#   章节标题 —— 实测一次能抄 60 个，光 sources 就吃掉全部预算，
#   于是 JSON 在 1419 字符处被硬截断、连 html 都拿不到。
#   拆开之后正文那次只写正文（输出小、几乎不会截断），
#   出处这次只需要"从清单里挑编号"（输出是纯数字，便宜且极短）。
_SOURCES_SYSTEM = """你是需求分析师。用户给你一份章节标题清单和一个主题。
挑出**最能支撑该主题**的标题，逐字照抄，最多 25 个。

只输出 JSON：{"idx": [清单里的编号, ...]}
依据不足时就少挑几个，不要凑数。不要输出标题全文，只输出编号。"""


def parse_section(key: str, data: object) -> OverviewSection:
    """模型对这一节的输出 → OverviewSection。容错，绝不抛。

    正文来源按节不同：
      * `flow` 这一节要的是 **Mermaid 源码**（用户要"画流程图"），
        存进 `mermaid` 字段；前端渲染成图，并且**可编辑**。
      * 其余节是 HTML。
    """
    title = dict((k, t) for k, t, _d in _SECTION_SPECS).get(key, key)
    if not isinstance(data, dict):
        return OverviewSection(key=key, title=title)
    if key == "flow":
        raw = str(data.get("mermaid") or "").strip()
        mermaid = normalize_mermaid(raw)
        note = str(data.get("note") or "").strip()
        ok, why = mermaid_looks_renderable(mermaid)
        if not ok:
            # 画不出来时退化成一句说明 + 原因，而不是留一片空白：
            # 用户需要知道"是文档里没有流程"还是"AI 没画出来"。
            log.warning("spec_overview: 流程图不可用（%s）", why)
            body = f"<p><em>{_esc(note or ('这一节要画流程图，但' + why))}</em></p>"
            return OverviewSection(key=key, title=title, html=body, mermaid="", mermaid_error=why)
        if note:
            return OverviewSection(key=key, title=title, html=f"<p>{_esc(note)}</p>", mermaid=mermaid)
        return OverviewSection(key=key, title=title, mermaid=mermaid)
    html = str(data.get("html") or "").strip()
    return OverviewSection(key=key, title=title, html=html)


def parse_sources(data: object, headings: list[str]) -> list[str]:
    """把"编号列表"还原成标题。越界的编号直接丢掉。

    为什么校验编号范围：模型会凭空给出一个不存在的编号（比如清单只有 240 个它给 300）。
    照抄下来，"依据章节"里就会出现一条文档里根本没有的标题 ——
    那比不给依据更糟，因为它看起来像是核对过了。
    """
    if not isinstance(data, dict):
        return []
    idx = data.get("idx")
    if not isinstance(idx, list):
        return []
    hs = [h.strip() for h in headings if h and h.strip()][:_MAX_HEADINGS]
    out: list[str] = []
    for x in idx[:25]:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(hs):
            h = hs[i - 1]
            if h not in out:
                out.append(h)
    return out


# 保留旧名字：bundle 生成（一次要六节）已被证明会截断，但提示词构造能力还有用，
# 供测试与将来可能的"一次多节"尝试复用。
def build_plan_prompt(headings: list[str], intro: str, extra: str = "") -> str:
    """把所有节的骨架喂进去（供测试用；实际生成走 `build_section_prompt`）。"""
    hs = [h.strip() for h in headings if h and h.strip()]
    used = hs[:_MAX_HEADINGS]
    listing = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(used))
    more = ""
    if len(listing) > _MAX_LISTING_CHARS:
        # 按行截，别把最后一条标题截成半截。
        listing = listing[:_MAX_LISTING_CHARS].rsplit("\n", 1)[0]
        more = f"\n（标题清单过长，这里只列了开头一部分，共 {len(hs)} 个标题。）"
    elif len(hs) > len(used):
        more = f"\n（清单过长，这里只列了前 {len(used)} 个，共 {len(hs)} 个标题。）"
    parts = [
        f"## 文档开头（判断这是什么系统）\n{(intro or '').strip()[:_MAX_INTRO_CHARS]}\n",
        f"## 章节标题清单（共 {len(hs)} 个）\n{listing}{more}\n",
    ]
    if extra.strip():
        parts.append(f"## 部分章节正文\n{extra.strip()[:_SECTION_CHARS]}\n")
    parts.append("请分别输出各节内容。")
    return "\n".join(parts)


def sections_from_payload(data: dict) -> list[OverviewSection]:
    """模型输出 → 分节列表。纯函数，可单测。

    模型很爱漏字段或给名字变体，所以这里对每一节都做存在性判断，
    而不是假设它老老实实按结构返回 —— 一个 KeyError 会让整份概览拿不到。
    """
    specs = [
        ("overview", "项目总览"),
        ("modules", "功能模块"),
        ("flow", "主要流程"),
        ("roles", "角色与权限"),
        ("rules", "关键业务规则"),
        ("glossary", "名词表"),
    ]
    srcs = data.get("sources")
    if not isinstance(srcs, dict):
        srcs = {}
    out: list[OverviewSection] = []
    for key, title in specs:
        html = str(data.get(key) or "").strip()
        s = srcs.get(key)
        # 只收非空字符串。写成 `str(x) for x in s` 会把 `None` 变成字面量 `"None"`
        # 显示在"依据章节"列表里 —— 用户会看到一行莫名其妙的 None，然后开始怀疑
        # 整份 AI 输出的可信度。
        sources = [x.strip() for x in s if isinstance(x, str) and x.strip()] if isinstance(s, list) else []
        if not html and not sources:
            continue  # 完全空的一节不显示 —— 一个标题配一片空白比没有更让人困惑
        out.append(OverviewSection(key=key, title=title, html=html, sources=sources))
    return out


# 供后端渲染成一份**可直接编辑**的富文本。刻意用同样的标签集（sanitizer 白名单），
# 这样"AI 生成的"和"人编辑的"走同一条渲染路径，不会出现"生成的好看、编辑的难看"。
def sections_to_html(sections: list[OverviewSection], *, with_sources: bool = True) -> str:
    """分节列表 → 一份完整的富文本 HTML。

    `with_sources=True` 时给每节附上"依据"，这是用户核对 AI 的唯一手段，
    所以默认开着 —— 关掉它是排障用的，不是常态。

    ★ 图表存的是**源码文本**，不是图片（2026-10-12 用户要"画流程图"）。
    流程这一节渲染成 `<pre class="tp-mermaid">flowchart TD …</pre>`：
      * 前端把这段源码交给 mermaid 渲染成图；
      * 同时它是一段**可编辑的文本** —— 用户能在编辑器里改图，
        而存成 PNG 就再也改不动了。
    这是"画图"和"可编辑"两个要求唯一能同时满足的形态。

    ★ 标签为什么写"章节或条目"而不是"章节"：
    需求文档里的标题清单本身是混的 —— `specgen.list_headings` 返回 586 条，
    其中 63 条是**角色名**（`…审批人`/`…使用人`）这类独立成行的条目。
    写"依据章节"会让用户以为引用的是章节，去目录里找一个不存在的章节。
    """
    parts: list[str] = []
    for s in sections:
        parts.append(f"<h2>{_esc(s.title)}</h2>")
        if s.key == "flow" and s.mermaid:
            # 源码放 <pre>：既是给前端渲染的输入，也是给用户改的文本。
            parts.append(f'<pre class="tp-mermaid">{_esc(s.mermaid)}</pre>')
            if s.html:
                parts.append(s.html)
        else:
            parts.append(s.html or "<p><em>（这一节 AI 没有找到可写的内容）</em></p>")
        if with_sources and s.sources:
            items = "".join(f"<li>{_esc(x)}</li>" for x in s.sources[:40])
            parts.append(
                "<blockquote><p><strong>依据（原文里的章节或条目）</strong></p><ul>"
                + items
                + "</ul><p><em>这些名称都在下面这段正文里逐字出现过。"
                "没有列出的部分属于 AI 的综合描述，请你自行核对。</em></p></blockquote>"
            )
        if with_sources:
            prov = _provenance_block(s)
            if prov:
                parts.append(prov)
    return "\n".join(parts)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---- 逐句溯源（2026-10-13）-------------------------------------------------
#
# 用户要求「确保 AI 依据需规改文档、且可验证」。做法：把本节正文拆成句子，
# 逐句和**喂给模型的原文片段**比字符二元组(bigram)重合度 —— 重合度高 = 这句话的
# 措辞能在原文里找到 → 有据；低 = 属于 AI 的概括 / 推断，请人工核对。
#
# ★ 这是**启发式**，不是逐句证明：模型会改写措辞，改写幅度大就会被判"无据"
#   （偏保守，宁多报勿漏报）。所以文案如实写"按措辞重合度判断"，绝不写"已证实"。
_MIN_SENT_CHARS = 12
_GROUND_RATIO = 0.5


def _sentences(html: str) -> list[str]:
    text = re.sub(r"<[^>]+>", " ", html or "")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(a, b)
    return [
        c.strip()
        for c in re.split(r"[。！？!?\n；;]+", text)
        if len(c.strip()) >= _MIN_SENT_CHARS
    ]


def _bigrams(s: str) -> set[str]:
    t = re.sub(r"\s+", "", s or "")
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _provenance_block(s: "OverviewSection") -> str:
    """给一节附上「逐句溯源」：多少句在原文里找得到依据、哪些找不到。"""
    sents = _sentences(s.html)
    src = _bigrams(s.recalled)
    if not sents or not src:
        return ""
    supported = 0
    weak: list[str] = []
    for sent in sents:
        g = _bigrams(sent)
        if not g:
            continue
        if sum(1 for x in g if x in src) / len(g) >= _GROUND_RATIO:
            supported += 1
        else:
            weak.append(sent)
    out = [
        f"<p><strong>溯源（{supported}/{len(sents)} 句在需规原文里找得到依据）</strong></p>",
        "<p><em>按措辞重合度的启发式判断，不是逐句证明 —— 改写幅度大就会被判为无据，请以原文为准。</em></p>",
    ]
    if weak:
        items = "".join(f"<li>{_esc(x[:120])}</li>" for x in weak[:8])
        out.append(
            f"<p>以下 {len(weak)} 句未在原文里找到对应措辞（AI 的概括或推断，请核对）：</p><ul>{items}</ul>"
        )
    return "<blockquote>" + "".join(out) + "</blockquote>"


# 出处匹配的判据（见 match_sources 的注释）。
#
# 太短的标题不收：两三个字的中文标题（"概述"、"目录"）在正文里碰巧出现太容易了，
# 收进来只会制造噪音。5 个字是个折中 —— 实测 `干系人分析`（5 字）是真章节，
# 而 `教学设备管理`（6 字）也算；3~4 字的短标题误报率明显升高。
_MIN_SOURCE_CHARS = 5

# 中文没有词边界，所以纯中文标题必须靠"结构性证据"才算引用：
# 被下面这些成对符号包着，或独占一个段落/列表项/表格单元格。
# （试过用"两边不能紧挨汉字"来近似词边界 —— 不够：`场地预占审批人` 出现在
#  "（场地预占审批人）"里时两边是括号，照样算"独立出现"，而它只是顺带提到。）
_QUOTE_PAIRS = (("【", "】"), ("「", "」"), ("《", "》"), ("“", "”"), ("‘", "’"))


def trim_unbalanced_tail(src: str) -> str:
    """从末尾逐行去掉**没写完**的部分，直到引号/括号配平。纯函数。

    ## 为什么需要它（2026-10-12 实测）

    模型经常把图画到一半就被 `max_tokens` 截断，最后一行是个残句：

        A1["甲人"] --> B1{"乙人 审批        ← 引号与 } 一起没了

    这时整段源码是**不可用**的（括号没配平），但前面几行完全没问题 ——
    直接丢掉等于把画好的大半个流程扔了。逐行往回退，直到配平，
    用户至少能看到主体流程（而不是"这一节没画出来"）。

    只从**末尾**退：图是从上往下画的，缺的必然在最后。

    ★ 退到只剩声明行时返回**空串**，而不是那一行 `flowchart TD`：
    只有一个表头的"图"没有任何意义，而 `mermaid_looks_renderable` 也会
    因为它没有连线而判不可用 —— 不如在这里就说清楚"没抢救出来"。
    """
    lines = [ln for ln in (src or "").split("\n") if ln.strip()]
    # ★ 先去掉**悬空连线**（2026-10-12 实测）。
    # 截断常常落在一条连线的中间：`L1 --> L` —— 括号是配平的，所以
    # `mermaid_balance_ok` 说没问题，而 mermaid 报
    # `Parse error on line 30 … got '1'`。这类"看着配平、其实断在半句"的残句
    # 只能按形态认。
    #
    # ★ 判据要**同时**满足两条（否则会误伤合法写法）：
    #   1. 连线后面只剩 1 个字符 —— 截断总会留下一个半个的 ID；
    #   2. 那个字符紧跟在一个 ID 后面（`…L1 --> L` 里的 `L` 紧贴 `L1`）。
    # 合法的 `A["甲"] --> B` 是"独立的 1 个 ID 字符"，不满足第 2 条，所以不会被误删。
    # 我第一版只判第 1 条，结果把这种合法写法整行删掉了。
    lines = [
        ln
        for ln in lines
        if not re.search(r"(-->|---|--\|)\s*[\w\u4e00-\u9fff]{0,1}[A-Za-z0-9_]$", ln.rstrip())
    ]
    while lines:
        cand = "\n".join(lines)
        ok, _why = mermaid_balance_ok(cand)
        if ok:
            # 只剩声明行 = 没有任何节点，等同于没抢救出来
            return cand if len(lines) > 1 else ""
        lines.pop()
    return ""


def _salvage_mermaid_from_text(text: str) -> str:
    """从**残缺的**模型输出里抢救出 Mermaid 源码。纯函数。

    ## 为什么需要这个

    实测（2026-10-12）：模型把图画得挺好，但整段 JSON 因为被截断而解析不出来：

        {"mermaid": "flowchart TD\\n  A1[\\"…使用人<br/>提交巡检任务\\"] --> B1{\\"…

    就断在半路。这时"丢掉重试"是浪费 —— 图其实已经在那儿了，只是 JSON 没收尾
    （而且重试还可能拿到同样长度受限的输出）。

    做法：抠出 `"mermaid"` 后面引号里的内容，按 JSON 字符串规则反转义。
    抠不出完整字符串时（连收尾引号都断了）就取到文本末尾 —— 反正后面
    `normalize_mermaid` + `mermaid_looks_renderable` 会判断它够不够用。
    """
    if not text:
        return ""
    m = re.search(r'"mermaid"\s*:\s*"', text)
    if not m:
        return ""
    rest = text[m.end() :]
    buf: list[str] = []
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest):
            nxt = rest[i + 1]
            buf.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "", "/": "/"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            break  # 字符串正常收尾
        buf.append(ch)
        i += 1
    return "".join(buf).strip()


def match_sources(html: str, headings: list[str], *, limit: int = 25) -> list[str]:
    """从标题清单里挑出这一节真正引用了的章节 —— **纯代码，不调模型**。

    ## 为什么不用模型挑（2026-10-12 改）

    原来这里是"再发一次 LLM 调用，让它从编号清单里挑"。三个问题实测都撞上了：

    1. **要花钱、会失败。** 它是每节一次的额外调用；网关一抖，出处就整批为空
       （实测：正文六节都写出来了，出处六节全空）。而"出处"恰恰是用户核对 AI 的
       唯一手段 —— 它不该是最脆的一环。
    2. **没必要。** 模型写正文时会**顺手把章节标题抄进去**（实测正文里就带着
       「干系人分析」「教学设备管理-基础信息维护」这类词）。也就是说引用了哪些章节，
       是**可以从正文里看出来**的事实，不需要再问一次。
    3. **顺序反了。** 先写正文、再问"你引用了什么"，等于让模型回忆自己刚说的话；
       而"在正文里找出清单里的标题"是字符串匹配，确定性、免费、不会失败。

    ## ★ 判据为什么这么严（2026-10-12 第二次修，用户反馈「没正常生成」）

    第一版只做"去掉空白后包含"，结果在真数据上**几乎全错**。原因是两头都不干净：

    1. **正文里到处是这些名字。** `场地预占审批人` 出现在"（场地预占审批人）"里，
       它只是**顺带提到**，不是"这一节引用了那一章"。
    2. **标题清单本身是混的。** `specgen.list_headings` 返回 586 条，其中 **63 条是
       角色名**（`…审批人` / `…使用人` / `…经理`）—— 它们在源文档里确实是独立成行的
       条目，所以被当成标题收了进来。这不是 bug，是文档本身的形状。

    所以"在正文里找到标题的字"完全不足以证明引用关系 —— 那样每一节都会匹配满
    25 条，用户看到一串又长又没用的清单，而它本意是"你可以核对这几节"。

    现在的判据只认**结构性证据**：
      * 标题被 `【】「」《》""` 包着出现（文档里的引用就是这么写的）；
      * 标题带 `-`/`_`/`:` 分隔符（`RULE-TRAINRES-116`）—— 这种形态很特异，不会偶然撞上；
      * 标题**独占**一个段落/列表项/表格单元格（去掉标记后完全相等）。

    刻意保守：宁可少标一条出处，也不要标一条其实没引用的 —— 后者会让用户
    以为核对过了，比不标更糟。实测同一份文档：项目总览从 10 条降到 0 条
    （它确实是 AI 综合写出的，没有逐字引用哪一章），而功能模块仍能标出真正的
    条目引用（`【教学设备采购记录】`）。
    """
    hs = [h.strip() for h in headings if h and h.strip()]
    if not hs or not html:
        return []
    text = re.sub(r"<[^>]+>", " ", html)
    flat = text.replace(" ", "")
    # 独占一个块（段落/li/td）的文本集合 —— 这是"结构性引用"的证据。
    blocks = {
        re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", b)).strip()
        for b in re.findall(r"<(?:p|li|td|th|h1|h2|h3|h4)[^>]*>(.*?)</(?:p|li|td|th|h1|h2|h3|h4)>", html, re.S)
    }
    blocks.discard("")

    out: list[str] = []
    # 长标题优先：长标题命中基本可以确定是真引用，短标题容易碰巧出现。
    for h in sorted(hs, key=len, reverse=True):
        core = h.replace(" ", "")
        if len(core) < _MIN_SOURCE_CHARS:
            continue
        if any(c in core for c in "-_:"):
            hit = core in flat
        else:
            hit = any(f"{a}{core}{b}" in flat for a, b in _QUOTE_PAIRS) or core in blocks
        if hit:
            out.append(h)
        if len(out) >= limit:
            break
    # 恢复成清单里的原始顺序，读起来更像"章节顺序"而不是"按长度排的"
    order = {h: i for i, h in enumerate(hs)}
    out.sort(key=lambda x: order.get(x, 0))
    return out


async def sources_for(
    key: str, html: str, headings: list[str], gate, usage: dict
) -> list[str]:
    """给这一节挑出处。

    保留 async 签名（调用点在 `asyncio.gather` 的协程里），但内部是纯匹配 ——
    见 `match_sources` 的注释：出处不该依赖一次会失败的模型调用。
    """
    return match_sources(html, headings)


async def _recall_text(
    project_id: int, key: str, *, limit: int = 6, cap: int = _SECTION_CHARS
) -> str:
    """从知识库检索与本节相关的**原文片段**，拼成给模型的"依据原文"（2026-10-13）。

    为什么按节检索而不是一次性取固定章节：六节关心的是文档的不同侧面
    （流程看状态流转、角色看权限、规则看校验），按主题检索才取得到贴题的原文。
    检索失败只是"少给原文"，**绝不该让生成失败** —— 所以整体 try 住。
    """
    from app import knowledge

    q = _SECTION_QUERY.get(key, "")
    if not q:
        return ""
    try:
        res = await knowledge.search(project_id, q, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.debug("spec_overview: 检索『%s』原文失败：%s", key, exc)
        return ""
    parts: list[str] = []
    used = 0
    for h in res.get("hits") or []:
        head = str(h.get("heading") or "").strip()
        body = str(h.get("text") or "").strip()
        block = f"### {head}\n{body}" if head else body
        if used + len(block) > cap:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


async def _full_read_notes(
    project_id: int, usage: dict, keys: tuple[str, ...] | None = None
) -> dict[str, list[str]]:
    """把整份需规**分段通读**一遍，抽成"按节归类的事实条目"（2026-10-13）。

    为什么分段：网关单次 user 提示词 ~4.5~5k 字符就返回空，整份需规塞不进一次调用。
    所以按 `_FULL_CHUNK_CHARS` 切段、逐一抽事实 —— **每段一次调用**（慢、贵，用户开关决定）。
    单段失败只少一条笔记，不拖垮整次生成。
    """
    from sqlalchemy import select

    from app import knowledge, specgen
    from app.db import db_session
    from app.models import KnowledgeChunk

    async with db_session() as s:
        rows = (
            await s.execute(
                select(KnowledgeChunk.text)
                .where(await knowledge.effective_condition(s, project_id))
                .order_by(KnowledgeChunk.chunk_index)
            )
        ).scalars().all()
    text = "\n\n".join(rows)
    if not text.strip():
        return {}

    # 按段落聚合到 _FULL_CHUNK_CHARS 一段（不切碎段落）。
    segs: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > _FULL_CHUNK_CHARS:
            segs.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        segs.append(buf)
    segs = segs[:_FULL_MAX_CHUNKS]

    keys = list(keys) if keys is not None else [k for k, _t, _d in _SECTION_SPECS]
    notes: dict[str, list[str]] = {k: [] for k in keys}
    for seg in segs:
        user = (
            f"## 需规原文片段\n{seg}\n\n"
            '只输出 JSON：{"overview":["…"],"modules":["…"],"flow":["…"],'
            '"roles":["…"],"rules":["…"],"glossary":["…"]}；'
            "某一类原文没写就给空数组；每条尽量短（一句话）。"
        )
        try:
            data, u = await specgen._call_json(_FULL_SYSTEM, user, max_tokens=1200, role="docgen")
        except Exception as exc:  # noqa: BLE001 — 单段失败只少一条笔记
            log.debug("spec_overview: 通读分段失败：%s", exc)
            continue
        for k in ("prompt", "completion", "cached", "total", "calls"):
            usage[k] += int((u or {}).get(k, 0) or 0)
        if not isinstance(data, dict):
            continue
        for k in keys:
            arr = data.get(k)
            if not isinstance(arr, list):
                continue
            for x in arr:
                t = str(x).strip()
                if t and t not in notes[k] and len(notes[k]) < _FULL_NOTE_CAP:
                    notes[k].append(t)
    return notes


async def analyze(
    project_id: int,
    *,
    full: bool = False,
    keys: tuple[str, ...] | None = None,
) -> OverviewResult:
    """读项目知识库 → 产出一份项目概况。**绝不抛异常。**

    `full=True` 走「通读全文」模式（2026-10-13 用户开关）：先把整份需规分段读一遍抽成
    要点，再按要点写各节 —— 慢、贵，但真读到全文。默认 False（检索片段，快）。

    默认生成全部六节，失败的那一节记下来但**不影响其它节**。
    `keys` 可以只生成某几节（2026-10-20）：生成「流程图」时只要 `flow`，
    不必再花五次调用陪跑 —— 调用越少，撞上网关空回复/截断的机会越少，
    也更快。生成失败大多发生在"根本用不到的节"上。
    （原来一次要六节，实测被硬截断，结果是零节。见 `_SECTION_SPECS` 的注释。）
    """
    import asyncio

    from app import specgen

    all_keys = tuple(k for k, _t, _d in _SECTION_SPECS)
    wanted = tuple(k for k in (keys or all_keys) if k in all_keys)
    if not wanted:
        return OverviewResult(error="没有可生成的章节类型。")

    try:
        headings = await specgen.list_headings(project_id)
        intro = await specgen.document_intro(project_id, _MAX_INTRO_CHARS)
    except Exception as exc:  # noqa: BLE001
        log.warning("spec_overview: 读知识库失败：%s", exc)
        return OverviewResult(error=f"读知识库失败：{type(exc).__name__}: {exc}"[:300])

    if not headings:
        return OverviewResult(
            error="这个项目的『需规/资料』还是空的，没有可分析的文档。先上传需规再生成。"
        )

    # 每节的"依据原文"：检索模式在 `one()` 里按主题单独检索（见 `_recall_text`）；
    # 通读模式先在这里把整份需规读一遍、抽成要点。
    usage = {"prompt": 0, "completion": 0, "cached": 0, "total": 0, "calls": 0}
    full_notes: dict[str, list[str]] = {}
    if full:
        full_notes = await _full_read_notes(project_id, usage, wanted)
    failures: list[str] = []
    # ★ 并发上限 1（2026-10-12 第三次调，见下面的实测）。
    #
    # 调优过程（每一步都有实测）：
    #   * 并发 6 全发 → `flow` 这一节**每次都失败**（网关返回空回复），
    #     而同一段提示词**单独跑就成功**（实测能画出 10 个节点的可用图）。
    #   * 并发 3 → 好一些，但 flow 仍然时好时坏。
    #   结论：这条路的问题不是提示词，是**同时压太多请求**。
    #
    # 代价是慢：六节串行约 1~2 分钟。但"六节里必坏一节"的代价更大 ——
    # 用户点"生成流程图"却永远拿不到图，只看得到"这一节没画出来"。
    gate = asyncio.Semaphore(1)

    async def one(key: str) -> OverviewSection:
        # ★ 先备好"依据原文"，再让它写（2026-10-13）。
        #   检索模式：按本节主题取相关片段；通读模式：用通读得到的该节要点。
        if full:
            recalled = "\n".join(f"- {x}" for x in full_notes.get(key, []))[:_SECTION_CHARS]
            label = _SOURCE_LABEL_FULL
        else:
            recalled = await _recall_text(project_id, key)
            label = _SOURCE_LABEL_RETRIEVED
        user = build_section_prompt(key, headings, intro, recalled, label)
        title = dict((k, t) for k, t, _d in _SECTION_SPECS).get(key, key)
        # ★ 判断"这一节有没有产出"要按节看字段（2026-10-12 实测踩到）：
        # `flow` 这一节的内容在 `mermaid` 里，不在 `html` 里。原来统一判 `sec.html`，
        # 于是**每次成功的流程图都被当成"空回复"重试两次后丢掉** ——
        # 用户点"生成流程图"拿到的是一份没有图的文档，而日志里只有"生成失败"。
        # 这类 bug 特别隐蔽：功能看着"没报错"，只是永远出不来图。
        def _produced(s: OverviewSection) -> bool:
            return bool(s.mermaid) if key == "flow" else bool(s.html)

        sec = OverviewSection(key=key, title=title)
        # 流程图多给一次机会：它是**唯一**一个"失败就等于整节没内容"的节
        # （其它节失败还有别的节撑着），而且实测它最容易撞上网关抖动。
        attempts = (1, 2, 3) if key == "flow" else (1, 2)
        for attempt in attempts:
            try:
                async with gate:
                    data, u = await specgen._call_json(
                        _PLAN_SYSTEM, user, max_tokens=4000, role="docgen"
                    )
                for k in ("prompt", "completion", "cached", "total", "calls"):
                    usage[k] += int((u or {}).get(k, 0) or 0)
                sec = parse_section(key, data)
                if _produced(sec):
                    break
                raise ValueError("空回复")
            except Exception as exc:  # noqa: BLE001 — 单节失败不该拖垮整份概览
                # ★ 流程图这一节：解析失败时**从原始文本里抢救**（2026-10-12）。
                #
                # 实测：模型会把图写好了，但整段 JSON 因为被截断而解析不出来
                # （`{"mermaid": "flowchart TD\n A1[\"…\"] --> B1{\"…` 就断在半路）。
                # 那种情况下"丢掉重试"是浪费 —— 图其实已经在那儿了，
                # 只是一个 JSON 收尾没写完。所以直接从原文里抠出 mermaid 字段的值。
                if key == "flow" and attempt == 1:
                    salvaged = _salvage_mermaid_from_text(getattr(exc, "raw_text", "") or "")
                    if salvaged:
                        log.info("spec_overview: 流程图解析失败但成功抢救出源码")
                        sec = OverviewSection(
                            key=key,
                            title=title,
                            # 抢救出的多半是截断的，所以要去掉没写完的尾巴，
                            # 让前面画好的部分能用（见 trim_unbalanced_tail）。
                            mermaid=trim_unbalanced_tail(normalize_mermaid(salvaged)),
                        )
                        if _produced(sec):
                            break
                if attempt == 2:
                    log.warning("spec_overview: 生成『%s』失败（已重试）：%s", key, exc)
                    failures.append(f"{key}: {type(exc).__name__}")
                else:
                    log.debug("spec_overview: 『%s』第 %d 次失败，重试：%s", key, attempt, exc)
                    await asyncio.sleep(1.5)
        # 出处：只在正文写出来之后才去要，而且要不到也无所谓（正文本身还有价值）。
        # 流程图没有正文可摘，所以跳过（流程图的出处就是它自己那张图）。
        if sec.html:
            sec.sources = await sources_for(key, sec.html, headings, gate, usage)
        # 记下本节依据的原文，供 sections_to_html 做「逐句溯源」
        sec.recalled = recalled
        return sec

    # 并发发出去。各节彼此独立、都要跑模型 —— 串行会让用户等成倍时间。
    produced = list(await asyncio.gather(*(one(k) for k in wanted)))
    # ★ 空节不能算"生成成功"（2026-10-20）。原来只要 gather 返回了对象，
    # `if not sections` 永远为假 —— 六节全失败也返回 ok=True + 空文档。
    # 必须按节判断它到底产出了正文（flow 看 mermaid，其余看 html）。
    sections = [x for x in produced if (x.mermaid if x.key == "flow" else x.html)]

    if not sections:
        detail = ("；".join(failures))[:300] if failures else "模型没有给出任何内容"
        return OverviewResult(
            error=f"生成失败（{detail}）。可以重试一次。",
            usage=usage,
            headings_used=min(len(headings), _MAX_HEADINGS),
            doc_chars=len(intro),
        )
    return OverviewResult(
        sections=sections,
        usage=usage,
        error=("部分章节生成失败：" + "；".join(failures)) if failures else "",
        headings_used=min(len(headings), _MAX_HEADINGS),
        doc_chars=len(intro),
        recalled_chars=sum(len(s.recalled or "") for s in sections),
    )
