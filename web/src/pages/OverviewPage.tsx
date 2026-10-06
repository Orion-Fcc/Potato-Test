import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, relTime, type ProjectStats } from "../lib/api";
import { fmtMs, pct, rateColor, rateTone } from "../lib/metrics";
import { isAbort, useAbortSignal } from "../lib/hooks";
import { Badge, Button, Card, PageHeader, Stat, StatRing } from "../components/ui";
import { chartTooltip } from "../components/table";
import { Empty, PageSkeleton } from "../components/feedback";
import { SetupChecklist } from "../components/SetupChecklist";
import { FailureDigestPanel } from "../components/FailureDigestPanel";

export function OverviewPage() {
  const { t } = useTranslation();
  const pid = Number(useParams().pid);
  const signal = useAbortSignal();
  const [stats, setStats] = useState<ProjectStats | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .getStats(pid, { signal })
      .then(setStats)
      .catch((e) => {
        if (!isAbort(e)) setErr(String(e));
      });
  }, [pid, signal]);

  if (err) return <div className="tp-alert tp-alert-bad">{err}</div>;
  if (!stats) return <PageSkeleton />;

  const trendData = stats.trend.map((t, i) => ({
    x: t.name?.slice(0, 12) || `#${t.run_id}`,
    pass: Math.round(t.pass_rate * 100),
    i,
  }));

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("Project")}
        title={t("Overview")}
        subtitle={t("Suite health at a glance.")}
      />

      {/* 失败清单放在概览页顶部：它是"现在该做什么"的入口，
          埋在下面会被 KPI 图表推到视线之外。 */}
      <FailureDigestPanel />
      <SetupChecklist pid={pid} stats={stats} />

      <div className="tp-rise grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label={t("Test cases")}
          value={`${stats.enabled_count}/${stats.case_count}`}
          sub={t("enabled / total")}
        />
        <Stat
          label={t("Last pass rate")}
          value={pct(stats.last_pass_rate)}
          sub={
            stats.last_pass_rate == null
              ? t("no completed run yet")
              : rateTone(stats.last_pass_rate) === "ok"
                ? t("healthy")
                : rateTone(stats.last_pass_rate) === "warn"
                  ? t("needs attention")
                  : t("failing")
          }
          tone={rateTone(stats.last_pass_rate)}
          right={
            stats.last_pass_rate != null ? (
              <StatRing pct={stats.last_pass_rate * 100} size={46} thickness={4} color={rateColor(stats.last_pass_rate)} />
            ) : undefined
          }
        />
        <Stat
          label={t("Last p50 latency")}
          value={fmtMs(stats.last_latency_p50_ms)}
          sub={t("median step time")}
        />
        <Stat
          label={t("Flaky (last run)")}
          value={String(stats.last_flaky)}
          sub={stats.last_flaky > 0 ? t("passed only after retry") : t("nothing flaky")}
          tone={stats.last_flaky > 0 ? "warn" : undefined}
        />
      </div>

      {stats.run_count === 0 ? (
        <Empty
          title={t("No runs yet")}
          sub={t("Add test cases, then run them to see pass-rate trends and suite health here.")}
          action={
            <Link to={`/projects/${pid}/cases`}>
              <Button size="sm">{t("Go to Test Cases →")}</Button>
            </Link>
          }
        />
      ) : (
        <>
          <Card className="p-5">
            <div className="mb-4 flex items-baseline justify-between">
              <div>
                <div className="text-[13px] font-semibold text-ink-900">{t("Pass rate over runs")}</div>
                <div className="mt-0.5 text-[11px] text-ink-400">{t("{{n}} completed runs", { n: trendData.length })}</div>
              </div>
              <div className="flex items-center gap-1.5 text-[11px] text-ink-400">
                <span className="h-0.5 w-3.5 rounded-full bg-gradient-to-r from-brand-500 to-brand-700" />
                {t("pass rate")}
              </div>
            </div>
            {trendData.length >= 2 ? (
              <ResponsiveContainer width="100%" height={224}>
                <LineChart data={trendData} margin={{ top: 8, right: 12, bottom: 0, left: -16 }}>
                  <defs>
                    <linearGradient id="tp-pass-stroke" x1="0" y1="0" x2="1" y2="0">
                      <stop offset="0%" stopColor="var(--brand-500)" />
                      <stop offset="100%" stopColor="var(--brand-700)" />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 4" stroke="var(--line)" vertical={false} />
                  <XAxis
                    dataKey="x"
                    tick={{ fontSize: 11, fill: "var(--muted)" }}
                    tickLine={false}
                    axisLine={{ stroke: "var(--line)" }}
                  />
                  <YAxis
                    domain={[0, 100]}
                    tickFormatter={(v) => `${v}%`}
                    tick={{ fontSize: 11, fill: "var(--muted)" }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <Tooltip formatter={(v) => [`${v}%`, "pass rate"]} {...chartTooltip} />
                  <Line
                    type="monotone"
                    dataKey="pass"
                    stroke="url(#tp-pass-stroke)"
                    strokeWidth={2.25}
                    dot={{ r: 3, strokeWidth: 0, fill: "var(--brand-600)" }}
                    activeDot={{ r: 5, strokeWidth: 2, stroke: "var(--panel)", fill: "var(--brand-600)" }}
                  />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="py-10 text-center text-sm text-ink-500">
                {t("Need at least two completed runs to draw a trend.")}
              </div>
            )}
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="p-5">
              <div className="mb-3 flex items-center justify-between">
                <div className="text-[13px] font-semibold text-ink-900">{t("Recent runs")}</div>
                <Link
                  to={`/projects/${pid}/runs`}
                  className="text-[12px] font-medium text-brand-700 hover:underline"
                >
                  {t("View all")}
                </Link>
              </div>
              {stats.last_run ? (
                <div className="divide-y divide-[var(--line)]">
                  {[stats.last_run].map((r) => (
                    <Link
                      key={r.id}
                      to={`/projects/${pid}/runs/${r.id}`}
                      className="-mx-2 flex items-center justify-between gap-3 rounded-lg px-2 py-2.5 text-[13px] transition-colors hover:bg-[var(--hover)]"
                    >
                      <span className="truncate text-ink-900">{r.name}</span>
                      <span className="flex shrink-0 items-center gap-3">
                        <span className="tabular-nums font-medium text-ink-700">
                          {r.summary ? pct(r.summary.pass_rate) : `${r.processed_count}/${r.total_count}`}
                        </span>
                        <Badge status={r.status} />
                        <span className="text-[11px] text-ink-400">{relTime(r.started_at)}</span>
                      </span>
                    </Link>
                  ))}
                </div>
              ) : (
                <div className="py-6 text-center text-sm text-ink-500">{t("No runs.")}</div>
              )}
            </Card>

            <Card className="p-5">
              <div className="mb-3 text-[13px] font-semibold text-ink-900">{t("Top failing cases")}</div>
              {stats.top_failing.length === 0 ? (
                <div className="py-6 text-center text-sm text-ink-500">{t("Nothing failing. Nice.")}</div>
              ) : (
                <div className="divide-y divide-[var(--line)]">
                  {stats.top_failing.map((c) => (
                    <div key={c.case_id} className="flex items-center justify-between gap-3 py-2.5 text-[13px]">
                      <span className="truncate text-ink-900">{c.name}</span>
                      <span className="shrink-0 rounded-full border border-transparent bg-[var(--bad-bg)] px-2 py-0.5 text-[11px] font-medium tabular-nums text-[var(--bad-fg)]">
                        {t("{{n}} fails", { n: c.fail_count })}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

