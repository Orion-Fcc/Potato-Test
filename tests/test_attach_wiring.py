"""attach 模式接线的回归护栏（2026-10-07）。

这一段代码守的不是功能，是**一个会造成真实损失的错误**：

`app/cdp_endpoint.py` 里 attach 上来的是**用户自己开着的浏览器窗口** —— 里面存着
内网系统的登录态（ACCESS_TOKEN 在 localStorage）、用户正在看的页面、可能还没提交的
表单。而 `_shutdown_browser` 里的 CDP `Browser.close` 语义是"请这个浏览器退出进程"，
不是"关掉这一个标签页"。一旦attach 分支误走到那条路，用户会发现自己整个窗口没了。

所以这里把三条线钉死成测试，而不是钉在注释里：

1. attach 时**绝不**调 `Browser.close`（只允许 `stop()`）；
2. attach 时**绝不**进复用池（复用池的 reset 会关多余标签页 + 导航当前页）；
3. attach 时只关**自己开的那个** target，用户原有的标签页一个都不动。

为什么用源码断言而不是跑真浏览器：上面这些全部发生在"要不要发那条 CDP 命令"的
决策上，而真浏览器测试需要人先手工起一个带调试端口的窗口 —— 在CI 里做不到，
而这恰恰是最需要自动化守住的地方。行为那一侧由tests 里用假 browser 的用例覆盖。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest

from app import executor


def _src(fn) -> str:
    return inspect.getsource(fn)


def _code_only(fn) -> str:
    """Source with the docstring and comments stripped.

    Needed because these guards look for API names like ``Browser.close``, and this
    file's own prose necessarily mentions them — a raw ``in`` check would "pass" (or
    fail) on an English sentence in a docstring rather than on the line that decides
    whether the user's window survives.
    """
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    lines = src.splitlines()
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        # Only statement-holding nodes have a .body list. Calling getattr(node, "body",
        # []) and then subscripting it looks safe but is not: on an expression node the
        # attribute exists and is a Call/AST object, so `body[0]` raises TypeError —
        # and with a bare getattr default the failure is swallowed nowhere, it just
        # crashes. Guard on the node TYPE, not on the attribute being present.
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return "\n".join(
        ln for i, ln in enumerate(lines, 1) if i not in doc_lines and not ln.strip().startswith("#")
    )


# ---------------------------------------------------------------------------
# 1. 绝不关闭用户浏览器
# ---------------------------------------------------------------------------


def test_shutdown_browser_supports_detach_only():
    """close_process=False 必须存在，且该分支里没有 Browser.close。"""
    assert "close_process" in inspect.signature(executor._shutdown_browser).parameters, (
        "_shutdown_browser 必须支持 close_process=False —— attach 模式没有别的路可走"
    )
    src = _code_only(executor._shutdown_browser)
    # Browser.close 只允许出现在 close_process=True 的那条路上。
    # 结构是 `if not close_process: <detach>; return` 之后才是 try/Browser.close 段，
    # 所以两段的边界就是那个 if 本身：它**之前**不许出现 Browser.close（那属于早退
    # 路径），它**之后**必须紧跟着 stop()（证明 detach 分支真的断开了连接）。
    guard = src.index("if not close_process:")
    assert "Browser.close" not in src[:guard], (
        "detach 分支之前出现了 Browser.close —— 那会关掉用户整个浏览器窗口"
    )
    tail = src[guard : src.index("try:", guard)]
    assert "browser.stop()" in tail, "detach 分支应当只做 stop()"
    # 反向：真关闭那段必须有 Browser.close，否则 profile 永远不落盘。
    assert "Browser.close" in src[src.index("try:", guard) :], (
        "close_process=True 那条路仍需 Browser.close —— 它负责让 profile 落盘"
    )


def _shutdown_if_node():
    """The `if _attach_ep:` that guards the **shutdown** path.

    Located by position rather than by "first match": execute_case contains several
    `if _attach_ep:` blocks (creation, beacons, pruner, shutdown) and walking the AST
    without a position hint picks whichever comes first in BFS order — the `warmed`
    check — which would then assert things about the wrong branch entirely.
    """
    src = textwrap.dedent(inspect.getsource(executor.execute_case))
    tree = ast.parse(src)
    cands = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "_attach_ep"
        and any(
            isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_shutdown_browser"
            for c in ast.walk(ast.Module(body=n.body, type_ignores=[]))
        )
    ]
    assert cands, "找不到带_shutdown_browser 的 `if _attach_ep:` —— 关闭分支被改了"
    return src, cands[-1]


def test_execute_case_never_closes_process_when_attached():
    """attach 的关闭分支里只能出现 detach-only 的关闭调用。

    用 AST 而不是字符串查找：这条断言要区分的是「attach 的那条分支」和「自启动的
    那条分支」，而它们是同一个 If 的 body / orelse —— 纯文本分不出谁是谁。
    字符串方案还会在两种情况下误判：注释里提了一句"这会关掉用户窗口"就报警，
    或者 elif 里正确的裸调用被当成 attach 分支的漏洞。
    """
    _src, node = _shutdown_if_node()
    assert node.orelse, "attach 关闭分支必须有 elif/else 兜住自启动路径"

    def _calls(body) -> list[tuple[str, bool]]:
        out = []
        for n in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_shutdown_browser":
                detach = any(
                    kw.arg == "close_process"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                    for kw in n.keywords
                )
                out.append((ast.unparse(n), detach))
        return out

    attach_calls = _calls(node.body)
    assert attach_calls, "attach 关闭分支应当有一处关闭调用"
    for text, detach in attach_calls:
        assert detach, f"attach 分支里的 {text} 没传 close_process=False —— 会关掉用户浏览器"

    # 反向：自启动那条路必须**仍然**走真关闭（否则 profile 不落盘，登录态每次丢）。
    other_calls = _calls(node.orelse)
    assert any(not detach for _, detach in other_calls), (
        "自启动分支必须保留 close_process=True —— profile 要靠它落盘"
    )


def test_attach_close_path_orders_tab_before_disconnect():
    """先关自己的标签页，再断 CDP。反了会留下永久孤儿标签页。"""
    _src, node = _shutdown_if_node()
    calls = [
        ast.unparse(n)
        for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))
        if isinstance(n, ast.Call)
    ]
    tab_at = next((i for i, c in enumerate(calls) if "_close_agent_tab" in c), None)
    det_at = next(
        (i for i, c in enumerate(calls) if "close_process=False" in c), None
    )
    assert tab_at is not None, "attach 关闭分支应先关掉自己开的标签页"
    assert det_at is not None, "attach 关闭分支应detach"
    assert tab_at < det_at, (
        "顺序反了：先断 CDP 就找不到 target 了，标签页会永久留在用户窗口里"
    )


def test_attach_branch_skips_beacon_and_sprite_injection():
    """attach 模式不得往用户的窗口里装东西。

    beacon 拦截是**浏览器级** Fetch 设置 —— 装上去会连带把用户正在正常浏览的页面
    也拦一遍；sprite 裁剪是往页面注入 JS 改 DOM —— 等于在用户窗口里改用户的页面。
    两者都只对"我们自己拉起的浏览器"有意义。
    """
    src = _src(executor.execute_case)
    assert "if not _attach_ep:\n                await _block_third_party_beacons(browser)" in src, (
        "_block_third_party_beacons 必须在 attach 模式下被跳过"
    )
    assert "if not _attach_ep:\n                await _install_sprite_pruner(browser)" in src, (
        "_install_sprite_pruner 必须在 attach 模式下被跳过"
    )


def test_attach_disables_profile_lease_and_pool():
    """attach 时不申请 persistent profile、不进复用池。

    复用池的 reset 会"关掉多余标签页 + 把当前页导航到 about:blank"，这两件事在
    用户自己的窗口上都是不可接受的。
    """
    src = _src(executor.execute_case)
    assert "use_profile = bool(\n            not _attach_ep" in src, (
        "use_profile 必须在 attach 模式下一律为False"
    )
    assert "lease.path if (lease is not None and s.browser_reuse) else None" in src, (
        "_pool_key 仍应保持原判定（靠 lease 为 None 自然落空），不要另写分支"
    )


# ---------------------------------------------------------------------------
# 2. 只关自己开的那个标签页
# ---------------------------------------------------------------------------


class _FakeTargetNS:
    """Stands in for the CDP ``Target`` namespace reached via ``send.Target``."""

    def __init__(self, sink: dict):
        self._sink = sink

    async def closeTarget(self, params=None, **_):  # noqa: N802 - CDP naming
        self._sink.update(params or {})


class _FakeSend:
    def __init__(self, sink: dict, fail: bool):
        self.Target = _FakeTargetNS(sink)
        self._fail = fail

    async def __call__(self, ns, **_):
        # Mirrors the real shape: send is awaited, and the namespace is its attribute.
        if self._fail:
            raise RuntimeError("target already gone")
        return await ns.closeTarget(**_)


class _FakeAttachBrowser:
    def __init__(self, sink: dict, fail: bool = False):
        self.sink = sink
        self._cdp_client_root = type("Root", (), {"send": _FakeSend(sink, fail)})()


def test_close_agent_tab_closes_only_the_given_target():
    """只关传进去的那一个 target id —— 用户原有的标签页一个都不动。"""
    recorded: dict = {}
    b = _FakeAttachBrowser(recorded)
    asyncio.run(executor._close_agent_tab(b, "T-123"))
    assert recorded == {"targetId": "T-123"}, f"应当只关 T-123，实际关了 {recorded}"


def test_close_agent_tab_tolerates_missing_id_and_failure():
    """关不掉不能抛 —— 用例结果比一个残留标签页重要。"""
    asyncio.run(executor._close_agent_tab(None, None))  # 没 tab id：静默跳过

    b = _FakeAttachBrowser({}, fail=True)
    asyncio.run(executor._close_agent_tab(b, "T-9"))  # 已消失的 target：静默吞掉


def test_open_agent_tab_reads_the_page_target_id():
    """从 Page 上取 target id —— 属性名写错就等于没开新页（静默用回当前页）。"""
    from browser_use.actor.page import Page

    # _target_id 是**实例**属性（Page.__init__ 里赋值），所以查类属性查不到，
    # 要查它赋值的名字出现在构造函数里。
    assert "_target_id" in Page.__init__.__code__.co_names, (
        "Page 上的 target id 属性名变了，_open_agent_tab 会一直退回当前页"
    )
    src = _code_only(executor._open_agent_tab)
    assert "_target_id" in src, "_open_agent_tab 应从 Page._target_id 取 id"


def test_open_agent_tab_never_returns_another_tabs_id():
    """新开失败时退回当前页是可以接受的，但必须**说清楚**，不能假装是新页。"""
    src = _src(executor._open_agent_tab)
    assert "新建标签页失败" in src, "回退到当前页必须打 WARNING（用户有权知道自己的页被导航了）"
    assert "该页可能会被本次用例导航走" in src, "WARNING 里要说明后果"


# ---------------------------------------------------------------------------
# 3. attach 失败可回退，且配置错误不该搞死整轮
# ---------------------------------------------------------------------------


def test_attach_failure_falls_back_when_configured():
    """browser_cdp_fallback=True（默认）时 attach 失败要降级，而不是整批失败。"""
    src = _src(executor.execute_case)
    assert "if not s.browser_cdp_fallback:" in src, "必须有开关判断"
    assert "_attach_ep = None" in src, "回退后必须把 _attach_ep 清掉，否则关闭路径会当成 attach"


def test_fallback_recomputes_lease_and_pool_key():
    """回退后要重算 use_profile / lease / _pool_key。

    不重算的后果：attach 时这三者被刻意置空，回退后仍为空 → 浏览器起来了但没有
    profile，登录态每次都丢，等于静默把持久化功能关掉了。
    """
    src = _src(executor.execute_case)
    anchor = "已回退为自启动浏览器"
    assert anchor in src, "找不到回退分支"
    tail = src[src.index(anchor):]
    assert "use_profile = bool(" in tail, "回退后必须重算 use_profile"
    assert "ProfileLease(" in tail, "回退后必须重建 lease"
    assert "_pool_key = lease.path" in tail, "回退后必须重算 _pool_key"


@pytest.mark.parametrize("raw", ["", None, "999.1.1.1:9222", "70000", "not-a-port"])
def test_bad_endpoint_never_reaches_the_browser(raw):
    """配错的值必须降级为 None，且绝不抛异常。"""
    from app.cdp_endpoint import normalize_endpoint

    assert normalize_endpoint(raw) is None