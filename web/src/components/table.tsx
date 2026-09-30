// Shared Recharts styling. This module used to also export usePagedSort /
// SortTh / Pagination, but no page ever imported them — every table in the app
// is rendered inline — so they were removed to keep them out of the bundle.

/** Tooltip skin that matches the panel tokens in both light and dark themes. */
export const chartTooltip = {
  contentStyle: {
    background: "var(--panel)",
    border: "1px solid var(--line)",
    borderRadius: 8,
    color: "var(--text)",
    fontSize: 12,
  },
  labelStyle: { color: "var(--muted)" },
  itemStyle: { color: "var(--text)" },
};
