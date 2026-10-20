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

from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sse_starlette.sse import EventSourceResponse

from app import auth, docparse, excel, feishu, feishu_notify, key_store, login_state, mailer, notify
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
    ProjectGroup,
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
    LlmTestIn,
    LoginIn,
    GroupIn,
    GroupPatch,
    MemberIn,
    MemberPatch,
    ProjectIn,
    ProjectPatch,
    ResetIn,
    RolesIn,
    RunIn,
    ResultPatch,
    RunPatch,
    SecretKeyIn,
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


# ---- credential key recovery ----

# The recovery page needs the state even when credentials are unusable, so these routes
# deliberately do not call crypto/secret_configured(). The router already enforces login;
# export/import additionally require an admin. The key itself is returned only by an
# explicit POST and is never cached, logged, or persisted anywhere but the key file.


@router.get("/secret-key/status")
async def secret_key_status(request: Request) -> dict:
    status = key_store.get_status().as_dict()
    user = await auth.current_user(request)
    is_admin = not get_settings().auth_enabled or bool(user and user.is_admin)
    if not is_admin:
        status.pop("key_file", None)
    status["recovery_mode"] = not status["can_use_credentials"]
    return status


@router.post("/secret-key/export")
async def export_secret_key(request: Request, response: Response) -> dict:
    """One-time display of the current key. POST keeps it out of URL history/caches."""
    await auth.require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        key = key_store.export_key()
    except key_store.SecretKeyMissing as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"key": key, "state": key_store.get_status().state.value}


@router.post("/secret-key/import")
async def import_secret_key(body: SecretKeyIn, request: Request) -> dict:
    """Validate a backup key against stored ciphertext, then persist it atomically."""
    await auth.require_admin(request)
    async with db_session() as session:
        status = await key_store.import_key(body.key, session)
        if status.can_use_credentials:
            # The key works again — migrate any legacy plaintext login states now
            # instead of waiting for the next restart.
            await login_state.backfill(session)
    if not status.can_use_credentials:
        raise HTTPException(
            400,
            detail={
                "message": status.reason or "导入失败：密钥不可用",
                "status": status.as_dict(),
            },
        )
    return {
        **status.as_dict(),
        "recovery_mode": False,
        # Credentials are usable immediately (cache cleared by import_key), but the
        # Feishu worker / suite scheduler were not started in recovery mode.
        "restart_required": True,
    }


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
    from app import llm as llm_mod
    from app.llm import roles_for_ui
    from app.settings_store import GITLAB_TOKEN_KEY, has_setting

    async with db_session() as s:
        token_set = await has_setting(s, GITLAB_TOKEN_KEY)
        feishu_cfg = await feishu.resolve_config(s)
    # ★ 2026-10-11 修正：这里以前读 `llm_config()`（env + 旧单值字段），而配了池之后
    # 真正在跑的是池的第 1 条 —— 于是「正在使用」面板会显示一个**没在用的模型名**。
    # 现在固定读真实生效的那一套，另外把来源（pool / env）也告诉界面，
    # 让"你正在编辑的那一层是不是生效的那一层"能当场说清。
    llm_status = _llm_status(await llm_mod.active_profile())
    llm_status["pool_source"] = await llm_mod.pool_origin()
    llm_status["pool_count"] = len(await llm_mod.load_pool())
    # 按用途分模型：每个用途的"配了什么 + 实际生效的模型/网关"。
    # 一起回给界面，省得前端自己推算继承关系（推错就会显示一个不存在的模型名）。
    llm_status["roles"] = await roles_for_ui()
    # 两级判定的当前开关（存在 AppSetting，可能覆盖 env 默认）
    llm_status["judge_two_tier"] = await llm_mod.judge_two_tier_enabled()
    # 平台级「高端模型」：开没开、管哪几个环节、能不能真用起来。
    # 一起回出来，界面才能说清"我开的高端模型到底管到了哪几个用途"。
    llm_status["premium"] = await llm_mod.premium_for_ui()
    return {
        "llm": llm_status,
        "gitlab_token_set": token_set or bool(get_settings().gitlab_token),
        "feishu": _feishu_status(feishu_cfg),
    }


async def _merge_into_pool_head(
    sess,
    *,
    base_url: str | None,
    model: str | None,
    agent_model: str | None,
    provider: str | None,
    api_key: str | None,
) -> None:
    """把「默认模型」的单值改动合并进**池的第 1 条**（池空则新建一条）。

    为什么需要它：这几个字段以前是独立的 AppSetting（`llm_model` / `llm_base_url` …），
    而 `load_pool()` 在池非空时会整体取代它们 —— 于是"改了模型却没生效"是必然的，
    而且不报错、界面上还显示得理直气壮。

    合并之后，"改默认模型"这个意图只落在唯一那个真相来源（池第 1 条）上。
    池里其余条目原样保留、顺序不变 —— 改默认模型不该动到 429 兜底的那几套。

    拼不出一条完整条目时（缺 base_url / model / api_key）**什么都不写**：
    半截的池条目会被 load_pool 忽略、然后回落 env，那正好又变回"保存了没生效"。
    """
    from app import llm as llm_mod
    from app.settings_store import set_setting
    import json
    import logging

    profiles, origin = await llm_mod._load_pool_raw()
    head = profiles[0] if profiles else None

    def _pick(new: str | None, old: str) -> str:
        """None = 不动；"" = 显式清空（但对 base_url/model 清空等于不可用，由下面兜住）。"""
        return old if new is None else new.strip()

    entry = {
        # 来源是 env 时不把 "env" 当名字存进池 —— 那会让人以为池里有个叫 env 的条目。
        "name": (head.name if head and origin == "pool" else "default"),
        "base_url": _pick(base_url, head.base_url if head else ""),
        # 钥匙是只写的：留空 = 保留已存的那把（界面只显示掩码，不可能让人重输）
        "api_key": (api_key or "").strip() or (head.api_key if head else ""),
        "model": _pick(model, head.model if head else ""),
        "agent_model": _pick(agent_model, head.agent_model if head else ""),
        "provider": _pick(provider, head.provider if head else "auto") or "auto",
    }
    if not entry["base_url"] or not entry["model"] or not entry["api_key"]:
        logging.getLogger("potato-test.api").warning(
            "llm: 默认模型改动未落池（缺 base_url/model/api_key）—— 拒绝写入半截配置"
        )
        return

    rest = (
        [
            {
                "name": p.name,
                "base_url": p.base_url,
                "api_key": p.api_key,
                "model": p.model,
                "agent_model": p.agent_model,
                "provider": p.provider,
            }
            for p in profiles[1:]
        ]
        if origin == "pool"
        else []
    )
    await set_setting(
        sess,
        llm_mod.LLM_POOL_KEY,
        json.dumps([entry, *rest], ensure_ascii=False),
        secret=True,
    )


@router.put("/admin/settings/llm", dependencies=[Depends(auth.require_admin)])
async def set_admin_llm(body: LlmSettingsIn) -> dict:
    """Set the platform's underlying model at runtime. Non-secret fields: None keeps,
    "" clears the override (falls back to env). The API key is write-only.

    ★ 2026-10-11 语义修正：base_url / model / agent_model / provider / api_key 这几个
    单值字段**不再单独落库**，而是合并进池的**第 1 条**（池空则新建一条）。

    为什么必须这么改：那五个字段存在 AppSetting `llm_*` 里，而 `load_pool()` 在池非空时
    会**整体取代**它们 —— 也就是说配了池之后，"保存基层模型"这个动作对运行零影响，
    界面上却照旧显示"正在使用"。用户改完模型发现没变，就是这个原因。
    现在改成"任何指定默认模型的请求都落到池的第 1 条"，只有一个真相来源：
    你设了什么，跑的就是什么。
    """
    from app import llm as llm_mod
    from app.settings_store import set_setting
    import json

    async with db_session() as s:
        if body.pool is not None:
            # Persist the ordered pool as one encrypted blob (api_keys inside are secret).
            # api_key blank/None means "inherit the key at this same pool position in
            # the CURRENT pool" (the UI only shows masked keys, so editing a set must
            # not force the user to retype it). A brand-new position with no key yet
            # cannot be resolved — that entry is dropped as unusable (safe default).
            old_pool = await llm_mod.load_pool()  # resolves the existing encrypted keys
            old_keys = [p.api_key for p in old_pool]
            old_providers = [p.provider for p in old_pool]
            resolved: list[dict] = []
            for i, p in enumerate(body.pool):
                if not p.base_url or not p.model:
                    continue
                key = (p.api_key or "").strip()
                if not key and i < len(old_keys):
                    key = old_keys[i]  # inherit from same position
                if not key:
                    continue  # new position with no key → cannot build a usable client
                # provider 同理：留空就继承同位置的旧值（老 UI 没有这个字段，不能因此把
                # 一个显式配好的 anthropic 条目悄悄改回 auto）
                provider = (p.provider or "").strip()
                if not provider and i < len(old_providers):
                    provider = old_providers[i]
                resolved.append(
                    {
                        "name": (p.name or f"llm{i + 1}").strip(),
                        "base_url": p.base_url.strip(),
                        "api_key": key,
                        "model": p.model.strip(),
                        "agent_model": (p.agent_model or "").strip(),
                        "provider": provider or "auto",
                    }
                )
            if resolved:
                await set_setting(
                    s, llm_mod.LLM_POOL_KEY,
                    json.dumps(resolved, ensure_ascii=False), secret=True,
                )
            else:
                await set_setting(s, llm_mod.LLM_POOL_KEY, "", secret=False)
        elif any(
            v is not None
            for v in (body.base_url, body.model, body.agent_model, body.provider)
        ) or body.api_key:
            # 单值字段 → 合并进池第 1 条（见上面的长注释）。
            await _merge_into_pool_head(
                s,
                base_url=body.base_url,
                model=body.model,
                agent_model=body.agent_model,
                provider=body.provider,
                api_key=body.api_key,
            )
        if body.judge_two_tier is not None:
            # 两级判定开关：存 "1" / ""（空 = 关，回落到 env 默认）
            await set_setting(
                s, llm_mod.LLM_JUDGE_TWO_TIER_KEY,
                "1" if body.judge_two_tier else "", secret=False,
            )
        if body.roles is not None:
            # 按用途分模型：整体替换（None 才表示"不动"）。未知用途直接丢弃 ——
            # 一个拼错的用途名如果被存下来，界面会显示它"已配置"而实际从不生效。
            old_roles = await llm_mod.load_roles()  # 已存的钥匙（加密），用于"留空=保留"
            kept: dict[str, dict] = {}
            for role, r in body.roles.items():
                if role not in llm_mod.ROLES:
                    continue
                prev = old_roles.get(role)
                item = {
                    "model": (r.model or "").strip(),
                    "base_url": (r.base_url or "").strip(),
                    "api_key": (r.api_key or "").strip() or (prev.api_key if prev else ""),
                    "provider": (r.provider or "").strip(),
                }
                # 四项全空 = 这一行就是"跟随默认"，不存即可（省得把空壳写进库）。
                if not (item["model"] or item["base_url"] or item["api_key"] or item["provider"]):
                    continue
                kept[role] = item
            await set_setting(
                s, llm_mod.LLM_ROLES_KEY,
                json.dumps(kept, ensure_ascii=False) if kept else "",
                secret=True,
            )
        if body.premium is not None:
            # 平台级「高端模型」：整体替换（None 才表示"不动"）。
            #
            # 2026-10-12 起：高端模型只存「从池里选哪套」(pool_name) + 用途名单，
            # 不再自己存连接信息 —— 钥匙只存在于模型池里，premium 不重复存。
            # 用途名按 PREMIUM_ROLES 白名单过滤：一个拼错的用途如果被存下来，
            # 界面会显示它"已生效"而实际从不生效 —— 正是这一版要消灭的那类错觉。
            roles_in = {r for r in (body.premium.roles or []) if r in llm_mod.PREMIUM_ROLES}
            item = {
                "enabled": bool(body.premium.enabled),
                "pool_name": (body.premium.pool_name or "").strip(),
                # 按固定顺序归一：界面勾选顺序不该泄漏进存储内容。
                "roles": [r for r in llm_mod.PREMIUM_ROLES if r in roles_in],
            }
            empty = not (item["enabled"] or item["pool_name"] or item["roles"])
            await set_setting(
                s, llm_mod.LLM_PREMIUM_KEY,
                "" if empty else json.dumps(item, ensure_ascii=False),
                secret=False,
            )
    llm_mod.invalidate_llm_cache()
    # 回真实生效的那一套（池的第 1 条 + 用途覆盖），不是旧单值字段 ——
    # 界面拿它刷新"正在使用"，两个口径必须一致。
    return _llm_status(await llm_mod.active_profile())


@router.get("/admin/settings/llm/pool", dependencies=[Depends(auth.require_admin)])
async def get_admin_llm_pool() -> dict:
    """The ordered LLM pool, WITHOUT secrets. api_key is shown masked so the UI can
    display how many sets are configured and their base_url/model, not the keys.

    ★ 池为空时这里回**一条从 env 读出来的条目**（`source="env"`），而不是空列表。
    为什么：空列表会让界面显示"没有配置"，而实际上平台正在用 .env 里那一套跑。
    用户看到的"什么都没有"和"有一套在跑"是最容易把人带偏的那种不一致。
    """
    from app import llm as llm_mod

    profiles, origin = await llm_mod._load_pool_raw()
    masked = [
        {
            "name": p.name,
            "base_url": p.base_url,
            "model": p.model,
            "agent_model": p.agent_model,
            "provider": p.provider,
            "api_key_set": bool(p.api_key),
        }
        for p in profiles
    ]
    return {
        "pool": masked,
        "count": len(masked),
        # "pool" = 来自模型池；"env" = 池为空，回落 .env（这一条只是回显，保存后才会成为池）
        "source": origin,
        "seeded": origin == "env",
    }


@router.get("/admin/settings/llm/token-share", dependencies=[Depends(auth.require_admin)])
async def get_admin_llm_token_share() -> dict:
    """各角色 token 占比：优先用真实实测（执行流里按角色拆分的 by_role 账本聚合），
    没有实测数据的角色回退到结构估算（界面会标 ≈）。

    ★ 为什么这样：历史运行的 usage 原本只有每条用例的「总量」，2026-10-12 才加了
    by_role 逐角色拆分。所以老运行没有 by_role、新运行才有 —— 回退估算保证界面
    永远有数字，且绝不把估算当真实显示（has_data=false 时前端只显示估算值）。
    """
    from app import llm as llm_mod
    from sqlalchemy import select

    async with db_session() as s:
        rows = (await s.execute(select(RunResult.timing))).scalars().all()
    # 跨所有运行聚合 per-role 的 total token。
    agg: dict[str, int] = {}
    runs = 0
    for t in rows:
        if not isinstance(t, dict):
            continue
        by_role = (t.get("usage") or {}).get("by_role") or {}
        if not isinstance(by_role, dict) or not by_role:
            continue
        runs += 1
        for role, u in by_role.items():
            if not isinstance(u, dict):
                continue
            agg[role] = agg.get(role, 0) + int(u.get("total") or 0)
    total = sum(agg.values())
    measured = {r: round(v / total * 100, 1) for r, v in agg.items()} if total else {}
    return {
        "measured": measured,
        "estimate": dict(llm_mod.ROLE_TOKEN_SHARE_ESTIMATE),
        "has_data": runs > 0,
        "runs": runs,
        "total_tokens": total,
    }


# 连通性检测结果短缓存（2026-10-12）：按钮语义仍是"实测"，但连点 / 切页面再点在
# TTL 内直接复用上次结果，避免对每一套模型重复发起真实调用（每套含能力探测 + ping
# 多次 API 请求）。只有"配置没变"（签名相同）才复用；改模型 / 网关 / 钥匙立刻失效。
_LLM_TEST_CACHE: dict = {"sig": None, "ts": 0.0, "report": None}
_LLM_TEST_TTL_S = 60.0


@router.post("/admin/settings/llm/test", dependencies=[Depends(auth.require_admin)])
async def test_admin_llm(body: LlmTestIn | None = None) -> dict:
    """连通性检测：测「平台可能调用的每一套模型」，而不只是 agent 在跑的那一套。

    为什么测全部（2026-10-12）：配了模型池之后，真正在跑的不止池第 1 条——
    高端模型可能指向池里另一套、按用途覆盖也可能指向池外的模型。只测 agent 那一套，
    "测通了"只代表其中一套通，其余几套配错（错 key / 错模型名 / 错网关）时界面会
    让人误以为全都对。所以一次性把每一套都探一遍，界面逐条告诉用户哪套通、哪套挂。

    探测并发进行（asyncio.gather）：一套超时（25s）不会拖住其它套，整体等待约等于
    最慢那套而不是累加。每套都带 force=True 绕过能力缓存。

    短 TTL 缓存：连点 / 切页面再点在 60s 内且配置没变时直接复用上次结果，省掉对每一套
    的重复真实调用；过了 TTL 或改了模型 / 网关 / 钥匙会重新实测。想强制重测就等 60s。

    可按名字精测（body.names）：设置页「每行一个探测按钮」只测指定那一套；不传 / 空 = 全部。
    """
    import asyncio
    import time

    from app import llm as llm_mod
    from app.llm import LLMProfile

    # 1) 模型池里的每一套 —— 这是高端模型 / 429 兜底真正会用的集合。
    profiles = list(await llm_mod.load_pool())

    # 2) 把「按用途覆盖」里**不在池里**的模型也补进来：它们也是平台会真实调用的模型，
    #    但界面已不再提供编辑入口，漏测它们就等于留了一处"静默可能配错"的点。
    roles = await llm_mod.load_roles()
    seen = {(p.base_url, p.model, p.api_key) for p in profiles}
    for role, ov in roles.items():
        if ov is None or ov.empty:
            continue
        bu = (ov.base_url or (profiles[0].base_url if profiles else "")).strip()
        mdl = (ov.model or "").strip()
        key = (ov.api_key or "").strip()
        if not mdl:
            continue
        k = (bu, mdl, key)
        if k in seen:
            continue
        seen.add(k)
        profiles.append(
            LLMProfile(
                name=f"{role}:override",
                base_url=bu,
                api_key=key,
                model=mdl,
                provider=(ov.provider or "auto").strip() or "auto",
            )
        )

    # 3) 可选：只测指定名字（设置页「每行一个探测按钮」精测单套）。没传 / 空 = 全部；
    #    传了就把 profiles 收敛到这些名字，未找到的如实回一条失败，不静默跳过。
    want = [n.strip() for n in ((body.names if body else None) or []) if n and n.strip()]
    not_found: list[dict] = []
    if want:
        have = {p.name for p in profiles}
        wset = set(want)
        profiles = [p for p in profiles if p.name in wset]
        not_found = [
            {"name": n, "ok": False, "error": "No saved model with this name — save it first."}
            for n in want
            if n not in have
        ]

    # 配置签名：把"要测哪些名字"也并进去 —— 精测单套与测全部互不串味；其余字段任一
    # 变化都会换签名 ⇒ 缓存自动失效。
    sig = (tuple(want),) + tuple(
        (p.name, p.base_url, p.model, p.agent_model, p.provider, bool(p.api_key))
        for p in profiles
    )
    cached = _LLM_TEST_CACHE
    if (
        cached["report"] is not None
        and cached["sig"] == sig
        and time.monotonic() - cached["ts"] < _LLM_TEST_TTL_S
    ):
        return cached["report"]

    async def _one(p: LLMProfile) -> dict:
        model = (p.agent_model or p.model or "").strip()
        if not p.base_url:
            return {"name": p.name, "model": model, "ok": False,
                    "error": "Base URL is empty — nothing to call."}
        if not model:
            return {"name": p.name, "model": model, "ok": False,
                    "error": "No model name set — fill in Model first."}
        try:
            # 协议与能力一起报出来：换厂商之后"能不能跑"就看这几个布尔值。
            caps = await llm_mod.capabilities_for(p, force=True)
            res = await llm_mod.chat(p, user="ping", max_tokens=16, timeout=25.0)
            return {
                "name": p.name,
                "model": model,
                "ok": True,
                "reply_model": getattr(res.raw, "model", model),
                "api_key_set": bool(p.api_key),
                "base_url": p.base_url,
                "protocol": caps.protocol,
                "json_object": caps.json_object,
                "json_schema": caps.json_schema,
                "tools": caps.tools,
                "vision": caps.vision,
                "probed": caps.probed,
            }
        except Exception as exc:  # noqa: BLE001 — surfaced verbatim to the operator
            return {"name": p.name, "model": model, "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}

    probed = await asyncio.gather(*[_one(p) for p in profiles])
    results = list(probed) + not_found
    all_ok = all(r["ok"] for r in results)
    report = {"ok": all_ok, "all_ok": all_ok, "results": results}
    # 落缓存：ts 用探测完成的时刻，让"60s 新鲜期"从结果就绪起算。
    cached["sig"] = sig
    cached["ts"] = time.monotonic()
    cached["report"] = report
    return report


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
async def _project_with_counts(s, p) -> dict:
    """`_project(p)` + 列表页需要的两个派生字段（用例数 / 最近一次运行）。

    2026-10-09 抽出来：原先这段只长在 `list_projects` 里，于是新加的
    `GET /projects/{pid}` 如果直接用 `_project()`，同一个项目在"列表里"和
    "单取时"会返回**不同形状** —— 前端只好写两套解析。派生字段只算一次、两处共用。

    `has_login_state` 的补算（storage_state 凭据也算登录态）保持原样。
    """
    d = _project(p)
    # 所属「大系统」（2026-10-13）：列表/单取都要带，前端据此分组。
    d["group_id"] = p.group_id
    if not d["has_login_state"]:
        d["has_login_state"] = bool(
            (
                await s.execute(
                    select(func.count())
                    .select_from(Credential)
                    .where(
                        Credential.project_id == p.id,
                        Credential.is_active == True,  # noqa: E712
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
    return d


@router.get("/projects/{pid}")
async def get_project(pid: int, request: Request) -> dict:
    """单个项目的详情（2026-10-09 新增）。

    为什么加：此前只有 `GET /projects`（整个列表），前端想刷新"当前这一个项目"
    只能把列表拉下来再自己筛，或者干脆不发请求。于是 `/api/projects/{pid}` 这个
    看起来理所当然的路径**没有路由**，未匹配的 GET 会被 SPA 兜底接走并回 HTML ——
    调用方拿到 200 + `<html>`，然后在 JSON 解析处报一个与原因无关的错。
    （兜底那条已在 app/main.py 里对 /api 前缀回 404，但"端点不存在"本身也该补上。）

    返回形状与列表**逐字段一致**（同一个 `_project_with_counts`），
    权限口径也一致：viewer 即可读，非成员在非共享工作区下 404。
    """
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        p = await _get_project_or_404(s, pid)
        return await _project_with_counts(s, p)


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
        # 派生字段的算法与 GET /projects/{pid} 共用同一个函数，两个端点不会漂。
        return [await _project_with_counts(s, p) for p in rows]


@router.post("/projects")
async def create_project(body: ProjectIn, request: Request) -> dict:
    if body.login_state and not secret_configured():
        raise HTTPException(
            400,
            "凭据加密密钥不可用，无法保存登录态；请先在恢复页修复密钥。",
        )
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
            # 名字必须 strip：`" 训练资源 "` 和 `"训练资源"` 在列表里长得一模一样，
            # 用户会以为是自己看错了，然后对着两条"同一个"项目反复改。同项目的
            # set_roles 早就这么做了（第 949 行），项目名以前漏了。
            name=body.name.strip(),
            base_url=body.base_url,
            group_id=body.group_id,
            case_timeout_s=_s.case_timeout_s,
            case_max_steps=_s.case_max_steps,
            run_concurrency=_s.run_concurrency,
        )
        s.add(p)
        await s.flush()
        # 登录态只落加密凭据，绝不写 Project.login_state 明文列。
        # 旧列保留一个版本只为回滚（见 app/login_state.py）。
        if body.login_state:
            await login_state.store(s, p.id, body.login_state)
        # the creator owns the project (so it's visible + manageable under RBAC)
        user = await auth.current_user(request)
        if user is not None:
            s.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
        await s.flush()
        return await _project_with_counts(s, p)


@router.put("/projects/{pid}")
async def update_project(pid: int, body: ProjectPatch, request: Request) -> dict:
    await _project_access(request, pid, "owner")
    async with db_session() as s:
        p = await _get_project_or_404(s, pid)
        data = body.model_dump(exclude_none=True)
        # 同 create_project：改名也要 strip，否则"改完看不出区别"。
        if "name" in data and data["name"] is not None:
            data["name"] = str(data["name"]).strip()
        # 这两个证据开关的 null **有语义**（= 改回"跟随服务器默认"），而 exclude_none
        # 会把 null 直接丢掉，于是"恢复默认"这个操作永远表达不出来。所以单独按
        # model_fields_set 判断"调用方到底有没有传这个键"：传了就照传的值设（哪怕是 None），
        # 没传就完全不碰。
        for _k in ("case_record_video", "live_shot_every"):
            if _k in body.model_fields_set:
                setattr(p, _k, getattr(body, _k))
                data.pop(_k, None)
        # 归属大系统同理：**显式传 null = 移出大系统**，没传 = 不碰。
        # 用 exclude_none 的 data 循环做不到这件事（null 会被丢掉）。
        if "group_id" in body.model_fields_set:
            p.group_id = body.group_id
            data.pop("group_id", None)
        if "feishu_bitable_url" in data:
            from app.feishu_bitable import parse_bitable_url

            app_token, table_id = parse_bitable_url(data.pop("feishu_bitable_url"))
            p.feishu_bitable_app_token = app_token
            p.feishu_bitable_table_id = table_id
        for k, v in data.items():
            setattr(p, k, v)
        await s.flush()
        return _project(p)


# ---------------------------------------------------------------------------
# 「大系统」容器（2026-10-13 用户要求）
# ---------------------------------------------------------------------------
#
# 用户的系统是**一个大系统**、下面挂若干模块项目，每个模块都要用同一份系统级需规。
# 这一层只做两件事：**归类**（项目挂到某个大系统下）+ **共享资料**（大系统的需规
# 子项目都能检索到）。**不是父子项目树** —— 大系统本身不跑用例，所以不必把用例/运行
# 往上汇总，改动面小得多。
#
# 权限：这一层是**工作区级**的（不属于某个项目）。
# ★ 2026-10-13 用户明确「就一个人用」—— 所以这里**刻意不加 admin 判断**，直接放开
# （它等价于"谁能建项目"）。将来若真的多人用，收紧点就在这一组端点：加 admin 判断即可。
# 写这段注释是为了把"这是决定、不是疏漏"记在读到它的地方。


async def _group_or_404(s, gid: int) -> ProjectGroup:
    row = (
        await s.execute(select(ProjectGroup).where(ProjectGroup.id == gid))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "大系统不存在")
    return row


@router.get("/project-groups")
async def list_project_groups() -> list[dict]:
    """所有「大系统」+ 挂了几个项目 + 共享需规多大。"""
    from app import knowledge

    async with db_session() as s:
        groups = (
            (await s.execute(select(ProjectGroup).order_by(ProjectGroup.name))).scalars().all()
        )
        out: list[dict] = []
        for g in groups:
            cnt = (
                await s.execute(
                    select(func.count()).select_from(Project).where(Project.group_id == g.id)
                )
            ).scalar_one()
            out.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "projects": int(cnt),
                    "knowledge": await knowledge.group_stats(s, g.id),
                }
            )
        return out


@router.post("/project-groups")
async def create_project_group(body: GroupIn) -> dict:
    async with db_session() as s:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "大系统名字不能为空")
        g = ProjectGroup(name=name)
        s.add(g)
        await s.flush()
        return {
            "id": g.id,
            "name": g.name,
            "projects": 0,
            "knowledge": {"chunks": 0, "chars": 0},
        }


@router.put("/project-groups/{gid}")
async def update_project_group(gid: int, body: GroupPatch) -> dict:
    from app.models import _utcnow

    async with db_session() as s:
        g = await _group_or_404(s, gid)
        if body.name is not None:
            nm = body.name.strip()
            if not nm:
                raise HTTPException(400, "大系统名字不能为空")
            g.name = nm
        g.updated_at = _utcnow()
        await s.flush()
        return {"id": g.id, "name": g.name}


@router.delete("/project-groups/{gid}")
async def delete_project_group(gid: int) -> dict:
    """删大系统：**不删项目**（把它们退回平铺），只删它的共享需规块。"""
    from app import knowledge

    async with db_session() as s:
        g = await _group_or_404(s, gid)
        await s.execute(update(Project).where(Project.group_id == gid).values(group_id=None))
        await knowledge.delete_group_knowledge(s, gid)
        await s.delete(g)
        await s.flush()
        return {"deleted": True}


@router.get("/project-groups/{gid}/knowledge")
async def get_group_knowledge(gid: int) -> dict:
    """大系统的**共享**需规（子项目检索时都能用到它）。"""
    from app import knowledge

    async with db_session() as s:
        g = await _group_or_404(s, gid)
        text = await knowledge.get_group_text(s, gid)
        st = await knowledge.group_stats(s, gid)
        # 上传过的原件清单（2026-10-17）+ 正文。界面据此列「原件」入口。
        return {
            "text": text,
            "chars": st["chars"],
            "chunks": st["chunks"],
            "files": list(getattr(g, "knowledge_files", None) or []),
        }


@router.put("/project-groups/{gid}/knowledge")
async def set_group_knowledge(gid: int, body: KnowledgeIn) -> dict:
    """换掉大系统的共享需规 —— 只动**共享**那份，不碰任何项目自己的资料。"""
    from app import knowledge
    from app.models import _utcnow

    async with db_session() as s:
        g = await _group_or_404(s, gid)
        res = await knowledge.replace_group_knowledge(s, gid, body.text or "", source="大系统共享")
        g.updated_at = _utcnow()
        await s.flush()
        return {"ok": True, **res}


@router.post("/project-groups/{gid}/knowledge/upload")
async def upload_group_knowledge(gid: int, files: list[UploadFile] = File(...)) -> dict:
    """把文件上传进大系统的**共享**需规（2026-10-17 用户要求「共享需规也要以上传的形式」）。

    和「项目文档」那边的上传走**同一套解析**（`docparse`：md / txt / json / csv / docx /
    pdf / xlsx / html / rtf），区别只有落点 —— 这里进的是大系统共享那份，
    它下面每个项目检索时都会带上。

    ## 为什么是**追加**而不是替换

    共享需规是一份**累积**的系统级资料（几个模块的说明书都往里放）。上传一份就把前面的
    替换掉，等于把之前传的全抹了。要整体换掉继续用 `PUT`（粘贴编辑那条路）。

    ## 为什么不行就明说

    单个文件读不了（格式不支持、扫描件没文字层）不该让整批失败 —— 其余照常入库，
    但**必须**在 warnings 里点名是哪一份、为什么，否则用户会以为传进去了。
    """
    from app import docparse, knowledge, storage
    from app.models import _utcnow

    import os as _os
    import tempfile as _tempfile
    import uuid as _uuid

    done: list[dict] = []
    warnings: list[str] = []
    files_meta: list[dict] = []

    async with db_session() as s:
        await _group_or_404(s, gid)
        existing = await knowledge.get_group_text(s, gid)
        blocks = [existing.strip()] if existing.strip() else []

        for f in files:
            name = (f.filename or "").strip() or "上传的资料"
            try:
                data = await _read_upload_capped(f, docparse.MAX_UPLOAD_BYTES)
            except HTTPException as exc:
                warnings.append(f"「{name}」太大或读取失败：{exc.detail}")
                continue
            try:
                res = docparse.extract(name, data)
            except docparse.UnsupportedDocument as exc:
                warnings.append(f"「{name}」读不了：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001 — 一份坏文件不该拖垮整批
                warnings.append(f"「{name}」读不了：{type(exc).__name__}: {exc}")
                continue
            text = (res.text or "").strip()
            if not text:
                warnings.append(f"「{name}」解析出来是空的（扫描件需要 OCR），已跳过。")
                continue
            blocks.append(f"## {name}\n{text}")
            warnings.extend(f"「{name}」：{w}" for w in (res.warnings or []))
            done.append(
                {"filename": name, "chars": len(text), "format": res.fmt, "bytes": len(data)}
            )
            # ★ 原件也留一份（2026-10-17 用户要求「同时也要可以查看到原格式的文件」）。
            # 存不下**不让这次上传失败**（正文已经并进去了），但必须在 warnings 里说出来 ——
            # 否则用户以为能打开原件，点的时候才发现没有。
            _entry = {
                "name": name,
                "bytes": len(data),
                "chars": len(text),
                "format": res.fmt,
                "url": "",
                "key": "",
                "at": _utcnow().isoformat(),
            }
            try:
                _ext = _os.path.splitext(name)[1].lower()[:12]
                _key = f"groups/{gid}/knowledge/{_uuid.uuid4().hex[:12]}{_ext}"
                with _tempfile.TemporaryDirectory(prefix="tp_gkb_") as _td:
                    _p = _os.path.join(_td, "up" + _ext)
                    with open(_p, "wb") as _fh:
                        _fh.write(data)
                    _entry["url"] = await storage.upload(_p, _key)
                _entry["key"] = _key
            except Exception as _exc:  # noqa: BLE001
                warnings.append(
                    f"「{name}」的原件没能保存（{type(_exc).__name__}）：只能看解析出的正文。"
                )
            files_meta.append(_entry)

        if not done:
            st = await knowledge.group_stats(s, gid)
            return {
                "ok": False,
                "added": [],
                "warnings": warnings,
                "text": existing,
                "chars": st["chars"],
                "chunks": st["chunks"],
            }

        g = await _group_or_404(s, gid)
        res2 = await knowledge.replace_group_knowledge(
            s, gid, "\n\n".join(blocks), source="大系统共享"
        )
        # ★ 必须**重新赋值**，不能原地 append —— SQLAlchemy 的 JSON 列不追踪容器内部变更，
        #   原地改的话 flush 时认为"没变"，清单会静默丢失（而文件其实已经存进 artifacts 了）。
        g.knowledge_files = list(g.knowledge_files or []) + files_meta
        g.updated_at = _utcnow()
        await s.flush()
        # 回读合并后的全文给界面 —— 用户刚追加完就该看到它，而不是等下一次刷新。
        text = await knowledge.get_group_text(s, gid)
        files_out = list(g.knowledge_files or [])

    return {
        "ok": True,
        "added": done,
        "warnings": warnings,
        "text": text,
        "files": files_out,
        "chars": res2.get("chars", len(text)),
        "chunks": res2.get("chunks", 0),
    }


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


@router.post("/projects/{pid}/assistant/parse")
async def assistant_parse_attachment(pid: int, request: Request, file: UploadFile = File(...)) -> dict:
    """解析一个要粘进对话的文件，返回文本（2026-10-12）。

    ## 为什么单独一个端点，而不是让对话那一路收文件

    对话那一路是 JSON、消息还有 4000 字上限；把文件塞进去会让请求体随附件线性变大
    （base64 还再涨三分之一）。这里先把文件变成文本，对话那边只传文本 ——
    而且**文本能被裁剪、能标出处**，模型看到的东西和用户上传的是同一份。

    ## 两个上限都不是随手定的

    * 单文件 201,000 字符：与知识库那条路的上限一致（`docparse` 里同一个理由 ——
      一份 46 万字的需规塞进一次对话只会把上下文挤爆，而模型也用不上那么多）；
    * 只认 `docparse` 支持的格式：不支持的（老的 `.doc`）**明确报 400 并说怎么改**，
      而不是回一段空文本让用户以为"上传成功但没内容"。
    """
    await _project_access(request, pid, "viewer")
    from app import docparse

    name = (file.filename or "").strip() or "attachment"
    data = await _read_upload_capped(file, 80 * 1024 * 1024)
    try:
        # extract 直接吃 (文件名, 字节)：文件不必落盘 —— 少一次写盘、少一处清理。
        res = docparse.extract(name, data)
    except docparse.UnsupportedDocument as exc:
        # 格式不支持要**明确报错并说怎么改**，不能回一段空文本 ——
        # 那会变成"上传成功但没内容"这种最难查的故障。
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"读不了「{name}」：{type(exc).__name__}: {exc}") from exc

    text = res.text or ""
    return {
        # `ExtractResult` 里没有文件名（它只描述"解析出来的东西"），
        # 所以这里回填上传时的原名 —— 对话里要显示"附了哪个文件"，没有名字就没法引用。
        "filename": name,
        "fmt": res.fmt,
        "chars": len(text),
        "text": text,
        "truncated": bool(res.truncated),
        "warnings": list(res.warnings or []),
    }


@router.post("/projects/{pid}/assistant")
async def project_assistant(pid: int, body: AssistantIn, request: Request) -> dict:
    """In-app chat assistant: one turn against this project, with tools that read
    cases/runs/issues and (on request) create a case or start a run."""
    await _project_access(request, pid, "viewer")
    from app.assistant import assistant_turn

    return await assistant_turn(pid, body.message, body.history, body.attachments)


@router.get("/projects/{pid}/knowledge/formats")
async def knowledge_formats(pid: int, request: Request) -> dict:
    """**后端实际支持**的需规文件格式（2026-10-12）。

    为什么要做成接口而不是在前端写死一份列表：前端写死的清单会和
    `docparse` 真实支持的集合**慢慢漂移** —— 加了新格式前端不知道，
    删了旧格式前端还在放行。放行一个后端读不了的格式，
    用户拿到的是"上传成功但内容为空"这种最难查的故障。

    分三组返回，让界面能说清"推荐用什么、哪些不支持"：
      * text   纯文本类（md/txt/csv/json…）
      * office 压缩格式的 Office（docx/xlsx/pdf/html/rtf）
      * legacy 老二进制格式（doc/xls/ppt）——**读不了**，但要明确列出来，
        否则用户会一直试 .doc 然后不知道为什么失败
    """
    await _project_access(request, pid, "viewer")
    from app import docparse

    text_exts = sorted(docparse._TEXT_EXTS)
    # docparse 里 Office/PDF/HTML/RTF 走的是各自的解析分支，不在 _TEXT_EXTS 里
    office_exts = sorted({".docx", ".docm", ".pdf", ".xlsx", ".xlsm", ".xltx", ".html", ".htm", ".xhtml", ".rtf"})
    legacy = sorted(docparse._LEGACY_BINARY)
    # 图片走 OCR（2026-10-17），单独一组：界面可以据此说明"识别出来的可能有错字"
    image_exts = sorted(docparse._IMAGE_EXTS)
    return {
        "text": text_exts,
        "office": office_exts,
        "image": image_exts,
        "legacy_unsupported": legacy,
        "accept": ",".join(text_exts + office_exts + image_exts),
        "label": docparse.SUPPORTED_EXT_LABEL,
    }


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
        emb = await knowledge.embed_stats(s, pid)
    return {"text": text, "chars": st["chars"], **st, "embed": emb}


@router.post("/projects/{pid}/knowledge/embed")
async def embed_project_knowledge(pid: int, request: Request, force: bool = False) -> dict:
    """补算/重算知识块的向量（2026-10-19）。

    `force=true` 全量重算（换模型、怀疑向量坏了时用）；默认只补缺失的部分。
    前台同步执行 —— 单项目通常 1~3 秒，用户点完想立刻看到结果。
    """
    await _project_access(request, pid, "editor")
    from app import knowledge

    return await knowledge.embed_project(pid, force=force)


@router.put("/projects/{pid}/knowledge")
async def set_project_knowledge(
    pid: int, body: KnowledgeIn, request: Request, background: BackgroundTasks
) -> dict:
    """Replace the project's spec/knowledge text (paste or upload the requirement doc).

    Stored as chunks, not one row: retrieval cost then depends on the query, not on
    how large the document is.
    """
    await _project_access(request, pid, "editor")
    from app import knowledge

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        res = await knowledge.replace_knowledge(s, pid, body.text or "", source="paste")
    # 向量化放后台：一份 46 万字的需规有上千块，卡在请求里会让用户以为上传失败。
    # 失败/模型缺失都只是"没向量"，检索自动退回关键词 —— 不影响上传本身。
    background.add_task(knowledge.embed_project, pid)
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


# ---- 从需规文档生成用例（2026-10-09）------------------------------------
#
# 串起原本散落的三步：文档已进知识库 → 按章节生成草稿 → 人工审阅后启用/直接跑。
# 刻意**不**做成一个大而全的"一键生成全部"：一份 46 万字的需规一次生成几十上百条，
# 调用会失控，而且生成质量无法逐节确认。所以接口按"列章节 → 逐节生成 → 审阅"三段拆开。


# ---- 项目文档：需求分析 / 总览 / 流程图（2026-10-12）------------------------
#
# 用户的原话：「我希望你加上一栏，里面放上需求分析或者该项目的总览……
# 里面要有该项目的需规和流程图……最好是和我上传的大需规一样，
# 但是你那个要是 AI 分析并截取过的」。
#
# 与知识库的分工：知识库存**原件**（检索用），这里存**提炼**（人读用）。
# 正文是富文本 HTML（用户要求能改字体/颜色/字号/贴图），渲染前必须净化。

_DOC_KINDS = ("overview", "analysis", "flow", "spec_source", "custom")

# 上传 HTML 存成文档正文时的字符上限（2026-10-13）。
# 自包含的"单文件网页"（图片内嵌成 base64）动辄几 MB；净化器默认上限 40 万字符会把
# 正文和图片大面积截掉 —— 实测一份 6.7MB 的需规在 40 万上限下只剩 3 张图（共 42 张）。
_DOC_HTML_MAX_CHARS = 20_000_000
_DOC_TITLE_DEFAULT = {
    "overview": "项目总览",
    "analysis": "需求分析",
    "flow": "流程图",
    # 上传的资料原文（2026-10-12）。单独一种 kind 而不是塞进 custom：
    # 它要能按文件名重复上传时**覆盖同一份**（见 create_doc_from_file），
    # 而 custom 允许多篇同名，定位不到"哪一份是重传"。
    "spec_source": "资料原文",
    "custom": "自定义文档",
}


def _sanitize_doc_html(html: str) -> str:
    """富文本净化的**唯一入口**。所有写文档的路径都必须过这里。

    单独包一层而不是直接调 `doc_sanitize.sanitize`：这样"哪些地方在存 HTML"
    可以 grep 出来，而漏掉一处就是一条存储型 XSS（见 doc_sanitize 的注释）。
    """
    from app import doc_sanitize

    return doc_sanitize.sanitize(html or "")


def _doc_html_from_parsed(data: bytes, res) -> str:
    """把 `docparse` 的解析结果转成文档正文 HTML（上传 / 重新排版共用）。

    HTML 原件保留原始结构；其它格式交给 `doc_text_html` 按段落/标题/列表/表格
    排版。2026-10-19 之前这里是"每行一个 `<p>`"，PDF 的折行于是变成一段一段。
    """
    from app.doc_sanitize import sanitize
    from app.doc_text_html import text_to_doc_html

    text = (res.text or "").strip()
    if res.fmt == "html":
        from bs4 import BeautifulSoup

        raw = str(BeautifulSoup(data, "lxml"))
        # 放宽上限（见 _DOC_HTML_MAX_CHARS）：默认 40 万会把自包含网页的图片/正文截掉。
        html = sanitize(raw, max_chars=_DOC_HTML_MAX_CHARS)
        if html:
            return html
        # 净化失败关闭 → 回落到文本版。`docparse` 对 HTML 走的是 markdownify，
        # 所以按 Markdown 排版才对得上。
        return sanitize(text_to_doc_html(text, "md"))
    # 文件名已经是页面顶部的标题（`title` 输入框），正文不再重复一份 `<h2>{name}</h2>`。
    return sanitize(text_to_doc_html(text, res.fmt))


def _doc_row(r) -> dict:
    return {
        "id": r.id,
        "kind": r.kind,
        "title": r.title or _DOC_TITLE_DEFAULT.get(r.kind, ""),
        "html": r.html or "",
        "sources": r.sources or [],
        "origin": r.origin or "",
        # 助手能不能改这份文档（2026-10-12）。前端据此显示"只有我能改"的开关；
        # 助手侧由 `_write_project_doc` 强制 —— 界面上的开关只是提示，
        # 真正的门在服务端（否则绕过界面就能改）。
        "ai_editable": bool(getattr(r, "ai_editable", True)),
        # 上传的原件（2026-10-16）。有 source_url 才显示「原件」入口 ——
        # 这一列上线前导入的文档没有原件，界面不该给它画一个点不开的按钮。
        "source_url": getattr(r, "source_url", None) or "",
        "source_name": getattr(r, "source_name", None) or "",
        "source_bytes": getattr(r, "source_bytes", None) or 0,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("/projects/{pid}/docs")
async def list_project_docs(pid: int, request: Request) -> dict:
    """项目文档列表（含内置文档的占位，界面不用自己编）。"""
    await _project_access(request, pid, "viewer")
    from sqlalchemy import select

    from app.models import ProjectDoc

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        rows = (
            (
                await s.execute(
                    select(ProjectDoc)
                    .where(ProjectDoc.project_id == pid)
                    .order_by(ProjectDoc.kind, ProjectDoc.id)
                )
            )
            .scalars()
            .all()
        )
    docs = [_doc_row(r) for r in rows]
    have = {d["kind"] for d in docs if d["kind"] != "custom"}
    # 三种内置文档都补上占位，这样左侧栏永远是同一组入口 ——
    # 缺哪一项就少一个按钮，用户会以为是权限问题。
    # `spec_source` **不补占位**：它是"上传了哪几份资料"，没有资料时就不该有一个
    # 空条目摆在那儿（用户会点进去发现是空的）。有上传才出现。
    for k in ("overview", "analysis", "flow"):
        if k not in have:
            docs.append(
                {
                    "id": 0, "kind": k, "title": _DOC_TITLE_DEFAULT[k],
                    "html": "", "sources": [], "origin": "", "updated_at": None,
                    # 占位也要带这个字段：前端 `active.ai_editable` 会读到 undefined，
                    # 而 undefined 在"是否可改"的判断里是假值 —— 于是新建的总览
                    # 会显示成"被锁住"。占位代表"还没有这份文档"，按可改处理。
                    "ai_editable": True,
                }
            )
    docs.sort(
        key=lambda d: (
            _DOC_KINDS.index(d["kind"]) if d["kind"] in _DOC_KINDS else 9,
            d["title"],
        )
    )
    return {"docs": docs}


@router.put("/projects/{pid}/docs")
async def upsert_project_doc(pid: int, request: Request, body: dict) -> dict:
    """新建或更新一份文档。

    按 (project_id, kind) 定位现有行再更新 —— 一个项目的"总览"就该只有一份，
    不然会出现两份互相矛盾的总览，而用户不知道该信哪个。
    `custom` 例外：允许任意多篇，靠标题区分。
    """
    await _project_access(request, pid, "editor")
    from sqlalchemy import select

    from app.models import ProjectDoc

    kind = str(body.get("kind") or "custom").strip()
    if kind not in _DOC_KINDS:
        raise HTTPException(400, f"kind 必须是 {_DOC_KINDS} 之一")
    title = str(body.get("title") or _DOC_TITLE_DEFAULT.get(kind, "")).strip()[:200]
    raw = str(body.get("html") or "")
    html = _sanitize_doc_html(raw)
    # 「只有我能改」（2026-10-12）：上传/新建时可以带上这个选择。
    # 只有显式传了才动它 —— 否则每次保存正文都会把权限重置回默认值，
    # 而用户根本不知道自己改过权限。
    perm = body.get("ai_editable")
    perm_set = isinstance(perm, bool)
    # 净化前后差距过大 = 有一段被丢掉了（脚本、iframe…）。
    # 静默丢弃会让用户以为"保存成功但内容不见了"，所以要把这件事说出来。
    dropped = len(raw) - len(html) > 200

    doc_id = body.get("id")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        row = None
        if doc_id:
            row = (
                await s.execute(
                    select(ProjectDoc).where(
                        ProjectDoc.id == int(doc_id), ProjectDoc.project_id == pid
                    )
                )
            ).scalar_one_or_none()
        if row is None and kind != "custom":
            row = (
                await s.execute(
                    select(ProjectDoc).where(
                        ProjectDoc.project_id == pid, ProjectDoc.kind == kind
                    )
                )
            ).scalars().first()
        if row is None:
            row = ProjectDoc(project_id=pid, kind=kind, title=title, origin="human")
            s.add(row)
        row.title = title or row.title
        row.html = html
        if perm_set:
            # 只在显式传入时改 —— 见上面 perm_set 的注释。
            row.ai_editable = bool(perm)
        # 人改过就标 human —— AI 生成的标 ai，用户一眼能看出哪些还没被人看过。
        row.origin = "human"
        from app.models import _utcnow

        row.updated_at = _utcnow()
        await s.flush()
        out = _doc_row(row)
    return out | {"dropped_content": dropped}


@router.post("/projects/{pid}/docs/from-file")
async def create_doc_from_file(
    pid: int,
    request: Request,
    file: UploadFile = File(...),
    # ★ 必须是 `Form`，不能是普通标量参数（2026-10-12 实测踩到）。
    # FastAPI 对"同时有 File 的端点"里的裸标量参数按 **query** 处理，
    # 于是前端以表单字段发来的 ai_editable 被**静默忽略**、永远用默认值 ——
    # 结果是用户取消勾选"允许助手修改"，上传完却没锁上，而且没有任何报错。
    ai_editable: str = Form("1"),
) -> dict:
    """把一个上传文件变成**可查看、可编辑的文档**（2026-10-12）。

    ## 为什么需要它

    用户反馈「上传的需归文档也无法查看修改」。查下来：上传走的是
    `/knowledge/append`，内容只进**知识库**（喂给 AI 检索的分块），
    而文档列表里那份「需归文档」是他自己新建的**空文档**（html 7 字节）——
    文件和文档之间根本没有连线，所以他看不到、也改不了上传的东西。

    知识库和项目文档是两件事，都该有：
      * 知识库 = AI 检索用的分块（大文档靠它，不必整篇进上下文）；
      * 项目文档 = 人读、人改的正文（也是"只有我能改"这条权限的载体）。
    所以这里把解析出的原文写进一份 `spec_source` 文档：同一份文件
    既进了知识库、也在文档页里看得见改得动。

    ## `ai_editable`

    上传处那个「允许助手修改」的勾选（默认勾上）。
    用户明确要求「上传的文件我可以选择 ai 是否可以改」——
    上传**外部原件**（客户给的说明书、别人写的需规）时取消勾选：
    那是别人给的东西，助手改了就对不上，而用户日后核对时看到的是被改过的版本。
    """
    await _project_access(request, pid, "editor")
    from app import docparse
    from app.models import ProjectDoc, _utcnow

    name = (file.filename or "").strip() or "上传的文档"
    data = await _read_upload_capped(file, 80 * 1024 * 1024)
    try:
        res = docparse.extract(name, data)
    except docparse.UnsupportedDocument as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"读不了「{name}」：{type(exc).__name__}: {exc}") from exc

    text = (res.text or "").strip()
    if not text:
        raise HTTPException(400, f"「{name}」解析出来是空的，没有内容可存入文档。")

    # 用户在**上传处**选的"允许助手修改"（默认允许）。
    editable = str(ai_editable).strip() not in ("0", "false", "False", "")

    # ★ 上传的 HTML 要**保留原始结构**（2026-10-13 修用户报的「html 文件变了、图片看不到」）。
    #
    # 原因：`docparse.extract` 是给**检索**用的 —— 它把任何文件拍成可搜索的纯文本
    # （HTML 走 markdownify 变成 markdown）。而文档正文要给人**看**。原来两条路都用
    # `res.text`，于是 HTML 的图片 / 表格 / 样式全没了 —— 正文只剩一段被转义的文本。
    #
    # 知识库继续用 `res.text`（检索要纯文本）；文档正文交给共用的排版函数 ——
    # HTML 保留原结构，其它格式按段落/标题/列表/表格排。
    #
    # ★ 2026-10-19 修：原来这里是 `text.split("\n")` 每行一个 `<p>`。PDF 的解析
    # 结果是"一个视觉行一个换行"，于是正文变成每行一段、句子被拆碎，标题和表格
    # 也原样显示成 `## 第 1 页` / `字段 | 含义` —— 用户反馈「文字排布有很大的问题」。
    html = _doc_html_from_parsed(data, res)

    # 解析产物的指纹（2026-10-16）：重传时用它判断"正文是不是已经被人改过"，
    # 别把人工修改静默覆盖掉。见 models.ProjectDoc.html_parse_hash。
    from hashlib import sha256 as _sha256

    parse_hash = _sha256(html.encode("utf-8")).hexdigest()[:32]

    # ★ 存一份**原件**（2026-10-16 用户要求「我想全部看到原件」）。
    #
    # 解析出来的正文是**有损**的：PDF 丢版面、docx 丢标题层级、图片和表格都不在里面。
    # 所以原件必须留下 —— 它是"格式到底对不对"的唯一权威，也是用户想"看回原版"的退路。
    # 存进 artifacts 之后：人从界面上打开看；服务端靠 source_key 把它读回来喂给助手。
    #
    # 存失败**不让上传失败**：正文已经解析好了，最坏情况只是这一份"没有原件可看"，
    # 但必须在 warnings 里说出来 —— 静默降级会让用户以为原件在，等到要看时才发现不在。
    from app import storage

    import os as _os
    import tempfile as _tempfile
    import uuid as _uuid

    warnings = list(res.warnings or [])
    src_key = src_url = ""
    src_bytes = 0
    _ext = _os.path.splitext(name)[1].lower()[:12]
    try:
        src_key = f"docs/{pid}/original/{_uuid.uuid4().hex[:12]}{_ext}"
        with _tempfile.TemporaryDirectory(prefix="tp_docsrc_") as _td:
            _p = _os.path.join(_td, "up" + _ext)
            with open(_p, "wb") as _f:
                _f.write(data)
            src_url = await storage.upload(_p, src_key)
        src_bytes = len(data)
    except Exception as _exc:  # noqa: BLE001 — 原件存不下不该拖垮正文导入
        src_key = src_url = ""
        src_bytes = 0
        warnings.append(f"原件没能保存（{type(_exc).__name__}）：这一份只能看解析出的正文。")

    async with db_session() as s:
        row = (
            await s.execute(
                select(ProjectDoc).where(
                    ProjectDoc.project_id == pid,
                    ProjectDoc.kind == "spec_source",
                    ProjectDoc.title == name[:200],
                )
            )
        ).scalars().first()
        created = row is None
        if row is None:
            row = ProjectDoc(
                project_id=pid,
                kind="spec_source",
                title=name[:200],
                # ★ 别忘了正文！漏掉这一个参数的效果是"创建成功但文档是空的" ——
                # 而这恰好就是用户报的那个症状（7 字节的空壳），
                # 所以测试必须断言**正文内容**，只断言 created=True 会漏过去。
                html=html,
                origin="human",
                ai_editable=editable,
                html_parse_hash=parse_hash,
                source_key=src_key or None,
                source_url=src_url or None,
                source_name=name[:300],
                source_bytes=src_bytes or None,
            )
            s.add(row)
        else:
            # 同一个文件重新上传：默认用新解析结果替换旧的，而不是越追加越长 ——
            # 用户重传通常正是因为上次解析得不理想。
            #
            # ★ 但**人手工改过的正文不能覆盖**（2026-10-16 用户要求"上传的文件人工都要
            # 可以手动修改"）：用户可能已经把那份有损正文补全、加了批注，一次重传就抹掉
            # 等于白干，而且界面上不会有任何提示。判断依据是**解析产物指纹**，不是 origin
            # —— 首次上传写进去的 origin 就是 "human"，用它判断等于永远为真。
            _old = row.html or ""
            _edited = (
                bool(_old.strip())
                and bool(row.html_parse_hash)
                and _sha256(_old.encode("utf-8")).hexdigest()[:32] != row.html_parse_hash
            )
            if _edited:
                warnings.append(
                    "这份文档的正文你已经手工改过，这次**没有**用新解析结果覆盖它（原件已更新）。"
                    "要按新解析重来，请先把正文清空再传一次。"
                )
            else:
                row.html = html
                row.html_parse_hash = parse_hash
            row.ai_editable = editable
            row.origin = "human"
            # 重传就连原件一起换掉（用户重传往往就是要换一版文件）。
            if src_key:
                row.source_key = src_key
                row.source_url = src_url
                row.source_name = name[:300]
                row.source_bytes = src_bytes
        row.updated_at = _utcnow()
        await s.flush()
        out = _doc_row(row)

    return {
        "ok": True,
        "created": created,
        "doc": out,
        "chars": len(text),
        "truncated": bool(res.truncated),
        "warnings": warnings,
    }


@router.post("/projects/{pid}/docs/{doc_id}/reparse")
async def reparse_project_doc(
    pid: int, doc_id: int, request: Request, body: dict | None = None
) -> dict:
    """用**原件**重新解析并排版正文（2026-10-19）。

    ## 为什么需要它

    上传时的排版逻辑修好了（见 `_doc_html_from_parsed`），但**已经上传过的文档**
    正文是旧逻辑写进库里的（每行一个 `<p>`）。用户不该为了排版升级重新找一遍
    文件、再上传一次 —— 原件本来就存着（`source_key`），直接重新解析即可。

    ## 为什么可能拒绝

    文档正文允许人工修改。重新解析会用原件重写正文，如果这份文档在解析之后
    被人改过（`html_parse_hash` 对不上），默认不覆盖 —— 先返回 `reason="edited"`，
    由界面确认后再带 `force=true` 重试。不能静默吞掉用户的编辑。
    """
    await _project_access(request, pid, "editor")
    from hashlib import sha256 as _sha256

    from app import docparse, storage
    from app.models import ProjectDoc, _utcnow

    force = bool((body or {}).get("force"))

    async with db_session() as s:
        row = await s.get(ProjectDoc, doc_id)
        if row is None or row.project_id != pid:
            raise HTTPException(404, "document not found")
        if not row.source_key:
            raise HTTPException(400, "这份文档没有保存原件，无法重新解析 —— 请重新上传原文件。")
        key = row.source_key
        name = row.source_name or row.title or "上传的文档"
        old = row.html or ""
        old_hash = row.html_parse_hash or ""
        edited = bool(old.strip() and old_hash) and _sha256(old.encode("utf-8")).hexdigest()[:32] != old_hash

    if edited and not force:
        return {
            "ok": False,
            "reason": "edited",
            "message": "这份文档的正文在解析后被人改过；重新排版会覆盖这些修改。",
        }

    data = await storage.read_bytes(key)
    if not data:
        raise HTTPException(404, "原件已经找不到了（存储里没有这份文件），请重新上传。")
    try:
        res = docparse.extract(name, data)
    except docparse.UnsupportedDocument as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"读不了「{name}」：{type(exc).__name__}: {exc}") from exc

    html = _doc_html_from_parsed(data, res)
    # 和上传路径同样失败关闭：宁可报错，也不能把一份读空的正文写进库里 ——
    # 那等于用一次点击把用户原来的文档清空。
    if not html.strip():
        raise HTTPException(400, "重新解析后没有可用的正文，已保留原正文。")
    new_hash = _sha256(html.encode("utf-8")).hexdigest()[:32]

    async with db_session() as s:
        row = await s.get(ProjectDoc, doc_id)
        if row is None or row.project_id != pid:
            raise HTTPException(404, "document not found")
        row.html = html
        row.html_parse_hash = new_hash
        row.updated_at = _utcnow()
        await s.flush()
        out = _doc_row(row)

    return {
        "ok": True,
        "doc": out,
        "chars": len(res.text or ""),
        "warnings": list(res.warnings or []),
    }


@router.post("/projects/{pid}/docs/analyze")
async def analyze_project_docs(pid: int, request: Request, body: dict | None = None) -> dict:
    """让 AI 把整份需规读成一份项目概况，并**存成可编辑的文档**。

    请求体可选：{"kind": "overview"|"analysis"|"flow", "replace": true}
    `replace=false`（默认）时把生成结果**追加**在现有内容后面 ——
    用户已经改过的地方不该被一次重新生成抹掉。
    """
    await _project_access(request, pid, "editor")
    from sqlalchemy import select

    from app import doc_sanitize, spec_overview
    from app.models import ProjectDoc

    body = body or {}
    kind = str(body.get("kind") or "overview").strip()
    if kind not in ("overview", "analysis", "flow"):
        raise HTTPException(400, "kind 必须是 overview / analysis / flow 之一")
    replace = bool(body.get("replace"))
    # 「通读全文」模式（2026-10-13 用户开关）：慢、贵，但真读整份需规。
    full = bool(body.get("full"))

    res = await spec_overview.analyze(pid, full=full)
    if res.error and not res.sections:
        # 失败要如实报，不能回一份空文档让用户以为"这个项目没内容"。
        return {
            "ok": False,
            "error": res.error,
            "usage": res.usage or None,
            "headings_used": res.headings_used,
            "doc_chars": res.doc_chars,
        }

    # ★ 只把**属于这份文档的**节写进去（2026-10-12 修一个真实的功能错位）。
    #
    # 问题：`analyze` 一次生成全部六节（总览/模块/流程/角色/规则/名词表），
    # 而这里原来把**六节全写进当前文档**。于是用户在「流程图」上点生成，
    # 拿到的文档开头是「项目总览」、流程图埋在很下面 —— 他点的是"画流程图"，
    # 看到的却是文案。实测截图就是这样。
    #
    # 现在按文档类型挑节：流程图只要那张图，需求分析要"是什么/有哪些/怎么走"这三块，
    # 项目总览拿其余全部（含名词表）。这样每份文档都只说它该说的事。
    _KIND_SECTIONS: dict[str, tuple[str, ...]] = {
        "flow": ("flow",),
        "analysis": ("overview", "modules", "roles", "rules"),
        "overview": ("overview", "modules", "flow", "roles", "rules", "glossary"),
    }
    wanted = _KIND_SECTIONS.get(kind, _KIND_SECTIONS["overview"])
    picked = [s for s in res.sections if s.key in wanted]
    if not picked:
        return {
            "ok": False,
            "error": f"这次没有生成出「{kind}」需要的内容，可以再试一次。",
            "usage": res.usage or None,
            "headings_used": res.headings_used,
            "doc_chars": res.doc_chars,
        }
    generated = doc_sanitize.sanitize(spec_overview.sections_to_html(picked))
    sources = sorted({x for sec in picked for x in sec.sources})

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        row = (
            await s.execute(
                select(ProjectDoc).where(ProjectDoc.project_id == pid, ProjectDoc.kind == kind)
            )
        ).scalars().first()
        if row is None:
            row = ProjectDoc(project_id=pid, kind=kind, title=_DOC_TITLE_DEFAULT[kind])
            s.add(row)
        existing = (row.html or "").strip()
        appended = bool(existing and not replace)
        row.html = f"{existing}\n<hr>\n{generated}" if appended else generated
        row.sources = sources[:200]
        row.origin = "ai"
        from app.models import _utcnow

        row.updated_at = _utcnow()
        await s.flush()
        out = _doc_row(row)

    return {
        "ok": True,
        "doc": out,
        "sections": [x.as_dict() for x in picked],
        "usage": res.usage or None,
        "headings_used": res.headings_used,
        "doc_chars": res.doc_chars,
        "recalled_chars": res.recalled_chars,
        "appended": appended,
    }


@router.patch("/projects/{pid}/docs/{doc_id}/permission")
async def set_doc_permission(pid: int, doc_id: int, request: Request, body: dict) -> dict:
    """设置「只有我能改」（2026-10-12 用户要求）。

    用户原话：「ai 可以阅读那里面所有文档，但是有些文档 ai 无法修改，
    我上传文档的时候可以加个选择，也就是只有我自己能修改」。

    ★ 读不受影响：助手永远能读全部文档（`read_project_docs` 不看这一列）。
    这一列只拦**写**。
    """
    await _project_access(request, pid, "editor")
    from app.models import ProjectDoc, _utcnow

    async with db_session() as s:
        row = await s.get(ProjectDoc, doc_id)
        if row is None or row.project_id != pid:
            raise HTTPException(404, "document not found")
        row.ai_editable = bool(body.get("ai_editable", True))
        row.updated_at = _utcnow()
        await s.flush()
        out = _doc_row(row)
    return out


@router.delete("/projects/{pid}/docs/{doc_id}")
async def delete_project_doc(pid: int, doc_id: int, request: Request) -> dict:
    """删一份自定义文档。内置的三种不允许删（删了左侧栏会缺一项）。"""
    await _project_access(request, pid, "editor")
    from app.models import ProjectDoc

    async with db_session() as s:
        row = await s.get(ProjectDoc, doc_id)
        if row is None or row.project_id != pid:
            raise HTTPException(404, "document not found")
        if row.kind != "custom":
            raise HTTPException(400, "内置文档不能删除，清空内容即可")
        await s.delete(row)
    return {"deleted": doc_id}


@router.post("/projects/{pid}/docs/image")
async def upload_doc_image(pid: int, request: Request, file: UploadFile = File(...)) -> dict:
    """富文本文档里粘贴/插入的图片（2026-10-12）。

    为什么必须走服务端而不是塞 data: URI：data URI 会让每个文档正文变大几十倍
    （一张截图转 base64 约 +33%），而文档是**每次打开页面都要整份读出来**的。
    存成文件、正文里只放 URL，读页面的代价就和图片大小脱钩了。

    校验只认图片 MIME + 扩展名白名单：这个端点会把文件原样存到一个**公网可读**的
    路径下（`/artifacts`），所以"允许上传任意文件"等于开了一个静态文件托管。
    """
    await _project_access(request, pid, "editor")
    from app import storage

    name = (file.filename or "image.png").rsplit(".", 1)[-1].lower()
    allowed = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
    ctype = (file.content_type or "").lower()
    if name not in allowed or (ctype and not ctype.startswith("image/")):
        raise HTTPException(400, f"只支持图片（{sorted(allowed)}），收到 {file.filename!r} / {ctype!r}")
    data = await _read_upload_capped(file, 8 * 1024 * 1024)  # 8MB：截图够用
    import uuid as _uuid

    key = f"docs/{pid}/{_uuid.uuid4().hex[:12]}.{name}"
    import os as _os
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory(prefix="tp_docimg_") as td:
        p = _os.path.join(td, f"up.{name}")
        with open(p, "wb") as f:
            f.write(data)
        url = await storage.upload(p, key)
    return {"url": url, "bytes": len(data)}


# ---- 用例优化提案（2026-10-12）----------------------------------------------
#
# 用户的原话：「我希望 ai 能根据这个页面去优化测试用例和增加测试用例
# 同删除不必要的测试用例」。他选的是**先出提案、逐条审阅后再入库**。
#
# 所以这里有一条硬规矩：**本组端点里只有 `apply` 会碰 `test_case` 表**，
# 生成阶段只写 `case_proposal`。一次静默的误删会让"通过率"从有依据变成没依据，
# 而报告上一个异常都不会有 —— 用例库是这个系统真正的资产，不该由模型直接改。

_PROPOSAL_ACTIONS = ("add", "update", "remove")


@router.get("/projects/{pid}/case-proposals")
async def list_case_proposals(pid: int, request: Request, status: str = "") -> dict:
    """提案清单 + 统计。"""
    await _project_access(request, pid, "viewer")
    from sqlalchemy import select

    from app.models import CaseProposal

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        stmt = select(CaseProposal).where(CaseProposal.project_id == pid)
        if status in ("pending", "accepted", "rejected"):
            stmt = stmt.where(CaseProposal.status == status)
        rows = (
            (await s.execute(stmt.order_by(CaseProposal.status, CaseProposal.action, CaseProposal.id)))
            .scalars()
            .all()
        )
    items = [
        {
            "id": r.id,
            "action": r.action,
            "case_key": r.case_key,
            "case_id": r.case_id,
            "summary": r.summary,
            "reason": r.reason,
            "evidence": r.evidence or [],
            "payload": r.payload or {},
            # 相关流程图（Mermaid 源码）与账号 —— 用户要求"告诉我为啥要加这个用例"
            # 时能看到的证据。
            "mermaid": r.mermaid or "",
            "role": r.role or "",
            "status": r.status,
            "applied_case_id": r.applied_case_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        }
        for r in rows
    ]
    counts = {"pending": 0, "accepted": 0, "rejected": 0}
    by_action: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
        if it["status"] == "pending":
            by_action[it["action"]] = by_action.get(it["action"], 0) + 1
    return {"proposals": items, "counts": counts, "pending_by_action": by_action}


@router.post("/projects/{pid}/case-proposals/generate")
async def generate_case_proposals(pid: int, request: Request, body: dict | None = None) -> dict:
    """让 AI 按项目文档审一遍用例库，产出提案。

    请求体可选：{"replace_pending": true} —— 先清掉还没决定的旧提案再生成。
    默认**保留**旧提案（用户可能已经看了一半，重新生成不该把它冲掉）。
    """
    await _project_access(request, pid, "editor")
    from sqlalchemy import delete, select

    from app import case_optimize
    from app.models import CaseProposal, ProjectDoc, TestCase

    body = body or {}
    replace_pending = bool(body.get("replace_pending"))

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        # 需规内容取"项目总览"，它本身就是从整份需规提炼的骨架 ——
        # 拿原件（46 万字）反而塞不进提示词。
        doc = (
            await s.execute(
                select(ProjectDoc).where(
                    ProjectDoc.project_id == pid, ProjectDoc.kind == "overview"
                )
            )
        ).scalars().first()
        spec_html = (doc.html if doc else "") or ""
        if not spec_html.strip():
            return {
                "ok": False,
                "error": "还没有项目文档。先到「项目文档」页点『AI 分析与生成』，再回来优化用例。",
                "created": 0,
            }
        case_rows = (
            (
                await s.execute(
                    select(TestCase).where(
                        TestCase.project_id == pid, TestCase.status == "active"
                    )
                )
            )
            .scalars()
            .all()
        )
        cases = [
            {
                "id": c.id,
                "case_key": c.case_key,
                "name": c.name,
                "module": c.module,
                "status": c.status,
                "role": c.role,
                "prompt": c.prompt,
                "expected": c.expected,
                "preconditions": c.preconditions,
            }
            for c in case_rows
        ]
        if not cases:
            return {"ok": False, "error": "这个项目还没有生效中的用例，无从优化。", "created": 0}

        # ★ 把这个项目**真实存在**的账号给模型（2026-10-12 用户要求「附上账号」）。
        #
        # 不给的话它会照需规里的角色名编一个（"结算单发起人"），
        # 而 credential 表里那个账号叫 `sittrapply` —— 采纳后用例会因为
        # "找不到账号"直接失败，而用户看不出是提案的问题。
        # 实测项目里已经有 2 条用例踩了这个坑（role 写成了中文角色名）。
        cred_rows = (
            (
                await s.execute(
                    select(Credential).where(
                        Credential.project_id == pid, Credential.is_active == True  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
        accounts = [
            {"role": c.role, "label": c.label, "healthy": bool(c.healthy)}
            for c in cred_rows
            if c.role
        ]

        groups = case_optimize.group_cases_by_module(cases)
        # 只取用例最多的前 12 个模块：一轮全跑会撞网关限额，而且用户第一屏也看不完。
        groups = groups[:12]
        payload_groups = [
            (m, cs, case_optimize.spec_text_for_module(spec_html, m)) for m, cs in groups
        ]

        proposals, usage, failed, failed_modules, mod_of = await case_optimize.optimize_project(
            payload_groups, accounts
        )

        if replace_pending:
            await s.execute(
                delete(CaseProposal).where(
                    CaseProposal.project_id == pid, CaseProposal.status == "pending"
                )
            )
        # 已经提过的提案不重复插入。去重键分两种：
        #   add            → (action, summary)
        #   update/remove  → (action, case_key)
        #
        # ★ 为什么 add 不能只用 case_key：新增提案的 case_key 是 None，
        #   用 None 当键会把**第二条新增提案**当成重复直接丢掉
        #   （实测就是这样：一次生成里两条 add 只进库了一条，而界面上看不出来）。
        existing: set[tuple] = set()
        for r in (
            (await s.execute(select(CaseProposal).where(CaseProposal.project_id == pid)))
            .scalars()
            .all()
        ):
            existing.add((r.action, r.summary if r.action == "add" else r.case_key))
        by_key = {str(c.get("case_key")): c["id"] for c in cases if c.get("case_key")}
        created = 0
        for p in proposals:
            if (p.action, p.summary if p.action == "add" else p.case_key) in existing:
                continue
            s.add(
                CaseProposal(
                    project_id=pid,
                    action=p.action,
                    case_key=p.case_key,
                    case_id=by_key.get(p.case_key) if p.case_key else None,
                    summary=p.summary[:400],
                    reason=p.reason,
                    evidence=p.evidence,
                    payload=p.payload,
                    # 流程图是**按模块**生成的一张，复制到该模块的每条提案上。
                    # 每条各画一张会把同一个调用付十几遍钱，而且图会各说各话。
                    mermaid=p.flow,
                    role=p.role,
                )
            )
            existing.add((p.action, p.case_key))
            created += 1

    return {
        "ok": True,
        "created": created,
        "modules_scanned": len(groups),
        "modules_failed": failed,
        # 点名失败的模块：只说"有 3 个失败"用户不知道是哪 3 个，也就无法判断要不要补。
        "failed_modules": failed_modules,
        "usage": usage or None,
    }


@router.post("/projects/{pid}/case-proposals/{prop_id}/decide")
async def decide_case_proposal(pid: int, prop_id: int, request: Request, body: dict) -> dict:
    """采纳或驳回**一条**提案。

    采纳的落地方式（三种动作各不相同，理由见 models.CaseProposal 的注释）：
      add    → 新建用例
      update → 只合并 payload 里出现的字段（不整条覆盖）
      remove → 改成 status='deprecated'，**不真删**
    """
    await _project_access(request, pid, "editor")
    return await _apply_proposals(pid, [prop_id], str(body.get("decision") or ""), request)


@router.post("/projects/{pid}/case-proposals/decide")
async def decide_case_proposals_bulk(pid: int, request: Request, body: dict) -> dict:
    """批量采纳/驳回。审阅动辄几十条，一条条点会让人放弃审阅 —— 那等于回到"直接改"。"""
    await _project_access(request, pid, "editor")
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids 必须是非空数组")
    return await _apply_proposals(
        pid, [int(x) for x in ids][:500], str(body.get("decision") or ""), request
    )


async def _apply_proposals(pid: int, ids: list[int], decision: str, request: Request) -> dict:
    """采纳/驳回的公共实现。"""
    if decision not in ("accept", "reject"):
        raise HTTPException(400, "decision 必须是 accept 或 reject")

    from app.models import CaseProposal, _utcnow

    applied: list[dict] = []
    skipped: list[dict] = []
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        # 记下"谁采纳的"。审计需要它：一条用例被改/被作废必须有经手人，
        # 否则出问题时只能看到"它变了"，查不到是谁点的。
        _u = await auth.current_user(request)
        who = _u.email if _u else None
        for prop_id in ids:
            row = await s.get(CaseProposal, prop_id)
            if row is None or row.project_id != pid:
                skipped.append({"id": prop_id, "why": "提案不存在"})
                continue
            if row.status != "pending":
                skipped.append({"id": prop_id, "why": f"已经{ '采纳' if row.status == 'accepted' else '驳回' }过"})
                continue
            row.decided_by = who
            row.decided_at = _utcnow()
            if decision == "reject":
                row.status = "rejected"
                applied.append({"id": prop_id, "action": row.action, "result": "rejected"})
                continue
            try:
                res = await _apply_one(s, pid, row)
            except HTTPException as exc:
                skipped.append({"id": prop_id, "why": str(exc.detail)})
                continue
            row.status = "accepted"
            row.applied_case_id = res.get("case_id")
            applied.append({"id": prop_id, "action": row.action, **res})
    return {"ok": True, "applied": applied, "skipped": skipped}


async def _apply_one(s, pid: int, row) -> dict:
    """把一条提案落到 `test_case`。**这是本组端点里唯一写用例库的地方。**"""
    from sqlalchemy import select

    from app.models import TestCase, _utcnow

    payload = row.payload or {}
    if row.action == "add":
        if not (payload.get("prompt") or "").strip():
            raise HTTPException(400, "这条新增提案没有任务描述，无法生成用例")
        key = await _next_case_key(s, pid)
        case = TestCase(
            project_id=pid,
            case_key=key,
            name=(payload.get("name") or row.summary or "AI 新增用例")[:300],
            module=(payload.get("module") or "")[:200] or None,
            priority=payload.get("priority") or "P1",
            preconditions=payload.get("preconditions") or "",
            prompt=payload.get("prompt") or "",
            expected=payload.get("expected") or "",
            # 账号以**提案自己那一列**为准（它已经过账号清单校验），
            # payload 里的 role 是同一份数据，但列才是权威。
            role=(row.role or payload.get("role") or None),
            tags=payload.get("tags") or [],
            # 出处：这条用例是从"AI 提案"来的，并且指向生成它的那次分析。
            # 没有它，用户无法回答"这 20 条新用例是哪儿冒出来的"。
            source=f"AI 提案 #{row.id}",
        )
        s.add(case)
        await s.flush()
        return {"case_id": case.id, "case_key": key, "result": "created"}

    # update / remove 都要先找到目标用例
    target = None
    if row.case_id:
        target = (
            await s.execute(
                select(TestCase).where(TestCase.id == row.case_id, TestCase.project_id == pid)
            )
        ).scalar_one_or_none()
    if target is None and row.case_key:
        target = (
            await s.execute(
                select(TestCase).where(
                    TestCase.project_id == pid, TestCase.case_key == row.case_key
                )
            )
        ).scalar_one_or_none()
    if target is None:
        raise HTTPException(400, f"找不到用例 {row.case_key or row.case_id}（可能已被别处删除）")

    if row.action == "remove":
        # ★ 不真删。删掉之后报告里那条编号会消失，而真删不可逆 ——
        # 用户想反悔时没有任何办法。作废可以在用例页一键改回来。
        target.status = "deprecated"
        target.updated_at = _utcnow()
        return {"case_id": target.id, "case_key": target.case_key, "result": "deprecated"}

    # update：只合并 payload 里出现的字段
    allowed = ("name", "priority", "preconditions", "prompt", "expected", "role", "module", "tags")
    changed: list[str] = []
    for k in allowed:
        if k not in payload:
            continue
        v = payload[k]
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if k == "priority":
            v = str(v).strip().upper()
            if v not in ("P0", "P1", "P2"):
                continue
        setattr(target, k, v if not isinstance(v, str) else v[:4000])
        changed.append(k)
    if not changed:
        raise HTTPException(400, "这条修改提案没有任何可落地的字段")
    target.updated_at = _utcnow()
    return {"case_id": target.id, "case_key": target.case_key, "result": "updated", "fields": changed}


@router.get("/projects/{pid}/verified-coverage")
async def verified_coverage(pid: int, request: Request) -> dict:
    """**人工确认过的**通过率，按模块统计（2026-10-12）。

    ## 为什么不直接用自动判定结果

    自动判定里有相当一部分不是产品结论：环境没起来、agent 没做完、
    判定器自己挂了。把这些当"失败"算进通过率，会让一个模块看起来千疮百孔，
    而实际上可能一条真缺陷都没有 —— 用户拿着一份不可信的数字做不了决定。

    所以这里只认**人工改判过的**结果（`verdict_override` 非空）：
    那是唯一一条"有人看过并负责"的证据链。同时如实报出另外两个数：
    自动判定的原始通过率、以及有多少条还等着人看 ——
    让用户自己判断"人工样本够不够"，而不是把这个判断藏起来。
    """
    await _project_access(request, pid, "viewer")
    from sqlalchemy import select

    from app.models import Run, RunResult, TestCase

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        rows = (
            await s.execute(
                select(
                    TestCase.module,
                    TestCase.case_key,
                    RunResult.status,
                    RunResult.verdict_override,
                    RunResult.attribution,
                )
                .join(RunResult, RunResult.case_id == TestCase.id)
                .join(Run, Run.id == RunResult.run_id)
                .where(TestCase.project_id == pid)
            )
        ).all()

    per_module: dict[str, dict] = {}
    for module, _key, status, override, attribution in rows:
        m = (module or "").strip() or "未分类"
        b = per_module.setdefault(
            m,
            {
                "module": m,
                "verified_pass": 0,
                "verified_fail": 0,
                "auto_pass": 0,
                "auto_fail": 0,
                "awaiting_review": 0,
                "product_defects": 0,
            },
        )
        # 人工确认过的：以 override 为准
        if override in ("passed", "failed"):
            if override == "passed":
                b["verified_pass"] += 1
            else:
                b["verified_fail"] += 1
        else:
            b["awaiting_review"] += 1
            if status == "passed":
                b["auto_pass"] += 1
            elif status in ("failed", "error"):
                b["auto_fail"] += 1
        if attribution == "product" or attribution == "product_defect":
            b["product_defects"] += 1

    out = []
    for b in per_module.values():
        vn = b["verified_pass"] + b["verified_fail"]
        an = b["auto_pass"] + b["auto_fail"]
        b["verified_total"] = vn
        b["auto_total"] = an
        b["verified_rate"] = round(b["verified_pass"] / vn * 100, 1) if vn else None
        b["auto_rate"] = round(b["auto_pass"] / an * 100, 1) if an else None
        out.append(b)
    out.sort(key=lambda x: (-(x["verified_total"] + x["auto_total"]), x["module"]))
    tot_v = sum(x["verified_total"] for x in out)
    tot_p = sum(x["verified_pass"] for x in out)
    return {
        "modules": out,
        "summary": {
            "verified_total": tot_v,
            "verified_pass": tot_p,
            "verified_rate": round(tot_p / tot_v * 100, 1) if tot_v else None,
            "awaiting_review": sum(x["awaiting_review"] for x in out),
            "product_defects": sum(x["product_defects"] for x in out),
        },
    }


@router.get("/projects/{pid}/insights")
async def project_insights(pid: int, request: Request) -> dict:
    """项目概况面板（2026-10-12）：需求覆盖矩阵 / 规则 / 接口 / 账号 / 变更 / 失败台账。

    ★ 这一层**不算、只查**，一个 LLM 调用都不发 —— 所以它永远可用、秒出、不花 token。
    理由见 `app/project_insights.py` 的模块注释：把事实交给 AI 复述一遍
    只会引入漂移（文档说"覆盖良好"，而实际有条需求一条用例都没测）。
    """
    await _project_access(request, pid, "viewer")
    from app import project_insights as insights

    async with db_session() as s:
        await _get_project_or_404(s, pid)
    # 读库是同步 sqlite 只读连接，放到线程里跑，免得堵住事件循环。
    # 用标准库的 asyncio.to_thread（而不是 anyio）：不额外依赖一个包，
    # 而 anyio 虽然被 starlette 间接带进来，但"间接依赖"不该被当接口用。
    import asyncio

    return await asyncio.to_thread(insights.read_insights, pid)


@router.get("/projects/{pid}/api-endpoints")
async def list_api_endpoints(pid: int, request: Request, writable: str = "") -> dict:
    """从页面流量里学到的后端接口（2026-10-09）。

    用户需求：「这个系统也要有探索后端的能力……去拿到这个菜单或者相关的数据」。

    这张清单是**观察**来的，不是扫描来的 —— 页面为了渲染必然要调后端，
    那些请求就是权威答案。所以清单里的每条接口都是真实在用的。
    写入由 `executor._persist_endpoints` 在每轮用例结束时完成。

    `writable` 可选过滤："1" 只看写接口，"0" 只看读接口。
    界面上默认展示全部，但**写接口应当有明显标记** —— 它们只能看，不能被用例直接调用。
    """
    await _project_access(request, pid, "viewer")
    from sqlalchemy import select

    from app.models import ApiEndpoint

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        rows = (
            (
                await s.execute(
                    select(ApiEndpoint)
                    .where(ApiEndpoint.project_id == pid)
                    .order_by(ApiEndpoint.hits.desc(), ApiEndpoint.path)
                )
            )
            .scalars()
            .all()
        )
    if writable == "1":
        rows = [r for r in rows if r.writable]
    elif writable == "0":
        rows = [r for r in rows if not r.writable]
    return {
        "endpoints": [
            {
                "id": r.id,
                "method": r.method,
                "path": r.path,
                "sample": r.sample,
                "query": r.query or {},
                "body_fields": r.body_fields or [],
                "hits": r.hits,
                "writable": r.writable,
                "origin": r.origin,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
            for r in rows
        ],
        "total": len(rows),
        # 有没有可探索的后端，取决于"跑过用例没有" —— 界面上要据此给出下一步提示，
        # 而不是显示一个空表让人以为功能坏了。
        "note": (
            "这些接口是从**跑用例时页面实际发出的请求**里观察到的。"
            "先把用例跑一轮，清单才会长出来。"
            if not rows
            else "写接口只做了解，用例不会直接调用它们（探索只读）。"
        ),
    }


@router.post("/projects/{pid}/api-endpoints/probe")
async def probe_api_endpoint(pid: int, request: Request, body: dict) -> dict:
    """只读重放一个观察到的接口，用来核对数据（2026-10-09）。

    用户选定的边界是「探索只读，写操作必须显式声明」，所以这里：

    * **只允许 GET/HEAD/OPTIONS** —— 写方法由 `api_probe.method_allowed` 拒绝；
    * **只允许打向项目自己的后端** —— 用 `build_url` 挡掉一切离开 origin 的写法
      （`//evil.com`、绝对 URL、`..` 穿越），否则这就是一个拿着项目凭据的 SSRF 跳板；
    * **需要 editor 权限** —— 它会带着项目凭据发出真实请求。

    请求体：{"method":"GET","path":"/api/menu","params":{...}}
    """
    await _project_access(request, pid, "editor")
    from app import api_probe
    from app.models import ApiEndpoint

    method = str(body.get("method") or "GET").upper()
    path = str(body.get("path") or "")
    if not path:
        raise HTTPException(400, "path 不能为空")
    if not api_probe.method_allowed(method):
        raise HTTPException(
            403,
            f"只允许只读方法（{'/'.join(sorted(api_probe.READ_METHODS))}），"
            f"{method} 会改动数据 —— 探索阶段的写请求可能在你测试环境里建/删真数据。",
        )

    async with db_session() as s:
        proj = await _get_project_or_404(s, pid)
        # 只允许重放**已经观察到**的接口。这一条把"探索"和"扫接口"彻底分开：
        # 不能让这个端点变成一个可以打任意路径的探针。
        row = (
            await s.execute(
                select(ApiEndpoint).where(
                    ApiEndpoint.project_id == pid,
                    ApiEndpoint.method == method,
                    ApiEndpoint.path == path,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                404,
                f"没有观察到过 {method} {path}。只允许调用已发现的接口 —— "
                "请先跑一轮用例，让页面把接口暴露出来。",
            )
        base = (proj.base_url or "").strip()

    if not base:
        raise HTTPException(400, "项目没有配置页面地址（base_url），无法确定后端 origin")

    # 用项目自己的后端 origin。菜单接口通常要登录态，所以带上浏览器里那份会话。
    origin = _origin_of(base)
    headers = await _probe_headers(pid)
    prober = api_probe.Prober(origin, headers)
    res = await prober.get(path, {k: v for k, v in (body.get("params") or {}).items()})
    return res.as_dict() | {"origin": origin, "method": method}


def _origin_of(url: str) -> str:
    from urllib.parse import urlparse

    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


async def _observed_endpoints(pid: int, limit: int = 25) -> list:
    """项目已观察到的后端接口（按被调用次数降序，取前 limit 个）。"""
    try:
        from sqlalchemy import select

        from app.models import ApiEndpoint

        async with db_session() as s:
            return list(
                (
                    await s.execute(
                        select(ApiEndpoint)
                        .where(ApiEndpoint.project_id == pid)
                        .order_by(ApiEndpoint.hits.desc(), ApiEndpoint.path)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
    except Exception:  # noqa: BLE001 — 接口清单是**增强**，拿不到不该让生成失败
        return []


def _api_context_note(endpoints: list) -> str:
    """把接口清单压成给模型的一段说明。没有接口时返回空串。

    刻意与 `api_probe.summarize_for_prompt` 分开：那个是给"助手对话"用的措辞，
    这个是给"生成用例"用的 —— 生成场景更需要一句"所以你的断言可以核对接口数据"，
    否则模型看到一堆接口也不知道该拿它们干什么。
    """
    if not endpoints:
        return ""
    from app.api_probe import summarize_for_prompt

    base = summarize_for_prompt(endpoints)
    if not base:
        return ""
    return (
        base
        + "\n**生成用例时请利用这些接口**：菜单、列表这类内容往往是后端返回的，"
        "所以更好的断言是核对「页面显示的内容」与「接口返回的数据」一致，"
        "而不是只断言「页面上有某个元素」。\n"
        "注意：写接口（POST/PUT/DELETE）只作了解，用例不要直接调用它们 —— "
        "测试只能通过界面去改数据。"
    )


async def _probe_headers(pid: int) -> dict[str, str]:
    """给只读探查用的请求头（带项目已保存的登录态）。

    为什么要带：菜单、列表这些接口基本都要登录态，不带就只会拿到 401，
    那"探索后端"就失去了意义 —— 用户要的正是"拿到这个菜单"。

    只取凭据里那份 cookie/bearer，**不**记录、不回显。
    """
    headers: dict[str, str] = {}
    try:
        from sqlalchemy import select

        from app.models import Credential

        async with db_session() as s:
            cred = (
                await s.execute(
                    select(Credential).where(Credential.project_id == pid).order_by(Credential.id)
                )
            ).scalars().first()
        if cred is None:
            return headers
        from app import crypto

        secret = crypto.decrypt(cred.secret) if cred.secret else ""
        if not secret:
            return headers
        import json

        if cred.type == "storage_state":
            data = json.loads(secret)
            cookies = data.get("cookies") or []
            if cookies:
                headers["Cookie"] = "; ".join(
                    f"{c.get('name')}={c.get('value')}" for c in cookies if c.get("name")
                )
        elif cred.type == "token":
            headers["Authorization"] = f"Bearer {secret}"
        elif cred.type == "password":
            # 用户名密码不能直接变成请求头 —— 那需要先登录换 token，
            # 属于另一个流程。这里明确不做，让接口返回 401 而不是伪造一个坏请求。
            pass
    except Exception as exc:  # noqa: BLE001 — 拿不到凭据不是错误，接口会返回 401
        # api.py 没有模块级 logger（其余地方也不需要），这里就地取一个。
        import logging

        logging.getLogger("potato-test.api").debug("api: 取探查凭据失败：%s", exc)
    return headers


def spec_outline_progress(*, total: int, lo: int, hi: int, failed: bool) -> dict:
    """分批规划的进度三件套（2026-10-09 抽出成纯函数，便于单测）。

    ## 为什么需要一个纯函数

    这里有一个**实测踩到**的缺陷：一份 586 个标题的文档，第 3 批起网关持续返回 500，
    而接口仍然回 `has_more=true` —— 界面于是给用户一个「规划下一批」按钮，
    点一次失败一次，**永远推不动**。用户能做的只有反复点同一个键，而且
    完全看不出是网关的问题还是功能的问题。

    失败时的正确表达是"这批没成功，你可以重试"，而不是"后面还有很多"。
    这条规则值得单独钉住 —— 藏在接口里就只能靠端到端测试才碰得到。
    """
    return {
        "total": total,
        # 失败时 planned_until 停在起点（lo）：没有推进，就不该让界面以为推进了。
        # 否则界面显示"已规划 120/586"而实际一个模块都没拿到 ——
        # 用户看到的进度是假的，下次还会从同一个位置重试失败。
        "planned_until": lo if failed else hi,
        "has_more": bool(hi < total and not failed),
        "batch_from": lo,
        "batch_to": hi,
        "failed": bool(failed),
    }


@router.post("/projects/{pid}/spec/upload")
async def spec_upload(
    pid: int,
    request: Request,
    file: UploadFile = File(...),
    append: bool = True,
) -> dict:
    """把一份需规文档直接吃进项目，并返回可生成的模块清单（2026-10-09）。

    ## 为什么要有这一个端点

    用户的原话是：「我想让这个 agent 实现的功能应该是我发需归文件给内置的助手，
    包括不同格式，然后它可以生成测试用例」。在那之前这件事是**三步手工**：
    界面上传 → 等解析 → 再去另一个面板点生成，而且每一步都要用户自己把上下文带过去。

    这里合成一步：**上传即入库、入库即规划**。格式由 `app/docparse.py` 处理，
    已经支持 md / txt / json / csv / docx / pdf / xlsx / html / rtf
    （以及任何纯文本）—— 这块能力本来就有，缺的是把它接到"生成用例"这条路上。

    `append=true`（默认）把文档**追加**进知识库：一个项目的需规常常是好几份文件
    （需求说明 + 字段表 + 流程图），替换式上传会让上一次的内容消失，
    而用户通常并不知道这一点。要整份替换得显式传 `append=false`。

    规划失败或文档没有标题时**不是错误** —— 仍然返回已入库的事实与提示，
    用户至少知道文件进去了（否则会反复重传同一份文件）。
    """
    await _project_access(request, pid, "editor")
    from app import docparse, knowledge, specgen

    data = await _read_upload_capped(file, docparse.MAX_UPLOAD_BYTES)
    try:
        res = docparse.extract(file.filename or "", data)
    except docparse.UnsupportedDocument as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001 — 损坏文件给一句清楚的话，而不是 500
        raise HTTPException(400, f"读不了「{file.filename}」：{e}") from e

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        if append:
            await knowledge.append_knowledge(s, pid, res.text, source=file.filename or "upload")
        else:
            await knowledge.replace_knowledge(s, pid, res.text, source=file.filename or "upload")

    headings = await specgen.list_headings(pid)
    planned = await specgen.plan_modules(headings[: specgen.MAX_SECTIONS], await specgen.document_intro(pid))
    modules = planned.modules
    # 规划失败（网关不可用、配额用完、返回格式不对）**也要给出可用的分批**。
    #
    # 为什么：文档已经入库了，用户此刻的诉求是"拿它生成用例"。如果这里回一个空列表，
    # 用户看到的是"上传成功但没有可生成的模块"，只能反复重传 —— 而真正的问题
    # （模型调不通）藏在 error 字段里，没人会去看。
    # 退化成"一个标题一个模块"粒度更碎，但整条路仍然走得通，而且 error 会如实带出去。
    degraded = planned.degraded
    if not modules and headings:
        modules = specgen.group_titles_into_modules(headings[: specgen.MAX_SECTIONS])
        degraded = True
    return {
        "filename": file.filename,
        "format": res.fmt,
        "chars": res.chars,
        "truncated": res.truncated,
        # 解析警告要带出去：空文档、图片型 PDF 这类情况**看起来是成功的**，
        # 用户只有看到警告才知道"为什么后面生成不出用例"。
        "warnings": res.warnings
        + ([] if headings else ["文档里没找到章节标题 —— 可能是纯文本或扫描件，按标题分批生成会用不上"]),
        "appended": append,
        "headings": len(headings),
        "modules": modules,
        # 586 个标题的实测：一批只规划 60 个，所以必须告诉界面"还有多少"，
        # 否则用户以为文档就这么大（这正是第一版的 bug）。
        "planned_until": min(len(headings), specgen.MAX_SECTIONS),
        "has_more": len(headings) > specgen.MAX_SECTIONS,
        "usage": planned.usage or None,
        "error": planned.error or None,
        "degraded": degraded,
    }


@router.get("/projects/{pid}/spec/outline")
async def spec_outline(
    pid: int,
    request: Request,
    plan: bool = True,
    start: int = 0,
    chunk: int = 0,
    refresh: bool = False,
) -> dict:
    """列出文档可以按哪些模块/章节分批生成用例。

    ## 为什么要分批规划（2026-10-09 真机实测纠正）

    实测那份《培训资源管理》合并需规有 **586 个章节标题**。第一版实现直接把
    `headings[:60]` 送进规划，于是 **526 个标题（90% 的文档）被静默丢掉** ——
    用户看到"2 个模块"还以为文档就这么大。这类"看起来正常、产出少一大截、
    没有任何提示"的行为比报错糟糕得多。

    现在改成**按文档顺序分块规划**：`start` 是本批的起点下标，`chunk` 是本批大小
    （0 = 用默认值）。响应里的 `total / planned_until / has_more` 让界面能如实显示
    "已规划 60 / 586，还有 526 个标题没规划"，并提供"继续规划下一批"。

    `plan=false` 时只返回按原文顺序去重的标题清单，不调模型（零 token）。
    """
    await _project_access(request, pid, "viewer")
    from app import specgen

    async with db_session() as s:
        await _get_project_or_404(s, pid)

    headings = await specgen.list_headings(pid)
    if not headings:
        return {
            "headings": [],
            "modules": [],
            "has_knowledge": False,
            "total": 0,
            "planned_until": 0,
            "has_more": False,
            "note": "这个项目还没有需规/资料。先在「需规 / 资料」里上传文档（支持 Word/PDF/Excel/Markdown/txt）。",
        }

    if not plan:
        return {
            "headings": headings,
            "modules": specgen.group_titles_into_modules(headings),
            "has_knowledge": True,
            "planned": False,
            "total": len(headings),
            "planned_until": len(headings),
            "has_more": False,
        }

    size = chunk if chunk and chunk > 0 else specgen.MAX_SECTIONS
    lo = max(0, min(int(start), len(headings)))
    hi = min(lo + size, len(headings))

    # ★ 走带缓存的那条：同一批连续调用会得到**同一个分批结果**，并且不再花 token。
    # 为什么必须这样：实测同一份文档连调四次，模型给出 2/16/18/13 个模块 ——
    # 用户刚看到"16 个可测模块"，刷新一下变成"2 个、0 个可测"，只能认为功能坏了；
    # 而且每次刷新都白烧一次规划调用（用户要求降消耗）。
    # `refresh=true` 时绕过缓存 —— "重新分批"必须是用户主动的动作，不是刷新的副作用。
    if refresh:
        specgen.clear_plan_cache(pid)
    # ★ 只在这一批的窗口内重排，把"最可能有需求的"排在前面（见 specgen.rank_window）。
    # 全局顺序不动 —— planned_until 的推进语义依赖窗口是按序切的。
    res = await specgen.plan_modules_cached(
        pid,
        specgen.rank_window(headings, lo, hi, pid),
        lo,
        hi,
        await specgen.document_intro(pid),
    )
    # ★ 这一批**失败**时不能再报 has_more=true（2026-10-09 实测）。
    #
    # 实测：一份 586 个标题的文档，第 3 批开始网关持续返回 500（do_request_failed），
    # 而接口仍然回 has_more=true —— 界面于是给用户一个"规划下一批"按钮，
    # 点一次失败一次，而且**永远推不动**。用户能做的只有反复点。
    #
    # 失败时的正确表达是"这批没成功，你可以重试"，而不是"后面还有很多"。
    # 所以失败时把 has_more 置 false 并带上 error，让界面停在错误上。
    failed = bool(res.error) and not res.modules
    return {
        # headings 仍然返回**全部**标题：界面要能显示"还有多少没规划"，
        # 而且用户可能想直接看清单（这一步不花钱）。
        "headings": headings,
        "modules": res.modules,
        "has_knowledge": True,
        "planned": True,
        "degraded": res.degraded,
        "cached": res.cached,
        "usage": res.usage or None,
        "error": res.error or None,
        # 进度三件套见 spec_outline_progress 的注释（失败时不能报 has_more）。
        **spec_outline_progress(total=len(headings), lo=lo, hi=hi, failed=failed),
    }


@router.post("/projects/{pid}/spec/draft")
async def spec_draft(pid: int, request: Request, body: dict) -> dict:
    """针对一个模块生成一批**草稿用例**（不落库，先给用户看）。

    为什么不直接落库：LLM 生成的用例不可能全对，而回归集里混进一条永远失败的
    假用例，代价是每次运行都红一次、且没人能证明它错。所以生成与入库分成两步，
    中间必须有人过一眼（见 `spec/accept`）。

    请求体：{"module": "模块名", "titles": ["逐字标题", ...]}
    """
    await _project_access(request, pid, "editor")
    from app import specgen

    module = str(body.get("module") or "").strip()
    titles = [str(t) for t in (body.get("titles") or []) if str(t).strip()]
    if not titles:
        raise HTTPException(400, "titles 不能为空 —— 需要知道生成哪一节")

    async with db_session() as s:
        await _get_project_or_404(s, pid)

    text = await specgen.section_text(pid, titles)
    if not text:
        raise HTTPException(
            404,
            "这些标题在知识库里找不到正文（可能文档被重新上传过）。请重新加载章节列表。",
        )

    # ★ 把"从页面流量里学到的后端接口"拼进生成上下文（2026-10-09）。
    #
    # 为什么这一步很关键：只知道界面的话，模型写断言只能写"点了菜单应该看到列表"，
    # 而它**不知道数据从哪来** —— 菜单往往是后端给的，列表数据其实是某个
    # `/api/xxx/list` 的返回。带上接口清单之后，模型就能写出"核对接口返回的
    # 菜单项与页面侧边栏一致"这类真正有判别力的用例，而不是只描述像素。
    #
    # 没有接口时（还没跑过用例）返回空串，提示词与以前完全一样。
    ctx = await specgen.document_intro(pid, 800)
    eps = await _observed_endpoints(pid)
    api_note = _api_context_note(eps)
    if api_note:
        ctx = f"{ctx}\n\n{api_note}" if ctx else api_note

    res = await specgen.generate_for_section(
        module or titles[0],
        text,
        extra_context=ctx,
    )
    return {
        "module": module or titles[0],
        "titles": titles,
        "cases": [c.as_dict() for c in res.cases],
        "dropped": res.dropped,
        "count": len(res.cases),
        "usage": res.usage or None,
        "source": _spec_source(module or titles[0]),
        "error": res.error or None,
        # 0 条时告诉用户**为什么**。见 specgen.generate_for_section 末尾的说明：
        # 不解释的话，用户看到"0 条"只能反复点同一个按钮。
        "note": res.note or None,
    }


def _spec_source(module: str) -> str:
    """草稿用例的来源标记，落进 TestCase.source。

    格式短、可读、可筛选："spec:<模块名>"。模块名就是章节名，
    所以事后能回答"这条用例是从哪一节生成的"。
    """
    return f"spec:{module}"[:300]


@router.post("/projects/{pid}/testcases/status")
async def bulk_set_case_status(pid: int, request: Request, body: dict) -> dict:
    """批量改用例状态（2026-10-09）。

    为什么必须批量：从需规生成一次常见的量级是几十到两百条，全部先落 `draft`。
    逐条 PATCH 意味着"审阅 120 条草稿 = 120 次请求"，而且界面要么做乐观更新、
    要么在每一条上等一个往返 —— 审阅体验会直接决定用户愿不愿意用这个功能。

    请求体：{"ids":[...], "status":"draft"|"active"|"deprecated"|"blocked",
             "reason":"..."}

    只接受白名单取值：这是**批量**写，写错一个值就是几十条用例一起错。

    ★ `blocked` 是用户明确要求的那一档（"有些用例 agent 可能执行不了"）。
    它和 `deprecated` 的区别见 app/schemas.py::CaseStatus —— 一句话：
    deprecated 是"这条不要了"，blocked 是"这条有效，但现在跑不了"。
    转 blocked 时 `reason` 必填：只标状态不写原因，过一个月没人知道它为什么被挡住。
    """
    await _project_access(request, pid, "editor")
    from app.schemas import CaseStatus  # noqa: F401  (keeps the allowed set in one place)

    ids = [int(i) for i in (body.get("ids") or []) if str(i).strip()]
    status = str(body.get("status") or "").strip()
    reason = str(body.get("reason") or "").strip()[:2000]
    allowed = {"draft", "active", "deprecated", "blocked"}
    if not ids:
        raise HTTPException(400, "ids 不能为空")
    if status not in allowed:
        raise HTTPException(400, f"status 必须是 {sorted(allowed)} 之一")
    # blocked 必须带原因：这个状态的**全部价值**就是那个原因。
    # 允许空原因会让它退化成"一个没有解释的红点"。
    if status == "blocked" and not reason:
        raise HTTPException(
            400,
            "标为 blocked 时必须说明原因（reason）—— "
            "否则下一个人只能重新试一遍才知道它为什么跑不了。",
        )

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        rows = (
            (await s.execute(select(TestCase).where(TestCase.id.in_(ids)))).scalars().all()
        )
        # 归属校验：ids 是调用方给的，必须逐条确认都在这个项目里，
        # 否则一个项目可以改另一个项目的用例状态。
        foreign = [r.id for r in rows if r.project_id != pid]
        if foreign:
            raise HTTPException(403, f"用例 {foreign[:5]} 不属于项目 {pid}")
        changed = 0
        for r in rows:
            if r.status != status or (status == "blocked" and r.blocked_reason != reason):
                r.status = status
                # 启用的同时把 enabled 归位：草稿期用户可能关过它，
                # 现在明确要"启用"，两个字段不一致会让"运行全部启用用例"的行为难以解释。
                if status == "active":
                    r.enabled = True
                if status == "blocked":
                    # 被挡住的用例必须从回归集里出来，否则"挡住"只是个标签，
                    # 下一次全量运行照样会跑到它、照样失败一次。
                    r.enabled = False
                    r.blocked_reason = reason
                elif status == "active":
                    # 解除阻挡时清掉旧原因：一条已经启用的用例还挂着
                    # "为什么跑不了"，后面看报告的人会被这条过期信息误导。
                    r.blocked_reason = ""
                changed += 1
    return {
        "requested": len(ids),
        "found": len(rows),
        "changed": changed,
        "status": status,
        "reason": reason if status == "blocked" else "",
    }


@router.post("/projects/{pid}/spec/accept")
async def spec_accept(pid: int, request: Request, body: dict) -> dict:
    """把审阅过的草稿**落库**为用例（默认 status=draft，仍不进回归集）。

    请求体：{"cases":[{name,prompt,expected,module,type,priority,precondition}],
             "activate": false}

    ★ `activate=false`（默认）时落库的用例是 `status=draft`，**不会**被
    "运行全部启用用例"选中 —— 这正是"先审后启用"的机制保障。
    只有用户显式传 `activate=true` 才直接建成 active。
    """
    await _project_access(request, pid, "editor")
    from app import specgen

    raw = body.get("cases")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "cases 不能为空")

    # 走同一个解析器：界面回传的是模型产物经过用户编辑的结果，
    # 用户也可能把 expected 清空 —— 那就必须在入库前再拦一次。
    cases, dropped = specgen.parse_cases(raw, module_hint=str(body.get("module") or ""))
    if not cases:
        raise HTTPException(400, f"没有可入库的用例：{'; '.join(dropped) or '全部不完整'}")

    activate = bool(body.get("activate"))
    module = str(body.get("module") or "").strip()
    source = str(body.get("source") or _spec_source(module))[:300]

    async with db_session() as s:
        await _get_project_or_404(s, pid)
        created: list[dict] = []
        for c in cases:
            key = await _next_case_key(s, pid)
            row = TestCase(
                project_id=pid,
                case_key=key,
                name=c.name,
                prompt=c.prompt,
                expected=c.expected,
                module=c.module or module,
                type=c.type,
                priority=c.priority,
                preconditions=c.precondition,
                # ★ 草稿是默认。回归集只收用户明确启用的东西。
                status="active" if activate else "draft",
                enabled=True,
                source=source,
            )
            s.add(row)
            await s.flush()
            created.append({"id": row.id, "case_key": row.case_key, "name": row.name})
    return {
        "created": created,
        "count": len(created),
        "status": "active" if activate else "draft",
        "dropped": dropped,
    }


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
async def list_cases(pid: int, request: Request, status: str = "") -> list[dict]:
    """项目下的用例。

    `status` 可选过滤（2026-10-09 加）：从需规生成的用例默认落成 `draft`，
    审阅页需要"只看草稿"这一个视图。此前列表没有服务端过滤，前端筛，
    草稿一多（生成 200 条很常见）每次都要把全部用例和它们的最近结果一起拉下来。
    """
    await _project_access(request, pid, "viewer")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        q = select(TestCase).where(TestCase.project_id == pid)
        if status:
            q = q.where(TestCase.status == status)
        rows = (await s.execute(q.order_by(TestCase.id))).scalars().all()
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


async def _reserved_case_keys(s, pid: int) -> list[str]:
    """项目里已有的、形如 TC-<数字> 的编号。"""
    rows = (
        await s.execute(
            select(TestCase.case_key).where(
                TestCase.project_id == pid, TestCase.case_key.is_not(None)
            )
        )
    ).scalars().all()
    return [k for k in rows if k]


def _key_number(key: str | None) -> int | None:
    """`TC-007` -> 7；不是这个形状（如导入的自由文本 "REQ-12"）-> None。

    只认 TC-<数字>：Excel 导入允许自由文本编号，对它做 int() 会抛异常把整批导入打断。
    """
    m = re.fullmatch(r"TC-(\d+)", (key or "").strip())
    return int(m.group(1)) if m else None


async def _next_case_key(s, pid: int) -> str:
    """Readable per-project id like TC-001，来自项目的**永久计数器**。

    历史（两个已修的缺陷，别再退回去）：

    1. `COUNT(*)` 当序号：删掉一条后总数少 1，下一条又拿到刚删掉的号 ——
       两条不同用例撞同一个编号，报告和缺陷单里无法区分。
    2. 改成"现有编号的最大值 +1"：修好了中间挖洞（删 TC-003 不会重发），
       但**删掉最大编号仍会重发**：TC-001..TC-005 里删掉 TC-005，下一条又是 TC-005。
       根因是把"下一个号"当成能从当前数据推导出来的东西，而它其实是
       "发行过就不回收"的事实 —— 必须自己存（见 models.Project.case_seq）。

    所以这里读写的是 `project.case_seq`，只增不减，删用例不动它。
    老库该列是后加的（值为 0 = 尚未回填），第一次发号时从现有最大编号回填。

    编号格式保持 `TC-NNN`：存量 1203 条用例都在用，改格式等于让所有历史报告对不上号。
    跨项目只靠 `(project_id, case_key)` 唯一约束区分 —— 每个项目都从 TC-001 开始是设计如此。
    """
    proj = await s.get(Project, pid)
    if proj is None:
        raise HTTPException(404, "project not found")
    await _backfill_case_seq(s, proj)
    proj.case_seq += 1
    # flush 不能省：Excel 导入是"一次会话里连着建多条"，号必须在同一批里递增，
    # 否则这一批会全部拿到同一个号，然后一起撞唯一约束。
    await s.flush()
    return f"TC-{proj.case_seq:03d}"


async def _backfill_case_seq(s, proj) -> None:
    """把 `case_seq` 从 0（= 尚未回填）补成"现有用例的最大编号"。

    只在为 0 时动手：计数器一旦发过号，它就是这个项目的真相，
    再拿现有数据的最大值去覆盖它，等于把"删掉最大号"那个缺陷又请回来。
    """
    if proj.case_seq:
        return
    top = 0
    for k in await _reserved_case_keys(s, proj.id):
        n = _key_number(k)
        if n is not None:
            top = max(top, n)
    proj.case_seq = top


async def _reserve_case_key(s, pid: int, key: str | None) -> None:
    """把**手工指定**的编号计入计数器，避免它之后被自动编号撞上。

    为什么必须有：用户可以导入或编辑出 `TC-050`，而计数器当时只到 3。
    不回填的话自动编号会一路发到 TC-050 撞上唯一约束（或更糟，撞号）。
    只在比当前计数大时才抬高 —— 计数器只增不减。
    """
    n = _key_number(key)
    if n is None:
        return
    proj = await s.get(Project, pid)
    if proj is None:
        return
    await _backfill_case_seq(s, proj)
    if n > proj.case_seq:
        proj.case_seq = n
        await s.flush()


@router.post("/projects/{pid}/testcases")
async def create_case(pid: int, body: TestCaseIn, request: Request) -> dict:
    await _project_access(request, pid, "editor")
    async with db_session() as s:
        await _get_project_or_404(s, pid)
        data = body.model_dump()
        if not data.get("case_key"):
            data["case_key"] = await _next_case_key(s, pid)
        else:
            # 手工指定的号也要计入计数器，否则自动编号以后会一路发到它并撞车。
            await _reserve_case_key(s, pid, data["case_key"])
        # 2026-10-04 多角色：归一化后再落库，并让 role 始终镜像 roles[0]。
        # 这样无论读哪一列、无论请求里只传了 role 还是只传了 roles，
        # 存下来的数据都自洽 —— 执行层也只需要认 roles 一个来源。
        _rs = _roles_of(body.role, body.roles)
        data["roles"] = _rs
        data["role"] = _rs[0] if _rs else None
        c = TestCase(project_id=pid, **data)
        s.add(c)
        try:
            await s.flush()
        except IntegrityError as exc:
            # (project_id, case_key) 复合唯一约束。以前这里直接 500，用户看到的是
            # "服务器错误"，而真实原因是"这个编号在本项目里已经有了" —— 一个能自己
            # 修的问题被报成了系统故障。约束是数据库强制的（不能只靠约定），所以
            # 必须把它翻成一句人话。跨项目重名不算冲突：每个项目都从 TC-001 开始。
            await s.rollback()
            raise HTTPException(
                409, f"编号 {data.get('case_key')} 在本项目里已经存在，请换一个。"
            ) from exc
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
        # blocked 的规则统一在这里收口（2026-10-09）：
        #
        #   * 转成 blocked 必须带原因 —— 这个状态的**全部价值**就是那条原因，
        #     允许空原因会让它退化成"一个没有解释的红点"，下一个人只能重新试一遍。
        #   * 离开 blocked 时清掉原因 —— 否则一条已经启用的用例还挂着"为什么跑不了"，
        #     后面看报告的人会被这条过期信息误导。
        #
        # 放在 setattr 之前：校验失败时不应该有任何字段被改到。
        if "status" in data:
            _new_status = data["status"]
            _incoming_reason = str(data.get("blocked_reason") or "").strip()
            if _new_status == "blocked" and not (_incoming_reason or (c.blocked_reason or "").strip()):
                raise HTTPException(
                    400,
                    "标为 blocked 时必须说明原因（blocked_reason）—— "
                    "否则下一个人只能重新试一遍才知道它为什么跑不了。",
                )
            if _new_status == "blocked":
                data["blocked_reason"] = _incoming_reason or (c.blocked_reason or "")
                # 被挡住的用例必须从回归集里出来，否则"挡住"只是个标签，
                # 下一次全量运行照样会跑到它、照样失败一次。
                data["enabled"] = False
            else:
                data["blocked_reason"] = ""
        # 改编号也要计入计数器（理由同 create_case）：手工把某条改成 TC-050 之后，
        # 自动编号不能还傻乎乎地往 50 上撞。
        if data.get("case_key"):
            await _reserve_case_key(s, c.project_id, data["case_key"])
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
            else:
                # 导入表里自带的编号同样要计入计数器（同 create_case 的理由）。
                await _reserve_case_key(s, pid, payload["case_key"])
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
        # ★ "运行全部启用用例" 的取数口径：`enabled=True` **且** `status=active`。
        #
        # 2026-10-09 修：这里原来只看 `enabled`。而 `status` 与 `enabled` 是两个字段，
        # 从需规生成的草稿落库时是 `status="draft", enabled=True` ——
        # 于是"草稿不会被跑到"这个承诺**在 run-all 这条路径上是假的**：
        # 用户点「运行全部」会把所有草稿一起跑掉，而且看起来完全正常。
        # `tests/test_specgen_api.py` 里那条边界用例是照 `enabled and status=="active"`
        # 的口径写的，所以它当时是绿的 —— 口径写对了，实现没跟上。
        #
        # 为什么用两个条件而不是只留 status：`enabled` 是用户手动勾的那个开关
        # （界面上勾选框直接改它），单独把某条从回归集里摘掉时不该被迫改 status。
        # 两个条件一起表达的是"启用 且 已审阅通过"，正是用户期望的语义。
        q = select(TestCase).where(
            TestCase.project_id == pid,
            TestCase.enabled == True,  # noqa: E712 — SQLAlchemy 需要 == True 才能生成 IS
            TestCase.status == "active",
        )
        if body.case_ids:
            # 显式点名要跑的用例不受这个口径限制：用户勾了草稿再点运行，
            # 意思就是"我现在就要试这一条" —— 那正是审阅草稿的方式。
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


@router.get("/runs/{rid}/perf")
async def run_perf_report(rid: int, request: Request) -> dict:
    """这一轮的性能异常分析（2026-10-09）。

    存在的理由：判定器只回答"结果对不对"，看不到"这条用例本不该花这么久"。
    实测真库里就有 3 条用例**单步耗时 271s**（同轮中位数 11.4s，23 倍）——
    它们的结果可能是对的，但那本身就是要报出去的信号。

    纯计算、不调模型：性能结论不该再花一次 LLM 往返，而且确定性规则比
    "让模型看数字下判断"更稳定、可复现、可单测。见 app/perf_analysis.py。
    """
    from app.perf_analysis import analyse_run

    async with db_session() as s:
        run = await s.get(Run, rid)
        if run is None:
            raise HTTPException(404, "run not found")
        await _project_access(request, run.project_id, "viewer")
        rows = (
            await s.execute(
                select(RunResult.case_id, RunResult.timing).where(RunResult.run_id == rid)
            )
        ).all()
    return analyse_run([{"case_id": c, "timing": t} for c, t in rows]).as_dict()


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


# ---- 应用更新（2026-10-17）--------------------------------------------------
# 检查：任何模式都能用（网页版也想知道自己落后没）。
# 安装：**只有桌面版** —— 网页版跑在服务器上，既读不到用户磁盘也不该往用户机器装东西。
# 安全边界全在 app/updater.py（下载白名单 + 只看本仓库 Release）。


@router.get("/app/update")
async def app_update(force: bool = False) -> dict:
    """查最新版本。`force=true` 跳过 10 分钟缓存（用户点了"再检查一次"）。"""
    from app import updater

    return await updater.check(force=force)


@router.post("/app/update/install")
async def app_update_install(body: dict) -> dict:
    """下载更新包并静默安装。**仅桌面版**。"""
    import tempfile
    from pathlib import Path

    from app import updater

    if not updater.install_supported():
        raise HTTPException(400, "只有桌面版能一键更新：网页版跑在服务器上，改不了本机的程序")

    url = str((body or {}).get("url") or "").strip()
    if not url:
        raise HTTPException(400, "缺少更新包地址")
    try:
        updater.validate_download_url(url)  # 白名单：放行 = 允许在用户机器上执行它
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        path = await updater.download_installer(url, Path(tempfile.gettempdir()) / "potato-update")
    except Exception as exc:  # noqa: BLE001 — 下载失败要说清原因，别让用户以为点空了
        raise HTTPException(502, f"下载更新包失败：{exc}") from exc

    updater.launch_installer(path)  # 延迟 6 秒：留给应用退出
    return {
        "started": True,
        "installer": str(path),
        "message": "更新包已下载，应用即将退出并自动安装",
    }


# ---- 文件存放位置（2026-10-19 用户要求）--------------------------------------
# 视频、截图、浏览器 profile 默认落在程序目录旁边。用户可以把它们放去别的盘
#（C 盘紧张、或想把录像集中到数据盘），设完**重启生效**。
#
# ★ 为什么用 `storage.json` 而不是环境变量/注册表：这是用户**在界面里**选的设置，
#   要在下一次启动就生效，且不能依赖 .env（桌面版可能没有 .env）。
#   `desktop.py` 在 import app 之前读它 —— 早于 `app/config.py` 解析相对路径。


def _app_root() -> Path:
    """程序目录：冻结版是 exe 所在目录，源码版是仓库根。"""
    import sys as _sys

    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _move_storage(root: Path, dest: Path) -> None:
    """把现有的 artifacts / profiles 搬到新位置。同盘是秒级改名，跨盘是复制。"""
    import logging
    import shutil

    # api.py 没有模块级 logger（见 `_probe_headers`），就地取一个。
    log = logging.getLogger("potato-test.api")

    for name in ("artifacts", "profiles"):
        src = root / name
        if not src.is_dir():
            continue
        target = dest / name
        try:
            if target.exists():
                for child in src.iterdir():  # 目标已存在：逐项搬，避免整体覆盖
                    shutil.move(str(child), str(target / child.name))
                src.rmdir()
            else:
                shutil.move(str(src), str(target))
            log.info("已把 %s 搬到 %s", name, target)
        except Exception as exc:  # noqa: BLE001 — 搬失败不能影响已经生效的设置
            log.warning("搬 %s 失败（%s: %s）—— 旧文件仍留在原处", name, type(exc).__name__, exc)


@router.get("/app/storage")
async def get_app_storage() -> dict:
    """当前的存放位置 + 程序默认位置（界面据此显示"恢复默认"）。"""
    import json as _json

    root = _app_root()
    chosen = ""
    f = root / "storage.json"
    if f.is_file():
        try:
            chosen = str(_json.loads(f.read_text(encoding="utf-8")).get("storage_dir") or "")
        except Exception:  # noqa: BLE001
            chosen = ""
    s = get_settings()
    return {
        "storage_dir": chosen,
        "artifact_dir": s.artifact_dir,
        "profile_dir": s.profile_dir,
        "default_dir": str(root),
        "app_root": str(root),
    }


@router.put("/app/storage")
async def set_app_storage(body: dict, background: BackgroundTasks) -> dict:
    """设置存放位置。传空 dir 表示恢复默认（放在程序目录）。重启生效。"""
    import json as _json

    root = _app_root()
    f = root / "storage.json"
    raw = str((body or {}).get("dir") or "").strip()

    if not raw:
        if f.is_file():
            f.unlink()
        return {"ok": True, "needs_restart": True, "storage_dir": "", "message": "已恢复默认位置，重启应用后生效"}

    dest = Path(raw).expanduser()
    if not dest.is_absolute():
        raise HTTPException(400, "请填绝对路径，例如 D:\\PotatoTestData")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        probe = dest / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"这个目录不可写：{type(exc).__name__}") from exc

    f.write_text(_json.dumps({"storage_dir": str(dest)}, ensure_ascii=False, indent=2), encoding="utf-8")
    if bool((body or {}).get("move_existing")):
        background.add_task(_move_storage, root, dest)
    return {
        "ok": True,
        "needs_restart": True,
        "storage_dir": str(dest),
        "message": "已保存，重启应用后生效" + ("（旧文件正在后台搬过去）" if (body or {}).get("move_existing") else ""),
    }
