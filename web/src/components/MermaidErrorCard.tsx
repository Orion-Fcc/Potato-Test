import { AlertTriangle } from "lucide-react";
import { useTranslation } from "react-i18next";

/** 流程图渲染失败的统一提示（2026-10-20）。
 *
 *  为什么不再直接显示 mermaid 的原始报错：
 *  `mermaid.render` 抛出的 message 是解析器内部输出（"Parse error on line 3…
 *  Expecting 'STADIUMEND'…"），对写文档的人没有指导意义，还自带英文红底观感。
 *  这里先把**能改的动作**说清楚，原始报错收进折叠面板，供排查时展开。
 */
export function MermaidErrorCard({
  detail,
  loadFailed = false,
}: {
  detail?: string;
  loadFailed?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="tp-alert tp-alert-warn flex items-start gap-2 px-3 py-2 text-[12px]">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-medium">
          {loadFailed
            ? t("流程图加载失败，请刷新页面后重试。")
            : t("⚠️ 流程图语法有误，请检查节点格式（如避免在节点内使用箭头或硬换行）")}
        </p>
        {!loadFailed && detail ? (
          <details className="mt-1">
            <summary className="cursor-pointer text-[11px] text-ink-500">
              {t("点击查看详细报错")}
            </summary>
            <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-black/5 p-2 font-mono text-[10.5px] text-ink-500">
              {detail}
            </pre>
          </details>
        ) : null}
      </div>
    </div>
  );
}

/** 删除 mermaid 渲染失败时可能留在 DOM 里的临时节点。
 *
 *  `mermaid.render` 在解析出错前会先插入 `#<id>`（或 `#d<id>`）的容器；
 *  不清理的话，有些错误会在页面角落留下半张图或错误图标。
 */
export function dropMermaidRenderNode(id: string) {
  for (const nodeId of [id, `d${id}`]) {
    document.getElementById(nodeId)?.remove();
  }
}
