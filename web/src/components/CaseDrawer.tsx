import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  relTime,
  type CasePriority,
  type CaseStatus,
  type CaseResult,
  type CaseType,
  type TestCase,
  type TestStep,
} from "../lib/api";
import { Badge, Button, Card, Checkbox, Field, Input, Select, Textarea } from "./ui";
import { CASE_STATUS_LABELS, CASE_TYPE_LABELS, PRIORITY_LABELS, label } from "../lib/labels";
import { Modal } from "./Modal";

const PRIORITIES: CasePriority[] = ["P0", "P1", "P2", "P3"];
const TYPES: CaseType[] = ["functional", "smoke", "regression", "acceptance", "negative"];
const STATUSES: CaseStatus[] = ["draft", "active", "deprecated"];

// 经验笔记的三类内容，与后端 app/case_memory.py 的白名单一一对应。
// 这里刻意只列这三类 —— 界面上能显示什么，就等于后端允许记什么。
const MEMORY_LABELS: [keyof NonNullable<TestCase["memory"]>, string][] = [
  ["navigation", "Last known navigation path"],
  ["page_notes", "Page quirks"],
  ["element_notes", "Element tips"],
];

export function CaseDrawer({
  caseData,
  onClose,
  onSaved,
  firstCase = false,
}: {
  caseData: TestCase;
  onClose: () => void;
  onSaved: () => void;
  /** true when the project has no cases yet — shows the how-to-write-a-case hint */
  firstCase?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const isNew = caseData.id === 0;
  const [c, setC] = useState<TestCase>(caseData);
  const [tags, setTags] = useState((caseData.tags ?? []).join(", "));
  const [history, setHistory] = useState<CaseResult[]>([]);
  const [clearingMemory, setClearingMemory] = useState(false);
  const [roles, setRoles] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!isNew) api.getCaseResults(caseData.id).then(setHistory).catch(() => {});
  }, [caseData.id, isNew]);
  useEffect(() => {
    api.getRoles(caseData.project_id).then((r) => setRoles(r.roles)).catch(() => {});
  }, [caseData.project_id]);

  const set = <K extends keyof TestCase>(k: K, v: TestCase[K]) => setC((p) => ({ ...p, [k]: v }));
  const setStep = (i: number, patch: Partial<TestStep>) =>
    setC((p) => ({ ...p, steps: p.steps.map((s, j) => (j === i ? { ...s, ...patch } : s)) }));
  const addStep = () => setC((p) => ({ ...p, steps: [...p.steps, { action: "", expected: "" }] }));
  const removeStep = (i: number) => setC((p) => ({ ...p, steps: p.steps.filter((_, j) => j !== i) }));

  const save = async () => {
    setSaving(true);
    setErr("");
    const body = {
      name: c.name,
      module: c.module || null,
      priority: c.priority,
      type: c.type,
      status: c.status,
      owner: c.owner || null,
      // 2026-10-04 多角色：roles 传完整序列，role 传首项保持向后兼容。
      // 不能直接传 c.role —— 用户可能只动过 roles 没动 role，后端归一化
      // 会把 role 拼到 roles 前面，导致顺序错乱（"先当审批人" 变成 "先当申请人"）。
      // 传首项让 role 永远是 roles[0]，两列永远自洽。
      roles: c.roles ?? [],
      role: (c.roles ?? [])[0] ?? c.role ?? null,
      references: c.references,
      preconditions: c.preconditions,
      prompt: c.prompt,
      steps: c.steps.filter((s) => s.action.trim() || s.expected.trim()),
      test_data: c.test_data,
      data_hygiene: c.data_hygiene ?? "",
      expected: c.expected,
      start_url: c.start_url || null,
      tags: tags.split(",").map((x) => x.trim()).filter(Boolean),
      enabled: c.enabled,
    };
    try {
      if (isNew) await api.createCase(c.project_id, body);
      else await api.updateCase(caseData.id, body);
      onSaved();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  };

  // 清空经验笔记。用例内容一变，后端本来就会靠指纹把旧笔记作废；
  // 但"页面改版了、用例却没改"这种情况指纹发现不了，所以得留一个手动出口。
  const clearMemory = async () => {
    setClearingMemory(true);
    try {
      const updated = await api.clearCaseMemory(caseData.id);
      // 就地更新，不关闭抽屉 —— 用户清完通常还想接着看别的
      setC((p) => ({ ...p, memory: updated.memory ?? null, memory_stale: false }));
    } catch (e) {
      setErr(String(e));
    } finally {
      setClearingMemory(false);
    }
  };

  return (
    <Modal onClose={onClose} className="max-w-2xl">
      {(close) => (
        <Card className="max-h-[88vh] overflow-auto p-0 shadow-xl">
          <div className="sticky top-0 flex items-center justify-between border-b border-[var(--line)] bg-[var(--panel)] px-5 py-3">
            <div className="flex items-center gap-2">
              {!isNew && (
                <span className="font-mono text-xs text-ink-500">
                  {c.case_key ?? `#${caseData.id}`}
                </span>
              )}
              <span className="font-medium text-ink-900">
                {isNew ? t("New case") : t("Edit case")}
              </span>
            </div>
            <Button variant="ghost" size="sm" onClick={close}>
              {t("Close")}
            </Button>
          </div>

          <div className="space-y-4 p-5">
            {err && <div className="tp-alert tp-alert-bad">{err}</div>}

            {firstCase && isNew && (
              <div className="space-y-1.5 rounded-lg border border-brand-100 bg-brand-50 px-3 py-2.5 text-xs text-ink-700">
                <div className="font-medium text-ink-900">{t("Writing your first case")}</div>
                <div>
                  <b>{t("Agent task")}</b> — {t("brief it like a new colleague, one step at a time.")}
                </div>
                <div>
                  <b>{t("Expected outcome")}</b> —{" "}
                  {t("say what must be on screen for this to pass. The judge only sees evidence; if it's vague, the case fails.")}
                </div>
                <div>
                  <b>{t("Runs as")}</b> — {t("picks which account executes the case.")}
                </div>
              </div>
            )}

            <Field label={t("Name")}>
              <Input value={c.name} onChange={(e) => set("name", e.target.value)} />
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label={t("Module")}>
                <Input value={c.module ?? ""} onChange={(e) => set("module", e.target.value)} placeholder="采购/询价" />
              </Field>
              <Field label={t("Owner")}>
                <Input value={c.owner ?? ""} onChange={(e) => set("owner", e.target.value)} placeholder={t("unassigned")} />
              </Field>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <Field label={t("Priority")}>
                <Select value={c.priority} onChange={(e) => set("priority", e.target.value as CasePriority)}>
                  {PRIORITIES.map((p) => (
                    <option key={p} value={p}>
                      {label(PRIORITY_LABELS, p, lang)}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={t("Type")}>
                <Select value={c.type} onChange={(e) => set("type", e.target.value as CaseType)}>
                  {TYPES.map((x) => (
                    <option key={x} value={x}>
                      {label(CASE_TYPE_LABELS, x, lang)}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={t("Status")}>
                <Select value={c.status} onChange={(e) => set("status", e.target.value as CaseStatus)}>
                  {STATUSES.map((x) => (
                    <option key={x} value={x}>
                      {label(CASE_STATUS_LABELS, x, lang)}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>

            <Field label={t("Runs as (role) — which account executes this case")}>
              <div className="space-y-2">
                <Select
                  value=""
                  onValueChange={(v) => {
                    // 2026-10-04 多角色：下拉里选一个角色 = 追加到切换序列末尾。
                    // 顺序即执行时的切换顺序，所以追加而不是替换。
                    if (!v) return;
                    set("roles", [...(c.roles ?? []), v]);
                  }}
                >
                  <option value="">{t("— add a role to the sequence —")}</option>
                  {roles
                    .filter((r) => !(c.roles ?? []).includes(r))
                    .map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                </Select>

                {(c.roles ?? []).length > 0 && (
                  <div className="space-y-1">
                    <div className="text-xs text-muted-foreground">
                      {t("Switch order — the case starts as the first one and switches through the rest:")}
                    </div>
                    {(c.roles ?? []).map((r, i) => (
                      <div
                        key={r}
                        className="flex items-center gap-2 rounded-md border px-2 py-1 text-sm"
                      >
                        <span className="w-5 shrink-0 text-center text-xs tabular-nums text-muted-foreground">
                          {i + 1}
                        </span>
                        <span className="flex-1 truncate">{r}</span>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          disabled={i === 0}
                          onClick={() => {
                            const next = [...(c.roles ?? [])];
                            [next[i - 1], next[i]] = [next[i], next[i - 1]];
                            set("roles", next);
                          }}
                          title={t("Move up")}
                        >
                          ↑
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          disabled={i === (c.roles ?? []).length - 1}
                          onClick={() => {
                            const next = [...(c.roles ?? [])];
                            [next[i + 1], next[i]] = [next[i], next[i + 1]];
                            set("roles", next);
                          }}
                          title={t("Move down")}
                        >
                          ↓
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => set("roles", (c.roles ?? []).filter((x) => x !== r))}
                          title={t("Remove")}
                        >
                          ×
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </Field>

            <Field label={t("References (linked requirements / tickets)")}>
              <Input value={c.references} onChange={(e) => set("references", e.target.value)} placeholder="REQ-000123, #12" />
            </Field>

            <Field label={t("Preconditions")}>
              <Textarea rows={2} value={c.preconditions} onChange={(e) => set("preconditions", e.target.value)} placeholder={t("State the app must be in before this runs.")} />
            </Field>

            <Field label={t("Agent task (natural language — this is what runs)")}>
              <Textarea
                rows={3}
                value={c.prompt}
                onChange={(e) => set("prompt", e.target.value)}
                placeholder="登录后进入 采购 › 询价，新建一条询价单，选择厂区A、料号 X，数量 10，提交。"
              />
            </Field>

            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-medium text-ink-500">{t("Steps (documentation)")}</span>
                <Button variant="outline" size="sm" onClick={addStep}>
                  {t("+ Step")}
                </Button>
              </div>
              {c.steps.length === 0 ? (
                <div className="rounded-lg border border-dashed border-[var(--line)] py-3 text-center text-xs text-ink-500">
                  {t("No steps. These document the case for humans; the agent runs the task above.")}
                </div>
              ) : (
                <div className="space-y-2">
                  {c.steps.map((s, i) => (
                    <div key={i} className="flex items-start gap-2">
                      <span className="mt-2 w-5 shrink-0 text-right text-xs text-ink-500">{i + 1}</span>
                      <Input value={s.action} onChange={(e) => setStep(i, { action: e.target.value })} placeholder={t("action")} />
                      <Input value={s.expected} onChange={(e) => setStep(i, { expected: e.target.value })} placeholder={t("expected")} />
                      <Button variant="ghost" size="sm" onClick={() => removeStep(i)}>
                        ✕
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Field label={t("Test data")}>
                <Textarea rows={2} value={c.test_data} onChange={(e) => set("test_data", e.target.value)} placeholder="厂区A / 料号 X" />
              </Field>
              <Field
                label={t("Data isolation note")}
                hint={t("Filled in when your expected result hard-codes a count (like \"1 row\"): the app's data is changed by other cases, so the note tells the agent not to treat leftovers as a defect. Empty = not injected.")}
              >
                <Textarea
                  rows={3}
                  value={c.data_hygiene ?? ""}
                  onChange={(e) => set("data_hygiene", e.target.value)}
                  placeholder={t("Leave empty unless the expected result mentions a specific number of rows")}
                />
              </Field>
              <Field label={t("Expected outcome (for the judge)")}>
                <Textarea
                  rows={2}
                  value={c.expected}
                  onChange={(e) => set("expected", e.target.value)}
                  placeholder="列表里出现该询价单，状态为“待报价”，且无错误提示。"
                />
              </Field>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Field label={t("Start URL (optional, overrides project base URL)")}>
                <Input value={c.start_url ?? ""} onChange={(e) => set("start_url", e.target.value)} placeholder="https://…" />
              </Field>
              <Field label={t("Tags (comma-separated)")}>
                <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="询价, 只读" />
              </Field>
            </div>

            <label className="flex items-center gap-2 text-sm text-ink-700">
              <Checkbox checked={c.enabled} onChange={(e) => set("enabled", e.target.checked)} />
              {t("Enabled (included in “run all enabled”)")}
            </label>

            <div className="flex gap-2">
              <Button onClick={save} disabled={saving || !c.name.trim() || !c.prompt.trim()}>
                {saving ? t("Saving…") : isNew ? t("Create") : t("Save")}
              </Button>
              <Button variant="outline" onClick={close}>
                {t("Cancel")}
              </Button>
            </div>

            {!isNew && (c.memory || c.memory_stale) && (
            <div className="border-t border-[var(--line)] pt-4">
              <div className="mb-2 flex items-center justify-between">
                <div className="text-sm font-medium text-ink-900">
                  {t("What it learned (experience notes)")}
                </div>
                <Button size="sm" variant="ghost" onClick={clearMemory} disabled={clearingMemory}>
                  {clearingMemory ? t("Clearing…") : t("Clear")}
                </Button>
              </div>

              {c.memory_stale ? (
                <p className="text-[13px] text-ink-500">
                  {t("This case was edited after these notes were written, so they are ignored when it runs. They will be rebuilt on the next run.")}
                </p>
              ) : (
                <div className="space-y-1.5">
                  {MEMORY_LABELS.map(([key, label]) => {
                    const items = (c.memory?.[key] as string[] | undefined) ?? [];
                    if (!items.length) return null;
                    return (
                      <div key={key} className="text-[13px]">
                        <span className="mr-1 font-medium text-ink-700">{t(label)}：</span>
                        <span className="text-ink-800">{items.join("；")}</span>
                      </div>
                    );
                  })}
                </div>
              )}

              <p className="mt-2 text-xs text-ink-500">
                {t("Notes only record how to get around — navigation paths and page quirks. They deliberately never record whether a run passed: that would turn the next run into copying an answer instead of testing.")}
              </p>
              {c.memory_updated_at && (
                <p className="mt-1 text-xs text-ink-500">
                  {t("Updated")}: {relTime(c.memory_updated_at)}
                </p>
              )}
            </div>
            )}

            {!isNew && (
            <div className="border-t border-[var(--line)] pt-4">
              <div className="mb-2 text-sm font-medium text-ink-900">
                {t("Run history")} ({history.length})
              </div>
              {history.length === 0 ? (
                <div className="py-4 text-center text-sm text-ink-500">{t("This case hasn't run yet.")}</div>
              ) : (
                <div className="divide-y divide-[var(--line)]">
                  {history.map((h) => (
                    <div key={h.result_id} className="flex items-center justify-between py-2 text-sm">
                      <span className="min-w-0">
                        <span className="truncate text-ink-900">{h.run_name}</span>
                        <span className="ml-2 text-xs text-ink-500">{relTime(h.finished_at)}</span>
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        <span className="text-xs text-ink-500">{(h.latency_ms / 1000).toFixed(0)}s</span>
                        {h.flaky && <Badge status="flaky">flaky</Badge>}
                        <Badge status={h.status} />
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            )}
          </div>
        </Card>
      )}
    </Modal>
  );
}
