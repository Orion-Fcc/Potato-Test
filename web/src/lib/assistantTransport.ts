import type { ChatTransport, UIMessage, UIMessageChunk } from "ai";

import { EMPTY_SELECTION, useWorkspaceStore } from "../state/workspaceStore";
import { api, type AssistantAttachment, type AssistantTurn } from "./api";
import { latestMermaidSource } from "./mermaidSource";
import { readSSE } from "./sse";

export type Mode = "doc" | "global";
export type TurnKind = "text" | "flow";
export type Applied = "replace" | "insert" | "discard";
export type ToolAction = { tool: string; args: Record<string, unknown>; ok: boolean };
export type SendKind = "chat" | "flow" | "global";

export type SendBody = {
  kind: SendKind;
  message: string;
  attachments?: AssistantAttachment[];
};

export type ChatData = {
  origin: { mode: Mode; kind: TurnKind };
  note: { text: string };
  actions: { actions: ToolAction[] };
  mermaid: { source: string };
  error: { message: string };
};

export type AssistantMessage = UIMessage<{ applied?: Applied }, ChatData>;

const MAX_TURNS_TO_MODEL = 12;
let seq = 0;

function nextId(prefix: string): string {
  seq += 1;
  return `${prefix}-${Date.now().toString(36)}-${seq}`;
}

export function messageText(message: AssistantMessage): string {
  return message.parts
    .filter((part) => part.type === "text")
    .map((part) => (part as { type: "text"; text: string }).text)
    .join("");
}

export function dataPart<K extends keyof ChatData>(
  message: AssistantMessage,
  name: K,
): ChatData[K] | undefined {
  const target: string = `data-${name}`;
  for (const part of message.parts) {
    if (part.type === target) {
      return (part as unknown as { data: ChatData[K] }).data;
    }
  }
  return undefined;
}

function parseJson(raw: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  } catch {
    return raw ? { text: raw } : {};
  }
}

function errorMessage(data: Record<string, unknown>, fallback: string): string {
  const value = data.message ?? data.error ?? data.detail;
  return typeof value === "string" && value ? value : fallback;
}

function readBody(body: object | undefined): SendBody {
  const raw = (body ?? {}) as Partial<SendBody>;
  const kind: SendKind = raw.kind === "flow" || raw.kind === "global" ? raw.kind : "chat";
  return {
    kind,
    message: typeof raw.message === "string" ? raw.message : "",
    attachments: Array.isArray(raw.attachments) ? raw.attachments : undefined,
  };
}

function buildHistory(messages: AssistantMessage[]): AssistantTurn[] {
  const out: AssistantTurn[] = [];
  for (const message of messages.slice(0, -1)) {
    if (message.role !== "user" && message.role !== "assistant") continue;
    if (dataPart(message, "error")) continue;
    const content = messageText(message).trim();
    if (!content) continue;
    out.push({ role: message.role, content: content.slice(0, 4000) });
  }
  return out.slice(-MAX_TURNS_TO_MODEL);
}

type FinishExtra = {
  note?: string;
  mermaid?: string;
  actions?: ToolAction[];
  error?: string;
};

export function createAssistantTransport(pid: number): ChatTransport<AssistantMessage> {
  return {
    async sendMessages({ messages, abortSignal, body }) {
      const request = readBody(body);
      const mode: Mode = request.kind === "global" ? "global" : "doc";
      const kind: TurnKind = request.kind === "flow" ? "flow" : "text";
      const history = buildHistory(messages);
      const ctx = mode === "global" ? null : useWorkspaceStore.getState().getContext();
      const selection = ctx?.selection ?? EMPTY_SELECTION;
      const doc = ctx?.doc ?? null;

      const path =
        request.kind === "flow"
          ? `/projects/${pid}/docs/flow/stream`
          : `/projects/${pid}/assistant/stream`;
      const payload =
        request.kind === "flow"
          ? {
              instruction: request.message,
              current: latestMermaidSource(ctx?.html ?? ""),
              selection,
            }
          : {
              message: request.message,
              history,
              doc,
              selection,
              content: (ctx?.html ?? "").slice(-12000),
            };

      return new ReadableStream<UIMessageChunk>({
        async start(controller) {
          const textId = nextId("text");
          let open = true;
          let text = "";

          const emit = (chunk: UIMessageChunk) => {
            if (!open) return;
            try {
              controller.enqueue(chunk);
            } catch {
              open = false;
            }
          };
          const close = () => {
            if (!open) return;
            open = false;
            try {
              controller.close();
            } catch {
              /* 已被 useChat 取消 */
            }
          };
          const finish = (extra: FinishExtra = {}) => {
            emit({ type: "text-end", id: textId });
            if (extra.mermaid) emit({ type: "data-mermaid", data: { source: extra.mermaid } });
            if (extra.note) emit({ type: "data-note", data: { text: extra.note } });
            if (extra.actions) emit({ type: "data-actions", data: { actions: extra.actions } });
            if (extra.error) emit({ type: "data-error", data: { message: extra.error } });
            emit({ type: "finish" });
          };

          emit({ type: "start" });
          emit({ type: "data-origin", data: { mode, kind } });
          emit({ type: "text-start", id: textId });

          try {
            if (request.kind === "global") {
              const res = await api.assistant(
                pid,
                { message: request.message, history, attachments: request.attachments },
                { signal: abortSignal },
              );
              text = res.reply ?? "";
              if (text) emit({ type: "text-delta", id: textId, delta: text });
              finish(text.trim() ? { actions: res.actions } : { error: "没有收到回复。" });
            } else {
              let done = false;
              let mermaid = "";
              let note = "";

              await readSSE(path, payload, {
                signal: abortSignal,
                onEvent(ev) {
                  if (done) return;
                  const data = parseJson(ev.data);
                  if (ev.event === "token") {
                    const delta = typeof data.text === "string" ? data.text : "";
                    if (!delta) return;
                    text += delta;
                    emit({ type: "text-delta", id: textId, delta });
                    return;
                  }
                  if (ev.event === "done") {
                    done = true;
                    if (typeof data.mermaid === "string") mermaid = data.mermaid;
                    if (typeof data.note === "string" && data.note.trim()) note = data.note.trim();
                    return;
                  }
                  if (ev.event === "error") {
                    throw new Error(errorMessage(data, "生成失败，请稍后重试。"));
                  }
                },
              });

              if (!done && !text.trim() && !mermaid.trim()) {
                finish({ error: "没有收到回复。" });
              } else {
                finish({ mermaid: mermaid || undefined, note: note || undefined });
              }
            }
          } catch (err) {
            if (abortSignal?.aborted) {
              if (!text.trim()) emit({ type: "data-note", data: { text: "已停止生成。" } });
              emit({ type: "abort" });
            } else {
              const message = err instanceof Error ? err.message : String(err);
              finish({ error: message || "生成失败，请稍后重试。" });
            }
          } finally {
            close();
          }
        },
      });
    },
    async reconnectToStream() {
      return null;
    },
  };
}
