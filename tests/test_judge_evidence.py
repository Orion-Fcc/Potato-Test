"""判定器"反编造"防线的测试。

背景：实测抓到过判定器凭空补充操作 —— agent 的全部输出只有
    "Clicked input type=text role=combobox id=el-id-6768-51"
步骤里根本没点过「查询」，判定却写成"且点击「查询」后列表仍显示全部 10 条记录"。
危害比"结果不稳定"更大：报告里的失败理由是编的，人会照着它去查不存在的问题。

所以这里钉三层：
  1. 提示词里必须有"不得编造"的约束
  2. 步骤证据必须带编号（否则判定器无从引用）
  3. 引用了不存在的编号要被标出来
"""
import json

from app.judge import _SYSTEM, Verdict, _check_evidence, build_prompt


def test_system_prompt_forbids_inventing_evidence() -> None:
    """提示词必须明确禁止编造动作与观察。"""
    low = _SYSTEM.lower()
    assert "do not invent" in low, "缺少「不得编造」的约束"
    # 这几条是针对实测到的具体编造形态写的，不能被改弱
    assert "never state a result" in low, "缺少「不得陈述未记录的结果/条数」的约束"
    assert "did not" in low, "缺少「没做就直说没做」的引导"
    assert "evidence" in low, "缺少要求它说明依据的指令"


def test_step_results_are_numbered() -> None:
    """步骤证据必须带编号 —— 判定器被要求引用编号，提示里就得有编号。"""
    p = json.loads(
        build_prompt(
            expected="两个条件同时生效",
            final_answer="Clicked combobox",
            actions=["click A", "click B"],
            task="查询",
            evidence=["点了使用范围下拉", "选了场地资源", "点了状态下拉"],
        )
    )
    steps = p["step_results"]
    assert [s["step"] for s in steps] == [1, 2, 3], "步骤编号必须是 1 起的连续整数"
    assert steps[0]["observation"] == "点了使用范围下拉", "观察内容不能丢"
    # 旧的裸字符串列表会让引用无从对齐
    assert all(isinstance(s, dict) for s in steps)


def test_evidence_citation_is_validated() -> None:
    """引用真实存在的步骤 → 不报警；引用不存在的 → 必须标出来。"""
    # 合法
    assert _check_evidence([1, 3], 3) == ((1, 3), None)
    # 越界：编号 9 根本不存在 —— 这类理由不可全信
    idx, warn = _check_evidence([1, 9], 3)
    assert idx == (1,) and warn and "未经核对" in warn
    # 没给引用：无法核对，也要标
    idx, warn = _check_evidence([], 3)
    assert idx == () and warn and "未说明依据" in warn
    # 给了非整数：同样是问题
    idx, warn = _check_evidence(["2"], 3)
    assert warn and "未经核对" in warn
    # bool 是 int 的子类，必须显式排除，否则 True 会被当成第 1 步
    idx, warn = _check_evidence([True], 3)
    assert warn and "未经核对" in warn
    # 不是列表
    assert _check_evidence(None, 3)[1] is not None
    assert _check_evidence("1,2", 3)[1] is not None


def test_verdict_defaults_keep_backward_compat() -> None:
    """新增字段不能破坏既有构造方式（很多地方是 Verdict(status=..., reason=...)）。"""
    v = Verdict(status="failed", reason="x")
    assert v.evidence == ()
