# Potato Test

用**自然语言**写测试用例，让浏览器 Agent 真的去点，再由 LLM 裁判判定「通过 / 失败」并写出理由。

用例是提示词，不是脚本 —— 页面改版、元素挪位置，用例不用改。

    「以审核员身份登录，打开待审批列表，通过第一条，确认状态变为已通过。」

Agent 自己想清楚该点哪里；裁判读截图和动作记录下结论，**证据不足时默认判失败**，不会出现那种"假绿"。

> **第一次用？先看 [docs/USAGE.md](docs/USAGE.md)（使用说明）。**
> 里面有启动/关闭、界面导航、怎么写用例（前置条件·多角色·Excel 导入）、怎么看结果
> （三段式失败描述·失败根因分类）、排障，以及本机当前的配置速查。
> 本文（README）偏"项目介绍与实现"，使用说明偏"照着做"。

---

## 它适合干什么

- 给 Web 系统 / 内网平台做端到端回归测试，又不想维护一堆脆弱的选择器
- 把「人工点一遍」的验收流程，变成可重复、有截图录像留痕的自动化运行
- 需要「为什么失败」而不只是「失败了」——每个用例都有裁判写的理由和可回放的步骤

---

## 快速开始

### 装安装包（推荐）

`PotatoTest-Setup-1.0.0.exe` 双击安装 —— 装到 `%LOCALAPPDATA%\Programs\PotatoTest`
（per-user，**不需要管理员**），**包里不带任何数据**，装完第一次打开就是空库。
安装时可选桌面快捷方式（默认不勾）、开机自启。

装完从**开始菜单打开**：窗口里就是界面 —— 没有地址要记，也没有浏览器。

> 数据库、上传的原件、浏览器 profile 都落在**程序目录旁边**，所以**别装到 `Program Files`**
> （那边对普通用户不可写，会出现"能打开、一保存就报错"，而且错误只在日志里）。用默认目录就行。

**应用内更新**：设置页底部「版本 → 检查更新」。更新源是本仓库的 GitHub Release，
发布用 `python scripts\release.py --publish`（会先校验三处版本号一致）。

### 从源码跑（开发用）

需要 Python **3.13**（与发布出来的安装包同一个解释器版本；3.11 / 3.12 不再验证）：

```bat
git clone https://github.com/Orion-Fcc/Potato-Test.git
cd Potato-Test
python -m venv .venv
.venv\Scripts\pip install -c constraints.txt -e .
.venv\Scripts\python desktop.py
```

`constraints.txt` 锁住的是本机验证过的直接依赖版本，CI 与打包用的是同一份。

首次装依赖约 5-10 分钟（`browser-use` 依赖树 + OCR 模型较大）。
`web/dist`（界面构建产物）**随仓库发布**，所以不需要 Node / pnpm。

要改界面时：`cd web && pnpm install && pnpm build`，产物仍落在 `web/dist`。

### 自己打包 / 发布

见 [docs/desktop-build.md](docs/desktop-build.md)：
`scripts\build_desktop.py` 打程序目录 → `makensis installer\potato.nsi` 编译安装包 →
`scripts\release.py --publish` 发 Release（应用内更新靠它）。

---

## 第一次使用要配什么

1. 打开**桌面版**（双击 `PotatoTest.exe`，窗口里就是界面）
2. 进 **系统设置 → LLM 模型**，填 Base URL / API Key / 模型名，点 **测试连通性** 确认能通
   （这一步别跳过。配错了后面每次运行都会失败，而报错看起来像用例问题）
3. 新建项目，填被测系统的地址
4. 写用例（用大白话描述预期行为），然后点运行

界面里 LLM 的配置**优先于** `.env`，改完立即生效，不用重启。

### 飞书机器人（可选）

配好之后，可以在群里用手机问进度、按需发起运行 —— **不会自动推送刷屏**。

1. 到 <https://open.feishu.cn> 建一个**自建应用**，拿到 `App ID` / `App Secret`。
2. 给应用开这两个权限，然后**发布版本**（不发布不生效）：
   - `im:message` —— 发消息
   - `im:message.history:readonly` —— 读群历史。**缺了它机器人只能推卡片、看不到你问的话**，
     表现就是「群里问『状态』没人理」。
3. 在 `.env` 里填好，并把开关打开：

   ```ini
   ENABLE_FEISHU=true
   FEISHU_APP_ID=cli_xxx
   FEISHU_APP_SECRET=xxx
   ```

4. 重启服务。机器人**跟着服务一起起停**（`FEISHU_WORKER_IN_PROCESS=true` 是默认值），
   不需要额外开进程。启动日志里会出现一行「飞书轮询已随服务启动」。
5. 把机器人拉进群，在群里发 `@机器人 绑定 <项目名>`，然后 `@机器人 状态` 试一下。

支持的指令：`状态` / `跑` / `重跑` / `绑定 <项目名>` / `帮助`。

排查用这两条：

```bash
# 看机器人有没有在跑、有没有报错（轮询日志是独立文件）
tail -f logs/feishu_poll.log
```

- 「已有轮询实例在运行（PID …）」—— 说明另有一个机器人在跑（比如你自己起的
  `python -m app.feishu_poll`）。一条消息只该被回一次，所以后来者会主动退出，
  这是**正常行为**，不是故障。
- 「拉取群消息失败（… ConnectError/ReadTimeout）」—— 网络抖动。机器人会退避重试，
  最多安静 60 秒就会恢复。持续失败则检查出网/代理（`FEISHU_IGNORE_PROXY`）。

（2026-10-17 起不再有 Docker 部署 —— 本产品只发桌面版，机器人默认随应用一起起停。）

---

## 功能

- **自然语言用例** —— 按项目组织，可打标签，支持 Excel 批量导入导出
- **真实浏览器执行** —— 每个用例一个独立浏览器（优先用本机已装的 Edge/Chrome，找不到才回退自带 Chromium），录屏 + 每步截图 + 可回放的动作历史
- **LLM 裁判** —— 给出通过/失败和书面理由；基础设施类错误会重试，裁判判的失败不重试
- **自带模型** —— 任何 OpenAI 兼容接口都能接，界面里随时改，不用重新部署
- **运行与报告** —— 并发执行带全局浏览器额度上限，实时进度，支持「只重跑失败的」，支持运行间对比
- **环境与账号** —— 每个项目可配多套环境（测试/预发…）和多个登录账号；会话会被捕获复用，用例不需要每次重新登录
- **资料与文档** —— 上传需规/资料（md / txt / json / csv / docx / pdf / xlsx / html / rtf）后，
  同一份文件既进知识库（给 AI 检索）又存成「项目文档」（给人看、给人改）；**原件会保留**，
  随时能打开原格式的文件核对；正文可以像 Word 一样手工编辑，**重传同一文件不会覆盖你改过的正文**
- **大系统共享需规** —— 若干小模块挂在一个"大系统"下共用一份系统级资料；支持直接上传文件，
  传过的原件可回溯（避免每个模块各传一遍同一份需求）
- **缺陷看板** —— 内置，失败的用例可以直接转成缺陷单
- **中英双语界面**

---

## 本项目用不到的功能（默认关闭，不用管）

这些是平台自带的集成能力，**单人本地使用完全用不到**，默认也都是关的：

| 功能 | 什么时候才需要 | 开关 |
| --- | --- | --- |
| 多用户 / 邀请 / 角色权限 | 团队共用一个实例 | `AUTH_ENABLED=true`（默认关闭，且界面上的「用户管理」入口会隐藏） |
| 飞书机器人 | 想在飞书群里触发运行、收结果卡片 | `ENABLE_FEISHU=true` |
| GitLab 缺陷双向同步 | 缺陷要同步到 GitLab Issue | `ENABLE_GITLAB=true` |
| Celery + Redis 队列 | 多机部署或需要任务持久化 | 设 `REDIS_URL`（留空就用进程内队列） |
| ~~Docker / 服务器部署~~ | **已移除**（2026-10-17）：本产品只发桌面版，见 `docs/desktop-build.md` | — |

换句话说：**默认配置就是为「一个人在一台机器上本地用」准备的**，不需要登录、不需要数据库服务、不需要 Redis。

---

## 目录结构

    app/
      config.py     配置（读 .env）
      models.py     数据模型：项目 · 用例 · 运行 · 结果 · 缺陷 · 用户
      engine.py     运行调度：并发、失败隔离、结果汇总
      executor.py   单用例执行：一个浏览器、录屏、会话捕获与复用
      judge.py      LLM 裁判（证据不足时判失败）
      llm.py        OpenAI 兼容客户端（Agent 用 + 裁判用）
      api.py        REST + SSE 接口
      assistant.py  内置助手（查用例、跑测试、看报告、起草用例）
      storage.py    产物存储：本地磁盘或 S3 兼容
      auth.py       登录会话、密码、权限
      excel.py      Excel 用例导入导出
      # 可选集成：gitlab_*.py / feishu*.py / celery_app.py
    web/            React + Vite + Tailwind 前端
    desktop.py      桌面版入口：同进程起服务 + 开窗口（唯一入口）
    desktop.py      桌面版入口：同一进程里起服务 + 开一个原生窗口（pywebview → Edge --app → 浏览器）
    scripts/        部署 / 体检 / 打包（build_desktop.py 打桌面版程序目录）
    installer/      NSIS 安装包脚本（per-user、不经过 %TEMP%、不含数据、可选桌面快捷方式）
    docs/           使用说明（USAGE.md）与部署、配置手册
    tests/          pytest 测试（纯逻辑，不需要浏览器）

---

## 配置

配置文件是 `.env`（首次启动会从 `.env.example` 自动生成）。常用的几项：

| 变量 | 说明 |
| --- | --- |
| `GATEWAY_BASE_URL` / `GATEWAY_API_KEY` / `GATEWAY_MODEL` | LLM 接口与模型（也可在界面里改，界面优先） |
| `CASE_MAX_STEPS` | 单个用例最多几步（**这是真正的主控制**，不是超时时间） |
| `CASE_TIMEOUT_S` | 单个用例的墙钟安全网，会按剩余步数自动放宽 |
| `MAX_GLOBAL_CONCURRENCY` | 同时最多开几个浏览器（每个约 0.5GB 内存，按机器内存调） |
| `CASE_RECORD_VIDEO` | 是否录屏；关掉能省不少 CPU 和磁盘 |
| `DATABASE_URL` | 默认 SQLite，换成 Postgres 也能用 |

完整说明见 `.env.example` 里的注释和 [docs/configuration.md](docs/configuration.md)。

> 写 `.env` 时注意：**注释要单独占一行**。写成 `KEY=   # 说明` 的话，dotenv 会把 `# 说明` 当成值。

---

## 常见问题

**关掉窗口服务就断了？**
桌面版就是"关窗口即停"，没有后台常驻这回事。
如果确实想让它常驻，用 `start "" .venv\Scripts\pythonw desktop.py`（窗口会自己开），
关掉终端也不会停，日志照样写进 `logs/potato.log`。

**提示端口被占用？**
桌面版被占用时会自动换一个空闲端口；要固定就设环境变量 `POTATO_DESKTOP_PORT=19000`。

**点「运行」报 `No module named 'browser_use'`？**
依赖没装全。执行 `.venv\Scripts\python -m pip install -c constraints.txt -e .` 补齐。
这类依赖是懒加载的，所以缺了不影响服务启动，只在真正跑用例时才暴露。

**跑用例报「找不到浏览器」？**
先确认机器上装了 Edge 或 Chrome —— 本项目默认直接用系统浏览器（启动更快，也更接近真人环境）。
两个都没有时才会回退到 Playwright 自带的 Chromium，执行 `.venv\Scripts\python -m playwright install chromium`。
想指定用哪个，见 `.env` 里的 `BROWSER_EXECUTABLE` / `BROWSER_CANDIDATES`。

**为什么界面里没有「用户管理」？**
因为它对单人本地使用没有意义 —— 没有登录就没有"用户"这个概念。开启 `AUTH_ENABLED=true` 后它就会出现。

**用例老是超时？**
先看报错是「步数预算用尽」还是「卡在某一步」。前者调大 `CASE_MAX_STEPS`，后者要去看那一步对应的页面 —— 两种情况的处理方式完全不同，报错里已经区分了。

---

## 技术栈

后端 FastAPI + SQLAlchemy（异步）；默认 SQLite，可换 Postgres。
浏览器自动化 `browser-use` → Playwright → 系统 Edge/Chrome（缺失时回退自带 Chromium）。
前端 React + Vite + Tailwind。
可选：Celery + Redis（队列）、飞书、GitLab。

---

## 许可

MIT
