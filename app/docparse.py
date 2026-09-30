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
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB — a spec doc is never bigger
MAX_OUTPUT_CHARS = 400_000  # ~400k chars of knowledge is plenty for retrieval

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
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    result_warnings.append(f"text truncated at {MAX_OUTPUT_CHARS:,} characters")
    return text[:MAX_OUTPUT_CHARS], True


# ---- per-format extractors ------------------------------------------------


def _from_docx(data: bytes, w: list[str]) -> str:
    import docx  # python-docx

    doc = docx.Document(io.BytesIO(data))
    parts: list[str] = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for i, table in enumerate(doc.tables, 1):
        rows = [[_cell(c.text) for c in row.cells] for row in table.rows]
        block = _rows_to_text(rows, header=f"表格 {i}")
        if block:
            parts.append(block)
    if not parts:
        w.append("the .docx has no extractable text (images-only document?)")
    return "\n\n".join(parts)


def _from_pdf(data: bytes, w: list[str]) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:  # noqa: BLE001 — surface as a warning, try anyway
            w.append(f"the PDF is encrypted ({type(e).__name__}) — text may be empty")
    pages: list[str] = []
    empty = 0
    for i, page in enumerate(reader.pages, 1):
        try:
            t = (page.extract_text() or "").strip()
        except Exception as e:  # noqa: BLE001 — one bad page shouldn't kill the import
            w.append(f"page {i} could not be read ({type(e).__name__})")
            continue
        if t:
            pages.append(f"## 第 {i} 页\n{t}")
        else:
            empty += 1
    if empty:
        w.append(f"{empty} page(s) had no text layer — a scanned PDF needs OCR")
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
        w.append("every sheet is empty")
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
        w.append(f"markdown conversion failed ({type(e).__name__}) — using plain text")
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
            warnings.append(f"unknown extension '{ext}' — read as plain text")
    else:
        raise UnsupportedDocument(f"unsupported file type '{ext}' — supported: {SUPPORTED_EXT_LABEL}")

    text = _clean(text)
    text, truncated = _cap(text, warnings)
    if not text.strip():
        raise UnsupportedDocument(
            "no text could be extracted — the file may be a scan/image, or password-protected"
        )
    return ExtractResult(text=text, fmt=fmt, chars=len(text), truncated=truncated, warnings=warnings)
