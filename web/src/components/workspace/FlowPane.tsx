import { GitBranch } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { MermaidBlock } from "../MermaidBlock";
import { Card } from "../ui";

/** 流程图工作区：上方写 Mermaid 源码，下方实时预览。
 *
 *  预览用 500ms 防抖：每敲一个字符就重新布局整张图会明显卡顿。
 *  语法错误由 MermaidBlock 内部的中文错误卡兜住，不会白屏。
 */
export function FlowPane({
  source,
  onChange,
}: {
  source: string;
  onChange: (source: string) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(source);
  const [preview, setPreview] = useState(source);

  // AI 应用建议 / 切换文档时，外部源码变了要同步进输入框。
  useEffect(() => {
    setDraft((prev) => (prev === source ? prev : source));
  }, [source]);

  useEffect(() => {
    const timer = setTimeout(() => setPreview(draft), 500);
    return () => clearTimeout(timer);
  }, [draft]);

  return (
    <div className="space-y-3">
      <Card className="overflow-hidden">
        <div className="flex items-center gap-2 border-b border-[var(--line)] px-3 py-2 text-[12px] font-medium text-ink-700">
          <GitBranch className="h-3.5 w-3.5 text-ink-400" />
          {t("Mermaid 源码")}
          <span className="ml-auto text-[11px] font-normal text-ink-400">
            {t("改完自动预览，记得点右上角保存")}
          </span>
        </div>
        <textarea
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            onChange(e.target.value);
          }}
          spellCheck={false}
          placeholder={t("flowchart TD\n  A[\"开始\"] --> B[\"结束\"]")}
          className="min-h-[260px] w-full resize-y bg-[var(--panel)] px-3 py-2.5 font-mono text-[12.5px] leading-relaxed text-ink-800 outline-none"
        />
      </Card>

      <Card className="p-3">
        <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-ink-800">
          <GitBranch className="h-4 w-4 text-ink-400" />
          {t("流程图预览")}
          <span className="ml-auto text-[11px] font-normal text-ink-400">{t("500ms 防抖")}</span>
        </div>
        {preview.trim() ? (
          <MermaidBlock source={preview} />
        ) : (
          <p className="px-1 py-3 text-[12px] text-ink-400">
            {t("还没有流程图。点右上角「AI 流式生成」，或直接在上面的源码框里写。")}
          </p>
        )}
      </Card>
    </div>
  );
}
