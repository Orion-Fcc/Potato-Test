import {
  AlertTriangle,
  BookOpen,
  ChevronDown,
  ChevronRight,
  FileUp,
  Loader2,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type KnowledgeFormats } from "../lib/api";
import { Button, Card } from "./ui";
import { useErrorToast } from "./toast";

/** 需规 / 资料 的上传与状态（2026-10-12）。
 *
 *  ## 为什么把它挪到「项目文档」页
 *
 *  用户的原话：「我希望需归的上传放在前面那个里面，要允许上传多种格式的」。
 *  这个位置本来就是对的：**上传需规是"项目文档"这件事的第一步** ——
 *  没有需规，AI 分析与生成无从下手。而它原来在「助手」页，
 *  用户得先知道去那儿上传、再回来生成，中间那一步没有任何提示。
 *
 *  ## 为什么要从后端取"支持哪些格式"
 *
 *  前端写死一份清单一定会和后端漂移：后端加了格式前端不知道，删了前端还放行。
 *  放行一个后端读不了的格式，症状是"上传成功但内容为空" —— 最难查的那种。
 *  所以 `accept` 和提示文案都由 `/knowledge/formats` 给。
 */
export function SpecSourcePanel({
  pid,
  onUploaded,
  formats: formatsProp,
}: {
  pid: number;
  onUploaded?: () => void;
  /** 支持格式由父组件给时就用它，省一次请求。
   *  父组件（项目文档页）也要用同一份清单做「上传成文档」的 accept ——
   *  两处各取一次会多一个请求，而且理论上可能拿到不同的结果。 */
  formats?: KnowledgeFormats | null;
}) {
  const { t } = useTranslation();
  const fail = useErrorToast();
  const fileRef = useRef<HTMLInputElement>(null);

  const [stats, setStats] = useState<{ chars: number; chunks: number } | null>(null);
  const [formatsLocal, setFormatsLocal] = useState<KnowledgeFormats | null>(null);
  const formats = formatsProp ?? formatsLocal;
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [problems, setProblems] = useState("");
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.getKnowledge(pid);
      setStats({ chars: r.chars, chunks: r.chunks });
    } catch (e) {
      fail(e);
    }
  }, [pid, fail]);

  useEffect(() => {
    load();
    // 父组件给了就不重复请求
    if (formatsProp) return;
    api
      .knowledgeFormats(pid)
      .then(setFormatsLocal)
      .catch(() => {
        /* 拿不到格式清单不影响上传，静默 */
      });
  }, [pid, load, formatsProp]);

  /** 一次可以选多个文件。逐个上传而不是本地拼成一个大字符串：
   *  那样浏览器和接口都不必一次吞下整份合并后的文档（100MB 的需规实测过）。 */
  async function onPick(list: FileList | null) {
    const files = Array.from(list ?? []);
    if (!files.length) return;
    setBusy(true);
    setNote("");
    setProblems("");
    const done: string[] = [];
    const bad: string[] = [];
    for (const f of files) {
      try {
        const r = await api.extractKnowledge(pid, f);
        // 每个文件单独起一个 `## 文件名` 段：出处的文件名要留在正文里，
        // 否则 AI 引用时说不清是哪份文档。
        const saved = await api.appendKnowledge(pid, `## ${r.filename}\n${r.text}\n`);
        done.push(
          `${r.filename} · ${r.chars.toLocaleString()} ${t("chars")}${r.truncated ? ` · ${t("truncated")}` : ""}`,
        );
        if (r.warnings.length) bad.push(`${r.filename}: ${r.warnings.join("; ")}`);
        setStats({ chars: saved.chars, chunks: saved.chunks });
      } catch (e) {
        bad.push(`${f.name}: ${e instanceof Error ? e.message : String(e)}`);
      }
    }
    if (done.length) setNote(`${t("Imported")}: ${done.join("、")}`);
    if (bad.length) setProblems(bad.join("；"));
    setBusy(false);
    if (fileRef.current) fileRef.current.value = ""; // 同一个文件还能再选一次
    if (done.length) onUploaded?.();
  }

  const hasDocs = (stats?.chars ?? 0) > 0;

  return (
    <Card className="p-3">
      <div className="flex flex-wrap items-center gap-2">
        <BookOpen className="h-4 w-4 shrink-0 text-ink-500" />
        <span className="text-[13px] font-medium text-ink-800">{t("需规 / 资料")}</span>
        {hasDocs ? (
          <span className="text-[12px] text-ink-500 tabular-nums">
            {stats!.chars.toLocaleString()} {t("chars")} · {t("{{n}} 段", { n: stats!.chunks })}
          </span>
        ) : (
          <span className="text-[12px] text-[var(--warn-fg)]">
            {t("还没有需规 —— 先生成文档前必须先上传")}
          </span>
        )}

        <div className="ml-auto flex items-center gap-2">
          <input
            ref={fileRef}
            type="file"
            multiple
            // accept 由后端给，避免和 docparse 的真实支持集合漂移
            accept={formats?.accept ?? ".md,.txt,.docx,.pdf,.xlsx,.html,.rtf"}
            className="hidden"
            onChange={(e) => onPick(e.target.files)}
          />
          <Button size="sm" disabled={busy} onClick={() => fileRef.current?.click()}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
            {busy ? t("Reading…") : t("Upload docs")}
          </Button>
        </div>
      </div>

      {/* 支持哪些格式：默认收起，但**一定要有** —— 用户会问"我的 .doc 能不能传" */}
      {formats && (
        <div className="mt-2">
          <button
            onClick={() => setOpen((v) => !v)}
            className="inline-flex items-center gap-1 text-[11px] text-brand-700 hover:underline"
          >
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {t("支持哪些格式？")}
          </button>
          {open && (
            <div className="mt-1 space-y-1 rounded border border-[var(--line)] bg-[var(--panel2)] px-2.5 py-2 text-[11px]">
              <p className="text-ink-600">
                <strong>{t("文档")}</strong>：docx · pdf · xlsx · xlsm · html · rtf · md · txt
              </p>
              <p className="text-ink-500">
                <strong>{t("纯文本类")}</strong>：{formats.text.map((x) => x.replace(".", "")).join(" · ")}
              </p>
              <p className="flex items-start gap-1 text-[var(--warn-fg)]">
                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  {t("不支持老版二进制格式")}：{formats.legacy_unsupported.map((x) => x.replace(".", "")).join(" · ")}
                  {" — "}
                  {t("请用 Office 另存为 docx / xlsx / pptx 后再上传。")}
                </span>
              </p>
              <p className="text-ink-400">{t("一次可以选多个文件，会按文件名分段落追加进去。")}</p>
            </div>
          )}
        </div>
      )}

      {note && <p className="mt-1.5 text-[11px] text-[var(--ok-fg)]">{note}</p>}
      {problems && (
        <p className="mt-1.5 flex items-start gap-1 text-[11px] text-[var(--bad-fg)]">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span className="min-w-0 break-all">{problems}</span>
        </p>
      )}
      {hasDocs && (
        <p className="mt-1.5 flex items-center gap-1 text-[11px] text-ink-400">
          <FileUp className="h-3 w-3" />
          {t("上传后到右边点「AI 分析与生成」，AI 会读它并写出项目文档。")}
        </p>
      )}
    </Card>
  );
}
