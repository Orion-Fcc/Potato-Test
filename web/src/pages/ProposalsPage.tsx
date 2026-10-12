import {
  Check,
  ChevronDown,
  ChevronRight,
  CirclePlus,
  Loader2,
  PencilLine,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import { api, type CaseProposal, type CaseProposalList } from "../lib/api";
import { Badge, Button, Card, PageHeader } from "../components/ui";
import { MermaidBlock } from "../components/MermaidBlock";
import { Empty, PageSkeleton } from "../components/feedback";
import { useErrorToast, useToast } from "../components/toast";

/** AI 用例优化提案（2026-10-12）。
 *
 *  ## 这一页的核心承诺：AI 不能自己改用例库
 *
 *  用户要的是"AI 优化/增加/删除用例"，但他选了**先出提案、逐条审阅后再入库**。
 *  这个选择是对的：用例库是这个系统真正的资产，一次静默的误删会让"通过率"
 *  从有依据变成没依据，而报告上一个异常都不会有。
 *
 *  所以这一页的界面设计有一条硬规矩：**每条提案都必须能看到理由和依据**。
 *  看不到依据的提案，用户只能凭感觉点采纳 —— 那和不审一样。
 *
 *  ## 三个动作的视觉区分
 *
 *  删（remove）用红色、改（update）用琥珀色、增（add）用绿色。
 *  不是为了好看：一屏几十条提案，用户扫的时候需要**一眼分出哪些是危险操作**。
 */
export function ProposalsPage() {
  const { t } = useTranslation();
  const { pid: pidRaw } = useParams();
  const pid = Number(pidRaw);
  const toast = useToast();
  const fail = useErrorToast();

  const [data, setData] = useState<CaseProposalList | null>(null);
  const [gen, setGen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [filter, setFilter] = useState<string>("pending");

  const load = useCallback(async () => {
    try {
      setData(await api.listCaseProposals(pid, filter || undefined));
      setSelected(new Set());
    } catch (e) {
      fail(e);
    }
  }, [pid, filter, fail]);

  useEffect(() => {
    load();
  }, [load]);

  const generate = async (replace: boolean) => {
    if (replace && !window.confirm(t("这会清掉还没决定的旧提案，重新生成。确定吗？"))) return;
    setGen(true);
    try {
      const r = await api.generateCaseProposals(pid, { replace_pending: replace });
      if (!r.ok) {
        fail(new Error(r.error || "generate failed"));
        return;
      }
      const extra = r.modules_failed
        ? t("（{{n}} 个模块没审成功：{{names}} —— 可以再点一次补齐）", {
            n: r.modules_failed,
            names: (r.failed_modules ?? []).slice(0, 3).join("、"),
          })
        : "";
      toast("success", t("AI 审完 {{m}} 个模块，新增 {{n}} 条提案", { m: r.modules_scanned ?? 0, n: r.created }) + extra);
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setGen(false);
    }
  };

  const decide = async (ids: number[], decision: "accept" | "reject") => {
    if (!ids.length) return;
    if (decision === "accept") {
      const risky = (data?.proposals ?? []).filter(
        (p) => ids.includes(p.id) && p.action === "remove",
      ).length;
      const msg = risky
        ? t("将采纳 {{n}} 条提案，其中 {{r}} 条会作废用例（可恢复）。继续？", { n: ids.length, r: risky })
        : t("将采纳 {{n}} 条提案。继续？", { n: ids.length });
      if (!window.confirm(msg)) return;
    }
    setBusy(true);
    try {
      const r = await api.decideCaseProposals(pid, ids, decision);
      if (r.skipped?.length) {
        // 没落地的必须说出来，否则用户以为全成功了
        toast(
          "info",
          t("已处理 {{ok}} 条，{{n}} 条没落地：{{why}}", {
            ok: r.applied.length,
            n: r.skipped.length,
            why: r.skipped.map((s) => s.why).join("；").slice(0, 120),
          }),
        );
      } else {
        toast("success", decision === "accept" ? t("已采纳 {{n}} 条", { n: r.applied.length }) : t("已驳回 {{n}} 条", { n: ids.length }));
      }
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (id: number) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const allPending = useMemo(
    () => (data?.proposals ?? []).filter((p) => p.status === "pending"),
    [data],
  );

  if (!data) return <PageSkeleton />;

  const ACTION_STYLE: Record<string, { cls: string; Icon: typeof CirclePlus; label: string }> = {
    add: { cls: "text-[var(--ok-fg)]", Icon: CirclePlus, label: t("补用例") },
    update: { cls: "text-[var(--warn-fg)]", Icon: PencilLine, label: t("改用例") },
    remove: { cls: "text-[var(--bad-fg)]", Icon: Trash2, label: t("删用例") },
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title={t("AI 用例优化")}
        subtitle={t(
          "AI 按「项目文档」审一遍用例库，提出 补/改/删 建议。只有你点「采纳」才会动用例库。",
        )}
        actions={
          <>
            <Button variant="outline" onClick={() => generate(true)} disabled={gen}>
              {gen ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              {t("重新生成")}
            </Button>
            <Button onClick={() => generate(false)} disabled={gen}>
              {gen ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              {t("让 AI 审一遍")}
            </Button>
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        {(
          [
            ["pending", t("待审阅")],
            ["accepted", t("已采纳")],
            ["rejected", t("已驳回")],
            ["", t("全部")],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k || "all"}
            onClick={() => setFilter(k)}
            className={
              "rounded-full px-3 py-1 text-[12px] transition-colors " +
              (filter === k
                ? "bg-brand-600 text-white"
                : "bg-[var(--panel2)] text-ink-600 hover:bg-[var(--line)]")
            }
          >
            {label}
            {k && data.counts[k as keyof typeof data.counts] != null
              ? ` ${data.counts[k as keyof typeof data.counts]}`
              : ""}
          </button>
        ))}
        {Object.entries(data.pending_by_action).length > 0 && (
          <span className="ml-1 text-[12px] text-ink-400">
            {Object.entries(data.pending_by_action)
              .map(([a, n]) => `${ACTION_STYLE[a]?.label ?? a} ${n}`)
              .join(" · ")}
          </span>
        )}
      </div>

      {allPending.length > 0 && (
        <Card className="flex flex-wrap items-center gap-2 p-2.5">
          <button
            className="text-[12px] text-brand-700 hover:underline"
            onClick={() =>
              setSelected(
                selected.size === allPending.length ? new Set() : new Set(allPending.map((p) => p.id)),
              )
            }
          >
            {selected.size === allPending.length ? t("取消全选") : t("全选待审阅")}
          </button>
          <span className="text-[12px] text-ink-400">{t("已选 {{n}} 条", { n: selected.size })}</span>
          <div className="ml-auto flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={!selected.size || busy}
              onClick={() => decide([...selected], "reject")}
            >
              <X className="h-3.5 w-3.5" />
              {t("驳回所选")}
            </Button>
            <Button size="sm" disabled={!selected.size || busy} onClick={() => decide([...selected], "accept")}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              {t("采纳所选")}
            </Button>
          </div>
        </Card>
      )}

      {data.proposals.length === 0 ? (
        <Card className="p-6">
          <Empty
            title={t("没有提案")}
            sub={t("先到「项目文档」生成项目总览，再回来点「让 AI 审一遍」。")}
          />
        </Card>
      ) : (
        <div className="space-y-2">
          {data.proposals.map((p) => (
            <ProposalRow
              key={p.id}
              p={p}
              open={open.has(p.id)}
              selected={selected.has(p.id)}
              onToggleOpen={() =>
                setOpen((s) => {
                  const n = new Set(s);
                  if (n.has(p.id)) n.delete(p.id);
                  else n.add(p.id);
                  return n;
                })
              }
              onToggle={() => toggle(p.id)}
              onDecide={(d) => decide([p.id], d)}
              busy={busy}
              style={ACTION_STYLE}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ProposalRow({
  p,
  open,
  selected,
  onToggle,
  onToggleOpen,
  onDecide,
  busy,
  style,
}: {
  p: CaseProposal;
  open: boolean;
  selected: boolean;
  onToggle: () => void;
  onToggleOpen: () => void;
  onDecide: (d: "accept" | "reject") => void;
  busy: boolean;
  style: Record<string, { cls: string; Icon: typeof CirclePlus; label: string }>;
}) {
  const { t } = useTranslation();
  const s = style[p.action] ?? style.update;
  const Icon = s.Icon;
  const resolved = p.status !== "pending";
  return (
    <Card className={"p-3 " + (resolved ? "opacity-60" : "")}>
      <div className="flex items-start gap-2.5">
        {!resolved && (
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            className="mt-1 h-4 w-4 shrink-0 accent-[var(--brand-600)]"
          />
        )}
        <Icon className={"mt-0.5 h-4 w-4 shrink-0 " + s.cls} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={"text-[11px] font-medium " + s.cls}>{s.label}</span>
            {p.case_key && (
              <code className="rounded bg-[var(--panel2)] px-1.5 py-0.5 text-[11px] text-ink-600">
                {p.case_key}
              </code>
            )}
            <span className="text-[13px] font-medium text-ink-900">{p.summary}</span>
            {resolved && (
              <Badge status={p.status === "accepted" ? "passed" : "unknown"}>
                {p.status === "accepted" ? t("已采纳") : t("已驳回")}
              </Badge>
            )}
          </div>

          {p.reason && <p className="mt-1 text-[12px] text-ink-500">{p.reason}</p>}

          <button
            onClick={onToggleOpen}
            className="mt-1.5 inline-flex items-center gap-1 text-[12px] text-brand-700 hover:underline"
          >
            {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            {t("依据与改动内容")}
          </button>

          {open && (
            <div className="mt-1.5 space-y-2 rounded-lg bg-[var(--panel2)] p-2.5 text-[12px]">
              {/* ★ 相关流程图（2026-10-12 用户要求）。
                  放最前面：用户问的是"你为什么要加这条"，而流程是他判断的依据。
                  同一模块的提案共享同一张图（都挂在"依据与改动内容"里），
                  所以它只在展开时渲染，且带缓存。 */}
              {p.mermaid && (
                <div>
                  <p className="font-medium text-ink-600">{t("这条涉及的业务流程")}</p>
                  <MermaidBlock source={p.mermaid} className="mt-1" cacheKey={p.mermaid} />
                </div>
              )}
              {/* 账号：这条用例该用谁跑 */}
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium text-ink-600">{t("用哪个账号跑")}</span>
                {p.role ? (
                  <code className="rounded bg-[var(--panel)] px-1.5 py-0.5 text-[11px] text-ink-700">
                    {p.role}
                  </code>
                ) : (
                  <span className="text-[11px] text-ink-400">
                    {t("未指定（沿用项目默认账号）")}
                  </span>
                )}
              </div>
              {p.evidence?.length > 0 && (
                <div>
                  <p className="font-medium text-ink-600">{t("依据")}</p>
                  <ul className="mt-0.5 list-disc pl-5 text-ink-500">
                    {p.evidence.map((e, i) => (
                      <li key={i}>{e}</li>
                    ))}
                  </ul>
                </div>
              )}
              {Object.keys(p.payload ?? {}).length > 0 && (
                <div>
                  <p className="font-medium text-ink-600">
                    {p.action === "add" ? t("将新建的用例") : t("将修改的字段")}
                  </p>
                  <dl className="mt-0.5 space-y-0.5">
                    {Object.entries(p.payload).map(([k, v]) => (
                      <div key={k} className="flex gap-2">
                        <dt className="w-24 shrink-0 text-ink-400">{k}</dt>
                        <dd className="min-w-0 flex-1 whitespace-pre-wrap text-ink-600">
                          {Array.isArray(v) ? v.join(", ") : String(v)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}
              {p.action === "remove" && Object.keys(p.payload ?? {}).length === 0 && (
                <p className="text-ink-500">
                  {t("采纳后这条用例会被**作废**（不真删），你可以在用例页改回来。")}
                </p>
              )}
            </div>
          )}
        </div>

        {!resolved && (
          <div className="flex shrink-0 gap-1.5">
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide("reject")}>
              <X className="h-3.5 w-3.5" />
            </Button>
            <Button size="sm" disabled={busy} onClick={() => onDecide("accept")}>
              <Check className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}
