import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import { api, relTime, type Issue } from "../lib/api";
import { useAbortSignal } from "../lib/hooks";
import { Button, PageHeader, Select } from "../components/ui";
import { IssueDrawer } from "../components/IssueDrawer";
import { FeedbackTab } from "../components/FeedbackTab";
import { useErrorToast, useToast } from "../components/toast";
import { ISSUE_STATUS_LABELS, SEVERITY_LABELS, label } from "../lib/labels";
import i18n from "../i18n";

const COLUMNS: { key: Issue["status"]; label: string }[] = [
  { key: "open", label: "Open" },
  { key: "in_progress", label: "In progress" },
  { key: "fixed", label: "Fixed" },
  { key: "verified", label: "Verified" },
  { key: "closed", label: "Closed" },
];

const SEV_COLOR: Record<string, string> = {
  low: "bg-[var(--panel2)] text-ink-500",
  medium: "bg-[var(--warn-bg)] text-[var(--warn-fg)]",
  high: "bg-[var(--bad-bg)] text-[var(--bad-fg)]",
  critical: "bg-[var(--bad-bg)] text-[var(--bad-fg)] font-semibold",
};

export function IssuesPage() {
  const pid = Number(useParams().pid);
  const fail = useErrorToast();
  const { t } = useTranslation();
  const lang = i18n.language;
  const toast = useToast();
  const signal = useAbortSignal();
  const [issues, setIssues] = useState<Issue[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [tab, setTab] = useState<"issues" | "feedback">("issues");

  const load = useMemo(
    () => () =>
      api
        .listIssues(pid, { signal })
        .then(setIssues)
        .catch((e) => {
          fail(e);
        }),
    [pid, signal, fail],
  );
  useEffect(() => {
    load();
  }, [load]);

  const newIssue = async () => {
    try {
      const i = await api.createIssue(pid, { title: "Untitled issue" });
      await load();
      setSelected(i.id);
    } catch (e) {
      fail(e);
    }
  };

  const move = async (i: Issue, status: Issue["status"]) => {
    await api.updateIssue(i.id, { status });
    load();
  };

  const remove = async (i: Issue) => {
    if (!window.confirm(t("Delete this issue? This cannot be undone."))) return;
    try {
      await api.deleteIssue(i.id);
      toast("success", t("Issue deleted"));
      if (selected === i.id) setSelected(null);
      load();
    } catch (e) {
      fail(e);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("Project")}
        title={t("Issues")}
        subtitle={t("Bugs from failed tests — track, fix, and re-run to verify.")}
        actions={tab === "issues" ? <Button onClick={newIssue}>{t("+ New issue")}</Button> : undefined}
      />

      <div className="inline-flex rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-0.5">
        {(["issues", "feedback"] as const).map((k) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={
              "rounded-md px-3.5 py-1.5 text-[12px] font-medium outline-none transition-[background-color,color,box-shadow] " +
              (tab === k
                ? "bg-[var(--panel)] text-ink-900 shadow-[var(--shadow-1)]"
                : "text-ink-500 hover:text-ink-900")
            }
          >
            {k === "issues" ? t("Issues") : t("Feedback")}
          </button>
        ))}
      </div>

      {tab === "feedback" && <FeedbackTab pid={pid} />}

      {tab === "issues" && (
      <div className="flex gap-3 overflow-x-auto pb-2">
        {COLUMNS.map((col) => {
          const items = issues.filter((i) => i.status === col.key);
          return (
            <div key={col.key} className="w-64 shrink-0">
              <div className="mb-2 flex items-center gap-2 px-1 text-[13px] font-semibold text-ink-700">
                {label(ISSUE_STATUS_LABELS, col.key, lang)}
                <span className="rounded-full bg-[var(--panel2)] px-1.5 text-[11px] font-medium tabular-nums text-ink-400">
                  {items.length}
                </span>
              </div>
              <div className="space-y-2">
                {items.map((i) => (
                  // a div (not <button>) so the status Select's button trigger can nest
                  // validly; keyboard-activatable to keep it accessible.
                  <div
                    key={i.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelected(i.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelected(i.id);
                      }
                    }}
                    className="tp-card block w-full cursor-pointer border border-[var(--line)] p-3 text-left outline-none transition-[border-color,box-shadow,transform] hover:-translate-y-px hover:border-[var(--line-strong)]"
                  >
                    <div className="flex items-center gap-2">
                      <span className={"rounded px-1.5 py-0.5 text-[10px] font-medium " + (SEV_COLOR[i.severity] ?? "")}>
                        {label(SEVERITY_LABELS, i.severity, lang)}
                      </span>
                      <span className="font-mono text-[11px] text-ink-500">#{i.id}</span>
                      {i.case_id && <span className="ml-auto text-[11px] text-ink-500">case #{i.case_id}</span>}
                      <button
                        title={t("Delete")}
                        aria-label={t("Delete")}
                        onClick={(e) => {
                          e.stopPropagation();
                          remove(i);
                        }}
                        onKeyDown={(e) => e.stopPropagation()}
                        className={
                          "grid h-5 w-5 shrink-0 place-items-center rounded text-ink-400 outline-none transition-colors hover:bg-[var(--bad-bg)] hover:text-[var(--bad-fg)] " +
                          (i.case_id ? "" : "ml-auto")
                        }
                      >
                        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                          <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />
                        </svg>
                      </button>
                    </div>
                    <div className="mt-1.5 line-clamp-2 text-sm text-ink-900">{i.title}</div>
                    <div className="mt-2 flex items-center justify-between">
                      <span
                        role="presentation"
                        onClick={(e) => e.stopPropagation()}
                        onPointerDown={(e) => e.stopPropagation()}
                      >
                        <Select
                          variant="bare"
                          value={i.status}
                          onValueChange={(v) => move(i, v as Issue["status"])}
                          className="border border-[var(--line)]"
                        >
                          {COLUMNS.map((c) => (
                            <option key={c.key} value={c.key}>
                              {label(ISSUE_STATUS_LABELS, c.key, lang)}
                            </option>
                          ))}
                        </Select>
                      </span>
                      <span className="text-[11px] text-ink-500">{relTime(i.updated_at)}</span>
                    </div>
                  </div>
                ))}
                {items.length === 0 && (
                  <div className="rounded-lg border border-dashed border-[var(--line)] py-7 text-center text-xs text-ink-500">
                    {t("empty")}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
      )}

      {selected != null && (
        <IssueDrawer
          issueId={selected}
          pid={pid}
          onClose={() => setSelected(null)}
          onChanged={load}
        />
      )}
    </div>
  );
}
