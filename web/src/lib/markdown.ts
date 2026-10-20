import DOMPurify from "dompurify";
import { marked } from "marked";

/** Markdown -> 可安全写入 DOM 的 HTML。
 *
 *  侧栏流式气泡和「插入到编辑器」共用这条路径，所以必须做两层：
 *  1. marked 解析 GFM（标题、列表、表格、代码块、链接、粗斜体）；
 *  2. DOMPurify 清洗，模型输出里的 script / on* / javascript: 一律丢弃。
 *
 *  流式过程中 Markdown 可能只到一半（例如代码围栏还没闭合），marked 会按
 *  普通段落继续输出，下一帧补齐后自动恢复正常排版。
 */
export function markdownToHtml(raw: string): string {
  const text = (raw || "").replace(/\r\n/g, "\n");
  if (!text.trim()) return "";

  const parsed = marked.parse(text, {
    async: false,
    gfm: true,
    breaks: true,
  });
  const clean = DOMPurify.sanitize(String(parsed), {
    USE_PROFILES: { html: true },
    ADD_ATTR: ["target", "rel"],
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed", "link", "meta"],
    FORBID_ATTR: ["style"],
  });

  if (typeof document === "undefined") return clean;
  const tpl = document.createElement("template");
  tpl.innerHTML = clean;

  // AI 常把流程图写成 ```mermaid。插入编辑器时转成 Mermaid 节点，
  // 否则会落成一个普通代码块，用户还得自己再转一次。
  tpl.content.querySelectorAll("pre > code").forEach((code) => {
    const lang = code.getAttribute("class") || "";
    if (!/(?:language|lang)-mermaid/.test(lang)) return;
    const pre = code.parentElement;
    if (!pre) return;
    pre.className = "tp-mermaid";
    pre.textContent = code.textContent || "";
  });

  tpl.content.querySelectorAll("a[href]").forEach((a) => {
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer");
  });
  return tpl.innerHTML;
}

/** 兼容旧调用点：以前手写 Markdown 转换，现在统一走 marked + DOMPurify。 */
export function replyToHtml(raw: string): string {
  return markdownToHtml(raw);
}
