/** 从文档 HTML 里取出**最新一张** Mermaid 流程图源码（2026-10-20）。
 *
 *  为什么需要单独抽这一层：
 *  - 流程图文档的正文是 `<pre class="tp-mermaid">源码</pre>`，不是 JSON；
 *  - 旧文档可能没有 class，只有一段以 `flowchart TD` 开头的 `<pre>`；
 *  - 追加生成会在同一份文档里留下多张图，编辑器里“看得见”的应当是最后一张。
 *
 *  不去重会出两个问题：重复渲染同一张图（浪费）、同一份坏源码出现两个报错框。
 *  所以这里按**源码内容**去重，只返回最后一张非空图。
 */
const MERMAID_HEAD =
  /^\s*(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|gantt|pie|journey|mindmap|timeline)\b/i;

export function latestMermaidSource(html: string): string {
  const raw = (html || "").trim();
  if (!raw) return "";
  // DOMParser 只在浏览器里有；SSR / 单测环境退化成空字符串，调用方当作“没有图”。
  if (typeof DOMParser === "undefined") return "";

  const doc = new DOMParser().parseFromString(raw, "text/html");
  const seen = new Set<string>();
  let latest = "";
  for (const node of Array.from(doc.querySelectorAll("pre"))) {
    // textContent 会把实体（&lt; &gt; &amp;）还原成真实字符，正是 mermaid 需要的。
    const src = (node.textContent || "").replace(/\r\n/g, "\n").trim();
    if (!src || seen.has(src)) continue;
    const marked = node.classList.contains("tp-mermaid");
    // 没带 class 的 <pre> 只有当首行像 Mermaid 声明时才认，避免把普通代码块当图。
    if (!marked && !MERMAID_HEAD.test(src)) continue;
    seen.add(src);
    latest = src;
  }
  return latest;
}

/** 把 Mermaid 源码包成文档正文里的标准块（AI 建议应用时用）。 */
export function mermaidHtml(source: string): string {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return `<pre class="tp-mermaid">${esc((source || "").trim())}</pre>`;
}
