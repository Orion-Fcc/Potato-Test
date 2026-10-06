import * as SelectPrimitive from "@radix-ui/react-select";
import clsx from "clsx";
import { Check, ChevronDown, Minus } from "lucide-react";
import { Children, isValidElement, useEffect, useRef } from "react";
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactElement,
  ReactNode,
  TextareaHTMLAttributes,
} from "react";

// Hand-rolled primitives on the brand token set (see tailwind.config.js + index.css).

export function Button({
  className,
  variant = "primary",
  size = "md",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "outline" | "ghost" | "danger";
  size?: "sm" | "md";
}) {
  const variants = {
    primary:
      "bg-brand-600 text-[var(--on-brand)] shadow-[0_1px_2px_-1px_color-mix(in_oklch,var(--ink-900)_30%,transparent),0_4px_12px_-6px_color-mix(in_oklch,var(--brand-700)_60%,transparent)] hover:brightness-[1.06] hover:shadow-[0_2px_4px_-2px_color-mix(in_oklch,var(--ink-900)_28%,transparent),0_10px_22px_-10px_color-mix(in_oklch,var(--brand-700)_70%,transparent)]",
    outline:
      "border border-[var(--line)] bg-[var(--panel)] text-ink-700 shadow-[var(--shadow-1)] hover:border-[var(--line-strong)] hover:bg-[var(--panel2)] hover:text-ink-900",
    ghost: "text-ink-700 hover:bg-[var(--panel2)] hover:text-ink-900",
    danger:
      "border border-[color-mix(in_oklch,var(--bad-fg)_32%,transparent)] bg-[var(--panel)] text-[var(--bad-fg)] shadow-[var(--shadow-1)] hover:border-[var(--bad-fg)] hover:bg-[var(--bad-bg)]",
  };
  const sizes = { sm: "h-8 px-3 text-xs", md: "h-9 px-4 text-[13px]" };
  return (
    <button
      className={clsx(
        "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg font-medium outline-none transition-[background-color,border-color,color,box-shadow,transform,filter] duration-150 focus-visible:ring-2 focus-visible:ring-brand-100 active:translate-y-px disabled:pointer-events-none disabled:opacity-40 disabled:shadow-none",
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    />
  );
}

export function Card({
  className,
  children,
  hover,
}: {
  className?: string;
  children: ReactNode;
  /** lift + deepen the shadow on hover (for clickable cards) */
  hover?: boolean;
}) {
  return (
    <div
      className={clsx(
        "tp-card border border-[var(--line)]",
        hover && "tp-card-hover hover:border-[var(--line-strong)]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={clsx(
        "h-9 w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-[13px] text-ink-900 outline-none transition-[border-color,box-shadow] placeholder:text-ink-400/80 focus:border-brand-500 focus:ring-2 focus:ring-brand-100",
        props.className,
      )}
    />
  );
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={clsx(
        "w-full rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-[13px] text-ink-900 outline-none transition-[border-color,box-shadow] placeholder:text-ink-400/80 focus:border-brand-500 focus:ring-2 focus:ring-brand-100",
        props.className,
      )}
    />
  );
}

// Radix-backed Select styled to match the design tokens (the OS-rendered native
// <select> dropdown can't be themed). Drop-in for the old native wrapper: it still
// accepts <option> children plus `value`/`onChange({target:{value}})`, so existing
// call sites keep working unchanged. Empty-string values are round-tripped through a
// sentinel because Radix forbids an item value of "".
const EMPTY = "\u0000empty";

type OptionProps = { value?: string | number; children?: ReactNode; disabled?: boolean };

export function Select({
  value,
  defaultValue,
  onChange,
  onValueChange,
  children,
  className,
  disabled,
  placeholder,
  name,
  variant = "default",
}: {
  value?: string;
  defaultValue?: string;
  onChange?: (e: { target: { value: string } }) => void;
  onValueChange?: (value: string) => void;
  children: ReactNode;
  className?: string;
  disabled?: boolean;
  placeholder?: string;
  name?: string;
  variant?: "default" | "bare";
}) {
  // Default h-9/w-full yield to caller-supplied h-*/w-* — clsx doesn't resolve
  // Tailwind conflicts, so stacking both lets stylesheet order (w-full) win.
  const has = (prefix: string) => new RegExp(`(?:^|\\s)${prefix}-`).test(className ?? "");
  const trigger =
    variant === "bare"
      ? "inline-flex h-auto w-auto items-center gap-1 rounded-md bg-transparent px-1.5 py-0.5 text-[13px] font-medium text-ink-700 outline-none transition-[background-color,color] hover:bg-[var(--panel2)] hover:text-ink-900 disabled:opacity-40 data-[placeholder]:text-ink-400"
      : clsx(
          "flex items-center justify-between gap-2 whitespace-nowrap rounded-lg border border-[var(--line)] bg-[var(--panel)] px-3 text-[13px] text-ink-900 outline-none transition-[border-color,box-shadow] hover:border-[var(--line-strong)] focus:border-brand-500 focus:ring-2 focus:ring-brand-100 disabled:opacity-40 data-[placeholder]:text-ink-400",
          !has("h") && "h-9",
          !has("w") && "w-full",
        );
  const options = Children.toArray(children)
    .filter((c): c is ReactElement<OptionProps> => isValidElement(c))
    .map((c) => ({
      value: String(c.props.value ?? ""),
      label: c.props.children,
      disabled: c.props.disabled,
    }));
  const toRadix = (v: string) => (v === "" ? EMPTY : v);
  const fromRadix = (v: string) => (v === EMPTY ? "" : v);
  const emit = (v: string) => {
    const real = fromRadix(v);
    onValueChange?.(real);
    onChange?.({ target: { value: real } });
  };
  return (
    <SelectPrimitive.Root
      value={value === undefined ? undefined : toRadix(value)}
      defaultValue={defaultValue === undefined ? undefined : toRadix(defaultValue)}
      onValueChange={emit}
      disabled={disabled}
      name={name}
    >
      <SelectPrimitive.Trigger className={clsx(trigger, className)}>
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon>
          <ChevronDown className="h-4 w-4 text-ink-400" />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={4}
          className="z-50 max-h-[min(20rem,var(--radix-select-content-available-height))] min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)] p-1 shadow-[var(--shadow-4)]"
        >
          <SelectPrimitive.Viewport>
            {options.map((o) => (
              <SelectPrimitive.Item
                key={o.value}
                value={toRadix(o.value)}
                disabled={o.disabled}
                className="relative flex cursor-pointer select-none items-center rounded-lg py-1.5 pl-2.5 pr-8 text-[13px] outline-none transition-colors data-[highlighted]:bg-brand-50 data-[highlighted]:text-brand-700 data-[disabled]:pointer-events-none data-[disabled]:opacity-40"
              >
                <SelectPrimitive.ItemText>{o.label}</SelectPrimitive.ItemText>
                <SelectPrimitive.ItemIndicator className="absolute right-2 inline-flex items-center">
                  <Check className="h-4 w-4 text-brand-600" />
                </SelectPrimitive.ItemIndicator>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}

// Checkbox styled on the design tokens — no dependency, just appearance-none + an
// overlaid check. Controlled like the native input it replaces (checked/onChange).
export function Checkbox({
  checked,
  indeterminate,
  onChange,
  disabled,
  className,
}: {
  checked?: boolean;
  indeterminate?: boolean;
  onChange?: (e: { target: { checked: boolean } }) => void;
  disabled?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const half = !!indeterminate && !checked;
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = half;
  }, [half]);
  return (
    <span className={clsx("relative inline-flex h-4 w-4 shrink-0", className)}>
      <input
        ref={ref}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange?.({ target: { checked: e.target.checked } })}
        className={clsx(
          "peer h-4 w-4 cursor-pointer appearance-none rounded-[5px] border outline-none transition-[background-color,border-color,box-shadow] hover:border-brand-500 focus-visible:ring-2 focus-visible:ring-brand-100 disabled:opacity-40 checked:border-brand-600 checked:bg-brand-600",
          half ? "border-brand-600 bg-brand-600" : "border-[var(--line-strong)] bg-[var(--panel)]",
        )}
      />
      {half ? (
        <Minus className="pointer-events-none absolute inset-0 m-auto h-3 w-3 text-[var(--on-brand)]" />
      ) : (
        <Check className="pointer-events-none absolute inset-0 m-auto h-3 w-3 text-[var(--on-brand)] opacity-0 peer-checked:opacity-100" />
      )}
    </span>
  );
}

const STATUS_STYLES: Record<string, string> = {
  passed: "border-transparent bg-[var(--ok-bg)] text-[var(--ok-fg)]",
  completed: "border-transparent bg-[var(--ok-bg)] text-[var(--ok-fg)]",
  failed: "border-transparent bg-[var(--bad-bg)] text-[var(--bad-fg)]",
  error: "border-transparent bg-[var(--warn-bg)] text-[var(--warn-fg)]",
  flaky: "border-transparent bg-[var(--warn-bg)] text-[var(--warn-fg)]",
  running: "border-transparent bg-brand-50 text-brand-700",
  pending: "border-transparent bg-[var(--panel2)] text-ink-500",
  cancelled: "border-transparent bg-[var(--panel2)] text-ink-500",
};

export function Badge({ status, children }: { status?: string; children?: ReactNode }) {
  const running = status === "running";
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-[3px] text-[11.5px] font-medium leading-none",
        STATUS_STYLES[status ?? ""] ?? "border-[var(--line)] bg-[var(--panel2)] text-ink-500",
      )}
    >
      {running && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />}
      {children ?? status}
    </span>
  );
}

/** Small ring gauge — a coverage / pass-rate dial. Replaces the hand-rolled
 *  conic-gradient circles that were duplicated across pages. */
export function StatRing({
  pct,
  size = 52,
  thickness = 5,
  color = "var(--ok)",
  label,
  showLabel = true,
}: {
  pct: number;
  size?: number;
  thickness?: number;
  color?: string;
  /** optional centre text; defaults to the rounded percentage */
  label?: string;
  /** set false when the tile already prints the number — avoids saying it twice */
  showLabel?: boolean;
}) {
  const clamped = Math.max(0, Math.min(100, pct));
  const inner = size - thickness * 2 - 4;
  return (
    <div
      className="relative grid flex-none place-items-center rounded-full"
      style={{
        width: size,
        height: size,
        background: `conic-gradient(${color} ${clamped}%, color-mix(in oklch, var(--panel3) 100%, transparent) 0)`,
        boxShadow: "inset 0 0 0 1px color-mix(in oklch, var(--ink-900) 5%, transparent)",
      }}
    >
      <div
        className="grid place-items-center rounded-full bg-[var(--panel)] font-semibold tabular-nums text-ink-900"
        style={{ width: inner, height: inner, fontSize: size * (showLabel ? 0.27 : 0.3) }}
      >
        {showLabel ? (label ?? `${Math.round(clamped)}%`) : ""}
      </div>
    </div>
  );
}

/** A compact metric tile: label, big number, caption, optional trailing node. */
export function Stat({
  label,
  value,
  sub,
  tone,
  right,
  className,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "ok" | "warn" | "bad" | "brand";
  right?: ReactNode;
  className?: string;
}) {
  const toneCls =
    tone === "ok"
      ? "text-[var(--ok-fg)]"
      : tone === "warn"
        ? "text-[var(--warn-fg)]"
        : tone === "bad"
          ? "text-[var(--bad-fg)]"
          : tone === "brand"
            ? "text-brand-700"
            : "text-ink-900";
  return (
    <Card hover className={clsx("tp-kpi flex items-center justify-between gap-3 p-4", className)}>
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-ink-500">{label}</div>
        <div className={clsx("tp-kpi-num mt-1.5 font-semibold", toneCls)}>{value}</div>
        {sub && <div className="mt-1 text-[11px] text-ink-400">{sub}</div>}
      </div>
      {right}
    </Card>
  );
}

/** Page header — eyebrow + title + subtitle on the left, actions on the right. */
export function PageHeader({
  eyebrow,
  title,
  subtitle,
  actions,
}: {
  eyebrow?: string;
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {eyebrow && <div className="tp-eyebrow mb-1.5">{eyebrow}</div>}
        <h1 className="tp-h1 text-[1.8rem] font-semibold leading-tight">{title}</h1>
        {subtitle && <p className="mt-1.5 text-[13px] text-ink-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}


/**
 * Pulsing brand dot that marks a live/running row, badge or label. Used in the
 * run report for "N running" chips and per-case status pills; previously the
 * same <span className="h-1.5 w-1.5 animate-pulse …"> markup was repeated six
 * times with subtly different class ordering.
 */
export function LiveDot({ className }: { className?: string }) {
  return <span aria-hidden className={clsx("h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-brand-600", className)} />;
}

// error 是后加的：声明式输入（JSON/正则/路径）出错时，光标停在框里没有任何线索，
// 用户只能猜。放在 Field 上而不是每个调用点自己渲染，是为了让"出错了长什么样"
// 在整个应用里是同一套 —— 分散渲染的红色提示最后会变成五种样子。
export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className="block space-y-1.5">
      <span className="block text-xs font-medium tracking-wide text-ink-500">{label}</span>
      {children}
      {hint && !error && <span className="block text-xs leading-relaxed text-ink-500">{hint}</span>}
      {error && (
        <span role="alert" className="block text-xs leading-relaxed text-red-600 dark:text-red-400">
          {error}
        </span>
      )}
    </label>
  );
}

// Lazy-route skeleton: keeps the page shell visually stable while a route
// chunk downloads, instead of flashing a blank screen.
export function RouteFallback() {
  return (
    <div className="tp-page tp-rise" aria-busy="true" aria-live="polite">
      <div className="flex items-end justify-between gap-4">
        <div className="space-y-2">
          <div className="tp-skel h-3 w-20" />
          <div className="tp-skel h-7 w-52" />
        </div>
        <div className="tp-skel h-8 w-28" />
      </div>
      <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="tp-card border border-[var(--line)] p-4">
            <div className="tp-skel h-3 w-24" />
            <div className="tp-skel mt-3 h-7 w-16" />
            <div className="tp-skel mt-3 h-3 w-32" />
          </div>
        ))}
      </div>
      <div className="tp-card mt-4 border border-[var(--line)] p-5">
        <div className="tp-skel h-4 w-40" />
        <div className="tp-skel mt-4 h-3 w-full" />
        <div className="tp-skel mt-2 h-3 w-11/12" />
        <div className="tp-skel mt-2 h-3 w-4/5" />
      </div>
    </div>
  );
}
