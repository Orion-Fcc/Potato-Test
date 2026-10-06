"""Feishu bot that reads group messages by POLLING — no SDK, no public callback URL.

Why this exists instead of `app.feishu_ws`
------------------------------------------
The WebSocket long-connection worker is the nicer transport (instant, no polling), but
it needs `lark-oapi`, and importing that package pulls in ~10,700 generated modules:
on this machine the very first `import lark_oapi` was still compiling after 6 minutes,
and every process start pays a good chunk of that again. A bot that must be alive
whenever someone might type 「状态」 cannot have a startup cost like that.

Polling needs nothing but the REST calls we already make — `catch_up_missed()` lists
messages in each bound chat since the persisted watermark and replays them through
`handle_message_event`. The trade is latency (one poll interval instead of instant),
which is irrelevant for a question a human typed by hand.

Usage
-----
    python  -m app.feishu_poll     # foreground (set POTATO_LOG_LEVEL=INFO to see logs)
    pythonw -m app.feishu_poll     # background — what the desktop PotatoTest.bat does

Interval: POTATO_FEISHU_POLL_SEC seconds (default 8, floor 3).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("potato-test.feishu.poll")

ROOT = Path(__file__).resolve().parent.parent
# Same pid file the desktop .bat looks for, whichever transport ends up running.
PID_PATH = ROOT / ".feishu_ws.pid"


def _setup_logging() -> None:
    level_name = os.environ.get("POTATO_LOG_LEVEL", "WARNING").strip().upper()
    handlers: list[logging.Handler] = []
    # pythonw.exe has no console: sys.stderr is None, and a StreamHandler on it would
    # be silently useless (logging swallows the failure). Attach one only when a real
    # stream exists — i.e. when someone deliberately runs this in a terminal.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    # 但"控制台不存在"不等于"不需要日志"：本进程用 pythonw 启动，之前的 handlers 落空时
    # 会退化成 NullHandler，于是**整个 poller 的日志被静默丢弃**。代价是它一旦不响应，
    # 没有任何线索可查（实测 2026-10-02：群里问「状态」没人回，日志里 feishu 出现 0 次，
    # 完全查不出是权限、网络还是进程卡住）。所以始终再挂一个轮转文件处理器。
    try:
        from logging.handlers import RotatingFileHandler

        log_dir = ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "feishu_poll.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    except Exception:  # noqa: BLE001 — 日志初始化失败绝不能挡住服务启动
        pass
    logging.basicConfig(
        level=getattr(logging, level_name, logging.WARNING),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )


def _write_pid() -> None:
    """Record our own PID so the desktop .bat can stop us later.

    Nothing else identifies this process: it runs under pythonw (no window title), and
    killing "pythonw.exe" wholesale would take the API server down with it.
    """
    try:
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        log.warning("无法写入 %s —— .bat 将无法自动停止本进程", PID_PATH)
        return
    atexit.register(lambda: PID_PATH.unlink(missing_ok=True))


def _interval_s() -> int:
    """轮询间隔（秒）。环境变量优先于设置，两者都不可用时回落到 8。"""
    from app.config import get_settings

    raw = os.environ.get("POTATO_FEISHU_POLL_SEC") or str(
        getattr(get_settings(), "feishu_poll_interval_sec", 8) or 8
    )
    try:
        interval = int(raw)
    except ValueError:
        interval = 8
    return max(3, interval)


async def _loop(interval_s: int) -> None:
    import httpx

    from app.config import get_settings
    from app.feishu import bind_shared_http, catch_up_missed  # documented never to raise

    # One connection for the whole process lifetime. Without this every request pays a
    # full TLS handshake: measured here at ~222 ms of CPU per request vs ~16 ms reused,
    # which at two requests per tick is what makes a "harmless" poll loop audible in the
    # fan. Same reason the token is cached inside catch_up_missed now.
    def fresh() -> httpx.AsyncClient:
        client = httpx.AsyncClient(timeout=15, trust_env=not get_settings().feishu_ignore_proxy)
        bind_shared_http(client)
        return client

    shared = fresh()
    log.info("轮询已启动：每 %s 秒查一次绑定的群", interval_s)
    wait = float(interval_s)
    while True:
        started = time.monotonic()
        try:
            ok = await catch_up_missed()
        except Exception:
            # catch_up_missed is already best-effort internally; this is the
            # belt-and-braces version so one bad response can never kill the loop.
            log.exception("轮询这一轮失败，%s 秒后继续", interval_s)
            ok = False
        if not ok and shared.is_closed:
            shared = fresh()  # e.g. the peer dropped our idle keep-alive connection
        # A call that fails the same way forever (missing scope, dead network) shouldn't
        # be retried every 8 seconds: stretch the gap, and snap back on the first success.
        #
        # 上限从 300 秒降到 60 秒（2026-10-06）。原因不是"重试太贵"，而是**症状会骗人**：
        # 这套部署面对的是一条时通时断的链路（实测 10-03 起 ConnectError / ReadTimeout
        # 反复出现，而同一时刻手工 curl 同一个域名是通的）。旧的 300 秒上限意味着
        # 一次瞬时抖动之后，机器人可能安静 5 分钟 —— 群里看到的是"机器人死了"，
        # 于是去查进程、查权限、查凭据，而实际上它只是在退避。
        # 60 秒仍然把"永久性失败"的重试成本压得住（每分钟一次而不是每 8 秒一次），
        # 同时把"抖动后恢复"的感知延迟限制在一分钟以内。
        wait = float(interval_s) if ok else min(wait * 2, 60.0)
        if wait > interval_s:
            log.info("上一轮没成功，下次 %s 秒后再试", int(wait))
        await asyncio.sleep(max(1.0, wait - (time.monotonic() - started)))


# 持锁对象的模块级强引用。锁是内核句柄，对象被 GC 并不会释放它，但显式持有
# 让"谁在持锁"这件事在代码里一眼可见，也避免以后有人给 SingleInstance 加 __del__。
_LOCK = None


def _acquire_lock():
    """抢 `potato-feishu-poll` 单实例锁；抢不到返回 None 并说明是谁在持有。

    单实例是硬要求：两份轮询会各自读同一个群并各回一次消息，用户收到重复回复
    （2026-10-02 实测漏过两份，其中一份已跑 190 分钟）。pidfile 挡不住这件事 ——
    它只是一个可能失真的快照；内核级锁在进程正常退出、崩溃、被杀时都会被释放。
    """
    global _LOCK
    from app.single_instance import SingleInstance

    lock = SingleInstance("potato-feishu-poll")
    if not lock.acquire():
        log.warning(
            "已有轮询实例在运行（PID %s，锁文件 %s），本次不启动机器人 —— "
            "一条消息只该被回一次。要换实例请先停掉持锁的那个。",
            lock.holder_pid() or "未知",
            lock.path,
        )
        return None
    _LOCK = lock
    log.info("轮询单实例锁已获取（%s）", lock.path)
    _write_pid()
    return lock


async def serve_in_process():
    """在**已经有事件循环**的情况下启动轮询，供 API 服务进程内使用。

    返回创建的任务；返回 None 表示本次不启动（锁在别的实例手里）。

    为什么需要它（2026-10-06）：机器人此前只能靠桌面 .bat 额外起一个进程，
    于是"服务起来了但机器人没起"这种部署状态下，群里问状态永远没人理，
    而日志里什么都没有 —— 没有进程，自然没有日志。让机器人随服务启停，
    把这一类"静默不可用"整体消掉。

    调用方必须持有返回的 task 并在关闭时 cancel，否则服务停了这个协程还活着。
    """
    lock = _acquire_lock()
    if lock is None:
        return None
    interval = _interval_s()
    log.info("飞书轮询已随服务启动（每 %s 秒查一次绑定的群）", interval)
    task = asyncio.create_task(_loop(interval), name="feishu-poll")

    # 释放锁挂在**任务结束回调**上，而不是协程体内的 finally。
    # 这不是风格问题：任务可能在第一次被调度之前就被取消（服务刚启动就关闭、
    # 或 lifespan 启动过程中出错回滚），那时协程体从未进入过，finally 不会执行，
    # 锁就被永久泄漏 —— 而内核锁只在进程退出时才会自动释放，所以本进程会一直
    # 握着它，下一次启动的机器人将以"已有实例持锁"静默退出。
    # 实测就是这么发现的：取消一个还没开始跑的任务，锁没有回来。
    # release() 是幂等的（第二次调用时句柄已是 None），重复释放安全。
    task.add_done_callback(lambda _t: lock.release())
    return task


def main() -> None:
    _setup_logging()
    lock = _acquire_lock()
    if lock is None:
        return
    try:
        asyncio.run(_loop(_interval_s()))
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()


if __name__ == "__main__":
    main()
