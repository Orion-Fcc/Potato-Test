"""Feishu interactive card builders (pure functions → card dicts).

Button ``value`` dicts are echoed back to us in the card.action.trigger event, so
we route on ``act``.
"""

from __future__ import annotations

from app.config import get_settings


def _run_web_url(project_id: int, run_id: int) -> str | None:
    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}/projects/{project_id}/runs/{run_id}" if base else None


def confirm_run_card(
    project_id: int, project_name: str, case_count: int, rerun_of: int | None
) -> dict:
    """A [确认运行][取消] card sent before actually launching a run."""
    what = (
        f"重跑 run #{rerun_of}"
        if rerun_of
        else f"运行 **{project_name}** 的 {case_count} 个启用用例"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "确认运行测试"},
            "template": "orange",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"要{what}吗?"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认运行"},
                        "type": "primary",
                        "value": {
                            "act": "run_confirm",
                            "project_id": project_id,
                            "rerun_of": rerun_of,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "default",
                        "value": {"act": "run_cancel"},
                    },
                ],
            },
        ],
    }


def run_result_card(run, project_name: str) -> dict:
    """Result summary pushed when a run finishes."""
    passed = run.passed_count or 0
    total = run.total_count or 0
    ok = run.status == "completed" and passed == total and total > 0
    template = "green" if ok else ("red" if run.status != "cancelled" else "grey")
    icon = "✅" if ok else ("❌" if run.status != "cancelled" else "⚪")
    lines = [
        f"**项目**:{project_name}",
        f"**运行**:{run.name} (#{run.id})",
        f"**结果**:{passed}/{total} 通过 · 状态 {run.status}",
    ]
    elements: list = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    url = _run_web_url(run.project_id, run.id)
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看运行"},
                        "type": "default",
                        "url": url,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{icon} 测试运行完成"},
            "template": template,
        },
        "elements": elements,
    }


def _progress_lines(run, project_name: str, *, elapsed_s: float) -> list[str]:
    """The four progress facts every in-flight card shows.

    Shared by the milestone card, the timed heartbeat and the on-demand status reply so
    they can never drift apart (e.g. one showing 未通过 and another not).
    """
    total = run.total_count or 0
    processed = run.processed_count or 0
    passed = run.passed_count or 0
    failed = processed - passed
    pct = f"{processed * 100 // total}%" if total else "-"
    mins = int(elapsed_s // 60)
    secs = int(elapsed_s % 60)
    return [
        f"**项目**:{project_name}",
        f"**运行**:{run.name} (#{run.id})",
        f"**进度**:{processed}/{total} ({pct}) · 通过 {passed} · 未通过 {failed}",
        f"**已用**:{mins} 分 {secs} 秒",
    ]


def _note(text: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def run_milestone_card(run, project_name: str, *, elapsed_s: float, percent: int) -> dict:
    """Pushed when progress crosses a configured milestone (25% / 50% / 75%)."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🎯 进度 {percent}%"},
            "template": "blue",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(_progress_lines(run, project_name, elapsed_s=elapsed_s))}},
            _note(f"跑到 {percent}% 自动同步一次 · 想随时看就在群里 @机器人 问「状态」"),
        ],
    }


def run_failure_card(
    run, project_name: str, *, elapsed_s: float, case_name: str | None
) -> dict:
    """Pushed once per run, the first time a case does not pass."""
    lines = _progress_lines(run, project_name, elapsed_s=elapsed_s)
    if case_name:
        lines.append(f"**首个未通过**:{case_name}")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ 出现未通过用例"},
            "template": "red",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            _note("每个运行只提醒一次 · 后续失败汇总在最终结果卡里"),
        ],
    }


def run_status_card(run, project_name: str, *, elapsed_s: float) -> dict:
    """Answer to an on-demand 「状态」 query — live if running, last result otherwise."""
    live = run.status in ("pending", "running")
    title = "📊 运行状态" if live else f"📊 最近一次运行 · {run.status}"
    lines = _progress_lines(run, project_name, elapsed_s=elapsed_s)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            _note("按需查询 · 不会自动推送"),
        ],
    }


def help_card() -> dict:
    """What the bot understands — sent on 「帮助」."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "我能做什么"}, "template": "turquoise"},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**@机器人 状态** —— 立刻回一张进度卡（在跑的显示进度，没在跑的显示最近一次结果）\n"
                        "**@机器人 跑** —— 弹确认卡片，点了才真的跑\n"
                        "**@机器人 重跑** —— 重跑最近一次\n"
                        "**@机器人 绑定 <项目名>** —— 把本群和项目绑起来\n"
                        "**@机器人 帮助** —— 这条说明"
                    ),
                },
            },
            _note("自动推送只有：里程碑 25%/50%/75% · 首次出现未通过 · 跑完的结果卡"),
        ],
    }


def run_started_card(run, project_name: str) -> dict:
    """Sent the moment a run starts, so the phone gets something within seconds.

    Without this the first signal about a run is its finish card, which for a long
    suite can be hours away -- indistinguishable from "it never started".
    """
    lines = [
        f"**项目**:{project_name}",
        f"**运行**:{run.name} (#{run.id})",
        f"**用例**:{run.total_count or 0} 个 · 并发 {run.concurrency or 2}",
    ]
    elements: list = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    url = _run_web_url(run.project_id, run.id)
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看进度"},
                        "type": "default",
                        "url": url,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🚀 测试已开始"},
            "template": "blue",
        },
        "elements": elements,
    }


def run_progress_card(run, project_name: str, *, elapsed_s: float, interval_s: int) -> dict:
    """Timed heartbeat card (only used when FEISHU_PROGRESS_INTERVAL_SEC > 0)."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⏳ 测试进行中"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(_progress_lines(run, project_name, elapsed_s=elapsed_s)),
                },
            },
            _note(f"每 {max(1, interval_s // 60)} 分钟同步一次，完成后再推最终卡片"),
        ],
    }


def issue_update_card(issue, project_name: str) -> dict:
    """Small note pushed when a bound project's issue changes status."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "问题状态更新"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{project_name}** · 问题 #{issue.id}\n"
                        f"**{issue.title}**\n"
                        f"状态 → **{issue.status}** · 严重度 {issue.severity}"
                    ),
                },
            }
        ],
    }
