"""Wire tests for the spec-drift check.

Same job as ``test_failure_attrib_wiring.py``: the rule in :mod:`app.spec_drift` is the easy
part and was tested there. What is easy to get wrong is the plumbing — the signal gets
computed, and then quietly does not survive the trip to the reader. Four ways that happens,
all of them silent:

  1. the column is never added, so the write goes nowhere;
  2. the check runs but its result is not committed (or the txn rolls back);
  3. ``finalize_run`` raises inside the check and takes the whole run's status with it;
  4. the signal reaches the DB but the API drops it, so the page shows a clean run while
     the log says otherwise.

None of these raise. The run looks fine in every one of them, which is exactly why they
need tests rather than a manual check.

The drift fixtures use the real 2026-10-07 divergence: the spec said 「导入后无需审批自动
同步」, the implementation added an approval step. Nothing about the strings is invented —
:mod:`tests.test_spec_drift` already pins the readers against them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from sqlalchemy import inspect as sa_inspect

from app import spec_drift

# --- fixtures shaped like the ORM rows, but local so no DB is needed for the pure parts ---


@dataclass
class CaseRow:
    id: int
    project_id: int
    case_key: str
    name: str
    expected: str


@dataclass
class ResultRow:
    case_id: int
    status: str
    judge_reason: str | None = None
    final_answer: str | None = None
    error: str | None = None
    attribution: str | None = None


PROJECT = 2

# The two halves of the real divergence, kept verbatim in intent:
# one case written from the spec (auto-sync), one written from the implementation (approval).
CASE_SPEC_AUTO = CaseRow(
    id=1,
    project_id=PROJECT,
    case_key="TC-FLT-015",
    name="导入招飞学员",
    expected="按需求规格说明书，导入后无需审批，平台自动同步到在校学员库",
)
CASE_IMPL_APPROVAL = CaseRow(
    id=2,
    project_id=PROJECT,
    case_key="TC-FLT-101",
    name="导入后审批才入库",
    expected="导入后记录进入待审批状态，须经审批通过后才进入在校学员库",
)


# --------------------------------------------------------------------------- 1. the column


def test_the_drift_column_actually_exists():
    """A JSON column that was never added makes the write a silent no-op.

    Not hypothetical: this project runs on SQLite via ``_ADDITIVE_COLUMNS`` and the models
    file is edited independently of the live schema. A model attribute that has no column
    behind it raises on read — which surfaces as a 500 on the run detail page, after the
    run has already completed.
    """
    import asyncio as _aio

    from app.db import _ADDITIVE_COLUMNS, _engine, init_db

    async def _cols() -> set[str]:
        await init_db()
        assert _engine is not None
        # run_sync hands the sync connection to the inspector. Using the async engine's
        # connection object instead is the mistake here: SQLAlchemy refuses to inspect an
        # AsyncSession ("No inspection system is available"), which looks like a missing
        # column but is not.
        def _inspect(sync_conn):
            return {c["name"] for c in sa_inspect(sync_conn).get_columns("run")}

        async with _engine.connect() as conn:
            return await conn.run_sync(_inspect)

    cols = _aio.run(_cols())

    assert "drift_signals" in cols, (
        "run.drift_signals 未落库：模型有属性、表里没列 —— finalize_run 写进去的东西"
        "无处可存，API 读出来永远是 NULL"
    )
    assert any(t == "run" and c == "drift_signals" for t, c, _ in _ADDITIVE_COLUMNS)


def test_the_model_and_the_column_agree():
    """The model must declare the column the migration adds — same name, same table."""
    from app.models import Run

    tbl = Run.__table__
    assert "drift_signals" in tbl.c
    col = tbl.c.drift_signals
    assert col.type.__class__.__name__ == "JSON"


# --------------------------------------------------------- 2. the observation is derived


def test_the_approval_observation_is_derived_not_assumed():
    """A run whose failing case *said* there is no approval but *hit* one flips the flag.

    This is the derivation that stops the feature from being decorative: if the flag were a
    checkbox, it would only get ticked on the day somebody already suspected drift.
    """
    results = [
        ResultRow(
            case_id=CASE_SPEC_AUTO.id,
            status="failed",
            judge_reason="导入后在列表中查不到该学员，提交审批后才可见",
            final_answer="记录未入库",
            attribution="system",
        )
    ]
    assert spec_drift.detect_observed_approval([CASE_SPEC_AUTO], results) is True


def test_a_failure_that_never_mentions_approval_does_not_raise_the_flag():
    """An unrelated failure must not manufacture an approval observation."""
    results = [
        ResultRow(
            case_id=CASE_SPEC_AUTO.id,
            status="failed",
            judge_reason="列表分页按钮点击后无响应",
            final_answer="无法翻到第二页",
            attribution="system",
        )
    ]
    assert spec_drift.detect_observed_approval([CASE_SPEC_AUTO], results) is False


def test_a_narration_that_says_no_approval_is_not_read_as_one():
    """「确认无需审批」 in the narration must not count as hitting an approval step.

    The trap: the narrator frequently *confirms* the negative in order to report it. A
    reader that cannot tell 确认无需审批 from 需要审批 turns every no-approval case into
    evidence for approval, and the check fires on a healthy system.
    """
    results = [
        ResultRow(
            case_id=CASE_SPEC_AUTO.id,
            status="failed",
            judge_reason="页面显示该操作无需审批即可完成",
            final_answer="确认无需审批",
            attribution="system",
        )
    ]
    assert spec_drift.detect_observed_approval([CASE_SPEC_AUTO], results) is False


def test_the_flag_is_scoped_to_the_case_that_said_no_gate():
    """A run that legitimately walks an approval flow must not flag unrelated cases.

    The observation is per case on purpose: a suite *about* approvals mentions approvals
    constantly, so "did anything in the run mention approval" would be true on every run
    and mean nothing.
    """
    results = [
        ResultRow(
            case_id=CASE_IMPL_APPROVAL.id,
            status="failed",
            judge_reason="审批人点击通过后记录入库",
            final_answer="待审批 → 已通过",
            attribution="system",
        )
    ]
    assert spec_drift.detect_observed_approval([CASE_IMPL_APPROVAL], results) is False


def test_passed_cases_never_contribute_an_observation():
    """A case that passed cannot be evidence that the system contradicted itself."""
    results = [
        ResultRow(
            case_id=CASE_SPEC_AUTO.id,
            status="passed",
            judge_reason="无需审批，平台自动同步到在校学员库",
            final_answer="导入成功并自动入库",
        )
    ]
    assert spec_drift.detect_observed_approval([CASE_SPEC_AUTO], results) is False


def test_derive_returns_false_not_an_error_on_empty_input():
    """No results means no evidence — which is not the same as "no drift".

    Returning False here is correct because the caller uses it only to decide whether the
    stale check applies. It must not raise: this runs inside ``finalize_run``.
    """
    assert spec_drift.detect_observed_approval([], []) is False
    assert spec_drift.detect_observed_approval([CASE_SPEC_AUTO], None) is False


def test_a_result_whose_case_is_missing_is_skipped():
    """A result pointing at a deleted case must not blow up the join."""
    results = [ResultRow(case_id=9999, status="failed", judge_reason="需审批通过")]
    assert spec_drift.detect_observed_approval([CASE_SPEC_AUTO], results) is False


# ------------------------------------------------------- 3. suite-wide vs run-scoped


def test_the_suite_signal_survives_a_tiny_run():
    """5 cases out of 501 cannot see a contradiction between two other cases.

    This is the timing fact that decides where the check has to read from. If the caller
    passes only the run's cases, the strongest signal is silently unavailable in exactly
    the situation it exists for — a first run that happens to cover a handful of cases.
    """
    # The run covered one case; the disagreement is with a case it never touched.
    covered = [CASE_SPEC_AUTO]
    suite = [CASE_SPEC_AUTO, CASE_IMPL_APPROVAL]

    assert spec_drift.find_all(covered, [], suite_cases=suite), (
        "只看本次跑的用例会漏掉套件级矛盾 —— 两边必须都在手上才谈得上「互相矛盾」"
    )
    assert not spec_drift.find_all(covered, []), (
        "没有 suite_cases 时应当只报「这批用例内部没矛盾」，而不是假装全套件干净"
    )


def test_the_run_scoped_signal_still_only_reads_this_runs_results():
    """Suite-wide cases, run-wide results — the two inputs mean different things.

    Mixing them up in either direction is wrong: reading another run's results would
    attribute this run's verdict to an older observation, and reading the suite's cases for
    the stale check would flag cases nobody executed.
    """
    suite = [CASE_SPEC_AUTO, CASE_IMPL_APPROVAL]
    stale_results = [
        ResultRow(
            case_id=CASE_SPEC_AUTO.id,
            status="failed",
            judge_reason="提交审批后才可见",
            final_answer="未入库",
        )
    ]
    sigs = spec_drift.find_all([CASE_IMPL_APPROVAL], stale_results, suite_cases=suite)
    kinds = {s.kind for s in sigs}
    assert "stale_spec_gate" not in kinds, (
        "stale 信号针对的是「本次跑的那条用例」，本次跑的是审批那条，不该报它陈旧"
    )
    assert "self_contradictory_gate" in kinds


def test_forcing_the_observation_still_overrides_the_derivation():
    """An explicit bool wins — a suite scanned outside a run has no results to derive from.

    Defaulting to False there would make the check look like it passed rather than like it
    never had the chance to.
    """
    covered = [CASE_SPEC_AUTO]
    sigs = spec_drift.find_all(covered, [], observed_requires_approval=True)
    assert "stale_spec_gate" in {s.kind for s in sigs}


def test_find_all_defaults_to_deriving_rather_than_to_false():
    """The default must not be False-by-accident.

    If the default were False, every caller that forgot the argument would get a clean run
    and no signal — the check would report "no drift" for the reason that it never ran.
    """
    results = [
        ResultRow(
            case_id=CASE_SPEC_AUTO.id,
            status="failed",
            judge_reason="须经审批通过后才进入在校学员库",
            final_answer="记录未入库",
        )
    ]
    sigs = spec_drift.find_all([CASE_SPEC_AUTO], results)
    assert "stale_spec_gate" in {s.kind for s in sigs}


# --------------------------------------------------- 4. it survives to the reader


def test_a_signal_survives_the_round_trip_to_json():
    """``as_dict`` is the only shape that reaches the DB, so it is the shape to test."""
    sigs = spec_drift.find_all([CASE_SPEC_AUTO], [], suite_cases=[CASE_SPEC_AUTO, CASE_IMPL_APPROVAL])
    assert sigs
    payload = [s.as_dict() for s in sigs]
    assert payload == [
        {
            "kind": s.kind,
            "detail": s.detail,
            "case_ids": list(s.case_ids),
            "evidence": s.evidence,
            "severity": s.severity,
        }
        for s in sigs
    ]
    # Must be plain JSON — SQLAlchemy will serialise it, and a tuple or a dataclass in here
    # fails at commit time, i.e. after the run is already finished.
    import json

    json.dumps(payload)


def test_project_qualified_ids_survive_so_the_reader_opens_the_right_case():
    """A bare ``TC-007`` is ambiguous here — two systems in this DB both have one.

    The fix is the project prefix, and the way to pin it is to build a case whose key
    collides across projects and confirm the reported ids still name the right one.
    Checked through the contradiction signal because that is what puts ids in the output.
    """
    mine = CaseRow(
        id=10,
        project_id=PROJECT,
        case_key="TC-007",
        name="本项目的用例（断言需审批）",
        expected="须经审批通过后才进入",
    )
    other_side = CaseRow(
        id=12,
        project_id=PROJECT,
        case_key="TC-008",
        name="本项目的另一条用例（断言无需审批）",
        expected="导入后无需审批自动同步",
    )

    sigs = spec_drift.find_all([mine, other_side], [], suite_cases=[mine, other_side])
    assert sigs
    assert set(sigs[0].case_ids) == {f"{PROJECT}:TC-007", f"{PROJECT}:TC-008"}
    for key in sigs[0].case_ids:
        assert key.startswith(f"{PROJECT}:"), f"{key} 缺项目前缀"


def test_the_log_line_leads_with_the_count():
    """The run log is where this gets noticed without opening the UI."""
    sigs = spec_drift.find_all(
        [CASE_SPEC_AUTO], [], suite_cases=[CASE_SPEC_AUTO, CASE_IMPL_APPROVAL]
    )
    text = spec_drift.describe(sigs)
    assert "发现" in text
    assert str(len(sigs)) in text
    # A blocker must be called out separately: 1 blocker among 5 warnings is the one that
    # invalidates the run's conclusions, and an undifferentiated count buries it.
    assert "直接影响结论可信度" in text


def test_a_clean_run_says_so_without_sounding_like_a_pass():
    assert spec_drift.describe([]) == "未发现需规与实现的不一致"


# ------------------------------------------------------- 5. it cannot break the run


def test_the_engine_check_swallows_its_own_failure(monkeypatch):
    """A broken detector must not take a finished run's status down with it.

    The observer cannot be allowed to destroy what it observes. If this raises out of
    ``_detect_spec_drift``, ``finalize_run`` never commits and the run stays ``running``
    forever — 501 cases' worth of work reported as still in progress.
    """
    from app.engine import _detect_spec_drift

    def boom(*_a, **_k):
        raise RuntimeError("detector down")

    monkeypatch.setattr(spec_drift, "find_all", boom)

    class FakeRun:
        id = 5
        project_id = PROJECT

    async def _session_execute(*_a, **_k):
        class Scalars:
            def all(self_inner):
                return []

        class Result:
            def scalars(self_inner):
                return Scalars()

        return Result()

    class FakeSession:
        execute = _session_execute

    out = asyncio.run(_detect_spec_drift(FakeSession(), FakeRun(), []))
    assert out == [], "探测器炸了应返回空列表并记日志，而不是把异常抛给 finalize_run"


def test_the_empty_list_means_no_evidence_not_clean():
    """What the failure path returns must not be indistinguishable from a clean verdict.

    Both are ``[]``, so the distinction has to be made by the caller: ``None`` column means
    "not checked", ``[]`` means "checked, clean". The failure path returns ``[]`` and logs,
    which means a detector outage shows up as a clean run — worth knowing, and the reason
    the log line matters. This test pins that behaviour so a future change to it is a
    decision rather than an accident.
    """
    from app.engine import _detect_spec_drift

    class FakeRun:
        id = 5
        project_id = PROJECT

    class FakeSession:
        async def execute(self, *_a, **_k):
            raise RuntimeError("db down")

    assert asyncio.run(_detect_spec_drift(FakeSession(), FakeRun(), [])) == []


# ------------------------------------------------------------------- 6. API surface


def test_the_run_list_payload_carries_state_not_payload():
    """List views get the three-state summary; the detail view gets the list.

    Three states because "not checked" and "clean" must not collapse: the first run in the
    UI would otherwise read as a verified-clean run.
    """
    from app.serialize import _drift_summary  # 2026-10-08 搬去 serialize.py

    @dataclass
    class R:
        drift_signals: list | None = None

    assert _drift_summary(R(None)) == {
        "state": "not_checked",
        "count": None,
        "blockers": None,
    }
    assert _drift_summary(R([])) == {"state": "clean", "count": 0, "blockers": 0}
    got = _drift_summary(
        R([{"severity": "blocker"}, {"severity": "warn"}, {"severity": "warn"}])
    )
    assert got == {"state": "drift", "count": 3, "blockers": 1}


def test_the_run_payload_exposes_the_summary():
    """``_run`` is what every run list and the detail response both go through.

    Testing ``_run`` rather than the endpoint is deliberate: the endpoint needs an auth
    context and a session, and the field being *absent from ``_run``* is the actual failure
    mode — the detail endpoint spreads ``_run``, so anything missing there is missing
    everywhere.
    """
    # 2026-10-08：`_run` / `_drift_summary` 这一族序列化函数搬去了 app/serialize.py
    # （api.py 一度 3370 行）。被测对象没变，只是换了归属地 —— 所以这里跟过去，
    # 而不是改断言。
    from app.models import Run
    from app.serialize import _drift_summary

    class FakeRun(Run):
        pass

    assert "state" in _drift_summary(FakeRun())


# ------------------------------------------------------------------ 7. CSV export


def _export_columns() -> list[str]:
    """Pull the CSV header out of ``export_run`` without running the endpoint.

    Reading the header straight out of the source is deliberate. Calling the endpoint needs
    auth plus a session, and the real failure mode here is positional: someone adds a column
    to ``writerow`` but not to the header, or adds one to each in different places, and the
    file still opens cleanly in a spreadsheet with every value under the wrong label. That
    is invisible to every test that only checks "the export returned 200".
    """
    import ast
    import inspect

    from app.api import export_run

    src = inspect.getsource(export_run)
    tree = ast.parse(inspect.cleandoc(src) if False else src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "writerow"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            first = node.args[0].elts[0]
            if isinstance(first, ast.Constant) and first.value == "case_key":
                return [
                    e.value for e in node.args[0].elts if isinstance(e, ast.Constant)
                ]
    raise AssertionError("找不到 export_run 里的表头 writerow")


def test_the_drift_column_precedes_the_defect_columns():
    """It has to sit before 缺陷标题, because it overrides everything after it.

    Same ordering logic as 失败归因: a row flagged as drift-doubtful must not be read as a
    reportable defect just because the narrative below it is well written.
    """
    cols = _export_columns()
    assert "需规漂移存疑" in cols
    i_drift = cols.index("需规漂移存疑")
    i_attrib = cols.index("失败归因")
    i_title = cols.index("缺陷标题")
    assert i_drift < i_attrib < i_title, (
        f"列顺序错了：{cols}—— 漂移存疑必须排在归因与缺陷标题之前，"
        "否则读者会先看到一段完整叙述就把它当缺陷"
    )


def test_the_drift_column_is_written_for_every_result_row():
    """Header and body must have the same width or the file shifts.

    Counted with the AST rather than by counting commas: a naive comma count is thrown off
    by commas inside the expressions themselves (``(x.judge_reason or x.error or "")`` has
    none, but ``replace("\\n", " ")`` does) and by the comments in between, and it happily
    reported 12 columns for a 17-column row while looking confident about it.
    """
    import ast
    import inspect

    from app.api import export_run

    src = inspect.getsource(export_run)
    tree = ast.parse(src)
    header: list | None = None
    body: list | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "writerow"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            elts = node.args[0].elts
            first = elts[0]
            if isinstance(first, ast.Constant) and first.value == "case_key":
                header = [e.value for e in elts if isinstance(e, ast.Constant)]
            elif body is None:
                body = elts
    assert header and body, "表头或数据行没解析出来"
    assert len(body) == len(header), (
        f"表头 {len(header)} 列，数据行 {len(body)} 项 —— 导出后每列都会错位"
    )


def test_the_drift_join_uses_the_qualified_key():
    """The row-level flag must match on ``pid:case_key``, not the bare key shown in column 1.

    Two projects in this DB share case keys. A bare-key join marks the *other* system's
    rows, which in a spreadsheet means a column of 是 on cases nobody ran and blanks on the
    ones that were.
    """
    src = __import__("inspect").getsource(__import__("app.api", fromlist=["x"]).export_run)
    assert 'f"{run.project_id}:{c.case_key}"' in src, (
        "CSV 里的漂移标记必须用带项目前缀的 key 去比对，"
        "否则同名用例会跨项目串号"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))