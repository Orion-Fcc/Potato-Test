import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type FeishuStatus, type LlmPoolEntry, type LlmRoleStatus, type LlmStatus, type LlmTestResult } from "../lib/api";
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
const ROLES = ["agent", "judge", "narrative", "specgen", "assistant"] as const;
const ROLE_LABEL: Record<string, string> = {
  agent: "Execution (agent, every step)",
  judge: "Judging (once per case)",
  narrative: "Failure narrative / case memory",
  specgen: "Case generation from spec",
  assistant: "Assistant chat & Feishu",
};
const ROLE_HINT: Record<string, string> = {
  agent: "~95% of all tokens — the only lever that really cuts cost",
  judge: "small volume, quality-critical — keep the strong model here",
  narrative: "runs only on failures, short output",
  specgen: "long-document batches, quality matters",
  assistant: "interactive, latency-sensitive",
};

// 能力徽标只用符号，不引入新的文案键（服务端实测出来的布尔值无歧义）
function mark(ok: boolean) {
  return ok ? "✓" : "✕";
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

export function AdminSettingsPage() {
  const { t } = useTranslation();
  const toast = useToast();
  const fail = useErrorToast();
  const { gitlabEnabled, feishuEnabled } = useAuth();
  const [tokenSet, setTokenSet] = useState(false);
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);

  // LLM model
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmAgentModel, setLlmAgentModel] = useState("");
  const [llmProvider, setLlmProvider] = useState("auto");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [savingLlm, setSavingLlm] = useState(false);
  const [testingLlm, setTestingLlm] = useState(false);
  const [llmTest, setLlmTest] = useState<{ ok: boolean; text: string; result?: LlmTestResult } | null>(
    null,
  );
  const [loadError, setLoadError] = useState("");

  // LLM 池（多套网关，按顺序 429 兜底）
  const [pool, setPool] = useState<PoolRow[] | null>(null);
  const [savingPool, setSavingPool] = useState(false);

  // 按用途分模型（agent 每步执行 / 判定 / 失败叙述 / 需规生成 / 助手对话）。
  // 空字符串 = 跟随默认配置；界面显示 effective_* 让"这个用途实际用哪个模型"一目了然。
  const [roles, setRoles] = useState<Record<string, RoleRow>>({});
  const [savingRoles, setSavingRoles] = useState(false);

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
        setLlm(s.llm);
        setLlmBaseUrl(s.llm.base_url);
        setLlmModel(s.llm.model);
        setLlmAgentModel(s.llm.agent_model);
        setLlmProvider(s.llm.provider || "auto");
        setRoles(s.llm.roles || {});
        setFs(s.feishu);
        setAppId(s.feishu.app_id);
        setApiBase(s.feishu.api_base);
        setAutoAnswer(s.feishu.auto_answer_detected);
      })
      .catch((e) => setLoadError(e?.message || String(e)));

  const loadPool = () =>
    api
      .getLlmPool()
      .then((r) => setPool(r.pool.map((p) => ({ ...p, api_key: "" }))))
      .catch((e) => setLoadError(e?.message || String(e)));

  useEffect(() => {
    load();
    loadPool();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const saveLlm = async () => {
    setSavingLlm(true);
    setLlmTest(null);
    try {
      const r = await api.setLlmSettings({
        base_url: llmBaseUrl.trim(),
        model: llmModel.trim(),
        agent_model: llmAgentModel.trim(),
        provider: llmProvider,
        api_key: llmApiKey.trim() || undefined,
      });
      setLlm(r);
      setLlmBaseUrl(r.base_url);
      setLlmModel(r.model);
      setLlmAgentModel(r.agent_model);
      setLlmProvider(r.provider || "auto");
      setLlmApiKey("");
      toast("success", t("Saved"));
    } catch (e: any) {
      // Show it inline too — a toast disappears and the user is left guessing.
      setLlmTest({ ok: false, text: e?.message || String(e) });
      fail(e);
    } finally {
      setSavingLlm(false);
    }
  };

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

  // 保存按用途分模型：只发有值的字段，空字符串 = 清除该覆盖（回归"跟随默认"）。
  const saveRoles = async () => {
    setSavingRoles(true);
    try {
      const payload: Record<string, { model?: string; base_url?: string; api_key?: string; provider?: string }> = {};
      for (const r of ROLES) {
        const row = roles[r];
        if (!row) continue;
        const item: { model?: string; base_url?: string; api_key?: string; provider?: string } = {};
        if (row.model.trim()) item.model = row.model.trim();
        if (row.base_url.trim()) item.base_url = row.base_url.trim();
        if (row.api_key?.trim()) item.api_key = row.api_key.trim();
        if (row.provider && row.provider !== "auto") item.provider = row.provider;
        if (Object.keys(item).length) payload[r] = item;
      }
      await api.setLlmSettings({ roles: payload });
      await load();
      toast("success", t("Saved"));
    } catch (e: any) {
      fail(e);
    } finally {
      setSavingRoles(false);
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

      <Card className="max-w-2xl space-y-3 p-4">
        <div>
          <div className="text-sm font-medium text-ink-900">{t("LLM model")}</div>
          <div className="text-xs text-ink-500">
            {t("The OpenAI-compatible endpoint and models behind the browser agent and the judge. Changes apply to new runs — no redeploy.")}
          </div>
        </div>

        {/* What the platform is ACTUALLY using right now. Only an admin sees this page, so
            there is no reason to hide the model behind the input boxes: the saved value and
            the inherited .env default look identical otherwise, and "which model ran that
            case?" is the single most common question when a verdict looks wrong. */}
        <div className="rounded-lg bg-[var(--panel2)] px-3 py-2 text-xs text-ink-700">
          <div className="font-medium">{t("Currently in use")}</div>
          <div className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
            <span className="text-ink-500">{t("Endpoint")}</span>
            <span className="break-all font-mono">{llm?.base_url || t("(not set)")}</span>
            <span className="text-ink-500">{t("Model (judge + default)")}</span>
            <span className="break-all font-mono">{llm?.model || t("(not set)")}</span>
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

        <Field label={t("Protocol")}>
          <Select value={llmProvider} onChange={(e) => setLlmProvider(e.target.value)}>
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {t(PROVIDER_LABEL[p])}
              </option>
            ))}
          </Select>
          <div className="mt-1 text-xs text-ink-500">
            {t("Auto-detect works for any OpenAI-compatible gateway (DeepSeek, Qwen, GLM, Kimi, vLLM, OpenRouter …) and for Claude / Gemini / Ollama. Only set it explicitly if detection guesses wrong.")}
          </div>
        </Field>

        <Field label={t("Base URL (OpenAI-compatible)")}>
          <Input value={llmBaseUrl} onChange={(e) => setLlmBaseUrl(e.target.value)}
            placeholder="https://api.openai.com/v1" />
        </Field>
        <Field label={llm?.api_key_set ? t("API key (set — leave blank to keep)") : t("API key")}>
          <Input type="password" value={llmApiKey} onChange={(e) => setLlmApiKey(e.target.value)}
            placeholder={llm?.api_key_set ? "••••••••" : "sk-…"} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("Model (judge + default)")}>
            <Input value={llmModel} onChange={(e) => setLlmModel(e.target.value)} placeholder="gpt-4o" />
          </Field>
          <Field label={t("Agent model (optional — empty uses Model)")}>
            <Input value={llmAgentModel} onChange={(e) => setLlmAgentModel(e.target.value)}
              placeholder={t("e.g. a local VLM")} />
          </Field>
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={saveLlm} disabled={savingLlm}>{savingLlm ? t("Saving…") : t("Save")}</Button>
          <Button variant="ghost" onClick={testLlm} disabled={testingLlm}>
            {testingLlm ? t("Testing…") : t("Test connection")}
          </Button>
          <span className="text-xs text-ink-500">
            {llm?.api_key_set ? t("API key is set.") : t("No API key set — the gateway may reject calls.")}
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
        <p className="text-xs text-ink-500">
          {t("The judge model needs vision (screenshots are evidence). Values here override .env; key stored encrypted.")}
        </p>
      </Card>

      {/* 池：多套网关按顺序兜底。之前只有 API/数据库能改 —— 也就是"换模型要改代码"
          这件事的最后一块。顺序即优先级，429（配额耗尽）自动换下一套。 */}
      <Card className="max-w-2xl space-y-3 p-4">
        <div>
          <div className="text-sm font-medium text-ink-900">{t("Model pool (fallback order)")}</div>
          <div className="text-xs text-ink-500">
            {t("Several gateways tried in order: index 1 is preferred, the next one takes over when a key is rate-limited (429) or misconfigured. Leave an API key blank to keep the stored one.")}
          </div>
        </div>

        {pool === null ? (
          <div className="text-xs text-ink-500">{t("Loading…")}</div>
        ) : (
          <div className="space-y-2">
            {pool.map((row, i) => (
              <div key={i} className="rounded-lg border border-[var(--line)] p-2.5 space-y-2">
                <div className="flex items-center gap-2">
                  <span className="w-6 text-center text-xs font-mono text-ink-500">{i + 1}</span>
                  <Input
                    value={row.name}
                    onChange={(e) =>
                      setPool(pool.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))
                    }
                    placeholder={t("Name")}
                    className="w-40"
                  />
                  <Select
                    value={row.provider || "auto"}
                    onChange={(e) =>
                      setPool(pool.map((r, j) => (j === i ? { ...r, provider: e.target.value } : r)))
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
                    onClick={() => setPool(pool.filter((_r, j) => j !== i))}
                  >
                    {t("Remove")}
                  </Button>
                </div>
                <Input
                  value={row.base_url}
                  onChange={(e) =>
                    setPool(pool.map((r, j) => (j === i ? { ...r, base_url: e.target.value } : r)))
                  }
                  placeholder="https://api.deepseek.com/v1"
                />
                <div className="grid grid-cols-3 gap-2">
                  <Input
                    value={row.model}
                    onChange={(e) =>
                      setPool(pool.map((r, j) => (j === i ? { ...r, model: e.target.value } : r)))
                    }
                    placeholder={t("Model")}
                  />
                  <Input
                    value={row.agent_model}
                    onChange={(e) =>
                      setPool(pool.map((r, j) => (j === i ? { ...r, agent_model: e.target.value } : r)))
                    }
                    placeholder={t("Agent model")}
                  />
                  <Input
                    type="password"
                    value={row.api_key}
                    onChange={(e) =>
                      setPool(pool.map((r, j) => (j === i ? { ...r, api_key: e.target.value } : r)))
                    }
                    placeholder={row.api_key_set ? t("•••••••• (kept)") : "sk-…"}
                  />
                </div>
              </div>
            ))}
            <div className="flex items-center gap-3">
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
              <Button onClick={savePool} disabled={savingPool}>
                {savingPool ? t("Saving…") : t("Save pool")}
              </Button>
              <span className="text-xs text-ink-500">
                {pool.length <= 1
                  ? t("One entry = no fallback: the single set above is used as-is.")
                  : t("{{n}} sets configured.", { n: pool.length })}
              </span>
            </div>
          </div>
        )}
      </Card>

      {/* 按用途分模型（2026-10-11）：钱花在"量"上，质量保在"判"上。
          留空 = 跟随默认；只换模型填模型名就够，换厂商才需要网关/钥匙。 */}
      <Card className="max-w-2xl space-y-3 p-4">
        <div>
          <div className="text-sm font-medium text-ink-900">{t("Model per purpose")}</div>
          <div className="text-xs text-ink-500">
            {t("Cheap model for the high-volume execution steps, strong model where quality matters. Blank = follow the default.")}
          </div>
        </div>
        <div className="space-y-3">
          {ROLES.map((r) => {
            const row = roles[r];
            return (
              <div key={r} className="space-y-1.5 rounded-lg border border-[var(--line)] p-3">
                <div className="flex items-baseline justify-between gap-3">
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
                <div className="text-[11px] text-ink-400">{t(ROLE_HINT[r])}</div>
              </div>
            );
          })}
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={saveRoles} disabled={savingRoles}>
            {savingRoles ? t("Saving…") : t("Save per-purpose models")}
          </Button>
          <Button
            variant="ghost"
            onClick={() => setRoles({})}
            disabled={savingRoles || !Object.keys(roles).length}
          >
            {t("Reset all to default")}
          </Button>
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
