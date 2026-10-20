# Potato Test 优化路线图

> 生成日期：2026-10-20
> 适用代码基线：`main` 分支 `7daccf9` 及其未提交改动
> 文档定位：可执行路线图。每项包含问题、证据（文件:行号）、改法、工作量、依赖、验收与回滚。
> 改造力度：已获授权允许破坏性变更（数据模型、依赖版本、API 契约），但每一步仍须可回滚。

---

## 0. 如何使用这份文档

- **P0 先做**：P0 只放正确性、安全、数据丢失与半迁移风险；纯性能与风格问题一律进 P1/P2。
- **一次一项**：遵循项目 `CLAUDE.md` 的工程纪律——改码前先读文件并说明计划、等确认、**每次只改一个文件**、改完跑相关测试。迁移脚本单独提交。
- **先测量再改**：每个性能项在动工前先复测基线，改完用同一命令复测；没有基线的优化不立项。
- **行号会漂移**：文中行号基于生成时快照，动手前以函数名/符号重新定位。
- **阶段可独立发布**：阶段 0→5 有依赖关系，但每个阶段结束时应用都应处于可发布状态。

---

## 1. 现状与优先级

### 1.1 基线（本机实测，动工前需复测）

| 指标 | 当前值 | 测量方式 |
|---|---|---|
| `import app.main` | 4.48s | `python -X importtime -c "import app.main"` |
| 桌面包 `dist_desktop` | 约 487MB（`_internal` 455MB） | `du -sh dist_desktop` |
| 安装包 | 约 170.9MB | 查看 Inno Setup 产物 |
| 测试规模 | 103 个测试文件、pytest 收集 1569 条 | `pytest --collect-only -q` |
| 覆盖率门槛 | 54%（`api.py` 28%、`executor.py` 45%） | `pytest --cov=app --cov-report=term-missing` |
| 宽泛 `except Exception` | 231 处 | `grep -rn "except Exception" app \| wc -l` |
| 大依赖 | cv2 82M、playwright 104M、pymupdf 49M、onnxruntime 43M、numpy 33M、rapidocr 32M、嵌入模型 24M | 逐目录统计 `_internal` |

关键性能/安全事实：

- 数据库已是 WAL + `busy_timeout=5000` + `synchronous=NORMAL`（`app/db.py:32-46`），但使用 `NullPool`（`app/db.py:56`），每个会话新建连接。
- 无版本化迁移：`create_all` + 约 30 条手写 `ALTER`/索引检查（`app/db.py:105-241`），只有 2 条增量索引（`app/db.py:212-216`）。Alembic 仅在可选依赖中声明（`pyproject.toml:77,87`），代码注释也写着 "Alembic added in Phase 2"（`app/db.py:1`）。
- 检索为"取 5000 条向量 + 最多 2000 条 `ILIKE '%x%'`，在 Python 中全量点积"（`app/knowledge.py:422-543`）。
- 更新链路只校验同 Release 的 SHA-256（`app/updater.py:194,225-248`），无 Authenticode、无回滚；安装用 `cmd /c` 拼接并延迟执行（`app/updater.py:251`）。

### 1.2 优先级矩阵

| 优先级 | 问题 | 证据 | 影响 |
|---|---|---|---|
| **P0** | 加密密钥单点丢失：密钥缺失即自动生成新密钥 | `app/crypto.py:32-61` | 换机/重装/恢复备份后，所有已加密凭据不可解 |
| **P0** | 浏览器登录态明文落库 | `app/models.py:108`（`Project.login_state: Text`） | 用户 Cookie/Token 明文泄露；与已加密的 `Credential`（`app/models.py:570-604`）并存两套 |
| **P0** | 更新包无代码签名、无回滚 | `app/updater.py:225-251`、`scripts/release.py` | 哈希与安装包同源，无法抵御整包替换；升级失败无法退回 |
| **P0** | 无版本化迁移，失败产生半迁移 | `app/db.py:105-241` | 升级中断后数据库结构不一致，且无 downgrade |
| **P0** | 存储迁移非原子、无进度 | `app/api.py:5735-5757` | 跨盘迁移中断后新旧目录混杂 |
| **P0** | 默认暴露面过大 | `app/config.py:578`（`auth_enabled=False`）、`app/main.py:207`（CORS `*`）、`app/main.py:226`（`/artifacts` 裸挂载） | 非本机部署时无鉴权可访问接口与产物 |
| **P1** | 项目列表 N+1 | `app/api.py:1126-1216` | 每个项目 2-3 次查询，项目多时首屏变慢 |
| **P1** | 内存向量/关键词检索 | `app/knowledge.py:384-419,422-543` | 大项目检索延迟随块数线性增长；嵌入写入逐行 `get` |
| **P1** | SSE 每秒查库 | `app/api.py:5135-5156` | 空闲运行页持续产生数据库查询 |
| **P1** | 异步端点内同步解析文档 | `app/api.py:1458,1822,1953,2241,2427,3377` | 上传/重解析期间阻塞事件循环，其他请求卡顿 |
| **P1** | PDF 双重解析、OCR 串行、RTF 近似 O(n²) | `app/docparse.py:132-177,303-336,401-473` | 扫描件处理慢；大 RTF 文档 CPU 占用高 |
| **P1** | 启动串行做重活 | `app/main.py:186-198` | 启动时同步执行建表、孤儿 run 对账、知识块回填、飞书与套件调度 |
| **P1** | 包体约 487MB | `scripts/build_desktop.py:99-173` | 下载/安装/更新成本高；OCR 与模型对多数用户非必需 |
| **P2** | 超大模块 | `app/api.py` 5816 行、`executor.py` 3399 行、`assistant.py` 3035 行 | 合并冲突频繁、评审与测试成本高 |
| **P2** | 231 处宽泛 except，部分无日志 | 全 `app/`（executor 47、assistant 30、api 20、docparse 9 等） | 故障被静默吞掉，线上问题不可观测 |
| **P2** | 前端无单元测试 | `web/` | 轮询、错误解析、更新卡片等逻辑无回归保护 |

---

## 2. 分阶段路线图

工作量标记：**S**（单点改动，半天内）、**M**（跨 2-3 个文件，需设计）、**L**（涉及迁移/构建/多模块，需分步提交）。

### 阶段 0：安全与数据完整性护栏（P0）

**目标**：先保证"数据不会丢、凭据不会裸奔、升级不会把用户锁死"，再做性能与架构。

| 编号 | 任务 | 证据 | 改法 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| P0-1 | 密钥生命周期 | `app/crypto.py:32-61` | 显式 `POTATO_SECRET_KEY` 优先；Windows 下用 DPAPI（`CryptProtectData`）保护密钥文件；启动时用现有 `Credential` 样本做一次"能否解密"自检；提供密钥导出/导入；密钥缺失时进入专用提示页而非运行期崩溃 | M | 无 |
| P0-2 | 登录态加密迁移 | `app/models.py:108`、`570-604` | 新增迁移：把 `Project.login_state` 写入 `Credential(type="storage_state")`；一个版本内双读双写（优先 Credential，失败回退旧列）；两个版本后用 Alembic 删除旧列 | M | P0-1、阶段 1 的 Alembic |
| P0-3 | 更新可信链 | `app/updater.py:194,225-251` | 发布侧用 `signtool` 或 Azure Trusted Signing 签 exe 与安装器；更新侧用 WinVerifyTrust 校验 Authenticode；SHA-256 计算移出事件循环（`asyncio.to_thread`）；`launch_installer` 改为参数列表调用并等待父进程退出 | L | 证书/签名账户 |
| P0-4 | 迁移前快照 | `app/db.py:105` | `init_db()` 前对 SQLite 执行 `VACUUM INTO`，保留最近 3 份；快照失败则中止启动并给出可操作提示 | S | 无 |
| P0-5 | 默认安全 | `app/config.py:578`、`app/main.py:207,226` | 非 loopback 绑定且 `auth_enabled=false` 时拒绝启动（显式 `ALLOW_INSECURE=1` 才放行）；CORS 收敛到同源/白名单；`/artifacts` 改为短期签名 URL 或经鉴权的下载路由 | M | 无 |
| P0-6 | 存储迁移可恢复 | `app/api.py:5735-5757` | 迁移前写 manifest（源、目标、逐文件状态）；按文件记录完成状态，失败可续跑；同盘优先 `os.replace`，跨盘按大小校验后删除源 | M | 无 |

**验收**

- 删除密钥文件后，应用给出可操作的恢复提示；导入备份密钥后既有凭据可正常解密。
- 数据库升级前自动生成快照；人为中断迁移后重启可继续。
- 被篡改的安装包在下载后被拒绝；断网、哈希不符、无签名三种失败分支均有测试。
- 非 loopback 且未启用鉴权时启动失败；`ALLOW_INSECURE=1` 可临时放行。

**回滚**：保留旧列、旧密文与数据库快照；更新器保留上一版安装包；认证收紧提供 `ALLOW_INSECURE=1` 与 CLI 重置管理员。

---

### 阶段 1：数据层、迁移与检索（P0/P1）

**目标**：消灭半迁移风险与 N+1，建立可量化、可回退的检索基线。

| 编号 | 任务 | 证据 | 改法 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| P1-1 | 引入 Alembic | `app/db.py:1,105-241`、`pyproject.toml:77,87` | 以当前 `create_all` 结果生成 baseline `0001`；存量库用 `alembic stamp head` 接管；删除 `_ADDITIVE_COLUMNS`/`_ADDITIVE_INDEXES`；SQLite 变更用 `batch_alter_table` | L | P0-4 |
| P1-2 | 关键索引 | `app/db.py:212-216` | 补 `run(project_id,status)`、`run_result(run_id,status)`、`knowledge_chunk(group_id,chunk_index)`、`test_case(project_id,status)`；用 `EXPLAIN QUERY PLAN` 验证命中 | S | P1-1 |
| P1-3 | 项目列表聚合 | `app/api.py:1126-1216` | 用 `LEFT JOIN`/相关子查询一次取 `case_count`、最近 run、登录态计数，替代逐项目查询；50 个项目目标 ≤3 条 SQL | M | P1-2 |
| P1-4 | 检索改造 | `app/knowledge.py:384-419,422-543` | 关键词改 SQLite FTS5（`trigram` 分词，适配中文子串；启动探测，不支持回退 `LIKE`）；向量列改 BLOB float32，numpy 批量点积取 top-K 后再取正文；`embed_project` 改 `bulk_update_mappings` 批量写 | L | P1-1、回填脚本 |
| P1-5 | 失败摘要下推 | `app/api.py:4883` | 用窗口函数取每个用例最近一次失败，Python 只做确定性合并，避免全量取回 | M | P1-2 |

**迁移策略**

1. 先在有代表性的旧库副本上执行 `alembic upgrade head`，记录耗时与锁表时间。
2. 启动路径改为：快照 → Alembic 版本检查 → 迁移 →（失败自动恢复快照）。
3. 新旧检索路径用 feature flag（如 `RETRIEVAL_BACKEND=fts|like`）隔离，一个版本后删除旧路径。
4. 向量列迁移期间保留旧 `embedding` TEXT 列双读；回填完成后切读 BLOB 列。

**验收**

- 空库、旧库、半迁移库三条路径可重复执行且幂等。
- 启动时不再执行 30 余次 inspector 查询。
- 1 万知识块的检索 p95 < 300ms；`GET /projects`（50 个项目）p95 < 150ms 且 ≤3 条 SQL。
- `failure_digest` 不再全量回传历史结果。

**回滚**：迁移前快照 + Alembic downgrade；检索 feature flag 切回旧实现；旧列保留一个版本。

---

### 阶段 2：运行时性能与稳定性（P1）

**目标**：让长任务不阻塞主循环，让空闲页面不制造无谓查询，让启动更快。

| 编号 | 任务 | 证据 | 改法 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| P2-1 | SSE 事件化 | `app/api.py:5135-5156` | 新增进程内 `RunEventBus`，run 状态变更时推送；`stream_run` 订阅事件，保留 2s 兜底轮询；前端 `web/src/pages/RunReport.tsx` 增加断线重连 | M | 无 |
| P2-2 | 文档处理离开事件循环 | `app/api.py:1458,1822,1953,2241,2427,3377` | 统一走有界线程池（`asyncio.to_thread` + 信号量）并设置超时；避免在请求协程内直接调用 `docparse.extract` | M | 无 |
| P2-3 | 解析器自身优化 | `app/docparse.py:132-177,303-336,401-473` | 有 OCR 需求时 PDF 只解析一次（fitz 同时取文字层与渲染图）；OCR 用 2-4 线程按页并行；RTF 改为单遍状态机 | M | P2-2 |
| P2-4 | 启动瘦身 | `app/main.py:186-198`、`app/desktop.py:220-298` | `browser_use`/`executor`/`feishu` 延迟导入；启动自检（孤儿对账、回填）改为后台任务并记录完成日志；窗口先出现再加载重模块 | M | 无 |
| P2-5 | 连接策略 | `app/db.py:56` | 按事件循环维护连接池（替代全局 `NullPool`），保留测试/Celery 下的 `NullPool` 开关；SSE 与列表轮询请求合并 | M | P2-1 |
| P2-6 | 长任务进度 | `app/api.py:5678-5760`、前端存储设置页 | 更新下载显示百分比/速度/取消；存储迁移暴露进度接口，前端显示进度条 | M | P0-6、P0-3 |

**验收**

- 空闲运行页每秒数据库查询数从 1 降为 0，前端仍能秒级刷新。
- 上传 50MB PDF 期间 `/health` p95 不受影响；扫描件处理耗时下降 ≥30%。
- `import app.main` < 2.5s；桌面窗口可交互 < 8s。
- 并发 20 个请求无 `database is locked`，建连次数显著下降。

**回滚**：事件总线故障时回退 2s 轮询；连接池异常时切回 `NullPool`；解析器保留旧实现开关。

---

### 阶段 3：包体积与发布流程（P1）

**目标**：把"必须装"和"按需装"分开，同时让更新过程可观测、可回退。

| 编号 | 任务 | 证据 | 改法 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| P3-1 | 核心/完整双包 | `scripts/build_desktop.py:99-173` | 核心包只含 FastAPI/浏览器执行/基础文档解析；OCR（cv2、pymupdf、rapidocr、onnxruntime）与嵌入模型（24M）作为可选能力包，首次使用时下载并校验签名；保留完整离线包 | L | P0-3 |
| P3-2 | 立即瘦身 | `scripts/build_desktop.py:36-52` | 删除 `pymupdf` 的 `mupdf-devel`（约 6.7MB）与未使用 DLL/类型文件；**不要**删除 `playwright/driver/node.exe`（执行器必需） | S | 无 |
| P3-3 | 更新体验 | `app/updater.py:225-251`、前端更新卡片 | 下载进度/速度/剩余时间；取消下载；安装前提示保留上一版本；失败一键回退 | M | P3-1 |
| P3-4 | 依赖可复现 | `pyproject.toml:16-94`、`constraints.txt`、`tests/test_dependency_lock.py` | 用 `uv` 生成跨平台 `uv.lock`，CI 与打包共用；产出 SBOM；保留依赖锁测试作为快速门禁 | M | 无 |

**验收**

- 核心包 `dist` ≤ 300MB、安装器 ≤ 110MB；完整包 ≤ 400MB / ≤ 160MB。
- 未安装可选能力时，OCR/向量化给出明确降级提示，不影响其他功能。
- 更新界面有真实进度且可取消；安装失败可回到上一版本。
- 干净环境按锁文件安装后可复现构建。

**回滚**：保留完整离线安装包；能力包下载失败不影响核心功能；保留上一版安装器。

---

### 阶段 4：架构拆分（P2，可与阶段 3 并行后置）

**目标**：降低合并冲突与评审成本，不改变外部行为。

| 编号 | 任务 | 证据 | 改法 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| P4-1 | 拆分 `api.py` | `app/api.py` 5816 行 | 按域拆为 `routers/projects.py`、`runs.py`、`knowledge.py`、`docs.py`、`auth.py`、`storage.py`；第一步只做纯移动，用 OpenAPI 快照对比确保契约不变；之后才改逻辑 | L | P1-3、P1-5 |
| P4-2 | 拆分执行器/助手 | `app/executor.py` 3399 行、`app/assistant.py` 3035 行 | executor 抽出浏览器生命周期、录制、判定注入；assistant 抽出工具注册表与 LLM 调用；目标单文件 < 1200 行 | L | P2-2、P2-3 |
| P4-3 | API 契约演进 | 现有路由 | 新增 `/api/v1` 前缀别名，旧路径保留两个版本并在响应头标记弃用；列表接口新增分页参数，默认行为保持不变 | M | P4-1 |

**验收**

- 拆分前后 OpenAPI 文档 diff 为空（第一步）。
- 每步拆分后相关测试与前端构建通过。
- `api.py`、`executor.py`、`assistant.py` 行数下降且职责边界清晰。

**回滚**：每个文件/每个路由一个提交，可独立 revert；OpenAPI 快照作为回归门禁。

---

### 阶段 5：测试与错误治理（P1，贯穿全程）

**目标**：让故障可观测，让高风险路径有回归保护；不追求数字好看。

| 编号 | 任务 | 证据 | 改法 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| P5-1 | 宽 except 治理 | 全 `app/`，231 处 | 保留失败隔离语义，但异常分支必须 `logger.exception` 并带上下文；优先处理无日志的 `docparse`（9 处）、SSE、存储迁移、更新分支；目标 ≤120 处且剩余均有注释与日志 | M | 贯穿 |
| P5-2 | 补关键测试 | `tests/` | updater 的代理/超时/哈希/签名分支（`respx`）；Alembic 空库/旧库/半迁移；docparse 坏 PDF/RTF/超限；SSE 断线；存储迁移续跑；桌面 headless 入口 | M | 各阶段 |
| P5-3 | 前端测试 | `web/` | 引入 Vitest，覆盖 `web/src/lib/hooks.ts` 轮询、`web/src/lib/api.ts` 错误解析、更新卡片 | M | 无 |
| P5-4 | 覆盖率门槛 | `pyproject.toml:139` | 全量 54%→60%，`api.py` 28%→40%、`executor.py` 45%→50%；只对核心域设门槛 | M | P5-2 |

**验收**

- 宽泛 except 数量 ≤120，且新增/保留项必须记录异常与上下文。
- CI 覆盖率门槛按阶段上调，且不发生回退。
- 高风险路径的失败分支均有测试。

**回滚**：仅涉及测试与门禁，出现误报时调整门槛或标记 flaky，不回滚业务代码。

---

## 3. 破坏性变更的迁移策略

用户已授权破坏性变更，但必须遵守"双写窗口 + 快照 + 可回退"三原则：

1. **数据模型**
   - 所有结构变更走 Alembic，禁止继续手写 `ALTER`。
   - 删列/改类型前至少保留一个版本的双读双写；删除动作单独提交，可 downgrade。
   - 每次启动迁移前自动 `VACUUM INTO` 快照，保留 3 份。

2. **凭据与密钥**
   - 密钥迁移前先验证旧密文可解；无法解密时停止迁移，不写入新格式。
   - 登录态迁移先回填、再切换读路径、最后删列。

3. **API 契约**
   - 旧路径保留两个版本，响应头标记 `Deprecation`；新前缀 `/api/v1` 先做别名。
   - 列表分页参数默认不改变旧行为。

4. **依赖**
   - `constraints.txt` 升级为 `uv.lock`，先并行验证一轮完整构建，再删除旧文件。
   - `browser-use`/`playwright` 的精确锁定保持，升级需单独验证执行器启动与录制。

5. **安全默认值**
   - 认证默认收紧必须配套 `ALLOW_INSECURE=1` 逃生开关、CLI 重置管理员、升级说明。

---

## 4. 依赖与工具取舍

### 新增

| 工具 | 用途 | 理由 |
|---|---|---|
| Alembic | 数据库迁移 | 取代 30 余条手写 ALTER；SQLite 用 `batch_alter_table`；已作为可选依赖声明（`pyproject.toml:77,87`） |
| uv + `uv.lock` | 依赖锁定 | 当前只锁直接依赖，传递依赖会漂移；CI 与打包共用同一份锁 |
| respx | HTTP mock | 覆盖 updater 的代理/超时/哈希分支，避免真实网络 |
| pytest-timeout | 测试超时 | 防止异步/SSE 测试卡死 |
| pytest-benchmark | 性能门禁 | 把检索、列表接口的延迟目标变成可回归指标 |
| signtool / Azure Trusted Signing | 代码签名 | 更新可信链必需，不是 Python 依赖 |

### 零新增

- **FTS5**：SQLite 内置，用于中文子串检索。
- **numpy**：已在依赖中（`pyproject.toml:65`），用于批量向量计算。

### 明确不引入

- `torch`、`sqlite-vec`、FAISS：Windows + PyInstaller 下 ABI/体积风险高；待单项目知识块超过 5 万再评估。
- Postgres/Redis/Celery 到桌面端：单机场景不需要。
- Qt 重写 UI、纯 CDP 自研替换 Playwright：收益不足以覆盖重写成本。
- onefile 打包：首启更慢、杀软误报更多。

---

## 5. 度量与验收

| 指标 | 基线 | 目标 | 测量方式 |
|---|---|---|---|
| `import app.main` | 4.48s | < 2.5s | `python -X importtime -c "import app.main"` |
| 桌面可交互 | 待埋点 | < 8s | `POTATO_DESKTOP_HEADLESS=1` 后解析 `desktop.log` 时间戳 |
| `GET /projects` p95（50 项目） | N+1 | < 150ms，≤3 条 SQL | httpx 压测 200 次 + SQLAlchemy `before_cursor_execute` 计数 |
| 检索 p95（1 万块） | Python 全量点积 | < 300ms | `pytest-benchmark` + 固定语料 |
| SSE 空闲数据库查询 | 1 次/秒 | 0 次/秒 | 查询计数器/日志 |
| 扫描 PDF 100 页 | 串行 OCR | 耗时 -30% | 固定样本计时 |
| `dist_desktop` / 安装器 | 487MB / 170.9MB | 核心 ≤300MB / ≤110MB；完整 ≤400MB / ≤160MB | `du -sh` + 安装包文件大小 |
| 覆盖率 | 54%（api 28%） | 60%（api 40%） | `pytest --cov=app --cov-report=term-missing` |
| 宽泛 except | 231 | ≤120 且均有日志 | `grep -rn "except Exception" app \| wc -l` |

**门禁规则**：基线未复测不立项；优化后指标不得回退；性能类 PR 必须附前后对比数据。

---

## 6. 实施顺序、提交策略与回归

### 推荐顺序

1. P0-1 密钥 → P0-4 快照 → P1-1 Alembic → P0-2 登录态 → P0-3 更新签名 → P0-5 默认安全 → P0-6 存储迁移。
2. P1-2 索引 → P1-3 列表聚合 → P1-5 失败摘要 → P1-4 检索改造。
3. P2-1 SSE → P2-2/P2-3 文档解析 → P2-4 启动瘦身 → P2-5 连接池 → P2-6 进度反馈。
4. P3-2 立即瘦身 → P3-4 依赖锁定 → P3-1 双包 → P3-3 更新体验。
5. P4 拆分与 P5 治理按阶段穿插，P5-1 优先处理新增代码。

### 分支与提交

- 分支：`p0/safety`、`p1/data`、`p1/runtime`、`p1/release`、`p2/arch`；每个阶段独立合入。
- 遵循项目纪律：**每个提交只改一个文件**；迁移脚本独立提交；重构与行为变更分开提交。
- 阶段间用 feature flag 隔离（如 `RETRIEVAL_BACKEND`、`SSE_BUS`、`OCR_PACK`），验证后再删除旧路径。

### 阶段回归清单

```bash
ruff check .
# mypy 严格子集（见 pyproject.toml:165-167）
mypy app
pytest tests/ -q
python scripts/build_desktop.py        # 涉及打包时
npm --prefix web run build
npm --prefix web run knip              # 若 package.json 配置了该脚本
```

数据迁移额外演练：**旧库副本 → 升级 → 业务读写 → 回滚 → 再次升级**。

发布流程：内部灰度（旧版 → 新版 → 主动回退），确认数据库、密钥、存储目录均可逆后再全量。

---

## 7. 风险登记表

| 风险 | 触发条件 | 影响 | 缓解与回滚 |
|---|---|---|---|
| 密钥/密文迁移导致凭据不可解 | 换机、备份缺密钥、迁移中断 | 高：用户账号数据全部失效 | 迁移前 dry-run 统计可解数量；保留旧列/旧密钥；失败恢复快照 |
| Alembic 在 SQLite 上重建表 | 大表、磁盘不足、并发写入 | 高：锁库、迁移半完成 | 迁移前 `VACUUM INTO`；低峰执行；大表分批；失败恢复快照 |
| 认证默认收紧把老部署锁在门外 | 升级后无管理员账号 | 中：服务不可用 | `ALLOW_INSECURE=1` 临时开关；CLI 重置管理员；升级文档说明 |
| 检索改造后召回退化 | FTS5 trigram 不可用/中文分词不理想 | 中：AI 回答质量下降 | 保留 `LIKE` 回退与旧向量列；feature flag 一键切回 |
| 可选能力包导致离线用户功能缺失 | 无网络/下载失败 | 中：OCR/向量化不可用 | 完整离线包；明确降级提示；下载包签名校验 |
| 拆 `api.py` 引入路由/权限回归 | 移动时遗漏依赖或鉴权装饰器 | 高：越权/404 | OpenAPI 快照对比；纯移动单独提交；逐步 revert |
| 包体裁剪误删 DLL | 删除判断错误 | 高：启动即崩 | headless 冒烟作为门禁；保留上一版安装器 |
| 连接池改造与多事件循环冲突 | Celery/测试各自 `asyncio.run` | 中：`another operation is in progress` | 按事件循环隔离；保留 `NullPool` 开关；并发测试覆盖 |

---

## 8. 不建议做的事

- **不要**用 Qt 重写桌面 UI；现方案（pywebview + WebView2）已覆盖目标平台。
- **不要**为省 90MB 自研 CDP 替换 Playwright `node.exe`；执行器稳定性优先。
- **不要**为单机产品引入 Postgres/Redis/Celery 或拆微服务。
- **不要**现在引入 sqlite-vec/FAISS/torch；FTS5 + numpy 足以吃到大部分收益。
- **不要**做 onefile 打包；首启更慢、杀软误报更多。
- **不要**"宽 except 全删"或追求 100% 覆盖率；失败隔离是产品语义，目标是可观测。
- **不要**在代码签名与回滚能力落地前扩大自动更新范围。

---

## 附录：快速复测命令

```bash
# 导入耗时
python -X importtime -c "import app.main" 2>&1 | tail -20

# 测试与覆盖率
pytest tests/ -q
pytest --cov=app --cov-report=term-missing

# 宽泛异常数量
grep -rn "except Exception" app | wc -l

# 包体
du -sh dist_desktop dist_desktop/PotatoTest/_internal

# 依赖一致性
pytest tests/test_dependency_lock.py tests/test_sqlite_pragmas.py -q
```

> 注：`import app.main` 会加载配置与模型；测量时确保未设置会触发真实数据库连接的测试环境变量。
