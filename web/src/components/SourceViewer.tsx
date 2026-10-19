import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download, ExternalLink, FileWarning } from "lucide-react";

/**
 * 原件查看器（2026-10-19）。
 *
 * ## 为什么需要它
 * 原来「原件」是个 `target="_blank"` 的链接。桌面版的窗口是 WebView2 —— **点它不开新窗口**，
 * 于是用户永远只看到解析出来的正文（有损：PDF 丢版面、docx 丢标题层级、长文档还会被截断），
 * 反馈就是「上传的文件依旧看不到原格式」。所以把原件**渲染在页面里**。
 *
 * ## 按类型分流（全用浏览器原生能力，不引第三方库）
 *   pdf            → `<iframe>`，Edge / WebView2 自带 PDF 阅读器
 *   png/jpg/webp/gif → `<img>`
 *   txt/md/csv/json… → fetch 回来 <pre>（等宽 + 可滚动）
 *   其它（docx / xlsx / pptx）→ 浏览器渲染不了，给「下载原件」并说明原因
 *
 * 每种类型都带一条「在新窗口打开 / 下载」的兜底 —— 内嵌渲染失败时用户还有路可走，
 * 不会像以前那样点了没反应。
 */
type DocLike = {
  source_url?: string | null;
  source_name?: string | null;
  source_bytes?: number;
};

const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "webp", "gif", "bmp", "svg"]);
const TEXT_EXT = new Set(["txt", "md", "markdown", "csv", "json", "log", "xml", "yaml", "yml"]);

function extOf(name?: string | null): string {
  const n = (name || "").trim().toLowerCase();
  const i = n.lastIndexOf(".");
  return i > 0 ? n.slice(i + 1) : "";
}

export function SourceViewer({ doc }: { doc: DocLike }) {
  const { t } = useTranslation();
  const url = doc.source_url || "";
  const ext = extOf(doc.source_name);
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const isText = TEXT_EXT.has(ext);
  useEffect(() => {
    if (!url || !isText) return;
    let alive = true;
    setText(null);
    setFailed(false);
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((body) => alive && setText(body))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [url, isText]);

  if (!url) return null;

  return (
    <div className="flex min-h-0 flex-col gap-2">
      {/* 兜底条：内嵌渲染不成功时，这两个入口永远可用 */}
      <div className="flex flex-wrap items-center gap-3 text-[12px] text-ink-500">
        <span>
          {t("原格式")}: <span className="font-mono">{doc.source_name || ext || "—"}</span>
        </span>
        <a
          className="inline-flex items-center gap-1 text-brand-700 hover:underline"
          href={url}
          target="_blank"
          rel="noreferrer"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          {t("在新窗口打开")}
        </a>
        <a
          className="inline-flex items-center gap-1 text-brand-700 hover:underline"
          href={url}
          download={doc.source_name || undefined}
        >
          <Download className="h-3.5 w-3.5" />
          {t("下载原件")}
        </a>
      </div>

      {ext === "pdf" && (
        <iframe
          key={url}
          src={url}
          title={doc.source_name || "pdf"}
          className="h-[70vh] w-full rounded-lg border border-[var(--line)] bg-white"
        />
      )}

      {IMAGE_EXT.has(ext) && (
        <div className="flex justify-center rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-3">
          <img src={url} alt={doc.source_name || ""} className="max-h-[70vh] max-w-full object-contain" />
        </div>
      )}

      {isText && (
        <div className="h-[70vh] overflow-auto rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-3">
          {failed ? (
            <div className="flex items-center gap-2 text-[12px] text-ink-500">
              <FileWarning className="h-3.5 w-3.5" />
              {t("原件载入失败，请用上面的「在新窗口打开」或「下载原件」。")}
            </div>
          ) : text === null ? (
            <div className="text-[12px] text-ink-500">{t("正在载入原件…")}</div>
          ) : (
            <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-ink-700">
              {text}
            </pre>
          )}
        </div>
      )}

      {!isText && ext !== "pdf" && !IMAGE_EXT.has(ext) && (
        <div className="rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-4">
          <div className="flex items-center gap-2 text-[13px] text-ink-700">
            <FileWarning className="h-4 w-4" />
            {t("浏览器不能直接预览 {{ext}} 文件，请下载后用本机程序打开。", {
              ext: ext ? "." + ext : t("这种"),
            })}
          </div>
          <p className="mt-2 text-[12px] text-ink-500">
            {t("右边「正文」里是解析出来的文字（可以改），但它是有损的；要核对格式请以原件为准。")}
          </p>
        </div>
      )}
    </div>
  );
}
