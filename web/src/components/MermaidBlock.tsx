import { AlertTriangle, Maximize2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

/** 渲染一段 Mermaid 源码为图（2026-10-12）。
 *
 *  ## 为什么从 MermaidPreview 里抽出来
 *
 *  那个组件是"扫文档 HTML 里的 `<pre class="tp-mermaid">`"，
 *  而用例优化提案里的流程图是**主题内的一段源码**（不在 HTML 里、也没有 pre 容器）。
 *  两者的渲染部分一样，抽出来共用就不会出现"文档里的图能画、提案里的图报错"
 *  这种不一致 —— 而 mermaid 的初始化配置（主题、securityLevel）必须一致，
 *  否则同一份源码在两处表现不同。
 */
export function MermaidBlock({
  source,
  className = "",
  cacheKey,
}: {
  source: string;
  className?: string;
  /** 同一页有多个图时用它做缓存键，避免重复渲染。默认用源码本身。 */
  cacheKey?: string;
}) {
  const { t } = useTranslation();
  const [svg, setSvg] = useState("");
  const [err, setErr] = useState("");
  // 点开大图的浮层
  const [zoom, setZoom] = useState(false);
  const seq = useRef(0);
  // 模块级缓存：同一份源码在页面上出现多次（同一模块的多条提案共享一张图）
  // 只渲染一次。提案列表动辄几十条，不缓存会把同一张图渲染几十遍。
  const key = cacheKey ?? source;

  useEffect(() => {
    if (!source.trim()) {
      setSvg("");
      setErr("");
      return;
    }
    const hit = _CACHE.get(key);
    if (hit) {
      setSvg(hit);
      setErr("");
      return;
    }
    let alive = true;
    (async () => {
      try {
        const mod = await import("mermaid");
        const mermaid = mod.default;
        mermaid.initialize({
          startOnLoad: false,
          // strict：不接受图表里的 HTML/链接 —— 源码来自模型，不该被当代码执行
          securityLevel: "strict",
          theme: "base",
          fontFamily: "inherit",
          themeVariables: {
            primaryColor: "#eef2ff",
            primaryTextColor: "#1f2937",
            primaryBorderColor: "#6366f1",
            lineColor: "#94a3b8",
            fontSize: "12px",
          },
        });
        const { svg: out } = await mermaid.render(`tp-mb-${++seq.current}`, source);
        if (!alive) return;
        _CACHE.set(key, out);
        setSvg(out);
        setErr("");
      } catch (e) {
        // 渲染失败**如实显示原因**：用户看到的应该是一句"这张图渲染不了"，
        // 而不是一片空白 —— 空白会让人以为功能坏了。
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [source, key]);

  if (!source.trim()) return null;
  if (err) {
    return (
      <div className="tp-alert tp-alert-warn flex items-start gap-2 px-2.5 py-2 text-[11px]">
        <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
        <div className="min-w-0">
          <p className="font-medium">{t("这张图渲染不了")}</p>
          <p className="mt-0.5 break-all text-ink-500">{err.slice(0, 200)}</p>
        </div>
      </div>
    );
  }
  if (!svg) {
    return <div className="px-2 py-4 text-center text-[11px] text-ink-400">{t("正在画图…")}</div>;
  }
  return (
    <>
      {/* ★ 点一下放大（2026-10-12 用户要求「流程图我就想点开看」）。
          为什么必须能点开：图在页面里受宽度限制，节点多的纵向链会被压小，
          字就看不真。点开用一个占满屏幕的浮层显示**原始尺寸**的图，
          可以滚动、也可再次点击关闭。
          整块都可点（不只是一个小按钮）—— 用户的直觉是"点图看图"。 */}
      <div
        className={
          "tp-mermaid-box group relative overflow-x-auto rounded-lg border border-[var(--line)] " +
          "bg-[var(--panel)] p-2 transition-colors hover:border-brand-300 " +
          className
        }
        onClick={() => setZoom(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setZoom(true);
          }
        }}
        role="button"
        tabIndex={0}
        title={t("点开查看大图")}
        // mermaid 自己生成的 SVG；源码里的 HTML 已被 securityLevel:"strict" 拦下
        dangerouslySetInnerHTML={{ __html: svg }}
      />
      {/* 提示"可以点开"—— 不给提示的话用户不知道这图是可点的 */}
      <p className="mt-1 text-[10.5px] text-ink-400 group-hover:text-ink-500">
        {t("点图可放大查看")}
      </p>

      {zoom && (
        <div
          // ★ `inset-0` 只固定四个边，内容更高时 flex 容器会被撑高
          //   （实测 1735px，视口只有 900）→ body 掉到屏幕外、背后内容透出来。
          //   max-h-[100dvh] + overflow-hidden 把它钉在视口内。
          className="fixed inset-0 z-50 flex max-h-[100dvh] flex-col gap-2 overflow-hidden bg-black/90 p-3 sm:p-6"
          onClick={() => setZoom(false)}
          role="dialog"
          aria-modal="true"
        >
          {/* 文字要自带深色底：浮层里的图有浅色背景，纯白字压上去看不见 */}
          <div className="flex shrink-0 items-center gap-2">
            <span className="inline-flex items-center gap-2 rounded-lg bg-black/75 px-3 py-1.5 text-[12px] text-white shadow-lg">
              <Maximize2 className="h-3.5 w-3.5" />
              {t("点任意处关闭 · 内容可滚动")}
            </span>
            <button
              className="ml-auto rounded-lg bg-black/75 px-3 py-1.5 text-[12px] text-white shadow-lg hover:bg-black/90"
              onClick={(e) => {
                e.stopPropagation();
                setZoom(false);
              }}
            >
              {t("关闭")}
            </button>
          </div>
          {/* ★ 这一层**不做缩放**：把 SVG 按原始尺寸放出来 + 允许滚动。
              缩小到屏幕内等于把刚才那个问题重复一遍。 */}
          <div
            className="tp-mermaid-zoom min-h-0 flex-1 overflow-auto rounded-xl bg-[var(--panel)] p-4"
            onClick={(e) => e.stopPropagation()}
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        </div>
      )}
    </>
  );
}

const _CACHE = new Map<string, string>();
