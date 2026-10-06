import type { DiagStep } from "../../lib/api";

/**
 * 一步的正文：**点了啥 → 结果**。
 *
 * 以前这里把 thought / action / result / error 四段全平铺出来，一步能占五六行。
 * 测试员扫描 80 步的时间线时真正要的只有两件事 —— 这一步操作了什么、系统回了什么。
 * 模型的 thinking 是它自己说服自己的过程，跟"操作是否符合用例"无关，
 * 所以默认不渲染，交给 useShowThoughts 开关控制（排查"它为什么这么走"时才开）。
 *
 * `detail` 是后端把工具调用压成的人话（click_element_by_index(index=12)）；
 * 只有在新数据里有，历史数据回退到裸工具名 `action`。
 */
export function StepLine({
  step,
  showThoughts = false,
  compact = false,
}: {
  step: DiagStep;
  showThoughts?: boolean;
  compact?: boolean;
}) {
  const label = (step.detail || step.action || "").trim();

  return (
    <div className="min-w-0">
      <div className={compact ? "mb-0.5" : "mb-1"}>
        <span
          className={
            "inline-block break-all rounded bg-brand-50 font-mono font-semibold text-brand-700 " +
            (compact ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-[11px]")
          }
        >
          {label || "—"}
        </span>
      </div>

      {showThoughts && step.thought && (
        <div
          className={
            "text-ink-700 " +
            (compact ? "text-[12px] leading-snug" : "text-[13px] leading-relaxed")
          }
        >
          <span className="mr-1 text-ink-500">💭</span>
          {step.thought}
        </div>
      )}

      {step.result && (
        <div className={"mt-1 text-[var(--ok-fg)] " + (compact ? "text-[11px]" : "text-xs")}>
          ✓ {step.result}
        </div>
      )}
      {step.error && (
        <div className={"mt-1 text-[var(--bad-fg)] " + (compact ? "text-[11px]" : "text-xs")}>
          ✗ {step.error}
        </div>
      )}
    </div>
  );
}
