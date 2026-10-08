"""Tests for :mod:`app.spec_drift`.

The material is the real 2026-10-07 divergence: the spec said the trainee import needed
no approval, the implementation added one, and 18 cases asserted the old behaviour. Two
fake defects were filed before anyone noticed the actual problem.

So these tests are not synthetic coverage of a regex module — each case is here because
the failure it guards has already cost something once.
"""

from __future__ import annotations

import pytest

from app.spec_drift import (
    claims_gate,
    claims_no_gate,
    describe,
    find_all,
    find_contradictory_expectations,
    find_repeated_identical_failures,
    find_stale_cited_gate_cases,
)

# The measured instance, quoted from the v3.1 spec wording.
SPEC_SAYS_AUTO = "导入完成后无需审批，平台自动同步到在校学员库，状态直接变为理论"
SPEC_SAYS_GATE = "导入后需提交审批，审批通过才进入在校学员库，状态变为在训"

# Referenced by the table test below so the two are visibly the same list.
_CLAIM_TABLE = [
    ("导入后无需审批，平台自动同步", True, False),
    ("系统自动生成并生效", True, False),
    ("不需要审核，直接入库", True, False),
    ("无需任何操作即自动生效", True, False),
    ("非需审批即可生效", True, False),
    ("导入后需提交审批", False, True),
    ("审批通过后才进入在校学员库", False, True),
    ("待提交审批", False, True),
    ("需经审批人确认", False, True),
    ("需审核", False, True),
    ("审批节点不参与本用例", False, True),
    ("导入后无需审批自动同步；审批节点不参与本用例", True, True),
    ("背景：老版本无需审批，现已改为需提交审批", False, False),
]


def case(key: str, expected: str, name: str = "") -> dict:
    return {"case_key": key, "name": name or key, "expected": expected}


# ── the two claim readers ──────────────────────────────────────────────────────────


def test_no_gate_claim_is_recognised():
    assert claims_no_gate("导入后无需审批，平台自动同步")
    assert claims_no_gate("系统自动生成并生效")
    assert claims_no_gate("不需要审核，直接入库")


def test_gate_claim_is_recognised():
    assert claims_gate("导入后需提交审批")
    assert claims_gate("审批通过后才进入在校学员库")
    assert claims_gate("待提交审批")


def test_neutral_text_claims_neither():
    assert not claims_no_gate("导入 2 条学员")
    assert not claims_gate("导入 2 条学员")


@pytest.mark.parametrize(
    "text",
    [
        "背景：老版本无需审批，现已改为需提交审批",
        "说明：无审批环节",
        "前提：不需要审核",
    ],
)
def test_background_mentions_are_not_claims(text: str):
    """Describing the situation is not asserting the behaviour.

    Without this guard every case whose *notes* mentioned approval would drift on every
    run, and a check that cries wolf gets switched off after one noisy report.
    """
    assert not claims_no_gate(text)
    assert not claims_gate(text)


def test_a_case_asserting_both_sides_is_still_a_claim():
    """The guard keys on 背景/说明/前提, not on 'both words appear' — so a case that
    genuinely asserts auto-sync while mentioning approval elsewhere still reads as a
    no-gate claim, which is what the stale-doc case looks like."""
    assert claims_no_gate("导入后无需审批自动同步；审批节点不参与本用例")


# ★ The whole table, pinned.
#
# Every row here is a wording that appeared while writing this module or in the real spec
# text, and each one cost a round-trip: the Chinese has no word boundaries, so 「需审批」
# matched inside 「无需审批」 and every no-gate case also read as a gate case. That made
# the two lists identical, so the check reported a contradiction inside single sentences
# and called a consistent suite inconsistent — worse than having no check at all.
#
# Asserted as one table rather than as separate tests so the two columns are visibly
# opposite for each row; a change that fixes one side and breaks the other shows up here
# immediately instead of as a slow drift in production findings.
@pytest.mark.parametrize(
    "text,expect_no_gate,expect_gate",
    _CLAIM_TABLE,
)
def test_claim_detection_table(text: str, expect_no_gate: bool, expect_gate: bool):
    assert claims_no_gate(text) is expect_no_gate, f"no_gate 判定错: {text}"
    assert claims_gate(text) is expect_gate, f"gate 判定错: {text}"


def test_no_gate_and_gate_are_not_mirror_images():
    """A wording must not land in both buckets by accident.

    Double-counting is how 18 cases became 37 in the signal: every case matched both
    patterns, so each appeared on both sides and the counts were meaningless. The case
    count is what a reader uses to size the fix, so it has to be right.
    """
    both = [t for t, n, g in _CLAIM_TABLE if n and g]
    assert len(both) == 1, f"只有一条用例是刻意的双重表述，实际有 {len(both)} 条: {both}"


# ── self-contradiction: the strongest signal, needs no observation ─────────────────


def test_two_cases_asserting_opposite_gate_behaviour_are_flagged():
    """The suite disagreeing with itself is drift, whatever the system does.

    This is the check that needs no knowledge of the real behaviour at all — only the
    observation that both statements cannot be true.
    """
    cases = [
        case("TC-FLT-001", SPEC_SAYS_AUTO),
        case("TC-FLT-002", SPEC_SAYS_GATE),
    ]
    sigs = find_contradictory_expectations(cases)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.kind == "self_contradictory_gate"
    assert s.severity == "blocker"
    assert set(s.case_ids) == {"TC-FLT-001", "TC-FLT-002"}


def test_contradiction_lists_every_case_involved():
    """The value is in the *other* cases carrying the same stale claim.

    Whoever fixes the doc needs to know all 18, not the one that happened to fail first.
    """
    cases = [case(f"TC-{i:03d}", SPEC_SAYS_AUTO) for i in range(1, 19)]
    cases.append(case("TC-900", SPEC_SAYS_GATE))
    (s,) = find_contradictory_expectations(cases)
    assert len(s.case_ids) == 19
    assert "18条" in s.detail or "18 条" in s.detail


def test_a_suite_that_agrees_produces_nothing():
    cases = [
        case("TC-1", "导入后无需审批自动同步"),
        case("TC-2", "另一个场景也无需审批"),
    ]
    assert find_contradictory_expectations(cases) == []


# ★ Scoping per project, added after running against the real DB.
#
# The first version paired cases across projects and reported 96 contradictions. Every one
# was a flight-trainee case arguing with a training-resource case — two different systems
# that genuinely have different approval rules. A finding dismissible with "those are
# different products" is worse than no finding: it teaches people to ignore the output.
def test_different_projects_are_not_paired_with_each_other():
    cases = [
        {"case_key": "TC-007", "name": "飞行学员导入", "expected": SPEC_SAYS_AUTO, "project_id": 2},
        {"case_key": "TP-APR-003", "name": "资源审批", "expected": SPEC_SAYS_GATE, "project_id": 1},
    ]
    assert find_contradictory_expectations(cases) == []


def test_the_same_project_still_gets_flagged():
    cases = [
        {"case_key": "TC-007", "name": "a", "expected": SPEC_SAYS_AUTO, "project_id": 2},
        {"case_key": "TC-008", "name": "b", "expected": SPEC_SAYS_GATE, "project_id": 2},
    ]
    (s,) = find_contradictory_expectations(cases)
    assert set(s.case_ids) == {"2:TC-007", "2:TC-008"}
    assert "项目 2" in s.detail


def test_a_contradiction_within_each_project_yields_one_signal_each():
    cases = [
        {"case_key": "A1", "name": "", "expected": SPEC_SAYS_AUTO, "project_id": 1},
        {"case_key": "A2", "name": "", "expected": SPEC_SAYS_GATE, "project_id": 1},
        {"case_key": "B1", "name": "", "expected": SPEC_SAYS_AUTO, "project_id": 2},
        {"case_key": "B2", "name": "", "expected": SPEC_SAYS_GATE, "project_id": 2},
    ]
    sigs = find_contradictory_expectations(cases)
    assert len(sigs) == 2
    assert {len(s.case_ids) for s in sigs} == {2}


def test_case_keys_are_project_qualified():
    """`case_key` is unique only within a project.

    The real DB has 887 rows and 887 distinct keys, because two projects each have their
    own TC-007. A bare key made one project's case show up under another's id, so a reader
    chasing the finding opened the wrong case — and concluded the finding was nonsense.
    """
    (s,) = find_stale_cited_gate_cases(
        [
            {"case_key": "TC-007", "name": "飞行", "expected": "按需求规格说明书，" + SPEC_SAYS_AUTO, "project_id": 2},
            {"case_key": "TP-APR-003", "name": "资源", "expected": SPEC_SAYS_GATE, "project_id": 1},
        ],
        observed_requires_approval=True,
    )
    assert "2:TC-007" in s.case_ids
    assert "1:TP-APR-003" not in s.case_ids
    assert "项目 2" in s.detail


def test_an_empty_suite_is_not_a_contradiction():
    assert find_contradictory_expectations([]) == []
    assert find_contradictory_expectations(None) == []


# ── stale spec-cited expectations, against an observation ──────────────────────────


def test_stale_cases_are_flagged_only_when_the_run_observed_approval():
    """The observation is what turns "the doc might be wrong" into "it is wrong".

    Without it this is just a text search over the suite, and it would fire on any case
    whose wording matches regardless of what the system actually did.
    """
    cases = [case("TC-101", "按需求规格说明书，导入后无需审批自动同步")]
    assert find_stale_cited_gate_cases(cases, observed_requires_approval=False) == []
    (s,) = find_stale_cited_gate_cases(cases, observed_requires_approval=True)
    assert s.kind == "stale_spec_gate"
    assert s.severity == "blocker"
    assert s.case_ids == ("TC-101",)


def test_cases_not_citing_the_spec_are_not_called_stale():
    """A case written from observed behaviour that expects no approval is a legitimate
    question about the system, not a stale document.

    Flagging it would make the check argue with the tester, and the fix for that is to
    turn the check off.
    """
    cases = [case("TC-102", "导入后无需审批自动同步")]
    assert find_stale_cited_gate_cases(cases, observed_requires_approval=True) == []


def test_cases_that_cite_the_spec_and_expect_approval_are_fine():
    cases = [case("TC-103", "按需求规格说明书，导入后需提交审批")]
    assert find_stale_cited_gate_cases(cases, observed_requires_approval=True) == []


def test_a_case_citing_the_spec_with_no_gate_claim_is_not_stale():
    """No claim, nothing to be stale about — the citation alone proves nothing."""
    cases = [case("TC-104", "按需求规格说明书的长度限制，姓名不得超过 20 字")]
    assert find_stale_cited_gate_cases(cases, observed_requires_approval=True) == []


# ── repeated identical failures ────────────────────────────────────────────────────


def test_three_identical_failures_collapse_into_one_finding():
    """Three cases failing identically are one behaviour observed three times.

    Filing three tickets for one cause is how a report loses the reader's trust.
    """
    results = [
        {"case_key": f"TC-{i}", "status": "failed", "judge_reason": "导入后列表查不到该学员"}
        for i in (1, 2, 3)
    ]
    (s,) = find_repeated_identical_failures(results)
    assert s.kind == "repeated_identical_failure"
    assert len(s.case_ids) == 3
    assert "一个" in s.detail


def test_two_identical_failures_are_not_enough():
    """Two can be coincidence. Reporting them would be noise on every run."""
    results = [
        {"case_key": f"TC-{i}", "status": "failed", "judge_reason": "同样的话"} for i in (1, 2)
    ]
    assert find_repeated_identical_failures(results) == []


def test_passed_cases_never_join_a_failure_bucket():
    results = [
        {"case_key": f"TC-{i}", "status": "passed", "judge_reason": "同样的话"} for i in (1, 2, 3, 4)
    ]
    assert find_repeated_identical_failures(results) == []


def test_cases_with_no_reason_are_not_bucketed():
    """No reason means nothing to compare; grouping them would lump every timeout
    together under one meaningless heading."""
    results = [{"case_key": f"TC-{i}", "status": "error", "judge_reason": ""} for i in range(5)]
    assert find_repeated_identical_failures(results) == []


def test_error_status_counts_as_a_failure():
    """An errored case is still something the tester has to look at."""
    results = [
        {"case_key": f"TC-{i}", "status": "error", "error": "浏览器连接断开"} for i in (1, 2, 3)
    ]
    (s,) = find_repeated_identical_failures(results)
    assert len(s.case_ids) == 3


def test_several_distinct_failure_causes_each_get_their_own_signal():
    results = (
        [{"case_key": f"TC-{i}", "status": "failed", "judge_reason": "查不到学员"} for i in (1, 2, 3)]
        + [{"case_key": f"TC-{i}", "status": "failed", "judge_reason": "导出按钮无响应"} for i in (4, 5, 6)]
    )
    sigs = find_repeated_identical_failures(results)
    assert len(sigs) == 2
    # Largest group first — that is the one a reader should see.
    assert len(sigs[0].case_ids) == 3


# ── the aggregate ──────────────────────────────────────────────────────────────────


def test_find_all_orders_blockers_first():
    """The thing that invalidates the report outranks the thing that tidies it."""
    cases = [
        case("TC-1", SPEC_SAYS_AUTO),
        case("TC-2", SPEC_SAYS_GATE),
    ]
    results = [
        {"case_key": f"TC-{i}", "status": "failed", "judge_reason": "同一句话"} for i in (1, 2, 3)
    ]
    sigs = find_all(cases, results, observed_requires_approval=True)
    assert sigs
    assert sigs[0].severity == "blocker"


def test_find_all_is_empty_on_a_clean_run():
    cases = [case("TC-1", "导入 2 条后列表可见")]
    results = [{"case_key": "TC-1", "status": "passed", "judge_reason": ""}]
    assert find_all(cases, results) == []


def test_find_all_accepts_orm_like_objects():
    """Called from the API with rows, from reports with dicts. A signature that only took
    one shape would push this branch into three call sites."""
    class Row:
        def __init__(self, k):
            self.case_key = k
            self.name = k
            # A citation, so the stale-spec check (not the contradiction check) fires —
            # these rows all agree with each other.
            self.expected = "按需求规格说明书，导入后无需审批自动同步"

    sigs = find_all([Row("TC-1"), Row("TC-2")], None, observed_requires_approval=True)
    assert sigs, "ORM 行读不出字段，等于功能对 ORM 静默失效"
    assert sigs[0].kind == "stale_spec_gate"
    assert set(sigs[0].case_ids) == {"TC-1", "TC-2"}


def test_describe_is_honest_about_finding_nothing():
    assert "未发现" in describe([])


def test_describe_leads_with_the_count():
    sigs = find_all(
        [case("TC-1", SPEC_SAYS_AUTO), case("TC-2", SPEC_SAYS_GATE)], None
    )
    text = describe(sigs)
    assert "1 处" in text
    assert "blocker" not in text  # Chinese output, not an internal verdict name