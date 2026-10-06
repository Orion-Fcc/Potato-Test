"""ORM models: Project -> TestCase -> RunResult >- Run.

Mirrors the sample-pool -> run -> result shape (see design doc §5.3):
TestCase is the reusable pool, Run is one batch execution, RunResult is one
row per (run, case).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # GitLab project this project's issues sync with (numeric id or "group/path"); the
    # access token lives in an encrypted Credential (type="gitlab_token").
    gitlab_project: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # Feishu group chat bound to this project: the bot routes runs/pushes results
    # here, and problems raised in this chat attach to this project (no LLM guess).
    feishu_chat_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Feishu Bitable (多维表格) this project mirrors its feedback into (one table
    # per project). Parsed from the pasted base URL: app_token + table_id.
    feishu_bitable_app_token: Mapped[str | None] = mapped_column(String(120), nullable=True)
    feishu_bitable_table_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # per-project run tuning; None => fall back to the server defaults
    case_timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    case_max_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 证据采集开关，做成 per-project 让用户自己选（原来只能在 .env 里改、还要重启）：
    #   case_record_video  None=跟随全局默认 / True=录 / False=不录（录像是每帧编码，最贵的一项）
    #   live_shot_every    None=跟随全局默认 / N>0=每 N 步截一张 / 0=完全不截逐步图
    # 注意：无论怎么设，用例结束时那张「最终截图」都会拍（走独立的 take_screenshot），
    # 失败用例不会变成没有图可看。
    case_record_video: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    live_shot_every: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # the project's role vocabulary for multi-account auth (e.g. ["requester","approver"])
    roles: Mapped[list] = mapped_column(JSON, default=list)
    # storage_state JSON for logged-in sessions; encrypt before persisting in prod
    login_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Environment(Base):
    """A target environment for a project (dev/test/prod): its own base_url and accounts.
    A run picks an environment; base_url + (env,role)->account resolve from it."""

    __tablename__ = "environment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TestCase(Base):
    __tablename__ = "test_case"

    # 2026-10-04 项目隔离：用例编号只在项目内唯一，跨项目可以重名
    # （每个项目都从 TC-001 开始）。这条约束把"项目内不许撞号"从约定变成
    # 数据库强制——之前 `_next_case_key` 用 COUNT 计算，删过用例就会发出重号，
    # 两条不同用例拿到同一个 TC-005，报告里无法区分。
    # 注意是 (project_id, case_key) 复合唯一，不是 case_key 单列唯一：
    # 单列唯一会让项目 2 建不了自己的 TC-001。
    __table_args__ = (
        UniqueConstraint("project_id", "case_key", name="uq_testcase_project_casekey"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    # readable id (e.g. TC-001), auto-assigned per project on create
    case_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    module: Mapped[str | None] = mapped_column(String(200), nullable=True)  # section / grouping
    priority: Mapped[str] = mapped_column(String(10), default="P2")  # P0|P1|P2|P3
    type: Mapped[str] = mapped_column(
        String(20), default="functional"
    )  # functional|smoke|regression|acceptance|negative
    status: Mapped[str] = mapped_column(String(20), default="active")  # draft|active|deprecated
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # role this case executes as (multi-account auth); null => project default account
    role: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # 2026-10-04 多角色：这条用例的流程中会依次用到哪些角色，按顺序。
    # 例 roles=["applicant", "approver"] = 先以申请人身份提交，再切到审核人处理。
    #
    # 为什么保留 role 单值列不动：378 条存量用例都带着它，删列等于逼所有人重录。
    # 两者的关系是「roles 是超集」：
    #   - roles 为空/null  -> 执行层回落到 role（老用例走这条，行为完全不变）
    #   - roles 非空       -> 用 roles，role 仅作为「第一个角色」的兼容镜像保留
    # 归一化在 app/schemas.py 的 _roles_of() 里做，写入前就把两者对齐，
    # 所以读出来的数据永远自洽，不依赖「哪个优先」这种隐式规则。
    roles: Mapped[list | None] = mapped_column(JSON, nullable=True)
    references: Mapped[str] = mapped_column(Text, default="")  # linked requirements / tickets
    preconditions: Mapped[str] = mapped_column(Text, default="")
    # the natural-language task the browser agent actually executes
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # structured steps kept as human documentation: [{"action": str, "expected": str}]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    test_data: Mapped[str] = mapped_column(Text, default="")

    # ★ 2026-10-06 测试数据文件声明（JSON）：{"files":[{"name","kind","sheets"/"content"}]}。
    # 执行前由 app/testdata.py 物化成真实文件，agent 用 upload_file 传给被测系统
    # —— 有些用例是"先导入一份 Excel，再验证导入结果"。
    #
    # 为什么单开一列而不复用 test_data：那一列已经有 181 条存量用例在写自由文本
    # （"名称=SIT手测-场地审批；使用范围=…"），塞结构化声明进去会把两种语义搅在一起，
    # 存量内容也会被当成声明解析。
    data_files: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 数据隔离提示（可选）。原样拼进 agent 的任务提示，见 app/data_hygiene.py。
    # 存在的原因：实测这批用例里 12 条的 expected 写死了"1 行""2 条"这类绝对数字，
    # 而新增类用例跑完不清理 —— 于是它们**第一次对、第二次必然假失败**
    # （自己上次留下的数据污染了自己的前置条件）。
    # 留空 = 不注入任何提示，避免给无关用例塞噪音。
    data_hygiene: Mapped[str] = mapped_column(Text, default="")
    expected: Mapped[str] = mapped_column(Text, nullable=False, default="")
    start_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # included in "run all enabled"
    # ── 操作经验记忆（见 app/case_memory.py）──────────────────────────────────
    # 存的是**过程知识**：这条用例上次是怎么走到目标的（导航路径）、界面上哪些元素
    # 有什么脾气（加载慢、按钮相邻易点错）。用来减少每次重新摸索，让同一条用例的
    # 运行路径趋于稳定。
    #
    # **绝对不存判定结果**（通过/失败/是否符合预期）。理由：一旦把结论写进记忆，
    # 下次运行就变成"背答案"，测试也就失去意义了。这条由 case_memory 的字段白名单
    # 和单元测试共同保证，不是靠自觉。
    #
    # memory_fingerprint 是 case 内容（name/prompt/steps/expected/test_data）的哈希：
    # 用户改了用例 → 指纹对不上 → **整条记忆作废**，避免拿旧经验误导新用例。
    memory: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    memory_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    memory_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Run(Base):
    __tablename__ = "run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    case_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending|running|completed|failed|cancelled
    concurrency: Mapped[int] = mapped_column(Integer, default=2)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # suite this run belongs to (nullable — ad-hoc runs have none), who actually ran it,
    # and how it was triggered.
    suite_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ran_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | suite
    environment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # target env
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TestSuite(Base):
    """A named, reusable selection of test cases with an owner and a default runner.
    Runs are executions of a suite; a suite carries the accountability + reminder cadence."""

    __tablename__ = "test_suite"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    selection_mode: Mapped[str] = mapped_column(String(10), default="cases")  # cases | tags
    case_ids: Mapped[list] = mapped_column(JSON, default=list)
    tag_filter: Mapped[list] = mapped_column(JSON, default=list)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    runner_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    cadence: Mapped[str] = mapped_column(
        String(10), default="none"
    )  # none|daily|weekly|biweekly|monthly
    environment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # default env
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Notification(Base):
    """An in-app notification for a user; `emailed` records whether the same event was
    also sent by email (so a resend/backfill doesn't double-mail)."""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    # assigned|reminder_due|reminder_overdue|run_done|issue_assigned|issue_fixed|verified
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(500), nullable=True)  # in-app route
    suite_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    emailed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class Issue(Base):
    """A bug/issue tracked on the Kanban board, usually opened from a failed result."""

    __tablename__ = "issue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(20), default="open"
    )  # open|in_progress|fixed|verified|closed
    severity: Mapped[str] = mapped_column(String(20), default="medium")  # low|medium|high|critical
    assignee: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )  # legacy display/GitLab
    assignee_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    labels: Mapped[list] = mapped_column(JSON, default=list)
    # provenance: the failing case/run/result this issue came from (nullable, no FK so
    # deleting a case/run doesn't cascade the issue away)
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # GitLab sync state: iid within the mapped GitLab project, a snapshot of that project
    # ref, the last time we reconciled, and a hash of the last-synced field set (so the
    # poller can tell whether the GitLab side actually changed).
    gitlab_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gitlab_project: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # Row id in the project's Feishu Bitable defect table (all issues mirror there).
    bitable_record_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class IssueComment(Base):
    __tablename__ = "issue_comment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issue.id"), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # set when this comment originated from (or was mirrored to) a GitLab note — dedupes
    # the poller so a pulled note isn't pushed back and vice-versa.
    gitlab_note_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FeedbackItem(Base):
    """A problem/question raised in a Feishu chat, collected by the bot.

    Every recorded message lands here first; bug/feature items are then promoted
    to an ``Issue`` (see app.feishu) with ``issue_id`` back-linking the promotion.
    ``feishu_message_id`` is unique so Feishu retries can't double-record.
    """

    __tablename__ = "feedback_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20), default="feishu")
    feishu_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    chat_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    chat_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # group/p2p
    sender_id: Mapped[str | None] = mapped_column(String(120), nullable=True)  # open_id
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(400), nullable=True)
    category: Mapped[str] = mapped_column(String(20), default="other")  # bug/question/feature/other
    severity: Mapped[str] = mapped_column(String(20), default="medium")  # low/medium/high/critical

    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open/resolved/ignored

    # Auto-identified project + the Issue it was promoted into (both nullable).
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    issue_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Record id in the project's Feishu Bitable (for status write-back). Null until synced.
    bitable_record_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Credential(Base):
    """A stored, encrypted login for a project's app-under-test.

    type=storage_state -> `secret` is an encrypted storage_state JSON (a captured,
      expiring browser session).
    type=password -> `secret` is an encrypted password for `username` (a dedicated
      robot/test account used for self-login).
    `secret` is always ciphertext (see app.crypto); it is never returned by the API.
    """

    __tablename__ = "credential"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # storage_state | password
    # role this account plays (multi-account auth); null = general/default account
    role: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # environment this account belongs to (null = usable in any environment)
    environment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    secret: Mapped[str] = mapped_column(Text, nullable=False)  # ciphertext
    # captured, reusable session bundle (encrypted JSON) for a password account, so cases
    # restore it instead of prompt-logging in every time. Captured on first use
    # (single-flight); re-captured reactively on auth failure (P3). Null until first capture.
    session_bundle: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # account health: flipped false when a run using it fails to log in; recover by a
    # manual re-check (server re-login) or a passing run.
    healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RunResult(Base):
    __tablename__ = "run_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id"), nullable=False, index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("test_case.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="error")  # passed|failed|error
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    flaky: Mapped[bool] = mapped_column(Boolean, default=False)  # passed only after a retry
    video_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    trace_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # per-step LLM diagnostics: [{i, action, thought, result, error, screenshot}]
    diagnostics: Mapped[list | None] = mapped_column(JSON, nullable=True)
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 2026-10-04 失败根因分类。取值见 app/judge.py 的 _ROOT_CAUSES。
    # 为什么要落库而不是只放在 judge_reason 文本里：报告要**按类分组**
    # （哪些该找开发、哪些是环境问题），而且要让"真缺陷率"能有独立的统计口径 ——
    # 混在自由文本里就只能靠关键词猜，猜不准。
    # 通过的用例为空串；判定器给不出分类时是 "unclear"。
    root_cause: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # 判定器引用了第几步作为依据（1-based）。落库是为了让"这条理由是否可核对"
    # 这件事在报告里可见 —— 空列表意味着判定器没说明依据，理由的可信度要打折。
    verdict_evidence: Mapped[list | None] = mapped_column(JSON, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AI-written bug description for FAILED cases:
    # {steps, actual, expected, title, severity} — see app/failure_narrative.py.
    # NULL for passed cases (no bug report needed) and when generation failed.
    failure_narrative: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # which account/role this case actually ran as (multi-account audit), e.g. "approver · 主管A"
    account_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # ---- 2026-10-06 人工改判 ----
    # AI 判定会不准（用户实测反馈），所以人必须能改。这里存的是"改判这件事本身"，
    # 而不是改后的结论（改后结论就落在 status 上，这样报表/统计/筛选全都自动生效，
    # 不用每个查询都判断一遍有没有改判）。
    # verdict_override 为 NULL 表示"没人改过，这就是 AI 的结论"。
    verdict_override: Mapped[str | None] = mapped_column(String(20), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    override_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 存字符串而不是 DateTime：这两个库（sqlite / postgres）在时区回环上行为不一致，
    # 而这只是一条审计痕迹，不需要参与计算 —— 见 bundle_is_fresh 里同样的取舍。
    override_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 改判前 AI 判的什么。撤销改判时靠它还原。
    original_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Heartbeat, not bookkeeping: a running case rewrites `diagnostics` after every browser
    # step, so this is the last sign of life from the worker driving it. That is what lets
    # reconcile_stale_runs tell a slow case from a case whose worker died.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ---- accounts / RBAC (Phase 1: tables only; enforcement flag-gated) ----
class User(Base):
    __tablename__ = "app_user"  # "user" is reserved in Postgres

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # None until set
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # system admin
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = has never been through (or dismissed) the guided first-run flow, so it
    # opens on their next sign-in. Existing rows are NULL, which is deliberate:
    # everyone sees it once.
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectMember(Base):
    __tablename__ = "project_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # owner|editor|viewer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Invite(Base):
    __tablename__ = "invite"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # optional project this invite grants membership to, with a role
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_role: Mapped[str] = mapped_column(String(20), default="editor")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # invite as system admin
    invited_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AppSetting(Base):
    """Admin-managed system settings (e.g. the global GitLab token). Values that are
    secret are stored encrypted (see app.crypto); `secret` marks those rows."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class KnowledgeChunk(Base):
    """One retrieval unit of a project's spec/knowledge document.

    Why this exists instead of the old single `proj_<pid>_knowledge` AppSetting string:
    a merged spec doc is easily 500k+ characters. Holding it in one text column meant
    every `search_knowledge` call re-split the whole thing and scored every paragraph
    in memory — cost grew linearly with document size, and the upload path had to cap
    the text at 400k chars to stay usable. Chunks make both bounded: retrieval only
    ever touches `limit` rows, and nothing is truncated on the way in.

    Chunks are split on blank lines (paragraph/table-row boundaries) with a soft size
    ceiling, so a chunk is roughly one meaningful block of the source document.
    `heading` carries the nearest preceding markdown heading, which is what makes a
    hit readable on its own ("§4.3 单位、属地和价格" beats "line 20241").
    """

    __tablename__ = "knowledge_chunk"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Position in the document, so hits can be shown in source order and a merge
    # (re-upload / paste) can replace a project's chunks deterministically.
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 1-based line number of the chunk's first line in the original document — kept so
    # the assistant can cite a location the operator can actually find.
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Nearest preceding markdown heading, truncated; "" when the doc has none.
    heading: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Provenance for re-upload/dedup: source filename when it came from a file.
    source: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_knowledge_chunk_proj_idx", "project_id", "chunk_index"),)
