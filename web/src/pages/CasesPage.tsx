import { FlaskConical as FlaskConicalIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, relTime, type CasePriority, type CaseStatus, type CaseType, type TestCase } from "../lib/api";
import { useAbortSignal, useDebounced } from "../lib/hooks";
import { rateColor, rateTone } from "../lib/metrics";
import { Badge, Button, Card, Checkbox, Field, Input, PageHeader, Select, Stat, StatRing } from "../components/ui";
import { CASE_STATUS_LABELS, CASE_TYPE_LABELS, PRIORITY_LABELS, label } from "../lib/labels";
import { CaseDrawer } from "../components/CaseDrawer";
import { Modal } from "../components/Modal";
import { useToast, useErrorToast } from "../components/toast";

const PRIORITIES: CasePriority[] = ["P0", "P1", "P2", "P3"];
const TYPES: CaseType[] = ["functional", "smoke", "regression", "acceptance", "negative"];
const STATUSES: CaseStatus[] = ["draft", "active", "deprecated"];

const blankCase = (pid: number): TestCase => ({
  id: 0,
  project_id: pid,
  case_key: null,
  name: "",
  module: null,
  priority: "P2",
  type: "functional",
  status: "active",
  owner: null,
  role: null,
  // 2026-10-04 多角色：空数组 = 跟随默认账号，与改动前 role:null 等价。
  roles: [],
  references: "",
  preconditions: "",
  prompt: "",
  steps: [],
  test_data: "",
  // 不声明要上传的文件。留 null 而不是空对象：后端看到 null 就不会生成任何东西，
  // 空对象会被当成"声明了一个空文件列表"，多一次无意义的解析。
  data_files: null,
  data_files_error: null,
  expected: "",
  start_url: null,
  tags: [],
  enabled: true,
});

const PRIORITY_STYLE: Record<string, string> = {
  P0: "bg-[var(--bad-bg)] text-[var(--bad-fg)]",
  P1: "bg-[var(--warn-bg)] text-[var(--warn-fg)]",
  P2: "bg-[var(--panel2)] text-ink-700",
  P3: "bg-[var(--panel2)] text-ink-500",
};

export function CasesPage() {
  const pid = Number(useParams().pid);
  const navigate = useNavigate();
  const toast = useToast();
  const fail = useErrorToast();
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const [cases, setCases] = useState<TestCase[]>([]);
  const xlsxRef = useRef<HTMLInputElement>(null);

  // filters + selection + drawer
  const [q, setQ] = useState("");
  const [tag, setTag] = useState("");
  const [priorityFilter, setPriorityFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [enabledFilter, setEnabledFilter] = useState("all");
  const [resultFilter, setResultFilter] = useState("all");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<TestCase | null>(null);

  const signal = useAbortSignal();
  const load = useMemo(
    () => () =>
      api
        .listCases(pid, { signal })
        .then(setCases)
        .catch((e) => {
          fail(e);
        }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pid, signal],
  );
  useEffect(() => {
    load();
  }, [load]);

  const allTags = useMemo(
    () => Array.from(new Set(cases.flatMap((c) => c.tags ?? []))).sort(),
    [cases],
  );
  // A 379-row suite is rescanned on every keystroke without this.
  const dq = useDebounced(q, 180);
  const filtered = useMemo(
    () =>
      cases.filter((c) => {
        if (dq && !`${c.case_key ?? ""} ${c.name} ${c.prompt}`.toLowerCase().includes(dq.toLowerCase()))
          return false;
        if (tag && !(c.tags ?? []).includes(tag)) return false;
        if (priorityFilter && c.priority !== priorityFilter) return false;
        if (typeFilter && c.type !== typeFilter) return false;
        if (statusFilter && c.status !== statusFilter) return false;
        if (enabledFilter === "enabled" && !c.enabled) return false;
        if (enabledFilter === "disabled" && c.enabled) return false;
        if (resultFilter === "passed" && c.last_status !== "passed") return false;
        if (resultFilter === "failing" && (!c.last_status || c.last_status === "passed")) return false;
        if (resultFilter === "never" && c.last_status) return false;
        return true;
      }),
    [cases, dq, tag, priorityFilter, typeFilter, statusFilter, enabledFilter, resultFilter],
  );
  const UNGROUPED = t("Ungrouped");
  const groups = useMemo(() => {
    const by = new Map<string, TestCase[]>();
    for (const c of filtered) {
      const key = c.module?.trim() || UNGROUPED;
      (by.get(key) ?? by.set(key, []).get(key)!).push(c);
    }
    for (const arr of by.values())
      arr.sort((a, b) => (a.case_key ?? "").localeCompare(b.case_key ?? "") || a.id - b.id);
    return [...by.entries()]
      .sort(([a], [b]) => (a === UNGROUPED ? 1 : b === UNGROUPED ? -1 : a.localeCompare(b)))
      .map(([module, list]) => ({ module, list }));
  }, [filtered, UNGROUPED]);

  // A suite can hold hundreds of cases across a dozen modules. Expanding every
  // group up front mounted the whole table at once (~24k px of DOM, thousands of
  // nodes) which is what made the page feel heavy. Big groups now start
  // collapsed; the operator's explicit toggles always win.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  /** Groups the user has explicitly opened or closed — their choice overrides
   *  the size-based default and survives re-filtering. */
  const [touched, setTouched] = useState<Set<string>>(new Set());
  /** Groups opened via "Expand all". */
  const [forcedOpen, setForcedOpen] = useState<Set<string>>(new Set());

  const AUTO_COLLAPSE_AT = 40;
  /** Rows rendered per group before a "show more" row takes over. */
  const ROW_PAGE = 50;
  const [shownRows, setShownRows] = useState<Record<string, number>>({});
  const isOpen = (m: string, size: number) => {
    if (forcedOpen.has(m)) return true;
    if (collapsed.has(m)) return false;
    if (touched.has(m)) return true;
    return size <= AUTO_COLLAPSE_AT;
  };
  const [renameId, setRenameId] = useState<number | null>(null);
  const [renameVal, setRenameVal] = useState("");
  const [moduleEdit, setModuleEdit] = useState<{ module: string; list: TestCase[]; value: string } | null>(null);
  const [moduleSaving, setModuleSaving] = useState(false);
  const [pending, setPending] = useState<{ caseIds?: number[]; name: string; count: number } | null>(null);
  const [starting, setStarting] = useState(false);

  /** Flip one group; mark it as user-touched so auto-collapse backs off. */
  const toggleGroup = (m: string, size: number) => {
    const open = isOpen(m, size);
    setForcedOpen((s) => {
      const n = new Set(s);
      n.delete(m);
      return n;
    });
    setCollapsed((s) => {
      const n = new Set(s);
      if (open) n.add(m);
      else n.delete(m);
      return n;
    });
    setTouched((s) => (s.has(m) ? s : new Set(s).add(m)));
  };

  const allOpen = groups.every((g) => isOpen(g.module, g.list.length));
  const toggleAll = () => {
    if (allOpen) {
      setForcedOpen(new Set());
      setCollapsed(new Set(groups.map((g) => g.module)));
    } else {
      setCollapsed(new Set());
      setTouched(new Set());
      setForcedOpen(new Set(groups.map((g) => g.module)));
    }
  };

  const submitModuleRename = async (close: () => void) => {
    if (!moduleEdit) return;
    const module = moduleEdit.value.trim() || null;
    setModuleSaving(true);
    try {
      await Promise.all(moduleEdit.list.map((c) => api.updateCase(c.id, { module })));
      close();
      load();
    } catch (e) {
      fail(e);
    } finally {
      setModuleSaving(false);
    }
  };

  const saveRename = async (c: TestCase) => {
    const name = renameVal.trim();
    setRenameId(null);
    if (!name || name === c.name) return;
    try {
      await api.updateCase(c.id, { name });
      load();
    } catch (e) {
      fail(e);
    }
  };

  const toggleEnabled = (c: TestCase) =>
    api.updateCase(c.id, { enabled: !c.enabled }).then(load).catch((e) => fail(e));

  const toggle = (id: number) =>
    setSelected((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  const onImportXlsx = async (f: File) => {
    try {
      const { imported } = await api.importCasesXlsx(pid, f);
      toast("success", t("Imported {{n}} cases", { n: imported }));
      load();
    } catch (e) {
      fail(e);
    }
  };

  // Naming a run is the whole point of this prompt: a list of "run 2026/8/28 16:01:57"
  // rows tells you nothing about why any of them was started. The default already carries
  // the scope, so Enter is as fast as the old one-click, and you can type over it.
  const askRun = (caseIds: number[] | undefined, scope: string) => {
    const when = new Date().toLocaleString([], {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
    setPending({ caseIds, name: `${scope} · ${when}`, count: caseIds?.length ?? cov.enabled });
  };

  const startRun = async (close: () => void) => {
    if (!pending) return;
    setStarting(true);
    try {
      const run = await api.createRun(pid, {
        name: pending.name.trim() || `run ${new Date().toLocaleString()}`,
        concurrency: 2,
        case_ids: pending.caseIds,
      });
      close();
      navigate(`/projects/${pid}/runs/${run.id}`);
    } catch (e) {
      fail(e);
      setStarting(false);
    }
  };

  const cov = useMemo(() => {
    const enabled = cases.filter((c) => c.enabled);
    const ran = enabled.filter((c) => c.last_status);
    const passed = enabled.filter((c) => c.last_status === "passed");
    const pri: Record<string, number> = { P0: 0, P1: 0, P2: 0, P3: 0 };
    const modules = new Set<string>();
    for (const c of cases) {
      pri[c.priority] = (pri[c.priority] ?? 0) + 1;
      if (c.module?.trim()) modules.add(c.module.trim());
    }
    return {
      total: cases.length,
      enabled: enabled.length,
      disabled: cases.length - enabled.length,
      coverage: enabled.length ? Math.round((ran.length / enabled.length) * 100) : 0,
      ranCount: ran.length,
      passRate: ran.length ? Math.round((passed.length / ran.length) * 100) : 0,
      passedCount: passed.length,
      pri,
      modules: modules.size,
    };
  }, [cases]);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("Project")}
        title={t("Test Cases")}
        subtitle={
          <span className="tabular-nums">{t("{{n}} cases in this project.", { n: cases.length })}</span>
        }
        actions={
          <>
            {selected.size > 0 && (
              <Button
                variant="outline"
                onClick={() => askRun([...selected], t("{{n}} selected cases", { n: selected.size }))}
              >
                {t("Run selected ({{n}})", { n: selected.size })}
              </Button>
            )}
            <Button onClick={() => askRun(undefined, t("All enabled"))} disabled={cases.length === 0}>
              {t("Run all enabled")}
            </Button>
          </>
        }
      />

      <div className="tp-rise grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label={t("Coverage")}
          value={`${cov.coverage}%`}
          sub={t("{{r}}/{{e}} ever run", { r: cov.ranCount, e: cov.enabled })}
          right={<StatRing pct={cov.coverage} size={46} thickness={4} color="var(--brand-600)" />}
        />
        <Stat
          label={t("Latest pass rate")}
          value={cov.ranCount ? `${cov.passRate}%` : "—"}
          sub={t("{{p}} passed / {{r}} run", { p: cov.passedCount, r: cov.ranCount })}
          tone={cov.ranCount ? rateTone(cov.passRate / 100) : undefined}
          right={
            cov.ranCount ? (
              <StatRing pct={cov.passRate} size={46} thickness={4} color={rateColor(cov.passRate / 100)} />
            ) : undefined
          }
        />
        <Stat
          label={t("Total cases")}
          value={cov.total}
          sub={t("{{e}} enabled · {{d}} disabled", { e: cov.enabled, d: cov.disabled })}
        />
        <Card hover className="tp-kpi flex flex-col justify-center p-4">
          <div className="text-[12px] font-medium text-ink-500">{t("Priority · modules")}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {(["P0", "P1", "P2", "P3"] as const).map((p) => (
              <span
                key={p}
                className={"rounded-full px-2 py-0.5 text-[11px] font-semibold tabular-nums " + (PRIORITY_STYLE[p] ?? "")}
              >
                {p} · {cov.pri[p] ?? 0}
              </span>
            ))}
          </div>
          <div className="mt-2 text-[11px] text-ink-400">{t("{{n}} modules", { n: cov.modules })}</div>
        </Card>
      </div>

      <Card className="flex flex-wrap items-center gap-2 p-3">
        <Button onClick={() => setEditing(blankCase(pid))}>{t("+ New case")}</Button>
        <div className="mx-1 h-5 w-px bg-[var(--line)]" />
        <Button variant="outline" size="sm" onClick={() => xlsxRef.current?.click()}>
          {t("Import Excel")}
        </Button>
        <a href={api.exportUrl(pid)}>
          <Button variant="outline" size="sm">
            {t("Export Excel")}
          </Button>
        </a>
        <a href={api.templateUrl(pid)} className="ml-1 text-[12px] text-brand-700 hover:underline">
          {t("Download template")}
        </a>
        <input
          ref={xlsxRef}
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          className="hidden"
          onChange={(e) => e.target.files?.[0] && onImportXlsx(e.target.files[0])}
        />
      </Card>

      <Card className="flex flex-wrap items-center gap-2 p-3">
        <div className="relative">
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t("Search key, name or task…")}
            className="h-9 w-64 pl-3"
          />
        </div>
        <div className="mx-1 hidden h-5 w-px bg-[var(--line)] sm:block" />
        <Select value={tag} onChange={(e) => setTag(e.target.value)} className="h-9 w-auto">
          <option value="">{t("All tags")}</option>
          {allTags.map((tg) => (
            <option key={tg} value={tg}>
              {tg}
            </option>
          ))}
        </Select>
        <Select value={priorityFilter} onChange={(e) => setPriorityFilter(e.target.value)} className="h-9 w-auto">
          <option value="">{t("All priorities")}</option>
          {PRIORITIES.map((p) => (
            <option key={p} value={p}>{label(PRIORITY_LABELS, p, lang)}</option>
          ))}
        </Select>
        <Select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} className="h-9 w-auto">
          <option value="">{t("All types")}</option>
          {TYPES.map((x) => (
            <option key={x} value={x}>{label(CASE_TYPE_LABELS, x, lang)}</option>
          ))}
        </Select>
        <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="h-9 w-auto">
          <option value="">{t("All statuses")}</option>
          {STATUSES.map((x) => (
            <option key={x} value={x}>{label(CASE_STATUS_LABELS, x, lang)}</option>
          ))}
        </Select>
        <Select value={enabledFilter} onChange={(e) => setEnabledFilter(e.target.value)} className="h-9 w-auto">
          <option value="all">{t("All")}</option>
          <option value="enabled">{t("Enabled")}</option>
          <option value="disabled">{t("Disabled")}</option>
        </Select>
        <Select value={resultFilter} onChange={(e) => setResultFilter(e.target.value)} className="h-9 w-auto">
          <option value="all">{t("All results")}</option>
          <option value="passed">{t("Passing")}</option>
          <option value="failing">{t("Not passing")}</option>
          <option value="never">{t("Never run")}</option>
        </Select>
        {(q || tag || priorityFilter || typeFilter || statusFilter || enabledFilter !== "all" || resultFilter !== "all") && (
          <button
            className="rounded-md px-1.5 py-1 text-xs text-ink-500 outline-none transition-colors hover:text-ink-900 hover:underline focus-visible:ring-2 focus-visible:ring-brand-100"
            onClick={() => {
              setQ("");
              setTag("");
              setPriorityFilter("");
              setTypeFilter("");
              setStatusFilter("");
              setEnabledFilter("all");
              setResultFilter("all");
            }}
          >
            {t("Clear")}
          </button>
        )}
        <span className="ml-auto shrink-0 rounded-full bg-[var(--panel2)] px-2.5 py-1 text-[11.5px] font-medium tabular-nums text-ink-500">
          {t("{{n}} shown", { n: filtered.length })}
        </span>
        {groups.length > 1 && (
          <button
            type="button"
            onClick={toggleAll}
            aria-expanded={allOpen}
            className="shrink-0 rounded-md border border-[var(--line)] px-2 py-1 text-[11.5px] font-medium text-ink-700 outline-none transition-colors hover:bg-[var(--panel2)] hover:text-ink-900 focus-visible:ring-2 focus-visible:ring-brand-100"
          >
            {allOpen ? t("Collapse all") : t("Expand all")}
          </button>
        )}
      </Card>

      {filtered.length === 0 ? (
        <Card className="px-6 py-14 text-center">
          {cases.length === 0 ? (
            <div className="mx-auto flex max-w-md flex-col items-center gap-3">
              <div className="grid h-11 w-11 place-items-center rounded-xl bg-brand-50 text-brand-700">
                <FlaskConicalIcon className="h-5 w-5" />
              </div>
              <div className="text-[13px] font-semibold text-ink-900">{t("No test cases yet")}</div>
              <p className="text-[13px] leading-relaxed text-ink-500">
                {t(
                  "A case is a task written in plain language plus one line saying what counts as a pass. An agent carries it out in a real browser.",
                )}
              </p>
              <Button size="sm" className="mt-1" onClick={() => setEditing(blankCase(pid))}>
                {t("+ New case")}
              </Button>
            </div>
          ) : (
            <div className="text-sm text-ink-500">{t("No cases match the filters.")}</div>
          )}
        </Card>
      ) : (
        <div className="space-y-3">
          {groups.map(({ module, list }) => {
            const open = isOpen(module, list.length);
            const runnable = list.filter((c) => c.enabled).map((c) => c.id);
            // Render a window of rows per group instead of the whole module: a
            // 400-case module is ~400 <tr> with a checkbox, badges and buttons
            // each, which is what made scrolling this page feel heavy.
            const shown = shownRows[module] ?? ROW_PAGE;
            const visible = list.slice(0, shown);
            const hidden = list.length - visible.length;
            return (
              <Card key={module} hover className="overflow-hidden">
                <div className="flex items-center gap-2 border-b border-[var(--line)] bg-[color-mix(in_oklch,var(--panel2)_58%,transparent)] px-3 py-2.5">
                  <button
                    onClick={() => toggleGroup(module, list.length)}
                    aria-expanded={open}
                    className="flex items-center gap-2 rounded text-[13px] font-semibold text-ink-900 outline-none"
                  >
                    <span
                      className={
                        "inline-block text-ink-400 transition-transform duration-150 " + (open ? "rotate-90" : "")
                      }
                    >
                      ›
                    </span>
                    {module}
                    <span className="rounded-full bg-[var(--panel)] px-1.5 py-0.5 text-[10.5px] font-medium tabular-nums text-ink-400">
                      {list.length}
                    </span>
                  </button>
                  <div className="ml-auto flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        setModuleEdit({ module, list, value: module === UNGROUPED ? "" : module })
                      }
                      title={t("Rename module")}
                    >
                      {t("Rename")}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => askRun(runnable, module === UNGROUPED ? t("Ungrouped") : module)}
                      disabled={runnable.length === 0}
                    >
                      {t("Run group")}
                    </Button>
                  </div>
                </div>
                {open && (
                  <table className="w-full text-[13px]">
                    <thead className="tp-thead">
                      <tr>
                        <th className="w-8 px-3 py-2.5" />
                        <th className="w-24 px-2 py-2.5">{t("ID")}</th>
                        <th className="px-2 py-2.5">{t("Case")}</th>
                        <th className="w-14 px-2 py-2.5">{t("Priority")}</th>
                        <th className="w-24 px-2 py-2.5">{t("Type")}</th>
                        <th className="w-20 px-2 py-2.5">{t("Status")}</th>
                        <th className="w-16 px-2 py-2.5">{t("Enabled")}</th>
                        <th className="w-28 px-2 py-2.5">{t("Last result")}</th>
                        <th className="px-2 py-2.5" />
                      </tr>
                    </thead>
                    <tbody>
                      {visible.map((c) => (
                        <tr key={c.id} className="tp-row">
                          <td className="w-8 px-3 py-2.5">
                            <Checkbox checked={selected.has(c.id)} onChange={() => toggle(c.id)} />
                          </td>
                          <td className="w-24 whitespace-nowrap px-2 py-2.5 font-mono text-[11px] text-ink-400">
                            {c.case_key ?? `#${c.id}`}
                          </td>
                          <td className="px-2 py-2.5">
                            {renameId === c.id ? (
                              <Input
                                autoFocus
                                value={renameVal}
                                onChange={(e) => setRenameVal(e.target.value)}
                                onBlur={() => saveRename(c)}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") saveRename(c);
                                  if (e.key === "Escape") setRenameId(null);
                                }}
                                className="h-7 max-w-sm"
                              />
                            ) : (
                              <div className="group/case flex items-center gap-1.5">
                                <button
                                  className="text-left font-medium text-ink-900 hover:text-brand-700"
                                  onClick={() => setEditing(c)}
                                >
                                  {c.name}
                                </button>
                                <button
                                  className="text-ink-400 opacity-0 transition-opacity hover:text-brand-700 group-hover/case:opacity-100"
                                  title={t("Rename")}
                                  onClick={() => {
                                    setRenameVal(c.name);
                                    setRenameId(c.id);
                                  }}
                                >
                                  ✎
                                </button>
                              </div>
                            )}
                            <div className="max-w-md truncate text-[11.5px] text-ink-400" title={c.prompt}>
                              {c.prompt}
                            </div>
                          </td>
                          <td className="w-14 px-2 py-2.5">
                            <span className={"inline-flex rounded px-1.5 py-0.5 text-[11px] font-semibold " + (PRIORITY_STYLE[c.priority] ?? "")}>
                              {c.priority}
                            </span>
                          </td>
                          <td className="w-24 whitespace-nowrap px-2 py-2.5 text-ink-700">
                            {label(CASE_TYPE_LABELS, c.type, lang)}
                          </td>
                          <td className="w-20 px-2 py-2.5">
                            <Badge status={c.status === "active" ? "passed" : c.status === "deprecated" ? "cancelled" : "pending"}>
                              {label(CASE_STATUS_LABELS, c.status, lang)}
                            </Badge>
                          </td>
                          <td className="w-16 px-2 py-2.5">
                            <button onClick={() => toggleEnabled(c)} title={t("Toggle enabled")}>
                              {c.enabled ? <Badge status="passed">{t("on")}</Badge> : <Badge status="cancelled">{t("off")}</Badge>}
                            </button>
                          </td>
                          <td className="w-28 whitespace-nowrap px-2 py-2.5">
                            {c.last_status ? (
                              <Link
                                to={c.last_run_id ? `/projects/${pid}/runs/${c.last_run_id}` : "#"}
                                className="block hover:opacity-80"
                                title={c.last_run_at ? new Date(c.last_run_at).toLocaleString() : undefined}
                              >
                                <Badge status={c.last_status}>{t(c.last_status)}</Badge>
                                <div className="mt-0.5 text-[10.5px] text-ink-400">{relTime(c.last_run_at)}</div>
                              </Link>
                            ) : (
                              <span className="text-[11.5px] text-ink-400">{t("never run")}</span>
                            )}
                          </td>
                          <td className="px-2 py-2.5 text-right">
                            <div className="flex justify-end gap-1">
                              <Button variant="outline" size="sm" onClick={() => setEditing(c)}>
                                {t("Edit")}
                              </Button>
                              <Button
                                variant="danger"
                                size="sm"
                                onClick={() => api.deleteCase(c.id).then(load).catch((e) => fail(e))}
                              >
                                {t("Delete")}
                              </Button>
                            </div>
                          </td>
                        </tr>
                      ))}
                      {hidden > 0 && (
                        <tr className="tp-row">
                          <td colSpan={9} className="px-3 py-2.5 text-center">
                            <button
                              onClick={() => setShownRows((m) => ({ ...m, [module]: shown + ROW_PAGE }))}
                              className="rounded-md px-3 py-1.5 text-[12.5px] font-medium text-brand-700 outline-none transition-colors hover:bg-[var(--hover)] focus-visible:ring-2 focus-visible:ring-brand-100"
                            >
                              {t("Show {{n}} more", { n: Math.min(ROW_PAGE, hidden) })}
                            </button>
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                )}
              </Card>
            );
          })}
        </div>
      )}

      {editing && (
        <CaseDrawer
          caseData={editing}
          firstCase={cases.length === 0}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      )}

      {pending && (
        <Modal
          onClose={() => {
            setPending(null);
            setStarting(false);
          }}
          className="max-w-md"
        >
          {(close) => (
            <Card className="space-y-4 p-5">
              <div>
                <h2 className="text-base font-semibold text-ink-900">{t("Start a run")}</h2>
                <p className="mt-1 text-sm text-ink-500">
                  {t("{{n}} case(s) will run. Name it so you can tell it apart later.", {
                    n: pending.count,
                  })}
                </p>
              </div>
              <Field label={t("Run name")}>
                <Input
                  autoFocus
                  value={pending.name}
                  onChange={(e) => setPending((p) => (p ? { ...p, name: e.target.value } : p))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") startRun(close);
                  }}
                />
              </Field>
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={close}>
                  {t("Cancel")}
                </Button>
                <Button onClick={() => startRun(close)} disabled={starting}>
                  {starting ? t("Starting…") : t("Start run")}
                </Button>
              </div>
            </Card>
          )}
        </Modal>
      )}

      {moduleEdit && (
        <Modal onClose={() => setModuleEdit(null)} className="max-w-md">
          {(close) => (
            <Card className="space-y-4 p-5">
              <div>
                <h2 className="text-base font-semibold text-ink-900">{t("Rename module")}</h2>
                <p className="mt-1 text-sm text-ink-500">
                  {t("Renames the module on all {{n}} cases in this group. Leave blank to ungroup.", {
                    n: moduleEdit.list.length,
                  })}
                </p>
              </div>
              <Field label={t("Module")}>
                <Input
                  autoFocus
                  value={moduleEdit.value}
                  onChange={(e) => setModuleEdit((m) => (m ? { ...m, value: e.target.value } : m))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") submitModuleRename(close);
                  }}
                  placeholder={t("Ungrouped")}
                />
              </Field>
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={close}>
                  {t("Cancel")}
                </Button>
                <Button onClick={() => submitModuleRename(close)} disabled={moduleSaving}>
                  {moduleSaving ? t("Saving…") : t("Save changes")}
                </Button>
              </div>
            </Card>
          )}
        </Modal>
      )}
    </div>
  );
}
