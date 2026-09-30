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

/** Run status (queued/running/completed/…) — used by badges and filters. */
export const RUN_STATUS_LABELS: L = {
  pending: { zh: "排队中", en: "Pending" },
  queued: { zh: "排队中", en: "Queued" },
  running: { zh: "运行中", en: "Running" },
  completed: { zh: "已完成", en: "Completed" },
  failed: { zh: "失败", en: "Failed" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  error: { zh: "出错", en: "Error" },
};

/** Per-case result status. */
export const RESULT_STATUS_LABELS: L = {
  passed: { zh: "通过", en: "Passed" },
  failed: { zh: "失败", en: "Failed" },
  error: { zh: "出错", en: "Error" },
  running: { zh: "运行中", en: "Running" },
  pending: { zh: "等待中", en: "Pending" },
  skipped: { zh: "已跳过", en: "Skipped" },
};

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
