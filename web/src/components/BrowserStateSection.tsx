import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type BrowserState } from "../lib/api";
import { Button, Card } from "./ui";
import { useErrorToast } from "./toast";

/**
 * Workspace-scoped persistent browser state.
 *
 * Potato Test keeps one Chromium user-data-dir PER PROJECT and reuses it between cases and
 * between runs, so cookies / localStorage / sessionStorage / IndexedDB (and the HTTP
 * cache) are already warm when the next case starts. That is what makes a regression
 * re-run skip the login form and the cold asset fetch.
 */
export function BrowserStateSection({ pid }: { pid: number }) {
  const { t } = useTranslation();
  const fail = useErrorToast();
  const [state, setState] = useState<BrowserState | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api
      .getBrowserState(pid)
      .then(setState)
      .catch((e) => fail(e));

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid]);

  const reset = async () => {
    if (!window.confirm(t("Wipe this project's saved browser state? Every case will have to log in again."))) return;
    setBusy(true);
    try {
      await api.resetBrowserState(pid);
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const warm = state?.slots.filter((s) => s.warm).length ?? 0;
  const mb = ((state?.total_bytes ?? 0) / 1024 / 1024).toFixed(1);

  return (
    <Card className="space-y-3 p-5">
      <div>
        <div className="flex items-center gap-2 text-sm font-semibold text-ink-900">
          {t("Browser state (workspace persistence)")}
          {state && (
            <span
              className={
                "rounded px-1.5 py-0.5 text-[10px] font-medium " +
                (state.enabled
                  ? "bg-[var(--ok-bg,#e8f5e9)] text-[var(--ok-fg,#2e7d32)]"
                  : "bg-[var(--panel2)] text-ink-500")
              }
            >
              {state.enabled ? t("on") : t("off")}
            </span>
          )}
        </div>
        <div className="mt-0.5 text-xs text-ink-500">
          {t(
            "Cookies, localStorage, sessionStorage and IndexedDB are kept per project between cases and between runs, so a re-run starts already logged in with assets cached. Isolated per project — one workspace never sees another's session.",
          )}
        </div>
      </div>

      {state && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-500">
          <span>
            {t("slots")}: <span className="tabular-nums text-ink-700">{state.slots.length}</span>
          </span>
          <span>
            {t("warm")}: <span className="tabular-nums text-ink-700">{warm}</span>
          </span>
          <span>
            {t("size")}: <span className="tabular-nums text-ink-700">{mb} MB</span>
          </span>
        </div>
      )}

      <Button variant="outline" onClick={reset} disabled={busy}>
        {busy ? t("Resetting…") : t("Reset browser state")}
      </Button>
    </Card>
  );
}
