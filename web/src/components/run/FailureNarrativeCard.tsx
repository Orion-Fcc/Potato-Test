import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { FailureNarrative } from "../../lib/api";
import { Button } from "../ui";

/**
 * The AI-written bug description for a failed case: 操作步骤 / 实际结果 / 预期结果.
 *
 * Shown FIRST in the replay modal, above the step timeline, because it is the part a
 * tester actually reads and pastes into a bug tracker — the timeline is supporting
 * evidence for when the description looks wrong.
 *
 * The copy button emits the exact bracket format the team already uses, so pasting it
 * into an issue needs no reformatting.
 */
export function FailureNarrativeCard({
  narrative,
  caseName,
}: {
  narrative: FailureNarrative;
  caseName?: string;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const asText = [
    `【用例】：${caseName ?? ""}`,
    `【操作步骤】：${narrative.steps ?? ""}`,
    `【实际结果】：${narrative.actual ?? ""}`,
    `【预期结果】：${narrative.expected ?? ""}`,
  ].join("\n");

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(asText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard is unavailable over plain http on some browsers; the text is on screen
      // and selectable, so this is not worth an error dialog.
      setCopied(false);
    }
  };

  const rows: { label: string; value: string }[] = [
    { label: t("Steps"), value: narrative.steps ?? "" },
    { label: t("Actual result"), value: narrative.actual ?? "" },
    { label: t("Expected result"), value: narrative.expected ?? "" },
  ];

  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-[var(--bad-fg)]/30 bg-[var(--bad-bg)]/40">
      <div className="flex items-center justify-between gap-2 border-b border-[var(--bad-fg)]/20 bg-[var(--bad-bg)]/60 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-xs font-semibold text-[var(--bad-fg)]">{t("Bug description")}</span>
          {narrative.severity && (
            <span className="rounded-full bg-[var(--bad-fg)]/15 px-2 py-0.5 text-[11px] font-medium text-[var(--bad-fg)]">
              {narrative.severity}
            </span>
          )}
        </div>
        <Button size="sm" variant="ghost" onClick={copy}>
          {copied ? t("Copied") : t("Copy")}
        </Button>
      </div>

      {narrative.title && (
        <div className="px-3 pt-2.5 text-[13px] font-medium text-ink-900">{narrative.title}</div>
      )}

      <div className="px-3 py-2.5">
        {rows.map((r) => (
          <div key={r.label} className="mt-1.5 first:mt-0">
            <span className="mr-1 text-xs font-semibold text-ink-700">【{r.label}】</span>
            <span className="text-[13px] leading-relaxed text-ink-800">
              {r.value || "—"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
