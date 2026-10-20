import i18n from "i18next";
import { initReactI18next } from "react-i18next";

// Keys ARE the English source strings; the `zh` resource overrides them. English is
// the fallback (returns the key). Toggle persists in localStorage.
const zh: Record<string, string> = {
  // sidebar / nav / chrome
  Projects: "项目",
  Overview: "概览",
  Assistant: "助手",
  // 失败清单（2026-10-06）
  "失败清单": "失败清单",
  "未通过": "未通过",
  "归为": "归为",
  "类原因": "类原因",
  "条待人工确认": "条待人工确认",
  "补全分类": "补全分类",
  "正在补分类…": "正在补分类…",
  "没有未通过的用例": "没有未通过的用例",
  涉及: "涉及",
  "条用例": "条用例",
  共: "共",
  "次失败": "次失败",
  失败: "失败",
  次: "次",
  "根因冲突，需人工判断": "根因冲突，需人工判断",
  "同一组里判定器给出了不同根因": "同一组里判定器给出了不同根因",
  "已按人工确认处理，不要按多数决定动作。": "已按人工确认处理，不要按多数决定动作。",
  "清单按相同原因合并；分不清原因的会单独列出，不做猜测。": "清单按相同原因合并；分不清原因的会单独列出，不做猜测。",
  "用例通过后会自动从这里消失，不需要手工维护。": "用例通过后会自动从这里消失，不需要手工维护。",
  "让内置助手按清单改用例：在助手页说「按失败清单把该改的用例改掉」即可。": "让内置助手按清单改用例：在助手页说「按失败清单把该改的用例改掉」即可。",
  // 用例改动审计（2026-10-08）
  "用例改动记录": "用例改动记录",
  "最近 {{n}} 条": "最近 {{n}} 条",
  "{{n}} 条由助手(AI)改动": "{{n}} 条由助手(AI)改动",
  "还没有用例改动": "还没有用例改动",
  "助手或人工改过用例字段后，改动会带审计留痕显示在这里。": "助手或人工改过用例字段后，改动会带审计留痕显示在这里。",
  "改前": "改前",
  "改后": "改后",
  // nav groups
  Workspace: "工作区",
  "Test Assets": "测试资产",
  Results: "结果",
  // kpi subtitles (note: `healthy` is defined once, further down, and is shared
  // by both the KPI subtitle and the environment-status chip)
  "needs attention": "需关注",
  failing: "失败",
  "no completed run yet": "暂无已完成的运行",
  "median step time": "步骤耗时中位数",
  "nothing flaky": "无不稳定用例",
  "passed only after retry": "重试后才通过",
  "pass rate": "通过率",
  "Thinking…": "思考中…",
  Actions: "执行的操作",
  "Clear chat": "清空对话",
  chars: "字符",
  "Upload docs": "上传文档",
  "Reading…": "解析中…",
  "Imported": "已导入",
  truncated: "已截断",
  "Reads your spec, operates Potato Test (any API), and can create cases.":
    "会读你的需规、能操控平台任意接口、还能创建用例。",
  "Ask it to inspect runs, or draft cases from your spec.":
    "可以问运行情况，也可以让它按需规起草用例。",
  "根据需规，给结算页补 3 条用例": "根据需规，给结算页补 3 条用例",
  "Ask the assistant… (Enter to send, Shift+Enter for newline)":
    "向助手提问…（Enter 发送，Shift+Enter 换行）",
  Send: "发送",
  "Test Cases": "测试用例",
  Runs: "运行",
  Compare: "对比",
  Issues: "问题",
  Feedback: "反馈",
  "+ New case": "+ 新建用例",
  "New case": "新建用例",
  "All types": "全部类型",
  Settings: "设置",
  // feedback panel
  Category: "分类",
  Project: "项目",
  Issue: "问题单",
  From: "来源",
  When: "时间",
  Resolved: "已解决",
  Ignored: "已忽略",
  Problem: "问题",
  "No feedback yet.": "暂无反馈。",
  "Promote to issue": "转为问题单",
  "Promoted to issue #{{id}}": "已转为问题单 #{{id}}",
  // feishu bot settings
  "Feishu bot": "飞书机器人",
  "Collect problems from a Feishu chat into Feedback / Issues. Configure the self-built app here.":
    "将飞书群中的问题收集为反馈与问题单。请在此配置自建应用。",
  "Long-connection mode needs no callback URL — just the app credentials below.":
    "长连接模式无需回调 URL，只需填写下方的应用凭据。",
  "(Webhook mode only) Event subscription URL:": "（仅 Webhook 模式）事件订阅 URL：",
  "App ID": "App ID",
  "App Secret": "App Secret",
  "App Secret (set — leave blank to keep)": "App Secret（已设置，留空则保持不变）",
  "Verification Token": "Verification Token",
  "Verification Token (set — leave blank to keep)": "Verification Token（已设置，留空则保持不变）",
  "API base": "API 域名",
  "Auto-answer context-detected questions (not just @-mentions)":
    "自动回答根据上下文识别到的问题（不限于 @机器人）",
  "Bot credentials are set.": "机器人凭据已配置。",
  "Bot not configured yet.": "机器人尚未配置。",
  "Secrets stored encrypted; never returned. Leave the Encrypt Key blank in the Feishu console.":
    "密钥加密存储且不会返回明文；飞书开放平台的 Encrypt Key 请留空。",
  'Bind a Feishu group to this project: problems raised there attach here, and runs/results route to it. Or send "@bot 绑定 <project>" in the group.':
    "将飞书群绑定到本项目：群内反馈的问题会归入此项目，运行与结果也会推送到该群。也可在群里发送「@机器人 绑定 <项目名>」。",
  "Feishu group chat_id": "飞书群 chat_id",
  "Feishu Bitable link (feedback mirrors here)": "飞书多维表格链接（反馈将同步至此）",
  "Feishu Bitable link (bound — paste to change)": "飞书多维表格链接（已绑定，粘贴新链接可更换）",
  Docs: "文档",
  Support: "支持",
  "Local instance": "本地实例",
  "no sign-in required": "无需登录",
  "Go to…": "快速跳转…",
  "Collapse sidebar": "收起侧边栏",
  "Expand sidebar": "展开侧边栏",
  "New project": "新建项目",

  // projects home
  "Search projects": "搜索项目",
  "Potato Test runs your natural-language test cases in a real browser. Pick a project to begin.":
    "Potato Test 在真实浏览器中执行自然语言测试用例。请选择一个项目开始。",
  Name: "名称",
  "Base URL (optional)": "基础 URL（可选）",
  Create: "创建",
  Cancel: "取消",
  "logged in": "已登录",
  cases: "用例",
  case: "用例",
  pass: "通过",
  "no base url": "未设置基础 URL",

  // overview
  "Suite health at a glance.": "测试套件健康状况一览。",
  "Test cases": "测试用例",
  "enabled / total": "启用 / 总数",
  "Last pass rate": "上次通过率",
  "Last p50 latency": "上次 p50 耗时",
  "Flaky (last run)": "不稳定用例（上次运行）",
  "No runs yet": "暂无运行",
  "Add test cases, then run them to see pass-rate trends and suite health here.":
    "先添加并运行测试用例，此处将展示通过率趋势与套件健康状况。",
  "Go to Test Cases →": "前往测试用例 →",
  "Pass rate over runs": "通过率趋势",
  "Need at least two completed runs to draw a trend.": "至少需要两次已完成的运行才能绘制趋势。",
  "Recent runs": "近期运行",
  "View all": "查看全部",
  "Top failing cases": "失败次数最多的用例",
  "Nothing failing. Nice.": "暂无失败用例。",
  "No runs.": "暂无运行记录。",

  // test cases
  "{{n}} cases in this project.": "本项目共 {{n}} 条用例。",
  "Run selected ({{n}})": "运行所选（{{n}} 条）",
  "Run all enabled": "运行全部启用用例",
  ID: "编号",
  "Last result": "最近结果",
  "never run": "未运行",
  "All results": "全部结果",
  Passing: "已通过",
  "Not passing": "未通过",
  "Never run": "未运行过",
  "All enabled": "全部启用用例",
  "{{n}} selected cases": "已选 {{n}} 条用例",
  "Start a run": "开始运行",
  "Run name": "运行名称",
  "{{n}} case(s) will run. Name it so you can tell it apart later.":
    "将运行 {{n}} 条用例。取个名字，方便以后区分。",
  "Start run": "开始运行",
  "Starting…": "正在启动…",
  // 失败重试（2026-10-08）：per-run 覆盖全局 CASE_RETRIES
  "Retries on failure": "失败后重试",
  "Follow the global setting": "跟随全局设置",
  "No retry": "不重试",
  "Retry once": "重试 1 次",
  "Retry twice": "重试 2 次",
  "Retry three times": "重试 3 次",
  "A retry only reruns failures a retry can actually fix (infra errors, steps that never ran). A real defect is never re-run, so it cannot be laundered into a flaky pass.":
    "重试只对「重试能救回来」的失败生效（环境类错误、一步都没跑完）。真实缺陷不会被重跑，避免把缺陷洗成偶发通过。",
  "Expected outcome (for the judge)": "预期结果（用于自动判定）",
  "Import Excel": "导入 Excel",
  "Export Excel": "导出 Excel",
  "Download template": "下载模板",
  "Imported {{n}} cases": "已导入 {{n}} 条用例",
  "Search key, name or task…": "搜索编号、名称或任务…",
  "All tags": "全部标签",
  All: "全部",
  Enabled: "启用",
  Disabled: "停用",
  "{{n}} shown": "共显示 {{n}} 条",
  Prompt: "提示词",
  Tags: "标签",
  Key: "编号",
  Module: "模块",
  Priority: "优先级",
  Owner: "负责人",
  Delete: "删除",
  Edit: "编辑",
  on: "启用",
  off: "停用",
  "Agent task (natural language — this is what runs)": "Agent 任务（自然语言，实际执行的内容）",
  "References (linked requirements / tickets)": "关联需求 / 单号",
  Preconditions: "前置条件",
  "State the app must be in before this runs.": "执行前系统需满足的状态。",
  "Steps (documentation)": "测试步骤（文档说明）",
  "+ Step": "+ 步骤",
  "No steps. These document the case for humans; the agent runs the task above.":
    "暂无步骤。步骤仅用于文档说明；实际执行的是上方的 Agent 任务。",
  action: "操作",
  expected: "预期",
  "Test data": "测试数据",
  "Test data files (Excel / CSV / TXT)": "测试数据文件（Excel / CSV / TXT）",
  "Test data files hint":
    "声明要生成的文件，执行前由平台物化成真实文件，agent 再用 upload_file 传给被测系统。支持 xlsx / csv / txt；" +
    "占位符：{{i}} 行号（从 1 起）、{{case_key}} 用例编号、{{rand:18}} 18 位随机数字、{{uuid:8}}、{{today}}、{{now}}。" +
    'rows 可以写数组，也可以写 {"count": 200, "template": ["B{{i}}"]} 批量生成。留空 = 这条用例不需要上传文件。',
  "Insert a template": "插入模板",
  Template: "模板",
  "No cases match the filters.": "没有符合筛选条件的用例。",

  // runs
  "Each run executes a batch of cases and produces a report.": "每次运行会执行一批用例并生成测试报告。",
  "Compare ({{n}})": "对比（{{n}}）",
  "All statuses": "全部状态",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  pending: "等待中",
  passed: "通过",
  error: "错误",
  Result: "结果",
  Duration: "耗时",
  View: "查看",
  "Re-run": "重跑",
  // 「用例」两个字在按钮里是冗余的（这一列显然就是用例），去掉能让五个按钮
  // 在动作列里排得下一行 —— 按钮组曾经因为太宽向左溢出遮住别的列，宽度能省则省。
  "Re-run failed ({{n}})": "重跑失败（{{n}} 条）",
  "Re-run errors ({{n}})": "重跑异常（{{n}} 条）",
  Concurrency: "并发数",
  "Export CSV": "导出 CSV",
  Flaky: "不稳定",
  Attempts: "尝试次数",
  "Failing only": "只看失败",
  "Search case…": "搜索用例…",
  "Search run name…": "搜索运行名称…",
  "{{p}}/{{n}} passed": "{{p}}/{{n}} 通过",
  "computed when done": "完成后计算",
  "in progress": "进行中",
  done: "已完成",
  "elapsed · running": "已用时 · 进行中",
  "passed after retry": "重试后通过",
  none: "无",
  "Run progress": "运行进度",
  "Live execution": "实时执行",
  "Step {{n}}": "第 {{n}} 步",
  "waiting for first screenshot…": "等待首帧截图…",
  "click to enlarge": "点击放大查看",
  "Stop run": "终止运行",
  "Cancelling…": "正在终止…",
  "Cancelling — cases stop after the current step": "正在终止 —— 各用例会在当前步骤结束后停下",
  "Could not cancel the run": "终止失败，请重试",
  "click a step to see its screenshot": "点击查看该步骤的截图",
  "showing step {{n}} · back to latest": "正在看第 {{n}} 步 · 回到最新",
  "open original": "打开原图",
  "previous step": "上一步",
  "next step": "下一步",
  "download video (mp4)": "下载视频（MP4）",
  "Total runs": "运行总数",
  "{{n}} completed": "{{n}} 次已完成",
  "Avg pass rate": "平均通过率",
  "across completed runs": "基于已完成的运行",
  "Suite coverage": "套件覆盖率",
  "{{r}}/{{e}} cases": "{{r}}/{{e}} 条用例",
  "Avg duration": "平均耗时",
  "per run": "单次运行",
  Coverage: "测试覆盖率",
  "{{r}}/{{e}} ever run": "{{r}}/{{e}} 条已执行过",
  "Latest pass rate": "最近通过率",
  "{{p}} passed / {{r}} run": "{{p}} 条通过 / {{r}} 条已运行",
  "Total cases": "用例总数",
  "{{e}} enabled · {{d}} disabled": "启用 {{e}} · 停用 {{d}}",
  "Priority · modules": "优先级 · 模块",
  "{{n}} modules": "{{n}} 个模块",
  "Step timeline": "步骤时间线",
  "action → result": "操作 → 结果",
  "show AI thinking": "显示 AI 思考过程",
  "Raw agent log": "Agent 原始日志",
  // 人工改判（2026-10-06）：AI 判定会不准，测试工程师要能自己拍板。
  "Override verdict": "人工改判",
  "AI verdict": "AI 判定",
  "Your verdict": "你的判定",
  "Reason (optional)": "改判理由（可选）",
  "Why is the AI verdict wrong?": "AI 为什么判错了？",
  "Revert to AI verdict": "撤销改判，回到 AI 判定",
  "Verdict updated": "判定已更新",
  "Reverted to the AI verdict": "已撤销改判，恢复 AI 判定",
  "Click to override this verdict (AI judging is not always right)": "点击改判（AI 判定不一定准）",
  "Manually overridden by {{who}}": "由 {{who}} 人工改判",
  "Override": "改判",
  "Edit override": "修改改判",
  // 按用途分模型（2026-10-11）：省钱靠"执行步用便宜模型"，质量靠"判定用强模型"。
  "Model per purpose": "按用途分模型（省钱）",
  // ★ 2026-10-11 配置收敛：网关配置只剩「模型池」一个入口。
  // 原来那张「基层模型」卡的字段在池非空时完全不生效，已合并进池。
  "The gateway the browser agent actually calls. Edit it in the model pool below — there is exactly one place to set it, so what you save is what runs.":
    "浏览器 agent 实际调用的网关。改它请到下面的「模型池」——现在只有这一个设置入口，保存什么就跑什么。",
  "Currently in use (agent)": "当前生效（agent）",
  "from .env — no pool saved yet": "来自 .env —— 还没有保存过模型池",
  "from the pool ({{n}} set(s))": "来自模型池（共 {{n}} 套）",
  "from the premium model": "来自高端模型",
  "from the execution row in “Model per purpose”": "来自「按用途分模型」里的「执行」那一行",
  "Per-purpose models below may override the model for judging, narration, spec generation and the assistant.":
    "下面的「按用途分模型」可能覆盖判定 / 失败叙述 / 需规生成 / 助手所用的模型。",
  "Tests the set the agent actually calls — and tells you which one that was.":
    "测的是 agent 真正调用的那一套，并会告诉你测的是哪一套。",
  "Several gateways tried in order: index 1 is preferred and is what the agent uses by default, the next one takes over when a key is rate-limited (429) or misconfigured. Leave an API key blank to keep the stored one.":
    "多套网关按顺序尝试：第 1 套优先、也是 agent 默认用的那一套；某套 key 配额耗尽（429）或配置有误时自动换下一套。钥匙留空 = 保留已存的那把。",
  "No pool saved yet — the row below is only a read-back of .env. Press Save to make it the real configuration; until then .env stays in charge.":
    "还没有保存过模型池 —— 下面这一条只是 .env 的回显。点「保存模型池」它才会成为真实配置；在那之前仍然以 .env 为准。",
  "One entry = no fallback: this set is used as-is.": "只有一套 = 没有兜底：就用这一套。",
  "Priority: a value here wins over the pool's Agent model. Leave the execution row blank unless you want a different model than the pool set uses.":
    "优先级：这里填了就以这里为准，会盖过模型池里的 Agent model。除非你想让「执行」用的模型和池条目不同，否则留空即可。",
  "Cheap model for the high-volume execution steps, strong model where quality matters. Blank = follow the default (= set 1 of the pool above).":
    "量大的执行步用便宜模型，质量敏感的环节留强模型。留空 = 跟随默认（即上面模型池的第 1 套）。",
  "Execution (agent, every step)": "执行（agent 每步调用）",
  "Judging (once per case)": "判定（每条用例一次）",
  "Judge escalation (review)": "判定复核（强模型）",
  "Failure narrative / case memory": "失败叙述 / 用例记忆",
  "Case generation from spec": "需规生成用例",
  "Project docs generation (overview / analysis / flowchart)": "生成项目文档（总览 / 需求分析 / 流程图）",
  "AI case optimization (rewrite cases)": "AI 用例优化（改写用例）",
  "Failure root-cause classification": "失败根因分类（回填）",
  "Assistant chat & Feishu": "助手对话 / 飞书",
  "~95% of all tokens — the only lever that really cuts cost": "约占总 token 的 95% —— 唯一真正能省钱的旋钮",
  "small volume, quality-critical — keep the strong model here": "量小但质量关键 —— 这里该留强模型",
  "only called when the cheap judge says failed or a gate warns — can be the priciest model":
    "只在便宜模型判失败 / 闸门报警时才调用 —— 这里可以放最贵的模型",
  "runs only on failures, short output": "只在失败时跑，输出很短",
  "long-document batches, quality matters": "长文档批量生成，质量重要",
  "interactive, latency-sensitive": "交互式，对速度敏感",
  // 平台级「高端模型」（2026-10-11）：用户要的是"可以自己选择是否加高端 LLM"。
  // 整个平台"要不要为质量多花钱"在这一个开关上回答一次，默认关 = 省钱优先。
  "Pick one expensive, smart model from the pool below and tick exactly which steps may use it. Off means every step falls back to the pool's set 1 — no extra spend at all.":
    "从下面的模型池里选一支贵的、聪明的模型，并勾选允许哪几个环节用它。关着的时候所有环节都还是模型池的第 1 套 —— 一分钱都不多花。",
  "Use a premium model": "启用高端模型",
  "In effect: {{roles}}": "正在生效：{{roles}}",
  "Which steps may use it": "允许哪几个环节用它",
  "Judge escalation is only called when the cheap judge says failed or a gate warns — it can be the priciest model, because a routine pass never calls it.":
    "「判定复核」只在便宜模型判失败 / 闸门报警时才被调用 —— 所以它可以放最贵的模型，因为通过且干净的结果一次都不会叫它。",
  "The execution step is about 95% of all tokens. Sending it to the premium model removes the main cost lever — the other steps are small-volume, so that is where the money actually goes.":
    "「执行」占全部 token 的约 95%。把它也交给高端模型等于关掉了省钱的主旋钮 —— 其余环节量都很小，钱其实都花在这一项上。",
  // 2026-10-12：高端模型改为「从模型池里选一条」（不再自带网关/钥匙/协议）
  "— pick a model from the pool —": "— 从模型池里选一个 —",
  "Premium model": "高端模型",
  "The premium model is one of the pool entries below — its gateway, key and protocol are configured there, not here.":
    "高端模型就是下面模型池里的某一条 —— 它的网关、钥匙、协议都在那里配置，不在这里。",
  "That pool entry no longer exists — pick another one, or the premium model will not take effect.":
    "那条模型池条目已经不存在了 —— 换一条，否则高端模型不会生效。",
  "Suggested: give the premium model to quality-sensitive, low-volume steps":
    "建议：把高端模型给那些质量敏感、量小的环节",
  "Apply suggestion": "应用建议",
  "Suggestion applied": "已应用建议",
  "These add up to about {{pct}}% of tokens — small volume, so the premium model is cheap here. Execution is ~{{agent}}% of tokens, so it is deliberately not recommended.":
    "这几项加起来约占总 token 的 {{pct}}% —— 量小，所以在这里用高端模型很便宜。执行约占 {{agent}}%，所以特意不建议给它上高端模型。",
  "Add at least one model to the pool first, then pick it above as the premium model.":
    "先在模型池里加至少一个模型，再到上面把它选作高端模型。",
  "Not in effect yet: pick a model from the pool and tick at least one step.":
    "尚未生效：还需要从模型池里选一个模型，并至少勾选一个环节。",
  // 2026-10-12：连通性检测改为「检测所有模型」（模型池全部条目 + 任何逐用途覆盖）
  "Test all models": "检测所有模型",
  "Testing all models…": "正在检测所有模型…",
  "Tests every model the platform may call — all pool entries and any per-purpose override — and lists each.":
    "检测平台可能调用的每一个模型 —— 所有模型池条目以及任何逐用途覆盖 —— 并逐一列出。",
  "All {{n}} model(s) connected.": "全部 {{n}} 个模型均已连通。",
  "{{ok}} of {{n}} model(s) connected — the rest failed.": "{{ok}} / {{n}} 个模型连通，其余失败。",
  "JSON object": "JSON 对象",
  "JSON schema": "JSON schema",
  "Tools": "工具",
  "Vision": "视觉",
  "Probe": "探测",
  "Probing…": "探测中…",
  "Probe this entry (uses the saved config)": "探测此条（用已保存的配置）",
  "Name and save this entry first.": "先给这条起个名并保存，再探测。",
  advanced: "高级",
  "A value here also wins over the premium model above — this card is the most specific layer.":
    "这里填的值同样盖过上面的高端模型 —— 这张卡是最具体的一层。",
  // 两级判定（2026-10-11）：便宜模型先判，判 failed / 闸门报警才升级复核。
  "Two-tier judging (cheap judge first, escalate on failure)": "两级判定（便宜模型先判，失败才升级复核）",
  "The cheap judge runs first. Only a failed verdict or a deterministic gate warning spends the escalation model — routine passes never call it. Takes effect after saving.":
    "先由便宜模型判定；只有判 failed 或确定性闸门报警时才动用复核模型 —— 通过且干净的结果不会多花一分钱。保存后生效。",
  "Judging and escalation resolve to the same model ({{model}}), so escalation is skipped. Give the escalation row a different model to get real review.":
    "判定层与复核层实际是同一支模型（{{model}}），复核会被跳过。给「判定复核」单独设一支模型才有真正的二次审查。",
  // 设置页简化（2026-10-11）：用户反馈「前端 llm 设置页面过于复杂」。
  // 改法是两层：① 长解释从正文段落挪进 ? 悬浮 → 默认视图每个区块只剩一句话；
  // ② 只有一套网关时的那部分、以及「按用途分模型」整卡，默认收起。
  // 注意措辞必须带上"收起来的东西还在"，否则折叠会被读成"功能被砍了"。
  "Fallback gateways": "兜底网关",
  "Only set 1 runs by default — the extras are tried in order when set 1 is rate-limited (429) or misconfigured. Keep them folded if you run a single gateway.":
    "默认只跑第 1 套；第 1 套被限流（429）或配置有误时，才会按顺序换用下面的网关。只用一个网关就不用展开。",
  "{{n}} extra gateway(s) configured for fallback.": "另有 {{n}} 套兜底网关已配置。",
  "No step is individually set — every step uses the pool's set 1.":
    "没有任何环节被单独指定 —— 全部使用模型池的第 1 套。",
  "{{n}} step(s) have their own model.": "有 {{n}} 个环节单独指定了模型。",
  "Only the model name is needed to swap models; the gateway, protocol and key are only for switching to a different provider.":
    "只想换个模型，填「模型」这一个框就够。「协议 / 网关 / 钥匙」是换到另一家厂商时才需要的。",
  "More options": "更多设置",
  "Fewer options": "收起设置",
  // 「模型分配」并卡（2026-10-11 二次简化）：用户看完截图说「高端模型」和
  // 「按用途分模型」这两块"看上去感觉冲突"。查下来冲突感来自结构（两张同级卡片、
  // 各一个保存按钮、两句近义现状），不是语义 —— 它们是同一问题的两级答案。
  // 所以并成一张卡 + 编号 + 竖向连线 + **一个保存按钮**，用结构消掉"二选一"的暗示。
  // 2026-10-12 三次简化：用户要求"只留一个"——删掉「逐环节指定模型」子卡，只保留
  // 「高端模型接管」这一支粗粒度机制；每个用途勾选项右侧显示该角色占全部 token 的
  // 百分比（真实实测优先，无数据回退结构估算并标 ≈），让用户一眼看清"勾哪个最烧钱"。
  "Model assignment": "模型分配",
  "These two are not alternatives — they are two levels of the same answer, and the more specific one wins: step 1 gives some steps a better model, step 2 names one model per step. Whatever step 2 leaves blank still follows the pool's set 1.":
    "这两步不是二选一，而是同一个问题的两级答案，越具体越优先：第 1 步让部分环节改用一支更好的模型，第 2 步逐行点名。第 2 步留空的环节，仍然跟模型池的第 1 套。",
  "Now: every step uses {{model}}": "现在：所有环节都用 {{model}}",
  "Not either-or: step 1 is coarse, step 2 is fine-grained, and step 2 overrides step 1 for the steps it names. Leave step 2 empty and step 1 simply applies to everything it ticks.":
    "不是二选一：第 1 步粗、第 2 步细，第 2 步点名的环节以它为准；第 2 步整个留空时，第 1 步勾了哪些环节就管哪些环节。",
  "Give some steps a better model": "让部分环节用更好的模型",
  "Off — nothing takes over.": "未启用 —— 没有任何环节被接管。",
  "These steps also have their own model in step 2 below, and that one wins: {{roles}}. Clear those rows if you want step 1 to take over.":
    "这几个环节在下面的第 2 步里也单独指定了模型，以那边为准：{{roles}}。想让第 1 步接管，请把那几行清空。",
  "Name one model per step": "逐个环节指定模型",
  "A value here also wins over step 1 above — this is the most specific layer.":
    "这里填的值同样盖过上面的第 1 步 —— 这是最具体的一层。",
  "All following the default.": "全部跟随默认。",
  "Save model assignment": "保存模型分配",
  "One save submits the premium-model assignment.": "一次保存只提交「高端模型接管」这一项设置。",
  "This is the only model-assignment control. Tick a step to give it the premium model; everything you leave unticked keeps using the cheap pool.":
    "这是唯一的模型分配控件。勾选某个环节，它就改用高端模型；没勾的环节继续用便宜的模型池。",
  "One switch decides quality vs cost: turn it on and the steps you tick use the premium model; leave it off and every step falls back to the pool's set 1 — no extra spend at all.":
    "一个开关决定质量与花费：打开后，你勾选的环节改用高端模型；不打开，所有环节回落到模型池第 1 套——零额外花费。",
  "Based on measured runs": "基于历史运行实测",
  "No measured data for this step yet — structural estimate": "该环节暂无实测数据，按调用结构估算",
  "No run data yet — the ratios above are structural estimates (agent dominates). They become measured once you run cases after this update.":
    "暂无运行数据，上方比例为按调用结构的估算（agent 占绝大多数）。本次更新后你跑过用例，就会变成真实实测值。",
  "Token share from {{n}} measured run(s).": "以上占比来自 {{n}} 次运行的实测。",
  "Clear all per-step models": "清空所有逐环节指定",
  "One save submits steps 1 and 2 together — they are the same setting, applied by priority.":
    "一个保存按钮同时提交第 1、2 步 —— 它们本来就是同一项设置，只是按优先级生效。",
  "Reviewed by the escalation model (initial verdict: {{from}})": "由复核模型给出（初判：{{from}}）",
  reviewed: "已复核",
  // 「这一轮的钱花在哪儿」（2026-10-11）。刻意把**往返次数**摆在最显眼处：
  // 实测每次往返的输入量各条之间差不了多少，钱是被次数乘出来的 ——
  // 次数是人能去查、能去改的东西，"60 万 token"不是。
  "Where this run's tokens went": "这一轮的 token 花在哪儿",
  "Total tokens": "总 token",
  "Cache hit": "缓存命中",
  "Cached input is billed far cheaper. Only the rest costs full price: {{n}} tokens.":
    "命中缓存的部分计价便宜得多，真正按全价算的只有剩下这些：{{n}} token。",
  "LLM round-trips": "LLM 往返次数",
  "Cost is driven by the number of round-trips, not by how big each one is.":
    "花多少钱由往返**次数**决定，而不是每次请求有多大。",
  "Top {{n}} share": "前 {{n}} 条占比",
  "How concentrated the bill is. High means a few cases are worth fixing.":
    "账单的集中程度。占比高说明只要修好几条就能省下大头。",
  "Most expensive cases": "最费钱的用例",
  "{{n}} calls": "{{n}} 次往返",
  "{{n}} case(s) reviewed by the escalation model, {{c}} verdict(s) changed":
    "{{n}} 条走了复核层，其中 {{c}} 条被改判",
  "To cut cost, look for cases with many round-trips — the per-call input size is roughly the same across cases.":
    "要省钱就找往返次数多的用例 —— 每次请求的输入量在各条之间差别不大，钱是被次数乘出来的。",
  "(inherit default)": "（跟随默认）",
  "now: {{model}}": "当前生效：{{model}}",
  "gateway URL (optional)": "网关地址（可选）",
  "api key (optional)": "钥匙（可选）",
  "Save per-purpose models": "保存用途配置",
  "Reset all to default": "全部恢复默认",
  manual: "人工",
  "No step diagnostics captured for this run.": "本次运行未采集到步骤诊断信息。",
  " — see the step timeline below": "，详见下方步骤时间线",
  "Rename run": "重命名运行",
  "Delete run(s)?": "确认删除运行？",
  "Delete ({{n}})": "删除（{{n}}）",
  "{{n}} runs": "{{n}} 次运行",
  "No runs match this filter.": "没有符合条件的运行。",
  "{{n}} runs · grouped by start date": "共 {{n}} 次运行 · 按开始日期分组",
  "This permanently removes {{n}} run(s) and their results. This cannot be undone.":
    "将永久删除 {{n}} 次运行及其全部结果，此操作不可恢复。",
  Run: "运行",
  Status: "状态",
  Progress: "进度",
  "Pass rate": "通过率",
  Started: "开始时间",

  // run report
  "← Runs": "← 运行",
  "Re-run this suite": "重跑本套件",
  "p50 latency": "p50 耗时",
  Outcome: "结果分布",
  "running…": "运行中…",
  "no data": "暂无数据",
  Cases: "用例",
  Latency: "耗时",
  Judge: "判定",
  "+ Issue": "+ 问题单",
  Replay: "回放",
  "executing…": "执行中…",
  "{{n}} running": "{{n}} 条运行中",
  "no results": "暂无结果",
  "Latency by case (s)": "各用例耗时（秒）",
  Close: "关闭",
  "no video recorded": "未录制视频",
  "Re-running the same suite": "正在重跑同一套件",
  "Re-running the failed cases": "正在重跑未通过的用例",
  "Re-running the errored cases": "正在重跑异常用例",
  Cancelling: "正在取消",
  "Case #{{id}} replay": "用例 #{{id}} 回放",
  "Final answer": "最终结论",
  // 2026-10-04 失败根因分类。分类标识到中文标签的映射在**后端**
  // （app/judge.py 的 ROOT_CAUSE_LABELS_ZH），前端直接用后端给的
  // root_cause_label，不在这里维护第二份表 —— 两份表不同步时会冒出空白分组。
  "Root cause": "失败根因",
  "system defect — worth a bug report": "系统缺陷 — 建议提缺陷单",
  // 「步骤」→「操作步骤」：缺陷描述卡片"复制"出来的文本用的是【操作步骤】，
  // 而界面标签原来只显示"步骤"，两者对不上。这个 key 只在那一个组件里用，可以安全统一。
  Steps: "操作步骤",
  "download operation trace (history.json)": "下载操作轨迹（history.json）",

  // compare
  "Compare runs": "对比运行",
  "Pick two or more runs to see per-case outcomes side by side.":
    "选择两个及以上的运行，并排查看每条用例的结果。",
  "{{n}} selected": "已选 {{n}} 个",
  "No runs to compare": "暂无可对比的运行",
  "Run a suite first, then come back to compare runs side by side.":
    "先执行一次运行，再回来进行并排对比。",
  "← Change selection": "← 重新选择",
  "Comparing {{n}} runs": "正在对比 {{n}} 个运行",
  Case: "用例",
  regressed: "回归",
  fixed: "已修复",
  "always failing": "持续失败",
  differs: "有差异",
  stable: "稳定",
  Trend: "趋势",
  "Regressed only": "只看回归",
  Differences: "有差异",
  Failing: "失败",
  "{{n}} cases": "{{n}} 条用例",
  "Rows tinted red = passed in an earlier run but not in the latest (a regression).":
    "红色行表示：较早的运行中通过、最近一次未通过（回归）。",

  // issues
  "Bugs from failed tests — track, fix, and re-run to verify.":
    "跟踪测试发现的缺陷：处理、修复，并重跑用例验证。",
  "+ New issue": "+ 新建问题单",
  Open: "待处理",
  "In progress": "处理中",
  Fixed: "已修复",
  Verified: "已验证",
  Closed: "已关闭",
  empty: "暂无",
  "Issue deleted": "问题单已删除",
  "Delete this issue? This cannot be undone.": "确定删除该问题单？此操作不可恢复。",

  // browser state (workspace persistence)
  "Browser state (workspace persistence)": "浏览器状态（工作区持久化）",
  // `on` / `off` already exist above (启用 / 停用) — reused here.
  slots: "占用槽位",
  warm: "已登录",
  size: "占用空间",
  "Reset browser state": "重置浏览器状态",
  "Resetting…": "重置中…",
  "Cookies, localStorage, sessionStorage and IndexedDB are kept per project between cases and between runs, so a re-run starts already logged in with assets cached. Isolated per project — one workspace never sees another's session.":
    "Cookie、localStorage、sessionStorage、IndexedDB 按项目在用例之间、运行之间保留，因此二次回归打开时就已是登录态、静态资源已缓存。项目之间互相隔离——一个工作区看不到另一个的会话。",
  "Wipe this project's saved browser state? Every case will have to log in again.":
    "确定清空本项目的浏览器状态？之后每条用例都需要重新登录。",

  // issue drawer
  Title: "标题",
  Description: "描述",
  Severity: "严重程度",
  Assignee: "处理人",
  unassigned: "未指派",
  "Labels (comma-separated)": "标签（逗号分隔）",
  Save: "保存",
  "Re-run case to verify": "重跑用例验证",
  "Add a comment…": "添加评论…",
  Add: "添加",
  "Re-running the linked case": "正在重跑关联用例",
  "Issue saved": "问题单已保存",

  // settings
  "Appearance": "外观",
  "Follow system / browser by default.": "默认跟随系统 / 浏览器设置。",
  "System": "跟随系统",
  "Light": "浅色",
  "Dark": "深色",
  "Project configuration.": "项目基础配置。",
  "Project name": "项目名称",
  "Base URL": "基础 URL",
  "Save changes": "保存更改",
  "Saving…": "保存中…",
  "Ungrouped": "未分组",
  "Expand all": "全部展开",
  "Collapse all": "全部折叠",
  "Show {{n}} more": "再显示 {{n}} 条",
  "Rename module": "重命名模块",
  "Renames the module on all {{n}} cases in this group. Leave blank to ungroup.":
    "将该分组内全部 {{n}} 条用例的模块统一重命名；留空则移出分组。",
  "Rename": "重命名",
  "Run group": "运行本组",
  "Toggle enabled": "启用 / 停用",
  "Settings saved": "设置已保存",
  "Run defaults": "运行默认值",
  "Applied to every case in this project. Leave blank to use the system default.":
    "应用于本项目的全部用例；留空则使用系统默认值。",
  "Timeout per case (s)": "单用例超时（秒）",
  "Max steps per case": "单用例最大步数",
  "Parallel cases": "并行用例数",

  // gitlab sync
  "GitLab sync": "GitLab 同步",
  "Two-way sync issues with a GitLab project. New issues auto-push; status, labels and comments stay in sync.":
    "与 GitLab 项目双向同步问题单：新建自动推送，状态、标签与评论保持一致。",
  "Sync worker is not running (REDIS_URL unset). You can save the mapping now; syncing starts once the Celery worker/beat are up.":
    "同步服务未运行（未配置 REDIS_URL）。可先保存映射，待 Celery worker/beat 启动后自动开始同步。",
  "GitLab project": "GitLab 项目",
  "— none (keep issues local) —": "— 不关联（问题单仅存本地）—",
  "Enter path manually": "手动填写路径",
  "Pick from list": "从列表选择",
  "Access token": "访问令牌",
  Refresh: "刷新",
  "Access token (api scope)": "访问令牌（api 权限）",
  "Save GitLab settings": "保存 GitLab 设置",
  "GitLab settings saved": "GitLab 设置已保存",
  "Not linked — leave the project blank to keep issues local only.":
    "未关联。项目留空时，问题单仅保存在本地。",
  "GitLab !{{iid}}": "GitLab !{{iid}}",

  // credentials
  Credentials: "凭据",
  "Logins used to reach the app under test. Stored encrypted; the active one is injected at run time.":
    "用于登录被测应用的凭据，加密存储；运行时注入当前启用的凭据。",
  Type: "类型",
  "Test account — auto login (recommended)": "测试账号 · 自动登录（推荐）",
  "Session snapshot (advanced)": "会话快照（高级）",
  "Label (optional)": "标签（可选）",
  Username: "用户名",
  Password: "密码",
  "Upload auth.json": "上传 auth.json",
  "Log in & capture session": "登录并捕获会话",
  // capture progress modal
  "Enter the test account username and password": "请填写测试账号的用户名和密码",
  "Starting the browser…": "正在启动浏览器…",
  "Opening the target site and signing in…": "正在打开目标站点并登录…",
  "Reading the captcha and capturing the session…": "正在识别验证码并捕获会话…",
  "Elapsed {{t}}": "已用时 {{t}}",
  "The server is signing in and capturing the session — first run takes ~30-60s. Please keep this window open.": "服务端正在登录并捕获会话，首次约需 30–60 秒，请保持此窗口打开。",
  "Session captured": "会话已捕获",
  "The credential is saved and set active. You can run test cases now.": "凭据已保存并设为启用，现在可以运行测试用例。",
  "Done": "完成",
  "Sign-in failed": "登录失败",
  "Could not complete sign-in on the target site. Check the account/password and retry.": "未能在目标站点完成登录，请检查账号密码后重试。",
  "Retry": "重试",
  "Logging in…": "登录中…",
  "Save credential": "保存凭据",
  active: "启用中",
  session: "会话",
  Activate: "启用",
  "Credential saved & activated": "凭据已保存并启用",
  "The server logs into the project's Base URL with this account and captures the session (simple captchas are read automatically). No local setup needed.":
    "服务端使用该账号登录项目基础 URL 并捕获会话（可自动识别简单验证码），无需本地配置。",

  // case drawer
  "Expected outcome": "预期结果",
  "Start URL (optional, overrides project base URL)": "起始 URL（可选，覆盖项目基础 URL）",
  "Tags (comma-separated)": "标签（逗号分隔）",
  "Enabled (included in “run all enabled”)": "启用（纳入「运行全部启用用例」）",
  "This case hasn't run yet.": "该用例尚未运行。",
  "Edit case": "编辑用例",
  "Run history": "运行历史",
  Comments: "评论",
  "Issue #": "问题单 #",

  // command palette
  "Jump to a project or section…": "跳转到项目或页面…",
  "No matches": "无匹配结果",
  Home: "首页",
  "all projects": "全部项目",
  project: "项目",
  section: "页面",

  // relative time + counts
  "just now": "刚刚",
  "{{n}}m ago": "{{n}} 分钟前",
  "{{n}}h ago": "{{n}} 小时前",
  "{{n}}d ago": "{{n}} 天前",
  Today: "今天",
  Yesterday: "昨天",
  "This week": "本周",
  Earlier: "更早",
  "{{n}} fails": "失败 {{n}} 次",

  // language switcher
  Language: "语言",

  // auth / users / members
  Email: "邮箱",
  Role: "角色",
  Members: "成员",
  Remove: "移除",
  Clear: "清除",
  Saved: "已保存",
  Admin: "管理员",
  Member: "成员",
  Users: "用户",
  Editor: "编辑者",
  Viewer: "查看者",
  "Search users by email or name": "按邮箱或姓名搜索用户",
  "Type to search accounts…": "输入关键字搜索账号…",
  "No matching users": "没有匹配的用户",
  "Sign in": "登录",
  "Signing in…": "登录中…",
  "Sign in to continue": "登录以继续",
  "Write test cases in plain language; they run in a real browser and are judged automatically.":
    "用自然语言编写测试用例，在真实浏览器中执行并自动判定结果。",
  "Incorrect email or password": "邮箱或密码不正确",
  "Need access? Contact a system administrator to provision your account.":
    "如需访问权限，请联系系统管理员开通账号。",
  "An agentic end-to-end testing platform, built for teams.": "面向团队的智能体（Agent）端到端测试平台。",
  "Real browser execution": "真实浏览器执行",
  "Multi-account · multi-env · access isolation": "多账号 · 多环境 · 权限隔离",
  "Sign out": "退出登录",
  "Interface language": "界面语言",
  "Activate your account": "激活账号",
  "Loading…": "加载中…",
  "Your name": "姓名",
  "Set a password (min 6 chars)": "设置登录密码（至少 6 位）",
  "Activating…": "激活中…",
  "Activate & sign in": "设置密码并激活",
  "This invite is invalid, used, or expired.": "该邀请链接无效、已被使用或已过期。",
  "Invite testers and manage accounts.": "邀请测试人员并管理账号。",
  "Invite a user": "邀请用户",
  "System admin": "系统管理员",
  "Send invite": "发送邀请",
  "Invite emailed to {{e}}": "邀请邮件已发送至 {{e}}",
  "Invite created — copy the link below": "邀请已创建，请复制下方链接",
  "Link (copy it if the email doesn't arrive):": "链接（若邮件未送达，可直接复制）：",
  // password reset
  "Forgot your password?": "忘记密码？",
  "Reset your password": "重置密码",
  "Email me a reset link": "发送重置链接到邮箱",
  "Sending…": "发送中…",
  "Back to sign in": "返回登录",
  "If an account exists for that address, a reset link is on its way. The link is valid for 2 hours.":
    "如果该邮箱已注册，重置链接已发出，2 小时内有效。",
  "Set a new password": "设置新密码",
  "New password (min 6 chars)": "新密码（至少 6 位）",
  "Confirm new password": "确认新密码",
  "The two passwords don't match.": "两次输入的密码不一致。",
  "Set password & sign in": "设置密码并登录",
  "This reset link is invalid, used, or expired. Request a new one.":
    "该重置链接无效、已被使用或已过期，请重新申请。",
  "Reset password": "重置密码",
  "Reset link emailed to {{e}}": "重置链接已发送至 {{e}}",
  "Reset link created — copy it below": "重置链接已生成，请复制下方链接",
  "Change password": "修改密码",
  "Current password": "当前密码",
  "Current password is incorrect.": "当前密码不正确。",
  "Password changed.": "密码修改成功。",
  "Make admin": "设为管理员",
  "Revoke admin": "取消管理员",
  Disable: "禁用",
  Enable: "启用",
  Active: "启用中",
  "System settings": "系统设置",
  "Server-wide configuration (admin only).": "服务端全局配置（仅管理员可见）。",
  "LLM model": "底层 LLM 模型",
  "The OpenAI-compatible endpoint and models behind the browser agent and the judge. Changes apply to new runs — no redeploy.":
    "浏览器 Agent 与裁判所使用的 OpenAI 兼容端点及模型。修改后对新的运行生效，无需重新部署。",
  "Base URL (OpenAI-compatible)": "Base URL（OpenAI 兼容）",
  "API key": "API 密钥",
  "API key (set — leave blank to keep)": "API 密钥（已设置，留空保持不变）",
  "Model (judge + default)": "模型（裁判 + 默认）",
  "Agent model (optional — empty uses Model)": "Agent 模型（可选，留空复用上面的模型）",
  "e.g. a local VLM": "如本地视觉模型",
  "API key is set.": "API 密钥已设置。",
  "No API key set — the gateway may reject calls.": "尚未设置 API 密钥，网关可能拒绝调用。",
  "The judge model needs vision (screenshots are evidence). Values here override .env; key stored encrypted.":
    "裁判模型需要视觉能力（截图是判定证据）。此处的值覆盖 .env；密钥加密存储。",
  "Test connection": "测试连通性",
  "Testing…": "测试中…",
  // token 账本（2026-10-09）。加这一列的目的就是让"烧在哪"可见 ——
  // 在这之前没有任何地方记录一条用例花了多少 token。
  "Tokens": "Token",
  "LLM tokens consumed by this case (agent + judge). Hover for the cache-hit share — cached input is billed far cheaper.":
    "这条用例消耗的 LLM token（agent + 判定器）。悬停可看缓存命中占比 —— 命中缓存的那部分计价便宜得多。",
  "{{total}} tokens in {{calls}} LLM calls · {{pct}}% of input served from cache":
    "共 {{total}} token，{{calls}} 次 LLM 调用 · 输入有 {{pct}}% 命中缓存",
  "not measured (no usage in the response)": "未测量（网关响应里没有 usage）",
  // 从需规文档生成用例（2026-10-09）
  "Generate from spec": "从需规生成",
  "{{n}} drafts awaiting review": "{{n}} 条草稿待审阅",
  "Cases generated from a spec that nobody has activated yet":
    "从需规生成、但还没人启用的用例（草稿不会进全量回归）",
  "Generate cases from a spec document": "从需规文档生成用例",
  "Pick a module, review what the model wrote, then save it — as drafts by default.":
    "选一个模块，审一遍模型写的内容，再决定怎么存 —— 默认存成草稿。",
  "Reading the document…": "正在读取文档…",
  "No spec uploaded yet": "还没有上传需规",
  "Go to this project's Settings → 需规 / 资料 and upload the document (Word / PDF / Excel / Markdown / txt), then come back.":
    "到本项目的「设置 → 需规 / 资料」上传文档（支持 Word / PDF / Excel / Markdown / txt），然后回到这里。",
  Modules: "模块",
  "Re-plan": "重新分批",
  "Grouping failed — fell back to one module per heading.":
    "归并失败，已退回到「一个标题一个模块」（分批更碎，但功能可用）。",
  "Not testable end-to-end in a browser": "在浏览器里端到端测不了",
  "Not testable in a browser — skipped": "浏览器里测不了 —— 已跳过",
  "Pick a module on the left — the model will draft cases from just that section.":
    "从左边的模块里选一个 —— 模型只会根据那一节的正文起草用例。",
  "Generating from «{{m}}»…": "正在从「{{m}}」生成…",
  "{{n}} drafted · {{sel}} selected": "起草 {{n}} 条 · 已选 {{sel}} 条",
  "Select none": "全不选",
  "{{n}} case(s) were rejected while parsing:": "有 {{n}} 条在解析阶段被丢弃：",
  "The model decided this section has nothing worth testing end-to-end. That is a valid answer — it did not pad the list.":
    "模型认为这一节没有值得端到端验证的内容。这是合法结果 —— 它没有为了凑数编用例。",
  "What the agent should do": "让 agent 做什么（操作指令）",
  "Decidable expected result": "可判定的预期结果",
  Drop: "丢弃",
  "Drafts do not run in a run-all until you activate them.":
    "草稿不会被「运行全部启用用例」跑到，直到你启用它们。",
  "Save {{n}} as draft": "存 {{n}} 条为草稿",
  "Save {{n}} and activate": "存 {{n}} 条并启用",
  "Saved {{n}} cases as active — they will run in the next run-all":
    "已存 {{n}} 条并启用 —— 下一轮全量运行会跑到它们",
  "Saved {{n}} cases as draft — they will NOT run until you activate them":
    "已存 {{n}} 条为草稿 —— 在你启用之前不会被跑到",
  // 后端接口探索（2026-10-09）
  "Backend API discovered from page traffic": "从页面流量里学到的后端接口",
  "Learned by watching what the page actually calls — nothing is scanned or guessed.":
    "靠观察页面实际发出的请求学来的 —— 不扫描、不猜路径。",
  "Run some cases first — the page's own requests are how this list gets built.":
    "先跑几轮用例 —— 这个清单是靠页面自己发出的请求攒出来的。",
  "{{n}} endpoints observed": "已观察到 {{n}} 个接口",
  "{{r}} readable · {{w}} writable (view only)": "{{r}} 个可读 · {{w}} 个写接口（只能看）",
  "Read data": "读数据",
  "view only": "只能看",
  "Write endpoints can be observed but never called by tests.":
    "写接口可以被观察到，但用例永远不会调用它们。",
  "This is a write endpoint. Exploration is read-only — tests must change data through the UI, not by calling this.":
    "这是写接口。探索阶段只读 —— 测试必须通过界面改数据，不能直接调它。",
  "Only the shape is stored — never the value.": "只存形状，不存值。",
  "body fields": "请求体字段",
  "sample": "样例",
  "params": "参数",
  // 人来变更用例状态（2026-10-09）
  "Enable ({{n}})": "启用（{{n}} 条）",
  "Mark blocked ({{n}})": "标为受阻（{{n}} 条）",
  "Enable {{n}} cases": "启用 {{n}} 条用例",
  "Mark {{n}} cases as blocked": "把 {{n}} 条用例标为受阻",
  "A blocked case stays in the list with its history — it just leaves the regression set until the reason is resolved.":
    "受阻的用例仍留在列表里、历史结果也保留 —— 只是在原因解决之前不再进回归集。",
  "Enabled cases join the next run-all. Blocked reasons are cleared.":
    "启用的用例会进下一轮全量运行；原有的受阻原因会被清掉。",
  "Why can the agent not run these?": "为什么 agent 跑不了这些用例？",
  "e.g. needs a human to create the approval record in another system first":
    "例如：需要人工先在另一个系统里建好审批单",
  "Updated {{n}} cases": "已更新 {{n}} 条用例",
  Confirm: "确认",
  // 直接上传需规文档（2026-10-09）
  "Upload a spec document": "上传需规文档",
  "{{n}} sections found": "识别到 {{n}} 个小节",
  "truncated — long document, only the beginning was indexed":
    "已截断 —— 文档太长，只把开头部分入库了",
  "grouping fell back to one module per section": "模型归并不可用，已退回「一个小节一个模块」",
  // 长文档分批（2026-10-09）
  "Planned {{done}} of {{total}} sections — {{left}} still unplanned.":
    "已规划 {{done}} / {{total}} 个小节 —— 还有 {{left}} 个没规划。",
  "Plan the next batch": "规划下一批",
  "All {{n}} sections are planned.": "全部 {{n}} 个小节都已规划。",
  "Planning this batch failed.": "这一批规划失败。",
  "That batch failed — nothing was added.": "这一批失败了，没有新增模块：",
  "AI Optimization": "AI 用例优化",
  "AI 用例优化": "AI 用例优化",
  "AI 按「项目文档」审一遍用例库，提出 补/改/删 建议。只有你点「采纳」才会动用例库。":
    "AI 按「项目文档」审一遍用例库，提出 补/改/删 建议。只有你点「采纳」才会动用例库。",
  "让 AI 审一遍": "让 AI 审一遍",
  "重新生成": "重新生成",
  "这会清掉还没决定的旧提案，重新生成。确定吗？": "这会清掉还没决定的旧提案，重新生成。确定吗？",
  "AI 审完 {{m}} 个模块，新增 {{n}} 条提案": "AI 审完 {{m}} 个模块，新增 {{n}} 条提案",
  "（{{n}} 个模块没审成功：{{names}} —— 可以再点一次补齐）": "（{{n}} 个模块没审成功：{{names}} —— 可以再点一次补齐）",
  "补用例": "补用例",
  "改用例": "改用例",
  "删用例": "删用例",
  "待审阅": "待审阅",
  "全部": "全部",
  "已采纳": "已采纳",
  "已驳回": "已驳回",
  "全选待审阅": "全选待审阅",
  "取消全选": "取消全选",
  "已选 {{n}} 条": "已选 {{n}} 条",
  "驳回所选": "驳回所选",
  "采纳所选": "采纳所选",
  "没有提案": "没有提案",
  "先到「项目文档」生成项目总览，再回来点「让 AI 审一遍」。":
    "先到「项目文档」生成项目总览，再回来点「让 AI 审一遍」。",
  "依据与改动内容": "依据与改动内容",
  "依据": "依据",
  "将新建的用例": "将新建的用例",
  "将修改的字段": "将修改的字段",
  "采纳后这条用例会被**作废**（不真删），你可以在用例页改回来。":
    "采纳后这条用例会被作废（不真删），你可以在用例页改回来。",
  "将采纳 {{n}} 条提案。继续？": "将采纳 {{n}} 条提案。继续？",
  "将采纳 {{n}} 条提案，其中 {{r}} 条会作废用例（可恢复）。继续？":
    "将采纳 {{n}} 条提案，其中 {{r}} 条会作废用例（可恢复）。继续？",
  "已处理 {{ok}} 条，{{n}} 条没落地：{{why}}": "已处理 {{ok}} 条，{{n}} 条没落地：{{why}}",
  "已采纳 {{n}} 条": "已采纳 {{n}} 条",
  "已驳回 {{n}} 条": "已驳回 {{n}} 条",
  "人工确认过的通过情况": "人工确认过的通过情况",
  "{{p}}/{{n}} 通过": "{{p}}/{{n}} 通过",
  "还没有人工改判过的结果": "还没有人工改判过的结果",
  "疑似缺陷 {{n}} 条": "疑似缺陷 {{n}} 条",
  "另有 {{n}} 条只有自动判定、还没人工确认": "另有 {{n}} 条只有自动判定、还没人工确认",
  "展开全部 {{n}} 个模块": "展开全部 {{n}} 个模块",
  "收起": "收起",
  "模块": "模块",
  "人工确认": "人工确认",
  "确认通过率": "确认通过率",
  "自动判定": "自动判定",
  "待人工确认": "待人工确认",
  "缺陷 {{n}}": "缺陷 {{n}}",
  // 上传时选择"允许助手修改" + 流程图点开看（2026-10-12）
  "允许助手修改": "允许助手修改",
  "在下面「需规 / 资料」里上传文件：它会同时存成可查看可编辑的文档，并加进 AI 检索的资料库。上传前可以选「允许助手修改」。":
    "在下面「需规 / 资料」里上传文件：它会同时存成可查看可编辑的文档，并加进 AI 检索的资料库。上传前可以选「允许助手修改」。",
  "取消勾选后，助手不能修改这些文件（它仍然能读）":
    "取消勾选后，助手不能修改这些文件（它仍然能读）",
  "；并已存成文档，可在左侧点开查看": "；并已存成文档，可在左侧点开查看",
  "点图可放大查看": "点图可放大查看",
  "点开查看大图": "点开查看大图",
  "点任意处关闭 · 内容可滚动": "点任意处关闭 · 内容可滚动",
  "关闭": "关闭",
  // 文档权限与「上传成文档」（2026-10-12）
  "只有我能改": "只有我能改",
  "只有我能改（助手不能写）": "只有我能改（助手不能写）",
  "勾上之后助手不能修改这份文档（它仍然能读）": "勾上之后助手不能修改这份文档（它仍然能读）",
  "这份文档已设为「只有我能改」：助手不能写它，但仍然能读。你自己照常编辑。":
    "这份文档已设为「只有我能改」：助手不能写它，但仍然能读。你自己照常编辑。",
  "已设为「只有我能改」—— 助手不能再修改它（仍能读）。":
    "已设为「只有我能改」—— 助手不能再修改它（仍能读）。",
  // 上传原件可见性（2026-10-13）：列表标"上传"、上传入口并入文档列表
  "上传需规 / 资料": "上传需规 / 资料",
  "读取中…": "读取中…",
  "已上传": "已上传",
  "已进知识库，但存成文档失败": "已进知识库，但存成文档失败",
  "已放开 —— 助手可以修改这份文档了。": "已放开 —— 助手可以修改这份文档了。",
  // 助手页的需规指引（2026-10-12）
  "需规 / 资料 已移到「项目文档」页（查看、粘贴、上传都在那里）。":
    "需规 / 资料 已移到「项目文档」页（查看、粘贴、上传都在那里）。",
  "去「项目文档」": "去「项目文档」",
  // 提案里的流程图与账号（2026-10-12）
  "这条涉及的业务流程": "这条涉及的业务流程",
  "用哪个账号跑": "用哪个账号跑",
  "未指定（沿用项目默认账号）": "未指定（沿用项目默认账号）",
  // 对话框附件（2026-10-12）
  "附加文件": "附加文件",
  "附加文件（也可以直接粘贴或拖进来）": "附加文件（也可以直接粘贴或拖进来）",
  "移除附件": "移除附件",
  "内容过长，只带了开头部分": "内容过长，只带了开头部分",
  "问助手…（Enter 发送，Shift+Enter 换行；可直接粘贴 / 拖入文件）":
    "问助手…（Enter 发送，Shift+Enter 换行；可直接粘贴 / 拖入文件）",
  "（见附件）": "（见附件）",
  "请看附件内容。": "请看附件内容。",
  // 需规 / 资料 上传（2026-10-12）
  "需规 / 资料": "需规 / 资料",
  "还没有需规 —— 先生成文档前必须先上传": "还没有需规 —— 先生成文档前必须先上传",
  "支持哪些格式？": "支持哪些格式？",
  "文档": "文档",
  "纯文本类": "纯文本类",
  "不支持老版二进制格式": "不支持老版二进制格式",
  "请用 Office 另存为 docx / xlsx / pptx 后再上传。": "请用 Office 另存为 docx / xlsx / pptx 后再上传。",
  "一次可以选多个文件，会按文件名分段落追加进去。": "一次可以选多个文件，会按文件名分段落追加进去。",
  "上传后到右边点「AI 分析与生成」，AI 会读它并写出项目文档。":
    "上传后到右边点「AI 分析与生成」，AI 会读它并写出项目文档。",
  "{{n}} 段": "{{n}} 段",
  // 流程图预览（2026-10-12）
  "流程图预览（下面的图由正文里的 mermaid 源码渲染，改源码图就变）":
    "流程图预览（下面的图由正文里的 mermaid 源码渲染，改源码图就变）",
  "这张图渲染不了": "这张图渲染不了",
  "上面编辑器里的 mermaid 源码可能写错了，改好就会自动重画。":
    "上面编辑器里的 mermaid 源码可能写错了，改好就会自动重画。",
  "正在画图…": "正在画图…",
  // 项目概况面板（2026-10-12）
  "需求覆盖矩阵": "需求覆盖矩阵",
  "{{c}}/{{n}} 条需求有用例引用；{{r}}/{{n2}} 条用例填了需求 ID":
    "{{c}}/{{n}} 条需求有用例引用；{{r}}/{{n2}} 条用例填了需求 ID",
  "用例里没有填需求 ID（references），所以无法建立需求追溯。":
    "用例里没有填需求 ID（references），所以无法建立需求追溯。",
  "需求 ID": "需求 ID",
  "需求原文（摘自需规）": "需求原文（摘自需规）",
  "用例数": "用例数",
  "用例": "用例",
  "人工确认通过率": "人工确认通过率",
  "（需规里找不到这条 ID 的原文）": "（需规里找不到这条 ID 的原文）",
  "0 · 没人测": "0 · 没人测",
  "展开全部 {{n}} 条": "展开全部 {{n}} 条",
  "注意粒度：{{req}} 被 {{n}} 条用例引用 —— 多数用例只挂这一个需求 ID，所以这个矩阵适合看「有没有」，不适合看「覆盖得多细」。":
    "注意粒度：{{req}} 被 {{n}} 条用例引用 —— 多数用例只挂这一个需求 ID，所以这个矩阵适合看「有没有」，不适合看「覆盖得多细」。",
  "功能模块覆盖": "功能模块覆盖",
  "{{n}} 个模块": "{{n}} 个模块",
  "未填需求ID": "未填需求ID",
  "待确认": "待确认",
  "业务规则清单": "业务规则清单",
  "{{n}} 条 · 每条后面是验证它的用例": "{{n}} 条 · 每条后面是验证它的用例",
  "筛选规则…": "筛选规则…",
  "没有用例验证它": "没有用例验证它",
  "{{n}} 条用例": "{{n}} 条用例",
  "接口资产清单": "接口资产清单",
  "{{n}} 个（从页面流量学到）· {{w}} 个写接口": "{{n}} 个（从页面流量学到）· {{w}} 个写接口",
  "红色是写接口：探索时只读，写操作必须显式声明。":
    "红色是写接口：探索时只读，写操作必须显式声明。",
  "测试账号状态": "测试账号状态",
  "{{n}} 个不可用（共 {{m}} 个）": "{{n}} 个不可用（共 {{m}} 个）",
  "{{n}} 个都可用": "{{n}} 个都可用",
  "还没有配账号。": "还没有配账号。",
  "已停用": "已停用",
  "环境/账号问题是失败归因里最大的一类 —— 先修这里比改用例更有效。":
    "环境/账号问题是失败归因里最大的一类 —— 先修这里比改用例更有效。",
  "失败台账": "失败台账",
  "{{n}} 条看起来不是产品缺陷": "{{n}} 条看起来不是产品缺陷",
  "人工改判为 {{v}}": "人工改判为 {{v}}",
  "用例变更历史": "用例变更历史",
  "还没有变更记录。": "还没有变更记录。",
  // 深链：点用例编号跳到用例页（2026-10-12）
  "看这条用例": "看这条用例",
  "看这 {{n}} 条用例": "看这 {{n}} 条用例",
  "全部 {{n}} 条": "全部 {{n}} 条",
  "只显示指定的 {{n}} 条用例（来自链接）。": "只显示指定的 {{n}} 条用例（来自链接）。",
  "只显示「{{refs}}」相关的用例（来自链接）。": "只显示「{{refs}}」相关的用例（来自链接）。",
  "只显示「{{refs}}」相关的 {{n}} 条用例（共 {{total}} 条）。":
    "只显示「{{refs}}」相关的 {{n}} 条用例（共 {{total}} 条）。",
  "显示全部用例": "显示全部用例",
  // 项目文档 / 富文本编辑器（2026-10-12）
  "Project Docs": "项目文档",
  "New document": "新建文档",
  "AI 读过的项目说明：总览、需求分析、流程图。可以像 Word 一样编辑。":
    "AI 读过的项目说明：总览、需求分析、流程图。可以像 Word 一样编辑。",
  "AI 分析与生成": "AI 分析与生成",
  "AI 分析完成": "AI 分析完成",
  "重新生成（覆盖）": "重新生成（覆盖）",
  "用新生成的内容替换现有正文": "用新生成的内容替换现有正文",
  "这会用 AI 生成的内容替换当前正文，确定吗？": "这会用 AI 生成的内容替换当前正文，确定吗？",
  "生成失败": "生成失败",
  "AI 读了 {{n}} 个章节标题、{{c}} 字需规原文，产出 {{s}} 节。":
    "AI 读了 {{n}} 个章节标题、{{c}} 字需规原文，产出 {{s}} 节。",
  // 通读全文开关（2026-10-13）
  "检索全文": "检索全文",
  "打开后先把整份需规通读一遍（多次调用，慢且更费），而不是只检索相关片段。":
    "打开后先把整份需规通读一遍（多次调用，慢且更费），而不是只检索相关片段。",
  // 「大系统」容器（2026-10-13）
  "新建大系统": "新建大系统",
  "更多": "更多",
  "展开": "展开",
  // 指标改名 + 页头瘦身（2026-10-13）
  "Case execution rate": "用例执行率",
  "{{r}}/{{e}} executed": "{{r}}/{{e}} 条已执行过",
  "Test cases for the selected project.": "这个项目的测试用例。",
  "Show all {{n}}": "展开全部（共 {{n}} 个）",
  "Show fewer": "收起，只看前几个",
  // 上传的原件（2026-10-16）
  "原件": "原件",
  "打开上传时的原件{{name}}": "打开上传时的原件{{name}}",
  "项目说明与上传的资料都在左边；每一份都可以像 Word 一样直接编辑。":
    "项目说明与上传的资料都在左边；每一份都可以像 Word 一样直接编辑。",
  "上传的资料和别的文档一样：可以在这里直接改，改完点右上角「保存」。":
    "上传的资料和别的文档一样：可以在这里直接改，改完点右上角「保存」。",
  // 大系统共享需规的上传（2026-10-17）
  "上传文件": "上传文件",
  "已追加 {{n}} 份资料（共 {{c}} 字）": "已追加 {{n}} 份资料（共 {{c}} 字）",
  "粘贴这个系统级的需规/资料，或者直接上传文件。**它下面的每个项目**检索时都会带上它 —— 传一次，各模块共用。":
    "粘贴这个系统级的需规/资料，或者直接上传文件。**它下面的每个项目**检索时都会带上它 —— 传一次，各模块共用。",
  "支持 md / txt / json / csv / docx / pdf / xlsx / html / rtf；上传的内容会**追加**到下面，不会覆盖已有内容。":
    "支持 md / txt / json / csv / docx / pdf / xlsx / html / rtf，图片（png/jpg）走 OCR；上传的内容会**追加**到下面，不会覆盖已有内容。",
  "上传过的原件（{{n}} 份）": "上传过的原件（{{n}} 份）",
  "无原件": "无原件",
  "这份的原件当时没存下来，只能看解析后的正文": "这份的原件当时没存下来，只能看解析后的正文",
  "大系统名称": "大系统名称",
  "例如：民航培训管理系统": "例如：民航培训管理系统",
  "改名": "改名",
  "共享需规": "共享需规",
  "共享需规 {{c}} 字": "共享需规 {{c}} 字",
  "{{n}} 个项目": "{{n}} 个项目",
  "{{c}} 字": "{{c}} 字",
  "未归类": "未归类",
  "所属大系统（可选）": "所属大系统（可选）",
  "不属于任何大系统": "不属于任何大系统",
  "大系统用来把若干小模块项目归拢在一起；它自己可以放一份共享需规，下面的项目都检索得到。":
    "大系统用来把若干小模块项目归拢在一起；它自己可以放一份共享需规，下面的项目都检索得到。",
  "还没有项目归到它下面 —— 在项目的「Edit」里选「所属大系统」。":
    "还没有项目归到它下面 —— 在项目的「Edit」里选「所属大系统」。",
  "粘贴这个系统级的需规/资料。**它下面的每个项目**检索时都会带上它 —— 传一次，各模块共用。":
    "粘贴这个系统级的需规/资料。它下面的每个项目检索时都会带上它 —— 传一次，各模块共用。",
  "把系统级需规粘贴到这里……": "把系统级需规粘贴到这里……",
  "已保存共享需规：{{c}} 字": "已保存共享需规：{{c}} 字",
  "大系统已删除，它下面的项目已退回平铺": "大系统已删除，它下面的项目已退回平铺",
  "删除大系统": "删除大系统",
  "删除「{{name}}」？它下面的 {{n}} 个项目**不会被删**，只会退回未归类；它那份共享需规会被删掉。":
    "删除「{{name}}」？它下面的 {{n}} 个项目不会被删，只会退回未归类；它那份共享需规会被删掉。",
  "（追加在原有内容之后）": "（追加在原有内容之后）",
  "正文引用了 {{n}} 个原文条目": "正文引用了 {{n}} 个原文条目",
  "正文里逐字提到的原文条目（点开核对）": "正文里逐字提到的原文条目（点开核对）",
  "这些名称都在正文里出现过，所以能对上。没列出的部分属于 AI 的综合描述，需要你自己核对。":
    "这些名称都在正文里出现过，所以能对上。没列出的部分属于 AI 的综合描述，需要你自己核对。",
  "AI 生成后还没有人改过": "AI 生成后还没有人改过",
  "还没有文档": "还没有文档",
  "先在「需规 / 资料」上传需求文档，然后点下面的按钮让 AI 读一遍。":
    "先在「需规 / 资料」上传需求文档，然后点下面的按钮让 AI 读一遍。",
  "在这里写项目说明，或点上面的「AI 分析与生成」让它先起草…":
    "在这里写项目说明，或点上面的「AI 分析与生成」让它先起草…",
  "新文档标题": "新文档标题",
  "自定义文档": "自定义文档",
  "删除「{{t}}」？": "删除「{{t}}」？",
  "已删除": "已删除",
  "删除": "删除",
  "已保存": "已保存",
  // 旧文档重新排版（2026-10-19）
  "重新排版": "重新排版",
  "用上传时的原件重新解析正文，并按新的段落/标题/表格排版重写":
    "用上传时的原件重新解析正文，并按新的段落/标题/表格排版重写",
  "这份文档的正文你手工改过，重新排版会用原件覆盖这些修改。继续吗？":
    "这份文档的正文你手工改过，重新排版会用原件覆盖这些修改。继续吗？",
  "已按原件重新排版": "已按原件重新排版",
  "已保存，但有部分内容因不安全被去掉了（脚本/内嵌框架等）。":
    "已保存，但有部分内容因不安全被去掉了（脚本/内嵌框架等）。",
  "Tip: paste from Word/网页会尽量保留格式；脚本等危险内容会被自动去掉。":
    "提示：从 Word / 网页粘贴会尽量保留格式；脚本等危险内容会被自动去掉。",
  // 编辑器工具栏
  "撤销": "撤销",
  "重做": "重做",
  "字体": "字体",
  "字号": "字号",
  "默认字体": "默认字体",
  "文字颜色": "文字颜色",
  "常用颜色": "常用颜色",
  "加粗": "加粗",
  "斜体": "斜体",
  "下划线": "下划线",
  "删除线": "删除线",
  "高亮": "高亮",
  "行内代码": "行内代码",
  "标题": "标题",
  "小标题": "小标题",
  "无序列表": "无序列表",
  "有序列表": "有序列表",
  "引用": "引用",
  "分割线": "分割线",
  "左对齐": "左对齐",
  "居中": "居中",
  "右对齐": "右对齐",
  "插入表格": "插入表格",
  "插入链接": "插入链接",
  "插入图片": "插入图片",
  "链接地址": "链接地址",
  // 失败归因徽标（2026-10-10 用户反馈后加）
  "Who this failure belongs to: {{what}}": "这条失败该算谁的：{{what}}",
  "Connected.": "连接成功。",
  "Model": "模型",
  "Connection failed.": "连接失败。",
  "Could not load settings:": "加载设置失败：",
  "Currently in use": "当前实际使用中",
  "Endpoint": "接口地址",
  "Agent model": "Agent 模型",
  " (inherited)": "（继承默认模型）",
  "set": "已设置",
  "not set — calls will likely be rejected": "未设置 —— 调用大概率会被拒绝",
  "(not set)": "（未设置）",
  "(not found in pool)": "（模型池里找不到）",
  // LLM 协议与能力（2026-10-11）：换厂商不改代码，协议自动识别、能力实测
  "Protocol": "协议",
  "Auto-detect": "自动识别",
  "OpenAI-compatible": "OpenAI 兼容",
  "Anthropic (Claude)": "Anthropic（Claude）",
  "Google (Gemini)": "Google（Gemini）",
  "Ollama (local)": "Ollama（本地）",
  " (detected by probe)": "（由探测得出）",
  "Auto-detect works for any OpenAI-compatible gateway (DeepSeek, Qwen, GLM, Kimi, vLLM, OpenRouter …) and for Claude / Gemini / Ollama. Only set it explicitly if detection guesses wrong.":
    "自动识别适用于所有 OpenAI 兼容网关（DeepSeek、通义、智谱、Kimi、vLLM、OpenRouter…）以及 Claude / Gemini / Ollama。只有识别猜错时才需要手动指定。",
  "Detected protocol": "识别到的协议",
  " (assumed, probe failed)": "（探测未通过，按协议默认值）",
  "JSON object mode": "原生 JSON 模式",
  "Strict JSON schema": "严格 JSON Schema",
  "Tool calling": "工具调用",
  "Image input": "图片输入",
  "This gateway rejects strict JSON schema — the browser agent automatically keeps the schema in the system prompt instead. Nothing to fix.":
    "该网关拒绝严格 JSON Schema —— 浏览器代理会自动改为把 schema 写进 system prompt，无需人工处理。",
  "Model pool (fallback order)": "模型池（按顺序兜底）",
  "Several gateways tried in order: index 1 is preferred, the next one takes over when a key is rate-limited (429) or misconfigured. Leave an API key blank to keep the stored one.":
    "按顺序尝试多套网关：第 1 套优先，某套配额耗尽（429）或配置有误时自动切换下一套。API Key 留空表示保持已存的那把。",
  "Add a gateway": "新增一套网关",
  "Save pool": "保存模型池",
  "•••••••• (kept)": "••••••••（保持不变）",
  "One entry = no fallback: the single set above is used as-is.":
    "只有一套时不会兜底：直接使用上面配置的那一套。",
  "{{n}} sets configured.": "已配置 {{n}} 套。",
  "Fill in at least one entry (base URL + model).": "请至少填写一套（接口地址 + 模型名）。",
  "Global GitLab token": "全局 GitLab 令牌",
  "An api-scope token used for GitLab sync across all projects. Projects then only pick a GitLab project.":
    "用于全部项目 GitLab 同步的 api 权限令牌；配置后各项目只需选择对应的 GitLab 项目。",
  "Access token (set — leave blank to keep)": "访问令牌（已设置，留空则保持不变）",
  "A global token is set.": "已设置全局令牌。",
  "No global token set.": "尚未设置全局令牌。",
  "Stored encrypted; never returned. Enter it here — not in code.": "加密存储且不会返回明文。请在此填写，不要写入代码。",
  "Who can access this project, and at what level.": "管理本项目的访问人员及其权限。",
  "Assign to project (optional)": "分配到项目（可选）",
  "— none —": "— 不分配 —",
  "Shared workspace": "共享工作区",
  Suites: "套件",
  "New suite": "新建套件",
  "Edit suite": "编辑套件",
  Runner: "执行人",
  "Default runner": "默认执行人",
  Cadence: "周期",
  "Cadence (drives reminders; does not auto-run)": "运行周期（用于提醒，不会自动运行）",
  "Next due": "下次到期",
  Last: "上次",
  "run by": "执行人",
  "Owned, assigned test suites with reminders. Anyone can run; each run records who did.":
    "测试套件支持负责人、默认执行人与周期提醒；任何成员均可运行，每次运行都会记录实际执行人。",
  "Cases in this suite": "套件包含的用例",
  "No cases in this project yet.": "本项目暂无用例。",
  "No suites yet.": "暂无套件。",
  "Name is required": "请填写名称",
  "Run started": "已开始运行",
  Notifications: "通知",
  "Mark all read": "全部标为已读",
  "No notifications.": "暂无通知。",
  "Account re-checked": "账号已重新校验",
  "Re-check": "重新校验",
  "Checking…": "校验中…",
  healthy: "正常",
  unhealthy: "异常",
  Environments: "环境",
  "Targets to run against (dev / test / prod), each with its own base URL and accounts.":
    "测试运行的目标环境（开发 / 测试 / 生产），各环境有独立的基础 URL 与账号。",
  default: "默认",
  "Set default": "设为默认",
  "Add environment": "添加环境",
  Environment: "环境",
  "— any environment —": "— 任意环境 —",
  "— project default —": "— 项目默认 —",
  Roles: "角色",
  "Roles a case can run as. Bind each role to an account below; a case picks a role.":
    "用例执行时可使用的角色。请在下方为每个角色绑定账号，用例按所选角色执行。",
  "No roles yet.": "暂无角色。",
  "Add role": "添加角色",
  "Role (optional)": "角色（可选）",
  "— default account —": "— 默认账号 —",
  "Account saved": "账号已保存",
  // 凭据就地编辑（2026-10-06）：改密码不必再"删掉重建"。
  "Edit account": "编辑账号",
  "New password (leave blank to keep the current one)": "新密码（留空则保持原密码）",
  "Verify the login right after saving": "保存后立即验证登录",
  "Changing the username or password discards this account's cached session — the next run logs in again.":
    "修改用户名或密码会丢弃该账号的会话缓存，下次运行会重新登录（这是预期行为）。",
  "Account updated": "账号已更新",
  "Account updated and verified": "账号已更新，登录验证通过",
  "Runs as (role) — which account executes this case": "执行角色（可多个，按顺序切换）",
  "— add a role to the sequence —": "— 添加一个角色到序列 —",
  "Switch order — the case starts as the first one and switches through the rest:": "切换顺序：用例以第 1 个身份开始，流程中依次切换到后面的身份：",
  "Move up": "上移",
  "Move down": "下移",
  "Search cases": "搜索用例",
  "All priorities": "全部优先级",
  "Select all": "全选",
  "No cases match.": "没有匹配的用例。",
  "{{n}} selected · {{m}} modules": "已选 {{n}} 条 · 涉及 {{m}} 个模块",
  "Go to project": "进入项目",
  "Edit project": "编辑项目",
  "Delete project": "删除项目",
  "Project deleted": "项目已删除",
  "No projects match your search.": "没有匹配的项目。",
  "Delete “{{name}}” and all its cases, suites, runs and issues? This cannot be undone.":
    "确认删除「{{name}}」及其全部用例、套件、运行与问题单？此操作不可恢复。",
  "Every signed-in user can access all projects — no need to add members here. Manage accounts under Users.":
    "当前所有登录用户均可访问全部项目，无需在此添加成员；账号统一在「用户」页管理。",
  "Add member": "添加成员",
  "No account yet? Invite them under Users first.": "还没有账号？请先在「用户」页发送邀请。",
  "No members yet.": "暂无成员。",
  "No GitLab token configured yet.": "尚未配置 GitLab 令牌。",
  "Set it in System settings.": "请在「系统设置」中配置。",
  "Linked to {{p}}": "已关联 {{p}}",
  "The GitLab token is managed by an admin under System settings.": "GitLab 令牌由管理员在「系统设置」中统一配置。",

  // first-run onboarding (setup checklist + teaching empty states)
  "Get this project running": "让这个项目跑起来",
  "This card disappears once you're set up.": "配置完成后这张卡片会自动消失。",
  "Set the base URL": "设置 Base URL",
  "Where the agent starts each case.": "每条用例从这里开始跑。",
  "Add a working account": "配置一个可用账号",
  "A credential exists but its session is stale — re-capture it.":
    "已有凭据但登录态已失效 —— 重新抓取一次。",
  "Capture a login session so the agent can reach pages behind sign-in.":
    "抓取登录态，Agent 才能进到登录之后的页面。",
  "Write your first test case": "写第一条测试用例",
  "A case is a natural-language task plus one line saying what counts as a pass.":
    "一条用例 = 一段自然语言任务 + 一句「怎样算通过」。",
  "Run it once": "先跑一次",
  "Start with a single case. First run takes 30–90s — a real browser is launched and recorded.":
    "先跑单条。首次约 30–90 秒 —— 要拉起真实浏览器并录像。",
  "Run a case": "跑一条用例",
  "Act on the result": "处理结果",
  "Green everywhere, or turn a failure into an issue from the report.":
    "全绿收工，或在报告里把失败一键转成问题单。",
  "No test cases yet": "还没有测试用例",
  "A case is a task written in plain language plus one line saying what counts as a pass. An agent carries it out in a real browser.":
    "一条用例 = 一段大白话写的任务 + 一句「怎样算通过」。Agent 会在真实浏览器里照着做。",
  "No suites yet. A suite is a saved set of cases with an owner and a schedule — use it for recurring regression runs.":
    "还没有套件。套件 = 一组用例 + 负责人 + 定时，用来做例行回归。",
  "Writing your first case": "第一条用例怎么写",
  "Agent task": "Agent 任务",
  "brief it like a new colleague, one step at a time.": "像给新同事交代活儿一样，一步一步写。",
  "say what must be on screen for this to pass. The judge only sees evidence; if it's vague, the case fails.":
    "写清楚「屏幕上出现什么才算过」。判定只看证据，写糊了就会判失败。",
  "Runs as": "执行角色",
  "picks which account executes the case.": "决定用哪个账号来执行这条用例。",

  // first screen (no projects yet)
  "Welcome to Potato Test": "欢迎使用 Potato Test",
  "You write test cases as plain-language tasks. An agent carries each one out in a real browser, records a video, and a judge decides pass or fail.":
    "你用大白话把测试用例写成一个任务。Agent 会在真实浏览器里照着做一遍、全程录像，再由判定模型给出通过或失败。",
  "A project holds the cases for one system under test. Creating one takes about a minute — a URL, an account, and your first case.":
    "一个项目装的是同一个被测系统的用例。建一个大约一分钟 —— 一个网址、一个账号、第一条用例。",
  "Create your first project": "创建第一个项目",

  // new-project wizard
  Account: "账号",
  "First case": "第一条用例",
  Next: "下一步",
  Back: "上一步",
  "Where every case starts unless it overrides it. You can change this later in Settings.":
    "所有用例默认从这里开始跑（单条用例可覆盖）。之后可以在「设置」里改。",
  "The agent signs in once and reuses the session. Without an account it can only reach pages that need no login.":
    "Agent 登录一次后复用这个会话。不配账号的话，它只能访问不需要登录的页面。",
  "Session captured.": "登录态已抓取。",
  skipped: "已跳过",
  "Skip — the site needs no login": "跳过 —— 这个站点不需要登录",
  "A case is a task in plain language plus one line saying what counts as a pass. Start with the smoke case below — you can run it right away and see what a report looks like.":
    "一条用例 = 一段大白话任务 + 一句「怎样算通过」。先用下面这条冒烟用例起步 —— 建完就能跑，顺便看看报告长什么样。",
  "Create a starter smoke case": "创建一条起步冒烟用例",
  "Open {{url}} and wait for the home page to finish loading.": "打开 {{url}},等待首页加载完成。",
  "the base URL": "Base URL",
  "The main content is rendered — no error page, no blank screen.":
    "页面主要内容已渲染出来，没有报错页、没有白屏。",
  "Smoke — home page loads": "冒烟 —— 首页可加载",
  "Pass when:": "通过条件:",
  "Already have cases in a spreadsheet?": "已经有 Excel 用例了?",
  "Create case & finish": "创建用例并完成",
  Finish: "完成",

  // first-run report coaching
  "Nothing has finished yet — that's expected. Each case launches a real browser, drives it and records video, so the first result takes roughly 30–90 seconds. Results stream in below as they land; you can leave this page.":
    "还没有结果 —— 这是正常的。每条用例都要拉起一个真实浏览器、操作它并录像；会话过期时还要先重新登录一次，所以第一个结果可能要一两分钟。结果会实时出现在下方，你也可以先离开这个页面。",
  "Your first report — what now?": "第一份报告 —— 接下来做什么?",
  "Everything passed. Save these cases as a suite and give it a schedule, and this becomes a regression run that happens without you.":
    "全部通过。把这些用例存成一个套件并设上定时，它就变成了不用你操心的例行回归。",
  "Create a suite →": "去建套件 →",
  "{{n}} case(s) did not pass. The Reason column has the judge's verdict; “Replay” plays back the video and the agent's steps so you can see where it went wrong, and “+ Issue” turns it into a tracked issue.":
    "有 {{n}} 条没通过。「原因」列是判定结论；点「回放」可以看录像和 Agent 的每一步，看清它卡在哪；点「+ Issue」把它转成问题单跟踪。",

  // guided first-run flow (opens on a user's first sign-in; reopen from the sidebar)
  "Create project": "建项目",
  "Add account": "配账号",
  "Write a case": "写用例",
  "Run it": "跑一次",
  "Start — 4 steps, about 10 minutes": "开始（4 步，约 10 分钟）",
  "Skip — I'll look around myself": "跳过，我自己逛逛",
  "Skip setup": "跳过配置",
  "Step {{n}} of {{total}}": "第 {{n}} 步 / 共 {{total}} 步",
  "A project holds the cases for one system under test.": "一个项目装的是同一个被测系统的用例。",
  "{{n}} case(s) ready.": "已就绪 {{n}} 条用例。",
  "First run": "首次运行",
  "That's the setup. Running it launches a real browser, drives it through your case and records video — the first result takes roughly 30–90 seconds, and you'll watch it stream in.":
    "配置就到这儿。点运行会拉起一个真实浏览器，按你的用例操作一遍并全程录像；会话过期时还要先重新登录一次，所以第一个结果可能要一两分钟，你可以看着它实时出现。",
  "You skipped the account step, so anything behind a login will fail. That's fine for a first look — add an account in Settings when you need it.":
    "你跳过了账号那步，所以需要登录才能看到的页面会失败。第一次看效果没关系 —— 需要时到「设置」里补一个账号。",
  "Finish without running": "先不跑，完成",
  "Run it now": "立即运行",
  "No projects yet": "还没有项目",
  // 证据采集开关（设置页 → 运行）
  "Video recording": "视频录制",
  "Per-step screenshots": "逐步截图",
  "Follow server default": "跟随服务器默认",
  "Off (fastest)": "关闭（最快）",
  "On (replay available)": "开启（可回放）",
  "Every step": "每一步",
  "Every 3 steps": "每 3 步",
  "Every 5 steps": "每 5 步",
  "Regardless of these settings, the final frame is always captured, so a failed case still has a screenshot to look at. Turning both off is the fastest configuration.":
    "无论怎么设置，用例结束时都会拍一张最终截图，失败用例依然有图可看。两个都关掉是最快的配置。",
  // 运行报告里"用例"按钮的悬停提示：光一个"用例"看不出点进去能改
  "View and edit this case: steps, expected result, tags":
    "查看并编辑这条用例：步骤、预期结果、标签",
  // 缺陷描述卡片（FailureNarrativeCard）—— 这几个 key 之前没进 zh 表，
  // 于是**界面上显示了英文标签**（Actual result / Expected result / Bug description），
  // 而"复制"出来的文本用的是硬编码中文【实际结果】【预期结果】，两边对不上。
  // 补齐后界面与复制内容一致。
  "Bug description": "缺陷描述",
  "Actual result": "实际结果",
  "Expected result": "预期结果",
  Copy: "复制",
  Copied: "已复制",
  // 全前端扫描后剩下的 4 条漏翻（扫描正则要注意用词边界 `(?<![A-Za-z])t\(`，
  // 否则 `toast("success", …)` 里的 "success" 会被当成 t() 的 key —— 我踩过这个坑）
  "{{n}} completed runs": "{{n}} 次已完成的运行",
  "A run executes a batch of cases and produces a video, a step-by-step trace and a pass/fail verdict for each.":
    "每次运行执行一批用例，并为每条用例生成录像、逐步轨迹和通过/失败判定。",
  "storage_state JSON (upload auth.json or paste)":
    "storage_state JSON（上传 auth.json 或直接粘贴）",
  // 用例抽屉里的"它学到了什么（经验笔记）"
  "What it learned (experience notes)": "它学到了什么（经验笔记）",
  "Clearing…": "清空中…",
  "Last known navigation path": "上次的导航路径",
  "Page quirks": "页面特性",
  "Element tips": "元素注意事项",
  Updated: "更新于",
  "This case was edited after these notes were written, so they are ignored when it runs. They will be rebuilt on the next run.":
    "这些笔记写下之后用例被改过，所以运行时会被忽略，下次运行会重新积累。",
  "Notes only record how to get around — navigation paths and page quirks. They deliberately never record whether a run passed: that would turn the next run into copying an answer instead of testing.":
    "笔记只记录「路怎么走」——导航路径和界面脾气。它刻意不记录某次运行是否通过：那会让下一次运行变成背答案，而不是真的在测试。",
  // 用例抽屉里的「数据隔离提示」
  "Data isolation note": "数据隔离提示",
  "Filled in when your expected result hard-codes a count (like \"1 row\"): the app's data is changed by other cases, so the note tells the agent not to treat leftovers as a defect. Empty = not injected.":
    "当预期结果里写死了条数（比如「1 行」）时填：应用里的数据会被其他用例改动，这段提示让 agent 不要把残留数据当成缺陷。留空 = 不注入。",
  "Leave empty unless the expected result mentions a specific number of rows":
    "除非预期结果里写死了具体行数，否则留空",
  // 2026-10-08 补齐：以下 4 条在代码里被 t() 使用，但 zh 表里没有登记。
  // 它们本身就是中文，所以界面显示一直是对的（fallback 返回 key 本身）—— 登记它们
  // 不是为了修显示，而是为了**让"还有没有漏网的"这件事可被机械验证**：
  // 只要 zh 表覆盖了全部 t() 字面量，就能写一条断言把"新增文案忘了登记"挡住。
  // 这四个值刻意等于键：它们没有英文源串，将来要出英文版就得在这里补翻译。
  "…": "…",
  "这个项目有多少条用例？": "这个项目有多少条用例？",
  "最近一次运行通过率多少？有哪些失败？": "最近一次运行通过率多少？有哪些失败？",
  // ---- 应用更新（2026-10-17）------------------------------------------------
  Version: "版本",
  "Current version": "当前版本",
  "Check for updates": "检查更新",
  "New version available": "有新版本可用",
  "You're on the latest version.": "已是最新版本。",
  "Download and install": "下载并安装",
  "Downloading…": "下载中…",
  "Open the release page": "打开发布页",
  "Release notes": "更新说明",
  "The app will close and install the update automatically.": "应用将退出并自动安装更新。",
  "Only the desktop app can update in place. For the web version, ask the person who runs the server.":
    "只有桌面版能就地更新；网页版请找部署这台服务的人。",
  "Updates are downloaded from this project's GitHub releases and installed in place; your data is kept.":
    "更新包从本项目的 GitHub Release 下载后就地安装，你的数据会保留。",

  // ---- 原件页内查看（2026-10-19）--------------------------------------------
  正文: "正文",
  "看上传时的原件（PDF / 图片 / 文本可以在这里直接看）": "看上传时的原件（PDF / 图片 / 文本可以在这里直接看）",
  原格式: "原格式",
  在新窗口打开: "在新窗口打开",
  下载原件: "下载原件",
  "原件载入失败，请用上面的「在新窗口打开」或「下载原件」。":
    "原件载入失败，请用上面的「在新窗口打开」或「下载原件」。",
  "正在载入原件…": "正在载入原件…",
  "浏览器不能直接预览 {{ext}} 文件，请下载后用本机程序打开。":
    "浏览器不能直接预览 {{ext}} 文件，请下载后用本机程序打开。",
  这种: "这种",
  "右边「正文」里是解析出来的文字（可以改），但它是有损的；要核对格式请以原件为准。":
    "右边「正文」里是解析出来的文字（可以改），但它是有损的；要核对格式请以原件为准。",

  // ---- 文件存放位置（2026-10-19）--------------------------------------------
  "File storage location": "文件存放位置",
  "Videos, screenshots and browser profiles live next to the app by default. Point them at another drive if C: is tight. Takes effect after restarting the app.":
    "视频、截图和浏览器登录态默认存在程序目录旁边。C 盘紧张时可以改到别的盘，重启应用后生效。",
  "Currently writing to": "当前实际写入",
  "Browser profiles": "浏览器登录态",
  "Default (inside the app folder)": "默认位置（程序目录内）",
  "New location (blank = back to default)": "新位置（留空 = 恢复默认）",
  "Also move the existing files (fast on the same drive, slow across drives)":
    "把现有文件也搬过去（同盘很快，跨盘要等）",
  "Takes effect after restarting the app.": "重启应用后生效。",

  // ---- 密钥恢复页（2026-10-20）----------------------------------------------
  "Credential encryption key": "凭据加密密钥",
  "Saved logins and browser sessions are encrypted with this key. Keep a backup somewhere safe: without it, existing credentials cannot be recovered.":
    "保存的登录态和浏览器会话都用这个密钥加密。请把备份放在安全的地方：一旦丢失，已有凭据将无法恢复。",
  "Back up this key": "备份这个密钥",
  "The key is shown once. Save it now if you have not already.": "密钥只显示一次。还没保存的话，现在就保存。",
  "Anyone with this key can decrypt your saved logins. Do not share it.":
    "拿到这个密钥的人都能解密你保存的登录态，不要分享给他人。",
  "Export backup key": "导出备份密钥",
  "Switch to a backup key": "使用备份密钥切换",
  "Restore from a backup key": "从备份密钥恢复",
  "Paste a key exported earlier. If the database already has encrypted credentials, the key must decrypt them or it will be rejected.":
    "粘贴之前导出的密钥。如果数据库里已有加密凭据，该密钥必须能解密它们，否则会被拒绝。",
  "Paste the key you exported from this app. It is checked against the existing encrypted credentials before anything is written.":
    "粘贴从这个应用导出的密钥。写入任何内容之前，会先拿它校验已有的加密凭据。",
  "Backup key": "备份密钥",
  "Show key again": "重新显示密钥",
  "Import key": "导入密钥",
  "Validating…": "正在校验…",
  "Key restored.": "密钥已恢复。",
  "Key restored. Restart the app to start background workers.":
    "密钥已恢复。重启应用后后台任务才会开始工作。",
  "Only an administrator can export or import the key.": "只有管理员可以导出或导入密钥。",
  "Key status is unavailable. Check that the backend is reachable.":
    "无法获取密钥状态，请确认后端可访问。",
  "Copy failed — select the text and copy it manually.":
    "复制失败 —— 请手动选中文本后复制。",
  "Back to workspace": "返回工作区",
  "Hide": "隐藏",

};

// 只在本文件里读写（init 与 setLang），故不导出。
const LANG_KEY = "tp_lang";

i18n.use(initReactI18next).init({
  resources: { zh: { translation: zh } },
  lng: localStorage.getItem(LANG_KEY) || "zh",
  fallbackLng: "en", // en has no resource -> returns the key (English source)
  keySeparator: false,
  nsSeparator: false,
  interpolation: { escapeValue: false },
});

export function setLang(lng: "zh" | "en") {
  localStorage.setItem(LANG_KEY, lng);
  i18n.changeLanguage(lng);
}

export default i18n;
