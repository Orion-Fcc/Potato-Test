# Potato Test

用**自然语言**写测试用例，让浏览器 Agent 真的去点，再由 LLM 裁判判定「通过 / 失败」并写出理由。

用例是提示词，不是脚本 —— 页面改版、元素挪位置，用例不用改。

    「以审核员身份登录，打开待审批列表，通过第一条，确认状态变为已通过。」

Agent 自己想清楚该点哪里；裁判读截图和动作记录下结论，**证据不足时默认判失败**，不会出现那种"假绿"。

---

## 它适合干什么

- 给 Web 系统 / 内网平台做端到端回归测试，又不想维护一堆脆弱的选择器
- 把「人工点一遍」的验收流程，变成可重复、有截图录像留痕的自动化运行
- 需要「为什么失败」而不只是「失败了」——每个用例都有裁判写的理由和可回放的步骤

---

## 快速开始（Windows，推荐）

**双击 `start-potato.bat` 就行。** 它会自动完成：

1. 建 Python 虚拟环境并安装依赖
2. 下载 Chromium（执行测试用）
3. 构建前端界面
4. **在后台启动服务**，等它就绪后自动打开浏览器
5. 窗口随即关闭，**服务继续在后台运行**

| 我想…… | 双击 |
| --- | --- |
| 启动（或已在运行时打开浏览器） | `start-potato.bat` |
| 停止服务 | `stop-potato.bat` |
| 每次开机自动启动 | `autostart-on.bat` |
| 取消开机自启 | `autostart-off.bat` |

启动后访问 <http://127.0.0.1:18080/>

几点说明：

- **服务是常驻的。** 关掉那个黑窗口不会停止服务，必须用 `stop-potato.bat` 才会停。
- **端口默认 18080。** 如果被占用，会自动往后找第一个空闲端口并在窗口里告诉你改用了哪个。也可以指定：`start-potato.bat 19000`
- **日志在 `logs\potato.log`。** 服务在后台跑，出问题看这里。
- **首次运行**要多等几分钟（装依赖 + 下 Chromium），之后就快了。

### 手动启动

不想用脚本、或者用 macOS / Linux：

    python -m venv .venv
    # Windows
    .venv\Scripts\python -m pip install -e .
    .venv\Scripts\python -m playwright install chromium
    .venv\Scripts\python -m uvicorn app.main:app --port 18080

    # macOS / Linux
    python3 -m venv .venv && source .venv/bin/activate
    pip install -e .
    playwright install chromium
    uvicorn app.main:app --port 18080

（`pip install -e .` 会按 `pyproject.toml` 装齐全部依赖。注意 `browser-use` 和 `playwright` 的版本是**锁死**的，别随手升级——它们的接口在版本间会变。）

---

## 第一次使用要配什么

1. 打开 <http://127.0.0.1:18080/>
2. 进 **系统设置 → LLM 模型**，填 Base URL / API Key / 模型名，点 **测试连通性** 确认能通
   （这一步别跳过。配错了后面每次运行都会失败，而报错看起来像用例问题）
3. 新建项目，填被测系统的地址
4. 写用例（用大白话描述预期行为），然后点运行

界面里 LLM 的配置**优先于** `.env`，改完立即生效，不用重启。

---

## 功能

- **自然语言用例** —— 按项目组织，可打标签，支持 Excel 批量导入导出
- **真实浏览器执行** —— 每个用例一个 Chromium，录屏 + 每步截图 + 可回放的动作历史
- **LLM 裁判** —— 给出通过/失败和书面理由；基础设施类错误会重试，裁判判的失败不重试
- **自带模型** —— 任何 OpenAI 兼容接口都能接，界面里随时改，不用重新部署
- **运行与报告** —— 并发执行带全局浏览器额度上限，实时进度，支持「只重跑失败的」，支持运行间对比
- **环境与账号** —— 每个项目可配多套环境（测试/预发…）和多个登录账号；会话会被捕获复用，用例不需要每次重新登录
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
| Docker 部署 | 部署到服务器 | 见 `docs/deployment.md` |

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
    run_server.py   后台启动入口（写日志用）
    start-potato.bat / stop-potato.bat / autostart-*.bat   一键脚本
    docs/           部署与配置手册
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
现在不会了。`start-potato.bat` 把服务放在后台跑，窗口只是用来显示启动过程。要停服务请用 `stop-potato.bat`。

**提示端口被占用？**
默认端口是 18080，被占用时会自动往后找空闲端口并告诉你结果。想固定某个端口就 `start-potato.bat 19000`。

**点「运行」报 `No module named 'browser_use'`？**
依赖没装全。重新双击 `start-potato.bat` 即可，它会补齐。这类依赖是懒加载的，所以缺了不影响服务启动，只在真正跑用例时才暴露。

**跑用例报「找不到浏览器」？**
Chromium 没下载。执行 `.venv\Scripts\python -m playwright install chromium`。

**为什么界面里没有「用户管理」？**
因为它对单人本地使用没有意义 —— 没有登录就没有"用户"这个概念。开启 `AUTH_ENABLED=true` 后它就会出现。

**用例老是超时？**
先看报错是「步数预算用尽」还是「卡在某一步」。前者调大 `CASE_MAX_STEPS`，后者要去看那一步对应的页面 —— 两种情况的处理方式完全不同，报错里已经区分了。

---

## 技术栈

后端 FastAPI + SQLAlchemy（异步）；默认 SQLite，可换 Postgres。
浏览器自动化 `browser-use` → Playwright → Chromium。
前端 React + Vite + Tailwind。
可选：Celery + Redis（队列）、飞书、GitLab。

---

## 许可

MIT
