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

**如果将来想改成 `P2-TC-001` 这种带项目前缀的形式**：那需要迁移 563 条
存量编号，且历史报告里的旧编号会对不上号。用户 2026-10-04 明确选择
**保持 TC-001 格式**，只修重号缺陷。

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

---

## 八、改过隔离相关代码的完整文件清单

| 文件 | 改动 |
|---|---|
| `app/executor.py` | 新增 `_capture_profile_dir()`；`capture_session()` 加 `project_id` 参数并传 `user_data_dir` |
| `app/engine.py` | `_ensure_bundle()` 捕获时带上项目的 project_id |
| `app/api.py` | `capture_credential` / `recheck_credential` 传入 project_id；`_next_case_key` 改为"最大值+1" |
| `app/models.py` | `TestCase` 加 `(project_id, case_key)` 复合唯一约束 |
| `tests/test_isolation_and_policy.py` | 隔离与策略的回归防线（22 条） |
