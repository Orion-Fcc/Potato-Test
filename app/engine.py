"""Engine loop: enqueue a run's cases, drain them with N concurrent workers, aggregate.

`drain` and `aggregate` are pure (stdlib only) so they are unit-testable without the DB
or browser stack. `run_suite` is the DB-backed wrapper.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any, NamedTuple, TypeVar
from urllib.parse import urljoin

from app.config import get_settings

log = logging.getLogger("potato-test.engine")

# a run in one of these is over — no case of it should still be waiting to start
_TERMINAL = ("completed", "failed", "cancelled")

T = TypeVar("T")
R = TypeVar("R")


def resolve_start_url(case_start_url: str | None, base_url: str | None) -> str | None:
    """Where the browser opens. A case's start_url is normally a path like
    /visitor/master/park so the same case can run against dev/test/prod — join it onto the
    environment's base_url. Left bare it reached the browser as-is and Chromium read it as
    http://localhost/visitor/master/park → ERR_CONNECTION_REFUSED, then the agent wandered
    off to a search engine looking for the app."""
    if not case_start_url:
        return base_url
    if case_start_url.startswith(("http://", "https://")) or not base_url:
        return case_start_url
    return urljoin(base_url, case_start_url)


def _effective_prompt(c: Any) -> str:
    """The task the agent actually runs: the NL prompt, with preconditions prepended
    and test data appended when present. Structured `steps` stay documentation-only.

    2026-10-04 前置条件增强（用户要求）：
      "有些用例前置条件可能不存在，如果该系统能自己实现，我希望他自己实现。"

    原来只是把 preconditions 原文拼进去，模型看见"前置条件：已有一条待审核的申请单"
    却发现列表是空的，就会直接报失败 —— 而这往往不是缺陷，只是**没准备数据**，
    属于假失败。现在明确授权它自己按真人操作把前置数据造出来再继续。

    为什么要划那三条红线：一旦允许模型"自己造数据"，就有个明显的偷懒路径 ——
    点两下没找到，就声称前置已满足，然后照常判通过。那会把假失败换成**假通过**，
    更糟。所以明确要求：造不出来要如实说，且必须验证造出来的东西真的存在。

    Structured `steps` stay documentation-only.
    """
    parts: list[str] = []
    if (c.preconditions or "").strip():
        pre = c.preconditions.strip()
        parts.append(f"Preconditions: {pre}")
        parts.append(
            "ABOUT THE PRECONDITIONS ABOVE: the data or state they describe may not exist "
            "yet. If it does not, CREATE IT YOURSELF first — do the same operations a real "
            "user would do to set it up (create the record, submit the order, assign the "
            "role, etc.), then continue with the task. Do not fail the case merely because "
            "the precondition was not pre-arranged.\n"
            "Three limits on this:\n"
            "  a) Only create what the precondition actually asks for. Do not invent extra "
            "data, and never modify or delete records you were not asked to touch.\n"
            "  b) After creating it, CONFIRM it exists (see it in the list / see the saved "
            "detail page). A precondition you believe you created but never verified does "
            "not count as satisfied.\n"
            "  c) If you genuinely cannot create it — no permission, a required upstream "
            "record is missing, the form rejects your input — say exactly that, name the "
            "step that blocked you, and report the case as blocked/unverified. Do NOT "
            "pretend the precondition was met and then judge the rest of the case on top "
            "of that."
        )
    parts.append(c.prompt)
    if (c.test_data or "").strip():
        parts.append(f"Test data: {c.test_data.strip()}")
    return "\n\n".join(parts)


# 2026-10-04 前置条件里的用例依赖解析（用户要求）。
#
# 用户原话："我希望前置条件里面可能存在一些其他用例，可以将其放在前面，以节省时间。"
#
# 场景：用例 TC-020 的前置条件写着"已存在一条 TC-008 创建的申请单"。
# 跑 TC-020 之前先把 TC-008 跑掉，既省掉重复准备数据的步骤，也避免
# "前置不存在"导致的假失败。
#
# 设计取舍：
#   - 只在同一个 project 内解析。不同项目的用例编号可以重名（都从 TC-001 开始），
#     拿项目外的编号去指代依赖会让项目重新耦合 —— 这正是用户要拆开的东西。
#     所以只认本项目的编号，找不到就忽略（编号可能只是写在文字里的举例）。
#   - 拓扑排序 + 环保护。A 依赖 B、B 又依赖 A 时不能死循环，检测到环就
#     按原顺序保留并各跑一次 —— 宁可多跑一次，不能卡住整轮。
#   - 只追加，不删除。用户选中的用例集合是最终要跑的内容，
#     依赖只会在它前面插入，不会把谁挤掉。
_CASE_KEY_RE = re.compile(r"\bTC-\d+\b", re.IGNORECASE)


async def resolve_case_dependencies(session, project_id: int, case_ids: list[int]) -> list[int]:
    """Expand `case_ids` with the cases their preconditions reference, deps first.

    Only resolves keys that exist in the SAME project (see module note above).
    Returns a new ordered list; the caller's original cases always appear, in their
    original relative order, after their dependencies. Cycle-safe.
    """
    from app.models import TestCase
    # 与文件内其它地方一致：函数内导入，避免把 SQLAlchemy 变成模块级硬依赖
    from sqlalchemy import select

    wanted = list(dict.fromkeys(case_ids))  # 去重且保序
    if not wanted:
        return []

    rows = (
        (
            await session.execute(
                select(TestCase.id, TestCase.case_key, TestCase.preconditions).where(
                    TestCase.project_id == project_id
                )
            )
        )
        .all()
    )
    key_to_id = {
        (k or "").strip().upper(): i for i, k, _ in rows if (k or "").strip()
    }
    pre_of = {i: (p or "") for i, _k, p in rows}

    # 收集每个用例的直接依赖（同项目、且不是自己）
    deps: dict[int, list[int]] = {}
    for cid in wanted:
        found: list[int] = []
        for k in _CASE_KEY_RE.findall(pre_of.get(cid, "")):
            dep = key_to_id.get(k.upper())
            if dep is not None and dep != cid and dep not in found:
                found.append(dep)
        deps[cid] = found

    # 依赖的依赖也要算进来：TC-030 依赖 TC-020，而 TC-020 又依赖 TC-008，
    # 那 TC-008 也必须先跑。用 BFS 把闭包收全。
    closure: list[int] = []
    seen: set[int] = set()
    stack = list(wanted)
    while stack:
        cur = stack.pop()
        for d in deps.get(cur, []):
            if d not in seen:
                seen.add(d)
                closure.append(d)
                # 被引入的依赖自己也可能有依赖，要继续展开它的前置条件
                if d not in deps and d in pre_of:
                    ds: list[int] = []
                    for k in _CASE_KEY_RE.findall(pre_of.get(d, "")):
                        dd = key_to_id.get(k.upper())
                        if dd is not None and dd != d and dd not in ds:
                            ds.append(dd)
                    deps[d] = ds
                stack.append(d)

    # 拓扑排序：按依赖深度分层，同层保持原顺序
    ordered: list[int] = []
    placed: set[int] = set()
    visiting: set[int] = set()

    def _place(cid: int) -> None:
        if cid in placed or cid in visiting:
            # visiting 命中 = 检测到环。直接返回，让调用方按已有顺序跑完，
            # 不抛异常 —— 环是用例作者写错了，不该让整轮跑不起来。
            return
        visiting.add(cid)
        for d in deps.get(cid, []):
            _place(d)
        visiting.discard(cid)
        if cid not in placed:
            placed.add(cid)
            ordered.append(cid)

    for cid in closure:
        _place(cid)
    for cid in wanted:
        _place(cid)

    return ordered


async def drain(
    items: list[T],
    handler: Callable[[T], Awaitable[R]],
    concurrency: int,
    slot: asyncio.Semaphore | None = None,
) -> list[R]:
    """Run handler over every item with `concurrency` workers. Failure-isolated:
    a handler that raises yields an Exception in the results, never aborts the run.

    `slot`: a semaphore acquired around each handler call. Pass the SAME instance to
    concurrent drains to cap total in-flight work across all runs (the global budget).
    A worker blocks on the slot *before* running the handler, so per-item timeouts
    inside the handler never count queue-wait time."""
    queue: asyncio.Queue[T] = asyncio.Queue()
    for it in items:
        queue.put_nowait(it)
    results: list[R] = []
    lock = asyncio.Lock()

    async def run_one(item: T) -> Any:
        try:
            return await handler(item)
        except Exception as exc:
            return exc

    async def worker() -> None:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if slot is not None:
                async with slot:
                    r: Any = await run_one(item)
            else:
                r = await run_one(item)
            async with lock:
                results.append(r)

    await asyncio.gather(*[worker() for _ in range(max(1, concurrency))])
    return results


_GLOBAL_SLOT: asyncio.Semaphore | None = None


def global_slot() -> asyncio.Semaphore:
    """Process-wide browser budget, created lazily inside the running loop. All runs
    share this one instance so simultaneous runs can't exceed max_global_concurrency."""
    global _GLOBAL_SLOT
    if _GLOBAL_SLOT is None:
        _GLOBAL_SLOT = asyncio.Semaphore(get_settings().max_global_concurrency)
    return _GLOBAL_SLOT


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * len(ordered)))
    return ordered[idx]


def aggregate(statuses: list[str], latencies: list[int], flaky: int = 0) -> dict:
    total = len(statuses)
    passed = statuses.count("passed")
    return {
        "total": total,
        "passed": passed,
        "failed": statuses.count("failed"),
        "error": statuses.count("error"),
        "flaky": flaky,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "latency_p50_ms": int(median(latencies)) if latencies else 0,
        "latency_p95_ms": _pct(latencies, 0.95),
    }


def resolve_attempts(statuses: list[str]) -> tuple[str, bool]:
    """Given each attempt's status in order, return (final_status, flaky).
    flaky == passed only after one or more retries."""
    final = statuses[-1]
    return final, (final == "passed" and len(statuses) > 1)


def should_retry(result, retries: int, attempts: int) -> bool:
    """这一条用例要不要再跑一次。抽成函数是为了让测试能直接验证同一份逻辑
    —— 之前把判断写在循环里，测试只能复制一份，两边必然漂移（实际已漂移过一格）。

    规则：
      * 超时 → 不重试。重试改不了结局，还会再烧一个完整预算。
      * infra error → 重试（这是它一直以来的行为）。
      * 判定"失败"且 evidence_gap（判定器认为 agent 根本没走到）→ 重试。
        实测近 121 条失败里 58% 属这类，只有 14% 是真实结果不符。
      * 其余（真实缺陷）→ 不重试。宁可少重试，也不能把真实缺陷重跑成"偶发"。

    **预算必须覆盖所有形式的重跑**（包括会话死亡后的自愈重试），否则总执行次数会
    变成 retries+2 而不是配置里承诺的 retries+1 —— 最坏情况多烧一个完整预算。
    调用方负责在每条重跑路径上自增计数。
    """
    if retries <= 0 or attempts >= retries:
        return False
    if getattr(result, "timed_out", False):
        return False
    if result.status == "error":
        return True
    return bool(getattr(result, "evidence_gap", False)) and result.status == "failed"


class DefaultLogin(NamedTuple):
    """What a role-less case logs in with. `pw_cred_id` is the password account's row —
    needed to capture a session bundle from it once and cache it there, the same way a
    role account does."""

    login_state: str | None
    username: str | None
    password: str | None
    state_cred_id: int | None
    pw_cred_id: int | None


async def _resolve_login(session, run, env_id: int | None = None) -> DefaultLogin:
    """A run's default login (no case role): active storage_state credential (decrypted)
    > legacy project column; plus a robot password account for per-run prompt-login (apps
    that keep auth in sessionStorage). Filtered to the run's environment (or
    env-agnostic accounts). Secrets resolved here, never sent through the broker."""
    from sqlalchemy import or_, select

    from app.crypto import decrypt, secret_configured
    from app.models import Credential, Project

    project = await session.get(Project, run.project_id)
    login_state = project.login_state if project else None
    login_user: str | None = None
    login_pass: str | None = None
    state_cred_id: int | None = None
    pw_cred_id: int | None = None
    env_ok = or_(Credential.environment_id == env_id, Credential.environment_id.is_(None))
    if secret_configured():
        cred = (
            (
                await session.execute(
                    select(Credential).where(
                        Credential.project_id == run.project_id,
                        Credential.is_active == True,
                        Credential.type == "storage_state",
                        env_ok,
                    )
                )
            )
            .scalars()
            .first()
        )
        if cred is not None:
            login_state = decrypt(cred.secret)
            state_cred_id = cred.id
        pw_cred = (
            (
                await session.execute(
                    select(Credential)
                    .where(
                        Credential.project_id == run.project_id,
                        Credential.type == "password",
                        env_ok,
                    )
                    .order_by(Credential.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if pw_cred is not None:
            login_user = pw_cred.username
            login_pass = decrypt(pw_cred.secret)
            pw_cred_id = pw_cred.id
    return DefaultLogin(login_state, login_user, login_pass, state_cred_id, pw_cred_id)


async def prepare_run(run_id: int) -> list[int] | None:
    """Validate the run and record total_count; return its case ids (order preserved).
    Does NOT mark the run running — the first case to start flips pending->running so a
    queued run shows as queued, not a silent 0/N. Returns None if the run isn't runnable."""
    from app.db import db_session
    from app.models import Run

    async with db_session() as s:
        run = await s.get(Run, run_id)
        if run is None or run.status not in ("pending", "running"):
            return None
        case_ids = list(run.case_ids or [])
        run.total_count = len(case_ids)
        await s.commit()
        return case_ids


async def _role_candidates(
    session, project_id: int, role: str, env_id: int | None = None
) -> list[dict]:
    """Decrypted accounts for a role in a project + environment (env-null = any env)."""
    from sqlalchemy import or_, select

    from app.crypto import decrypt, secret_configured
    from app.models import Credential

    if not secret_configured():
        return []
    rows = (
        (
            await session.execute(
                select(Credential)
                .where(
                    Credential.project_id == project_id,
                    Credential.role == role,
                    or_(Credential.environment_id == env_id, Credential.environment_id.is_(None)),
                )
                .order_by(Credential.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": c.id,
            "type": c.type,
            "username": c.username,
            "secret": decrypt(c.secret),
            "label": c.label,
        }
        for c in rows
    ]


def _case_roles(case) -> list[str]:
    """The ordered roles a case switches through (2026-10-04 multi-role).

    Mirrors app/schemas._roles_of: `roles` is the superset, `role` is the
    fallback, so all 557 existing single-role cases resolve to a one-element
    list and behave exactly as before. Duplicates are dropped because leasing
    the same account twice would deadlock the case against itself.
    """
    out: list[str] = []
    for r in list(getattr(case, "roles", None) or []) + [getattr(case, "role", None)]:
        v = (r or "").strip() if isinstance(r, str) else ""
        if v and v not in out:
            out.append(v)
    return out


# 2026-10-04 多角色：一个用例可以声明流程中依次用到哪些角色
# （例 ["applicant", "approver"] = 先以申请人提交，再切到审核人处理）。
#
# 为什么单独抽一个函数：单角色时 engine 直接 lease 一个账号、拿它的 login_state
# 就完事；多角色要为每个角色各做一遍（找账号 -> 租约 -> 取/捕获会话），
# 两边逻辑必须完全一致，否则「单角色走 A 路径、多角色走 B 路径」会出现
# 只有多角色才踩的 bug。这里把「准备一个角色的登录态」收成一步，两种情况共用。
#
# 返回的 RoleLogin 说明：
#   bundle     —— storage_state JSON，登录用；password 类账号还没捕获时为空
#   user/pass  —— 需要在页面上填账号密码（提示登录）时用
#   heal_*     —— 会话过期时用来重新捕获的凭据信息
#   lease_id   —— 已租到的账号（None 表示没租到，调用方负责报错）
RoleLogin = NamedTuple(
    "RoleLogin",
    [
        ("role", str),
        ("account", dict | None),
        ("lease_id", int | None),
        ("bundle", str | None),
        ("user", str | None),
        ("password", str | None),
        ("heal_cred_id", int | None),
        ("heal_field", str),
        ("label", str | None),
    ],
)


async def _prepare_role_login(
    session,
    project_id: int,
    role: str,
    env_id: int | None,
    base_url: str | None,
    timeout_s: int,
) -> RoleLogin:
    """Lease one account for `role` and get a usable session (bundle or password).

    A storage_state account is used as-is. A password account gets a captured
    bundle cached on its row (capture is single-flighted in _ensure_bundle), so
    switching to it later in a multi-role case reuses that instead of logging in
    again — which is the whole point of capturing up front.
    """
    from app import leasing

    candidates = await _role_candidates(session, project_id, role, env_id)
    if not candidates:
        return RoleLogin(role, None, None, None, None, None, None, "session_bundle", None)

    # wait up to the case timeout for a free account, else the caller reports it
    lease_id = await leasing.acquire(
        [c["id"] for c in candidates], ttl_s=timeout_s + 30, wait_s=timeout_s
    )
    if lease_id is None:
        return RoleLogin(role, None, None, None, None, None, None, "session_bundle", None)

    acct = next(c for c in candidates if c["id"] == lease_id)
    who = acct["label"] or acct["username"] or f"#{acct['id']}"
    label = f"{role} · {who}"

    if acct["type"] == "storage_state":
        return RoleLogin(role, acct, lease_id, acct["secret"], None, None, None, "session_bundle", label)

    bundle = await _ensure_bundle(
        acct["id"], "session_bundle", base_url, acct["username"], acct["secret"], timeout_s
    )
    if bundle:
        return RoleLogin(
            role, acct, lease_id, bundle, None, None, acct["id"], "session_bundle", label
        )
    # No captured session and we cannot capture one -> the agent fills the login form.
    return RoleLogin(role, acct, lease_id, None, acct["username"], acct["secret"], None, "session_bundle", label)


# A capture is only worth re-using while the app-side session behind it is still alive.
# Most systems expire a session around 30 minutes, so SESSION_TTL_MIN defaults just under
# that; the old 8h guess meant a bundle stayed "fresh" for hours after the server had
# forgotten it, and every case restored a corpse and landed on the login page.
def bundle_ttl() -> timedelta:
    return timedelta(minutes=get_settings().session_ttl_min)


# A herd of self-heals must not become a herd of logins. The rate limit on the app under
# test is per-account, so what matters is how often we log in, not how many browsers run.
RECAPTURE_COOLDOWN_S = 90


def bundle_is_fresh(
    expires_at: datetime | None,
    now: datetime | None = None,
    min_remaining_s: int = 0,
) -> bool:
    """Is the stored bundle usable for the next `min_remaining_s` seconds?

    Callers pass the case's timeout, so a case never starts on a session due to die
    mid-run. No expiry recorded means it was captured before expiry was tracked — so it is
    at least as old as that change and stale by definition. Trusting that (the first cut
    did) kept the exact bug it was meant to fix: run 44 served a day-old dead session to
    16 browsers at once and every one fell through to the login form."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:  # sqlite drops the offset on round-trip; postgres keeps it
        expires_at = expires_at.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) + timedelta(seconds=min_remaining_s) < expires_at


def captured_recently(
    expires_at: datetime | None,
    ttl: timedelta,
    within_s: int,
    now: datetime | None = None,
) -> bool:
    """Was this bundle captured in the last `within_s` seconds? capture time is implied by
    the expiry (captured_at == expires_at - ttl). Used to collapse a burst of forced
    re-captures — every case of a run noticing the same dead session — into one login."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return (expires_at - ttl) > (now or datetime.now(UTC)) - timedelta(seconds=within_s)


async def _ensure_bundle(
    cred_id: int,
    field: str,
    base_url: str | None,
    username: str | None,
    password: str,
    timeout_s: int,
    force: bool = False,
) -> str | None:
    """Reusable session for an account, stored in credential.<field> (encrypted). Returns
    its captured bundle, capturing one (single-flight) when missing — or force-recapturing
    on `force=True` (P3 self-heal after a dead session). Only ONE worker captures per
    credential (leasing.capture_lock); the rest wait for the bundle to land in the DB.
    Returns None if capture isn't possible (caller falls back to prompt-login)."""
    import asyncio as _asyncio
    import time as _time

    from app import leasing
    from app.crypto import decrypt, encrypt
    from app.db import db_session
    from app.executor import capture_session
    from app.models import Credential

    ttl = bundle_ttl()

    async def _read() -> str | None:
        """The stored bundle, but only if it will outlive the case about to use it."""
        async with db_session() as s:
            c = await s.get(Credential, cred_id)
            v = getattr(c, field, None) if c else None
            if not v or not bundle_is_fresh(c.expires_at, min_remaining_s=timeout_s):
                return None
            return decrypt(v)

    async def _read_if_just_captured() -> str | None:
        """Someone re-captured while we queued for the lock — take theirs, don't log in
        again. This is what keeps N cases self-healing off the same dead session from
        becoming N logins against a per-account rate limit."""
        async with db_session() as s:
            c = await s.get(Credential, cred_id)
            v = getattr(c, field, None) if c else None
            if not v or not captured_recently(c.expires_at, ttl, RECAPTURE_COOLDOWN_S):
                return None
            return decrypt(v)

    if not force:
        existing = await _read()
        if existing:
            return existing
    elif (just := await _read_if_just_captured()) is not None:
        return just
    if not base_url or not username:
        return None
    got = await leasing.capture_lock(cred_id, ttl_s=timeout_s + 60)
    try:
        if not got:
            # another worker is (re)capturing — wait for a bundle to land
            end = _time.monotonic() + timeout_s
            while _time.monotonic() < end:
                await _asyncio.sleep(2.0)
                b = await _read()
                if b:
                    return b
            return None
        # double-check under the lock: someone may have captured while we queued
        b = await (_read() if not force else _read_if_just_captured())
        if b:
            return b
        try:
            # 2026-10-04 隔离修复：捕获必须带上项目，否则浏览器会用全局默认 profile，
            # 把别的项目的 cookies 一起捕获进来（用户报的"串账号"就是这个）。
            async with db_session() as _s:
                _c = await _s.get(Credential, cred_id)
                _pid = _c.project_id if _c else None
            bundle = await capture_session(base_url, username, password, _pid)
        except Exception:
            return None
        async with db_session() as s:
            c = await s.get(Credential, cred_id)
            if c is not None:
                setattr(c, field, encrypt(bundle))
                c.expires_at = datetime.now(UTC) + ttl
        return bundle
    finally:
        if got:
            await leasing.capture_unlock(cred_id)


async def _update_account_health(account_id: int, status: str, error_msg: str | None) -> None:
    """Passive health: mark the leased account healthy (pass) or unhealthy (error). On the
    first transition to unhealthy, notify the account's creator."""
    from datetime import datetime

    from sqlalchemy import select

    from app import notify
    from app.db import db_session
    from app.models import Credential, User

    async with db_session() as s:
        cred = await s.get(Credential, account_id)
        if cred is None:
            return
        cred.last_checked_at = datetime.now(UTC)
        if status == "passed":
            cred.healthy = True
            cred.last_error = None
            return
        was_healthy = cred.healthy
        cred.healthy = False
        cred.last_error = (error_msg or "登录/执行失败")[:500]
        if was_healthy and cred.created_by:
            u = (
                (await s.execute(select(User).where(User.email == cred.created_by)))
                .scalars()
                .first()
            )
            if u is not None:
                await notify.push(
                    s,
                    user_id=u.id,
                    type="account_unhealthy",
                    title=f"账号异常:{cred.label or cred.username or '#' + str(cred.id)}",
                    body=f"角色「{cred.role or '默认'}」的账号执行失败:{cred.last_error}",
                    link=f"/projects/{cred.project_id}/settings",
                )


async def _finalize_case_error(live_id: int, run_id: int, msg: str) -> str:
    """Mark a live result as error (e.g. no account for the role) and bump progress."""
    from app.db import db_session
    from app.models import Run, RunResult

    async with db_session() as s:
        row = await s.get(RunResult, live_id)
        if row is not None:
            row.status = "error"
            row.error = msg[:500]
            row.attempts = 1
        run_row = await s.get(Run, run_id)
        if run_row is not None:
            run_row.processed_count += 1
    return "error"


async def execute_one_case(run_id: int, case_id: int) -> str:
    """Run one case end-to-end: resolve the account (by the case's role, leasing a free
    one), drive the browser agent with infra-error retries, persist the RunResult, bump
    progress, and flip the run pending->running on first start. Never raises — returns the
    final status, so it is a safe Celery task body and the chord always reaches finalize."""
    from datetime import datetime

    from app import leasing
    from app.db import db_session
    from app.executor import CaseSpec, execute_case
    from app.models import Project, Run, RunResult, TestCase

    lease_id: int | None = None
    run_slot: int | None = None
    try:
        # Wait for one of the run's concurrency slots before doing ANY work. fan_out_run
        # hands every case to Celery at once, so until now the only cap was the worker's
        # --concurrency (16) and the project's 并发数 did nothing: a 49-case run opened 16
        # browsers in the same instant, which is what turned one dead session into a login
        # storm. Re-reads the run each round so cancelling releases the waiters too.
        while run_slot is None:
            async with db_session() as s:
                run = await s.get(Run, run_id)
                # Any terminal state, not just cancelled: the reconciler marks an abandoned
                # run 'completed', and a waiter that only checks for 'cancelled' would spin
                # on a run nobody will ever finish until the worker is restarted.
                if run is None or run.status in _TERMINAL:
                    return "error"
                slots = max(1, run.concurrency or 1)
                project = await s.get(Project, run.project_id)
                # The lease has to outlive the case holding it, or it expires mid-browser,
                # the slot frees, and another case starts on top of it — 并发 drifts past
                # its cap silently. Sized off the PROJECT's timeout: VRS overrides it to
                # 1200s against a global default of 150s, which would have expired the
                # lease at 600s with the browser still running.
                slot_ttl = int(
                    hold_limit(
                        (project.case_timeout_s if project else None)
                        or get_settings().case_timeout_s
                    ).total_seconds()
                )
            run_slot = await leasing.run_slot_acquire(run_id, slots, ttl_s=slot_ttl, wait_s=20)

        role: str | None = None
        candidates: list[dict] = []
        default_login = DefaultLogin(None, None, None, None, None)
        # 2026-10-04 多角色：本次用例实际租到的**所有**账号（首个角色 + 额外角色）。
        # 必须在函数作用域外可见，finally 里要逐个释放。
        _leased_ids: list[int] = []
        # 额外角色（roles[1:]）的登录态，供 agent 中途 switch_account 使用。
        _extra_logins: list[RoleLogin] = []
        # setup: load run/case/project, plan login (role candidates or default), flip
        # pending->running (idempotent), create the live row.
        async with db_session() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status in _TERMINAL:  # finished while we queued for a slot
                return "error"
            case = await s.get(TestCase, case_id)
            if case is None:
                return "error"
            project = await s.get(Project, run.project_id)
            env_id = run.environment_id
            # 2026-10-04 多角色：roles 是有序列表，role 保留为兼容镜像。
            # _case_roles() 负责「roles 空就回落到 role」，所以下面只需要看 case_roles。
            case_roles = _case_roles(case)
            role = case_roles[0] if case_roles else None
            if not case_roles:
                default_login = await _resolve_login(s, run, env_id)
            _s = get_settings()
            to_s = (project.case_timeout_s if project else None) or _s.case_timeout_s
            max_st = (project.case_max_steps if project else None) or _s.case_max_steps
            # 证据采集开关：项目上设过就用项目的，没设（None）就跟随全局默认。
            # 让用户在界面上自己选，不必改 .env 再重启。
            _rec_video = project.case_record_video if project else None
            _shot_every = project.live_shot_every if project else None
            # base_url from the run's environment, falling back to the project's
            base_url = project.base_url if project else None
            if env_id is not None:
                from app.models import Environment

                env = await s.get(Environment, env_id)
                if env is not None and env.base_url:
                    base_url = env.base_url
            prompt = _effective_prompt(case)
            expected = case.expected
            start_url = resolve_start_url(case.start_url, base_url)
            if run.status == "pending":
                run.status = "running"
                run.started_at = datetime.now(UTC)
            live = RunResult(run_id=run_id, case_id=case_id, status="running", diagnostics=[])
            s.add(live)
            await s.flush()
            live_id = live.id

        login_state: str | None = None
        login_user: str | None = None
        login_pass: str | None = None
        account_label: str | None = None
        # P3 self-heal source: which credential's bundle to re-capture (and how) if the
        # restored session turns out dead. None => nothing to heal (e.g. prompt-login).
        bundle_cred_id: int | None = None
        bundle_field: str = "session_bundle"
        refresh_user: str | None = None
        refresh_pass: str | None = None
        # 2026-10-04 多角色：roles[1:] 的预备登录态，供 agent 中途 switch_account 用。
        # 第一个角色走下面原有的单角色路径，行为与改动前完全一致 —— 这样 557 条
        # 存量单角色用例的执行链路一个字节都没变，回归风险为零。
        extra_logins: list[RoleLogin] = []
        if case_roles:
            # 每个角色都要有账号，缺任何一个都明确失败（绝不悄悄用别的账号顶替）
            for r in case_roles:
                rl = await _prepare_role_login(
                    s, run.project_id, r, env_id, base_url, to_s
                )
                if rl.account is None:
                    why = (
                        "没有可用账号"
                        if not await _role_candidates(s, run.project_id, r, env_id)
                        else "账号都在忙"
                    )
                    return await _finalize_case_error(live_id, run_id, f"角色「{r}」{why}")
                _leased_ids.append(rl.lease_id)
                if r == role:
                    # 首个角色：沿用原来的单角色变量，heal 信息也照旧填
                    login_state = rl.bundle
                    login_user = rl.user
                    login_pass = rl.password
                    account_label = rl.label
                    lease_id = rl.lease_id
                    if rl.heal_cred_id:
                        bundle_cred_id = rl.heal_cred_id
                        bundle_field = rl.heal_field
                        refresh_user = rl.account["username"]
                        refresh_pass = rl.account["secret"]
                else:
                    extra_logins.append(rl)
        else:
            login_state, login_user, login_pass, state_cred_id, pw_cred_id = default_login
            # default account's bundle lives in the storage_state cred's `secret`; the robot
            # password account (login_user/pass) is how we re-capture it on a dead session.
            if login_state and state_cred_id and login_user:
                bundle_cred_id = state_cred_id
                bundle_field = "secret"
                refresh_user = login_user
                refresh_pass = login_pass
            elif login_user and login_pass and pw_cred_id:
                # No captured session yet. Capture one from the default password account and
                # cache it on that row — exactly what a role account already does. Without
                # this a role-less case typed the login form on EVERY case of EVERY run,
                # burning steps before it could even reach the module under test.
                bundle = await _ensure_bundle(
                    pw_cred_id, "session_bundle", base_url, login_user, login_pass, to_s
                )
                if bundle:
                    login_state = bundle
                    bundle_cred_id = pw_cred_id
                    bundle_field = "session_bundle"
                    refresh_user = login_user
                    refresh_pass = login_pass

        spec = CaseSpec(
            case_id=case_id,
            name=case.name or "",
            prompt=prompt,
            expected=expected,
            start_url=start_url,
            login_state=login_state,
            login_username=login_user,
            login_password=login_pass,
            # 2026-10-04 多角色：把额外角色的登录态交给执行器，供 agent 中途切换。
            # 单角色时是空列表，switch_account 工具不会被注册。
            extra_role_logins=[
                {
                    "role": rl.role,
                    "bundle": rl.bundle,
                    "user": rl.user,
                    "password": rl.password,
                    "label": rl.label,
                }
                for rl in extra_logins
            ],
            timeout_s=to_s,
            max_steps=max_st,
            result_id=live_id,
            # Workspace-scoped persistent profile: cases of one project reuse the same
            # warm browser state (cookies/storage/IndexedDB/cache) across cases and runs,
            # which is what makes a regression re-run skip login + cold asset loads.
            project_id=run.project_id,
            concurrency=locals().get("slots") or 1,
            persistent_profile=True,
            record_video=_rec_video,
            shot_every=_shot_every,
            # 数据隔离提示：只在用例作者显式写了才注入。默认不注入 ——
            # 给每个用例都塞一段"环境可能不干净"的话，只会让真正相关的用例被淹没。
            data_hygiene=case.data_hygiene or None,
        )

        async def on_step(steps: list) -> None:
            async with db_session() as s:
                row = await s.get(RunResult, live_id)
                if row is not None:
                    row.diagnostics = steps

        async def run_cancelled() -> bool:
            """这个 run 是否已被取消。取消后绝不能再起新的一次尝试 ——
            取消的语义是"停下"，重试会让它重新跑起来，白烧一个完整预算。"""
            async with db_session() as s:
                r = await s.get(Run, run_id)
                return r is None or r.status == "cancelled"

        async def should_abort() -> bool:
            """Cancelling a run used to only flip the run row: cases already driving a
            browser ran to completion, so the button did nothing for up to case_timeout_s
            each and the results sat at 'running' forever. Checked once per step, on the
            session the step callback opens anyway."""
            return await run_cancelled()

        from dataclasses import replace

        retries = get_settings().case_retries
        attempt_statuses: list[str] = []
        r = None
        healed = False
        infra_attempts = 0
        while True:
            r = await execute_case(spec, on_step=on_step, should_abort=should_abort)
            attempt_statuses.append(r.status)
            # A timeout means the case wants more wall clock than its budget. Neither
            # re-attempt below can change that, and each one burns another full budget
            # (10 minutes at case_timeout_s=600). It also stops the self-heal from
            # misreading a run cut off mid-flight as a dead session: the last URL of a
            # timed-out attempt says nothing about whether the login still works.
            if r.timed_out:
                break
            # P3: dead restored session (ended on a login page) → invalidate + re-capture +
            # retry once. This one-shot heal is separate from the infra-error retries.
            if (
                r.auth_failed
                and not healed
                and bundle_cred_id is not None
                and refresh_user
                and base_url
            ):
                healed = True
                infra_attempts += 1  # heal 也要计入预算，见下面 should_retry 处的说明
                fresh = await _ensure_bundle(
                    bundle_cred_id,
                    bundle_field,
                    base_url,
                    refresh_user,
                    refresh_pass,
                    to_s,
                    force=True,
                )
                if fresh:
                    spec = replace(spec, login_state=fresh)
                    continue
            if await run_cancelled():
                break
            if not should_retry(r, retries, infra_attempts):
                break
            infra_attempts += 1
        final, flaky = resolve_attempts(attempt_statuses)

        async with db_session() as s:
            row = await s.get(RunResult, live_id)
            if row is not None:
                row.status = final
                row.attempts = len(attempt_statuses)
                row.flaky = flaky
                row.video_url = r.video_url
                row.trace_url = r.trace_url
                row.steps = r.steps
                # final history-based diagnostics (with results/errors) supersede live ones
                row.diagnostics = r.diagnostics or row.diagnostics
                row.judge_reason = r.judge_reason
                # 2026-10-04 失败根因分类 + 判定依据步骤。
                # `or row.*` 的原因同 failure_narrative：一次重试若没产出分类
                # （例如被取消），不能把上一轮已经得到的分类擦掉。
                row.root_cause = r.root_cause or row.root_cause
                row.verdict_evidence = r.verdict_evidence or row.verdict_evidence
                row.final_answer = r.final_answer
                # AI bug description for failed/errored cases (NULL for passed).
                # `or row.failure_narrative` keeps the last non-empty value: the live
                # heartbeat path may have written a progress snapshot, and a later retry
                # that produced no narrative must not erase a good one.
                row.failure_narrative = r.failure_narrative or row.failure_narrative
                row.account_label = account_label
                row.latency_ms = r.latency_ms
                row.error = r.error
            run_row = await s.get(Run, run_id)
            if run_row is not None:
                run_row.processed_count += 1
                if final == "passed":
                    run_row.passed_count += 1
        # Heartbeat after every case; it self-throttles to ~1 message per interval.
        # Outside any transaction: the push can take seconds and must not hold a lock.
        try:
            from app import feishu_notify

            await feishu_notify.push_run_progress(run_id)
        except Exception:
            pass
        # 积累"操作经验"：把本次的过程记录提炼成记忆写回用例，下次跑同一条时注入，
        # 减少重新摸索、让运行路径更稳。见 app/case_memory.py。
        #
        # 同样放在事务外：提炼要走一次 LLM 调用（可能几十秒），绝不能占着数据库锁。
        # 也**只传过程（diagnostics），不传判定结果** —— 记忆里不允许出现"通过/失败"，
        # 否则下次就变成背答案。传什么由这里决定，过滤由 case_memory 的白名单兜底。
        try:
            from app.case_memory import remember_case

            await remember_case(case_id, (r.diagnostics if r is not None else None) or [])
        except Exception:
            pass
        # passive account health: the leased role account is healthy on a pass, unhealthy
        # on an infra error (likely login). A judge 'failed' is a test bug, not the account.
        if lease_id is not None and final in ("passed", "error"):
            await _update_account_health(lease_id, final, r.error if r else None)
        return final
    except Exception:
        # never propagate: a raising case task would break the Celery chord so finalize
        # would never fire. The run's counters/summary are reconciled in finalize_run.
        return "error"
    finally:
        # 2026-10-04 多角色：释放**所有**租到的账号，不只是第一个。
        # 只释放 lease_id 会让 roles[1:] 的账号一直被占住，跑第二轮就报「账号都在忙」，
        # 而且 leasing 有 TTL 只是兜底 —— 那等于并发上限被慢慢吃光。
        for _lid in _leased_ids:
            try:
                await leasing.release(_lid)
            except Exception:
                pass
        await leasing.run_slot_release(run_slot)


async def finalize_run(run_id: int) -> dict:
    """Aggregate the persisted results into the run summary and mark it completed.
    Idempotent: safe as a Celery chord callback and as the in-process tail."""
    from datetime import datetime

    from sqlalchemy import select

    from app.db import db_session
    from app.models import Run, RunResult

    async with db_session() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return {"error": f"run {run_id} missing"}
        res_rows = (
            (await session.execute(select(RunResult).where(RunResult.run_id == run_id)))
            .scalars()
            .all()
        )
        summary = aggregate(
            [x.status for x in res_rows],
            [x.latency_ms for x in res_rows],
            flaky=sum(1 for x in res_rows if x.flaky),
        )
        run.summary = summary
        run.processed_count = len(res_rows)
        run.passed_count = summary["passed"]
        run.status = "completed" if run.status != "cancelled" else "cancelled"
        run.finished_at = datetime.now(UTC)
        if run.suite_id is not None:
            await _finalize_suite(session, run, summary)
        await session.commit()

    # Proactive Feishu push to the project's bound chat (best-effort, outside the txn).
    try:
        from app import feishu_notify

        await feishu_notify.push_run_result(run_id)
    except Exception:
        pass
    return summary


def hold_limit(timeout_s: int) -> timedelta:
    """The longest a single case can legitimately hold a slot, or go silent.

    It can spend one timeout waiting for a free account for its role, one capturing a
    session bundle, and one actually running — plus judge and upload slack. Both the
    run-concurrency lease and the stale-run reconciler are sized off this: a lease shorter
    than it frees a slot with a browser still on it (并发 drifts past its cap), and a
    reconciler shorter than it kills a healthy run."""
    return timedelta(seconds=timeout_s * 3 + 300)


def stall_limit(timeout_s: int, cancelled: bool = False) -> timedelta:
    """How long a run may go completely silent before we conclude nothing is running it.
    A live case rewrites its result row after every browser step, so silence is the signal.
    A cancelled run gets a much shorter fuse: its cases stop at their next step."""
    return timedelta(seconds=timeout_s + 60) if cancelled else hold_limit(timeout_s)


def _aware(dt: datetime | None) -> datetime | None:
    """sqlite drops the offset on round-trip; postgres keeps it."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def reconcile_stale_runs(now: datetime | None = None) -> list[int]:
    """Finish runs whose case tasks died with their worker. Returns the ids finalized.

    Celery acks a case task when it hands it to a worker, so a worker restart — a deploy,
    an OOM kill — drops every case that worker held, with no redelivery. Those RunResult
    rows stay 'running' forever, processed_count freezes, and the chord callback that
    finalizes the run never fires. Nothing in the system could then move that run: run 44
    sat at 43/49 with six cases 'running' and no finished_at for three days after a deploy
    recreated the worker mid-run.

    Beat calls this every minute. A run silent for longer than one case can legitimately
    take has no workers left, so terminalize its unfinished cases and finalize it."""
    from sqlalchemy import select

    from app.db import db_session
    from app.models import Project, Run, RunResult, TestCase

    now = now or datetime.now(UTC)
    dead: list[int] = []
    async with db_session() as s:
        runs = (
            (
                await s.execute(
                    select(Run).where(
                        Run.finished_at.is_(None),
                        Run.status.in_(("pending", "running", "cancelled")),
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in runs:
            project = await s.get(Project, run.project_id)
            timeout_s = (
                project.case_timeout_s if project else None
            ) or get_settings().case_timeout_s
            beats = [_aware(r.updated_at) for r in await _run_rows(s, run.id)]
            beats += [_aware(run.started_at), _aware(run.created_at)]
            if now - max(b for b in beats if b is not None) >= stall_limit(
                timeout_s, run.status == "cancelled"
            ):
                dead.append(run.id)

    finalized: list[int] = []
    for rid in dead:
        # One transaction per run: a run that cannot be written must not take the rest of
        # the backlog down with it. The first live pass hit exactly that — an old run
        # referencing a since-deleted case raised a foreign-key error that rolled back
        # every other run in the same session, so nothing got reconciled at all.
        try:
            async with db_session() as s:
                run = await s.get(Run, rid)
                why = (
                    "运行已取消"
                    if run.status == "cancelled"
                    else "执行进程中断(worker 重启或被回收)"
                )
                rows = await _run_rows(s, rid)
                for r in rows:
                    if r.status == "running":
                        r.status = "error"
                        r.error = why
                # A case whose task died before it could open a row would otherwise vanish
                # from the report — record it so the run's total and its table agree, and
                # so "只重跑失败用例" can pick it up. Cases deleted since the run started
                # get no row: there is nothing left to re-run and the FK would reject it.
                missing = [c for c in (run.case_ids or []) if c not in {r.case_id for r in rows}]
                still_exist = (
                    (await s.execute(select(TestCase.id).where(TestCase.id.in_(missing))))
                    .scalars()
                    .all()
                )
                for cid in still_exist:
                    s.add(
                        RunResult(
                            run_id=rid,
                            case_id=cid,
                            status="error",
                            error=f"未执行:{why}",
                            diagnostics=[],
                        )
                    )
            await finalize_run(rid)
            finalized.append(rid)
        except Exception:
            log.exception("could not finish stale run %s", rid)
    return finalized


async def _run_rows(session, run_id: int) -> list:
    from sqlalchemy import select

    from app.models import RunResult

    return list(
        (await session.execute(select(RunResult).where(RunResult.run_id == run_id))).scalars().all()
    )


# cadence -> next-due delta (kept here so engine doesn't import api; mirrors api._CADENCE)
_CADENCE_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}


async def _finalize_suite(session, run, summary: dict) -> None:
    """A suite run finished: update the suite's last_* + advance its due date, then
    notify the owner and the actual runner (failures always emailed)."""
    from datetime import datetime, timedelta

    from app import notify
    from app.models import TestSuite

    suite = await session.get(TestSuite, run.suite_id)
    if suite is None:
        return
    suite.last_run_id = run.id
    suite.last_status = run.status
    suite.last_run_at = run.finished_at
    days = _CADENCE_DAYS.get(suite.cadence)
    if days is not None:
        suite.due_at = (run.finished_at or datetime.now(UTC)) + timedelta(days=days)

    total = summary.get("total", 0)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0) + summary.get("error", 0)
    title = f"套件运行完成:{suite.name}(通过 {passed}/{total})"
    body = f"通过率 {round(100 * passed / total) if total else 0}%,失败/异常 {failed} 条。"
    recipients = {suite.owner_user_id, run.ran_by_user_id} - {None}
    link = f"/projects/{suite.project_id}/runs/{run.id}"
    for uid in recipients:
        await notify.push(
            session,
            user_id=uid,
            type="run_done",
            title=title,
            body=body,
            link=link,
            suite_id=suite.id,
            run_id=run.id,
        )


async def run_suite(run_id: int) -> dict:
    """In-process fallback (no Redis/Celery): prepare, drain every case under the global
    slot at the run's per-run concurrency, then finalize. The Celery path fans the same
    cases out as case-level tasks instead — see app.celery_app.fan_out_run."""
    from app.db import db_session
    from app.models import Run

    case_ids = await prepare_run(run_id)
    if case_ids is None:
        return {"error": f"run {run_id} not runnable"}
    async with db_session() as s:
        run = await s.get(Run, run_id)
        concurrency = (run.concurrency if run else None) or 2
    # Tell the phone the run started, before any case has had time to finish. Everything
    # here is best-effort: a failed push must never stop the run itself.
    try:
        from app import feishu_notify

        await feishu_notify.push_run_started(run_id)
    except Exception:
        pass
    await drain(
        case_ids,
        lambda cid: execute_one_case(run_id, cid),
        concurrency,
        slot=global_slot(),
    )
    return await finalize_run(run_id)
