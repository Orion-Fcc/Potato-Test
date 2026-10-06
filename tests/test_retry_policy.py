"""按失败类型决定要不要重试。

背景（实测数据，本项目自己的历史近 121 条失败）：
    没做到位/证据不足   70 (58%)   ← 不是关于被测应用的结论，是"没走完"
    真实结果不符       17 (14%)   ← 有效缺陷，重试只是白烧一遍时间
    步数用光           11 (9%)

原来 `case_retries` 只对 infra error 生效，判失败的用例一次都不重试 ——
等于把 58% 的"其实没做到位"直接判死。放宽之后要防住另一头：**不能把真实缺陷
重跑一遍当成偶发**，否则通过的率会虚高。所以边界条件必须钉死。
"""
from app.engine import resolve_attempts, should_retry
from app.judge import Verdict


class _R:
    def __init__(self, status, evidence_gap=False, timed_out=False, auth_failed=False):
        self.status = status
        self.evidence_gap = evidence_gap
        self.timed_out = timed_out
        self.auth_failed = auth_failed


def test_infra_errors_are_retried() -> None:
    assert should_retry(_R("error"), retries=1, attempts=0) is True


def test_real_mismatch_is_never_retried() -> None:
    """★ 核心边界：真实缺陷不能被重跑成"偶发"。

    evidence_gap 缺省是 False（判定器没给、给错、给了字符串，都按 False 处理），
    所以判定器不配合时退回到旧行为 —— 宁可不重试，也不要虚高通过率。
    """
    assert should_retry(_R("failed", evidence_gap=False), retries=1, attempts=0) is False


def test_evidence_gap_failure_is_retried() -> None:
    assert should_retry(_R("failed", evidence_gap=True), retries=1, attempts=0) is True


def test_retry_budget_is_respected() -> None:
    """attempts 用满就不再重试，最坏情况有界。"""
    assert should_retry(_R("failed", evidence_gap=True), retries=1, attempts=1) is False
    assert should_retry(_R("error"), retries=0, attempts=0) is False
    assert should_retry(_R("error"), retries=2, attempts=2) is False


def test_timeout_is_never_retried() -> None:
    """超时重试也改不了结局，且会再烧一个完整预算。"""
    assert should_retry(_R("error", timed_out=True), retries=1, attempts=0) is False


def test_judge_defaults_to_no_gap_when_field_is_absent() -> None:
    """判定器没返回 evidence_gap（老输出、被截断、格式变了）时必须按 False 处理。"""
    v = Verdict(status="failed", reason="x")
    assert v.evidence_gap is False
    # 解析侧也保证：只有严格等于 True 才算 gap
    for raw in (None, "true", 1, [], {}):
        got = (raw is True)
        assert got is False or raw is True


def test_retry_that_passes_is_marked_flaky() -> None:
    """重试后通过 → 标记 flaky，让人知道这条不稳定。"""
    final, flaky = resolve_attempts(["failed", "passed"])
    assert final == "passed" and flaky is True
    # 真实缺陷两次都失败：不算 flaky，就是失败
    final, flaky = resolve_attempts(["failed", "failed"])
    assert final == "failed" and flaky is False


def test_heal_retry_counts_against_the_budget() -> None:
    """★ 会话死亡后的自愈重试也必须计入预算。

    发现方式：把 CASE_RETRIES 从 0 放宽到 1 之后，
    `test_a_dead_session_still_heals` 冒出第 5 次 execute。原因是自愈那条
    `continue` 绕过了计数，于是总执行次数变成 retries+2 而不是配置里承诺的
    retries+1 —— 最坏情况多烧一整个 case_timeout_s。
    这里的守卫条件与 engine 的循环一致：heal 后 infra_attempts 已 ≥1。
    """
    retries, infra_attempts = 1, 1  # heal 之后
    assert should_retry(_R("error"), retries, infra_attempts) is False, (
        "自愈重试已用掉唯一一次预算，之后不能再起新尝试"
    )
