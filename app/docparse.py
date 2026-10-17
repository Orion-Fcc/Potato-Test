"""Extract plain text from an uploaded spec / requirement document.

Used by `POST /api/projects/{pid}/knowledge/extract`, so an operator can drop in
the requirement spec instead of copy-pasting it: Word, PDF, Excel, HTML, RTF and
every plain-text-ish format.

Design constraints:
  * Only the libraries the base image already ships (python-docx, pypdf,
    openpyxl, beautifulsoup4/lxml, markdownify) — nothing new to install, so no
    base-image rebuild.
  * Chinese specs are usually GB18030-encoded, not UTF-8: decode with a fallback
    chain rather than trusting the first codec.
  * Never raise for a "weird but readable" file — sniff the bytes instead.
  * Hard caps on input size and output length: the result lands in `AppSetting.text`.

Raises `UnsupportedDocument` for formats we genuinely cannot read (legacy binary
.doc/.xls/.ppt). Every other failure to decode is `UnsupportedDocument` too, so
the API can answer 400 with one clear message.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

# ---- limits ---------------------------------------------------------------
# 100 MB. Raised from 25 MB together with the move to chunked knowledge storage:
# the old 25 MB cap and the 400k-char output cap existed only because the whole
# document had to fit in one `AppSetting.text` row and was re-split on every
# search. Chunks remove both reasons, so a merged multi-document spec (the
# 461,690-char 资源管理使用.md is the case that started this) now survives intact.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Kept as a genuine last-resort guard against a pathological file (a 100 MB
# spreadsheet can expand to hundreds of MB of text), NOT as a normal truncation.
# Set far above any real spec so the warning below should never fire in practice.
MAX_OUTPUT_CHARS = 50_000_000

# ---- format table ---------------------------------------------------------
# ext -> (kind, label)
_TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".csv",
    ".tsv",
    ".log",
    ".yaml",
    ".yml",
    ".xml",
    ".sql",
    ".ini",
    ".conf",
    ".properties",
    ".rst",
    ".adoc",
    ".text",
}
_LEGACY_BINARY = {".doc": "Word 97-2003 (.doc)", ".xls": "Excel 97-2003 (.xls)", ".ppt": "PowerPoint 97-2003 (.ppt)"}

SUPPORTED_EXT_LABEL = "md, txt, json, csv, docx, pdf, xlsx, html, rtf"


class UnsupportedDocument(ValueError):
    """The uploaded file is not something we can turn into text."""


@dataclass
class ExtractResult:
    text: str
    fmt: str
    chars: int
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


# ---- helpers --------------------------------------------------------------


def _decode(data: bytes) -> str:
    """Decode a text file, preferring UTF-8 and falling back to GB18030 (Chinese
    Windows files) then Latin-1 as a never-fail last resort."""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise UnsupportedDocument("could not decode the file as text")


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip().replace("\n", " / ")


def _rows_to_text(rows: list[list[str]], header: str | None = None) -> str:
    """Render a table as pipe-separated lines — compact, and still readable to the
    assistant's keyword search (which splits on blank lines / headings)."""
    out: list[str] = []
    if header:
        out.append(f"## {header}")
    for r in rows:
        line = " | ".join(r).strip(" |")
        if line:
            out.append(line)
    return "\n".join(out)


def _clean(text: str) -> str:
    """Collapse the whitespace noise that PDF/RTF extraction leaves behind."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _cap(text: str, result_warnings: list[str]) -> tuple[str, bool]:
    """Last-resort guard. Should never fire for a real document now that knowledge is
    stored as chunks — if it does, the extracted text is genuinely pathological
    (e.g. a 100 MB spreadsheet), and we say so instead of silently dropping it."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    result_warnings.append(
        f"正文超过 {MAX_OUTPUT_CHARS:,} 字（实际 {len(text):,}），**超出部分已截断** —— "
        "请把文档拆成几份再传，否则 AI 读不到后半部分。"
    )
    return text[:MAX_OUTPUT_CHARS], True


# ---- per-format extractors ------------------------------------------------


def _from_docx(data: bytes, w: list[str]) -> str:
    import docx  # python-docx
    from docx.table import Table as DocxTable

    doc = docx.Document(io.BytesIO(data))
    parts: list[str] = []
    tables = 0

    # ★ 按**原文顺序**输出段落与表格（2026-10-17）。
    # 原来的写法是"先所有段落、再所有表格"，于是"下表列出了…"和真正的表格被拆到两处，
    # AI 读到的上下文是错位的。`iter_inner_content()` 是 python-docx 1.1+ 的接口；
    # 老版本退回原来的顺序（宁可顺序错，也不能读不到）。
    if hasattr(doc, "iter_inner_content"):
        for block in doc.iter_inner_content():
            if isinstance(block, DocxTable):
                tables += 1
                rows = [[_cell(c.text) for c in row.cells] for row in block.rows]
                md = _rows_to_text(rows, header=f"表格 {tables}")
                if md:
                    parts.append(md)
            else:
                t = (block.text or "").strip()
                if t:
                    parts.append(t)
    else:
        parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for i, table in enumerate(doc.tables, 1):
            rows = [[_cell(c.text) for c in row.cells] for row in table.rows]
            block = _rows_to_text(rows, header=f"表格 {i}")
            if block:
                parts.append(block)

    if not parts:
        w.append(
            "这份 docx 里**没读到任何文字**（很可能是截图拼的或纯图片文档），AI 读不到内容。"
            "建议把内容另存成 .md 再传一次，或者点「原件」自己核对。"
        )
    return "\n\n".join(parts)


def _from_pdf(data: bytes, w: list[str]) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:  # noqa: BLE001 — surface as a warning, try anyway
            w.append(f"这份 PDF 是加密的（{type(exc).__name__}），可能只能读到部分文字")
    pages: list[str] = []
    empty = 0
    for i, page in enumerate(reader.pages, 1):
        try:
            t = (page.extract_text() or "").strip()
        except Exception as e:  # noqa: BLE001 — one bad page shouldn't kill the import
            w.append(f"第 {i} 页读不出来（{type(exc).__name__}）")
            continue
        if t:
            pages.append(f"## 第 {i} 页\n{t}")
        else:
            empty += 1
    if empty:
        w.append(
            f"有 {empty} 页**没有文字层**（扫描件/图片页），AI 读不到这几页的内容。"
            "建议把它们另存成 .md 再传，或点「原件」自己核对。"
        )
    return "\n\n".join(pages)


def _from_xlsx(data: bytes, w: list[str]) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    blocks: list[str] = []
    for ws in wb.worksheets:
        rows = [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
        while rows and not any(rows[-1]):
            rows.pop()
        if not rows:
            continue
        blocks.append(_rows_to_text(rows, header=f"工作表：{ws.title}"))
    try:
        wb.close()
    except Exception:  # noqa: BLE001
        pass
    if not blocks:
        w.append("所有工作表都是空的")
    return "\n\n".join(blocks)


def _from_html(data: bytes, w: list[str]) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    try:
        from markdownify import markdownify

        return markdownify(str(soup), heading_style="ATX").strip()
    except Exception as e:  # noqa: BLE001 — malformed HTML still has text
        w.append(f"HTML 转 markdown 失败（{type(exc).__name__}），已退回纯文本 —— 内容还在，格式丢了")
        return soup.get_text("\n")


# RTF: destinations whose content is not document text — skip the whole group.
_RTF_SKIP = {
    "fonttbl",
    "colortbl",
    "stylesheet",
    "info",
    "pict",
    "object",
    "themedata",
    "colorschememapping",
    "latentstyles",
    "datastore",
    "listtable",
    "listoverridetable",
    "rsidtbl",
    "generator",
    "xmlnstbl",
    "filetbl",
    "header",
    "footer",
    "footnote",
}
_RTF_GROUP = re.compile(r"\{\s*\\\*?\s*([a-zA-Z]+)")
_RTF_UNICODE = re.compile(r"\\u(-?\d+) ?")


def _from_rtf(data: bytes) -> str:
    """Small RTF reader.

    Word writes non-ASCII as either `\\uNNNN` (unicode escape) or `\\'hh` (one byte in
    the document's ANSI codepage, declared by `\\ansicpg`). Handling both is what makes
    a Chinese spec come out readable instead of as mojibake; tables come out flat
    (`\\tab` -> ` | `).
    """
    s = data.decode("latin-1", errors="replace")
    cp_match = re.search(r"\\ansicpg(\d+)", s)
    codepage = f"cp{cp_match.group(1)}" if cp_match else "cp1252"

    out: list[str] = []
    buf = bytearray()
    uc_skip = 1  # RTF default: one ANSI fallback char follows each \uN (usually "?")

    def flush() -> None:
        if not buf:
            return
        try:
            out.append(buf.decode(codepage, errors="replace"))
        except LookupError:
            out.append(buf.decode("latin-1", errors="replace"))
        buf.clear()

    i, n, skip = 0, len(s), 0
    while i < n:
        c = s[i]
        if c == "{":
            header = _RTF_GROUP.match(s, i)
            if skip or (header and header.group(1).lower() in _RTF_SKIP):
                skip += 1
            i += 1
        elif c == "}":
            if skip:
                skip -= 1
            i += 1
        elif skip:
            i += 1
        elif c == "\\":
            if s[i + 1 : i + 2] == "'":
                buf.append(int(s[i + 2 : i + 4], 16))
                i += 4
                continue
            uni = _RTF_UNICODE.match(s, i)
            if uni:
                flush()
                code = int(uni.group(1))
                out.append(chr(code + 65536 if code < 0 else code))
                i = uni.end()
                # drop the ANSI fallback char(s) Word writes after each \uN
                for _ in range(uc_skip):
                    if i < n and s[i] not in "\\{}":
                        i += 1
                continue
            uc = re.match(r"\\uc(\d+) ?", s[i:])
            if uc:
                uc_skip = int(uc.group(1))
                i += uc.end()
                continue
            word = re.match(r"\\(par|line|tab)\b ?", s[i:])
            if word:
                flush()
                out.append("\n" if word.group(1) != "tab" else " | ")
                i += word.end()
                continue
            ctrl = re.match(r"\\[a-zA-Z]+-?\d* ?", s[i:])
            i += ctrl.end() if ctrl else 1
        else:
            buf.append(ord(c) & 0xFF)
            i += 1
    flush()
    return _clean("".join(out))


# ---- entry point ----------------------------------------------------------


def extract(filename: str, data: bytes) -> ExtractResult:
    """Turn one uploaded file into searchable text.

    `filename` is used for the extension; unknown extensions fall back to
    content sniffing, then to a plain-text decode.
    """
    if not data:
        raise UnsupportedDocument("the file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UnsupportedDocument(f"the file is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB")

    name = (filename or "").strip()
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    warnings: list[str] = []

    if ext in _LEGACY_BINARY:
        raise UnsupportedDocument(
            f"{_LEGACY_BINARY[ext]} is not supported — save it as "
            f"{'.docx' if ext == '.doc' else '.xlsx' if ext == '.xls' else '.pptx'} and upload again"
        )

    head = data[:8]
    is_zip = head[:2] == b"PK"
    is_pdf = head[:4] == b"%PDF"

    # 1) zip container: OOXML (docx/xlsx) or a bare zip
    if is_zip:
        if ext in (".xlsx", ".xlsm", ".xltx"):
            text, fmt = _from_xlsx(data, warnings), "xlsx"
        else:
            text, fmt = _from_docx(data, warnings), "docx"
    # 2) PDF, by extension or magic bytes
    elif is_pdf:
        text, fmt = _from_pdf(data, warnings), "pdf"
    # 3) HTML, by extension or sniff
    elif ext in (".html", ".htm", ".xhtml") or b"<html" in data[:4096].lower():
        text, fmt = _from_html(data, warnings), "html"
    # 4) RTF
    elif ext == ".rtf" or data[:5] == b"{\\rtf":
        text, fmt = _from_rtf(data), "rtf"
    # 5) plain text (and anything else we can decode)
    elif ext in _TEXT_EXTS or ext == "" or b"\x00" not in data[:8192]:
        text, fmt = _decode(data), ext.lstrip(".") or "text"
        if ext not in _TEXT_EXTS and ext:
            warnings.append(f"扩展名 '{ext}' 不认识，已按纯文本读 —— 若出现乱码，请转成 .md 再传")
    else:
        raise UnsupportedDocument(
            f"不支持这种文件类型 '{ext}' —— 支持：{SUPPORTED_EXT_LABEL}。建议先转成 .md。"
        )

    text = _clean(text)
    text, truncated = _cap(text, warnings)
    if not text.strip():
        raise UnsupportedDocument(
            "没读到任何文字 —— 可能是扫描件 / 纯图片，或文件加密了。"
            "建议转成 .md 再传，或直接看「原件」。"
        )
    # ★ 覆盖率粗判（2026-10-17 用户要求「确保 AI 能读到所有内容」）。
    # 最危险的不是报错，而是"读得很少却没报错"：界面上看着上传成功，AI 却几乎没看到东西。
    # 用很宽松的比例兜底（40 KB 的文件读到不足 400 字才算异常），
    # 只在**明显**不成比例时才说话 —— 别对正常的小文件瞎报警。
    if len(data) > 40_000 and len(text) < 400:
        warnings.append(
            f"整份文件 {len(data) // 1024} KB，却只读到 {len(text)} 个字 —— 很可能是图片 / 扫描件。"
            "AI 基本读不到内容，建议另存成 .md 再传，或点「原件」自己看。"
        )
    return ExtractResult(text=text, fmt=fmt, chars=len(text), truncated=truncated, warnings=warnings)
