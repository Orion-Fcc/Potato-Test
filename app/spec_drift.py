"""Detect when the system stops matching the spec the cases were written against.

What this exists for
---------------------
Measured on 2026-10-07: the requirements spec (v3.1 §4019/4015/4029) said the trainee
import path needed **no approval and synced automatically**. The implementation had
changed — an approval step was introduced, and a record only becomes real once it is
approved. So the cases were asserting behaviour the system deliberately no longer had.

What happened next is the part that matters: the agent dutifully reported "the record
did not appear", which reads exactly like data loss. Two fake defects got filed. The
real finding was one sentence long: *the spec and the implementation disagree*.

That class of finding is the one a re-run cannot produce and an agent will never
volunteer, because from inside a single case both behaviours look normal. It needs the
cases to be read **against each other**, which is why it belongs here and not in the
executor.

Why it is a pure function first
-------------------------------
Everything below operates on text and structured rows, with no DB and no browser, for the
same reason as :mod:`app.failure_attrib` and :mod:`app.page_gate`: the interesting part
(the rule that says "this is drift, not a bug") is the part worth testing exhaustively,
and it must be testable without a 10-minute browser session.

What it deliberately does not do
--------------------------------
It does not change any case and it does not touch the spec. It reports. Automating the
edit is exactly the wrong instinct: whether the spec moved or the implementation regressed
is a question for a person who can talk to the developer, and guessing wrong rewrites 18
cases that were right.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Phrases that mean "the expectation was written from the document, not from the system".
#
# These must be **assertion** phrasings. If they included neutral ones like "无需审批" on
# its own, every case mentioning approval would drift on every run.
_SPEC_CITED = re.compile(
    r"(按(照)?(需求|规格|需规|文档|设计)|文档(要求|规定)|规格(要求|规定)|"
    r"需求(要求|规定)|按需规|依据(需求|规格))",
    re.IGNORECASE,
)

# A claim that something happens **with no human step in the loop**. This is the shape the
# 2026-10-07 drift took: "无需审批、平台自动同步".
_NO_GATE_CLAIM = re.compile(
    r"(无需(审批|审核|确认|提交)|不需要(审批|审核|确认|提交)|"
    r"(自动|无需任何操作即)(同步|生成|生效|入库|完成)|"
    r"免审批|直接(同步|生成|生效|入库)|"
    # 「非需审批即可生效」 — the spec's way of saying the same thing. Found by the table
    # check rather than imagined: without it a correctly-worded no-gate case reads as a
    # gate case, and the two sides swap places.
    r"非需[^，。；]{0,6}即可|"
    r"即可(同步|生成|生效|入库))",
    re.IGNORECASE,
)

# The same behaviour seen **after** a human step. Presence of this in the same case is the
# contradiction: one of the two statements is wrong.
#
# ★ No bare 「审批」 in here. An earlier version had it, and it matched the 审批 inside
# 「无需审批」 — so every "no approval needed" case also read as "requires approval", and
# the two lists came out identical. That is worse than no check: it reports a
# contradiction inside single sentences and calls a consistent suite inconsistent.
# 「审批」 only counts when something else in the phrase makes it a step:
# 需/要/待/提交/通过/人/节点/环节.
# ★ Two exclusions the Chinese wording forces, both found by a failing test:
#
#  1. No bare 「审批」 — it matches the 审批 inside 「无需审批」.
#  2. 「需」 must not be the tail of 「无需」/「不需要」. Chinese has no word boundaries,
#     so `需审批` matches inside `无需审批` and every "no approval needed" case also read
#     as "requires approval". A negative lookbehind is the only thing that expresses
#     "this 需 is not part of 不需".
_GATE_CLAIM = re.compile(
    r"(?<!不)(?<!无)(?<!别)(?<!未)(?<!毋)(?<!免)(?<!非)"
    r"(需(要|经|提交)(审批|审核|确认|提交)"
    r"|需(审批|审核|确认)"
    r"|待提交审批|提交审批"
    r"|审批(通过|人|节点|环节|中|后)"
    r"|经审批|审批后|审批中)",
    re.IGNORECASE,
)

# Wording that must never be read as an assertion — describing the situation is not
# asserting the behaviour. This is the false-positive guard: a case titled "无需审批场景
# 下应正常入库" is asserting, but one whose *expected result* explains the background is
# not.
#
# ★ 「说明书」 must not match 「说明」. `按需求规格说明书，导入后…` is a citation — the
# single most common form in this suite — and the bare word 说明 swallowed it, so every
# properly-cited case was classified as background and skipped. Found by a failing test;
# the tell was that `cited_spec_expectations` said True while `claims_no_gate` said False
# on the same string, which is a contradiction no real sentence can be.
_BACKGROUND_CLAIM = re.compile(
    r"(背景|前提|现状|目前|当前实现|历史上)"
    r"|说明(?!书|明[，,。；;])"
    r"|注：|注:",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DriftSignal:
    """One suspected spec/implementation divergence.

    ``case_ids`` is the set of cases that carry the claim, not just the one that failed —
    the value of this check is that it finds the *other* 17 cases asserting the same
    stale behaviour, which nobody would have noticed until each one failed in turn.
    """

    kind: str
    detail: str
    case_ids: tuple[str, ...] = ()
    evidence: str = ""
    severity: str = "warn"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "case_ids": list(self.case_ids),
            "evidence": self.evidence,
            "severity": self.severity,
        }


def _rows(cases: Any) -> list[dict[str, Any]]:
    """Accept dicts, ORM objects, or anything with the three fields we read.

    Kept permissive on purpose: this gets called from the API with ORM rows, from a
    report with dicts, and from tests with literals. A signature that only accepts one
    shape would push that branching into three call sites instead of here.
    """
    out: list[dict[str, Any]] = []
    for c in cases or ():
        if isinstance(c, dict):
            get = c.get
        else:
            get = lambda k, _c=c: getattr(_c, k, "")  # noqa: E731
        key = str(get("case_key") or get("id") or "")
        pid = get("project_id")
        out.append(
            {
                # The primary key, kept for joining. See detect_observed_approval: case_key
                # is nullable and collides across projects, case_id is neither.
                "id": get("id"),
                # ★ Project-qualified. `case_key` is only unique *within* a project — the
                # real DB has two projects whose keys collide (TC-007 exists in both, 887
                # rows / 887 distinct keys). A bare key made one project's cases show up
                # under another's id, so a reader chasing the finding opened the wrong
                # case and concluded the finding was nonsense.
                "case_key": f"{pid}:{key}" if pid not in (None, "", 0) else key,
                "name": str(get("name") or ""),
                "expected": str(get("expected") or ""),
            }
        )
    return out


def cited_spec_expectations(text: str) -> bool:
    """True when ``text`` reads like it was written from the document."""
    return bool(_SPEC_CITED.search(text or ""))


def claims_no_gate(text: str) -> bool:
    """True when ``text`` asserts something happens with no approval step."""
    return _asserts(text, _NO_GATE_CLAIM)


def claims_gate(text: str) -> bool:
    """True when ``text`` asserts something requires an approval step."""
    return _asserts(text, _GATE_CLAIM)


# A claim that is immediately undone: 「无需审批，审批节点不参与本用例」.
#
# ★ This guard is what stops one case from landing in both buckets. It was found by a
# test, not by reading: 18 cases asserting the stale auto-sync behaviour were counted as
# 37 instead of 19, because every one of them mentioned approval somewhere in order to
# rule it out. A signal that double-counts its own evidence is worse than no signal — the
# case count is the part a reader uses to decide how much work fixing this is.
_NEGATED = re.compile(
    r"(无需|不需要|不用|不必|不再|不涉及|不参与|不经过|不经过审批)[^，。；]{0,12}"
    r"(审批|审核|确认|提交)"
    r"|"
    r"(审批|审核|确认|提交)[^，。；]{0,12}(不参与|不涉及|不经过|不生效|无关|跳过|不需要|无需)",
    re.IGNORECASE,
)


def _asserts(text: str, pattern: re.Pattern[str]) -> bool:
    """``pattern`` matched **and** not negated.

    Background mentions (背景/说明/前提) are excluded outright because they describe
    rather than assert; negation is checked per-match because a case can legitimately
    mention approval in order to exclude it, and that is the commonest form here.

    ★ The negator's own span is removed from the window before matching. Otherwise the
    negator cancels itself: 「无需审批」 matches ``_NO_GATE_CLAIM``, then the surrounding
    window still contains 「无需」 and ``_NEGATED`` matches, so the assertion is silently
    dropped. That turned every genuine "no approval needed" case into silence — the check
    then reports nothing at all, which is the failure mode that gets a feature switched
    off without anyone noticing why.
    """
    body = text or ""
    if _BACKGROUND_CLAIM.search(body):
        return False
    for m in pattern.finditer(body):
        start = max(0, m.start() - 16)
        end = min(len(body), m.end() + 16)
        window = body[start:end]
        # Blank out the negators so they cannot match inside their own window, then look
        # for one *outside* the matched span — which is what "审批节点不参与本用例" is.
        window = _NEGATED.sub(lambda x: " " * len(x.group(0)), window)
        if _NEGATED.search(window):
            continue
        return True
    return False


def find_contradictory_expectations(cases: Any) -> list[DriftSignal]:
    """Groups of cases that assert opposite behaviour **within the same project**.

    Two cases whose expectations disagree about whether approval is needed cannot both be
    right, whatever the system does. That is the strongest drift signal available without
    talking to anyone, because it does not depend on guessing what the system does — only
    on noticing that the suite disagrees with itself.

    ★ Scoped per project. This suite holds two unrelated systems (flight trainees, training
    resources) and they genuinely have different approval rules, so a cross-project pairing
    reported 96 contradictions where there were none — every finding was a case from one
    system arguing with a case from the other. A finding that can be dismissed by "those
    are different products" is worse than no finding, because it trains people to ignore
    the output.
    """
    by_project: dict[str, list[dict[str, Any]]] = {}
    for r in _rows(cases):
        pid = r["case_key"].split(":", 1)[0] if ":" in r["case_key"] else ""
        by_project.setdefault(pid, []).append(r)

    signals: list[DriftSignal] = []
    bridge: list[dict[str, Any]] = []
    for pid, rows in by_project.items():
        no_gate = [r for r in rows if claims_no_gate(r["expected"])]
        gate = [r for r in rows if claims_gate(r["expected"])]
        if not (no_gate and gate):
            continue

        # ★ Cases that state BOTH are not contradictions — they are describing a flow
        # that has both. 「审批通过后自动生成」 uses 自动 while still requiring approval,
        # and it is exactly right: 自动 means nobody types the data in, not that nobody
        # approves it. Counting these on both sides inflated the contradiction and made
        # the signal impossible to act on (10 of 85 in the real suite).
        #
        # They are still worth reporting — a case saying 「须经审批通过后才进入」 next to
        # one saying 「无需审批即自动同步」 is precisely where a spec edit left a mix — so
        # they get their own line rather than being dropped.
        overlapping = {r["case_key"]: r for r in no_gate if any(g["case_key"] == r["case_key"] for g in gate)}
        clean_no_gate = [r for r in no_gate if r["case_key"] not in overlapping]
        clean_gate = [r for r in gate if r["case_key"] not in overlapping]
        bridge.extend(overlapping.values())

        if not (clean_no_gate and clean_gate):
            continue
        a = "、".join(sorted(r["case_key"] for r in clean_no_gate)[:4])
        b = "、".join(sorted(r["case_key"] for r in clean_gate)[:4])
        total = len(clean_no_gate) + len(clean_gate)
        more = total - 8
        where = f"（项目 {pid}）" if pid else ""
        signals.append(
            DriftSignal(
                kind="self_contradictory_gate",
                detail=(
                    f"{len(clean_no_gate)} 条用例断言「无需审批/自动生效」，"
                    f"另有 {len(clean_gate)} 条断言「需审批」，两者不可能同时成立"
                    + where
                    + (f"（另有 {more} 条未列出）" if more > 0 else "")
                ),
                case_ids=tuple(sorted([r["case_key"] for r in clean_no_gate + clean_gate])),
                evidence=f"无需审批侧: {a} ｜ 需审批侧: {b}",
                severity="blocker",
            )
        )

    if bridge:
        signals.append(
            DriftSignal(
                kind="mixed_auto_and_gate",
                detail=(
                    f"{len(bridge)} 条用例同时提到「自动生成」与「需审批」"
                    "—— 需规改动很可能就落在这里，建议逐条确认措辞"
                ),
                case_ids=tuple(sorted(r["case_key"] for r in bridge)),
                evidence="；".join(f"{r['case_key']}: {r['expected'][:60]}" for r in bridge[:3]),
                severity="warn",
            )
        )
    return signals


def detect_observed_approval(cases: Any, results: Any) -> bool:
    """Did this run actually walk into an approval step the cases said was not there?

    This is the observation half of :func:`find_stale_cited_gate_cases`, and it is derived
    from the run rather than passed in by a person on purpose. The 2026-10-07 drift was
    found by a human reading a run; if the flag is a checkbox in the UI then it only gets
    set on the day somebody is already looking, and the check is decorative.

    The join is per case, deliberately:

      * the result's own text mentions an approval step (``claims_gate`` — the same reader
        the expectations use, so 「无需审批」 in the narration does not read as one), and
      * **that case's** expectation says there is none.

    A run-wide "did anything mention approval" would fire on any suite, because a suite
    about approvals mentions approvals in the cases that legitimately have them. Pairing
    per case is the only way the observation means anything: this case said no gate, and
    this case's run hit one.

    Returns a bool because that is what the detector consumes. ``False`` for a run with
    no results is not "no drift" — it is "no evidence", and the caller should treat the
    stale check as simply not applicable.
    ★ The join is on ``case_id``, not on ``case_key``. This was found by a failing test, and
    the reason it matters is that ``case_key`` is the one field here that can be NULL, can
    collide across projects (two systems both have a ``TC-007``), and needs a project prefix
    before it is unique — three ways to silently fail to match. ``case_id`` is the primary
    key of the table the result came from, so it cannot. ``case_key`` stays as a fallback
    for callers holding reports rather than ORM rows.
    """
    rows = _rows(cases)
    by_id = {r["id"]: r for r in rows if r["id"] is not None}
    by_key = {r["case_key"]: r for r in rows}
    for res in results or ():
        get = res.get if isinstance(res, dict) else lambda k, _r=res: getattr(_r, k, "")
        status = str(get("status") or "")
        if status == "passed":
            continue
        rid = get("case_id")
        row = by_id.get(rid) if rid is not None else None
        if row is None:
            key = get("case_key")
            pid = get("project_id")
            qualified = f"{pid}:{key}" if pid not in (None, "", 0) else str(key or "")
            row = by_key.get(qualified) or by_key.get(str(key or ""))
        if row is None or not claims_no_gate(row["expected"]):
            continue
        said = " ".join(
            str(get(f) or "") for f in ("judge_reason", "final_answer", "error")
        )
        if said.strip() and claims_gate(said):
            return True
    return False


def find_stale_cited_gate_cases(
    cases: Any, observed_requires_approval: bool
) -> list[DriftSignal]:
    """Cases that cite the spec *and* assert no approval, against observed behaviour.

    ``observed_requires_approval`` comes from the run: at least one case reached the
    approval step in the real system. That is the observation that turns "the doc might be
    wrong" into "the doc **is** wrong about this case".

    Only spec-cited cases are flagged. A case written from observed behaviour that happens
    to expect no approval is a legitimate question about the system, not a stale document;
    conflating the two would produce findings nobody can act on.
    """
    if not observed_requires_approval:
        return []

    stale = [
        r
        for r in _rows(cases)
        if cited_spec_expectations(r["expected"])
        and claims_no_gate(r["expected"])
        and not claims_gate(r["expected"])
    ]
    if not stale:
        return []
    ids = tuple(sorted(r["case_key"] for r in stale))
    projects = {i.split(":", 1)[0] for i in ids if ":" in i}
    where = f"（项目 {'、'.join(sorted(projects))}）" if projects else ""
    return [
        DriftSignal(
            kind="stale_spec_gate",
            detail=(
                f"{len(stale)} 条用例引用需规断言「无需审批/自动生效」{where}，"
                "但本轮实测走到了审批环节 —— 需规与实现已经不一致"
            ),
            case_ids=ids,
            evidence="；".join(f"{r['case_key']}: {r['expected'][:60]}" for r in stale[:3]),
            severity="blocker",
        )
    ]


def find_repeated_identical_failures(results: Any, min_count: int = 3) -> list[DriftSignal]:
    """Several cases failing the same way is a statement about the system, not a case.

    Three cases that each fail on "查不到刚导入的学员" are not three bugs; they are one
    behaviour observed three times, and the reporter should say so instead of filing three
    tickets. Threshold is 3 because two can be coincidence but three rarely is.

    ``results`` rows need ``status``/``judge_reason``/``case_key``.
    """
    buckets: dict[str, list[str]] = {}
    for r in results or ():
        get = r.get if isinstance(r, dict) else lambda k, _r=r: getattr(_r, k, "")
        status = str(get("status") or "")
        if status == "passed":
            continue
        reason = (str(get("judge_reason") or get("error") or "")).strip()
        if not reason:
            continue
        key = str(get("case_key") or get("case_id") or get("id") or "")
        pid = get("project_id")
        buckets.setdefault(reason[:160], []).append(
            f"{pid}:{key}" if pid not in (None, "", 0) else key
        )

    signals: list[DriftSignal] = []
    for reason, ids in buckets.items():
        if len(ids) < min_count:
            continue
        signals.append(
            DriftSignal(
                kind="repeated_identical_failure",
                detail=f"{len(ids)} 条用例以完全相同的方式失败，应作为**一个**问题上报",
                case_ids=tuple(sorted(ids)),
                evidence=reason[:200],
                severity="warn",
            )
        )
    return sorted(signals, key=lambda s: -len(s.case_ids))


def find_all(
    cases: Any,
    results: Any = None,
    *,
    observed_requires_approval: bool | None = None,
    suite_cases: Any = None,
) -> list[DriftSignal]:
    """Every drift signal we can establish from a run, most severe first.

    ``cases`` are the cases this run covered. ``suite_cases`` is the whole project's case
    list, and it exists because of a timing fact: the self-contradiction signal compares
    cases **against each other**, so a 5-case run out of 501 cannot see it — the case
    asserting 「无需审批」 and the case asserting 「需审批」 have to both be in hand. The
    caller that has the project at hand should pass it; without it the check degrades to
    "did *these* cases disagree", which on a small sample is silence rather than a clean
    bill of health.

    ``observed_requires_approval=None`` (the default) means "derive it from the run" via
    :func:`detect_observed_approval`. Pass a bool only to force the answer — a suite
    scanned outside a run has no results to derive from, and there the honest input is
    "I watched it happen", not a default of False.
    """
    if observed_requires_approval is None:
        observed_requires_approval = detect_observed_approval(cases, results)
    pool = suite_cases if suite_cases is not None else cases
    signals: list[DriftSignal] = []
    signals.extend(find_contradictory_expectations(pool))
    signals.extend(
        find_stale_cited_gate_cases(cases, observed_requires_approval)
    )
    signals.extend(find_repeated_identical_failures(results))
    order = {"blocker": 0, "warn": 1}
    return sorted(signals, key=lambda s: (order.get(s.severity, 2), s.kind))


def describe(signals: list[DriftSignal]) -> str:
    """One-line summary for the run log."""
    if not signals:
        return "未发现需规与实现的不一致"
    blockers = sum(1 for s in signals if s.severity == "blocker")
    return (
        f"发现 {len(signals)} 处疑似需规漂移"
        + (f"（其中 {blockers} 处会直接影响结论可信度）" if blockers else "")
    )