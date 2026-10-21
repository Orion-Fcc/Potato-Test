import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, type LlmUsageReport } from "../lib/api";
import { isAbort, useAbortSignal } from "../lib/hooks";
import { Button, Card, PageHeader, Select, Stat } from "../components/ui";
import { chartTooltip } from "../components/table";
import { Empty, PageSkeleton } from "../components/feedback";

const RANGE_DAYS = [7, 30, 90, 0] as const;
export function LlmUsagePage() {
  const { t } = useTranslation();
  const signal = useAbortSignal();
  const [days, setDays] = useState<number>(30);
  const [role, setRole] = useState("");
  const [model, setModel] = useState("");
  const [report, setReport] = useState<LlmUsageReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  useEffect(() => {
    let live = true;
    setLoading(true);
    setErr("");
    api
      .getLlmUsage({ days, role: role || undefined, model: model || undefined, signal })
      .then((r) => {
        if (live) setReport(r);
      })
      .catch((e) => {
        if (live && !isAbort(e)) setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [days, role, model, signal]);

  const fmt = (n: number) => n.toLocaleString("zh-CN");

  if (err && !report) {
    return (
      <div className="tp-alert tp-alert-bad">
        {t("Failed to load usage data.")} {err}
      </div>
    );
  }
  if (!report) return <PageSkeleton />;

  const daily = report.series.map((d) => ({
    ...d,
    label: d.date.slice(5),
    uncached: Math.max(0, d.prompt - d.cached),
  }));
  const roleRows = Object.entries(report.by_role)
    .map(([key, v]) => ({
      key,
      label: report.role_labels[key] || key,
      cached: v.cached,
      uncached: Math.max(0, v.prompt - v.cached),
      total: v.total,
    }))
    .sort((a, b) => b.total - a.total);
  const modelRows = Object.entries(report.by_model)
    .map(([name, v]) => ({ name, total: v.total, calls: v.calls }))
    .sort((a, b) => b.total - a.total);

  // prompt = 0 时没有可比的输入量，显示「—」而不是误导性的 0%。
  const hitRate =
    report.totals.prompt > 0
      ? Math.round((report.totals.cached / report.totals.prompt) * 100)
      : null;

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("AI usage")}
        subtitle={t("Token consumption and cache hit rate by day, role and model.")}
        actions={
          <div className="flex flex-wrap items-center gap-1.5">
            {RANGE_DAYS.map((d) => (
              <Button
                key={d}
                variant={days === d ? "primary" : "ghost"}
                className="h-7 text-[11px]"
                onClick={() => setDays(d)}
              >
                {d === 0 ? t("All time") : t("Last {{n}} days", { n: d })}
              </Button>
            ))}
          </div>
        }
      />

      <div className="tp-rise grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label={t("Total tokens")} value={fmt(report.totals.total)} />
        <Stat
          label={t("Input tokens")}
          value={fmt(report.totals.prompt)}
          sub={`${t("Cached")} ${fmt(report.totals.cached)}`}
        />
        <Stat label={t("Output tokens")} value={fmt(report.totals.completion)} />
        <Stat label={t("Total calls")} value={fmt(report.totals.calls)} />
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[12px] text-ink-600">
        <span>
          {t("Cache hit rate")}：
          <span className="font-medium text-ink-900">{hitRate === null ? "—" : `${hitRate}%`}</span>
        </span>
        {report.has_missing_cached && (
          <span className="text-ink-500">· {t("Some historical data did not record cache.")}</span>
        )}
      </div>

      <Card className="space-y-3 p-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="space-y-1 text-[11px] text-ink-500">
            <span>{t("Role")}</span>
            <Select value={role} onChange={(e) => setRole(e.target.value)} className="w-44">
              <option value="">{t("All roles")}</option>
              {report.roles.map((r) => (
                <option key={r.key} value={r.key}>
                  {r.label}
                </option>
              ))}
            </Select>
          </label>
          <label className="space-y-1 text-[11px] text-ink-500">
            <span>{t("Model")}</span>
            <Select value={model} onChange={(e) => setModel(e.target.value)} className="w-52">
              <option value="">{t("All models")}</option>
              {report.models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
              {report.has_unrecorded && <option value="未记录">{t("Unrecorded")}</option>}
            </Select>
          </label>
          {loading && <span className="pb-2 text-[11px] text-ink-500">{t("Loading…")}</span>}
        </div>

        {err && <div className="tp-alert tp-alert-bad text-[11px]">{err}</div>}

        {!report.has_data ? (
          <Empty
            title={t("No usage data")}
            sub={t("Run a test and the token usage will show up here. Older runs without per-role usage are not included.")}
          />
        ) : (
          <>
            <div className="text-[13px] font-medium text-ink-900">{t("Daily consumption")}</div>
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={daily} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={{ stroke: "var(--line)" }}
                />
                <YAxis
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                  width={52}
                  tickFormatter={(v) => fmt(Number(v))}
                />
                <Tooltip {...chartTooltip} formatter={(v) => fmt(Number(v))} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="cached" name={t("Cached")} stackId="input" fill="var(--brand-500)" />
                <Bar dataKey="uncached" name={t("Uncached")} stackId="input" fill="var(--brand-700)" />
                <Bar dataKey="completion" name={t("Output")} fill="var(--muted)" />
              </BarChart>
            </ResponsiveContainer>
          </>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="space-y-3 p-4">
          <div className="text-[13px] font-medium text-ink-900">{t("By role")}</div>
          {roleRows.length === 0 ? (
            <div className="py-8 text-center text-[12px] text-ink-500">{t("No usage data")}</div>
          ) : (
            <ResponsiveContainer width="100%" height={230}>
              <BarChart
                data={roleRows}
                layout="vertical"
                margin={{ top: 6, right: 16, left: 0, bottom: 0 }}
              >
                <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" horizontal={false} />
                <XAxis
                  type="number"
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={{ stroke: "var(--line)" }}
                  tickFormatter={(v) => fmt(Number(v))}
                />
                <YAxis
                  type="category"
                  dataKey="label"
                  width={130}
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                />
                <Tooltip {...chartTooltip} formatter={(v) => fmt(Number(v))} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="cached" name={t("Cached")} stackId="role" fill="var(--brand-500)" />
                <Bar dataKey="uncached" name={t("Uncached")} stackId="role" fill="var(--brand-700)" />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Card>

        <Card className="space-y-3 p-4">
          <div className="text-[13px] font-medium text-ink-900">{t("By model")}</div>
          {modelRows.length === 0 ? (
            <div className="py-8 text-center text-[12px] text-ink-500">
              {t("No model dimension data")}
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={230}>
              <BarChart
                data={modelRows}
                layout="vertical"
                margin={{ top: 6, right: 16, left: 0, bottom: 0 }}
              >
                <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" horizontal={false} />
                <XAxis
                  type="number"
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={{ stroke: "var(--line)" }}
                  tickFormatter={(v) => fmt(Number(v))}
                />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={130}
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                />
                <Tooltip {...chartTooltip} formatter={(v) => fmt(Number(v))} />
                <Bar dataKey="total" name={t("Tokens")} fill="var(--brand-600)" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Card>
      </div>
    </div>
  );
}
