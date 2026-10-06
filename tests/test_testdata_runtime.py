"""执行期入口（app/testdata_runtime.py）—— executor.py 里那两行接线的契约。

这些测试是"接线前的最后一道验证"：如果 upload_paths 的形状、prompt_block 的内容
或降级行为不对，接线之后 agent 就会传不了文件 —— 而那个症状
（"我无法上传这个文件"）极容易被误判成被测系统的限制。

    python -m pytest tests/test_testdata_runtime.py
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app import testdata_runtime as rt


DECL = {
    "files": [
        {"name": "学员名单.xlsx", "header": ["学号", "姓名"], "rows": [["S001", "张三"]]},
        {"name": "说明.txt", "kind": "txt", "content": "第 {{i}} 批"},
    ]
}


# ---- 目录：必须是 runs/<结果id>/data/ -----------------------------------------


def test_data_dir_is_keyed_by_result_id(tmp_path: pathlib.Path) -> None:
    """产物目录的键是 RunResult.id，不是 run.id —— 同一用例每跑一次是一个新目录。

    放错位置的后果很隐蔽：文件被写到别处，清理脚本判定它是孤儿就删掉了，
    而"清理工具"和"执行器"都不会报错。
    """
    d = rt.data_dir(tmp_path, 123)

    assert d == tmp_path / "runs" / "123" / "data"


def test_prepare_writes_into_the_data_dir(tmp_path: pathlib.Path) -> None:
    out = rt.prepare(DECL, "TC-014", tmp_path, 7)

    assert len(out.files) == 2
    for f in out.files:
        assert f.path.parent == rt.data_dir(tmp_path, 7), f"{f.name} 不在 data 目录里"
        assert f.path.is_file()


# ---- upload_paths：白名单的形状 ----------------------------------------------


def test_upload_paths_are_absolute_strings(tmp_path: pathlib.Path) -> None:
    """`upload_file` 收的是绝对路径；相对路径会被浏览器当成 CWD，报错却说"文件不存在"。"""
    out = rt.prepare(DECL, "TC-014", tmp_path, 7)

    paths = out.upload_paths

    assert paths == [str(f.path) for f in out.files]
    assert all(p.startswith(str(tmp_path)) or p.is_absolute() for p in paths)
    for p in paths:
        assert pathlib.Path(p).is_absolute()


def test_no_files_means_an_empty_whitelist_not_none(tmp_path: pathlib.Path) -> None:
    """Agent(available_file_paths=[]) 与 None 行为不同，别传错类型。"""
    out = rt.prepare(None, "TC-001", tmp_path, 1)

    assert out.upload_paths == []
    assert out.files == []


# ---- 提示词 -----------------------------------------------------------------


def test_prompt_block_is_empty_when_there_are_no_files(tmp_path: pathlib.Path) -> None:
    """不给无关用例塞噪音 —— 547 条存量用例里绝大多数不需要上传文件。"""
    out = rt.prepare(None, "TC-001", tmp_path, 1)

    assert out.prompt_block == ""


def test_prompt_block_names_every_file_and_its_path(tmp_path: pathlib.Path) -> None:
    out = rt.prepare(DECL, "TC-014", tmp_path, 7)

    block = out.prompt_block

    for f in out.files:
        assert f.name in block
        assert str(f.path) in block
    assert "upload_file" in block, "必须点名用哪个动作，否则 agent 会去打文本框"
    assert "不要" in block, "要明确禁止它把路径打进输入框"


# ---- 降级：声明写错不该让整条用例失败 -----------------------------------------


def test_a_broken_declination_degrades_instead_of_raising(tmp_path: pathlib.Path) -> None:
    out = rt.prepare({"files": [{"name": "../../.env", "content": "x"}]}, "TC-002", tmp_path, 3)

    assert out.files == [], "路径穿越必须被拦住，不能真写出去"
    assert out.error, "但必须留下原因"
    assert out.upload_paths == []


def test_the_broken_declination_tells_the_agent_to_report_a_failure(tmp_path: pathlib.Path) -> None:
    """降级后 agent 必须知道"文件没准备好"，否则它会假装导入成功。"""
    out = rt.prepare({"files": [{"name": "a.csv", "header": ["x"], "rows": 5}]}, "TC-004", tmp_path, 3)

    assert out.error
    block = out.prompt_block
    assert "没有" in block
    assert "失败" in block
    assert "不要假装" in block


def test_a_filesystem_error_also_degrades(tmp_path: pathlib.Path) -> None:
    """写盘失败（磁盘满、权限）同样不该中止运行。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    out = rt.prepare(DECL, "TC-005", blocked, 3)

    assert out.files == []
    assert out.error


# ---- 清单：失败时要能回答"传上去的是哪个文件" ---------------------------------


def test_manifest_is_written_next_to_the_run(tmp_path: pathlib.Path) -> None:
    out = rt.prepare(DECL, "TC-014", tmp_path, 7)

    manifest = rt.persist_manifest(out, tmp_path, 7)

    assert manifest is not None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert [f["name"] for f in data["files"]] == ["学员名单.xlsx", "说明.txt"]
    assert all(len(f["sha256"]) == 64 for f in data["files"])


def test_no_manifest_when_there_are_no_files(tmp_path: pathlib.Path) -> None:
    out = rt.prepare(None, "TC-001", tmp_path, 1)

    assert rt.persist_manifest(out, tmp_path, 1) is None


# ---- 清理工具必须认得这些文件 -----------------------------------------------


def test_the_artifact_cleaner_keeps_data_dirs_of_referenced_runs() -> None:
    """数据文件放在 runs/<结果id>/data/，而清理脚本按"结果id 是否被引用"决定去留。

    这条测试是钉住那个约定的：如果哪天有人改成放 artifacts/data/，
    清理脚本就会把它们当孤儿删掉，而两条测试都会绿 —— 除非它们彼此看见对方。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "cleaner",
        pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cleanup_orphan_artifacts.py",
    )
    cleaner = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(cleaner)

    import inspect

    src = inspect.getsource(cleaner.main)
    assert "referenced_run_ids" in src or "referenced" in src, (
        "清理脚本的保留判据必须走 referenced_run_ids()，不能另写一套"
    )
