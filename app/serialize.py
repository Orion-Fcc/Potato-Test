"""把 ORM 行折成 API 返回的 dict：所有 `_xxx(row) -> dict` 的唯一归属地。

为什么单独一个模块
==================
`api.py` 一度 3370 行、87 个端点，里面夹着 21 个序列化/格式化函数（315 行）。
它们与端点逻辑的关系是"每个列表接口都要调一次"，但放在一起就分不清
"改这个 dict 会影响哪些页面"。

这些函数有一个共同特征：**只读 ORM 行，不碰数据库、不碰请求**。所以它们可以整体
搬走，且能在毫秒级单测里断言（不需要起 FastAPI、不需要连库）。

命名约定：`_xxx(row) -> dict`，前缀下划线表示"模块内部约定"，只在 api.py 与
本模块之间使用。

★ 这里定义的是**对外契约**：前端与报告都按这些键取值。改名等于改 API，
要么不动，要么前后端一起改。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

from app.config import get_settings
from app.failure_attrib import VERDICT_INFO
from app.judge import ROOT_CAUSE_IS_REAL_DEFECT, ROOT_CAUSE_LABELS_ZH
from app.models import (
    CaseChange,
    Credential,
    Environment,
    FeedbackItem,
    Issue,
    IssueComment,
    Notification,
    Project,
    Run,
    RunResult,
    TestCase,
    User,
)
from app.schemas import _roles_of


def _iso(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset, so browsers parse it as UTC.

    SQLite (via SQLAlchemy's DateTime(timezone=True)) does NOT preserve tzinfo: the column
    stores a UTC wall-clock value and hands back a NAIVE datetime. `.isoformat()` on a naive
    datetime emits '2026-10-02T02:37:08.681319' — no offset. A browser reads an offset-less
    string as LOCAL time, so on a GMT+8 machine every timestamp silently shifted back 8
    hours: the running-run tile showed '8h17m' elapsed for a run 19 minutes old.

    Everything the API emits goes through here (see `_expired` just below — the same trap,
    already handled there for comparisons). Do NOT go back to bare `.isoformat()`.
    """
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _user(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "is_admin": u.is_admin,
        "onboarded_at": _iso(u.onboarded_at),
    }


def _feishu_status(cfg: dict) -> dict:
    """Public-safe view of the Feishu config (secrets shown only as *_set booleans)."""
    return {
        "app_id": cfg["app_id"],
        "app_secret_set": bool(cfg["app_secret"]),
        "verification_token_set": bool(cfg["verification_token"]),
        "api_base": cfg["api_base"],
        "auto_answer_detected": cfg["auto_answer_detected"],
    }


def _llm_status(cfg) -> dict:
    """Public-safe view of the effective LLM config (key shown only as a boolean)."""
    return {
        "base_url": cfg.base_url,
        "model": cfg.model,
        "agent_model": cfg.agent_model,
        "api_key_set": bool(cfg.api_key),
    }


def _project(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "base_url": p.base_url,
        "has_login_state": bool(p.login_state),
        "gitlab_project": p.gitlab_project,
        "case_timeout_s": p.case_timeout_s,
        "case_max_steps": p.case_max_steps,
        "run_concurrency": p.run_concurrency,
        # 证据采集开关（用户自己选）。None 表示该字段从未设过，跟随服务器全局默认。
        "case_record_video": p.case_record_video,
        "live_shot_every": p.live_shot_every,
        "roles": p.roles or [],
        "feishu_chat_id": p.feishu_chat_id,
        "feishu_bitable_bound": bool(p.feishu_bitable_app_token and p.feishu_bitable_table_id),
    }


def _gitlab_web_url(project_ref: str | None, iid: int | None) -> str | None:
    """Best-effort browser URL for a synced GitLab issue (derives web host from the API base)."""
    if not project_ref or not iid:
        return None
    host = get_settings().gitlab_base_url.split("/api/v4", 1)[0].rstrip("/")
    return f"{host}/{project_ref}/-/issues/{iid}"


class LastResult(NamedTuple):
    """A case's most recent verdict — status, the run it came from, when it ran."""

    status: str
    run_id: int
    at: datetime | None


def _case(c: TestCase, last: LastResult | None = None) -> dict:
    return {
        "id": c.id,
        "project_id": c.project_id,
        # How this case last did. The cases table shows 通过/失败 + when + a link to that
        # report, so "is this case OK?" is answerable without opening every run.
        "last_status": last.status if last else None,  # passed|failed|error, None = never run
        "last_run_id": last.run_id if last else None,
        "last_run_at": _iso(last.at) if last else None,
        "case_key": c.case_key,
        "name": c.name,
        "module": c.module,
        "priority": c.priority,
        "type": c.type,
        "status": c.status,
        "owner": c.owner,
        "role": c.role,
        # 2026-10-04 multi-role: the ordered list the agent switches through.
        # Always at least [role] when a role is set, so the UI can bind to this one
        # field and render old single-role cases without a special case.
        "roles": _roles_of(c.role, c.roles),
        "references": c.references or "",
        "preconditions": c.preconditions or "",
        "prompt": c.prompt,
        "steps": c.steps or [],
        "test_data": c.test_data or "",
        # 2026-10-06 测试数据文件声明（供用例编辑页渲染）。
        # 永远序列化，哪怕解析失败也要把原文带回去 —— 否则用户在界面上看到空白，
        # 无从判断是自己没填还是后端把它吞了。
        "data_files": c.data_files,
        "data_files_error": _data_files_error(c.data_files),
        "data_hygiene": c.data_hygiene or "",
        "expected": c.expected,
        "start_url": c.start_url,
        "tags": c.tags or [],
        "enabled": c.enabled,
        "updated_at": _iso(c.updated_at),
        # 操作经验记忆（见 app/case_memory.py）。前端要能看到"它到底学到了什么"，
        # 否则用户没法判断这份记忆可不可信。
        # `memory_stale` 表示记忆还在、但用例已被改过导致指纹对不上 —— 运行时会被忽略，
        # 这里显式告诉用户，免得他看到一份"看起来很新其实已经作废"的笔记。
        "memory": c.memory,
        "memory_updated_at": _iso(c.memory_updated_at),
        "memory_stale": bool(
            c.memory and c.memory_fingerprint and c.memory_fingerprint != _case_fingerprint(c)
        ),
    }


def _data_files_error(decl: object) -> str | None:
    """声明不合法时给出原因，合法或为空时返回 None。

    为什么把校验放在"读"路径上也做一遍：写路径已经拦过一次，但库里可能躺着
    改 schema 之前写入的、或手工改过的数据。界面上直接显示这句话，比让用户
    跑到执行期才发现"文件没准备好"要早得多。
    """
    if decl in (None, "", {}):
        return None
    from app import testdata

    try:
        testdata.parse(decl)
    except testdata.SpecError as exc:
        return str(exc)
    return None


def _case_fingerprint(c: TestCase) -> str:
    """与 app/case_memory.fingerprint 同一套算法；放在这里只为了让 _case() 能判断是否过期。"""
    try:
        from app.case_memory import fingerprint

        return fingerprint(c)
    except Exception:  # noqa: BLE001
        return ""


def _drift_summary(r: Run) -> dict:
    """Compact drift state for list views.

    Three states, not two, and the difference matters:
      * ``not_checked`` — NULL. The run predates the check. Reporting this as "clean"
        would be a lie: nobody looked.
      * ``clean``     — checked, found nothing.
      * ``drift``     — checked, found signals.

    The full signal list is only on the detail endpoint; a run list can hold 100+ rows and
    each blocker carries dozens of case ids, so putting them in the list payload would make
    the page slow to render the one screen where nobody is reading them.
    """
    sigs = r.drift_signals
    if sigs is None:
        return {"state": "not_checked", "count": None, "blockers": None}
    blockers = sum(1 for s in sigs if s.get("severity") == "blocker")
    return {
        "state": "drift" if sigs else "clean",
        "count": len(sigs),
        "blockers": blockers,
    }


def _run(r: Run) -> dict:
    return {
        "id": r.id,
        "project_id": r.project_id,
        "name": r.name,
        "status": r.status,
        "case_ids": r.case_ids or [],
        "concurrency": r.concurrency,
        # None = 沿用全局 case_retries（不是 0）。前端据此显示"跟随全局"而不是"不重试"。
        "retries": r.retries,
        "total_count": r.total_count,
        "processed_count": r.processed_count,
        "passed_count": r.passed_count,
        "summary": r.summary,
        # 2026-10-07 需规漂移：列表里只给状态与计数，明细在 /runs/{id}。
        "drift": _drift_summary(r),
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
        "created_at": _iso(r.created_at),
        "created_by": r.created_by,
        "suite_id": r.suite_id,
        "ran_by_user_id": r.ran_by_user_id,
        "trigger": r.trigger,
        "environment_id": r.environment_id,
    }


def _verdict_info(attribution: str | None) -> tuple[str, bool]:
    """取归因的中文标签与"是否可上报缺陷"。

    未知归因（含历史数据 NULL 与未判定的空串）一律返回 ("", False)：宁可没标签，
    也不能给个猜测的标签 —— 错标签比没标签更糟，人会照着它去提缺陷。
    """
    info = VERDICT_INFO.get(attribution or "")
    if info is None:
        return ("", False)
    return (info[0], info[1])


def _result(x: RunResult) -> dict:
    return {
        "id": x.id,
        "run_id": x.run_id,
        "case_id": x.case_id,
        "status": x.status,
        "attempts": x.attempts,
        "flaky": x.flaky,
        "video_url": x.video_url,
        "trace_url": x.trace_url,
        "steps": x.steps or [],
        "diagnostics": x.diagnostics or [],
        "judge_reason": x.judge_reason,
        # 2026-10-04 失败根因分类与判定依据。
        # root_cause="" 表示通过（无根因）；None 表示分类功能上线前的历史结果 ——
        # 前端要区分这两种，别把历史数据一律显示成"未分类"而误导。
        "root_cause": x.root_cause,
        # 该分类是否代表被测系统的真实缺陷。前端/统计直接用它分流，
        # 不必在前端再维护一份分类表（两边各存一份迟早会不一致）。
        "is_real_defect": (x.root_cause in ROOT_CAUSE_IS_REAL_DEFECT) if x.root_cause else False,
        "root_cause_label": ROOT_CAUSE_LABELS_ZH.get(x.root_cause or "", ""),
        # 2026-10-07 失败归因：这次失败是谁的锅。与 root_cause 分工不同 ——
        # 那个回答"用例为什么判失败"，这个回答"这次失败能不能算被测系统的缺陷"。
        # 两者必须分开看：今天实测抓到的两条假缺陷，root_cause 都长得像正常的
        # "功能不符"，只有归因能认出它们其实是页面没加载完 / 流程没走完。
        "attribution": x.attribution,
        "attribution_label": _verdict_info(x.attribution)[0],
        # 归因判成"系统行为"才允许上报缺陷。setup / transient / agent 三种都不允许 ——
        # 前端提缺陷按钮应当据此置灰，而不是先让人填完单再告诉他这不算缺陷。
        "attribution_is_real_defect": _verdict_info(x.attribution)[1],
        "verdict_evidence": x.verdict_evidence or [],
        "final_answer": x.final_answer,
        "failure_narrative": x.failure_narrative or None,
        "account_label": x.account_label,
        "latency_ms": x.latency_ms,
        # 2026-10-08 耗时分解（5 段墙钟 + 步数）。NULL = 埋点上线前的历史行，
        # 前端要显示"无数据"而不是画一条 0s 的柱子 —— 那会让人以为用例瞬间跑完了。
        "timing": x.timing or None,
        "error": x.error,
        # 2026-10-06 人工改判痕迹。前端据此把"人改的"和"AI 判的"区分开 ——
        # 混在一起的话，通过率/真缺陷率这些数字就没有可信度了。
        "verdict_override": x.verdict_override,
        "override_reason": x.override_reason,
        "override_by": x.override_by,
        "override_at": x.override_at,
        "original_status": x.original_status,
    }


def _case_change(x: CaseChange) -> dict:
    """一条用例改动审计记录。

    刻意只回字段级前后值、不回完整用例：审计的目的是回答
    "这条用例被改过吗、哪一格被动了" ，不是重建历史快照。
    """
    return {
        "id": x.id,
        "project_id": x.project_id,
        "case_id": x.case_id,
        "field": x.field,
        "before": x.before,
        "after": x.after,
        # assistant = 助手自动改的。前端要把这类单独标出来 ——
        # 助手改用例是用户明确要求的，但"AI 改的断言"天然可疑，
        # 必须能一眼区分，否则通过率会被悄悄污染。
        "source": x.source,
        "digest_signal": x.digest_signal,
        "by_label": x.by_label,
        "created_at": x.created_at.isoformat() if x.created_at else "",
    }


def _env(e: Environment) -> dict:
    return {
        "id": e.id,
        "project_id": e.project_id,
        "name": e.name,
        "base_url": e.base_url,
        "is_default": e.is_default,
    }


def _cred(c: Credential) -> dict:
    # NOTE: never include `secret` — ciphertext must not leave the server.
    return {
        "id": c.id,
        "project_id": c.project_id,
        "type": c.type,
        "role": c.role,
        "environment_id": c.environment_id,
        "label": c.label,
        "username": c.username,
        "is_active": c.is_active,
        "healthy": c.healthy,
        "last_error": c.last_error,
        "last_checked_at": _iso(c.last_checked_at),
        "expires_at": _iso(c.expires_at),
        "created_at": _iso(c.created_at),
    }


def _notif(n: Notification) -> dict:
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "suite_id": n.suite_id,
        "run_id": n.run_id,
        "issue_id": n.issue_id,
        "read": n.read_at is not None,
        "created_at": _iso(n.created_at),
    }


def _issue(i: Issue) -> dict:
    return {
        "id": i.id,
        "project_id": i.project_id,
        "title": i.title,
        "description": i.description,
        "status": i.status,
        "severity": i.severity,
        "assignee": i.assignee,
        "assignee_user_id": i.assignee_user_id,
        "labels": i.labels or [],
        "case_id": i.case_id,
        "run_id": i.run_id,
        "result_id": i.result_id,
        "gitlab_iid": i.gitlab_iid,
        "gitlab_url": _gitlab_web_url(i.gitlab_project, i.gitlab_iid),
        "created_at": _iso(i.created_at),
        "updated_at": _iso(i.updated_at),
    }


def _comment(c: IssueComment) -> dict:
    return {
        "id": c.id,
        "issue_id": c.issue_id,
        "body": c.body,
        "author": c.author,
        "created_at": _iso(c.created_at),
    }


def _feedback(f: FeedbackItem) -> dict:
    return {
        "id": f.id,
        "source": f.source,
        "chat_id": f.chat_id,
        "chat_type": f.chat_type,
        "sender_id": f.sender_id,
        "sender_name": f.sender_name,
        "content": f.content,
        "title": f.title,
        "category": f.category,
        "severity": f.severity,
        "answer": f.answer,
        "answered": f.answered,
        "status": f.status,
        "project_id": f.project_id,
        "issue_id": f.issue_id,
        "created_at": _iso(f.created_at),
    }
