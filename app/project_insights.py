"""项目概况面板：把散在库里的"事实"聚成几块看得懂的东西（2026-10-12）。

## 这一层和「项目文档」的区别（很重要，别混）

* **项目文档**（`ProjectDoc`）= AI **写的文字**。会随重新生成而变，也可能错，
  所以它每一节都带"依据章节"让你核对。
* **本模块** = 从库里**算出来的事实**。需求 ID 到用例的映射是查出来的，
  凭据健不健康是表里记的，接口是页面流量里抓到的。

把事实写成文字、再让 AI 复述一遍，只会引入漂移：文档说"某模块覆盖良好"，
而实际有一条需求一条用例都没测。所以这里**一律不算、只查**，
一个 LLM 调用都不发 —— 也因此它永远可用、秒出、不花 token。

## 用户要的六块

1. **需求覆盖矩阵**（按需求 ID）—— 回答"哪条需求没人测"
2. **功能模块覆盖**（按模块汇总）—— 更贴近人的读法
3. **业务规则清单** —— 含 RULE-/AC-/DEC- 的条目，每条挂上验证它的用例
4. **接口资产清单** —— 从页面流量学到的接口，标出写接口
5. **测试账号状态** —— 环境问题是失败归因里最大的一类，这里一眼看出
6. **用例变更历史** —— 回答"这条为什么变成这样"
7. **失败台账** —— 把"AI 判失败、其实是环境/agent 问题"的挑出来

## 一道必须守住的诚实底线

**覆盖率的分母要说清楚。** 1131 条用例里只有 18 个不同的需求 ID，
而其中两个 ID 被 357 条用例各自引用 —— 也就是说"用例很多"不等于"需求覆盖很全"。
所以每个数字旁边都要能看出**它是从多少条数据里算出来的**，
否则用户会拿着一个漂亮的百分比去做决定。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

# 需求 ID 的形态：前缀-关键词-编号（TST-TRAINRES-002）或 前缀:编号（DES:1358）。
#
# ★ 每段允许 1 个字符（2026-10-12 修）：原来是 `{2,12}`，于是 `RULE-A-1` 这种
#   短段编号**匹配不上**。真实数据里的段都很长（TRAINRES），所以这个洞一直没暴露；
#   但用户完全可能用 `XX-A-1` 这种编号，那时矩阵会静默漏掉整条需求。
#
# 为什么要按形态认，而不是"凡是 references 里的都当 ID"：
# 实测 references 里还混着 `v1.38` 这种版本号 —— 把版本号当需求去统计覆盖，
# 矩阵里会多出一条永远"未被覆盖"的假需求，用户会去追一个不存在的问题。
_ID_RE = re.compile(r"\b([A-Z]{2,6}(?:[-_][A-Z0-9]{1,12})+|\b[A-Z]{2,6}:\d{1,6})\b")

# 识别"这条 chunk 里带业务规则"
_RULE_KIND = ("RULE", "AC", "DEC")

# ★ 说明性分块要排除（2026-10-12 实测踩到）。
#
# 用户的文档里有一节叫「引用格式说明（怎么找到原文）」，内容是**记号表**：
#     | `RULE-TRAINRES-116` | 文档内的**规则编号**（`DES` §4.5 配置规则表） |
#     | `AC-TRAINRES-110`   | 文档内的**验收标准编号**（`DES` §11） |
# 我的代码原来会命中这里，于是"RULE-TRAINRES-116 的内容"变成了
# 「文档内的规则编号」—— 一句正确的废话，而用户拿它去核对用例时完全无从下手。
# 真正的定义在另一节（`4.5 配置`）：「同配置类型、使用范围、资源范围只能被一条未删除规则占用」。
_LEGEND_HEADING = re.compile(
    r"引用格式|格式说明|记号|标记说明|如何阅读|阅读说明|说明：|术语表|名词表|目录|索引|附录.*说明",
)
_LEGEND_BODY = re.compile(
    r"文档内的\*{0,2}(规则编号|验收标准编号|决定编号|版本增量条目|章节号)|"
    r"逐字摘不到|不收录|只是索引|供检索用",
)


def _is_legend(heading: str, text: str) -> bool:
    """判断一个分块是不是"解释记号"而不是"定义内容"。

    两道判据（命中其一即算说明性）：标题像说明，或正文反复出现
    "文档内的**XX编号**"这类**指向别处**的措辞。
    只看标题不够 —— 实测那一节的标题是「引用格式说明（怎么找到原文）」，
    而正文里的特征更稳。
    """
    if _LEGEND_HEADING.search(heading or ""):
        return True
    hits = len(_LEGEND_BODY.findall(text or ""))
    return hits >= 2


@dataclass
class RequirementCoverage:
    """一条需求的覆盖情况。"""

    req_id: str
    # 需求原文（从知识库里逐字摘出来的片段）
    excerpt: str = ""
    cases: list[str] = field(default_factory=list)
    verified_pass: int = 0
    verified_fail: int = 0
    auto_pass: int = 0
    auto_fail: int = 0

    @property
    def case_count(self) -> int:
        return len(self.cases)

    # 这两个"合计"原来只在 `as_dict()` 里现算，于是**对象上没有它们** ——
    # 调用方（和测试）只能去读 as_dict 的输出。放到属性上，
    # 让"合计"成为这个数据自己的事，而不是序列化时顺手算的。
    @property
    def verified_total(self) -> int:
        return self.verified_pass + self.verified_fail

    @property
    def auto_total(self) -> int:
        return self.auto_pass + self.auto_fail

    def as_dict(self) -> dict:
        vn = self.verified_total
        an = self.auto_total
        return {
            "req_id": self.req_id,
            "excerpt": self.excerpt,
            "case_count": self.case_count,
            "cases": self.cases[:60],
            "verified_total": vn,
            "verified_pass": self.verified_pass,
            "verified_fail": self.verified_fail,
            "verified_rate": round(self.verified_pass / vn * 100, 1) if vn else None,
            "auto_total": an,
            "auto_pass": self.auto_pass,
            "auto_fail": self.auto_fail,
            "auto_rate": round(self.auto_pass / an * 100, 1) if an else None,
            # 没有任何用例引用 = 这条需求没人测。矩阵里最该被看见的一格。
            "uncovered": self.case_count == 0,
        }


def extract_refs(raw: str) -> list[str]:
    """从 references 字段里抽出需求 ID。纯函数。

    丢掉不像 ID 的东西（实测 references 里混着 `v1.38` 这类版本号）。
    """
    out: list[str] = []
    for m in _ID_RE.finditer(raw or ""):
        v = m.group(1).strip("-_")
        # 单段（没有连字符/冒号）的短串多半是版本号或误写，不收
        if len(v) < 6 or ("-" not in v and ":" not in v):
            continue
        if v.lower().startswith("v") and "-" not in v:
            continue
        if v not in out:
            out.append(v)
    return out


def excerpt_for(req_id: str, chunks: list[tuple[str, str]], *, cap: int = 300) -> str:
    """在知识库里找这条需求 ID 的**定义**片段（不是它被引用的地方）。

    ★ 为什么要挑而不是"找到就返回"（2026-10-12 实测）：
    同一个 ID 会出现在三种完全不同的位置：

    1. **定义**（我们要的）—— 规则表里那一行：
       `| RULE-TRAINRES-116 | 同配置类型、使用范围、资源范围只能被一条未删除规则占用；已配置项禁选 |`
    2. **引用** —— 别的用例里写着"依据 RULE-TRAINRES-116「已配置项禁选」"
    3. **记号说明** —— 「引用格式说明」那一节把它当例子列出来（见 `_is_legend`）

    原来的实现"碰到第一个就返回"，实测命中的是第 3 种，于是矩阵里
    这条需求的原文显示成「文档内的规则编号」—— 用户拿着这句**无法核对任何东西**。

    打分规则：优先"ID 位于表格行首（`| \\`ID\\` | 内容`）"的形态 ——
    那是定义表的写法；排除带"依据/见/参见"的引用句；说明性分块整块跳过。
    """
    if not req_id:
        return ""
    best = ""
    best_score = -1
    for heading, text in chunks:
        if not text or req_id not in text:
            continue
        if _is_legend(heading or "", text):
            continue
        for m in re.finditer(re.escape(req_id), text):
            start, end = m.start(), m.end()
            tail = text[end : end + cap]
            tail = clean_excerpt(tail)
            tail = tail.lstrip("`| 　:：-—。")
            # ★ 在下一个 `|` 处截断（2026-10-12）：这些定义都是表格行，
            # 不截的话摘出来的会是"本条定义 | 下一条 ID | 下一条定义…"，
            # 用户读到的第一句是对的、后面全串行 —— 那比只给半句更误导。
            if "|" in tail:
                tail = tail.split("|", 1)[0].strip()
            if _too_short_to_be_a_definition(tail):
                continue
            score = 0
            # 表格定义行：ID 前面是行首或 `|`，后面紧跟 `|`
            before = text[max(0, start - 3) : start]
            if re.search(r"\|\s*`?$", before):
                score += 3
            if tail.startswith("|"):
                score += 2
            # 引用句：正文里说"依据/参见/见 §"
            if re.search(r"(依据|参见|见|遵循|按)\s*$", before):
                score -= 4
            if re.match(r"^[|`]?\s*(依据|见|参见)", tail):
                score -= 4
            # 正文越长越可能是真定义（"已配置项禁选"这种一句话就是定义）
            if len(tail) >= 20:
                score += 1
            if score > best_score:
                best_score, best = score, tail
    return best[:cap]


def build_requirement_matrix(
    refs_by_case: dict[str, list[str]],
    chunks: list[tuple[str, str]],
    outcomes: dict[str, dict],
) -> list[RequirementCoverage]:
    """把"用例 → 需求 ID"反过来，得到"需求 → 用例"。

    `refs_by_case`  : case_key → [req_id]
    `outcomes`      : case_key → {"verified": "passed"/"failed"/None,
                                 "auto": "passed"/"failed"/None}
    """
    by_req: dict[str, RequirementCoverage] = {}
    for case_key, refs in refs_by_case.items():
        for rid in refs:
            rc = by_req.setdefault(rid, RequirementCoverage(req_id=rid))
            if case_key not in rc.cases:
                rc.cases.append(case_key)
            o = outcomes.get(case_key) or {}
            v = o.get("verified")
            if v == "passed":
                rc.verified_pass += 1
            elif v == "failed":
                rc.verified_fail += 1
            if v is None:
                a = o.get("auto")
                if a == "passed":
                    rc.auto_pass += 1
                elif a == "failed":
                    rc.auto_fail += 1
    for rid, rc in by_req.items():
        rc.excerpt = excerpt_for(rid, chunks)
    # 引用最多的排前面：那是最核心的需求；未被覆盖的没引用数，自然沉底
    return sorted(by_req.values(), key=lambda r: (-r.case_count, r.req_id))


def build_module_matrix(cases: list[dict], outcomes: dict[str, dict]) -> list[dict]:
    """按模块汇总覆盖情况（用户要的第二块）。"""
    by_mod: dict[str, dict] = {}
    for c in cases:
        m = (c.get("module") or "").strip() or "未分类"
        b = by_mod.setdefault(
            m,
            {
                "module": m,
                "case_total": 0,
                "verified_pass": 0,
                "verified_fail": 0,
                "auto_pass": 0,
                "auto_fail": 0,
                "awaiting_review": 0,
                "with_refs": 0,
            },
        )
        b["case_total"] += 1
        if c.get("has_refs"):
            b["with_refs"] += 1
        o = outcomes.get(str(c.get("case_key"))) or {}
        v = o.get("verified")
        if v == "passed":
            b["verified_pass"] += 1
        elif v == "failed":
            b["verified_fail"] += 1
        else:
            b["awaiting_review"] += 1
            a = o.get("auto")
            if a == "passed":
                b["auto_pass"] += 1
            elif a == "failed":
                b["auto_fail"] += 1
    out = []
    for b in by_mod.values():
        vn = b["verified_pass"] + b["verified_fail"]
        b["verified_total"] = vn
        b["verified_rate"] = round(b["verified_pass"] / vn * 100, 1) if vn else None
        b["no_refs"] = b["case_total"] - b["with_refs"]
        out.append(b)
    out.sort(key=lambda x: (-x["case_total"], x["module"]))
    return out


def build_rules(
    rules: list[dict], refs_by_case: dict[str, list[str]]
) -> list[dict]:
    """业务规则清单：每条规则后面挂上验证它的用例。"""
    by_req: dict[str, list[str]] = {}
    for case_key, refs in refs_by_case.items():
        for r in refs:
            by_req.setdefault(r, []).append(case_key)
    # 显式标注累加器类型：不标的话 mypy 只能从第一个 append 推断，
    # 随后 `-x["case_count"]` 就成了"对 object 取负"。这不是 mypy 挑刺 ——
    # 它说明"这个列表里到底装什么"在代码里没说清。
    out: list[dict] = []
    # 变量名不要复用上面那个 `r`（refs 里的需求 ID，是 str）——
    # 复用会让同一作用域里 `r` 的类型漂移，mypy 报的 4 个错全出自这里。
    for rule in rules:
        rid = str(rule["req_id"])
        cases = by_req.get(rid, [])
        out.append(
            {
                "req_id": rid,
                "kind": rid.split("-")[0].split(":")[0].upper(),
                "text": str(rule.get("text") or ""),
                "heading": str(rule.get("heading") or ""),
                "cases": cases[:40],
                "case_count": len(cases),
            }
        )
    out.sort(key=lambda x: (-int(x["case_count"]), str(x["req_id"])))
    return out


_ws = re.compile(r"\s+")


# 摘出来的文本要**给人读**，所以顺手把源文档里的书写痕迹去掉。
#
# 实测（2026-10-12）：需求文件本身是 Markdown/HTML 混排，摘出来长这样：
#     ...（申请、结算和数据库删除）。<br>**步骤**：填报页 → 基本信息区...
# 直接显示的话，用户看到的是 `**步骤**` 和 `<br>`，正文反而读不出来。
# 这些字符**不是需求内容的一部分**，去掉不损失信息。
_JUNK = (
    (re.compile(r"<br\s*/?>", re.I), " "),
    (re.compile(r"</?[a-zA-Z][^>]{0,60}>"), " "),   # 其余 HTML 标签
    (re.compile(r"\*\*|__|~~"), ""),                 # Markdown 强调符
    (re.compile(r"\\([\"'`])"), r"\1"),              # 转义引号
    (re.compile(r"&nbsp;?"), " "),
    (re.compile(r"&lt;"), "<"),
    (re.compile(r"&gt;"), ">"),
    (re.compile(r"&amp;"), "&"),
)


def clean_excerpt(text: str) -> str:
    """把摘出来的片段清成可读文本。纯函数。"""
    out = text or ""
    for pat, repl in _JUNK:
        out = pat.sub(repl, out)
    return _ws.sub(" ", out).strip()


def _too_short_to_be_a_definition(tail: str) -> bool:
    """判断摘出来的片段是不是"短到不可能是一条定义"。

    ★ 不能用字符数当阈值（2026-10-12 修）。原来写的是 `len(tail) < 4`，
    于是「定义甲」这种**中文三字定义**被丢掉了 —— 中文一个字符承载的信息
    远多于英文一个字符，`len()` 在这里不是合适的度量。
    （实测踩到：「已配置项禁选」7 个字是好定义，而「定义甲」3 个字同样是好定义。）

    改成看"有没有实义内容"：去掉标点与空白后至少 2 个字符，
    且不能全是 `-`/`.`/`—` 这类占位。
    """
    core = re.sub(r"[\s`|·.。,:：;；\-—]+", "", tail or "")
    return len(core) < 2


def extract_rules(chunks: list[tuple[str, str, int]], *, cap: int = 240) -> list[dict]:
    """从知识库分块里抽业务规则条目。

    `chunks` 每项是 **(heading, text, id)** —— 顺序就是这样，别传反。

    ★ 两条实测教训（2026-10-12）：

    1. **跳过说明性分块**。见 `_is_legend` —— 否则"规则内容"会变成
       「文档内的规则编号」这种记号说明。
    2. **同一个 ID 只留最好的一条**。原实现是"见过就丢"（`seen` 集合按出现顺序），
       于是谁先出现谁赢 —— 而先出现的往往是**引用**或**说明**，
       真正的定义表在后面。改成按打分保留最优。
    """
    best: dict[str, dict] = {}
    best_score: dict[str, int] = {}

    def consider(rid: str, text: str, start: int, end: int, heading: str) -> None:
        before = text[max(0, start - 3) : start]
        tail = clean_excerpt(text[end : end + cap]).lstrip("`| 　:：-—。")
        # 在下一个 `|` 处截断：定义是表格行，不截会串到下一条规则去。
        if "|" in tail:
            tail = tail.split("|", 1)[0].strip()
        if _too_short_to_be_a_definition(tail):
            return
        score = 0
        if re.search(r"\|\s*`?$", before):
            score += 3  # 表格定义行
        if tail.startswith("|"):
            score += 1
        if re.search(r"(依据|参见|见|遵循|按)\s*$", before):
            score -= 4
        if len(tail) >= 20:
            score += 1
        if score > best_score.get(rid, -99):
            best_score[rid] = score
            best[rid] = {"req_id": rid, "text": tail[:cap], "heading": heading or ""}

    for item in chunks:
        # 元组顺序传反是很容易犯的错，而 `text.find(1)` 抛的是
        # `TypeError: expected string or bytes-like object, got 'int'` ——
        # 报错信息完全指不到"顺序反了"。所以在这里显式说清楚。
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            raise ValueError(
                f"extract_rules: chunks 每项必须是 (heading, text, id) 三元组，收到 {item!r}"
            )
        heading, text, _cid = item
        if not isinstance(text, str) or not isinstance(heading, str):
            raise ValueError(
                f"extract_rules: 顺序应为 (heading, text, id)，文本必须是字符串；"
                f"收到 heading={heading!r} text={text!r}"
            )
        if not text or _is_legend(heading, text):
            continue
        for m in re.finditer(r"\b((?:RULE|AC|DEC)[-_][A-Z0-9][A-Z0-9_\-]{1,24})\b", text):
            # 全部出现位置都参与打分，不只第一个 —— 定义在后面的表格里。
            for mm in re.finditer(re.escape(m.group(1)), text):
                consider(m.group(1), text, mm.start(), mm.end(), heading)
    return list(best.values())


def read_insights(project_id: int, *, limit_events: int = 60) -> dict:
    """读库算全部面板。**只读**，不发任何 LLM 调用。"""
    # 只读连接：这一层永远不该写任何东西（它展示的是事实，不是状态）
    c = sqlite3.connect("file:potato.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        cases = [
            {
                "id": r["id"],
                "case_key": r["case_key"],
                "name": r["name"],
                "module": r["module"],
                "status": r["status"],
                "has_refs": bool((r["refs"] or "").strip()),
            }
            for r in c.execute(
                'select id, case_key, name, module, status, "references" as refs '
                "from test_case where project_id=?",
                (project_id,),
            )
        ]
        chunks = [
            (r["heading"] or "", r["text"] or "", r["id"])
            for r in c.execute(
                "select id, heading, text from knowledge_chunk where project_id=? order by chunk_index",
                (project_id,),
            )
        ]

        # 结果 → 每条用例的最新一次结论（人工优先）
        outcomes: dict[str, dict] = {}
        for r in c.execute(
            "select tc.case_key as ck, rr.status as st, rr.verdict_override as ov "
            "from run_result rr join test_case tc on tc.id = rr.case_id "
            "where tc.project_id=? order by rr.id",
            (project_id,),
        ):
            ck = r["ck"]
            if not ck:
                continue
            ov = r["ov"]
            o = outcomes.setdefault(ck, {"verified": None, "auto": None})
            if ov in ("passed", "failed"):
                o["verified"] = ov
            elif o["verified"] is None and r["st"] in ("passed", "failed", "error"):
                o["auto"] = "passed" if r["st"] == "passed" else "failed"

        refs_by_case: dict[str, list[str]] = {}
        for r in c.execute(
            'select case_key, "references" as refs from test_case where project_id=?',
            (project_id,),
        ):
            got = extract_refs(r["refs"] or "")
            if got:
                refs_by_case[r["case_key"]] = got

        matrix = build_requirement_matrix(refs_by_case, [(h, t) for h, t, _ in chunks], outcomes)
        modules = build_module_matrix(cases, outcomes)
        rules = build_rules(extract_rules(chunks), refs_by_case)

        # 接口资产
        apis = [
            {
                "method": r["method"],
                "path": r["path"],
                "hits": r["hits"],
                "writable": bool(r["writable"]),
                "origin": r["origin"],
                "last_seen": (r["last_seen"] or "")[:19],
            }
            for r in c.execute(
                "select method, path, hits, writable, origin, last_seen from api_endpoint "
                "where project_id=? order by writable desc, hits desc limit 60",
                (project_id,),
            )
        ]

        # 账号状态。**不返回 secret**：这一块只要"能不能用"，不是凭据管理页。
        creds = [
            {
                "id": r["id"],
                "role": r["role"],
                "label": r["label"],
                "is_active": bool(r["is_active"]),
                "healthy": bool(r["healthy"]),
                "last_error": (r["last_error"] or "")[:200],
                "last_checked_at": (r["last_checked_at"] or "")[:19],
                "expires_at": (r["expires_at"] or "")[:19],
            }
            for r in c.execute(
                "select id, role, label, is_active, healthy, last_error, last_checked_at, expires_at "
                "from credential where project_id=? order by is_active desc, role",
                (project_id,),
            )
        ]

        # 用例变更历史：带上用例编号，否则用户看到 case_id 不知道是哪条
        changes = [
            {
                "id": r["id"],
                "case_key": r["case_key"],
                "case_name": (r["case_name"] or "")[:80],
                "field": r["field"],
                "before": (r["before"] or "")[:220],
                "after": (r["after"] or "")[:220],
                "source": r["source"] or "",
                "created_at": (r["created_at"] or "")[:19],
            }
            for r in c.execute(
                "select cc.id, cc.field, cc.before, cc.after, cc.source, cc.created_at, "
                "tc.case_key, tc.name as case_name "
                "from case_change cc left join test_case tc on tc.id = cc.case_id "
                "where cc.project_id=? order by cc.id desc limit ?",
                (project_id, limit_events),
            )
        ]

        # 失败台账：按归因汇总，并挑出"AI 判失败但归因不是产品缺陷"的
        attribution: Counter[str] = Counter()
        suspects: list[dict] = []
        for r in c.execute(
            "select rr.status, rr.attribution, rr.verdict_override, rr.judge_reason, "
            "rr.error, tc.case_key, tc.name, rr.id "
            "from run_result rr join test_case tc on tc.id = rr.case_id "
            "where tc.project_id=? and rr.status in ('failed','error') order by rr.id desc",
            (project_id,),
        ):
            attribution[r["attribution"] or "(未判定)"] += 1
            # 人工改判成通过 = AI 判错了。这是最有价值的一类"假失败"。
            if r["verdict_override"] == "passed" or (
                r["verdict_override"] is None
                and (r["attribution"] or "") not in ("product", "product_defect", "system")
            ):
                suspects.append(
                    {
                        "result_id": r["id"],
                        "case_key": r["case_key"],
                        "case_name": (r["name"] or "")[:70],
                        "attribution": r["attribution"] or "",
                        "overridden_to": r["verdict_override"] or "",
                        "reason": (r["judge_reason"] or r["error"] or "")[:200],
                    }
                )

        uncovered = [m for m in matrix if m.case_count == 0]
        return {
            "requirements": [m.as_dict() for m in matrix],
            "requirement_summary": {
                "total": len(matrix),
                "covered": len(matrix) - len(uncovered),
                "uncovered": len(uncovered),
                # 分母要摆出来："1131 条用例只引用 18 个需求 ID"这句话
                # 比一个 100% 的覆盖率更能说明真实的追溯粒度。
                "cases_with_refs": len(refs_by_case),
                "cases_total": len(cases),
                "top_req": matrix[0].req_id if matrix else "",
                "top_req_cases": matrix[0].case_count if matrix else 0,
            },
            "modules": modules,
            "rules": rules,
            "apis": apis,
            "credentials": creds,
            "changes": changes,
            "failures": {
                "by_attribution": [{"attribution": k, "count": v} for k, v in attribution.most_common()],
                "suspects": suspects[:limit_events],
                "suspect_total": len(suspects),
            },
        }
    finally:
        c.close()


def json_dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)[:2000]
