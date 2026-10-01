"""Settings loaded from environment / .env."""

from __future__ import annotations

import logging
import os
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
    gateway_max_tokens: int = 16000  # completion cap for the browser agent's structured output
    agent_max_tokens: int = 0  # cap for the browser agent's per-step call; 0 = use gateway_max_tokens
    report_language: str = "Chinese (简体中文)"  # language for agent reasoning + judge reason

    # --- optional integrations (both OFF by default — a plain install needs neither) ---
    # Flip via ENABLE_GITLAB / ENABLE_FEISHU. Off hides the UI and 404s the endpoints.
    enable_gitlab: bool = False  # two-way GitLab issue sync
    enable_feishu: bool = False  # Feishu (Lark) bot + Bitable mirror

    # Fernet key encrypting stored test credentials at rest (empty => credential API disabled).
    # The old TESTPILOT_ name still works so an existing .env keeps functioning after the rename.
    secret_key: str = Field(
        default="", validation_alias=AliasChoices("POTATO_SECRET_KEY", "TESTPILOT_SECRET_KEY")
    )

    # Empty S3_ENDPOINT => store artifacts on local disk under ./artifacts
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "potato-test"
    s3_region: str = "us-east-1"

    default_concurrency: int = 2  # per-run worker count (in-process fallback only)
    # Global ceiling on concurrent browsers across ALL runs/users (each ~0.5 GB RAM).
    # Celery path: set the worker's --concurrency to this (each case == one browser).
    # In-process fallback: an asyncio.Semaphore(this) shared by all runs.
    max_global_concurrency: int = 4  # live Chromium budget; 16 needs ~8 GB+ and swaps a 16 GB box
    # Step budget is the PRIMARY control on a case, not wall clock: a step is one
    # think+act round-trip, and a simple case finishes in 8-15 of them while a long
    # wizard needs 30+. Running out of steps ends the case cleanly and reports how far
    # it got, which is far more actionable than a clock cutting it off mid-action.
    case_max_steps: int = 40
    # Wall clock is a SAFETY NET, not the budget. A time-based cap that does not scale
    # with the work is what produced the "every case times out" symptom: a 60s cap
    # aborts a case that legitimately needs 90s, then reports it as a failure the code
    # cannot fix. Default is generous and, importantly, RAISED automatically per step
    # (see effective_case_timeout) so the clock only ever fires on a genuine hang.
    case_timeout_s: int = 300  # per-case wall-clock safety net; per-project override wins
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
    # Screenshot upload cadence for the live view. Every step costs a CDP screenshot
    # plus an artifact write; every Nth step + always the final one is plenty for a
    # progress feed, and this is one of the largest per-case overheads on a long run.
    live_shot_every: int = 3
    # Playwright video encoding is pure CPU per frame. Off = faster, no replay video.
    case_record_video: bool = True
    # Trace capture adds a second recording stream; off = faster, no timeline trace.
    case_record_trace: bool = True
    # browser-use draws an index badge on every interactive element each step. On a
    # heavy SPA that is an extra full-page DOM pass + repaint per step. The a11y tree
    # already numbers the elements the model acts on, so this is usually pure overhead.
    browser_highlight_elements: bool = False
    # Per-step model call is the dominant cost and was unbounded; a wall-clock guard
    # keeps one slow step from eating the whole case budget.
    step_timeout_s: int = 45
    # How many independent actions the agent may batch into ONE model round-trip.
    # Step count is the cost of a case, and form-filling steps are naturally batchable.
    max_actions_per_step: int = 5
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
    # Ambient monitoring cost gate: non-@ messages need a problem keyword to be
    # processed. Set False to LLM-classify EVERY non-@ message (never drop, higher cost).
    feishu_ambient_require_keyword: bool = True

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

        return self

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
