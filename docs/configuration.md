# Configuration reference

All settings come from environment variables (or a `.env` file — copy `.env.example`).
Defaults shown are the code defaults from `app/config.py`.

## Database

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./potato-test.db` | SQLite for dev; use `postgresql+asyncpg://user:pass@host:5432/potato-test` in production. docker-compose wires this to the bundled Postgres automatically. |
| `POSTGRES_PASSWORD` | `potato-test` | Only used by docker-compose to provision the bundled `db` service and build `DATABASE_URL`. |

## Optional integrations

Both are **off by default** — a plain install needs neither. Enabling one shows its
settings UI and activates its endpoints/workers.

| Variable | Default | Notes |
|---|---|---|
| `ENABLE_GITLAB` | `false` | Two-way GitLab issue sync (also needs `REDIS_URL` + the GitLab settings below). |
| `ENABLE_FEISHU` | `false` | Feishu (Lark) bot + Bitable mirror. |

## LLM gateway

Both the browser agent and the judge speak the OpenAI API. Any OpenAI-compatible endpoint works (OpenAI, a corporate gateway, vLLM/Ollama serving a VLM, …).

The env values below are **defaults** — an admin can override base URL, API key, and
models at runtime in **System settings → LLM model** (key stored encrypted; changes
apply to new runs without a redeploy).

| Variable | Default | Notes |
|---|---|---|
| `GATEWAY_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL. |
| `GATEWAY_API_KEY` | *(empty)* | API key for that endpoint. |
| `GATEWAY_MODEL` | `gpt-4o` | Shared default model; the **judge always uses this**. Needs vision (screenshots are part of the evidence). |
| `AGENT_MODEL` | *(empty)* | Optional separate model for the browser agent; empty falls back to `GATEWAY_MODEL`. E.g. run the agent on a local VLM and keep a stronger judge. |
| `GATEWAY_VERIFY_SSL` | `true` | Set `false` for gateways with self-signed certificates. |
| `GATEWAY_MAX_TOKENS` | `16000` | Completion cap for the agent's structured output. browser-use needs a high cap — don't lower it casually. |
| `REPORT_LANGUAGE` | `Chinese (简体中文)` | Language for the agent's reasoning and the judge's written reason, e.g. `English`. |

## Artifact storage

| Variable | Default | Notes |
|---|---|---|
| `S3_ENDPOINT` | *(empty)* | Empty ⇒ store artifacts (videos, screenshots, history) on local disk under `ARTIFACT_DIR`. Set to any S3-compatible endpoint (MinIO, RustFS, AWS…) to upload instead. |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | *(empty)* | Credentials for the endpoint. |
| `S3_BUCKET` | `potato-test` | |
| `S3_REGION` | `us-east-1` | |
| `ARTIFACT_DIR` | `./artifacts` | Local artifact root (also the compose shared volume). |

## Security

| Variable | Default | Notes |
|---|---|---|
| `POTATO_SECRET_KEY` | *(empty)* | **Required for stored credentials.** Fernet key encrypting test-account passwords/sessions at rest. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Empty ⇒ the credential API is disabled. |

## Execution limits

| Variable | Default | Notes |
|---|---|---|
| `CASE_TIMEOUT_S` | `60` | Hard wall-clock cap per case (login + task). A per-project override in the UI wins. |
| `CASE_MAX_STEPS` | `30` | Agent step budget per case. |
| `CASE_RETRIES` | `0` | Auto-retries for **infra errors only** (timeouts, connection failures). A case that passes only on retry is marked *flaky*. Judge failures are never retried. |
| `SESSION_TTL_MIN` | `25` | How long the app-under-test keeps a login session alive. A captured login is re-used until this expires, then re-captured once (single-flight). Keep it just under the real session lifetime. |
| `DEFAULT_CONCURRENCY` | `2` | Per-run worker count (in-process fallback only). |
| `MAX_GLOBAL_CONCURRENCY` | `16` | Global ceiling on concurrent browsers across **all** runs/users (~0.5 GB RAM each). With Celery this **must** equal the worker's `--concurrency` — compose reads it from `.env` and does this for you. |

## Token 消耗：钱花在哪，以及哪些旋钮真的省（2026-10-09 实测）

先看数（3 条真实列表/查询用例，走真网关，`python _measure_tokens.py --case 1,2,3`）：

| 项 | 实测 |
|---|---|
| 每条用例 | ~41,800 token（agent + 判定器） |
| 每步 | ~15,700 token |
| 输入 : 输出 | 97 : 3 |
| 缓存命中 | 58%~89%（同一批用例不同轮次） |

**每步那 ~15,700 里，约 12,000 与页面无关** —— 每次 LLM 调用都要重发一遍：

| 组成 | ~token/次调用 | 能不能动 |
|---|---|---|
| browser-use 自带 system prompt | 6,000 | 不能（它在库里写死，改它等于改 agent 行为） |
| 本项目的规则（`_*_RULE`） | 4,100 | 能动，但**每条都对应一次真实质量事故**（见各自注释） |
| 工具 schema（20 个 → 17 个） | 2,000 → 1,816 | 能动（见下） |

**结论：token 主要花在"调用次数 × 每次固定开销"，不在页面内容上。**
所以省 token 的第一杠杆是**减少调用次数**（步数），第二是砍每次的固定开销。

### 已做的（无质量代价）

`EXCLUDED_FILE_TOOLS` —— 排除 `write_file` / `read_file` / `replace_file`。
依据：拿真库 1203 条用例的 prompt/expected/preconditions/test_data/name 检索，命中 **0 条**；
而这三个工具的 schema 会出现在**每一次**调用里。实测省 **239 token/次调用**
（schema 8,223 → 7,264 字符），按 368 条用例 × ~3 次调用算约 **264k token/轮**。

⚠️ **刻意保留**的（有真实用例依赖，别顺手删）：`upload_file`（38 条用例要传测试数据文件）、
`save_as_pdf`（94 条用例涉及 PDF，测的是被测系统自己的 PDF 功能，而这个工具可能是
查看 PDF 证据的唯一途径）、`extract`（从已渲染文本归纳，真人会做）。

### 还没做的高杠杆项（需要你拍板，因为都涉及质量取舍）

| 手段 | 省多少 | 代价 |
|---|---|---|
| 让 agent 每步多批量几个动作（`MAX_ACTIONS_PER_STEP` 5 → 8） | 步数减少 → 按比例省 | 一步里动作越多，"前一个动作失败、后面全打在旧状态上"的风险越大 |
| 压缩本项目那 4,100 token 的规则 | 每次调用最多省 ~2,000 | 每条规则都是为一次真实质量事故加的，压缩=把那类事故请回来 |
| 关 `AGENT_USE_THINKING` | 输出侧（只占 3%） | 思考变少，判断准确性下降 |

**不建议**动的：`AGENT_USE_VISION`（默认已关）。它看着省 token，实则相反 ——
`use_vision=True` 会让**每步**都带一张截图，而截图按 token 计价非常贵；
按需的 `look_at_screen` 工具（默认开）才是省钱的那条路。

### 2026-10-09 复盘：为什么"再省 20%"没有做到，以及什么才是真正的省法

用户的原始要求是「尽量降低该系统的 agent 的消耗，然后不能使执行速度变慢
还有和执行用例的质量降低」。我去做了，但**必须如实报告结论**：

**规则压缩的天花板很低。** 精确测了每个规则块的字符数，然后删掉其中
"给维护者看的实测证据"（例如"实测 44 步那条用例是把同一个动作重试了十几次…"）——
这类文字有说服力，但**模型不需要被说服，它只需要被告知**。结果：
`_ANTI_WASTE_RULE` 等共减少约 190 字符，而整套规则是 16,000 字符 / 3,800 token。

也就是说：**把这 3,800 token 全删掉，每步也只省 24%**（15,700 → 11,900），
而 24% 里每一条规则都对应一次真实的质量事故。这个交换不划算，
所以我把上限做成了**护栏**而不是继续砍：

```python
# tests/test_speed_levers.py
_MAX_RULE_CHARS = 17_000   # 超了就红，逼后来者先量一下值不值
```

**真正的省法是减少调用次数，不是砍固定开销。** 固定开销是
`调用次数 × 每次 ~11.6k`，所以"少走一步"省下的钱等于"删掉 6,000 字符规则"。
而实测每步约 15,700 token 里，页面状态与历史占了约 4,100 ——
那正是步数多的时候会膨胀的部分。

**这次实际落地、无质量代价的部分：**

1. 规则里的"实测证据"文字精简（省 ~190 字符/次调用）。
2. 新增护栏，保证它不会以同样的方式长回去。
3. **不再增加**固定开销：新功能（后端探索）挂在已有的 CDP 拦截钩子上，零额外调用；
   接口清单进提示词时按被调用次数排序**截断到前 25 个**（`summarize_for_prompt`），
   而不是把整张表塞进去 —— 一个活跃系统能有几百个接口。

**没做、也不建议做的**：为了让数字好看而删规则、关思考、或提高批量数。
用户明确要求"不能降低质量"，而这些都属于拿质量换 token。

**下一步真正值得做的**（需要真实运行数据才能验证，不该拍脑袋改）：
用 `executor` 已经落库的 `timing.steps` + `usage` 找出"步数明显多于同轮同类用例"
的那些用例 —— 它们才是 token 的真正去处。`app/perf_analysis.py` 已经能识别
"每步耗时是同轮中位数 3 倍以上"的异常用例（实测抓到了单步 271s 的 3 条），
同一套思路可以直接用来找"步数异常"的用例。

### 怎么自己量

```bash
python _measure_tokens.py --list              # 有哪些可测用例
python _measure_tokens.py --case 1,2,3 --tag before
# ...改配置...
python _measure_tokens.py --case 1,2,3 --tag after
```

它跑在**真库的副本**上（复制到临时文件并断言连的是副本），不会碰你的 `potato.db`。
结果同时写 `.tok_<tag>.json`，可以直接 diff。

> ⚠️ 单条用例的 token 数**波动很大**：实测同一批用例两轮，步数从 8 步变成 18 步，
> 总 token 从 125k 变成 301k。所以别用"跑一次比一次"下结论 ——
> 要比就同一个 tag 多跑几条，或者用上面那种**固定状态**的对比（工具 schema 那种）。

每次运行的实际消耗已经落库（`run_result.timing.usage`）并在**运行报告**里按用例显示
（含缓存命中占比）。历史行没有这个字段时界面显示 `—`，**不是 0** ——
"未测量"和"零消耗"必须能区分。

## GitLab issue sync (optional)

Two-way sync between the built-in Kanban and a GitLab project. Requires `ENABLE_GITLAB=true` **and** `REDIS_URL`; otherwise issues stay local and creation never blocks.

| Variable | Default | Notes |
|---|---|---|
| `REDIS_URL` | *(empty)* | Celery broker/result backend, e.g. `redis://localhost:6379/0`. Compose wires the bundled Redis. |
| `GITLAB_BASE_URL` | `https://gitlab.com/api/v4` | Your GitLab instance's API v4 base. |
| `GITLAB_TOKEN` | *(empty)* | Optional server-wide `api`-scope token; projects then only pick a GitLab project in the UI. A per-project token (stored encrypted) overrides it. |
| `GITLAB_VERIFY_SSL` | `true` | |
| `GITLAB_POLL_INTERVAL_S` | `60` | Beat poll interval for pulling status/label/comment changes back. |

## Auth / multi-user (optional)

| Variable | Default | Notes |
|---|---|---|
| `AUTH_ENABLED` | `false` | Require login on every endpoint. |
| `SHARED_WORKSPACE` | `false` | `true`: any logged-in user has full access to every project (login gate only). `false`: per-project RBAC via owner/editor/viewer membership; admins see all. |
| `JWT_SECRET` | *(empty)* | HS256 signing key (`openssl rand -hex 32`); required once `AUTH_ENABLED=true`. |
| `JWT_TTL_HOURS` | `168` | Session cookie lifetime. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | *(empty)* | Seed the first admin on first startup when no users exist. |
| `PUBLIC_BASE_URL` | *(empty)* | Canonical public URL — drives invite-email links and the address shown in the UI. |

### Invite emails (SMTP)

Best-effort: a send failure is logged, never raised — an invite still returns a copyable link.

| Variable | Default |
|---|---|
| `EMAIL_FROM` | `noreply@example.com` |
| `EMAIL_HOST` / `EMAIL_PORT` | `smtp.example.com` / `25` |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | *(empty — unauthenticated relay)* |
| `EMAIL_USE_SSL` / `EMAIL_USE_TLS` | `false` / `false` |

## Feishu (Lark) bot (optional)

Requires `ENABLE_FEISHU=true`. Create a self-built app on the Feishu open platform, then either set these or configure them at runtime in **System settings** in the UI.

Two scopes are needed, and a version must be **published** for either to take effect:

- `im:message` — send cards and replies.
- `im:message.history:readonly` — read group history. **Without it the bot can push but never sees a question**, which is exactly what 「群里问『状态』没人理」 looks like. The worker logs a hint mentioning this scope when the API answers `99991672`.

| Variable | Default | Notes |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | *(empty)* | Empty ⇒ bot disabled. |
| `FEISHU_WORKER_IN_PROCESS` | `true` | Run the bot **inside the API process** so it starts and stops with the server. Set `false` if something else already runs one: the bundled `docker-compose` `feishu` service (WebSocket transport), or a standalone `python -m app.feishu_poll`. All three take the same kernel-level single-instance lock, so the extras bow out on their own instead of answering twice. |
| `FEISHU_POLL_INTERVAL_SEC` | `8` | Poll interval — also the worst-case latency before the bot answers. After a failure the worker backs off by doubling, capped at 60 s (was 300 s: a transient blip then looked like a dead bot for five minutes). |
| `FEISHU_IGNORE_PROXY` | `true` | Ignore `HTTP(S)_PROXY` when calling `open.feishu.cn`. Set `false` only if Feishu is reachable solely through a proxy. |
| `FEISHU_VERIFICATION_TOKEN` | *(empty)* | Event-subscription verification token; only used by the HTTP-webhook transport. |
| `FEISHU_API_BASE` | `https://open.feishu.cn` | Use `https://open.larksuite.com` for international Lark. |
| `FEISHU_AUTO_ANSWER_DETECTED` | `true` | Also answer questions detected from context, not just @-mentions / DMs. |
| `FEISHU_AMBIENT_REQUIRE_KEYWORD` | `true` | Cost gate: non-@ messages need a problem keyword before the LLM classifies them. `false` ⇒ classify every message (higher cost). |

The in-process worker needs **no** extra dependency — it talks plain REST. Only the
WebSocket transport (`python -m app.feishu_ws`, which docker-compose uses) needs
`pip install -e ".[feishu]"`.

## Serving the SPA

| Variable | Default | Notes |
|---|---|---|
| `WEB_DIST` | *(empty)* | Path to the built frontend, served by the API at `/`. The Docker image sets `/app/web_dist`. **Left empty, `run_server.py` auto-detects `./web/dist`** — which ships in the repo, so a fresh clone serves the UI with no configuration and no Node install. Only set it explicitly to point somewhere else. |
