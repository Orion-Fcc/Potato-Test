/**
 * Shared metric helpers — one source of truth for pass-rate wording, duration
 * formatting and status tones.
 *
 * Why this file exists: the pass-rate → colour rule was copy-pasted into four
 * pages (Overview, Cases, Runs, Projects), two of them in 0-1 units and two in
 * 0-100, which is exactly the kind of drift that turns a green dashboard red.
 * Everything here is pure and side-effect free.
 */

/** Tone names understood by <Stat tone=…> and the --ok/--warn/--bad tokens. */
export type Tone = "ok" | "warn" | "bad";

/** Pass-rate thresholds, in ONE place. 0-1 unit. */
export const PASS_GOOD = 0.9;
export const PASS_FAIR = 0.6;

/**
 * Map a pass rate to a tone.
 * @param rate 0-1. Accepts null/undefined for "no data" → no tone.
 */
export function rateTone(rate: number | null | undefined): Tone | undefined {
  if (rate == null || Number.isNaN(rate)) return undefined;
  if (rate >= PASS_GOOD) return "ok";
  if (rate >= PASS_FAIR) return "warn";
  return "bad";
}

/** CSS colour for a pass rate — for rings, bars and dots that need a fill. */
export function rateColor(rate: number | null | undefined): string {
  const tone = rateTone(rate);
  return tone === "ok" ? "var(--ok)" : tone === "warn" ? "var(--warn)" : tone === "bad" ? "var(--bad)" : "var(--panel3)";
}

/** Format a 0-1 rate as a whole percentage. `null` renders as an em dash. */
export function pct(rate: number | null | undefined): string {
  return rate == null ? "—" : `${Math.round(rate * 100)}%`;
}

/**
 * Compact duration from milliseconds: "42s", "3m07s", "1h04m".
 * Returns an em dash when the input is not a usable span.
 */
export function fmtMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

/** Compact duration between two ISO timestamps. */
export function fmtSpan(startedAt: string | null | undefined, finishedAt: string | null | undefined): string {
  if (!startedAt || !finishedAt) return "—";
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
  return fmtMs(ms);
}

/** "HH:MM:SS" wall-clock time from an ISO timestamp — run start/end stamps. */
export function fmtClockTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(
    d.getSeconds(),
  ).padStart(2, "0")}`;
}
