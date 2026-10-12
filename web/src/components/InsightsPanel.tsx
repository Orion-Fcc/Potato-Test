import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  KeyRound,
  Plug,
  ScrollText,
  ShieldAlert,
} from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { api, type ProjectInsights } from "../lib/api";
import { Badge, Card } from "./ui";
import { useErrorToast } from "./toast";

/** 用例编号 → 用例页（只显示这几条）。
 *
 *  为什么用链接而不是"点一下展开列表"：用户看到 `TP-ACFG-016` 时想做的事
 *  是**去看那条用例**（它的步骤、预期、上次结果），而不是看更多编号。
 *  链接可以直接分享和收藏，也不需要在这一页再实现一遍用例详情。
 */
function CaseLinks({ pid, keys, limit = 12 }: { pid: number; keys: string[]; limit?: number }) {
  const { t } = useTranslation();
  if (!keys.length) return null;
  const shown = keys.slice(0, limit);
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {shown.map((k) => (
        <Link
          key={k}
          to={`/projects/${pid}/cases?keys=${encodeURIComponent(k)}`}
          className="rounded bg-[var(--panel2)] px-1.5 py-0.5 text-[10px] text-brand-700 hover:bg-[var(--line)]"
          title={t("看这条用例")}
        >
          {k}
        </Link>
      ))}
      {keys.length > shown.length && (
        <Link
          to={`/projects/${pid}/cases?keys=${encodeURIComponent(keys.join(","))}`}
          className="text-[10px] text-brand-700 hover:underline"
        >
          {t("全部 {{n}} 条", { n: keys.length })}
        </Link>
      )}
    </span>
  );
}

/** 项目概况面板（2026-10-12）。
 *
 *  ## 与「项目文档」的分工（这是这一块存在的理由）
 *
 *  项目文档是 AI **写的文字**（会变、可能错，所以每节带"依据章节"）；
 *  这里是**从库里算出来的事实**（需求→用例的映射是查出来的，账号健不健康是表里记的）。
 *  所以它一个 LLM 调用都不发 —— 秒出、免费、永远可用。
 *
 *  ## 一条界面上的诚实要求
 *
 *  **每个百分比旁边都要能看出样本量**。1131 条用例只引用 18 个需求 ID，
 *  其中一个 ID 被 357 条引用 —— "覆盖 100%"这个说法在两种粒度下含义完全不同。
 *  把分母藏起来，用户会拿着一个漂亮数字去做决定。
 */
export function InsightsPanel({ pid }: { pid: number }) {
  const fail = useErrorToast();
  const [data, setData] = useState<ProjectInsights | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .projectInsights(pid)
      .then((r) => alive && setData(r))
      .catch((e) => alive && fail(e));
    return () => {
      alive = false;
    };
  }, [pid, fail]);

  if (!data) return null;

  return (
    <div className="space-y-3">
      <RequirementMatrix data={data} pid={pid} />
      <ModuleTable data={data} />
      <RulesPanel data={data} pid={pid} />
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <ApiPanel data={data} />
        <CredentialPanel data={data} />
      </div>
      <FailurePanel data={data} pid={pid} />
      <ChangePanel data={data} pid={pid} />
    </div>
  );
}

/** 可折叠容器。默认收起：这些是"需要时查"的东西，全展开会把项目文档挤到屏幕外。 */
function Fold({
  title,
  hint,
  defaultOpen = false,
  children,
  icon: Icon,
}: {
  title: string;
  hint?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
  icon?: typeof Plug;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Card className="overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-[var(--panel2)]"
      >
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-ink-400" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-ink-400" />
        )}
        {Icon && <Icon className="h-4 w-4 shrink-0 text-ink-500" />}
        <span className="text-[13px] font-medium text-ink-800">{title}</span>
        {hint && <span className="text-[11px] text-ink-400">{hint}</span>}
      </button>
      {open && <div className="border-t border-[var(--line)] px-3 py-2.5">{children}</div>}
    </Card>
  );
}

function pct(v: number | null) {
  if (v == null) return <span className="text-ink-400">—</span>;
  const cls = v >= 90 ? "text-[var(--ok-fg)]" : v >= 60 ? "text-[var(--warn-fg)]" : "text-[var(--bad-fg)]";
  return <span className={cls}>{v}%</span>;
}

/** 需求覆盖矩阵（主线一）。 */
function RequirementMatrix({ data, pid }: { data: ProjectInsights; pid: number }) {
  const { t } = useTranslation();
  const s = data.requirement_summary;
  const [all, setAll] = useState(false);
  const rows = all ? data.requirements : data.requirements.slice(0, 8);
  return (
    <Fold
      title={t("需求覆盖矩阵")}
      icon={ShieldAlert}
      hint={t("{{c}}/{{n}} 条需求有用例引用；{{r}}/{{n2}} 条用例填了需求 ID", {
        c: s.covered,
        n: s.total,
        r: s.cases_with_refs,
        n2: s.cases_total,
      })}
    >
      {s.total === 0 ? (
        <p className="text-[12px] text-ink-400">
          {t("用例里没有填需求 ID（references），所以无法建立需求追溯。")}
        </p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="tp-table w-full text-[12px]">
              <thead>
                <tr>
                  <th className="text-left">{t("需求 ID")}</th>
                  <th className="text-left">{t("需求原文（摘自需规）")}</th>
                  <th className="text-right">{t("用例数")}</th>
                  <th className="text-right">{t("人工确认通过率")}</th>
                  <th className="text-right">{t("自动判定")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.req_id} className={r.uncovered ? "bg-[var(--bad-bg)]" : ""}>
                    <td className="whitespace-nowrap align-top">
                      <code className="text-[11px]">{r.req_id}</code>
                    </td>
                    <td className="max-w-[460px] align-top text-ink-600">
                      {r.excerpt ? (
                        <span title={r.excerpt}>{r.excerpt.slice(0, 130)}</span>
                      ) : (
                        <span className="text-ink-400">{t("（需规里找不到这条 ID 的原文）")}</span>
                      )}
                    </td>
                    <td className="text-right align-top tabular-nums">
                      {r.uncovered ? (
                        <span className="font-medium text-[var(--bad-fg)]">{t("0 · 没人测")}</span>
                      ) : (
                        // ★ 按**需求 ID** 筛，而不是把 357 个编号塞进查询串。
                        // 实测枚举 keys 的链接有 6000 字符：没法分享、可能被截断，
                        // 而且看不出筛了什么。`?refs=XXX` 短且自解释。
                        <Link
                          to={`/projects/${pid}/cases?refs=${encodeURIComponent(r.req_id)}`}
                          className="text-brand-700 hover:underline"
                          title={t("看这 {{n}} 条用例", { n: r.case_count })}
                        >
                          {r.case_count}
                        </Link>
                      )}
                    </td>
                    <td className="text-right align-top tabular-nums">
                      {pct(r.verified_rate)}
                      {r.verified_total > 0 && (
                        <span className="ml-1 text-[10px] text-ink-400">({r.verified_total})</span>
                      )}
                    </td>
                    <td className="text-right align-top tabular-nums text-ink-400">
                      {r.auto_rate == null ? "—" : `${r.auto_rate}% (${r.auto_total})`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.requirements.length > 8 && (
            <button
              className="mt-1.5 text-[12px] text-brand-700 hover:underline"
              onClick={() => setAll((v) => !v)}
            >
              {all ? t("收起") : t("展开全部 {{n}} 条", { n: data.requirements.length })}
            </button>
          )}
          {/* 追溯粒度必须说清楚：一个 ID 被 357 条用例引用，
              意思是"这 357 条都声称与它相关"，不等于需求被细致覆盖了。 */}
          {s.top_req && (
            <p className="mt-2 text-[11px] text-ink-400">
              {t(
                "注意粒度：{{req}} 被 {{n}} 条用例引用 —— 多数用例只挂这一个需求 ID，所以这个矩阵适合看「有没有」，不适合看「覆盖得多细」。",
                { req: s.top_req, n: s.top_req_cases },
              )}
            </p>
          )}
        </>
      )}
    </Fold>
  );
}

/** 功能模块覆盖（主线二）。 */
function ModuleTable({ data }: { data: ProjectInsights }) {
  const { t } = useTranslation();
  return (
    <Fold
      title={t("功能模块覆盖")}
      icon={ScrollText}
      hint={t("{{n}} 个模块", { n: data.modules.length })}
    >
      <div className="overflow-x-auto">
        <table className="tp-table w-full text-[12px]">
          <thead>
            <tr>
              <th className="text-left">{t("模块")}</th>
              <th className="text-right">{t("用例")}</th>
              <th className="text-right">{t("未填需求ID")}</th>
              <th className="text-right">{t("人工确认")}</th>
              <th className="text-right">{t("确认通过率")}</th>
              <th className="text-right">{t("待确认")}</th>
            </tr>
          </thead>
          <tbody>
            {data.modules.map((m) => (
              <tr key={m.module}>
                <td className="max-w-[260px] truncate" title={m.module}>
                  {m.module}
                </td>
                <td className="text-right tabular-nums">{m.case_total}</td>
                <td className="text-right tabular-nums text-ink-400">{m.no_refs || "—"}</td>
                <td className="text-right tabular-nums">{m.verified_total || "—"}</td>
                <td className="text-right tabular-nums">{pct(m.verified_rate)}</td>
                <td className="text-right tabular-nums text-ink-400">{m.awaiting_review || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Fold>
  );
}

function RulesPanel({ data, pid }: { data: ProjectInsights; pid: number }) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const rows = q
    ? data.rules.filter(
        (r) => r.req_id.toLowerCase().includes(q.toLowerCase()) || r.text.includes(q),
      )
    : data.rules;
  return (
    <Fold
      title={t("业务规则清单")}
      icon={ScrollText}
      hint={t("{{n}} 条 · 每条后面是验证它的用例", { n: data.rules.length })}
    >
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder={t("筛选规则…")}
        className="mb-2 h-8 w-full max-w-[280px] rounded border border-[var(--line)] bg-[var(--panel)] px-2 text-[12px]"
      />
      <div className="max-h-[340px] space-y-1.5 overflow-y-auto">
        {rows.slice(0, 120).map((r) => (
          <div key={r.req_id} className="rounded border border-[var(--line)] px-2 py-1.5">
            <div className="flex flex-wrap items-center gap-1.5">
              <code className="text-[11px] text-ink-700">{r.req_id}</code>
              {r.case_count === 0 ? (
                <span className="text-[10px] text-[var(--bad-fg)]">{t("没有用例验证它")}</span>
              ) : (
                <span className="text-[10px] text-ink-400">
                  {t("{{n}} 条用例", { n: r.case_count })}
                </span>
              )}
              {r.heading && <span className="text-[10px] text-ink-300">§ {r.heading}</span>}
            </div>
            <p className="mt-0.5 text-[12px] text-ink-600">{r.text}</p>
            {r.cases.length > 0 && (
              <div className="mt-0.5">
                <CaseLinks pid={pid} keys={r.cases} />
              </div>
            )}
          </div>
        ))}
      </div>
    </Fold>
  );
}

function ApiPanel({ data }: { data: ProjectInsights }) {
  const { t } = useTranslation();
  const writes = data.apis.filter((a) => a.writable).length;
  return (
    <Fold
      title={t("接口资产清单")}
      icon={Plug}
      hint={t("{{n}} 个（从页面流量学到）· {{w}} 个写接口", { n: data.apis.length, w: writes })}
    >
      <div className="max-h-[280px] overflow-y-auto">
        <table className="tp-table w-full text-[11px]">
          <tbody>
            {data.apis.map((a, i) => (
              <tr key={i}>
                <td className="w-14">
                  <span
                    className={
                      a.writable ? "font-medium text-[var(--bad-fg)]" : "text-ink-400"
                    }
                  >
                    {a.method}
                  </span>
                </td>
                <td className="max-w-[240px] truncate" title={a.path}>
                  {a.path}
                </td>
                <td className="text-right tabular-nums text-ink-400">{a.hits}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {writes > 0 && (
        <p className="mt-1.5 text-[10px] text-[var(--bad-fg)]">
          {t("红色是写接口：探索时只读，写操作必须显式声明。")}
        </p>
      )}
    </Fold>
  );
}

function CredentialPanel({ data }: { data: ProjectInsights }) {
  const { t } = useTranslation();
  const bad = data.credentials.filter((c) => c.is_active && !c.healthy).length;
  return (
    <Fold
      title={t("测试账号状态")}
      icon={KeyRound}
      hint={
        bad
          ? t("{{n}} 个不可用（共 {{m}} 个）", { n: bad, m: data.credentials.length })
          : t("{{n}} 个都可用", { n: data.credentials.length })
      }
    >
      {data.credentials.length === 0 ? (
        <p className="text-[12px] text-ink-400">{t("还没有配账号。")}</p>
      ) : (
        <div className="max-h-[280px] space-y-1 overflow-y-auto">
          {data.credentials.map((c) => (
            <div key={c.id} className="flex items-start gap-2 text-[12px]">
              <span
                className={
                  "mt-1.5 h-2 w-2 shrink-0 rounded-full " +
                  (c.healthy ? "bg-[var(--ok-fg)]" : "bg-[var(--bad-fg)]")
                }
              />
              <div className="min-w-0 flex-1">
                <span className="font-medium text-ink-700">{c.role || c.label}</span>
                {!c.is_active && <span className="ml-1 text-[10px] text-ink-400">{t("已停用")}</span>}
                {!c.healthy && c.last_error && (
                  <p className="truncate text-[10px] text-ink-400" title={c.last_error}>
                    {c.last_error}
                  </p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {/* 环境问题是失败归因里最大的一类，所以这块要能直接对上"该去修什么" */}
      {bad > 0 && (
        <p className="mt-1.5 text-[10px] text-ink-400">
          {t("环境/账号问题是失败归因里最大的一类 —— 先修这里比改用例更有效。")}
        </p>
      )}
    </Fold>
  );
}

function FailurePanel({ data, pid }: { data: ProjectInsights; pid: number }) {
  const { t } = useTranslation();
  const f = data.failures;
  return (
    <Fold
      title={t("失败台账")}
      icon={AlertTriangle}
      hint={t("{{n}} 条看起来不是产品缺陷", { n: f.suspect_total })}
    >
      <div className="mb-2 flex flex-wrap gap-1.5">
        {f.by_attribution.map((a) => (
          <Badge
            key={a.attribution}
            status={
              a.attribution === "system" || a.attribution === "product_defect"
                ? "failed"
                : "unknown"
            }
          >
            {a.attribution} {a.count}
          </Badge>
        ))}
      </div>
      <div className="max-h-[300px] space-y-1 overflow-y-auto">
        {f.suspects.slice(0, 60).map((s) => (
          <div key={s.result_id} className="rounded border border-[var(--line)] px-2 py-1">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
              {s.case_key ? (
                <Link
                  to={`/projects/${pid}/cases?keys=${encodeURIComponent(s.case_key)}`}
                  className="rounded bg-[var(--panel2)] px-1.5 py-0.5 text-brand-700 hover:bg-[var(--line)]"
                >
                  {s.case_key}
                </Link>
              ) : (
                <code className="text-ink-700">—</code>
              )}
              {s.overridden_to && (
                <span className="rounded bg-[var(--ok-bg)] px-1 text-[10px] text-[var(--ok-fg)]">
                  {t("人工改判为 {{v}}", { v: s.overridden_to })}
                </span>
              )}
              {s.attribution && (
                <span className="text-[10px] text-ink-400">{s.attribution}</span>
              )}
              <span className="truncate text-ink-600">{s.case_name}</span>
            </div>
            {s.reason && (
              <p className="mt-0.5 truncate text-[10px] text-ink-400" title={s.reason}>
                {s.reason}
              </p>
            )}
          </div>
        ))}
      </div>
    </Fold>
  );
}

function ChangePanel({ data, pid }: { data: ProjectInsights; pid: number }) {
  const { t } = useTranslation();
  return (
    <Fold
      title={t("用例变更历史")}
      icon={ScrollText}
      hint={t("最近 {{n}} 条", { n: data.changes.length })}
    >
      {data.changes.length === 0 ? (
        <p className="text-[12px] text-ink-400">{t("还没有变更记录。")}</p>
      ) : (
        <div className="max-h-[300px] space-y-1 overflow-y-auto">
          {data.changes.map((c) => (
            <div key={c.id} className="text-[11px]">
              <div className="flex flex-wrap items-center gap-1.5">
                {c.case_key ? (
                  <Link
                    to={`/projects/${pid}/cases?keys=${encodeURIComponent(c.case_key)}`}
                    className="rounded bg-[var(--panel2)] px-1.5 py-0.5 text-brand-700 hover:bg-[var(--line)]"
                  >
                    {c.case_key}
                  </Link>
                ) : (
                  <code className="text-ink-700">—</code>
                )}
                <span className="font-medium text-ink-600">{c.field}</span>
                {c.source && <span className="text-[10px] text-ink-400">{c.source}</span>}
                <span className="text-[10px] text-ink-300">{c.created_at}</span>
              </div>
              {c.before !== c.after && (
                <p className="truncate text-[10px] text-ink-400">
                  <span className="line-through">{c.before.slice(0, 60)}</span>
                  {" → "}
                  <span className="text-ink-600">{c.after.slice(0, 60)}</span>
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </Fold>
  );
}
