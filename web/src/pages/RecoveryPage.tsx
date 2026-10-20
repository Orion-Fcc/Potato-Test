import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import { useErrorToast, useToast } from "../components/toast";
import { Button, Card, Field, Input } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";

/** Key state → human sentence. State values come from app/key_store.py. */
const STATE_LABEL: Record<string, string> = {
  ok_env: "Key loaded from POTATO_SECRET_KEY",
  ok_dpapi: "Key loaded from the protected key file",
  ok_legacy_migrated: "Legacy key migrated and protected",
  ok_generated: "A new key was generated on this device",
  missing: "No key found, but this database has encrypted credentials",
  mismatch: "The key found cannot decrypt the existing credentials",
  invalid: "The key file is not a valid key",
};

/** Recovery / key-management page.
 *
 * Reached automatically when the server reports `can_use_credentials=false`, and
 * available at /recovery at any time for exporting a backup. It must stay outside
 * the `RequireKeyReady` guard — otherwise a broken key locks the user out of the
 * only page that can fix it.
 */
export function RecoveryPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const fail = useErrorToast();
  const { keyStatus, refreshKeyStatus, isAdmin } = useAuth();

  const [key, setKey] = useState("");
  const [exported, setExported] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const ready = Boolean(keyStatus?.can_use_credentials);

  const doExport = async () => {
    setError("");
    setBusy(true);
    try {
      const r = await api.exportSecretKey();
      setExported(r.key);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const doImport = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const status = await api.importSecretKey(key.trim());
      setKey("");
      await refreshKeyStatus();
      toast(
        "success",
        status.restart_required
          ? t("Key restored. Restart the app to start background workers.")
          : t("Key restored."),
      );
      navigate("/", { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(exported);
      toast("success", t("Copied"));
    } catch {
      setError(t("Copy failed — select the text and copy it manually."));
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--surface)] p-6">
      <Card className="w-full max-w-lg p-6">
        <div className="text-lg font-semibold text-ink-900">{t("Credential encryption key")}</div>
        <p className="mt-1 text-xs leading-relaxed text-ink-500">
          {t(
            "Saved logins and browser sessions are encrypted with this key. Keep a backup somewhere safe: without it, existing credentials cannot be recovered.",
          )}
        </p>

        {!keyStatus ? (
          <div className="tp-alert tp-alert-info mt-4 px-3 py-2 text-xs">
            <div>{t("Key status is unavailable. Check that the backend is reachable.")}</div>
            <Button variant="outline" size="sm" className="mt-2" onClick={() => void refreshKeyStatus()}>
              {t("Retry")}
            </Button>
          </div>
        ) : (
          <div className={`mt-4 rounded-lg border px-3 py-2 ${ready ? "tp-alert-info" : "tp-alert-bad"}`}>
            <div className="text-[13px] font-medium">
              {t(STATE_LABEL[keyStatus.state] ?? keyStatus.state)}
            </div>
            {keyStatus.reason && <div className="mt-1 text-xs leading-relaxed">{keyStatus.reason}</div>}
            {keyStatus.key_file && (
              <div className="mt-2 break-all font-mono text-[11px] text-ink-500">{keyStatus.key_file}</div>
            )}
          </div>
        )}

        {!isAdmin ? (
          <div className="tp-alert tp-alert-warn mt-4 px-3 py-2 text-xs">
            {t("Only an administrator can export or import the key.")}
          </div>
        ) : (
          <>
            {ready && (
              <div className="mt-5 border-t border-[var(--line)] pt-4">
                <div className="text-[13px] font-medium text-ink-900">{t("Back up this key")}</div>
                <p className="mt-1 text-xs text-ink-500">
                  {t("The key is shown once. Save it now if you have not already.")}
                </p>
                <Button className="mt-2" variant="outline" size="sm" disabled={busy} onClick={() => void doExport()}>
                  {exported ? t("Show key again") : t("Export backup key")}
                </Button>
                {exported && (
                  <div className="mt-3 space-y-2">
                    <div className="tp-alert tp-alert-warn px-3 py-2 text-xs">
                      {t("Anyone with this key can decrypt your saved logins. Do not share it.")}
                    </div>
                    <Input readOnly value={exported} className="font-mono" onFocus={(e) => e.target.select()} />
                    <div className="flex gap-2">
                      <Button size="sm" variant="outline" onClick={() => void copy()}>
                        {t("Copy")}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setExported("")}>
                        {t("Hide")}
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )}

            <form onSubmit={doImport} className="mt-5 space-y-3 border-t border-[var(--line)] pt-4">
              <div className="text-[13px] font-medium text-ink-900">
                {ready ? t("Switch to a backup key") : t("Restore from a backup key")}
              </div>
              <p className="text-xs leading-relaxed text-ink-500">
                {ready
                  ? t(
                      "Paste a key exported earlier. If the database already has encrypted credentials, the key must decrypt them or it will be rejected.",
                    )
                  : t(
                      "Paste the key you exported from this app. It is checked against the existing encrypted credentials before anything is written.",
                    )}
              </p>
              <Field label={t("Backup key")}>
                <Input
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  placeholder="POTATO_SECRET_KEY"
                  className="font-mono"
                  autoFocus={!ready}
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>
              {error && <div className="tp-alert tp-alert-bad px-3 py-2 text-xs">{error}</div>}
              <Button type="submit" className="w-full" disabled={busy || !key.trim()}>
                {busy ? t("Validating…") : t("Import key")}
              </Button>
            </form>
          </>
        )}

        {ready && (
          <Link to="/" className="mt-4 block text-center text-xs text-ink-500 hover:text-ink-900">
            {t("Back to workspace")}
          </Link>
        )}
      </Card>
    </div>
  );
}
