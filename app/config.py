"""Settings loaded from environment / .env."""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("potato-test.config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./potato-test.db"

    gateway_base_url: str = "https://api.openai.com/v1"
    gateway_api_key: str = ""
    gateway_model: str = "gpt-4o"  # shared default; the judge always uses this
    agent_model: str = ""  # browser-use agent model; empty falls back to gateway_model
    gateway_verify_ssl: bool = True
    # Ignore HTTP(S)_PROXY / NO_PROXY from the environment when calling the gateway and
    # the loopback API. httpx defaults to trust_env=True, which means a proxy var that is
    # present at launch but dead later (a rotating sandbox proxy, a closed VPN client)
    # silently turns every LLM call into APIConnectionError: Connection error.
    # Set GATEWAY_IGNORE_PROXY=false if the gateway is genuinely only reachable via proxy.
    gateway_ignore_proxy: bool = True
    gateway_max_tokens: int = 16000  # completion cap for the browser agent's structured output
    agent_max_tokens: int = 0  # cap for the browser agent's per-step call; 0 = use gateway_max_tokens
    report_language: str = "Chinese (简体中文)"  # language for agent reasoning + judge reason

    # --- optional integrations (both OFF by default — a plain install needs neither) ---
    # Flip via ENABLE_GITLAB / ENABLE_FEISHU. Off hides the UI and 404s the endpoints.
    enable_gitlab: bool = False  # two-way GitLab issue sync
    enable_feishu: bool = False  # Feishu (Lark) bot + Bitable mirror

    # 监听地址固定为 127.0.0.1：这是给单人本机用的测试工具，且 AUTH_ENABLED=false，
    # 一旦绑 0.0.0.0，同网段（咖啡厅/公司 WiFi）任何人都能读到全部测试数据和报表。
    # 手机本来也打不开这个页面，开放出去只有风险没有收益，所以不提供开关。

    # Fernet key encrypting stored test credentials at rest (empty => credential API disabled).
    # 2026-10-04 改名收尾：这里原本还有一个旧项目名的兼容别名
    # （AliasChoices("POTATO_SECRET_KEY", "<旧名>_SECRET_KEY")），
    # 用户要求项目里不留旧名痕迹，已删除。
    # 影响面已确认：本机 .env 与 .env.example 用的都是 POTATO_SECRET_KEY，
    # 所以删掉不影响任何现存的 .env。
    # ⚠️ 但如果你在别处（另一台机器 / Docker / CI）还留着带旧键名的 .env，
    # 那份密钥会被**静默忽略** → 凭据接口自动关闭，已存的账号全部读不出来。
    # 迁移办法：把那个文件里的 <旧名>_SECRET_KEY 改名成 POTATO_SECRET_KEY 即可。
    secret_key: str = Field(default="", validation_alias=AliasChoices("POTATO_SECRET_KEY"))

    # Empty S3_ENDPOINT => store artifacts on local disk under ./artifacts
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "potato-test"
    s3_region: str = "us-east-1"

    # 2026-10-04 内网硬约束：全局并发上限压到 1。
    # 原来是 4（按内存算的：每个浏览器约 0.5 GB，16GB 机器最多开 4 个）。
    # 但用户明确说**内网只有 1 条通道**，并发不是被内存卡住的，是被网络卡住的。
    # 内网带宽不够时开多个浏览器不会更快：请求互相排队，每个都变慢，
    # 结果是"3 个用例同时超时"而不是"1 个用例跑完"。
    # 这里当硬上限用 —— 就算有人在项目设置里填了 2，引擎层也会兜到 1。
    default_concurrency: int = 1  # per-run worker count (in-process fallback only)
    # Global ceiling on concurrent browsers across ALL runs/users (each ~0.5 GB RAM).
    # Celery path: set the worker's --concurrency to this (each case == one browser).
    # In-process fallback: an asyncio.Semaphore(this) shared by all runs.
    max_global_concurrency: int = 1  # 内网通道只有 1 条，见上
    # Step budget is the PRIMARY control on a case, not wall clock: a step is one
    # think+act round-trip, and a simple case finishes in 8-15 of them while a long
    # wizard needs 30+. Running out of steps ends the case cleanly and reports how far
    # it got, which is far more actionable than a clock cutting it off mid-action.
    #
    # 2026-10-04 改：40 → 80。2026-10-06 再改：80 → 120。
    #
    # 用户明确要求"不要那个 160/40/1 了，直接告诉 agent 用最少步数和最快时间完成"。
    # 关键区分：**"最少步数"是给 agent 的目标，不是给系统的硬上限。**
    # 原来的 40 被当成预算用，长流程用例会跑到一半被砍断，报出来的失败是"步数用完"
    # 而不是真实缺陷 —— 用户说的"保证不了系统正常运行"就是这个。
    # 所以这里把数值放宽成"只拦真正卡死的用例"，速度压力改由提示词承担
    # （见 executor.py 的 _EFFICIENCY_RULE）。工程上仍必须有上限，
    # 否则一个卡在等待循环里的用例能占掉一整夜 —— 这是 C5「有界自主」的要求。
    #
    # 80 → 120 的依据（实测 run_result 的动作数分布，150 用例/177 次执行）：
    #   p50=22/ p90=54 / p95=65 / max=127，超过 80 的只有 4 条。
    #   即 80 从未被正常用例触达 → 保持 80 零收益，却有"砍半长用例"的风险
    #   （步数用完会被报成"失败"，而那不是真缺陷）。
    # 配套动作：.env 里 CASE_MAX_STEPS=120。**改这里没用，.env 会覆盖它** ——
    # 这是本项目的老坑（改运行参数先 grep .env，注意重复键取最后一次）。
    case_max_steps: int = 120
    # Wall clock is a SAFETY NET, not the budget. A time-based cap that does not scale
    # with the work is what produced the "every case times out" symptom: a 60s cap
    # aborts a case that legitimately needs 90s, then reports it as a failure the code
    # cannot fix. Default is RAISED automatically per step (see effective_case_timeout)
    # so the clock only ever fires on a genuine hang.
    #
    # 2026-10-04 改：从 160 提到 600。理由同上 —— 160 秒对多角色切换、
    # 长表单填报这类用例根本不够，中途被砍会被误判成缺陷。600 秒只用来兜住
    # "页面卡死/无限重试"这类异常，正常用例根本碰不到它。
    case_timeout_s: int = 600  # per-case wall-clock safety net; per-project override wins
    # 登录态捕获（capture_session）自己的预算 —— **必须比用例小得多**。
    #
    # 2026-10-05 新增。此前它直接复用 case_max_steps(当时 80) / case_timeout_s(600)，
    # 而登录本身正常只要 5 步左右；一旦凭据失效，agent 会开始**猜账号密码**
    # （实测日志里出现 admin/123456、role01/1234 等一轮轮试探，
    # 空动作事件累计 526 次），把 80 步和 600 秒全部烧光才报错。
    # 那段时间对用户表现为"卡在登录"，而且极难从日志看出是凭据问题还是输入机制问题。
    #
    # 15 步足够容纳：租户/用户名/密码三个字段 + 可能的验证码 + 首次登录强制改密。
    # 超了就快速失败，让调用方走回退路径 —— 而不是让 agent 无限试探。
    login_capture_max_steps: int = 15
    # 同步收紧墙钟：登录页本身要 2-6 秒解析，15 步给 180 秒已经很宽松。
    login_capture_timeout_s: int = 180
    # Per-project run concurrency.
    #
    # 2026-10-04 用户明确要求："并发数只能是 1，因为内网限制了"。
    # 这不是一个"默认值"，而是一条**硬约束**，所以要区分两件事：
    #   - 默认值 = 可以按项目调（比如 VRS 想开 2）
    #   - 硬上限 = 谁都不能超过（内网承受不了）
    # 现在整个内网只有 1 条可用通道，开 2 个并发浏览器不会更快，只会两边互相
    # 抢带宽、双双超时 —— 那不是提速，是把 1 个用例的等待变成 2 个用例的失败。
    # 所以把它当**上限**用：max_global_concurrency 也一起压到 1，
    # 即使有人在项目设置里填了更大的数字，引擎那层也兜住（见 max_global_concurrency）。
    run_concurrency: int = 1
    # Seconds added to the wall-clock net per step allowed. The net therefore tracks the
    # actual work the agent is permitted to do instead of being a fixed guess.
    per_step_timeout_s: int = 12
    # Above this ratio of the wall-clock net, a case is treated as "went the distance"
    # rather than an infra error: it gets ONE extra chance with a wider net instead of
    # being re-run into the same wall (the old retry loop's real failure mode).
    timeout_grace_ratio: float = 0.9
    case_retries: int = (
        0  # no auto-retry — a stuck case fails fast instead of burning 2x the timeout
    )
    # --- per-case speed knobs (do not touch the model; see executor.execute_case) ---
    # Screenshot upload cadence for the live view. 1 = capture EVERY step.
    #
    # Changed from 3 to 1 on request: a failure is usually explained by one specific step
    # (the error toast appears, the field goes disabled, the row is missing), and sampling
    # every 3rd step can miss exactly the frame that matters. The cost is one extra CDP
    # screenshot + artifact write per step; after the icon-sprite fix a step's fixed
    # overhead is ~1s, so this is no longer the bottleneck it was when it was set to 3.
    # A step's screenshot failing is best-effort and never fails the case.
    live_shot_every: int = 1
    # Playwright video encoding is pure CPU per frame. Off = faster, no replay video.
    case_record_video: bool = True
    # 录制规格。默认 1280x800@10fps；browser-use 的帧率默认 30 —— 逐帧编码是每个用例
    # 都固定付出的 CPU 成本，在没有并发可用的前提下（浏览器用例只能串行跑）
    # 这笔开销无法靠并发摊薄，只能从规格上降。回放只需看清操作步骤，足够。
    # 要高清回放就把这两项调回去。
    #
    # 注意：这里改的是**录像**尺寸，不动 viewport。下面那句"默认视口 = 屏幕分辨率"
    # 曾是对的、现在**过时了**：viewport 已改为 1434x825 与用户手动测试对齐
    # （见 browser_viewport_width，那是本项目最重要的一条对齐）。
    # 这条红线依然成立且更强：改 viewport 等于改变被测对象，
    # **绝不能为了提速去动它** —— 只允许"与用户手动环境对齐"这一个理由。
    video_width: int = 1280
    video_height: int = 800
    video_framerate: int = 10
    # ★ 跨用例复用浏览器进程 —— **默认关闭**，因为实测它做不干净、反而更慢。
    #
    # 初衷：`Browser.start()` 每个用例 6-7s（378 个用例 ≈ 45 分钟），看着像是可以省的钱。
    # 机制上 browser-use 确实提供了 `keep_alive`（设了之后 stop() 只发事件不杀进程），
    # 但**它和 Agent 的生命周期绑得太紧**，实测连续踩了四个坑：
    #   1. 关闭条件写 `not reused` → 新建那轮刚入池就被关掉，池里全是死对象
    #   2. `get_pages()/get_current_page()` 是协程，漏 await → 重置静默失败
    #   3. `agent.close()` 在 keep_alive 分支里会 stop 掉 event_bus 并把 event_queue 置 None
    #      → 复用时报 "Client is not started"，`start()` 也发不出事件
    #   4. 想重建 event_bus 救回来：`event_queue is None` 这个判据**实测不成立**（bubus 会把它补回来）
    # 结果是池里每个实例都判为不可用 → 丢弃重开 → 又因旧进程占着 profile，重开要 **50 多秒**，
    # 比不复用的 6 秒**更慢**（实测 case-timing: 启动浏览器 55.9s / 54.5s）。
    #
    # 结论：要真正复用，得接管 browser-use 的 session/事件总线生命周期，维护成本高于收益。
    # 代码保留（browser_reuse=True 可再试），但默认走"一用例一浏览器"的稳妥路径。
    browser_reuse: bool = False
    # ★ 用哪个浏览器跑用例。
    #
    # 背景：browser-use 默认拉一份自己的 Chromium（ms-playwright/chromium-*，本机实测
    # 启动 6-7s）。但本机本来就装了 Edge / Chrome —— 用自己的浏览器有两个好处：
    #   1. 少一份几百 MB 的重复下载与版本漂移风险；
    #   2. 真实用户就是用 Edge/Chrome 访问被测系统，同一渲染内核的结论更可信。
    #
    # 实测（本机 2026-10-06，browser-use 0.13.10 + 系统 Edge）：
    #   BrowserProfile(executable_path=<msedge.exe>, headless=True, args=["--no-proxy-server"])
    #   → 启动 1.4s，导航/DOM 读取/agent 决策全链路正常，比自带 Chromium 更快。
    #
    # 候选值：
    #   ""      = 自动探测（默认）：按下面 BROWSER_CANDIDATES 的顺序找第一个存在的
    #   "edge"  = 强制 Edge        "chrome" = 强制 Chrome        "bundled" = 强制内置 Chromium
    #   其它值  = 直接当成可执行文件的绝对路径（逃生口，指定公司内网定制浏览器）
    #
    # 传绝对路径时不校验存在性 —— 交由 Playwright 启动时报错，那条信息比我们自己
    # 编造的一句"找不到"要准得多（browser_reuse 那次教训：自己猜状态不如让底层说话）。
    browser_executable: str = ""
    # 自动探测的候选顺序，逗号分隔（**不用 tuple**：pydantic-settings 对 .env 里的
    # 复杂类型要按 JSON 解析，写成 `a,b` 会直接校验失败；逗号串是用户能一眼看懂、
    # 一眼就能改的写法）。Edge 排第一：Windows 自带、装机率最高、实测启动最快。
    # 想改成"永远用 Chrome"就写 BROWSER_CANDIDATES=chrome,edge。
    browser_candidates: str = "edge,chrome"
    # ★ 页面视口（CSS 像素）—— 让 agent 看到的页面大小和你自己手动测时**一致**。
    #
    # 背景（本机 2026-10-06 实测）：
    #   屏幕物理 2880×1800，Windows 缩放 200%（DPI 192）。
    #   browser-use 的 headless 分支会把 `viewport` 直接设成 `screen`，
    #   而 screeninfo 返回的是**物理像素** → 视口 = 2880×1800 CSS 像素。
    #   你自己用 Edge（最大化）测的时候，页面真实 CSS 视口只有 **1434×825**
    #   （实测 innerWidth/innerHeight，dpr=2）。
    #   → 两者宽高都差约 2 倍。响应式布局（el-row/el-col 断点、表格列折叠、
    #     侧边栏是否收起、按钮是否换行）在 2880 宽下和 1434 宽下**不是同一个页面**，
    #     "我这儿复现不了"和"元素找不到"都会从这一步长出来。
    #
    # 为什么默认是 1434×825 而不是"窗口尺寸"：
    #   2906×1730 是**窗口外框**（含标签栏/地址栏/书签栏），换算成 CSS 要除以 dpr=2
    #   再扣掉浏览器 chrome 高度。这套换算随 Edge 版本、用户是否显示书签栏、
    #   缩放比例而变，写死会悄悄漂。直接量 innerWidth/innerHeight 是唯一可靠来源，
    #   而"你手动测时的视口"就是这个值。
    #
    # 想临时改回老行为（不推荐，会与手动测试环境不一致）：两个都设 0。
    # 0 = 交给 browser-use 自己决定（headless 下等于 screen = 2880×1800）。
    #
    # ★ 注意这与"提速"无关：视口大小不改变任何等待/重试/步数策略。
    #   它只让"被测对象"和你手动测的那个页面严格同构。
    #   副作用是页面变窄 → 更多纵向滚动、每次采集的可见元素略少，
    #   这是**忠实还原**而不是退化。
    browser_viewport_width: int = 1434
    browser_viewport_height: int = 825
    # Trace capture adds a second recording stream; off = faster, no timeline trace.
    case_record_trace: bool = True
    # browser-use draws an index badge on every interactive element each step. On a
    # heavy SPA that is an extra full-page DOM pass + repaint per step. The a11y tree
    # already numbers the elements the model acts on, so this is usually pure overhead.
    browser_highlight_elements: bool = False
    # ★ THE per-step speed lever. The app under test injects a hidden inline SVG icon
    # sprite (<svg id="__svg__icons__dom__">) that carries ~33k decorative <path>
    # elements — on the login page, ONE unused symbol was 32,609 of them. browser-use
    # serialises the DOM every step, so it walked all 33k nodes each time.
    # Measured on 192.0.2.10:8600: get_browser_state_summary() 20.7–27.3s with the
    # sprite, 0.80–0.93s with the unused symbols removed — same 29 interactive elements
    # and same 853-char prompt either way. Set false only to A/B this yourself.
    browser_prune_icon_sprite: bool = True
    # ★ 身份守卫：用例开始前核对浏览器里**当前登录的人**是不是本用例该用的角色账号。
    #
    # 为什么必须有：持久 profile（跨用例复用 cookies/localStorage）里存的是**上一个
    # 登录者**的会话。实测 2026-10-06 的现场 —— 飞行学员管理（项目 2）的 profile
    # 里躺着 `localStorage.user = {"nickname":"系统管理员","username":"admin"}`、
    # `loginForm = {"tenantName":"示例培训","username":"admin","rememberMe":true}`
    # 以及 admin 的 ACCESS_TOKEN/REFRESH_TOKEN。于是：
    #   · 访问 /login **直接 302 到 /index**（已是登录态），agent 根本没机会填账号；
    #   · 179 条用例全部以 admin 身份跑完，界面右上角一直显示「系统管理员」；
    #   · 即便走登录页，登录框也被「记住密码」预填成 admin。
    # 表面上看每条用例都"跑通了"，实际测的是 admin 的菜单与数据权限 —— 结论全是假的。
    #
    # 守卫做的事：读 localStorage 里的当前身份，与本用例的角色账号比对；不一致就
    # 清掉该站点的登录态与"记住密码"，让 agent 用正确的账号重新登录。
    # 只在**能给定期望账号**时生效（角色账号 / 默认口令账号），纯 storage_state
    # 账号没有用户名可比，自动跳过。关掉它等于接受"用例可能跑在别人的身份下"。
    browser_identity_guard: bool = True
    # --- browser-use 自身能力的开关（不改模型，只少发调用 / 少发 token） ---
    # browser-use 自带一个 judge：agent 判定"完成"时会额外调一次 LLM，并把最多 10 张
    # 截图附进去（源码 _judge_trace → judge_llm.ainvoke）。它的结论**不会**覆盖 agent
    # 的自述成功，而我们有自己的 app/judge.py 做判定 —— 所以对我们是纯粹重复的一次调用。
    # 关掉它：每个正常结束的用例少一次 LLM 往返。
    agent_use_judge: bool = False
    # 单次 LLM 调用的墙钟上限（秒）。browser-use 默认 None = 不限制，一次卡住的调用会
    # 一直占着用例预算；step_timeout 管的是"整步"，管不住单次调用内部的挂起。
    llm_timeout_s: int = 90
    # ★ 传输层 LLM 重试次数（OpenAI SDK 的 max_retries）。
    #
    # 2026-10-06 实测：一条只跑 3 步的用例，判定阶段耗掉 10.6s，日志里是 **8 次连续
    # 429**（网关回"您已达到免费用户的 API 速率限制"）。两次都来自盲目重试：
    #   · 判定器 app/llm.py::openai_client —— SDK 默认 max_retries=2
    #   · agent 侧 ChatOpenAI —— browser-use 自己抬到 **5**（源码注释写着
    #     "Increase default retries for automation reliability"）
    #
    # 重试对"偶发网络抖动"是对的，但对"配额耗尽"是错的：后者是**必然失败**的状态，
    # 重试只是把一次失败拖成 5 次指数退避，时间花完结果还是失败。
    # 传输层退避看不懂 429 的语义，交给上层按语义处理更准。
    #
    # 代价要说清楚：设0 意味着**真的**网络抖动也会立刻失败。实测 LLM 走内网网关，
    # 抖动少，所以取 0 换掉那 10.6 秒是划算的。如果你的网络真的不稳，调回 2。
    llm_max_retries: int = 0
    # ★ 是否让 browser-use 每次启动都联网查"有没有新版本"。
    #
    # 实测（2026-10-06）：这个检查是 `GET https://pypi.org/pypi/browser-use/json`
    # （browser_use/utils.py::check_latest_browser_use_version，timeout=3.0），
    # 实测单次3.4 秒，且发生在"启动浏览器"计时之外 → **每条用例白付 3.4 秒**。
    # 它唯一的产出是一行 "Newer version available" 提示，对执行结果毫无影响。
    #
    # 三态None=交给 browser-use 自己（它默认 true，即联网查），
    # 显式 True/False 才由我们覆盖。关掉不影响功能，只影响那条提示。
    browser_version_check: bool | None = False
    # 送进 prompt 的历史步数上限。None = 不限制，历史随步数线性增长，长用例的 prompt
    # 会越来越大、每步越来越慢。设一个上限能让长用例的每步耗时保持平稳。
    # 默认 None（不改现有行为）：调小它会丢掉早期上下文，属于**质量换速度**的取舍，
    # 建议先看埋点数据再决定，不要盲调。
    agent_max_history_items: int | None = None
    # 输出侧 token 的两个开关（都是**质量换速度**，默认保守）：
    #   use_thinking=False  → 模型不再输出思考过程
    #   flash_mode=True     → 从输出 schema 里剥离 plan 字段且强制 use_thinking=False
    #     （源码：flash mode strips plan fields，planning 在结构上不可能）
    # 开发/冒烟可开；正式回归建议保持默认，输出变少会牺牲判断准确性。
    agent_use_thinking: bool = True
    agent_flash_mode: bool = False
    # Write a 操作步骤/实际结果/预期结果 bug description for every FAILED case (see
    # app/failure_narrative.py). Costs one extra model call per failing case only —
    # passed cases are skipped. Turn off to keep the report strictly verdict-only.
    failure_narrative_enabled: bool = True
    # Per-step model call is the dominant cost and was unbounded; a wall-clock guard
    # keeps one slow step from eating the whole case budget.
    step_timeout_s: int = 45
    # How many independent actions the agent may batch into ONE model round-trip.
    # Step count is the cost of a case, and form-filling steps are naturally batchable.
    max_actions_per_step: int = 5
    # Actions removed from the agent's tool registry entirely (see executor._build_tools).
    #
    # Why hard removal and not just a prompt rule: measured on this project, 44% of all
    # steps were no-op probes (`wait`/`scroll`/`search_page`/`find_elements`/`evaluate`),
    # and with concurrency pinned to 1 a wasted step is pure serial dead time — nothing
    # overlaps it. `wait` and `search_page` are the two that cannot pay for themselves:
    #   * `wait` re-observes a page the agent is handed a fresh snapshot of anyway.
    #   * `search_page` re-reads text already in that snapshot (run #140 searched for the
    #     same validation message twice, in two different wordings).
    # `scroll` / `find_elements` / `evaluate` are deliberately KEPT — they earn their
    # round-trip on long lists and on values the a11y tree does not expose.
    # 从 agent 工具集里移除的动作。
    #
    # 起因是"要像真人一样操作"。默认全部移除以下四项，理由分两类：
    #
    # 1) 真人做不到的（脚本捷径）—— 用户明确要求"模拟人在 web 网页里操作"，
    #    而这些是绕过界面的捷径，留着就一定会被用：
    #      * `evaluate`      直接在页面里执行 JS。可以跳过输入框赋值、跳过点击改状态，
    #                        等于"没有通过界面操作却拿到了结果"，测试意义被架空。
    #      * `find_elements` 用脚本查元素列表。真人是用眼睛在页面上找的。
    #
    # 2) 纯浪费时间、且不属于人类行为的：
    #      * `wait`          每步之后本来就会拿到新快照，等着看不会多出任何信息。
    #                        日志里出现过 wait→wait→wait 链，模型自己写着"DOM not captured yet"。
    #      * `search_page`   重新读页面上已有的文本。run #140 先用"请选择审批节点"搜一次，
    #                        又用"必填|请选择|审批节点"搜一次，为同一个答案烧掉两个完整往返。
    #
    # `scroll` / `navigate` / `click` / `input` / `send_keys` 等**保留**——那些真人真的会做。
    # `extract` 也保留：它是从页面**已渲染的文本**里归纳，不执行脚本。
    excluded_agent_actions: list[str] = ["wait", "search_page", "evaluate", "find_elements"]
    # --- workspace-scoped persistent browser profile -------------------------------
    # Keep ONE Chromium user-data-dir per project between cases and between runs, so
    # cookies / localStorage / sessionStorage / IndexedDB (and the app's own HTTP cache)
    # are already warm when the next case starts: no cold boot, no re-login, no re-parse
    # of the SPA bundle. This is the single biggest win for a regression re-run.
    # Trade-off: cases in the same project now share a browser identity (cookies), which
    # also means they can observe each other's leftovers. 0/false keeps the old
    # fresh-browser-per-case behaviour.
    persistent_profile: bool = True
    # Directory holding the per-project profiles (mounted as a docker volume).
    profile_dir: str = "./profiles"
    # How long the app under test keeps a session alive. A captured login is re-used until
    # this runs out, then re-captured once (single-flight) for everyone. Most systems sit
    # around 30 minutes, so stay just under it.
    session_ttl_min: int = 25

    artifact_dir: str = "./artifacts"

    # Built frontend (web/dist) served by the API as an SPA. Empty => API only
    # (dev uses the Vite server). The Docker image sets this to /app/web_dist; a native
    # run leaves it empty and the validator below auto-detects ./web/dist if it was built.
    web_dist: str = ""

    # GitLab two-way issue sync (Celery worker + beat). Empty redis_url => sync disabled.
    redis_url: str = ""  # e.g. redis://localhost:6379/0 — Celery broker/result backend
    gitlab_base_url: str = "https://gitlab.com/api/v4"
    gitlab_verify_ssl: bool = True
    gitlab_poll_interval_s: int = 60
    # Server-wide GitLab token (api scope). When set, projects need only pick a
    # GitLab project — no per-project token. A per-project token still overrides it.
    gitlab_token: str = ""

    # --- auth / users ---
    # Auth (login page, invites, password reset, per-project RBAC) is fully implemented.
    # It defaults to False only so a single-user local install needs no setup; ANY
    # deployment reachable by someone else must set AUTH_ENABLED=true, otherwise every
    # endpoint is open and visitors can spend your LLM quota.
    auth_enabled: bool = False
    # Per-project RBAC by default: a user sees only projects an admin assigned them to
    # (ProjectMember owner/editor/viewer); admins see all. Set True for a shared workspace
    # where any logged-in user has full access to every project (login gate only).
    shared_workspace: bool = False
    jwt_secret: str = ""  # HS256 signing key; required once auth_enabled
    jwt_ttl_hours: int = 24 * 7
    # Session cookie Secure flag: "auto" | "true" | "false".
    # "auto" => Secure when public_base_url is https, else not. You must set it to "true"
    # explicitly when TLS is terminated in FRONT of the app (Cloudflare Tunnel, nginx,
    # Caddy): the app itself still speaks plain http, so it cannot detect that the browser
    # reached it over https — and a non-Secure cookie can then leak over a downgraded
    # request. This is the single most common way a "we put it behind a proxy" deployment
    # silently keeps handing out session cookies over http.
    cookie_secure: str = "auto"
    admin_email: str = ""  # seeds the first admin on startup (with admin_password)
    admin_password: str = ""
    # Canonical public URL (domain). Drives BOTH email links (invites/notifications)
    # and the address shown in the UI. Set via PUBLIC_BASE_URL, e.g. http://potato-test.example.com
    public_base_url: str = ""

    # --- Feishu (Lark) feedback bot ---
    # Self-built app credentials (飞书开放平台 自建应用). Empty => webhook disabled.
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # Event subscription Verification Token. When set, incoming events must match.
    feishu_verification_token: str = ""
    # Open API base. Cloud 飞书: https://open.feishu.cn ; Lark 国际版: https://open.larksuite.com
    feishu_api_base: str = "https://open.feishu.cn"
    # Also answer questions detected from context (not just @-mentions / p2p).
    feishu_auto_answer_detected: bool = True
    # Ignore HTTP(S)_PROXY / NO_PROXY when calling the Feishu Open API. Same rationale as
    # `gateway_ignore_proxy`: httpx defaults to trust_env=True, and this machine's proxy
    # port rotates (a sandbox proxy, a VPN client that later closes), so a var that was
    # valid at launch can be dead minutes later -- every push then fails with
    # "All connection attempts failed", which looks like bad credentials but is not.
    # Set FEISHU_IGNORE_PROXY=false if Feishu is genuinely only reachable via proxy.
    feishu_ignore_proxy: bool = True
    # --- 推送时机（默认：只在真有事的时候说话） ---
    # 定时心跳：每 N 秒一张进度卡。默认 0 = 关。
    #
    # 为什么关掉：手机上看不了实时页面，最初做心跳是为了「缩短等待感知」，但实测下来
    # 大部分心跳卡的内容跟上一条相比毫无变化，属于纯噪音 —— 要的是"有变化时告诉我"，
    # 不是"每隔三分钟证明你还活着"。真需要"还活着"的信号时把它设成大于 0 即可，
    # 里程碑推送与它并存、互不干扰。
    feishu_progress_interval_sec: int = 0
    # 里程碑：进度（processed/total）跨过这些百分比时各推一张卡，例如 "25,50,75"。
    # 这是自动推送的主要来源，配合"跑完一张结果卡"，一轮 378 用例最多 4 张卡。
    feishu_milestone_percents: str = "25,50,75"
    # 首次失败告警：出现第一个未通过用例时立刻推一张红卡。
    # 每个运行只推一次 —— 一轮里 200 个失败不能变成 200 张卡。
    # 价值在于"第 3 个用例就挂了"这种信号能马上到达，不必等到 25% 里程碑。
    feishu_push_first_failure: bool = True
    # 开始卡：点运行后几秒内推一张"🚀 测试已开始"。
    # 默认关 —— 明确要求减少自动消息，而且"到底跑起来没有"现在可以在群里问。
    feishu_push_start: bool = False
    # Ambient monitoring cost gate: non-@ messages need a problem keyword to be
    # processed. Set False to LLM-classify EVERY non-@ message (never drop, higher cost).
    feishu_ambient_require_keyword: bool = True
    # 轮询机器人的间隔（秒）。它决定了"在群里问状态"多久能得到回答。
    # 8 秒实测下来 CPU 占空比不到 1%（连接复用之后），几乎不会拖慢机器；
    # 想更省就调到 20~30，代价是最坏情况下要多等这么久才回你。
    # 连续失败时进程会自动把间隔往上翻倍（最高 60 秒），成功后立刻恢复。
    feishu_poll_interval_sec: int = 8
    # 机器人是否**跟着 API 服务一起启动**（2026-10-06）。
    #
    # 默认 True。为什么改默认值：此前机器人只能靠桌面 .bat 额外
    # `start pythonw -m app.feishu_poll` 起第二个进程，于是任何一次
    # "只启动了服务、没启动机器人"的部署，症状都是「群里问状态没人理」——
    # 而且日志里**什么都不会有**，因为压根没有进程在跑。对着一份空日志
    # 排查一个不存在的问题，是最浪费时间的一类故障。
    # 一键部署要成立，机器人就必须随服务一起起来、随服务一起停。
    #
    # 不会重复回复：进程内启动前先抢 `potato-feishu-poll` 单实例锁
    # （app/single_instance.py，内核级），独立的 `python -m app.feishu_poll`
    # 或 Docker 里单独跑的 worker 抢不到锁时会自己退出并记录原因。
    #
    # Docker 部署请设成 false —— docker-compose.yml 里已经有单独的 feishu_ws
    # 服务（WebSocket 长连接），两边同时在线会让一条消息被回两次。
    feishu_worker_in_process: bool = True

    # --- system SMTP (invite emails / notifications) ---
    email_from: str = "noreply@example.com"
    email_host: str = "smtp.example.com"
    email_port: int = 25
    email_host_user: str = ""
    email_host_password: str = ""
    email_use_ssl: bool = False
    email_use_tls: bool = False

    @model_validator(mode="after")
    def _detect_local_paths(self) -> "Settings":
        """Make a native (non-Docker) run work with no extra setup.

        The container sets WEB_DIST/PROFILE_DIR explicitly. A raw clone does not, so
        auto-detect the built SPA and keep every relative path anchored to the project
        root — otherwise `uvicorn` started from another cwd silently serves no frontend.
        """
        root = Path(__file__).resolve().parent.parent

        if not self.web_dist:
            built = root / "web" / "dist"
            if (built / "index.html").is_file():
                self.web_dist = str(built)

        for name in ("artifact_dir", "profile_dir"):
            value = getattr(self, name)
            if value and not os.path.isabs(value):
                setattr(self, name, str((root / value).resolve()))

        if self.database_url.startswith("sqlite") and "///" in self.database_url:
            head, tail = self.database_url.split("///", 1)
            if tail and tail != ":memory:" and not os.path.isabs(tail.split("?", 1)[0]):
                self.database_url = f"{head}///{(root / tail).resolve().as_posix()}"

        # dotenv keeps an inline comment as the value when the value part is otherwise
        # empty: `JWT_SECRET=   # use openssl` sets the secret to the literal text
        # "# use openssl". Since a signing key that ships in the repository lets anyone
        # forge a session cookie, treat a comment-shaped secret as NOT SET (we then fall
        # back to the per-install generated key) instead of trusting it.
        for field in ("jwt_secret", "secret_key", "gateway_api_key"):
            value = (getattr(self, field) or "").strip()
            if value.startswith("#"):
                log.warning(
                    "config: %s looks like a comment, not a value (%r) — ignoring it. "
                    "Move the comment to its own line in .env.",
                    field.upper(),
                    value[:60],
                )
                setattr(self, field, "")

        if self.auth_enabled and not self.jwt_secret:
            # Not fatal (auth falls back to the generated key), but the operator asked for
            # login and did not pin a signing key, so sessions reset on every restart.
            log.warning(
                "config: AUTH_ENABLED=true but JWT_SECRET is empty — sessions will be "
                "signed with the auto-generated per-install key. Set JWT_SECRET=%s "
                "to keep logins stable across restarts.",
                "(openssl rand -hex 32)",
            )

        if not (self.public_base_url or "").strip():
            # 故意不再自动推导。
            #
            # 以前这里会探测本机局域网 IP 并拼成 http://192.168.x.x:18080，用来给飞书卡片的
            # 「查看运行」按钮和邮件链接兜底。但服务现在只监听 127.0.0.1，那个地址手机上根本
            # 打不开，唯一的实际效果是把内网 IP 写进飞书群消息里扩散出去。
            #
            # 留空的行为是安全的：feishu_cards._run_web_url() 收到空值会返回 None，卡片自动
            # 不带按钮，而不是产出一个点不开的死链。真要给别人用，显式配 PUBLIC_BASE_URL
            # （配真实可达的域名/地址），这里永远不会覆盖显式值。
            log.info(
                "config: PUBLIC_BASE_URL 未设置 —— 飞书卡片不会有外链按钮，邮件链接用相对地址。"
                "需要对外访问时请显式配 PUBLIC_BASE_URL。"
            )

        return self

    @property
    def feishu_milestones(self) -> list[int]:
        """`feishu_milestone_percents` parsed into sorted unique ints.

        Tolerant on purpose: "25 50 75", "25,50,75" and "25，50，75" (full-width comma)
        all work, and junk like "abc" or "0"/"100" is dropped rather than raising —
        a typo in .env must not stop a test run from starting.
        """
        out: set[int] = set()
        for part in re.split(r"[,，\s]+", self.feishu_milestone_percents or ""):
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if 0 < value < 100:
                out.add(value)
        return sorted(out)

    @property
    def cookie_secure_enabled(self) -> bool:
        """Whether the session cookie gets the Secure flag (see `cookie_secure`)."""
        value = (self.cookie_secure or "auto").strip().lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off"):
            return False
        return (self.public_base_url or "").strip().lower().startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
