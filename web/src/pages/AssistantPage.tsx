import { BookOpen, Bot, Loader2, Paperclip, Send, Trash2, User, Wrench, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";
import clsx from "clsx";
import { api, type AssistantAttachment } from "../lib/api";
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

/**
 * 内置助手 —— 一个能读需规、能操作平台的**操作型**助手。
 *  - POST /api/projects/:pid/assistant  → LLM + 工具（potato-test_api / search_knowledge / create_case…）
 *  - 需规/资料（搜索用的知识库）在**「项目文档」页**管理与上传（2026-10-12 移过去）。
 *    助手仍能用 search_knowledge 检索它；这一页只是不再做它的编辑界面。
 *  - 对话按项目存在 localStorage，刷新不丢。
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

  // 需规相关的状态（kb / kbChars / kbSaving / kbBusy / kbPreviewOnly / fileRef）
  // 随面板一起删掉了（2026-10-12）—— 留着这些死状态会让人以为这一页还能改需规。

  // 对话框里的附件（粘贴 / 拖入 / 点选）。文件先经后端解析成文本再随这一轮发出。
  const [atts, setAtts] = useState<AssistantAttachment[]>([]);
  const [attBusy, setAttBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const attRef = useRef<HTMLInputElement>(null);

  /** 解析并挂上文件。**逐个失败逐个报**，不让一个坏文件把好的也丢掉。 */
  async function addFiles(files: File[]) {
    if (!files.length) return;
    setAttBusy(true);
    const ok: AssistantAttachment[] = [];
    const bad: string[] = [];
    for (const f of files) {
      try {
        ok.push(await api.parseAssistantAttachment(projectId, f));
      } catch (e) {
        bad.push(`${f.name}: ${e instanceof Error ? e.message : String(e)}`);
      }
    }
    if (ok.length) setAtts((s) => [...s, ...ok]);
    if (bad.length) setErr(bad.join("；"));
    setAttBusy(false);
  }

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
  // ★ 这一页不再读写需规（2026-10-12）。原来这里会 getKnowledge + 把正文塞进
  // 一个 textarea，并在保存时提醒"只载入了前 20 万字符"。整个面板搬到
  // 「项目文档」页之后，这些状态与守卫都成了死代码 —— 留着只会让人以为
  // 这一页还能改需规。需规的编辑与守卫（含那个 20 万字符的提醒）在那边。
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  async function send(text?: string) {
    const msg = (text ?? input).trim();
    // ★ 只有附件、没有文字也要能发（用户拖进来一份需规就想问"这是什么"很常见）。
    if ((!msg && atts.length === 0) || busy) return;
    const history = turns.map((m) => ({ role: m.role, content: m.content }));
    // 对话里显示一条带附件名的用户消息，这样回看历史时知道当时附了什么 ——
    // 附件正文本身不进历史（太占篇幅），但要让人看得见"这一轮基于什么"。
    const label = atts.length
      ? `${msg || t("（见附件）")}\n📎 ${atts.map((a) => a.filename).join("、")}`
      : msg;
    const next: Msg[] = [...turns, { role: "user", content: label }];
    setTurns(next);
    setInput("");
    const sent = atts;
    setAtts([]);
    setBusy(true);
    setErr(null);
    try {
      const res = await api.assistant(
        projectId,
        { message: msg || t("请看附件内容。"), history, attachments: sent },
        { signal },
      );
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
          {/* ★ 需规入口整体移走了（2026-10-12）。
              用户明确说过「需归的上传放在前面那个里面」，我上次只是把**上传按钮**
              换成了链接，却把这个「需规 / 资料 · 459416」的入口留在这儿 ——
              于是他看到的仍然是"需规放在助手页"。现在整个按钮与折叠面板都去掉，
              助手页只做对话一件事；需规的查看与上传都在「项目文档」页。 */}
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

      {/* ★ 需规的查看 / 粘贴 / 上传整个面板都移走了（2026-10-12）。
          用户说的是「需归的上传放在前面那个里面」，而他截图里仍然看到
          「需规 / 资料 · 459416」挂在助手页 —— 那说明我上次只搬了上传按钮，
          没搬这个入口。现在助手页**只做对话**一件事；需规在「项目文档」页。
          想改需规时点这里过去，不必在这一页再放一个编辑器 ——
          两个地方都能改同一份文本，早晚会出现"改哪边为准"的困惑。 */}
      <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-[var(--line)] bg-[var(--panel2)] px-3 py-2 text-[12px] text-ink-500">
        <BookOpen className="h-3.5 w-3.5 shrink-0 text-ink-400" />
        <span>{t("需规 / 资料 已移到「项目文档」页（查看、粘贴、上传都在那里）。")}</span>
        <Link
          to={`/projects/${projectId}/docs`}
          className="font-medium text-brand-700 hover:underline"
        >
          {t("去「项目文档」")}
        </Link>
      </div>

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

      {/* ★ 附件区：粘贴/拖入/点选文件（2026-10-12 用户要求「和其他 agent 一样
          有粘贴文件在对话框的能力」）。
          文件先被后端解析成文本再挂进这一轮对话 —— 所以模型看到的是**内容**，
          而不是"这里有个文件"这种它没法处理的占位。 */}
      {atts.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {atts.map((a, i) => (
            <span
              key={i}
              className="flex items-center gap-1.5 rounded-lg border border-[var(--line)] bg-[var(--panel2)] px-2 py-1 text-[11px] text-ink-700"
              title={a.truncated ? t("内容过长，只带了开头部分") : undefined}
            >
              <Paperclip className="h-3 w-3 shrink-0 text-ink-400" />
              <span className="max-w-[220px] truncate">{a.filename}</span>
              <span className="tabular-nums text-ink-400">
                {a.chars.toLocaleString()}
                {a.truncated ? "+" : ""}
              </span>
              <button
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

      <div
        className={
          "mt-3 flex items-end gap-2 rounded-xl " +
          (drag ? "ring-2 ring-brand-300 ring-offset-0" : "")
        }
        onDragOver={(e) => {
          // 必须 preventDefault，否则浏览器会用默认行为（直接打开文件）
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          void addFiles(Array.from(e.dataTransfer?.files ?? []));
        }}
      >
        <input
          ref={attRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            void addFiles(Array.from(e.target.files ?? []));
            e.target.value = ""; // 同一个文件还能再选
          }}
        />
        <button
          onClick={() => attRef.current?.click()}
          disabled={busy || attBusy}
          title={t("附加文件（也可以直接粘贴或拖进来）")}
          className="grid h-11 w-11 shrink-0 place-items-center rounded-xl border border-[var(--line)] text-ink-600 outline-none transition-[background-color,border-color] hover:border-[var(--line-strong)] hover:bg-[var(--panel2)] disabled:opacity-40"
          aria-label={t("附加文件")}
        >
          {attBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
        </button>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onPaste={(e) => {
            // 粘贴板里带文件（截图、复制的文件）也要能进来
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
          rows={2}
          placeholder={t(
            "问助手…（Enter 发送，Shift+Enter 换行；可直接粘贴 / 拖入文件）",
          )}
          className="flex-1 resize-none rounded-xl border border-[var(--line)] bg-[var(--panel)] px-3.5 py-2.5 text-[13px] text-ink-900 outline-none transition-[border-color,box-shadow] placeholder:text-ink-400/80 focus:border-brand-500 focus:ring-2 focus:ring-brand-100"
        />
        <button
          onClick={() => send()}
          disabled={busy || (!input.trim() && atts.length === 0)}
          className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 text-[var(--on-brand)] shadow-[0_3px_10px_-4px_color-mix(in_oklch,var(--brand-700)_70%,transparent)] outline-none transition-[opacity,transform,filter] hover:brightness-[1.06] focus-visible:ring-2 focus-visible:ring-brand-100 active:translate-y-px disabled:pointer-events-none disabled:opacity-40"
          aria-label={t("Send")}
        >
          <Send className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}



