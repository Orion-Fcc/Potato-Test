import { useState } from "react";
import { useTranslation } from "react-i18next";
import { MediaPlayer, MediaProvider } from "@vidstack/react";
import { DefaultVideoLayout, defaultLayoutIcons } from "@vidstack/react/player/layouts/default";
import type { RunResult } from "../../lib/api";
import { Badge, Button, Card } from "../ui";
import { Modal } from "../Modal";
import { ShotViewer } from "../ShotViewer";

/**
 * Replay modal for a single case result: recorded video, the agent's
 * thought → action → result steps, screenshots and the raw diagnostics JSON.
 * Split out of RunReport.tsx, which had grown past 900 lines.
 */
export function ReplayPanel({
  result,
  caseName,
  onClose,
}: {
  result: RunResult;
  caseName?: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [rawOpen, setRawOpen] = useState(false);
  const [shotIdx, setShotIdx] = useState<number | null>(null);
  const diag = result.diagnostics ?? [];
  const shots = diag.filter((d) => d.screenshot);

  return (
    <Modal onClose={onClose} className="max-w-3xl">
      {(close) => (
        <Card className="max-h-[85vh] overflow-auto p-5">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="font-medium text-ink-900">
                {caseName ?? t("Case #{{id}} replay", { id: result.case_id })}
              </span>
              <Badge status={result.status} />
              {result.flaky && <Badge status="flaky">flaky ×{result.attempts}</Badge>}
            </div>
            <Button size="sm" variant="ghost" onClick={close}>
              {t("Close")}
            </Button>
          </div>

          {result.video_url ? (
            <div className="overflow-hidden rounded-xl border border-[var(--line)] bg-black">
              <MediaPlayer
                className="mx-auto max-h-[300px] w-full"
                aspectRatio="16/9"
                title={caseName ?? `#${result.case_id}`}
                src={result.video_url}
              >
                <MediaProvider />
                <DefaultVideoLayout icons={defaultLayoutIcons} />
              </MediaPlayer>
            </div>
          ) : (
            <div className="rounded-xl bg-[var(--panel2)] px-3 py-6 text-center text-sm text-ink-500">
              {t("no video recorded")}
              {diag.length > 0 ? t(" — see the step timeline below") : ""}
            </div>
          )}

          <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
            <div>
              <span className="text-ink-500">{t("Judge")}: </span>
              {result.judge_reason ?? result.error ?? "—"}
            </div>
            <div>
              <span className="text-ink-500">{t("Final answer")}: </span>
              {result.final_answer || "—"}
            </div>
          </div>

          {diag.length > 0 ? (
            <>
              <div className="mb-2 mt-5 flex items-center gap-2 text-sm font-medium text-ink-900">
                {t("Step timeline")}
                <span className="rounded-full bg-[var(--panel2)] px-1.5 py-0.5 text-[11px] font-normal text-ink-500">
                  {diag.length}
                </span>
                <span className="text-xs font-normal text-ink-500">{t("thought → action → result")}</span>
              </div>
              <div>
                {diag.map((step) => (
                  <div
                    key={step.i}
                    className="grid grid-cols-[26px_1fr_auto] gap-3 tp-divide-row py-2.5"
                  >
                    <div
                      className={
                        "grid h-[22px] w-[22px] place-items-center rounded-full text-[11px] font-bold text-[var(--on-brand)] " +
                        (step.error ? "bg-[var(--bad-fg)]" : "bg-brand-600")
                      }
                    >
                      {step.i}
                    </div>
                    <div className="min-w-0">
                      <div className="mb-1">
                        <span className="rounded bg-brand-50 px-2 py-0.5 font-mono text-[11px] font-semibold text-brand-700">
                          {step.action}
                        </span>
                      </div>
                      {step.thought && (
                        <div className="text-[13px] leading-relaxed text-ink-700">
                          <span className="mr-1.5 text-ink-500">💭</span>
                          {step.thought}
                        </div>
                      )}
                      {step.result && <div className="mt-1 text-xs text-[var(--ok-fg)]">✓ {step.result}</div>}
                      {step.error && <div className="mt-1 text-xs text-[var(--bad-fg)]">✗ {step.error}</div>}
                    </div>
                    {step.screenshot ? (
                      <button
                        onClick={() => setShotIdx(shots.findIndex((s) => s.i === step.i))}
                        title={t("click to enlarge")}
                        className="block cursor-zoom-in"
                      >
                        <img
                          src={step.screenshot}
                          alt={`step ${step.i}`}
                          className="h-[70px] w-[120px] rounded-md border border-[var(--line)] object-cover"
                        />
                      </button>
                    ) : (
                      <div className="w-[120px]" />
                    )}
                  </div>
                ))}
              </div>

              <div className="mt-4 overflow-hidden rounded-lg border border-[var(--line)]">
                <button
                  onClick={() => setRawOpen((o) => !o)}
                  className="flex w-full items-center gap-2 bg-[var(--panel2)] px-3 py-2 text-left text-xs font-semibold text-ink-900"
                >
                  <span className="text-ink-500">{rawOpen ? "▾" : "▸"}</span>
                  {t("Raw agent log")}
                  <span className="font-normal text-ink-500">({diag.length})</span>
                </button>
                {rawOpen && (
                  <pre className="whitespace-pre-wrap px-3 py-2 font-mono text-[11.5px] leading-relaxed text-ink-700">
                    {diag
                      .map(
                        (s) =>
                          `#${s.i} ${s.action}\n  💭 ${s.thought}\n  ${s.error ? "✗ " + s.error : "✓ " + (s.result || "")}`,
                      )
                      .join("\n")}
                  </pre>
                )}
              </div>
            </>
          ) : (
            <div className="mt-5 rounded-lg bg-[var(--panel2)] px-3 py-6 text-center text-xs text-ink-500">
              {t("No step diagnostics captured for this run.")}
            </div>
          )}

          <div className="mt-3 flex flex-wrap gap-4 text-xs">
            {result.video_url && (
              <a
                href={result.video_url}
                download={`run-${result.run_id}-case-${result.case_id}.mp4`}
                className="inline-block text-brand-700 underline"
              >
                {t("download video (mp4)")}
              </a>
            )}
            {result.trace_url && (
              <a
                href={result.trace_url}
                target="_blank"
                rel="noreferrer"
                className="inline-block text-brand-700 underline"
              >
                {t("download operation trace (history.json)")}
              </a>
            )}
          </div>
          {shotIdx !== null && (
            <ShotViewer shots={shots} index={shotIdx} onIndex={setShotIdx} onClose={() => setShotIdx(null)} />
          )}
        </Card>
      )}
    </Modal>
  );
}
