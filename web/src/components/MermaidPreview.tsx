import { AlertTriangle, GitBranch } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

/** Mermaid 图表预览（2026-10-12，用户要求「ai 给我画流程图」）。
 *
 *  ## 为什么图和编辑器分成两块，而不是把图塞进编辑器里
 *
 *  用户的诉求是两个：**要看到图**，并且**能像文档一样编辑**。这两件事有冲突 ——
 *  编辑器（TipTap）里放不进一个可交互的 SVG，而图片又改不动。
 *
 *  解法是把源码当正文、把图当预览：
 *    * 正文里存的是 `<pre class="tp-mermaid">flowchart TD …</pre>`，
 *      它是一段**普通文本**，用户在编辑器里能直接改；
 *    * 这块面板扫出所有 `tp-mermaid` 文本块，渲染成图。
 *  改源码 → 图跟着变；图存的是文本，所以永远改得动。
 *
 *  ## mermaid 为什么动态 import
 *
 *  它很大（解包 122MB，gzip 后仍有数百 KB），而只有含图表的文档才需要它。
 *  静态 import 会让**每个**进入项目文档页的人都下载它。动态 import 之后，
 *  没有图的文档一分钱不花。
 */
export function MermaidPreview({ html }: { html: string }) {
  const { t } = useTranslation();
  const [rendered, setRendered] = useState<Record<number, string>>({});
  const [errors, setErrors] = useState<Record<number, string>>({});
  /** 点开的大图源码（null = 没开）。存 SVG 而不是下标：
   *  源码变了图会重画，存下标可能指向已经不存在的那张。 */
  const [zoom, setZoom] = useState<string | null>(null);
  const seq = useRef(0);

  // 从正文里扫出所有图表源码。
  //
  // ★ 不能只认 `pre.tp-mermaid`（2026-10-12 实测）：TipTap 会把 `<pre>` 收进它的
  // codeBlock 节点，而 codeBlock **不带 class** —— 于是用户一编辑一保存，
  // 这个 class 就没了，图再也认不出来。所以还要认"内容看起来就是 mermaid"的代码块。
  //
  // 判据是首行的图类型关键字：mermaid 的图**必须**以它开头，所以这个判据很稳，
  // 不会把普通代码块误认成图。
  const sources = useMemo(() => {
    if (!html || typeof DOMParser === "undefined") return [] as string[];
    try {
      const doc = new DOMParser().parseFromString(html, "text/html");
      const out: string[] = [];
      const seen = new Set<string>();
      const push = (raw: string) => {
        const t = (raw || "").trim();
        if (t && !seen.has(t)) {
          seen.add(t);
          out.push(t);
        }
      };
      for (const el of Array.from(doc.querySelectorAll("pre.tp-mermaid"))) {
        push(el.textContent || "");
      }
      // 退路：任何代码块，只要首行是 mermaid 的图类型声明，就当图表源码
      const head = /^\s*(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|journey)\b/;
      for (const el of Array.from(doc.querySelectorAll("pre"))) {
        const t = (el.textContent || "").trim();
        if (head.test(t)) push(t);
      }
      return out;
    } catch {
      return [] as string[];
    }
  }, [html]);

  useEffect(() => {
    if (!sources.length) {
      setRendered({});
      setErrors({});
      return;
    }
    let alive = true;
    (async () => {
      const mod = await import("mermaid");
      const mermaid = mod.default;
      // 配色跟随系统主题，否则浅色主题下会出现白底黑字的图块。
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict", // 不接受图表里的 HTML/链接 —— 正文本来就是用户内容
        theme: "base",
        fontFamily: "inherit",
        themeVariables: {
          primaryColor: "#eef2ff",
          primaryTextColor: "#1f2937",
          primaryBorderColor: "#6366f1",
          lineColor: "#94a3b8",
          fontSize: "13px",
        },
      });
      const out: Record<number, string> = {};
      const bad: Record<number, string> = {};
      for (let i = 0; i < sources.length; i++) {
        const id = `tp-mmd-${++seq.current}`;
        try {
          const { svg } = await mermaid.render(id, sources[i]);
          out[i] = svg;
        } catch (e) {
          // 渲染失败要**如实显示原因**，不要静默吞掉：
          // 用户看到的应该是一句"这张图渲染不了"加原因，而不是一片空白。
          bad[i] = e instanceof Error ? e.message : String(e);
        }
      }
      if (!alive) return;
      setRendered(out);
      setErrors(bad);
    })().catch((e) => {
      if (alive) setErrors({ 0: e instanceof Error ? e.message : String(e) });
    });
    return () => {
      alive = false;
    };
  }, [sources]);

  if (!sources.length) return null;

  return (
    <>
      <div className="space-y-2">
      <div className="flex items-center gap-1.5 text-[12px] text-ink-500">
        <GitBranch className="h-3.5 w-3.5" />
        {t("流程图预览（下面的图由正文里的 mermaid 源码渲染，改源码图就变）")}
      </div>
      {sources.map((_, i) =>
        errors[i] ? (
          <div
            key={i}
            className="tp-alert tp-alert-warn flex items-start gap-2 px-3 py-2 text-[12px]"
          >
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <div className="min-w-0">
              <p className="font-medium">{t("这张图渲染不了")}</p>
              <p className="mt-0.5 break-all text-ink-500">{errors[i]}</p>
              <p className="mt-0.5 text-ink-400">
                {t("上面编辑器里的 mermaid 源码可能写错了，改好就会自动重画。")}
              </p>
            </div>
          </div>
        ) : rendered[i] ? (
          <div key={i}>
            {/* ★ 点一下放大（2026-10-12 用户要求「流程图我就想点开看」）。
                项目文档里的图受栏宽约束，节点多的纵向链会被压小、字看不真。
                点开用占满屏幕的浮层显示**原始尺寸**的图。
                整块可点 —— 用户的直觉是"点图看图"，不是"找一个小按钮"。 */}
            <div
              className="tp-mermaid-box group relative overflow-x-auto rounded-lg border border-[var(--line)] bg-[var(--panel)] p-3 transition-colors hover:border-brand-300"
              onClick={() => setZoom(rendered[i])}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setZoom(rendered[i]);
                }
              }}
              role="button"
              tabIndex={0}
              title={t("点开查看大图")}
              // mermaid 输出的 SVG 由库自己生成；源码里的 HTML 已被
              // securityLevel:"strict" 拦下，正文本身也在入库时净化过。
              dangerouslySetInnerHTML={{ __html: rendered[i] }}
            />
            <p className="mt-1 text-[10.5px] text-ink-400 group-hover:text-ink-500">
              {t("点图可放大查看")}
            </p>
          </div>
        ) : (
          <div
            key={i}
            className="rounded-lg border border-[var(--line)] px-3 py-6 text-center text-[12px] text-ink-400"
          >
            {t("正在画图…")}
          </div>
        ),
      )}
      </div>

      {/* 放大浮层：把图按**原始尺寸**放出来 + 允许滚动。
          缩小到屏幕内等于把"看不清"的问题重复一遍，所以这里不限高。 */}
      {zoom && (
      <div
        // ★ `inset-0` 只固定了四个边，但**内容更高时 flex 容器会被撑高**：
        //   实测弹层高度变成了 1735px（视口只有 900），于是 body 掉到屏幕外，
        //   图的下半部分看不见、背后页面内容还从半透明底里透出来，和标题糊在一起。
        //   `max-h-[100dvh]` 把它钉在视口内，`overflow-hidden` 保证不溢出。
        className="fixed inset-0 z-50 flex max-h-[100dvh] flex-col gap-2 overflow-hidden bg-black/90 p-3 sm:p-6"
        onClick={() => setZoom(null)}
        role="dialog"
        aria-modal="true"
      >
        {/* ★ 文字不能用 text-white/90（2026-10-12 修）：浮层背景是 black/60 + 模糊，
            而图自己有**浅色底**，白字压在浅色底上直接看不见（实测截图里那行是透明的）。
            给这一行自己一个深色胶囊底，无论图多亮都读得清。 */}
        <div className="flex shrink-0 items-center gap-2">
          <span className="inline-flex items-center gap-2 rounded-lg bg-black/75 px-3 py-1.5 text-[12px] text-white shadow-lg">
            <GitBranch className="h-3.5 w-3.5" />
            {t("点任意处关闭 · 内容可滚动")}
          </span>
          <button
            className="ml-auto rounded-lg bg-black/75 px-3 py-1.5 text-[12px] text-white shadow-lg hover:bg-black/90"
            onClick={(e) => {
              e.stopPropagation();
              setZoom(null);
            }}
          >
            {t("关闭")}
          </button>
        </div>
        {/* flex-1 + min-h-0：占满剩余高度，图在里面滚动。
            自身不缩放 —— 缩小到屏幕内等于把"看不清"重复一遍。 */}
        <div
          className="tp-mermaid-zoom min-h-0 flex-1 overflow-auto rounded-xl bg-[var(--panel)] p-4"
          onClick={(e) => e.stopPropagation()}
          dangerouslySetInnerHTML={{ __html: zoom }}
        />
      </div>
      )}
    </>
  );
}
