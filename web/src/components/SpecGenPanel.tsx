import { AlertTriangle, CheckCircle2, FileText, Loader2, Sparkles, Trash2, Upload, Wand2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  type SpecDraftCase,
  type SpecModule,
  type SpecOutline,
  type SpecUploadResult,
} from "../lib/api";
import { Badge, Button, Card, Checkbox, Input, Select } from "./ui";
import { Modal } from "./Modal";
import { useErrorToast, useToast } from "./toast";

/** 按模块名去重，保留先出现的那个。
 *
 *  为什么需要：逐批规划时把各批的模块拼起来，而模块名是模型自由起的 ——
 *  两批之间真撞名的话，界面上会出现两条同名项，用户点哪条都一样却看着像重复。
 *  去重放在**拼接处**而不是渲染处：渲染处去重会让"列表长度"和"实际模块数"不一致，
 *  后面按索引操作（选中、滚动定位）就会错位。
 */
function dedupeModules(mods: SpecModule[]): SpecModule[] {
  const seen = new Set<string>();
  const out: SpecModule[] = [];
  for (const m of mods) {
    if (seen.has(m.name)) continue;
    seen.add(m.name);
    out.push(m);
  }
  return out;
}

/** 从需规文档生成用例（2026-10-09）。
 *
 *  ## 为什么要有这个界面
 *
 * 在这之前，这件事是**三步手工**：文档粘进知识库 → 在助手里让它一条一条建
 * （一次 LLM 往返一条）→ 再去运行页点一次。没有任何一步是"对着这份文档"做的。
 *
 *  ## 界面刻意做成"逐节推进"而不是"一键全生成"
 *
 * 一份需规可能生成几十上百条。一键全生成有两个问题：单次调用会大到失控，
 * 而且用户拿到的是一大坨没法逐节确认的结果。所以左侧是模块清单（可逐个生成），
 * 右侧是这一次生成的结果 —— 用户一次只需要判断"这一节生成得对不对"。
 *
 *  ## 草稿的边界在界面上也是可见的
 *
 * 生成的用例默认存成 **草稿**，不进回归集。所以这里必须把"存草稿"和"存并启用"
 * 做成两个按钮，而不是一个"保存"后面藏一个开关 —— 用户得清楚自己按下的那个键
 * 会不会让这些用例从下一轮开始就跑起来。
 */
export function SpecGenPanel({ pid, onClose }: { pid: number; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const fail = useErrorToast();

  const [outline, setOutline] = useState<SpecOutline | null>(null);
  const [loading, setLoading] = useState(true);
  const [planning, setPlanning] = useState(false);
  // 分批规划失败的说明（见 planNextBatch）。独立于 outline.error：
  // 那个是**本次整体规划**的错误，这个是**某一批**的错误，就地显示在按钮旁最清楚。
  const [batchError, setBatchError] = useState("");
  const [busyModule, setBusyModule] = useState<string | null>(null);

  // 当前这一批生成的草稿（还没入库）。用户勾掉不想要的，再决定怎么存。
  const [module, setModule] = useState<SpecModule | null>(null);
  const [drafts, setDrafts] = useState<SpecDraftCase[] | null>(null);
  const [dropped, setDropped] = useState<string[]>([]);
  const [keep, setKeep] = useState<boolean[]>([]);
  const [saving, setSaving] = useState(false);
  // 直接把文档发给它（2026-10-09）。用户的原话是"我发需归文件给内置的助手，
  // 包括不同格式" —— 所以上传入口就在这个面板里，不需要先去设置页。
  const [uploading, setUploading] = useState(false);
  const [uploadInfo, setUploadInfo] = useState<SpecUploadResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(
    (plan: boolean, refresh = false) =>
      api
        .specOutline(pid, plan, refresh)
        .then(setOutline)
        .catch(fail)
        .finally(() => setLoading(false)),
    [pid, fail],
  );

  // 首次进入用 plan=true：归并过的分批才有意义（一份 46 万字的需规可能有
  // 200 个原始小标题，逐个生成就是 200 次调用、200 次人工确认）。
  useEffect(() => {
    load(true);
  }, [load]);

  /** 规划下一批（2026-10-09）。
   *
   *  ★ 关键在于**累加**而不是替换：一批 60 个标题，586 个标题的文档要看 10 批。
   *  如果每批都覆盖掉上一批的模块，用户规划到第 10 批时就只剩最后 60 个标题的模块，
   *  前面 9 批选择的上下文全丢了 —— 而他要的是"把这份文档逐批过完"。
   *
   *  已完成生成的模块不需要重新出现在列表里？需要 —— 用户会回来继续做，
   *  而且哪一个模块做过了只有他自己知道（我们不在服务端记"已生成"状态）。
   */
  const planNextBatch = async () => {
    const from = outline?.planned_until ?? 0;
    if (!from) return;
    setPlanning(true);
    setBatchError("");
    try {
      // 只请求 from 开始的那一批。
      const more = await api.specOutlineFrom(pid, from);
      // ★ 失败这一批**不并入**，也不推进 planned_until（后端失败时已把它停在起点）。
      // 否则界面会显示"已规划 120/586"而实际一个模块都没拿到 ——
      // 用户看到的进度是假的，下次还会从同一个位置重试失败。
      if (more.failed) {
        setBatchError(more.error || t("Planning this batch failed."));
        return;
      }
      setOutline((prev) =>
        prev
          ? {
              ...prev,
              // 按模块名去重后拼接：不同批次理论上不该出现同名模块，
              // 但模型起名是自由的，真撞了就保留先出现的那个。
              modules: dedupeModules([...prev.modules, ...more.modules]),
              planned_until: more.planned_until,
              has_more: more.has_more,
            }
          : more,
      );
    } catch (e) {
      setBatchError(e instanceof Error ? e.message : String(e));
      fail(e);
    } finally {
      setPlanning(false);
    }
  };

  const replan = async () => {
    setPlanning(true);
    // ★ 只有这里传 refresh=true。后端把分批结果缓存了半小时，因为实测同一份文档
    // 连调四次会给出 2/16/18/13 个模块 —— 用户刚看到"16 个可测"，刷新一下就变成
    // "2 个、0 个可测"，只能认为功能坏了；而且每次刷新都白烧一次规划调用。
    // 所以"重新分批"必须是用户**主动点的这一下**，而不是刷新的副作用。
    await load(true, true);
    setPlanning(false);
  };

  const upload = async (f: File) => {
    setUploading(true);
    setUploadInfo(null);
    try {
      const r = await api.specUpload(pid, f);
      setUploadInfo(r);
      // 上传后重新规划：上传接口已经顺带规划过，但重拉一次能让"重新分批"
      // 与面板里的模块列表保持同一个来源，避免两处状态不一致。
      await load(true);
      if (r.error) fail(new Error(r.error));
    } catch (e) {
      fail(e);
    } finally {
      setUploading(false);
    }
  };

  const generate = async (m: SpecModule) => {
    setBusyModule(m.name);
    setModule(m);
    setDrafts(null);
    setDropped([]);
    try {
      const r = await api.specDraft(pid, { module: m.name, titles: m.titles });
      if (r.error) {
        fail(new Error(r.error));
        return;
      }
      setDrafts(r.cases);
      setDropped(r.dropped);
      setKeep(r.cases.map(() => true));
    } catch (e) {
      fail(e);
    } finally {
      setBusyModule(null);
    }
  };

  const patch = (i: number, field: keyof SpecDraftCase, value: string) =>
    setDrafts((prev) => (prev ? prev.map((c, idx) => (idx === i ? { ...c, [field]: value } : c)) : prev));

  const selected = (drafts ?? []).filter((_, i) => keep[i]);

  const save = async (activate: boolean) => {
    if (!selected.length) return;
    setSaving(true);
    try {
      const r = await api.specAccept(pid, {
        cases: selected,
        module: module?.name ?? "",
        activate,
      });
      toast(
        "success",
        activate
          ? t("Saved {{n}} cases as active — they will run in the next run-all", { n: r.count })
          : t("Saved {{n}} cases as draft — they will NOT run until you activate them", { n: r.count }),
      );
      setDrafts(null);
      setModule(null);
    } catch (e) {
      fail(e);
    } finally {
      setSaving(false);
    }
  };

  const noKnowledge = outline && !outline.has_knowledge;

  return (
    <Modal onClose={onClose} className="max-w-6xl">
      {(close) => (
        <Card className="overflow-hidden">
          <div className="flex items-center justify-between gap-3 border-b border-[var(--line)] px-5 py-3.5">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-[15px] font-semibold text-ink-900">
                <Wand2 className="h-4 w-4 text-brand-600" />
                {t("Generate cases from a spec document")}
              </div>
              <div className="mt-0.5 text-xs text-ink-500">
                {t("Pick a module, review what the model wrote, then save it — as drafts by default.")}
              </div>
            </div>
            <div className="flex items-center gap-2">
              {/* 直接发文件给它 —— 用户说的是"我发需归文件给内置的助手，包括不同格式"，
                  所以入口放在这里，而不是让他先去设置页上传再回来。 */}
              <input
                ref={fileRef}
                type="file"
                accept=".md,.txt,.json,.csv,.docx,.pdf,.xlsx,.xls,.html,.htm,.rtf"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  e.target.value = ""; // 同一个文件再选一次也要能触发
                  if (f) upload(f);
                }}
              />
              <Button
                variant="outline"
                size="sm"
                disabled={uploading}
                onClick={() => fileRef.current?.click()}
              >
                {uploading ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Upload className="h-3.5 w-3.5" />
                )}
                {t("Upload a spec document")}
              </Button>
              <Button variant="ghost" size="sm" onClick={close}>
                {t("Close")}
              </Button>
            </div>
          </div>

          {/* 上传结果横幅。★ warnings 必须显示：一份"上传成功但没有章节标题"的文档
              （扫描件、纯文本）看起来和成功一模一样，用户只有看到这句话才知道
              为什么生成不出用例 —— 否则他会反复重传同一份文件。 */}
          {uploadInfo && (
            <div
              className={
                "border-b border-[var(--line)] px-5 py-2.5 text-[12px] " +
                (uploadInfo.warnings.length ? "tp-alert-warn" : "bg-[var(--panel2)]")
              }
            >
              <div className="flex flex-wrap items-center gap-2">
                <FileText className="h-3.5 w-3.5" />
                <span className="font-medium">{uploadInfo.filename}</span>
                <span className="text-ink-500">
                  {uploadInfo.format} · {uploadInfo.chars} {t("chars")} ·{" "}
                  {t("{{n}} sections found", { n: uploadInfo.headings })}
                </span>
                {uploadInfo.truncated && (
                  <span className="text-[var(--warn-fg)]">
                    {t("truncated — long document, only the beginning was indexed")}
                  </span>
                )}
                {uploadInfo.degraded && (
                  <span className="text-ink-500">{t("grouping fell back to one module per section")}</span>
                )}
              </div>
              {uploadInfo.warnings.map((w, i) => (
                <div key={i} className="mt-1 flex items-start gap-1.5 text-[11.5px] text-[var(--warn-fg)]">
                  <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                  <span>{w}</span>
                </div>
              ))}
            </div>
          )}

          {loading ? (
            <div className="flex items-center gap-2 px-5 py-10 text-sm text-ink-500">
              <Loader2 className="h-4 w-4 animate-spin" /> {t("Reading the document…")}
            </div>
          ) : noKnowledge ? (
            <div className="px-5 py-8">
              <div className="flex items-start gap-3 rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-4">
                <FileText className="mt-0.5 h-5 w-5 shrink-0 text-ink-400" />
                <div className="text-sm text-ink-700">
                  <div className="font-medium text-ink-900">{t("No spec uploaded yet")}</div>
                  <div className="mt-1 text-ink-500">{outline?.note}</div>
                  <div className="mt-2 text-xs text-ink-500">
                    {t("Go to this project's Settings → 需规 / 资料 and upload the document (Word / PDF / Excel / Markdown / txt), then come back.")}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="grid max-h-[72vh] grid-cols-1 gap-0 overflow-hidden lg:grid-cols-[320px_1fr]">
              {/* 左：模块清单 */}
              <div className="overflow-y-auto border-r border-[var(--line)] p-4">
                <div className="mb-2 flex items-center justify-between">
                  <div className="text-xs font-medium uppercase tracking-wide text-ink-500">
                    {t("Modules")}
                  </div>
                  <Button variant="ghost" size="sm" onClick={replan} disabled={planning}>
                    {planning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                    {t("Re-plan")}
                  </Button>
                </div>
                {outline?.degraded && (
                  <div className="tp-alert tp-alert-warn mb-2 text-[11px]">
                    {t("Grouping failed — fell back to one module per heading.")}
                  </div>
                )}
                {outline?.error && (
                  <div className="tp-alert tp-alert-bad mb-2 text-[11px]">{outline.error}</div>
                )}
                {/* ★ 分批进度 + 「继续规划」（2026-10-09）。
                    一份 46 万字的合并需规实测有 **586 个标题**，而一次只规划 60 个 ——
                    没有这个按钮的话，用户只能看到文档的 10%，而且**完全不知道**
                    还有 526 个标题没被处理（界面上一句提示都没有）。
                    这正是这个功能在真实文档上最要紧的一处。 */}
                {outline?.has_more && (
                  <div className="mb-2 rounded-lg border border-[var(--line)] bg-[var(--panel2)] px-2.5 py-2 text-[11px]">
                    <div className="text-ink-600">
                      {t("Planned {{done}} of {{total}} sections — {{left}} still unplanned.", {
                        done: outline.planned_until ?? 0,
                        total: outline.total ?? 0,
                        left: (outline.total ?? 0) - (outline.planned_until ?? 0),
                      })}
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="mt-1.5 h-7 w-full"
                      disabled={planning}
                      onClick={planNextBatch}
                    >
                      {planning ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Sparkles className="h-3.5 w-3.5" />
                      )}
                      {t("Plan the next batch")}
                    </Button>
                    {batchError && (
                      <div className="mt-1.5 text-[11px] text-[var(--bad-fg)]">
                        {t("That batch failed — nothing was added.")} {batchError.slice(0, 160)}
                      </div>
                    )}
                  </div>
                )}
                {outline && !outline.has_more && (outline.total ?? 0) > 0 && (
                  <div className="mb-2 text-[11px] text-ink-400">
                    {t("All {{n}} sections are planned.", { n: outline.total ?? 0 })}
                  </div>
                )}
                <div className="space-y-1.5">
                  {(outline?.modules ?? []).map((m) => {
                    const active = module?.name === m.name;
                    const busy = busyModule === m.name;
                    return (
                      <button
                        key={m.name}
                        onClick={() => generate(m)}
                        disabled={!!busyModule || !m.testable}
                        title={m.reason || (m.testable ? "" : t("Not testable end-to-end in a browser"))}
                        className={
                          "w-full rounded-lg border px-2.5 py-2 text-left text-[13px] transition-colors " +
                          (active
                            ? "border-brand-500 bg-brand-50 text-brand-700"
                            : "border-[var(--line)] hover:border-[var(--line-strong)] hover:bg-[var(--panel2)]") +
                          (m.testable ? "" : " cursor-not-allowed opacity-50")
                        }
                      >
                        <div className="flex items-center gap-1.5">
                          {busy ? (
                            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                          ) : null}
                          <span className="min-w-0 flex-1 truncate font-medium">{m.name}</span>
                          <span className="shrink-0 text-[11px] text-ink-400">
                            {m.titles.length}
                          </span>
                        </div>
                        {!m.testable && (
                          <div className="mt-0.5 text-[11px] text-ink-500">
                            {t("Not testable in a browser — skipped")}
                          </div>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* 右：这一批的结果 */}
              <div className="overflow-y-auto p-4">
                {!drafts && !busyModule && (
                  <div className="px-2 py-10 text-center text-sm text-ink-500">
                    {t("Pick a module on the left — the model will draft cases from just that section.")}
                  </div>
                )}
                {busyModule && (
                  <div className="flex items-center justify-center gap-2 py-10 text-sm text-ink-500">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    {t("Generating from «{{m}}»…", { m: busyModule })}
                  </div>
                )}

                {drafts && (
                  <>
                    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                      <div className="text-sm text-ink-700">
                        {t("{{n}} drafted · {{sel}} selected", { n: drafts.length, sel: selected.length })}
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setKeep(drafts.map(() => true))}
                        >
                          {t("Select all")}
                        </Button>
                        <Button variant="outline" size="sm" onClick={() => setKeep(drafts.map(() => false))}>
                          {t("Select none")}
                        </Button>
                      </div>
                    </div>

                    {dropped.length > 0 && (
                      <div className="tp-alert tp-alert-warn mb-3 text-[11px]">
                        {t("{{n}} case(s) were rejected while parsing:", { n: dropped.length })}
                        <ul className="mt-1 list-disc pl-4">
                          {dropped.slice(0, 5).map((d, i) => (
                            <li key={i}>{d}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {drafts.length === 0 && (
                      <div className="rounded-lg border border-dashed border-[var(--line)] px-4 py-8 text-center text-sm text-ink-500">
                        {t("The model decided this section has nothing worth testing end-to-end. That is a valid answer — it did not pad the list.")}
                      </div>
                    )}

                    <div className="space-y-2.5">
                      {drafts.map((c, i) => (
                        <div
                          key={i}
                          className={
                            "rounded-lg border p-3 " +
                            (keep[i] ? "border-[var(--line)]" : "border-dashed border-[var(--line)] opacity-50")
                          }
                        >
                          <div className="flex items-start gap-2.5">
                            <Checkbox
                              checked={keep[i]}
                              onChange={() => setKeep((k) => k.map((v, idx) => (idx === i ? !v : v)))}
                              className="mt-0.5"
                            />
                            <div className="min-w-0 flex-1 space-y-2">
                              <Input
                                value={c.name}
                                onChange={(e) => patch(i, "name", e.target.value)}
                                className="h-8 font-medium"
                              />
                              <div className="grid gap-2 sm:grid-cols-2">
                                <label className="block">
                                  <span className="mb-1 block text-[11px] text-ink-500">
                                    {t("What the agent should do")}
                                  </span>
                                  <textarea
                                    value={c.prompt}
                                    onChange={(e) => patch(i, "prompt", e.target.value)}
                                    rows={3}
                                    className="w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-2.5 py-1.5 text-[12.5px] text-ink-900 outline-none focus:border-brand-500"
                                  />
                                </label>
                                <label className="block">
                                  <span className="mb-1 block text-[11px] text-ink-500">
                                    {t("Decidable expected result")}
                                  </span>
                                  <textarea
                                    value={c.expected}
                                    onChange={(e) => patch(i, "expected", e.target.value)}
                                    rows={3}
                                    className="w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-2.5 py-1.5 text-[12.5px] text-ink-900 outline-none focus:border-brand-500"
                                  />
                                </label>
                              </div>
                              <div className="flex flex-wrap items-center gap-2">
                                <Select
                                  value={c.priority}
                                  onChange={(e) => patch(i, "priority", e.target.value)}
                                  className="h-7 w-[5.5rem] text-[12px]"
                                >
                                  {["P0", "P1", "P2", "P3"].map((p) => (
                                    <option key={p} value={p}>
                                      {p}
                                    </option>
                                  ))}
                                </Select>
                                <Select
                                  value={c.type}
                                  onChange={(e) => patch(i, "type", e.target.value)}
                                  className="h-7 w-[8rem] text-[12px]"
                                >
                                  {["functional", "smoke", "regression", "acceptance", "negative"].map((x) => (
                                    <option key={x} value={x}>
                                      {x}
                                    </option>
                                  ))}
                                </Select>
                                {c.module && <Badge>{c.module}</Badge>}
                                <button
                                  onClick={() => {
                                    setDrafts((d) => (d ? d.filter((_, idx) => idx !== i) : d));
                                    setKeep((k) => k.filter((_, idx) => idx !== i));
                                  }}
                                  className="ml-auto inline-flex items-center gap-1 text-[11.5px] text-ink-500 hover:text-[var(--bad-fg)]"
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                  {t("Drop")}
                                </button>
                              </div>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>

                    <div className="sticky bottom-0 mt-4 flex flex-wrap items-center justify-end gap-2 border-t border-[var(--line)] bg-[var(--panel)] py-3">
                      <div className="mr-auto text-[11px] text-ink-500">
                        {t("Drafts do not run in a run-all until you activate them.")}
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={saving || !selected.length}
                        onClick={() => save(false)}
                      >
                        {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                        {t("Save {{n}} as draft", { n: selected.length })}
                      </Button>
                      <Button size="sm" disabled={saving || !selected.length} onClick={() => save(true)}>
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        {t("Save {{n}} and activate", { n: selected.length })}
                      </Button>
                    </div>
                  </>
                )}
              </div>
            </div>
          )}
        </Card>
      )}
    </Modal>
  );
}
