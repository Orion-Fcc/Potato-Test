import i18n from "../i18n";

export interface Project {
  id: number;
  name: string;
  base_url: string | null;
  /** 所属「大系统」（2026-10-13）。null/undefined = 不属于任何大系统（平铺显示）。 */
  group_id?: number | null;
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

/** 「大系统」容器（2026-10-13）：把若干小模块项目归到一个大系统下面。
 *  `knowledge` 是它那份**共享需规**的大小 —— 子项目检索时都会带上它。 */
export interface ProjectGroup {
  id: number;
  name: string;
  /** 挂在这个大系统下的项目数。 */
  projects: number;
  knowledge: { chunks: number; chars: number };
}

interface BrowserStateSlot {
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
interface DigestCase {
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
interface DigestGroup {
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

/** 粘进对话的一个文件（已由后端解析成文本）。 */
export interface AssistantAttachment {
  filename: string;
  /** 解析出的字符数（可能小于原文字符数，见 truncated）。 */
  chars: number;
  text: string;
  /** true = 内容太长，只带了开头部分（后端已注明）。 */
  truncated?: boolean;
  warnings?: string[];
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

/** 一条"贵"的用例（见后端 app/perf_analysis.py::CostCase）。 */
export interface CostCase {
  case_id: number;
  tokens: number;
  calls: number;
  /** 占本轮总 token 的百分数（后端已 ×100）。 */
  share: number;
}

/** 一轮的性能/成本分析（2026-10-11 起含 token 经济学）。纯计算，不调模型。 */
export interface PerfReport {
  baseline_per_step_ms: number;
  samples: number;
  flags: { case_id: number; kind: string; detail: string; note: string; ratio: number }[];
  note: string;
  token_prompt: number;
  token_cached: number;
  token_uncached: number;
  token_total: number;
  /** LLM 往返总次数 —— 实测这是成本的真正驱动（而不是每步输入量）。 */
  token_calls: number;
  cache_hit_pct: number;
  cost_cases: CostCase[];
  cost_top_share: number;
  escalated_cases: number;
  escalated_changed: number;
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

type IssueStatus = "open" | "in_progress" | "fixed" | "verified" | "closed";
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

type FeedbackCategory = "bug" | "question" | "feature" | "other";

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
  // auto（默认，自动识别）/ openai / anthropic / google / ollama
  provider: string;
  api_key_set: boolean;
  // 按用途分模型（2026-10-11）：agent 每步执行 / judge 判定 / judge_escalate 复核 /
  // narrative 失败叙述 / specgen 需规生成 / assistant 助手对话。
  // effective_* 是实际生效值（含继承默认）。
  roles?: Record<string, LlmRoleStatus>;
  // 两级判定总开关：便宜模型先判，判 failed 或闸门告警时才升级复核。
  // 默认关（要花钱），前端必须让用户显式打开。
  judge_two_tier?: boolean;
  // ★ 2026-10-11：上面这几个字段现在是**真实生效**的那一套（池的第 1 条），
  // 不再是 env / 旧单值字段 —— 后者在池非空时完全不参与运行。
  // pool_source 说明这套配置来自哪里：pool = 已保存的池；env = 池为空、回落 .env。
  pool_source?: "pool" | "env";
  pool_count?: number;
  // 平台级「高端模型」（贵、聪明，可选）。默认关。
  premium?: LlmPremiumStatus;
}

// 平台级「高端模型」的现状。2026-10-12 起：高端模型**只从模型池里选一套**
// （pool_name 指向池里某条目的 name），自己不再存连接信息。
// `usable` 不只看 enabled —— 没选池条目、或一个用途都没勾，都算"看起来配了其实没用"。
export interface LlmPremiumStatus {
  enabled: boolean;
  // 从模型池里选的那套的 name（空 = 没选）
  pool_name: string;
  // 选中的那套在池里的模型名（解析不到就是空串）—— 界面直接显示"高端模型 = X"
  resolved_model: string;
  // 勾选的用途（已按后端固定顺序归一）
  roles: string[];
  usable: boolean;
  // 真正会被它接管的用途 —— 界面用它说清"我开的高端模型管到了哪几个环节"
  applies_to: string[];
}

// 一个用途的配置现状。model/base_url/provider 为空 = 跟随默认；
// effective_* 永远有值，界面直接显示"这个用途现在实际用哪个模型"。
export interface LlmRoleStatus {
  model: string;
  base_url: string;
  provider: string;
  api_key_set: boolean;
  effective_model: string;
  effective_base_url: string;
  // 生效值来自哪一层：pool（池第 1 套）/ premium（高端模型）/ override（这张卡自己配的）。
  // 少了它用户只能看着一个模型名猜它从哪来，而猜错的表现正是"高端模型配了没生效"。
  source?: "pool" | "premium" | "override";
}

// 保存某个用途：留空 = 跟随默认；只换模型填 model 就够，换厂商才要 base_url/api_key。
export interface LlmRoleIn {
  model?: string;
  base_url?: string;
  api_key?: string;
  provider?: string;
}

export interface LlmSettingsIn {
  base_url?: string; // "" clears the override (falls back to env)
  model?: string;
  agent_model?: string;
  provider?: string;
  api_key?: string; // blank keeps the stored key
  pool?: LlmPoolIn[]; // when present it fully replaces the ordered pool
  roles?: Record<string, LlmRoleIn>; // when present it fully replaces the per-purpose map
  judge_two_tier?: boolean; // two-tier judging on/off; omitted = leave unchanged
  premium?: LlmPremiumIn; // when present it fully replaces the premium slot
}

// 保存平台级「高端模型」。整体替换语义：发上去的就是全部。
// 2026-10-12 起：高端模型只从模型池里选一套（pool_name），自己不存连接信息。
export interface LlmPremiumIn {
  enabled: boolean;
  // 指向模型池里某个条目的 name
  pool_name?: string;
  roles?: string[];
}

// 池里的一套网关（多套时按顺序 429 兜底）。api_key 是只写的：留空表示
// "保持同位置已存的那把钥匙"（界面只显示掩码，不允许被编辑动作意外清空）。
export interface LlmPoolIn {
  name?: string;
  base_url: string;
  model: string;
  agent_model?: string;
  provider?: string;
  api_key?: string;
}

export interface LlmPoolEntry {
  name: string;
  base_url: string;
  model: string;
  agent_model: string;
  provider: string;
  api_key_set: boolean;
}

// 探活结果：不只是"通不通"，还把实测到的协议与能力一起报出来 ——
// 换厂商之后"到底能不能跑、哪个特性不可用"看这几个布尔值就够了。
export interface LlmTestResult {
  /** 测的是哪一套（池条目的 name，或 role:override）。让"测通了"不至于不知道测的谁。 */
  name: string;
  model?: string;
  reply_model?: string;
  api_key_set?: boolean;
  base_url?: string;
  protocol?: string;
  json_object?: boolean;
  json_schema?: boolean;
  tools?: boolean;
  vision?: boolean;
  probed?: boolean;
  ok: boolean;
  error?: string;
}

// 连通性检测报告：一次性测「平台可能调用的每一套模型」，逐条列出。
export interface LlmTestReport {
  ok: boolean;
  all_ok: boolean;
  results: LlmTestResult[];
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
// 2026-10-09 加 blocked：有些用例 agent 跑不了（需要人工先做一步、依赖拿不到的数据…），
// 人要能把它标出来并写清原因。与 deprecated 的区别见后端 app/schemas.py::CaseStatus。
export type CaseStatus = "draft" | "active" | "deprecated" | "blocked";

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
  /** status="blocked" 时为什么跑不了（2026-10-09）。
   *  必须由人能直接看到：只显示一个"受阻"标记的话，还得点进去才知道原因，
   *  而原因才是决定下一步做什么的东西。 */
  blocked_reason: string;
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

interface RunSummary {
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
  // null = 沿用全局 CASE_RETRIES（不是 0）。详见后端 engine.effective_retries。
  retries?: number | null;
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
  trigger?: "manual" | "suite" | "schedule";
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
  /** 2026-10-07 失败归因：这次失败是谁的锅。与 root_cause 分工不同 —— 那个回答
   *  "用例为什么判失败"，这个回答"这次失败能不能算被测系统的缺陷"。
   *  取值 system / setup / transient / agent / unknown；null 或缺失 = 历史数据未判定。
   *  只有 attribution_is_real_defect 为 true 才允许提缺陷单。 */
  attribution?: string | null;
  attribution_label?: string | null;
  attribution_is_real_defect?: boolean;
  verdict_evidence?: number[];
  final_answer: string | null;
  /** AI-written bug description, present only for failed/errored cases. */
  failure_narrative?: FailureNarrative | null;
  account_label?: string | null;
  latency_ms: number;
  /** LLM token 账本（2026-10-09）。null/缺失 = 没测到（网关不回 usage，或埋点前的历史行），
   *  界面要显示"—"而不是 0 —— 把"未测量"画成"零消耗"会让人低估真实成本。 */
  usage?: TokenUsage | null;
  error: string | null;
  // 2026-10-06 人工改判。verdict_override 非空 => 这条结论是人改的，不是 AI 判的。
  // 界面必须把两者区分开，否则通过率/真缺陷率这些数字没法信。
  verdict_override?: string | null;
  override_reason?: string | null;
  override_by?: string | null;
  override_at?: string | null;
  /** 改判前 AI 的原始结论（撤销改判时用来还原）。 */
  original_status?: string | null;
  /** 两级判定（2026-10-11）：这条结论是**复核层**给出的（初判判失败/闸门报警才触发）。
   *  必须能看到 —— 复核用的是更贵的模型，"哪些结论真动用了复核"决定这套机制是省钱还是烧钱。 */
  judge_escalated?: boolean;
  /** 初判的结论（passed/failed/error:XXX），用于统计"升级改判率"。 */
  judge_escalated_from?: string | null;
}

/** LLM token 消耗（2026-10-09）。键名刻意取短：它跟着每条结果落库。
 *  只被 RunResult.usage 引用，故不导出（多一个导出就多一个别人以为能依赖的面）。 */
interface TokenUsage {
  /** 输入 token（含缓存命中的部分）。 */
  prompt: number;
  /** 输出 token。 */
  completion: number;
  /** 其中命中 prompt 缓存的部分 —— 缓存命中的计价通常远低于新输入。 */
  cached: number;
  total: number;
  /** LLM 往返次数。与 tokens 一起看才有意义：少量大调用和大量小调用的优化方向不同。 */
  calls: number;
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

/** 请求失败时抛出的错误。**故意不导出**：它是本模块的内部实现细节，
 *  外部只需要 `catch (e) { e.message }`（见 lib/errors.ts 的 messageOf）。
 *  导出它只会诱使别处写 `instanceof ApiError` —— 而"超时/断网/4xx"抛的都是它，
 *  靠类型区分不出"该不该重试"。 */
class ApiError extends Error {
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
    // ★ FormData 不能带 content-type（2026-10-12 修）。
    //
    // 这里原来无条件设 `content-type: application/json`，于是任何走 FormData 的
    // 上传都会变成"body 是 multipart、头却说是 JSON" —— 后端按 JSON 解析、
    // 找不到字段，报一个和真实原因毫无关系的 422（实测症状：
    // 附件 chip 上显示 `xx.md: Field required`）。
    //
    // 必须**完全不设**这个头，让浏览器自己带上 multipart 的 boundary ——
    // 手写 `multipart/form-data` 会漏掉 boundary，同样解析不了。
    const isForm = typeof FormData !== "undefined" && rest.body instanceof FormData;
    const res = await fetch(`/api${path}`, {
      // same-origin cookies are how the session is carried; without this an
      // authenticated deployment silently degrades to anonymous.
      credentials: "same-origin",
      ...rest,
      signal: ctrl.signal,
      headers: isForm ? { ...headers } : { "content-type": "application/json", ...headers },
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

// ---- 从需规生成用例（2026-10-09）------------------------------------------
// 一条待审阅的生成用例。字段与 TestCase 对齐，但它**还没入库** ——
// 所以刻意不是 TestCase 的子类型：把它当 TestCase 用会让人以为它已经有 id 和 case_key。
export interface SpecDraftCase {
  name: string;
  prompt: string;
  expected: string;
  module: string;
  type: string;
  priority: string;
  precondition: string;
}

/** 一个可分批生成的模块（由模型把原始章节标题归并而成）。 */
export interface SpecModule {
  name: string;
  titles: string[];
  /** false = 这一节通篇在浏览器里观测不到（性能指标/数据库约束/部署运维），不该生成用例。 */
  testable: boolean;
  reason: string;
}

export interface SpecOutline {
  headings: string[];
  modules: SpecModule[];
  has_knowledge: boolean;
  planned?: boolean;
  /** true = 模型归并失败、已退回"每个标题一个模块"。界面要如实说明，
   *  别让用户以为文档就这么碎。 */
  degraded?: boolean;
  /** true = 这次是**缓存命中**（没花 token）。 */
  cached?: boolean;
  // ---- 长文档的分批进度（2026-10-09）------------------------------------
  // ★ 这三个字段是"586 个标题的文档能不能被走完"的关键。一次只规划 60 个标题，
  // 所以界面必须能显示"已规划 60 / 586"，并提供"继续规划下一批"。
  // 少了它们，用户只能看到文档的 10%，而且完全不知道剩下的去哪儿了。
  /** 文档里的章节标题总数。 */
  total?: number;
  /** 已经规划到第几个标题（下一批从它开始）。 */
  planned_until?: number;
  /** 还有没规划的标题。 */
  has_more?: boolean;
  batch_from?: number;
  batch_to?: number;
  /** true = 这一批**规划失败**（网关报错等），没有推进。
   *  界面据此停在错误上，而不是继续给"下一批"按钮 ——
   *  实测网关持续 500 时，那个按钮会变成一个点一次失败一次的陷阱。 */
  failed?: boolean;
  usage?: TokenUsage | null;
  error?: string | null;
  note?: string;
}

/** 直接把一份需规文档发过去的结果（2026-10-09）。 */
export interface SpecUploadResult {
  filename: string | null;
  format: string;
  chars: number;
  /** true = 文档太长被截断了。仍然"上传成功"，但生成出来的用例会缺一块。 */
  truncated: boolean;
  /** 解析期的提醒（空文档、图片型 PDF、没有标题…）。必须显示给用户 ——
   *  否则"上传成功却没有模块"看起来像功能坏了。 */
  warnings: string[];
  appended: boolean;
  headings: number;
  modules: SpecModule[];
  planned_until: number;
  has_more: boolean;
  usage?: TokenUsage | null;
  error?: string | null;
  /** true = 模型归并失败，模块是"一个标题一个"的兜底分批。 */
  degraded?: boolean;
}

export interface SpecDraftResult {
  module: string;
  titles: string[];
  cases: SpecDraftCase[];
  /** 被解析层丢弃的用例及原因。不静默丢弃 —— 用户要靠它判断这次生成的整体质量。 */
  dropped: string[];
  count: number;
  usage?: TokenUsage | null;
  source: string;
  error?: string | null;
}

// ---- 项目文档（2026-10-12）------------------------------------------------
/** 一份项目文档。`html` 是富文本，后端存前已净化。 */
export interface ProjectDoc {
  /** 0 = 还没保存过的内置占位（界面据此显示"AI 生成"按钮而不是"删除"）。 */
  id: number;
  /** overview（项目总览）/ analysis（需求分析）/ flow（流程图）/ custom */
  kind: string;
  title: string;
  html: string;
  /** AI 生成时依据的章节标题 —— 用户据此核对，而不是把 AI 的话当结论。 */
  sources: string[];
  /** "ai" = 生成后未改；"human" = 人改过；"" = 空 */
  origin: string;
  /** ★ 「只有我能改」（2026-10-12）。
   *  false 时助手**不能写**这份文档（读不受影响 —— 它永远能读全部）。
   *  服务端强制，界面上的开关只是提示。 */
  ai_editable: boolean;
  /** ★ 上传的**原件**（2026-10-16）。非空才能显示「原件」入口。
   *  解析出来的正文是有损的（PDF 丢版面、docx 丢标题层级），原件才是格式的权威。 */
  source_url: string;
  source_name: string;
  source_bytes: number;
  updated_at: string | null;
}

/** AI 生成的一节。 */
export interface AnalyzeSection {
  key: string;
  title: string;
  html: string;
  sources: string[];
}

export interface AnalyzeDocsResult {
  ok: boolean;
  /** ok=false 时的原因（网关挂了、文档为空…）。必须显示给用户。 */
  error?: string;
  doc?: ProjectDoc;
  sections?: AnalyzeSection[];
  usage?: TokenUsage | null;
  /** AI 实际看了多少个章节标题、多少字符的正文 —— 让用户知道"它看了多少"。 */
  headings_used?: number;
  doc_chars?: number;
  /** 本轮真正喂给模型的**需规原文**合计字数（各节检索片段之和）。 */
  recalled_chars?: number;
  /** true = 追加在原有内容后面（没覆盖用户改过的部分）。 */
  appended?: boolean;
}

// ---- 项目概况面板（2026-10-12）--------------------------------------------
/** 一条需求的覆盖情况。`uncovered=true`（没有任何用例引用）是最该被看见的一格。 */
export interface RequirementCoverage {
  req_id: string;
  /** 需求原文（从知识库里摘出来的定义行）。空串 = 需规里找不到，本身是信息。 */
  excerpt: string;
  case_count: number;
  cases: string[];
  verified_total: number;
  verified_pass: number;
  verified_fail: number;
  verified_rate: number | null;
  auto_total: number;
  auto_pass: number;
  auto_fail: number;
  auto_rate: number | null;
  uncovered: boolean;
}

export interface ModuleCoverageRow {
  module: string;
  case_total: number;
  /** 没有填需求 ID 的用例数 —— 追溯粒度粗不粗看这个。 */
  no_refs: number;
  with_refs: number;
  verified_total: number;
  verified_pass: number;
  verified_fail: number;
  verified_rate: number | null;
  awaiting_review: number;
  auto_pass: number;
  auto_fail: number;
}

export interface ProjectInsights {
  requirements: RequirementCoverage[];
  requirement_summary: {
    total: number;
    covered: number;
    uncovered: number;
    cases_with_refs: number;
    cases_total: number;
    top_req: string;
    top_req_cases: number;
  };
  modules: ModuleCoverageRow[];
  rules: Array<{
    req_id: string;
    kind: string;
    text: string;
    heading: string;
    cases: string[];
    case_count: number;
  }>;
  apis: Array<{
    method: string;
    path: string;
    hits: number;
    writable: boolean;
    origin: string;
    last_seen: string;
  }>;
  credentials: Array<{
    id: number;
    role: string;
    label: string;
    is_active: boolean;
    healthy: boolean;
    last_error: string;
    last_checked_at: string;
    expires_at: string;
  }>;
  changes: Array<{
    id: number;
    case_key: string;
    case_name: string;
    field: string;
    before: string;
    after: string;
    source: string;
    created_at: string;
  }>;
  failures: {
    by_attribution: Array<{ attribution: string; count: number }>;
    suspects: Array<{
      result_id: number;
      case_key: string;
      case_name: string;
      attribution: string;
      overridden_to: string;
      reason: string;
    }>;
    suspect_total: number;
  };
}

// ---- 用例优化提案（2026-10-12）--------------------------------------------
/** 一条 AI 提案。`status` 为 pending 时才可采纳/驳回。 */
export interface CaseProposal {
  id: number;
  /** add（补用例）/ update（改用例）/ remove（删用例） */
  action: string;
  /** 受影响的用例编号（add 时为 null） */
  case_key: string | null;
  case_id: number | null;
  summary: string;
  reason: string;
  /** 依据：需规片段 / 与哪几条用例重复。**用户核对 AI 的唯一手段。** */
  evidence: string[];
  /** add 时是完整草稿；update 时只含要改的字段 */
  payload: Record<string, unknown>;
  /** 这条提案涉及的业务流程图（Mermaid 源码）。同模块的提案共享同一张。 */
  mermaid: string;
  /** 这条用例该用哪个账号跑（项目里真实存在的 credential.role；空=默认账号）。 */
  role: string;
  status: string;
  applied_case_id: number | null;
  created_at: string | null;
  decided_at: string | null;
}

export interface CaseProposalList {
  proposals: CaseProposal[];
  counts: { pending: number; accepted: number; rejected: number };
  pending_by_action: Record<string, number>;
}

export interface GenerateProposalsResult {
  ok: boolean;
  error?: string;
  created: number;
  modules_scanned?: number;
  modules_failed?: number;
  /** 点名失败的模块 —— 只说数量用户无法判断要不要补。 */
  failed_modules?: string[];
  usage?: TokenUsage | null;
}

export interface DecideProposalsResult {
  ok: boolean;
  applied: Array<Record<string, unknown>>;
  /** 没落地的条目及原因 —— 必须显示，否则用户以为全成功了。 */
  skipped: Array<{ id: number; why: string }>;
}

/** 一个模块的覆盖率。`verified_*` 只统计人工确认过的结果。 */
export interface ModuleCoverage {
  module: string;
  verified_pass: number;
  verified_fail: number;
  verified_total: number;
  verified_rate: number | null;
  auto_pass: number;
  auto_fail: number;
  auto_total: number;
  auto_rate: number | null;
  awaiting_review: number;
  product_defects: number;
}

export interface VerifiedCoverage {
  modules: ModuleCoverage[];
  summary: {
    verified_total: number;
    verified_pass: number;
    verified_rate: number | null;
    awaiting_review: number;
    product_defects: number;
  };
}

// ---- 需规文件格式（2026-10-12）--------------------------------------------
/** 由后端按 `docparse` 的真实支持集合给出 —— 前端不写死，避免两边漂移。 */
export interface KnowledgeFormats {
  /** 纯文本类扩展名，如 `.md`、`.csv` */
  text: string[];
  /** 需要解析的 Office/PDF/HTML/RTF 扩展名 */
  office: string[];
  /** **读不了**的老二进制格式（.doc/.xls/.ppt）。明确列出来，用户才知道为什么失败。 */
  legacy_unsupported: string[];
  /** 直接喂给 `<input accept>` 的字符串 */
  accept: string;
  label: string;
}

// ---- 后端接口探索（2026-10-09）--------------------------------------------
/** 从**页面实际流量**里学到的后端接口。写接口只能看，不能被用例调用。 */
export interface ApiEndpoint {
  id: number;
  method: string;
  /** 归一化路径，具体 ID 已换成 {id} —— 所以翻两页不会多出两条"不同"接口。 */
  path: string;
  /** 原始样例 URL，便于核对归一化有没有做错。 */
  sample: string;
  /** 参数名 → 值的形状（int/str/opaque…）。**只有形状，没有值**。 */
  query: Record<string, string>;
  body_fields: string[];
  /** 被观察到的次数：多说明它是主干接口（菜单、列表）。 */
  hits: number;
  writable: boolean;
  origin: string;
  last_seen: string | null;
}

export interface ApiEndpointList {
  endpoints: ApiEndpoint[];
  total: number;
  note: string;
}

export interface ApiProbeResult {
  ok: boolean;
  status: number;
  json: unknown;
  text: string;
  content_type: string;
  error: string;
  url: string;
  elapsed_ms: number;
  origin?: string;
  method?: string;
}

export const api = {
  listProjects: (o?: Opts) => req<Project[]>("/projects", o),
  /** 单个项目详情。形状与 listProjects 的每一项逐字段一致（后端共用同一个 helper）。 */
  getProject: (pid: number, o?: Opts) => req<Project>(`/projects/${pid}`, o),

  /** 项目概况面板：需求覆盖矩阵 / 规则 / 接口 / 账号 / 变更 / 失败台账。
   *  纯读库计算，不发 LLM 调用。 */
  projectInsights: (pid: number, o?: Opts) => req<ProjectInsights>(`/projects/${pid}/insights`, o),

  // ---- 用例优化提案（2026-10-12）----------------------------------------
  /** 提案清单 + 统计。 */
  listCaseProposals: (pid: number, status?: string, o?: Opts) =>
    req<CaseProposalList>(
      `/projects/${pid}/case-proposals${status ? `?status=${status}` : ""}`,
      o,
    ),
  /** 让 AI 按项目文档审一遍用例库，产出提案（要跑模型，比较慢）。 */
  generateCaseProposals: (pid: number, b: { replace_pending?: boolean } = {}) =>
    req<GenerateProposalsResult>(`/projects/${pid}/case-proposals/generate`, {
      method: "POST",
      body: JSON.stringify(b),
      timeoutMs: 600_000,
    }),
  /** 采纳/驳回若干条提案。**只有这个动作会改用例库。** */
  decideCaseProposals: (pid: number, ids: number[], decision: "accept" | "reject") =>
    req<DecideProposalsResult>(`/projects/${pid}/case-proposals/decide`, {
      method: "POST",
      body: JSON.stringify({ ids, decision }),
      timeoutMs: 120_000,
    }),
  /** 人工确认过的通过率（按模块）。 */
  verifiedCoverage: (pid: number, o?: Opts) =>
    req<VerifiedCoverage>(`/projects/${pid}/verified-coverage`, o),

  // ---- 项目文档：需求分析 / 总览 / 流程图（2026-10-12）------------------
  /** 项目文档列表（含内置三种的占位）。 */
  listDocs: (pid: number, o?: Opts) => req<{ docs: ProjectDoc[] }>(`/projects/${pid}/docs`, o),
  /** 新建或更新一份文档。正文会被后端净化（去掉脚本等）。 */
  saveDoc: (pid: number, b: { id?: number; kind: string; title: string; html: string; ai_editable?: boolean }) =>
    req<ProjectDoc & { dropped_content: boolean }>(`/projects/${pid}/docs`, {
      method: "PUT",
      body: JSON.stringify(b),
    }),
  /** 让 AI 把整份需规读成项目概况并存成可编辑文档（要跑模型，比较慢）。 */
  analyzeDocs: (pid: number, b: { kind?: string; replace?: boolean; full?: boolean } = {}) =>
    req<AnalyzeDocsResult>(`/projects/${pid}/docs/analyze`, {
      method: "POST",
      body: JSON.stringify(b),
      timeoutMs: 300_000,
    }),
  deleteDoc: (pid: number, id: number) =>
    req<{ deleted: number }>(`/projects/${pid}/docs/${id}`, { method: "DELETE" }),
  /** 切换「只有我能改」（AI 不可写）。读不受影响 —— 助手永远能读全部。 */
  setDocPermission: (pid: number, id: number, aiEditable: boolean) =>
    req<ProjectDoc>(`/projects/${pid}/docs/${id}/permission`, {
      method: "PATCH",
      body: JSON.stringify({ ai_editable: aiEditable }),
    }),
  /** 把一个文件解析成**可查看可编辑的文档**（也同时可再入知识库）。
   *
   *  为什么要有它：上传原来只进知识库（AI 检索用的分块），
   *  文档页里看不到 —— 用户反馈「上传的需归文档无法查看修改」就是这个。
   */
  docFromFile: async (
    pid: number,
    file: File,
    aiEditable: boolean,
  ): Promise<{ ok: boolean; created: boolean; doc: ProjectDoc; chars: number; truncated: boolean; warnings: string[] }> => {
    const fd = new FormData();
    fd.append("file", file);
    // 用户在上传处选的"助手能不能改"。
    // ★ 必须走 **Form 字段**：这个端点是 multipart，普通标量参数在 FastAPI 里
    // 会被当成 **query** 而静默忽略（踩过：勾了"只有我能改"却没锁上，且无报错）。
    fd.append("ai_editable", aiEditable ? "1" : "0");
    return req(`/projects/${pid}/docs/from-file`, {
      method: "POST",
      body: fd,
      timeoutMs: 180_000,
    });
  },
  /** 上传一张图片，返回可直接放进正文的 URL。 */
  uploadDocImage: async (pid: number, file: File): Promise<{ url: string; bytes: number }> => {
    const fd = new FormData();
    fd.append("file", file);
    return req<{ url: string; bytes: number }>(`/projects/${pid}/docs/image`, {
      method: "POST",
      body: fd,
      timeoutMs: 120_000,
    });
  },

  // ---- 后端接口探索（2026-10-09）---------------------------------------
  /** 从页面流量里学到的后端接口。写接口会被标记 —— 它们只能看，不能被调用。 */
  listApiEndpoints: (pid: number, writable?: "0" | "1", o?: Opts) =>
    req<ApiEndpointList>(
      `/projects/${pid}/api-endpoints${writable ? `?writable=${writable}` : ""}`,
      o,
    ),
  /** 只读重放一个**已观察到**的接口，用来核对数据。写方法与没见过的路径都会被拒。 */
  probeApiEndpoint: (
    pid: number,
    b: { method: string; path: string; params?: Record<string, unknown> },
  ) =>
    req<ApiProbeResult>(`/projects/${pid}/api-endpoints/probe`, {
      method: "POST",
      body: JSON.stringify(b),
      timeoutMs: 60_000,
    }),

  // ---- 从需规文档生成用例（2026-10-09）----------------------------------
  /** 列出可按哪些模块/章节分批生成。plan=false 时不调模型（零 token）。
   *
   *  同一批的后端结果是**缓存**的 —— 所以重复进页面/刷新不会再花 token，
   *  也不会出现"刷新一下分批全变了"。`refresh=true` 才强制重新分批，
   *  而且只有用户主动点「重新分批」时才该传它。 */
  specOutline: (pid: number, plan = true, refresh = false) =>
    req<SpecOutline>(
      `/projects/${pid}/spec/outline?plan=${plan ? "true" : "false"}${refresh ? "&refresh=true" : ""}`,
    ),
  /** 从第 `start` 个标题开始规划下一批（长文档用）。
   *  586 个标题的文档一批只规划 60 个，所以要逐批往下推 ——
   *  没有这个入口的话，用户只能看到文档的 10%，而且不知道剩下的去哪儿了。 */
  specOutlineFrom: (pid: number, start: number) =>
    req<SpecOutline>(`/projects/${pid}/spec/outline?plan=true&start=${start}`),
  /** 直接把一份需规文档发给它：入库 + 规划一步完成（2026-10-09）。
   *  支持 Word / PDF / Excel / Markdown / txt / csv / html / rtf。 */
  specUpload: async (pid: number, file: File, append = true): Promise<SpecUploadResult> => {
    const fd = new FormData();
    fd.append("file", file);
    return req<SpecUploadResult>(`/projects/${pid}/spec/upload?append=${append ? "true" : "false"}`, {
      method: "POST",
      body: fd,
      // 大文档解析 + 一次规划，远超默认 30s。
      timeoutMs: 300_000,
    });
  },
  /** 针对一个模块生成一批**草稿**（不落库，先给用户看）。 */
  specDraft: (pid: number, b: { module: string; titles: string[] }) =>
    req<SpecDraftResult>(`/projects/${pid}/spec/draft`, {
      method: "POST",
      body: JSON.stringify(b),
      // 生成一次要跑模型，远超默认 30s 预算 —— 超时会看起来像"生成失败"。
      timeoutMs: 300_000,
    }),
  /** 把审阅过的草稿落库。activate=false（默认）时落成 draft，不进回归集。 */
  specAccept: (
    pid: number,
    b: { cases: SpecDraftCase[]; module?: string; source?: string; activate?: boolean },
  ) =>
    req<{ created: { id: number; case_key: string; name: string }[]; count: number; status: string }>(
      `/projects/${pid}/spec/accept`,
      { method: "POST", body: JSON.stringify(b) },
    ),
  /** 批量改用例状态（草稿审阅用：一次改一批，而不是 120 次请求）。
   *  status="blocked" 时 reason 必填 —— 后端会拒空原因。 */
  setCasesStatus: (pid: number, ids: number[], status: string, reason?: string) =>
    req<{ requested: number; found: number; changed: number; status: string }>(
      `/projects/${pid}/testcases/status`,
      { method: "POST", body: JSON.stringify({ ids, status, reason: reason ?? "" }) },
    ),
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
      // 所属大系统：传 null = 移出大系统；不传 = 不改（后端按 model_fields_set 判断）。
      group_id?: number | null;
    },
  ) =>
    req<Project>(`/projects/${pid}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteProject: (pid: number) =>
    req<{ deleted: number }>(`/projects/${pid}`, { method: "DELETE" }),
  // ── 「大系统」容器（2026-10-13）──────────────────────────────────────
  // 把若干小模块项目归到一个大系统下面；大系统自己可以放**共享需规**，
  // 子项目检索时自动带上它（见后端 app/knowledge.py::effective_condition）。
  listProjectGroups: (o?: Opts) => req<ProjectGroup[]>("/project-groups", o),
  createProjectGroup: (name: string) =>
    req<ProjectGroup>("/project-groups", { method: "POST", body: JSON.stringify({ name }) }),
  updateProjectGroup: (gid: number, patch: { name?: string }) =>
    req<{ id: number; name: string }>(`/project-groups/${gid}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  deleteProjectGroup: (gid: number) =>
    req<{ deleted: boolean }>(`/project-groups/${gid}`, { method: "DELETE" }),
  getGroupKnowledge: (gid: number, o?: Opts) =>
    req<{ text: string; chars: number; chunks: number }>(`/project-groups/${gid}/knowledge`, o),
  setGroupKnowledge: (gid: number, text: string) =>
    req<{ ok: boolean; chunks: number; chars: number }>(`/project-groups/${gid}/knowledge`, {
      method: "PUT",
      body: JSON.stringify({ text }),
    }),
  /** 上传文件进大系统的**共享**需规（2026-10-17）。
   *  服务端解析后**追加**到共享那份（不是替换 —— 那是 PUT 的事），
   *  并把合并后的全文回传，界面直接拿它刷新编辑框。 */
  uploadGroupKnowledge: async (gid: number, files: File[]) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    const res = await fetch(`/api/project-groups/${gid}/knowledge/upload`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
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
      ok: boolean;
      added: { filename: string; chars: number; format: string; bytes: number }[];
      warnings: string[];
      text: string;
      chars: number;
      chunks: number;
    }>;
  },
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

  assistant: (pid: number, b: { message: string; history: AssistantTurn[]; attachments?: AssistantAttachment[] }, o?: Opts) =>
    req<AssistantReply>(`/projects/${pid}/assistant`, {
      method: "POST",
      body: JSON.stringify(b),
      // 附件可能很长（一份需规几万字），对话这一路要给足时间
      timeoutMs: 300_000,
      ...o,
    }),
  /** 解析一个要粘进对话的文件 → 文本。对话那一路只传文本，不传文件字节。 */
  parseAssistantAttachment: async (pid: number, file: File): Promise<AssistantAttachment> => {
    const fd = new FormData();
    fd.append("file", file);
    return req<AssistantAttachment>(`/projects/${pid}/assistant/parse`, {
      method: "POST",
      body: fd,
      timeoutMs: 180_000,
    });
  },
  getKnowledge: (pid: number, o?: Opts) =>
    req<{ text: string; chars: number; chunks: number }>(`/projects/${pid}/knowledge`, o),
  /** 后端**实际支持**的需规文件格式。前端不写死这份清单 —— 会漂移。 */
  knowledgeFormats: (pid: number, o?: Opts) =>
    req<KnowledgeFormats>(`/projects/${pid}/knowledge/formats`, o),
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

  createRun: (pid: number, b: { name?: string; case_ids?: number[]; tags?: string[]; concurrency?: number; retries?: number }) =>
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
  /** 这一轮的耗时/成本分析（纯计算，不调模型）。 */
  runPerf: (rid: number, o?: Opts) => req<PerfReport>(`/runs/${rid}/perf`, o),
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
  // ★ 池为空时这里回一条从 .env 读出来的**回显**条目（seeded=true），而不是空列表 ——
  // 空列表会让界面显示"没有配置"，而实际上平台正在用 .env 那一套跑。
  getLlmPool: () =>
    req<{ pool: LlmPoolEntry[]; count: number; source: "pool" | "env"; seeded: boolean }>(
      "/admin/settings/llm/pool",
    ),
  testLlmSettings: (names?: string[]) =>
    req<LlmTestReport>("/admin/settings/llm/test", {
      method: "POST",
      body: names && names.length ? JSON.stringify({ names }) : undefined,
    }),
  // 各角色 token 占比：真实实测（执行流 by_role 聚合）优先，无数据回退结构估算。
  getLlmTokenShare: () =>
    req<{
      measured: Record<string, number>;
      estimate: Record<string, number>;
      has_data: boolean;
      runs: number;
      total_tokens: number;
    }>("/admin/settings/llm/token-share"),
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

