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


class ProjectGroup(Base):
    """「大系统」容器（2026-10-13 用户要求）—— 把若干小项目/模块挂在一个大系统下面。

    ## 为什么要有它

    用户的系统是**一个很大的系统**（民航培训管理平台），而现在每个模块
    （培训资源管理 / 飞行学员管理 / 考核单管理…）都是**一个独立项目**。
    平铺在一个侧栏里既看不出从属关系，也逼着每个模块**各传一遍同一份系统级需规**。

    所以加这一层（**不是**父子项目树：大系统本身不是一个能跑用例的项目）：
      * 项目可归属一个「大系统」（`Project.group_id`）；
      * 大系统自己可放**共享的需规/资料**（`KnowledgeChunk.group_id`），
        子项目检索时**自己的块 + 所属大系统的块一起用** —— 上层传一次、下面各模块都可用。
    """

    __tablename__ = "project_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # ★ 传进「共享需规」的那些**原件**清单（2026-10-17 用户要求「同时也要可以查看到原格式的文件」）。
    #
    # 共享需规本身是一整份合并后的文本（`KnowledgeChunk.group_id`），**没有**"每份文件一行"的落点，
    # 所以原件清单挂在这里。每项：{name, url, key, bytes, chars, format, at}。
    # 说明：清单是**溯源用的**，不是"删掉就等于删掉内容"—— 正文仍以合并后的文本为准，
    # 要删内容直接改文本；这里只回答"这些规则最初来自哪几份文件"。
    knowledge_files: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 所属「大系统」（2026-10-13）。NULL = 不属于任何大系统（侧栏里平铺显示）。
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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
    # ★ 2026-10-09 用例编号的**永久计数器**（每个项目一份）。
    #
    # 为什么要单独存一列：之前 `_next_case_key` 靠"查现有用例的最大编号 +1"算下一个号。
    # 那套在"删掉的恰好是最大编号"时会**把号重新发出去**：TC-001..TC-005 里删掉 TC-005，
    # 下一条新建的又拿到 TC-005。用户看到的是"我删了的那条又回来了"，而报告/缺陷单里
    # 两条完全不同的用例共用一个编号。
    # 根因是"可用编号"被当成从当前数据推导出来的东西 —— 它其实是**发行过就不回收**的事实，
    # 必须自己存下来。计数器只增不减，删用例不动它。
    #
    # 0 表示"还没有计数器"：老库（这一列是后加的）第一次发号时会从现有用例的最大编号
    # 回填一次，见 app/api.py::_next_case_key。新项目默认 0，所以第一条就是 TC-001。
    case_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # storage_state JSON for logged-in sessions; encrypt before persisting in prod
    login_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseProposal(Base):
    """AI 对测试用例的优化提案（2026-10-12）—— **先审后改，绝不直接动用例库**。

    ## 为什么是"提案"而不是"直接改"

    用户的原话：「我希望 ai 能根据这个页面去优化测试用例和增加测试用例
    同删除不必要的测试用例」。但他同时选了"先出提案，我逐条审阅后再入库"。

    这个选择是对的，因为用例库是这个系统真正的资产：
    一次静默的误删/误改会让"通过率"从有依据变成没依据，而报告上一个异常都不会有。
    所以 AI 只能**写提案**，只有人点"采纳"才会落到 `test_case`。

    ## 三种动作与各自的落地方式

    * `add`    —— 生成新用例（用 `draft` 存整份草稿，采纳时才插入）。
    * `update` —— 修改已有用例。`payload` 只放**要改的字段**；
                  采纳时按字段合并，而不是整条覆盖 —— AI 只该改它看见的那几处。
    * `remove` —— 删除。采纳时**不真删**，改成 `status='deprecated'`：
                  删掉的用例编号会从报告里消失，而真删是不可逆的。

    ## 为什么存 `evidence`

    用户会问"它凭什么说这条多余"。答案必须在提案里看得到（哪一节需规、
    关联的哪几条现存用例），否则这就是一段看起来很像真的建议 ——
    和它要解决的问题一样不可信。
    """

    __tablename__ = "case_proposal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # add | update | remove
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    # 受影响的项目内用例编号（add 时为 None）
    case_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 现存用例的 id（add 时为 None）。不加外键约束：用例可能在审阅期间被别处删掉，
    # 而提案不该因此消失或让删除失败 —— 那种情况下界面上显示"原用例已不存在"即可。
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 依据（需规章节标题 / 关联用例编号），逐条一条
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    # add/update 的草稿字段；remove 时为空
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # ★ 这条提案涉及的**流程图**（Mermaid 源码，2026-10-12 用户要求
    # 「用例优化里面也希望可以加上相关流程图告诉我为啥要加这个用例」）。
    #
    # 存**按模块**生成的那一张，而不是每条提案各画一张：
    #   * 同一模块里的一条用例关心的流程是同一张（"审批链怎么走"）；
    #   * 每条都画一次等于把同一个调用付十几遍钱，而且图会各说各话、
    #     用户没法把它们放在一起看。
    # 所以它是模块级的图，复制到该模块的每条提案上，界面上挂在"依据与改动内容"里。
    mermaid: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # ★ 这条用例应当用哪个**账号/角色**跑（2026-10-12 用户要求「附上账号」）。
    #
    # 取值必须是项目里真实存在的 credential.role —— 编一个不存在的角色，
    # 用例跑起来会因为"找不到账号"直接失败，而用户看不出是提案的错。
    # 空串 = 没指定（沿用项目默认账号）。
    role: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # pending | accepted | rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    # 采纳后落成的用例 id（add/update），便于回溯"这条提案变成了哪条用例"
    applied_case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_case_proposal_project_status", "project_id", "status"),
    )


class ProjectDoc(Base):
    """项目文档：需求分析 / 流程图 / 总览（2026-10-12）。

    ## 为什么需要它

    用户的原话：「我希望你加上一栏，里面放上需求分析或者该项目的总览……
    里面要有该项目的需规和流程图……最好是和我上传的大需规一样，但是你那个要是
    AI 分析并截取过的，因为我上传的大需规可能不止这一个模块」。

    也就是说：知识库（`KnowledgeChunk`）里躺着**整份原件**（46 万字、586 个标题），
    那是给检索用的；而人需要一个**读得懂的概览** —— 这个项目的模块有哪些、
    流程怎么走、角色怎么分、关键规则是什么。

    ## 为什么正文存 HTML 而不是 Markdown

    用户明确要求「页面有很多格式可以编辑，包括字体、照片、字体颜色大小等等」，
    所以用富文本编辑器（TipTap）。它最小的持久化格式就是 HTML。

    存 HTML 的取舍要说清楚：
      * 好处：编辑器直接吃、直接吐，格式零损失（颜色/字号/对齐/表格/图片都在标签里）；
      * 代价：渲染时**必须**净化，否则用户粘贴进来的 `<script>` 会变成存储型 XSS。
        见 `app/api.py::_sanitize_doc_html` —— 那是这条路上唯一的防线，
        改动本模块时不要绕过它。
    """

    __tablename__ = "project_doc"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # "overview"（项目总览）/ "analysis"（需求分析）/ "flow"（流程图）/ "custom"（自己加的）
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="custom")
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # 富文本正文（HTML）。**渲染前必须净化**，见类注释。
    html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # AI 生成时记下它依据了哪些章节 —— 用户据此核对，而不是把 AI 的话当结论。
    sources: Mapped[list] = mapped_column(JSON, default=list)
    # "ai" = AI 生成后未经人工修改；"human" = 人编辑过；"" = 空文档
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    # ★ 助手能不能改这份文档（2026-10-12 用户要求）。
    #
    # 用户原话：「ai 可以阅读那里面所有文档，但是有些文档 ai 无法修改，
    # 我上传文档的时候可以加个选择，也就是只有我自己能修改」。
    #
    # 默认 True（可改）—— 项目文档的主要用法就是"助手起稿、人改"。
    # 而上传的**参考文档**（客户给的说明书、别人写的需规）常常不该被 AI 动：
    # 那是外部输入，改了就对不上原件了。
    #
    # ★ 存成**数据**而不是按 kind 推断。上一版我把「flow 不让 AI 写」硬编码在
    # 代码里，那是把权限藏进了实现：用户想放开流程图时改不了，
    # 每加一种文档也要回去改一处判断。现在由这一列说了算，用户自己勾 ——
    # 读的权限不受影响（助手永远能读全部）。
    ai_editable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    # ★ 上传的原件（2026-10-16 用户要求「我想全部看到原件」）。
    #
    # 之前上传只留**解析出的文本**，原件直接丢 —— 于是 PDF 的版面、docx 的标题层级、
    # 图片、表格全都不可逆地没了，用户连"看回原件"的退路都没有。
    # 现在把原件存进 artifacts（storage.upload），并记下这几样：
    #   source_key   —— 存储键。服务端靠它把原件**读回来**（让助手也能看原件）。
    #   source_url   —— 浏览器可打开的地址（本地模式 = /artifacts/<key>）。
    #   source_name  —— 原始文件名（含扩展名），用来告诉用户"这是原件"。
    #   source_bytes —— 原件大小，界面上显示。
    # 全部可空：这一列上线前导入的文档没有原件，不是错误状态。
    source_key: Mapped[str | None] = mapped_column(String(400), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ★ 上次**解析产物**的指纹（2026-10-16）。
    #
    # 用来回答一个问题：这份正文还是解析出来的那一份，还是**人已经改过**了？
    # 重传同一个文件时靠它决定要不要覆盖 —— 不看指纹的话，用户手工改过的正文
    # 会被新解析结果**静默抹掉**，而他只是想把文件重传一遍。
    # （不能用 origin == "human" 判断：首次上传写进去的就是 "human"。）
    html_parse_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # 查询索引（**不是唯一约束**）。"一个项目一种 kind 只有一份"由 API 层保证
    # （`upsert_doc` 按 (project_id, kind) 找现有行再更新），不在这里做唯一约束 ——
    # 因为 "custom" 允许用户加任意多篇（靠标题区分），
    # 那种情况下真正该唯一的是 (project_id, kind, title)，多一个漂移点不值得。
    __table_args__ = (
        Index("ix_project_doc_kind", "project_id", "kind"),
    )


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
    # draft|active|deprecated|blocked —— 见 app/schemas.py::CaseStatus 的注释，
    # 那里解释了为什么 blocked 必须与 deprecated 分开。
    status: Mapped[str] = mapped_column(String(20), default="active")
    # 为什么这条用例跑不了（status=blocked 时必填）。
    #
    # ★ 用户明确提出的需求：「用例也加上人可以变更状态的功能，因为有些用例 agent
    #   可能执行不了」。只给一个状态枚举是不够的 —— 挡住的原因（需要人工在别的系统
    #   先做一步 / 依赖拿不到的数据 / 需要特定时段）才是下一个人接手时要看的东西。
    #   没有它，一个月后没人知道这条用例为什么是 blocked，只能重新试一遍。
    blocked_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
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
    # ★ 2026-10-09 来源溯源："用例是谁写的"。
    #
    # 为什么必须单独存一列：现在用例有两个来源 —— 人写的，和"从需规文档生成"的。
    # 两者混在一起之后**没法再分开**：
    #   * 生成出来的用例质量参差，审计时要能按来源筛选、批量回退；
    #   * 生成质量问题（例如把"建议支持"写成了断言）复盘时，需要知道这批到底有多少条
    #     来自生成、又是从哪份文档的哪一节来的，否则只能靠记忆猜。
    # 格式刻意保持短（跟着 1200+ 条结果落库）："" 或 "manual" = 人写的；
    # "spec:<文档名>#<章节>" = 从某份需规的具体章节生成。
    source: Mapped[str | None] = mapped_column(String(300), nullable=True, default="")
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
    # 2026-10-08 per-run 重试次数。此前只有全局 `.env` 的 CASE_RETRIES=0 一刀切：
    # 想给某一轮开重试就得改配置并重启，而且一开就是全部轮次一起开。
    # **NULL = 沿用全局 case_retries**（不是 0）—— 否则一条没显式指定的新 run 会带着
    # 默认 0 把用户设的全局值顶掉，"我明明开了重试怎么没生效"就查不出来了。
    # 显式填 0 才表示"这一轮就是不重试"。取值语义见 engine.should_retry。
    retries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 2026-10-07 需规漂移信号（app/spec_drift.py 的 DriftSignal.as_dict 列表）。
    # 为什么挂在 run 上：它是**这次执行**的结论，不是某条用例的属性。
    # NULL = 没跑过漂移检查（功能上线前），报告侧要显示「未检查」而不是「无漂移」。
    drift_signals: Mapped[list | None] = mapped_column(JSON, nullable=True)
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
    # ★ 失败归因：谁造成了这次失败。取值见 app/failure_attrib.py 的 classify()
    #   （system / setup / transient / agent / unknown）。
    #
    # 与上面 root_cause 的分工，混淆了就等于白加：
    #   root_cause  —— 判定器给的，"用例为什么判失败"；
    #   attribution —— 文本证据算的，"这次失败是谁的锅"。
    # 两条实测假缺陷（列表读到渲染中间态、导入未提交审批）的 root_cause 都长得像
    # 正常的"功能不符"，因为判定器只能看到 agent 的说法，而说错的是 agent。
    # 归因看的是步骤证据本身。报告要用它把"可上报缺陷"和"我们的执行问题"分开。
    # NULL = 未计算（历史行 / 关闭了叙述功能）。
    attribution: Mapped[str | None] = mapped_column(String(30), nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AI-written bug description for FAILED cases:
    # {steps, actual, expected, title, severity} — see app/failure_narrative.py.
    # NULL for passed cases (no bug report needed) and when generation failed.
    failure_narrative: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # which account/role this case actually ran as (multi-account audit), e.g. "approver · 主管A"
    account_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # 2026-10-08 耗时分解。executor 早就在每条用例结束时算好 5 段墙钟并打成一条
    # WARNING（见 executor.execute_case 末尾），但**只进日志不进库** —— 结果是
    # "这轮为什么慢"只能在日志里翻，报告页看不到、也没法按阶段汇总。
    # 形状：{"browser_up_ms","agent_ms","wrap_up_ms","video_ms","judge_ms",
    #        "total_ms","steps","per_step_ms"}。NULL = 埋点上线前的历史行。
    # 为什么是 JSON 而不是 5 个 int 列：这套分段以后还会调（比如把 agent 拆成
    # 登录/首屏/步骤），加列要动 _ADDITIVE_COLUMNS 且每个消费点都要改；
    # 一个 JSON 里加 key 不用迁移。
    timing: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
    # 所属「大系统」的共享知识块（2026-10-13）。
    #
    # ★ 两种块的区分：
    #   * 项目自己的块：project_id=<pid>，group_id=NULL；
    #   * 大系统共享的块：project_id=0（哨兵，没有这个项目）+ group_id=<gid>。
    #     为什么用 0 而不是让 project_id 可空：它是 NOT NULL，而 SQLite 改一列的
    #     NOT NULL 要**重建整张表**（knowledge_chunk 可能几十万行）。0 不会被任何项目
    #     用到（id 从 1 开始），SQLite 默认也不强制外键，所以安全。
    # 检索某项目时的有效块 = 自己的 + `group_id == 它所属大系统` 的（见 knowledge.py）。
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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


class CaseChange(Base):
    """用例改动审计（2026-10-06）。

    为什么必须有这张表：失败清单（`/projects/{id}/failure-digest`）是**实时算出来的
    派生视图** —— 用例一旦通过就自动从清单里消失，不需要人工维护。
    但一旦允许助手自动改用例（用户明确要求"都要"），就会产生一个新问题：
    **改完之后没人说得清改了什么**。没有留痕的话，清单条目消失可能是
    「用例真的修好了」，也可能是「助手把断言改宽松了让它通过」——
    后者是**假通过**，比原来的假失败更难发现。

    所以这张表只记一件事：**谁在什么时候把哪个字段从什么改成了什么**。
    不存 diff 全文（那样很快会变成没人读的日志），只存被改字段 + 前后值截断。

    刻意不与 run_result 关联：改用例和跑用例是两件事，
    一次改动可能对应 0 次或多次运行。
    """

    __tablename__ = "case_change"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True, nullable=False
    )
    case_id: Mapped[int] = mapped_column(
        ForeignKey("test_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # field / before / after —— 一行只记一个字段。
    # 批量改一条用例的 5 个字段 = 5 行：这样"改了哪些"一目了然，
    # 也不会把 before/after 塞成一大段 JSON 变得没法读。
    field: Mapped[str] = mapped_column(String(60), nullable=False)
    before: Mapped[str] = mapped_column(Text, nullable=False, default="")
    after: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # "assistant" | "human" | "import" —— 助手自动改的必须能单独筛出来。
    # 用户明确要求「都要」：既让助手能改，也要能一眼看出哪些是它改的。
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="human")
    # 这次改动对应清单里的哪条（signal 键），便于从清单反查改动。
    digest_signal: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    by_label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_case_change_proj_idx", "project_id", "id"),)


class ApiEndpoint(Base):
    """从**页面实际发出的流量**里学出来的后端接口（2026-10-09）。

    ## 为什么需要它

    用户要求系统有"探索后端"的能力：不少内部管理系统的**菜单本身就是后端给的**
    （前端拿 `/api/menu` 渲染侧边栏），而"进了列表页看到数据"其实是
    `GET /api/xxx/list` 的返回。只会点界面的话，这两件事都只能靠猜 ——
    猜错就会写出"点了菜单但不知道数据从哪来"的用例。

    ## 为什么是"观察"而不是"扫描"

    页面为了渲染必然要调后端，那些请求就是权威答案：不用猜路径、不用撞目录，
    也不会朝服务器发它没被设计来处理的请求（主动扫描在真实环境里是有风险的）。
    实现挂在 `app/browser_hygiene.py` 已有的 CDP 请求拦截上，不新增开销。

    ## 安全：这张表里**不含任何敏感值**

    `query` 只存"参数名 → 值的形状"（`page` → `int`），请求头一律丢弃，
    请求体只取顶层字段名。所以这张表即使被导出来也不会泄漏 token 或业务数据。
    `writable=True` 的接口只被**登记**，重放层（app/api_probe.py）默认拒绝它们 ——
    探索这个动作本身绝不允许改数据。
    """

    __tablename__ = "api_endpoint"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 归一化后的路径（具体 ID 换成 {id}），与 method 一起构成"同一个接口"的身份
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET")
    # 原始样例 URL（截断）。保留一条是为了让人能核对归一化有没有做错 ——
    # 把 `/api/user/1024` 归一成 `/api/user/{id}` 是对的，归一错了就查不出来。
    sample: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # {"参数名": "形状"}。**只有形状，没有值**，见类注释。
    query: Mapped[dict] = mapped_column(JSON, default=dict)
    # 请求体顶层字段名（写接口才有）
    body_fields: Mapped[list] = mapped_column(JSON, default=list)
    # 被观察到的次数：多说明它是核心接口
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # True = 写操作。登记但默认拒绝重放。
    writable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 从哪个页面观察到的（Origin）—— 一个项目可能同时访问多个后端
    origin: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # (project, method, path) 唯一：重复观察到同一个接口时累加 hits，
    # 而不是插一行新的 —— 否则清单里会出现几十条一模一样的接口。
    __table_args__ = (
        Index("ix_api_endpoint_uniq", "project_id", "method", "path", unique=True),
    )
