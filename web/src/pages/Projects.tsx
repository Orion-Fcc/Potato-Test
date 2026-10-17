import {
  ArrowRight,
  ChevronDown,
  ChevronRight,
  FolderOpen,
  Loader2,
  MoreHorizontal,
  Pencil,
  Plus,
  Trash2,
  Upload,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { api, relTime, type Project, type ProjectGroup } from "../lib/api";
import { pct, rateTone } from "../lib/metrics";
import { useAbortSignal, useCollapsedGroups, useDebounced } from "../lib/hooks";
import { Badge, Button, Card, Field, Input, PageHeader } from "../components/ui";
import { Modal } from "../components/Modal";
import { NewProjectWizard } from "../components/NewProjectWizard";
import { useToast, useErrorToast } from "../components/toast";

/** Edit-only draft. Creating a project goes through NewProjectWizard. */
type Draft = { id: number; name: string; base_url: string; group_id: number | null };

/**
 * 大系统右上角的「⋯」菜单（2026-10-13）。
 *
 * 原来"改名 / 共享需规 / 删除"是三个并排的小按钮，其中两个是图标、一个是文字，
 * 形态不统一、要 hover 才明显。收进一个菜单后：入口只有一个、三个动作形态一致。
 */
function GroupMenu({
  onRename,
  onKnowledge,
  onDelete,
}: {
  onRename: () => void;
  onKnowledge: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const item =
    "flex w-full items-center rounded-md px-2.5 py-1.5 text-left text-[12.5px] text-ink-700 outline-none hover:bg-[var(--panel2)]";
  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-label={t("更多")}
        title={t("更多")}
        aria-expanded={open}
        className="rounded-md p-1.5 text-ink-400 outline-none transition-colors hover:bg-[var(--panel)] hover:text-ink-900"
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-1 w-40 overflow-hidden rounded-lg border border-[var(--line)] bg-[var(--panel)] p-1 shadow-xl">
          <button
            className={item}
            onClick={() => {
              setOpen(false);
              onRename();
            }}
          >
            {t("改名")}
          </button>
          <button
            className={item}
            onClick={() => {
              setOpen(false);
              onKnowledge();
            }}
          >
            {t("共享需规")}
          </button>
          <button
            className={item + " text-[var(--bad-fg)]"}
            onClick={() => {
              setOpen(false);
              onDelete();
            }}
          >
            {t("删除")}
          </button>
        </div>
      )}
    </div>
  );
}

export function Projects() {
  const { t } = useTranslation();
  const toast = useToast();
  const fail = useErrorToast();
  const signal = useAbortSignal();
  // 大系统分组的折叠状态（和侧栏共用同一个 localStorage 键）。
  const { collapsed: collapsedGroups, toggle: toggleGroup } = useCollapsedGroups();
  const [projects, setProjects] = useState<Project[]>([]);
  // 「大系统」容器（2026-10-13）：把若干小模块项目归到它下面。
  const [groups, setGroups] = useState<ProjectGroup[]>([]);
  const [q, setQ] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [wizard, setWizard] = useState(false);
  const [confirmDel, setConfirmDel] = useState<Project | null>(null);
  // 大系统的三个弹窗：新建/改名、编辑共享需规、删除确认。
  const [groupDraft, setGroupDraft] = useState<{ id: number | null; name: string } | null>(null);
  const [kbEdit, setKbEdit] = useState<{ id: number; name: string; text: string } | null>(null);
  // 共享需规的文件上传（2026-10-17）：解析在服务端做（和项目文档同一套 docparse），
  // 这里只管把文件递过去、把回传的全文贴回编辑框。
  const [kbUploading, setKbUploading] = useState(false);
  const kbFileRef = useRef<HTMLInputElement>(null);
  const [confirmDelGroup, setConfirmDelGroup] = useState<ProjectGroup | null>(null);

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
  const loadGroups = useMemo(
    () => () =>
      api
        .listProjectGroups({ signal })
        .then(setGroups)
        .catch(() => {
          /* 大系统拉不到不该让整页打不开 —— 项目列表本身才是主体 */
        }),
    [signal],
  );
  useEffect(() => {
    load();
    loadGroups();
  }, [load, loadGroups]);

  const dq = useDebounced(q, 180);
  // ★ 搜索也匹配**大系统名**（2026-10-13 用户要求「搜索要能按大系统」）：
  // 输入"培训管理"应当把它下面的项目都带出来，而不是因为没有项目名匹配就报"无结果"。
  const groupName = useMemo(() => {
    const m = new Map<number, string>();
    for (const g of groups) m.set(g.id, g.name);
    return m;
  }, [groups]);
  const filtered = useMemo(
    () =>
      projects.filter((p) => {
        const needle = dq.trim().toLowerCase();
        if (!needle) return true;
        if (p.name.toLowerCase().includes(needle)) return true;
        const g = p.group_id != null ? groupName.get(p.group_id) : undefined;
        return !!g && g.toLowerCase().includes(needle);
      }),
    [projects, dq, groupName],
  );

  // 按大系统分组：group_id -> 项目列表（null = 未归类）。
  const byGroup = useMemo(() => {
    const m = new Map<number | null, Project[]>();
    for (const p of filtered) {
      const k = p.group_id ?? null;
      const arr = m.get(k);
      if (arr) arr.push(p);
      else m.set(k, [p]);
    }
    return m;
  }, [filtered]);
  const ungrouped = byGroup.get(null) ?? [];

  const save = async () => {
    if (!draft?.name.trim()) return toast("error", t("Name is required"));
    try {
      await api.updateProject(draft.id, {
        name: draft.name.trim(),
        base_url: draft.base_url,
        // null = 移出大系统（后端按"有没有传这个键"判断，传 null 是有意义的）
        group_id: draft.group_id,
      });
      setDraft(null);
      load();
      loadGroups();
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
      loadGroups();
      toast("success", t("Project deleted"));
    } catch (e) {
      fail(e);
    }
  };

  const saveGroup = async () => {
    if (!groupDraft?.name.trim()) return toast("error", t("Name is required"));
    try {
      if (groupDraft.id == null) await api.createProjectGroup(groupDraft.name.trim());
      else await api.updateProjectGroup(groupDraft.id, { name: groupDraft.name.trim() });
      setGroupDraft(null);
      loadGroups();
      toast("success", t("Saved"));
    } catch (e) {
      fail(e);
    }
  };

  const doDeleteGroup = async () => {
    if (!confirmDelGroup) return;
    try {
      await api.deleteProjectGroup(confirmDelGroup.id);
      setConfirmDelGroup(null);
      load();
      loadGroups();
      toast("success", t("大系统已删除，它下面的项目已退回平铺"));
    } catch (e) {
      fail(e);
    }
  };

  const openKb = async (g: ProjectGroup) => {
    try {
      const r = await api.getGroupKnowledge(g.id);
      setKbEdit({ id: g.id, name: g.name, text: r.text });
    } catch (e) {
      fail(e);
    }
  };
  const saveKb = async () => {
    if (!kbEdit) return;
    try {
      const r = await api.setGroupKnowledge(kbEdit.id, kbEdit.text);
      setKbEdit(null);
      loadGroups();
      toast("success", t("已保存共享需规：{{c}} 字", { c: r.chars }));
    } catch (e) {
      fail(e);
    }
  };

  /**
   * 上传文件进共享需规（2026-10-17 用户要求「共享需规也要以上传的形式」）。
   *
   * 服务端解析后**追加**到共享那份，并把合并后的全文回传 —— 这里直接用回传的文本
   * 刷新编辑框，不去前端自己拼接（分隔符/编码的坑不值得重造一遍）。
   * 附带的 warnings 要原样显示：某份读不了必须点名，否则用户以为传进去了。
   */
  const onKbUpload = async (list: FileList | null) => {
    const files = Array.from(list ?? []);
    if (!kbEdit || !files.length) return;
    setKbUploading(true);
    try {
      const r = await api.uploadGroupKnowledge(kbEdit.id, files);
      setKbEdit((cur) => (cur ? { ...cur, text: r.text } : cur));
      if (r.added.length) {
        toast("success", t("已追加 {{n}} 份资料（共 {{c}} 字）", { n: r.added.length, c: r.chars }));
      }
      if (r.warnings?.length) toast("info", r.warnings.join("；"));
      loadGroups();
    } catch (e) {
      fail(e);
    } finally {
      setKbUploading(false);
      if (kbFileRef.current) kbFileRef.current.value = "";
    }
  };

  /** 一张项目卡。抽出来是因为分组后要在"大系统里"和"未归类"两处复用。 */
  const renderCard = (p: Project) => {
    const rate = p.last_run?.pass_rate;
    return (
      <Card key={p.id} hover className="group relative flex flex-col overflow-hidden p-5">
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
              onClick={() =>
                setDraft({
                  id: p.id,
                  name: p.name,
                  base_url: p.base_url ?? "",
                  group_id: p.group_id ?? null,
                })
              }
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
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("Workspace")}
        title={t("Projects")}
        subtitle={t(
          "Potato Test runs your natural-language test cases in a real browser. Pick a project to begin.",
        )}
        actions={
          // 三个控件收成**一组**（2026-10-13）：原来搜索框和两个按钮散在一行、间距不均。
          <div className="flex items-center gap-2">
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("Search projects")}
              className="w-52"
            />
            <Button variant="outline" onClick={() => setGroupDraft({ id: null, name: "" })}>
              <FolderOpen className="h-4 w-4" />
              {t("新建大系统")}
            </Button>
            <Button onClick={() => setWizard(true)}>
              <Plus className="h-4 w-4" />
              {t("New project")}
            </Button>
          </div>
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
              {t(
                "A project holds the cases for one system under test. Creating one takes about a minute — a URL, an account, and your first case.",
              )}
            </p>
            <Button className="mt-2" onClick={() => setWizard(true)}>
              <Plus className="h-4 w-4" />
              {t("Create your first project")}
            </Button>
          </div>
        </Card>
      ) : (
        <div className="space-y-5">
          {/* 每个「大系统」= 一个**区块**（2026-10-13 重做）：原来分组标题只比卡片名大一点点，
              而页面大标题却很重 —— 层级是反的。现在整组包进一块带边框的区块，"组"才是主结构。 */}
          {groups.map((g) => {
            const ps = byGroup.get(g.id) ?? [];
            const off = collapsedGroups.has(g.id);
            return (
              <section
                key={g.id}
                className="rounded-[var(--radius)] border border-[var(--line)] bg-[color-mix(in_oklch,var(--panel2)_38%,transparent)] p-3.5"
              >
                <div className="mb-3 flex items-center gap-2">
                  {/* 点标题收起/展开这一组（2026-10-13 用户要求）。 */}
                  <button
                    onClick={() => toggleGroup(g.id)}
                    title={off ? t("展开") : t("收起")}
                    aria-expanded={!off}
                    className="flex min-w-0 items-center gap-2 text-left outline-none"
                  >
                    {off ? (
                      <ChevronRight className="h-4 w-4 shrink-0 text-ink-400" />
                    ) : (
                      <ChevronDown className="h-4 w-4 shrink-0 text-ink-400" />
                    )}
                    <FolderOpen className="h-4 w-4 shrink-0 text-brand-600" />
                    <h2 className="truncate text-[15px] font-semibold tracking-tight text-ink-900">
                      {g.name}
                    </h2>
                    <span className="shrink-0 rounded-full bg-[var(--panel)] px-2 py-0.5 text-[11px] text-ink-500">
                      {t("{{n}} 个项目", { n: g.projects })}
                    </span>
                    {g.knowledge.chars > 0 && (
                      <span className="shrink-0 text-[11px] text-ink-400">
                        {t("共享需规 {{c}} 字", { c: g.knowledge.chars })}
                      </span>
                    )}
                  </button>
                  <div className="ml-auto shrink-0">
                    <GroupMenu
                      onRename={() => setGroupDraft({ id: g.id, name: g.name })}
                      onKnowledge={() => openKb(g)}
                      onDelete={() => setConfirmDelGroup(g)}
                    />
                  </div>
                </div>
                {!off && (
                  <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
                    {ps.map(renderCard)}
                    {ps.length === 0 && (
                      <div className="grid min-h-[80px] place-items-center rounded-[var(--radius)] border border-dashed border-[var(--line-strong)] px-4 text-center text-[12px] text-ink-400">
                        {t("还没有项目归到它下面 —— 在项目的「Edit」里选「所属大系统」。")}
                      </div>
                    )}
                  </div>
                )}
              </section>
            );
          })}

          {/* 未归类：**为空时整节不渲染**（2026-10-13）。原来"0 个项目"的空标题 + 一个大虚线
              框纯属噪音；而且把"新建项目"放在它下面，语义上是错的 —— 新建项目不归任何组。 */}
          {ungrouped.length > 0 && (
            <section className="space-y-3">
              {groups.length > 0 && (
                <div className="flex items-center gap-2 border-b border-[var(--line)] pb-2">
                  <h2 className="text-[13px] font-semibold text-ink-500">{t("未归类")}</h2>
                  <span className="text-[11.5px] text-ink-400">
                    {t("{{n}} 个项目", { n: ungrouped.length })}
                  </span>
                </div>
              )}
              <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
                {ungrouped.map(renderCard)}
              </div>
            </section>
          )}

          {/* 「新建项目」是**全局动作**，不属于任何一组 —— 单独一条，别再塞进"未归类"里。 */}
          <button
            onClick={() => setWizard(true)}
            className="flex w-full items-center justify-center gap-1.5 rounded-[var(--radius)] border border-dashed border-[var(--line-strong)] py-3 text-[13px] text-ink-500 outline-none transition-[border-color,color,background-color] hover:border-brand-200 hover:bg-brand-50/40 hover:text-brand-700"
          >
            <Plus className="h-4 w-4" />
            {t("New project")}
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
              <Field label={t("所属大系统（可选）")}>
                <select
                  value={draft.group_id ?? ""}
                  onChange={(e) =>
                    setDraft({ ...draft, group_id: e.target.value ? Number(e.target.value) : null })
                  }
                  className="h-9 w-full rounded-[var(--radius)] border border-[var(--line-strong)] bg-[var(--panel)] px-3 text-[13px] text-ink-900 outline-none"
                >
                  <option value="">{t("不属于任何大系统")}</option>
                  {groups.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                </select>
              </Field>
              <div className="flex justify-end gap-2 pt-1">
                <Button variant="outline" onClick={() => setDraft(null)}>
                  {t("Cancel")}
                </Button>
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
              <Button variant="outline" onClick={() => setConfirmDel(null)}>
                {t("Cancel")}
              </Button>
              <Button variant="danger" onClick={doDelete}>
                {t("Delete")}
              </Button>
            </div>
          </Card>
        </Modal>
      )}

      {/* 新建 / 改名大系统 */}
      {groupDraft && (
        <Modal onClose={() => setGroupDraft(null)} className="max-w-md">
          <Card className="p-5">
            <h2 className="mb-1 text-lg font-semibold text-ink-900">
              {groupDraft.id == null ? t("新建大系统") : t("改名")}
            </h2>
            <p className="mb-4 text-[12px] leading-relaxed text-ink-500">
              {t("大系统用来把若干小模块项目归拢在一起；它自己可以放一份共享需规，下面的项目都检索得到。")}
            </p>
            <Field label={t("大系统名称")}>
              <Input
                value={groupDraft.name}
                onChange={(e) => setGroupDraft({ ...groupDraft, name: e.target.value })}
                placeholder={t("例如：民航培训管理系统")}
                autoFocus
              />
            </Field>
            <div className="mt-4 flex justify-end gap-2">
              <Button variant="outline" onClick={() => setGroupDraft(null)}>
                {t("Cancel")}
              </Button>
              <Button onClick={saveGroup}>{t("Save")}</Button>
            </div>
          </Card>
        </Modal>
      )}

      {/* 大系统共享需规 */}
      {kbEdit && (
        <Modal onClose={() => setKbEdit(null)} className="max-w-3xl">
          <Card className="p-5">
            <h2 className="mb-1 text-lg font-semibold text-ink-900">
              {t("共享需规")} · {kbEdit.name}
            </h2>
            <p className="mb-3 text-[12px] leading-relaxed text-ink-500">
              {t(
                "粘贴这个系统级的需规/资料，或者直接上传文件。**它下面的每个项目**检索时都会带上它 —— 传一次，各模块共用。",
              )}
            </p>
            {/* 上传入口（2026-10-17 用户要求「共享需规也要以上传的形式」）。
                走服务端解析，和「项目文档」那边是同一套格式支持；上传是**追加**，
                不会覆盖已经在里面的内容（要整体替换直接改下面的文本再保存）。 */}
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={kbUploading}
                onClick={() => kbFileRef.current?.click()}
              >
                {kbUploading ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Upload className="h-3.5 w-3.5" />
                )}
                {kbUploading ? t("读取中…") : t("上传文件")}
              </Button>
              <span className="text-[11.5px] leading-relaxed text-ink-400">
                {t(
                  "支持 md / txt / json / csv / docx / pdf / xlsx / html / rtf；上传的内容会**追加**到下面，不会覆盖已有内容。",
                )}
              </span>
              <input
                ref={kbFileRef}
                type="file"
                multiple
                accept=".md,.txt,.json,.csv,.docx,.pdf,.xlsx,.html,.rtf"
                className="hidden"
                onChange={(e) => onKbUpload(e.target.files)}
              />
            </div>
            <textarea
              value={kbEdit.text}
              onChange={(e) => setKbEdit({ ...kbEdit, text: e.target.value })}
              rows={16}
              className="w-full rounded-[var(--radius)] border border-[var(--line-strong)] bg-[var(--panel)] p-3 font-mono text-[12px] leading-relaxed text-ink-900 outline-none"
              placeholder={t("把系统级需规粘贴到这里……")}
            />
            <div className="mt-4 flex items-center gap-2">
              <span className="text-[11.5px] text-ink-400">
                {t("{{c}} 字", { c: kbEdit.text.length })}
              </span>
              <div className="ml-auto flex gap-2">
                <Button variant="outline" onClick={() => setKbEdit(null)}>
                  {t("Cancel")}
                </Button>
                <Button onClick={saveKb}>{t("Save")}</Button>
              </div>
            </div>
          </Card>
        </Modal>
      )}

      {confirmDelGroup && (
        <Modal onClose={() => setConfirmDelGroup(null)} className="max-w-md">
          <Card className="p-5">
            <h2 className="text-lg font-semibold text-ink-900">{t("删除大系统")}</h2>
            <p className="mt-2 text-sm text-ink-500">
              {t(
                "删除「{{name}}」？它下面的 {{n}} 个项目**不会被删**，只会退回未归类；它那份共享需规会被删掉。",
                { name: confirmDelGroup.name, n: confirmDelGroup.projects },
              )}
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="outline" onClick={() => setConfirmDelGroup(null)}>
                {t("Cancel")}
              </Button>
              <Button variant="danger" onClick={doDeleteGroup}>
                {t("Delete")}
              </Button>
            </div>
          </Card>
        </Modal>
      )}
    </div>
  );
}
