# Deployment manual

## Architecture

One image runs every process; docker-compose starts the full stack:

| Service | Role |
|---|---|
| `db` | Postgres 16 (data) |
| `redis` | Redis 7 (Celery broker) |
| `api` | FastAPI — REST + SSE + serves the built SPA at `/`. Runs `alembic upgrade head` on start. |
| `worker` | Celery worker — executes test cases (each case = one Chromium) and GitLab sync tasks |
| `beat` | Celery beat — suite reminders, stale-run reconcile, GitLab polling (when enabled) |
| `feishu` | Feishu bot long-connection worker (dials **out** to Feishu — no public callback URL needed). Idle unless `ENABLE_FEISHU=true` and the bot is configured; safe to delete the service otherwise. |

## Image layout (two layers)

- **`Dockerfile.base`** (`potato-test-base`) — Python 3.11 + every dependency from
  `pyproject.toml` + Playwright Chromium + its OS libs. Rebuild only when
  `Dockerfile.base` or `pyproject.toml` change (slow: downloads Chromium).
- **`Dockerfile`** — stage 1 builds the Vite SPA (`node:20-slim`), stage 2 starts
  `FROM` the base and only copies `app/`, `alembic/`, and the built frontend.
  Installs nothing, so day-to-day builds take seconds.

## First deployment

```bash
git clone <your-fork> potato-test && cd potato-test
cp .env.example .env
```

Edit `.env` — the minimum for a real deployment:

- `POTATO_SECRET_KEY` — Fernet key (see [configuration.md](configuration.md#security))
- `GATEWAY_BASE_URL` / `GATEWAY_API_KEY` / `GATEWAY_MODEL` — your LLM endpoint
- `POSTGRES_PASSWORD` — anything but the default
- For multi-user: `AUTH_ENABLED=true`, `JWT_SECRET`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `PUBLIC_BASE_URL`

Then:

```bash
docker build -f Dockerfile.base -t potato-test-base:latest .   # once
docker compose build
docker compose up -d
curl -f http://localhost:8000/health
```

Open `http://<host>:8000` — API and UI share one origin.

## Upgrades

```bash
git pull
docker compose build          # fast — base unchanged
docker compose up -d          # api runs alembic upgrade head on start
```

Rebuild the base first only if `pyproject.toml` or `Dockerfile.base` changed.

The worker has `stop_grace_period: 12m`: a deploy during a run waits for in-flight
cases (up to their timeout) instead of SIGKILLing them mid-browser — killed cases
are already acked and would be silently lost.

## Sizing the browser budget

Each running case is one Chromium (~0.5 GB RAM). `MAX_GLOBAL_CONCURRENCY` is the
global ceiling and **is** the Celery worker's `--concurrency` (compose wires it).
Default 16 ≈ 8 GB peak. Size it to the host's free RAM.

## Ports / reverse proxy

The API listens on 8000. To publish on another port, add a
`docker-compose.override.yml`:

```yaml
services:
  api:
    ports: !override ["8010:8000"]
```

Behind nginx/Caddy, proxy to `:8000` and keep SSE unbuffered
(`proxy_buffering off;` for `/api/…/events`).

## Internal DNS

If the app under test resolves only through an internal resolver, uncomment the
`dns:` block in `docker-compose.yml` — containers otherwise use public DNS and
will NXDOMAIN internal names.

## Migrations

- `api` runs `alembic upgrade head` at start (`RUN_MIGRATIONS=1` default).
- `worker` / `beat` / `feishu` set `RUN_MIGRATIONS=0` so exactly one process migrates.
- Manual: `docker compose exec api alembic upgrade head`.

## Artifacts

Videos/screenshots/history live in the shared `artifacts` volume (`ARTIFACT_DIR`),
served by the API at `/artifacts/…`. Point `S3_ENDPOINT` at any S3-compatible
store to upload instead (see [configuration.md](configuration.md#artifact-storage)).

## 桌面版（Windows，一个人本机用就用这个）

不需要 Docker、不需要服务器。打一次包、装一次即可：

```bat
:: 1) 打程序目录（PyInstaller，约 2-3 分钟；产物 dist_desktop\PotatoTest\）
.venv\Scripts\python scripts\build_desktop.py

:: 2) 编译安装包（需要装一次 Inno Setup）
::    装到默认位置时：
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\potato.iss
::    只装了当前用户时（不需要管理员）：
"%LOCALAPPDATA%\InnoSetup6\ISCC.exe" installer\potato.iss
:: 产物：dist_desktop\installer\PotatoTest-Setup-1.0.0.exe
```

安装包**不含任何数据** —— 没有 `potato.db` / `artifacts` / `profiles` / `.env`，
装完首次启动自动建空库。装到 `%LOCALAPPDATA%\Programs\PotatoTest`（per-user，不需要管理员），
安装时可选桌面快捷方式与开机自启。

> ★ **别装到 `Program Files`**：数据库、上传的原件、浏览器 profile 都落在程序目录旁边，
> 那边对普通用户不可写 —— 症状是"能打开、一保存就报错"，而且错误只在日志里。
> `.iss` 里加了安装前的目录可写检查，就是为了拦住这种装法。

`desktop.py` 是桌面版入口：同一进程里起 uvicorn，再开一个原生窗口
（pywebview → Edge WebView2；没装 pywebview 时退化为 Edge 的 `--app` 窗口）。
端口默认 18081，被占用会自动换一个。**网页版和桌面版是同一套服务**，
同一台机器用浏览器打开就是全功能，不需要额外部署。

## Running without Docker

有一条命令做完的版本（幂等，重复跑只补缺的部分）：

```bash
git clone https://github.com/Orion-Fcc/Potato-Test.git && cd Potato-Test && ./scripts/deploy.sh
```

手工版：

```bash
pip install -e .
playwright install --with-deps chromium
# web/dist 随仓库发布，所以这一步只在你要改界面时才需要：
# cd web && pnpm install && pnpm build && cd ..
# 没配 WEB_DIST 时 run_server.py 会自动使用 ./web/dist
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
# optional, for GitLab sync + queued runs:
celery -A app.celery_app.celery worker -l info --concurrency=16
celery -A app.celery_app.celery beat -l info
```

Without `REDIS_URL`, runs execute in-process in the API (fine for small setups)
and GitLab sync stays disabled.

### 飞书机器人不需要单独起进程（2026-10-06 起）

机器人**默认随 API 服务一起启动和停止**（`FEISHU_WORKER_IN_PROCESS=true`），
所以上面的启动命令已经包含了它 —— 不要再去起第二个进程。

为什么改：以前它只能靠桌面 `.bat` 额外 `start pythonw -m app.feishu_poll`。于是
"服务起来了、机器人没起"这种部署状态下，群里 @机器人 问状态永远没人理，而且
`logs/` 下**什么都不会有** —— 没有进程，自然没有日志。排查一个不存在的问题，
是最浪费时间的一类故障。

三种启动方式的关系（都靠 `potato-feishu-poll` 内核级单实例锁排队，抢不到的会
自己退出并记录原因，**绝不会出现一条消息回两次**）：

| 场景 | 用什么 | 配置 |
|---|---|---|
| 本机 / 单机部署（默认） | 进程内轮询，随服务起停 | 什么都不用做 |
| 想独立控制机器人进程 | `python -m app.feishu_poll` | `FEISHU_WORKER_IN_PROCESS=false` |
| Docker Compose | 仓库自带的 `feishu` 服务（WebSocket 长连接） | compose 里已设 `FEISHU_WORKER_IN_PROCESS=false` |

轮询用的是普通 REST 调用，**不依赖 `lark-oapi`**（那个 SDK 光 import 在冷缓存下
要几分钟，不适合当一个"随时可能在群里被问一句"的进程的启动成本）。只有
`python -m app.feishu_ws` 这条 WebSocket 路线才需要 `pip install -e ".[feishu]"`。

机器人日志在 `logs/feishu_poll.log`（独立文件：服务日志是 WARNING 起步，
而"群里为什么没人回"恰恰只能靠这里的 INFO/WARNING 看出来）。
