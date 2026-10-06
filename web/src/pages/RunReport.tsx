import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import "@vidstack/react/player/styles/default/theme.css";
import "@vidstack/react/player/styles/default/layouts/video.css";
import { api, type Run, type RunResult, type TestCase } from "../lib/api";
import { usePolling, useShowThoughts } from "../lib/hooks";
import { fmtSpan } from "../lib/metrics";
import { Badge, Button, Card, Input, LiveDot } from "../components/ui";
import { Modal } from "../components/Modal";
import { chartTooltip } from "../components/table";
import { ShotViewer } from "../components/ShotViewer";
import { useToast, useErrorToast } from "../components/toast";
import { RunHeader } from "../components/run/RunHeader";
import { RunKpis } from "../components/run/RunKpis";
import { ReplayPanel } from "../components/run/ReplayPanel";
import { StepLine } from "../components/run/StepLine";
import { CaseDrawer } from "../components/CaseDrawer";

const COLORS: Record<string, string> = {
  passed: "oklch(0.6 0.17 150)",
  failed: "oklch(0.58 0.22 25)",
  error: "oklch(0.7 0.17 70)",
};
const PRIORITY_STYLE: Record<string, string> = {
  P0: "bg-[var(--bad-bg)] text-[var(--bad-fg)]",
  P1: "bg-[var(--warn-bg)] text-[var(--warn-fg)]",
  P2: "bg-[var(--panel2)] text-ink-700",
  P3: "bg-[var(--panel2)] text-ink-500",
};

const FILTERS = [
  { key: "all", label: "All" },
  { key: "failing", label: "Failing only" },
  { key: "passed", label: "passed" },
  { key: "error", label: "error" },
] as const;

export function RunReport() {
  const { rid, pid } = useParams();
  const runId = Number(rid);
  const navigate = useNavigate();
  const toast = useToast();
  const fail = useErrorToast();
  const { t } = useTranslation();
  const [run, setRun] = useState<Run | null>(null);
  const [results, setResults] = useState<RunResult[]>([]);
  const [cases, setCases] = useState<TestCase[]>([]);
  const [envName, setEnvName] = useState<string | null>(null);
  const [selected, setSelected] = useState<RunResult | null>(null);
  // 在报告页直接查看/编辑用例本身。
  // 为什么需要：用例本身也可能写错（步骤描述不准、预期结果不对），
  // 而看到失败原因的那一刻正是最该能就地改的时候 —— 否则要跳到"测试用例"页去翻。
  const [editCase, setEditCase] = useState<TestCase | null>(null);
  // 2026-10-06 人工改判：AI 判定会不准，测试工程师要能自己拍板。
  // 存整条 RunResult 而不只是 id —— 弹窗里要显示用例名和 AI 原来的理由，
  // 让人知道自己在改什么、凭什么改。
  const [overrideFor, setOverrideFor] = useState<RunResult | null>(null);
  const [overrideReason, setOverrideReason] = useState("");
  const [overrideSaving, setOverrideSaving] = useState(false);
  const [filter, setFilter] = useState("all");
  const [q, setQ] = useState("");
  const [confirmDel, setConfirmDel] = useState(false);
  const [liveShot, setLiveShot] = useState<number | null>(null);
  // 实时面板与回放共用同一个开关：见 useShowThoughts 的注释。
  const [showThoughts, setShowThoughts] = useShowThoughts();
  const [cancelling, setCancelling] = useState(false);
  // step whose screenshot the live pane shows; null = follow the newest one
  const [pinnedStep, setPinnedStep] = useState<number | null>(null);

  useEffect(() => {
    const es = new EventSource(api.streamUrl(runId));
    es.addEventListener("progress", (e) => setRun(JSON.parse((e as MessageEvent).data)));
    api.getRun(runId).then((r) => {
      setRun(r);
      if (r.environment_id != null && pid) {
        api
          .listEnvironments(Number(pid))
          .then((es2) => setEnvName(es2.find((e) => e.id === r.environment_id)?.name ?? null))
          .catch(() => {});
      }
    });
    api.getResults(runId).then(setResults).catch(() => {});
    return () => es.close();
  }, [runId, pid]);
  useEffect(() => {
    if (pid) api.listCases(Number(pid)).then(setCases).catch(() => {});
  }, [pid]);

  const caseById = useMemo(() => new Map(cases.map((c) => [c.id, c] as const)), [cases]);
  const s = run?.summary;
  const chartData = s
    ? [
        { name: "passed", value: s.passed },
        { name: "failed", value: s.failed },
        { name: "error", value: s.error },
      ].filter((d) => d.value > 0)
    : [];
  const duration = run?.started_at && run?.finished_at ? fmtSpan(run.started_at, run.finished_at) : null;

  const active = run?.status === "running" || run?.status === "pending";

  // Fallback poll for environments where SSE does not deliver row updates.
  // It stops by itself once the run reaches a terminal state, and the shared
  // hook already pauses it while the tab is hidden.
  const refetchResults = useMemo(
    () => () => api.getResults(runId).then(setResults).catch(() => {}),
    [runId],
  );
  usePolling(refetchResults, active, 2500, 8000);

  // First-run coaching. A first run is slow (real browser + recording) and its report is
  // the first one anyone reads, so it gets a "this is normal" hint while nothing has
  // finished yet, and a what-now card at the end. Both self-retire: the hint as soon as
  // a result lands, the card once the project has more than one run.
  const [isFirstRun, setIsFirstRun] = useState(false);
  useEffect(() => {
    if (!pid || active) return;
    api
      .getStats(Number(pid))
      .then((st) => setIsFirstRun(st.run_count <= 1))
      .catch(() => {});
  }, [pid, active]);
  // merge case_ids with result rows so pending cases show; running cases now have real
  // rows (status "running") carrying a live-growing step timeline.
  const rows = useMemo(() => {
    const byId = new Map(results.map((r) => [r.case_id, r] as const));
    const ids = run?.case_ids ?? [];
    if (!ids.length) return results.map((r) => ({ case_id: r.case_id, status: r.status, r }));
    return ids.map((id) => {
      const r = byId.get(id);
      if (r) return { case_id: id, status: r.status, r };
      return { case_id: id, status: "pending" as const, r: undefined };
    });
  }, [results, run]);
  const runningIds = rows.filter((x) => x.status === "running").map((x) => x.case_id);
  const counts = useMemo(() => {
    const c = { passed: 0, failed: 0, error: 0, running: 0, pending: 0 };
    for (const x of rows) (c as Record<string, number>)[x.status] = ((c as Record<string, number>)[x.status] ?? 0) + 1;
    return c;
  }, [rows]);
  const total = rows.length;
  const liveRow = active ? results.find((r) => r.status === "running") : undefined;
  const liveShots = (liveRow?.diagnostics ?? []).filter((d) => d.screenshot);
  // -1 when the pinned step has no shot (or vanished with a new case) => fall back to newest
  const pinnedIdx = pinnedStep === null ? -1 : liveShots.findIndex((s) => s.i === pinnedStep);
  const shownIdx = pinnedIdx >= 0 ? pinnedIdx : liveShots.length - 1;
  const shownStep = liveShots[shownIdx];
  const filtered = useMemo(
    () =>
      rows.filter((x) => {
        if (filter === "failing" && !(x.status === "failed" || x.status === "error")) return false;
        if (filter === "passed" && x.status !== "passed") return false;
        if (filter === "error" && x.status !== "error") return false;
        if (q) {
          const c = caseById.get(x.case_id);
          const hay = `${c?.case_key ?? ""} ${c?.name ?? ""} #${x.case_id}`.toLowerCase();
          if (!hay.includes(q.toLowerCase())) return false;
        }
        return true;
      }),
    [rows, filter, q, caseById],
  );
  const latencyData = results
    .filter((r) => r.status === "passed" || r.status === "failed" || r.status === "error")
    .map((r) => ({
      x: caseById.get(r.case_id)?.case_key ?? `#${r.case_id}`,
      s: +(r.latency_ms / 1000).toFixed(1),
      status: r.status,
    }));

  const createIssue = async (r: RunResult) => {
    try {
      const c = caseById.get(r.case_id);
      const issue = await api.createIssue(Number(pid), {
        title: `${c?.name ?? `Case #${r.case_id}`} ${r.status}`,
        description: r.judge_reason ?? r.error ?? "",
        severity: "high",
        case_id: r.case_id,
        run_id: runId,
        result_id: r.id,
      });
      toast("success", `Issue #${issue.id} created`);
      navigate(`/projects/${pid}/issues`);
    } catch (e) {
      fail(e);
    }
  };

  const rerun = async (only?: "failing" | "error") => {
    try {
      const nr = await api.rerun(runId, only);
      toast(
        "success",
        only === "error"
          ? t("Re-running the errored cases")
          : only === "failing"
            ? t("Re-running the failed cases")
            : t("Re-running the same suite"),
      );
      navigate(`/projects/${pid}/runs/${nr.id}`);
    } catch (e) {
      fail(e);
    }
  };
  const doDelete = async (close: () => void) => {
    try {
      await api.deleteRun(runId);
      close();
      navigate(`/projects/${pid}/runs`);
    } catch (e) {
      fail(e);
    }
  };

  // ---- 2026-10-06 人工改判 ----
  // 改完立刻重拉结果列表：报告页的 KPI、饼图、延迟图全都是从 results 算出来的，
  // 不刷新的话界面上状态变了但统计没变，看起来就像改了个寂寞。
  const refreshResults = () => api.getResults(runId).then(setResults).catch(() => {});

  const doOverride = async (status: "passed" | "failed", close: () => void) => {
    if (!overrideFor) return;
    setOverrideSaving(true);
    try {
      const updated = await api.overrideResult(overrideFor.id, {
        status,
        reason: overrideReason.trim() || undefined,
      });
      setResults((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
      // 详情面板如果正开着同一条，也要跟着更新
      setSelected((prev) => (prev && prev.id === updated.id ? updated : prev));
      toast("success", t("Verdict updated"));
      close();
      refreshResults();
    } catch (e) {
      fail(e);
    } finally {
      setOverrideSaving(false);
    }
  };

  const clearOverride = async (close: () => void) => {
    if (!overrideFor) return;
    setOverrideSaving(true);
    try {
      const updated = await api.overrideResult(overrideFor.id, { clear: true });
      setResults((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
      setSelected((prev) => (prev && prev.id === updated.id ? updated : prev));
      toast("success", t("Reverted to the AI verdict"));
      close();
      refreshResults();
    } catch (e) {
      fail(e);
    } finally {
      setOverrideSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <RunHeader
        pid={pid}
        runId={runId}
        run={run}
        duration={duration}
        envName={envName}
        active={active}
        cancelling={cancelling}
        counts={counts}
        onCancel={() => {
          setCancelling(true);
          api
            .cancelRun(runId)
            .then(() => toast("info", t("Cancelling — cases stop after the current step")))
            .catch(() => {
              setCancelling(false);
              toast("error", t("Could not cancel the run"));
            });
        }}
        onRerun={rerun}
        onDelete={() => setConfirmDel(true)}
      />

      {active && counts.passed + counts.failed + counts.error === 0 && (
        <div className="rounded-xl border border-brand-100 bg-brand-50 px-4 py-3 text-sm text-ink-700">
          {t("Nothing has finished yet — that's expected. Each case launches a real Chrome, drives it and records video, so the first result takes roughly 30–90 seconds. Results stream in below as they land; you can leave this page.")}
        </div>
      )}

      {!active && isFirstRun && s && s.total > 0 && (
        <div className="rounded-xl border border-brand-100 bg-brand-50 px-4 py-3 text-sm text-ink-700">
          <div className="font-medium text-ink-900">{t("Your first report — what now?")}</div>
          {s.failed + s.error === 0 ? (
            <div className="mt-1">
              {t("Everything passed. Save these cases as a suite and give it a schedule, and this becomes a regression run that happens without you.")}{" "}
              <Link to={`/projects/${pid}/suites`} className="font-medium text-brand-700 hover:underline">
                {t("Create a suite →")}
              </Link>
            </div>
          ) : (
            <div className="mt-1">
              {t("{{n}} case(s) did not pass. The Reason column has the judge's verdict; “Replay” plays back the video and the agent's steps so you can see where it went wrong, and “+ Issue” turns it into a tracked issue.", {
                n: s.failed + s.error,
              })}
            </div>
          )}
        </div>
      )}

      <RunKpis run={run} summary={s ?? null} active={active} duration={duration} runningCount={runningIds.length} />

      {active && (
        <Card className="p-4">
          <div className="flex items-center gap-2">
            <div className="text-sm font-medium text-ink-900">{t("Run progress")}</div>
            {runningIds.length > 0 && (
              <span
                className="flex min-w-0 items-center gap-1.5 truncate text-xs text-ink-500"
                title={runningIds.map((i) => `#${i}`).join(", ")}
              >
                <LiveDot />
                {t("{{n}} running", { n: runningIds.length })}
              </span>
            )}
            <span className="ml-auto text-xl font-semibold text-ink-900">
              {run?.processed_count ?? 0}/{total}{" "}
              <span className="text-xs font-normal text-ink-500">
                · {total ? Math.round(((run?.processed_count ?? 0) / total) * 100) : 0}%
              </span>
            </span>
          </div>
          <div className="my-3 flex h-3.5 overflow-hidden rounded-lg bg-[var(--panel2)]">
            <span style={{ width: `${total ? (counts.passed / total) * 100 : 0}%`, background: "var(--ok-fg)" }} />
            <span style={{ width: `${total ? (counts.failed / total) * 100 : 0}%`, background: "var(--bad-fg)" }} />
            <span style={{ width: `${total ? (counts.error / total) * 100 : 0}%`, background: "var(--warn-fg)" }} />
            <span
              className="animate-pulse"
              style={{ width: `${total ? (counts.running / total) * 100 : 0}%`, background: "var(--brand-600)" }}
            />
          </div>
          <div className="flex flex-wrap gap-4 text-[13px] text-ink-700">
            {(
              [
                ["passed", "var(--ok-fg)"],
                ["failed", "var(--bad-fg)"],
                ["error", "var(--warn-fg)"],
                ["running", "var(--brand-600)"],
                ["pending", "var(--line)"],
              ] as const
            ).map(([k, col]) => (
              <span key={k} className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-[2px]" style={{ background: col }} />
                {t(k)} <b className="text-ink-900">{counts[k]}</b>
              </span>
            ))}
          </div>
        </Card>
      )}

      {liveShot !== null && liveShots.length > 0 && (
        <ShotViewer shots={liveShots} index={liveShot} onIndex={setLiveShot} onClose={() => setLiveShot(null)} />
      )}

      {active && liveRow && (
        <Card className="p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <div className="text-sm font-medium text-ink-900">{t("Live execution")}</div>
            <span className="text-xs text-ink-500">
              {caseById.get(liveRow.case_id)?.name ?? `#${liveRow.case_id}`}
              {caseById.get(liveRow.case_id)?.case_key ? ` · ${caseById.get(liveRow.case_id)?.case_key}` : ""}
            </span>
            {liveRow.diagnostics && liveRow.diagnostics.length > 0 && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-brand-50 px-2 py-0.5 text-xs font-medium text-brand-700">
                <LiveDot />
                {t("Step {{n}}", { n: liveRow.diagnostics.length })}
              </span>
            )}
            {pinnedIdx >= 0 && (
              <button
                onClick={() => setPinnedStep(null)}
                className="rounded-full border border-[var(--line)] px-2 py-0.5 text-xs text-ink-700 hover:bg-brand-50"
              >
                {t("showing step {{n}} · back to latest", { n: pinnedStep })}
              </button>
            )}
            <label className="ml-auto flex cursor-pointer select-none items-center gap-1.5 text-xs text-ink-500 hover:text-ink-800">
              <input
                type="checkbox"
                checked={showThoughts}
                onChange={(e) => setShowThoughts(e.target.checked)}
                className="cursor-pointer accent-brand-600"
              />
              {t("show AI thinking")}
            </label>
          </div>
          <div className="grid gap-4 lg:grid-cols-[1.15fr_1fr]">
            <div className="overflow-hidden rounded-xl border border-[var(--line)]">
              {shownStep ? (
                // cropped to 16/10 here — click through for the whole page at full size
                <button
                  onClick={() => setLiveShot(shownIdx)}
                  title={t("click to enlarge")}
                  className="block w-full cursor-zoom-in"
                >
                  <img
                    src={shownStep.screenshot!}
                    alt={`step ${shownStep.i}`}
                    className="aspect-[16/10] w-full bg-white object-cover object-top"
                  />
                </button>
              ) : (
                <div className="grid aspect-[16/10] place-items-center bg-[var(--panel2)] text-sm text-ink-500">
                  {t("waiting for first screenshot…")}
                </div>
              )}
            </div>
            <div className="max-h-[340px] overflow-auto">
              {(liveRow.diagnostics ?? []).map((step, i, arr) => {
                const isLast = i === arr.length - 1;
                const isShown = shownStep?.i === step.i;
                return (
                  <button
                    key={step.i}
                    onClick={() => setPinnedStep(step.i)}
                    disabled={!step.screenshot}
                    title={step.screenshot ? t("click a step to see its screenshot") : undefined}
                    className={
                      "tp-divide-row grid w-full grid-cols-[22px_1fr] gap-2.5 px-1.5 py-2 text-left " +
                      (step.screenshot ? "cursor-pointer hover:bg-brand-50/60 " : "") +
                      (isShown ? "bg-brand-50" : "")
                    }
                  >
                    <div
                      className={
                        "grid h-[20px] w-[20px] place-items-center rounded-full text-[10px] font-bold text-[var(--on-brand)] " +
                        (isLast ? "bg-brand-600" : "bg-[var(--ok-fg)]")
                      }
                    >
                      {step.i}
                    </div>
                    <div className="min-w-0">
                      <StepLine step={step} showThoughts={showThoughts} compact />
                      {isLast && (
                        <div className="mt-1 flex items-center gap-1.5 text-xs text-brand-700">
                          <LiveDot />
                          {t("running…")}
                        </div>
                      )}
                    </div>
                  </button>
                );
              })}
              {(liveRow.diagnostics ?? []).length === 0 && (
                <div className="py-6 text-center text-xs text-ink-500">{t("executing…")}</div>
              )}
            </div>
          </div>
        </Card>
      )}

      <Card className="overflow-x-auto">
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2">
            <div className="flex min-w-0 items-center gap-2 text-sm font-medium text-ink-900">
              {t("Cases")}
              <span className="text-xs font-normal text-ink-500">{rows.length}</span>
              {runningIds.length > 0 && (
                <span
                  className="flex min-w-0 items-center gap-1.5 truncate text-xs font-normal text-ink-500"
                  title={runningIds.map((i) => `#${i}`).join(", ")}
                >
                  <LiveDot />
                  {t("{{n}} running", { n: runningIds.length })}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder={t("Search case…")}
                className="h-8 max-w-[160px] text-xs"
              />
              <div className="inline-flex overflow-hidden rounded-lg border border-[var(--line)]">
                {FILTERS.map((f) => (
                  <button
                    key={f.key}
                    onClick={() => setFilter(f.key)}
                    className={
                      "border-r border-[var(--line)] px-2.5 py-1 text-xs last:border-r-0 " +
                      (filter === f.key ? "bg-brand-600 text-[var(--on-brand)]" : "text-ink-700 hover:text-ink-900")
                    }
                  >
                    {t(f.label)}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <table className="w-full text-sm">
            <thead className="border-y border-[var(--line)] text-left text-xs text-ink-500">
              <tr>
                <th className="px-4 py-2">{t("Case")}</th>
                <th className="px-3 py-2">{t("Module")}</th>
                <th className="px-3 py-2">{t("Priority")}</th>
                <th className="px-3 py-2">{t("Status")}</th>
                <th className="px-3 py-2">{t("Attempts")}</th>
                <th className="px-3 py-2">{t("Latency")}</th>
                <th className="px-4 py-2">{t("Judge")}</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {filtered.map((x) => {
                const r = x.r;
                const done = !!r && x.status !== "running";
                const c = caseById.get(x.case_id);
                return (
                  <tr key={x.case_id} className="tp-row hover:bg-brand-50/40">
                    <td className="max-w-[300px] px-4 py-2">
                      <div className="truncate font-medium text-ink-900" title={c?.name ?? ""}>
                        {c?.name ?? `#${x.case_id}`}
                      </div>
                      <div className="font-mono text-[11px] text-ink-500">{c?.case_key ?? `#${x.case_id}`}</div>
                      {r?.account_label && (
                        <div className="mt-0.5 text-[11px] text-brand-700">👤 {r.account_label}</div>
                      )}
                    </td>
                    <td className="max-w-[110px] truncate px-3 py-2 text-ink-500" title={c?.module ?? ""}>
                      {c?.module ?? "—"}
                    </td>
                    <td className="px-3 py-2">
                      {c ? (
                        <span
                          className={"inline-flex rounded px-1.5 py-0.5 text-[11px] font-semibold " + (PRIORITY_STYLE[c.priority] ?? "")}
                        >
                          {c.priority}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="space-x-1 px-3 py-2">
                      {/* 结论可点 —— 点开就是改判弹窗。
                          为什么要能改：AI 判定会不准（用户实测反馈），
                          没有改判入口的话，一条误判会一直挂在报告里，
                          而"去改用例描述再重跑"要几十分钟，代价完全不对等。 */}
                      {x.status === "running" ? (
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-brand-50 px-2 py-0.5 text-xs font-medium text-brand-700">
                          <LiveDot />
                          {t("running")}
                        </span>
                      ) : done ? (
                        <button
                          type="button"
                          onClick={() => {
                            setOverrideFor(r!);
                            setOverrideReason(r!.override_reason ?? "");
                          }}
                          title={t("Click to override this verdict (AI judging is not always right)")}
                          className="rounded-full outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
                        >
                          <Badge status={x.status} />
                        </button>
                      ) : (
                        <Badge status={x.status}>{x.status === "pending" ? t("pending") : undefined}</Badge>
                      )}
                      {/* 人工改判标记：不标出来，报告里的"通过"就分不清是 AI 判的还是人认的，
                          通过率/真缺陷率这些数字也就没法信了。 */}
                      {r?.verdict_override && (
                        <span
                          className="inline-flex items-center rounded-full border border-brand-200 bg-brand-50 px-1.5 py-[1px] text-[10px] font-medium text-brand-700"
                          title={
                            t("Manually overridden by {{who}}", { who: r.override_by ?? "—" }) +
                            (r.override_reason ? ` — ${r.override_reason}` : "")
                          }
                        >
                          {t("manual")}
                        </span>
                      )}
                      {r?.flaky && <Badge status="flaky">flaky</Badge>}
                    </td>
                    <td className="px-3 py-2 text-ink-700">{done ? (r!.flaky ? `×${r!.attempts}` : r!.attempts) : "—"}</td>
                    <td className="px-3 py-2 text-ink-700">{done ? `${(r!.latency_ms / 1000).toFixed(1)}s` : "—"}</td>
                    <td className="max-w-[160px] truncate px-4 py-2 text-ink-700" title={r?.judge_reason ?? r?.error ?? ""}>
                      {done ? (r!.judge_reason ?? r!.error ?? "—") : x.status === "running" ? t("executing…") : "—"}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <div className="flex justify-end gap-1">
                        {/* 查看/编辑用例本身。刻意放在 done 判断**之外**：
                            用例写错了跟它跑没跑完无关，未跑的用例同样需要能改。 */}
                        {c && (
                          <Button
                            size="sm"
                            variant="outline"
                            title={t("View and edit this case: steps, expected result, tags")}
                            onClick={() => setEditCase(c)}
                          >
                            {t("Case")}
                          </Button>
                        )}
                        {done && (
                          <>
                            {(r!.status === "failed" || r!.status === "error") && (
                              <Button size="sm" variant="outline" onClick={() => createIssue(r!)}>
                                {t("+ Issue")}
                              </Button>
                            )}
                            <Button size="sm" variant="outline" onClick={() => setSelected(r!)}>
                              {t("Replay")}
                            </Button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-4 py-6 text-center text-ink-500">
                    {run?.status === "running" ? t("executing…") : t("no results")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>

      {!active && (
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="p-4">
          <div className="mb-2 text-sm font-medium text-ink-900">{t("Outcome")}</div>
          {chartData.length ? (
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie data={chartData} dataKey="value" nameKey="name" innerRadius={50} outerRadius={80}>
                  {chartData.map((d) => (
                    <Cell key={d.name} fill={COLORS[d.name]} />
                  ))}
                </Pie>
                <Tooltip {...chartTooltip} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="grid h-[200px] place-items-center text-sm text-ink-500">
              {run?.status === "running" ? t("running…") : t("no data")}
            </div>
          )}
        </Card>

        {latencyData.length > 0 && (
          <Card className="p-4 lg:col-span-2">
            <div className="mb-3 text-sm font-medium text-ink-900">{t("Latency by case (s)")}</div>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={latencyData} margin={{ top: 4, right: 12, bottom: 0, left: -20 }}>
                <XAxis dataKey="x" tick={{ fontSize: 11, fill: "oklch(0.55 0.015 250)" }} tickLine={false} />
                <YAxis tick={{ fontSize: 11, fill: "oklch(0.55 0.015 250)" }} tickLine={false} axisLine={false} />
                <Tooltip formatter={(v) => [`${v}s`, "latency"]} {...chartTooltip} />
                <Bar dataKey="s" radius={[3, 3, 0, 0]}>
                  {latencyData.map((d, i) => (
                    <Cell key={i} fill={COLORS[d.status] ?? "oklch(0.52 0.21 250)"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </Card>
        )}
      </div>
      )}

      {selected && <ReplayPanel result={selected} caseName={caseById.get(selected.case_id)?.name} onClose={() => setSelected(null)} />}

      {editCase && (
        <CaseDrawer
          caseData={editCase}
          onClose={() => setEditCase(null)}
          onSaved={() => {
            setEditCase(null);
            // 重新拉一遍用例列表：报告页的用例名/模块/优先级都取自 caseById，
            // 改完不刷新的话页面上还是旧值，会让人以为没保存成功。
            if (pid) api.listCases(Number(pid)).then(setCases).catch(() => {});
          }}
        />
      )}

      {overrideFor && (
        <Modal
          onClose={() => {
            setOverrideFor(null);
            setOverrideReason("");
          }}
          className="max-w-lg"
        >
          {(close) => (
            <Card className="space-y-4 p-5">
              <div>
                <h2 className="text-base font-semibold text-ink-900">{t("Override verdict")}</h2>
                <p className="mt-1 text-sm text-ink-500">
                  {caseById.get(overrideFor.case_id)?.name ?? `case #${overrideFor.case_id}`}
                </p>
              </div>

              {/* 改之前先看清楚 AI 原来判的什么、凭什么判的 ——
                  否则改判就变成了没有依据的拍脑袋。 */}
              <div className="rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-3 text-xs leading-relaxed text-ink-600">
                <div className="mb-1 font-medium text-ink-800">
                  {t("AI verdict")}: {overrideFor.original_status ?? overrideFor.status}
                </div>
                <div className="whitespace-pre-wrap">
                  {overrideFor.judge_reason || overrideFor.error || "—"}
                </div>
              </div>

              <div>
                <div className="mb-1.5 text-sm font-medium text-ink-900">{t("Your verdict")}</div>
                <div className="flex gap-2">
                  <Button
                    variant={overrideFor.status === "passed" ? "primary" : "outline"}
                    onClick={() => doOverride("passed", close)}
                    disabled={overrideSaving}
                  >
                    {t("passed")}
                  </Button>
                  <Button
                    variant={overrideFor.status === "failed" ? "primary" : "outline"}
                    onClick={() => doOverride("failed", close)}
                    disabled={overrideSaving}
                  >
                    {t("failed")}
                  </Button>
                </div>
              </div>

              <div>
                <div className="mb-1.5 text-sm font-medium text-ink-900">{t("Reason (optional)")}</div>
                <Input
                  value={overrideReason}
                  onChange={(e) => setOverrideReason(e.target.value)}
                  placeholder={t("Why is the AI verdict wrong?")}
                />
              </div>

              <div className="flex items-center justify-between gap-2">
                {/* 撤销：回到 AI 的原判。改错了要能退回去。 */}
                {overrideFor.verdict_override ? (
                  <Button variant="ghost" onClick={() => clearOverride(close)} disabled={overrideSaving}>
                    {t("Revert to AI verdict")}
                  </Button>
                ) : (
                  <span />
                )}
                <Button variant="outline" onClick={close}>
                  {t("Cancel")}
                </Button>
              </div>
            </Card>
          )}
        </Modal>
      )}

      {confirmDel && (
        <Modal onClose={() => setConfirmDel(false)} className="max-w-md">
          {(close) => (
            <Card className="space-y-4 p-5">
              <h2 className="text-base font-semibold text-ink-900">{t("Delete run(s)?")}</h2>
              <p className="text-sm text-ink-500">
                {t("This permanently removes {{n}} run(s) and their results. This cannot be undone.", { n: 1 })}
              </p>
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={close}>
                  {t("Cancel")}
                </Button>
                <Button variant="danger" onClick={() => doDelete(close)}>
                  {t("Delete")}
                </Button>
              </div>
            </Card>
          )}
        </Modal>
      )}
    </div>
  );
}

/**
 * Run header — breadcrumb, title, status badge, meta line and the action bar
 * (Export / Stop / Re-run failed / Re-run errors / Re-run suite / Delete).
 * Extracted from the page body: the button cluster is ~60 lines of conditionals
 * that made the main component hard to scan.
 */
