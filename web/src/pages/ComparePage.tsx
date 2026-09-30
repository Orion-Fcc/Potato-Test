import { Fragment, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, relTime, type Run, type RunResult, type TestCase } from "../lib/api";
import { useAbortSignal } from "../lib/hooks";
import { Badge, Button, Card, Checkbox, PageHeader } from "../components/ui";
import { Empty, PageSkeleton } from "../components/feedback";

type RunData = { run: Run; results: Record<number, RunResult> };

export function ComparePage() {
  const pid = Number(useParams().pid);
  const [params, setParams] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean).map(Number);

  if (runIds.length === 0) return <RunPicker pid={pid} onCompare={(ids) => setParams({ runs: ids.join(",") })} />;
  return <Matrix pid={pid} runIds={runIds} />;
}

function RunPicker({ pid, onCompare }: { pid: number; onCompare: (ids: number[]) => void }) {
  const { t } = useTranslation();
  const signal = useAbortSignal();
  const [runs, setRuns] = useState<Run[]>([]);
  const [sel, setSel] = useState<Set<number>>(new Set());
  useEffect(() => {
    api
      .listRuns(pid, { signal })
      .then(setRuns)
      .catch(() => {});
  }, [pid, signal]);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("Project")}
        title={t("Compare runs")}
        subtitle={t("Pick two or more runs to see per-case outcomes side by side.")}
      />
      {runs.length === 0 ? (
        <Empty title={t("No runs to compare")} sub={t("Run a suite first, then come back to compare runs side by side.")} />
      ) : (
      <Card className="overflow-x-auto">
        <div className="flex items-center justify-between border-b border-[var(--line)] bg-[color-mix(in_oklch,var(--panel2)_58%,transparent)] px-4 py-2.5">
          <span className="text-[12.5px] tabular-nums text-ink-500">{t("{{n}} selected", { n: sel.size })}</span>
          <Button disabled={sel.size < 2} onClick={() => onCompare([...sel])}>
            {t("Compare ({{n}})", { n: sel.size })}
          </Button>
        </div>
        <table className="w-full text-[13px]">
          <tbody>
            {runs.map((r) => (
              <tr key={r.id} className="tp-row">
                <td className="w-10 px-4 py-2.5">
                  <Checkbox
                    checked={sel.has(r.id)}
                    onChange={() =>
                      setSel((s) => {
                        const n = new Set(s);
                        if (n.has(r.id)) n.delete(r.id);
                        else n.add(r.id);
                        return n;
                      })
                    }
                  />
                </td>
                <td className="px-4 py-2.5 font-medium text-ink-900">{r.name}</td>
                <td className="px-4 py-2.5">
                  <Badge status={r.status} />
                </td>
                <td className="px-4 py-2.5 text-ink-500">{relTime(r.started_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      )}
    </div>
  );
}

const PRIORITY_STYLE: Record<string, string> = {
  P0: "bg-[var(--bad-bg)] text-[var(--bad-fg)]",
  P1: "bg-[var(--warn-bg)] text-[var(--warn-fg)]",
  P2: "bg-[var(--panel2)] text-ink-700",
  P3: "bg-[var(--panel2)] text-ink-500",
};
const CMP_FILTERS = [
  { k: "all", l: "All" },
  { k: "regressed", l: "Regressed only" },
  { k: "diff", l: "Differences" },
  { k: "failing", l: "Failing" },
] as const;

function StatusPill({ st }: { st?: string }) {
  const map: Record<string, [string, string]> = {
    passed: ["bg-[var(--ok-bg)] text-[var(--ok-fg)]", "✓"],
    failed: ["bg-[var(--bad-bg)] text-[var(--bad-fg)]", "✗"],
    error: ["bg-[var(--warn-bg)] text-[var(--warn-fg)]", "⚠"],
    running: ["bg-brand-50 text-brand-700", "●"],
  };
  const [cls, ch] = map[st ?? ""] ?? ["bg-[var(--panel2)] text-ink-500", "—"];
  return (
    <span className={"inline-flex h-5 w-5 items-center justify-center rounded-md text-[11px] font-bold " + cls}>
      {ch}
    </span>
  );
}

function Matrix({ pid, runIds }: { pid: number; runIds: number[] }) {
  const { t } = useTranslation();
  const signal = useAbortSignal();
  const [cases, setCases] = useState<TestCase[]>([]);
  const [data, setData] = useState<RunData[]>([]);
  const [filter, setFilter] = useState("all");
  const key = runIds.join(",");

  useEffect(() => {
    let live = true;
    api.listCases(pid, { signal }).then((cs) => live && setCases(cs)).catch(() => {});
    Promise.all(
      runIds.map(async (id) => {
        const [run, results] = await Promise.all([api.getRun(id, { signal }), api.getResults(id, { signal })]);
        const byCase: Record<number, RunResult> = {};
        results.forEach((r) => {
          byCase[r.case_id] = r;
        });
        return { run, results: byCase };
      }),
    )
      .then((d) => live && setData(d))
      .catch(() => {});
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, pid, signal]);

  const caseById = useMemo(() => new Map(cases.map((c) => [c.id, c] as const)), [cases]);
  const caseIds = useMemo(() => {
    const set = new Set<number>();
    data.forEach((d) => Object.keys(d.results).forEach((k) => set.add(Number(k))));
    return [...set];
  }, [data]);

  const rowsRaw = useMemo(
    () =>
      caseIds.map((cid) => {
        const statuses = data.map((d) => d.results[cid]?.status);
        const present = statuses.filter(Boolean) as string[];
        const first = present[0];
        const last = present[present.length - 1];
        let trend = "stable";
        if (!present.length) trend = "none";
        else if (first === "passed" && last && last !== "passed") trend = "regressed";
        else if (first && first !== "passed" && last === "passed") trend = "fixed";
        else if (present.every((s) => s !== "passed")) trend = "failing";
        else if (new Set(present).size > 1) trend = "diff";
        return { cid, c: caseById.get(cid), statuses, trend };
      }),
    [caseIds, data, caseById],
  );

  const summary = useMemo(() => {
    const s = { regressed: 0, fixed: 0, failing: 0, diff: 0 };
    for (const r of rowsRaw) {
      if (r.trend === "regressed") s.regressed++;
      else if (r.trend === "fixed") s.fixed++;
      else if (r.trend === "failing") s.failing++;
      if (new Set(r.statuses.filter(Boolean)).size > 1) s.diff++;
    }
    return s;
  }, [rowsRaw]);

  const rows = useMemo(
    () =>
      rowsRaw.filter((r) => {
        if (filter === "regressed") return r.trend === "regressed";
        if (filter === "failing") return r.trend === "failing" || r.trend === "regressed";
        if (filter === "diff") return new Set(r.statuses.filter(Boolean)).size > 1;
        return true;
      }),
    [rowsRaw, filter],
  );

  const groups = useMemo(() => {
    const by = new Map<string, typeof rows>();
    for (const r of rows) {
      const m = r.c?.module?.trim() || t("Ungrouped");
      (by.get(m) ?? by.set(m, []).get(m)!).push(r);
    }
    return [...by.entries()];
  }, [rows, t]);

  if (data.length === 0) return <PageSkeleton />;

  const trendLabel = (tr: string) => {
    if (tr === "regressed") return <span className="font-medium text-[var(--bad-fg)]">↓ {t("regressed")}</span>;
    if (tr === "fixed") return <span className="font-medium text-[var(--ok-fg)]">↑ {t("fixed")}</span>;
    if (tr === "failing") return <span className="text-ink-500">{t("always failing")}</span>;
    if (tr === "diff") return <span className="text-ink-500">{t("differs")}</span>;
    if (tr === "stable") return <span className="text-ink-500">{t("stable")}</span>;
    return <span className="text-ink-500">—</span>;
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link
          to={`/projects/${pid}/compare`}
          className="text-sm text-ink-500 outline-none transition-colors hover:text-ink-900"
        >
          {t("← Change selection")}
        </Link>
        <h1 className="tp-h1 text-xl font-semibold">
          {t("Comparing {{n}} runs", { n: data.length })}
        </h1>
      </div>

      <Card className="overflow-x-auto">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--line)] bg-[color-mix(in_oklch,var(--panel2)_55%,transparent)] px-4 py-3">
          <div className="flex flex-wrap gap-4 text-[13px]">
            <span>
              🔴 {t("regressed")} <b className="tabular-nums text-[var(--bad-fg)]">{summary.regressed}</b>
            </span>
            <span>
              🟢 {t("fixed")} <b className="tabular-nums text-[var(--ok-fg)]">{summary.fixed}</b>
            </span>
            <span>
              ⚫ {t("always failing")} <b className="tabular-nums">{summary.failing}</b>
            </span>
            <span>
              ~ {t("differs")} <b className="tabular-nums">{summary.diff}</b>
            </span>
            <span className="text-ink-500">{t("{{n}} cases", { n: rowsRaw.length })}</span>
          </div>
          <div className="inline-flex overflow-hidden rounded-lg border border-[var(--line)] bg-[var(--panel)] p-0.5">
            {CMP_FILTERS.map((f) => (
              <button
                key={f.k}
                onClick={() => setFilter(f.k)}
                className={
                  "rounded-md px-2.5 py-1 text-xs font-medium outline-none transition-[background-color,color,box-shadow] focus-visible:ring-2 focus-visible:ring-brand-100 " +
                  (filter === f.k
                    ? "bg-[var(--panel2)] text-ink-900 shadow-[0_1px_2px_-1px_color-mix(in_oklch,var(--ink-900)_25%,transparent)]"
                    : "text-ink-500 hover:text-ink-900")
                }
              >
                {t(f.l)}
              </button>
            ))}
          </div>
        </div>
        <table className="w-full text-sm">
          <thead className="tp-thead">
            <tr>
              <th className="px-4 py-2.5">{t("Case")}</th>
              <th className="px-3 py-2.5">{t("Priority")}</th>
              {data.map((d) => (
                <th key={d.run.id} className="px-3 py-2.5">
                  <Link to={`/projects/${pid}/runs/${d.run.id}`} className="text-ink-900 hover:text-brand-700">
                    #{d.run.id}
                  </Link>
                  <div className="font-normal normal-case tracking-normal text-ink-500">
                    {relTime(d.run.started_at ?? d.run.created_at)}
                    {d.run.summary ? ` · ${Math.round(d.run.summary.pass_rate * 100)}%` : ""}
                  </div>
                </th>
              ))}
              <th className="px-3 py-2.5">{t("Trend")}</th>
            </tr>
          </thead>
          <tbody>
            {groups.map(([module, rrows]) => (
              <Fragment key={module}>
                <tr className="bg-[color-mix(in_oklch,var(--panel2)_62%,transparent)]">
                  <td colSpan={data.length + 3} className="px-4 py-2 text-xs font-semibold text-ink-900">
                    {module}
                    <span className="ml-2 font-normal tabular-nums text-ink-500">{rrows.length}</span>
                  </td>
                </tr>
                {rrows.map((r) => (
                  <tr
                    key={r.cid}
                    className={
                      "tp-divide-row transition-colors " +
                      (r.trend === "regressed"
                        ? "bg-[color-mix(in_oklch,var(--bad-bg)_55%,transparent)]"
                        : "hover:bg-[var(--hover)]")
                    }
                  >
                    <td className="px-4 py-2.5">
                      <div className="font-medium text-ink-900">{r.c?.name ?? `#${r.cid}`}</div>
                      <div className="font-mono text-[11px] text-ink-500">{r.c?.case_key ?? `#${r.cid}`}</div>
                    </td>
                    <td className="px-3 py-2.5">
                      {r.c ? (
                        <span
                          className={"inline-flex rounded px-1.5 py-0.5 text-[11px] font-semibold " + (PRIORITY_STYLE[r.c.priority] ?? "")}
                        >
                          {r.c.priority}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    {r.statuses.map((st, i) => (
                      <td key={i} className="px-3 py-2.5">
                        <StatusPill st={st} />
                      </td>
                    ))}
                    <td className="px-3 py-2.5 text-xs">{trendLabel(r.trend)}</td>
                  </tr>
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </Card>
      <p className="text-xs text-ink-500">
        {t("Rows tinted red = passed in an earlier run but not in the latest (a regression).")}
      </p>
    </div>
  );
}
