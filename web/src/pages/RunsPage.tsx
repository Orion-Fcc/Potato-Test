import { Fragment, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, relTime, type Run, type TestCase } from "../lib/api";
import { fmtMs, fmtSpan, rateColor, rateTone } from "../lib/metrics";
import { useAbortSignal, useDebounced, usePolling } from "../lib/hooks";
import { Badge, Button, Card, Checkbox, Input, PageHeader, Select, Stat, StatRing } from "../components/ui";
import { Modal } from "../components/Modal";
import { useErrorToast } from "../components/toast";

// ── date bucketing (grouping key + display order) ──────────────────────────────
const BUCKET_ORDER = ["Today", "Yesterday", "This week", "Earlier"] as const;
function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}
function bucketOf(iso: string | null | undefined, now: Date): string {
  if (!iso) return "Earlier";
  const t = startOfDay(new Date(iso));
  const today = startOfDay(now);
  const day = 86_400_000;
  if (t === today) return "Today";
  if (t === today - day) return "Yesterday";
  if (t > today - 7 * day) return "This week";
  return "Earlier";
}
function fmtDuration(run: Run): string {
  return fmtSpan(run.started_at, run.finished_at);
}

/** Cases in this run worth re-running: everything the judge didn't pass, plus anything
 *  the run never got a verdict for. Drives the "Re-run failed" button. */
function failedCount(run: Run): number {
  if (run.summary) return run.summary.failed + run.summary.error;
  return Math.max(0, run.total_count - run.passed_count);
}

/** Infra outcomes only — timeouts, dead sessions, crashed browsers. A judge "failed" is a
 *  finding about the product; re-running it proves nothing. Shown as its own button only
 *  when it is a strict subset of the failures, else it duplicates "Re-run failed". */
function errorCount(run: Run): number {
  return run.summary?.error ?? 0;
}

function ResultChips({ run }: { run: Run }) {
  const s = run.summary;
  if (!s) {
    return <span className="text-[12px] font-semibold text-[var(--ok-fg)]">✓{run.passed_count}</span>;
  }
  const chip = (cls: string, mark: string, n: number) =>
    n === 0 ? null : (
      <span className={cls}>
        {mark}
        {n}
      </span>
    );
  return (
    <span className="inline-flex gap-1.5 text-[12px] font-semibold tabular-nums">
      {chip("text-[var(--ok-fg)]", "✓", s.passed)}
      {chip("text-[var(--bad-fg)]", "✗", s.failed)}
      {chip("text-[var(--warn-fg)]", "⚠", s.error)}
      {s.passed + s.failed + s.error === 0 && <span className="text-ink-400">—</span>}
    </span>
  );
}


export function RunsPage() {
  const pid = Number(useParams().pid);
  const navigate = useNavigate();
  const fail = useErrorToast();
  const { t } = useTranslation();
  const [runs, setRuns] = useState<Run[]>([]);
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [renaming, setRenaming] = useState<{ id: number; value: string } | null>(null);
  const [renameSaving, setRenameSaving] = useState(false);
  const [confirmDel, setConfirmDel] = useState<number[] | null>(null);

  const [cases, setCases] = useState<TestCase[]>([]);
  const signal = useAbortSignal();

  const load = useMemo(
    () => () =>
      api
        .listRuns(pid, { signal })
        .then(setRuns)
        .catch((e) => {
          fail(e);
        }),
    // toast is a stable context value; pid/signal are the real inputs
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pid, signal],
  );

  useEffect(() => {
    load();
    api
      .listCases(pid, { signal })
      .then(setCases)
      .catch(() => {});
  }, [pid, load, signal]);

  const metrics = useMemo(() => {
    const done = runs.filter((r) => r.summary);
    const avgPass = done.length
      ? Math.round((done.reduce((a, r) => a + (r.summary?.pass_rate ?? 0), 0) / done.length) * 100)
      : 0;
    const durs = done
      .filter((r) => r.started_at && r.finished_at)
      .map((r) => new Date(r.finished_at as string).getTime() - new Date(r.started_at as string).getTime());
    const avgMs = durs.length ? durs.reduce((a, b) => a + b, 0) / durs.length : 0;
    const enabled = cases.filter((c) => c.enabled);
    const ran = enabled.filter((c) => c.last_status);
    return {
      total: runs.length,
      done: done.length,
      avgPass,
      avgDur: fmtMs(avgMs),
      coverage: enabled.length ? Math.round((ran.length / enabled.length) * 100) : 0,
      ranCount: ran.length,
      enabled: enabled.length,
    };
  }, [runs, cases]);

  const active = runs.some((r) => r.status === "pending" || r.status === "running");
  // Shared poller: never overlaps two in-flight reads, and pauses entirely
  // while the tab is hidden (the old setInterval kept firing at 8s forever).
  usePolling(load, active, 2500, 8000);

  // typing in the search box re-filters the table; debounce so a long run list
  // is not rescanned on every keystroke
  const dq = useDebounced(q, 180);
  const filtered = useMemo(
    () =>
      runs.filter((r) => {
        if (status !== "all" && r.status !== status) return false;
        if (dq && !r.name.toLowerCase().includes(dq.toLowerCase()) && String(r.id) !== dq.trim())
          return false;
        return true;
      }),
    [runs, status, dq],
  );

  // group by date bucket of started_at (fall back to created_at); newest first inside each group
  const groups = useMemo(() => {
    const now = new Date();
    const by = new Map<string, Run[]>();
    for (const r of filtered) {
      const key = bucketOf(r.started_at ?? r.created_at, now);
      (by.get(key) ?? by.set(key, []).get(key)!).push(r);
    }
    for (const arr of by.values()) arr.sort((a, b) => b.id - a.id);
    return BUCKET_ORDER.filter((k) => by.has(k)).map((k) => ({ bucket: k, rows: by.get(k)! }));
  }, [filtered]);

  const toggle = (id: number) =>
    setSel((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  const toggleGroup = (b: string) =>
    setCollapsed((s) => {
      const n = new Set(s);
      n.has(b) ? n.delete(b) : n.add(b);
      return n;
    });

  const saveRename = async (close: () => void) => {
    if (!renaming) return;
    const name = renaming.value.trim();
    if (!name) return;
    setRenameSaving(true);
    try {
      await api.renameRun(renaming.id, name);
      close();
      load();
    } catch (e) {
      fail(e);
    } finally {
      setRenameSaving(false);
    }
  };

  const doDelete = async (close: () => void) => {
    if (!confirmDel) return;
    try {
      await Promise.all(confirmDel.map((id) => api.deleteRun(id)));
      setSel((s) => {
        const n = new Set(s);
        confirmDel.forEach((id) => n.delete(id));
        return n;
      });
      close();
      load();
    } catch (e) {
      fail(e);
    }
  };

  const rerun = async (id: number, only?: "failing" | "error") => {
    try {
      const r = await api.rerun(id, only);
      navigate(`/projects/${pid}/runs/${r.id}`);
    } catch (e) {
      fail(e);
    }
  };
  const cancel = (id: number) => api.cancelRun(id).then(load).catch((e) => fail(e));

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("Project")}
        title={t("Runs")}
        subtitle={
          <>
            {t("Each run executes a batch of cases and produces a report.")} ·{" "}
            <span className="tabular-nums">{t("{{n}} runs", { n: runs.length })}</span>
          </>
        }
        actions={
          <>
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("Search run name…")}
              className="h-9 max-w-[200px]"
            />
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="h-9 w-[7.5rem]">
              {["all", "running", "completed", "failed", "cancelled"].map((s) => (
                <option key={s} value={s}>
                  {s === "all" ? t("All statuses") : t(s)}
                </option>
              ))}
            </Select>
            {sel.size >= 2 && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => navigate(`/projects/${pid}/compare?runs=${[...sel].join(",")}`)}
              >
                {t("Compare ({{n}})", { n: sel.size })}
              </Button>
            )}
            {sel.size > 0 && (
              <Button variant="danger" size="sm" onClick={() => setConfirmDel([...sel])}>
                {t("Delete ({{n}})", { n: sel.size })}
              </Button>
            )}
          </>
        }
      />

      {runs.length > 0 && (
        <div className="tp-rise grid grid-cols-2 gap-4 lg:grid-cols-4">
          <Stat
            label={t("Total runs")}
            value={metrics.total}
            sub={t("{{n}} completed", { n: metrics.done })}
          />
          <Stat
            label={t("Avg pass rate")}
            value={metrics.done ? `${metrics.avgPass}%` : "—"}
            sub={t("across completed runs")}
            tone={metrics.done ? rateTone(metrics.avgPass / 100) : undefined}
            right={
              metrics.done ? (
                <StatRing
                  pct={metrics.avgPass}
                  size={46}
                  thickness={4}
                  showLabel={false}
                  color={rateColor(metrics.avgPass / 100)}
                />
              ) : undefined
            }
          />
          <Stat
            label={t("Suite coverage")}
            value={metrics.enabled ? `${metrics.coverage}%` : "—"}
            sub={t("{{r}}/{{e}} cases", { r: metrics.ranCount, e: metrics.enabled })}
            right={
              metrics.enabled > 0 ? (
                <StatRing pct={metrics.coverage} size={46} thickness={4} showLabel={false} color="var(--brand-600)" />
              ) : undefined
            }
          />
          <Stat
            label={t("Avg duration")}
            value={metrics.avgDur}
            sub={t("per run")}
          />
        </div>
      )}

      {filtered.length === 0 ? (
        <Card className="px-6 py-14 text-center">
          <div className="mx-auto flex max-w-md flex-col items-center gap-2">
            <div className="text-[13px] font-medium text-ink-900">
              {runs.length === 0 ? t("No runs yet") : t("No runs match this filter.")}
            </div>
            {runs.length === 0 && (
              <>
                <p className="text-[13px] leading-relaxed text-ink-500">
                  {t(
                    "A run executes a batch of cases and produces a video, a step-by-step trace and a pass/fail verdict for each.",
                  )}
                </p>
                <Button size="sm" className="mt-2" onClick={() => navigate(`/projects/${pid}/cases`)}>
                  {t("Go to Test Cases →")}
                </Button>
              </>
            )}
          </div>
        </Card>
      ) : (
        <Card className="overflow-x-auto">
          {/*
            table-fixed with TWO flexing columns: Run (col 2) and Actions (last col).
            Actions holds at most 3 controls per row and never wraps — the long
            "Re-run failed (378)" labels are the widest thing in the table, so the
            column gets a min-width and the whole card scrolls below ~1080px
            instead of squeezing the buttons onto a second line.
          */}
          <table className="w-full min-w-[1080px] table-fixed text-[13px]">
            <colgroup>
              <col className="w-9" />
              <col />
              <col className="w-[96px]" />
              <col className="w-[124px]" />
              <col className="w-[104px]" />
              <col className="w-[60px]" />
              <col className="w-[92px]" />
              <col className="w-[100px]" />
              <col className="w-[300px]" />
            </colgroup>
            <thead className="tp-thead">
              <tr>
                <th className="px-3 py-2.5" />
                <th className="px-4 py-2.5">{t("Run")}</th>
                <th className="whitespace-nowrap px-2 py-2.5">{t("Status")}</th>
                <th className="whitespace-nowrap px-2 py-2.5">{t("Progress")}</th>
                <th className="whitespace-nowrap px-2 py-2.5">{t("Result")}</th>
                <th className="whitespace-nowrap px-2 py-2.5">{t("Cases")}</th>
                <th className="whitespace-nowrap px-2 py-2.5">{t("Duration")}</th>
                <th className="whitespace-nowrap px-2 py-2.5">{t("Started")}</th>
                <th className="px-2 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {groups.map(({ bucket, rows }) => {
                const open = !collapsed.has(bucket);
                return (
                  <Fragment key={bucket}>
                    <tr className="border-b border-[var(--line)] bg-[color-mix(in_oklch,var(--panel2)_58%,transparent)]">
                      <td colSpan={9} className="px-3 py-2">
                        <button
                          onClick={() => toggleGroup(bucket)}
                          className="flex items-center gap-2 text-[12px] font-semibold text-ink-900 outline-none"
                        >
                          <span
                            className={
                              "inline-block text-ink-400 transition-transform duration-150 " +
                              (open ? "rotate-90" : "")
                            }
                          >
                            ›
                          </span>
                          {t(bucket)}
                          <span className="rounded-full bg-[var(--panel)] px-1.5 py-0.5 text-[10.5px] font-medium tabular-nums text-ink-400">
                            {rows.length}
                          </span>
                        </button>
                      </td>
                    </tr>
                    {open &&
                      rows.map((r) => {
                        const running = r.status === "running" || r.status === "pending";
                        const pct = r.total_count ? (r.processed_count / r.total_count) * 100 : 0;
                        return (
                          <tr key={r.id} className="tp-row">
                            <td className="px-3 py-2.5">
                              <Checkbox checked={sel.has(r.id)} onChange={() => toggle(r.id)} />
                            </td>
                            <td className="px-4 py-3">
                              <div className="flex items-start gap-1.5">
                                <Link
                                  to={`/projects/${pid}/runs/${r.id}`}
                                  title={r.name}
                                  className="line-clamp-2 break-words font-medium text-ink-900 transition-colors hover:text-brand-700"
                                >
                                  {r.name}
                                </Link>
                                <button
                                  className="shrink-0 rounded text-ink-400 outline-none transition-colors hover:text-brand-700"
                                  title={t("Rename")}
                                  onClick={() => setRenaming({ id: r.id, value: r.name })}
                                >
                                  ✎
                                </button>
                              </div>
                              <div className="mt-0.5 font-mono text-[10.5px] text-ink-400">
                                #{r.id}
                                {r.created_by ? ` · ${r.created_by}` : ""}
                              </div>
                            </td>
                            <td className="whitespace-nowrap px-2 py-3">
                              {running ? (
                                <Badge status="running">{t(r.status)}</Badge>
                              ) : (
                                <Badge status={r.status} />
                              )}
                            </td>
                            <td className="px-2 py-3">
                              <div className="flex items-center gap-2">
                                <span className="inline-block h-1.5 w-[64px] shrink-0 overflow-hidden rounded-full bg-[var(--panel3)]">
                                  <span
                                    className="block h-full rounded-full bg-gradient-to-r from-brand-500 to-brand-700 transition-[width] duration-300"
                                    style={{ width: `${pct}%` }}
                                  />
                                </span>
                                <span className="whitespace-nowrap text-[11.5px] tabular-nums text-ink-500">
                                  {r.processed_count}/{r.total_count}
                                </span>
                              </div>
                            </td>
                            <td className="whitespace-nowrap px-2 py-3">
                              <ResultChips run={r} />
                            </td>
                            <td className="whitespace-nowrap px-2 py-3 tabular-nums text-ink-700">{r.total_count}</td>
                            <td className="whitespace-nowrap px-2 py-3 tabular-nums text-ink-700">{fmtDuration(r)}</td>
                            <td className="whitespace-nowrap px-2 py-3 text-ink-500">
                              {relTime(r.started_at ?? r.created_at)}
                            </td>
                            <td className="px-2 py-3 text-right">
                              <div className="flex items-center justify-end gap-1 whitespace-nowrap">
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() => navigate(`/projects/${pid}/runs/${r.id}`)}
                                >
                                  {t("View")}
                                </Button>
                                {running ? (
                                  <Button size="sm" variant="outline" onClick={() => cancel(r.id)}>
                                    {t("Cancel")}
                                  </Button>
                                ) : (
                                  <>
                                    {failedCount(r) > 0 && (
                                      <Button size="sm" variant="outline" onClick={() => rerun(r.id, "failing")}>
                                        {t("Re-run failed ({{n}})", { n: failedCount(r) })}
                                      </Button>
                                    )}
                                    {errorCount(r) > 0 && errorCount(r) < failedCount(r) && (
                                      <Button size="sm" variant="outline" onClick={() => rerun(r.id, "error")}>
                                        {t("Re-run errors ({{n}})", { n: errorCount(r) })}
                                      </Button>
                                    )}
                                    <Button size="sm" variant="ghost" onClick={() => rerun(r.id)}>
                                      {t("Re-run")}
                                    </Button>
                                  </>
                                )}
                                <Button size="sm" variant="ghost" onClick={() => setConfirmDel([r.id])}>
                                  {t("Delete")}
                                </Button>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
      <p className="text-[11.5px] text-ink-400">{t("{{n}} runs · grouped by start date", { n: filtered.length })}</p>

      {renaming && (
        <Modal onClose={() => setRenaming(null)} className="max-w-md">
          {(close) => (
            <Card className="space-y-4 p-5">
              <h2 className="text-base font-semibold text-ink-900">{t("Rename run")}</h2>
              <Input
                autoFocus
                value={renaming.value}
                onChange={(e) => setRenaming((m) => (m ? { ...m, value: e.target.value } : m))}
                onKeyDown={(e) => {
                  if (e.key === "Enter") saveRename(close);
                }}
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={close}>
                  {t("Cancel")}
                </Button>
                <Button onClick={() => saveRename(close)} disabled={renameSaving}>
                  {renameSaving ? t("Saving…") : t("Save changes")}
                </Button>
              </div>
            </Card>
          )}
        </Modal>
      )}

      {confirmDel && (
        <Modal onClose={() => setConfirmDel(null)} className="max-w-md">
          {(close) => (
            <Card className="space-y-4 p-5">
              <h2 className="text-base font-semibold text-ink-900">{t("Delete run(s)?")}</h2>
              <p className="text-sm text-ink-500">
                {t("This permanently removes {{n}} run(s) and their results. This cannot be undone.", {
                  n: confirmDel.length,
                })}
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
