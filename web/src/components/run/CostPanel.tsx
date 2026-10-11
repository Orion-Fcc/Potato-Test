import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type PerfReport } from "../../lib/api";
import { fmtTokens } from "../../lib/metrics";
import { Card } from "../ui";

/** 「这一轮的钱花在哪儿」。
 *
 *  为什么要这块面板（2026-10-11）
 *  ------------------------------
 *  用户的诉求是「token 太花钱」，而原来的报告只按条列出 token —— 面对一轮 555 条
 *  用例，那个列表回答不了唯一重要的问题：**该从哪儿下手**。
 *
 *  真库实测（45 条有 usage 的结果、470 万输入 token）拆出来是这样：
 *    · 缓存命中 67.9%，也就是说按全价计费的只有那 32%（约 150 万）；
 *    · 但成本是**高度集中**的 —— 最贵的 8 条就占了一半 token，
 *      它们无一例外是 12~33 次 LLM 往返（中位数只有 5 次）；
 *    · 每次往返的输入量各条之间差不了多少（约 1.6 万），
 *      所以"这条为什么贵"几乎总能翻译成"这条为什么跑了这么多步"。
 *
 *  所以这块面板刻意把**往返次数**放在最显眼的位置，而不是只报 token 总量：
 *  次数是人能去查、能去改的东西；"60 万 token"不是。
 *
 *  没测到就不显示（旧运行没有 usage 埋点）—— 画一个 0 会让人以为这轮不花钱。
 */
export function CostPanel({
  runId,
  active,
  caseName,
}: {
  runId: number;
  /** 跑的过程中数字一直在变，等结束再抓一次；期间只在切轮次时抓一次即可。 */
  active: boolean;
  caseName: (id: number) => string;
}) {
  const { t } = useTranslation();
  const [rep, setRep] = useState<PerfReport | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .runPerf(runId)
      .then((r) => alive && setRep(r))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [runId, active]);

  // 没有 token 账本（埋点前的历史运行，或网关不回 usage）→ 整块不渲染。
  if (!rep || !rep.token_total) return null;

  const pct = (x: number) => `${x.toFixed(0)}%`;

  return (
    <Card className="space-y-3 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-sm font-medium text-ink-900">{t("Where this run's tokens went")}</div>
        {rep.escalated_cases > 0 && (
          <div className="text-[11px] text-ink-500">
            {t("{{n}} case(s) reviewed by the escalation model, {{c}} verdict(s) changed", {
              n: rep.escalated_cases,
              c: rep.escalated_changed,
            })}
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label={t("Total tokens")} value={fmtTokens(rep.token_total)} />
        <Stat
          label={t("Cache hit")}
          value={pct(rep.cache_hit_pct)}
          hint={t("Cached input is billed far cheaper. Only the rest costs full price: {{n}} tokens.", {
            n: fmtTokens(rep.token_uncached),
          })}
        />
        <Stat
          label={t("LLM round-trips")}
          value={String(rep.token_calls)}
          hint={t("Cost is driven by the number of round-trips, not by how big each one is.")}
        />
        <Stat
          label={t("Top {{n}} share", { n: rep.cost_cases.length })}
          value={pct(rep.cost_top_share)}
          hint={t("How concentrated the bill is. High means a few cases are worth fixing.")}
        />
      </div>

      {rep.cost_cases.length > 0 && (
        <div className="space-y-1">
          <div className="text-[11px] text-ink-500">{t("Most expensive cases")}</div>
          {rep.cost_cases.map((c) => (
            <div key={c.case_id} className="flex items-center gap-2 text-xs">
              <span className="tabular-nums text-ink-500">{pct(c.share)}</span>
              <span className="w-16 shrink-0 tabular-nums text-ink-700">{fmtTokens(c.tokens)}</span>
              {/* 往返次数放最前面：它才是可行动的那一列 */}
              <span className="w-20 shrink-0 tabular-nums text-ink-700">
                {t("{{n}} calls", { n: c.calls })}
              </span>
              <span className="truncate text-ink-600" title={caseName(c.case_id)}>
                {caseName(c.case_id)}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="text-[11px] text-ink-400">
        {t("To cut cost, look for cases with many round-trips — the per-call input size is roughly the same across cases.")}
      </div>
    </Card>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-[var(--line)] px-3 py-2" title={hint}>
      <div className="text-[11px] text-ink-500">{label}</div>
      <div className="text-sm font-medium tabular-nums text-ink-900">{value}</div>
    </div>
  );
}
