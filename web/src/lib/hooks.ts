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
 * 「只看操作，不看 AI 思考」开关（默认关）。
 *
 * 时间线的用途是核对「这一步点了啥」。模型的 thinking 是几百字的自言自语，
 * 每步都渲染会把真正的信息淹掉 —— 一份几十步的报告里找一步，等于读一篇小说。
 * 需要排查「它为什么这么走」时才打开。
 *
 * 存 localStorage 是因为这是查看偏好：每次打开报告都要再关一次很烦。
 * 抽成 hook 是因为 ReplayPanel（回放）和 RunReport（实时）都要用同一个开关，
 * 两边各自存一份会出现"回放里开了、实时里还关着"的割裂。
 */
const SHOW_THOUGHTS_KEY = "tp.show_thoughts";

export function useShowThoughts(): [boolean, (v: boolean) => void] {
  const [show, setShow] = useState(() => {
    try {
      return localStorage.getItem(SHOW_THOUGHTS_KEY) === "1";
    } catch {
      return false; // 隐私模式下 localStorage 会抛，退回默认（不看思考）
    }
  });
  const update = (v: boolean) => {
    setShow(v);
    try {
      localStorage.setItem(SHOW_THOUGHTS_KEY, v ? "1" : "0");
    } catch {
      /* 存不下就算了，本次会话内依然生效 */
    }
  };
  return [show, update];
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
