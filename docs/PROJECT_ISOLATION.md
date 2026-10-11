# 项目隔离说明（共同记忆）

> 这份文档是给**人**和**AI 助手**一起看的。目的：记录 Potato Test 里"哪些东西是
> 项目之间共享的、哪些是隔离的、边界在哪"。后续任何人（或任何 AI）动这块代码前，
> 先读这里，不要凭感觉把项目耦合起来。

最后更新：2026-10-04

---

## 一、为什么要写这份文档

用户的原话：

> "为什么每个项目之间的数据可以共存，我需要你做数据隔离……虽然我这些项目之间
> 可能确实有关联，但是我不希望你把他们弄得这么紧密，因为你不知道他们的具体关系。"

关键在于**"你不知道它们的真实关系"**。当前库里有：

| 项目 ID | 名称 | 用例数 |
|---|---|---|
| 1 | 培训资源管理 | 378 |
| 2 | 飞行学员管理 | 179 |

这两个项目在业务上**可能**有关联（比如同一套内网系统、同一批账号），
但**具体怎么关联只有用户知道**。所以系统的默认立场是：

> **默认隔离，关联由用户显式建立。**

绝不替用户"顺手打通"——比如自动复用另一个项目的账号、自动按编号跨项目引用用例。
一旦打通，用户在不知情的情况下就会拿到一份混合了两个项目数据的报告。

---

## 二、隔离边界总表

| 资源 | 隔离粒度 | 存储位置 / 键 | 说明 |
|---|---|---|---|
| 测试用例 | 按项目 | `test_case.project_id` | 用例编号**只在项目内唯一** |
| 用例编号 | 按项目 | `(project_id, case_key)` 唯一 | 见下方"用例编号"一节 |
| 执行账号 | 按项目 | `credential.project_id` | 含角色账号，绝不跨项目取用 |
| 环境 | 按项目 | `environment.project_id` | base_url 绑在环境上 |
| 运行记录 | 按项目 | `run.project_id` | |
| 测试套件 | 按项目 | `test_suite.project_id` | |
| 问题/ticket | 按项目 | `issue.project_id` | |
| 浏览器 profile | 按项目 | `profiles/project_{id}/slot{n}` | cookies/缓存/localStorage 全部隔离 |
| 会话捕获 profile | 按项目 | `profiles/project_{id}/_capture` | ★ 2026-10-04 才修，见下方"已修复的泄漏" |
| 用例经验记忆 | 按项目 | `case_memory`（按 case_id） | |
| 产物（截图/视频） | 按项目 | `artifacts/{project_id}/...` | |
| **登录用户** | **全局共享** | `app_user` | ⚠️ 见下方"刻意共享的部分" |
| **知识库** | **全局共享** | `knowledge_chunk` | ⚠️ 所有项目共用一份 |
| **LLM 配置** | **全局共享** | `.env` / 设置页 | 所有项目用同一个模型 |

---

## 三、已修复的泄漏（2026-10-04）

### 3.1 会话捕获用了全局浏览器 profile ★ 最严重

**现象**：给 B 项目捕获登录态时，浏览器里还留着 A 项目的 cookies，
捕获出来的 session bundle 可能带着 A 项目的会话 —— B 项目的用例就这样
"用上了别人家的账号和缓存"。

**根因**：`app/executor.py::capture_session` 创建 Browser 时**完全没传
`user_data_dir`**，于是走 browser-use 的默认 profile 目录，那是所有项目共用的一份。

**修法**：新增 `_capture_profile_dir(project_id)`，落在
`profiles/project_{id}/_capture`；`capture_session` 增加 `project_id` 参数，
三处调用点全部传入。

**为什么用独立子目录而不是复用 `slot0`**：捕获可能和正在跑的用例并发，
共用同一个 Chromium profile 会撞"profile 被占用"，两边都起不来。

**回归防线**：`tests/test_isolation_and_policy.py` 里有一条源码级断言，
检查 `capture_session` 里必须出现 `user_data_dir=` ——
谁把它删了，测试立刻红。

### 3.2 用例编号会重号

**问题**：旧 `_next_case_key` 用 `COUNT(*)` 当序号。删掉 TC-005 之后总数少 1，
下一条新建的又拿到 TC-005，两条不同用例撞同一个编号。

**修法**：改成取**已用编号的最大值 +1**，序号只增不减。
同时加 `(project_id, case_key)` 复合唯一约束，把"项目内不许撞号"
从约定变成数据库强制。

**注意**：是**复合**唯一，不是 case_key 单列唯一。单列唯一会让项目 2
建不了自己的 TC-001 —— 每个项目从 TC-001 开始是**设计如此**，不是 bug。

---

## 四、刻意共享的部分（不要"修"它们）

### 4.1 登录用户是全平台共享的

`.env` 里 `SHARED_WORKSPACE=true` + `AUTH_ENABLED=false`：本地实例免登录，
**任何一个登录用户对所有项目都有全部权限**。这是为了单人本地使用方便，
不是权限漏洞。要改必须同时改 `auth` 模块和设置页，不要只改一个地方。

### 4.2 知识库是全平台共享的

`knowledge_chunk` 没有 `project_id`。如果用户说"知识库也要按项目隔离"，
那是一次**表结构变更 + 上传入口改造**，不是加个 WHERE 就完事 ——
要跟用户确认历史数据怎么归类。

### 4.3 LLM 配置是全平台共享的

模型、网关、token 上限都在 `.env` / 设置页全局生效。

---

## 五、用例编号的跨项目重名（已知，非缺陷）

每个项目的编号都从 `TC-001` 开始，所以：

- `TC-001` 在项目 1 和项目 2 里**都存在，是两条不同的用例**
- 光看 `TC-001` 无法判断属于哪个项目

**处理原则**：

1. 任何按编号查找的地方**必须带 `project_id`**（见 `_find_case_by_key` 的约定）。
2. 报告、缺陷单、消息推送里提到编号时，要带上项目名。
3. **前置条件里引用其他用例时，只在同一个项目内解析** ——
   跨项目引用会让项目重新耦合，这正是要拆开的东西。
   实现见 `app/engine.py::resolve_case_dependencies`。

**如果将来想改成 `P2-TC-001` 这种带项目前缀的形式**：那需要迁移 1203 条
存量编号，且历史报告里的旧编号会对不上号。用户 2026-10-04 明确选择
**保持 TC-001 格式**，只修重号缺陷。

### 5.1 编号由**项目永久计数器**发放（2026-10-09）

编号"不该重发"这件事修过三次，记在这里免得再退回去：

| 时间 | 算法 | 缺陷 |
|---|---|---|
| 最初 | `COUNT(*)` 当序号 | 删掉**任意**一条 → 下一条重发它的号（直接撞号） |
| 2026-10-04 | 现有最大编号 +1 | 删掉**最大**编号 → 下一条重发它 |
| 2026-10-09 | `project.case_seq` 永久计数器 | —— |

前两版的共同根因：**把"下一个号"当成能从当前数据推导出来的东西**。
它不是 —— 它是"发行过就不回收"的事实，必须自己存一列：

- `project.case_seq`，只增不减，删用例**不动**它；
- 老库该列是后加的，值为 `0` = 尚未回填；第一次发号时从现有用例的最大编号补上
  （`_backfill_case_seq`），否则老项目会从 TC-001 重发并撞存量数据；
- **手工指定**编号（新建 / 编辑 / Excel 导入自带）也要计入计数器
  （`_reserve_case_key`），否则自动编号以后会一路发到它并撞车；
- Excel 里的自由文本编号（`REQ-12`）不参与计数（`int()` 会抛，把整批导入打断）。

**⚠️ 计数器只管 `TC-<数字>` 这一个命名空间**（2026-10-09 在真库上实测确认）：
存量数据的编号**并不统一**。真库实测的形态：

| 项目 | 主力编号形态 | `TC-NNN` 形态 |
|---|---|---|
| 1 培训资源管理 | `TP-ACFG-001` / `TP-APR-012`…（`TP-<模块>-NNN`） | 0 条 |
| 2 飞行学员管理 | `TC-FLT-001`、`TC-RN-01`、`12-3`… | 391 条 |
| 4 一般补充培训 | `TC-GRM-001`… | 54 条 |
| 5 飞行学员补充培训 | `AN` / `BN` / `EN-01`… | 0 条 |

所以"删掉最大编号会不会重发"这个问题，**只对 `TC-NNN` 这一族有意义** ——
其它形态都是导入时自带的分模块编号，既不参与自动发号，也不会被自动发号撞到
（自动编号只发 `TC-<数字>`）。

推论：**项目 1 的计数器从 TC-001 起是正确行为**，不是"忘了回填 368"——
它一条 `TC-NNN` 都没有，`TP-ACFG-368` 属于另一个命名空间。
若将来要用这个计数器统一接管分模块编号（`TP-ACFG-369` 之类），
要先把 `_key_number` 的正则扩到那些形态，并确认不会与手工导入撞号。

回归防线：`tests/test_case_key_counter.py`（16 条），
其中"删掉最新那条之后号不再出现"和"全删光也不从 TC-001 重来"是核心两条。

---

## 六、改动这类代码时的检查清单

动隔离相关代码前，逐条过一遍：

1. 新加的查询带 `project_id` 了吗？（漏了就是跨项目泄漏）
2. 新起的浏览器传 `user_data_dir` 了吗？（漏了会用全局默认 profile）
3. 新加的按编号查找，是否可能匹配到别的项目的同名编号？
4. 如果新增"跨项目"能力，**先问用户**，别自己打通。
5. 跑 `tests/test_isolation_and_policy.py` —— 里面的断言就是为了拦这几类问题。

---

## 七、测试数据隔离（写给改测试的人）

**这个坑踩过两次，务必读完再写测试。**

本仓库大量测试用这种写法切库：

```python
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_db}"
```

**这种写法在本项目里是不可靠的**，原因有三个叠加：

1. `app/db.py::_ensure()` 把 engine/sessionmaker **缓存在模块级全局变量**里。
   同一个 pytest 进程里只要更早的测试触发过 `_ensure()`，engine 就绑死在
   真实 `potato.db` 上；之后再改环境变量**毫无作用**。
2. `pydantic-settings` 读 `.env` 与环境变量的优先级在这个环境下表现不一致
   （实测：单独 `python -c` 时环境变量生效，在 pytest 里同一个赋值却被
   `.env` 里的值盖掉）。
3. 变了 engine 却没变 `_Session`，或者反过来，都会拿到半新半旧的组合。

**实测后果**：2026-10-04 两次把测试数据写进用户的真实库
（先 6 个名为 `P` 的项目，后 14 个 `DEP`/`OTHER` 项目）。

**正确写法**（见 `tests/test_isolation_and_policy.py` 的 `dep_project` fixture）：
**直接构造 engine 并赋值**，绕开环境变量与 `.env` 解析：

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app import db as dbmod
from app.models import Base

eng = create_async_engine("sqlite+aiosqlite:///" + tmp_path, future=True, poolclass=NullPool)
dbmod._engine = eng
dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

async def _create():
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
asyncio.run(_create())

# 关键：自证隔离有效。没有这条断言，隔离失效时会**静默污染真实数据**。
assert "_tp_xxx.db" in str(eng.url), f"没连到临时库：{eng.url}"
```

**那条 assert 不是形式主义**：本文件第一次跑时就是因为它才发现
"设了环境变量却仍连真实库"，当时已经往真库写了 12 个项目。

### 7.1 想在**真库**上跑测试：用 `real_db`（2026-10-09 加）

上面那套临时库解决的是"不要写脏真库"，但它也带来一个盲区：
**临时库测出来的"通过"不代表真库上通过**。真实数据有它的形状 ——
1203 条用例、多角色、多环境、31 份凭据、1295 条知识库切片、各种 NULL / 空 JSON 列 ——
这些边界只有真库里有。

所以 `tests/conftest.py` 现在多了一条路径：`real_db` fixture。
**它不是"放开写"，而是"写了也会回滚"**：

```python
def test_reads_real_data(real_db):
    n = real_db.run(_count(TestCase()))   # 读的是真数据
    assert n > 0
```

机制：手动开一个外层事务 → 把 sessionmaker 绑到这条连接上，用
`join_transaction_mode="rollback_only"` → 结束时**无条件 rollback**。
`rollback_only` 的语义是"这个会话里的 commit 不可能生效"，
所以测试里 `await s.commit()` 看起来成功、事务内也读得到，但永远不落盘。

**★ 用错模式会真的写脏真库 —— 这条是实测踩出来的**：
第一版用了 `join_transaction_mode="create_savepoint"`，结果两条
`__ROLLBACK_PROBE__` 真的写进了 `potato.db`（已清除）。在真库副本上对比实测：

| `join_transaction_mode` | 结果 |
|---|---|
| `create_savepoint` | **LEAKED**（SAVEPOINT 的 RELEASE 在外层事务里是持久化的） |
| default（`conditional_savepoint`） | ROLLBACK OK |
| `rollback_only` | ROLLBACK OK ← **选它** |

任何时候要改这个 fixture，先跑 `tests/test_real_db_mode.py` ——
它里面有一条"换一条独立连接去看，标记行必须不存在"的断言，
就是专门拦上面那个坑的。

三条防线的分工（**都要留着**）：

| 机制 | 作用 |
|---|---|
| 会话级临时库 | **预防**：默认写入目标就不是真库 |
| `_never_touch_the_real_db` | **探测**：真库行数变了就 fail |
| `real_db` fixture | **受控例外**：确实要在真数据上跑时，写了也回滚 |

### 7.4 真库里出现 9 个测试项目 —— 一次真实事故（2026-10-10）

**症状**：真库 `potato.db` 里凭空多出 9 个项目（名字是 `reconcile-check` /
`live-check` / `fk-check`，共 18 条用例、12 个 run、18 条 run_result）。
更麻烦的是**报错出现在别的文件里** —— `tests/test_stale_run_reconcile.py`
跑全量套件时 3 条 ERROR，单独跑却全绿。

**成因**：15 个测试文件里重复出现同一个模式：

```python
db._engine = db._Session = None      # "先把上一个测试的 engine 丢掉"
...
await db.init_db()                   # ← 按 settings 重建 = 连真库
```

上半句看着人畜无害，下半句就把所有写入送到了 `potato.db`。
上面那三条防线**都拦不住它**：

* 会话级临时库只在**会话开始时**设一次，被置 None 之后就回不来了；
* `_never_touch_the_real_db` 是**事后**比对 —— 数据已经写进去了，用户已经能看见了；
* `real_db` 是自愿使用的 fixture，那些文件根本没用它。

**修法（三层，缺一不可）**：

| 层 | 位置 | 作用 |
|---|---|---|
| 哨兵 | `app/db.py::_ensure` | pytest 里重建 engine 时，**目标 URL 含 potato.db 就直接抛** |
| 逐测试兜底 | `tests/conftest.py::_db_points_at_a_temp_db_before_every_test` | 每个测试**开始前**都确保指向临时库 |
| 显式绑定 | `tests/conftest.py::bind_temp_db` / `restore_db` | 给"要自己控制库文件"的测试用的正确写法 |

为什么修在 conftest 而不是去改那 15 个文件：那是 15 处抄来抄去的样板，
改它们既容易漏、以后还会有人再写一遍。而"每个测试都从一个临时库开始"
本来就该是整个套件的默认前提。

**顺带发现的两个坑**（都被测试钉住了）：

1. `_real_db_url()` 原来解析 `get_settings().database_url`，而 `DATABASE_URL`
   会被别的测试留在环境变量里 → `real_db` 会把**那个临时库**当真库。
   现在直接指向仓库根的 `potato.db`，不受环境变量影响。
2. `real_db` 必须**显式依赖**那条逐测试 fixture，否则 pytest 让临时库最后生效。
   踩过：六条用例一起读空库、报"真库里一个项目都没有"，完全指不到是顺序问题。
   `tests/test_real_db_mode.py::test_the_real_db_fixture_documented_its_ordering`
   把这条依赖钉住了。

**教训**：`db._engine = None` 这种"清干净"的写法，在一个 engine 由
**settings 惰性重建**的系统里，等价于"把我的写入目标交还给配置文件"。
要换库就**直接赋值**，要收尾就**还原原值**，永远不要置 None。


---

## 八、改过隔离相关代码的完整文件清单

| 文件 | 改动 |
|---|---|
| `app/executor.py` | 新增 `_capture_profile_dir()`；`capture_session()` 加 `project_id` 参数并传 `user_data_dir` |
| `app/engine.py` | `_ensure_bundle()` 捕获时带上项目的 project_id |
| `app/api.py` | `capture_credential` / `recheck_credential` 传入 project_id；`_next_case_key` 改为"最大值+1" |
| `app/config.py` | `database_url` 默认从 `./potato-test.db` 改为 `./potato.db`（2026-10-09）—— 没有 `.env` 的入口以前会静默连到一个空库 |
| `tests/conftest.py` | `real_db` fixture：真库 + 外层事务 + `rollback_only`，写入回滚（2026-10-09）|
| `tests/test_real_db_mode.py` | 真库模式的自证：连的是真库、写入回滚、默认引擎不许是真库 |
| `app/models.py` | `Project.case_seq` 永久计数器（2026-10-09）|
| `app/api.py` | `_next_case_key` 改用计数器；新增 `_backfill_case_seq` / `_reserve_case_key` / `_key_number`；抽出 `_project_with_counts`；新增 `GET /projects/{pid}` |
| `app/db.py` | `_ADDITIVE_COLUMNS` 加 `("project","case_seq","INTEGER NOT NULL DEFAULT 0")` |
| `tests/test_case_key_counter.py` | 编号计数器 + 单项目端点的回归防线（16 条）|
| `app/models.py` | `TestCase` 加 `(project_id, case_key)` 复合唯一约束 |
| `tests/test_isolation_and_policy.py` | 隔离与策略的回归防线（22 条） |
