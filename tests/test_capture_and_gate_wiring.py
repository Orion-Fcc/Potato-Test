"""本轮三项改动的接线测试：判定闸门、登录态捕获计时、裁剪器修复。

## 为什么接线测试要单独钉死

这三处都是"改了就静默失效"的类型：
- 闸门不接→ 假通过照旧，而且**不会有任何报错**（最危险的一种失效）。
- `start()` 不加 → 裁剪器装不上，代价是每步 DOM 采集多 20s，
  但日志只有一行 warning，肉眼极易忽略。
- 计时点不埋 → 耗时又变成"2m29s 但说不清钱花在哪"。

已验证过这些断言是有效的：把 `if _gate.blocked:` 改成 `if False and ...`
后 `test_gate_branch_exists_in_executor` 立刻变红。
"""

from __future__ import annotations

import inspect


# ══════════════════════════════════════════════════════════════════
# 闸门接线
# ══════════════════════════════════════════════════════════════════


def test_gate_is_imported_in_executor():
    from app import executor

    assert hasattr(executor, "check_gates"), "executor 没有导入 check_gates"


def test_gate_branch_exists_in_executor():
    """闸门分支必须真的在判定路径上，且闸门命中时不调用 LLM 判定器。

    用源码结构断言而非行为模拟：这个分支在 execute_case 的深处，
    完整跑一遍要起浏览器 + 调 LLM，成本 100 倍以上。
    有效性已验证（改成 `if False and _gate.blocked:` 后本条立刻失败）。
    """
    from app import executor

    src = inspect.getsource(executor)
    assert "if _gate.blocked:" in src, "闸门拦截分支不见了"
    # 命中后走 else 调用 judge —— 也就是"拦下就不问判定器"
    assert "verdict = await judge(" in src
    seg = src.split("if _gate.blocked:")[1].split("else:")[0]
    assert "await judge(" not in seg, "闸门命中分支里不该再调用 LLM 判定器"


def test_gate_hit_sets_failure_fields():
    """闸门命中时必须把 status / reason / root_cause 都写全。"""
    from app import executor

    src = inspect.getsource(executor)
    seg = src.split("if _gate.blocked:")[1].split("else:")[0]
    for field in ("res.status =", "res.judge_reason =", "res.evidence_gap =",
                  "res.root_cause ="):
        assert field in seg, f"闸门命中时缺 {field}"


# ══════════════════════════════════════════════════════════════════
# 裁剪器修复：capture_session 必须先 start()
# ══════════════════════════════════════════════════════════════════


def test_capture_session_starts_browser_before_pruner():
    """`await browser.start()` 必须在 `navigate_to` / `_install_sprite_pruner` 之前。

    现场日志「拿不到 CDP 会话，图标精灵裁剪未安装 —— Root CDP client not initialized」
    的根因：browser-use 的 `navigate_to()` 在未启动时**不抛异常**
    （`event_result(raise_if_none=False)` 把「事件没跑」当成成功），
    随后的 `get_or_create_cdp_session()` 撞 `assert _cdp_client_root is not None` 必炸。
    登录页内联3.3 万个 <path>，不裁剪则每步 DOM 采集 20-27s。
    """
    from app import executor

    src = inspect.getsource(executor.capture_session)
    i_start = src.find("await browser.start()")
    assert i_start != -1, "capture_session 里没有 start() —— 裁剪器必然装不上"
    i_nav = src.find("await browser.navigate_to(")
    i_prune = src.find("await _install_sprite_pruner(browser)")
    assert i_nav != -1 and i_prune != -1
    assert i_start < i_nav, "start() 必须在 navigate_to 之前"
    assert i_start < i_prune, "start() 必须在装裁剪器之前"


def test_capture_session_keeps_keep_alive():
    """keep_alive 不能在本次改动中丢掉。

    少了它，`export_storage_state()` 会报"Root CDP client not initialized"
    —— 而它和裁剪器的报错是同一句，极易混淆。
    """
    from app import executor

    src = inspect.getsource(executor.capture_session)
    assert "keep_alive=True" in src


# ══════════════════════════════════════════════════════════════════
# 登录态捕获计时
# ══════════════════════════════════════════════════════════════════


def test_stage_timer_basic():
    from app.executor import _StageTimer

    t = _StageTimer("u").start()
    t.mark("A")
    t.mark("B")
    # 不抛异常即可；真实耗时由 mark 的增量决定
    assert t._marks is not None
    assert [n for n, _ in t._marks] == ["A", "B"]


def test_stage_timer_mark_before_start_is_noop():
    """没 start 就 mark 不应崩 —— 崩了会把捕获路径整个打断。"""
    from app.executor import _StageTimer

    t = _StageTimer("u")
    t.mark("A")  # 不抛
    t.finish()  # 不抛


def test_capture_session_has_all_timing_marks():
    """六个阶段都要有 mark，否则耗时仍然说不清。

    顺序也有意义：start 必须早于第一个 mark，agent登录 必须在身份守卫之后。
    """
    from app import executor

    src = inspect.getsource(executor.capture_session)
    for stage in ("启浏览器", "预导航", "装裁剪器", "身份守卫", "agent登录", "导出登录态"):
        assert f'_t.mark("{stage}")' in src, f"缺计时点：{stage}"


def test_capture_session_finishes_timer_in_finally():
    """finish 必须在 finally 里。

    捕获失败（超时 / 凭据失效 / 身份守卫拒绝）时，恰恰最需要知道时间花在哪，
    而这些路径全都走异常。放在函数末尾等return 的话永远记不上。
    """
    from app import executor

    src = inspect.getsource(executor.capture_session)
    i_finally = src.find("finally:")
    assert i_finally != -1, "capture_session 没有 finally 块"
    assert "_t.finish()" in src[i_finally:], "finish 不在 finally 里"

    # execute_case 也有 finally，别认错函数
    assert src.count("_t.finish()") == 1, "finish 应只出现一次"


def test_slow_capture_threshold_is_sane():
    """慢捕获阈值 60s：低于它日志没价值，高于它就该报警了。

    依据：实测 145s；缓存 TTL 30min = 1800s，即捕获占缓存期 8%，
    超过 60s（3.3%）就说明这条链路的成本值得看一眼。
    """
    from app.executor import _CAPTURE_SLOW_WARN_S

    assert 30 <= _CAPTURE_SLOW_WARN_S <= 120, f"阈值 {_CAPTURE_SLOW_WARN_S} 不合理"
