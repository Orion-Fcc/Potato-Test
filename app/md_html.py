"""极小的 Markdown → HTML 转换（2026-10-12）。

## 为什么需要它

助手（还有别的 agent）写文档时的自然输出是 **Markdown**，而项目文档
（`ProjectDoc.html`）存的是 **HTML**（富文本编辑器要它）。于是"让助手往项目总览里
写一份东西"这个需求，中间必须有一次转换。

## 为什么不用 markdown / commonmark 库

本项目的依赖表是按"一个人一台机器跑起来"精心收窄的（见 `pyproject.toml` 的注释）。
为了一个只服务"助手写文档"的功能加一个 Markdown 解析器不划算 —— 而且
**转换后的 HTML 无论如何都要过 `doc_sanitize.sanitize`**，所以这里不需要完整实现
Markdown 规范，只需要覆盖人真正会写的那些构造。

## 覆盖范围（刻意收敛）

标题 / 段落 / 无序与有序列表（含一层嵌套）/ 表格 / **加粗** *斜体* `代码` /
引用 / 分隔线 / 围栏代码块。**不覆盖**图片、链接自动识别、脚注、任务列表 ——
它们要么用不到，要么需要完整的行内解析器。

不属于自己的构造时**原样当文本输出**（并转义），绝不猜。
"""

from __future__ import annotations

import re

_esc_re = re.compile(r"[&<>]")


def _esc(s: str) -> str:
    return _esc_re.sub(lambda m: {"&": "&amp;", "<": "&lt;", ">": "&gt;"}[m.group(0)], s)


def _inline(s: str) -> str:
    """行内标记：先转义，再替换标记，最后逃过已生成的标签。"""
    out = _esc(s)
    # 用占位符保护代码片段，避免里面的 `*` 被当成强调
    codes: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    out = re.sub(r"`([^`]+)`", _stash, out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    for i, c in enumerate(codes):
        out = out.replace(f"\x00{i}\x00", f"<code>{c}</code>")
    return out


_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _split_row(line: str) -> list[str]:
    t = line.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|"):
        t = t[:-1]
    return [c.strip() for c in t.split("|")]


def markdown_to_html(md: str) -> str:
    """把 Markdown 转成富文本编辑器能吃的 HTML 片段。纯函数。"""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    def flush_para(buf: list[str]) -> None:
        text = " ".join(x.strip() for x in buf if x.strip())
        if text:
            out.append(f"<p>{_inline(text)}</p>")

    para: list[str] = []
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 围栏代码块：原样保留（chunk 里的图表源码就走这条路）
        if stripped.startswith("```"):
            flush_para(para)
            para = []
            i += 1
            body: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # 跳过收尾的 ```
            out.append("<pre>" + _esc("\n".join(body)) + "</pre>")
            continue

        # 空行 = 段落结束
        if not stripped:
            flush_para(para)
            para = []
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", line):
            flush_para(para)
            para = []
            out.append("<hr>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para(para)
            para = []
            lvl = min(6, max(1, len(m.group(1))))
            out.append(f"<h{lvl}>{_inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        # 表格：当前行是 | … |，下一行是分隔行
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1].strip()):
            flush_para(para)
            para = []
            head = _split_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            # 变量名别复用 `body`：上面代码块分支里那个 `body` 是 list[str]，
            # 同名混用会让类型在两处漂移（mypy 报的就是这个）。
            rows_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{rows_html}</tbody></table>")
            continue

        # 引用
        if stripped.startswith(">"):
            flush_para(para)
            para = []
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote><p>{_inline(' '.join(quote))}</p></blockquote>")
            continue

        # 列表（含一层嵌套缩进）
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            flush_para(para)
            para = []
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", line))
            items: list[tuple[int, str]] = []
            while i < n:
                lm = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if lm:
                    items.append((len(lm.group(1)), lm.group(3).strip()))
                    i += 1
                elif lines[i].strip() and lines[i].startswith(("  ", "\t")):
                    # 续行：接到上一条
                    if items:
                        d, t = items[-1]
                        items[-1] = (d, f"{t} {lines[i].strip()}")
                    i += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            html = [f"<{tag}>"]
            base = min((d for d, _ in items), default=0)
            for indent, text in items:
                # 只支持一层嵌套：比基准缩进多就算子列表
                if indent > base and html and html[-1] == f"<{tag}>":
                    html.append(f"<{tag}>")
                html.append(f"<li>{_inline(text)}</li>")
            html.append(f"</{tag}>")
            out.append("".join(html))
            continue

        para.append(line)
        i += 1

    flush_para(para)
    return "\n".join(p for p in out if p)
