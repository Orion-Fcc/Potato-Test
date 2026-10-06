import i18n from "../i18n";

export interface Project {
  id: number;
  name: string;
  base_url: string | null;
  has_login_state: boolean;
  roles?: string[];
  gitlab_project?: string | null;
  case_timeout_s?: number | null;
  case_max_steps?: number | null;
  run_concurrency?: number | null;
  // 证据采集开关。null/undefined = 该项目没设过，跟随服务器默认。
  case_record_video?: boolean | null;
  live_shot_every?: number | null;
  feishu_chat_id?: string | null;
  feishu_bitable_bound?: boolean;
  case_count?: number;
  last_run?: {
    id: number;
    status: string;
    pass_rate: number | null;
    finished_at: string | null;
  } | null;
}

export interface BrowserStateSlot {
  slot: string;
  path: string;
  bytes: number;
  warm: boolean;
  mtime: number;
}

/** Workspace-scoped persistent browser state: cookies / localStorage / sessionStorage /
 *  IndexedDB kept between cases and between runs, isolated per project. */
export interface BrowserState {
  project_id: number;
  enabled: boolean;
  root: string;
  slots: BrowserStateSlot[];
  total_bytes: number;
  last_used: number;
}

/** 失败清单里的一条用例（同一用例多次失败只出现一次，fail_count 是次数）。 */
export interface DigestCase {
  case_id: number | null;
  case_key: string;
  name: string;
  module: string;
  fail_count: number;
  latest_reason: string;
  latest_error: string;
  latest_at?: string;
}

/**
 * 一条清单项 = 一组「同因」失败。
 *
 * action 决定该做什么，助手据此决定能不能改用例：
 *   fix_case 改用例 / fix_data 改测试数据 / fix_env 改环境或凭据 /
 *   report_to_dev 提缺陷给开发 / rerun 重跑观察 / inspect 需人工看一眼
 *
 * ★ report_to_dev 的条目绝不能去改 expected —— 那是真缺陷，改预期等于掩盖它。
 */
export interface DigestGroup {
  signal: string;
  summary: string;
  action: string;
  action_label: string;
  case_count: number;
  result_count: number;
  cases: DigestCase[];
  result_ids: number[];
  latest_at: string;
  /** 同一组里判定器给出了互相矛盾的根因 —— 需要人看，action 会降级为 inspect。 */
  cause_conflict: boolean;
  causes_seen: string[];
}

export interface FailureDigest {
  project_id: number;
  generated_at: string;
  /** 本次调用新补了多少条历史根因分类（已写库，下次是 0）。 */
  backfilled: number;
  /** 仍然没有根因的条数：这些会落在「原因待人工确认」里。 */
  uncategorized: number;
  group_count: number;
  case_count: number;
  groups: DigestGroup[];
  note?: string;
}

export interface CaseChange {
  id: number;
  project_id: number;
  case_id: number;
  field: string;
  before: string;
  after: string;
  /** assistant = 内置助手自动改的。前端要单独标出来——AI 改的断言天然可疑。 */
  source: string;
  digest_signal: string;
  by_label: string;
  created_at: string;
}

export interface AssistantTurn {
  role: "user" | "assistant";
  content: string;
}

export interface AssistantReply {
  reply: string;
  actions: { tool: string; args: Record<string, unknown>; ok: boolean }[];
}

export interface ProjectStats {
  base_url: string | null;
  has_credential: boolean;
  has_healthy_credential: boolean;
  issue_count: number;
  case_count: number;
  enabled_count: number;
  run_count: number;
  last_run: Run | null;
  trend: { run_id: number; name: string; pass_rate: number; finished_at: string | null }[];
  last_pass_rate: number | null;
  last_latency_p50_ms: number | null;
  last_flaky: number;
  top_failing: { case_id: number; name: string; fail_count: number }[];
}

export interface Environment {
  id: number;
  project_id: number;
  name: string;
  base_url: string | null;
  is_default: boolean;
}

export interface Credential {
  id: number;
  project_id: number;
  type: "storage_state" | "password";
  role: string | null;
  environment_id: number | null;
  label: string;
  username: string | null;
  is_active: boolean;
  healthy: boolean;
  last_error: string | null;
  last_checked_at: string | null;
  expires_at: string | null;
  created_at: string | null;
}

export type IssueStatus = "open" | "in_progress" | "fixed" | "verified" | "closed";
export type Severity = "low" | "medium" | "high" | "critical";

export interface IssueComment {
  id: number;
  issue_id: number;
  body: string;
  author: string | null;
  created_at: string | null;
}

export interface Issue {
  id: number;
  project_id: number;
  title: string;
  description: string;
  status: IssueStatus;
  severity: Severity;
  assignee: string | null;
  labels: string[];
  case_id: number | null;
  run_id: number | null;
  result_id: number | null;
  gitlab_iid?: number | null;
  gitlab_url?: string | null;
  created_at: string | null;
  updated_at: string | null;
  comments?: IssueComment[];
}

export type FeedbackCategory = "bug" | "question" | "feature" | "other";

export interface FeedbackItem {
  id: number;
  source: string;
  chat_id: string | null;
  chat_type: string | null;
  sender_id: string | null;
  sender_name: string | null;
  content: string;
  title: string | null;
  category: FeedbackCategory;
  severity: Severity;
  answer: string | null;
  answered: boolean;
  status: string;
  project_id: number | null;
  issue_id: number | null;
  created_at: string | null;
}

export interface LlmStatus {
  base_url: string;
  model: string;
  agent_model: string;
  api_key_set: boolean;
}

export interface LlmSettingsIn {
  base_url?: string; // "" clears the override (falls back to env)
  model?: string;
  agent_model?: string;
  api_key?: string; // blank keeps the stored key
}

export interface FeishuStatus {
  app_id: string;
  app_secret_set: boolean;
  verification_token_set: boolean;
  api_base: string;
  auto_answer_detected: boolean;
}

export interface FeishuSettingsIn {
  app_id?: string;
  api_base?: string;
  auto_answer_detected?: boolean;
  app_secret?: string; // blank keeps the stored value
  verification_token?: string; // blank keeps the stored value
}

export interface GitLabConfig {
  gitlab_project: string | null;
  has_token: boolean;
  token_source?: "project" | "global" | null;
  sync_enabled: boolean;
}

export interface CaseResult {
  result_id: number;
  run_id: number;
  run_name: string;
  status: "passed" | "failed" | "error";
  flaky: boolean;
  latency_ms: number;
  judge_reason: string | null;
  video_url: string | null;
  trace_url: string | null;
  finished_at: string | null;
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "";
  // API timestamps are UTC; treat tz-less ISO strings as UTC, not local time.
  const utc = /Z$|[+-]\d{2}:\d{2}$/.test(iso) ? iso : iso + "Z";
  const s = Math.round((Date.now() - new Date(utc).getTime()) / 1000);
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const t = i18n.t.bind(i18n);
  if (s < 60) return t("just now");
  if (s < 3600) return t("{{n}}m ago", { n: Math.floor(s / 60) });
  if (s < 86400) return t("{{n}}h ago", { n: Math.floor(s / 3600) });
  return t("{{n}}d ago", { n: Math.floor(s / 86400) });
}

export type CasePriority = "P0" | "P1" | "P2" | "P3";
export type CaseType = "functional" | "smoke" | "regression" | "acceptance" | "negative";
export type CaseStatus = "draft" | "active" | "deprecated";

export interface TestStep {
  action: string;
  expected: string;
}

export interface TestCase {
  id: number;
  project_id: number;
  case_key: string | null;
  name: string;
  module: string | null;
  priority: CasePriority;
  type: CaseType;
  status: CaseStatus;
  owner: string | null;
  role: string | null;
  // 2026-10-04 多角色：执行时依次使用的身份，顺序即切换顺序。
  // 后端保证：只要 role 有值，roles 就至少含 role 这一项（见 _roles_of），
  // 所以前端可以只认 roles，不必再单独处理 role 的兼容逻辑。
  roles?: string[];
  references: string;
  preconditions: string;
  prompt: string;
  steps: TestStep[];
  test_data: string;
  // 2026-10-06 测试数据文件声明（后端 app/testdata.py 消费）。
  // data_files 是原样保存的声明；data_files_error 是后端校验后的原因（没有则为空）——
  // 界面靠它当场显示"哪里写错了"，而不是等一条用例跑完才发现文件没准备好。
  // 保存走 Partial<TestCase>，所以加在这里就同时覆盖 createCase / updateCase 的请求体。
  data_files: { files?: unknown[] } | null;
  data_files_error: string | null;
  // 数据隔离提示：拼进 agent 任务提示。留空 = 不注入。
  // 见后端 app/data_hygiene.py —— 预期里写死了"1 行""2 条"这类绝对数字时必填，
  // 否则用例会被自己上次留下的数据污染，第二次跑必然假失败。
  data_hygiene?: string;
  expected: string;
  start_url: string | null;
  tags: string[];
  enabled: boolean;
  updated_at?: string | null;
  last_status?: "passed" | "failed" | "error" | null; // latest run result; null = never run
  last_run_id?: number | null; // the run that verdict came from
  last_run_at?: string | null; // when it last ran
  // 操作经验记忆（见后端 app/case_memory.py）。**只含过程知识**（导航路径、
  // 页面脾气、元素注意事项），绝不含判定结果 —— 否则下次运行就变成背答案。
  memory?: {
    navigation?: string[];
    page_notes?: string[];
    element_notes?: string[];
  } | null;
  memory_updated_at?: string | null;
  // true = 记忆还在但用例已被改动，指纹对不上、运行时会被忽略
  memory_stale?: boolean;
}

export interface RunSummary {
  total: number;
  passed: number;
  failed: number;
  error: number;
  flaky: number;
  pass_rate: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
}

export interface Run {
  id: number;
  project_id: number;
  name: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  concurrency: number;
  case_ids: number[];
  total_count: number;
  processed_count: number;
  passed_count: number;
  summary: RunSummary | null;
  started_at: string | null;
  finished_at: string | null;
  created_at?: string | null;
  created_by?: string | null;
  suite_id?: number | null;
  ran_by_user_id?: number | null;
  ran_by_label?: string | null;
  trigger?: "manual" | "suite";
  environment_id?: number | null;
}

export type Cadence = "none" | "daily" | "weekly" | "biweekly" | "monthly";

export interface Suite {
  id: number;
  project_id: number;
  name: string;
  description: string;
  selection_mode: "cases" | "tags";
  case_ids: number[];
  tag_filter: string[];
  owner_user_id: number | null;
  owner_label: string | null;
  runner_user_id: number | null;
  runner_label: string | null;
  environment_id: number | null;
  cadence: Cadence;
  due_at: string | null;
  last_run_id: number | null;
  last_status: string | null;
  last_run_at: string | null;
  created_at?: string | null;
}

export interface AppNotification {
  id: number;
  type: string;
  title: string;
  body: string;
  link: string | null;
  suite_id: number | null;
  run_id: number | null;
  issue_id: number | null;
  read: boolean;
  created_at: string | null;
}

export interface RunResult {
  id: number;
  run_id: number;
  case_id: number;
  status: "passed" | "failed" | "error" | "running";
  attempts: number;
  flaky: boolean;
  video_url: string | null;
  trace_url: string | null;
  steps: string[];
  diagnostics?: DiagStep[];
  judge_reason: string | null;
  // 2026-10-04 失败根因分类。三种取值要分清：
  //   ""        -> 通过的用例（无根因）
  //   null/缺失 -> 分类功能上线前的历史结果（报告显示"未分类"，不能显示成 unclear）
  //   分类标识   -> 后端 ROOT_CAUSES 里的 key
  // root_cause_label 是后端给的中文标签 —— 前端不再自己维护一份分类表，
  // 两份表不同步时报告里会冒出空白分组。
  root_cause?: string | null;
  root_cause_label?: string | null;
  is_real_defect?: boolean;
  verdict_evidence?: number[];
  final_answer: string | null;
  /** AI-written bug description, present only for failed/errored cases. */
  failure_narrative?: FailureNarrative | null;
  account_label?: string | null;
  latency_ms: number;
  error: string | null;
  // 2026-10-06 人工改判。verdict_override 非空 => 这条结论是人改的，不是 AI 判的。
  // 界面必须把两者区分开，否则通过率/真缺陷率这些数字没法信。
  verdict_override?: string | null;
  override_reason?: string | null;
  override_by?: string | null;
  override_at?: string | null;
  /** 改判前 AI 的原始结论（撤销改判时用来还原）。 */
  original_status?: string | null;
}

/** The 操作步骤/实际结果/预期结果 bug description produced for failed cases. */
export interface FailureNarrative {
  steps: string;
  actual: string;
  expected: string;
  title?: string;
  severity?: string;
}

export interface DiagStep {
  i: number;
  action: string;
  /** 「点了啥」的人话摘要（click_element_by_index(index=12)）。旧数据没有这一列。 */
  detail?: string;
  thought: string;
  result: string;
  error: string;
  screenshot: string | null;
  elapsed_s?: number;
}

/** Default request budget. Long-running endpoints (run start, doc extract)
 *  pass their own value via `init.timeoutMs`. */
const DEFAULT_TIMEOUT_MS = 30_000;

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Strip a server error body down to something safe and short.
 *  A raw HTML stack trace or a giant JSON blob has no business being rendered
 *  into the UI (and is the usual way internals leak to the browser). */
function safeMessage(status: number, raw: string): string {
  let msg = raw.trim();
  // JSON envelope: {detail: "…"} / {message: "…"} / {error: "…"}
  if (msg.startsWith("{") || msg.startsWith("[")) {
    try {
      const j = JSON.parse(msg) as Record<string, unknown>;
      const d = j.detail ?? j.message ?? j.error;
      if (typeof d === "string") msg = d;
      else if (Array.isArray(d) && d.length > 0) {
        const first = d[0] as Record<string, unknown>;
        msg = typeof first?.msg === "string" ? first.msg : JSON.stringify(d).slice(0, 300);
      }
    } catch {
      /* keep the raw text, truncated below */
    }
  }
  // A stack trace / HTML page is never useful to the operator.
  if (/<(html|!doctype|body)/i.test(msg) || /^\s*Traceback/m.test(msg)) {
    return `${status} ${statusText(status)}`;
  }
  return msg.length > 300 ? `${msg.slice(0, 300)}…` : msg;
}

function statusText(status: number): string {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not found";
  if (status === 409) return "conflict";
  if (status === 422) return "invalid input";
  if (status >= 500) return "server error";
  return "request failed";
}

type ReqInit = RequestInit & { timeoutMs?: number };

async function req<T>(path: string, init?: ReqInit): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal, headers, ...rest } = init ?? {};

  // Compose the caller's signal with a timeout so an unmounted component can
  // abort in-flight work instead of resolving into dead state.
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => ctrl.abort(new DOMException("timeout", "TimeoutError")), timeoutMs);

  try {
    const res = await fetch(`/api${path}`, {
      // same-origin cookies are how the session is carried; without this an
      // authenticated deployment silently degrades to anonymous.
      credentials: "same-origin",
      ...rest,
      signal: ctrl.signal,
      headers: { "content-type": "application/json", ...headers },
    });
    if (!res.ok) {
      throw new ApiError(res.status, safeMessage(res.status, await res.text()));
    }
    // 204 / empty body would blow up res.json()
    if (res.status === 204) return undefined as T;
    const text = await res.text();
    return (text ? JSON.parse(text) : undefined) as T;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    if (e instanceof DOMException || (e as Error)?.name === "AbortError") {
      // Distinguish our own timeout from a caller-driven cancel.
      const timedOut = ctrl.signal.reason instanceof DOMException;
      throw new ApiError(0, timedOut ? `request timed out after ${Math.round(timeoutMs / 1000)}s` : "request cancelled");
    }
    throw new ApiError(0, "network error — is the backend reachable?");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}

/** Per-call options. `signal` lets a component abort a read on unmount so a
 *  slow response can never land on a screen the operator already left. */
export type Opts = { signal?: AbortSignal };

export const api = {
  listProjects: (o?: Opts) => req<Project[]>("/projects", o),
  createProject: (b: { name: string; base_url?: string; login_state?: string }) =>
    req<Project>("/projects", { method: "POST", body: JSON.stringify(b) }),
  updateProject: (
    pid: number,
    b: {
      name?: string;
      base_url?: string;
      case_timeout_s?: number | null;
      case_max_steps?: number | null;
      run_concurrency?: number | null;
      // 传 null 表示"改回跟随服务器默认"（后端按 model_fields_set 判断有没有传这个键）；
      // 不传这个键则完全不修改。
      case_record_video?: boolean | null;
      live_shot_every?: number | null;
      feishu_chat_id?: string | null;
      feishu_bitable_url?: string | null;
    },
  ) =>
    req<Project>(`/projects/${pid}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteProject: (pid: number) =>
    req<{ deleted: number }>(`/projects/${pid}`, { method: "DELETE" }),
  // Workspace-scoped persistent browser state (cookies/localStorage/sessionStorage/
  // IndexedDB kept between cases and runs, isolated per project).
  getBrowserState: (pid: number, o?: Opts) =>
    req<BrowserState>(`/projects/${pid}/browser-state`, o),
  // ── 失败清单（2026-10-06）─────────────────────────────────────────────
  // backfill=true 会给缺根因的历史失败补一次分类（LLM，首次十几秒，之后走缓存）。
  // 清单是从 run_result 实时算出来的：用例一旦通过就自动从清单消失，
  // 所以**不需要任何手工维护**。
  getFailureDigest: (pid: number, backfill = true, o?: Opts) =>
    req<FailureDigest>(
      `/projects/${pid}/failure-digest?backfill=${backfill ? "true" : "false"}`,
      o,
    ),
  getCaseChanges: (pid: number, limit = 30, o?: Opts) =>
    req<CaseChange[]>(`/projects/${pid}/case-changes?limit=${limit}`, o),
  resetBrowserState: (pid: number) =>
    req<{ reset: number }>(`/projects/${pid}/browser-state`, { method: "DELETE" }),
  getRoles: (pid: number, o?: Opts) => req<{ roles: string[] }>(`/projects/${pid}/roles`, o),
  setRoles: (pid: number, roles: string[]) =>
    req<{ roles: string[] }>(`/projects/${pid}/roles`, { method: "PUT", body: JSON.stringify({ roles }) }),

  // environments
  listEnvironments: (pid: number, o?: Opts) => req<Environment[]>(`/projects/${pid}/environments`, o),
  createEnvironment: (pid: number, b: { name: string; base_url?: string; is_default?: boolean }) =>
    req<Environment>(`/projects/${pid}/environments`, { method: "POST", body: JSON.stringify(b) }),
  updateEnvironment: (eid: number, b: Record<string, unknown>) =>
    req<Environment>(`/environments/${eid}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteEnvironment: (eid: number) =>
    req<{ deleted: number }>(`/environments/${eid}`, { method: "DELETE" }),

  getGitlabConfig: (pid: number, o?: Opts) => req<GitLabConfig>(`/projects/${pid}/gitlab`, o),
  listGitlabProjects: (pid: number) =>
    req<{ id: number; path: string; name: string | null }[]>(`/projects/${pid}/gitlab/projects`),
  setGitlabConfig: (pid: number, b: { gitlab_project: string; token?: string }) =>
    req<GitLabConfig>(`/projects/${pid}/gitlab`, { method: "PUT", body: JSON.stringify(b) }),

  listCredentials: (pid: number, o?: Opts) => req<Credential[]>(`/projects/${pid}/credentials`, o),
  createCredential: (
    pid: number,
    b: {
      type: "storage_state" | "password";
      role?: string | null;
      environment_id?: number | null;
      label?: string;
      username?: string;
      secret: string;
    },
  ) => req<Credential>(`/projects/${pid}/credentials`, { method: "POST", body: JSON.stringify(b) }),
  captureCredential: (pid: number, b: { label?: string; username: string; password: string }) =>
    req<Credential>(`/projects/${pid}/credentials/capture`, { method: "POST", body: JSON.stringify(b) }),
  /** 改一条既有凭据：只传要改的字段（密码留空/不传 = 不改）。 */
  updateCredential: (
    cid: number,
    b: { label?: string; username?: string; secret?: string; role?: string | null; environment_id?: number | null },
  ) => req<Credential>(`/credentials/${cid}`, { method: "PATCH", body: JSON.stringify(b) }),
  activateCredential: (cid: number) =>
    req<Credential>(`/credentials/${cid}/activate`, { method: "POST" }),
  recheckCredential: (cid: number) =>
    req<Credential>(`/credentials/${cid}/recheck`, { method: "POST" }),
  deleteCredential: (cid: number) =>
    req<{ deleted: number }>(`/credentials/${cid}`, { method: "DELETE" }),

  listCases: (pid: number, o?: Opts) => req<TestCase[]>(`/projects/${pid}/testcases`, o),
  createCase: (pid: number, b: Partial<TestCase>) =>
    req<TestCase>(`/projects/${pid}/testcases`, { method: "POST", body: JSON.stringify(b) }),
  updateCase: (id: number, b: Partial<TestCase>) =>
    req<TestCase>(`/testcases/${id}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteCase: (id: number) => req<{ deleted: number }>(`/testcases/${id}`, { method: "DELETE" }),
  // 清空一条用例的"操作经验记忆"：页面改版导致记忆里记的东西失效时用
  clearCaseMemory: (id: number) =>
    req<TestCase>(`/testcases/${id}/memory`, { method: "DELETE" }),
  importCasesXlsx: async (pid: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/projects/${pid}/testcases/import`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
    return res.json() as Promise<{ imported: number }>;
  },
  exportUrl: (pid: number) => `/api/projects/${pid}/testcases/export`,
  templateUrl: (pid: number) => `/api/projects/${pid}/testcases/template`,

  getStats: (pid: number, o?: Opts) => req<ProjectStats>(`/projects/${pid}/stats`, o),
  getCaseResults: (cid: number, o?: Opts) => req<CaseResult[]>(`/testcases/${cid}/results`, o),

  assistant: (pid: number, b: { message: string; history: AssistantTurn[] }, o?: Opts) =>
    req<AssistantReply>(`/projects/${pid}/assistant`, {
      method: "POST",
      body: JSON.stringify(b),
      ...o,
    }),
  getKnowledge: (pid: number, o?: Opts) =>
    req<{ text: string; chars: number; chunks: number }>(`/projects/${pid}/knowledge`, o),
  setKnowledge: (pid: number, text: string) =>
    req<{ chars: number; chunks: number }>(`/projects/${pid}/knowledge`, {
      method: "PUT",
      body: JSON.stringify({ text }),
    }),
  /** Add to the project's knowledge without replacing it — used by upload so the SPA
   *  never has to read back (and re-send) a multi-megabyte document. */
  appendKnowledge: (pid: number, text: string) =>
    req<{ chars: number; chunks: number }>(`/projects/${pid}/knowledge/append`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  /** Preview what the assistant's search_knowledge tool returns for a query. */
  searchKnowledge: (pid: number, query: string) =>
    req<{
      query: string;
      matched_blocks: number;
      total_blocks: number;
      hits: { line: number; heading: string; text: string }[];
      note?: string;
    }>(`/projects/${pid}/knowledge/search`, {
      method: "POST",
      body: JSON.stringify({ text: query }),
    }),
  /** Upload a spec document (docx/pdf/xlsx/html/rtf/txt/md/csv/json) and get its
   *  text back. Does not save — the caller appends it to the spec box and PUTs. */
  extractKnowledge: async (pid: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/projects/${pid}/knowledge/extract`, { method: "POST", body: fd });
    if (!res.ok) {
      // the API answers {"detail": "..."} — show that, not the raw JSON blob
      const raw = await res.text();
      let msg = raw;
      try {
        msg = (JSON.parse(raw) as { detail?: string }).detail ?? raw;
      } catch {
        /* not JSON — keep raw */
      }
      throw new Error(msg);
    }
    return res.json() as Promise<{
      filename: string;
      format: string;
      text: string;
      chars: number;
      truncated: boolean;
      warnings: string[];
    }>;
  },

  createRun: (pid: number, b: { name?: string; case_ids?: number[]; tags?: string[]; concurrency?: number }) =>
    req<Run>(`/projects/${pid}/runs`, { method: "POST", body: JSON.stringify(b) }),
  listRuns: (pid: number, o?: Opts) => req<Run[]>(`/projects/${pid}/runs`, o),
  getRun: (rid: number, o?: Opts) => req<Run>(`/runs/${rid}`, o),

  // suites
  listSuites: (pid: number, o?: Opts) => req<Suite[]>(`/projects/${pid}/suites`, o),
  createSuite: (
    pid: number,
    b: {
      name: string;
      description?: string;
      selection_mode?: "cases" | "tags";
      case_ids?: number[];
      tag_filter?: string[];
      owner_user_id?: number | null;
      runner_user_id?: number | null;
      cadence?: Cadence;
    },
  ) => req<Suite>(`/projects/${pid}/suites`, { method: "POST", body: JSON.stringify(b) }),
  updateSuite: (sid: number, b: Record<string, unknown>) =>
    req<Suite>(`/suites/${sid}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteSuite: (sid: number) => req<{ deleted: number }>(`/suites/${sid}`, { method: "DELETE" }),
  assignSuite: (sid: number, runner_user_id: number | null) =>
    req<Suite>(`/suites/${sid}/assign`, {
      method: "POST",
      body: JSON.stringify({ runner_user_id }),
    }),
  runSuite: (sid: number) => req<Run>(`/suites/${sid}/run`, { method: "POST" }),

  // notifications
  listNotifications: (o?: Opts) => req<AppNotification[]>("/notifications", o),
  unreadCount: (o?: Opts) => req<{ count: number }>("/notifications/unread-count", o),
  readNotification: (nid: number) =>
    req<{ ok: boolean }>(`/notifications/${nid}/read`, { method: "POST" }),
  readAllNotifications: () =>
    req<{ ok: boolean }>("/notifications/read-all", { method: "POST" }),
  getResults: (rid: number, o?: Opts) => req<RunResult[]>(`/runs/${rid}/results`, o),
  // 2026-10-06 人工改判：AI 判定会不准，测试工程师必须能自己拍板。
  // clear=true 表示撤销改判、回到 AI 的原判。
  overrideResult: (resId: number, b: { status?: "passed" | "failed"; reason?: string; clear?: boolean }) =>
    req<RunResult>(`/results/${resId}`, { method: "PATCH", body: JSON.stringify(b) }),
  cancelRun: (rid: number) => req<Run>(`/runs/${rid}/cancel`, { method: "POST" }),
  // undefined = every case; "failing" = everything that didn't pass; "error" = infra
  // outcomes only (timeouts, dead sessions) — a judge failure is a product finding.
  rerun: (rid: number, only?: "failing" | "error") =>
    req<Run>(`/runs/${rid}/rerun${only ? `?only=${only}` : ""}`, { method: "POST" }),
  renameRun: (rid: number, name: string) =>
    req<Run>(`/runs/${rid}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  deleteRun: (rid: number) => req<{ deleted: number }>(`/runs/${rid}`, { method: "DELETE" }),
  runExportUrl: (rid: number) => `/api/runs/${rid}/export`,

  listIssues: (pid: number, o?: Opts) => req<Issue[]>(`/projects/${pid}/issues`, o),
  createIssue: (pid: number, b: Partial<Issue>) =>
    req<Issue>(`/projects/${pid}/issues`, { method: "POST", body: JSON.stringify(b) }),
  getIssue: (iid: number, o?: Opts) => req<Issue>(`/issues/${iid}`, o),
  updateIssue: (iid: number, b: Partial<Issue>) =>
    req<Issue>(`/issues/${iid}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteIssue: (iid: number) => req<{ deleted: number }>(`/issues/${iid}`, { method: "DELETE" }),
  addComment: (iid: number, b: { body: string; author?: string }) =>
    req<IssueComment>(`/issues/${iid}/comments`, { method: "POST", body: JSON.stringify(b) }),
  listProjectFeedback: (pid: number, q?: { status?: string; category?: string }) => {
    const s = new URLSearchParams();
    if (q?.status) s.set("status", q.status);
    if (q?.category) s.set("category", q.category);
    const qs = s.toString();
    return req<FeedbackItem[]>(`/projects/${pid}/feedback${qs ? `?${qs}` : ""}`);
  },
  promoteFeedback: (fid: number) =>
    req<Issue>(`/feedback/${fid}/promote`, { method: "POST" }),

  streamUrl: (rid: number) => `/api/runs/${rid}/stream`,

  // --- auth ---
  config: () =>
    req<{
      auth_enabled: boolean;
      shared_workspace: boolean;
      public_base_url: string;
      gitlab_enabled: boolean;
      feishu_enabled: boolean;
    }>("/config"),
  me: (o?: Opts) => req<AuthUser | null>("/auth/me", o),
  login: (email: string, password: string) =>
    req<AuthUser>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => req<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  setOnboarded: () =>
    req<{ onboarded_at: string | null }>("/auth/onboarded", { method: "POST" }),
  getInvite: (token: string) =>
    req<{ email: string; is_admin: boolean; project_id: number | null }>(`/invite/${token}`),
  acceptInvite: (token: string, b: { name: string; password: string }) =>
    req<AuthUser>(`/invite/${token}/accept`, { method: "POST", body: JSON.stringify(b) }),
  forgotPassword: (email: string) =>
    req<{ ok: boolean }>("/auth/forgot", { method: "POST", body: JSON.stringify({ email }) }),
  resetPassword: (token: string, password: string) =>
    req<AuthUser>("/auth/reset", { method: "POST", body: JSON.stringify({ token, password }) }),
  changePassword: (b: { old_password: string; new_password: string }) =>
    req<{ ok: boolean }>("/auth/change-password", { method: "POST", body: JSON.stringify(b) }),

  // --- admin ---
  listUsers: (o?: Opts) => req<AdminUser[]>("/admin/users", o),
  inviteUser: (b: { email: string; is_admin?: boolean; project_id?: number; project_role?: string }) =>
    req<{ email: string; link: string; emailed: boolean }>("/admin/users/invite", {
      method: "POST",
      body: JSON.stringify(b),
    }),
  resetUserPassword: (uid: number) =>
    req<{ email: string; link: string; emailed: boolean }>(`/admin/users/${uid}/reset-password`, {
      method: "POST",
    }),
  updateUser: (uid: number, b: { is_active?: boolean; is_admin?: boolean }) =>
    req<AdminUser>(`/admin/users/${uid}`, { method: "PATCH", body: JSON.stringify(b) }),
  getAdminSettings: () =>
    req<{ llm: LlmStatus; gitlab_token_set: boolean; feishu: FeishuStatus }>("/admin/settings"),
  setGitlabToken: (token: string) =>
    req<{ gitlab_token_set: boolean }>("/admin/settings/gitlab-token", {
      method: "PUT",
      body: JSON.stringify({ token }),
    }),
  setLlmSettings: (b: LlmSettingsIn) =>
    req<LlmStatus>("/admin/settings/llm", { method: "PUT", body: JSON.stringify(b) }),
  testLlmSettings: () =>
    req<{ ok: boolean; model?: string; reply_model?: string; api_key_set?: boolean; error?: string }>(
      "/admin/settings/llm/test",
      { method: "POST" },
    ),
  setFeishuSettings: (b: FeishuSettingsIn) =>
    req<FeishuStatus>("/admin/settings/feishu", { method: "PUT", body: JSON.stringify(b) }),

  // --- project members ---
  listMembers: (pid: number, o?: Opts) => req<Member[]>(`/projects/${pid}/members`, o),
  assignableUsers: (pid: number, q: string) =>
    req<{ id: number; email: string; name: string | null }[]>(
      `/projects/${pid}/assignable-users?q=${encodeURIComponent(q)}`,
    ),
  addMember: (pid: number, b: { email: string; role: string }) =>
    req<Member>(`/projects/${pid}/members`, { method: "POST", body: JSON.stringify(b) }),
  updateMember: (pid: number, uid: number, role: string) =>
    req<Member>(`/projects/${pid}/members/${uid}`, { method: "PATCH", body: JSON.stringify({ role }) }),
  removeMember: (pid: number, uid: number) =>
    req<{ removed: number }>(`/projects/${pid}/members/${uid}`, { method: "DELETE" }),
};

export interface AuthUser {
  id: number;
  email: string;
  name: string | null;
  is_admin: boolean;
  /** null = has never finished or dismissed the guided first-run flow */
  onboarded_at: string | null;
}
export interface AdminUser extends AuthUser {
  is_active: boolean;
  last_login_at?: string | null;
}
export interface Member {
  user_id: number;
  email: string;
  name: string | null;
  role: "owner" | "editor" | "viewer";
}
