import { Maximize2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { MermaidErrorCard, dropMermaidRenderNode } from "./MermaidErrorCard";

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
  // import/初始化失败与源码语法错误要分开说：前者刷新可救，后者只能改源码。
  const [loadFailed, setLoadFailed] = useState(false);
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
      setLoadFailed(false);
      return;
    }
    const hit = _CACHE.get(key);
    if (hit) {
      setSvg(hit);
      setErr("");
      setLoadFailed(false);
      return;
    }
    let alive = true;
    (async () => {
      const id = `tp-mb-${++seq.current}`;
      let modLoaded = false;
      try {
        const mod = await import("mermaid");
        modLoaded = true;
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
        const { svg: out } = await mermaid.render(id, source);
        if (!alive) return;
        _CACHE.set(key, out);
        setSvg(out);
        setErr("");
        setLoadFailed(false);
      } catch (e) {
        // 失败不能白屏：清掉可能残留的临时节点，并记录原因。
        // 原始报错只在"详细报错"折叠面板里展示，默认给中文可操作提示。
        dropMermaidRenderNode(id);
        if (alive) {
          setSvg("");
          setErr(e instanceof Error ? e.message : String(e));
          setLoadFailed(!modLoaded);
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [source, key]);

  if (!source.trim()) return null;
  if (err) {
    return <MermaidErrorCard detail={err} loadFailed={loadFailed} />;
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
