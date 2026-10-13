"""按项目文档优化测试用例：生成提案（2026-10-12）。

## 用户要的

「我希望 ai 能根据这个页面去优化测试用例和增加测试用例同删除不必要的测试用例」

而"这个页面"= 项目文档（总览/需求分析），那是 AI 从整份需规提炼出来的骨架。
所以这里做的是**用需规去校准用例库**，三个动作：

* `add`    —— 需规写了、但用例库没有对应的（漏测）
* `update` —— 用例与需规对不上（范围过宽/过窄、预期写错、前置缺失）
* `remove` —— 需规里没有、或者与别的用例重复（冗余）

## 为什么必须分批（这决定了整个实现形状）

实测教训（见 `spec_overview._SECTION_SPECS` 的注释）：**一次要模型写太多，输出会被硬截断，
JSON 拿不回来、那一批全丢。** 项目 1 有 368 条用例，全塞进一次调用必然撞上这条墙。

所以按**模块**分批：每个模块一次调用，输入 = 该模块的现存用例 + 相关需规内容。
一批坏掉只影响那个模块。这也让提示词短、命中率高。

## 为什么提案里必须有 evidence

用户会问"它凭什么说这条多余"。答案要在提案里看得到（哪一节需规、关联哪几条用例），
否则这就是一段看起来很像真的建议 —— 和它要解决的问题一样不可信。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("potato-test.case_optimize")

# 每个模块最多带多少条现存用例给模型看。超过就截断并**说明截断了** ——
# 不说明的话模型会对没看到的部分照样下结论。
#
# ★ 从 40 降到 12（2026-10-12 实测）。
# 40 条用例的提示词约 9700 字符，而**这个网关对超过约 4.5~5k 字符的提示词
# 返回空内容**（HTTP 200、正文空、无报错）。实测：同一模块、同一提示词，
#   3500 字符→OK  4000→OK  4500→OK  5000→空  8000→空  11000→空
# 于是 12 个模块**全部**报"模型未返回 JSON 对象（共 0 字符）"—— 一次都跑不成。
#
# 12 条 + 1500 字需规 ≈ 4200 字符，落在安全区。
# 代价是模型看不到全部用例（它可能因此漏判"与某条重复"），
# 所以提示词里会**明说只列了前 N 条**，并且优化入口优先取用例最多的模块。
_MAX_CASES_PER_MODULE = 12

# 需规内容最多给多少字符。
# 同样为提示词总长让路：4000 → 1500。需规是判断依据，但它只用来提供
# "这个模块应该有什么"，1500 字足够覆盖一个模块的规则描述。
_MAX_SPEC_CHARS = 1500

# 一次运行最多收多少条提案（防止模型话多到把审阅页撑爆）。
_MAX_PROPOSALS = 120

_SYSTEM = """你是资深测试架构师。用户会给你：
* 该项目需求文档提炼出的**模块/规则描述**；
* 该模块下**已有的测试用例**（编号 + 名称 + 预期 + 账号）；
* 该项目**可用的账号清单**（角色 + 是否可用）。

你的任务是找出用例库与需规之间的**差距**，逐条给出提案。三种动作：

* `add`    —— 需规里要求了，但现有用例**没有覆盖**的场景。给出可直接执行的用例。
* `update` —— 现有用例与需规**对不上**（预期写错、范围过宽/过窄、前置缺失、
              步骤无法验证该预期）。只列出**要改的字段**，不要重写整条。
* `remove` —— 需规里**没有**这个功能，或它与另一条用例**实质重复**。

## 铁律

1. **不许编造需规里没有的功能。** 你的每一条提案都要能指到上面给你的需规描述。
2. **`remove` 要格外保守。** 只在"需规确实没写"或"与某条实质重复"时才提。
   拿不准就不要提 —— 漏一条冗余用例的代价，远小于误删一条有效用例。
3. **每条都要给 `evidence`**：写清依据（需规里的原话片段 / 与哪几条用例重复）。
4. 用**中文**。只输出 JSON，不要解释。

## 账号（`role`）

每条提案都要给 `role`：这条用例**该用哪个账号跑**。
* 只能从上面给的**账号清单**里选，逐字照抄，不许自己编一个角色名 ——
  编出来的角色会让用例因为"找不到账号"直接失败，而用户看不出是提案的错。
* 判断依据是**需规里写的角色职责**（谁能做这件事就用谁）。
* 只有确实无法判断时才留空字符串 ""。

## 相关流程图（`flow`）

还要画**一张** Mermaid 流程图（`flow` 字段），讲清这个模块涉及的业务流程 ——
用户会拿它来判断"你为什么要加/改/删这条用例"。所以：
* 图上要能看出**谁（哪个角色）在什么环节做什么、进入什么状态**；
* 节点标签里**带上角色名**（例如 `A["审批人 · 审批申请"]`），这样图和账号能对上；
* 只画需规里写了的环节，不要补不存在的分支；
* 如果这一批**只有 remove 提案**（比如"需规没写兼容性，建议删掉那几条"），
  `flow` 就留空字符串 —— 删一条用例不需要流程图，硬画一张反而让人以为
  改动涉及那条流程。别为了凑字段而画。

Mermaid 写法要求（写错就渲染不出来）：
* 第一行必须是 `flowchart LR` 或 `flowchart TD`。**按步骤数选方向**：
  * 步骤 **≤4 个** → `LR`（从左到右，扁扁的正好，不容易显得空）；
  * 步骤 **≥5 个** → `TD`（从上到下）。
    为什么：`LR` 把 N 个节点横向串起来，而提案页的容器只有 ~950px 宽 ——
    实测 7 个节点的 `LR` 是 1320x76（**17:1**），缩到容器里每个节点只剩
    十几像素宽，文字根本看不清。`TD` 同样的 7 个节点是竖向排布，
    宽高比接近 1:1，字看得清也不用横向滚动。
* 节点 ID 只用 **ASCII 字母数字**（`A`、`B1`），中文只能出现在标签里；
* 标签一律方括号加引号：`A["提交申请"]`；**不要**在标签里用 `()`、`[]`、`{}`；
* 判断节点用花括号：`B{"是否通过"}`；连线 `A -->|"通过"| B`；
* 节点 **6~10 个**，不要更多。实测一个 10 节点的纵向链渲染出来是 932x2822
  （近三屏），再多就没法看了 —— 图是给人一眼看懂用的，不是把整条链路抄一遍。
  一个环节里有多个角色时，合并成一个节点写清（`A["发起人/审批人 · 提交并审批"]`）
  比拆成两个节点好。
  不要写 `%%` 注释、`classDef`/`style`/`linkStyle`。

## 输出格式

{"flow": "<Mermaid 源码，画这个模块的流程>",
 "proposals": [
  {"action": "add",
   "summary": "一句话说清这条提案要做什么（≤60字）",
   "reason": "为什么（≤200字，要说清加这条是为了补上什么缺口）",
   "evidence": ["依据一", "依据二"],
   "role": "从账号清单里逐字照抄的角色名",
   "case_key": null,
   "case": {"name": "...", "module": "...", "priority": "P0|P1|P2",
            "preconditions": "...", "prompt": "给浏览器 agent 的任务描述（可执行、自足）",
            "expected": "可核对的预期结果", "tags": []}},
  {"action": "update",
   "summary": "...", "reason": "...", "evidence": [...], "role": "...",
   "case_key": "TP-XXX-001",
   "changes": {"expected": "改后的预期", "prompt": "改后的任务"}},
  {"action": "remove",
   "summary": "...", "reason": "...", "evidence": [...],
   "case_key": "TP-XXX-002"}
]}

没有差距时输出 {"flow": "...", "proposals": []}。**不要**为了凑数而提。"""



@dataclass
class Draft:
    """一条 `add` 提案的完整草稿字段。"""

    name: str = ""
    module: str = ""
    priority: str = "P1"
    preconditions: str = ""
    prompt: str = ""
    expected: str = ""
    role: str = ""
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "module": self.module,
            "priority": self.priority,
            "preconditions": self.preconditions,
            "prompt": self.prompt,
            "expected": self.expected,
            "role": self.role,
            "tags": list(self.tags),
        }


@dataclass
class Proposal:
    action: str
    summary: str = ""
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    case_key: str | None = None
    case_id: int | None = None
    payload: dict = field(default_factory=dict)
    # 该用哪个账号跑（必须是项目里真实存在的 credential.role）
    role: str = ""
    # 该模块的流程图（Mermaid 源码）。由 optimize_project 在装配时填，
    # 同一个模块的所有提案共享同一张。
    flow: str = ""

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "summary": self.summary,
            "reason": self.reason,
            "evidence": self.evidence,
            "case_key": self.case_key,
            "case_id": self.case_id,
            "payload": self.payload,
            "role": self.role,
            "flow": self.flow,
        }


def build_prompt(
    module: str, cases: list[dict], spec_text: str, accounts: list[dict] | None = None
) -> str:
    """拼一个模块的提示词。抽出来是为了可单测（不必真调模型）。

    `accounts` 是这个项目真实存在的账号（role/label/healthy）。**必须给它** ——
    不然模型会照需规里的角色名（"结算单发起人"）编一个 role 出来，
    而那和 credential 表里的那个（`sittrapply`）对不上，用例会因找不到账号直接失败。
    实测项目里已经有 2 条用例踩了这个坑。
    """
    shown = cases[:_MAX_CASES_PER_MODULE]
    lines = []
    for c in shown:
        # ★ 每条只给"判定够用"的信息（2026-10-12 收紧）。
        #
        # 用例是用来判"需规有没有覆盖、有没有写错"，靠的是**名称 + 预期**；
        # `prompt`（给浏览器 agent 的任务描述）常常几百字，塞进来会挤掉
        # 别的用例的位置 —— 而提示词总长有硬上限（见 _MAX_CASES_PER_MODULE 的注释）。
        # 所以 prompt 只留 60 字当线索，够看出"这条在测什么"就行。
        bits = [f"- [{c.get('case_key') or '?'}] {c.get('name') or ''}"]
        if c.get("role"):
            bits.append(f"    账号：{c['role']}")
        if c.get("expected"):
            bits.append(f"    预期：{str(c['expected'])[:120]}")
        if c.get("prompt"):
            bits.append(f"    任务：{str(c['prompt'])[:60]}")
        lines.append("\n".join(bits))
    more = ""
    if len(cases) > len(shown):
        # 必须说明截断了：不说的话模型会以为这就是全部，
        # 然后对没看到的用例照样下"没覆盖 / 重复"的结论。
        more = (
            f"\n（该模块共 {len(cases)} 条用例，上面**只列了前 {len(shown)} 条**。"
            "没看到的不要提 remove，也不要断言它重复。）"
        )

    # 账号清单：只给**可用的**和**不可用的**分开列。
    # 不可用的也要列出来 —— 模型才知道"这个角色存在但现在跑不了"，
    # 从而可以在提案里点出来，而不是傻傻地指定一个跑不通的账号。
    acc_block = ""
    if accounts:
        ok = [a for a in accounts if a.get("healthy")]
        bad = [a for a in accounts if not a.get("healthy")]
        parts = []
        if ok:
            parts.append("可用：" + "、".join(f"{a['role']}" for a in ok))
        if bad:
            parts.append(
                "不可用（存在但现在跑不通，指定它们要在 reason 里说明）："
                + "、".join(f"{a['role']}" for a in bad)
            )
        acc_block = "\n## 项目可用账号（role 只能从这里选）\n" + "\n".join(parts) + "\n"
    else:
        acc_block = "\n## 项目账号\n（这个项目还没有配账号，role 一律留空字符串。）\n"

    return (
        f"## 模块：{module}\n\n"
        f"## 该模块的需规描述\n{(spec_text or '（文档里没有这个模块的描述）').strip()[:_MAX_SPEC_CHARS]}\n"
        f"{acc_block}\n"
        f"## 该模块现有用例（{len(cases)} 条）\n"
        + ("\n".join(lines) if lines else "（该模块下还没有任何用例）")
        + more
        + "\n\n请输出 flow 与 proposals 的 JSON。"
    )


def parse_proposals(
    data: object,
    known_keys: set[str],
    module: str,
    valid_roles: set[str] | None = None,
) -> list[Proposal]:
    """模型输出 → 提案列表。纯函数，可单测。

    这里做四件**收口**的事，都是在真实数据里踩过的形状：

    1. 丢掉 `unknown` 动作与缺字段的条目 —— 一条格式不对的提案会让整批解析失败。
    2. `update`/`remove` 的 `case_key` 必须**在给出的清单里**。
       模型会编出一个不存在的编号（比如把模块前缀换掉），照收下来就会生成
       "改一条不存在的用例"的提案，用户点采纳时才发现，而那时他已经信了这份清单。
    3. `add` 必须有可执行的 `prompt`：没有任务描述的用例跑不了，
       收进来只会污染用例库。
    4. ★ `role` 必须**在项目的账号清单里**（2026-10-12 加）。
       这是用户要求"附上账号"时最容易出问题的一环：模型照着需规里的角色名写
       （"结算单发起人"），而 credential 表里那个账号叫 `sittrapply` ——
       照收下来，采纳后用例会因为"找不到账号"直接失败，
       而用户看不出是提案的问题。所以不在清单里的角色**一律丢掉**（留空 = 用默认账号），
       而不是留着让它在跑的时候炸。
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("proposals")
    if not isinstance(raw, list):
        return []
    roles = valid_roles or set()
    out: list[Proposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower()
        if action not in ("add", "update", "remove"):
            continue
        summary = str(item.get("summary") or "").strip()[:400]
        reason = str(item.get("reason") or "").strip()
        ev = item.get("evidence")
        evidence = (
            [str(x).strip()[:200] for x in ev if isinstance(x, str) and x.strip()][:10]
            if isinstance(ev, list)
            else []
        )
        key = item.get("case_key")
        key = str(key).strip() if isinstance(key, str) and key.strip() else None
        # 角色只在清单里才收；没有账号清单时一律留空（宁可用默认账号，
        # 也不要一个跑不通的角色）
        role = str(item.get("role") or "").strip()
        if role not in roles:
            role = ""

        if action == "add":
            c = item.get("case")
            if not isinstance(c, dict):
                continue
            prompt = str(c.get("prompt") or "").strip()
            if not prompt:
                continue  # 没有任务描述的用例跑不了
            tags = c.get("tags")
            d = Draft(
                name=str(c.get("name") or summary or "未命名用例").strip()[:300],
                module=str(c.get("module") or module).strip()[:200],
                priority=str(c.get("priority") or "P1").strip().upper()[:10],
                preconditions=str(c.get("preconditions") or "").strip(),
                prompt=prompt,
                expected=str(c.get("expected") or "").strip(),
                # 用例内部的 role 字段也用校验过的那个，别让 case.role 绕过校验
                role=role,
                tags=[str(x).strip() for x in tags if isinstance(x, str) and x.strip()][:10]
                if isinstance(tags, list)
                else [],
            )
            if d.priority not in ("P0", "P1", "P2"):
                d.priority = "P1"
            out.append(
                Proposal(
                    action="add", summary=summary or d.name, reason=reason,
                    evidence=evidence, payload=d.as_dict(), role=role,
                )
            )
            continue

        # update / remove 都必须指向一条**我们确实给它看过**的用例
        if not key or key not in known_keys:
            continue
        if action == "update":
            ch = item.get("changes")
            if not isinstance(ch, dict):
                continue
            allowed = ("name", "priority", "preconditions", "prompt", "expected", "role", "module")
            changes = {
                k: (str(v).strip() if not isinstance(v, list) else v)
                for k, v in ch.items()
                if k in allowed and v is not None and str(v).strip()
            }
            # 模型想在 changes 里改 role 时，同样要过账号校验
            if "role" in changes and str(changes["role"]) not in roles:
                changes.pop("role")
            if not changes:
                continue
            out.append(
                Proposal(
                    action="update", summary=summary, reason=reason,
                    evidence=evidence, case_key=key, payload=changes, role=role,
                )
            )
            continue
        out.append(
            Proposal(
                action="remove", summary=summary, reason=reason,
                evidence=evidence, case_key=key, role=role,
            )
        )
    return out[: _MAX_PROPOSALS]


@dataclass
class ModuleResult:
    """一个模块的生成结果。

    ★ 为什么要区分 `ok` 与"提案为空"（2026-10-12 修）：
    原来是"调用没抛异常就算成功，靠 usage 是不是空来判失败"。这个判据是错的 ——
    模型对一个**已经覆盖良好**的模块，正确的回答就是 `{"proposals": []}`，
    此时 usage 非空、提案为空。而旧代码把它当成失败，
    于是在日志里报"生成失败"、在界面上报"有 N 个模块失败"，
    让用户以为漏审了 —— 实际上那一批已经审过了、结论是"没问题"。

    反过来，真正的失败（网关 402/超时/JSON 截断）在 `_call_json` 里是**抛异常**的，
    不需要靠 usage 猜。所以这里显式记一个 `ok` 标志。
    """

    module: str
    proposals: list[Proposal] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    ok: bool = True
    # 这个模块的业务流程图（Mermaid 源码）。界面上挂在每条提案里，
    # 让用户看得出"为什么要加/改这条"。
    flow: str = ""


def _fix_flow_direction(src: str) -> str:
    """把过长的 `LR` 流程图改成 `TD`。纯函数。

    ## 为什么要有这个兜底

    提示词里已经让模型按步骤数选方向（≤4 步用 LR，≥5 步用 TD），但**模型不总听**：
    实测 7 个节点的 `LR` 渲染出来是 1320x76 —— **17:1**，缩进 950px 宽的容器后
    每个节点只剩十几像素，文字根本看不清。图是给用户判断"为什么要改这条用例"用的，
    看不清就等于没有。

    方向是**第一行**的事，所以改起来只用动一行，不必让模型重画。

    ## 判据为什么用"箭头数"而不是"节点数"

    数节点要解析 ID 与标签，中文标签里还可能带 `-`、`|` 之类；
    而流程图的边数就是 `-->` 的个数，正则一数就准。一条链上 N 个节点有 N-1 条边，
    用边数当"步骤数"是等价的、且不会误判。
    """
    if not src.strip():
        return src
    lines = src.split("\n")
    # 找第一行非空、非注释的行（declaration 必须在那）
    head_i = -1
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("%%"):
            continue
        head_i = i
        break
    if head_i < 0:
        return src
    head = lines[head_i].strip()
    if not head.lower().startswith(("flowchart lr", "graph lr")):
        return src
    edges = len(re.findall(r"--+>|--+x|==+>|-\.->", src))
    if edges < 4:  # ≤4 步：LR 正好，扁扁的填满宽度
        return src
    lines[head_i] = re.sub(r"(?i)^(\s*(?:flowchart|graph))\s+LR", r"\1 TD", lines[head_i])
    return "\n".join(lines)


async def optimize_module(
    module: str,
    cases: list[dict],
    spec_text: str,
    accounts: list[dict] | None = None,
) -> ModuleResult:
    """跑一个模块。失败返回 `ok=False`，**不代表"没有提案"**（见 ModuleResult）。

    重试 3 次、退避递增。实测（2026-10-12）：并发下网关会丢请求（空回复），
    退避重试能把它们捡回来，代价只是那个模块慢几秒。
    """
    from app import specgen
    from app.spec_overview import mermaid_looks_renderable, normalize_mermaid

    user = build_prompt(module, cases, spec_text, accounts)
    known = {str(c.get("case_key")) for c in cases if c.get("case_key")}
    roles = {str(a.get("role")) for a in (accounts or []) if a.get("role")}
    last: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            data, usage = await specgen._call_json(_SYSTEM, user, max_tokens=4000, role="caseopt")
            # 流程图同样要过规范化与判定 —— 它和项目文档的流程图走**同一套**校验，
            # 否则同一个渲染器在这里会收到没清理过的源码，整张图炸掉。
            flow = ""
            raw_flow = str(data.get("flow") or "").strip() if isinstance(data, dict) else ""
            if raw_flow:
                cand = normalize_mermaid(raw_flow)
                if mermaid_looks_renderable(cand)[0]:
                    flow = _fix_flow_direction(cand)
            return ModuleResult(
                module=module,
                proposals=parse_proposals(data, known, module, roles),
                usage=usage or {},
                ok=True,
                flow=flow,
            )
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 3:
                log.debug("case_optimize: 模块『%s』第 %d 次失败，退避重试：%s", module, attempt, exc)
                await asyncio.sleep(2.5 * attempt)
    log.warning("case_optimize: 模块『%s』生成失败（已重试 3 次）：%s", module, last)
    return ModuleResult(module=module, ok=False)


async def optimize_project(
    groups: list[tuple[str, list[dict], str]],
    accounts: list[dict] | None = None,
) -> tuple[list[Proposal], dict, int, list[str], dict[int, str]]:
    """跑多个模块。返回 (全部提案, 合计 usage, 失败模块数, 失败模块名, id(提案)→模块名)。

    ★ **串行**，不并发（2026-10-12 实测改）。
    并发 2 时 12 个模块里报 7 个失败；串行 + 退避重试之后失败数降到 0。

    ★ 为什么返回失败模块**名字**：只说"有 3 个模块失败"，
    用户不知道是哪 3 个，也就无法判断"我需要关心吗"。点名才能让他去补。

    ★ 为什么用 `id(提案)→模块名` 而不是把模块名塞进 Proposal 字段：
    流程图是**按模块**生成的一张，写库时要按模块找回来。用 id() 当键是
    因为在同一个函数里刚创建这些对象、引用一定还在（不会因 GC 复用 id）。
    """
    usage = {"prompt": 0, "completion": 0, "cached": 0, "total": 0, "calls": 0}
    failed: list[str] = []
    mod_of: dict[int, str] = {}
    flat: list[Proposal] = []
    for g in groups:
        res = await optimize_module(*g, accounts)
        for k in usage:
            usage[k] += int((res.usage or {}).get(k, 0) or 0)
        if not res.ok:
            failed.append(res.module)
        for p in res.proposals:
            # 流程图存在 Proposal 上（同一个模块的每条提案共享同一张）
            p.flow = res.flow
            mod_of[id(p)] = res.module
        flat.extend(res.proposals)
        if len(flat) >= _MAX_PROPOSALS:
            break
        # 模块之间歇一下：连续打请求是空回复的主要触发条件。
        await asyncio.sleep(1.0)
    return flat[:_MAX_PROPOSALS], usage, len(failed), failed, mod_of



def group_cases_by_module(cases: list[dict], *, only_active: bool = True) -> list[tuple[str, list[dict]]]:
    """按模块分组。模块为空或占位符的归到「未分类」。

    只在 `only_active` 时收 active：对已作废的用例提"改/删"没有意义，
    而把它算进"冗余"里还会让 AI 误判重复（一条作废的和一条有效的内容相同）。
    """
    groups: dict[str, list[dict]] = {}
    for c in cases:
        if only_active and str(c.get("status") or "active") != "active":
            continue
        m = str(c.get("module") or "").strip() or "未分类"
        groups.setdefault(m, []).append(c)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def spec_text_for_module(doc_html: str, module: str) -> str:
    """从项目文档里抠出跟这个模块相关的段落。

    做法很土但**确定**：按标题切块，标题里包含模块名的块归给它；
    都没匹配上时退回整篇（截断）。用模型来"找相关段落"是又一次会失败的调用，
    而这里只是要缩小上下文 —— 土办法够用。
    """
    import re

    if not doc_html:
        return ""
    text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n@@\1@@\n", doc_html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    blocks = [b.strip() for b in text.split("@@") if b.strip()]
    key = (module or "").strip()
    if key:
        hits = [b for b in blocks if key in b]
        if hits:
            return "\n".join(hits)[:_MAX_SPEC_CHARS]
    return text.strip()[:_MAX_SPEC_CHARS]


def json_dumps(obj: object) -> str:
    """给日志用的小工具（保持中文可读）。"""
    return json.dumps(obj, ensure_ascii=False)[:2000]
