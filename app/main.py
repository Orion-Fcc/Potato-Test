"""FastAPI app factory."""

from __future__ import annotations

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _seed_admin()
    await _fail_orphaned_runs()
    await _backfill_knowledge_chunks()
    yield


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
