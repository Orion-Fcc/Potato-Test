"""Request bodies validated at the API boundary (Pydantic)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: str | None = None
    login_state: str | None = None  # storage_state JSON string


class ProjectPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    case_timeout_s: int | None = Field(default=None, ge=15, le=600)
    case_max_steps: int | None = Field(default=None, ge=3, le=100)
    run_concurrency: int | None = Field(default=None, ge=1, le=16)
    # 证据采集开关（用户自己选）。
    # 注意语义：**没传这个键 = 不修改**；显式传 null = 跟随全局默认；
    # 传 true/false（或 0/正整数）= 明确设定。判断"有没有传"要用 model_fields_set，
    # 不能只看值是不是 None —— 否则"改回跟随默认"这个操作就表达不出来。
    case_record_video: bool | None = None
    live_shot_every: int | None = Field(default=None, ge=0, le=50)
    feishu_chat_id: str | None = None  # "" unbinds; bot routes runs/pushes to this chat
    feishu_bitable_url: str | None = None  # paste the base URL; "" clears the binding


class AssistantIn(BaseModel):
    """In-app chat assistant turn (see app/assistant.py)."""

    message: str = Field(min_length=1, max_length=4000)
    # prior turns: [{"role": "user"|"assistant", "content": str}]
    history: list[dict] = Field(default_factory=list)


class KnowledgeIn(BaseModel):
    """Project spec/knowledge text (pasted or uploaded), searchable by the assistant."""

    text: str = Field(default="", max_length=2_000_000)


Priority = Literal["P0", "P1", "P2", "P3"]
CaseType = Literal["functional", "smoke", "regression", "acceptance", "negative"]
CaseStatus = Literal["draft", "active", "deprecated"]


class TestStep(BaseModel):
    action: str = ""
    expected: str = ""


def _roles_of(role: str | None, roles: list[str] | None) -> list[str]:
    """Normalize the two role inputs into one ordered, de-duplicated list.

    A case used to carry a single `role`. Multi-role cases (2026-10-04) need an
    ordered list, but the 378 existing cases must keep working **without being
    rewritten**, so the rule is "roles is the superset, role is the fallback":

      - roles given, role blank  -> roles as-is
      - roles blank, role given  -> [role]              (every legacy case lands here)
      - both given, role in roles-> roles (role adds nothing)
      - role given, not in roles -> [role, *roles]      (role leads: it was the
        primary identity before, and the first role is who the case starts as)

    Order matters — it is the order the agent switches in — so this is a list, not
    a set. Duplicates are dropped, blanks skipped, and `role` is kept mirrored to
    roles[0] by the callers so either column alone is enough to read the case.
    """
    out: list[str] = []
    for r in ([role] if role else []) + list(roles or []):
        v = (r or "").strip()
        if v and v not in out:
            out.append(v)
    return out


class TestCaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    prompt: str = Field(min_length=1)  # the NL task the agent executes
    expected: str = ""
    case_key: str | None = None  # auto-assigned (TC-001) when omitted
    module: str | None = None
    priority: Priority = "P2"
    type: CaseType = "functional"
    status: CaseStatus = "active"
    owner: str | None = None
    role: str | None = None  # multi-account: which role/account this case runs as
    # 2026-10-04 multi-role: every role this case switches through, in order.
    # Empty => fall back to `role`, so existing single-role cases are unchanged.
    roles: list[str] = Field(default_factory=list)
    references: str = ""
    preconditions: str = ""
    steps: list[TestStep] = Field(default_factory=list)  # documentation, not executed
    test_data: str = ""
    start_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class TestCasePatch(BaseModel):
    name: str | None = None
    prompt: str | None = None
    expected: str | None = None
    case_key: str | None = None
    module: str | None = None
    priority: Priority | None = None
    type: CaseType | None = None
    status: CaseStatus | None = None
    owner: str | None = None
    role: str | None = None
    # 2026-10-04 multi-role; empty list means "fall back to role".
    # An empty list here is meaningful ("回落到单角色"), so the API must be able to
    # tell it apart from "this key was not sent" — see TestCasePatch handling in api.py.
    roles: list[str] | None = None
    references: str | None = None
    preconditions: str | None = None
    steps: list[TestStep] | None = None
    test_data: str | None = None
    # 2026-10-06 测试数据文件声明。接受 dict / 数组 / JSON 字符串三种形态：
    # 前端 textarea 给出的是字符串，API 编程调用给 dict 更顺手，都不该逼用户转换。
    data_files: dict | list | str | None = None
    data_hygiene: str | None = None
    start_url: str | None = None
    tags: list[str] | None = None
    enabled: bool | None = None


class EnvironmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str | None = None
    is_default: bool = False


class EnvironmentPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    is_default: bool | None = None


class CredentialIn(BaseModel):
    type: Literal["storage_state", "password"]
    role: str | None = None  # multi-account: the role this account plays
    environment_id: int | None = None  # environment this account belongs to (null = any)
    label: str = ""
    username: str | None = None  # required for type=password
    secret: str = Field(min_length=1)  # storage_state JSON, or the password (encrypted server-side)


class CredentialPatch(BaseModel):
    """改一条**既有**凭据的字段（2026-10-06）。

    为什么需要：此前只有 POST（新建）和 DELETE，想改密码只能"先删后建"。
    而删掉一条凭据会连带丢掉它的 role / environment / 会话缓存，还得重新走一遍
    登录验证 —— 密码轮换（例如把 13 个 role 账号统一改成同一新密码）
    是常规运维动作，它应该是一条独立的操作，不是"删了重建"。

    字段全部可选，用 `model_fields_set` 区分"没传"与"传了 null"：
    没传 = 保持原值，传 null = 显式清空（role / environment_id 需要这个区别）。
    """

    label: str | None = None
    username: str | None = None
    # 新密码（明文，服务端加密后存）。不传 = 不改密码。
    secret: str | None = Field(default=None, min_length=1)
    role: str | None = None
    environment_id: int | None = None


class CredentialCaptureIn(BaseModel):
    """Server logs into the project's base_url with this test account and captures the
    resulting session (no CLI). Password is used to log in and stored encrypted for refresh."""

    label: str = ""
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class IssueIn(BaseModel):
    title: str = Field(min_length=1, max_length=400)
    description: str = ""
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    assignee: str | None = None
    assignee_user_id: int | None = None
    labels: list[str] = Field(default_factory=list)
    case_id: int | None = None
    run_id: int | None = None
    result_id: int | None = None


class IssuePatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: Literal["open", "in_progress", "fixed", "verified", "closed"] | None = None
    severity: Literal["low", "medium", "high", "critical"] | None = None
    assignee: str | None = None
    assignee_user_id: int | None = None
    labels: list[str] | None = None


Cadence = Literal["none", "daily", "weekly", "biweekly", "monthly"]


class SuiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    description: str = ""
    selection_mode: Literal["cases", "tags"] = "cases"
    case_ids: list[int] = Field(default_factory=list)
    tag_filter: list[str] = Field(default_factory=list)
    owner_user_id: int | None = None
    runner_user_id: int | None = None
    cadence: Cadence = "none"
    environment_id: int | None = None


class SuitePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    selection_mode: Literal["cases", "tags"] | None = None
    case_ids: list[int] | None = None
    tag_filter: list[str] | None = None
    owner_user_id: int | None = None
    runner_user_id: int | None = None
    cadence: Cadence | None = None
    environment_id: int | None = None


class SuiteAssignIn(BaseModel):
    runner_user_id: int | None = None


class RolesIn(BaseModel):
    roles: list[str] = Field(default_factory=list)


class IssueCommentIn(BaseModel):
    body: str = Field(min_length=1)
    author: str | None = None


class GitLabConfigIn(BaseModel):
    gitlab_project: str = Field(default="", max_length=400)  # "" unlinks
    token: str | None = None  # write-only; omit to keep the current token


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)


class ForgotIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class ResetIn(BaseModel):
    token: str = Field(min_length=1, max_length=1024)
    password: str = Field(min_length=6)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=6)


ProjectRole = Literal["owner", "editor", "viewer"]


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    is_admin: bool = False
    project_id: int | None = None
    project_role: ProjectRole = "editor"


class InviteAcceptIn(BaseModel):
    name: str = Field(default="", max_length=120)
    password: str = Field(min_length=6)


class MemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    role: ProjectRole = "editor"


class MemberPatch(BaseModel):
    role: ProjectRole


class UserPatch(BaseModel):
    is_active: bool | None = None
    is_admin: bool | None = None


class GitlabTokenIn(BaseModel):
    token: str = ""  # "" clears the global token


class LlmSettingsIn(BaseModel):
    # Non-secret fields: None => leave unchanged; "" => clear the override (env fallback).
    base_url: str | None = None
    model: str | None = None
    agent_model: str | None = None
    # Secret: blank/None => keep the stored key; non-empty => replace.
    api_key: str | None = None


class FeishuSettingsIn(BaseModel):
    # Non-secret fields: None => leave unchanged; "" => clear.
    app_id: str | None = None
    api_base: str | None = None
    auto_answer_detected: bool | None = None
    # Secrets: blank/None => keep the stored value; non-empty => replace.
    app_secret: str | None = None
    verification_token: str | None = None


class RunIn(BaseModel):
    name: str = "run"
    case_ids: list[int] | None = None  # None => all enabled cases in the project
    tags: list[str] | None = None  # optional filter when case_ids omitted
    concurrency: int = Field(default=2, ge=1, le=16)
    # 2026-10-08 per-run 重试。None（不传）= 沿用全局 CASE_RETRIES；
    # 显式 0 = 这一轮不重试；N = 每条用例最多再跑 N 次（只重跑"重试有救"的失败）。
    # 上限 3：再往上就是在一条卡死的用例上反复烧满预算，收益为负。
    retries: int | None = Field(default=None, ge=0, le=3)
    environment_id: int | None = None  # target environment (null => project default base_url)


class RunPatch(BaseModel):
    name: str = Field(min_length=1, max_length=300)


class ResultPatch(BaseModel):
    """人工改判一条用例的结论（2026-10-06）。

    为什么要这个接口：AI 判定会不准 —— 它会把"没看到"判成"通过"，也会把"页面长得
    不一样但其实是对的"判成失败。测试工程师必须能一键纠正，否则报告不可用。

    status 只允许 passed / failed 两个值：
      · 不接受 error / running —— 那是执行层的状态，不是"结论"，人改判的是结论；
      · 传 null 表示**撤销改判**，恢复成 AI 的原始结论（因此下面三个字段都可为 None）。
    """

    status: Literal["passed", "failed"] | None = None
    reason: str | None = Field(default=None, max_length=500)
    # 撤销改判：true 表示清掉 verdict_override*，回到 AI 判定。
    clear: bool = False
