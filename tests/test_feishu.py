"""Pure-logic checks for the Feishu bot: event parsing, JSON tolerance, reply text.

No network, no DB, no LLM — just the functions with tricky logic.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.feishu import (
    _apply_override,
    _confirmation,
    _is_cancel_command,
    _is_confirm_command,
    _parse_bind_command,
    _parse_json,
    _parse_override_command,
    _parse_run_command,
    _rerun_case_ids,
    _status_reply,
    parse_message_event,
)
from app.feishu_cards import confirm_run_card, run_result_card


def _text_event(text: str, chat_type: str = "group", mentions=None) -> dict:
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_123"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "mentions": mentions or [],
            },
        },
    }


def test_parse_strips_mention_and_flags_mentioned() -> None:
    evt = parse_message_event(_text_event("@_user_1 登录页报错了", mentions=[{"key": "@_user_1"}]))
    assert evt is not None
    assert evt["text"] == "登录页报错了"
    assert evt["mentioned"] is True
    assert evt["sender_id"] == "ou_123"


def test_parse_p2p_mentioned_and_non_text_skipped() -> None:
    assert parse_message_event(_text_event("hi", chat_type="p2p"))["mentioned"] is True
    payload = _text_event("x")
    payload["event"]["message"]["message_type"] = "image"
    assert parse_message_event(payload) is None


def test_parse_post_message_with_screenshot() -> None:
    # 富文本(post): text + image, @-mentioned — the QA-bug-report shape.
    post_content = json.dumps(
        {
            "title": "",
            "content": [
                [{"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": "下载文件名是随机码"}],
                [{"tag": "img", "image_key": "img_x"}, {"tag": "text", "text": "需修正"}],
            ],
        }
    )
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_p",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "post",
                "content": post_content,
                "mentions": [],  # post carries the @ in content, not here
            },
        },
    }
    evt = parse_message_event(payload)
    assert evt is not None
    assert "下载文件名是随机码" in evt["text"]
    assert "需修正" in evt["text"]
    # @ lives in the post's `at` element → mention_ids picks it up
    assert evt["mention_ids"] == ["ou_bot"]
    assert evt["mentioned"] is True


def test_parse_json_tolerates_fence_and_prose() -> None:
    raw = '好的\n```json\n{"is_feedback": true, "category": "bug", "project_id": 3}\n```'
    parsed = _parse_json(raw)
    assert parsed["is_feedback"] is True
    assert parsed["project_id"] == 3
    assert _parse_json("not json") is None


def test_parse_bind_command() -> None:
    assert _parse_bind_command("绑定 Demo-test") == "Demo-test"
    assert _parse_bind_command("绑定商城项目") == "商城项目"  # no space (common in Chinese)
    assert _parse_bind_command("bind smoke") == "smoke"
    assert _parse_bind_command("下单报错") is None
    assert _parse_bind_command("绑定") is None  # needs a project name


def test_looks_like_problem() -> None:
    from app.feishu import _looks_like_problem

    assert _looks_like_problem("登录页又报错了")
    assert _looks_like_problem("这个功能有个 bug")
    assert _looks_like_problem("系统崩了")
    assert _looks_like_problem("点了没反应")
    assert _looks_like_problem("报了个错")
    assert _looks_like_problem("页面打不开")
    assert not _looks_like_problem("大家早上好")
    assert not _looks_like_problem("这个不错")
    assert not _looks_like_problem("今天下午开会")


def test_parse_run_command() -> None:
    assert _parse_run_command("跑冒烟") == ("run", "冒烟")
    assert _parse_run_command("运行 Demo-test") == ("run", "Demo-test")
    assert _parse_run_command("重跑") == ("rerun", "")
    assert _parse_run_command("下单报错") is None
    assert _parse_run_command("最近跑得怎么样") is None


def test_confirm_run_card_has_buttons() -> None:
    card = confirm_run_card(2, "Demo-test", 12, None)
    actions = card["elements"][-1]["actions"]
    values = [a["value"]["act"] for a in actions]
    assert values == ["run_confirm", "run_cancel"]
    assert actions[0]["value"]["project_id"] == 2


class _Run:
    id = 7
    project_id = 2
    name = "冒烟"
    status = "completed"
    passed_count = 10
    total_count = 12


def test_run_result_card_renders() -> None:
    card = run_result_card(_Run(), "Demo-test")
    assert "测试运行完成" in card["header"]["title"]["content"]
    assert card["header"]["template"] == "red"  # 10/12 → not all passed


def test_parse_bitable_url() -> None:
    from app.feishu_bitable import parse_bitable_url

    app, tbl = parse_bitable_url(
        "https://x.feishu.cn/base/T55fb4ziRaJJDhsUXiVcoNsknke?a=1&table=tbldrYrsiMHAeXuz&view=v"
    )
    assert app == "T55fb4ziRaJJDhsUXiVcoNsknke"
    assert tbl == "tbldrYrsiMHAeXuz"
    assert parse_bitable_url("") == (None, None)
    assert parse_bitable_url("not a url") == (None, None)


def test_confirmation_variants() -> None:
    assert _confirmation([7], 3, "").startswith("已记录并建为问题 #7")
    assert _confirmation([7, 8], 3, "").startswith("已拆成 2 条并建为问题 #7、#8")
    assert "未能识别所属项目" in _confirmation([], None, "")
    assert _confirmation([], 3, "答案") == "已记录 ✅\n\n答案"


def test_api_item_roundtrips_through_parser() -> None:
    """Catch-up replay: a GET /im/v1/messages item must parse like a live event."""
    from app.feishu import _api_item_to_payload

    item = {
        "message_id": "om_9",
        "chat_id": "oc_1",
        "msg_type": "text",
        "create_time": "1787045949000",
        "sender": {"id": "ou_wang", "sender_type": "user"},
        "body": {"content": json.dumps({"text": "@_user_1 登录页报错了"})},
        "mentions": [{"id": "ou_bot", "key": "@_user_1", "name": "TestPolit"}],
    }
    evt = parse_message_event(_api_item_to_payload(item))
    assert evt is not None
    assert evt["message_id"] == "om_9"
    assert evt["text"] == "登录页报错了"
    assert evt["mentioned"] is True
    assert evt["mention_ids"] == ["ou_bot"]
    assert evt["create_time"] == "1787045949000"
    assert evt["sender_id"] == "ou_wang"


def test_ws_sdk_event_roundtrips_through_parser() -> None:
    """SDK MentionEvent objects (no .get) must be normalized before parsing —
    passing them through raw crashed the parser and swallowed @-text messages."""
    from types import SimpleNamespace as NS

    # Feishu is an optional integration: app/feishu_ws.py is a separate worker process
    # that imports the lark SDK at module level, and the SDK lives in the `feishu` extra.
    # A local install never has it, so this test skips instead of failing.
    pytest.importorskip("lark_oapi", reason="飞书集成未安装（pip install -e '.[feishu]'）")

    from app.feishu_ws import _to_payload

    data = NS(
        event=NS(
            sender=NS(sender_id=NS(open_id="ou_alice")),
            message=NS(
                message_id="om_ws1",
                chat_id="oc_1",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "@_user_1 现在有哪些问题了"}),
                create_time="1787108200000",
                mentions=[NS(id=NS(open_id="ou_bot"), key="@_user_1", name="TestPolit")],
            ),
        )
    )
    evt = parse_message_event(_to_payload(data))
    assert evt is not None
    assert evt["text"] == "现在有哪些问题了"
    assert evt["mention_ids"] == ["ou_bot"]
    assert evt["mentioned"] is True
    assert evt["create_time"] == "1787108200000"


def test_bitable_fields_keep_user_words_authoritative() -> None:
    """缺陷描述 = user's original text + model summary; 问题备注 = metadata only."""
    from types import SimpleNamespace as NS

    from app.feishu_bitable import _issue_fields

    issue = NS(
        id=10,
        status="open",
        title="Agent图表重复生成两次",
        description="agent对话生成图表会出现两次，仅保留第一次出现有中文标题的图标即可。",
        severity="medium",
        labels=["feishu"],
        gitlab_iid=None,
        run_id=None,
        created_at=None,
    )
    fields = _issue_fields(issue, "demo-app")
    assert fields["缺陷描述"].startswith("agent对话生成图表会出现两次")
    assert fields["缺陷描述"].endswith("摘要：Agent图表重复生成两次")
    assert "生成图表" not in fields["问题备注"]  # user words not duplicated into 备注
    assert fields["问题备注"] == "项目：demo-app · 来源：飞书群反馈 · Issue #10"
    assert fields["缺陷来源"] == "飞书群反馈"  # no reporter resolved → category
    # no description → title alone
    issue2 = NS(**{**issue.__dict__, "description": ""})
    assert _issue_fields(issue2, "demo-app")["缺陷描述"] == "Agent图表重复生成两次"
    # reporter's name takes over 缺陷来源; category still readable in 问题备注
    with_reporter = _issue_fields(issue, "demo-app", "ou_x", "张三")
    assert with_reporter["缺陷来源"] == "张三"
    assert "来源：飞书群反馈" in with_reporter["问题备注"]
    assert with_reporter["报告人"] == [{"id": "ou_x"}]


def test_post_message_collects_image_keys() -> None:
    from app.feishu import _post_image_keys

    content = json.dumps(
        {
            "title": "",
            "content": [
                [{"tag": "text", "text": "图表重复生成"}, {"tag": "img", "image_key": "img_k1"}],
                [{"tag": "img", "image_key": "img_k2"}, {"tag": "at", "user_id": "ou_bot"}],
            ],
        }
    )
    assert _post_image_keys(content) == ["img_k1", "img_k2"]
    assert _post_image_keys("not json") == []

    evt = parse_message_event(
        {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_id": "om_p1",
                    "chat_id": "oc_1",
                    "chat_type": "group",
                    "message_type": "post",
                    "content": content,
                },
            },
        }
    )
    assert evt is not None
    assert evt["image_keys"] == ["img_k1", "img_k2"]


def test_multi_problem_message_splits_into_items() -> None:
    """A 1./2./3. laundry list becomes one record per problem; single stays single."""
    from app.feishu import _feedback_items

    result = {
        "category": "bug",
        "severity": "medium",
        "title": "Agent 0824优化",
        "items": [
            {
                "title": "sbs结果缓存",
                "content": "1.sbs结果缓存，iframe不能刷新丢失",
                "severity": "high",
            },
            {
                "title": "去掉需关注tab",
                "content": "2.自动化任务，”需关注“tab页不明确",
                "category": "feature",
            },
        ],
    }
    parts = _feedback_items(result, "原始整条消息")
    assert [p["title"] for p in parts] == ["sbs结果缓存", "去掉需关注tab"]
    assert parts[0]["severity"] == "high"  # per-item wins
    assert parts[0]["category"] == "bug"  # falls back to the message-level value
    assert parts[1]["category"] == "feature"

    # no/blank items → the whole message, one record (old behaviour)
    single = _feedback_items({"title": "标题", "category": "bug", "items": []}, "只有一个问题")
    assert len(single) == 1
    assert single[0]["content"] == "只有一个问题"
    assert single[0]["severity"] == "medium"
    assert _feedback_items({}, "裸文本")[0]["title"] == "裸文本"


# --------------------------------------------------------------- 按需查询「状态」

class _Bound:
    """Minimal stand-in for the bound Project (only .id / .name are read)."""

    def __init__(self, pid: int, name: str) -> None:
        self.id = pid
        self.name = name


class _Row:
    """Minimal stand-in for a Run row (only the fields the card reads)."""

    def __init__(self, rid: int, status: str) -> None:
        self.id = rid
        self.status = status
        self.name = f"run-{rid}"
        self.total_count = 10
        self.processed_count = 4
        self.passed_count = 3
        self.started_at = None
        self.finished_at = None


class _FakeSession:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, *_a, **_k):  # noqa: ANN002, ANN003
        rows = self._rows

        class _Scalars:
            def all(self_inner):
                return rows

        class _Result:
            def scalars(self_inner):
                return _Scalars()

        return _Result()


def test_status_query_answers_when_the_project_has_runs() -> None:
    """回归：只要项目有运行记录，「状态」曾经**必定**崩，群里永远等不到回复。

    根因是 app/feishu.py 里 `live = next(...)` 被错缩进到 `if not rows:` 块内、
    且排在 `return` 之后 —— 不可达代码 + 名字未绑定，于是下面那句
    `run = live or rows[0]` 抛 UnboundLocalError。
    现场证据：logs/feishu_poll.log 里 5 条真实消息（om_x100b64...）全是这一个错。

    这个测试的价值不在"卡片长得对"，而在**把这条路径真的走一遍** ——
    原来没有任何测试调用过 _status_reply，所以一个让核心命令 100% 失效的
    错缩进能一直躺在主干上。
    """
    card, text = asyncio.run(_status_reply(_FakeSession([_Row(7, "completed")]), _Bound(1, "项目A")))

    assert text == "", "有运行记录时必须回卡片，而不是文字说明"
    assert card is not None
    assert "📊" in card["header"]["title"]["content"]
    assert "#7" in card["elements"][0]["text"]["content"] or "run-7" in card["elements"][0]["text"]["content"]


def test_status_query_prefers_a_run_that_is_still_going() -> None:
    """正在跑的要优先显示 —— 问「状态」的人最想知道的是当前进度。"""
    rows = [_Row(9, "completed"), _Row(8, "running")]
    card, text = asyncio.run(_status_reply(_FakeSession(rows), _Bound(1, "项目A")))

    assert text == ""
    body = card["elements"][0]["text"]["content"]
    assert "#8" in body or "run-8" in body, f"应显示正在跑的 #8，实际：{body}"


def test_status_query_explains_itself_when_there_is_nothing_to_show() -> None:
    """零运行记录是正常状态，要给一句能照着做的话，而不是空回复。"""
    card, text = asyncio.run(_status_reply(_FakeSession([]), _Bound(1, "项目A")))

    assert card is None
    assert "还没有运行记录" in text


def test_status_query_without_a_binding_tells_you_how_to_bind() -> None:
    """没绑定项目时要说清怎么绑 —— 否则用户只会觉得机器人坏了。"""
    card, text = asyncio.run(_status_reply(_FakeSession([]), None))

    assert card is None
    assert "绑定" in text


# ══════════════════════════════════════════════════════════════════
# 2026-10-09：确认/取消、重跑失败、改判 三条命令的接线
#
# 这三条原先**只写了一半**：正则、解析函数、_PENDING 字典都在，
# 但 handle_message_event 里没有任何一处调用它们 —— 也就是说
# 「重跑失败」会被当成普通「重跑」（跑全量），而群里回「确认」什么也不会发生。
# 下面的用例守住"解析出来"和"确实接上"这两件事。
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text,expected",
    [
        ("重跑失败", ("rerun_failing", "")),
        ("只重跑失败", ("rerun_failing", "")),
        ("rerun failing", ("rerun_failing", "")),
        ("重跑", ("rerun", "")),
        ("重跑一次", ("rerun", "")),
        ("跑 Demo-test", ("run", "Demo-test")),
        ("运行", ("run", "")),
        # 「重跑失败」必须**先于**「重跑」匹配，否则通用前缀会把它吃掉 ——
        # 那样用户以为只跑失败的，实际跑的是全量。
        ("随便聊聊别的", None),
    ],
)
def test_parse_run_command_prefixes(text: str, expected) -> None:
    assert _parse_run_command(text) == expected


def test_rerun_failing_is_matched_before_plain_rerun() -> None:
    """顺序断言单拎一条：这条一旦反了，用户拿到的是一整轮全量回归。"""
    assert _parse_run_command("重跑失败")[0] == "rerun_failing"


@pytest.mark.parametrize("text", ["确认", "确认运行", "开跑", "走起", "confirm", " 确认 "])
def test_confirm_words(text: str) -> None:
    assert _is_confirm_command(text)


@pytest.mark.parametrize("text", ["取消", "算了", "不要了", "cancel"])
def test_cancel_words(text: str) -> None:
    assert _is_cancel_command(text)


@pytest.mark.parametrize("text", ["跑", "重跑", "确认运行一下", ""])
def test_confirm_words_do_not_swallow_commands(text: str) -> None:
    """「跑」「重跑」不能被当成确认 —— 否则一句「跑」就绕过了确认卡。"""
    assert not _is_confirm_command(text)


@pytest.mark.parametrize(
    "text,status,reason",
    [
        ("改判 12 通过", "passed", ""),
        ("改判 #12 失败", "failed", ""),
        ("override 12 passed", "passed", ""),
        ("改判 12 失败 页面确实没渲染", "failed", "页面确实没渲染"),
        ("override #7 fail 列表为空", "failed", "列表为空"),
    ],
)
def test_parse_override_command(text: str, status: str, reason: str) -> None:
    got = _parse_override_command(text)
    assert got == {"result_id": 12 if "12" in text else 7, "status": status, "reason": reason}


@pytest.mark.parametrize("text", ["改判", "改判 通过", "改判 abc 通过", "随便", ""])
def test_override_rejects_malformed(text: str) -> None:
    assert _parse_override_command(text) is None


def test_rerun_case_ids_keeps_everything_that_is_not_passed() -> None:
    """「只重跑失败」= 非 passed 全留（failed + error 都算），passed 丢掉。"""
    ids = [1, 2, 3, 4]
    verdicts = {1: "passed", 2: "failed", 3: "error", 4: "passed"}
    assert _rerun_case_ids(ids, verdicts, "failing") == [2, 3]


def test_rerun_case_ids_treats_a_missing_verdict_as_not_passed() -> None:
    """没有结果行 = 没跑成 = 该重跑。

    宁可多跑一条，也不要因为查不到结论就把一条可能真失败的用例静默跳过。
    """
    assert _rerun_case_ids([1, 2], {1: "passed"}, "failing") == [2]


def test_rerun_case_ids_none_means_the_whole_set() -> None:
    assert _rerun_case_ids([1, 2, 3], {1: "passed"}, None) == [1, 2, 3]


def test_confirm_card_says_it_is_only_rerunning_failures() -> None:
    """卡片必须写明"只重跑未通过的那些"。

    否则「重跑 run #12」看起来就是整轮重跑，操作者会把部分通过率当成全量通过率。
    """
    card = confirm_run_card(1, "项目A", 3, 12, "failing", [5, 6, 7])
    body = card["elements"][0]["text"]["content"]
    assert "只重跑" in body and "未通过" in body and "3" in body
    assert "#12" in body


def test_confirm_card_without_only_reads_as_a_full_rerun() -> None:
    card = confirm_run_card(1, "项目A", 3, 12)
    body = card["elements"][0]["text"]["content"]
    assert "只重跑" not in body
    assert "重跑 run #12" in body


def test_confirm_card_carries_the_resolved_case_set_in_the_button() -> None:
    """按钮里要带上已算好的 case_ids。

    卡片说"只重跑 3 条"，按钮就得跑那 3 条；让按钮在点击时重新推导，
    两次结果就可能不是同一个集合（期间又跑过一轮 / 用例被禁用）。
    """
    card = confirm_run_card(1, "项目A", 3, 12, "failing", [5, 6, 7])
    button = card["elements"][1]["actions"][0]
    assert button["value"]["case_ids"] == [5, 6, 7]
    assert button["value"]["only"] == "failing"
    assert button["value"]["act"] == "run_confirm"


def _rm(path: str) -> None:
    """删掉临时库文件；删不掉不算测试失败（沙箱可能拒绝），垃圾在 .tmp 里无害。"""
    import os

    try:
        os.remove(path)
    except OSError:
        pass


def _override_db(tag: str = "a"):
    """一个只含 run/result 的临时库；写法理由见 PROJECT_ISOLATION.md。

    刻意**不**用 pytest 的 ``tmp_path``：本机沙箱会给它新建出来的目录打一条拒绝项，
    于是连 sqlite 都打不开库、teardown 也删不掉，红得与产品代码无关。
    直接把库文件放在工作区已有的临时目录里，就没有"新建目录"这一步。
    """
    import asyncio
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod
    from app.models import Base, Project, Run, RunResult, TestCase

    d = os.path.join(os.environ.get("TEMP") or os.getcwd(), "potato-tests")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"_tp_override_{tag}.db")
    if os.path.exists(path):
        os.remove(path)

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + path.replace("\\", "/"), future=True, poolclass=NullPool
    )
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    # 自证隔离有效，绝不静默写用户的真库
    assert "_tp_override_" in str(eng.url), f"没连到临时库：{eng.url}"
    return eng, dbmod, (Project, Run, RunResult, TestCase), path


def test_apply_override_writes_the_same_columns_as_the_api() -> None:
    """改判要落在 API 用的同一批列上，否则报告分不清"谁改的"。"""
    import asyncio

    eng, dbmod, models, path = _override_db("api")
    Project, Run, RunResult, TestCase = models

    async def _run():
        async with dbmod.db_session() as s:
            p = Project(name="P")
            s.add(p)
            await s.flush()
            c = TestCase(project_id=p.id, case_key="TC-001", name="c", prompt="x")
            s.add(c)
            await s.flush()
            r = Run(project_id=p.id, name="r", case_ids=[c.id], total_count=1)
            s.add(r)
            await s.flush()
            res = RunResult(run_id=r.id, case_id=c.id, status="failed")
            s.add(res)
            await s.flush()
            rid = res.id

            msg = await _apply_override(s, rid, "passed", "人工核对确实通过", by="feishu:ou_1")
            await s.commit()
            assert "通过" in msg

        async with dbmod.db_session() as s:
            row = await s.get(RunResult, rid)
            assert row.status == "passed"
            assert row.verdict_override == "passed"
            assert row.original_status == "failed", "AI 原判必须留一份，撤销时才有得还原"
            assert row.override_reason == "人工核对确实通过"
            assert row.override_by == "feishu:ou_1"
            assert row.override_at

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(eng.dispose())
        dbmod._engine = None
        dbmod._Session = None
        _rm(path)


def test_apply_override_keeps_the_first_ai_verdict() -> None:
    """连改两次不能把第一次记下的 AI 原判冲掉。"""
    import asyncio

    eng, dbmod, models, path = _override_db("twice")
    Project, Run, RunResult, TestCase = models

    async def _run():
        async with dbmod.db_session() as s:
            p = Project(name="P")
            s.add(p)
            await s.flush()
            c = TestCase(project_id=p.id, case_key="TC-001", name="c", prompt="x")
            s.add(c)
            await s.flush()
            r = Run(project_id=p.id, name="r", case_ids=[c.id], total_count=1)
            s.add(r)
            await s.flush()
            res = RunResult(run_id=r.id, case_id=c.id, status="error")
            s.add(res)
            await s.flush()
            rid = res.id

            await _apply_override(s, rid, "passed", "", by="feishu:ou_1")
            await _apply_override(s, rid, "failed", "又看了一遍", by="feishu:ou_2")
            await s.commit()

        async with dbmod.db_session() as s:
            row = await s.get(RunResult, rid)
            assert row.status == "failed"
            assert row.original_status == "error", "第二次改判把 AI 原判冲掉了"
            assert row.override_by == "feishu:ou_2"

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(eng.dispose())
        dbmod._engine = None
        dbmod._Session = None
        _rm(path)


def test_apply_override_on_a_missing_result_says_so() -> None:
    """改判一个不存在的号要回一句人话，不能抛异常把整条消息处理打断。"""
    import asyncio

    eng, dbmod, _models, path = _override_db("missing")

    async def _run():
        async with dbmod.db_session() as s:
            msg = await _apply_override(s, 999999, "passed", "", by="feishu:ou_1")
            assert "999999" in msg and "没找到" in msg

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(eng.dispose())
        dbmod._engine = None
        dbmod._Session = None
        _rm(path)
