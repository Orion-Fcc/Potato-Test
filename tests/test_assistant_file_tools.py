"""The assistant's file access, tool discovery, and import previews.

Three things are under test, and the first is a safety boundary rather than a feature:

* **The read allowlist.** The assistant runs on the operator's machine but is *driven by a model*,
  so "the user asked for it" is not authorisation — the allowlist is. Without it, a prompt-injected
  document could turn into "read~/.ssh/id_rsa and add it to the knowledge base", and the operator
  would find their key quoted back at them in the chat.

* **Dry-run by default.** Both imports write into things that have no per-item undo (the knowledge
  base is quoted from for the rest of the project; a half-parsed workbook becomes dozens of
  unremovable cases). The default is preview, and the tests pin that the write path is only reached
  with ``save``.

* **Discovery reads the live table.** A hardcoded list in the prompt is a snapshot; ``list_tools``
  is not. The tests assert the discovery tool's own count matches ``_tools()`` — if they drift, the
  model is told it has fewer tools than it has.
"""

from __future__ import annotations

import inspect
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import assistant  # noqa: E402


@pytest.fixture(autouse=True)
def _allow_tmp_path_in_roots(tmp_path, monkeypatch):
    """Test fixtures create files under pytest's tmp dir; allow them to read exactly those.

    The production allowlist must NOT include ``C:\\Users\\...\\AppData\\Local\\Temp`` — that is a
    world-writable scratch space, not a test-work location, and adding it would let the model read
    anything a process dropped there. But the tests need a place to plant a fixture file. So the
    fixture, and only the fixture, is appended for the duration of the test and torn down after.
    """
    roots = list(assistant._read_roots())
    roots.append(tmp_path.resolve())
    monkeypatch.setattr(assistant, "_ASSISTANT_READ_ROOTS", tuple(roots))
    yield


# ── the read allowlist ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target",
    [
        "~/.ssh/id_rsa",
        "~/.ssh/id_ed25519",
        "~/.aws/credentials",
        "~/Pictures/private/photo.png",
        "~/.config/gcloud/application_default_credentials.json",
        "C:/Windows/System32/config/SAM",
        # 用占位用户名，不写跑测试这台机器的真实账户名 —— 本仓库是公开的，
        # 而样本值是什么完全不影响这条断言（它只要求路径被拒绝）。
        "C:/Users/some-user/AppData/Roaming/Microsoft/Passwords",
    ],
)
def test_secrets_and_system_paths_are_refused(target):
    """The reason these exist. Each one is a file whose contents should never reach a chat.

    ``~/Pictures`` is here deliberately: it is a *user* directory, but not one test work lives in,
    and the rule is easier to trust when it is "an allowlist of work directories" rather than
    "anything under the user's home".

    断言只钉住「被拒绝」（``path is None`` 且给出非空理由），不钉死具体文案：
    Windows 风格的 ``C:/...`` 在 Linux 上不是绝对路径，会被当相对路径走「文件不存在」
    分支，而不是「不在允许读取的目录下」分支。安全属性（没放行）两个平台一致，但
    文案随平台不同，死磕文案会让同一份用例在一个平台恒红。
    """
    path, err = assistant._resolve_readable(target)
    assert path is None, f"{target} 被放行了 -> {path}"
    assert err, f"{target} 被拒绝了却没给理由，模型无法据此纠偏"


@pytest.mark.parametrize(
    "target",
    [
        "C:/Windows/System32/drivers/etc/hosts",
        "../../../../etc/passwd",
        "../../../../Windows/win.ini",
    ],
)
def test_traversal_out_of_the_project_is_refused(target):
    """``..`` must not walk out. The check is on the *resolved* path, so this cannot be bypassed
    by spelling the same file differently."""
    path, err = assistant._resolve_readable(target)
    assert path is None, f"{target} 越权成功 -> {path}"
    assert err


def test_a_home_relative_path_is_expanded_before_the_check():
    """★ ``~`` must be expanded *before* the allowlist runs.

    Without ``expanduser``, ``~/.ssh/id_rsa`` becomes a literal ``~`` directory inside the project
    and is refused only because nothing happens to exist there — the file itself was never out of
    reach. That is luck, not a control, and it stops being luck the moment a ``~`` folder exists.
    """
    path, err = assistant._resolve_readable("~/.ssh/id_rsa")
    assert path is None
    # The refusal must come from the allowlist, not from "file not found".
    assert "不在允许读取的目录下" in err, f"是碰巧没找到，而不是被拦住：{err[:60]}"


def test_a_missing_file_inside_an_allowed_root_is_reported_as_missing():
    """A different failure needs a different fix from the user.

    "Not allowed" means "copy it somewhere else"; "does not exist" means "check the name". Telling
    the model they are the same problem makes it guess wrong.
    """
    path, err = assistant._resolve_readable("definitely-not-here-9f2b.md")
    assert path is None
    assert "文件不存在" in err


def test_an_allowed_project_file_resolves():
    """The positive case: the guard must not refuse legitimate work."""
    path, err = assistant._resolve_readable("app/assistant.py")
    assert err == ""
    assert path is not None and path.name == "assistant.py"
    assert path.is_absolute()


def test_the_user_desktop_is_readable_because_that_is_where_specs_land(tmp_path, monkeypatch):
    """Where a downloaded spec actually is. If this were refused, the feature would be useless.

    ★ 2026-10-08 重写。原写法断言「当前机器的 `~/Desktop` 在可读根里」，而这在 CI（Linux）
    上必然失败：`/home/runner` 下没有 Desktop 目录，而 `_read_roots()` 会过滤掉不存在的根
    （那条过滤本身是对的，见该函数注释），于是断言红。但红说明不了任何问题 —— 它测的是
    "这台机器恰好有没有桌面"，不是"桌面在不在允许列表里"。

    改成**把环境造出来**：给 `Path.home()` 指一个临时 home，在里面建出 Desktop，清掉缓存
    再断言。这样在任何平台上验证的都是同一件事，且不再依赖跑测试的机器长什么样。
    """
    fake_home = tmp_path / "home"
    (fake_home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    # 缓存必须清掉：_read_roots() 只在第一次调用时算，autouse 夹具已经让它算过一遍了。
    monkeypatch.setattr(assistant, "_ASSISTANT_READ_ROOTS", None)

    roots = {str(r).lower() for r in assistant._read_roots()}
    desktop = str((fake_home / "Desktop").resolve()).lower()
    assert desktop in roots, "桌面不在可读目录里 —— 用户刚下载的需规就导不进来"


def test_import_refuses_a_secret_path_rather_than_reading_it(tmp_path):
    """End to end through the tool: not just the helper.

    A guard that exists but is not on the import path is the exact failure mode already hit twice
    this session (a guard added to three tools while a fourth route stayed open).
    """
    import asyncio

    res = asyncio.run(
        assistant._run_tool(
            "import_file_to_knowledge", {"path": "~/.ssh/id_rsa", "save": True}, 3
        )
    )
    assert "error" in res
    assert "不在允许读取的目录下" in res["error"]


# ── tool discovery ────────────────────────────────────────────────────────────


def test_list_tools_reports_the_live_count_not_a_hardcoded_one():
    """The whole point of discovery is that it cannot go stale.

    If this ever returns a pinned number, the model is told it has fewer tools than it has and will
    refuse to try the ones it cannot name.
    """
    import asyncio

    res = asyncio.run(assistant._run_tool("list_tools", {}, 3))
    assert res["total_tools"] == len(assistant._tools())
    assert res["matched"] == res["total_tools"]


def test_list_tools_filters_by_purpose():
    """The query is how the model finds a tool whose name it does not know."""
    import asyncio

    res = asyncio.run(assistant._run_tool("list_tools", {"query": "导入"}, 3))
    names = {t["name"] for t in res["tools"]}
    assert "import_file_to_knowledge" in names
    assert "import_cases_from_file" in names
    assert res["matched"] < res["total_tools"], "过滤器没起作用（关键词匹配了全部）"


@pytest.mark.parametrize("query", ["文件", "path", "用例", "doc"])
def test_searching_by_a_word_in_the_params_finds_the_tool(query):
    """Parameter names count as search surface: someone asks about "path" and gets list_files."""
    import asyncio

    res = asyncio.run(assistant._run_tool("list_tools", {"query": query}, 3))
    assert res["matched"] >= 1, f"搜「{query}」一个都没命中"


def test_every_tool_is_described_in_the_prompt_or_discoverable():
    """A tool that exists but is in neither place is unreachable in practice.

    Prompt-listed tools save a round trip; the rest are reachable via ``list_tools``. Anything in
    neither category is something a future author added and nobody can find.
    """
    import asyncio

    import app.assistant as a

    prompt = a._system_prompt("(catalog)", "飞行学员管理", 2)
    res = asyncio.run(assistant._run_tool("list_tools", {}, 3))
    assert res["total_tools"] == len(a._tools())
    # list_tools itself must be named in the prompt, or the model has no reason to try it.
    assert "list_tools" in prompt


def test_the_prompt_tells_the_model_the_imports_default_to_preview():
    """Otherwise the model calls save=true first and the guard never fires.

    The dry-run default only protects anyone if the model knows about it — the tool description
    says it, but the prompt is what it reads before choosing.
    """
    prompt = assistant._system_prompt("(catalog)", "飞行学员管理", 2)
    assert "预检" in prompt
    assert "save" in prompt


# ── import previews ───────────────────────────────────────────────────────────


def _sheet(tmp_path, name, rows):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    p = tmp_path / name
    wb.save(p)
    return p


@pytest.fixture
def good_sheet(tmp_path):
    return _sheet(
        tmp_path,
        "good.xlsx",
        [
            ("Name", "Agent Task (prompt)", "Expected"),
            ("导入验证-1", "打开列表页。", "有数据"),
            ("导入验证-2", "点击新增并保存。", "保存成功"),
        ],
    )


@pytest.fixture
def ragged_sheet(tmp_path):
    """Two of three data rows lack a required field — the case a preview exists to catch."""
    return _sheet(
        tmp_path,
        "ragged.xlsx",
        [
            ("Name", "Agent Task (prompt)", "Expected"),
            ("缺prompt", None, "有结果"),
            (None, "有prompt没name", "有结果"),
            ("完整用例", "做点什么", "正常"),
        ],
    )


def test_a_good_sheet_previews_cleanly(good_sheet):
    import asyncio

    res = asyncio.run(
        assistant._run_tool("import_cases_from_file", {"path": str(good_sheet)}, 3)
    )
    assert res["mode"] == "dry_run"
    assert res["rows_total"] == 2
    assert res["rows_usable"] == 2
    assert res["rows_missing_required"] == 0
    assert "warning" not in res


def test_a_ragged_sheet_reports_the_rows_it_will_drop(ragged_sheet):
    """★ The reason the preview counts rows itself.

    ``excel.parse_workbook`` ends with ``if rec.get("name") and rec.get("prompt")  # skip
    blank/incomplete rows`` — correct for importing, fatal for a preview. Counting what survived
    parsing reported "3 rows, 1 usable" and never mentioned the 2 that vanish. The operator sees a
    number that does not match their spreadsheet and has no idea why.
    """
    import asyncio

    res = asyncio.run(
        assistant._run_tool("import_cases_from_file", {"path": str(ragged_sheet)}, 3)
    )
    assert res["rows_total"] == 3, "原始数据行数应从工作表直接数"
    assert res["rows_usable"] == 1
    assert res["rows_missing_required"] == 2, "被跳过的行必须报出来，否则用户不知道为什么少了两条"
    assert "warning" in res and "2 行" in res["warning"]


def test_a_sheet_with_unrecognisable_headers_is_refused_outright(tmp_path):
    """Importing then would silently create nothing at all — the worst outcome for a tool the
    operator trusted with a file."""
    import asyncio

    p = _sheet(tmp_path, "wrong.xlsx", [("标题", "内容"), ("a", "b")])
    res = asyncio.run(assistant._run_tool("import_cases_from_file", {"path": str(p)}, 3))
    assert "error" in res
    assert "表头" in res["error"]


def test_the_header_check_matches_headers_not_keys():
    """Regression: it compared a header against ``{k for _, k in COLUMNS}`` — the *keys* — so
    nothing ever matched and a perfect sheet was reported as "表头一列都没对上"."""
    n, err = assistant._count_data_rows(
        __import__("app.excel", fromlist=["build_workbook"]).build_workbook(
            [{"name": "x", "prompt": "y", "expected": "z"}]
        )
    )
    assert err == "", err
    assert n == 1


def test_a_non_xlsx_file_gets_an_actionable_message(tmp_path):
    """.xls is the common one; "unsupported file" does not tell the user what to do."""
    import asyncio

    p = tmp_path / "cases.md"
    p.write_text("not a workbook", encoding="utf-8")
    res = asyncio.run(assistant._run_tool("import_cases_from_file", {"path": str(p)}, 3))
    assert ".xlsx" in res["error"]
    assert "另存为" in res["error"]


@pytest.mark.parametrize("fmt", ["md", "txt", "json", "csv", "html", "rtf"])
def test_every_plain_text_format_previews(tmp_path, fmt):
    """The format list is the feature. Reuses ``docparse`` rather than reimplementing parsing."""
    import asyncio

    p = tmp_path / f"doc.{fmt}"
    p.write_text("导入验证内容", encoding="utf-8")
    res = asyncio.run(
        assistant._run_tool("import_file_to_knowledge", {"path": str(p)}, 3)
    )
    assert "error" not in res, res.get("error")
    assert res["mode"] == "dry_run"
    assert res["chars"] > 0


def test_the_preview_does_not_dump_the_whole_file(tmp_path):
    """A preview exists to confirm *what the file is*, not to fill the context.

    Importing a 200k-character spec in preview mode would spend the window on the very thing the
    dry run is meant to avoid.
    """
    import asyncio

    p = tmp_path / "big.md"
    p.write_text("内容" * 50000, encoding="utf-8")  # 200k chars
    res = asyncio.run(
        assistant._run_tool("import_file_to_knowledge", {"path": str(p)}, 3)
    )
    assert len(res["preview"]) <= 3500
    assert res["chars"] > len(res["preview"]), "chars 应报告真实大小，让模型知道被截断了"


def test_an_empty_parse_says_so_instead_of_importing_nothing(tmp_path):
    """An images-only scan parses to ''. Importing that achieves nothing silently."""
    import asyncio

    p = tmp_path / "scan.md"
    p.write_bytes(b"")
    res = asyncio.run(
        assistant._run_tool("import_file_to_knowledge", {"path": str(p)}, 3)
    )
    assert "empty" in res.get("error", "") or "空" in res.get("error", "") or res.get("chars", 1) == 0


def test_list_files_confirms_a_path_before_it_is_used(tmp_path):
    """User-supplied paths are wrong in small ways. Confirming is cheaper than a failed import."""
    import asyncio

    p = tmp_path / "some.xlsx"
    p.write_bytes(b"x")
    res = asyncio.run(assistant._run_tool("list_files", {"path": str(tmp_path)}, 3))
    assert "some.xlsx" in res["entries"]


def test_the_import_tools_are_reachable_from_run_tool():
    """Structural pin: a tool can exist in the table and still be unhandled in the dispatcher.

    That exact gap — defined but never wired — is silent: the model calls it and gets
    ``{"error": "unknown tool ..."}``, which reads like a platform limitation rather than a bug.
    """
    src = inspect.getsource(assistant._run_tool)
    for name in (
        "list_tools",
        "list_files",
        "import_file_to_knowledge",
        "import_cases_from_file",
    ):
        assert f'if name == "{name}"' in src, f"{name} 在工具表里但没接进 _run_tool"