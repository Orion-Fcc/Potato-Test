"""助手读写「项目文档」（2026-10-12）。

## 用户要的两件事

1. 「我希望助手有阅读并分析项目总览那里所有功能的能力」——
   助手要能读到项目总览/需求分析/流程图里写了什么，而不只是检索需规原文。
2. 「助手自己新建文件到项目总览里面」——
   助手要能往项目文档里写东西，而不是只给一个可下载的 Markdown。

## 为什么单独一个模块

这两个能力的共同点是**都要处理正文格式**：
读的时候 HTML 要变成模型能理解的结构（尤其是 Mermaid 源码必须原样给出，
否则模型看到的是被剥掉标记的一串词），写的时候 Markdown 要变成 HTML。
把它们和助手那 800 行的工具分发混在一起会很难维护。

## 这两条路的边界

* 读：只读，不改任何东西。
* 写：写进 `ProjectDoc`（和人在「项目文档」页里编辑的是同一张表、同一份内容），
  所以助手写的东西**用户能直接在上面改** —— 这正是我们要的：
  助手起稿，人负责定稿。
"""

from __future__ import annotations

import re

# 项目文档的四种 kind（与 app/api.py 的 _DOC_KINDS 对齐）
KINDS = ("overview", "flow", "custom")
_KIND_LABEL = {
    "overview": "项目总览",
    "flow": "流程图",
    "custom": "自定义文档",
}


def html_to_readable(html: str, *, mermaid_as_source: bool = True) -> str:
    """把项目文档的 HTML 变成喂给模型的可读文本。纯函数。

    ## 为什么不能直接用 `doc_sanitize.html_to_text`

    那个函数是"把标签全去掉"，对普通段落够用，但会把**图表源码**弄坏：
    项目文档里流程图存成 `<pre class="tp-mermaid">flowchart TD …</pre>`，
    全去标签后就变成 `flowchart TD A --> B`（换行没了）。
    模型拿到这种文本会以为那只是一句话，而不是一张图的定义。

    所以这里把图表块**单独原样摘出来**并标明它是图 —— 模型才可能理解
    "这一节描述的是这张流程图"，也才可能去改它。
    """
    if not html:
        return ""
    blocks: list[str] = []
    # 先摘图表源码
    if mermaid_as_source:
        for m in re.finditer(r'<pre[^>]*class="[^"]*tp-mermaid[^"]*"[^>]*>(.*?)</pre>', html, re.S):
            src = re.sub(r"<[^>]+>", "", m.group(1))
            src = (
                src.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
            )
            blocks.append("[流程图源码]\n" + src.strip())
        html = re.sub(r'<pre[^>]*class="[^"]*tp-mermaid[^"]*"[^>]*>.*?</pre>', "\n", html, flags=re.S)

    text = html
    text = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", r"\n## \1\n", text, flags=re.S | re.I)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", text, flags=re.S | re.I)
    text = re.sub(r"<tr[^>]*>(.*?)</tr>", r"\n| \1", text, flags=re.S | re.I)
    text = re.sub(r"</t[dh]>\s*<t[dh][^>]*>", " | ", text, flags=re.I)
    text = re.sub(r"</p>|<br\s*/?>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        text = text.replace(a, b)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    body = text.strip()
    return (body + "\n\n" + "\n\n".join(blocks)).strip() if blocks else body


def clamp(text: str, cap: int) -> tuple[str, bool]:
    """截断长文本并**如实说明截断了**。

    不说明的话，模型会以为读全了，然后对没读到的部分照样下结论 ——
    这比不给它内容更危险（它在用一份缺页的资料做判断，而自己不知道）。
    """
    t = text or ""
    if len(t) <= cap:
        return t, False
    return t[:cap], True


def summarize_doc_row(
    kind: str,
    title: str,
    html: str,
    doc_id: int | None = None,
    source_name: str = "",
    source_bytes: int = 0,
) -> dict:
    """一份文档的索引信息（不含正文）—— 先让模型知道有哪些文档。"""
    plain = html_to_readable(html)
    return {
        "kind": kind,
        "title": title or _KIND_LABEL.get(kind, kind),
        "chars": len(html or ""),
        "readable_chars": len(plain),
        "has_diagram": 'class="tp-mermaid"' in (html or ""),
        "empty": not (html or "").strip(),
        # 上传的原件（2026-10-16）。**索引里就要报出来**：否则模型不知道"还有一份原文"，
        # 遇到正文里缺表格/丢层级时只会说"文档里没写"，而不是去读原件。
        "doc_id": doc_id,
        "has_original": bool(source_name),
        "original_name": source_name,
        "original_bytes": source_bytes,
    }


def default_title(kind: str) -> str:
    return _KIND_LABEL.get(kind, "自定义文档")


def guessed_kind(name: str) -> str:
    """从用户的话里猜要写进哪份文档。**猜不出就返回空**，让调用方决定。

    只做很保守的关键词匹配，不做语义判断 —— 猜错了会把流程图的内容写进总览里，
    而那种错误用户很难发现（两份文档都有内容，只是内容放错了地方）。
    """
    s = (name or "").strip()
    if not s:
        return ""
    if any(k in s for k in ("流程", "flow", "图")):
        return "flow"
    # 「需求分析」已并入项目总览（2026-10-20），旧叫法继续认到总览。
    if any(k in s for k in ("需求分析", "分析", "analysis", "总览", "概览", "overview", "说明书")):
        return "overview"
    return ""
