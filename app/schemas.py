"""Request bodies validated at the API boundary (Pydantic)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: str | None = None
    login_state: str | None = None  # storage_state JSON string
    # 所属「大系统」（2026-10-13）：建项目时可直接归到某个大系统下面，NULL=不归。
    group_id: int | None = None


class SecretKeyIn(BaseModel):
    """凭据加密密钥导入体。密钥只在内存中校验，不写日志。"""

    key: str = Field(min_length=1, max_length=512)


class GroupIn(BaseModel):
    """新建「大系统」（2026-10-13）。"""

    name: str = Field(min_length=1, max_length=200)


class GroupPatch(BaseModel):
    name: str | None = None


class ProjectPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    # 归属大系统。注意语义：**没传 = 不改**；显式传 null = 移出大系统（见 api.update_project，
    # 它按 model_fields_set 判断"到底传没传"）。
    group_id: int | None = None
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
    # ★ 粘贴/拖入对话的文件（2026-10-12 用户要求「和其他 agent 一样有粘贴文件在对话框的能力」）。
    #
    # 每个元素是**已经解析好的文本**（前端先调 `/assistant/parse` 拿到），而不是原始字节：
    #   * 解析放服务端（docx/pdf/xlsx 都需要 Python 库），但**不必把文件塞进这一路请求**——
    #     100MB 的需规走 JSON base64 会让请求体膨胀三分之一，
    #     而这一路是对话、不该被附件撑大；
    #   * 文本进来后能被裁剪、能标出处，模型看到的和用户上传的是同一份东西。
    attachments: list[dict] = Field(default_factory=list)


class KnowledgeIn(BaseModel):
    """Project spec/knowledge text (pasted or uploaded), searchable by the assistant."""

    text: str = Field(default="", max_length=2_000_000)


Priority = Literal["P0", "P1", "P2", "P3"]
CaseType = Literal["functional", "smoke", "regression", "acceptance", "negative"]
# 用例状态（2026-10-09 扩充）。
#
# `draft` 与 `blocked` 是这次为"从需规生成"加的两个状态，它们回答的是**不同**的问题：
#
#   draft   —— *还没人审过*。生成出来的东西一律先落这里。
#              它**不会**被"运行全部启用用例"选中（那个只收 active）。
#   blocked —— *审过了，但 agent 跑不了*。这是用户明确提出的需求：
#              "有些用例 agent 可能执行不了"。典型形态是它需要人工在别的系统里
#              先做一步、或者依赖一件现实中拿不到的东西。
#
# 为什么 `blocked` 不能简单用 `deprecated` 代替：`deprecated` 的语义是
# "这条用例不要了"，而 blocked 是"这条用例**有效**，只是当前执行不了"。
# 混在一起的话，看清单的人分不清"该删的"和"该想办法的"——
# 而这两者的后续动作完全相反。所以状态之外还要有 `blocked_reason`：
# 只标一个红点而不写原因，下个月没人知道它为什么被挡住。
CaseStatus = Literal["draft", "active", "deprecated", "blocked"]


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
    # 为什么这条用例跑不了（status=blocked 时必填，其余状态可空）。
    # 见 CaseStatus 的注释：只标状态不写原因，过一个月就没人知道它为什么被挡住了。
    blocked_reason: str = ""
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
    blocked_reason: str | None = None
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


class LlmProfileIn(BaseModel):
    """One LLM gateway in the pool. Ordered by array position: index 0 = most preferred.

    api_key is write-only (never echoed back) and OPTIONAL: blank/None means "keep the
    key currently stored at this same pool position" (the UI only ever shows masked
    keys, so editing an existing set must not force the user to retype it). A brand-new
    entry with no key yet at that position will fail validation server-side and be
    dropped, which is the safe behaviour."""

    name: str | None = None
    base_url: str = Field(min_length=1)
    api_key: str | None = None  # blank => inherit the key at this pool position
    model: str = Field(min_length=1)
    agent_model: str | None = None
    # auto（默认，自动识别）/ openai / anthropic / google / ollama。
    # 也认 deepseek、qwen、kimi、zhipu、openrouter 等别名（归一化到 openai 兼容协议）。
    provider: str | None = None


class LlmRoleIn(BaseModel):
    """一个**用途**（agent 每步执行 / judge 判定 / narrative 失败叙述 / specgen 需规生成 /
    assistant 助手对话）的模型覆盖。

    为什么要有这一层：真库实测 agent 每步调用占全部 token 的约 95%，判定只占个位数
    百分比 —— "全平台用一个最贵的模型"等于把钱全花在量最大的地方，而真正需要强模型的
    是判定与生成这类质量敏感、量又小的环节。分开配才能一边省钱一边保住质量。

    所有字段留空 = 继承默认配置（同一个模型）。只换模型时填 model 就够；
    要换厂商才需要 base_url/api_key/provider。
    """

    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # 只写：留空表示保留已存钥匙（界面只显示掩码）
    provider: str | None = None


class LlmPremiumIn(BaseModel):
    """平台级「高端模型」（贵、聪明，**可选**）。整体替换语义：配置页保存的就是全部。

    2026-10-12 起：高端模型**不再自己存连接信息**，而是从模型池里选一套
    （`pool_name` 指向池里某个条目的 name）。所以这里只有 enabled / pool_name / roles
    三个语义位——"开不开 + 选池里哪套 + 管哪几件事"。所有网关/钥匙/协议只在模型池里填。

    roles 里不认识的名字会被服务端丢掉（按 app.llm.PREMIUM_ROLES 白名单过滤）。
    """

    enabled: bool = False
    # 指向模型池里某个条目的 name；空 = 没选（等同于没配）。
    pool_name: str = ""
    roles: list[str] = Field(default_factory=list)


class LlmTestIn(BaseModel):
    """连通性检测的可选过滤：只测指定名字的模型。

    不传 / 空列表 = 测全部（模型池全部条目 + 任何逐用途覆盖），与历史行为一致 ——
    「检测所有模型」按钮走这条。传了名字 = 只测这些，供设置页「每行一个探测按钮」
    精测单套；没找到的名字会以一条 ok=False 的结果如实回报，而不是静默跳过。
    """

    names: list[str] = Field(default_factory=list)


class LlmSettingsIn(BaseModel):
    # Non-secret fields: None => leave unchanged; "" => clear the override (env fallback).
    base_url: str | None = None
    model: str | None = None
    agent_model: str | None = None
    # 协议：auto / openai / anthropic / google / ollama（见 app/llm_protocol.py）
    provider: str | None = None
    # Secret: blank/None => keep the stored key; non-empty => replace.
    api_key: str | None = None
    # The ordered pool. When provided, it fully replaces the pool (None => keep current).
    pool: list[LlmProfileIn] | None = None
    # 按用途分模型。给定时**整体替换**（None => 保持现状）；传空 dict 即全部恢复默认。
    roles: dict[str, LlmRoleIn] | None = None
    # 两级判定开关：None => 不动；True/False => 开/关（存在 AppSetting，立即生效）。
    judge_two_tier: bool | None = None
    # 平台级高端模型：给定时**整体替换**（None => 保持现状）。
    premium: LlmPremiumIn | None = None


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
