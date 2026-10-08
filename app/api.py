"""REST + SSE API. Thin layer over models/engine — no business logic here beyond
project scoping and kicking off runs."""

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import delete, func, select
from sse_starlette.sse import EventSourceResponse

from app import auth, docparse, excel, feishu, feishu_notify, mailer, notify
from app.config import get_settings
from app.crypto import decrypt, encrypt, secret_configured
from app.gitlab_client import GitLabClient
from app.gitlab_sync import resolve_token
# 根因分类表（分类标识 / 中文标签 / 是否算真缺陷）。放在 judge 里作为唯一来源，
# 这里只引用 —— 前端再抄一份必然会出现"新增分类忘了同步"的空白分组。
# 失败归因表（谁造成了这次失败 / 中文标签 / 是否可上报缺陷）。同样只引用 judge 之外的
# 唯一来源 failure_attrib，理由见上。
from app.db import db_session
from app.engine import run_suite
from app.models import (
    CaseChange,
    Credential,
    Environment,
    FeedbackItem,
    Invite,
    Issue,
    IssueComment,
    Notification,
    Project,
    ProjectMember,
    Run,
    RunResult,
    TestCase,
    TestSuite,
    User,
)

# 2026-10-08 拆出：ORM 行 → dict 的 21 个序列化函数（315 行）搬去了 app/serialize.py。
# 它们只读行、不碰库与请求，是纯粹的对外契约。
from app.serialize import (  # noqa: F401
    LastResult,
    _case,
    _case_change,
    _comment,
    _cred,
    _env,
    _feedback,
    _feishu_status,
    _iso,
    _issue,
    _llm_status,
    _notif,
    _project,
    _result,
    _run,
    _user,
    _verdict_info,
)
from app.schemas import (
    AssistantIn,
    ChangePasswordIn,
    CredentialCaptureIn,
    CredentialIn,
    CredentialPatch,
    EnvironmentIn,
    EnvironmentPatch,
    FeishuSettingsIn,
    ForgotIn,
    GitLabConfigIn,
    GitlabTokenIn,
    InviteAcceptIn,
    InviteIn,
    IssueCommentIn,
    IssueIn,
    IssuePatch,
    KnowledgeIn,
    LlmSettingsIn,
    LoginIn,
    MemberIn,
    MemberPatch,
    ProjectIn,
    ProjectPatch,
    ResetIn,
    RolesIn,
    RunIn,
    ResultPatch,
    RunPatch,
    SuiteAssignIn,
    SuiteIn,
    SuitePatch,
    TestCaseIn,
    TestCasePatch,
    UserPatch,
    _roles_of,
)


def _enqueue_push(issue_id: int) -> None:
    """Fire-and-forget GitLab push.

    Imported lazily on purpose: a plain install (no Redis, no Celery, no GitLab) must be
    able to start the API even when the `celery` package is not installed at all. The old
    top-level ``from app.celery_app import enqueue_push`` made celery a hard runtime
    dependency of every request path, even with GitLab sync disabled.
    """
    from app.celery_app import enqueue_push

    enqueue_push(issue_id)


# cadence -> timedelta for computing a suite's next due date (None = no reminders)
_CADENCE = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "biweekly": timedelta(weeks=2),
    "monthly": timedelta(days=30),
}


def _next_due(cadence: str, frm: datetime | None = None) -> datetime | None:
    delta = _CADENCE.get(cadence)
    if delta is None:
        return None
    return (frm or datetime.now(UTC)) + delta


# public routes (login / invite / config) — no login gate
public = APIRouter(prefix="/api")
# everything else requires login when auth_enabled (require_user is a no-op otherwise)
router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_user)])

# keep strong refs to background run tasks so they aren't garbage-collected mid-run
_TASKS: set[asyncio.Task] = set()

ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


async def _project_access(request: Request, pid: int, min_role: str) -> str:
    """Resolve the caller's effective role on a project and enforce a minimum.
    Admins (and everyone, while auth is disabled) get 'owner'. Raises 403/404."""
    from app.models import ProjectMember

    settings = get_settings()
    if not settings.auth_enabled:
        return "owner"
    user = await auth.current_user(request)
    if user is None:
        raise HTTPException(401, "authentication required")
    # shared workspace: any logged-in user has full access (login gate only, no RBAC)
    if user.is_admin or settings.shared_workspace:
        return "owner"
    async with db_session() as s:
        m = (
            (
                await s.execute(
                    select(ProjectMember).where(
                        ProjectMember.project_id == pid, ProjectMember.user_id == user.id
                    )
                )
            )
            .scalars()
            .first()
        )
    role = m.role if m else None
    if role is None or ROLE_RANK.get(role, 0) < ROLE_RANK[min_role]:
        raise HTTPException(403, f"requires {min_role} on this project")
    return role


# Bounded read chunk. `await file.read()` with no argument buffers the entire body
# before anything can judge it, so a 2 GB post would OOM the server before hitting
# the size check. Read in pieces and stop as soon as the cap is passed.
_UPLOAD_CHUNK = 4 * 1024 * 1024


async def _read_upload_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload into memory, refusing to exceed `max_bytes`.

    Rejects early on the declared Content-Length (cheap, and usually correct), then
    enforces the real limit while streaming so a lying/missing header can't get past.
    """
    declared = file.size
    if declared is not None and declared > max_bytes:
        raise HTTPException(
            413,
            f"文件过大（{declared / 1024 / 1024:.1f} MB），上限 {max_bytes // 1024 // 1024} MB",
        )

    buf = bytearray()
    while True:
        piece = await file.read(_UPLOAD_CHUNK)
        if not piece:
            break
        buf.extend(piece)
        if len(buf) > max_bytes:
            raise HTTPException(
                413,
                f"文件过大（超过 {max_bytes // 1024 // 1024} MB），请拆分后再上传",
            )
    return bytes(buf)


# ---- auth ----


def _expired(dt: datetime | None) -> bool:
    """tz-safe expiry check (SQLite hands back naive datetimes; treat them as UTC)."""
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt < datetime.now(UTC)




@public.get("/config")
async def public_config() -> dict:
    s = get_settings()
    return {
        "auth_enabled": s.auth_enabled,
        "shared_workspace": s.shared_workspace,
        "public_base_url": s.public_base_url,
        # optional integrations — the SPA hides their UI when disabled
        "gitlab_enabled": s.enable_gitlab,
        "feishu_enabled": s.enable_feishu,
    }


def _require_feature(enabled: bool, name: str) -> None:
    """404 for endpoints of an integration this server has switched off."""
    if not enabled:
        raise HTTPException(404, f"{name} integration is disabled (see ENABLE_* in .env)")


def _set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        # Secure matters whenever TLS terminates in front of us (tunnel / reverse proxy):
        # this process only sees plain http, so without it the session cookie is also
        # sent over http and can be captured by a downgrade.
        secure=s.cookie_secure_enabled,
        max_age=s.jwt_ttl_hours * 3600,
    )


@public.post("/auth/login")
async def login(body: LoginIn, response: Response) -> dict:
    async with db_session() as s:
        u = (
            (await s.execute(select(User).where(User.email == body.email.strip().lower())))
            .scalars()
            .first()
        )
        if u is None or not u.is_active or not auth.verify_password(body.password, u.password_hash):
            raise HTTPException(401, "invalid email or password")
        u.last_login_at = datetime.now(UTC)
        token = auth.create_session_token(u.id)
        payload = _user(u)
    _set_session_cookie(response, token)
    return payload


@public.post("/auth/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@public.get("/auth/me")
async def me(request: Request) -> dict | None:
    u = await auth.current_user(request)
    return _user(u) if u else None


@public.post("/auth/onboarded")
async def set_onboarded(request: Request) -> dict:
    """Mark the guided first-run flow as seen (finished OR skipped — either way we
    stop opening it). No-op without a session, e.g. an auth-disabled instance, where
    the frontend keeps the flag in localStorage instead."""
    u = await auth.current_user(request)
    if u is None:
        return {"onboarded_at": None}
    async with db_session() as s:
        row = await s.get(User, u.id)
        if row is None:
            return {"onboarded_at": None}
        if row.onboarded_at is None:
            row.onboarded_at = datetime.now(UTC)
        return {"onboarded_at": _iso(row.onboarded_at)}


# ---- password reset ----
# ponytail: per-process throttle on "forgot password" so the endpoint can't be used to
# mailbomb an address. Good enough with a handful of workers; move to Redis if it grows.
_FORGOT_SENT_AT: dict[str, float] = {}
FORGOT_MIN_INTERVAL_S = 60.0


def _forgot_throttled(email: str) -> bool:
    now = time.monotonic()
    last = _FORGOT_SENT_AT.get(email)
    if last is not None and now - last < FORGOT_MIN_INTERVAL_S:
        return True
    _FORGOT_SENT_AT[email] = now
    return False


def _reset_link(token: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/reset/{token}" if base else f"/reset/{token}"


async def _send_reset(info: dict) -> dict:
    """Email a minted reset link. The link is returned either way, so an admin can hand
    it over directly when the relay is unreachable (same shape as an invite)."""
    subject, body = mailer.reset_email_body(info["link"], auth.RESET_TTL_HOURS)
    return {**info, "emailed": await mailer.send_email(info["email"], subject, body)}


@public.post("/auth/forgot")
async def forgot_password(body: ForgotIn) -> dict:
    """Email a reset link. Always {"ok": True} — the response must not reveal whether
    an account exists for that address."""
    email = body.email.strip().lower()
    if _forgot_throttled(email):
        return {"ok": True}
    async with db_session() as s:
        u = (await s.execute(select(User).where(User.email == email))).scalars().first()
        info = (
            {"email": u.email, "link": _reset_link(auth.create_reset_token(u))}
            if (u and u.is_active)
            else None
        )
    if info:  # SMTP outside the session — the relay can take seconds
        await _send_reset(info)
    return {"ok": True}


@public.post("/auth/reset")
async def reset_password(body: ResetIn, response: Response) -> dict:
    """Consume a reset link: set the new password and sign the user straight in."""
    claim = auth.decode_reset_token(body.token)
    if claim is None:
        raise HTTPException(400, "this reset link is invalid or has expired")
    uid, fingerprint = claim
    async with db_session() as s:
        u = await s.get(User, uid)
        if u is None or not u.is_active:
            raise HTTPException(400, "this reset link is invalid or has expired")
        if auth.pw_fingerprint(u.password_hash) != fingerprint:
            raise HTTPException(400, "this reset link has already been used")
        u.password_hash = auth.hash_password(body.password)
        u.last_login_at = datetime.now(UTC)
        token = auth.create_session_token(u.id)
        payload = _user(u)
    _set_session_cookie(response, token)
    return payload


@router.post("/auth/change-password")
async def change_password(body: ChangePasswordIn, request: Request) -> dict:
    """Change your own password (requires the current one)."""
    me_user = await auth.current_user(request)
    if me_user is None:
        raise HTTPException(401, "authentication required")
    async with db_session() as s:
        u = await s.get(User, me_user.id)
        if u is None or not auth.verify_password(body.old_password, u.password_hash):
            raise HTTPException(400, "current password is incorrect")
        u.password_hash = auth.hash_password(body.new_password)
    return {"ok": True}


# ---- admin: users + invites ----
@router.get("/admin/users", dependencies=[Depends(auth.require_admin)])
async def list_users() -> list[dict]:
    async with db_session() as s:
        rows = (await s.execute(select(User).order_by(User.id))).scalars().all()
        return [
            {**_user(u), "is_active": u.is_active, "last_login_at": _iso(u.last_login_at)}
            for u in rows
        ]


@router.post("/admin/users/invite", dependencies=[Depends(auth.require_admin)])
async def invite_user(body: InviteIn, request: Request) -> dict:
    email = body.email.strip().lower()
    inviter = await auth.current_user(request)
    async with db_session() as s:
        exists = (
            await s.execute(select(func.count()).select_from(User).where(User.email == email))
        ).scalar_one()
        if exists:
            raise HTTPException(400, "a user with this email already exists")
        token = auth.new_invite_token()
        s.add(
            Invite(
                email=email,
                token=token,
                project_id=body.project_id,
                project_role=body.project_role,
                is_admin=body.is_admin,
                invited_by=inviter.id if inviter else None,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
    base = get_settings().public_base_url.rstrip("/")
    link = f"{base}/invite/{token}" if base else f"/invite/{token}"
    subject, mailbody = mailer.invite_email_body(inviter.email if inviter else "Potato Test", link)
    sent = await mailer.send_email(email, subject, mailbody)
    return {"email": email, "link": link, "emailed": sent}


@router.post("/admin/users/{uid}/reset-password", dependencies=[Depends(auth.require_admin)])
async def admin_reset_password(uid: int) -> dict:
    """Send a locked-out user a reset link. Admins never see or set the password itself —
    the link is single-use and expires, so it beats handing out a temporary one."""
    async with db_session() as s:
        u = await s.get(User, uid)
        if u is None:
            raise HTTPException(404, "user not found")
        if not u.is_active:
            raise HTTPException(400, "user is disabled — enable them first")
        info = {"email": u.email, "link": _reset_link(auth.create_reset_token(u))}
    return await _send_reset(info)


@router.patch("/admin/users/{uid}", dependencies=[Depends(auth.require_admin)])
async def update_user(uid: int, body: UserPatch) -> dict:
    async with db_session() as s:
        u = await s.get(User, uid)
        if u is None:
            raise HTTPException(404, "user not found")
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(u, k, v)
        await s.flush()
        return {**_user(u), "is_active": u.is_active}


# ---- admin: system settings ----




@router.get("/admin/settings", dependencies=[Depends(auth.require_admin)])
async def get_admin_settings() -> dict:
    from app.llm import llm_config
    from app.settings_store import GITLAB_TOKEN_KEY, has_setting

    async with db_session() as s:
        token_set = await has_setting(s, GITLAB_TOKEN_KEY)
        feishu_cfg = await feishu.resolve_config(s)
    return {
        "llm": _llm_status(await llm_config()),
        "gitlab_token_set": token_set or bool(get_settings().gitlab_token),
        "feishu": _feishu_status(feishu_cfg),
    }


@router.put("/admin/settings/llm", dependencies=[Depends(auth.require_admin)])
async def set_admin_llm(body: LlmSettingsIn) -> dict:
    """Set the platform's underlying model at runtime. Non-secret fields: None keeps,
    "" clears the override (falls back to env). The API key is write-only."""
    from app import llm as llm_mod
    from app.settings_store import set_setting

    async with db_session() as s:
        for value, key in (
            (body.base_url, llm_mod.LLM_BASE_URL_KEY),
            (body.model, llm_mod.LLM_MODEL_KEY),
            (body.agent_model, llm_mod.LLM_AGENT_MODEL_KEY),
        ):
            if value is not None:
                await set_setting(s, key, value.strip(), secret=False)
        if body.api_key:
            await set_setting(s, llm_mod.LLM_API_KEY_KEY, body.api_key.strip(), secret=True)
    llm_mod.invalidate_llm_cache()
    return _llm_status(await llm_mod.llm_config())


@router.post("/admin/settings/llm/test", dependencies=[Depends(auth.require_admin)])
async def test_admin_llm() -> dict:
    """Ping the configured gateway with a 1-token completion and report what came back.

    Exists because "saved but nothing works" is otherwise indistinguishable from a bad
    key, a wrong base URL, or a model name the gateway does not serve. Returns ok=False
    with the upstream message rather than raising, so the UI can show it inline.
    """
    from app import llm as llm_mod

    cfg = await llm_mod.llm_config()
    model = (cfg.agent_model or cfg.model or "").strip()
    if not cfg.base_url:
        return {"ok": False, "error": "Base URL is empty — nothing to call."}
    if not model:
        return {"ok": False, "error": "No model name set — fill in Model first."}

    try:
        from openai import AsyncOpenAI

        import httpx

        s = get_settings()
        # Mirror the real client exactly (same proxy policy, same TLS policy), otherwise
        # this probe says "ok" while the assistant/judge still fail — the most misleading
        # possible outcome for a diagnostic button.
        client = AsyncOpenAI(
            api_key=cfg.api_key or "not-needed",
            base_url=cfg.base_url,
            timeout=25.0,
            http_client=httpx.AsyncClient(
                verify=s.gateway_verify_ssl, trust_env=not s.gateway_ignore_proxy
            ),
        )
        resp = await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=1
        )
        return {
            "ok": True,
            "model": model,
            "reply_model": getattr(resp, "model", model),
            "api_key_set": bool(cfg.api_key),
        }
    except Exception as exc:  # noqa: BLE001 — surfaced verbatim to the operator
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "model": model}


@router.put("/admin/settings/gitlab-token", dependencies=[Depends(auth.require_admin)])
async def set_admin_gitlab_token(body: GitlabTokenIn) -> dict:
    from app.settings_store import GITLAB_TOKEN_KEY, set_setting

    _require_feature(get_settings().enable_gitlab, "GitLab")
    if body.token and not secret_configured():
        raise HTTPException(400, "POTATO_SECRET_KEY not configured — cannot store token")
    async with db_session() as s:
        await set_setting(s, GITLAB_TOKEN_KEY, body.token.strip(), secret=True)
    return {"gitlab_token_set": bool(body.token.strip())}


@router.put("/admin/settings/feishu", dependencies=[Depends(auth.require_admin)])
async def set_admin_feishu(body: FeishuSettingsIn) -> dict:
    from app.settings_store import set_setting

    _require_feature(get_settings().enable_feishu, "Feishu")
    async with db_session() as s:
        if body.app_id is not None:
            await set_setting(s, "feishu_app_id", body.app_id.strip(), secret=False)
        if body.api_base is not None:
            await set_setting(s, "feishu_api_base", body.api_base.strip(), secret=False)
        if body.auto_answer_detected is not None:
            await set_setting(
                s,
                "feishu_auto_answer_detected",
                "true" if body.auto_answer_detected else "false",
                secret=False,
            )
        # Secrets: only overwrite when a non-empty value is provided (blank keeps existing).
        for value, key in (
            (body.app_secret, "feishu_app_secret"),
            (body.verification_token, "feishu_verification_token"),
        ):
            if value:
                if not secret_configured():
                    raise HTTPException(
                        400, "POTATO_SECRET_KEY not configured — cannot store secret"
                    )
                await set_setting(s, key, value.strip(), secret=True)
        cfg = await feishu.resolve_config(s)
    return _feishu_status(cfg)


# ---- invite acceptance (public) ----
@public.get("/invite/{token}")
async def get_invite(token: str) -> dict:
    async with db_session() as s:
        inv = (await s.execute(select(Invite).where(Invite.token == token))).scalars().first()
        if inv is None or inv.accepted_at is not None:
            raise HTTPException(404, "invite not found or already used")
        if _expired(inv.expires_at):
            raise HTTPException(410, "invite expired")
        return {"email": inv.email, "is_admin": inv.is_admin, "project_id": inv.project_id}


@public.post("/invite/{token}/accept")
async def accept_invite(token: str, body: InviteAcceptIn, response: Response) -> dict:
    async with db_session() as s:
        inv = (await s.execute(select(Invite).where(Invite.token == token))).scalars().first()
        if inv is None or inv.accepted_at is not None:
            raise HTTPException(404, "invite not found or already used")
        if _expired(inv.expires_at):
            raise HTTPException(410, "invite expired")
        u = User(
            email=inv.email,
            name=body.name or inv.email.split("@")[0],
            password_hash=auth.hash_password(body.password),
            is_admin=inv.is_admin,
            is_active=True,
        )
        s.add(u)
        await s.flush()
        if inv.project_id is not None:
            s.add(ProjectMember(project_id=inv.project_id, user_id=u.id, role=inv.project_role))
        inv.accepted_at = datetime.now(UTC)
        token_str = auth.create_session_token(u.id)
        payload = _user(u)
    _set_session_cookie(response, token_str)
    return payload


# ---- project members ----
@router.get("/projects/{pid}/members")
async def list_members(pid: int, request: Request) -> list[dict]:
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        rows = (
            await s.execute(
                select(ProjectMember, User)
                .join(User, User.id == ProjectMember.user_id)
                .where(ProjectMember.project_id == pid)
                .order_by(ProjectMember.id)
            )
        ).all()
        return [
            {"user_id": u.id, "email": u.email, "name": u.name, "role": m.role} for m, u in rows
        ]


@router.get("/projects/{pid}/assignable-users")
async def assignable_users(pid: int, request: Request, q: str = "") -> list[dict]:
    """Active users (with an account) not already members — powers the member search picker."""
    await _project_access(request, pid, "owner")
    async with db_session() as s:
        member_ids = (
            (await s.execute(select(ProjectMember.user_id).where(ProjectMember.project_id == pid)))
            .scalars()
            .all()
        )
        query = select(User).where(User.is_active.is_(True))
        ql = q.strip().lower()
        if ql:
            like = f"%{ql}%"
            query = query.where(
                func.lower(User.email).like(like)
                | func.lower(func.coalesce(User.name, "")).like(like)
            )
        if member_ids:
            query = query.where(User.id.not_in(member_ids))
        rows = (await s.execute(query.order_by(User.email).limit(10))).scalars().all()
        return [{"id": u.id, "email": u.email, "name": u.name} for u in rows]


@router.post("/projects/{pid}/members")
async def add_member(pid: int, body: MemberIn, request: Request) -> dict:
    await _project_access(request, pid, "owner")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        u = (
            (await s.execute(select(User).where(User.email == body.email.strip().lower())))
            .scalars()
            .first()
        )
        if u is None:
            raise HTTPException(404, "no user with that email (invite them first)")
        existing = (
            (
                await s.execute(
                    select(ProjectMember).where(
                        ProjectMember.project_id == pid, ProjectMember.user_id == u.id
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing:
            existing.role = body.role
        else:
            s.add(ProjectMember(project_id=pid, user_id=u.id, role=body.role))
        return {"user_id": u.id, "email": u.email, "name": u.name, "role": body.role}


@router.patch("/projects/{pid}/members/{uid}")
async def update_member(pid: int, uid: int, body: MemberPatch, request: Request) -> dict:
    await _project_access(request, pid, "owner")
    async with db_session() as s:
        m = (
            (
                await s.execute(
                    select(ProjectMember).where(
                        ProjectMember.project_id == pid, ProjectMember.user_id == uid
                    )
                )
            )
            .scalars()
            .first()
        )
        if m is None:
            raise HTTPException(404, "member not found")
        m.role = body.role
        return {"user_id": uid, "role": body.role}


@router.delete("/projects/{pid}/members/{uid}")
async def remove_member(pid: int, uid: int, request: Request) -> dict:
    await _project_access(request, pid, "owner")
    async with db_session() as s:
        m = (
            (
                await s.execute(
                    select(ProjectMember).where(
                        ProjectMember.project_id == pid, ProjectMember.user_id == uid
                    )
                )
            )
            .scalars()
            .first()
        )
        if m is not None:
            await s.delete(m)
    return {"removed": uid}
























# ---- projects ----
@router.get("/projects")
async def list_projects(request: Request) -> list[dict]:
    async with db_session() as s:
        q = select(Project).order_by(Project.id.desc())
        # non-admins see only projects they're a member of (unless shared_workspace)
        settings = get_settings()
        user = await auth.current_user(request) if settings.auth_enabled else None
        if user is not None and not user.is_admin and not settings.shared_workspace:
            member_ids = (
                (
                    await s.execute(
                        select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
                    )
                )
                .scalars()
                .all()
            )
            if not member_ids:
                return []
            q = q.where(Project.id.in_(member_ids))
        rows = (await s.execute(q)).scalars().all()
        out = []
        for p in rows:
            d = _project(p)
            if not d["has_login_state"]:
                d["has_login_state"] = bool(
                    (
                        await s.execute(
                            select(func.count())
                            .select_from(Credential)
                            .where(
                                Credential.project_id == p.id,
                                Credential.is_active == True,
                                Credential.type == "storage_state",
                            )
                        )
                    ).scalar_one()
                )
            d["case_count"] = (
                await s.execute(
                    select(func.count()).select_from(TestCase).where(TestCase.project_id == p.id)
                )
            ).scalar_one()
            last = (
                await s.execute(
                    select(Run).where(Run.project_id == p.id).order_by(Run.id.desc()).limit(1)
                )
            ).scalar_one_or_none()
            d["last_run"] = (
                {
                    "id": last.id,
                    "status": last.status,
                    "pass_rate": (last.summary or {}).get("pass_rate") if last.summary else None,
                    "finished_at": _iso(last.finished_at),
                }
                if last
                else None
            )
            out.append(d)
        return out


@router.post("/projects")
async def create_project(body: ProjectIn, request: Request) -> dict:
    async with db_session() as s:
        # Seed the run-tuning fields from the server defaults instead of leaving them
        # NULL. NULL means "follow the system default", which would still work — but the
        # settings form renders NULL as an EMPTY box, so a freshly created project looks
        # unconfigured even though it is not. Seeding makes the effective values visible
        # and editable, and keeps every project consistent with the tuned defaults
        # (2026-10-04: 160s / 40 steps / 1 concurrent — 1 because a browser case needs
        # ~0.5 GB and this 16 GB soldered box only ever had 3.5-5 GB free).
        # A project can still blank any field to go back to "follow the default".
        _s = get_settings()
        p = Project(
            name=body.name,
            base_url=body.base_url,
            login_state=body.login_state,
            case_timeout_s=_s.case_timeout_s,
            case_max_steps=_s.case_max_steps,
            run_concurrency=_s.run_concurrency,
        )
        s.add(p)
        await s.flush()
        # the creator owns the project (so it's visible + manageable under RBAC)
        user = await auth.current_user(request)
        if user is not None:
            s.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
        return _project(p)


@router.put("/projects/{pid}")
async def update_project(pid: int, body: ProjectPatch, request: Request) -> dict:
    await _project_access(request, pid, "owner")
    async with db_session() as s:
        p = await _get_project_or_404(s, pid)
        data = body.model_dump(exclude_none=True)
        # 这两个证据开关的 null **有语义**（= 改回"跟随服务器默认"），而 exclude_none
        # 会把 null 直接丢掉，于是"恢复默认"这个操作永远表达不出来。所以单独按
        # model_fields_set 判断"调用方到底有没有传这个键"：传了就照传的值设（哪怕是 None），
        # 没传就完全不碰。
        for _k in ("case_record_video", "live_shot_every"):
            if _k in body.model_fields_set:
                setattr(p, _k, getattr(body, _k))
                data.pop(_k, None)
        if "feishu_bitable_url" in data:
            from app.feishu_bitable import parse_bitable_url

            app_token, table_id = parse_bitable_url(data.pop("feishu_bitable_url"))
            p.feishu_bitable_app_token = app_token
            p.feishu_bitable_table_id = table_id
        for k, v in data.items():
            setattr(p, k, v)
        await s.flush()
        return _project(p)


@router.get("/projects/{pid}/roles")
async def get_roles(pid: int, request: Request) -> dict:
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        p = await _get_project_or_404(s, pid)
        return {"roles": p.roles or []}


@router.put("/projects/{pid}/roles")
async def set_roles(pid: int, body: RolesIn, request: Request) -> dict:
    await _project_access(request, pid, "editor")
    async with db_session() as s:
        p = await _get_project_or_404(s, pid)
        # keep order, drop blanks/dupes
        seen: list[str] = []
        for r in body.roles:
            r = r.strip()
            if r and r not in seen:
                seen.append(r)
        p.roles = seen
        return {"roles": seen}




async def _default_env_id(s, pid: int) -> int | None:
    """The project's default environment. A run without an explicit env must fall back to
    it — base_url and (env, role)->account both live on the environment, so env=None means
    env-bound accounts never match ("角色「x」没有可用账号")."""
    e = (
        (
            await s.execute(
                select(Environment)
                .where(Environment.project_id == pid, Environment.is_default == True)
                .order_by(Environment.id)
            )
        )
        .scalars()
        .first()
    )
    return e.id if e else None


@router.get("/projects/{pid}/environments")
async def list_environments(pid: int, request: Request) -> list[dict]:
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        rows = (
            (
                await s.execute(
                    select(Environment)
                    .where(Environment.project_id == pid)
                    .order_by(Environment.id)
                )
            )
            .scalars()
            .all()
        )
        return [_env(e) for e in rows]


@router.post("/projects/{pid}/environments")
async def create_environment(pid: int, body: EnvironmentIn, request: Request) -> dict:
    await _project_access(request, pid, "editor")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        if body.is_default:
            for e in (
                (await s.execute(select(Environment).where(Environment.project_id == pid)))
                .scalars()
                .all()
            ):
                e.is_default = False
        e = Environment(
            project_id=pid, name=body.name, base_url=body.base_url, is_default=body.is_default
        )
        s.add(e)
        await s.flush()
        return _env(e)


@router.put("/environments/{eid}")
async def update_environment(eid: int, body: EnvironmentPatch, request: Request) -> dict:
    async with db_session() as s:
        e = await s.get(Environment, eid)
        if e is None:
            raise HTTPException(404, "environment not found")
        await _project_access(request, e.project_id, "editor")
        data = body.model_dump(exclude_none=True)
        if data.get("is_default"):
            for other in (
                (await s.execute(select(Environment).where(Environment.project_id == e.project_id)))
                .scalars()
                .all()
            ):
                other.is_default = False
        for k, v in data.items():
            setattr(e, k, v)
        return _env(e)


@router.delete("/environments/{eid}")
async def delete_environment(eid: int, request: Request) -> dict:
    async with db_session() as s:
        e = await s.get(Environment, eid)
        if e is None:
            raise HTTPException(404, "environment not found")
        await _project_access(request, e.project_id, "editor")
        await s.delete(e)
        return {"deleted": eid}


@router.delete("/projects/{pid}")
async def delete_project(pid: int, request: Request) -> dict:
    """Delete a project and everything under it. Owner-only, irreversible."""
    await _project_access(request, pid, "owner")
    async with db_session() as s:
        p = await _get_project_or_404(s, pid)
        run_ids = (await s.execute(select(Run.id).where(Run.project_id == pid))).scalars().all()
        issue_ids = (
            (await s.execute(select(Issue.id).where(Issue.project_id == pid))).scalars().all()
        )
        if run_ids:
            await s.execute(delete(RunResult).where(RunResult.run_id.in_(run_ids)))
        if issue_ids:
            await s.execute(delete(IssueComment).where(IssueComment.issue_id.in_(issue_ids)))
        for model in (TestCase, Run, Issue, Credential, ProjectMember, TestSuite, Environment):
            await s.execute(delete(model).where(model.project_id == pid))
        await s.delete(p)
        return {"deleted": pid}


@router.get("/projects/{pid}/browser-state")
async def get_browser_state(pid: int, request: Request) -> dict:
    """Report the project's persistent browser profile pool (the workspace-scoped
    cookies/localStorage/sessionStorage/IndexedDB store shared by this project's cases)."""
    await _project_access(request, pid, "viewer")
    from app.executor import browser_state_info

    return browser_state_info(pid)


@router.delete("/projects/{pid}/browser-state")
async def reset_browser_state(pid: int, request: Request) -> dict:
    """Wipe this project's persistent browser profiles. Next case starts cold (re-login,
    re-fetch assets); use it when a stale cookie or a corrupted profile is breaking runs."""
    await _project_access(request, pid, "editor")
    from app.executor import reset_project_profiles

    reset_project_profiles(pid)
    return {"reset": pid}


@router.get("/projects/{pid}/gitlab")
async def get_gitlab_config(pid: int, request: Request) -> dict:
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        p = await _get_project_or_404(s, pid)
        token = (
            (
                await s.execute(
                    select(Credential).where(
                        Credential.project_id == pid,
                        Credential.type == "gitlab_token",
                        Credential.is_active == True,  # noqa: E712
                    )
                )
            )
            .scalars()
            .first()
        )
        has_global = bool(get_settings().gitlab_token)
        source = "project" if token is not None else ("global" if has_global else None)
        return {
            "gitlab_project": p.gitlab_project,
            "has_token": token is not None or has_global,
            "token_source": source,
            "sync_enabled": bool(get_settings().redis_url and get_settings().enable_gitlab),
        }


@router.get("/projects/{pid}/gitlab/projects")
async def list_gitlab_projects(pid: int, request: Request) -> list[dict]:
    """GitLab projects the token can access — populates the settings dropdown. Uses the
    project's token or the server-wide GITLAB_TOKEN; empty list if neither is set."""
    _require_feature(get_settings().enable_gitlab, "GitLab")
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        token = await resolve_token(s, pid)
    if not token:
        return []
    client = GitLabClient(token, project="")
    try:
        items = await client.list_accessible_projects()
    except Exception as e:
        raise HTTPException(502, f"could not list GitLab projects: {e}") from e
    return [
        {"id": p["id"], "path": p["path_with_namespace"], "name": p.get("name")}
        for p in items
        if p.get("path_with_namespace")
    ]


@router.put("/projects/{pid}/gitlab")
async def set_gitlab_config(pid: int, body: GitLabConfigIn, request: Request) -> dict:
    _require_feature(get_settings().enable_gitlab, "GitLab")
    await _project_access(request, pid, "owner")
    """Map this project to a GitLab project and (optionally) store an encrypted token.
    An empty gitlab_project unlinks the project; the token is write-only (never returned)."""
    async with db_session() as s:
        p = await _get_project_or_404(s, pid)
        p.gitlab_project = body.gitlab_project or None
        if body.token:
            if not secret_configured():
                raise HTTPException(
                    400, "POTATO_SECRET_KEY not configured — token storage disabled"
                )
            for c in (
                (
                    await s.execute(
                        select(Credential).where(
                            Credential.project_id == pid, Credential.type == "gitlab_token"
                        )
                    )
                )
                .scalars()
                .all()
            ):
                c.is_active = False
            s.add(
                Credential(
                    project_id=pid,
                    type="gitlab_token",
                    label="GitLab token",
                    secret=encrypt(body.token),
                    is_active=True,
                )
            )
        await s.flush()
        project_token = (
            await s.execute(
                select(func.count())
                .select_from(Credential)
                .where(
                    Credential.project_id == pid,
                    Credential.type == "gitlab_token",
                    Credential.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one() > 0
        has_global = bool(get_settings().gitlab_token)
        source = "project" if project_token else ("global" if has_global else None)
        return {
            "gitlab_project": p.gitlab_project,
            "has_token": project_token or has_global,
            "token_source": source,
            "sync_enabled": bool(get_settings().redis_url and get_settings().enable_gitlab),
        }


@router.post("/projects/{pid}/assistant")
async def project_assistant(pid: int, body: AssistantIn, request: Request) -> dict:
    """In-app chat assistant: one turn against this project, with tools that read
    cases/runs/issues and (on request) create a case or start a run."""
    await _project_access(request, pid, "viewer")
    from app.assistant import assistant_turn

    return await assistant_turn(pid, body.message, body.history)


@router.get("/projects/{pid}/knowledge")
async def get_project_knowledge(pid: int, request: Request) -> dict:
    """The project's spec/knowledge text — searched by the assistant's search_knowledge tool.

    Reassembled from chunks for the editor. Nothing on the assistant path calls this;
    it would defeat the point of chunking (see app/knowledge.py).
    """
    await _project_access(request, pid, "viewer")
    from app import knowledge

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        text = await knowledge.get_all_text(s, pid)
        st = await knowledge.stats(s, pid)
    return {"text": text, "chars": st["chars"], **st}


@router.put("/projects/{pid}/knowledge")
async def set_project_knowledge(pid: int, body: KnowledgeIn, request: Request) -> dict:
    """Replace the project's spec/knowledge text (paste or upload the requirement doc).

    Stored as chunks, not one row: retrieval cost then depends on the query, not on
    how large the document is.
    """
    await _project_access(request, pid, "editor")
    from app import knowledge

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        res = await knowledge.replace_knowledge(s, pid, body.text or "", source="paste")
    return res


@router.post("/projects/{pid}/knowledge/extract")
async def extract_project_knowledge(pid: int, request: Request, file: UploadFile = File(...)) -> dict:
    """Parse an uploaded spec document into plain text WITHOUT saving it.

    The SPA appends the returned text to the spec box and then PUTs /knowledge, so
    the operator can see what was extracted before it becomes the assistant's
    knowledge. Formats: md/txt/json/csv/docx/pdf/xlsx/html/rtf, plus any plain text.

    The upload is read in bounded pieces and rejected on the declared length first, so
    a 500 MB body cannot be buffered into memory before the size check runs.
    """
    await _project_access(request, pid, "editor")
    data = await _read_upload_capped(file, docparse.MAX_UPLOAD_BYTES)
    try:
        res = docparse.extract(file.filename or "", data)
    except docparse.UnsupportedDocument as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001 — corrupt file: one clear message, not a 500
        raise HTTPException(400, f"could not read «{file.filename}»: {e}") from e
    return {
        "filename": file.filename,
        "format": res.fmt,
        "text": res.text,
        "chars": res.chars,
        "truncated": res.truncated,
        "warnings": res.warnings,
    }


@router.post("/projects/{pid}/knowledge/append")
async def append_project_knowledge(pid: int, body: KnowledgeIn, request: Request) -> dict:
    """Add text to the project's knowledge without replacing what is already there.

    Uploading several documents into one project is the normal case; this avoids the
    SPA having to read back a multi-megabyte document, concatenate, and write it all
    again on every added file.
    """
    await _project_access(request, pid, "editor")
    from app import knowledge

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        res = await knowledge.append_knowledge(s, pid, body.text or "", source="upload")
    return res


@router.post("/projects/{pid}/knowledge/search")
async def search_project_knowledge(pid: int, request: Request, body: KnowledgeIn) -> dict:
    """Preview what the assistant's search_knowledge tool would return for a query."""
    await _project_access(request, pid, "viewer")
    from app import knowledge

    async with db_session() as s:
        await _get_project_or_404(s, pid)
    return await knowledge.search(pid, body.text or "")


@router.get("/projects/{pid}/stats")
async def project_stats(pid: int, request: Request) -> dict:
    await _project_access(request, pid, "viewer")
    """Aggregates for the Overview dashboard: counts, pass-rate trend, last-run
    latency/flaky, and the cases that fail most across this project's runs."""
    async with db_session() as s:
        project = await _get_project_or_404(s, pid)

        case_count = (
            await s.execute(
                select(func.count()).select_from(TestCase).where(TestCase.project_id == pid)
            )
        ).scalar_one()
        enabled_count = (
            await s.execute(
                select(func.count())
                .select_from(TestCase)
                .where(TestCase.project_id == pid, TestCase.enabled == True)
            )
        ).scalar_one()

        # Setup-checklist inputs (Overview). The checklist is derived from live state,
        # never from a stored "onboarding done" flag — so it can't drift.
        cred_rows = (
            (await s.execute(select(Credential.healthy).where(Credential.project_id == pid)))
            .scalars()
            .all()
        )
        issue_count = (
            await s.execute(select(func.count()).select_from(Issue).where(Issue.project_id == pid))
        ).scalar_one()

        runs = (
            (await s.execute(select(Run).where(Run.project_id == pid).order_by(Run.id)))
            .scalars()
            .all()
        )
        completed = [r for r in runs if r.status in ("completed", "cancelled") and r.summary]
        trend = [
            {
                "run_id": r.id,
                "name": r.name,
                "pass_rate": (r.summary or {}).get("pass_rate", 0.0),
                "finished_at": _iso(r.finished_at),
            }
            for r in completed[-10:]
        ]
        last = runs[-1] if runs else None
        last_summary = (last.summary or {}) if last else {}

        # cases that fail most across all runs in this project
        fail_rows = (
            await s.execute(
                select(RunResult.case_id, func.count().label("n"))
                .join(Run, RunResult.run_id == Run.id)
                .where(Run.project_id == pid, RunResult.status.in_(("failed", "error")))
                .group_by(RunResult.case_id)
                .order_by(func.count().desc())
                .limit(5)
            )
        ).all()
        names = (
            {
                c.id: c.name
                for c in (
                    await s.execute(
                        select(TestCase).where(TestCase.id.in_([r.case_id for r in fail_rows]))
                    )
                ).scalars()
            }
            if fail_rows
            else {}
        )
        top_failing = [
            {"case_id": r.case_id, "name": names.get(r.case_id, f"#{r.case_id}"), "fail_count": r.n}
            for r in fail_rows
        ]

        return {
            "base_url": project.base_url,
            "has_credential": bool(cred_rows),
            "has_healthy_credential": any(cred_rows),
            "issue_count": issue_count,
            "case_count": case_count,
            "enabled_count": enabled_count,
            "run_count": len(runs),
            "last_run": _run(last) if last else None,
            "trend": trend,
            "last_pass_rate": last_summary.get("pass_rate"),
            "last_latency_p50_ms": last_summary.get("latency_p50_ms"),
            "last_flaky": last_summary.get("flaky", 0),
            "top_failing": top_failing,
        }


# ---- test cases ----
async def _get_project_or_404(s, pid: int) -> Project:
    p = await s.get(Project, pid)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


@router.get("/projects/{pid}/testcases")
async def list_cases(pid: int, request: Request) -> list[dict]:
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        rows = (
            (
                await s.execute(
                    select(TestCase).where(TestCase.project_id == pid).order_by(TestCase.id)
                )
            )
            .scalars()
            .all()
        )
        # latest RunResult per case (coverage / pass-rate / compare, and the cases table's
        # "last result" column); one query, newest-first, keep the first row seen per case.
        last: dict[int, LastResult] = {}
        case_ids = [c.id for c in rows]
        if case_ids:
            res_rows = (
                await s.execute(
                    select(
                        RunResult.case_id,
                        RunResult.status,
                        RunResult.run_id,
                        RunResult.created_at,
                    )
                    .where(RunResult.case_id.in_(case_ids))
                    .order_by(RunResult.id.desc())
                )
            ).all()
            for cid, st, run_id, at in res_rows:
                if cid not in last:
                    last[cid] = LastResult(st, run_id, at)
        return [_case(c, last.get(c.id)) for c in rows]


async def _next_case_key(s, pid: int) -> str:
    """Readable per-project id like TC-001.

    2026-10-04 修两个缺陷：

    1. **删过用例就会重号。** 旧实现用 `COUNT(*)` 当序号：删掉 TC-005 后总数少 1，
       下一条新建的又拿到 TC-005 —— 两条不同用例撞同一个编号，报告和缺陷单里
       无法区分。改成取**已用编号的最大值 +1**，序号只增不减。
    2. **跨项目只靠前缀区分。** 每个项目都从 TC-001 开始，所以光看 "TC-001"
       根本不知道是哪个项目的。这里保持 TC-NNN 的显示格式（563 条存量用例都在用，
       改格式等于让所有历史报告对不上号），但把项目维度交给
       `(project_id, case_key)` 唯一约束去保证，并在所有按编号查找的地方
       强制带 project_id —— 见 _find_case_by_key。
    """
    rows = (
        await s.execute(
            select(TestCase.case_key).where(
                TestCase.project_id == pid, TestCase.case_key.is_not(None)
            )
        )
    ).scalars().all()

    top = 0
    for k in rows:
        # 只认 TC-<数字> 形式；导入的 Excel 里可能有自由文本编号（如 "REQ-12"），
        # 那些不参与序号计算，否则 int() 会抛异常把整批导入打断。
        m = re.fullmatch(r"TC-(\d+)", (k or "").strip())
        if m:
            top = max(top, int(m.group(1)))
    return f"TC-{top + 1:03d}"


@router.post("/projects/{pid}/testcases")
async def create_case(pid: int, body: TestCaseIn, request: Request) -> dict:
    await _project_access(request, pid, "editor")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        data = body.model_dump()
        if not data.get("case_key"):
            data["case_key"] = await _next_case_key(s, pid)
        # 2026-10-04 多角色：归一化后再落库，并让 role 始终镜像 roles[0]。
        # 这样无论读哪一列、无论请求里只传了 role 还是只传了 roles，
        # 存下来的数据都自洽 —— 执行层也只需要认 roles 一个来源。
        _rs = _roles_of(body.role, body.roles)
        data["roles"] = _rs
        data["role"] = _rs[0] if _rs else None
        c = TestCase(project_id=pid, **data)
        s.add(c)
        await s.flush()
        return _case(c)


@router.put("/testcases/{cid}")
async def update_case(cid: int, body: TestCasePatch, request: Request) -> dict:
    async with db_session() as s:
        c = await s.get(TestCase, cid)
        if c is None:
            raise HTTPException(404, "test case not found")
        await _project_access(request, c.project_id, "editor")
        data = body.model_dump(exclude_none=True)
        # 2026-10-04 多角色归一化。三种传法都要区分清楚，否则会出现两种错：
        #
        #   roles 被改动 -> 顺序被旧 role 打乱。
        #     上一版把 c.role（库里那个旧首项）当"缺省值"混进 _roles_of，
        #     结果用户把单角色改成多角色时，旧 role 仍留在库里并被排到最前，
        #     实际拿到 ['admin','applicant','hr'] 而不是用户设的顺序。
        #     **传了 roles 就以 roles 为准，不再回头看旧 role。**
        #   roles=[] 传了却没生效。
        #     `exclude_none=True` 会把 [] 留下（空列表不是 None），但旧写法还
        #     叠加了 `body.roles is not None` 的分支去回落旧值，于是清空失效。
        #
        # 规则：调用方显式传了 roles -> 只按 roles 算（空列表就是清空成默认账号）；
        #       只传了 role     -> 单角色更新；
        #       都没传           -> 保持原样，不碰这两列。
        if "roles" in body.model_fields_set:
            # 只认本次传来的 roles；旧 role 一律不参与（它就是顺序错乱的来源）
            _rs = _roles_of(body.role, body.roles or [])
            data["roles"] = _rs
            data["role"] = _rs[0] if _rs else None
        elif "role" in body.model_fields_set:
            # 只改了单角色字段
            _rs = _roles_of(body.role, None)
            data["roles"] = _rs
            data["role"] = _rs[0] if _rs else None
        # 2026-10-06 测试数据声明在**保存时**就校验。
        # 为什么不留到执行期：执行期遇到坏声明会降级成"没有文件"（见
        # app/testdata_runtime.py），那对运行是对的，但对用户是延迟的坏体验 ——
        # 他要等一条用例跑完，才知道自己填错了。所以这里 400 回去，界面当场显示。
        if "data_files" in body.model_fields_set:
            from app import testdata

            try:
                # parse() 兼做归一化：字符串 JSON / 裸数组都能收下
                data["data_files"] = {"files": testdata.parse(body.data_files)} or None
            except testdata.SpecError as exc:
                raise HTTPException(422, f"测试数据声明无效：{exc}") from exc
        # 改动前先快照这些字段的值 —— setattr 是就地的，事后拿不到旧值。
        # 审计要回答"哪一格被动了"，只有改之前的值才能回答。
        for k in data:
            _AUDIT_BEFORE[(id(c), k)] = _str_field(getattr(c, k, None))
        for k, v in data.items():
            setattr(c, k, v)
        await s.flush()
        # ★ Must be awaited. `_audit_case_change` is async (it awaits the session to add the
        # row), and calling it without `await` produces a bare coroutine that never runs —
        # the case change persists and `case_change` stays empty forever. Found on
        # 2026-10-07 by an end-to-end check after wiring the assistant's case tools: the
        # change was visible in the DB while the audit table had zero rows in it, and the
        # only warning was a RuntimeWarning at GC time, far from the call site.
        #
        # The tell is worth remembering: **an audit that is silently always empty is the
        # signature of a coroutine nobody awaited.** An audit log that works is boring; one
        # that is uniformly empty is not "unused feature", it is broken plumbing.
        await _audit_case_change(s, c, body, data, request)
        return _case(c)


# 改写前的值暂存。用模块级 dict 而不是参数透传，是为了不打乱 setattr 的就地语义；
# 键是 (id(obj), field)，update_case 同一事务内 id 不会复用。
_AUDIT_BEFORE: dict[tuple[int, str], str] = {}
# 助手改用例时带上"这是为了清单第几条"，便于从清单反查改动。
_AUDIT_SIGNAL: dict[tuple[int, str], str] = {}


def _decode_header_label(value: str) -> str:
    """Restore a percent-encoded ``x-change-by`` to the text the sender meant.

    ★ Header values are ASCII, so a Chinese audit reason has to be percent-encoded to survive
    the trip — see ``assistant._header_safe``, which does the encoding. This is the other half.

    Without this the audit table stores ``%E4%B8%8E%20TC-001%20...`` and the UI shows the operator
    a wall of escapes instead of "与 TC-001 重复". Encoding without decoding keeps the request
    legal but loses the only thing the column is for.

    ``unquote`` is applied on a copy of the original only when it actually looks encoded:
    ``%`` is a legal character in a plain label, and a human header like ``50% done`` must not be
    mangled into ``50%E2%80%8Bdone``. ``unquote`` leaves invalid escapes alone, so a false positive
    here degrades to "unchanged" rather than to corrupted text.
    """
    v = value or ""
    if "%" not in v:
        return v
    try:
        decoded = unquote(v, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return v
    # A decode is only believed if it introduced characters that needed encoding in the first
    # place. Decoding "50%20off" is legitimate too, so this is a sanity check, not a filter.
    return decoded or v


async def _audit_case_change(
    s,
    case: TestCase,
    body: "TestCasePatch",
    applied: dict,
    request: Request | None = None,
) -> None:
    """把一条用例的字段级改动写进 case_change 审计表。

    单独抽出来是因为 PUT /testcases/{cid} 和助手自动改用例走同一条路——
    助手那侧不该另写一份记录逻辑，否则两边迟早漂移。
    """
    # 只看调用方**显式传了**的字段：exclude_none 会把没传的也带进来，
    # 而模型常常带着一堆未修改字段回传，全记一遍审计表就没人读了。
    explicit = (set(body.model_fields_set) & set(applied.keys())) - _AUDIT_SKIP_FIELDS
    if not explicit:
        return

    # source：助手那条路会带 x-change-source 头，分不开的宁可贵——
    # "把助手的改动混进人改的"比反过来危险得多。
    source = "human"
    by_label = ""
    if request is not None:
        source = request.headers.get("x-change-source") or "human"
        by_label = _decode_header_label(request.headers.get("x-change-by") or "")
    if source not in ("human", "assistant", "import"):
        source = "human"

    for field in sorted(explicit):
        before = _AUDIT_BEFORE.pop((id(case), field), "")
        signal = _AUDIT_SIGNAL.pop((id(case), field), "")
        after = _str_field(getattr(case, field, None))
        if before == after:
            continue  # 没真变，不记
        s.add(CaseChange(
            project_id=case.project_id,
            case_id=case.id,
            field=field,
            before=before[:2000],
            after=after[:2000],
            source=source,
            by_label=by_label[:200],
            digest_signal=(signal or "")[:80],
        ))
    # 清掉没被消费的快照（值没变的字段也会进这里）
    for field in list(applied):
        _AUDIT_BEFORE.pop((id(case), field), None)


# 审计时忽略的字段：元数据类，改它们不影响用例语义，记录只会淹没审计表。
_AUDIT_SKIP_FIELDS = frozenset({"updated_at", "memory", "memory_fingerprint", "memory_updated_at"})


def _str_field(v) -> str:
    """把字段值转成可比较/可读的字符串。JSON 列存 json 字符串，不存 repr。"""
    import json as _json

    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            return _json.dumps(v, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


@router.delete("/testcases/{cid}/memory")
async def clear_case_memory(cid: int, request: Request) -> dict:
    """清空一条用例的"操作经验记忆"（见 app/case_memory.py）。

    什么时候用户会想清空：
      * 记忆里记错了（比如页面改版，原来记的路径/元素已经不存在了）——
        虽然指纹机制会在用例被改动时自动作废，但页面本身变了用例内容却没变，
        这种就得手动清；
      * 单纯想让 agent 完全从零跑一遍，看看它现在自己能不能走通。
    """
    async with db_session() as s:
        c = await s.get(TestCase, cid)
        if c is None:
            raise HTTPException(404, "test case not found")
        await _project_access(request, c.project_id, "editor")
        c.memory = None
        c.memory_fingerprint = None
        c.memory_updated_at = None
        await s.flush()
        return _case(c)


@router.delete("/testcases/{cid}")
async def delete_case(cid: int, request: Request) -> dict:
    async with db_session() as s:
        c = await s.get(TestCase, cid)
        if c is None:
            raise HTTPException(404, "test case not found")
        await _project_access(request, c.project_id, "editor")
        # run_result.case_id is a non-cascading FK — drop the case's historical
        # results first, otherwise the delete violates run_result_case_id_fkey.
        await s.execute(delete(RunResult).where(RunResult.case_id == cid))
        await s.delete(c)
        return {"deleted": cid}




@router.get("/projects/{pid}/credentials")
async def list_credentials(pid: int, request: Request) -> list[dict]:
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        await _project_access(request, pid, "viewer")
        rows = (
            (
                await s.execute(
                    select(Credential)
                    # gitlab_token is managed by the GitLab-sync section, not the login list
                    .where(Credential.project_id == pid, Credential.type != "gitlab_token")
                    .order_by(Credential.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_cred(c) for c in rows]


@router.post("/projects/{pid}/credentials")
async def create_credential(pid: int, body: CredentialIn, request: Request) -> dict:
    if not secret_configured():
        raise HTTPException(
            400, "POTATO_SECRET_KEY not configured — credential storage disabled"
        )
    if body.type == "password" and not body.username:
        raise HTTPException(400, "username is required for a password credential")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        await _project_access(request, pid, "editor")
        # the default login is a single active storage_state; adding a new one deactivates
        # the previous storage_state only. Role/password accounts coexist (multi-account).
        if body.type == "storage_state":
            existing = (
                (
                    await s.execute(
                        select(Credential).where(
                            Credential.project_id == pid, Credential.type == "storage_state"
                        )
                    )
                )
                .scalars()
                .all()
            )
            for e in existing:
                e.is_active = False
        c = Credential(
            project_id=pid,
            type=body.type,
            role=body.role,
            environment_id=body.environment_id,
            label=body.label or body.type,
            username=body.username,
            secret=encrypt(body.secret),
            is_active=True,
        )
        s.add(c)
        await s.flush()
        return _cred(c)


@router.post("/projects/{pid}/credentials/capture")
async def capture_credential(pid: int, body: CredentialCaptureIn, request: Request) -> dict:
    """Web-only login: server logs into the project's base_url with this account,
    captures the session, and stores it (active) plus the account (for refresh)."""
    if not secret_configured():
        raise HTTPException(
            400, "POTATO_SECRET_KEY not configured — credential storage disabled"
        )
    async with db_session() as s:
        project = await _get_project_or_404(s, pid)
        await _project_access(request, pid, "editor")
        base_url = project.base_url
    if not base_url:
        raise HTTPException(400, "set the project Base URL first")

    from app.executor import capture_session

    try:
        state = await capture_session(base_url, body.username, body.password, pid)
    except Exception as exc:
        raise HTTPException(
            502, f"login capture failed: {type(exc).__name__}: {exc}"[:300]
        ) from exc

    async with db_session() as s:
        existing = (
            (await s.execute(select(Credential).where(Credential.project_id == pid)))
            .scalars()
            .all()
        )
        for e in existing:
            e.is_active = False
        # store the account (inactive, for future refresh) + the captured session (active)
        s.add(
            Credential(
                project_id=pid,
                type="password",
                label=(body.label or "robot account"),
                username=body.username,
                secret=encrypt(body.password),
                is_active=False,
            )
        )
        session_cred = Credential(
            project_id=pid,
            type="storage_state",
            label=(body.label or f"{body.username} session"),
            secret=encrypt(state),
            is_active=True,
        )
        s.add(session_cred)
        await s.flush()
        return _cred(session_cred)


@router.post("/credentials/{cid}/activate")
async def activate_credential(cid: int, request: Request) -> dict:
    async with db_session() as s:
        c = await s.get(Credential, cid)
        if c is None:
            raise HTTPException(404, "credential not found")
        await _project_access(request, c.project_id, "editor")
        others = (
            (await s.execute(select(Credential).where(Credential.project_id == c.project_id)))
            .scalars()
            .all()
        )
        for o in others:
            o.is_active = o.id == cid
        return _cred(c)


@router.post("/credentials/{cid}/recheck")
async def recheck_credential(cid: int, request: Request) -> dict:
    """Re-validate a password account by logging into its environment's base URL, and
    update its health. Only password accounts (they carry the login) can be re-checked."""
    async with db_session() as s:
        c = await s.get(Credential, cid)
        if c is None:
            raise HTTPException(404, "credential not found")
        await _project_access(request, c.project_id, "editor")
        if c.type != "password" or not c.username:
            raise HTTPException(400, "only password accounts can be re-checked")
        project = await s.get(Project, c.project_id)
        base_url = project.base_url if project else None
        if c.environment_id is not None:
            env = await s.get(Environment, c.environment_id)
            if env is not None and env.base_url:
                base_url = env.base_url
        username = c.username
        password = decrypt(c.secret)
        # 在 session 内取出项目 id：db_session 关闭后 ORM 对象可能已过期，
        # 那时再读 c.project_id 会触发 refresh 并报 DetachedInstanceError。
        c_pid = c.project_id
    if not base_url:
        raise HTTPException(400, "set the project or environment Base URL first")

    from app.executor import capture_session

    ok = True
    err: str | None = None
    try:
        await capture_session(base_url, username, password, c_pid)
    except Exception as exc:
        ok = False
        err = f"{type(exc).__name__}: {exc}"[:400]

    async with db_session() as s:
        c = await s.get(Credential, cid)
        if c is None:
            raise HTTPException(404, "credential not found")
        c.healthy = ok
        c.last_error = None if ok else err
        c.last_checked_at = datetime.now(UTC)
        return _cred(c)


@router.patch("/credentials/{cid}")
async def update_credential(cid: int, body: CredentialPatch, request: Request) -> dict:
    """就地修改一条既有凭据（密码轮换、改标签/角色/环境）。

    只有传了的字段会被改（`model_fields_set`），没传的保持原值。

    改密码或改用户名时**必须丢掉 `session_bundle`**，这不是顺手清理，是正确性要求：
      * 会话缓存是"上一个身份"的登录态。换了用户名还沿用旧缓存，等于让用例跑在
        别人的身份下 —— 这正是本轮花大力气修掉的那类污染（见 _enforce_identity）。
      * 密码改错时（如 role01 那份只有百度统计 cookie 的假 bundle），
        缓存里存的根本不是一个有效登录，留着只会让下一次运行继续"看起来正常"。
    代价是下一次运行要重新登录一次，这是应该付的价。
    """
    if not secret_configured():
        raise HTTPException(
            400, "POTATO_SECRET_KEY not configured — credential storage disabled"
        )
    fields = body.model_fields_set
    async with db_session() as s:
        c = await s.get(Credential, cid)
        if c is None:
            raise HTTPException(404, "credential not found")
        await _project_access(request, c.project_id, "editor")

        if "label" in fields and body.label is not None:
            c.label = body.label
        if "role" in fields:
            c.role = body.role or None
        if "environment_id" in fields:
            c.environment_id = body.environment_id

        identity_changed = False
        if "username" in fields and body.username != c.username:
            c.username = body.username or None
            identity_changed = True
        if "secret" in fields and body.secret is not None:
            c.secret = encrypt(body.secret)
            identity_changed = True

        # 密码账号必须有用户名，否则这条凭据没法用来登录（create 也守这一条）。
        if c.type == "password" and not (c.username or "").strip():
            raise HTTPException(400, "username is required for a password credential")

        if identity_changed:
            c.session_bundle = None
            c.last_error = None
        return _cred(c)


@router.delete("/credentials/{cid}")
async def delete_credential(cid: int, request: Request) -> dict:
    async with db_session() as s:
        c = await s.get(Credential, cid)
        if c is None:
            raise HTTPException(404, "credential not found")
        await _project_access(request, c.project_id, "editor")
        await s.delete(c)
        return {"deleted": cid}


@router.get("/testcases/{cid}/results")
async def case_results(cid: int, request: Request) -> list[dict]:
    """This case's outcome across every run it appeared in (newest first)."""
    async with db_session() as s:
        c = await s.get(TestCase, cid)
        if c is None:
            raise HTTPException(404, "test case not found")
        await _project_access(request, c.project_id, "viewer")
        rows = (
            await s.execute(
                select(RunResult, Run)
                .join(Run, RunResult.run_id == Run.id)
                .where(RunResult.case_id == cid)
                .order_by(Run.id.desc())
            )
        ).all()
        return [
            {
                "result_id": rr.id,
                "run_id": run.id,
                "run_name": run.name,
                "status": rr.status,
                "flaky": rr.flaky,
                "latency_ms": rr.latency_ms,
                "judge_reason": rr.judge_reason,
                "video_url": rr.video_url,
                "trace_url": rr.trace_url,
                "finished_at": _iso(run.finished_at),
            }
            for rr, run in rows
        ]


@router.post("/projects/{pid}/testcases/import")
async def import_cases(pid: int, request: Request, file: UploadFile = File(...)) -> dict:
    """Bulk-import test cases from an .xlsx (the only supported format)."""
    data = await _read_upload_capped(file, docparse.MAX_UPLOAD_BYTES)
    try:
        records = excel.parse_workbook(data)
    except Exception as e:
        raise HTTPException(400, f"could not read Excel file: {e}") from e
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        await _project_access(request, pid, "editor")
        imported = 0
        for rec in records:
            try:
                item = TestCaseIn(**rec)
            except Exception:
                continue  # skip malformed rows rather than failing the whole import
            payload = item.model_dump()
            if not payload.get("case_key"):
                payload["case_key"] = await _next_case_key(s, pid)
            s.add(TestCase(project_id=pid, **payload))
            await s.flush()
            imported += 1
    return {"imported": imported}


@router.get("/projects/{pid}/testcases/export")
async def export_cases(pid: int, request: Request) -> StreamingResponse:
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        await _project_access(request, pid, "viewer")
        rows = (
            (
                await s.execute(
                    select(TestCase).where(TestCase.project_id == pid).order_by(TestCase.id)
                )
            )
            .scalars()
            .all()
        )
    content = excel.build_workbook([_case(c) for c in rows])
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="project-{pid}-testcases.xlsx"'},
    )


@router.get("/projects/{pid}/testcases/template")
async def testcase_template(pid: int) -> StreamingResponse:
    return StreamingResponse(
        iter([excel.template_bytes()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="potato-test-testcase-template.xlsx"'},
    )


# ---- runs ----
async def _launch(run_id: int) -> None:
    """Kick off a run: fan its cases out as Celery case-tasks when Redis is configured
    (worker --concurrency is the global budget), else run in-process (dev/no-broker)."""
    if get_settings().redis_url:
        from app.celery_app import fan_out_run
        from app.engine import prepare_run

        case_ids = await prepare_run(run_id)
        if case_ids:
            fan_out_run(run_id, case_ids)
        return
    task = asyncio.create_task(run_suite(run_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


@router.post("/projects/{pid}/runs")
async def create_run(pid: int, body: RunIn, request: Request) -> dict:
    await _project_access(request, pid, "editor")
    async with db_session() as s:
        proj = await _get_project_or_404(s, pid)
        q = select(TestCase).where(TestCase.project_id == pid, TestCase.enabled == True)
        if body.case_ids:
            q = select(TestCase).where(TestCase.project_id == pid, TestCase.id.in_(body.case_ids))
        cases = (await s.execute(q)).scalars().all()
        if body.tags:
            wanted = set(body.tags)
            cases = [c for c in cases if wanted & set(c.tags or [])]
        if not cases:
            raise HTTPException(400, "no matching test cases")
        # 2026-10-04 前置条件里的用例依赖：被引用的用例插到前面先跑（用户要求）。
        # 只解析本项目内的编号，依赖的依赖也会被收进来（见 engine 里的说明）。
        # 放在这里而不是 engine 层，是因为要按**用户选中的顺序**去扩展，
        # 而选中的顺序只有这一层知道（cases 的排列）。
        from app.engine import resolve_case_dependencies

        _ordered_ids = await resolve_case_dependencies(s, pid, [c.id for c in cases])
        if _ordered_ids and len(_ordered_ids) != len(cases):
            _by_id = {c.id: c for c in cases}
            _extra = [i for i in _ordered_ids if i not in _by_id]
            if _extra:
                _rows = (
                    (await s.execute(
                        select(TestCase).where(
                            TestCase.project_id == pid, TestCase.id.in_(_extra)
                        )
                    )).scalars().all()
                )
                for _c in _rows:
                    _by_id[_c.id] = _c
            cases = [_by_id[i] for i in _ordered_ids if i in _by_id]
        # concurrency: explicit request wins, else the project's setting, else the
        # server default (Settings.run_concurrency, was a hard-coded 2).
        # The `!= 2` sentinel means "the UI did not choose" — 2 is the schema default.
        # It is kept as-is so the frontend contract does not change; only the final
        # fallback moved from a literal to the setting.
        concurrency = (
            body.concurrency
            if body.concurrency != 2
            else ((proj.run_concurrency if proj else None) or get_settings().run_concurrency)
        )
        run = Run(
            project_id=pid,
            name=body.name,
            case_ids=[c.id for c in cases],
            concurrency=concurrency,
            # None（不传）保持 NULL = 沿用全局 case_retries；显式传 0 表示这一轮不重试。
            retries=body.retries,
            total_count=len(cases),
            environment_id=body.environment_id or await _default_env_id(s, pid),
        )
        s.add(run)
        await s.flush()
        run_id = run.id
        payload = _run(run)
    await _launch(run_id)
    return payload


@router.get("/projects/{pid}/runs")
async def list_runs(pid: int, request: Request) -> list[dict]:
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        rows = (
            (await s.execute(select(Run).where(Run.project_id == pid).order_by(Run.id.desc())))
            .scalars()
            .all()
        )
        return [_run(r) for r in rows]


@router.get("/runs/{rid}")
async def get_run(rid: int, request: Request) -> dict:
    async with db_session() as s:
        r = await s.get(Run, rid)
        if r is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, r.project_id, "viewer")
        return {
            **_run(r),
            "ran_by_label": await _user_label(s, r.ran_by_user_id),
            # 明细只在详情给：这是「停下来改文档」的入口，列表页不展开。
            "drift_signals": r.drift_signals,
        }


@router.get("/runs/{rid}/results")
async def get_results(rid: int, request: Request) -> list[dict]:
    async with db_session() as s:
        run = await s.get(Run, rid)
        if run is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, run.project_id, "viewer")
        rows = (
            (
                await s.execute(
                    select(RunResult).where(RunResult.run_id == rid).order_by(RunResult.id)
                )
            )
            .scalars()
            .all()
        )
        return [_result(x) for x in rows]


@router.patch("/results/{res_id}")
async def override_result(res_id: int, body: ResultPatch, request: Request) -> dict:
    """人工改判一条用例的结论（2026-10-06）。

    AI 判定会不准，测试工程师必须能自己拍板。改判直接写进 `status`（而不是另起一个
    "人工结论"字段），这样报表、KPI、筛选、重跑逻辑全都自动跟着走，不用每个查询
    都判断一遍"有没有被改过"。改判这件事本身另存 verdict_override* 四列 + 一个
    original_status（存 AI 原判，供撤销），用于审计和在界面上标出"这条是人改的"。

    body.clear=True 表示撤销改判，恢复 AI 的原始结论 —— 所以 status 允许为 null。
    """
    from datetime import UTC, datetime

    async with db_session() as s:
        row = await s.get(RunResult, res_id)
        if row is None:
            raise HTTPException(404, "result not found")
        run = await s.get(Run, row.run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, run.project_id, "editor")
        user = await auth.current_user(request)

        if body.clear:
            # 撤销改判：回到 AI 的原始结论。original_status 是首次改判时存下的，
            # 老数据（加列之前就改判过的）没有它 —— 那种情况清标记、status 不动，
            # 至少不会把结论改成一个编造的值。
            if row.original_status:
                row.status = row.original_status
            row.verdict_override = None
            row.override_reason = None
            row.override_by = None
            row.override_at = None
            row.original_status = None
            await s.commit()
            return _result(row)

        if body.status is None:
            raise HTTPException(400, "status is required (or pass clear=true)")

        # 首次改判时把 AI 的原判留一份，撤销时才有得还原。
        # 已经是人工结论的再改一次时**不覆盖** original_status ——
        # 否则连改两次就把第一次的 AI 原判冲掉了。
        if row.verdict_override is None:
            row.original_status = row.status
        row.status = body.status
        row.verdict_override = body.status
        row.override_reason = (body.reason or "").strip() or None
        row.override_by = (getattr(user, "email", None) if user else None) or "unknown"
        row.override_at = datetime.now(UTC).isoformat()
        await s.commit()
        return _result(row)


@router.post("/runs/{rid}/cancel")
async def cancel_run(rid: int, request: Request) -> dict:
    async with db_session() as s:
        r = await s.get(Run, rid)
        if r is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, r.project_id, "editor")
        if r.status in ("pending", "running"):
            r.status = "cancelled"
        return _run(r)


# strip any stack of re-run prefixes so names don't nest ("re-run of re-run of …")
_RERUN_PREFIX = re.compile(r"^(re-run of ((failures|errors) in )?)+")

# Which of the previous run's cases to take, keyed on their verdict. A case with no result
# row at all counts as an error — no verdict is an infra outcome, not a product one.
_RERUN_SETS = {
    "failing": lambda st: st != "passed",  # judge failures AND infra errors
    "error": lambda st: st == "error",  # infra errors + timeouts only
}
_RERUN_NAMES = {"failing": "failures", "error": "errors"}


@router.post("/runs/{rid}/rerun")
async def rerun(
    rid: int, request: Request, only: str | None = None, failed_only: bool = False
) -> dict:
    """Re-run an existing run's cases. `only` narrows the set:

    - unset: every case, exactly as before.
    - failing: everything that did not pass. After fixing something you want the verdict on
      what was broken, not another hour re-proving what passed (49 cases ≈ 2h, its failures
      ≈ 30min).
    - error: only the infra outcomes — timeouts, dead sessions, crashed browsers. A judge
      'failed' is a finding about the product and re-running it proves nothing; an 'error'
      means the tool never got a verdict, so it is the one worth another attempt.
    """
    if failed_only and only is None:  # accept the older SPA's parameter
        only = "failing"
    if only is not None and only not in _RERUN_SETS:
        raise HTTPException(400, f"unknown re-run set {only!r}")
    async with db_session() as s:
        old = await s.get(Run, rid)
        if old is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, old.project_id, "editor")
        case_ids = list(old.case_ids or [])
        base = _RERUN_PREFIX.sub("", old.name)
        name = f"re-run of {base}"
        if only is not None:
            rows = (
                (await s.execute(select(RunResult).where(RunResult.run_id == rid))).scalars().all()
            )
            verdict = {r.case_id: r.status for r in rows}
            keep = _RERUN_SETS[only]
            case_ids = [c for c in case_ids if keep(verdict.get(c, "error"))]
            if not case_ids:
                raise HTTPException(400, f"this run has no {only} cases to re-run")
            name = f"re-run of {_RERUN_NAMES[only]} in {base}"
        # the project's current 并发数 wins over whatever the original run was created
        # with — otherwise raising it in settings silently does nothing for re-runs, which
        # is exactly when you are trying to make a two-hour run shorter.
        project = await s.get(Project, old.project_id)
        new = Run(
            project_id=old.project_id,
            name=name,
            case_ids=case_ids,
            concurrency=(project.run_concurrency if project else None) or old.concurrency,
            total_count=len(case_ids),
            environment_id=old.environment_id or await _default_env_id(s, old.project_id),
        )
        s.add(new)
        await s.flush()
        new_id = new.id
        payload = _run(new)
    await _launch(new_id)
    return payload


@router.patch("/runs/{rid}")
async def update_run(rid: int, body: RunPatch, request: Request) -> dict:
    """Rename a run."""
    async with db_session() as s:
        r = await s.get(Run, rid)
        if r is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, r.project_id, "editor")
        r.name = body.name
        return _run(r)


@router.delete("/runs/{rid}")
async def delete_run(rid: int, request: Request) -> dict:
    """Delete a run and its results. A running run is cancelled first."""
    async with db_session() as s:
        r = await s.get(Run, rid)
        if r is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, r.project_id, "editor")
        await s.execute(delete(RunResult).where(RunResult.run_id == rid))
        await s.delete(r)
    return {"deleted": rid}


@router.get("/projects/{pid}/failure-digest")
async def failure_digest(
    pid: int,
    request: Request,
    limit: int = 200,
    backfill: bool = True,
) -> dict:
    """本项目**当前**所有未通过用例的失败清单（2026-10-06）。

    需求原文：「弄一个该项目里面所有未通过的用例整一个完整的错误清单，
    如果因为相同的原因，不同的测试用例的，可以合并，但要讲清楚，
    但是不能讲废话」。

    三条设计约束，都是为了满足"不讲废话"：

    1. **只取每条用例最近一次未通过的结果**。同一用例跑过 5 次取最新那次，
       否则清单里会重复列出同一条用例（实测 case 4 有4 次失败）。
    2. **合并靠确定性信号，不靠 LLM 语义聚类**。`app/failure_digest.py` 只在
       信号完全相同时合并；分不清的单列。见该模块 docstring 里
       「代理」一词被误聚类的实例。
    3. **LLM 只用于补历史 root_cause**（`backfill=True`）。实测 119 条历史失败
       的 root_cause 全为空（分类功能 10-04 才上线），不补的话119 条会聚成
       117 条、一条都合并不了。LLM 只出分类，不出分组。

    `backfill=false` 可跳过 LLM 调用（离线/无网关时用），此时历史数据会落在
    "原因待人工确认" 里，但清单照常返回。
    """
    from app.failure_backfill import backfill_root_causes
    from app.failure_digest import build_groups

    await _project_access(request, pid, "viewer")
    cap = max(1, min(int(limit), 2000))

    async with db_session() as s:
        # 每条用例最近一次未通过的结果。
        # 用"取 max(id) 的那一条"而不是"全部"，因为用户要的是"当前清单"，
        # 而历史失败里可能有一次是真缺陷、后来又通过了 —— 那不该继续占着清单。
        rows = (
            (
                await s.execute(
                    select(RunResult, TestCase)
                    .join(TestCase, TestCase.id == RunResult.case_id)
                    .where(
                        TestCase.project_id == pid,
                        RunResult.status.in_(("failed", "error")),
                    )
                    .order_by(RunResult.case_id.desc(), RunResult.id.desc())
                )
            ).all()
        )
        if not rows:
            return {
                "project_id": pid,
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "backfilled": 0,
                "group_count": 0,
                "case_count": 0,
                "groups": [],
                "note": "本项目当前没有未通过的用例。",
            }

        latest: dict[int, dict] = {}
        for res, case in rows:
            if res.case_id in latest:      # 已取到该用例更新的一次
                continue
            latest[res.case_id] = {
                "id": res.id,
                "case_id": res.case_id,
                "case_key": case.case_key,
                "name": case.name,
                "module": case.module,
                "root_cause": res.root_cause,
                "judge_reason": res.judge_reason,
                "error": res.error,
                "created_at": res.created_at.isoformat() if res.created_at else "",
                "run_id": res.run_id,
                "video_url": res.video_url,
            }
            if len(latest) >= cap:
                break

    data = list(latest.values())

    # 历史数据补分类：让"相同原因合并"真正可用。
    #
    # ★必须**回填写库**，不能每次现算（实测踩过）：
    # 现算意味着每次打开清单都要发 114 条分类请求，而免费额度网关
    # 实测第 3 批就开始 429 —— 清单直接打不开，而且把额度耗光会让正在跑的用例失败。
    # 现在改成：未分类的才分类，且结果写回 run_result.root_cause，
    # 于是第二次打开是 0 次 LLM 调用。429 也会被分类模块吞掉降级为unclear，
    # 不会让整个端点失败。
    backfilled = 0
    if backfill:
        need = [d for d in data if not (d.get("root_cause") or "").strip()]
        if need:
            mapped = await backfill_root_causes(need)
            if mapped:
                async with db_session() as s2:
                    for d in data:
                        new_cause = mapped.get(d["id"])
                        if not new_cause:
                            continue
                        res = await s2.get(RunResult, d["id"])
                        if res is not None and not (res.root_cause or "").strip():
                            res.root_cause = new_cause
                            backfilled += 1
                    await s2.commit()
                for d in data:
                    if d["id"] in mapped:
                        d["root_cause"] = mapped[d["id"]]
    data.sort(key=lambda d: d["id"], reverse=True)

    groups = build_groups(data)
    return {
        "project_id": pid,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "backfilled": backfilled,
        "uncategorized": sum(1 for d in data if not (d.get("root_cause") or "").strip()),
        "group_count": len(groups),
        "case_count": len(data),
        "groups": [g.to_dict() for g in groups],
    }


@router.get("/projects/{pid}/case-changes")
async def case_changes(pid: int, request: Request, limit: int = 100) -> list[dict]:
    """本项目用例的改动审计（2026-10-06，用户要求"通过后也需要修改清单"的配套）。

    清单本身是从run_result 实时算出来的派生视图 —— 用例一旦通过就自动消失，
    不需要人工维护。但"是谁在什么时候把哪条用例改成了什么"需要留痕：
    没有这条审计，助手自动改过用例之后就没人说得清改了什么。
    """
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(CaseChange)
                    .where(CaseChange.project_id == pid)
                    .order_by(CaseChange.id.desc())
                    .limit(max(1, min(int(limit), 500)))
                )
            ).scalars()
            .all()
        )
        return [_case_change(x) for x in rows]


@router.get("/runs/{rid}/export")
async def export_run(rid: int, request: Request) -> Response:
    """Download a run's per-case results as CSV (case metadata joined in)."""
    async with db_session() as s:
        run = await s.get(Run, rid)
        if run is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, run.project_id, "viewer")
        results = (
            (
                await s.execute(
                    select(RunResult).where(RunResult.run_id == rid).order_by(RunResult.id)
                )
            )
            .scalars()
            .all()
        )
        cases = (
            (await s.execute(select(TestCase).where(TestCase.id.in_([x.case_id for x in results]))))
            .scalars()
            .all()
        )
    by_case = {c.id: c for c in cases}
    # Cases the drift check named, as "pid:case_key" — the same qualified form it reports.
    #
    # ★ Per-row, not a run-level banner. A run-level conclusion does not survive CSV: the
    # file is filtered and sorted in a spreadsheet, and a banner in row0 is the row somebody
    # deletes. Putting the flag on the row makes it travel with the case, so the reader can
    # filter to exactly the cases whose *expectation itself* is in question — which is the
    # set that must not be filed as bugs.
    drift_cases: set[str] = set()
    for sig in run.drift_signals or ():
        drift_cases.update(sig.get("case_ids") or ())
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "case_key",
            "name",
            "module",
            "priority",
            "status",
            "attempts",
            "flaky",
            "latency_ms",
            "judge_reason",
            # 需规漂移（2026-10-07）。这一列为「是」的用例，其**预期本身**存疑 ——
            # 不是功能不符，是用例断言的行为与需规/其它用例矛盾。排它在前面的缺陷列
            # 之前是因为它一票否决：预期存疑时，后面三段叙述再完整也不能提单。
            "需规漂移存疑",
            # 失败归因（2026-10-07）。放在缺陷描述前面，因为这一列决定后面三列
            # 到底该不该被采纳 —— "流程未走完""页面未就绪"的用例即使写了三段
            # 叙述也不该提单，混在一起导出等于把假缺陷一起递上去了。
            "失败归因",
            "可上报缺陷",
            # The AI bug description for failed cases, split into the three sections the
            # tester actually pastes into a bug tracker. Empty for passed cases.
            "缺陷标题",
            "严重程度",
            "操作步骤",
            "实际结果",
            "预期结果",
        ]
    )
    for x in results:
        c = by_case.get(x.case_id)
        nar = x.failure_narrative or {}
        # Qualified to match what spec_drift reports. The first column stays bare for
        # readability, so the join cannot reuse it — and reusing a bare key would mark the
        # wrong project's rows whenever two systems share a case_key.
        qkey = (
            f"{run.project_id}:{c.case_key}"
            if c is not None and c.case_key
            else ""
        )
        writer.writerow(
            [
                (c.case_key if c else "") or f"#{x.case_id}",
                c.name if c else "",
                (c.module if c else "") or "",
                c.priority if c else "",
                x.status,
                x.attempts,
                "yes" if x.flaky else "",
                x.latency_ms,
                (x.judge_reason or x.error or "").replace("\n", " "),
                "是" if qkey in drift_cases else "",
                _verdict_info(x.attribution)[0],
                "是" if _verdict_info(x.attribution)[1] else "",
                nar.get("title", ""),
                nar.get("severity", ""),
                (nar.get("steps", "") or "").replace("\n", " "),
                (nar.get("actual", "") or "").replace("\n", " "),
                (nar.get("expected", "") or "").replace("\n", " "),
            ]
        )
    filename = f"run-{rid}-results.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs/{rid}/stream")
async def stream_run(rid: int, request: Request) -> EventSourceResponse:
    async with db_session() as s:
        r0 = await s.get(Run, rid)
        if r0 is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, r0.project_id, "viewer")

    async def gen() -> Any:
        while True:
            async with db_session() as s:
                r = await s.get(Run, rid)
            if r is None:
                yield {"event": "error", "data": "run not found"}
                return
            import json

            yield {"event": "progress", "data": json.dumps(_run(r))}
            if r.status in ("completed", "failed", "cancelled"):
                return
            await asyncio.sleep(1.0)

    return EventSourceResponse(gen())


# ---- issues (Kanban) ----
# ---- test suites ----
async def _user_label(s, uid: int | None) -> str | None:
    if uid is None:
        return None
    u = await s.get(User, uid)
    return (u.name or u.email) if u else None


async def _suite(s, x: TestSuite) -> dict:
    return {
        "id": x.id,
        "project_id": x.project_id,
        "name": x.name,
        "description": x.description,
        "selection_mode": x.selection_mode,
        "case_ids": x.case_ids or [],
        "tag_filter": x.tag_filter or [],
        "owner_user_id": x.owner_user_id,
        "owner_label": await _user_label(s, x.owner_user_id),
        "runner_user_id": x.runner_user_id,
        "runner_label": await _user_label(s, x.runner_user_id),
        "environment_id": x.environment_id,
        "cadence": x.cadence,
        "due_at": _iso(x.due_at),
        "last_run_id": x.last_run_id,
        "last_status": x.last_status,
        "last_run_at": _iso(x.last_run_at),
        "created_at": _iso(x.created_at),
    }


async def _resolve_suite_case_ids(s, x: TestSuite) -> list[int]:
    """Concrete, still-existing case ids for a suite (tag filter resolved live)."""
    if x.selection_mode == "tags" and x.tag_filter:
        wanted = set(x.tag_filter)
        rows = (
            (
                await s.execute(
                    select(TestCase).where(
                        TestCase.project_id == x.project_id, TestCase.enabled == True
                    )
                )
            )
            .scalars()
            .all()
        )
        return [c.id for c in rows if wanted & set(c.tags or [])]
    ids = x.case_ids or []
    if not ids:
        return []
    keep = set(
        (
            await s.execute(
                select(TestCase.id).where(TestCase.id.in_(ids), TestCase.project_id == x.project_id)
            )
        )
        .scalars()
        .all()
    )
    return [i for i in ids if i in keep]


@router.get("/projects/{pid}/suites")
async def list_suites(pid: int, request: Request) -> list[dict]:
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        rows = (
            (
                await s.execute(
                    select(TestSuite)
                    .where(TestSuite.project_id == pid)
                    .order_by(TestSuite.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return [await _suite(s, x) for x in rows]


@router.post("/projects/{pid}/suites")
async def create_suite(pid: int, body: SuiteIn, request: Request) -> dict:
    await _project_access(request, pid, "editor")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        user = await auth.current_user(request)
        x = TestSuite(
            project_id=pid,
            **body.model_dump(),
            due_at=_next_due(body.cadence),
            created_by=user.email if user else None,
        )
        s.add(x)
        await s.flush()
        if x.runner_user_id is not None:
            await notify.push(
                s,
                user_id=x.runner_user_id,
                type="assigned",
                title=f"你被指派为套件执行人:{x.name}",
                body="请按约定周期运行该测试套件并复核结果。",
                link=f"/projects/{pid}/suites",
                suite_id=x.id,
            )
        return await _suite(s, x)


@router.get("/suites/{sid}")
async def get_suite(sid: int, request: Request) -> dict:
    async with db_session() as s:
        x = await s.get(TestSuite, sid)
        if x is None:
            raise HTTPException(404, "suite not found")
        await _project_access(request, x.project_id, "viewer")
        return await _suite(s, x)


@router.put("/suites/{sid}")
async def update_suite(sid: int, body: SuitePatch, request: Request) -> dict:
    async with db_session() as s:
        x = await s.get(TestSuite, sid)
        if x is None:
            raise HTTPException(404, "suite not found")
        await _project_access(request, x.project_id, "editor")
        prev_runner = x.runner_user_id
        data = body.model_dump(exclude_none=True)
        for k, v in data.items():
            setattr(x, k, v)
        if "cadence" in data:  # recompute next due from the new cadence
            x.due_at = _next_due(x.cadence, x.last_run_at)
        await s.flush()
        if x.runner_user_id is not None and x.runner_user_id != prev_runner:
            await notify.push(
                s,
                user_id=x.runner_user_id,
                type="assigned",
                title=f"你被指派为套件执行人:{x.name}",
                body="请按约定周期运行该测试套件并复核结果。",
                link=f"/projects/{x.project_id}/suites",
                suite_id=x.id,
            )
        return await _suite(s, x)


@router.post("/suites/{sid}/assign")
async def assign_suite(sid: int, body: SuiteAssignIn, request: Request) -> dict:
    async with db_session() as s:
        x = await s.get(TestSuite, sid)
        if x is None:
            raise HTTPException(404, "suite not found")
        await _project_access(request, x.project_id, "editor")
        changed = x.runner_user_id != body.runner_user_id
        x.runner_user_id = body.runner_user_id
        await s.flush()
        if changed and x.runner_user_id is not None:
            await notify.push(
                s,
                user_id=x.runner_user_id,
                type="assigned",
                title=f"你被指派为套件执行人:{x.name}",
                body="请按约定周期运行该测试套件并复核结果。",
                link=f"/projects/{x.project_id}/suites",
                suite_id=x.id,
            )
        return await _suite(s, x)


@router.delete("/suites/{sid}")
async def delete_suite(sid: int, request: Request) -> dict:
    async with db_session() as s:
        x = await s.get(TestSuite, sid)
        if x is None:
            raise HTTPException(404, "suite not found")
        await _project_access(request, x.project_id, "editor")
        await s.delete(x)
        return {"deleted": sid}


@router.post("/suites/{sid}/run")
async def run_suite_now(sid: int, request: Request) -> dict:
    """Any project member may run a suite; the Run records who actually ran it.

    建 run 的部分与定时自动执行共用 `suite_schedule.create_run_for_suite` —— 两条路径
    必须选出同一批用例、同一个环境，否则"手动跑和定时跑结果不一样"会变成很难查的问题。
    """
    from app.suite_schedule import create_run_for_suite

    async with db_session() as s:
        x = await s.get(TestSuite, sid)
        if x is None:
            raise HTTPException(404, "suite not found")
        await _project_access(request, x.project_id, "viewer")
        user = await auth.current_user(request)
        run = await create_run_for_suite(
            s,
            x,
            trigger="suite",
            ran_by_user_id=user.id if user else None,
            created_by=user.email if user else None,
        )
        if run is None:
            raise HTTPException(400, "suite has no runnable cases")
        run_id = run.id
        payload = _run(run)
    await _launch(run_id)
    return payload


# ---- notifications (per current user) ----


@router.get("/notifications")
async def list_notifications(request: Request) -> list[dict]:
    user = await auth.current_user(request)
    if user is None:
        return []
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(Notification)
                    .where(Notification.user_id == user.id)
                    .order_by(Notification.read_at.isnot(None), Notification.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        return [_notif(n) for n in rows]


@router.get("/notifications/unread-count")
async def unread_count(request: Request) -> dict:
    user = await auth.current_user(request)
    if user is None:
        return {"count": 0}
    async with db_session() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == user.id, Notification.read_at.is_(None))
            )
        ).scalar_one()
        return {"count": n}


@router.post("/notifications/{nid}/read")
async def read_notification(nid: int, request: Request) -> dict:
    user = await auth.current_user(request)
    async with db_session() as s:
        n = await s.get(Notification, nid)
        if n is None or (user is not None and n.user_id != user.id):
            raise HTTPException(404, "notification not found")
        if n.read_at is None:
            n.read_at = datetime.now(UTC)
        return {"ok": True}


@router.post("/notifications/read-all")
async def read_all_notifications(request: Request) -> dict:
    user = await auth.current_user(request)
    if user is None:
        return {"ok": True}
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(Notification).where(
                        Notification.user_id == user.id, Notification.read_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        for n in rows:
            n.read_at = now
        return {"ok": True, "marked": len(rows)}






@router.get("/projects/{pid}/issues")
async def list_issues(pid: int, request: Request, status: str | None = None) -> list[dict]:
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        q = select(Issue).where(Issue.project_id == pid).order_by(Issue.id.desc())
        if status:
            q = q.where(Issue.status == status)
        return [_issue(i) for i in (await s.execute(q)).scalars().all()]


@router.post("/projects/{pid}/issues")
async def create_issue(pid: int, body: IssueIn, request: Request) -> dict:
    await _project_access(request, pid, "editor")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        i = Issue(project_id=pid, **body.model_dump())
        s.add(i)
        await s.flush()
        if i.assignee_user_id is not None:
            await notify.push(
                s,
                user_id=i.assignee_user_id,
                type="issue_assigned",
                title=f"你被指派了问题:{i.title}",
                body=f"严重度 {i.severity}。",
                link=f"/projects/{pid}/issues",
                issue_id=i.id,
            )
        _enqueue_push(i.id)  # auto-push every new issue (no-ops if project has no GitLab config)
        issue_id = i.id
        payload = _issue(i)
    await feishu_notify.mirror_issue(issue_id)  # mirror to the project's Bitable (best-effort)
    return payload


@router.get("/issues/{iid}")
async def get_issue(iid: int, request: Request) -> dict:
    async with db_session() as s:
        i = await s.get(Issue, iid)
        if i is None:
            raise HTTPException(404, "issue not found")
        await _project_access(request, i.project_id, "viewer")
        comments = (
            (
                await s.execute(
                    select(IssueComment)
                    .where(IssueComment.issue_id == iid)
                    .order_by(IssueComment.id)
                )
            )
            .scalars()
            .all()
        )
        return {**_issue(i), "comments": [_comment(c) for c in comments]}


@router.put("/issues/{iid}")
async def update_issue(iid: int, body: IssuePatch, request: Request) -> dict:
    async with db_session() as s:
        i = await s.get(Issue, iid)
        if i is None:
            raise HTTPException(404, "issue not found")
        await _project_access(request, i.project_id, "editor")
        prev_assignee = i.assignee_user_id
        prev_status = i.status
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(i, k, v)
        await s.flush()
        status_changed = i.status != prev_status
        if i.assignee_user_id is not None and i.assignee_user_id != prev_assignee:
            await notify.push(
                s,
                user_id=i.assignee_user_id,
                type="issue_assigned",
                title=f"你被指派了问题:{i.title}",
                body=f"严重度 {i.severity},当前状态 {i.status}。",
                link=f"/projects/{i.project_id}/issues",
                issue_id=i.id,
            )
        _enqueue_push(i.id)  # mirror status/field edits up to GitLab
        issue_id = i.id
        payload = _issue(i)
    if status_changed:
        await feishu_notify.push_issue_update(issue_id)  # best-effort push to bound chat
        await feishu_notify.sync_issue_to_bitable(issue_id)  # write status back to Bitable
    return payload


@router.delete("/issues/{iid}")
async def delete_issue(iid: int, request: Request) -> dict:
    async with db_session() as s:
        i = await s.get(Issue, iid)
        if i is None:
            raise HTTPException(404, "issue not found")
        await _project_access(request, i.project_id, "editor")
        await s.delete(i)
        return {"deleted": iid}


@router.post("/issues/{iid}/comments")
async def add_comment(iid: int, body: IssueCommentIn, request: Request) -> dict:
    async with db_session() as s:
        i = await s.get(Issue, iid)
        if i is None:
            raise HTTPException(404, "issue not found")
        await _project_access(request, i.project_id, "editor")
        c = IssueComment(issue_id=iid, body=body.body, author=body.author)
        s.add(c)
        await s.flush()
        _enqueue_push(iid)  # push mirrors unsynced comments up as GitLab notes
        return _comment(c)


# ---- feishu feedback bot ----


@public.post("/feishu/events")
async def feishu_events(request: Request) -> dict:
    """Feishu event subscription callback (Feishu posts here; no user auth).

    Handles the url_verification handshake and dispatches message events to a
    background worker, returning 200 immediately so Feishu doesn't retry (3s).
    """
    _require_feature(get_settings().enable_feishu, "Feishu")
    body = await request.json()
    if "encrypt" in body:
        # Leave "Encrypt Key" blank in the Feishu console — decryption unsupported.
        raise HTTPException(400, "encrypted events not supported; clear the Encrypt Key")
    async with db_session() as s:
        token = (await feishu.resolve_config(s))["verification_token"]
    if body.get("type") == "url_verification":
        if token and body.get("token") != token:
            raise HTTPException(403, "bad verification token")
        return {"challenge": body.get("challenge")}
    header = body.get("header") or {}
    if token and header.get("token") != token:
        raise HTTPException(403, "bad verification token")
    if header.get("event_type") == "im.message.receive_v1":
        feishu.schedule(body)
    return {"code": 0}


@router.get("/projects/{pid}/feedback")
async def list_project_feedback(
    pid: int,
    request: Request,
    status: str | None = None,
    category: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Feishu-collected feedback for one project, newest first."""
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        q = (
            select(FeedbackItem)
            .where(FeedbackItem.project_id == pid)
            .order_by(FeedbackItem.id.desc())
        )
        if status:
            q = q.where(FeedbackItem.status == status)
        if category:
            q = q.where(FeedbackItem.category == category)
        q = q.limit(min(max(limit, 1), 200))
        return [_feedback(f) for f in (await s.execute(q)).scalars().all()]


@router.post("/feedback/{fid}/promote")
async def promote_feedback(fid: int, request: Request) -> dict:
    """Convert a feedback item into an Issue on its project's board (idempotent)."""
    async with db_session() as s:
        f = await s.get(FeedbackItem, fid)
        if f is None:
            raise HTTPException(404, "feedback not found")
        if f.project_id is None:
            raise HTTPException(400, "feedback has no project — bind the chat first")
        await _project_access(request, f.project_id, "editor")
        if f.issue_id is not None:
            existing = await s.get(Issue, f.issue_id)
            if existing is not None:
                return _issue(existing)
        i = Issue(
            project_id=f.project_id,
            title=f.title or f.content[:60],
            description=f.content,
            severity=f.severity,
            labels=["feishu"],
            created_by=f.sender_id,
        )
        s.add(i)
        await s.flush()
        f.issue_id = i.id
        issue_id = i.id
        payload = _issue(i)
    _enqueue_push(issue_id)
    await feishu_notify.mirror_issue(issue_id)
    return payload


# ---- artifacts ----
@router.get("/results/{rid}/video")
async def result_video(rid: int, request: Request) -> RedirectResponse:
    return await _redirect_artifact(rid, "video_url", request)


@router.get("/results/{rid}/trace")
async def result_trace(rid: int, request: Request) -> RedirectResponse:
    return await _redirect_artifact(rid, "trace_url", request)


async def _redirect_artifact(rid: int, field: str, request: Request) -> RedirectResponse:
    async with db_session() as s:
        x = await s.get(RunResult, rid)
        if x is None:
            raise HTTPException(404, "result not found")
        run = await s.get(Run, x.run_id)
        await _project_access(request, run.project_id, "viewer")
        url = getattr(x, field)
        if not url:
            raise HTTPException(404, f"no {field}")
        return RedirectResponse(url)
