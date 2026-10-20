import { CornerDownLeft, GitBranch, Replace, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "../ui";
import { MarkdownBody } from "./MarkdownBody";

/** AI 生成完成后的「建议卡片」。
 *
 *  不把模型输出直接写进文档：先让用户看清楚，再选择替换或插入。
 *  这就是 Human-in-the-loop 的落点 —— 模型只提议，用户拍板。
 */
export function SuggestionCard({
  content,
  kind,
  applied,
  onApply,
  onDiscard,
}: {
  content: string;
  kind: "text" | "flow";
  applied?: "replace" | "insert" | "discard";
  onApply: (mode: "replace" | "insert") => void;
  onDiscard: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="mt-2 rounded-lg border border-brand-200 bg-brand-50/40 p-2.5">
      <div className="mb-2 flex items-center gap-1.5 text-[11px] font-medium text-brand-700">
        {kind === "flow" ? <GitBranch className="h-3.5 w-3.5" /> : <Replace className="h-3.5 w-3.5" />}
        {kind === "flow" ? t("流程图建议") : t("建议内容")}
      </div>
      {kind === "flow" ? (
        <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md border border-[var(--line)] bg-[var(--panel)] px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-ink-800">
          {content}
        </pre>
      ) : (
        <div className="max-h-56 overflow-auto rounded-md border border-[var(--line)] bg-[var(--panel)] px-2.5 py-2 text-[12px] leading-relaxed text-ink-800">
          <MarkdownBody content={content} />
        </div>
      )}
      {applied ? (
        <p className="mt-2 text-[11px] text-ink-500">
          {applied === "discard"
            ? t("已忽略这条建议。")
            : applied === "insert"
              ? t("已插入到光标处，记得保存。")
              : t("已替换左侧文档，记得保存。")}
        </p>
      ) : (
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Button size="sm" onClick={() => onApply("replace")}>
            <Replace className="h-3.5 w-3.5" />
            {t("替换左侧文档")}
          </Button>
          {kind !== "flow" && (
            <Button size="sm" variant="outline" onClick={() => onApply("insert")}>
              <CornerDownLeft className="h-3.5 w-3.5" />
              {t("插入到光标处")}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={onDiscard}>
            <X className="h-3.5 w-3.5" />
            {t("忽略")}
          </Button>
        </div>
      )}
    </div>
  );
}
