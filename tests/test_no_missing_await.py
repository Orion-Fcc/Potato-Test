"""Catch ``await``-less coroutine calls before they ship.

Found on 2026-10-07 while wiring the assistant's case tools. ``_audit_case_change`` is
``async``, and ``PUT /testcases/{cid}`` called it as a bare statement:

    _audit_case_change(s, c, body, data, request)     # no await

The case change persisted; ``case_change`` stayed empty — forever, for every caller. The
only symptom was a ``RuntimeWarning: coroutine ... was never awaited`` emitted at GC time,
long after the request, pointing at a stack that no longer had the call in it.

Why it survived a test suite
----------------------------
Nothing in the suite asserted on ``case_change`` content, and ``case_change`` being empty is
indistinguishable from "the feature is unused". So this is exactly the bug a test suite
reports as green. What catches it is a *structural* check: the shape of the call, not its
behaviour.

Scope
-----
Every ``app/*.py`` module, ``await``-less calls of module-local coroutine functions only.
Both halves matter:

* *module-local* — calling ``httpx.get(...)`` without await is a legitimate-looking style
  that pyflakes already flags as an unused import in many setups; here it would be noise,
  and the interesting cases are our own functions.
* *functions, not methods* — a plain ``def`` returning a coroutine is rare and would be
  flagged by other means; restricting to ``async def`` keeps the signal high.

Excluded from the scan: this file, the test modules, and anything under ``_`` scratch
scripts (they are one-off probes, and one of them is what misdiagnosed the bug in the first
place).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# One-off probe scripts. They are throwaway by design and one of them already produced a
# wrong conclusion about a bug; scanning them would add noise without adding protection.
_SKIP_PREFIX = "_"
_SKIP_NAMES = {"_append_memory_18.py"}


def _sources() -> list[pathlib.Path]:
    out = []
    for p in sorted(APP_DIR.glob("*.py")):
        if p.name in _SKIP_NAMES:
            continue
        if p.name.startswith(_SKIP_PREFIX):
            continue
        out.append(p)
    return out


def _async_func_names(tree: ast.AST) -> set[str]:
    """Top-level and nested ``async def`` names declared in this module."""
    return {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef,))
    }


def _awaited_calls(tree: ast.AST) -> set[int]:
    """``id()`` of call nodes that sit anywhere inside an ``await``.

    "Anywhere inside", not "directly": ``await (f() if cond else g())`` is correct and the
    first version of this scanner reported it, which produced six false positives on the first
    run (``engine._read`` at line 630 being the clearest).
    """
    out: set[int] = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Await):
            continue
        for sub in ast.walk(n.value):
            if isinstance(sub, ast.Call):
                out.add(id(sub))
    return out


def _discarded_calls(tree: ast.AST) -> set[int]:
    """``id()`` of calls whose return value is thrown away.

    A call statement — ``f(x)`` on its own line, as the whole statement — is the only shape
    that actually loses the coroutine. Every other shape hands it to someone who awaits it:

        await drain(ids, lambda: execute_one_case(...), n)   # lambda, awaited by drain
        return asyncio.run(_run())                           # awaited by asyncio.run
        b = await (_read() if cond else _read2())            # awaited by the outer await
        asyncio.run_coroutine_threadsafe(_poll_forever(), _loop)

    Two versions of this rule were wrong before this one, and both are worth remembering:

    * Only recognising ``await f(x)`` reported all four shapes above — six false positives on
      the first run.
    * Keying the result by *line number* looked fine until ``asyncio.run(_loop(...))`` appeared:
      the inner and outer calls share a line, so the inner one inherited the outer's verdict
      and got reported. Node identity, not source position.

    A scanner that cries wolf is worse than none: it trains people to skip the warning when
    the real one arrives.
    """
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            out.add(id(n.value))
    return out


def _offenders(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, name)`` for every module-local coroutine call that is thrown away.

    Shared by the tests below on purpose. When each test carried its own copy of the predicate
    they drifted apart — and a test that pins a *different* rule than the scanner uses is worse
    than no test, because it passes while the scanner is broken.
    """
    names = _async_func_names(tree)
    discarded = _discarded_calls(tree)
    return [
        (n.lineno, n.func.id)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in names
        and id(n) in discarded
    ]


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_async_function_is_called_without_await(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if not _async_func_names(tree):
        pytest.skip("no local async functions")

    offenders = _offenders(tree)
    assert not offenders, (
        f"{path.name}: 这些 async 函数被调用但没有 await："
        + ", ".join(f"第 {ln} 行 {name}()" for ln, name in sorted(offenders))
        + " —— 协程不会执行，副作用（写审计、发通知、落库）全部静默丢失"
    )


def test_the_audit_writer_is_awaited_at_its_call_site():
    """Named test for the concrete regression, separate from the generic scan.

    The generic scan would catch a future re-introduction, but it does not say *which*
    behaviour broke. This one fails with a sentence that explains the consequence.
    """
    api = (APP_DIR / "api.py").read_text(encoding="utf-8")
    tree = ast.parse(api)

    target = None
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_audit_case_change":
            target = n
            break
    assert target is not None, "找不到 _audit_case_change —— 它可能被改名或删除了"

    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_audit_case_change"
    ]
    assert calls, "_audit_case_change 没有任何调用点：用例改动审计实际上不工作"

    awaited = _awaited_calls(tree)
    missing = [c.lineno for c in calls if id(c) not in awaited]
    assert not missing, (
        f"api.py 第 {missing} 行调用 _audit_case_change 时漏了 await —— "
        "用例改动能落库但 case_change 表永远是空的，"
        "而且不会有任何报错，只有 GC 时一条无关的 RuntimeWarning"
    )


def test_the_scanner_would_actually_catch_the_bug():
    """Prove the detector is not vacuous by feeding it the exact broken shape.

    A test that greps for a pattern nobody ever writes passes forever and detects nothing.
    This runs the same AST logic over a synthetic module containing the original mistake and
    asserts it is flagged — so if the scan silently stops working, this goes red first.
    """
    broken = ast.parse(
        "async def helper(s):\n"
        "    return 1\n"
        "\n"
        "def caller(s):\n"
        "    helper(s)\n"
        "    return helper(s)\n"
    )
    # Line 5 is the bare statement — the real bug. Line 6 hands the coroutine back to the
    # caller, which is a decision for them to make, not a dropped coroutine here.
    assert [ln for ln, _ in _offenders(broken)] == [5], (
        f"扫描器应只报第 5 行（未 await 的裸语句），实际报了 {_offenders(broken)}"
    )


def test_a_correctly_awaited_call_is_not_flagged():
    """The other half: the check must not fire on correct code, or it gets ignored.

    Each of these three shapes is correct and each one was a false positive in the first
    version of the scanner. They are kept together on purpose: they are the shapes that
    look wrong to a naive matcher, so they are the ones to re-check whenever the rule moves.
    """
    correct = ast.parse(
        "async def helper(s):\n"
        "    return 1\n"
        "\n"
        "async def direct(s):\n"
        "    return await helper(s)\n"
        "\n"
        "async def via_conditional(s, cond):\n"
        "    return await (helper(s) if cond else helper(s))\n"
        "\n"
        "def via_runner(s):\n"
        "    import asyncio\n"
        "    return asyncio.run(helper(s))\n"
    )
    assert not _offenders(correct), (
        f"正确写法被误报了：{_offenders(correct)}"
    )


def test_a_generator_passed_as_a_lambda_is_not_flagged():
    """The shape that caused six of the first run's false positives.

    ``execute_one_case`` and ``_run`` are coroutines, but at their call sites they are wrapped
    in a lambda or handed to ``asyncio.run`` — someone else awaits them. Treating "not directly
    awaited" as "dropped" is what produced the noise.
    """
    handed_off = ast.parse(
        "async def work(s):\n"
        "    return 1\n"
        "\n"
        "async def drain(s, items):\n"
        "    return [await f(s) for f in items]\n"
        "\n"
        "def caller(s, ids):\n"
        "    return drain(s, [lambda: work(s) for _ in ids])\n"
    )
    assert not _offenders(handed_off)


def test_a_coroutine_nested_in_a_same_line_call_is_not_flagged():
    """A same-line nesting bug, caught after it shipped a false positive.

    ``asyncio.run(_loop(...))`` and ``asyncio.run_coroutine_threadsafe(_poll_forever(), _loop)``
    both run in this codebase. The inner call is an *argument*, and the outer call consumes it.
    When the scanner keyed on line numbers the inner call inherited the outer's verdict and got
    reported — two false positives on the second run, from real code that was correct.

    These two lines are the whole regression: line numbers cannot distinguish "the call that is
    thrown away" from "a call sitting inside the call that is thrown away".
    """
    same_line = ast.parse(
        "async def _loop(n):\n"
        "    return 1\n"
        "\n"
        "def via_run(n):\n"
        "    import asyncio\n"
        "    asyncio.run(_loop(n))\n"
        "\n"
        "def via_thread_scheduler(loop):\n"
        "    import asyncio\n"
        "    asyncio.run_coroutine_threadsafe(_loop(120), loop)\n"
    )
    assert not _offenders(same_line), (
        f"同行业务嵌套被误报：{_offenders(same_line)}"
    )


def test_a_bare_statement_is_still_caught_when_it_shares_a_line():
    """The other half of the line-number fix: the real bug must not slip through.

    ``asyncio.run(_loop(n))`` and ``_loop(n)`` on one line — only the second loses the
    coroutine. If narrowing the rule had simply stopped looking at same-line calls, this is
    what would hide the defect.
    """
    mixed = ast.parse(
        "async def _loop(n):\n"
        "    return 1\n"
        "\n"
        "def caller(n):\n"
        "    _loop(n); asyncio.run(_loop(n))\n"
    )
    assert [ln for ln, _ in _offenders(mixed)] == [5], (
        f"应只报第 5 行的裸语句调用，实际报了 {_offenders(mixed)}"
    )