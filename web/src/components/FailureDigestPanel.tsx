import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";

import { Button, Card } from "./ui";
import { Empty, PageSkeleton } from "./feedback";
import { api, relTime, type FailureDigest } from "../lib/api";
import { isAbort, useAbortSignal } from "../lib/hooks";

/**
 * 动作 → 徽章配色。语义固定：提缺陷=红（最该被人看到）、改用例=蓝、其余中性。
 * 不用 Badge 组件：它的取色是按状态（ok/bad/…），与"该做什么动作"不是一回事，
 * 两套语义混在一处会出现"改用例"被涂成红色的怪事。
 */
const ACTION_TONE_ZH: Record<string, string> = {
  report_to_dev: "bg-red-500/15 text-red-700 dark:text-red-300",
  fix_case: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  fix_data: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  fix_env: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  rerun: "bg-slate-500/15 text-slate-700 dark:text-slate-300",
  inspect: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
};

/**
 * 本项目当前未通过用例的失败清单。
 *
 * 清单是**实时算出来的派生视图** —— 用例一旦通过就自动从这里消失，
 * 不需要任何手工维护（用户 2026-10-06 明确要求这一点）。
 *
 * 合并规则由后端负责（同因才合并，分不清的单列）。这里刻意**不做**任何
 * "按标题相似度"的二次合并 —— 合并错比不合并不糟得多：人会照着清单
 * 去查一个根本不存在的问题。
 */
export function FailureDigestPanel() {
  const { t } = useTranslation();
  const pid = Number(useParams().pid);
  const signal = useAbortSignal();
  const [data, setData] = useState<FailureDigest | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = (backfill: boolean) => {
    setLoading(true);
    setErr("");
    api
      .getFailureDigest(pid, backfill, { signal })
      .then((d) => {
        setData(d);
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
    // 首屏不做 LLM 回填：那个调用可能十几秒且消耗网关额度，页面不能干等。
    // 先给未回填的清单，回填由用户点按钮触发 —— 也符合"省着用"的规矩。
    load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, signal]);

  if (err) return <div className="tp-alert tp-alert-bad">{err}</div>;
  if (!data) return <PageSkeleton />;

  return (
    <Card>
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-base font-medium">{t("失败清单")}</h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">
              {t("未通过")} {data.case_count} · {t("归为")} {data.group_count} {t("类原因")}
              {data.uncategorized > 0 ? ` · ${data.uncategorized} ${t("条待人工确认")}` : ""}
            </span>
            <Button size="sm" variant="ghost" disabled={loading} onClick={() => load(true)}>
              {loading ? t("正在补分类…") : t("补全分类")}
            </Button>
          </div>
        </div>

        {data.case_count === 0 ? (
          <Empty title={t("没有未通过的用例")} sub={data.note || undefined} />
        ) : (
          <>
            {data.groups.map((g) => {
              const key = g.signal;
              const open = !!expanded[key];
              return (
                <div
                  key={key}
                  className="rounded-lg border border-[var(--line)] overflow-hidden"
                >
                  <button
                    type="button"
                    className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-[var(--line-faint)]"
                    onClick={() => setExpanded((s) => ({ ...s, [key]: !open }))}
                  >
                    <span
                      className={`mt-0.5 shrink-0 rounded px-2 py-0.5 text-xs font-medium ${
                        ACTION_TONE_ZH[g.action] ?? ACTION_TONE_ZH.inspect
                      }`}
                    >
                      {g.action_label}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm">{g.summary}</span>
                      <span className="mt-0.5 block text-xs text-slate-500">
                        {t("涉及")} {g.case_count} {t("条用例")}
                        {g.result_count > g.case_count
                          ? `（${t("共")} ${g.result_count} ${t("次失败")}）`
                          : ""}
                        {g.latest_at ? ` · ${relTime(g.latest_at)}` : ""}
                        {g.cause_conflict ? ` · ${t("根因冲突，需人工判断")}` : ""}
                      </span>
                    </span>
                    <span className="shrink-0 text-xs text-slate-400">{open ? "−" : "+"}</span>
                  </button>

                  {open ? (
                    <div className="border-t border-[var(--line)] px-4 py-2">
                      {g.cause_conflict ? (
                        <div className="tp-alert tp-alert-warn mb-2 text-xs">
                          {t("同一组里判定器给出了不同根因")}：{g.causes_seen.join("、")}。
                          {t("已按人工确认处理，不要按多数决定动作。")}
                        </div>
                      ) : null}
                      <ul className="space-y-1.5">
                        {g.cases.map((c) => (
                          <li key={c.case_id} className="text-xs">
                            <span className="font-mono text-slate-500">{c.case_key}</span>{" "}
                            <span>{c.name}</span>
                            {c.fail_count > 1 ? (
                              <span className="ml-1 text-slate-400">
                                （{t("失败")} {c.fail_count} {t("次")}）
                              </span>
                            ) : null}
                            {c.latest_reason ? (
                              <div className="mt-0.5 text-slate-500">{c.latest_reason}</div>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </div>
              );
            })}

            <p className="pt-1 text-xs text-slate-500">
              {t("清单按相同原因合并；分不清原因的会单独列出，不做猜测。")}
              {t("用例通过后会自动从这里消失，不需要手工维护。")}
              {t("让内置助手按清单改用例：在助手页说「按失败清单把该改的用例改掉」即可。")}
            </p>
          </>
        )}
      </div>
    </Card>
  );
}
