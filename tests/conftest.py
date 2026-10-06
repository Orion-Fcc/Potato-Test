"""测试隔离：回滚"直接赋值模块属性"的打桩。

背景（2026-10-02 实测到的真实故障）：

若干测试用**直接赋值**的方式打桩：

    executor.execute_case = fake_execute_case
    engine._ensure_bundle = fake_ensure_bundle

它们写的是 `def main(): ...` + `asyncio.run(main())` 的形式，没有用 pytest 的
`monkeypatch` fixture，所以**打桩不会被回滚**，会泄漏给同进程里后续跑的所有测试。

后果不是"某个测试报错"，而是**静默失效**，这更危险：
`tests/test_speed_levers.py` 和 `tests/test_failure_evidence.py` 用
`inspect.getsource(execute_case)` 断言"真实实现里有没有某段关键代码"。
一旦 `execute_case` 已被替换成 fake，读到的是 **fake 的源码**：
  * 断言失败 → 看起来像产品代码坏了（本次就是这样误导了一次排查）
  * 更糟：fake 里恰好也有同名字符串时断言通过 → 那条约束**根本没在测**，
    而套件仍然全绿。

这里用 autouse fixture 在**每个测试结束后**把被替换的属性还原。原始值在第一次
fixture setup（即第一个测试运行前、任何打桩发生前）捕获，所以不会把已污染的值
误当成"原始值"。
"""

from __future__ import annotations

import os

import pytest

# 被"直接赋值"污染的模块级属性。新增此类打桩时，请同时把名字加到这里，
# 或者干脆改用 monkeypatch fixture（那个会自动回滚，不需要登记）。
_GUARDED = (
    ("app.executor", "execute_case"),
    ("app.engine", "_ensure_bundle"),
)

_ORIGINALS: dict[tuple[str, str], object] = {}


def _capture_originals() -> None:
    import importlib

    for mod_name, attr in _GUARDED:
        key = (mod_name, attr)
        if key in _ORIGINALS:
            continue
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 — 模块导入失败不该让整个套件挂掉
            continue
        _ORIGINALS[key] = getattr(mod, attr, None)


@pytest.fixture(autouse=True)
def _rollback_module_stubs():
    import importlib

    # 捕获必须发生在任何测试体执行之前，否则 capture 到的就是被污染的值。
    _capture_originals()
    yield
    for (mod_name, attr), original in _ORIGINALS.items():
        if original is None:
            continue
        try:
            setattr(importlib.import_module(mod_name), attr, original)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 环境变量与进程级缓存的隔离（2026-10-06）。
#
# 这是上面那类"静默失效"的另一半。十几个测试文件直接写环境变量而不还原：
#
#     os.environ["POTATO_SECRET_KEY"] = "x" * 32      # 不是合法的 Fernet 密钥
#     os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_db}"
#
# 而 app 侧有两个进程级 lru_cache：`app.config.get_settings` 与 `app.crypto._fernet`。
# 缓存 + 未还原的环境变量 = 测试之间互相投毒。实测到的形态：
# tests/test_case_last_result.py 把密钥设成 "x"*32、自己 cache_clear 了 settings，
# 但没管 _fernet；于是之后**第一个真正调用 encrypt() 的测试**会拿到这个非法密钥，
# 报出 `ValueError: Fernet key must be 32 url-safe base64-encoded bytes` ——
# 一个跟它自己毫无关系、也不知道该去改哪里的报错。
#
# 实测数字：tests/test_credential_edit.py 单独跑 13 passed；
# 接在 test_case_last_result.py 后面跑 11 failed。失败与被测代码无关，
# 纯粹是套件内部的顺序依赖 —— 而这类失败最费时间，因为它指向错误的方向。
#
# 不逐个去修那十几个文件（改动面大，而且以后还会有人这么写），
# 在这里统一兜底：测前存档 + 清缓存，测后还原 + 清缓存。
_ENV_GUARDED = ("POTATO_SECRET_KEY", "DATABASE_URL", "AUTH_ENABLED")


@pytest.fixture(autouse=True)
def _no_real_feishu_worker(monkeypatch):
    """测试里绝不启动真的飞书轮询机器人。

    2026-10-06 起，机器人随 API 服务进程启动（app/main.py 的 lifespan 会调
    app.feishu_poll.serve_in_process）。而本仓库的端到端测试大量使用
    `TestClient(app)`，那是会真的跑 lifespan 的 —— 不拦的话，每建一个
    TestClient 就会：抢单实例锁、往 .feishu_ws.pid 写本进程 PID、
    并开始每 8 秒调一次 open.feishu.cn。

    后果不只是慢：锁被测试进程占住之后，用户手动启动的机器人会以
    "已有轮询实例在运行"静默退出 —— 一次 pytest 就能让群机器人再也不回话，
    而症状出现在完全无关的时间点。

    monkeypatch 会在测试结束时自动还原，不会污染后续用例。
    """
    monkeypatch.setenv("FEISHU_WORKER_IN_PROCESS", "false")


def _clear_process_caches() -> None:
    """清掉读取环境变量的两处 lru_cache，让下一次调用重新解析。"""
    try:
        from app.config import get_settings

        get_settings.cache_clear()
    except Exception:  # noqa: BLE001 — 模块导入失败不该让整个套件挂掉
        pass
    try:
        from app import crypto

        crypto._fernet.cache_clear()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _rollback_env_and_caches():
    saved = {k: os.environ.get(k) for k in _ENV_GUARDED}
    # 测前就要清：上一个测试可能已经把非法密钥留在缓存里了。
    _clear_process_caches()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _clear_process_caches()
