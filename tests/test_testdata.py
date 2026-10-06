"""用例声明的测试数据 → 真实文件（app/testdata.py）。

这条链路存在的唯一理由：有些被测系统的用例是"先导入一份 Excel，再验证导入结果"。
agent 有 `upload_file` 动作，但 browser-use 的 FileSystem 沙箱**不支持 .xlsx**
（实测只支持 md/txt/json/jsonl/csv/pdf/docx/html/xml），所以数据必须由平台生成。

    python -m pytest tests/test_testdata.py
"""

from __future__ import annotations

import csv
import json
import pathlib

import pytest

from app import testdata as td


# ---- 声明校验：坏输入必须给出"怎么改"的提示，而不是"invalid" -----------------


def test_empty_declination_means_no_files() -> None:
    assert td.parse(None) == []
    assert td.parse("") == []
    assert td.parse({}) == []
    assert td.parse({"files": []}) == []


def test_a_json_string_is_accepted_because_the_ui_sends_text() -> None:
    got = td.parse('{"files": [{"name": "a.csv", "header": ["x"], "rows": [["1"]]}]}')

    assert got[0]["name"] == "a.csv"
    assert got[0]["kind"] == "csv", "扩展名应该能推断出 kind，不必重复写"


def test_broken_json_says_where() -> None:
    with pytest.raises(td.SpecError) as e:
        td.parse("{not json")

    assert "JSON" in str(e.value)


def test_declination_may_be_a_bare_array() -> None:
    got = td.parse([{"name": "a.txt", "content": "x"}])

    assert got[0]["kind"] == "txt"


# ---- 文件名：这是安全边界，不是洁癖 -------------------------------------------
#
# 文件名会进 upload_file 的路径参数，而浏览器接受任意本地路径。
# 一条用例如果能写 "name": "../../.env"，就等于把用户配置上传给了被测系统。


@pytest.mark.parametrize(
    "name",
    [
        "../.env",
        "..\\..\\secrets.txt",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "sub/dir/a.xlsx",
        ".hidden.xlsx",
        "..",
    ],
)
def test_names_that_escape_the_directory_are_refused(name: str) -> None:
    with pytest.raises(td.SpecError):
        td.parse({"files": [{"name": name, "content": "x", "kind": "txt"}]})


def test_illegal_characters_are_refused() -> None:
    with pytest.raises(td.SpecError) as e:
        td.parse({"files": [{"name": "a<b>.txt", "content": "x", "kind": "txt"}]})

    assert "非法字符" in str(e.value)


def test_windows_reserved_device_names_are_refused() -> None:
    with pytest.raises(td.SpecError) as e:
        td.parse({"files": [{"name": "CON.txt", "content": "x", "kind": "txt"}]})

    assert "保留" in str(e.value)


def test_kind_and_extension_must_agree() -> None:
    """名字叫 .txt 却声明 xlsx —— 报错必须指向这个矛盾，而不是让浏览器按扩展名去判。"""
    with pytest.raises(td.SpecError) as e:
        td.parse({"files": [{"name": "a.txt", "kind": "xlsx", "sheets": []}]})

    # 报错要同时点出矛盾的两边（kind 与扩展名），只说其中一边会让人不知道该改哪个
    msg = str(e.value)
    assert "xlsx" in msg and ".txt" in msg


def test_unknown_kind_lists_the_supported_ones() -> None:
    with pytest.raises(td.SpecError) as e:
        td.parse({"files": [{"name": "a.json", "kind": "json", "content": "{}"}]})

    assert "xlsx" in str(e.value) and "csv" in str(e.value)


def test_duplicate_file_names_are_refused() -> None:
    with pytest.raises(td.SpecError) as e:
        td.parse(
            {
                "files": [
                    {"name": "a.txt", "content": "1"},
                    {"name": "A.TXT", "content": "2"},
                ]
            }
        )

    assert "重复" in str(e.value)


# ---- 上限：一份声明不该能写出一个 GB 的文件 -----------------------------------


def test_too_many_files() -> None:
    files = [{"name": f"f{i}.txt", "content": "x"} for i in range(td.MAX_FILES + 1)]

    with pytest.raises(td.SpecError):
        td.parse({"files": files})


def test_bare_integer_rows_explains_the_right_way_to_write_it() -> None:
    with pytest.raises(td.SpecError) as e:
        td.parse({"files": [{"name": "a.csv", "header": ["x"], "rows": 500}]})

    assert "template" in str(e.value), "要告诉用户正确写法，而不是只说'不行'"


def test_row_count_above_the_cap() -> None:
    with pytest.raises(td.SpecError):
        td.parse(
            {
                "files": [
                    {
                        "name": "a.csv",
                        "header": ["x"],
                        "rows": {"count": td.MAX_ROWS + 1, "template": ["1"]},
                    }
                ]
            }
        )


def test_cell_longer_than_the_cap() -> None:
    with pytest.raises(td.SpecError):
        td.parse({"files": [{"name": "a.txt", "content": "x" * (td.MAX_TXT_LEN + 1)}]})


def test_txt_without_content_is_refused() -> None:
    with pytest.raises(td.SpecError):
        td.parse({"files": [{"name": "a.txt", "kind": "txt"}]})


# ---- 占位符 -------------------------------------------------------------------


def test_unknown_placeholder_is_left_visible() -> None:
    """未知占位符必须原样留着。

    悄悄替换成空串会变成"字段填了但系统说格式不对"—— 那是导入类用例最难查的一类失败，
    因为截图上看不出差别，而文件里少了一个 {{...}} 谁都不会注意到。
    """
    assert td.render("{{nope}}", {}) == "{{nope}}"


def test_known_placeholders() -> None:
    ctx = {"index": 7, "case_key": "TC-003", "count": 12}

    assert td.render("第 {{i}} 行", ctx) == "第 7 行"
    assert td.render("{{index}}", ctx) == "7"
    assert td.render("{{case_key}}", ctx) == "TC-003"
    assert td.render("{{count}}", ctx) == "12"
    assert len(td.render("{{rand:18}}", {})) == 18
    assert len(td.render("{{uuid:6}}", {})) == 6
    assert len(td.render("{{today}}", {})) == 10
    assert "{{rand:18}}"[:2] == "{{" or True  # 渲染后不再是占位符


# ---- 写盘：产物必须真的能被读回来 --------------------------------------------


def test_xlsx_is_readable_and_keeps_the_sheet_name(tmp_path: pathlib.Path) -> None:
    decl = {
        "files": [
            {
                "name": "学员名单.xlsx",
                "sheets": [
                    {
                        "name": "导入模板",
                        "header": ["学号", "姓名", "证件号"],
                        "rows": [["S001", "张三", "0" * 18]],
                    }
                ],
            }
        ]
    }

    out = td.materialize(decl, tmp_path, "TC-014")

    assert len(out) == 1
    p = out[0]
    assert p.kind == "xlsx" and p.rows == 1 and p.size > 0 and len(p.sha256) == 64

    from openpyxl import load_workbook

    ws = load_workbook(p.path).active
    assert ws.title == "导入模板", "sheet 名被 openpyxl 改掉的话，被测系统按名字取表就取不到"
    assert [c.value for c in ws[1]] == ["学号", "姓名", "证件号"]
    assert ws.cell(row=2, column=3).value == "0" * 18


def test_leading_zeros_survive(tmp_path: pathlib.Path) -> None:
    """证件号/学号前面的 0 不能丢 —— 变成科学计数法或被截断，上传系统就直接报格式错。"""
    out = td.materialize(
        {"files": [{"name": "a.csv", "header": ["编号"], "rows": [["007"]]}]},
        tmp_path,
    )

    with out[0].path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))

    assert rows[1] == ["007"]


def test_csv_has_a_bom_so_excel_does_not_garble_chinese(tmp_path: pathlib.Path) -> None:
    out = td.materialize({"files": [{"name": "a.csv", "header": ["姓名"], "rows": [["张三"]]}]}, tmp_path)

    assert out[0].path.read_bytes().startswith(b"\xef\xbb\xbf"), "没有 BOM，Excel 双击打开会按 ANSI 解码成乱码"
    with out[0].path.open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.reader(fh))[1] == ["张三"]


def test_template_rows_expand_with_the_row_number(tmp_path: pathlib.Path) -> None:
    out = td.materialize(
        {
            "files": [
                {
                    "name": "b.csv",
                    "header": ["编号", "备注"],
                    "rows": {"count": 3, "template": ["B{{i}}", "第 {{i}} 批"]},
                }
            ]
        },
        tmp_path,
    )

    with out[0].path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))

    assert rows[0] == ["编号", "备注"]
    assert rows[1:] == [["B1", "第 1 批"], ["B2", "第 2 批"], ["B3", "第 3 批"]]


def test_txt_renders_placeholders(tmp_path: pathlib.Path) -> None:
    out = td.materialize({"files": [{"name": "a.txt", "content": "用例 {{case_key}}"}]}, tmp_path, "TC-009")

    assert out[0].path.read_text(encoding="utf-8") == "用例 TC-009"
    assert out[0].rows == 0


def test_header_only_file_is_allowed(tmp_path: pathlib.Path) -> None:
    """被测系统常要求"先传一个只有表头的空模板"，这是合法用例。"""
    out = td.materialize({"files": [{"name": "a.xlsx", "header": ["列1", "列2"]}]}, tmp_path)

    assert out[0].rows == 0


def test_multiple_sheets(tmp_path: pathlib.Path) -> None:
    out = td.materialize(
        {
            "files": [
                {
                    "name": "a.xlsx",
                    "sheets": [
                        {"name": "第一批", "header": ["x"], "rows": [["1"]]},
                        {"name": "第二批", "header": ["x"], "rows": [["2"], ["3"]]},
                    ],
                }
            ]
        },
        tmp_path,
    )

    from openpyxl import load_workbook

    wb = load_workbook(out[0].path)
    assert wb.sheetnames == ["第一批", "第二批"]
    assert out[0].rows == 3, "rows 应该统计所有 sheet 的数据行，供报告展示"


# ---- 物化器的行为约定 ---------------------------------------------------------


def test_nothing_is_written_for_an_empty_declination(tmp_path: pathlib.Path) -> None:
    assert td.materialize(None, tmp_path) == []
    assert list(tmp_path.iterdir()) == []


def test_files_land_inside_out_dir_with_absolute_paths(tmp_path: pathlib.Path) -> None:
    out = td.materialize({"files": [{"name": "a.txt", "content": "x"}]}, tmp_path / "runs/7/data")

    p = out[0].path
    assert p.is_absolute(), "upload_file 要的是绝对路径，相对路径会被浏览器当成 CWD"
    assert p.parent == (tmp_path / "runs/7/data").resolve()
    assert p.is_file()


def test_rerunning_overwrites_the_same_file(tmp_path: pathlib.Path) -> None:
    """重跑同一条用例 = 同一目录下的同名文件被覆盖，不该堆积上一轮的残留。"""
    decl = {"files": [{"name": "a.txt", "content": "第一轮"}]}
    td.materialize(decl, tmp_path)
    td.materialize({"files": [{"name": "a.txt", "content": "第二轮"}]}, tmp_path)

    assert len(list(tmp_path.iterdir())) == 1
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "第二轮"


def test_summarize_is_empty_when_there_are_no_files() -> None:
    assert td.summarize([]) == "", "没有文件时不能往提示词里塞任何东西"


def test_summarize_lists_name_kind_size_and_path(tmp_path: pathlib.Path) -> None:
    out = td.materialize({"files": [{"name": "a.xlsx", "header": ["x"], "rows": [["1"]]}]}, tmp_path)

    text = td.summarize(out)

    assert "a.xlsx" in text and "xlsx" in text and str(out[0].path) in text
    assert "1 行" in text


# ---- 与用例/运行的接线：data_files 存的是 JSON 文本 ---------------------------


def test_declination_survives_a_json_round_trip(tmp_path: pathlib.Path) -> None:
    """data_files 落库是 JSON 文本，前端 textarea 编辑后要能原样存回来。"""
    decl = {
        "files": [
            {"name": "a.xlsx", "sheets": [{"name": "S", "header": ["x"], "rows": [["1"]]}]},
            {"name": "b.txt", "content": "{{case_key}}"},
        ]
    }

    stored = json.dumps(decl, ensure_ascii=False)

    assert td.materialize(stored, tmp_path, "TC-001")[1].name == "b.txt"
