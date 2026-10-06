import { useState } from "react";
import { useTranslation } from "react-i18next";
import { MediaPlayer, MediaProvider } from "@vidstack/react";
import { DefaultVideoLayout, defaultLayoutIcons } from "@vidstack/react/player/layouts/default";
import type { RunResult } from "../../lib/api";
import { useShowThoughts } from "../../lib/hooks";
import { Badge, Button, Card } from "../ui";
import { Modal } from "../Modal";
import { ShotViewer } from "../ShotViewer";
import { FailureNarrativeCard } from "./FailureNarrativeCard";
import { StepLine } from "./StepLine";

/**
 * Replay modal for a single case result: recorded video, the agent's
 * action → result steps, screenshots and the raw diagnostics JSON.
 * Split out of RunReport.tsx, which had grown past 900 lines.
 *
 * 每一步默认只显示「操作了什么 / 系统回了什么」。AI 的思考过程（thought）
 * 收在顶部开关后面：它解释"为什么这么走"，但会占掉 2/3 的版面，
 * 而测试员看回放是为了核对操作是否符合用例，不是读模型的内心独白。
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
  const [showThoughts, setShowThoughts] = useShowThoughts();
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

          {result.failure_narrative?.steps || result.failure_narrative?.actual ? (
            <FailureNarrativeCard
              narrative={result.failure_narrative}
              caseName={caseName}
            />
          ) : null}

          {/*
            判决理由与最终回答：**默认折叠**。

            测试员真正要的是上面那张三段式卡片（【操作步骤】/【实际结果】/【预期结果】）——
            它可以直接贴进缺陷单。而 judge_reason / final_answer 是"为什么判成这样"的
            支撑材料，只有在怀疑判定本身时才需要看。
            此前它们是默认展开的两行大字，实测会把三段式卡片挤到看不见的地方，
            用户反馈"回答太多了，其实只要三段式就够了"。所以收进 <details>。
          */}
          {(result.judge_reason || result.final_answer || result.error) && (
            <details className="mt-3 rounded-lg border border-[var(--line)] bg-[var(--panel2)]/40">
              <summary className="cursor-pointer select-none px-3 py-2 text-xs text-ink-500 hover:text-ink-800">
                {t("Judge")} / {t("Final answer")}
              </summary>
              <div className="flex flex-col gap-1.5 px-3 pb-3 text-sm">
                {/*
                  2026-10-04 失败根因分类：放在最上面，因为它回答的是测试员
                  打开这条失败时第一个问题 —— "这该找谁处理"。
                  用颜色区分"真缺陷"和"执行侧问题"：红色=要找开发，
                  琥珀色=环境/账号/数据类，重试或修环境即可。
                  通过的用例不显示这一行（没有根因可言）。
                */}
                {result.status !== "passed" && result.root_cause && (
                  <div className="flex items-center gap-2">
                    <span className="text-ink-500">{t("Root cause")}: </span>
                    <span
                      className={
                        "rounded px-1.5 py-0.5 text-xs font-medium " +
                        (result.is_real_defect
                          ? "bg-red-100 text-red-700"
                          : "bg-amber-100 text-amber-700")
                      }
                    >
                      {result.root_cause_label || result.root_cause}
                    </span>
                    {result.is_real_defect && (
                      <span className="text-xs text-ink-500">
                        {t("system defect — worth a bug report")}
                      </span>
                    )}
                  </div>
                )}
                <div>
                  <span className="text-ink-500">{t("Judge")}: </span>
                  <span className="text-ink-800">{result.judge_reason ?? result.error ?? "—"}</span>
                </div>
                <div>
                  <span className="text-ink-500">{t("Final answer")}: </span>
                  <span className="text-ink-800">{result.final_answer || "—"}</span>
                </div>
              </div>
            </details>
          )}

          {diag.length > 0 ? (
            <>
              <div className="mb-2 mt-5 flex items-center gap-2 text-sm font-medium text-ink-900">
                {t("Step timeline")}
                <span className="rounded-full bg-[var(--panel2)] px-1.5 py-0.5 text-[11px] font-normal text-ink-500">
                  {diag.length}
                </span>
                <span className="text-xs font-normal text-ink-500">{t("action → result")}</span>
                <label className="ml-auto flex cursor-pointer select-none items-center gap-1.5 text-xs font-normal text-ink-500 hover:text-ink-800">
                  <input
                    type="checkbox"
                    checked={showThoughts}
                    onChange={(e) => setShowThoughts(e.target.checked)}
                    className="cursor-pointer accent-brand-600"
                  />
                  {t("show AI thinking")}
                </label>
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
                    <StepLine step={step} showThoughts={showThoughts} />
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
                          `#${s.i} ${s.detail || s.action}\n  💭 ${s.thought}\n  ${s.error ? "✗ " + s.error : "✓ " + (s.result || "")}`,
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
