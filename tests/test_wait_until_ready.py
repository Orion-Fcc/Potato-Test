"""_wait_until_ready 的行为测试（2026-10-06 回归）。

## 为什么必须有这个测试

我第一版写了 `await _time.sleep(...)` —— `time.sleep()` 是**同步**的、返回 `None`，
于是每次导航都抛：

    TypeError: object NoneType can't be used in 'await' expression

整条用例直接崩，现场连挂 10 条（TP-ACFG-001~012 全是同一个错）。

★为什么单元测试没抓住：那个错只在**真的等不到就绪、走到 sleep 那一行**时才暴露。
页面秒开时立刻 return，压根不会执行到。所以"跑一遍没报错"不等于对——
**必须让代码真的走进 sleep 分支**。这正是本文件用假 CDP 逼它走进去的原因。
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from app import executor


class _FakeCdpClient:
    """模拟 browser-use 的 `cdp_client`。

    ★关键：它不是 `send(method, ...)`，而是 `send.Runtime.evaluate(params=..., session_id=...)`
    —— CDP 的命名空间对象式调用。我第一版写成普通方法签名，测试全红而生产代码是对的，
    差点反过来去改正确的实现。**假件必须照抄真实接口形状。**
    """

    def __init__(self, script_result):
        self._script_result = script_result
        self.send = self  # send.Runtime.evaluate(...) -> self.Runtime.evaluate(...)
        self.eval_calls = 0   # ★轮询次数看这个 —— get_or_create 只调 1 次，计它没意义

    async def evaluate(self, params=None, session_id=None):  # noqa: ANN001
        self.eval_calls += 1
        v = self._script_result
        if callable(v):
            v = v()
        return {"result": {"value": v}}

    Runtime = property(lambda self: self)


class _FakeCdp:
    def __init__(self, script_result):
        self.cdp_client = _FakeCdpClient(script_result)
        self.session_id = "s1"


class _FakeBrowser:
    """只提供 _wait_until_ready 用到的那一个方法。"""

    def __init__(self, script_result):
        self._cdp = _FakeCdp(script_result)
        self.cdp_calls = 0

    async def get_or_create_cdp_session(self):  # noqa: ANN003
        self.cdp_calls += 1
        return self._cdp

    @property
    def eval_calls(self) -> int:
        """轮询次数。计数发生在 cdp_client 里，这里代理过去。

        ★别把计数器加在 browser 上——evaluate 是 cdp_client 执行的，
        加错对象会让断言永远拿到 0，然后"测试通过"而 bug 仍在。
        """
        return self._cdp.cdp_client.eval_calls


def _js(interactive: int, loading: bool) -> str:
    import json as _json

    return _json.dumps({"loading": loading, "interactive": interactive})


async def test_returns_ready_when_interactive_appears():
    """页面有可交互元素 → 立刻返回 ready，不睡。"""
    b = _FakeBrowser(_js(interactive=12, loading=False))
    out = await executor._wait_until_ready(b, timeout_s=2.0)
    assert out == "ready(12)"
    assert b.eval_calls == 1, "就绪时不该反复轮询"


async def test_sleeps_then_times_out_without_awaiting_sync_sleep():
    """★回归：一直不出现可交互元素 → 走进 sleep 分支再超时。

    这一条就是当初漏掉那个 bug 的分支。旧代码在这里抛
    "object NoneType can't be used in 'await' expression"。

    ★ timeout 必须给足多轮的余量：间隔 0.6s，timeout 给 1.5s 才够跑 2 轮以上。
    我第一版给 0.7s，实测只跑 1 轮就到期 —— **是测试预期写错了，不是代码错**。
    """
    b = _FakeBrowser(_js(interactive=0, loading=True))
    out = await executor._wait_until_ready(b, timeout_s=1.5)
    # 不抛异常就是修好了；返回值说明它等到了超时而不是崩了
    assert out.startswith("timeout("), f"应超时返回，实际 {out!r}"
    assert "loading" in out, "超时描述要带上最后一次看到的状态"
    assert b.eval_calls > 1, f"应轮询多次，实际 {b.eval_calls}"
    # get_or_create_cdp_session 只该调一次（每次新建 CDP 对象没意义）
    assert b.cdp_calls == 1, f"CDP 会话应复用，实际取了 {b.cdp_calls} 次"


async def test_mixed_ready_after_a_few_polls():
    """先加载、后就绪 —— 中间必须真的睡过（证明 sleep 路径可用）。"""
    seq = [_js(0, True), _js(0, True), _js(5, False)]
    state = {"i": 0}

    def _next():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v

    b = _FakeBrowser(_next)
    out = await executor._wait_until_ready(b, timeout_s=3.0)
    assert out == "ready(5)", f"应等到就绪，实际 {out!r}"
    assert state["i"] >= 3, "应至少轮询 3 次"


async def test_never_raises_on_cdp_failure():
    """拿不到 CDP 不能把用例搞死 —— 返回 skip 描述即可。"""

    class _Broken:
        async def get_or_create_cdp_session(self):  # noqa: ANN003
            raise RuntimeError("no cdp")

    out = await executor._wait_until_ready(_Broken(), timeout_s=1.0)
    assert out.startswith("skip(")


async def test_survives_eval_failure_then_recovers():
    """中途一次求值失败，下一轮仍能拿到就绪 —— 不许因单次异常就退出。"""
    seq = [_js(0, True), "not-json", _js(3, False)]
    state = {"i": 0}

    def _next():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v

    b = _FakeBrowser(_next)
    out = await executor._wait_until_ready(b, timeout_s=3.0)
    assert out == "ready(3)", f"应跳过坏数据继续轮询，实际 {out!r}"


def test_uses_async_sleep_not_sync():
    """★钉死"不能 await 同步 sleep"这个 bug 不再复发。

    只查源码不够 —— 真正的原因是它在运行到 sleep 分支时才炸；
    上一条 `test_sleeps_then_times_out` 才是行为保证，这一条是快速护栏。
    """
    body = "\n".join(
        line for line in inspect.getsource(executor._wait_until_ready).splitlines()
        if not line.strip().startswith("#")
    )
    assert "await _time.sleep" not in body, "又用了同步的 time.sleep —— 不能 await"
    assert "await _aio.sleep" in body, "应使用 asyncio.sleep"


def test_ready_js_is_raw_string():
    """★_WAIT_READY_JS 必须是 raw string。

    否则 `\\.` 先被 Python 当无效转义（SyntaxWarning），
    送到浏览器时正则从"字面三个点"变成"任意字符"。
    """
    js = executor._WAIT_READY_JS
    assert "loading\\.\\.\\." in js, r"正则里的 \. 被吃掉了 —— 字符串没按原样送到浏览器"
    assert "正在加载" in js, "中文加载字样丢了"
