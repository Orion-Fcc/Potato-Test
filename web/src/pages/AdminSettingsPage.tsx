import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ChevronRight } from "lucide-react";
import { api, type FeishuStatus, type LlmPoolEntry, type LlmPremiumStatus, type LlmRoleStatus, type LlmStatus, type LlmTestResult } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Card, Checkbox, Field, Input, Select } from "../components/ui";
import { useToast, useErrorToast } from "../components/toast";

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
// 用途行：服务端回来的状态 + 本地编辑用的钥匙输入框（只写，留空=保留已存）。
type RoleRow = LlmRoleStatus & { api_key?: string };

// 用途清单与后端 app/llm.py::ROLES 对齐。顺序按"花费从大到小"排：
// 真库实测 agent 每步调用占全部 token 的约 95%，所以"执行"这一行是省钱的唯一大旋钮，
// 判定/生成量小而质量敏感，值得留强模型。
// judge_escalate 是复核层：只有初判 failed 或确定性闸门报警时才调用，
// 所以它可以放比 judge 更贵的模型 —— 没触发就一分钱不花。
const ROLES = ["agent", "judge", "judge_escalate", "narrative", "specgen", "assistant"] as const;
const ROLE_LABEL: Record<string, string> = {
  agent: "Execution (agent, every step)",
  judge: "Judging (once per case)",
  judge_escalate: "Judge escalation (review)",
  narrative: "Failure narrative / case memory",
  specgen: "Case generation from spec",
  assistant: "Assistant chat & Feishu",
};
const ROLE_HINT: Record<string, string> = {
  agent: "~95% of all tokens — the only lever that really cuts cost",
  judge: "small volume, quality-critical — keep the strong model here",
  judge_escalate: "only called when the cheap judge says failed or a gate warns — can be the priciest model",
  narrative: "runs only on failures, short output",
  specgen: "long-document batches, quality matters",
  assistant: "interactive, latency-sensitive",
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
function emptyRole(): RoleRow {
  return {
    model: "",
    base_url: "",
    provider: "",
    api_key_set: false,
    effective_model: "",
    effective_base_url: "",
  };
}

// 平台级「高端模型」可以挂到哪些用途上（与服务端 app/llm.py::PREMIUM_ROLES 对齐）。
//
// agent 排在最后并且**默认不勾**：它占全部 token 的约 95%，把执行步换成贵模型
// 等于把省钱的唯一大旋钮关掉。放在这里是因为"我就想要最高质量"也该被允许，
// 但要勾就得先看到那句警告 —— 不能让人拨个总开关就悄悄多花一大笔。
const PREMIUM_ROLES = [
  "judge_escalate",
  "judge",
  "specgen",
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
    model: "",
    base_url: "",
    provider: "",
    api_key_set: false,
    roles: [],
    usable: false,
    applies_to: [],
  };
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
  const [llmTest, setLlmTest] = useState<{ ok: boolean; text: string; result?: LlmTestResult } | null>(
    null,
  );
  const [loadError, setLoadError] = useState("");

  // LLM 池（多套网关，按顺序 429 兜底）—— 这是**唯一**的网关配置入口。
  const [pool, setPool] = useState<PoolRow[] | null>(null);
  // 池为空时后端会回一条从 .env 读出来的回显条目（seeded=true）。界面要如实说明
  // "这条还没进池"，否则用户会以为已经保存过了。
  const [poolSeeded, setPoolSeeded] = useState(false);
  const [savingPool, setSavingPool] = useState(false);

  // 按用途分模型（agent 每步执行 / 判定 / 失败叙述 / 需规生成 / 助手对话）。
  // 空字符串 = 跟随默认配置；界面显示 effective_* 让"这个用途实际用哪个模型"一目了然。
  const [roles, setRoles] = useState<Record<string, RoleRow>>({});
  // 两级判定（2026-10-11）：便宜模型先判，只有判 failed 或确定性闸门报警时才叫复核层。
  // 默认关 —— 它确实会多花 token，必须由人显式打开。
  const [twoTier, setTwoTier] = useState(false);

  // 平台级「高端模型」（贵、聪明，可选）。默认关。
  // 有意的设计：整个平台"要不要为质量多花钱"只在这里回答一次，而不是让用户去六个
  // 用途行里各填一遍。空壳用 emptyPremium()，字段与后端 premium_for_ui() 对齐。
  const [premium, setPremium] = useState<LlmPremiumStatus>(emptyPremium);
  // 钥匙是只写的：本地编辑用的输入框（留空 = 保留已存）
  const [premiumKey, setPremiumKey] = useState("");
  // 「高端模型 / 逐环节覆盖 / 两级判定」共用一个保存按钮和一个 saving 标记 ——
  // 它们是一次 PUT 提交的整体，分成两个 loading 态只会让按钮出现"只转一个"的怪现象。
  const [savingModels, setSavingModels] = useState(false);

  // 折叠态（2026-10-11 简化）。默认视图只留"最常用的两件事"，其余收起来：
  //   showExtraGateways —— 模型池里第 2 套以后的兜底网关（只有一套网关时纯噪音）
  //   showRolesCard     —— 「按用途分模型」整卡（6×4 = 24 个控件，占了页面一半的密度）
  //   roleMore          —— 按用途卡里每一行是否展开"协议/网关/钥匙"
  // 说明：roleMore 抽成"每行一个开关"而不是全卡一个开关，是因为绝大多数人只换模型名、
  // 不换厂商；而"换厂商"是少数行的事，不该让所有行都摊开四个框。
  const [showExtraGateways, setShowExtraGateways] = useState(false);
  const [showRolesCard, setShowRolesCard] = useState(false);
  const [roleMore, setRoleMore] = useState<Record<string, boolean>>({});

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
        setRoles(s.llm.roles || {});
        // 已经单独配了网关/协议/钥匙的用途行，默认把那三个框展开 ——
        // 否则用户看到"这行只有模型名"，会以为自己之前填的网关丢了。
        setRoleMore(
          Object.fromEntries(
            Object.entries(s.llm.roles || {})
              .filter(
                ([, r]) =>
                  !!(r.base_url || "").trim() ||
                  !!r.api_key_set ||
                  (!!r.provider && r.provider !== "auto"),
              )
              .map(([k]) => [k, true]),
          ),
        );
        setTwoTier(!!s.llm.judge_two_tier);
        setPremium(s.llm.premium || emptyPremium());
        // 高端模型挂在「判定复核」上、但两级判定还关着 ⇒ 直接展开高级区。
        // 不这样的话下面那句黄色警告会指着"一个收起来的开关"，用户得先猜它在哪。
        setShowRolesCard(
          !!s.llm.premium?.usable &&
            (s.llm.premium.applies_to || []).includes("judge_escalate") &&
            !s.llm.judge_two_tier,
        );
        setPremiumKey("");
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

  // 保存「模型分配」= 高端模型 + 逐环节覆盖 + 两级判定，**一次 PUT**。
  //
  // 为什么合成一个保存按钮（2026-10-11 二次简化）：原来这三样各有一个保存按钮，
  // 页面上等于摆着三份"模型配置 + 保存"，读起来就是三个互相打架的配置。而它们
  // 其实是同一个问题的两层答案（默认 → 高端接管 → 逐环节指定），本来就该一起
  // 提交、一起生效。后端 PUT 本来就同时接受这三个字段，合并没有任何语义变化。
  //
  // 两块的提交语义不同，这里必须分别构造、不能混用：
  //   roles   —— 只发有值的行，缺席的行等于"清掉该覆盖"（后端整体替换 kept 字典）。
  //   premium —— 整体替换，发上去的就是全部；钥匙留空发 undefined = 保留已存的那把。
  const saveModels = async () => {
    setSavingModels(true);
    try {
      const rolePayload: Record<string, { model?: string; base_url?: string; api_key?: string; provider?: string }> = {};
      for (const r of ROLES) {
        const row = roles[r];
        if (!row) continue;
        const item: { model?: string; base_url?: string; api_key?: string; provider?: string } = {};
        if (row.model.trim()) item.model = row.model.trim();
        if (row.base_url.trim()) item.base_url = row.base_url.trim();
        if (row.api_key?.trim()) item.api_key = row.api_key.trim();
        if (row.provider && row.provider !== "auto") item.provider = row.provider;
        if (Object.keys(item).length) rolePayload[r] = item;
      }
      await api.setLlmSettings({
        roles: rolePayload,
        judge_two_tier: twoTier,
        premium: {
          enabled: premium.enabled,
          model: premium.model.trim(),
          base_url: premium.base_url.trim(),
          provider: premium.provider || "auto",
          roles: [...premium.roles],
          api_key: premiumKey.trim() || undefined,
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
      setLlmTest(
        r.ok
          ? {
              ok: true,
              text: `${t("Connected.")} ${t("Model")}: ${r.reply_model || r.model}`,
              result: r,
            }
          : { ok: false, text: r.error || t("Connection failed.") },
      );
    } catch (e: any) {
      setLlmTest({ ok: false, text: e?.message || String(e) });
    } finally {
      setTestingLlm(false);
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

  // 判定层/复核层"实际生效"的模型名（没配就是默认池的）。用来当场判断两级判定
  // 是不是白开 —— 两个角色指向同一支模型时后端会直接跳过复核调用。
  const judgeEff = roles.judge?.effective_model || llm?.model || "";
  const escalateEff = roles.judge_escalate?.effective_model || llm?.model || "";

  // 高端模型的表单态判断（不看服务端回值，否则"改了还没保存"时提示是错的）。
  const premiumReady = premium.enabled && !!premium.model.trim() && premium.roles.length > 0;
  const premiumOnAgent = premium.enabled && premium.roles.includes("agent");
  // 勾了高端模型、但同一个用途又在下面那张卡里单独配了模型 ⇒ 按用途的那张赢。
  // 不说清楚的话，用户会觉得高端模型"配了没生效"。
  const premiumShadowed = premium.enabled
    ? premium.roles.filter((r) => !!(roles[r]?.model || "").trim())
    : [];
  // 已保存且真的在生效的用途（用服务端回值，避免把未保存的编辑当成现状）
  const premiumLiveRoles = (llm?.premium?.applies_to || [])
    .map((r) => `${t(ROLE_LABEL[r] || r)}`)
    .join(" / ");
  // 高端模型（已保存且可用）是否被挂到了「判定复核」上。
  // 判定复核**只**由两级判定调用，所以开关关着时它一次都不会被叫 —— 属于
  // "看起来配了其实没用"，必须当场说破，否则用户会以为质量已经提上去了。
  const premiumConfiguredForReview =
    !!llm?.premium?.usable && (llm.premium.applies_to || []).includes("judge_escalate");

  // 折叠状态下"你错过了什么"要如实报出来：有几个用途被单独指定了模型。
  // 折叠+不说 = 藏东西，那是另一个问题，不是简化。
  const rolesConfigured = ROLES.filter((r) => !!(roles[r]?.model || "").trim()).length;

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
            {testingLlm ? t("Testing…") : t("Test connection")}
          </Button>
          <span className="inline-flex items-center text-xs text-ink-500">
            {t("Tests the set the agent actually calls — and tells you which one that was.")}
            <Hint text={t("The judge model needs vision (screenshots are evidence). Values here override .env; key stored encrypted.")} />
          </span>
        </div>
        {llmTest && (
          <div
            className={`rounded-lg px-3 py-2 text-xs ${
              llmTest.ok
                ? "bg-[var(--ok-bg,#e8f7ee)] text-[var(--ok-fg,#1a7f4b)]"
                : "bg-[var(--bad-bg,#fee)] text-[var(--bad-fg)]"
            }`}
          >
            {llmTest.ok ? "✓ " : "✕ "}
            <span className="break-all">{llmTest.text}</span>
            {/* 实测能力：换厂商后"能不能跑"就看这几个值，不用去翻日志找 400。
                json_schema=false 是正常情况（如 DeepSeek）—— 代理会自动改走
                system prompt 里的 schema，不需要用户做任何事。 */}
            {llmTest.result?.protocol && (
              <div className="mt-2 space-y-1 border-t border-[var(--line)] pt-2 text-[11px]">
                <div className="font-mono">
                  {t("Detected protocol")}: {llmTest.result.protocol}
                  {llmTest.result.probed === false ? t(" (assumed, probe failed)") : ""}
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono">
                  <span>{t("JSON object mode")}: {mark(!!llmTest.result.json_object)}</span>
                  <span>{t("Strict JSON schema")}: {mark(!!llmTest.result.json_schema)}</span>
                  <span>{t("Tool calling")}: {mark(!!llmTest.result.tools)}</span>
                  <span>{t("Image input")}: {mark(!!llmTest.result.vision)}</span>
                </div>
                {llmTest.result.json_schema === false && (
                  <div>
                    {t("This gateway rejects strict JSON schema — the browser agent automatically keeps the schema in the system prompt instead. Nothing to fix.")}
                  </div>
                )}
              </div>
            )}
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

      {/* 「模型分配」一张卡（2026-10-11 二次简化）。
          用户反馈：「高端模型」和「按用途分模型」这两块**看上去像在冲突**。

          查下来，冲突感来自三个结构信号，跟语义无关：
            ① 两张同级卡片、各带一套输入框和**各自的保存按钮** —— 读起来就是几份并列的
               模型配置（算上模型池那张，全页三个"保存"按钮）；
            ② 两张卡右侧各挂一句几乎同义的现状（"未生效 —— 都用池第 1 套" /
               "没有任何环节被单独指定 —— 全部用池第 1 套"），像两个对等的开关；
            ③ 没有任何视觉线索说明"谁盖过谁"，于是只能猜。

          而语义上它们从来不冲突：是**同一个问题的两级答案**（默认 → 高端接管 →
          逐环节指定，越往下越具体）。所以这里并成一张卡、编上号、连成一条竖线，
          并且**只留一个保存按钮** —— 一次 PUT 提交全部（后端本来就同时接受这三个
          字段，合并没有任何语义变化），让"它们是一件事"从结构上就成立，而不是靠
          再写一句"其实不冲突"去解释。 */}
      <Card className="max-w-2xl space-y-4 p-4">
        <CardTitle
          title={t("Model assignment")}
          hint={t("These two are not alternatives — they are two levels of the same answer, and the more specific one wins: step 1 gives some steps a better model, step 2 names one model per step. Whatever step 2 leaves blank still follows the pool's set 1.")}
          right={t("Now: every step uses {{model}}", { model: llm?.agent_model || llm?.model || "—" })}
        />

        {/* 把"不是二选一"讲在明面上，就放在两块的正上方 —— 结构性消除之外，再给一句话兜底 */}
        <div className="rounded-lg bg-[var(--panel2)] px-3 py-2 text-[11px] text-ink-600">
          {t("Not either-or: step 1 is coarse, step 2 is fine-grained, and step 2 overrides step 1 for the steps it names. Leave step 2 empty and step 1 simply applies to everything it ticks.")}
        </div>

        {/* 竖向连线：把两步在视觉上串成一条"从粗到细"的阶梯。
            线画在徽标正下方（left-[9px]、徽标 20px 宽居中在 10px），徽标有实底盖住它。 */}
        <div className="relative space-y-4">
          <span aria-hidden className="absolute bottom-4 left-[9px] top-3 w-px bg-[var(--line)]" />

          {/* ── 第 1 步：粗粒度 —— 一支贵模型接管若干环节 ── */}
          <div className="relative pl-7">
            <span className="absolute left-0 top-0 grid h-5 w-5 place-items-center rounded-full bg-brand-600 text-[11px] font-semibold leading-none text-[var(--on-brand)]">
              1
            </span>
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <div className="text-[13px] font-medium text-ink-900">
                {t("Give some steps a better model")}
                <Hint text={t("Add one expensive, smart model and tick exactly which steps may use it. Off means every step falls back to the pool's set 1 — no extra spend at all.")} />
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
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={premium.model}
                onChange={(e) => setPremium({ ...premium, model: e.target.value })}
                placeholder={t("model name (required)")}
                className="h-9 w-56"
              />
              <Select
                value={premium.provider || "auto"}
                onChange={(e) => setPremium({ ...premium, provider: e.target.value })}
                className="h-9 w-44"
              >
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {t(PROVIDER_LABEL[p])}
                  </option>
                ))}
              </Select>
              <Input
                value={premium.base_url}
                onChange={(e) => setPremium({ ...premium, base_url: e.target.value })}
                placeholder={t("gateway URL (blank = pool's set 1)")}
                className="h-9 w-64"
              />
              <Input
                type="password"
                value={premiumKey}
                onChange={(e) => setPremiumKey(e.target.value)}
                placeholder={premium.api_key_set ? t("•••••••• (kept)") : t("api key (blank = keep stored)")}
                className="h-9 w-48"
              />
            </div>
            <div className="text-[11px] text-ink-400">
              {t("Gateway and key are optional: leave the gateway blank to reuse the pool's set 1, leave the key blank to keep the stored one.")}
            </div>

            <div className="space-y-1.5 rounded-lg border border-[var(--line)] p-3">
              <div className="text-[13px] font-medium text-ink-900">
                {t("Which steps may use it")}
                <Hint text={t("Judge escalation is only called when the cheap judge says failed or a gate warns — it can be the priciest model, because a routine pass never calls it.")} />
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-2">
                {PREMIUM_ROLES.map((r) => (
                  <label key={r} className="flex cursor-pointer items-center gap-2">
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
                  </label>
                ))}
              </div>
            </div>

            {premiumOnAgent && (
              <div className="tp-alert tp-alert-warn text-[11px]">
                {t("The execution step is about 95% of all tokens. Sending it to the premium model removes the main cost lever — the other steps are small-volume, so that is where the money actually goes.")}
              </div>
            )}
            {premiumConfiguredForReview && !twoTier && (
              <div className="tp-alert tp-alert-warn text-[11px]">
                {t("You assigned the premium model to judging review, but two-tier judging is off — so it will never be called. Turn that switch on for it to actually do something.")}
              </div>
            )}
            {premiumShadowed.length > 0 && (
              <div className="tp-alert tp-alert-warn text-[11px]">
                {t("These steps also have their own model in step 2 below, and that one wins: {{roles}}. Clear those rows if you want step 1 to take over.", {
                  roles: premiumShadowed.map((r) => t(ROLE_LABEL[r] || r)).join(" / "),
                })}
              </div>
            )}
            {!premiumReady && (
              <div className="tp-alert tp-alert-warn text-[11px]">
                {t("Not in effect yet: fill in a model name and tick at least one step.")}
              </div>
            )}
          </>
        )}
          </div>

          {/* ── 第 2 步：细粒度 —— 逐个环节指定 ──
              这一层**更具体**（单独指名某个环节），所以它盖过第 1 步。
              保留折叠：6 行 × 4 控件占了半页密度，而大多数人只走第 1 步。 */}
          <div className="relative pl-7">
            <span className="absolute left-0 top-0 grid h-5 w-5 place-items-center rounded-full border border-[var(--line-strong)] bg-[var(--panel)] text-[11px] font-semibold leading-none text-ink-500">
              2
            </span>
            <Fold
              open={showRolesCard}
              onToggle={() => setShowRolesCard((v) => !v)}
              title={t("Name one model per step")}
              badge={t("advanced")}
              hint={
                t("Cheap model for the high-volume execution steps, strong model where quality matters. Blank = follow the default (= set 1 of the pool above).") +
                " " +
                t("A value here also wins over step 1 above — this is the most specific layer.")
              }
              summary={
                rolesConfigured > 0
                  ? t("{{n}} step(s) have their own model.", { n: rolesConfigured })
                  : t("All following the default.")
              }
            >
          {/* 优先级说明。两个地方都能表达"执行用哪个模型"（池条目的 Agent model 与
              这里的「执行」行），不说清楚就会出现"我改了池里的却没生效"。 */}
          <div className="tp-alert tp-alert-warn text-[11px]">
            {t("Priority: a value here wins over the pool's Agent model. Leave the execution row blank unless you want a different model than the pool set uses.")}
          </div>

          {/* 两级判定总开关。默认关：它确实会多花 token，得由人拍板。
              右上角直接算出"实际生效的两个模型"是否同一支 —— 同一个模型时后端会跳过
              复核调用（不做重复劳动），但那也就等于没开，必须当场说清楚，
              否则用户会以为开了复核却什么都没发生。 */}
          <div className="space-y-1.5 rounded-lg border border-[var(--line)] p-3">
            <label className="flex cursor-pointer items-center gap-2">
              <Checkbox checked={twoTier} onChange={(e) => setTwoTier(e.target.checked)} />
              <span className="text-[13px] font-medium text-ink-900">
                {t("Two-tier judging (cheap judge first, escalate on failure)")}
              </span>
            </label>
            <div className="text-[11px] text-ink-400">
              {t("The cheap judge runs first. Only a failed verdict or a deterministic gate warning spends the escalation model — routine passes never call it. Takes effect after saving.")}
            </div>
            {twoTier && judgeEff && judgeEff === escalateEff && (
              <div className="tp-alert tp-alert-warn text-[11px]">
                {t("Judging and escalation resolve to the same model ({{model}}), so escalation is skipped. Give the escalation row a different model to get real review.", { model: judgeEff })}
              </div>
            )}
          </div>

          <div className="space-y-3">
            {ROLES.map((r) => {
              const row = roles[r];
              // 每行默认只摊开「模型」一个框：绝大多数情况就是"换个模型名"，
              // 协议/网关/钥匙是"换到另一家厂商"才需要的，少数行的事。
              // 已经存过网关/钥匙的行在 load() 里被预置为展开，不会被藏起来。
              const more = !!roleMore[r];
              return (
                <div key={r} className="space-y-1.5 rounded-lg border border-[var(--line)] p-3">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                    <div className="text-[13px] font-medium text-ink-900">{t(ROLE_LABEL[r])}</div>
                    <div className="text-[11px] text-ink-400">
                      {t("now: {{model}}", { model: row?.effective_model || llm?.model || "—" })}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Input
                      value={row?.model ?? ""}
                      onChange={(e) =>
                        setRoles({
                          ...roles,
                          [r]: { ...(row || emptyRole()), model: e.target.value },
                        })
                      }
                      placeholder={t("(inherit default)")}
                      className="h-9 w-56"
                    />
                    <button
                      type="button"
                      onClick={() => setRoleMore({ ...roleMore, [r]: !more })}
                      className="text-[11px] text-brand-700 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-brand-100"
                    >
                      {more ? t("Fewer options") : t("More options")}
                    </button>
                    {!more && <Hint text={t("Only the model name is needed to swap models; the gateway, protocol and key are only for switching to a different provider.")} />}
                  </div>
                  {more && (
                    <div className="flex flex-wrap items-center gap-2">
                      <Select
                        value={row?.provider || "auto"}
                        onChange={(e) =>
                          setRoles({
                            ...roles,
                            [r]: { ...(row || emptyRole()), provider: e.target.value },
                          })
                        }
                        className="h-9 w-44"
                      >
                        {PROVIDERS.map((p) => (
                          <option key={p} value={p}>
                            {t(PROVIDER_LABEL[p])}
                          </option>
                        ))}
                      </Select>
                      <Input
                        value={row?.base_url ?? ""}
                        onChange={(e) =>
                          setRoles({
                            ...roles,
                            [r]: { ...(row || emptyRole()), base_url: e.target.value },
                          })
                        }
                        placeholder={t("gateway URL (optional)")}
                        className="h-9 w-64"
                      />
                      <Input
                        type="password"
                        value={row?.api_key ?? ""}
                        onChange={(e) =>
                          setRoles({
                            ...roles,
                            [r]: { ...(row || emptyRole()), api_key: e.target.value },
                          })
                        }
                        placeholder={row?.api_key_set ? t("•••••••• (kept)") : t("api key (optional)")}
                        className="h-9 w-48"
                      />
                    </div>
                  )}
                  <div className="text-[11px] text-ink-400">{t(ROLE_HINT[r])}</div>
                </div>
              );
            })}
          </div>
            </Fold>
          </div>
        </div>

        {/* 整张卡只有一个保存按钮 —— 这是"它们是一件事"最直接的表达。
            以前是三份配置配三个保存，用户只能靠猜它们的关系。 */}
        <div className="flex flex-wrap items-center gap-3 border-t border-[var(--line)] pt-3">
          <Button onClick={saveModels} disabled={savingModels}>
            {savingModels ? t("Saving…") : t("Save model assignment")}
          </Button>
          <Button
            variant="ghost"
            onClick={() => setRoles({})}
            disabled={savingModels || !Object.keys(roles).length}
          >
            {t("Clear all per-step models")}
          </Button>
          <span className="text-[11px] text-ink-500">
            {t("One save submits steps 1 and 2 together — they are the same setting, applied by priority.")}
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
    </div>
  );
}
