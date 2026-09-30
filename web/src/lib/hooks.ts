import { useEffect, useRef, useState } from "react";

/**
 * A signal that aborts when the component unmounts.
 *
 * Every page in this app fires a fetch inside useEffect. Without this, a quick
 * route change leaves the old request running: it resolves into a dead
 * component (React warning, wasted work) and — on a slow backend — several
 * stale responses can land after the fresh one and overwrite good data.
 *
 *   const signal = useAbortSignal();
 *   useEffect(() => {
 *     api.listCases(pid, { signal }).then(setCases).catch(ignoreAbort);
 *   }, [pid, signal]);
 */
export function useAbortSignal(): AbortSignal {
  const ref = useRef<AbortController | null>(null);
  if (ref.current === null) ref.current = new AbortController();
  useEffect(() => () => ref.current?.abort(), []);
  return ref.current.signal;
}

/** Swallow the expected "request cancelled" noise so it never reaches a toast. */
export function isAbort(e: unknown): boolean {
  const msg = e instanceof Error ? e.message : String(e);
  return msg.includes("cancelled");
}

/**
 * Debounce a rapidly-changing value (search boxes, filter typing).
 * Keeps list filtering from rescanning a 379-row table on every keystroke.
 */
export function useDebounced<T>(value: T, delayMs = 200): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

/**
 * Poll `fn` on an interval, pausing while the tab is hidden and skipping
 * overlapping runs. Replaces the bare setInterval copies in RunsPage /
 * RunReport, which kept hammering the API in background tabs.
 */
export function usePolling(fn: () => void | Promise<void>, active: boolean, visibleMs = 2500, hiddenMs = 8000) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let running = false;
    let id: ReturnType<typeof setTimeout>;

    const tick = async () => {
      if (stopped || running) return;
      if (document.hidden) return;
      running = true;
      try {
        await saved.current();
      } finally {
        running = false;
      }
    };
    const schedule = () => {
      id = setTimeout(async () => {
        await tick();
        if (!stopped) schedule();
      }, document.hidden ? hiddenMs : visibleMs);
    };
    schedule();

    // refresh immediately when the operator comes back to the tab
    const onVisible = () => {
      if (!document.hidden) void tick();
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      stopped = true;
      clearTimeout(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [active, visibleMs, hiddenMs]);
}
