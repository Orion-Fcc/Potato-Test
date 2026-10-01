import { Bot, BookOpen, Send, Trash2, Upload, User, Wrench } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import clsx from "clsx";
import { api } from "../lib/api";
import { isAbort, useAbortSignal } from "../lib/hooks";

interface MsgAction {
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
}
interface Msg {
  role: "user" | "assistant";
  content: string;
  actions?: MsgAction[];
}

const chatKey = (pid: number) => `tp.assistant.chat.${pid}`;

/** How much of the extracted document we echo into the textarea after an upload.
 *  The full text is already saved server-side; pulling a 20 MB corpus into a
 *  <textarea> just to show it is what made large uploads unusable before. */
const KB_PREVIEW_CHARS = 200_000;

/**
 * In-app assistant — an OPERATOR with a knowledge base:
 *  - POST /api/projects/:pid/assistant  → LLM + tools (generic `potato-test_api`, search_knowledge, create_case…)
 *  - GET/PUT/POST /api/projects/:pid/knowledge[/append] → the spec/requirement text it searches
 *    Stored as chunks server-side (app/knowledge.py), so a large merged spec stays intact
 *    and search cost does not grow with document size.
 * The chat is persisted per project in localStorage so reloads don't lose it.
 */
export function AssistantPage() {
  const { pid } = useParams();
  const projectId = Number(pid);
  const { t } = useTranslation();
  const signal = useAbortSignal();

  const [turns, setTurns] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const [kbOpen, setKbOpen] = useState(false);
  const [kb, setKb] = useState("");
  const [kbChars, setKbChars] = useState(0);
  const [kbSaving, setKbSaving] = useState(false);
  const [kbBusy, setKbBusy] = useState(false);
  const [kbNote, setKbNote] = useState<string | null>(null);
  /** True when the textarea holds only the first KB_PREVIEW_CHARS of the saved corpus.
   *  Saving from this state would silently discard the rest, so it is blocked until the
   *  operator explicitly acknowledges it. */
  const [kbPreviewOnly, setKbPreviewOnly] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // --- persist chat per project ---
  useEffect(() => {
    try {
      const raw = localStorage.getItem(chatKey(projectId));
      setTurns(raw ? (JSON.parse(raw) as Msg[]) : []);
    } catch {
      setTurns([]);
    }
  }, [projectId]);
  useEffect(() => {
    try {
      localStorage.setItem(chatKey(projectId), JSON.stringify(turns.slice(-200)));
    } catch {
      /* quota — ignore */
    }
  }, [turns, projectId]);

  // --- knowledge ---
  useEffect(() => {
    api
      .getKnowledge(projectId)
      .then((r) => {
        // The corpus can be tens of MB. Put a bounded preview in the textarea (which is
        // the only editable surface) and keep the true size in kbChars — the saved text
        // itself is never truncated.
        setKb(r.text.length > KB_PREVIEW_CHARS ? r.text.slice(0, KB_PREVIEW_CHARS) : r.text);
        setKbChars(r.chars);
        setKbPreviewOnly(r.text.length > KB_PREVIEW_CHARS);
        setKbOpen(r.chars === 0); // nudge to fill it in when empty
      })
      .catch(() => {});
  }, [projectId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  async function saveKb() {
    // Guard the one destructive path: when only a preview is loaded, "保存" would
    // replace a multi-MB corpus with its first 200k chars. Append (upload) is safe and
    // unaffected; a deliberate full rewrite is the only thing blocked here.
    if (kbPreviewOnly) {
      const ok = window.confirm(
        t(
          "当前编辑框只载入了前 20 万字符的预览。若直接保存，超出的部分会被删除。确定要保存吗？",
        ),
      );
      if (!ok) return;
    }
    setKbSaving(true);
    try {
      const r = await api.setKnowledge(projectId, kb);
      setKbChars(r.chars);
      setKbPreviewOnly(false);
    } catch (e) {
      setErr(String(e));
    } finally {
      setKbSaving(false);
    }
  }

  /** Import one or many spec documents. Each file is parsed server-side (docx /
   *  pdf / xlsx / html / rtf / plain text), appended under a `## <filename>` heading
   *  so the origin of every block stays visible to the assistant, then saved. */
  async function onPickFiles(list: FileList | null) {
    const files = Array.from(list ?? []);
    if (files.length === 0) return;
    setKbBusy(true);
    setErr(null);
    setKbNote(null);
    const imported: string[] = [];
    const problems: string[] = [];
    // Each extracted document is appended server-side. Doing it file-by-file (rather
    // than building one giant local string and PUTting it) is what lets a 100 MB spec
    // through: the browser never has to hold the whole merged document, and the API
    // never receives it back as one request body.
    for (const f of files) {
      try {
        const r = await api.extractKnowledge(projectId, f);
        const block = `## ${r.filename}\n${r.text}\n`;
        const saved = await api.appendKnowledge(projectId, block);
        const added = r.chars.toLocaleString();
        imported.push(
          `${r.filename} · ${added} ${t("chars")}${r.truncated ? ` · ${t("truncated")}` : ""}`,
        );
        if (r.warnings.length > 0) problems.push(`${r.filename}: ${r.warnings.join("; ")}`);
        setKbChars(saved.chars);
        // Show the head of the new document so the operator can eyeball what landed,
        // without pulling the entire multi-MB corpus into the textarea.
        setKb((prev) => {
          const head = r.text.slice(0, KB_PREVIEW_CHARS);
          const more = r.text.length > KB_PREVIEW_CHARS ? "\n\n…（其余内容已保存，展开预览可查看前 20 万字符）" : "";
          const base = prev.trimEnd();
          return `${base}${base ? "\n\n" : ""}${block.slice(0, head.length)}${more}`;
        });
      } catch (e) {
        problems.push(`${f.name}: ${e instanceof Error ? e.message : String(e)}`);
      }
    }
    setKbOpen(true);
    if (imported.length > 0) setKbNote(`${t("Imported")}: ${imported.join("、")}`);
    if (problems.length > 0) setErr(problems.join("；"));
    setKbBusy(false);
    if (fileRef.current) fileRef.current.value = ""; // allow re-picking the same file
  }

  async function send(text?: string) {
    const msg = (text ?? input).trim();
    if (!msg || busy) return;
    const history = turns.map((m) => ({ role: m.role, content: m.content }));
    const next: Msg[] = [...turns, { role: "user", content: msg }];
    setTurns(next);
    setInput("");
    setBusy(true);
    setErr(null);
    try {
      const res = await api.assistant(projectId, { message: msg, history }, { signal });
      setTurns([...next, { role: "assistant", content: res.reply, actions: res.actions }]);
    } catch (e) {
      // A navigation away mid-answer is not an error worth showing.
      if (!isAbort(e)) setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const describe = (a: MsgAction) => {
    const arg = a.args as { method?: string; path?: string; query?: string };
    if (arg.method && arg.path) return `${arg.method} ${arg.path}`;
    if (a.tool === "search_knowledge") return `${a.tool}("${arg.query ?? ""}")`;
    return a.tool;
  };

  const examples = [
    t("这个项目有多少条用例？"),
    t("最近一次运行通过率多少？有哪些失败？"),
    t("根据需规，给结算页补 3 条用例"),
  ];

  return (
    // Full-height chat: <main> scrolls, so a viewport-locked column here needs its
    // own height. dvh (not vh) keeps the composer visible when mobile browser
    // chrome collapses; the 10.5rem covers the top bar + the page's py-7/py-9.
    <div className="mx-auto flex h-[calc(100dvh-10.5rem)] min-h-[28rem] max-w-5xl flex-col">
      <div className="mb-3 flex items-center gap-3">
        <div className="relative grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 text-[var(--on-brand)] shadow-[0_3px_10px_-4px_color-mix(in_oklch,var(--brand-700)_70%,transparent)]">
          <Bot className="h-5 w-5" />
          <span className="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-[var(--ok)] ring-2 ring-[var(--panel)]" />
        </div>
        <div className="min-w-0">
          <div className="tp-eyebrow mb-0.5">{t("Project")}</div>
          <h1 className="text-[17px] font-semibold tracking-tight text-ink-900">{t("Assistant")}</h1>
          <p className="mt-0.5 text-[12.5px] text-ink-500">
            {t("Reads your spec, operates Potato Test (any API), and can create cases.")}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setKbOpen((v) => !v)}
            aria-expanded={kbOpen}
            className={clsx(
              "flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] font-medium outline-none transition-[background-color,border-color,color]",
              kbOpen
                ? "border-brand-200 bg-brand-50 text-brand-700"
                : "border-[var(--line)] text-ink-700 hover:border-brand-200 hover:bg-brand-50",
            )}
          >
            <BookOpen className="h-3.5 w-3.5" />
            {t("Spec / Docs")}
            {kbChars > 0 && <span className="tabular-nums opacity-70">· {kbChars.toLocaleString()}</span>}
          </button>
          <button
            onClick={() => setTurns([])}
            title={t("Clear chat")}
            className="flex items-center gap-1.5 rounded-lg border border-[var(--line)] px-2.5 py-1.5 text-[12px] font-medium text-ink-700 outline-none transition-[background-color,border-color] hover:border-[var(--line-strong)] hover:bg-[var(--panel2)]"
          >
            <Trash2 className="h-3.5 w-3.5" />
            {t("Clear chat")}
          </button>
        </div>
      </div>

      {kbOpen && (
        <div className="tp-card mb-3 max-h-[42dvh] shrink-0 overflow-auto border border-[var(--line)] p-4">
          <div className="mb-2.5 flex items-center gap-2 text-[12px] text-ink-500">
            <BookOpen className="h-3.5 w-3.5 text-ink-400" />
            {t("Paste your spec/requirement here — the assistant searches it before answering.")}
            <span className="ml-auto rounded-full bg-[var(--panel2)] px-2 py-0.5 tabular-nums text-[11px] text-ink-400">
              {kbChars.toLocaleString()} {t("chars")}
            </span>
          </div>
          <textarea
            value={kb}
            onChange={(e) => setKb(e.target.value)}
            rows={8}
            placeholder={t("e.g. paste the requirement spec, page rules, field definitions…")}
            className="w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-2.5 font-mono text-[12px] leading-relaxed text-ink-900 outline-none transition-[border-color,box-shadow] focus:border-brand-500 focus:ring-2 focus:ring-brand-100"
          />
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <input
              ref={fileRef}
              type="file"
              multiple
              accept=".md,.markdown,.txt,.json,.csv,.tsv,.log,.yaml,.yml,.xml,.docx,.pdf,.xlsx,.xlsm,.html,.htm,.rtf"
              className="hidden"
              onChange={(e) => onPickFiles(e.target.files)}
            />
            <button
              onClick={() => fileRef.current?.click()}
              disabled={kbBusy}
              title={t("Supports md, txt, json, csv, docx, pdf, xlsx, html, rtf — multiple files at once")}
              className="flex items-center gap-1.5 rounded-lg border border-[var(--line)] px-2.5 py-1.5 text-[12px] font-medium text-ink-700 outline-none transition-[background-color,border-color] hover:border-[var(--line-strong)] hover:bg-[var(--panel2)] disabled:opacity-40"
            >
              <Upload className="h-3.5 w-3.5" />
              {kbBusy ? t("Reading…") : t("Upload docs")}
            </button>
            <button
              onClick={saveKb}
              disabled={kbSaving || kbBusy}
              className="rounded-lg bg-brand-600 px-3 py-1.5 text-[12px] font-medium text-[var(--on-brand)] shadow-[var(--shadow-1)] transition-[filter] hover:brightness-[1.06] disabled:opacity-40"
            >
              {kbSaving ? t("Saving…") : t("Save")}
            </button>
            <span className="text-[11px] text-ink-400">
              {t("Supports md, txt, json, csv, docx, pdf, xlsx, html, rtf — multiple files at once")}
            </span>
          </div>
          {kbNote && (
            <div className="mt-2.5 flex items-start gap-1.5 rounded-lg bg-[var(--ok-bg)] px-2.5 py-1.5 text-[11.5px] text-[var(--ok-fg)]">
              <span>✓</span>
              <span>{kbNote}</span>
            </div>
          )}
        </div>
      )}

      <div className="tp-card min-h-0 flex-1 space-y-4 overflow-auto border border-[var(--line)] p-5">
        {turns.length === 0 && (
          <div className="space-y-3 py-12 text-center">
            <div className="mx-auto grid h-11 w-11 place-items-center rounded-2xl bg-brand-50 text-brand-700">
              <Bot className="h-5 w-5" />
            </div>
            <div className="text-[13px] text-ink-500">{t("Ask it to inspect runs, or draft cases from your spec.")}</div>
            <div className="flex flex-wrap justify-center gap-2 pt-3">
              {examples.map((e) => (
                <button
                  key={e}
                  onClick={() => send(e)}
                  className="rounded-full border border-[var(--line)] px-3 py-1.5 text-[12px] text-ink-700 outline-none transition-[background-color,border-color,transform] hover:border-brand-200 hover:bg-brand-50 focus-visible:ring-2 focus-visible:ring-brand-100 active:translate-y-px"
                >
                  {e}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((m, i) => (
          <div key={i} className={clsx("flex gap-3", m.role === "user" && "flex-row-reverse")}>
            <div
              className={clsx(
                "grid h-7 w-7 shrink-0 place-items-center rounded-lg",
                m.role === "user"
                  ? "bg-gradient-to-br from-brand-500 to-brand-700 text-[var(--on-brand)]"
                  : "border border-[var(--line)] bg-[var(--panel2)] text-ink-700",
              )}
            >
              {m.role === "user" ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
            </div>
            <div className="max-w-[82%] space-y-1.5">
              <div
                className={clsx(
                  "whitespace-pre-wrap rounded-xl px-3.5 py-2.5 text-[13px] leading-relaxed",
                  m.role === "user"
                    ? "bg-brand-50 text-ink-900"
                    : "border border-[var(--line)] bg-[var(--panel2)] text-ink-900",
                )}
              >
                {m.content}
              </div>
              {!!m.actions?.length && (
                <div className="space-y-1 rounded-lg border border-[var(--line)] bg-[var(--panel)] px-2.5 py-1.5">
                  <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-ink-400">
                    <Wrench className="h-3 w-3" />
                    {t("Actions")}
                  </div>
                  {m.actions.map((a, j) => (
                    <div key={j} className="font-mono text-[11px] text-ink-700">
                      <span className={a.ok ? "text-[var(--ok-fg)]" : "text-[var(--bad-fg)]"}>{a.ok ? "✓" : "✗"}</span>{" "}
                      {describe(a)}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}

        {busy && (
          <div className="flex items-center gap-2 text-[13px] text-ink-500">
            <span className="flex gap-1">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand-500 [animation-delay:-0.3s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand-500 [animation-delay:-0.15s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand-500" />
            </span>
            {t("Thinking…")}
          </div>
        )}
        {err && <div className="text-[13px] text-[var(--bad-fg)]">{err}</div>}
        <div ref={endRef} />
      </div>

      <div className="mt-3 flex items-end gap-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={2}
          placeholder={t("Ask the assistant… (Enter to send, Shift+Enter for newline)")}
          className="flex-1 resize-none rounded-xl border border-[var(--line)] bg-[var(--panel)] px-3.5 py-2.5 text-[13px] text-ink-900 outline-none transition-[border-color,box-shadow] placeholder:text-ink-400/80 focus:border-brand-500 focus:ring-2 focus:ring-brand-100"
        />
        <button
          onClick={() => send()}
          disabled={busy || !input.trim()}
          className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 text-[var(--on-brand)] shadow-[0_3px_10px_-4px_color-mix(in_oklch,var(--brand-700)_70%,transparent)] outline-none transition-[opacity,transform,filter] hover:brightness-[1.06] focus-visible:ring-2 focus-visible:ring-brand-100 active:translate-y-px disabled:pointer-events-none disabled:opacity-40"
          aria-label={t("Send")}
        >
          <Send className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

