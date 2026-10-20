"""Legacy plaintext login state → encrypted `Credential` migration.

`Project.login_state` predates encrypted credential storage and holds raw
storage_state JSON (cookies + tokens). New writes are stopped; this module moves
existing values into `Credential(type="storage_state")` so the engine reads the
encrypted copy first.

The legacy column is intentionally left untouched for one release: if a rollback
is needed, the old code can still read it. It must be dropped in a later version.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.crypto import encrypt, secret_configured
from app.models import Credential, Project

log = logging.getLogger("potato-test.login_state")

_MIGRATED_LABEL = "旧版登录态（自动迁移）"


async def backfill(session) -> int:
    """Encrypt every project's legacy login state; return how many were moved.

    Idempotent: a project that already has any storage_state credential is
    skipped. That includes an inactive one — a user who deliberately deactivated
    or replaced the migrated session must not get it resurrected on the next boot.
    Reads already prefer credentials, so leaving the plaintext in place is safe.
    """
    if not secret_configured():
        return 0

    projects = (
        (
            await session.execute(
                select(Project).where(
                    Project.login_state.is_not(None), Project.login_state != ""
                )
            )
        )
        .scalars()
        .all()
    )
    if not projects:
        return 0

    migrated = set(
        (
            await session.execute(
                select(Credential.project_id).where(Credential.type == "storage_state")
            )
        )
        .scalars()
        .all()
    )

    moved = 0
    for project in projects:
        if project.id in migrated:
            continue
        session.add(
            Credential(
                project_id=project.id,
                type="storage_state",
                role=None,
                environment_id=None,
                label=_MIGRATED_LABEL,
                secret=encrypt(project.login_state),
                is_active=True,
            )
        )
        moved += 1

    if moved:
        await session.flush()
        log.info("login state backfill: %s project(s) migrated to encrypted credentials", moved)
    return moved


async def store(session, project_id: int, raw: str) -> Credential:
    """Persist a raw storage_state as the project's active default credential."""
    cred = Credential(
        project_id=project_id,
        type="storage_state",
        role=None,
        environment_id=None,
        label="默认登录态",
        secret=encrypt(raw),
        is_active=True,
    )
    session.add(cred)
    await session.flush()
    return cred
