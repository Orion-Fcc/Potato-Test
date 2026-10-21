import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Area,
  AreaChart,
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
type Metric = "total" | "prompt" | "completion";

// 只用设计令牌，不写死颜色（深色/浅色主题都跟着走）。
const ROLE_COLORS = [
  "var(--brand-500)",
  "var(--brand-600)",
  "var(--brand-700)",
  "var(--ok)",
  "var(--warn)",
  "var(--bad-fg)",
  "var(--muted)",
];

export function LlmUsagePage() {
  const { t } = useTranslation();
  const signal = useAbortSignal();
  const [days, setDays] = useState<number>(30);
  const [role, setRole] = useState("");
  const [model, setModel] = useState("");
  const [metric, setMetric] = useState<Metric>("total");
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

  const daily = report.series.map((d) => ({ ...d, label: d.date.slice(5) }));
  const roleRows = Object.entries(report.by_role)
    .map(([key, v]) => ({ key, label: report.role_labels[key] || key, total: v.total }))
    .sort((a, b) => b.total - a.total);
  const roleChartData = [
    { name: t("Tokens"), ...Object.fromEntries(roleRows.map((r) => [r.key, r.total])) },
  ];
  const modelRows = Object.entries(report.by_model)
    .map(([name, v]) => ({ name, total: v.total, calls: v.calls }))
    .sort((a, b) => b.total - a.total);
  const costText = report.unpriced ? t("Not priced") : `¥${report.totals.cost.toFixed(2)}`;

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("AI usage")}
        subtitle={t("Token consumption and estimated cost by day, role and model.")}
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

      <div className="tp-rise grid gap-4 sm:grid-cols-3">
        <Stat label={t("Total tokens")} value={fmt(report.totals.total)} />
        <Stat label={t("Total calls")} value={fmt(report.totals.calls)} />
        <Stat
          label={t("Estimated cost")}
          value={costText}
          tone={report.unpriced ? "warn" : undefined}
          sub={
            report.unpriced
              ? t("Set prices in the model pool")
              : t("Estimated from manually configured prices")
          }
        />
      </div>

      <div className="rounded-lg border border-[var(--line)] bg-[var(--panel2)] px-3 py-2 text-[11px] text-ink-500">
        {t("Costs are estimated from the prices you enter manually in the model pool (¥ per million tokens). They are not measured bill amounts.")}
        {report.unpriced
          ? ` ${t("Every model that has no price is left out of the cost total.")}`
          : ""}
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
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-[13px] font-medium text-ink-900">{t("Daily consumption")}</div>
              <div className="flex items-center gap-1.5">
                {(["total", "prompt", "completion"] as Metric[]).map((m) => (
                  <Button
                    key={m}
                    variant={metric === m ? "primary" : "ghost"}
                    className="h-7 text-[11px]"
                    onClick={() => setMetric(m)}
                  >
                    {m === "total" ? t("Total") : m === "prompt" ? t("Input") : t("Output")}
                  </Button>
                ))}
              </div>
            </div>
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={daily} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
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
                <Area
                  type="monotone"
                  dataKey={metric}
                  stroke="var(--brand-600)"
                  fill="var(--brand-500)"
                  fillOpacity={0.18}
                  strokeWidth={2}
                  dot={false}
                />
              </AreaChart>
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
              <BarChart data={roleChartData} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="name"
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
                {roleRows.map((r, i) => (
                  <Bar
                    key={r.key}
                    dataKey={r.key}
                    name={r.label}
                    stackId="role"
                    fill={ROLE_COLORS[i % ROLE_COLORS.length]}
                  />
                ))}
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
