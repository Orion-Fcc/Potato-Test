# 参与开发

这是个人维护的项目，没有"上游私有仓库"之类的流程 —— 直接改这个仓库就行。
下面是在本机把开发环境跑起来、以及提交前的自检步骤。

## 开发环境

```bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m playwright install chromium

# macOS / Linux
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

前端（开发时用 Vite 热更新，它会把 `/api` 和 `/artifacts` 代理到后端）：

```bash
cd web && npm install && npm run dev
```

只想起服务、不需要热更新的话，跑 `python run_server.py 18081`
（加 `pythonw` 是后台无窗口运行，日志会写进 `logs/potato.log`）。

## 提交前自检

```bash
ruff check .                 # 静态检查（配置在 pyproject.toml）
python -m pytest tests/      # 快，不需要浏览器和 LLM
cd web && npm run build      # 类型检查 + 构建前端
```

## 约定

- **测试保持"不需要浏览器"**：engine / judge / 解析逻辑用纯函数测；真正的浏览器行为交给
  `scripts/smoke.py`，它跑一次真实链路。
- **不要随便升级 `browser-use` 和 `playwright`。** 这两个在 `pyproject.toml` 里是锁死版本的：
  `browser-use` 的 Agent 参数和 `ChatOpenAI` 签名跨版本会变，而且它约束 `openai<3`。
  升级后务必确认 `Agent(max_actions_per_step=..., step_timeout=..., use_vision=...,
  register_new_step_callback=..., extend_system_message=...)` 这些参数仍然存在。
- **改数据库结构要配 Alembic 迁移**：`alembic revision --autogenerate -m "说明"`。
  （纯本地跑用的是 SQLite + `create_all`，可以不迁移，但 Docker 部署会走 `alembic upgrade head`。）
- **`.env` 里注释必须单独占一行。** 写成 `KEY=   # 说明`，dotenv 会把 `# 说明` 当成值 ——
  这个坑在 `JWT_SECRET` 上会直接变成"签名密钥是公开字符串"的安全问题。
- **提交信息**：`<type>: <描述>`，type 用 `feat fix refactor docs test chore perf ci`。
- 一个提交只做一件逻辑上的事，描述里写清"为什么"。

## Windows 批处理脚本

`*.bat` 一律 **GBK 编码 + CRLF**，并且**不要**在里面调用 `chcp`（调了中文反而乱码）。
`.gitattributes` 里已把 `*.bat` 标为二进制，避免 git 改行尾时破坏编码。

## 这个仓库是公开的：不要写进真实环境信息

代码、注释、文档、测试、README 截图里都**不要**出现：

- **LLM 网关地址与真实模型名**（例如某家厂商的 `api.<vendor>.cn`）。它不长得像密钥，
  但它说明"谁在用哪家网关"，而且比密钥更难轮换 —— 换网关要走流程。
  示例一律用 `https://api.example.com/v1` / `example-model`。
- **被测系统的真实地址**。内网地址外网不可达，但能反推"给谁做的项目"。
  示例用 RFC 5737 的文档地址段：`192.0.2.10`。
- **客户名 / 租户名 / 角色账号前缀 / 管理员显示名**（示例：`示例`、`role01`、
  `系统管理员`）。用例和数据用通用写法。
- **本机的绝对路径**（`D:\某人的工作目录\…`）。测试里需要路径就用 `tmp_path` 或相对路径。
- 任何凭据：`.env`、`*.db`、`.potato-secret.key`、`profiles/`、`artifacts/` 都在
  `.gitignore` 里，别用 `-f` 强加。

推之前跑一遍检查（`--head` 看的是**远端此刻能看到的版本**，那才是"有没有泄漏"的答案）：

```bash
python scripts/check_secrets.py            # 工作区
python scripts/check_secrets.py --head     # 已提交版本 ★
python scripts/check_secrets.py --history  # 全部历史（慢，发布前跑一次）
```

它会检查高危路径、`.env`/数据库里**真正配置着的**值、密钥形状、以及上面那张
"本项目真实标识"清单（`scripts/check_secrets.py::KNOWN_IDENTIFIERS`）。

**改那张清单时，要同步任何脱敏/重写脚本的替换规则** —— 两份不一致的后果是
"检查说干净、实际没抹掉"。对应的回归测试在 `tests/test_no_secrets_committed.py`。

注意：**历史是删不掉的。** 一旦某个字符串进过一次提交，公开仓库上它就存在过
（`git log -p` 能看到），重写历史 + 强推也只是让新克隆看不到它 —— 旧的 commit
仍可被按 SHA 访问，fork 会保留，而且强推会打断所有已有克隆。所以：
**真凭据一旦泄漏，正确处置是轮换，不是重写历史。**
