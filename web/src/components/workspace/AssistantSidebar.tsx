import { useChat } from "@ai-sdk/react";
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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import { api, type AssistantAttachment } from "../../lib/api";
import {
  createAssistantTransport,
  dataPart,
  messageText,
  type Applied,
  type AssistantMessage,
  type Mode,
  type SendBody,
  type SendKind,
  type ToolAction,
  type TurnKind,
} from "../../lib/assistantTransport";
import { markdownToHtml } from "../../lib/markdown";
import { mermaidHtml } from "../../lib/mermaidSource";
import { useWorkspaceStore } from "../../state/workspaceStore";
import { Button } from "../ui";
import { useToast } from "../toast";
import { MarkdownBody } from "./MarkdownBody";
import { SuggestionCard } from "./SuggestionCard";

const HISTORY_LIMIT = 60;

/** 合并前存下的旧对话结构，只用于一次性迁移。 */
type LegacyTurn = {
  id: string;
  role: "user" | "assistant";
  content: string;
  kind?: TurnKind;
  status?: "streaming" | "done" | "error";
  error?: string;
  note?: string;
  applied?: Applied;
  origin?: Mode;
  actions?: ToolAction[];
};

/** 去掉模型偶尔带上的围栏标记。 */
function stripFences(raw: string): string {
  let s = (raw || "").trim();
  const fence = /^```[a-zA-Z]*\s*\n?([\s\S]*?)\n?```$/;
  const m = fence.exec(s);
  if (m) s = m[1].trim();
  return s.replace(/^~~~[a-zA-Z]*\s*\n?/, "").replace(/\n?~~~$/, "").trim();
}

function legacyToMessage(turn: LegacyTurn): AssistantMessage {
  const parts: AssistantMessage["parts"] = [
    { type: "data-origin", data: { mode: turn.origin ?? "doc", kind: turn.kind ?? "text" } },
  ];
  if (turn.content) parts.push({ type: "text", text: turn.content, state: "done" });
  if (turn.note) parts.push({ type: "data-note", data: { text: turn.note } });
  if (turn.actions?.length) parts.push({ type: "data-actions", data: { actions: turn.actions } });
  if (turn.status === "error") {
    parts.push({ type: "data-error", data: { message: turn.error || "生成失败" } });
  }
  return {
    id: `legacy-${turn.id}`,
    role: turn.role,
    metadata: turn.applied ? { applied: turn.applied } : undefined,
    parts,
  };
}

function normalizeMessage(message: AssistantMessage): AssistantMessage {
  return {
    ...message,
    parts: message.parts.map((part) =>
      part.type === "text" && part.state === "streaming" ? { ...part, state: "done" as const } : part,
    ),
  };
}

function loadMessages(pid: number): AssistantMessage[] {
  if (!Number.isFinite(pid)) return [];
  try {
    const raw =
      localStorage.getItem(`tp.workspace.chat.${pid}`) ??
      localStorage.getItem(`tp.assistant.chat.${pid}`);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const out: AssistantMessage[] = [];
    for (const item of parsed) {
      if (!item || typeof item !== "object") continue;
      const obj = item as Record<string, unknown>;
      if (
        (obj.role === "user" || obj.role === "assistant") &&
        typeof obj.id === "string" &&
        Array.isArray(obj.parts)
      ) {
        out.push(normalizeMessage(item as AssistantMessage));
      } else if ((obj.role === "user" || obj.role === "assistant") && typeof obj.content === "string") {
        out.push(legacyToMessage(item as unknown as LegacyTurn));
      }
    }
    return out.slice(-HISTORY_LIMIT);
  } catch {
    return [];
  }
}

function saveMessages(pid: number, messages: AssistantMessage[]): void {
  if (!Number.isFinite(pid)) return;
  try {
    localStorage.setItem(
      `tp.workspace.chat.${pid}`,
      JSON.stringify(messages.slice(-HISTORY_LIMIT).map(normalizeMessage)),
    );
  } catch {
    /* 存储满了不影响对话 */
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
 * 切项目时用 key 重建面板，避免 useChat 状态和持久化串到另一个项目。
 */
export function AssistantSidebar() {
  const { pid: pidRaw } = useParams();
  const pid = Number(pidRaw);
  return <AssistantPanel key={pid} pid={pid} />;
}

function AssistantPanel({ pid }: { pid: number }) {
  const { t } = useTranslation();
  const toast = useToast();
  // 按字段订阅：正文逐键变化不会触发侧栏重渲染。
  const doc = useWorkspaceStore((s) => s.currentDoc);
  const selection = useWorkspaceStore((s) => s.selection);
  const setStreaming = useWorkspaceStore((s) => s.setStreaming);
  const setFlowTrigger = useWorkspaceStore((s) => s.setFlowTrigger);

  const [input, setInput] = useState("");
  const [mode, setModeState] = useState<Mode>(() => loadMode(pid));

  // 全局模式的附件（粘贴 / 拖入 / 点选）。文件先由后端解析成文本再随这一轮发出。
  const [atts, setAtts] = useState<AssistantAttachment[]>([]);
  const [attBusy, setAttBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const attRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const transport = useMemo(() => createAssistantTransport(pid), [pid]);
  const initialMessages = useMemo(() => loadMessages(pid), [pid]);
  const chat = useChat<AssistantMessage>({
    id: `assistant-${pid}`,
    messages: initialMessages,
    transport,
    // SDK 自带的节流，代替原先手写的 50ms 合批。
    throttle: 50,
    onError: (err) => toast("error", err.message || t("生成失败，请稍后重试。")),
  });
  const { messages, status, sendMessage, setMessages, stop } = chat;
  const busy = status === "submitted" || status === "streaming";

  useEffect(() => {
    if (status === "submitted" || status === "streaming") return;
    saveMessages(pid, messages);
  }, [messages, pid, status]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, busy]);

  useEffect(() => {
    setStreaming(mode === "doc" && busy);
    return () => setStreaming(false);
  }, [busy, mode, setStreaming]);

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

  const dispatch = useCallback(
    (kind: SendKind, message: string, attachments?: AssistantAttachment[]) => {
      if (busy) return;
      const sent = attachments ?? [];
      // 对话里显示附件名，回看历史时知道这一轮基于什么；正文只随请求发送。
      const label = sent.length
        ? `${message || t("（见附件）")}\n📎 ${sent.map((a) => a.filename).join("、")}`
        : message;
      const body: SendBody = {
        kind,
        message: message || (sent.length ? t("请看附件内容。") : ""),
        attachments: sent.length ? sent : undefined,
      };
      void sendMessage({ text: label }, { body });
    },
    [busy, sendMessage, t],
  );

  const dispatchRef = useRef(dispatch);
  dispatchRef.current = dispatch;

  // 工具栏上的「AI 流式生成」复用它。挂 ref 而不是把 dispatch 透传下去，
  // 是为了让侧栏内部状态变化不引起整页重渲染。
  useEffect(() => {
    setFlowTrigger((instruction?: string) => {
      const text = (instruction ?? "").trim() || t("请根据左侧文档和项目需规，画一张主要业务流程图。");
      dispatchRef.current("flow", text);
    });
    return () => {
      setFlowTrigger(null);
    };
  }, [setFlowTrigger, t]);

  const startFlow = () => {
    if (busy) return;
    const text = input.trim() || t("请根据左侧文档和项目需规，画一张主要业务流程图。");
    setInput("");
    dispatch("flow", text);
  };

  const send = () => {
    const text = input.trim();
    if (busy) return;
    if (mode === "global" ? !text && atts.length === 0 : !text) return;
    const sent = mode === "global" ? atts : [];
    setInput("");
    if (mode === "global") setAtts([]);
    dispatch(mode === "global" ? "global" : "chat", text, sent);
  };

  const apply = useCallback(
    (message: AssistantMessage, applyMode: "replace" | "insert") => {
      const origin = dataPart(message, "origin");
      const mermaid = dataPart(message, "mermaid")?.source;
      const content = stripFences(mermaid ?? messageText(message));
      if (!content.trim()) return;
      if (applyMode === "replace" && !window.confirm(t("这会替换左侧文档的当前正文，确定吗？"))) {
        return;
      }

      const html = origin?.kind === "flow" ? mermaidHtml(content) : markdownToHtml(content);
      const store = useWorkspaceStore.getState();

      if (applyMode === "insert") {
        const ok = store.insertContent(html);
        if (!ok) {
          toast("info", t("当前文档无法插入：流程图只能整份替换，或编辑器尚未就绪。"));
          return;
        }
      } else {
        store.replaceContent(html);
      }
      setMessages((prev) =>
        prev.map((m) =>
          m.id === message.id ? { ...m, metadata: { ...m.metadata, applied: applyMode } } : m,
        ),
      );
    },
    [setMessages, t, toast],
  );

  const discard = useCallback(
    (message: AssistantMessage) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === message.id ? { ...m, metadata: { ...m.metadata, applied: "discard" } } : m,
        ),
      );
    },
    [setMessages],
  );

  const clearChat = () => {
    if (busy) return;
    setMessages([]);
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

  const awaitingReply = busy && (messages.length === 0 || messages[messages.length - 1].role === "user");

  return (
    <aside
      className={
        "tp-ai-sidebar flex h-full min-h-0 flex-col overflow-hidden rounded-lg border bg-[var(--panel)] " +
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
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--line)] px-3 py-2.5">
        <Sparkles className="h-4 w-4 shrink-0 text-brand-600" />
        <span className="shrink-0 text-[13px] font-medium text-ink-900">{t("AI 协同助手")}</span>
        <span
          className="ml-auto truncate text-[11px] text-ink-400"
          title={mode === "doc" ? doc?.title || "" : ""}
        >
          {mode === "doc" ? `${kindLabel}${doc?.title ? ` · ${doc.title}` : ""}` : t("全局项目")}
        </span>
        {messages.length > 0 && (
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

      <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-[var(--line)] px-3 py-1.5 text-[11px] text-ink-400">
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

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3">
        {messages.length === 0 && !busy && (
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
                    dispatch(mode === "global" ? "global" : "chat", ex);
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

        {messages.map((message, index) => {
          const isUser = message.role === "user";
          const origin = dataPart(message, "origin");
          const kind: TurnKind = origin?.kind === "flow" ? "flow" : "text";
          const content = messageText(message);
          const mermaid = dataPart(message, "mermaid")?.source;
          // 流式期间显示 token，结束后用后端校验通过的源码。
          const shown = !isUser && kind === "flow" && mermaid ? mermaid : content;
          const error = dataPart(message, "error")?.message;
          const note = dataPart(message, "note")?.text;
          const actions = dataPart(message, "actions")?.actions ?? [];
          const applied = message.metadata?.applied;
          const streaming = busy && index === messages.length - 1 && !isUser;
          const canApply =
            !isUser &&
            !streaming &&
            !error &&
            origin?.mode !== "global" &&
            !actions.length &&
            !!(mermaid?.trim() || content.trim());

          return (
            <div key={message.id} className={isUser ? "flex justify-end" : "flex justify-start"}>
              <div className={"max-w-[96%] " + (isUser ? "text-right" : "")}>
                {!isUser && kind !== "flow" ? (
                  <div className="break-words rounded-lg rounded-bl-sm bg-[var(--panel2)] px-3 py-2 text-[12.5px] leading-relaxed text-ink-800">
                    {content ? (
                      <MarkdownBody content={content} />
                    ) : streaming ? (
                      t("正在生成…")
                    ) : null}
                    {streaming && content && (
                      <span className="ml-0.5 inline-block h-3.5 w-1 animate-pulse bg-brand-500 align-middle" />
                    )}
                  </div>
                ) : (
                  <div
                    className={
                      isUser
                        ? "inline-block whitespace-pre-wrap break-words rounded-lg rounded-br-sm bg-brand-600 px-3 py-2 text-left text-[12.5px] leading-relaxed text-white"
                        : "whitespace-pre-wrap break-words rounded-lg rounded-bl-sm bg-[var(--panel2)] px-3 py-2 font-mono text-[11.5px] leading-relaxed text-ink-800"
                    }
                  >
                    {shown || (streaming ? t("正在生成…") : "")}
                    {streaming && content && (
                      <span className="ml-0.5 inline-block h-3.5 w-1 animate-pulse bg-brand-500 align-middle" />
                    )}
                  </div>
                )}
                {error && <p className="mt-1 text-[11px] text-[var(--bad-fg)]">{error}</p>}
                {note && !error && <p className="mt-1 text-[11px] text-ink-400">{note}</p>}
                {!isUser && actions.length > 0 && (
                  <div className="mt-1.5 space-y-1 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-2.5 py-1.5 text-left">
                    <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-ink-400">
                      <Wrench className="h-3 w-3" />
                      {t("操作记录")}
                    </div>
                    {actions.map((a, i) => (
                      <div key={i} className="break-all font-mono text-[11px] text-ink-700">
                        <span className={a.ok ? "text-[var(--ok-fg)]" : "text-[var(--bad-fg)]"}>
                          {a.ok ? "✓" : "✗"}
                        </span>{" "}
                        {describeAction(a)}
                      </div>
                    ))}
                  </div>
                )}
                {canApply && (
                  <SuggestionCard
                    content={stripFences(mermaid ?? content)}
                    kind={kind}
                    applied={applied}
                    onApply={(applyMode) => apply(message, applyMode)}
                    onDiscard={() => discard(message)}
                  />
                )}
              </div>
            </div>
          );
        })}

        {awaitingReply && (
          <div className="flex justify-start">
            <div className="rounded-lg rounded-bl-sm bg-[var(--panel2)] px-3 py-2 text-[12.5px] leading-relaxed text-ink-800">
              {t("正在生成…")}
            </div>
          </div>
        )}
      </div>

      <div className="shrink-0 border-t border-[var(--line)] p-2.5">
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
              <Button size="sm" variant="outline" onClick={() => void stop()}>
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
