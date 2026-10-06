"""守住"图标精灵裁剪必须覆盖两条 Agent 路径"这个修复。

背景（2026-10-05 实测）：
  代码里有两处 `Agent(...)` —— execute_case 和 capture_session。
  裁剪器原先只装在 execute_case，capture_session 全程不裁剪，后果是
  登录页 DOM 33,915 个节点（内联 32,995 个 <path>），agent 判定
  "页面还没有可交互元素"→ wait 3s → 再 wait → 干等循环。
  那次实测 element index 一路到 67346，用例失败；装上之后只剩 335 个节点，
  同一条用例从 92.1s/失败 变成 67.1s/通过。

这些断言很"文本化"，是有意的：这个 bug 的形态就是"某个构造点忘了挂钩子"，
纯逻辑测不到。改 prompt、改超时都不会碰这几行，所以不会误红。
"""

import inspect
import sys

sys.path.insert(0, "<PROJECT_DIR>")


def test_both_agent_constructions_install_pruner():
    """两处 Agent 构造都必须能触达裁剪器。

    只查 execute_case 是不够的 —— 当初就是漏了 capture_session。
    """
    from app import executor as ex

    src = inspect.getsource(ex)
    n_agent = src.count("agent = Agent(")
    assert n_agent >= 2, "Agent 构造点变少了？先确认结构再改这条断言"

    cap = inspect.getsource(ex.capture_session)
    assert "_install_sprite_pruner" in cap, "capture_session 没装裁剪器（本 bug 的原始形态）"
    assert "_ensure_sprite_pruner" in cap, "capture_session 缺每步补装"


def test_capture_pruner_uses_real_async_callback():
    """回调必须是 async def，不能是 lambda。

    browser-use 用 `inspect.iscoroutinefunction(回调)` 决定 await 还是直接调用；
    lambda 返回协程对象但不是协程函数 → 走同步分支 → 协程永不执行。
    这是本次踩过的坑，值得钉住。
    """
    from app import executor as ex

    src = inspect.getsource(ex.capture_session)
    assert "register_new_step_callback=_capture_step_cb" in src, "没挂步回调"
    assert "async def _capture_step_cb" in src, "回调不是 async def（lambda 会静默失效）"
    assert "register_new_step_callback=lambda" not in src, "回调被写成了 lambda"


def test_install_falls_back_to_cdp_session():
    """拿不到页面 target 时必须回落到当前 CDP 会话。

    `get_page_targets()` 在 session_manager 未初始化时**恒返回空**
    （源码首行 `if not self.session_manager: return []`），而 browser-use 的
    Browser 是懒加载的 —— capture_session 预装时必然命中这个分支。
    只靠页面列表的话，那条路径永远装不上。
    """
    from app import executor as ex

    src = inspect.getsource(ex._install_sprite_pruner)
    assert "get_page_targets" in src
    assert "get_or_create_cdp_session" in src, "缺 CDP 会话回落，capture_session 会永远装不上"


def test_prune_on_session_helper_exists():
    """两条路共用同一个安装实现，避免只修好一条。"""
    from app import executor as ex

    assert hasattr(ex, "_install_prune_on_session")
    sig = inspect.signature(ex._install_prune_on_session)
    assert len(sig.parameters) == 1, "只接收 cdp 会话；带 browser 的旧签名已废弃"


def test_prune_disabled_switch_still_respected():
    """开关关掉时必须彻底不动 —— 这是 A/B 对比的唯一手段。"""
    from app import executor as ex

    src = inspect.getsource(ex._install_sprite_pruner)
    assert "_sprite_prune_enabled()" in src
    assert "_sprite_prune_enabled()" in inspect.getsource(ex._ensure_sprite_pruner)


def test_pruner_targets_only_unused_symbols():
    """裁剪只删没有被 <use> 引用的 <symbol>，在用的必须保留。

    这是"不牺牲质量"的依据：交互元素与 prompt 不变，只是不再遍历纯图形节点。
    """
    from app import executor as ex

    js = ex._sprite_prune_js()
    assert "__svg__icons__dom__" in js, "精灵容器 id 变了？先确认被测系统的实际 id"
    assert "querySelectorAll('use')" in js, "没收集在用的符号 → 会误删"
    assert "tpKept" in js, "缺少'已用过就永久保留'的标记，可能在视图切换后误删"


def test_max_actions_per_step_raised():
    """每步可批量动作数已上调（2→4），减少模型往返次数。

    这条是速度杠杆，不是正确性约束：调小只是变慢，不会错。
    设成 1 会让每个动作都单开一次模型往返。
    """
    from app.config import get_settings

    s = get_settings()
    assert s.max_actions_per_step >= 2, "每步动作数被压到 1 会让模型往返数翻倍"
