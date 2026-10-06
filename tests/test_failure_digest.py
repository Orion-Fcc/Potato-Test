"""失败清单与用例改动审计的测试。

## 为什么要单独测这两块

1. **合并逻辑会"合并错"**。合并错比不合并不糟得多：人会照着清单去查一个
   根本不存在的问题，然后对系统失去信任。所以这里钉死"什么情况必须合并、
   什么情况必须单列"，而不是只测"能跑出结果"。

2. **审计不能漏也不能多**。漏了 =助手改过用例没人知道；
   多到没法看 = 没人看。两者都靠"只记显式传了且真变了的字段"这条规则约束。

3. **回填必须落库**。实测现算会撞网关 429（免费额度，第 3 批就开始限流），
   清单直接打不开，还会耗光额度让正在跑的用例失败。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.failure_digest import ACTION_LABELS, build_groups, group_signal

_DB = Path(__file__).resolve().parents[1] / "potato.db"


def _row(**kw):
    base = {
        "id": 1, "case_id": 1, "case_key": "T-001", "name": "x", "module": "m",
        "root_cause": "", "judge_reason": "", "error": "", "created_at": "2026-10-06 10:00:00",
        "run_id": 1, "video_url": None,
    }
    base.update(kw)
    return base


# ══════════════════════════════════════════════════════════════════
# 分组信号
# ══════════════════════════════════════════════════════════════════


def test_gate_reasons_group_together():
    """判定闸门拦截的必须聚成一条 —— 那是规则触发的确定信号。"""
    a = _row(id=1, judge_reason="【判定闸门】agent 全篇未给出任何成功陈述，却明确声明放弃…")
    b = _row(id=2, case_id=2, judge_reason="【判定闸门】agent 全篇未给出任何成功陈述，却明确声明放弃…")
    sa, _ = group_signal(a)
    sb, _ = group_signal(b)
    assert sa == sb == "gate:agent_declined"


def test_same_root_cause_groups_together():
    a = _row(id=1, root_cause="product_defect")
    b = _row(id=2, case_id=2, root_cause="product_defect")
    assert group_signal(a)[0] == group_signal(b)[0] == "cause:product_defect"


def test_different_causes_do_not_merge():
    """★核心防错：不同根因绝不能合并。合并错比不合并不糟得多。"""
    a = _row(id=1, root_cause="product_defect")
    b = _row(id=2, case_id=2, root_cause="agent_incomplete")
    groups = build_groups([a, b])
    assert len(groups) == 2, "不同根因被合并了 —— 人会照着清单去查不存在的问题"


def test_unknown_root_cause_does_not_merge():
    """模型自造的分类不能进清单 —— 那会让清单口径被污染。"""
    a = _row(id=1, root_cause="timeout")     # 不在白名单
    b = _row(id=2, case_id=2, root_cause="timeout")
    groups = build_groups([a, b])
    assert len(groups) == 2, "不在白名单的根因必须单列"


def test_never_uses_judge_reason_keywords():
    """★防"代理"这类误聚类。

    实测：judge_reason 里"代理"出现 15 次，但那里的"代理"指**测试 agent**
    （"代理未能勾选多条规则"），不是网络代理。按关键词聚类会把两类
    完全不同的问题混成一条 —— 这正是用户说的"废话"。
    """
    a = _row(id=1, judge_reason="截图显示代理未能勾选多条规则，流程未完成")
    b = _row(id=2, case_id=2, judge_reason="代理错误：无法连接代理服务器")
    groups = build_groups([a, b])
    assert len(groups) == 2, "只因都含『代理』二字就合并了 —— 正是要避免的废话"


def test_timeout_parsed_from_error():
    r = _row(error="超时：480s 内只走到第 18/40 步。这一步本身卡住了…")
    sig, summary = group_signal(r)
    assert sig == "timeout"
    assert "480" in summary and "18/40" in summary, "超时摘要要带上具体数字，否则没法判断严重程度"


def test_cause_conflict_is_exposed():
    """同组内根因冲突必须显式暴露，且动作降级为"人工看"。

    实测 case 4失败 4 次：前 3 次是 agent 没走完，第 4 次被分类成真缺陷。
    这种情况下按多数决猜一个动作会把真缺陷当成重跑处理掉。
    """
    a = _row(id=1, case_id=1, root_cause="agent_incomplete")
    b = _row(id=1, case_id=1, root_cause="product_defect")  # 同一用例不同次
    # 构造同一信号下的冲突：手动给两行相同 judge 但不同 cause
    rows = [
        _row(id=1, case_id=1, root_cause="product_defect", judge_reason="X"),
        _row(id=2, case_id=1, root_cause="agent_incomplete", judge_reason="X"),
    ]
    # 信号不同 -> 本来就分成两组
    groups = build_groups(rows)
    assert all(not g.cause_conflict for g in groups)

    # 显式构造冲突：同 signal（都用 error 里的同一段文本）但 root_cause 不同
    same_err = "Error: something failed"
    rows2 = [
        _row(id=1, case_id=1, root_cause="product_defect", error=same_err),
        _row(id=2, case_id=2, root_cause="agent_incomplete", error=same_err),
    ]
    groups2 = build_groups(rows2)
    conflicted = [g for g in groups2 if g.cause_conflict]
    assert conflicted, "同组内根因冲突没有被标记"
    assert conflicted[0].action == "inspect", "冲突时必须降级为人工确认，不能按多数决猜"


# ══════════════════════════════════════════════════════════════════
# 动作标签
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "cause,expected_action",
    [
        ("product_defect", "report_to_dev"),
        ("test_case_issue", "fix_case"),
        ("test_data", "fix_data"),
        ("auth_or_permission", "fix_env"),
        ("agent_incomplete", "rerun"),
    ],
)
def test_action_matches_cause(cause, expected_action):
    """动作必须由根因唯一决定 —— 助手靠它决定"该不该改用例"。"""
    g = build_groups([_row(root_cause=cause)])[0]
    assert g.action == expected_action
    assert g.action_label == ACTION_LABELS[expected_action]


def test_product_defect_never_maps_to_fix_case():
    """★红线：真缺陷绝不能导向"改用例"。

    这是整套动作映射里最重要的一条 —— 助手拿到 fix_case 就会去改 expected，
    改宽松了用例就"通过"了，真缺陷被掩盖。
    """
    g = build_groups([_row(root_cause="product_defect")])[0]
    assert g.action != "fix_case"
    assert g.action == "report_to_dev"


# ══════════════════════════════════════════════════════════════════
# 清单内容完整性
# ══════════════════════════════════════════════════════════════════


def test_case_deduped_with_fail_count():
    """同一条用例跑失败 3 次，清单里只列一次并标注次数 —— 否则清单会重复刷屏。"""
    rows = [
        _row(id=1, case_id=7, root_cause="product_defect"),
        _row(id=2, case_id=7, root_cause="product_defect"),
        _row(id=3, case_id=7, root_cause="product_defect"),
    ]
    g = build_groups(rows)[0]
    assert len(g.cases) == 1, "同一用例在清单里出现了多次"
    assert g.cases[0]["fail_count"] == 3
    assert len(g.result_ids) == 3, "结果 id 要全留 —— 要能点进去看每一次"


def test_groups_sorted_by_case_count_desc():
    """受影响用例最多的排最前 —— 那是最该先解决的。"""
    rows = [_row(id=1, case_id=1, root_cause="product_defect")]
    rows += [_row(id=10 + i, case_id=10 + i, root_cause="agent_incomplete") for i in range(5)]
    groups = build_groups(rows)
    assert len(groups[0].cases) == 5, "最多的那组没排在最前"


def test_to_dict_shape_has_no_waste():
    """每条清单只给决策要用的字段。

    用户要求"不能讲废话" —— 序列化结果里出现一堆模型自评、
    置信度、可读性分之类的字段，对使用者是纯噪音。
    """
    d = build_groups([_row(root_cause="product_defect")])[0].to_dict()
    for k in ("signal", "summary", "action", "action_label", "case_count", "cases"):
        assert k in d, f"缺 {k}"
    # 不该出现的
    for k in ("confidence", "score", "reasoning", "raw_prompt"):
        assert k not in d, f"清单里混进了无用字段 {k}"


def test_empty_input_does_not_crash():
    assert build_groups([]) == []


# ══════════════════════════════════════════════════════════════════
# 端点与助手接线
# ══════════════════════════════════════════════════════════════════


def test_endpoints_registered():
    """两个端点必须在 app.api 上（助手与前端都靠它们）。"""
    from app import api

    paths = {r.path for r in api.router.routes}
    assert "/api/projects/{pid}/failure-digest" in paths
    assert "/api/projects/{pid}/case-changes" in paths


def test_assistant_has_digest_tools():
    """助手必须能读清单、改用例、看审计（用户要求"都要"）。"""
    from app import assistant

    names = {t["function"]["name"] for t in assistant._tools()}
    assert {"get_failure_digest", "update_case", "list_case_changes"} <= names


def test_update_case_writes_audit():
    """改用例必须落审计 —— 助手自动改的尤其不能漏。"""
    import inspect

    from app import api

    src = inspect.getsource(api.update_case)
    assert "_audit_case_change" in src, "update_case 没有接审计"
    assert "_AUDIT_BEFORE" in src, "改前快照缺失，before 拿不到"
    # 审计 helper 必须在 add 之后由 session 提交，不能自己 commit
    asrc = inspect.getsource(api._audit_case_change)
    assert "CaseChange(" in asrc
    assert ".commit()" not in asrc, "审计不该自己 commit —— 要跟用例改动同一个事务"


def test_audit_skips_metadata_fields():
    """元数据字段（记忆/时间戳）改动不该进审计表，否则很快没人看。"""
    from app import api

    assert "memory" in api._AUDIT_SKIP_FIELDS
    assert "updated_at" in api._AUDIT_SKIP_FIELDS


def test_system_prompt_tells_assistant_when_not_to_edit_expected():
    """提示词必须写明"真缺陷不要改预期"。

    这是最容易被忽略却最危险的一条：助手看到清单就动手改 expected，
    真缺陷会被"改没了"。
    """
    from app import assistant

    p = assistant._system_prompt("", "TestProj", 1)
    assert "get_failure_digest" in p
    assert "report_to_dev" in p
    assert "不要改 expected" in p or "不要改预期" in p
