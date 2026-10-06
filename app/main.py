"""FastAPI app factory."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import public as public_router
from app.api import router
from app.config import get_settings
from app.db import init_db


async def _seed_admin() -> None:
    """Create the first admin from ADMIN_EMAIL/ADMIN_PASSWORD when no users exist."""
    from sqlalchemy import func, select

    from app import auth
    from app.db import db_session
    from app.models import User

    s = get_settings()
    if not (s.admin_email and s.admin_password):
        return
    async with db_session() as sess:
        count = (await sess.execute(select(func.count()).select_from(User))).scalar_one()
        if count:
            return
        sess.add(
            User(
                email=s.admin_email.strip().lower(),
                name="Admin",
                password_hash=auth.hash_password(s.admin_password),
                is_admin=True,
                is_active=True,
            )
        )


async def _fail_orphaned_runs() -> None:
    """In-process runs die with the process, so any left 'pending'/'running' at boot are
    dead — mark them failed (per-case results already persisted). Skipped when Redis is
    configured: there Celery owns durability and queued case tasks survive a restart."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app.db import db_session
    from app.models import Run

    if get_settings().redis_url:
        return
    async with db_session() as s:
        await s.execute(
            update(Run)
            .where(Run.status.in_(("pending", "running")))
            .values(status="failed", finished_at=datetime.now(UTC))
        )


async def _backfill_knowledge_chunks() -> None:
    """Move legacy `proj_<pid>_knowledge` settings into the chunk table, once.

    Knowledge used to live in one AppSetting row and the assistant re-split it on every
    search. The new model stores chunks (see app/knowledge.py); existing projects would
    otherwise look empty. Idempotent — a project that already has chunks is skipped, so
    this is safe on every boot.
    """
    import logging

    from sqlalchemy import select

    from app import knowledge
    from app.db import db_session
    from app.models import Project

    log = logging.getLogger("potato-test.boot")
    try:
        async with db_session() as s:
            pids = (await s.execute(select(Project.id))).scalars().all()
            moved = await knowledge.backfill_from_settings(s, list(pids))
        if moved:
            log.info("knowledge backfill: %s project(s) migrated to chunks", moved)
    except Exception as exc:  # noqa: BLE001 — a failed migration must not block boot
        log.warning("knowledge backfill skipped: %s", exc)


async def _start_feishu_worker():
    """把飞书轮询机器人挂到本服务进程上（返回 task，或 None 表示没启动）。

    为什么放进服务进程（2026-10-06）：机器人原先只能靠桌面 .bat 另起一个
    `pythonw -m app.feishu_poll`。任何一次"服务起来了、机器人没起"的部署，
    表现都是「群里 @机器人 问状态没人理」，而**日志里没有任何线索** ——
    没有进程，就没有日志。让别人部署这个工具时，这一步根本不该存在。

    三层保护，不会出现"一条消息回两次"：
      1. `feishu_worker_in_process=false` 直接关掉（Docker 就设了这个，
         那边有独立的 ws 服务）；
      2. 抢 `potato-feishu-poll` 内核级单实例锁，独立的 poller 抢不到会自己退出；
      3. 未配置 app_id/app_secret 时 `catch_up_missed` 直接返回，不会有任何网络调用。

    启动失败绝不能让服务起不来：机器人是可选的附加能力，API 才是主体。
    """
    import logging

    log = logging.getLogger("potato-test.boot")
    s = get_settings()
    if not s.enable_feishu:
        log.info("飞书机器人未启用（ENABLE_FEISHU=false），跳过")
        return None
    if not s.feishu_worker_in_process:
        log.info("feishu_worker_in_process=false，机器人交由外部进程/Docker 服务负责")
        return None
    try:
        from app.feishu_poll import serve_in_process

        task = await serve_in_process()
        if task is None:
            log.warning("飞书机器人未在本进程启动（已有实例持锁）—— 群里仍会有回答，由那个实例负责")
        return task
    except Exception as exc:  # noqa: BLE001 — 机器人起不来不能拖垮 API
        log.warning("飞书机器人启动失败（不影响测试功能）：%s", exc)
        return None


async def _stop_feishu_worker(task) -> None:
    """关闭时取消机器人任务，让单实例锁立刻释放。

    不取消的后果：进程虽然要退了，但 uvicorn 会等所有任务结束 —— 一个
    `while True` 的轮询协程会一直等下去，表现为"关服务关不掉"。
    """
    import logging

    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    logging.getLogger("potato-test.boot").info("飞书机器人已随服务停止")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _seed_admin()
    await _fail_orphaned_runs()
    await _backfill_knowledge_chunks()
    feishu_task = await _start_feishu_worker()
    try:
        yield
    finally:
        await _stop_feishu_worker(feishu_task)


def create_app() -> FastAPI:
    app = FastAPI(title="Potato Test", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # internal tool; tighten per deployment
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(public_router)
    app.include_router(router)

    # serve locally-stored artifacts (mp4 / history.json) when S3 is not configured
    s = get_settings()
    if not s.s3_endpoint:
        os.makedirs(s.artifact_dir, exist_ok=True)
        app.mount("/artifacts", StaticFiles(directory=s.artifact_dir), name="artifacts")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # Serve the built SPA (Docker image sets WEB_DIST=/app/web_dist). Registered
    # last so /api, /artifacts and /health win; unknown paths fall back to
    # index.html for client-side routing.
    if s.web_dist and os.path.isdir(s.web_dist):
        index = os.path.join(s.web_dist, "index.html")

        @app.get("/{full_path:path}")
        async def spa(full_path: str) -> FileResponse:
            candidate = os.path.join(s.web_dist, full_path)
            if full_path and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(index)

    return app


app = create_app()
