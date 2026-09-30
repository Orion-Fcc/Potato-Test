import { ArrowRight, Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { api, relTime, type Project } from "../lib/api";
import { pct, rateTone } from "../lib/metrics";
import { useAbortSignal, useDebounced } from "../lib/hooks";
import { Badge, Button, Card, Field, Input, PageHeader } from "../components/ui";
import { Modal } from "../components/Modal";
import { NewProjectWizard } from "../components/NewProjectWizard";
import { useToast, useErrorToast } from "../components/toast";

/** Edit-only draft. Creating a project goes through NewProjectWizard. */
type Draft = { id: number; name: string; base_url: string };

export function Projects() {
  const { t } = useTranslation();
  const toast = useToast();
  const fail = useErrorToast();
  const signal = useAbortSignal();
  const [projects, setProjects] = useState<Project[]>([]);
  const [q, setQ] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [wizard, setWizard] = useState(false);
  const [confirmDel, setConfirmDel] = useState<Project | null>(null);

  const load = useMemo(
    () => () =>
      api
        .listProjects({ signal })
        .then(setProjects)
        .catch((e) => {
          fail(e);
        }),
    [signal, fail],
  );
  useEffect(() => {
    load();
  }, [load]);

  const dq = useDebounced(q, 180);
  const filtered = useMemo(
    () => projects.filter((p) => p.name.toLowerCase().includes(dq.toLowerCase())),
    [projects, dq],
  );

  const save = async () => {
    if (!draft?.name.trim()) return toast("error", t("Name is required"));
    try {
      await api.updateProject(draft.id, { name: draft.name.trim(), base_url: draft.base_url });
      setDraft(null);
      load();
      toast("success", t("Saved"));
    } catch (e) {
      fail(e);
    }
  };

  const doDelete = async () => {
    if (!confirmDel) return;
    try {
      await api.deleteProject(confirmDel.id);
      setConfirmDel(null);
      load();
      toast("success", t("Project deleted"));
    } catch (e) {
      fail(e);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("Workspace")}
        title={t("Projects")}
        subtitle={t("Potato Test runs your natural-language test cases in a real browser. Pick a project to begin.")}
        actions={
          <>
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("Search projects")}
              className="w-56"
            />
            <Button onClick={() => setWizard(true)}>
              <Plus className="h-4 w-4" />
              {t("New project")}
            </Button>
          </>
        }
      />

      {projects.length === 0 ? (
        // Deliberately terse: the guided flow already makes the "what is this" pitch.
        <Card className="px-6 py-16 text-center">
          <div className="mx-auto flex max-w-md flex-col items-center gap-3">
            <div className="grid h-12 w-12 place-items-center rounded-2xl bg-brand-50 text-brand-700">
              <Plus className="h-5 w-5" />
            </div>
            <div className="text-base font-semibold text-ink-900">{t("No projects yet")}</div>
            <p className="text-[13px] leading-relaxed text-ink-500">
              {t("A project holds the cases for one system under test. Creating one takes about a minute — a URL, an account, and your first case.")}
            </p>
            <Button className="mt-2" onClick={() => setWizard(true)}>
              <Plus className="h-4 w-4" />
              {t("Create your first project")}
            </Button>
          </div>
        </Card>
      ) : (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {filtered.map((p) => {
          const rate = p.last_run?.pass_rate;
          return (
            <Card
              key={p.id}
              hover
              className="group relative flex flex-col overflow-hidden p-5"
            >
              <div className="flex items-start justify-between gap-2">
                <Link to={`/projects/${p.id}/overview`} className="min-w-0">
                  <div className="truncate text-[15px] font-semibold text-ink-900 transition-colors group-hover:text-brand-700">
                    {p.name}
                  </div>
                  <div className="mt-0.5 truncate font-mono text-[11px] text-ink-400">
                    {p.base_url ?? t("no base url")}
                  </div>
                </Link>
                <div className="flex shrink-0 gap-0.5 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100">
                  <button
                    onClick={() => setDraft({ id: p.id, name: p.name, base_url: p.base_url ?? "" })}
                    className="rounded-md p-1.5 text-ink-400 outline-none transition-colors hover:bg-[var(--panel2)] hover:text-ink-900"
                    aria-label={t("Edit")}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <button
                    onClick={() => setConfirmDel(p)}
                    className="rounded-md p-1.5 text-ink-400 outline-none transition-colors hover:bg-[var(--bad-bg)] hover:text-[var(--bad-fg)]"
                    aria-label={t("Delete")}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>

              <div className="mt-5 flex items-end gap-6">
                <div>
                  <div className="text-[1.4rem] font-semibold tabular-nums tracking-tight text-ink-900">
                    {p.case_count ?? 0}
                  </div>
                  <div className="text-[10.5px] uppercase tracking-wider text-ink-400">{t("cases")}</div>
                </div>
                <div>
                  <div
                    className={
                      "text-[1.4rem] font-semibold tabular-nums tracking-tight " +
                      (rate == null
                        ? "text-ink-900"
                        : rateTone(rate) === "ok"
                          ? "text-[var(--ok-fg)]"
                          : rateTone(rate) === "warn"
                            ? "text-[var(--warn-fg)]"
                            : "text-[var(--bad-fg)]")
                    }
                  >
                    {rate != null ? pct(rate) : "—"}
                  </div>
                  <div className="text-[10.5px] uppercase tracking-wider text-ink-400">{t("pass")}</div>
                </div>
                {p.last_run && (
                  <div className="ml-auto flex flex-col items-end gap-1">
                    <Badge status={p.last_run.status} />
                    <span className="text-[10.5px] text-ink-400">{relTime(p.last_run.finished_at)}</span>
                  </div>
                )}
              </div>

              <div className="mt-5 flex items-center gap-2 border-t border-[var(--line)] pt-3.5">
                <Link to={`/projects/${p.id}/overview`}>
                  <Button size="sm">
                    {t("Go to project")}
                    <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
                  </Button>
                </Link>
                {p.has_login_state && (
                  <span className="ml-auto inline-flex items-center gap-1.5 rounded-full bg-brand-50 px-2 py-0.5 text-[10.5px] font-medium text-brand-700">
                    <span className="h-1.5 w-1.5 rounded-full bg-brand-600" />
                    {t("logged in")}
                  </span>
                )}
              </div>
            </Card>
          );
        })}

        <button
          onClick={() => setWizard(true)}
          className="grid min-h-[176px] place-items-center rounded-[var(--radius)] border border-dashed border-[var(--line-strong)] text-[13px] text-ink-500 outline-none transition-[border-color,color,background-color] hover:border-brand-200 hover:bg-brand-50/40 hover:text-brand-700"
        >
          <span className="flex items-center gap-1.5 font-medium">
            <Plus className="h-4 w-4" />
            {t("New project")}
          </span>
        </button>
      </div>
      )}

      {filtered.length === 0 && projects.length > 0 && (
        <div className="py-10 text-center text-sm text-ink-500">{t("No projects match your search.")}</div>
      )}

      {wizard && <NewProjectWizard onClose={() => setWizard(false)} onDone={() => setWizard(false)} />}

      {draft && (
        <Modal onClose={() => setDraft(null)} className="max-w-md">
          <Card className="p-5">
            <h2 className="mb-4 text-lg font-semibold text-ink-900">{t("Edit project")}</h2>
            <div className="space-y-4">
              <Field label={t("Name")}>
                <Input
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder="e.g. Web regression"
                  autoFocus
                />
              </Field>
              <Field label={t("Base URL (optional)")}>
                <Input
                  value={draft.base_url}
                  onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
                  placeholder="https://your-app.example.com"
                />
              </Field>
              <div className="flex justify-end gap-2 pt-1">
                <Button variant="outline" onClick={() => setDraft(null)}>{t("Cancel")}</Button>
                <Button onClick={save}>{t("Save")}</Button>
              </div>
            </div>
          </Card>
        </Modal>
      )}

      {confirmDel && (
        <Modal onClose={() => setConfirmDel(null)} className="max-w-md">
          <Card className="p-5">
            <h2 className="text-lg font-semibold text-ink-900">{t("Delete project")}</h2>
            <p className="mt-2 text-sm text-ink-500">
              {t("Delete “{{name}}” and all its cases, suites, runs and issues? This cannot be undone.", {
                name: confirmDel.name,
              })}
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="outline" onClick={() => setConfirmDel(null)}>{t("Cancel")}</Button>
              <Button variant="danger" onClick={doDelete}>{t("Delete")}</Button>
            </div>
          </Card>
        </Modal>
      )}
    </div>
  );
}
