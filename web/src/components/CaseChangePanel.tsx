import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";

import { Button, Card } from "./ui";
import { Empty, PageSkeleton } from "./feedback";
import { api, relTime, type CaseChange } from "../lib/api";
import { isAbort, useAbortSignal } from "../lib/hooks";

/**
 * 用例改动审计（case_change 表）。
 *
 * 谁、什么时候、把哪条用例的哪个字段从什么改成了什么 —— 都记在这。
 * 内置助手改用例（MCP 的 update_case / 界面助手）带 source=assistant 标记，
 * 这里把它单独染成红色：AI 改的断言天然可疑，人扫一眼就知道该重点复核。
 *
 * 只读面板：只展示，不改数据。数据来源 GET /projects/{pid}/case-changes（已存在，
 * 后端一行不动）。limit 默认 30，和 api.getCaseChanges 的默认一致。
 */
const SOURCE_TONE: Record<string, string> = {
  assistant: "bg-red-500/15 text-red-700 dark:text-red-300",
  human: "bg-slate-500/15 text-slate-700 dark:text-slate-300",
  import: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
};

/** before/after 都是字符串（后端已序列化）。多行就换行展示，别塞成一坨。 */
function Diff({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="w-10 shrink-0 text-ink-400">{label}</span>
      <span className="min-w-0 whitespace-pre-wrap break-words text-ink-700 dark:text-ink-300">
        {value.trim() === "" ? <span className="text-ink-300">(空)</span> : value}
      </span>
    </div>
  );
}

export function CaseChangePanel({ limit = 30 }: { limit?: number }) {
  const { t } = useTranslation();
  const pid = Number(useParams().pid);
  const signal = useAbortSignal();
  const [rows, setRows] = useState<CaseChange[] | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const load = () => {
    setLoading(true);
    setErr("");
    api
      .getCaseChanges(pid, limit, { signal })
      .then((d) => {
        setRows(d);
        setLoading(false);
      })
      .catch((e) => {
        if (!isAbort(e)) {
          setErr(String(e));
          setLoading(false);
        }
      });
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, signal, limit]);

  if (err) return <div className="tp-alert tp-alert-bad">{err}</div>;
  if (!rows) return <PageSkeleton />;

  const aiCount = rows.filter((r) => r.source === "assistant").length;

  return (
    <Card>
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-base font-medium">{t("用例改动记录")}</h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">
              {t("最近 {{n}} 条", { n: limit })}
              {aiCount > 0 ? ` · ${t("{{n}} 条由助手(AI)改动", { n: aiCount })}` : ""}
            </span>
            <Button size="sm" variant="ghost" disabled={loading} onClick={load}>
              {loading ? t("Loading…") : t("Refresh")}
            </Button>
          </div>
        </div>

        {rows.length === 0 ? (
          <Empty
            title={t("还没有用例改动")}
            sub={t("助手或人工改过用例字段后，改动会带审计留痕显示在这里。")}
          />
        ) : (
          <div className="divide-y divide-[var(--line)]">
            {rows.map((r) => (
              <div key={r.id} className="py-2.5">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs">
                  <span
                    className={`rounded px-1.5 py-0.5 font-medium ${
                      SOURCE_TONE[r.source] ?? SOURCE_TONE.human
                    }`}
                  >
                    {r.source}
                  </span>
                  <span className="font-mono text-ink-500">
                    case#{r.case_id}
                  </span>
                  <span className="font-medium text-ink-900 dark:text-ink-100">{r.field}</span>
                  <span className="ml-auto text-[11px] text-ink-400">
                    {r.by_label ? `${r.by_label} · ` : ""}
                    {relTime(r.created_at)}
                  </span>
                </div>
                <div className="space-y-0.5">
                  <Diff label={t("改前")} value={r.before} />
                  <Diff label={t("改后")} value={r.after} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}
