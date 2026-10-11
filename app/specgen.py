"""从需规/项目文档生成测试用例（2026-10-09）。

## 这个模块解决什么

用户的原话是：「我发需规或者相关的系统文件项目文件给他，然后他能帮我生成测试用例，
然后并进行测试」。在此之前这些东西**散落在三个地方**：文档要手工粘进知识库
（`POST /knowledge/extract` 只返回纯文本、不落库、不生成），生成要靠助手里
`create_case` **一条一条**建（一次 LLM 往返一条，几十条用例就是几十次往返），
跑起来还得再单独去运行页点一次。没有任何一步是"对着这份文档"做的。

所以这里把那一串接成一条链，并且是**按章节分批**的 —— 理由不是好看，是 token：
一整份 46 万字的需规一次性塞进去，单次调用就会大到跑不完也不可控；
按章节走，每次调用的输入都是"这一节 + 少量全局背景"，有界。

## 三条质量防线（这个功能最容易产出的就是"看起来对"的垃圾用例）

一条 LLM 生成的用例，如果它**不可判定**，那它进了回归集就是长期噪音：
跑一次红一次，而每次都"看起来像是被测系统的问题"，没人能证明它错了。
提示词和解析两道一起守：

1. **只把明确要求写成断言。** 需规里大量出现"建议支持""可选""待确认""后续考虑"，
   这些**不是**可断言的需求。把它写成"应当支持 X"，就制造了一条永远失败、
   且无法反驳的用例。
2. **不可端到端验证的需求不许生成。** "响应时间 < 200ms""数据库主键唯一"
   这类要么观测不到、要么根本不在浏览器里。生成出来只是浪费一次运行。
3. **一条用例只断言一件事。** 一条里塞五个断言，失败时只知道"有问题"，
   而报告的价值就在"哪个字段错了"。

解析层再兜一道：prompt/expected 为空、或 expected 只是"符合预期"这种同义反复的，
直接丢弃并计数 —— 宁可少生成几条，也不要往审阅队列里灌一批必须逐条重写的东西。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from typing import Any

from app.config import get_settings

log = logging.getLogger("potato-test.specgen")

# Markdown 标题。与 app/knowledge.py::_HEADING_RE 保持一致的形状 ——
# 那边用它切分 chunk，这边用它从 chunk 正文里把标题扫回来（见 list_headings）。
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# 单节送进模型的正文上限。节的正文理论上可能很长（chunk 是 2000 字符切一段，
# 一节可能有几十段），而这里的取舍是"宁可少给一点正文，也不要让一次调用失去控制"：
# 截断处会明确告诉模型"下面还有内容被省略"，让它据此降低断言强度，而不是假装读全了。
MAX_SECTION_CHARS = 12_000

# 单次生成最多要几条 —— 防止模型对一节"自由发挥"出五十条同质用例。
MAX_CASES_PER_BATCH = 30

# 一次列出的候选章节上限。
#
# ★ 2026-10-09 真机实测纠正：这个上限**不能**用来"砍掉文档其余部分"。
# 实测那份《培训资源管理》合并需规有 **586 个标题**，而这里原来直接把
# `headings[:60]` 送进规划 —— 结果 526 个标题（90% 的文档）**被静默丢掉**，
# 用户看到"已规划 2 个模块"还以为文档就这么大。这是最糟的一类 bug：
# 功能看起来正常，产出少了一大截，而且没有任何提示。
#
# 正确做法是**分块规划**：把 586 个标题切成 10 批、每批 60 个，逐批调用。
# 分批按文档顺序切，所以相邻小节落在同一批里；每批的模块数上限也放宽，
# 因为一批 60 个标题本来就该被归成十几二十个模块，而不是两三个。
MAX_SECTIONS = 60

# 一批 60 个标题最多归并出多少个模块。真机实测：60 个标题被归成 2 个模块，
# 那说明模型把"个数"当成了需要压缩的目标 —— 但这里的目标是**可读的分批**，
# 不是越少越好。上限放宽后，"结构信息""联系人角色""设备维护"会各成一块。
MAX_MODULES_PER_BATCH = 18

# 标题清单总长上限（字符）。分块规划时用来判断"要不要提示用户还有多少没规划"。

TYPES = ("functional", "smoke", "regression", "acceptance", "negative")
PRIORITIES = ("P0", "P1", "P2", "P3")


_PLAN_SYSTEM = """你是资深测试架构师。用户会给你一份软件需求规格/项目文档的**一批章节标题**
（按原文顺序，每行是"编号<TAB>标题"）以及文档开头的一小段用于判断领域。

这一批只是整个文档的一部分（用户会逐批规划），所以你**只需处理给到的这些标题**。

你的任务：把这些原始标题**归并成若干"测试模块"**，供后续按模块逐个生成测试用例。

规则：
1. **用编号引用标题，不要抄写标题原文。** 每个模块给出 `idx` 数组，元素是标题前面的编号。
   这样不会因为抄错一个字而丢掉一条需求。
2. 一个模块 = 一组需要一起理解的相邻需求。层级标题（大章节下的子节）归到同一个模块；
   互不相关的业务域不要硬凑成一个。
3. **不要为了"少"而过度合并。** 这批有 60 个标题时，通常应该归出 8~18 个模块。
   把"结构信息/文档索引/联系人/版本记录"这类元信息和业务需求混在一个模块里，
   会让那一节既生成不出用例、又淹掉真正的需求。
4. `idx` 必须覆盖**每一个编号**，不重不漏。
5. 判断每个模块**是否适合端到端浏览器测试**：若某个模块通篇是性能指标、数据库约束、
   部署运维、编码规范、联系方式、文档索引、版本变更记录这类**在浏览器里观测不到**的内容，
   把 testable 设为 false。这类模块不要生成用例 —— 生成出来只会是永远失败且无法反驳的噪音。

只输出 JSON：
{"modules":[{"name":"模块名（简短中文）","idx":[1,2,3],"testable":true,
"reason":"一句话说明为什么这样归并"}]}"""


_GEN_SYSTEM = """你是资深测试工程师，负责把需求规格写成**可执行的端到端浏览器测试用例**。

## 输出什么

每条用例由三部分构成，缺一不可：
- `name`：简短中文标题（不超过 40 字），说明在测什么行为。
- `prompt`：给浏览器 Agent 的**操作指令**。必须写清"怎么走到那个界面、点什么、填什么"，
  用业务语言（菜单名、按钮文字、字段名），不要写选择器或坐标。
- `expected`：**可判定的预期结果**。必须是"打开列表后能在页面上看到什么/状态变成什么样"
  这种能被截图或页面文本证实的事实。

## 三条硬规则（违反其一，这条用例就是垃圾）

1. **只把明确要求写成断言。**
   需求里写着"建议""可选""可以""待确认""后续考虑""最好能"的，**都不是必测需求**。
   遇到这类表述，要么不生成，要么把断言写成"当前行为是什么"的**观察型**用例
   （例如"打开该弹窗，记录里面实际有哪些字段"），而不是"应当具备 X"的**断言型**用例。
   把建议写成断言，会制造一条永远失败、且无法反驳的用例。

2. **不可端到端验证的不要生成。**
   响应时间、并发数、数据库约束、日志内容、代码规范、部署配置 —— 这些在浏览器里
   观测不到。宁可少生成，也不要生成一条跑了也只能说"看不出问题"的用例。

3. **一条用例只断言一件事。**
   不要在一节里把"字段有 5 个 + 能保存 + 保存后列表刷新"塞进同一条。
   拆开之后，"哪一件事坏了"在报告里才看得出来。

## 还要遵守

- 测试数据不要写死具体值（除非需求里明确给了固定值）。写"输入一个不存在的名称"，
  而不是"输入 测试001"—— 后者会因为数据残留而第二次跑就失败。
- 不要依赖执行顺序，每条用例都要能单独跑。
- 覆盖**正常路径、边界、异常**三类，不要只写happy path；但一节里同类用例不要重复
  （三单击、五条必填校验各一条即可，不要每种输入组合都写一条）。
- 每条用例标注：`type`（functional/smoke/regression/acceptance/negative）、
  `priority`（P0 核心主流程 / P1 重要 / P2 一般 / P3 边缘）。
- `module` 填这一节所属的模块名（用给你的模块名）。

只输出 JSON：
{"cases":[{"name":"...","prompt":"...","expected":"...","type":"functional",
"priority":"P2","module":"...","precondition":"..."}]}

`precondition` 可省略或留空字符串。`cases` 为空数组是**合法**答案 ——
当这一节通篇都是不可测内容时，返回 {"cases":[]}，不要为了凑数编用例。"""


# 结构性/元信息标题：它们是文档的**骨架**，不是需求。
#
# ★ 为什么必须自己判一遍，而不是只信模型的 testable（2026-10-09 实测）：
# 模型把「需求说明书索引及联系人」（titles = 需求说明书 / 版本清单 / 联系人分配）
# 标成了 testable=true，然后对它生成用例时返回 {"cases": []} —— 因为那一节真的
# 没有任何可在浏览器里验证的内容。结果是用户看到"可测模块"，点进去得到 0 条，
# **而且没有任何解释**，只能认为功能坏了。
#
# 模型在任何单点上判断都可能是错的，所以这类"一眼可辨"的结构性标题自己拦一道。
_STRUCTURAL_TITLE = re.compile(
    r"(索引|目录|版本记录|版本说明|版本清单|修订|变更记录|更新记录|联系人|联系方式|"
    r"说明书|使用说明|文档说明|阅读说明|前言|术语表|缩略语|参考资料|"
    r"index|toc|changelog|revision history|contact|glossary)",
    re.I,
)


def looks_structural(titles: list[str]) -> bool:
    """这一批标题是不是**通篇**结构性/元信息（索引、版本记录、联系人…）。

    判据是"全部"而不是"任一"：一个模块里混着一个小节名叫「清单」但实际是业务
    列表（比如「订单清单」）是常见的，那种不该被跳过。
    只有**每一节**都像元信息时才判定为不可测。
    """
    ts = [t.strip() for t in titles if t and t.strip()]
    if not ts:
        return False
    return all(_STRUCTURAL_TITLE.search(t) for t in ts)


@dataclass
class DraftCase:
    """一条待审阅的生成用例。字段与 TestCase 对齐，但**故意**不是 ORM 对象。

    生成结果先过审阅再落库，所以这里只需要一个可序列化、可校验的中间形态。
    """

    name: str
    prompt: str
    expected: str
    module: str = ""
    type: str = "functional"
    priority: str = "P2"
    precondition: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "prompt": self.prompt,
            "expected": self.expected,
            "module": self.module,
            "type": self.type,
            "priority": self.priority,
            "precondition": self.precondition,
        }


@dataclass
class GenResult:
    cases: list[DraftCase] = dc_field(default_factory=list)
    # 被解析层丢掉的数量与原因 —— 不静默丢弃：用户看到"生成 12 条、丢弃 3 条"
    # 才能判断这次生成的整体质量，只看 12 条会以为模型很靠谱。
    dropped: list[str] = dc_field(default_factory=list)
    usage: dict = dc_field(default_factory=dict)
    error: str = ""
    # 0 条时的解释（见 generate_for_section 末尾）。空串 = 无需解释。
    # 为什么必须有：0 条有三种完全不同的原因（真没需求 / 模型判断错了 / 调用失败），
    # 对应三种不同的下一步动作；不区分的话用户只能反复点同一个按钮。
    note: str = ""


@dataclass
class PlanResult:
    """章节标题清单 → 测试模块的结果。"""

    modules: list[dict] = dc_field(default_factory=list)
    usage: dict = dc_field(default_factory=dict)
    error: str = ""
    # 规划失败退回"逐标题一个模块"时为 True —— 界面要据此提示用户
    # "这次分批粒度是降级过的"，而不是让他以为文档本来就这么碎。
    degraded: bool = False
    # 命中缓存时为 True，且 usage 为空 —— 让报告如实反映"这次没花 token"。
    # 用户明确要求降低消耗，所以"省下了一次调用"必须是可见的，否则没法验证。
    cached: bool = False


# ---------------------------------------------------------------------------
# 解析层：把模型输出变成可信的草稿
# ---------------------------------------------------------------------------

# "符合预期"这类同义反复的 expected 不算可判定断言 —— 它和"没有预期"是一回事，
# 但看起来像是填了。这是审查里最容易被放过的一类。
_TAUTOLOGY = (
    "符合预期",
    "按照预期",
    "与预期一致",
    "正常显示",
    "功能正常",
    "无异常",
    "一切正常",
    "as expected",
    "works as expected",
    "no error",
)


def _is_meaningful(text: str, min_len: int) -> bool:
    t = (text or "").strip()
    if len(t) < min_len:
        return False
    low = t.lower()
    # 整句就是一句同义反复 → 丢掉；只是包含这些词、但另有实质内容 → 保留。
    return not any(low == kw or low.rstrip("。.!！") == kw for kw in _TAUTOLOGY)


def parse_cases(raw: Any, module_hint: str = "") -> tuple[list[DraftCase], list[str]]:
    """把模型的 `cases` 数组变成草稿，并**报告**丢弃了什么。

    返回 (草稿, 丢弃原因)。丢弃原因要能读：用户需要判断"这次生成靠不靠谱"，
    而"生成了 12 条"本身说明不了问题。
    """
    dropped: list[str] = []
    out: list[DraftCase] = []
    if not isinstance(raw, list):
        return out, ["模型没有返回 cases 数组"]

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            dropped.append(f"第 {i + 1} 条不是对象")
            continue
        name = str(item.get("name") or "").strip()[:200]
        prompt = str(item.get("prompt") or "").strip()
        expected = str(item.get("expected") or "").strip()

        # 三条硬性前置：没有名字/没有操作指令/没有可判定预期 —— 任一缺失就不是用例。
        if not name:
            dropped.append(f"第 {i + 1} 条没有标题")
            continue
        if not _is_meaningful(prompt, 10):
            dropped.append(f"「{name}」缺少可执行的操作指令")
            continue
        if not _is_meaningful(expected, 6):
            dropped.append(f"「{name}」的预期结果不可判定（空或是同义反复）")
            continue

        t = str(item.get("type") or "functional").strip().lower()
        p = str(item.get("priority") or "P2").strip().upper()
        out.append(
            DraftCase(
                name=name,
                prompt=prompt,
                expected=expected,
                module=str(item.get("module") or module_hint or "").strip()[:200],
                # 认不出来的一律归默认值，不要把模型的自由文本落进枚举列 ——
                # 那会让报告的按类型/优先级分组冒出一次性类目。
                type=t if t in TYPES else "functional",
                priority=p if p in PRIORITIES else "P2",
                precondition=str(item.get("precondition") or "").strip()[:2000],
            )
        )
    return out, dropped


def parse_modules(raw: Any, valid_titles: list[str]) -> list[dict]:
    """把规划输出变成模块列表，并**强制**它只引用清单里真实存在的标题。

    为什么必须在这里强制：模型很乐意"顺手补全"文档里没有的标题（尤其是它认识的
    领域，比如自己加一个"登录模块"）。那些标题下面没有任何原文，生成出来的用例
    就是纯编造 —— 而且要等跑失败才发现。

    ★ 2026-10-09 改成**按编号引用**（`idx`）：第一版让模型逐字抄回标题，实测 60 个
    中文长标题抄下来就是上万字符的 JSON，直接撞上输出上限、JSON 被截断成
    "Unterminated string"，整批规划失败。改成编号之后输出体积降到零头，
    而且**抄错一个字就丢掉一条需求**这个隐患也不存在了。

    两种形状都收（`idx` 优先，`titles` 兼容），这样提示词回退时不会把功能弄坏。
    """
    allowed = {t.strip() for t in valid_titles if t and t.strip()}
    used: set[str] = set()
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        keep: list[str] = []
        idx = item.get("idx")
        if isinstance(idx, list):
            for i in idx:
                try:
                    n = int(i)
                except (TypeError, ValueError):
                    continue
                # 编号是 1-based（提示词里就是这么列的）
                if 1 <= n <= len(valid_titles):
                    t = valid_titles[n - 1].strip()
                    if t in allowed and t not in used:
                        keep.append(t)
                        used.add(t)
        else:
            titles = item.get("titles")
            if isinstance(titles, list):
                for t in titles:
                    s = str(t or "").strip()
                    if s in allowed and s not in used:
                        keep.append(s)
                        used.add(s)
        if not keep:
            continue  # 一条真实标题都没对上 → 这个模块没有原文依据，丢掉
        # 结构性标题由**代码**兜底判定，不完全信模型的 testable。
        # 实测：模型把「需求说明书索引及联系人」标成 testable=true，然后对那一节
        # 生成时返回空数组 —— 用户看到"可测模块"点进去得到 0 条、没有解释。
        if looks_structural(keep):
            testable = False
            reason = "（索引/版本/联系人这类元信息，在浏览器里没有可验证的需求）"
        else:
            testable = item.get("testable") is not False
            reason = str(item.get("reason") or "").strip()[:300]
        out.append(
            {
                "name": str(item.get("name") or "").strip()[:120] or f"模块 {len(out) + 1}",
                "titles": keep,
                # 默认 True：只有模型**明确**说不适合时才跳过 ——
                # 反过来（默认 False）会因为一次漏填就把整个模块的用例都吞掉。
                "testable": testable,
                "reason": reason,
            }
        )
    return out


async def _call_json(system: str, user: str, max_tokens: int = 8000) -> tuple[dict, dict]:
    """一次 JSON 模式调用，返回 (解析出的对象, usage 账本)。

    复用 app/llm.py 的池与它的重试语义：池里换 key 是我们自己的事（429 换下一套），
    传输层不重试（配额耗尽是必然失败，重试只是把一次失败拖成五次等待）。
    失败往上抛，由调用方决定怎么报给用户 —— 生成失败必须说出来，不能静默返回空列表。

    协议由 profile 决定（门面内部处理）：OpenAI 兼容 / Anthropic / Gemini 都能生成。
    """
    from app import llm as _llm

    res, _prof = await _llm.chat_with_pool(
        role="specgen",
        system=system,
        user=user,
        json_mode=True,
        temperature=0,
        max_tokens=min(max_tokens, get_settings().gateway_max_tokens or max_tokens),
        timeout=180.0,
    )
    data = res.json(default=None)
    if not isinstance(data, dict):
        raise ValueError(f"specgen: 模型未返回 JSON 对象（前 120 字：{(res.text or '')[:120]}）")
    return data, dict(res.usage or _llm.empty_usage())


# ---------------------------------------------------------------------------
# 对外的两件事：列章节、生成一批
# ---------------------------------------------------------------------------


def _clip_section(text: str) -> tuple[str, bool]:
    """截断过长的章节正文，并**如实说明**截断了。

    不说明的话，模型会以为读全了，然后对没读到的部分照样下断言 ——
    这比不给它内容更危险。
    """
    t = (text or "").strip()
    if len(t) <= MAX_SECTION_CHARS:
        return t, False
    return t[:MAX_SECTION_CHARS], True


async def generate_for_section(
    module_name: str,
    section_text: str,
    *,
    extra_context: str = "",
    max_cases: int = MAX_CASES_PER_BATCH,
) -> GenResult:
    """一个模块 → 一批草稿用例。绝不抛出去：失败以 `error` 字段返回。

    为什么 never-raises：这个函数在一个"逐节推进"的循环里被调用，
    让某一段的生成失败（模型抽风、网关抖动）把整轮生成中断掉，
    用户就得从头再来一遍已经生成好的部分。
    """
    body, truncated = _clip_section(section_text)
    if not body:
        return GenResult(error="这一节没有正文（可能文档里该标题下没有内容）")

    user_parts = [f"## 模块\n{module_name}\n"]
    if extra_context.strip():
        # 全局背景只给一次、且限长：它的作用是让模型知道"这是什么系统"，
        # 不是让它把整份文档再读一遍。
        user_parts.append(f"## 项目背景（节选）\n{extra_context.strip()}\n")
    user_parts.append(f"## 本节需求正文\n{body}\n")
    if truncated:
        user_parts.append(
            "\n⚠️ 本节正文过长，上面只给了前一部分，**后面还有内容被省略**。"
            "请只针对你确实读到的内容生成用例；不要为没读到的部分推测需求。"
        )
    user_parts.append(f"\n最多生成 {max_cases} 条；宁可少而准，不要凑数。")

    try:
        data, usage = await _call_json(_GEN_SYSTEM, "\n".join(user_parts))
    except Exception as exc:  # noqa: BLE001
        log.warning("specgen: 模块「%s」生成失败：%s", module_name, exc)
        return GenResult(error=f"{type(exc).__name__}: {exc}"[:300])

    cases, dropped = parse_cases(data.get("cases"), module_hint=module_name)
    if len(cases) > max_cases:
        # 保住靠前的：模型倾向于把主流程写在前面，后面的更可能是凑数的边缘用例。
        dropped.extend(f"「{c.name}」超出本批上限被舍弃" for c in cases[max_cases:])
        cases = cases[:max_cases]
    # ★ 0 条时必须**说清为什么**（2026-10-09 实测出来的体验问题）。
    #
    # 原来这里静默返回空列表，用户看到"这个模块 0 条"没有任何解释 ——
    # 而 0 条其实有三种完全不同的原因，对应三种完全不同的下一步动作：
    #   1. 这一节真的没有可端到端验证的需求（该跳过，去做别的模块）
    #   2. 生成被模型自己拒了但模型判断错了（该换个说法重试，或手工写）
    #   3. 调用其实失败了（该去查网关）
    # 不区分的话用户只能反复点同一个按钮。
    #
    # 这里只能确定地区分第 1 种与"其它"，所以措辞刻意留有余地。
    if not cases and not dropped:
        note = (
            "模型认为这一节没有可端到端验证的需求（索引、版本记录、联系人、"
            "性能指标这类内容在浏览器里观测不到），所以没有生成 —— 这是**合法结果**，"
            "不是失败。可以换下一个模块；如果认为这一节确实有可测需求，"
            "请检查需求原文是否写明了页面上的具体行为。"
        )
    else:
        note = ""
    return GenResult(cases=cases, dropped=dropped, usage=usage, note=note)


# 模块规划结果缓存：(project_id, start, end) -> (时间戳, PlanResult)
#
# ## 为什么必须有这个缓存（2026-10-09 实测出来的缺陷）
#
# 同一份文档、同一个接口，连续调四次，模型给出的分批**每次都不同**：
#
#     第1次: modules= 2  testable= 0
#     第2次: modules=16  testable=14
#     第3次: modules=18  testable=16
#     第4次: modules=13  testable=12
#
# 这不是"随机性可以接受"，而是三个具体问题：
#
#  1. **用户看不懂。** 他刚看到"16 个可测模块"，点一下刷新变成"2 个，0 个可测" ——
#     只能认为功能坏了。
#  2. **白烧钱。** 每次刷新/重进页面都重跑一次规划调用，而结果是一样的用处。
#     用户明确要求"尽量降低消耗"。
#  3. **接不上。** 界面上列的是第 N 次的分批，用户点"生成"时后端可能按第 N+1 次的
#     分组去找正文 —— 模块名对不上就报"找不到正文"，而文档明明在那儿。
#
# 缓存把"同一次分批"固定下来。TTL 到期或用户显式要求重新分批时才重算 ——
# **重算必须是用户主动的动作**，而不是刷新的副作用。
_PLAN_CACHE: dict[tuple[int, int, int], tuple[float, PlanResult]] = {}
_PLAN_TTL_S = 1800.0


def clear_plan_cache(project_id: int | None = None) -> None:
    """丢掉规划缓存。`project_id=None` 时清空全部（测试用）。"""
    if project_id is None:
        _PLAN_CACHE.clear()
        return
    for k in [k for k in _PLAN_CACHE if k[0] == project_id]:
        _PLAN_CACHE.pop(k, None)


async def plan_modules_cached(
    project_id: int, window: list[str], lo: int, hi: int, doc_intro: str = ""
) -> PlanResult:
    """带缓存的 `plan_modules`。

    `window` 是**已经切好、已经排好序**的这一批标题（调用方用 `rank_window` 得到）。
    `lo`/`hi` 只用来做缓存键 —— 刻意不拿它们再去切 `window`：
    那样窗口内重排（rank_window）会被切坏，而且"传进来的到底是不是全量清单"
    会变成一个只能靠注释记住的约定。**传什么就规划什么**，没有隐含假设。
    """
    import time

    key = (project_id, lo, hi)
    hit = _PLAN_CACHE.get(key)
    if hit is not None:
        ts, res = hit
        if time.monotonic() - ts < _PLAN_TTL_S:
            # 命中缓存 = 零 token。usage 记为 0，让报告诚实反映"这次没花钱"。
            return PlanResult(
                modules=res.modules,
                usage={},
                error=res.error,
                degraded=res.degraded,
                cached=True,
            )
    res = await plan_modules(window, doc_intro)
    # 只在成功时缓存：失败（网关 401、超时）不该被记住半小时 ——
    # 那样用户修好网关后再点还是看到同一个错误。
    if res.modules and not res.error:
        _PLAN_CACHE[key] = (time.monotonic(), res)
    return res


async def plan_modules(headings: list[str], doc_intro: str = "") -> PlanResult:
    """**一批**章节标题 → 测试模块。

    调用方负责分批（见 `chunk_bounds`）。这里刻意只处理一批：
    把 586 个标题一次性送过去，模型要么超长、要么把大半标题忽略掉 ——
    而"忽略掉"是静默的，用户只会看到模块数比预期少。

    会重复调用的场景请用 `plan_modules_cached` —— 见那个函数的注释。
    """
    if not headings:
        return PlanResult(error="这一批没有可规划的标题")

    numbered = "\n".join(f"{i + 1}\t{h}" for i, h in enumerate(headings))
    user = (
        f"## 文档开头（判断领域用）\n{doc_intro.strip()[:1500]}\n\n"
        f"## 本批章节标题（共 {len(headings)} 个）\n{numbered}\n\n"
        f"用编号引用（idx），最多归并出 {MAX_MODULES_PER_BATCH} 个模块。"
    )
    try:
        data, usage = await _call_json(_PLAN_SYSTEM, user, max_tokens=8000)
    except Exception as exc:  # noqa: BLE001
        log.warning("specgen: 模块规划失败：%s", exc)
        return PlanResult(error=f"{type(exc).__name__}: {exc}"[:300])

    mods = parse_modules(data.get("modules"), headings)
    if not mods:
        log.warning("specgen: 本批规划没有对上任何真实标题，退回逐标题模式")
        return PlanResult(
            modules=group_titles_into_modules(headings),
            usage=usage,
            degraded=True,
        )
    # 模型可能漏掉几个标题（它不总是"必须全部用到"都遵守）。漏掉的标题如果直接
    # 丢掉，那些需求就永远生成不出用例 —— 而用户完全看不出来。补成独立模块，
    # 让"漏掉"变成"多一个模块"（可见、可生成）而不是"少一块需求"（不可见）。
    covered = {t for m in mods for t in m["titles"]}
    missing = [h for h in headings if h not in covered]
    if missing:
        mods.extend(
            {
                "name": h,
                "titles": [h],
                "testable": True,
                "reason": "（模型归并时漏掉，已单独成模块）",
            }
            for h in missing
        )
    return PlanResult(modules=mods, usage=usage)


def chunk_bounds(total: int, size: int = MAX_SECTIONS) -> list[tuple[int, int]]:
    """把 total 个标题切成 [start, end) 区间，按文档顺序。

    这是"586 个标题一个都不能丢"的机制：规划按批走，每批负责一段连续标题，
    所有区间拼起来**完整覆盖** [0, total)。
    """
    if total <= 0:
        return []
    n = max(1, int(size))
    return [(i, min(i + n, total)) for i in range(0, total, n)]


def group_titles_into_modules(headings: list[str]) -> list[dict]:
    """纯函数版的兜底分组：每个标题一个模块。不带 LLM，可单测。"""
    return [
        {"name": h, "titles": [h], "testable": True, "reason": ""}
        for h in headings
        if h and h.strip()
    ]


# ---------------------------------------------------------------------------
# 从知识库取正文
# ---------------------------------------------------------------------------


async def list_headings(project_id: int) -> list[str]:
    """文档里出现过的标题，按原文顺序、去重。

    顺序很关键：它决定"模块 A 在模块 B 前面"，也就决定了界面上的推进顺序
    和用户的阅读顺序。`chunk_index` 就是原文位置，所以直接按它排。

    ## ★ 为什么不直接读 `KnowledgeChunk.heading`（2026-10-09 修）

    `knowledge.chunk_text` 会把小于 `CHUNK_TARGET_CHARS`（2000）的相邻小节**合并**
    成一个 chunk，而一个 chunk 只带**一个** heading（第一个）。于是：

        # 甲
        ## 甲-1
        甲的内容。
        ## 甲-2
        甲的第二节。

    只产出一个 chunk、heading 是「甲」—— **甲-2 在标题清单里完全不存在**，
    而且它的正文会被算进「甲」那一节。实测症状：一份 586 个标题的合并需规，
    用 `heading` 列只看得见很少一部分，用户在界面上根本看不到那些章节，
    也就永远生成不出它们的用例。

    正确做法是**从 chunk 正文里重新扫标题**：正文是完整保留的（合并不丢内容），
    所以扫出来的标题是什么就是什么。`heading` 列仍然用于检索排序，不动它。
    """
    from sqlalchemy import select

    from app.db import db_session
    from app.models import KnowledgeChunk

    async with db_session() as s:
        rows = (
            await s.execute(
                select(KnowledgeChunk.text)
                .where(KnowledgeChunk.project_id == project_id)
                .order_by(KnowledgeChunk.chunk_index)
            )
        ).scalars().all()

    seen: set[str] = set()
    out: list[str] = []
    levels: dict[str, int] = {}
    for body in rows:
        for line in (body or "").splitlines():
            m = _HEADING_RE.match(line.strip())
            if not m:
                continue
            t = m.group(2).strip()
            if not t:
                continue
            lvl = len(m.group(1) or "#")
            if t not in seen:
                seen.add(t)
                out.append(t)
                levels[t] = lvl
            else:
                # 同一标题可能在合并 chunk 里重复出现，取**最浅**的那次：
                # `# R-查询` 和 `### R-查询` 指的是同一节，用最浅的层级更有代表性。
                levels[t] = min(levels[t], lvl)
    # 层级信息挂到模块级，供 rank_window 使用。
    _HEADING_LEVELS[project_id] = levels
    return out


# 标题层级缓存：project_id -> {标题: # 的个数}。
# 为什么不改 list_headings 的返回类型去带上层级：它的调用方（接口、助手工具）
# 只需要标题字符串，改成元组会让每个调用点都要拆包 —— 为一个排序信号付这个代价不值。
_HEADING_LEVELS: dict[int, dict[str, int]] = {}


def rank_window(headings: list[str], lo: int, hi: int, project_id: int) -> list[str]:
    """把 [lo, hi) 这段窗口**内部**重排成"先看最可能有需求的"。

    ## 为什么要排序（2026-10-09 实测出来的体验问题）

    用户打开面板看到的第一批模块决定了他对这个功能的第一印象。实测那份
    586 个标题的合并需规，文档**最前面**恰好是全部元信息：

        资源审批使用（合并文档）/ 版本清单 / 未导入（纯文本文档）/
        需求说明书 / 联系人分配 / …

    第一批 60 个里大半是这类内容，于是用户看到"12 个模块、其中 6 个不可测"，
    而真正有需求的小节（`R-查询`、`R-新增`、`维护计划管理`…）散在后面 500 多个
    标题里，要连点十次「规划下一批」才碰得到。

    ## 为什么只在窗口内排、不改全局顺序

    `planned_until` 的推进和"586 个标题一个不丢"都依赖窗口是按序切出来的。
    全局重排会让进度语义（"已规划 60/586"）变得没法解释 ——
    "前 60 个"到底指哪些？窗口内重排则两全：进度照常推进，
    而每批里最可能有用的排在最前面。代价是排序不是全局最优，
    但用户是按批推进的，每批都先用上最有用的那些，实际体验更好。
    """
    lv = _HEADING_LEVELS.get(project_id, {})
    window = [(t, lv.get(t, 1), i) for i, t in enumerate(headings[lo:hi])]
    return rank_headings(window)


# 一眼看起来像"有需求"的标题：这些小节更可能写出用例。
# 用词都很保守 —— 这只是一个**排序**信号，判错一个词只是顺序差一点，
# 不会像"跳过某节"那样直接丢掉内容。
_REQ_SIGNAL = re.compile(
    r"(查询|新增|修改|删除|导入|导出|提交|审批|审核|配置|管理|列表|详情|权限|角色|"
    r"登录|校验|必填|流转|状态|申请|流程|规则|设置|统计|报表|"
    r"R-|U-|C-|D-)"
)


def rank_headings(scored: list[tuple[str, int, int]]) -> list[str]:
    """把 (标题, 层级, 原序) 排成"先看最可能有需求的"。

    ## 为什么要排序，而不是按文档顺序（2026-10-09 实测出来的体验问题）

    规划是**按 60 个一批**做的，而用户看到的第一批决定了他对这个功能的第一印象。
    实测那份 586 个标题的合并需规，文档**最前面**恰好是全部元信息：

        资源审批使用（合并文档）
        版本清单
        未导入（纯文本文档 / 扫描件）
        需求说明书
        联系人分配
        ...

    第一批 60 个里大半是这类内容，于是用户打开面板看到的是
    "12 个模块、其中 6 个不可测"，而那些真正有需求的小节
    （`R-查询`、`R-新增`、`维护计划管理`…）散在后面 500 多个标题里，
    要连点十次「规划下一批」才碰得到。

    排序把"最可能有需求的"提到前面，用户第一眼就能开工。
    排序键（从重到轻）：

    1. **层级深**优先 —— `###` 通常是具体功能点，`#` 往往是文档/大章节标题
       （实测那批元信息标题恰好都是 `#` 一级）。
    2. **含需求动词**（查询/新增/审批/配置…）加分。
    3. **元信息关键词**（索引/版本/联系人…）大幅减分。
    4. 仍以原序为末位键 —— 所以**同分内部保持文档顺序**，
       不会把内容搅乱，只是把明显没用的挪到后面。

    注意这里只改**规划的顺序**：`planned_until` 的推进、分批的完整性都不受影响，
    586 个标题一个都不会丢，只是先规划有用的那批。
    """
    structural = _STRUCTURAL_TITLE

    def sort_key(item: tuple[str, int, int]) -> tuple:
        title, level, order = item
        # 层级深 = 更具体。level 是 # 的个数（1..6），取负让深的排前面。
        depth = -int(level or 1)
        # 元信息：排到最后（1），其它 0。
        useless = 1 if structural.search(title) else 0
        # 有需求信号：排前面（0），没有 1。
        no_signal = 0 if _REQ_SIGNAL.search(title) else 1
        return (useless, no_signal, depth, order)

    return [t for t, _lvl, _ord in sorted(scored, key=sort_key)]


def split_chunk_sections(text: str) -> list[tuple[str, str]]:
    """一个 chunk 的正文 → [(标题, 该标题下的正文), ...]，按出现顺序。

    ★ 为什么必须切开（2026-10-09 修）：`knowledge.chunk_text` 会把小于
    2000 字符的相邻小节**合并**进一个 chunk，而一个 chunk 只记**一个** heading。
    实测：

        # 甲
        ## 甲-1
        甲的内容。
        ## 甲-2
        甲的第二节。

    产出的**单个** chunk 内容是上面全部五行、heading 是「甲」。

    如果按 `heading` 列取正文，会发生两件坏事：
      * 「甲-2」在标题清单里根本不存在（用户看不到这一节，永远生成不出它的用例）；
      * 就算点名要「甲」，拿到的正文里混着「甲-2」的内容 ——
        于是模型会把甲-2 的需求写成甲-1 的断言。

    切开之后，标题和它的正文永远是对上的。没有标题的行归到上一个标题下；
    第一个标题之前的文字（文档开头的说明）用空标题返回，调用方可以忽略。
    """
    out: list[tuple[str, list[str]]] = []
    for line in (text or "").splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            out.append((m.group(2).strip(), []))
        elif out:
            out[-1][1].append(line)
        else:
            out.append(("", [line]))
    return [(h, "\n".join(ls).strip()) for h, ls in out]


async def section_text(project_id: int, titles: list[str], cap: int = MAX_SECTION_CHARS) -> str:
    """把若干标题下的正文拼起来（按原文顺序）。

    为什么按标题取而不是把整份文档再读一遍：这是"按章节分批"能成立的前提 ——
    每次调用只带这一节的正文，输入大小与文档总量脱钩。

    实现上**从 chunk 正文重新切分**而不是按 `heading` 列匹配 ——
    原因见 `split_chunk_sections`：合并 chunk 会让标题与正文错配。
    """
    from sqlalchemy import select

    from app.db import db_session
    from app.models import KnowledgeChunk

    want = {t.strip() for t in titles if t and t.strip()}
    if not want:
        return ""
    async with db_session() as s:
        rows = (
            await s.execute(
                select(KnowledgeChunk.text)
                .where(KnowledgeChunk.project_id == project_id)
                .order_by(KnowledgeChunk.chunk_index)
            )
        ).scalars().all()

    # 先摊平成 (标题, 正文) 的有序序列，再按请求的标题筛。
    # 不按 `heading.in_(want)` 过滤数据库行：一个 chunk 里可能同时有想要和不想要的节。
    picked: list[tuple[str, str]] = []
    for body in rows:
        for h, sec in split_chunk_sections(body or ""):
            if h and h in want:
                picked.append((h, sec))

    parts: list[str] = []
    used = 0
    for h, body in picked:
        parts.append(f"\n### {h}\n")
        used += len(h) + 6
        if used + len(body) > cap:
            parts.append(body[: max(0, cap - used)])
            break
        parts.append(body)
        used += len(body)
    return "\n".join(parts).strip()


async def document_intro(project_id: int, chars: int = 2000) -> str:
    """文档最前面的一段，用来让模型判断"这是什么领域的系统"。

    只给开头：一整份需规的开头通常就是背景/术语/范围说明，判断领域足够了，
    而给多了就是把"按章节分批"省下来的 token 又花回去。
    """
    from sqlalchemy import select

    from app.db import db_session
    from app.models import KnowledgeChunk

    async with db_session() as s:
        rows = (
            await s.execute(
                select(KnowledgeChunk.text)
                .where(KnowledgeChunk.project_id == project_id)
                .order_by(KnowledgeChunk.chunk_index)
                .limit(6)
            )
        ).scalars().all()
    return "\n".join(rows)[:chars]
