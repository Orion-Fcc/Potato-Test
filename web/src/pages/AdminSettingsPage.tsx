import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ChevronRight } from "lucide-react";
import { api, type FeishuStatus, type LlmPoolEntry, type LlmPremiumStatus, type LlmStatus, type LlmTestReport, type LlmTestResult } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Card, Checkbox, Field, Input, Select } from "../components/ui";
import { useToast, useErrorToast } from "../components/toast";
import { AppUpdateCard } from "../components/AppUpdateCard";
import { StorageCard } from "../components/StorageCard";

// 协议取值与后端 app/llm_protocol.py 的 normalize_provider 对齐。
// auto = 自动识别（先看地址线索，再依次小请求探测），绝大多数国产网关是 OpenAI 兼容。
const PROVIDERS = ["auto", "openai", "anthropic", "google", "ollama"] as const;
const PROVIDER_LABEL: Record<string, string> = {
  auto: "Auto-detect",
  openai: "OpenAI-compatible",
  anthropic: "Anthropic (Claude)",
  google: "Google (Gemini)",
  ollama: "Ollama (local)",
};

type PoolRow = LlmPoolEntry & { api_key: string };

// 用途清单与后端 app/llm.py::ROLES 对齐。顺序按"花费从大到小"排：
// 真库实测 agent 每步调用占全部 token 的约 95%，所以"执行"这一行是省钱的唯一大旋钮，
// 判定/生成量小而质量敏感，值得留强模型。
// judge_escalate 是复核层：只有初判 failed 或确定性闸门报警时才调用，
// 所以它可以放比 judge 更贵的模型 —— 没触发就一分钱不花。
const ROLE_LABEL: Record<string, string> = {
  agent: "Execution (agent, every step)",
  judge: "Judging (once per case)",
  judge_escalate: "Judge escalation (review)",
  narrative: "Failure narrative / case memory",
  specgen: "Case generation from spec",
  docgen: "Project docs generation (overview / analysis / flowchart)",
  caseopt: "AI case optimization (rewrite cases)",
  failattr: "Failure root-cause classification",
  assistant: "Assistant chat & Feishu",
};

// 能力徽标只用符号，不引入新的文案键（服务端实测出来的布尔值无歧义）
function mark(ok: boolean) {
  return ok ? "✓" : "✕";
}

/**
 * 一个 ? 图标，悬浮（或键盘聚焦）才展开完整解释。
 *
 * 为什么要有它（2026-10-11 简化）：这个页面以前把每一条设计理由都写成正文段落，
 * 一屏能堆七八段 —— 这就是「LLM 设置页面过于复杂」的一半来源。但那些理由本身有
 * 价值（不讲清楚，用户会在"配了却没生效"上反复踩坑，而这正是前几版花大力气消灭的
 * 那类错觉），直接删掉只是把复杂度换成了沉默。所以挪到悬浮层：默认视图每个区块
 * 只说一句话，想深究的把鼠标放上去看全部。
 *
 * 两个实现上的坑：
 * 1) 必须是 <button> 而不是 <span> —— 需要能 Tab 到，group-focus-within 才有意义，
 *    也顺带给读屏器一个 aria-label。
 * 2) **不要**把 Hint 放进 <label> 里。按钮的 click 会冒泡到 label、顺带切换关联的
 *    复选框，于是"点问号看说明"会变成"点问号改了配置"。
 */
function Hint({ text }: { text: string }) {
  return (
    <span className="group relative ml-1 inline-flex align-baseline">
      <button
        type="button"
        aria-label={text}
        className="grid h-3.5 w-3.5 translate-y-[1px] place-items-center rounded-full border border-[var(--line-strong)] text-[9px] font-semibold leading-none text-ink-400 outline-none transition-colors hover:border-brand-500 hover:text-brand-600 focus-visible:ring-2 focus-visible:ring-brand-100"
      >
        ?
      </button>
      {/* pointer-events-none：它盖在下面的控件上，不能挡住点击 */}
      <span
        role="tooltip"
        className="pointer-events-none absolute left-0 top-full z-30 mt-1.5 hidden w-[20rem] max-w-[70vw] rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-[11px] font-normal leading-relaxed text-ink-600 shadow-[var(--shadow-4)] group-hover:block group-focus-within:block"
      >
        {text}
      </span>
    </span>
  );
}

/**
 * 折叠区。默认收起 —— 这个页面的问题从来不是"功能缺失"，而是默认视图里塞了
 * 四十多个控件和八段解释。折叠让默认视图只回答两个问题：现在跑的是哪一套 /
 * 要不要为质量多加一支贵模型；真要逐项微调的人再展开。
 *
 * 标题用 <button>、提示用**同级**的 <Hint>（不是嵌在按钮里）—— 按钮套按钮是非法
 * HTML，点击行为也会互相打架。右侧 summary 显示"收起来的时候你错过了什么"，
 * 免得折叠等于藏东西。
 */
function Fold({
  open,
  onToggle,
  title,
  badge,
  hint,
  summary,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  title: string;
  badge?: string;
  hint?: string;
  summary?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <button
            type="button"
            onClick={onToggle}
            className="inline-flex items-center gap-1.5 text-sm font-medium text-ink-900 outline-none transition-colors hover:text-brand-700 focus-visible:ring-2 focus-visible:ring-brand-100"
          >
            <ChevronRight
              className={`h-4 w-4 shrink-0 text-ink-400 transition-transform duration-150${open ? " rotate-90" : ""}`}
            />
            {title}
          </button>
          {badge && <span className="text-[11px] font-normal text-ink-400">{badge}</span>}
          {hint && <Hint text={hint} />}
        </div>
        {summary && <div className="text-[11px] text-ink-500">{summary}</div>}
      </div>
      {open && <div className="mt-3 space-y-3">{children}</div>}
    </div>
  );
}

/** 卡片标题行：左边标题（可挂 ? 提示），右边一句"现在的状态"。 */
function CardTitle({ title, hint, right }: { title: string; hint?: string; right?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <div className="text-sm font-medium text-ink-900">
        {title}
        {hint && <Hint text={hint} />}
      </div>
      {right && <div className="text-[11px] text-ink-500">{right}</div>}
    </div>
  );
}

// 一个用途行在"还没从后端拿到"时的空壳（含 effective_* 的占位），
// 免得每个 onChange 都要判 null。
// 平台级「高端模型」可以挂到哪些用途上（与服务端 app/llm.py::PREMIUM_ROLES 对齐）。
//
// agent 排在最后并且**默认不勾**：它占全部 token 的约 95%，把执行步换成贵模型
// 等于把省钱的唯一大旋钮关掉。放在这里是因为"我就想要最高质量"也该被允许，
// 但要勾就得先看到那句警告 —— 不能让人拨个总开关就悄悄多花一大笔。
const PREMIUM_ROLES = [
  "judge_escalate",
  "judge",
  "specgen",
  "docgen",
  "caseopt",
  "failattr",
  "narrative",
  "assistant",
  "agent",
] as const;
// 一开开关默认勾上的用途：量小、质量关键的那些。judge 不在内 ——
// 初判刻意留给便宜模型，那正是两级判定的意义。
const PREMIUM_DEFAULT_ROLES = ["judge_escalate", "specgen", "narrative"];

// 一个高端模型配置在"还没从后端拿到"时的空壳。
function emptyPremium(): LlmPremiumStatus {
  return {
    enabled: false,
    pool_name: "",
    resolved_model: "",
    roles: [],
    usable: false,
    applies_to: [],
  };
}

/**
 * 「高端模型」卡里的建议块：直接告诉用户哪些环节该上高端模型。
 *
 * 依据（和 ROLE_TOKEN_SHARE_ESTIMATE 一致）：agent 占全部 token 的约 94%，把执行步
 * 换贵等于把"省钱主旋钮"关掉，所以**不推荐**；其余用途都是"质量敏感、量小"——
 * 判定/复核决定用例成败、需规/叙述决定交付物质量、助手偶发——用更好的模型性价比最高。
 *
 * 占比数字优先用真实实测（tokenShare.measured），无数据回退结构估算并标 ≈，
 * 与「勾选项旁占比」同一套口径，绝不把估算冒充实测。
 */
function PremiumSuggestion({
  poolEmpty,
  suggested,
  selected,
  tokenShare,
  onApply,
}: {
  poolEmpty: boolean;
  suggested: readonly string[];
  selected: string[];
  tokenShare: {
    measured: Record<string, number>;
    estimate: Record<string, number>;
    has_data: boolean;
    runs: number;
  } | null;
  onApply: () => void;
}) {
  const { t } = useTranslation();

  const pctOf = (r: string): { pct: number; real: boolean } | null => {
    if (!tokenShare) return null;
    if (tokenShare.has_data && tokenShare.measured[r] != null) {
      return { pct: tokenShare.measured[r], real: true };
    }
    return { pct: tokenShare.estimate[r] ?? 0, real: false };
  };
  const agentPct = pctOf("agent");
  const suggestedPct = suggested.reduce((s, r) => s + (pctOf(r)?.pct ?? 0), 0);
  const allSelected = suggested.every((r) => selected.includes(r));

  return (
    <div className="rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[12px] font-medium text-ink-900">
          {t("Suggested: give the premium model to quality-sensitive, low-volume steps")}
        </div>
        <Button
          variant="ghost"
          className="h-7 text-[11px]"
          disabled={poolEmpty || allSelected}
          onClick={onApply}
        >
          {allSelected ? t("Suggestion applied") : t("Apply suggestion")}
        </Button>
      </div>
      <div className="mt-1.5 space-y-0.5">
        {suggested.map((r) => {
          const tp = pctOf(r);
          return (
            <div key={r} className="flex items-center gap-2 text-[11px] text-ink-600">
              <span className="text-ink-700">{t(ROLE_LABEL[r])}</span>
              {tp && (
                <span className={tp.real ? "text-ink-500" : "text-ink-400"}>
                  {tp.real ? `· ${tp.pct}%` : `· ≈${tp.pct}%`}
                </span>
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-1.5 text-[11px] text-ink-400">
        {t(
          "These add up to about {{pct}}% of tokens — small volume, so the premium model is cheap here. Execution is ~{{agent}}% of tokens, so it is deliberately not recommended.",
          { pct: Math.round(suggestedPct), agent: agentPct ? Math.round(agentPct.pct) : "—" },
        )}
        {poolEmpty && (
          <>
            {" "}
            {t("Add at least one model to the pool first, then pick it above as the premium model.")}
          </>
        )}
      </div>
    </div>
  );
}

function RowTestResult({ result }: { result: LlmTestResult }) {
  const { t } = useTranslation();
  const mark = (b?: boolean) => (b ? "✓" : "✕");
  if (!result.ok) {
    return <div className="text-[11px] text-[var(--bad-fg)]">✕ {result.error || "failed"}</div>;
  }
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px] text-ink-500">
      <span className="font-medium text-[var(--ok-fg,#1a7f4b)]">✓ {result.model}</span>
      <span>{t("Protocol")}: {result.protocol}</span>
      <span>{t("JSON object")}: {mark(result.json_object)}</span>
      <span>{t("JSON schema")}: {mark(result.json_schema)}</span>
      <span>{t("Tools")}: {mark(result.tools)}</span>
      <span>{t("Vision")}: {mark(result.vision)}</span>
    </div>
  );
}

export function AdminSettingsPage() {
  const { t } = useTranslation();
  const toast = useToast();
  const fail = useErrorToast();
  const { gitlabEnabled, feishuEnabled } = useAuth();
  const [tokenSet, setTokenSet] = useState(false);
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);

  // LLM model（只读展示真实生效值 + 连通性测试；编辑入口在下面的模型池里）
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [testingLlm, setTestingLlm] = useState(false);
  const [llmTest, setLlmTest] = useState<LlmTestReport | null>(null);
  // 每行「探测」：精测模型池里单一条目（按 name 走后端过滤），结果就地显示在该行。
  const [rowTests, setRowTests] = useState<Record<number, { loading: boolean; result?: LlmTestResult }>>({});
  const [loadError, setLoadError] = useState("");

  // LLM 池（多套网关，按顺序 429 兜底）—— 这是**唯一**的网关配置入口。
  const [pool, setPool] = useState<PoolRow[] | null>(null);
  // 池为空时后端会回一条从 .env 读出来的回显条目（seeded=true）。界面要如实说明
  // "这条还没进池"，否则用户会以为已经保存过了。
  const [poolSeeded, setPoolSeeded] = useState(false);
  const [savingPool, setSavingPool] = useState(false);

  // 平台级「高端模型」（贵、聪明，可选）。默认关。
  // 有意的设计：整个平台"要不要为质量多花钱"只在这里回答一次（2026-10-12 简化后，
  // 这是唯一的模型分配控件，不再有"逐环节指定"那张子卡）。空壳用 emptyPremium()，
  // 字段与后端 premium_for_ui() 对齐。
  const [premium, setPremium] = useState<LlmPremiumStatus>(emptyPremium);
  // 「模型分配」只用这一个保存按钮和一个 saving 标记。
  const [savingModels, setSavingModels] = useState(false);

  // 折叠态（2026-10-11 简化）。默认视图只留"最常用的两件事"，其余收起来：
  //   showExtraGateways —— 模型池里第 2 套以后的兜底网关（只有一套网关时纯噪音）
  const [showExtraGateways, setShowExtraGateways] = useState(false);
  // 各角色 token 占比（设置页展示用）。measured=真实实测，estimate=结构估算兜底。
  const [tokenShare, setTokenShare] = useState<{
    measured: Record<string, number>;
    estimate: Record<string, number>;
    has_data: boolean;
    runs: number;
  } | null>(null);

  // Feishu
  const [fs, setFs] = useState<FeishuStatus | null>(null);
  const [appId, setAppId] = useState("");
  const [apiBase, setApiBase] = useState("https://open.feishu.cn");
  const [autoAnswer, setAutoAnswer] = useState(true);
  const [appSecret, setAppSecret] = useState("");
  const [verifToken, setVerifToken] = useState("");
  const [savingFs, setSavingFs] = useState(false);

  const webhookUrl = `${window.location.origin}/api/feishu/events`;

  const load = () =>
    api
      .getAdminSettings()
      .then((s) => {
        setLoadError("");
        setTokenSet(s.gitlab_token_set);
        // s.llm 现在是**真实生效**的那一套（池的第 1 条），不再是 env/旧单值字段。
        // 所以下面这块面板显示的模型名与实际调用逐字节一致。
        setLlm(s.llm);
        setPremium(s.llm.premium || emptyPremium());
        // 各角色 token 占比：与上面的设置加载并行，互不依赖。
        api.getLlmTokenShare().then(setTokenShare).catch(() => {});
        setFs(s.feishu);
        setAppId(s.feishu.app_id);
        setApiBase(s.feishu.api_base);
        setAutoAnswer(s.feishu.auto_answer_detected);
      })
      .catch((e) => setLoadError(e?.message || String(e)));

  const loadPool = () =>
    api
      .getLlmPool()
      .then((r) => {
        setPool(r.pool.map((p) => ({ ...p, api_key: "" })));
        setPoolSeeded(!!r.seeded);
        setRowTests({}); // 重新装载后旧的逐行结果可能对不上条目，清掉
      })
      .catch((e) => setLoadError(e?.message || String(e)));

  useEffect(() => {
    load();
    loadPool();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const savePool = async () => {
    if (!pool) return;
    setSavingPool(true);
    try {
      const rows = pool
        .filter((r) => r.base_url.trim() && r.model.trim())
        .map((r, i) => ({
          name: (r.name || `llm${i + 1}`).trim(),
          base_url: r.base_url.trim(),
          model: r.model.trim(),
          agent_model: (r.agent_model || "").trim(),
          provider: r.provider || "auto",
          // 留空 → 后端保持同位置已存的那把钥匙
          api_key: r.api_key.trim() || undefined,
        }));
      if (!rows.length) {
        toast("error", t("Fill in at least one entry (base URL + model)."));
        return;
      }
      await api.setLlmSettings({ pool: rows });
      await loadPool();
      toast("success", t("Saved"));
    } catch (e: any) {
      fail(e);
    } finally {
      setSavingPool(false);
    }
  };

  // 保存「模型分配」= 只提交平台级「高端模型接管」（2026-10-12 简化：砍掉合并时的
  // 逐环节覆盖 / 两级判定两张子卡，只留这一个机制）。一次 PUT，后端整体替换 premium。
  // 2026-10-12 起：高端模型只从模型池里选一套（pool_name），不再自己提交连接信息。
  // 注意：逐环节覆盖（后端 roles 字段）不在界面暴露后这里不提交它 —— 后端保持原值，
  // 已存的逐环节覆盖仍按"更具体者胜"生效，只是不再可编辑；需要清空告诉我即可。
  const saveModels = async () => {
    setSavingModels(true);
    try {
      await api.setLlmSettings({
        premium: {
          enabled: premium.enabled,
          pool_name: premium.pool_name.trim(),
          roles: [...premium.roles],
        },
      });
      await load();
      await loadPool();
      toast("success", t("Saved"));
    } catch (e: any) {
      fail(e);
    } finally {
      setSavingModels(false);
    }
  };

  const testLlm = async () => {
    setTestingLlm(true);
    setLlmTest(null);
    try {
      const r = await api.testLlmSettings();
      setLlmTest(r);
    } catch (e: any) {
      setLlmTest({
        ok: false,
        all_ok: false,
        results: [{ name: "—", ok: false, error: e?.message || String(e) }],
      });
    } finally {
      setTestingLlm(false);
    }
  };

  const testRow = async (i: number, name: string) => {
    const nm = (name || "").trim();
    if (!nm) {
      setRowTests((m) => ({
        ...m,
        [i]: { loading: false, result: { name: "", ok: false, error: t("Name and save this entry first.") } },
      }));
      return;
    }
    setRowTests((m) => ({ ...m, [i]: { loading: true } }));
    try {
      const rep = await api.testLlmSettings([nm]);
      const r = rep.results.find((x) => x.name === nm) ?? rep.results[0];
      setRowTests((m) => ({ ...m, [i]: { loading: false, result: r } }));
    } catch (e: any) {
      setRowTests((m) => ({ ...m, [i]: { loading: false, result: { name: nm, ok: false, error: e?.message || String(e) } } }));
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const r = await api.setGitlabToken(token.trim());
      setTokenSet(r.gitlab_token_set);
      setToken("");
      toast("success", t("Saved"));
    } catch (e) {
      fail(e);
    } finally {
      setSaving(false);
    }
  };

  const saveFeishu = async () => {
    setSavingFs(true);
    try {
      const r = await api.setFeishuSettings({
        app_id: appId.trim(),
        api_base: apiBase.trim(),
        auto_answer_detected: autoAnswer,
        app_secret: appSecret.trim() || undefined,
        verification_token: verifToken.trim() || undefined,
      });
      setFs(r);
      setAppSecret("");
      setVerifToken("");
      toast("success", t("Saved"));
    } catch (e) {
      fail(e);
    } finally {
      setSavingFs(false);
    }
  };

  // 高端模型的表单态判断（不看服务端回值，否则"改了还没保存"时提示是错的）。
  // 高端模型"真的能用" = 开了 + 选的池条目确实存在于模型池 + 勾了至少一步。
  const premiumResolved = pool != null && pool.some((p) => p.name === premium.pool_name);
  const premiumResolvedModel = pool?.find((p) => p.name === premium.pool_name)?.model ?? "";
  const premiumReady = premium.enabled && premiumResolved && premium.roles.length > 0;
  const premiumOnAgent = premium.enabled && premium.roles.includes("agent");
  // 已保存且真的在生效的用途（用服务端回值，避免把未保存的编辑当成现状）
  const premiumLiveRoles = (llm?.premium?.applies_to || [])
    .map((r) => `${t(ROLE_LABEL[r] || r)}`)
    .join(" / ");

  // 池条目的编辑块。抽成函数是因为它现在出现在两处：默认展开的第 1 套，
  // 以及折叠区里的第 2 套以后（两处必须长得一模一样，否则用户会以为"兜底的"是另一套东西）。
  const poolEntry = (row: PoolRow, i: number) => (
    <div key={i} className="space-y-2 rounded-lg border border-[var(--line)] p-2.5">
      <div className="flex items-center gap-2">
        <span className="w-6 text-center font-mono text-xs text-ink-500">{i + 1}</span>
        <Input
          value={row.name}
          onChange={(e) => setPool((pool || []).map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))}
          placeholder={t("Name")}
          className="w-40"
        />
        <Select
          value={row.provider || "auto"}
          onChange={(e) =>
            setPool((pool || []).map((r, j) => (j === i ? { ...r, provider: e.target.value } : r)))
          }
          className="w-44"
        >
          {PROVIDERS.map((p) => (
            <option key={p} value={p}>
              {t(PROVIDER_LABEL[p])}
            </option>
          ))}
        </Select>
        <Button
          variant="ghost"
          className="h-7 text-[11px]"
          onClick={() => testRow(i, row.name)}
          disabled={rowTests[i]?.loading}
          title={t("Probe this entry (uses the saved config)")}
        >
          {rowTests[i]?.loading ? t("Probing…") : t("Probe")}
        </Button>
        {/* 第 1 套不给"移除"：它是 agent 实际在跑的那一套，清掉整池就空了。
            想换掉它就直接改地址/模型。 */}
        {i > 0 && (
          <Button variant="ghost" onClick={() => setPool((pool || []).filter((_r, j) => j !== i))}>
            {t("Remove")}
          </Button>
        )}
      </div>
      <Input
        value={row.base_url}
        onChange={(e) =>
          setPool((pool || []).map((r, j) => (j === i ? { ...r, base_url: e.target.value } : r)))
        }
        placeholder="https://api.deepseek.com/v1"
      />
      <div className="grid grid-cols-3 gap-2">
        <Input
          value={row.model}
          onChange={(e) =>
            setPool((pool || []).map((r, j) => (j === i ? { ...r, model: e.target.value } : r)))
          }
          placeholder={t("Model")}
        />
        <Input
          value={row.agent_model}
          onChange={(e) =>
            setPool((pool || []).map((r, j) => (j === i ? { ...r, agent_model: e.target.value } : r)))
          }
          placeholder={t("Agent model")}
        />
        <Input
          type="password"
          value={row.api_key}
          onChange={(e) =>
            setPool((pool || []).map((r, j) => (j === i ? { ...r, api_key: e.target.value } : r)))
          }
          placeholder={row.api_key_set ? t("•••••••• (kept)") : "sk-…"}
        />
      </div>
      {rowTests[i]?.result && <RowTestResult result={rowTests[i]!.result!} />}
    </div>
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-ink-900">{t("System settings")}</h1>
        <p className="mt-1 text-sm text-ink-500">{t("Server-wide configuration (admin only).")}</p>
      </div>

      {loadError && (
        <div className="max-w-2xl rounded-lg bg-[var(--bad-bg,#fee)] px-3 py-2 text-xs text-[var(--bad-fg)]">
          {t("Could not load settings:")} {loadError}
        </div>
      )}

      {/* 真实生效的那一套（只读）。
          ★ 2026-10-11 修正：这块面板以前读的是 env / 旧单值字段，而配了池之后
          它们**完全不参与运行** —— 也就是说它会显示一个没在跑的模型名，且不报错。
          现在后端直接回解析后的最终结果（把高端模型/按用途覆盖都算进去），并把来源
          一并回出来，让"我在编辑的那一层是不是生效的那一层"当场可见。
          2026-10-11 简化：标题从「LLM model」改成「当前生效」—— 原标题和下面那张
          「模型池」的关系说不清，用户会以为这里也能改。这是只读面板。 */}
      <Card className="max-w-2xl space-y-3 p-4">
        <CardTitle
          title={t("Currently in use (agent)")}
          hint={t("The gateway the browser agent actually calls. Edit it in the model pool below — there is exactly one place to set it, so what you save is what runs.")}
          right={
            /* 来源要按**优先级**说，不能只看池：高端模型或某个用途的覆盖一旦接管 agent，
               实际模型就不是池第 1 套了 —— 那时还说"来自模型池"就成了新的假信息
               （这正是这一版要消灭的那类错觉）。 */
            llm?.roles?.agent?.source === "premium"
              ? t("from the premium model")
              : llm?.roles?.agent?.source === "override"
                ? t("from the execution row in “Model per purpose”")
                : llm?.pool_source === "env"
                  ? t("from .env — no pool saved yet")
                  : t("from the pool ({{n}} set(s))", { n: llm?.pool_count ?? 1 })
          }
        />

        <div className="rounded-lg bg-[var(--panel2)] px-3 py-2 text-xs text-ink-700">
          <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
            <span className="text-ink-500">{t("Endpoint")}</span>
            <span className="break-all font-mono">{llm?.base_url || t("(not set)")}</span>
            <span className="text-ink-500">{t("Agent model")}</span>
            <span className="break-all font-mono">
              {llm?.agent_model || llm?.model || t("(not set)")}
              {!llm?.agent_model && llm?.model ? t(" (inherited)") : ""}
            </span>
            <span className="text-ink-500">{t("API key")}</span>
            <span className="font-mono">
              {llm?.api_key_set ? t("set") : t("not set — calls will likely be rejected")}
            </span>
            <span className="text-ink-500">{t("Protocol")}</span>
            <span className="font-mono">
              {llm?.provider || "auto"}
              {llm?.provider === "auto" ? t(" (detected by probe)") : ""}
            </span>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button variant="ghost" onClick={testLlm} disabled={testingLlm}>
            {testingLlm ? t("Testing all models…") : t("Test all models")}
          </Button>
          <span className="inline-flex items-center text-xs text-ink-500">
            {t("Tests every model the platform may call — all pool entries and any per-purpose override — and lists each.")}
          </span>
        </div>
        {llmTest && (
          <div className="space-y-2">
            <div
              className={`rounded-lg px-3 py-2 text-xs ${
                llmTest.all_ok
                  ? "bg-[var(--ok-bg,#e8f7ee)] text-[var(--ok-fg,#1a7f4b)]"
                  : "bg-[var(--bad-bg,#fee)] text-[var(--bad-fg)]"
              }`}
            >
              {llmTest.all_ok
                ? t("All {{n}} model(s) connected.", { n: llmTest.results.length })
                : t("{{ok}} of {{n}} model(s) connected — the rest failed.", {
                    ok: llmTest.results.filter((r) => r.ok).length,
                    n: llmTest.results.length,
                  })}
            </div>
            <div className="space-y-1.5">
              {llmTest.results.map((r, i) => (
                <div
                  key={`${r.name}-${i}`}
                  className="rounded-lg border border-[var(--line)] px-3 py-2 text-[11px]"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono font-medium text-ink-900">{r.name}</span>
                    <span className="text-ink-500">{r.model}</span>
                    <span className={r.ok ? "text-[var(--ok-fg,#1a7f4b)]" : "text-[var(--bad-fg)]"}>
                      {r.ok ? "✓" : "✕"}
                    </span>
                  </div>
                  {r.ok ? (
                    // 实测能力：换厂商后"能不能跑"就看这几个值，不用去翻日志找 400。
                    // json_schema=false 是正常情况（如 DeepSeek）—— 代理会自动改走
                    // system prompt 里的 schema，不需要用户做任何事。
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-ink-500">
                      <span>{t("Protocol")}: {r.protocol}</span>
                      <span>{t("JSON object")}: {mark(!!r.json_object)}</span>
                      <span>{t("JSON schema")}: {mark(!!r.json_schema)}</span>
                      <span>{t("Tools")}: {mark(!!r.tools)}</span>
                      <span>{t("Vision")}: {mark(!!r.vision)}</span>
                    </div>
                  ) : (
                    <div className="mt-1 break-all text-[var(--bad-fg)]">{r.error}</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      {/* 池：多套网关按顺序兜底。**这里是唯一的网关配置入口** ——
          2026-10-11 起把原来那张"基层模型"卡的输入框合并到这里了：那张卡的字段在池非空时
          完全不生效（load_pool 会整体取代它们），留着只会让人改了模型发现没变。
          顺序即优先级，429（配额耗尽）自动换下一套。
          2026-10-11 简化：默认只摊开第 1 套（真正在跑的那一套），第 2 套以后收进折叠区 ——
          只有一套网关时那部分纯属噪音。 */}
      <Card className="max-w-2xl space-y-3 p-4">
        <CardTitle
          title={t("Model pool (fallback order)")}
          hint={t("Several gateways tried in order: index 1 is preferred and is what the agent uses by default, the next one takes over when a key is rate-limited (429) or misconfigured. Leave an API key blank to keep the stored one.")}
          right={
            pool && pool.length > 1
              ? t("{{n}} sets configured.", { n: pool.length })
              : t("One entry = no fallback: this set is used as-is.")
          }
        />

        {poolSeeded && (
          <div className="tp-alert tp-alert-warn text-[11px]">
            {t("No pool saved yet — the row below is only a read-back of .env. Press Save to make it the real configuration; until then .env stays in charge.")}
          </div>
        )}

        {pool === null ? (
          <div className="text-xs text-ink-500">{t("Loading…")}</div>
        ) : (
          <div className="space-y-2">
            {pool.length > 0 && poolEntry(pool[0], 0)}

            {pool.length > 1 ? (
              <Fold
                open={showExtraGateways}
                onToggle={() => setShowExtraGateways((v) => !v)}
                title={t("Fallback gateways")}
                hint={t("Only set 1 runs by default — the extras are tried in order when set 1 is rate-limited (429) or misconfigured. Keep them folded if you run a single gateway.")}
                summary={t("{{n}} extra gateway(s) configured for fallback.", { n: pool.length - 1 })}
              >
                {pool.map((row, i) => (i > 0 ? poolEntry(row, i) : null))}
                <Button
                  variant="ghost"
                  onClick={() =>
                    setPool([
                      ...pool,
                      {
                        name: `llm${pool.length + 1}`,
                        base_url: "",
                        model: "",
                        agent_model: "",
                        provider: "auto",
                        api_key_set: false,
                        api_key: "",
                      },
                    ])
                  }
                >
                  {t("Add a gateway")}
                </Button>
              </Fold>
            ) : (
              <div>
                <Button
                  variant="ghost"
                  onClick={() => {
                    // 点了就直接展开 —— 加完还收着的话，用户会以为没加上。
                    setPool([
                      ...pool,
                      {
                        name: `llm${pool.length + 1}`,
                        base_url: "",
                        model: "",
                        agent_model: "",
                        provider: "auto",
                        api_key_set: false,
                        api_key: "",
                      },
                    ]);
                    setShowExtraGateways(true);
                  }}
                >
                  {t("Add a gateway")}
                </Button>
              </div>
            )}

            <div className="flex items-center gap-3">
              <Button onClick={savePool} disabled={savingPool}>
                {savingPool ? t("Saving…") : t("Save pool")}
              </Button>
            </div>
          </div>
        )}
      </Card>

      {/* 「模型分配」一张卡（2026-10-12 三次简化）。
          2026-10-11 把「高端模型」与「按用途分模型」并成一张卡消掉"二选一"的暗示；
          2026-10-12 用户要求"只留一个"——删掉「逐环节指定模型」子卡，只保留
          「高端模型接管」这一支粗粒度机制。每个用途勾选项右侧显示该角色占全部 token
          的百分比（真实实测优先，无数据回退结构估算并标 ≈），让用户一眼看清"勾哪个最烧钱"。 */}
      <Card className="max-w-2xl space-y-4 p-4">
        <CardTitle
          title={t("Model assignment")}
          hint={t("One switch decides quality vs cost: turn it on and the steps you tick use the premium model; leave it off and every step falls back to the pool's set 1 — no extra spend at all.")}
          right={t("Now: every step uses {{model}}", { model: llm?.agent_model || llm?.model || "—" })}
        />

        {/* 已是「单个机制」，不再需要"不是二选一"的兜底说明。一句话讲清语义即可。 */}
        <div className="rounded-lg bg-[var(--panel2)] px-3 py-2 text-[11px] text-ink-600">
          {t("This is the only model-assignment control. Tick a step to give it the premium model; everything you leave unticked keeps using the cheap pool.")}
        </div>

        <div className="space-y-4">
          {/* ── 高端模型接管（粗粒度）：一支贵模型接管若干环节 ── */}
          <div>
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <div className="text-[13px] font-medium text-ink-900">
                {t("Give some steps a better model")}
                <Hint text={t("Pick one expensive, smart model from the pool below and tick exactly which steps may use it. Off means every step falls back to the pool's set 1 — no extra spend at all.")} />
              </div>
              <div className="text-[11px] text-ink-500">
                {llm?.premium?.usable
                  ? t("In effect: {{roles}}", { roles: premiumLiveRoles || "—" })
                  : t("Off — nothing takes over.")}
              </div>
            </div>

        <label className="mt-2 flex cursor-pointer items-center gap-2">
          <Checkbox
            checked={premium.enabled}
            onChange={(e) => {
              const on = e.target.checked;
              setPremium({
                ...premium,
                enabled: on,
                // 第一次打开时预勾"量小、质量关键"的那几个 —— 而不是让用户对着六个空勾选框发呆。
                // 刻意不含 agent 与 judge：前者是省钱主旋钮，后者初判就该留给便宜模型。
                roles:
                  on && premium.roles.length === 0 ? [...PREMIUM_DEFAULT_ROLES] : premium.roles,
              });
            }}
          />
          <span className="text-[13px] font-medium text-ink-900">{t("Use a premium model")}</span>
        </label>

        {premium.enabled && (
          <>
            {/* 高端模型 = 从模型池里选一套（连接信息只在池里填） */}
            <div className="flex flex-wrap items-center gap-2">
              <Select
                value={premium.pool_name}
                onChange={(e) => setPremium({ ...premium, pool_name: e.target.value })}
                className="h-9 w-72"
              >
                <option value="">{t("— pick a model from the pool —")}</option>
                {(pool || []).map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name} · {p.model}
                  </option>
                ))}
              </Select>
              {premium.pool_name && (
                <span className="text-[11px] text-ink-500">
                  {premiumResolvedModel
                    ? `${t("Premium model")}: ${premiumResolvedModel}`
                    : t("(not found in pool)")}
                </span>
              )}
            </div>
            <div className="text-[11px] text-ink-400">
              {t("The premium model is one of the pool entries below — its gateway, key and protocol are configured there, not here.")}
            </div>

            {!premiumResolved && premium.pool_name && (
              <div className="tp-alert tp-alert-warn text-[11px]">
                {t("That pool entry no longer exists — pick another one, or the premium model will not take effect.")}
              </div>
            )}

            {/* 建议：哪些环节该上高端模型（质量敏感、量小；agent 量大不推荐） */}
            <PremiumSuggestion
              poolEmpty={!pool || pool.length === 0}
              suggested={PREMIUM_ROLES.filter((r) => r !== "agent")}
              selected={premium.roles}
              tokenShare={tokenShare}
              onApply={() =>
                setPremium((p) => ({
                  ...p,
                  enabled: true,
                  roles: PREMIUM_ROLES.filter((r) => r !== "agent"),
                }))
              }
            />

            <div className="space-y-1.5 rounded-lg border border-[var(--line)] p-3">
              <div className="text-[13px] font-medium text-ink-900">
                {t("Which steps may use it")}
                <Hint text={t("Judge escalation is only called when the cheap judge says failed or a gate warns — it can be the priciest model, because a routine pass never calls it.")} />
              </div>
              {/* 每个用途一行：勾选框 + 名称 + 该角色占全部 token 的百分比。
                  右侧百分比优先用真实实测（tokenShare.measured），无数据则回退结构估算并标 ≈。 */}
              <div className="space-y-1">
                {PREMIUM_ROLES.map((r) => {
                  const tp =
                    tokenShare != null
                      ? tokenShare.has_data && tokenShare.measured[r] != null
                        ? { pct: tokenShare.measured[r], real: true }
                        : { pct: tokenShare.estimate[r] ?? 0, real: false }
                      : null;
                  return (
                    <label key={r} className="flex w-full cursor-pointer items-center gap-2">
                      <Checkbox
                        checked={premium.roles.includes(r)}
                        onChange={(e) =>
                          setPremium({
                            ...premium,
                            roles: e.target.checked
                              ? [...premium.roles, r]
                              : premium.roles.filter((x) => x !== r),
                          })
                        }
                      />
                      <span className="text-[12px] text-ink-700">{t(ROLE_LABEL[r])}</span>
                      {tp && (
                        <span
                          className={
                            tp.real
                              ? "ml-auto text-[11px] tabular-nums text-ink-500"
                              : "ml-auto text-[11px] tabular-nums text-ink-400"
                          }
                          title={
                            tp.real
                              ? t("Based on measured runs")
                              : t("No measured data for this step yet — structural estimate")
                          }
                        >
                          {tp.real ? `${tp.pct}%` : `≈${tp.pct}%`}
                        </span>
                      )}
                    </label>
                  );
                })}
              </div>
              {tokenShare && !tokenShare.has_data && (
                <div className="text-[11px] text-ink-400">
                  {t("No run data yet — the ratios above are structural estimates (agent dominates). They become measured once you run cases after this update.")}
                </div>
              )}
              {tokenShare && tokenShare.has_data && (
                <div className="text-[11px] text-ink-400">
                  {t("Token share from {{n}} measured run(s).", { n: tokenShare.runs })}
                </div>
              )}
            </div>

            {premiumOnAgent && (
              <div className="tp-alert tp-alert-warn text-[11px]">
                {t("The execution step is about 95% of all tokens. Sending it to the premium model removes the main cost lever — the other steps are small-volume, so that is where the money actually goes.")}
              </div>
            )}
            {!premiumReady && (
              <div className="tp-alert tp-alert-warn text-[11px]">
                {t("Not in effect yet: pick a model from the pool and tick at least one step.")}
              </div>
            )}
          </>
        )}
          </div>

          {/* ── 第 2 步（逐环节指定模型）已于 2026-10-12 删除：用户要求"只留一个" ── */}

        </div>

        {/* 整张卡只有一个保存按钮 */}
        <div className="flex flex-wrap items-center gap-3 border-t border-[var(--line)] pt-3">
          <Button onClick={saveModels} disabled={savingModels}>
            {savingModels ? t("Saving…") : t("Save model assignment")}
          </Button>
          <span className="text-[11px] text-ink-500">
            {t("One save submits the premium-model assignment.")}
          </span>
        </div>
      </Card>

      {gitlabEnabled && (
      <Card className="max-w-2xl space-y-3 p-4">
        <div>
          <div className="text-sm font-medium text-ink-900">{t("Global GitLab token")}</div>
          <div className="text-xs text-ink-500">
            {t("An api-scope token used for GitLab sync across all projects. Projects then only pick a GitLab project.")}
          </div>
        </div>
        <Field label={tokenSet ? t("Access token (set — leave blank to keep)") : t("Access token (api scope)")}>
          <Input type="password" value={token} onChange={(e) => setToken(e.target.value)}
            placeholder={tokenSet ? "••••••••" : "glpat-…"} />
        </Field>
        <div className="flex items-center gap-3">
          <Button onClick={save} disabled={saving}>{saving ? t("Saving…") : t("Save")}</Button>
          <span className="text-xs text-ink-500">
            {tokenSet ? t("A global token is set.") : t("No global token set.")}
          </span>
          {tokenSet && (
            <button className="text-xs text-[var(--bad-fg)] hover:underline"
              onClick={() => api.setGitlabToken("").then(() => setTokenSet(false)).catch((e) => fail(e))}>
              {t("Clear")}
            </button>
          )}
        </div>
        <p className="text-xs text-ink-500">{t("Stored encrypted; never returned. Enter it here — not in code.")}</p>
      </Card>
      )}

      {feishuEnabled && (
      <Card className="max-w-2xl space-y-3 p-4">
        <div>
          <div className="text-sm font-medium text-ink-900">{t("Feishu bot")}</div>
          <div className="text-xs text-ink-500">
            {t("Collect problems from a Feishu chat into Feedback / Issues. Configure the self-built app here.")}
          </div>
        </div>

        <div className="rounded-lg bg-[var(--panel2)] px-3 py-2 text-xs text-ink-700">
          <div>{t("Long-connection mode needs no callback URL — just the app credentials below.")}</div>
          <div className="mt-1 text-ink-500">
            {t("(Webhook mode only) Event subscription URL:")}{" "}
            <code className="break-all font-mono text-brand-700">{webhookUrl}</code>
          </div>
        </div>

        <Field label={t("App ID")}>
          <Input value={appId} onChange={(e) => setAppId(e.target.value)} placeholder="cli_xxx" />
        </Field>
        <Field label={fs?.app_secret_set ? t("App Secret (set — leave blank to keep)") : t("App Secret")}>
          <Input type="password" value={appSecret} onChange={(e) => setAppSecret(e.target.value)}
            placeholder={fs?.app_secret_set ? "••••••••" : ""} />
        </Field>
        <Field label={fs?.verification_token_set ? t("Verification Token (set — leave blank to keep)") : t("Verification Token")}>
          <Input type="password" value={verifToken} onChange={(e) => setVerifToken(e.target.value)}
            placeholder={fs?.verification_token_set ? "••••••••" : ""} />
        </Field>
        <Field label={t("API base")}>
          <Input value={apiBase} onChange={(e) => setApiBase(e.target.value)}
            placeholder="https://open.feishu.cn" />
        </Field>
        <label className="flex items-center gap-2 text-sm text-ink-700">
          <Checkbox checked={autoAnswer} onChange={(e) => setAutoAnswer(e.target.checked)} />
          {t("Auto-answer context-detected questions (not just @-mentions)")}
        </label>

        <div className="flex items-center gap-3">
          <Button onClick={saveFeishu} disabled={savingFs}>{savingFs ? t("Saving…") : t("Save")}</Button>
          <span className="text-xs text-ink-500">
            {fs?.app_secret_set ? t("Bot credentials are set.") : t("Bot not configured yet.")}
          </span>
        </div>
        <p className="text-xs text-ink-500">
          {t("Secrets stored encrypted; never returned. Leave the Encrypt Key blank in the Feishu console.")}
        </p>
      </Card>
      )}
      <AppUpdateCard />
      <StorageCard />
    </div>
  );
}
