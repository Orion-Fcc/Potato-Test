import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Controller, useFieldArray, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import {
  api,
  relTime,
  type CasePriority,
  type CaseStatus,
  type CaseType,
  type TestCase,
} from "../lib/api";
import { Badge, Button, Card, Checkbox, Field, Input, Select, Textarea } from "./ui";
import { CASE_STATUS_LABELS, CASE_TYPE_LABELS, PRIORITY_LABELS, label } from "../lib/labels";
import { Modal } from "./Modal";

const PRIORITIES: CasePriority[] = ["P0", "P1", "P2", "P3"];
const TYPES: CaseType[] = ["functional", "smoke", "regression", "acceptance", "negative"];
const STATUSES: CaseStatus[] = ["draft", "active", "deprecated"];

// 「测试数据文件」声明的模板。放在代码里而不是后端下发，是因为它必须与
// app/testdata.py 的判据一起演进 —— 两处分叉的模板比没有模板更糟。
const DATA_FILES_TEMPLATE = `{
  "files": [
    {
      "name": "导入模板.xlsx",
      "kind": "xlsx",
      "sheets": [
        {
          "name": "Sheet1",
          "header": ["学号", "姓名", "证件号"],
          "rows": [["S001", "张三", "{{rand:18}}"], ["S002", "李四", "{{rand:18}}"]]
        }
      ]
    }
  ]
}`;


// 经验笔记的三类内容，与后端 app/case_memory.py 的白名单一一对应。
// 这里刻意只列这三类 —— 界面上能显示什么，就等于后端允许记什么。
const MEMORY_LABELS: [keyof NonNullable<TestCase["memory"]>, string][] = [
  ["navigation", "Last known navigation path"],
  ["page_notes", "Page quirks"],
  ["element_notes", "Element tips"],
];

type CaseFormValues = {
  name: string;
  module: string;
  priority: CasePriority;
  type: CaseType;
  status: CaseStatus;
  owner: string;
  roles: string[];
  references: string;
  preconditions: string;
  prompt: string;
  steps: { action: string; expected: string }[];
  test_data: string;
  data_files_text: string;
  data_hygiene: string;
  expected: string;
  start_url: string;
  tags: string;
  enabled: boolean;
};

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
  const queryClient = useQueryClient();

  // 经验笔记不参与保存（它由后端在运行时写入），所以只保留本地副本。
  const [memory, setMemory] = useState(caseData.memory ?? null);
  const [memoryStale, setMemoryStale] = useState(Boolean(caseData.memory_stale));

  // 表单里用 textarea 编辑 JSON，语法错在本地就显示，不用等一个网络往返。
  // 语义校验（文件名/上限/占位符）仍由后端做，它才是权威。
  const caseFormSchema = useMemo(
    () =>
      z.object({
        name: z
          .string()
          .max(300, t("At most 300 characters"))
          .refine((v) => v.trim().length > 0, t("Name is required")),
        module: z.string(),
        priority: z.enum(["P0", "P1", "P2", "P3"]),
        type: z.enum(["functional", "smoke", "regression", "acceptance", "negative"]),
        status: z.enum(["draft", "active", "deprecated", "blocked"]),
        owner: z.string(),
        roles: z.array(z.string()),
        references: z.string(),
        preconditions: z.string(),
        prompt: z.string().refine((v) => v.trim().length > 0, t("Agent task is required")),
        steps: z.array(z.object({ action: z.string(), expected: z.string() })),
        test_data: z.string(),
        data_files_text: z.string().superRefine((val, ctx) => {
          const s = val.trim();
          if (!s) return;
          try {
            JSON.parse(s);
          } catch (e) {
            ctx.addIssue({ code: z.ZodIssueCode.custom, message: String(e) });
          }
        }),
        data_hygiene: z.string(),
        expected: z.string(),
        start_url: z
          .string()
          .refine(
            (v) => !v.trim() || /^https?:\/\/[^\s]+$/i.test(v.trim()),
            t("Enter a valid http(s) URL"),
          ),
        tags: z.string(),
        enabled: z.boolean(),
      }),
    [t],
  );

  const defaultValues = useMemo<CaseFormValues>(
    () => ({
      name: caseData.name,
      module: caseData.module ?? "",
      priority: caseData.priority,
      type: caseData.type,
      status: caseData.status,
      owner: caseData.owner ?? "",
      roles: caseData.roles ?? [],
      references: caseData.references,
      preconditions: caseData.preconditions,
      prompt: caseData.prompt,
      steps: caseData.steps ?? [],
      test_data: caseData.test_data,
      data_files_text: caseData.data_files ? JSON.stringify(caseData.data_files, null, 2) : "",
      data_hygiene: caseData.data_hygiene ?? "",
      expected: caseData.expected,
      start_url: caseData.start_url ?? "",
      tags: (caseData.tags ?? []).join(", "),
      enabled: caseData.enabled,
    }),
    [caseData],
  );

  const {
    control,
    handleSubmit,
    setValue,
    watch,
    setError,
    clearErrors,
    formState: { errors },
  } = useForm<CaseFormValues>({
    defaultValues,
    resolver: zodResolver(caseFormSchema),
  });

  const { fields, append, remove } = useFieldArray({ control, name: "steps" });
  const roles = watch("roles");

  const historyQuery = useQuery({
    queryKey: ["case-results", caseData.id],
    queryFn: () => api.getCaseResults(caseData.id),
    enabled: !isNew,
  });
  const historyQueryData = historyQuery.data ?? [];

  const rolesQuery = useQuery({
    queryKey: ["roles", caseData.project_id],
    queryFn: () => api.getRoles(caseData.project_id).then((r) => r.roles),
    staleTime: 5 * 60_000,
  });
  const availableRoles = rolesQuery.data ?? [];

  const saveMutation = useMutation({
    mutationFn: async (values: CaseFormValues) => {
      const body: Partial<TestCase> = {
        name: values.name.trim(),
        module: values.module.trim() || null,
        priority: values.priority,
        type: values.type,
        status: values.status,
        owner: values.owner.trim() || null,
        // 2026-10-04 多角色：roles 传完整序列，role 传首项保持向后兼容。
        // 不能直接传 caseData.role —— 用户可能只动过 roles 没动 role，后端归一化
        // 会把 role 拼到 roles 前面，导致顺序错乱（"先当审批人" 变成 "先当申请人"）。
        roles: values.roles,
        role: values.roles[0] ?? caseData.role ?? null,
        references: values.references,
        preconditions: values.preconditions,
        prompt: values.prompt,
        steps: values.steps.filter((s) => s.action.trim() || s.expected.trim()),
        test_data: values.test_data,
        // 空文本 -> null（后端看到 null 就不会生成文件）。
        data_files: values.data_files_text.trim() ? JSON.parse(values.data_files_text) : null,
        data_hygiene: values.data_hygiene,
        expected: values.expected,
        start_url: values.start_url.trim() || null,
        tags: values.tags
          .split(",")
          .map((x) => x.trim())
          .filter(Boolean),
        enabled: values.enabled,
      };
      return isNew
        ? api.createCase(caseData.project_id, body)
        : api.updateCase(caseData.id, body);
    },
    onSuccess: () => {
      // 列表以 ["cases", pid] 为键。失效后由 CasesPage 的 useQuery 自动重拉，
      // 不再需要调用方手工 load()。
      void queryClient.invalidateQueries({ queryKey: ["cases", caseData.project_id] });
      onSaved();
    },
    onError: (e) => setError("root", { message: String(e) }),
  });

  // 清空经验笔记。用例内容一变，后端本来就会靠指纹把旧笔记作废；
  // 但"页面改版了、用例却没改"这种情况指纹发现不了，所以得留一个手动出口。
  const clearMemoryMutation = useMutation({
    mutationFn: () => api.clearCaseMemory(caseData.id),
    onSuccess: (updated) => {
      // 就地更新，不关闭抽屉 —— 用户清完通常还想接着看别的
      setMemory(updated.memory ?? null);
      setMemoryStale(false);
    },
    onError: (e) => setError("root", { message: String(e) }),
  });

  const onSubmit = handleSubmit((values) => {
    clearErrors("root");
    saveMutation.mutate(values);
  });

  return (
    <Modal onClose={onClose} className="max-w-2xl">
      {(close) => (
        <Card className="max-h-[88vh] overflow-auto p-0 shadow-xl">
          <div className="sticky top-0 flex items-center justify-between border-b border-[var(--line)] bg-[var(--panel)] px-5 py-3">
            <div className="flex items-center gap-2">
              {!isNew && (
                <span className="font-mono text-xs text-ink-500">
                  {caseData.case_key ?? `#${caseData.id}`}
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

          <form onSubmit={onSubmit} className="space-y-4 p-5">
            {errors.root?.message && <div className="tp-alert tp-alert-bad">{errors.root.message}</div>}

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

            <Controller
              control={control}
              name="name"
              render={({ field }) => (
                <Field label={t("Name")} error={errors.name?.message}>
                  <Input value={field.value} onChange={(e) => field.onChange(e.target.value)} />
                </Field>
              )}
            />

            <div className="grid grid-cols-2 gap-3">
              <Controller
                control={control}
                name="module"
                render={({ field }) => (
                  <Field label={t("Module")}>
                    <Input value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder="采购/询价" />
                  </Field>
                )}
              />
              <Controller
                control={control}
                name="owner"
                render={({ field }) => (
                  <Field label={t("Owner")}>
                    <Input value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder={t("unassigned")} />
                  </Field>
                )}
              />
            </div>

            <div className="grid grid-cols-3 gap-3">
              <Controller
                control={control}
                name="priority"
                render={({ field }) => (
                  <Field label={t("Priority")}>
                    <Select value={field.value} onChange={(e) => field.onChange(e.target.value as CasePriority)}>
                      {PRIORITIES.map((p) => (
                        <option key={p} value={p}>
                          {label(PRIORITY_LABELS, p, lang)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                )}
              />
              <Controller
                control={control}
                name="type"
                render={({ field }) => (
                  <Field label={t("Type")}>
                    <Select value={field.value} onChange={(e) => field.onChange(e.target.value as CaseType)}>
                      {TYPES.map((x) => (
                        <option key={x} value={x}>
                          {label(CASE_TYPE_LABELS, x, lang)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                )}
              />
              <Controller
                control={control}
                name="status"
                render={({ field }) => (
                  <Field label={t("Status")}>
                    <Select value={field.value} onChange={(e) => field.onChange(e.target.value as CaseStatus)}>
                      {STATUSES.map((x) => (
                        <option key={x} value={x}>
                          {label(CASE_STATUS_LABELS, x, lang)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                )}
              />
            </div>

            <Field label={t("Runs as (role) — which account executes this case")}>
              <div className="space-y-2">
                <Select
                  value=""
                  onValueChange={(v) => {
                    // 2026-10-04 多角色：下拉里选一个角色 = 追加到切换序列末尾。
                    // 顺序即执行时的切换顺序，所以追加而不是替换。
                    if (!v) return;
                    setValue("roles", [...roles, v], { shouldDirty: true });
                  }}
                >
                  <option value="">{t("— add a role to the sequence —")}</option>
                  {availableRoles
                    .filter((r) => !roles.includes(r))
                    .map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                </Select>

                {roles.length > 0 && (
                  <div className="space-y-1">
                    <div className="text-xs text-muted-foreground">
                      {t("Switch order — the case starts as the first one and switches through the rest:")}
                    </div>
                    {roles.map((r, i) => (
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
                            const next = [...roles];
                            [next[i - 1], next[i]] = [next[i], next[i - 1]];
                            setValue("roles", next, { shouldDirty: true });
                          }}
                          title={t("Move up")}
                        >
                          ↑
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          disabled={i === roles.length - 1}
                          onClick={() => {
                            const next = [...roles];
                            [next[i + 1], next[i]] = [next[i], next[i + 1]];
                            setValue("roles", next, { shouldDirty: true });
                          }}
                          title={t("Move down")}
                        >
                          ↓
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => setValue("roles", roles.filter((x) => x !== r), { shouldDirty: true })}
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

            <Controller
              control={control}
              name="references"
              render={({ field }) => (
                <Field label={t("References (linked requirements / tickets)")}>
                  <Input value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder="REQ-000123, #12" />
                </Field>
              )}
            />

            <Controller
              control={control}
              name="preconditions"
              render={({ field }) => (
                <Field label={t("Preconditions")}>
                  <Textarea rows={2} value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder={t("State the app must be in before this runs.")} />
                </Field>
              )}
            />

            <Controller
              control={control}
              name="prompt"
              render={({ field }) => (
                <Field label={t("Agent task (natural language — this is what runs)")} error={errors.prompt?.message}>
                  <Textarea
                    rows={3}
                    value={field.value}
                    onChange={(e) => field.onChange(e.target.value)}
                    placeholder="登录后进入 采购 › 询价，新建一条询价单，选择厂区A、料号 X，数量 10，提交。"
                  />
                </Field>
              )}
            />

            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-medium text-ink-500">{t("Steps (documentation)")}</span>
                <Button type="button" variant="outline" size="sm" onClick={() => append({ action: "", expected: "" })}>
                  {t("+ Step")}
                </Button>
              </div>
              {fields.length === 0 ? (
                <div className="rounded-lg border border-dashed border-[var(--line)] py-3 text-center text-xs text-ink-500">
                  {t("No steps. These document the case for humans; the agent runs the task above.")}
                </div>
              ) : (
                <div className="space-y-2">
                  {fields.map((s, i) => (
                    <div key={s.id} className="flex items-start gap-2">
                      <span className="mt-2 w-5 shrink-0 text-right text-xs text-ink-500">{i + 1}</span>
                      <Controller
                        control={control}
                        name={`steps.${i}.action`}
                        render={({ field }) => (
                          <Input value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder={t("action")} />
                        )}
                      />
                      <Controller
                        control={control}
                        name={`steps.${i}.expected`}
                        render={({ field }) => (
                          <Input value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder={t("expected")} />
                        )}
                      />
                      <Button type="button" variant="ghost" size="sm" onClick={() => remove(i)}>
                        ✕
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Controller
                control={control}
                name="test_data"
                render={({ field }) => (
                  <Field label={t("Test data")}>
                    <Textarea rows={2} value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder="厂区A / 料号 X" />
                  </Field>
                )}
              />
              {/* 测试数据文件声明。放在"测试数据"旁边而不是另开一节：
                  两者在用户脑子里是同一件事（"这条用例要准备什么数据"），
                  分成两处会让人以为它们互不相干。 */}
              <Controller
                control={control}
                name="data_files_text"
                render={({ field }) => (
                  <Field
                    label={t("Test data files (Excel / CSV / TXT)")}
                    hint={t("Test data files hint")}
                    error={errors.data_files_text?.message || caseData.data_files_error || undefined}
                  >
                    <div className="flex gap-2 items-start">
                      <Textarea
                        rows={7}
                        className="font-mono text-xs"
                        value={field.value}
                        onChange={(e) => field.onChange(e.target.value)}
                        placeholder={'{\n  "files": [ ... ]\n}'}
                      />
                      <Button
                        type="button"
                        onClick={() => setValue("data_files_text", DATA_FILES_TEMPLATE, { shouldValidate: true, shouldDirty: true })}
                        title={t("Insert a template")}
                      >
                        {t("Template")}
                      </Button>
                    </div>
                  </Field>
                )}
              />
              <Controller
                control={control}
                name="data_hygiene"
                render={({ field }) => (
                  <Field
                    label={t("Data isolation note")}
                    hint={t("Filled in when your expected result hard-codes a count (like \"1 row\"): the app's data is changed by other cases, so the note tells the agent not to treat leftovers as a defect. Empty = not injected.")}
                  >
                    <Textarea
                      rows={3}
                      value={field.value}
                      onChange={(e) => field.onChange(e.target.value)}
                      placeholder={t("Leave empty unless the expected result mentions a specific number of rows")}
                    />
                  </Field>
                )}
              />
              <Controller
                control={control}
                name="expected"
                render={({ field }) => (
                  <Field label={t("Expected outcome (for the judge)")}>
                    <Textarea
                      rows={2}
                      value={field.value}
                      onChange={(e) => field.onChange(e.target.value)}
                      placeholder="列表里出现该询价单，状态为“待报价”，且无错误提示。"
                    />
                  </Field>
                )}
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Controller
                control={control}
                name="start_url"
                render={({ field }) => (
                  <Field label={t("Start URL (optional, overrides project base URL)")} error={errors.start_url?.message}>
                    <Input value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder="https://…" />
                  </Field>
                )}
              />
              <Controller
                control={control}
                name="tags"
                render={({ field }) => (
                  <Field label={t("Tags (comma-separated)")}>
                    <Input value={field.value} onChange={(e) => field.onChange(e.target.value)} placeholder="询价, 只读" />
                  </Field>
                )}
              />
            </div>

            <Controller
              control={control}
              name="enabled"
              render={({ field }) => (
                <label className="flex items-center gap-2 text-sm text-ink-700">
                  <Checkbox checked={field.value} onChange={(e) => field.onChange(e.target.checked)} />
                  {t("Enabled (included in “run all enabled”)")}
                </label>
              )}
            />

            <div className="flex gap-2">
              <Button type="submit" disabled={saveMutation.isPending}>
                {saveMutation.isPending ? t("Saving…") : isNew ? t("Create") : t("Save")}
              </Button>
              <Button type="button" variant="outline" onClick={close}>
                {t("Cancel")}
              </Button>
            </div>

            {!isNew && (memory || memoryStale) && (
            <div className="border-t border-[var(--line)] pt-4">
              <div className="mb-2 flex items-center justify-between">
                <div className="text-sm font-medium text-ink-900">
                  {t("What it learned (experience notes)")}
                </div>
                <Button type="button" size="sm" variant="ghost" onClick={() => clearMemoryMutation.mutate()} disabled={clearMemoryMutation.isPending}>
                  {clearMemoryMutation.isPending ? t("Clearing…") : t("Clear")}
                </Button>
              </div>

              {memoryStale ? (
                <p className="text-[13px] text-ink-500">
                  {t("This case was edited after these notes were written, so they are ignored when it runs. They will be rebuilt on the next run.")}
                </p>
              ) : (
                <div className="space-y-1.5">
                  {MEMORY_LABELS.map(([key, label]) => {
                    const items = (memory?.[key] as string[] | undefined) ?? [];
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
              {caseData.memory_updated_at && (
                <p className="mt-1 text-xs text-ink-500">
                  {t("Updated")}: {relTime(caseData.memory_updated_at)}
                </p>
              )}
            </div>
            )}

            {!isNew && (
            <div className="border-t border-[var(--line)] pt-4">
              <div className="mb-2 text-sm font-medium text-ink-900">
                {t("Run history")} ({historyQueryData.length})
              </div>
              {historyQueryData.length === 0 ? (
                <div className="py-4 text-center text-sm text-ink-500">{t("This case hasn't run yet.")}</div>
              ) : (
                <div className="divide-y divide-[var(--line)]">
                  {historyQueryData.map((h) => (
                    <div key={h.result_id} className="flex items-center justify-between py-2 text-sm">
                      <span className="min-w-0">
                        <span className="truncate text-ink-900">{h.run_name}</span>
                        <span className="ml-2 text-xs text-ink-500">{relTime(h.finished_at)}</span>
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        <span className="text-xs text-ink-500">{(h.latency_ms / 1000).toFixed(0)}s</span>
                        {h.flaky && <Badge status="flaky">{t("flaky")}</Badge>}
                        <Badge status={h.status} />
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            )}
          </form>
        </Card>
      )}
    </Modal>
  );
}
