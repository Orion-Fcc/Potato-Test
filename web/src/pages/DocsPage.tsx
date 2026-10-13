import { FileText, GitBranch, Loader2, Lock, Plus, Save, Sparkles, Trash2, Unlock, Wand2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import { api, type KnowledgeFormats, type ProjectDoc, type VerifiedCoverage } from "../lib/api";
import { Badge, Button, Card, Input, PageHeader } from "../components/ui";
import { RichTextEditor } from "../components/RichTextEditor";
import { InsightsPanel } from "../components/InsightsPanel";
import { MermaidPreview } from "../components/MermaidPreview";
import { SpecSourcePanel } from "../components/SpecSourcePanel";
import { Empty, PageSkeleton } from "../components/feedback";
import { useErrorToast, useToast } from "../components/toast";

/** 人工确认过的通过率面板。
 *
 *  ## 为什么单独做一块、而不是把数字写进文档正文
 *
 *  文档正文是 AI 生成的**文字**，会随每次重新生成而变；而通过率是**事实**，
 *  随运行结果变。把事实嵌进会变的文字里，两边的更新时机永远对不上 ——
 *  最后用户看到的是"文档说 90% 通过，实际只有一半"。
 *
 *  ## 为什么必须同时显示"待人工确认"
 *
 *  只显示 `verified_rate` 会撒谎：样本可能是 10 条里的 1 条。
 *  所以每个模块都把"还有多少条没人工看过"一起显示出来 ——
 *  让用户自己判断这个百分比有多可信，而不是把这个判断藏起来。
 */
function CoveragePanel({ cov }: { cov: VerifiedCoverage }) {
  const { t } = useTranslation();
  const [showAll, setShowAll] = useState(false);
  const rows = showAll ? cov.modules : cov.modules.slice(0, 6);
  const s = cov.summary;
  return (
    <Card className="p-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-[13px] font-medium text-ink-800">
          {t("人工确认过的通过情况")}
        </span>
        {s.verified_total > 0 ? (
          <span className="text-[13px] text-ink-600">
            {t("{{p}}/{{n}} 通过", { p: s.verified_pass, n: s.verified_total })}
            <span className="ml-1 font-medium text-ink-900">({s.verified_rate}%)</span>
          </span>
        ) : (
          <span className="text-[12px] text-ink-400">{t("还没有人工改判过的结果")}</span>
        )}
        {s.product_defects > 0 && (
          <Badge status="failed">{t("疑似缺陷 {{n}} 条", { n: s.product_defects })}</Badge>
        )}
        {s.awaiting_review > 0 && (
          <span className="text-[12px] text-[var(--warn-fg)]">
            {t("另有 {{n}} 条只有自动判定、还没人工确认", { n: s.awaiting_review })}
          </span>
        )}
        {cov.modules.length > 6 && (
          <button
            className="ml-auto text-[12px] text-brand-700 hover:underline"
            onClick={() => setShowAll((v) => !v)}
          >
            {showAll ? t("收起") : t("展开全部 {{n}} 个模块", { n: cov.modules.length })}
          </button>
        )}
      </div>
      <div className="mt-2 overflow-x-auto">
        <table className="tp-table w-full text-[12px]">
          <thead>
            <tr>
              <th className="text-left">{t("模块")}</th>
              <th className="text-right">{t("人工确认")}</th>
              <th className="text-right">{t("确认通过率")}</th>
              <th className="text-right">{t("自动判定")}</th>
              <th className="text-right">{t("待人工确认")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr key={m.module}>
                <td className="max-w-[280px] truncate" title={m.module}>
                  {m.module}
                  {m.product_defects > 0 && (
                    <span className="ml-1.5 text-[10px] text-[var(--bad-fg)]">
                      {t("缺陷 {{n}}", { n: m.product_defects })}
                    </span>
                  )}
                </td>
                <td className="text-right tabular-nums">{m.verified_total || "—"}</td>
                <td className="text-right tabular-nums">
                  {m.verified_rate == null ? (
                    <span className="text-ink-400">—</span>
                  ) : (
                    <span
                      className={
                        m.verified_rate >= 90
                          ? "text-[var(--ok-fg)]"
                          : m.verified_rate >= 60
                            ? "text-[var(--warn-fg)]"
                            : "text-[var(--bad-fg)]"
                      }
                    >
                      {m.verified_rate}%
                    </span>
                  )}
                </td>
                <td className="text-right tabular-nums text-ink-400">
                  {m.auto_rate == null ? "—" : `${m.auto_rate}% (${m.auto_total})`}
                </td>
                <td className="text-right tabular-nums text-ink-400">
                  {m.awaiting_review || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** 项目文档：需求分析 / 总览 / 流程图（2026-10-12）。
 *
 *  ## 这一页解决什么
 *
 *  用户的原话：「我希望你加上一栏，里面放上需求分析或者该项目的总览……
 *  最好是和我上传的大需规一样，但是你那个要是 AI 分析并截取过的，
 *  因为我上传的大需规可能不止这一个模块」。
 *
 *  知识库里是**原件**（46 万字、586 个标题，给检索用）；这一页是**提炼**
 *  （给人读的骨架）。所以它不是"把文档再显示一遍"，而是"AI 读过之后写给人看"。
 *
 *  ## 两条界面上的硬要求
 *
 *  1. **必须显示"依据章节"**。AI 写的东西如果标不出出处，读起来和真的没区别 ——
 *     而用户没法核对就只能信或不信。所以每节下面都挂着它引用的标题。
 *  2. **生成的内容必须可编辑**。用户要"页面有很多格式可以编辑"，
 *     意思就是 AI 只是起个草稿，最终他的话说了算。所以生成完直接落进编辑器，
 *     而不是给一个只读的展示框。
 */
export function DocsPage() {
  const { t } = useTranslation();
  const { pid: pidRaw } = useParams();
  const pid = Number(pidRaw);
  const toast = useToast();
  const fail = useErrorToast();

  const [docs, setDocs] = useState<ProjectDoc[] | null>(null);
  const [active, setActive] = useState<ProjectDoc | null>(null);
  const [html, setHtml] = useState("");
  const [title, setTitle] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  // 后端给的支持格式（用于上传处的 accept，避免和 docparse 的真实支持集合漂移）。
  const [formats, setFormats] = useState<KnowledgeFormats | null>(null);
  const [genInfo, setGenInfo] = useState("");
  const [cov, setCov] = useState<VerifiedCoverage | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api.listDocs(pid);
      setDocs(r.docs);
      setActive((cur) => {
        if (cur) {
          const found = r.docs.find((d) => d.id === cur.id && d.kind === cur.kind);
          if (found) return found;
        }
        return r.docs.find((d) => d.kind === "overview") ?? r.docs[0] ?? null;
      });
    } catch (e) {
      fail(e);
    }
  }, [pid, fail]);

  useEffect(() => {
    load();
  }, [load]);

  // 支持格式取一次给「上传成文档」的 accept 用。取不到不影响功能（accept 只是提示）。
  useEffect(() => {
    api
      .knowledgeFormats(pid)
      .then(setFormats)
      .catch(() => {
        /* 静默：拿不到就用浏览器默认（什么都能选），后端仍会明确拒绝不支持的 */
      });
  }, [pid]);

  // 人工确认过的通过率：和文档分开取，取不到也不该让整页失败 ——
  // 它是"补充信息"，而文档本身才是这一页的主体。
  useEffect(() => {
    let alive = true;
    api
      .verifiedCoverage(pid)
      .then((r) => {
        if (alive) setCov(r);
      })
      .catch(() => {
        /* 静默：通过率拿不到不影响读文档 */
      });
    return () => {
      alive = false;
    };
  }, [pid]);

  // 切换文档时把编辑器内容同步过去。放在 effect 里而不是 onSelect 里，
  // 是因为 load() 之后 active 也会变（重新拉取），两条路径都要同步。
  useEffect(() => {
    if (!active) return;
    setHtml(active.html || "");
    setTitle(active.title || "");
    setDirty(false);
  }, [active]);

  const save = async () => {
    if (!active) return;
    setSaving(true);
    try {
      const r = await api.saveDoc(pid, { id: active.id, kind: active.kind, title, html });
      if (r.dropped_content) {
        // 净化丢掉了内容必须说出来，否则用户以为"保存成功但内容不见了"。
        // 用 info 而不是 error：内容确实存下了，只是有一小段不安全的部分被去掉。
        toast("info", t("已保存，但有部分内容因不安全被去掉了（脚本/内嵌框架等）。"));
      } else {
        toast("success", t("已保存"));
      }
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setSaving(false);
    }
  };

  const analyze = async (replace = false) => {
    if (!active) return;
    if (replace && !window.confirm(t("这会用 AI 生成的内容替换当前正文，确定吗？"))) return;
    setGenerating(true);
    setGenInfo("");
    try {
      const r = await api.analyzeDocs(pid, { kind: active.kind, replace });
      if (!r.ok) {
        setGenInfo(r.error || t("生成失败"));
        fail(new Error(r.error || "analyze failed"));
        return;
      }
      if (r.doc) {
        setActive(r.doc);
        setHtml(r.doc.html);
        setTitle(r.doc.title);
        setDirty(false);
      }
      const n = r.headings_used ?? 0;
      const c = r.doc_chars ?? 0;
      setGenInfo(
        t("AI 读了 {{n}} 个章节标题、{{c}} 字正文，产出 {{s}} 节。", {
          n,
          c,
          s: r.sections?.length ?? 0,
        }) + (r.appended ? t("（追加在原有内容之后）") : ""),
      );
      toast("success", t("AI 分析完成"));
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setGenerating(false);
    }
  };

  /** 切换「只有我能改」（2026-10-12）。 */
  async function toggleLock() {
    if (!active || active.id <= 0) return;
    const next = !active.ai_editable;
    try {
      const r = await api.setDocPermission(pid, active.id, next);
      // 立刻更新本地状态，别等 load() 往返 —— 勾选框要马上反映点击。
      setActive(r);
      toast(
        "success",
        next
          ? t("已设为「只有我能改」—— 助手不能再修改它（仍能读）。")
          : t("已放开 —— 助手可以修改这份文档了。"),
      );
    } catch (e) {
      fail(e);
    }
  }

  const addCustom = async () => {
    const name = window.prompt(t("新文档标题"), t("自定义文档"));
    if (!name) return;
    try {
      const r = await api.saveDoc(pid, { kind: "custom", title: name, html: "<p></p>" });
      await load();
      setActive(r);
    } catch (e) {
      fail(e);
    }
  };

  const removeCustom = async (d: ProjectDoc) => {
    if (!d.id) return;
    if (!window.confirm(t("删除「{{t}}」？", { t: d.title }))) return;
    try {
      await api.deleteDoc(pid, d.id);
      if (active?.id === d.id) setActive(null);
      await load();
      toast("success", t("已删除"));
    } catch (e) {
      fail(e);
    }
  };

  if (!docs) return <PageSkeleton />;

  const KIND_ICON: Record<string, typeof FileText> = {
    overview: FileText,
    analysis: Wand2,
    flow: GitBranch,
    custom: FileText,
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title={t("Project Docs")}
        subtitle={t("AI 读过的项目说明：总览、需求分析、流程图。可以像 Word 一样编辑。")}
        actions={
          <>
            <Button variant="outline" onClick={addCustom}>
              <Plus className="h-4 w-4" />
              {t("New document")}
            </Button>
            <Button onClick={save} disabled={!dirty || saving}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {t("Save")}
            </Button>
          </>
        }
      />

      {/* 需规 / 资料 上传（2026-10-12 从「助手」页搬过来）。
          位置本来就该在这儿：**上传需规是"项目文档"这件事的第一步** ——
          没有它，右边的「AI 分析与生成」无从下手。放在助手页时，
          用户得先知道去那儿上传、再回来生成，中间那一步没有任何提示。

          ★ 上传会**同时**走两条路（2026-10-12）：
            知识库（AI 检索用的分块）+ 项目文档（你读、你改的正文）。
          所以这里不再需要单独一个「上传成文档」按钮 ——
          两个入口会让用户不知道该点哪个，而且"允许助手修改"这个选择
          必须和上传动作在一起（见 SpecSourcePanel 里的勾选框）。 */}
      <SpecSourcePanel
        pid={pid}
        formats={formats}
        onUploaded={() => {
          /* 知识库更新不影响文档列表 */
        }}
        onDocCreated={async (doc) => {
          await load();
          setActive(doc);
        }}
      />

      {/* 文档与编辑器（2026-10-12 调整：面板移到文档下方） */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[240px_1fr]">
        {/* 左：文档清单 */}
        <Card className="h-fit p-2">
          <div className="space-y-0.5">
            {docs.map((d) => {
              const Icon = KIND_ICON[d.kind] ?? FileText;
              const on = active?.kind === d.kind && active?.id === d.id;
              return (
                <div key={`${d.kind}-${d.id}`} className="group flex items-center">
                  <button
                    onClick={() => setActive(d)}
                    className={
                      "flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors " +
                      (on ? "bg-brand-50 text-brand-700" : "hover:bg-[var(--panel2)]")
                    }
                  >
                    <Icon className="h-4 w-4 shrink-0 text-ink-400" />
                    <span className="min-w-0 flex-1 truncate">{d.title}</span>
                    {d.kind === "spec_source" && (
                      <span
                        className="shrink-0 rounded bg-[var(--panel2)] px-1 text-[10px] text-ink-500"
                        title={t("你上传的资料原文")}
                      >
                        {t("上传")}
                      </span>
                    )}
                    {d.origin === "ai" && (
                      <span
                        className="shrink-0 text-[10px] text-ink-400"
                        title={t("AI 生成后还没有人改过")}
                      >
                        AI
                      </span>
                    )}
                    {/* 列表里也要能看出哪份被锁了 —— 否则用户得逐份点开才知道
                        为什么助手"不肯"改某一份。lucide 图标不接受 title，
                        所以包一层 span 承载提示。 */}
                    {d.id > 0 && !d.ai_editable && (
                      <span
                        className="shrink-0"
                        title={t("只有我能改（助手不能写）")}
                      >
                        <Lock className="h-3 w-3 text-[var(--warn-fg)]" />
                      </span>
                    )}
                  </button>
                  {d.kind === "custom" && d.id > 0 && (
                    <button
                      onClick={() => removeCustom(d)}
                      title={t("删除")}
                      className="mr-1 hidden shrink-0 rounded p-1 text-ink-400 hover:text-[var(--bad-fg)] group-hover:block"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </Card>

        {/* 右：编辑区 */}
        <div className="min-w-0 space-y-3">
          {/* ★ 这个空态**实际上到不了**（2026-10-12 实测确认）：后端
              `list_project_docs` 总会补上「项目总览 / 需求分析 / 流程图」三个占位，
              所以 `docs` 永远非空、`active` 也永远有值 —— 我改了这里的文案之后
              在浏览器里根本看不到它，才发现这一点。
              保留它作为兜底（后端某天改成不补占位时还要有个东西显示），
              但文案要说真话，不能指向一个已经删掉的按钮。 */}
          {!active ? (
            <Card className="p-6">
              <Empty
                title={t("还没有文档")}
                sub={t(
                  "在下面「需规 / 资料」里上传文件：它会同时存成可查看可编辑的文档，并加进 AI 检索的资料库。上传前可以选「允许助手修改」。",
                )}
              />
            </Card>
          ) : (
            <>
              <Card className="flex flex-wrap items-center gap-2 p-3">
                <Input
                  value={title}
                  onChange={(e) => {
                    setTitle(e.target.value);
                    setDirty(true);
                  }}
                  className="h-9 max-w-[280px] font-medium"
                />
                {active.kind !== "custom" && (
                  <>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={generating}
                      onClick={() => analyze(false)}
                    >
                      {generating ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Sparkles className="h-3.5 w-3.5" />
                      )}
                      {t("AI 分析与生成")}
                    </Button>
                    {active.html?.trim() && (
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={generating}
                        onClick={() => analyze(true)}
                        title={t("用新生成的内容替换现有正文")}
                      >
                        {t("重新生成（覆盖）")}
                      </Button>
                    )}
                  </>
                )}
                {/* 出处徽标只对**有正文**的节显示。流程图没有正文
                    （它的内容就是那张图），显示"正文引用了 N 个条目"会误导。 */}
                {active.sources?.length > 0 && active.kind !== "flow" && (
                  <Badge status="passed">
                    {t("正文引用了 {{n}} 个原文条目", { n: active.sources.length })}
                  </Badge>
                )}

                {/* ★ 「只有我能改」开关（2026-10-12 用户要求）。
                    读不受影响 —— 助手永远能读全部；这一项只拦**写**。
                    占位文档（id=0）没有行可改，所以不显示这个开关。

                    2026-10-13：`spec_source`（上传的资料原文）**也放开**这个开关 ——
                    原来把它排除在外（"原件不给开"），于是用户看到上传的需规挂着锁、
                    却找不到任何地方改它，观感就是"我不可以修改"。上传时选过之后
                    仍然应该能改，这里补上。 */}
                {active.id > 0 && (
                  <label
                    className="flex cursor-pointer select-none items-center gap-1.5 rounded-lg border border-[var(--line)] px-2.5 py-1.5 text-[12px] text-ink-700"
                    title={t("勾上之后助手不能修改这份文档（它仍然能读）")}
                  >
                    <input
                      type="checkbox"
                      checked={!active.ai_editable}
                      onChange={toggleLock}
                      className="h-3.5 w-3.5 accent-[var(--brand-600)]"
                    />
                    {active.ai_editable ? (
                      <Unlock className="h-3.5 w-3.5 text-ink-400" />
                    ) : (
                      <Lock className="h-3.5 w-3.5 text-[var(--warn-fg)]" />
                    )}
                    {t("只有我能改")}
                  </label>
                )}
              </Card>

              {/* 锁住时明确说清"是**助手**不能改"，不是"你不能改" ——
                  含糊会让人以为文档坏了。 */}
              {!active.ai_editable && (
                <div className="tp-alert tp-alert-warn flex items-start gap-2 px-3 py-2 text-[12px]">
                  <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>
                    {t("这份文档已设为「只有我能改」：助手不能写它，但仍然能读。你自己照常编辑。")}
                  </span>
                </div>
              )}

              {genInfo && (
                <div className="tp-alert tp-alert-info px-3 py-2 text-[12px]">{genInfo}</div>
              )}

              {/* 依据章节：AI 写的东西必须能核对，否则和编造没区别 */}
              {active.sources?.length > 0 && active.kind !== "flow" && (
                <details className="rounded-lg border border-[var(--line)] bg-[var(--panel2)] px-3 py-2 text-[12px]">
                  <summary className="cursor-pointer text-ink-600">
                    {t("正文里逐字提到的原文条目（点开核对）")}
                  </summary>
                  <p className="mt-1 text-[11px] text-ink-400">
                    {t(
                      "这些名称都在正文里出现过，所以能对上。没列出的部分属于 AI 的综合描述，需要你自己核对。",
                    )}
                  </p>
                  <ul className="mt-1 list-disc pl-5 text-ink-500">
                    {active.sources.slice(0, 80).map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                </details>
              )}

              {/* ★ 流程图预览放**编辑器上面**（2026-10-12 用户反馈「流程图无法点开查看」）。
                  原来它在编辑器下面，而流程图文档的正文是 20 多行 mermaid 源码，
                  于是"打开流程图文档"看到的是满屏源码，图被挤到页面最底部
                  （实测在 2000px 处）—— 用户以为打不开。
                  放上面：切过去第一眼就是图，源码在下面等着改。 */}
              <MermaidPreview html={html} />

              <RichTextEditor
                value={html}
                onChange={(v) => {
                  setHtml(v);
                  setDirty(true);
                }}
                onUploadImage={async (f) => (await api.uploadDocImage(pid, f)).url}
                placeholder={t("在这里写项目说明，或点上面的「AI 分析与生成」让它先起草…")}
              />
            </>
          )}
        </div>
      </div>

      {/* 人工确认过的通过率（2026-10-12）。
          只认人工改判过的结果 —— 自动判定里相当一部分不是产品结论。 */}
      {cov && cov.modules.length > 0 && <CoveragePanel cov={cov} />}

      {/* 项目概况面板：需求覆盖矩阵 / 规则 / 接口 / 账号 / 变更 / 失败台账。
          放在文档**下面**：这些是从库里算出来的事实，属于"读完说明书后要查的东西"，
          而不是"打开页面先看的东西"。默认全部收起，需要时再展开。 */}
      <InsightsPanel pid={pid} />
    </div>
  );
}

