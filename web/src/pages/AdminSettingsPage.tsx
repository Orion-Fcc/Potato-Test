import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight } from "lucide-react";
import { api, type FeishuStatus, type LlmPoolEntry, type LlmRouting, type LlmTestReport, type LlmTestResult } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Card, Checkbox, Field, Input, Select } from "../components/ui";
import { useToast, useErrorToast } from "../components/toast";
import { AppUpdateCard } from "../components/AppUpdateCard";
import { StorageCard } from "../components/StorageCard";

// 协议取值与后端 app/llm_protocol.py 的 normalize_provider 对齐。
// auto = 自动识别（先看地址线索，再依次小请求探测），绝大多数国产网关是 OpenAI 兼容。
// 快捷厂商预设：选厂商 → 自动填 Base URL + 默认模型 → 粘 Key → 保存即可用。
// 这是「默认极简」那一半；高级字段（代理/上下文/能力）收在每张卡的高级设置里。
const VENDORS = [
  { id: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com", model: "deepseek-chat" },
  { id: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  { id: "zhipu", label: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
  { id: "kimi", label: "Kimi (Moonshot)", base_url: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
  { id: "qwen", label: "阿里通义", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  { id: "ollama", label: "本地 Ollama", base_url: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b" },
  { id: "custom", label: "自定义", base_url: "", model: "" },
] as const;

type PoolRow = LlmPoolEntry & { api_key: string };

// 模型名列表缓存：同一个 Base URL 拉过一次，之后直接给下拉选项。
const MODEL_CACHE_KEY = "potato.llm.models";
const modelKey = (baseUrl: string) => (baseUrl || "").trim().toLowerCase().replace(/\/+$/, "");

// 用途清单与中文名完全由后端 roles_for_ui() 驱动（app/llm.py::ROLES / ROLE_LABELS_ZH），
// 前端不再维护硬编码的用途数组 —— 后端加一个用途，这里自动多一行。

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

  // 连通性测试：按钮与结果都放在下面「模型池」卡片的工具栏里。
  const [testingLlm, setTestingLlm] = useState(false);
  const [llmTest, setLlmTest] = useState<LlmTestReport | null>(null);
  const [showLlmTest, setShowLlmTest] = useState(false);
  // 每行「探测」：精测模型池里单一条目（按 name 走后端过滤），结果就地显示在该行。
  const [rowTests, setRowTests] = useState<Record<number, { loading: boolean; result?: LlmTestResult }>>({});
  const [loadError, setLoadError] = useState("");

  // LLM 模型池（多张模型卡片，按顺序 429 兜底）—— 这是**唯一**的模型配置入口。
  const [pool, setPool] = useState<PoolRow[] | null>(null);
  const [savingPool, setSavingPool] = useState(false);
  // 全局路由：默认对话 / 默认 Agent / 自动降级。
  const [routing, setRouting] = useState<LlmRouting>({ chat: "", agent: "", failover: true });
  const [poolDirty, setPoolDirty] = useState(false);

  // 批量管理 & 新增模型（拉取多选）
  const [batchMode, setBatchMode] = useState(false);
  const [batchSelected, setBatchSelected] = useState<Set<string>>(new Set());
  const [advancedOpen, setAdvancedOpen] = useState<Set<number>>(new Set());


  // 每张卡「拉取模型」的结果缓存（按 Base URL 分桶，落 localStorage）：
  // 拉过一次之后，模型名就从手打变成可选列表；刷新页面也不丢。
  const [modelCache, setModelCache] = useState<Record<string, string[]>>(() => {
    try {
      return JSON.parse(window.localStorage.getItem(MODEL_CACHE_KEY) || "{}") || {};
    } catch {
      return {};
    }
  });
  const [pullingRow, setPullingRow] = useState<Set<number>>(new Set());
  const [modelMenu, setModelMenu] = useState<number | null>(null);

  // 按用途覆盖：key 存在 = 勾选；value = 模型池条目的 name（"" = 勾了但还没选）。
  // 未勾选的用途使用池里第一套，不报错、不提示。
  const [roleModels, setRoleModels] = useState<Record<string, string>>({});
  // 用途清单与显示名来自后端（roles_for_ui），前端只负责渲染。
  const [roleCatalog, setRoleCatalog] = useState<{ key: string; label: string }[]>([]);
  // 「模型分配」只用这一个保存按钮和一个 saving 标记。
  const [savingModels, setSavingModels] = useState(false);

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
        // 回显「特定环节覆盖」：优先取逐环节配置；旧版 premium 接管过的环节
        // 也迁移成逐环节下拉框的值 —— 新版保存后 premium 会被关闭，行为不变。
        const nextRoles: Record<string, string> = {};
        for (const [role, r] of Object.entries(s.llm.roles || {})) {
          if (r.source === "override") nextRoles[role] = r.pool_name || r.model || "";
        }
        if (s.llm.premium?.usable) {
          for (const role of s.llm.premium.applies_to || []) {
            if (!(role in nextRoles)) nextRoles[role] = s.llm.premium.pool_name;
          }
        }
        setRoleModels(nextRoles);
        // 顺序即后端 ROLES 的顺序；label 缺失时回退到 key（老后端也能用）。
        setRoleCatalog(
          Object.entries(s.llm.roles || {}).map(([key, r]) => ({
            key,
            label: r.label || key,
          })),
        );
        setRouting({
          chat: s.llm.routing?.chat || "",
          agent: s.llm.routing?.agent || "",
          failover: s.llm.routing?.failover !== false,
        });
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
        // 后端只回用户真实保存过的条目；池为空就是空列表，不再有 .env 假卡片。
        setPool(r.pool.map((p) => ({ ...p, api_key: "" })));
        setPoolDirty(false);
        setBatchSelected(new Set());
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
      // 配置名称是用户自己的标识（模型分配/用途覆盖都按它引用），
      // 不再自动生成 llm1、llm2 这类没有意义的名字。
      const unnamed = pool.find(
        (r) => r.base_url.trim() && r.model.trim() && !r.name.trim(),
      );
      if (unnamed) {
        toast("error", t("Give each model a configuration name."));
        return;
      }
      const rows = pool
        .filter((r) => r.base_url.trim() && r.model.trim())
        .map((r) => ({
          name: r.name.trim(),
          base_url: r.base_url.trim(),
          model: r.model.trim(),
          agent_model: (r.agent_model || "").trim(),
          provider: r.provider || "auto",
          vendor: r.vendor || "",
          proxy_mode: r.proxy_mode || "system",
          proxy_url: (r.proxy_url || "").trim(),
          context_tokens: r.context_tokens || 4096,
          vision: r.vision !== false,
          tools: r.tools !== false,
          enabled: r.enabled !== false,
          price_in: (r.price_in || "").trim(),
          price_out: (r.price_out || "").trim(),
          // 留空 → 后端保持同位置已存的那把钥匙
          api_key: r.api_key.trim() || undefined,
        }));
      if (!rows.length) {
        toast("error", t("Fill in at least one entry (base URL + model)."));
        return;
      }
      // 后端会丢弃"新条目但没有 key"的记录。以前是静默丢弃 → 池里只剩 env 回显，
      // 探测结果看起来像是别的厂商在报错（1.0.8 现场：选 DeepSeek 却报 Gemini）。
      // 这里提前拦住，并点名哪一条，绝不让配置无声消失。
      const missingKey = pool.find(
        (r) => r.base_url.trim() && r.model.trim() && !r.api_key.trim() && !r.api_key_set,
      );
      if (missingKey) {
        toast("error", t("API key is required for {{name}}.", { name: missingKey.name || missingKey.vendor }));
        return;
      }
      await api.setLlmSettings({ pool: rows, routing });
      await loadPool();
      toast("success", t("Saved"));
    } catch (e: any) {
      fail(e);
    } finally {
      setSavingPool(false);
    }
  };

  // 保存「模型分配」：只提交用途覆盖与自动降级开关。
  // ★ routing 原样回传：routing.chat / routing.agent 是加载进来的旧值，界面已不再提供
  //   修改入口，但**不能传空** —— 后端把这两个字段当作全局路由保存，丢了就等于重置用户配置。
  // 用途覆盖按模型池条目 name 提交，后端取该条目的整条凭据（网关/钥匙/协议），
  // 避免"换了模型却沿用另一家网关的钥匙"。旧 premium 槽位同时清空 ——
  // 新版界面不再有这层，留着会让它继续悄悄接管环节。
  const saveModels = async () => {
    setSavingModels(true);
    try {
      const roles: Record<string, { pool_name?: string; model?: string }> = {};
      for (const [role, value] of Object.entries(roleModels)) {
        const v = value.trim();
        if (!v) {
          // 勾了却没选：行内已经标红；保存时明确拦住，绝不静默丢掉这一行。
          toast("error", t("Select a model for this step."));
          return;
        }
        // 池条目 → 用 pool_name 引用整条凭据；旧式覆盖只剩模型名 → 原样保留。
        roles[role] = poolNames.includes(v) ? { pool_name: v } : { model: v };
      }
      await api.setLlmSettings({
        routing,
        roles,
        premium: { enabled: false, pool_name: "", roles: [] },
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
      setShowLlmTest(true);
    } catch (e: any) {
      setLlmTest({
        ok: false,
        all_ok: false,
        results: [{ name: "—", ok: false, error: e?.message || String(e) }],
      });
      setShowLlmTest(true);
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

  const setRow = (i: number, patch: Partial<PoolRow>) => {
    setPool((pool || []).map((r, j) => {
      if (j !== i) return r;
      const next = { ...r, ...patch };
      // 换了 Base URL 就不再沿用旧钥匙/旧掩码：否则保存时后端匹配不到同一条目，
      // 这一行会被静默丢弃（看起来像"保存成功但模型没进池"）。
      if (patch.base_url !== undefined && patch.base_url.trim() !== (r.base_url || "").trim()) {
        next.api_key = "";
        next.api_key_set = false;
      }
      return next;
    }));
    setPoolDirty(true);
  };

  const applyVendor = (i: number, vendorId: string) => {
    const v = VENDORS.find((x) => x.id === vendorId);
    if (!v) return;
    // 自定义厂商保留用户已填的地址/模型；预设厂商才覆盖。
    setRow(i, {
      vendor: v.id,
      ...(v.id === "custom" ? {} : { base_url: v.base_url, model: v.model }),
      provider: "auto",
    });
  };

  const toggleAdvanced = (i: number) =>
    setAdvancedOpen((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });

  const refreshAll = async () => {
    await Promise.all([load(), loadPool()]);
  };

  const runBatch = async (action: "enable" | "disable" | "delete") => {
    const names = [...batchSelected];
    if (!names.length) {
      toast("error", t("Select at least one model."));
      return;
    }
    if (action === "delete" && !window.confirm(t("Delete the selected models from the pool?"))) return;
    try {
      await api.batchActionLlmPool(names, action);
      await refreshAll();
      setBatchSelected(new Set());
      toast("success", t("Done"));
    } catch (e: any) {
      fail(e);
    }
  };

  const poolNames = (pool || []).map((p) => p.name).filter(Boolean);

  // 池条目增删同样要标脏：否则黄色提示不出现，用户会以为已经保存。
  const addEmptyRow = () => {
    const rows = pool || [];
    setPool([
      ...rows,
      {
        name: "",
        base_url: "",
        model: "",
        agent_model: "",
        provider: "auto",
        api_key_set: false,
        api_key: "",
        price_in: "",
        price_out: "",
      },
    ]);
    setPoolDirty(true);
  };

  const removeRow = (i: number) => {
    setPool((pool || []).filter((_r, j) => j !== i));
    setPoolDirty(true);
  };

  const rememberModels = (baseUrl: string, models: string[]) => {
    const key = modelKey(baseUrl);
    if (!key || !models.length) return;
    setModelCache((prev) => {
      const next = { ...prev, [key]: [...new Set([...(prev[key] || []), ...models])].sort() };
      try {
        window.localStorage.setItem(MODEL_CACHE_KEY, JSON.stringify(next));
      } catch {
        /* 存不下不影响本次会话使用 */
      }
      return next;
    });
  };

  // 单卡拉取：用这张卡自己的地址/钥匙/代理。拉完立刻记缓存，模型名变成可选列表。
  const pullRowModels = async (i: number, row: PoolRow) => {
    if (!row.base_url.trim()) {
      toast("error", t("Base URL is required."));
      return;
    }
    setPullingRow((prev) => new Set(prev).add(i));
    try {
      const r = await api.pullLlmModels({
        base_url: row.base_url.trim(),
        api_key: row.api_key.trim() || undefined,
        proxy_mode: row.proxy_mode || "system",
        proxy_url: row.proxy_mode === "custom" ? (row.proxy_url || "").trim() || undefined : undefined,
      });
      rememberModels(row.base_url, r.models);
      if (!row.model.trim() && r.models[0]) setRow(i, { model: r.models[0] });
      toast("success", t("Found {{n}} models.", { n: r.count }));
    } catch (e: any) {
      fail(e);
    } finally {
      setPullingRow((prev) => {
        const next = new Set(prev);
        next.delete(i);
        return next;
      });
    }
  };

  // 可选项 = 厂商默认模型 + 该地址拉取过的模型 + 当前值。未拉取时至少给厂商默认，
  // 拉取过就成了一份可直接点选的清单（输入框同时支持打字过滤）。
  const modelOptions = (row: PoolRow) => {
    const preset = VENDORS.find((v) => v.id === row.vendor)?.model || "";
    const cached = modelCache[modelKey(row.base_url)] || [];
    return [...new Set([preset, ...cached, row.model].filter(Boolean))];
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

  // 下拉框展示：优先显示池条目的模型名。
  const modelOf = (name: string) =>
    (pool || []).find((p) => p.name === name)?.model || name;
  // 一张卡片 = 一次完整配置。固定四行：
  //   ① 厂商预设 + 模型名称  ② Base URL  ③ API Key  ④ 拉取模型
  // 顶部只留用户自定义的「配置名称」和行操作。没有序号，也没有自动生成的 llm1。
  const poolEntry = (row: PoolRow, i: number) => {
    const adv = advancedOpen.has(i);
    const enabled = row.enabled !== false;
    const checked = batchSelected.has(row.name);
    return (
      <div
        key={`row-${i}`}
        className={`space-y-2 rounded-lg border border-[var(--line)] p-2.5 ${enabled ? "" : "opacity-60"}`}
      >
        {/* 卡片头：配置名称（用户自己起名，模型分配/环节覆盖都按它引用）。 */}
        <div className="flex flex-wrap items-center gap-2">
          {batchMode && (
            <Checkbox
              checked={checked}
              disabled={!row.name}
              onChange={(e) =>
                setBatchSelected((prev) => {
                  const next = new Set(prev);
                  if (e.target.checked) next.add(row.name);
                  else next.delete(row.name);
                  return next;
                })
              }
            />
          )}
          <Input
            value={row.name}
            onChange={(e) => setRow(i, { name: e.target.value })}
            placeholder={t("Configuration name (e.g. Daily driver)")}
            className="w-56"
          />
          {!batchMode && (
            <label className="flex items-center gap-1.5 text-[11px] text-ink-600">
              <Checkbox checked={enabled} onChange={(e) => setRow(i, { enabled: e.target.checked })} />
              {t("Enabled")}
            </label>
          )}
          <span className="flex-1" />
          <Button
            variant="ghost"
            className="h-7 text-[11px]"
            onClick={() => testRow(i, row.name)}
            disabled={rowTests[i]?.loading}
            title={t("Probe this entry (uses the saved config)")}
          >
            {rowTests[i]?.loading ? t("Probing…") : t("Probe")}
          </Button>
          <Button variant="ghost" className="h-7 text-[11px]" onClick={() => setRow(i, { enabled: !enabled })}>
            {enabled ? t("Disable") : t("Enable")}
          </Button>
          <Button variant="ghost" className="h-7 text-[11px]" onClick={() => removeRow(i)}>
            {t("Remove")}
          </Button>
        </div>

        {/* 第一行：厂商预设 + 模型名称（可打字过滤，也可展开已拉取的列表）。 */}
        <div className="grid grid-cols-2 gap-2">
          <Select
            value={row.vendor || "custom"}
            onChange={(e) => applyVendor(i, e.target.value)}
            aria-label={t("Vendor preset")}
          >
            {VENDORS.map((v) => (
              <option key={v.id} value={v.id}>
                {v.label}
              </option>
            ))}
          </Select>
          <div className="relative min-w-0">
            <Input
              value={row.model}
              onChange={(e) => {
                setRow(i, { model: e.target.value });
                setModelMenu(i);
              }}
              onFocus={() => setModelMenu(i)}
              onBlur={() => window.setTimeout(() => setModelMenu((cur) => (cur === i ? null : cur)), 150)}
              placeholder={t("Model name")}
              title={t("Type to filter, or click to pick from the pulled list")}
              className="pr-7"
            />
            <button
              type="button"
              tabIndex={-1}
              className="absolute inset-y-0 right-1 flex w-6 items-center justify-center text-ink-400 hover:text-ink-700"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => setModelMenu((cur) => (cur === i ? null : i))}
              aria-label={t("Model name")}
            >
              <ChevronDown size={14} />
            </button>
            {modelMenu === i && modelOptions(row).length > 0 && (() => {
              const opts = modelOptions(row);
              const q = row.model.trim().toLowerCase();
              const shown = q && !opts.some((m) => m.toLowerCase() === q)
                ? opts.filter((m) => m.toLowerCase().includes(q))
                : opts;
              if (!shown.length) return null;
              return (
                <div
                  className="absolute left-0 right-0 top-full z-50 mt-1 max-h-52 overflow-auto rounded-md border border-[var(--line)] bg-[var(--panel)] p-1 shadow-lg"
                  onMouseDown={(e) => e.preventDefault()}
                >
                  {shown.map((m) => (
                    <button
                      key={m}
                      type="button"
                      className={`block w-full truncate rounded px-2 py-1.5 text-left text-xs hover:bg-[var(--panel2)] ${m === row.model ? "font-medium text-ink-900" : "text-ink-600"}`}
                      onClick={() => {
                        setRow(i, { model: m });
                        setModelMenu(null);
                      }}
                    >
                      {m}
                    </button>
                  ))}
                </div>
              );
            })()}
          </div>
        </div>

        {/* 第二行：Base URL。 */}
        <Input
          value={row.base_url}
          onChange={(e) => setRow(i, { base_url: e.target.value })}
          placeholder="https://api.deepseek.com"
          aria-label="Base URL"
        />

        {/* 第三行：API Key。 */}
        <Input
          type="password"
          value={row.api_key}
          onChange={(e) => setRow(i, { api_key: e.target.value })}
          placeholder={row.api_key_set ? t("•••••••• (kept, {{mask}})", { mask: row.api_key_masked || "" }) : "sk-…"}
          aria-label="API Key"
        />

        {/* 第四行：用这张卡自己的地址/钥匙拉取模型，结果进入上方模型下拉框。 */}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="ghost"
            className="h-7 text-[11px]"
            onClick={() => pullRowModels(i, row)}
            disabled={pullingRow.has(i)}
            title={t("Pull the model list for this entry's Base URL")}
          >
            {pullingRow.has(i) ? t("Pulling…") : t("Pull models")}
          </Button>
          <span className="text-[11px] text-ink-500">
            {t("Pulled models appear in the model list above.")}
          </span>
        </div>

        <button
          type="button"
          className="flex items-center gap-1 text-[11px] text-ink-500 hover:text-ink-700"
          onClick={() => toggleAdvanced(i)}
        >
          <ChevronRight className={`h-3 w-3 transition-transform ${adv ? "rotate-90" : ""}`} />
          {t("Advanced settings")}
        </button>

        {adv && (
          <div className="space-y-2 rounded-md bg-[var(--panel2)] p-2.5">
            <div className="grid grid-cols-2 gap-2">
              <label className="space-y-1 text-[11px] text-ink-500">
                <span>{t("Network proxy")}</span>
                <Select
                  value={row.proxy_mode || "system"}
                  onChange={(e) => setRow(i, { proxy_mode: e.target.value as PoolRow["proxy_mode"] })}
                >
                  <option value="system">{t("Follow system")}</option>
                  <option value="direct">{t("Force direct")}</option>
                  <option value="custom">{t("Custom proxy")}</option>
                </Select>
              </label>
              <label className="space-y-1 text-[11px] text-ink-500">
                <span>{t("Max context Tokens")}</span>
                <Input
                  type="number"
                  min={256}
                  value={row.context_tokens || 4096}
                  onChange={(e) => setRow(i, { context_tokens: Number(e.target.value) || 4096 })}
                />
              </label>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="space-y-1 text-[11px] text-ink-500">
                <span>{t("Input price (¥ per million tokens)")}</span>
                <Input
                  value={row.price_in || ""}
                  onChange={(e) => setRow(i, { price_in: e.target.value })}
                  placeholder={t("Unset")}
                />
              </label>
              <label className="space-y-1 text-[11px] text-ink-500">
                <span>{t("Output price (¥ per million tokens)")}</span>
                <Input
                  value={row.price_out || ""}
                  onChange={(e) => setRow(i, { price_out: e.target.value })}
                  placeholder={t("Unset")}
                />
              </label>
            </div>
            <div className="text-[11px] text-ink-500">
              {t("Prices are manually configured and only used to estimate cost on the AI usage page.")}
            </div>
            {row.proxy_mode === "custom" && (
              <Input
                value={row.proxy_url || ""}
                onChange={(e) => setRow(i, { proxy_url: e.target.value })}
                placeholder="http://127.0.0.1:7877"
              />
            )}
            <div className="flex items-center gap-4 text-[11px] text-ink-600">
              <label className="flex items-center gap-1.5">
                <Checkbox checked={row.vision !== false} onChange={(e) => setRow(i, { vision: e.target.checked })} />
                {t("Vision")}
              </label>
              <label className="flex items-center gap-1.5">
                <Checkbox checked={row.tools !== false} onChange={(e) => setRow(i, { tools: e.target.checked })} />
                {t("Tools")}
              </label>
            </div>
          </div>
        )}

        {rowTests[i]?.result && <RowTestResult result={rowTests[i]!.result!} />}
      </div>
    );
  };

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

      {/* 模型池 —— 唯一的模型配置入口。
          每个条目就是一张卡片：配置名称、厂商/模型、Base URL、API Key、拉取模型。
          没有顶部快捷表单，也没有“网关”这一层；高级字段收在每张卡的「高级设置」里。 */}
      <Card className="max-w-2xl space-y-3 p-4">
        <CardTitle
          title={t("Model pool (fallback order)")}
          hint={t("The first enabled card is preferred. On timeout, 5xx or connection failure the next enabled card takes over automatically. Leave an API key blank to keep the stored one.")}
          right={
            pool && pool.length > 1
              ? t("{{n}} sets configured.", { n: pool.length })
              : t("One entry = no fallback: this set is used as-is.")
          }
        />

        {poolDirty && (
          <div className="tp-alert tp-alert-warn text-[11px]">
            {t("有未保存的改动。点击下方「保存模型池」后生效。")}
          </div>
        )}

        {pool === null ? (
          <div className="text-xs text-ink-500">{t("Loading…")}</div>
        ) : (
          <>
            {/* 工具栏：批量管理 + 新增模型。 */}
            <div className="flex flex-wrap items-center gap-2">
              <Button
                variant={batchMode ? "primary" : "ghost"}
                className="h-7 text-[11px]"
                onClick={() => {
                  setBatchMode((v) => !v);
                  setBatchSelected(new Set());
                }}
              >
                {batchMode ? t("Exit batch mode") : t("Batch manage")}
              </Button>
              {batchMode && (
                <>
                  <Button
                    variant="ghost"
                    className="h-7 text-[11px]"
                    onClick={() => setBatchSelected(new Set(pool.map((p) => p.name).filter(Boolean)))}
                  >
                    {t("Select all")}
                  </Button>
                  <Button variant="ghost" className="h-7 text-[11px]" onClick={() => setBatchSelected(new Set())}>
                    {t("Clear selection")}
                  </Button>
                  <Button className="h-7 text-[11px]" onClick={() => runBatch("enable")} disabled={!batchSelected.size}>
                    {t("Batch enable")}
                  </Button>
                  <Button
                    variant="ghost"
                    className="h-7 text-[11px]"
                    onClick={() => runBatch("disable")}
                    disabled={!batchSelected.size}
                  >
                    {t("Batch disable")}
                  </Button>
                  <Button
                    variant="danger"
                    className="h-7 text-[11px]"
                    onClick={() => runBatch("delete")}
                    disabled={!batchSelected.size}
                  >
                    {t("Batch delete")}
                  </Button>
                </>
              )}
              <Button
                variant="ghost"
                className="h-7 text-[11px]"
                onClick={() => (llmTest ? setShowLlmTest((v) => !v) : testLlm())}
                disabled={testingLlm}
              >
                {testingLlm ? t("Testing all models…") : t("Test all model connectivity")}
              </Button>
              {llmTest && (
                <Button
                  variant="ghost"
                  className="h-7 text-[11px]"
                  onClick={() => setShowLlmTest((v) => !v)}
                >
                  {showLlmTest ? t("Hide results") : t("Show results")}
                </Button>
              )}
            </div>

            {llmTest && showLlmTest && (
              <div className="space-y-2 rounded-lg border border-[var(--line)] p-3">
                <div className="text-[11px] text-ink-500">
                  {t("Tests every model the platform may call — all pool entries and any per-purpose override — and lists each.")}
                </div>
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

            {/* 模型卡片列表：点「+ 新增模型」就多一张。 */}
            <div className="space-y-2">
              {pool.length === 0 && (
                <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-[var(--line)] bg-[var(--panel2)] px-4 py-8 text-center">
                  <div className="text-xs text-ink-500">
                    {t("当前未配置任何模型，所有 AI 功能都不可用。点击下方【新增模型】添加第一套。")}
                  </div>
                </div>
              )}
              {pool.map((row, i) => poolEntry(row, i))}
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <Button variant="ghost" onClick={addEmptyRow}>
                {t("+ Add model")}
              </Button>
              <Button onClick={savePool} disabled={savingPool}>
                {savingPool ? t("Saving…") : t("Save pool")}
              </Button>
            </div>
          </>
        )}
      </Card>

      {/* 「模型分配」只剩一层：按用途指定。
          用途清单、顺序、中文名全部来自后端 roles_for_ui()；未勾选的用途使用池里第一套，
          429 时按池顺序自动换下一套。routing.chat / routing.agent 只回传、不在界面修改。 */}
      <Card className="max-w-2xl space-y-4 p-4">
        <CardTitle title={t("Model assignment")} />

        <div className="space-y-2 rounded-lg border border-[var(--line)] p-3">
          <div className="text-[13px] font-medium text-ink-900">
            {t("Model per purpose")}
          </div>
          <div className="text-[11px] text-ink-500">
            {t(
              "Assign models per purpose — checked purposes show a dropdown; unchecked ones use the first pool entry (on 429 the next entry in pool order takes over).",
            )}
          </div>
          <div className="space-y-1.5">
            {roleCatalog.map(({ key: r, label }) => {
              const checked = r in roleModels;
              const value = roleModels[r] ?? "";
              const missing = checked && !value.trim();
              return (
                <div key={r} className="flex flex-wrap items-center gap-2">
                  <label className="flex cursor-pointer items-center gap-2">
                    <Checkbox
                      checked={checked}
                      onChange={(e) =>
                        setRoleModels((prev) => {
                          const next = { ...prev };
                          if (e.target.checked) next[r] = "";
                          else delete next[r];
                          return next;
                        })
                      }
                    />
                    <span className="text-[12px] text-ink-700">{label}</span>
                  </label>
                  {checked && (
                    <>
                      <Select
                        value={value}
                        onChange={(e) =>
                          setRoleModels((prev) => ({ ...prev, [r]: e.target.value }))
                        }
                        className="ml-auto h-8 w-64"
                      >
                        <option value="">{t("— select a model —")}</option>
                        {poolNames.map((n) => (
                          <option key={n} value={n}>
                            {n} · {modelOf(n)}
                          </option>
                        ))}
                        {value && !poolNames.includes(value) && (
                          <option value={value}>{value}</option>
                        )}
                      </Select>
                      {missing && (
                        <span className="text-[11px] text-[var(--bad-fg)]">
                          {t("Select a model for this step.")}
                        </span>
                      )}
                    </>
                  )}
                </div>
              );
            })}
          </div>
          <label className="flex items-center gap-2 border-t border-[var(--line)] pt-2 text-[11px] text-ink-600">
            <Checkbox
              checked={routing.failover}
              onChange={(e) => setRouting((r) => ({ ...r, failover: e.target.checked }))}
            />
            {t("On 429 / timeout, switch to the next pool entry in order")}
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-3 border-t border-[var(--line)] pt-3">
          <Button onClick={saveModels} disabled={savingModels}>
            {savingModels ? t("Saving…") : t("Save model assignment")}
          </Button>
          <span className="text-[11px] text-ink-500">
            {t("Only purpose overrides and failover are saved; routing is kept as loaded.")}
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
