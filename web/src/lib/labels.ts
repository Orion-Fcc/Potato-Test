// Display labels for enum-valued fields. These are NOT i18n keys: the app's i18n
// scheme uses the English string as the key with only a zh bundle, so machine keys
// like "type.functional" render raw in EN. Keep these as explicit {zh,en} maps.

type L = Record<string, { zh: string; en: string }>;

export const CASE_TYPE_LABELS: L = {
  functional: { zh: "功能", en: "Functional" },
  smoke: { zh: "冒烟", en: "Smoke" },
  regression: { zh: "回归", en: "Regression" },
  acceptance: { zh: "验收", en: "Acceptance" },
  negative: { zh: "反向", en: "Negative" },
};

export const CASE_STATUS_LABELS: L = {
  draft: { zh: "草稿", en: "Draft" },
  active: { zh: "生效", en: "Active" },
  deprecated: { zh: "废弃", en: "Deprecated" },
  // 2026-10-09：人和 agent 都可能跑不了的用例。与"废弃"分开，因为后续动作相反 ——
  // 废弃是"这条不要了"，受阻是"这条有效，但当前跑不了，得想办法"。
  blocked: { zh: "受阻", en: "Blocked" },
};

/** P0-P3 are shown verbatim (they are the industry-standard shorthand), so the map
 *  exists mainly to give the bare "P0" a readable tooltip/label where one is wanted. */
export const PRIORITY_LABELS: L = {
  P0: { zh: "P0 (最高)", en: "P0 (highest)" },
  P1: { zh: "P1 (高)", en: "P1 (high)" },
  P2: { zh: "P2 (中)", en: "P2 (medium)" },
  P3: { zh: "P3 (低)", en: "P3 (low)" },
};

export const ISSUE_STATUS_LABELS: L = {
  open: { zh: "待处理", en: "Open" },
  in_progress: { zh: "处理中", en: "In progress" },
  fixed: { zh: "已修复", en: "Fixed" },
  verified: { zh: "已验证", en: "Verified" },
  closed: { zh: "已关闭", en: "Closed" },
};

export const SEVERITY_LABELS: L = {
  low: { zh: "低", en: "Low" },
  medium: { zh: "中", en: "Medium" },
  high: { zh: "高", en: "High" },
  critical: { zh: "严重", en: "Critical" },
};

// 2026-10-09 删掉 RUN_STATUS_LABELS / RESULT_STATUS_LABELS（原第 45-64 行）。
//
// 它们看着像"状态文案的唯一来源"，其实两个问题都有：
//   1. **没有任何消费点**（knip 报 unused export）。运行/结果状态是用 i18n 翻的：
//      `t(r.status)`，而 i18n.ts 里 pending/running/completed/failed/cancelled/
//      passed/error/skipped 都已经有词条 —— 两套并存只会让人不知道该改哪套。
//   2. **内容已经过时**：RUN_STATUS_LABELS 里有 `queued`，但后端 Run.status 的取值是
//      pending|running|completed|failed|cancelled（见 app/models.py），从来没有 queued；
//      RESULT_STATUS_LABELS 里还留着 `skipped`。
//      留着它们等于在仓库里放一份"看起来权威、实际是错的"的对照表。

export const CADENCE_LABELS: L = {
  none: { zh: "手动(不提醒)", en: "Manual (no reminders)" },
  daily: { zh: "每日", en: "Daily" },
  weekly: { zh: "每周", en: "Weekly" },
  biweekly: { zh: "每两周", en: "Biweekly" },
  monthly: { zh: "每月", en: "Monthly" },
};

/** Look up a label for the current language; falls back to the raw key. */
export function label(map: L, key: string, lang: string): string {
  const e = map[key];
  return e ? (lang.startsWith("zh") ? e.zh : e.en) : key;
}

/**
 * 枚举值 → 首字母大写的可读名（"admin" → "Admin"）。
 *
 * 原先 AdminUsersPage 与 MembersPage 各抄了一份一模一样的实现；
 * 角色是后端自由文本，没有对应的 i18n 词条，所以这条规则必须有**唯一**来源 ——
 * 否则同一个角色在两个页面里可能显示成两种样子。
 */
export function titleCase(v: string): string {
  return v ? v.charAt(0).toUpperCase() + v.slice(1) : v;
}
