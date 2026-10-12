"""富文本正文的净化（2026-10-12）。

## 为什么这个模块非有不可

项目文档（`ProjectDoc.html`）存的是**用户/模型产出的 HTML**，然后原样渲染回页面。
这条路上一旦漏掉净化，任何能写文档的人（或任何一次模型输出）都能存进一段
`<script>`，之后**每个打开这个页面的同事**都会执行它 —— 存储型 XSS。

而它比普通 XSS 更值得防：本系统的定位是内部工具，用户会从各种地方**粘贴**
内容（Word、Confluence、别人发的邮件），粘贴板里带 `<script>`/`onerror` 是很常见的。

## 为什么不用 bleach / nh3

它们更成熟，但都要新增一个后端依赖，而本项目的依赖表是按"一个人一台机器跑起来"
精心收窄的（见 `pyproject.toml` 里的注释）。这里用**标准库的 HTML 分词器**
自己按白名单过滤 —— 逻辑很短、边界清楚、可单测，而且不引入新的供应链。

## 安全立场：白名单 + 失败关闭

* 标签、属性、属性值都在**白名单**里，不在的一律丢掉；
* 危险标签（script/style/iframe/object/embed/form 及 svg/math 这类可承载脚本的）
  连同**内容一起**丢弃，而不是只剥标签 —— 只剥标签会把脚本正文变成可见文本；
* 无法解析的输入宁可返回**空串**，也不返回原文（失败关闭）；
* `href`/`src` 只允许 http/https/mailto 和站内相对路径，`javascript:`/`data:` 一律拒。
  （`data:` 只对图片放行，且只放行图片 MIME —— 富文本里贴图是常态。）
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 允许保留的标签。范围就是"富文本编辑器会产出的那些"。
_ALLOWED_TAGS = frozenset(
    {
        "p", "br", "hr", "div", "span",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "strong", "b", "em", "i", "u", "s", "del", "ins", "mark", "sub", "sup", "code", "pre",
        "ul", "ol", "li",
        "blockquote",
        "a", "img",
        "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    }
)

# 这些标签连同**内容**一起丢掉。只剥标签不够：`<script>alert(1)</script>`
# 剥完标签会把 `alert(1)` 变成页面上的可见文本。
_DROP_WITH_CONTENT = frozenset(
    {"script", "style", "iframe", "object", "embed", "form", "input", "button",
     "textarea", "select", "option", "svg", "math", "template", "noscript",
     "link", "meta", "base", "title", "head"}
)

# 空元素（没有闭合标签），序列化时不补 `</x>`。
_VOID_TAGS = frozenset({"br", "hr", "img", "col"})

# 每个标签允许的属性。`*` 是全局通用的一条（对齐/样式等）。
_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "*": frozenset({"style", "class", "title", "dir", "lang"}),
    "a": frozenset({"href", "target", "rel"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "colwidth"}),
    "th": frozenset({"colspan", "rowspan", "colwidth", "scope"}),
    "col": frozenset({"span", "width"}),
    "ol": frozenset({"start", "type"}),
    "table": frozenset({"border", "cellpadding", "cellspacing"}),
}

# style 里允许的属性（富文本要的颜色/字号/对齐就在这几个里）。
_ALLOWED_STYLE_PROPS = frozenset(
    {
        "color", "background-color", "background",
        "font-size", "font-family", "font-weight", "font-style",
        "text-align", "text-decoration", "text-indent",
        "line-height", "letter-spacing",
        "margin", "margin-left", "margin-right", "margin-top", "margin-bottom",
        "padding", "padding-left", "padding-right", "padding-top", "padding-bottom",
        "border", "border-collapse", "border-color", "border-width", "border-style",
        "width", "height", "min-width", "max-width", "vertical-align", "white-space",
        "list-style-type", "display",
    }
)

_SAFE_URL = re.compile(r"^(?:https?://|mailto:|/|#|\.{1,2}/)", re.I)
_SAFE_IMG_DATA = re.compile(r"^data:image/(?:png|jpe?g|gif|webp|bmp);base64,[A-Za-z0-9+/=\s]+$", re.I)
# 图片允许"看起来像路径"的值：相对文件名（`a.png`）、站内路径、data:image。
# 为什么单独给 img 放宽：富文本里贴图是常态，而编辑器/用户经常给出不带前导 `/`
# 的路径；上面那条 _SAFE_URL 会把这些当危险值丢掉，结果是"图片莫名其妙不见了"。
_IMG_PATHISH = re.compile(r"^[\w./\-%]+\.(?:png|jpe?g|gif|webp|bmp|svg)$", re.I)


def _url_ok(tag: str, attr: str, value: str) -> bool:
    """`href`/`src` 的目标安全吗。

    只认 http/https/mailto/站内相对路径 —— `javascript:` 是经典的 XSS 载体。
    `data:` 只对 `<img src>` 放行（用户粘图是常态），且必须是图片 MIME：
    `data:text/html` 配上导航就可能变成脚本执行。
    """
    v = (value or "").strip()
    if not v:
        return False
    if tag == "img" and attr == "src" and _SAFE_IMG_DATA.match(v):
        return True
    if tag == "img" and attr == "src" and _IMG_PATHISH.match(v):
        return True
    if _SAFE_URL.match(v):
        # 相对路径里也可能藏 `javascript:`，但上面那条已经把它排除在开头之外；
        # 再挡一次大小写变体与空白绕过。
        return not re.match(r"^\s*(?:javascript|vbscript|data)\s*:", v, re.I)
    return False


def _clean_style(value: str) -> str:
    """只保留白名单里的 CSS 属性；顺手挡掉 `expression(`/`url(` 这类历史漏洞。"""
    out: list[str] = []
    for decl in (value or "").split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip().lower()
        val = val.strip()
        if prop not in _ALLOWED_STYLE_PROPS or not val:
            continue
        low = val.lower()
        if "expression(" in low or "javascript:" in low:
            continue
        # `url()` 在 style 里可以外带请求；富文本用不到，直接丢值。
        if "url(" in low:
            continue
        out.append(f"{prop}: {val}")
    return "; ".join(out)


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_depth = 0  # >0 表示正处在"连内容一起丢"的标签里
        # 还没闭合的标签栈。收尾时补上闭合标签 —— 存进库里的 HTML 应当是**良构**的：
        # 不然每次读出来回填进编辑器，解析器都要替我们猜一次嵌套关系，
        # 而"猜"在富文本里会让整段格式跑掉（微软 Word 粘出来的 HTML 就常年缺闭合）。
        self._open: list[str] = []

    # -- 工具：把属性按白名单过一遍 --
    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = _ALLOWED_ATTRS.get(tag, frozenset()) | _ALLOWED_ATTRS["*"]
        kept: list[str] = []
        for name, value in attrs:
            n = (name or "").lower()
            v = value if value is not None else ""
            if n.startswith("on"):  # 事件处理器一律丢（onerror/onclick…）
                continue
            if n not in allowed:
                continue
            if n in ("href", "src"):
                if not _url_ok(tag, n, v):
                    continue
                kept.append(f'{n}="{_escape_attr(v)}"')
                continue
            if n == "style":
                s = _clean_style(v)
                if s:
                    kept.append(f'style="{_escape_attr(s)}"')
                continue
            if n in ("target",):
                # 外链带 target=_blank 时必须带 rel，否则是 tabnabbing
                kept.append(f'{n}="{_escape_attr(v)}"')
                continue
            kept.append(f'{n}="{_escape_attr(v)}"')
        if tag == "a" and any(k.startswith("target=") for k in kept) and not any(
            k.startswith("rel=") for k in kept
        ):
            kept.append('rel="noopener noreferrer"')
        return (" " + " ".join(kept)) if kept else ""

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in _DROP_WITH_CONTENT:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if t not in _ALLOWED_TAGS:
            return  # 未知标签：剥掉标签、保留内容
        self.parts.append(f"<{t}{self._attrs(t, attrs)}>")
        if t not in _VOID_TAGS:
            self._open.append(t)

    def handle_startendtag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in _DROP_WITH_CONTENT or self._drop_depth:
            return
        if t not in _ALLOWED_TAGS:
            return
        self.parts.append(f"<{t}{self._attrs(t, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _DROP_WITH_CONTENT:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return
        if t in _ALLOWED_TAGS and t not in _VOID_TAGS:
            self.parts.append(f"</{t}>")
            # 弹出到最近一个同名标签（含它）。找不到就不动栈 ——
            # 一个多余的 `</div>` 不该把栈搅乱，否则收尾会补出一堆错的闭合标签。
            if t in self._open:
                while self._open:
                    if self._open.pop() == t:
                        break

    def close_open_tags(self) -> None:
        """把没闭合的标签按**逆序**补上闭合。"""
        while self._open:
            self.parts.append(f"</{self._open.pop()}>")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        self.parts.append(_escape_text(data))

    def handle_entityref(self, name: str) -> None:  # pragma: no cover - convert_charrefs=True
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:  # pragma: no cover
        self.parts.append(f"&#{name};")


def _escape_text(s: str) -> str:
    """文本内容转义。

    ★ **不转义 `>`**（2026-10-12）。理由：
      * HTML 规范里 `>` 在文本内容中是**合法**的，只有 `<`（开标签）和 `&`（实体）
        必须转义；
      * 而转义它会把图表源码弄坏 —— `flowchart TD\\n A --> B` 变成 `A --&gt; B`，
        mermaid 就再也认不出这条连线了。用户在流程图上看到的是"渲染失败"。

    实测：`<pre class="tp-mermaid">` 里的箭头被转义后，整张图渲染成一串错误文字。
    属性值仍然转义 `>`（`_escape_attr` 里做）—— 那里它是引号边界的潜在干扰。
    """
    return s.replace("&", "&amp;").replace("<", "&lt;")


def _escape_attr(s: str) -> str:
    return _escape_text(s).replace('"', "&quot;").replace(">", "&gt;")


def sanitize(html: str, *, max_chars: int = 400_000) -> str:
    """净化一段富文本 HTML。**失败关闭**：解析不了就返回空串。

    `max_chars` 是输入上限：一份文档正文不该比整份需规还长，
    而"存一个 500MB 的字符串"本身也是一种资源耗尽的攻击面。
    """
    if not html or not html.strip():
        return ""
    src = html[:max_chars]
    p = _Cleaner()
    try:
        p.feed(src)
        p.close()
        p.close_open_tags()
    except Exception:  # noqa: BLE001 — 解析异常时宁可丢内容，也不放原文过去
        return ""
    return "".join(p.parts).strip()


def html_to_text(html: str) -> str:
    """富文本 → 纯文本（给搜索/预览用）。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()
