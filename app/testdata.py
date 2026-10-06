r"""把用例声明的测试数据物化成**真实的文件**，供 agent 通过 `upload_file` 上传。

为什么需要它
------------
有些被测系统的用例是"先导入一份 Excel，再验证导入结果"。agent 手里有
`upload_file` 动作（browser-use 0.13 自带），但**它写不出 .xlsx** ——
实测 browser-use 的 FileSystem 沙箱只支持
`md txt json jsonl csv pdf docx html xml`，`write_file('x.xlsx')` 直接报
`Unsupported file extension`。

于是分工是：**数据在用例里声明（确定性），文件在执行前生成（可复现）**。
不让 agent 现场写代码生成文件，是因为那样两次跑同一���用例可能产出不同内容 ——
而"传上去的文件不对"这种失败最难定位：截图看不出差别。

声明格式（存 `test_case.data_files`，JSON）
------------------------------------------
    {
      "files": [
        {"name": "学员名单.xlsx", "kind": "xlsx",
         "sheets": [{"name": "导入模板",
                     "header": ["学号", "姓名", "证件号"],
                     "rows": [["S001", "张三", "{{rand:18}}"],
                              ["S002", "李四", "{{rand:18}}"]]}]},
        {"name": "批量.csv", "kind": "csv",
         "header": ["编号", "备注"],
         "rows": {"count": 200, "template": ["B{{i}}", "第 {{i}} 批"]}},
        {"name": "说明.txt", "kind": "txt", "content": "共 {{count}} 条"}
      ]
    }

`rows` 可以是三种形态：
  * 二维数组 —— 字面量，逐行写出
  * `{"count": N, "template": [...]}` —— 重复 N 行，模板里可用 `{{i}}`
  * 省略 —— 只有表头（被测系统常要求"先传一个空模板"）

占位符（对所有单元格与 txt 内容生效）
------------------------------------
  {{i}} / {{index}}   当前行的序号，从 1 开始
  {{case_key}}        用例编号，例如 TC-001
  {{rand:N}}          N 位随机数字
  {{uuid:N}}          N 位十六进制
  {{today}}           YYYY-MM-DD
  {{now}}             YYYY-MM-DD HH:MM:SS
  {{count}}           本文件总共写了几行（只有 txt 用得上）

安全边界
--------
文件名必须过 `_safe_name()`：禁目录分隔符与盘符、禁 `..`、禁 Windows 保留名、
长度封顶。**这不是洁癖** —— 文件名会进 `upload_file` 的路径，浏览器接受任意
本地路径，如果不拦，一条用例就能把 `C:\Users\<用户名>\.env` 传给被测系统上传。
"""

from __future__ import annotations

import csv
import hashlib

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---- 上限：一份声明不该能写出一个 GB 的文件 ---------------------------------
MAX_FILES = 8
MAX_SHEETS = 8
MAX_ROWS = 2000
MAX_COLS = 64
MAX_CELL_LEN = 512
MAX_TXT_LEN = 200_000
MAX_TOTAL_BYTES = 32 * 1024 * 1024

# 扩展名 → kind。kind 可以省略，由扩展名推断。
KIND_BY_EXT = {".xlsx": "xlsx", ".xlsm": "xlsx", ".csv": "csv", ".txt": "txt", ".log": "txt"}
ALLOWED_KINDS = tuple(sorted(set(KIND_BY_EXT.values())))

# Windows 保留设备名：CON、PRN 这类名字在 Windows 上不能当文件名。
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_]+)\s*(?::\s*(\d+)\s*)?\}\}")


class SpecError(ValueError):
    """声明写错了。消息必须能直接告诉用户哪一行、怎么改。"""


@dataclass(frozen=True)
class PreparedFile:
    """一个已经写到磁盘、可交给 upload_file 的文件。"""

    name: str
    path: Path
    kind: str
    size: int
    sha256: str
    rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "rows": self.rows,
        }


@dataclass
class _Sheet:
    name: str
    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


# ---- 文件名 -----------------------------------------------------------------


def _safe_name(raw: Any, expect_ext: str | None = None) -> str:
    """把声明里的文件名变成一个安全的**纯文件名**。

    为什么要这么严：文件名会进 `upload_file` 的路径参数，而浏览器接受任意本地
    路径。一条用例如果能写 `"name": "../../.env"`，就等于把用户配置上传给了
    被测系统。所以这里不接受任何目录结构 —— 要放子目录请改 out_dir 的组织方式。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise SpecError("files[].name 必须是非空字符串")
    name = raw.strip()
    if _ILLEGAL_CHARS.search(name):
        raise SpecError(
            f"文件名含非法字符：{raw!r}。不能包含 < > : \" / \\ | ? * 或控制字符"
            "（要放子目录请改用 runs/<结果id>/data/ 下的固定结构）"
        )
    if name in {".", ".."} or name.startswith("."):
        raise SpecError(f"文件名不能以点开头，也不能是 . 或 ..：{raw!r}")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.lower() in _RESERVED:
        raise SpecError(f"{stem!r} 是 Windows 保留设备名，换一个：{raw!r}")
    if len(name) > 120:
        raise SpecError(f"文件名过长（{len(name)} > 120）：{name[:40]}…")
    if expect_ext and ext.lower() != expect_ext:
        raise SpecError(
            f"扩展名与 kind 不一致：{raw!r}（kind 决定的扩展名是 {expect_ext}）"
        )
    return name


def _kind_of(name: str, raw_kind: Any) -> str:
    _, dot, ext = name.rpartition(".")
    inferred = KIND_BY_EXT.get("." + ext.lower()) if dot else None
    if raw_kind in (None, ""):
        if inferred is None:
            raise SpecError(
                f"{name!r} 认不出格式：显式写 kind，或用 "
                f"{'/'.join(sorted(KIND_BY_EXT))} 结尾的文件名"
            )
        return inferred
    if raw_kind not in ALLOWED_KINDS:
        raise SpecError(f"kind={raw_kind!r} 不支持，只支持 {', '.join(ALLOWED_KINDS)}")
    if inferred and inferred != raw_kind:
        raise SpecError(
            f"kind={raw_kind!r} 与文件名扩展名 {('.'+ext.lower())!r} 矛盾"
            "（浏览器按扩展名判类型，名字和内容不一致最难排查）"
        )
    return raw_kind


# ---- 占位符 -----------------------------------------------------------------


def _rand_digits(n: int) -> str:
    # randbelow 会被 secrets 用；这里用标准库 random 的 SystemRandom，
    # 因为它读的是 OS 熵源，不会因为默认 seed 相同而每次跑出同一串。
    import secrets

    return "".join(secrets.choice("0123456789") for _ in range(n))


def render(value: Any, ctx: dict[str, Any]) -> str:
    """把一个单元格/一段文本里的占位符替换掉。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)

    def sub(m: re.Match[str]) -> str:
        key, arg = m.group(1), m.group(2)
        n = int(arg) if arg else 0
        if key in ("i", "index", "row"):
            return str(ctx.get("index", 1))
        if key == "case_key":
            return str(ctx.get("case_key", ""))
        if key == "count":
            return str(ctx.get("count", ""))
        if key == "today":
            return datetime.now().strftime("%Y-%m-%d")
        if key == "now":
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if key == "rand":
            return _rand_digits(n or 4)
        if key == "uuid":
            return uuid.uuid4().hex[: (n or 8)]
        # 未知占位符原样留着：宁可让上传的文件里出现 "{{foo}}" 让人一眼看见，
        # 也不要悄悄替换成空串 —— 后者会变成"字段填了但系统说格式不对"。
        return m.group(0)

    out = _PLACEHOLDER.sub(sub, text)
    if len(out) > MAX_CELL_LEN:
        raise SpecError(f"单元格过长（{len(out)} > {MAX_CELL_LEN}）：{out[:40]}…")
    return out


# ---- 声明解析 ---------------------------------------------------------------


def _rows_of(spec: Any, header_len: int) -> list[list[str]]:
    """把 rows 的三种形态统一成"待渲染的行"。"""
    if spec is None:
        return []
    if isinstance(spec, int):
        raise SpecError(
            "rows 直接写数字是不行的 —— 那样没有内容可写。请用 "
            '{"count": N, "template": [...]} 来重复某一行。'
        )
    if isinstance(spec, dict):
        count = spec.get("count")
        template = spec.get("template")
        if not isinstance(count, int) or count < 0:
            raise SpecError("rows.count 必须是非负整数")
        if not isinstance(template, list) or not template:
            raise SpecError("rows.template 必须是非空数组（重复的是哪一行？）")
        if count > MAX_ROWS:
            raise SpecError(f"rows.count={count} 超过上限 {MAX_ROWS}")
        return [list(template) for _ in range(count)]
    if isinstance(spec, list):
        rows: list[list[str]] = []
        for i, row in enumerate(spec, 1):
            if not isinstance(row, list):
                raise SpecError(f"rows 第 {i} 行不是数组：{row!r}")
            if len(row) > MAX_COLS:
                raise SpecError(f"rows 第 {i} 行有 {len(row)} 列，超过上限 {MAX_COLS}")
            rows.append(list(row))
        if len(rows) > MAX_ROWS:
            raise SpecError(f"rows 有 {len(rows)} 行，超过上限 {MAX_ROWS}")
        return rows
    raise SpecError(f"rows 只能是数组、整数或含 count/template 的对象，收到 {type(spec).__name__}")


def _sheets_of(fspec: dict[str, Any], kind: str) -> list[_Sheet]:
    raw = fspec.get("sheets")
    if raw is None:
        # 简写：csv/txt 只需要一张表；xlsx 也允许只给 header/rows（落到 Sheet1）
        raw = [
            {
                "name": fspec.get("sheet") or ("Sheet1" if kind == "xlsx" else kind),
                "header": fspec.get("header"),
                "rows": fspec.get("rows"),
            }
        ]
    if not isinstance(raw, list) or not raw:
        raise SpecError("sheets 必须是非空数组")
    if len(raw) > MAX_SHEETS:
        raise SpecError(f"sheets 有 {len(raw)} 个，超过上限 {MAX_SHEETS}")

    sheets: list[_Sheet] = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            raise SpecError(f"sheets[{i}] 不是对象")
        header = s.get("header") or []
        if not isinstance(header, list):
            raise SpecError(f"sheets[{i}].header 必须是数组")
        if len(header) > MAX_COLS:
            raise SpecError(f"sheets[{i}].header 有 {len(header)} 列，超过上限 {MAX_COLS}")
        sheets.append(
            _Sheet(
                name=str(s.get("name") or f"Sheet{i}"),
                header=[render(h, {"index": 0}) for h in header],
                rows=_rows_of(s.get("rows"), len(header)),
            )
        )
    return sheets


def parse(decl: Any) -> list[dict[str, Any]]:
    """校验并归一化 data_files 声明。**只校验，不写盘。**

    分成"校验"和"写盘"两步是有意的：UI 要能在保存前就告诉用户声明写错了，
    而写盘发生在执行期（那时用例可能已经在跑了）。
    """
    if decl in (None, "", {}):
        return []
    if isinstance(decl, str):
        import json

        try:
            decl = json.loads(decl)
        except ValueError as exc:
            raise SpecError(f"data_files 不是合法 JSON：{exc}") from exc
    if isinstance(decl, list):
        decl = {"files": decl}
    if not isinstance(decl, dict):
        raise SpecError("data_files 必须长这样：{\"files\": [ ... ]}")
    files = decl.get("files")
    if files in (None, []):
        return []
    if not isinstance(files, list):
        raise SpecError("data_files.files 必须是数组")
    if len(files) > MAX_FILES:
        raise SpecError(f"files 有 {len(files)} 个，超过上限 {MAX_FILES}")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, f in enumerate(files, 1):
        if not isinstance(f, dict):
            raise SpecError(f"files[{i}] 不是对象")
        name = _safe_name(f.get("name"))
        kind = _kind_of(name, f.get("kind"))
        # 名字的扩展名必须与 kind 对得上：浏览器按扩展名判类型，
        # 出现"叫 .txt 却是 xlsx 内容"时，报错会指向完全错误的方向。
        expect = {"xlsx": (".xlsx", ".xlsm"), "csv": (".csv",), "txt": (".txt", ".log")}[kind]
        if not name.lower().endswith(expect):
            raise SpecError(
                f"files[{i}] 的名字 {name!r} 与 kind={kind!r} 不匹配"
                f"（{kind} 应该是 {'/'.join(expect)} 结尾）"
            )
        if name.lower() in seen:
            raise SpecError(f"files 里有重复文件名：{name!r}")
        seen.add(name.lower())
        entry: dict[str, Any] = {"name": name, "kind": kind}
        if kind == "txt":
            content = f.get("content")
            if not isinstance(content, str):
                raise SpecError(f"files[{i}] 是 txt，必须给 content 字符串")
            if len(content) > MAX_TXT_LEN:
                raise SpecError(f"content 过长（{len(content)} > {MAX_TXT_LEN}）")
            entry["content"] = content
        else:
            entry["sheets"] = [
                {"name": s.name, "header": s.header, "rows": s.rows} for s in _sheets_of(f, kind)
            ]
        out.append(entry)
    return out


# ---- 写盘 -------------------------------------------------------------------


def _write_xlsx(path: Path, sheets: list[_Sheet], ctx: dict[str, Any]) -> int:
    from openpyxl import Workbook

    wb = Workbook()
    total = 0
    for i, sheet in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        # openpyxl 会自动补"Sheet"/"Sheet1"，显式命名才不会被它改掉
        try:
            ws.title = sheet.name[:31] or f"Sheet{i + 1}"
        except ValueError:
            ws.title = f"Sheet{i + 1}"[:31]
        if sheet.header:
            ws.append(sheet.header)
        for idx, row in enumerate(sheet.rows, 1):
            cells = [render(c, {**ctx, "index": idx}) for c in row]
            if len(cells) < len(sheet.header):
                cells += [""] * (len(sheet.header) - len(cells))
            ws.append(cells)
            total += 1
    wb.save(path)
    wb.close()
    return total


def _write_csv(path: Path, sheets: list[_Sheet], ctx: dict[str, Any]) -> int:
    # utf-8-sig：Excel 双击打开中文 CSV 不乱码（无 BOM 时它会按 ANSI 解）。
    # 这是"导入类用例"最常见的失败原因之一，值得为它多写 3 个字节。
    total = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        for sheet in sheets:
            if sheet.header:
                w.writerow(sheet.header)
            for idx, row in enumerate(sheet.rows, 1):
                cells = [render(c, {**ctx, "index": idx}) for c in row]
                if len(cells) < len(sheet.header):
                    cells += [""] * (len(sheet.header) - len(cells))
                w.writerow(cells)
                total += 1
    return total


def materialize(
    decl: Any,
    out_dir: Path,
    case_key: str = "",
) -> list[PreparedFile]:
    """把声明写成一组真实文件，返回可直接喂给 `upload_file` 的路径。

    同一个 out_dir 里重复调用会覆盖同名文件 —— 这正是"重跑同一条用例"的语义。
    """
    entries = parse(decl)
    if not entries:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    prepared: list[PreparedFile] = []
    total_bytes = 0
    for entry in entries:
        name = entry["name"]
        target = out_dir / name
        # 双保险：即便 _safe_name 漏了 something，这里再挡一次"解析后不在目录内"
        if target.resolve().parent != out_dir.resolve():
            raise SpecError(f"拒绝写到 {out_dir} 之外：{name!r}")

        ctx = {"case_key": case_key}
        if entry["kind"] == "txt":
            text = render(entry["content"], {**ctx, "count": 0})
            target.write_text(text, encoding="utf-8")
            rows = 0
        else:
            sheets = [
                _Sheet(name=s["name"], header=list(s["header"]), rows=[list(r) for r in s["rows"]])
                for s in entry["sheets"]
            ]
            if entry["kind"] == "xlsx":
                rows = _write_xlsx(target, sheets, ctx)
            else:
                rows = _write_csv(target, sheets, ctx)

        data = target.read_bytes()
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise SpecError(
                f"生成的测试数据超过 {MAX_TOTAL_BYTES // 1048576} MB 上限 —— "
                "确认 rows.count 写对了？"
            )
        prepared.append(
            PreparedFile(
                name=name,
                path=target.resolve(),
                kind=entry["kind"],
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                rows=rows,
            )
        )
    return prepared


def summarize(prepared: list[PreparedFile]) -> str:
    """给系统提示词用的一句话说明。空列表返回空串（不注入噪音）。"""
    if not prepared:
        return ""
    lines = [f"本次运行已为你准备好 {len(prepared)} 个文件（由用例的「测试数据」声明自动生成）："]
    for p in prepared:
        lines.append(
            f"  - {p.name}（{p.kind}，{p.size} 字节"
            + (f"，{p.rows} 行数据" if p.rows else "")
            + f"，绝对路径 {p.path}）"
        )
    return "\n".join(lines)


def iter_spec_examples() -> Iterator[dict[str, Any]]:
    """给 UI 用的声明模板。"""
    yield {
        "name": "导入模板.xlsx",
        "kind": "xlsx",
        "sheets": [
            {
                "name": "Sheet1",
                "header": ["学号", "姓名", "证件号"],
                "rows": [
                    ["S001", "张三", "{{rand:18}}"],
                    ["S002", "李四", "{{rand:18}}"],
                ],
            }
        ],
    }
    yield {
        "name": "批量数据.csv",
        "kind": "csv",
        "header": ["编号"],
        "rows": {"count": 50, "template": ["B{{i}}"]},
    }
    yield {"name": "说明.txt", "kind": "txt", "content": "共 {{count}} 条"}
