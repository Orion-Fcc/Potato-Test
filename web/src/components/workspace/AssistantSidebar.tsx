import {
  CircleStop,
  GitBranch,
  Loader2,
  Paperclip,
  Send,
  Sparkles,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import { api, type AssistantAttachment } from "../../lib/api";
import { markdownToHtml } from "../../lib/markdown";
import { latestMermaidSource, mermaidHtml } from "../../lib/mermaidSource";
import { readSSE, type SSEEvent } from "../../lib/sse";
import { EMPTY_SELECTION, useWorkspace, type SelectionInfo } from "../../state/WorkspaceContext";
import { Button } from "../ui";
import { useToast } from "../toast";
import { MarkdownBody } from "./MarkdownBody";
import { SuggestionCard } from "./SuggestionCard";

type TurnRole = "user" | "assistant";
type TurnKind = "text" | "flow";
type TurnStatus = "streaming" | "done" | "error";
type Applied = "replace" | "insert" | "discard";
/** doc = 只读写左侧当前文档；global = 走原「助手」页的全局能力（工具调用）。 */
type Mode = "doc" | "global";
type ToolAction = { tool: string; args: Record<string, unknown>; ok: boolean };

type Turn = {
  id: string;
  role: TurnRole;
  content: string;
  kind?: TurnKind;
  status?: TurnStatus;
  error?: string;
  note?: string;
  applied?: Applied;
  /** 缺失时按 doc 处理（兼容合并前存下的旧对话）。 */
  origin?: Mode;
  /** 全局模式下的工具调用记录。 */
  actions?: ToolAction[];
};

const HISTORY_LIMIT = 60;
const MAX_TURNS_TO_MODEL = 12;

let seq = 0;
function nextId(): string {
  seq += 1;
  return `${Date.now().toString(36)}-${seq}`;
}

/** 去掉模型偶尔带上的围栏标记。 */
export function stripFences(raw: string): string {
  let s = (raw || "").trim();
  const fence = /^```[a-zA-Z]*\s*\n?([\s\S]*?)\n?```$/;
  const m = fence.exec(s);
  if (m) s = m[1].trim();
  return s.replace(/^~~~[a-zA-Z]*\s*\n?/, "").replace(/\n?~~~$/, "").trim();
}

function loadTurns(pid: number): Turn[] {
  if (!Number.isFinite(pid)) return [];
  try {
    const raw =
      localStorage.getItem(`tp.workspace.chat.${pid}`) ??
      localStorage.getItem(`tp.assistant.chat.${pid}`);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((x): x is Turn => {
        if (!x || typeof x !== "object") return false;
        const t = x as Partial<Turn>;
        return (t.role === "user" || t.role === "assistant") && typeof t.content === "string";
      })
      .map((t) => ({ ...t, status: t.status ?? "done" }))
      .slice(-HISTORY_LIMIT);
  } catch {
    return [];
  }
}

function loadMode(pid: number): Mode {
  if (!Number.isFinite(pid)) return "doc";
  try {
    return localStorage.getItem(`tp.workspace.ai.mode.${pid}`) === "global" ? "global" : "doc";
  } catch {
    return "doc";
  }
}

function describeAction(a: ToolAction): string {
  const arg = a.args as { method?: string; path?: string; query?: string };
  if (arg.method && arg.path) return `${arg.method} ${arg.path}`;
  if (a.tool === "search_knowledge") return `${a.tool}("${arg.query ?? ""}")`;
  return a.tool;
}

/**
 * 常驻 AI 侧栏（2026-10-20，2026-10-21 合并原「助手」页）。
 *
 * 两种模式：
 *  - **当前文档模式**（默认）：POST + SSE，只读写左侧文档；生成后由用户
 *    点「替换 / 插入」才写入正文。
 *  - **全局项目模式**：复用 `/assistant` 的工具调用能力，可以查用例、运行、
 *    失败与问题，也可以直接改用例；回复下方展示实际操作记录。
 *
 * 对话按项目存在同一个 localStorage key 里，刷新 / 切模式都不丢。
 */
export function AssistantSidebar() {
  const { t } = useTranslation();
  const { pid: pidRaw } = useParams();
  const pid = Number(pidRaw);
  const toast = useToast();
  const { doc, selection, setStreaming, apiRef, flowTriggerRef } = useWorkspace();

  const [turns, setTurns] = useState<Turn[]>(() => loadTurns(pid));
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setModeState] = useState<Mode>(() => loadMode(pid));

  // 全局模式的附件（粘贴 / 拖入 / 点选）。文件先由后端解析成文本再随这一轮发出。
  const [atts, setAtts] = useState<AssistantAttachment[]>([]);
  const [attBusy, setAttBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const attRef = useRef<HTMLInputElement>(null);

  const turnsRef = useRef<Turn[]>(turns);
  turnsRef.current = turns;
  const busyRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const firstPersist = useRef(true);

  const docRef = useRef(doc);
  docRef.current = doc;

  useEffect(() => {
    setTurns(loadTurns(pid));
    setModeState(loadMode(pid));
  }, [pid]);

  useEffect(() => {
    if (firstPersist.current) {
      firstPersist.current = false;
      return;
    }
    if (!Number.isFinite(pid)) return;
    try {
      localStorage.setItem(
        `tp.workspace.chat.${pid}`,
        JSON.stringify(turns.slice(-HISTORY_LIMIT)),
      );
    } catch {
      /* 存储满了不影响对话 */
    }
  }, [turns, pid]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  const changeMode = useCallback(
    (next: Mode) => {
      setModeState(next);
      if (!Number.isFinite(pid)) return;
      try {
        localStorage.setItem(`tp.workspace.ai.mode.${pid}`, next);
      } catch {
        /* 存不下就只在本次会话生效 */
      }
    },
    [pid],
  );

  const patchTurn = useCallback((id: string, patch: Partial<Turn>) => {
    setTurns((prev) => prev.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  }, []);

  /** 解析并挂上文件。逐个失败逐个报，不让一个坏文件把好的也丢掉。 */
  const addFiles = useCallback(
    async (files: File[]) => {
      if (!files.length || !Number.isFinite(pid)) return;
      setAttBusy(true);
      const ok: AssistantAttachment[] = [];
      const bad: string[] = [];
      for (const f of files) {
        try {
          ok.push(await api.parseAssistantAttachment(pid, f));
        } catch (e) {
          bad.push(`${f.name}: ${e instanceof Error ? e.message : String(e)}`);
        }
      }
      if (ok.length) setAtts((s) => [...s, ...ok]);
      if (bad.length) toast("error", bad.join("；"));
      setAttBusy(false);
    },
    [pid, toast],
  );

  const begin = useCallback(
    async (kind: "chat" | "flow" | "global", message: string) => {
      if (busyRef.current || !Number.isFinite(pid)) return;
      const isGlobal = kind === "global";
      const ctx = isGlobal ? null : apiRef.current?.getContext();
      const docMeta = ctx?.doc ?? docRef.current;
      const sel: SelectionInfo = ctx?.selection ?? EMPTY_SELECTION;
      const sentAtts = isGlobal ? atts : [];

      // 对话里显示附件名，回看历史时知道这一轮基于什么；正文只随请求发送。
      const label =
        sentAtts.length > 0
          ? `${message || t("（见附件）")}\n📎 ${sentAtts.map((a) => a.filename).join("、")}`
          : message;

      const userTurn: Turn = {
        id: nextId(),
        role: "user",
        content: label,
        status: "done",
        origin: isGlobal ? "global" : "doc",
      };
      const replyId = nextId();
      const replyTurn: Turn = {
        id: replyId,
        role: "assistant",
        content: "",
        kind: kind === "flow" ? "flow" : "text",
        status: "streaming",
        origin: isGlobal ? "global" : "doc",
      };
      setTurns((prev) => [...prev, userTurn, replyTurn]);
      if (isGlobal) setAtts([]);

      busyRef.current = true;
      setBusy(true);
      if (!isGlobal) setStreaming(true);
      const ac = new AbortController();
      abortRef.current = ac;
      let acc = "";
      // 流式 token 可能每秒几十个；每帧都 setState 会让长文档卡顿。
      // 攒 50ms 刷一次气泡，肉眼仍是实时输出。
      let flushTimer: ReturnType<typeof setTimeout> | null = null;
      let lastFlush = 0;
      const cancelFlush = () => {
        if (flushTimer) {
          clearTimeout(flushTimer);
          flushTimer = null;
        }
      };
      const flush = () => {
        flushTimer = null;
        lastFlush = Date.now();
        patchTurn(replyId, { content: acc });
      };
      const scheduleFlush = () => {
        const wait = 50 - (Date.now() - lastFlush);
        if (wait <= 0) {
          cancelFlush();
          flush();
          return;
        }
        if (!flushTimer) flushTimer = setTimeout(flush, wait);
      };

      const handle = (ev: SSEEvent) => {
        let data: Record<string, unknown> = {};
        try {
          data = JSON.parse(ev.data) as Record<string, unknown>;
        } catch {
          data = { text: ev.data };
        }
        if (ev.event === "token") {
          if (typeof data.text === "string") {
            acc += data.text;
            scheduleFlush();
          }
          return;
        }
        if (ev.event === "done") {
          if (kind === "flow" && typeof data.mermaid === "string") acc = data.mermaid;
          cancelFlush();
          patchTurn(replyId, {
            content: acc,
            status: "done",
            note: typeof data.note === "string" && data.note.trim() ? data.note.trim() : undefined,
          });
          return;
        }
        if (ev.event === "error") {
          const msg =
            (typeof data.message === "string" && data.message) ||
            (typeof data.error === "string" && data.error) ||
            (typeof data.detail === "string" && data.detail) ||
            t("生成失败，请稍后重试。");
          throw new Error(msg);
        }
      };

      try {
        if (isGlobal) {
          const history = turnsRef.current
            .filter((x) => x.status !== "streaming" && x.status !== "error" && x.content.trim())
            .slice(-MAX_TURNS_TO_MODEL)
            .map((x) => ({ role: x.role, content: x.content.slice(0, 4000) }));
          const res = await api.assistant(
            pid,
            { message: message || t("请看附件内容。"), history, attachments: sentAtts },
            { signal: ac.signal },
          );
          acc = res.reply || "";
          patchTurn(replyId, {
            content: acc,
            status: acc.trim() ? "done" : "error",
            error: acc.trim() ? undefined : t("没有收到回复。"),
            actions: res.actions,
          });
        } else if (kind === "flow") {
          await readSSE(`/projects/${pid}/docs/flow/stream`, {
            instruction: message,
            current: latestMermaidSource(ctx?.html ?? ""),
            selection: sel,
          }, { onEvent: handle, signal: ac.signal });
        } else {
          const history = turnsRef.current
            .filter((x) => x.status !== "streaming" && x.status !== "error" && x.content.trim())
            .slice(-MAX_TURNS_TO_MODEL)
            .map((x) => ({ role: x.role, content: x.content.slice(0, 4000) }));
          await readSSE(`/projects/${pid}/assistant/stream`, {
            message,
            history,
            doc: docMeta,
            selection: sel,
            content: (ctx?.html ?? "").slice(-12000),
          }, { onEvent: handle, signal: ac.signal });
        }
        // 服务端没发 done（连接被中途关闭）时也要收尾，不能永远转圈。
        cancelFlush();
        if (!isGlobal) {
          setTurns((prev) =>
            prev.map((x) =>
              x.id === replyId && x.status === "streaming"
                ? {
                    ...x,
                    content: acc,
                    status: acc.trim() ? "done" : "error",
                    error: acc.trim() ? undefined : t("没有收到回复。"),
                  }
                : x,
            ),
          );
        }
      } catch (e) {
        cancelFlush();
        if (ac.signal.aborted) {
          setTurns((prev) =>
            prev.map((x) =>
              x.id === replyId
                ? { ...x, content: acc, status: "done", note: acc.trim() ? x.note : t("已停止生成。") }
                : x,
            ),
          );
        } else {
          const msg = e instanceof Error ? e.message : String(e);
          setTurns((prev) =>
            prev.map((x) =>
              x.id === replyId ? { ...x, content: acc, status: "error", error: msg } : x,
            ),
          );
        }
      } finally {
        cancelFlush();
        busyRef.current = false;
        setBusy(false);
        if (!isGlobal) setStreaming(false);
        abortRef.current = null;
      }
    },
    [apiRef, atts, patchTurn, pid, setStreaming, t],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  // 工具栏上的「AI 流式生成」复用它。挂 ref 而不是把 begin 透传下去，
  // 是为了让侧栏内部状态变化不引起整页重渲染。
  const beginRef = useRef(begin);
  beginRef.current = begin;
  useEffect(() => {
    flowTriggerRef.current = (instruction?: string) => {
      const text = (instruction ?? "").trim() || t("请根据左侧文档和项目需规，画一张主要业务流程图。");
      void beginRef.current("flow", text);
    };
    return () => {
      flowTriggerRef.current = null;
    };
  }, [flowTriggerRef, t]);

  const startFlow = () => {
    if (busy) return;
    void begin("flow", input.trim() || t("请根据左侧文档和项目需规，画一张主要业务流程图。"));
    setInput("");
  };

  const send = () => {
    const text = input.trim();
    if (busy) return;
    if (mode === "global" ? !text && atts.length === 0 : !text) return;
    setInput("");
    void begin(mode === "global" ? "global" : "chat", text);
  };

  const apply = useCallback(
    (turn: Turn, applyMode: "replace" | "insert") => {
      const api = apiRef.current;
      if (!api || !turn.content.trim()) return;
      if (applyMode === "replace" && !window.confirm(t("这会替换左侧文档的当前正文，确定吗？"))) return;

      const html =
        turn.kind === "flow" ? mermaidHtml(stripFences(turn.content)) : markdownToHtml(turn.content);

      if (applyMode === "insert") {
        const ok = api.insertContent(html);
        if (!ok) {
          toast("info", t("当前文档无法插入：流程图只能整份替换，或编辑器尚未就绪。"));
          return;
        }
      } else {
        api.replaceContent(html);
      }
      patchTurn(turn.id, { applied: applyMode });
    },
    [apiRef, patchTurn, t, toast],
  );

  const applyRef = useRef(apply);
  applyRef.current = apply;

  const discard = useCallback(
    (turn: Turn) => patchTurn(turn.id, { applied: "discard" }),
    [patchTurn],
  );

  const clearChat = () => {
    if (busy) return;
    setTurns([]);
  };

  const kindLabel =
    doc?.kind === "flow"
      ? t("流程图")
      : doc?.kind === "overview"
        ? t("项目总览")
        : doc?.kind === "spec_source"
          ? t("上传的资料")
          : doc?.kind === "custom"
            ? t("自定义文档")
            : t("未选择文档");

  const examples = (mode === "global"
    ? [
        "这个项目有多少条用例？",
        "最近一次运行通过率多少？有哪些失败？",
        "根据需规，给结算页补 3 条用例",
      ]
    : [
        "总结这份文档的要点",
        "检查这份文档有没有遗漏",
        "把选中的文字改写得更清楚",
      ]
  ).map((s) => t(s));

  return (
    <aside
      className={
        "tp-ai-sidebar flex h-[calc(100vh-8.5rem)] min-h-[520px] flex-col overflow-hidden rounded-lg border bg-[var(--panel)] " +
        (drag ? "border-brand-300 ring-2 ring-brand-100" : "border-[var(--line)]")
      }
      onDragOver={(e) => {
        if (mode !== "global") return;
        // 必须 preventDefault，否则浏览器会用默认行为（直接打开文件）
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        if (mode !== "global") return;
        e.preventDefault();
        setDrag(false);
        void addFiles(Array.from(e.dataTransfer?.files ?? []));
      }}
    >
      <div className="flex items-center gap-2 border-b border-[var(--line)] px-3 py-2.5">
        <Sparkles className="h-4 w-4 shrink-0 text-brand-600" />
        <span className="shrink-0 text-[13px] font-medium text-ink-900">{t("AI 协同助手")}</span>
        <span className="ml-auto truncate text-[11px] text-ink-400" title={mode === "doc" ? doc?.title || "" : ""}>
          {mode === "doc" ? `${kindLabel}${doc?.title ? ` · ${doc.title}` : ""}` : t("全局项目")}
        </span>
        {turns.length > 0 && (
          <button
            type="button"
            onClick={clearChat}
            disabled={busy}
            title={t("清空对话")}
            aria-label={t("清空对话")}
            className="shrink-0 rounded-md p-1 text-ink-400 outline-none transition-colors hover:bg-[var(--panel2)] hover:text-ink-700 disabled:opacity-40"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5 border-b border-[var(--line)] px-3 py-1.5 text-[11px] text-ink-400">
        <select
          value={mode}
          onChange={(e) => changeMode(e.target.value === "global" ? "global" : "doc")}
          disabled={busy}
          aria-label={t("对话模式")}
          title={t("当前文档模式只改左侧文档；全局项目模式可查用例、运行并直接操作平台")}
          className="rounded-md border border-[var(--line)] bg-[var(--panel)] px-1.5 py-0.5 text-[11px] font-medium text-ink-700 outline-none transition-colors hover:border-[var(--line-strong)] focus:border-brand-300 disabled:opacity-50"
        >
          <option value="doc">{t("当前文档模式")}</option>
          <option value="global">{t("全局项目模式")}</option>
        </select>
        {mode === "doc" ? (
          selection.empty ? (
            <span>{t("未选中文字（AI 会读整份正文）")}</span>
          ) : (
            <span className="text-brand-700">
              {t("已选中 {{n}} 字，会作为重点上下文", { n: selection.text.length })}
            </span>
          )
        ) : (
          <span>{t("可查询用例、运行与问题，也可直接让我改用例")}</span>
        )}
        {mode === "doc" && doc?.kind === "flow" && (
          <Button
            size="sm"
            variant="outline"
            className="ml-auto h-6 px-2 text-[11px]"
            disabled={busy}
            onClick={startFlow}
            title={t("根据当前文档和需规流式重画流程图")}
          >
            <GitBranch className="h-3 w-3" />
            {busy ? t("生成中…") : t("AI 流式生成")}
          </Button>
        )}
      </div>

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-3 py-3">
        {turns.length === 0 && (
          <div className="rounded-lg border border-dashed border-[var(--line)] px-3 py-5 text-center text-[12px] leading-relaxed text-ink-400">
            <p>
              {mode === "doc"
                ? t("问我任何关于这份文档的问题；改文档不会直接生效，生成后由你点「替换」或「插入」。")
                : t("直接问项目全局问题：用例、运行、失败、问题……也可以让我直接改用例。")}
            </p>
            <div className="mt-3 flex flex-wrap justify-center gap-1.5">
              {examples.map((ex) => (
                <button
                  key={ex}
                  type="button"
                  onClick={() => {
                    if (busy) return;
                    void begin(mode === "global" ? "global" : "chat", ex);
                  }}
                  disabled={busy}
                  className="rounded-full border border-[var(--line)] px-2.5 py-1 text-[11.5px] text-ink-700 outline-none transition-[background-color,border-color,transform] hover:border-brand-200 hover:bg-brand-50 focus-visible:ring-2 focus-visible:ring-brand-100 active:translate-y-px disabled:opacity-50"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}
        {turns.map((turn) => (
          <div key={turn.id} className={turn.role === "user" ? "flex justify-end" : "flex justify-start"}>
            <div className={"max-w-[96%] " + (turn.role === "user" ? "text-right" : "")}>
              {turn.role === "assistant" && turn.kind !== "flow" ? (
                <div className="break-words rounded-lg rounded-bl-sm bg-[var(--panel2)] px-3 py-2 text-[12.5px] leading-relaxed text-ink-800">
                  {turn.content ? (
                    <MarkdownBody content={turn.content} />
                  ) : turn.status === "streaming" ? (
                    t("正在生成…")
                  ) : null}
                  {turn.status === "streaming" && turn.content && (
                    <span className="ml-0.5 inline-block h-3.5 w-1 animate-pulse bg-brand-500 align-middle" />
                  )}
                </div>
              ) : (
                <div
                  className={
                    turn.role === "user"
                      ? "inline-block whitespace-pre-wrap break-words rounded-lg rounded-br-sm bg-brand-600 px-3 py-2 text-left text-[12.5px] leading-relaxed text-white"
                      : "whitespace-pre-wrap break-words rounded-lg rounded-bl-sm bg-[var(--panel2)] px-3 py-2 font-mono text-[11.5px] leading-relaxed text-ink-800"
                  }
                >
                  {turn.content || (turn.status === "streaming" ? t("正在生成…") : "")}
                  {turn.status === "streaming" && turn.content && (
                    <span className="ml-0.5 inline-block h-3.5 w-1 animate-pulse bg-brand-500 align-middle" />
                  )}
                </div>
              )}
              {turn.status === "error" && (
                <p className="mt-1 text-[11px] text-[var(--bad-fg)]">{turn.error || t("生成失败")}</p>
              )}
              {turn.note && turn.status !== "error" && (
                <p className="mt-1 text-[11px] text-ink-400">{turn.note}</p>
              )}
              {turn.role === "assistant" && !!turn.actions?.length && (
                <div className="mt-1.5 space-y-1 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-2.5 py-1.5 text-left">
                  <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-ink-400">
                    <Wrench className="h-3 w-3" />
                    {t("操作记录")}
                  </div>
                  {turn.actions.map((a, i) => (
                    <div key={i} className="break-all font-mono text-[11px] text-ink-700">
                      <span className={a.ok ? "text-[var(--ok-fg)]" : "text-[var(--bad-fg)]"}>
                        {a.ok ? "✓" : "✗"}
                      </span>{" "}
                      {describeAction(a)}
                    </div>
                  ))}
                </div>
              )}
              {turn.role === "assistant" &&
                turn.status === "done" &&
                turn.content.trim() &&
                turn.origin !== "global" &&
                !turn.actions?.length && (
                  <SuggestionCard
                    content={stripFences(turn.content)}
                    kind={turn.kind === "flow" ? "flow" : "text"}
                    applied={turn.applied}
                    onApply={(applyMode) => applyRef.current(turn, applyMode)}
                    onDiscard={() => discard(turn)}
                  />
                )}
            </div>
          </div>
        ))}
      </div>

      <div className="border-t border-[var(--line)] p-2.5">
        {mode === "global" && atts.length > 0 && (
          <div className="mb-1.5 flex flex-wrap gap-1.5">
            {atts.map((a, i) => (
              <span
                key={i}
                className="flex items-center gap-1.5 rounded-lg border border-[var(--line)] bg-[var(--panel2)] px-2 py-1 text-[11px] text-ink-700"
                title={a.truncated ? t("内容过长，只带了开头部分") : undefined}
              >
                <Paperclip className="h-3 w-3 shrink-0 text-ink-400" />
                <span className="max-w-[160px] truncate">{a.filename}</span>
                <span className="tabular-nums text-ink-400">
                  {a.chars.toLocaleString()}
                  {a.truncated ? "+" : ""}
                </span>
                <button
                  type="button"
                  onClick={() => setAtts((s) => s.filter((_, j) => j !== i))}
                  className="rounded p-0.5 text-ink-400 hover:text-[var(--bad-fg)]"
                  aria-label={t("移除附件")}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}
        {mode === "global" && (
          <div className="mb-1.5 flex items-center gap-1.5">
            <input
              ref={attRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                void addFiles(Array.from(e.target.files ?? []));
                e.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => attRef.current?.click()}
              disabled={busy || attBusy}
              title={t("附加文件（也可以直接粘贴或拖进来）")}
              aria-label={t("附加文件")}
              className="grid h-6 w-6 place-items-center rounded-md border border-[var(--line)] text-ink-500 outline-none transition-colors hover:border-[var(--line-strong)] hover:bg-[var(--panel2)] disabled:opacity-40"
            >
              {attBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Paperclip className="h-3 w-3" />}
            </button>
            <span className="text-[11px] text-ink-400">{t("附加文件：点回形针、粘贴或拖入")}</span>
          </div>
        )}
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onPaste={(e) => {
            if (mode !== "global") return;
            const files = Array.from(e.clipboardData?.files ?? []);
            if (files.length) {
              e.preventDefault();
              void addFiles(files);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={3}
          placeholder={
            mode === "global"
              ? t("问项目全局问题…（Enter 发送，Shift+Enter 换行；可粘贴 / 拖入文件）")
              : t("描述你想怎么改，或直接提问…（Enter 发送，Shift+Enter 换行）")
          }
          className="w-full resize-none rounded-lg border border-[var(--line)] bg-[var(--panel)] px-2.5 py-2 text-[12.5px] leading-relaxed text-ink-800 outline-none focus:border-brand-300"
        />
        <div className="mt-1.5 flex items-center gap-2">
          <span className="text-[11px] text-ink-400">
            {mode === "global"
              ? t("全局模式会直接调用平台工具，修改会立即生效")
              : t("AI 生成的内容需要你确认后才会写入文档")}
          </span>
          <div className="ml-auto flex items-center gap-1.5">
            {busy ? (
              <Button size="sm" variant="outline" onClick={stop}>
                <CircleStop className="h-3.5 w-3.5" />
                {t("停止")}
              </Button>
            ) : (
              <Button
                size="sm"
                onClick={send}
                disabled={mode === "global" ? !input.trim() && !atts.length : !input.trim()}
              >
                <Send className="h-3.5 w-3.5" />
                {t("发送")}
              </Button>
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}
