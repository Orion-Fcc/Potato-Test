<div align="center">

# Potato Test

**Agentic end-to-end testing** — write test cases in plain natural language, let a browser
agent execute them in a real Chrome, and let an LLM judge decide pass/fail with evidence.

[![CI](https://github.com/Orion-Fcc/Potato-Test/actions/workflows/ci.yml/badge.svg)](https://github.com/Orion-Fcc/Potato-Test/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Node 20](https://img.shields.io/badge/node-20-339933.svg)](web/package.json)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

English · [简体中文](./README.zh-CN.md)

<img src="docs/screenshots/run-report.png" alt="Run report — pass/fail per case with the judge's written verdict" width="900">

</div>

```
natural-language case ──▶ browser agent (browser-use → Playwright/CDP → Chromium)
                                │  video + screenshots + action history
                                ▼
                          LLM judge ──▶ pass / fail + reason ──▶ aggregated run report
```

## Why

Traditional E2E suites (Playwright/Cypress scripts) break every time a selector changes. Potato Test cases are *prompts*, not scripts:

> "Log in as the reviewer role, open the pending-approval list, approve the first entry, and verify its status changes to Approved."

The agent figures out the clicks; the judge reads the evidence and defaults to **failed** when the evidence is thin — no flaky green.

## Screenshots

| Test cases — plain-language, grouped by module | Runs — live progress over SSE |
| :---: | :---: |
| ![Test cases](docs/screenshots/cases.png) | ![Runs](docs/screenshots/runs.png) |

| **Replay — video, judge verdict, step-by-step agent trace** | **Project overview — pass-rate trend & top failing cases** |
| :---: | :---: |
| ![Replay](docs/screenshots/replay.png) | ![Overview](docs/screenshots/overview.png) |

| **Issue kanban — failed cases become tracked issues** | **Runtime LLM settings — bring your own model, no redeploy** |
| :---: | :---: |
| ![Issues](docs/screenshots/issues.png) | ![LLM settings](docs/screenshots/settings-llm.png) |

## Features

- **Natural-language test cases** — project-scoped, reusable, tagged, Excel (.xlsx) import/export
- **Real-browser execution** — one Chromium per case via [browser-use](https://github.com/browser-use/browser-use), with mp4 video, per-step screenshots, and a replayable action-history JSON
- **LLM-as-judge** — pass/fail with a written reason; conservative by design (thin evidence ⇒ failed); infra errors are retried and flaky passes are marked, judge failures are not retried
- **Bring your own model** — any OpenAI-compatible endpoint; base URL / key / models are configurable at runtime in **System settings** (no redeploy), with a separate optional agent model (e.g. a local VLM)
- **Runs & reports** — concurrent execution with a global browser budget, live progress over SSE, per-case replay, re-run *only failed* or *only errored* cases, run comparison
- **Environments & accounts** — per-project environments (test/staging/…), multiple login accounts with roles; session capture & reuse (cookies + localStorage + **sessionStorage** via CDP) so cases start logged in instead of burning steps on the login page
- **Issue tracking** — built-in Kanban; optional **two-way GitLab issue sync** (off by default — `ENABLE_GITLAB=true`)
- **Feishu (Lark) bot** — optional (off by default — `ENABLE_FEISHU=true`): bind a project to a group chat, trigger runs by chatting, result cards, feedback messages become issues, Bitable sync
- **Multi-user** — optional login, shared workspace or per-project owner/editor/viewer roles, email invites
- **Bilingual UI** — English / 简体中文

## Quick start — native, zero infrastructure (recommended)

No Docker, no Postgres, no Redis: the app runs on **SQLite** and an **in-process queue**, and it
creates its own encryption key on first start. All you need is Python ≥ 3.11.

**Windows** — double-click `start-potato.bat` (it creates the venv, installs deps, builds the
frontend if needed, and opens the browser). Or by hand:

> **Port**: defaults to `18080`. If it is taken, the launcher scans upward for a free port
> automatically, so a leftover container or another service can never block startup. Pass an
> explicit one when you want it: `start-potato.bat 19000`. (`18000` is the stock value and is
> commonly held by an older Docker deployment, where the listener shows up as Docker Desktop's
> `com.docker.backend.exe` rather than a Python process.)

```bat
python -m venv .venv
.venv\Scripts\python -m pip install fastapi "uvicorn[standard]" "sqlalchemy[asyncio]" aiosqlite ^
    pydantic-settings httpx openai sse-starlette python-multipart cryptography bcrypt pyjwt ^
    openpyxl playwright
.venv\Scripts\python -m playwright install chromium
copy .env.example .env
.venv\Scripts\python -m uvicorn app.main:app --port 18080
```

**macOS / Linux**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install fastapi "uvicorn[standard]" "sqlalchemy[asyncio]" aiosqlite pydantic-settings \
    httpx openai sse-starlette python-multipart cryptography bcrypt pyjwt openpyxl playwright
playwright install chromium
cp .env.example .env
uvicorn app.main:app --port 18080
```

Then open <http://127.0.0.1:18080>. Configure the LLM under **System settings → LLM model**
(click **Test connection** to verify before running anything) — or set `GATEWAY_BASE_URL` /
`GATEWAY_API_KEY` / `GATEWAY_MODEL` in `.env`. Both work; the UI overrides `.env`.

Relative paths in `.env` (`./potato.db`, `./profiles`, `./artifacts`) resolve against the project
root, and the API auto-serves `web/dist` once the frontend is built — so the server works no
matter which directory you launch it from.

### Frontend

```bash
cd web && npm install && npm run build   # emits web/dist, which the API serves
```

For hot reload during development, run `npm run dev` (Vite proxies `/api` and `/artifacts` to the
backend) instead of building.

## Docker deployment (optional)

Prefer this when you want Postgres, Redis and Celery for a multi-user deployment. One image runs
the whole stack (API + SPA + Celery worker/beat + Feishu worker), plus bundled Postgres and Redis:

```bash
cp .env.example .env                                          # set POTATO_SECRET_KEY etc.
docker build -f Dockerfile.base -t potato-test-base:latest .  # deps + Chromium (once)
docker compose build
docker compose up -d
open http://localhost:8000
```

Setting `REDIS_URL` is what turns Celery sync on; leave it empty and even a Docker deployment
falls back to the in-process queue.

See **[docs/deployment.md](docs/deployment.md)** for the full manual (image layout, scaling the browser budget, ports, migrations) and **[docs/configuration.md](docs/configuration.md)** for every setting.

## Project layout

```
app/
  config.py     settings (.env)
  models.py     Project · TestCase · Run · RunResult · Issue · User · …
  engine.py     run loop: concurrent drain + failure isolation + aggregation
  executor.py   one browser per case, recording, session capture/restore (CDP)
  judge.py      LLM-as-judge (defaults to 'failed' on thin evidence)
  llm.py        OpenAI-compatible clients (agent + judge)
  api.py        REST + SSE
  storage.py    artifacts: S3-compatible or local disk
  gitlab_*.py   two-way issue sync (Celery worker + beat)
  feishu*.py    Lark bot: commands, cards, feedback → issues, Bitable
web/            React + Vite + Tailwind SPA
alembic/        migrations (container entrypoint runs upgrade head)
docs/           deployment & configuration manuals, design specs
tests/          pytest suite (pure logic, no browser needed)
```

## How judging works

Each case produces: final URL trail, step-by-step action history with screenshots, and an mp4. The judge gets the case's expected outcome plus this evidence and must answer passed/failed **with a reason**, in your configured `REPORT_LANGUAGE`. Timeouts/connection errors count as *infra errors* (retried up to `CASE_RETRIES`, and a case that only passes on retry is flagged *flaky*); a judge "failed" is never retried.

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Run `ruff check .` and `python -m pytest tests/` before pushing.

## License

[MIT](LICENSE)
