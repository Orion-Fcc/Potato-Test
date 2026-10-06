"""飞书机器人：随服务启停的生命周期约束。

背景（2026-10-06）：用户反馈「依旧接收不到来自飞书的询问命令」。查下来有两层：

1. **一个让「状态」100% 失效的错缩进**（UnboundLocalError）—— 见 tests/test_feishu.py
   里的 status query 回归测试。
2. **机器人需要一个独立进程才活着**：原先只有桌面 .bat 会额外
   `start pythonw -m app.feishu_poll`；API 服务自己不启动它。于是"服务起来了、
   机器人没起"的部署状态下，群里问状态永远没人理，而且**日志里什么都没有** ——
   没有进程，自然没有日志。这是最难查的一类故障：症状在群里，证据在哪都没有。

这个文件钉住第 2 层的修法：机器人随 API 服务进程启动，且**恰好只有一个实例**
在回答问题。

python -m pytest tests/test_feishu_worker_lifecycle.py
"""

from __future__ import annotations

import asyncio

import pytest


class _Settings:
    def __init__(self, enable: bool, in_process: bool) -> None:
        self.enable_feishu = enable
        self.feishu_worker_in_process = in_process


def test_worker_follows_the_server_by_default() -> None:
    """默认必须随服务启动 —— 否则"一键部署"对别人来说还是少一步。"""
    from app.config import Settings

    assert Settings.model_fields["feishu_worker_in_process"].default is True


def test_nothing_starts_when_feishu_is_disabled(monkeypatch) -> None:
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "get_settings", lambda: _Settings(False, True))

    assert asyncio.run(main_mod._start_feishu_worker()) is None


def test_nothing_starts_in_docker_mode(monkeypatch) -> None:
    """容器里已有独立的 feishu_ws 服务，进程内再起一个会让一条消息被回两次。"""
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "get_settings", lambda: _Settings(True, False))

    assert asyncio.run(main_mod._start_feishu_worker()) is None


def test_a_broken_bot_never_blocks_the_api(monkeypatch) -> None:
    """机器人是附加能力，它起不来不能把 API 拖下水。"""
    from app import feishu_poll
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "get_settings", lambda: _Settings(True, True))

    async def _boom():
        raise RuntimeError("import lark_oapi failed")

    monkeypatch.setattr(feishu_poll, "serve_in_process", _boom)

    assert asyncio.run(main_mod._start_feishu_worker()) is None


def _use_private_lock(monkeypatch) -> str:
    """把互斥体换成这次测试专属的名字，并返回它。

    为什么必须换：`potato-feishu-poll` 是**进程级**锁，而用户可能正开着服务跑用例
    —— 服务里的 worker 持着它。于是硬编码锁名的测试会在"app 正在运行"时变红，
    而那与被测代码毫无关系。实测 2026-10-06：用户一打开 app，
    `test_serve_in_process_bows_out_when_the_lock_is_taken` 与
    `test_cancelling_the_worker_releases_the_lock` 双双失败 —— 看起来像刚改坏了
    什么，实际上只是"用户在场"。

    名字带上 pid：同一台机器上两个 pytest 进程并行时也不互相干扰。
    """
    import os

    from app import feishu_poll

    name = f"potato-feishu-poll-test-{os.getpid()}-{id(monkeypatch):x}"
    monkeypatch.setattr(feishu_poll, "LOCK_NAME", name)
    return name


def test_serve_in_process_bows_out_when_the_lock_is_taken(monkeypatch) -> None:
    """单实例：一条消息只该被回一次。

    抢不到锁时返回 None（而不是抛异常），调用方据此只记一行日志。
    """
    from app import feishu_poll
    from app.single_instance import SingleInstance

    name = _use_private_lock(monkeypatch)
    holder = SingleInstance(name)
    assert holder.acquire(), "前置：本进程先占住锁"
    try:
        assert asyncio.run(feishu_poll.serve_in_process()) is None
    finally:
        holder.release()


def test_cancelling_the_worker_releases_the_lock(monkeypatch) -> None:
    """服务关闭 → 任务被取消 → 锁必须立刻释放。

    不释放的后果很隐蔽：重启后新进程抢不到锁，机器人永久沉默，
    而日志里只有一句"已有轮询实例在运行"，看起来像是主动跳过的正常行为。
    """
    from app import feishu_poll
    from app.single_instance import SingleInstance

    # ★ 必须在 serve_in_process() **之前**换锁名：worker 是在那里抢锁的，
    # 事后替换等于让它去抢真锁 —— 而真锁正被用户开着的服务持着，于是任务为 None。
    name = _use_private_lock(monkeypatch)

    async def _never(_interval_s: int) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(feishu_poll, "_loop", _never)
    # 别在测试里写 pid 文件：那会让桌面 .bat 误以为机器人已经在跑
    monkeypatch.setattr(feishu_poll, "_write_pid", lambda: None)

    async def _run() -> None:
        task = await feishu_poll.serve_in_process()
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    other = SingleInstance(name)
    assert other.acquire(), "取消后没有释放锁"
    other.release()


def test_the_stop_helper_cancels_the_task(monkeypatch) -> None:
    """lifespan 关闭时必须把机器人任务取消掉。

    不取消的话，uvicorn 会等所有任务结束 —— 一个 while True 的轮询协程
    永远不结束，表现为"关服务关不掉"。
    """
    from app import main as main_mod

    async def _run() -> None:
        async def _never() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(_never())
        await main_mod._stop_feishu_worker(task)
        assert task.cancelled(), "任务没有被取消"

    asyncio.run(_run())


def test_stopping_with_no_task_is_a_noop() -> None:
    """没启用飞书时 feishu_task 是 None，关闭流程不能因此报错。"""
    from app import main as main_mod

    asyncio.run(main_mod._stop_feishu_worker(None))
