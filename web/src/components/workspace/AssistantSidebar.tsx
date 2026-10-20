import { CircleStop, GitBranch, Send, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
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

type Turn = {
  id: string;
  role: TurnRole;
  content: string;
  kind?: TurnKind;
  status?: TurnStatus;
  error?: string;
  note?: string;
  applied?: Applied;
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

/**
 * 常驻 AI 侧栏（2026-10-20）。
 *
 * 三件事在这里汇合：
 *  1. **上下文**：每次发送都从 `WorkspaceContext.apiRef` 现取左侧文档正文与选区，
 *     而不是逐键同步 —— 长文档逐键 setState 会拖慢编辑器。
 *  2. **流式**：POST + SSE，token 只更新这条消息的气泡；**不碰左侧编辑器**。
 *  3. **人确认**：生成完成才出现建议卡片，用户点「替换 / 插入」才写入正文。
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

  const patchTurn = useCallback((id: string, patch: Partial<Turn>) => {
    setTurns((prev) => prev.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  }, []);

  const begin = useCallback(
    async (mode: "chat" | "flow", message: string) => {
      if (busyRef.current || !Number.isFinite(pid)) return;
      const ctx = apiRef.current?.getContext();
      const docMeta = ctx?.doc ?? docRef.current;
      const sel: SelectionInfo = ctx?.selection ?? EMPTY_SELECTION;

      const userTurn: Turn = { id: nextId(), role: "user", content: message, status: "done" };
      const replyId = nextId();
      const replyTurn: Turn = {
        id: replyId,
        role: "assistant",
        content: "",
        kind: mode === "flow" ? "flow" : "text",
        status: "streaming",
      };
      setTurns((prev) => [...prev, userTurn, replyTurn]);

      busyRef.current = true;
      setBusy(true);
      setStreaming(true);
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
          if (mode === "flow" && typeof data.mermaid === "string") acc = data.mermaid;
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
        if (mode === "flow") {
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
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [apiRef, patchTurn, pid, setStreaming, t],
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
    if (!text || busy) return;
    setInput("");
    void begin("chat", text);
  };

  const apply = useCallback(
    (turn: Turn, mode: "replace" | "insert") => {
      const api = apiRef.current;
      if (!api || !turn.content.trim()) return;
      if (mode === "replace" && !window.confirm(t("这会替换左侧文档的当前正文，确定吗？"))) return;

      const html =
        turn.kind === "flow" ? mermaidHtml(stripFences(turn.content)) : markdownToHtml(turn.content);

      if (mode === "insert") {
        const ok = api.insertContent(html);
        if (!ok) {
          toast("info", t("当前文档无法插入：流程图只能整份替换，或编辑器尚未就绪。"));
          return;
        }
      } else {
        api.replaceContent(html);
      }
      patchTurn(turn.id, { applied: mode });
    },
    [apiRef, patchTurn, t, toast],
  );

  const applyRef = useRef(apply);
  applyRef.current = apply;

  const discard = useCallback(
    (turn: Turn) => patchTurn(turn.id, { applied: "discard" }),
    [patchTurn],
  );

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

  return (
    <aside className="tp-ai-sidebar flex h-[calc(100vh-8.5rem)] min-h-[520px] flex-col overflow-hidden rounded-lg border border-[var(--line)] bg-[var(--panel)]">
      <div className="flex items-center gap-2 border-b border-[var(--line)] px-3 py-2.5">
        <Sparkles className="h-4 w-4 text-brand-600" />
        <span className="text-[13px] font-medium text-ink-900">{t("AI 协同助手")}</span>
        <span className="ml-auto truncate text-[11px] text-ink-400" title={doc?.title || ""}>
          {kindLabel}
          {doc?.title ? ` · ${doc.title}` : ""}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 border-b border-[var(--line)] px-3 py-1.5 text-[11px] text-ink-400">
        {selection.empty ? (
          <span>{t("未选中文字（AI 会读整份正文）")}</span>
        ) : (
          <span className="text-brand-700">
            {t("已选中 {{n}} 字，会作为重点上下文", { n: selection.text.length })}
          </span>
        )}
        {doc?.kind === "flow" && (
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
          <div className="rounded-lg border border-dashed border-[var(--line)] px-3 py-6 text-center text-[12px] leading-relaxed text-ink-400">
            {t("问我任何关于这份文档的问题；改文档不会直接生效，生成后由你点「替换」或「插入」。")}
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
              {turn.role === "assistant" && turn.status === "done" && turn.content.trim() && (
                <SuggestionCard
                  content={stripFences(turn.content)}
                  kind={turn.kind === "flow" ? "flow" : "text"}
                  applied={turn.applied}
                  onApply={(mode) => applyRef.current(turn, mode)}
                  onDiscard={() => discard(turn)}
                />
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="border-t border-[var(--line)] p-2.5">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={3}
          placeholder={t("描述你想怎么改，或直接提问…（Enter 发送，Shift+Enter 换行）")}
          className="w-full resize-none rounded-lg border border-[var(--line)] bg-[var(--panel)] px-2.5 py-2 text-[12.5px] leading-relaxed text-ink-800 outline-none focus:border-brand-300"
        />
        <div className="mt-1.5 flex items-center gap-2">
          <span className="text-[11px] text-ink-400">{t("AI 生成的内容需要你确认后才会写入文档")}</span>
          <div className="ml-auto flex items-center gap-1.5">
            {busy ? (
              <Button size="sm" variant="outline" onClick={stop}>
                <CircleStop className="h-3.5 w-3.5" />
                {t("停止")}
              </Button>
            ) : (
              <Button size="sm" onClick={send} disabled={!input.trim()}>
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
