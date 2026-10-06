import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, relTime, type Credential, type Environment } from "../lib/api";
import { Badge, Button, Card, Checkbox, Field, Input, Select, Textarea } from "./ui";
import { Modal } from "./Modal";
import { CaptureProgressModal } from "./CaptureProgressModal";
import { useToast, useErrorToast } from "./toast";

export function CredentialsSection({
  pid,
  roles: propRoles,
  envs = [],
  onChanged,
}: {
  pid: number;
  roles?: string[];
  envs?: Environment[];
  onChanged?: () => void;
}) {
  const toast = useToast();
  const fail = useErrorToast();
  const { t } = useTranslation();
  const [creds, setCreds] = useState<Credential[]>([]);
  const [fetchedRoles, setFetchedRoles] = useState<string[]>([]);
  const roles = propRoles ?? fetchedRoles;
  const [type, setType] = useState<"storage_state" | "password">("password");
  const [role, setRole] = useState("");
  const [envId, setEnvId] = useState("");
  const envName = (id: number | null) => envs.find((e) => e.id === id)?.name;
  const [label, setLabel] = useState("");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [capture, setCapture] = useState<{ username: string; password: string; label: string; role: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = () =>
    api
      .listCredentials(pid)
      .then((c) => {
        setCreds(c);
        onChanged?.();
      })
      .catch((e) => fail(e));
  useEffect(() => {
    load();
    if (!propRoles) api.getRoles(pid).then((r) => setFetchedRoles(r.roles)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid]);

  const reset = () => {
    setLabel("");
    setUsername("");
    setSecret("");
    setRole("");
    setEnvId("");
  };

  const add = async () => {
    if (type === "password") {
      if (!username.trim() || !secret.trim()) {
        toast("error", t("Enter the test account username and password"));
        return;
      }
      // role accounts are stored directly (the agent prompt-logs in per case); the
      // default account (no role) goes through capture to validate + snapshot the session.
      if (role) {
        setSaving(true);
        try {
          await api.createCredential(pid, {
            type: "password",
            role,
            environment_id: envId ? Number(envId) : undefined,
            label: label || undefined,
            username,
            secret,
          });
          toast("success", t("Account saved"));
          reset();
          load();
        } catch (e) {
          fail(e);
        } finally {
          setSaving(false);
        }
        return;
      }
      setCapture({ username, password: secret, label, role });
      return;
    }
    if (!secret.trim()) {
      toast("error", "Paste or upload the login state");
      return;
    }
    setSaving(true);
    try {
      await api.createCredential(pid, {
        type,
        role: role || undefined,
        environment_id: envId ? Number(envId) : undefined,
        label: label || undefined,
        secret,
      });
      toast("success", t("Credential saved & activated"));
      reset();
      load();
    } catch (e) {
      fail(e);
    } finally {
      setSaving(false);
    }
  };

  const onFile = async (f: File) => {
    setSecret(await f.text());
    if (!label) setLabel(f.name.replace(/\.json$/i, ""));
  };

  // 2026-10-06 就地编辑既有凭据。
  // 此前一条凭据只有「激活 / 重新检测 / 删除」，改密码只能删了重建 ——
  // 而删掉会连 role、环境、会话缓存一起丢掉，还得重走一遍验证。
  // 密码轮换（把 13 个 role 账号统一改成新密码）是常规运维动作，
  // 必须是一条独立、可重复执行的操作。
  const [editing, setEditing] = useState<Credential | null>(null);
  const [eLabel, setELabel] = useState("");
  const [eUsername, setEUsername] = useState("");
  const [eSecret, setESecret] = useState("");
  const [eRole, setERole] = useState("");
  const [eEnvId, setEEnvId] = useState("");
  const [eVerify, setEVerify] = useState(true);
  const [eSaving, setESaving] = useState(false);

  const openEdit = (c: Credential) => {
    setEditing(c);
    setELabel(c.label || "");
    setEUsername(c.username || "");
    setESecret(""); // 留空 = 保持原密码。凭据的明文永远不会回到前端。
    setERole(c.role || "");
    setEEnvId(c.environment_id != null ? String(c.environment_id) : "");
    setEVerify(c.type === "password");
  };

  const saveEdit = async (close: () => void) => {
    if (!editing) return;
    if (editing.type === "password" && !eUsername.trim()) {
      toast("error", t("Enter the test account username and password"));
      return;
    }
    const body: {
      label?: string;
      username?: string;
      secret?: string;
      role?: string | null;
      environment_id?: number | null;
    } = {
      label: eLabel,
      role: eRole || null,
      environment_id: eEnvId ? Number(eEnvId) : null,
    };
    if (editing.type === "password") body.username = eUsername.trim();
    // 空密码 = 不改密码。不能把空串传下去（后端 min_length=1 会拒绝，
    // 更糟的是会让人以为"清空了密码"）。
    if (eSecret.trim()) body.secret = eSecret;

    setESaving(true);
    try {
      await api.updateCredential(editing.id, body);
      // 改了密码却不知道新密码对不对，等于没改完 —— 默认顺手验证一次。
      if (eVerify && editing.type === "password") {
        await api.recheckCredential(editing.id);
        toast("success", t("Account updated and verified"));
      } else {
        toast("success", t("Account updated"));
      }
      close();
      setEditing(null);
      load();
    } catch (e) {
      fail(e);
      load();
    } finally {
      setESaving(false);
    }
  };

  const [rechecking, setRechecking] = useState<number | null>(null);
  const recheck = async (cid: number) => {
    setRechecking(cid);
    try {
      await api.recheckCredential(cid);
      toast("success", t("Account re-checked"));
      load();
    } catch (e) {
      fail(e);
      load();
    } finally {
      setRechecking(null);
    }
  };

  return (
    <Card className="space-y-4 p-4">
      <div>
        <div className="text-sm font-medium text-ink-900">{t("Credentials")}</div>
        <div className="text-xs text-ink-500">
          {t("Logins used to reach the app under test. Stored encrypted; the active one is injected at run time.")}
        </div>
      </div>

      {creds.length > 0 && (
        <div className="divide-y divide-[var(--line)] rounded-lg border border-[var(--line)]">
          {creds.map((c) => (
            <div key={c.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span
                className={`h-2 w-2 shrink-0 rounded-full ${c.healthy ? "bg-[var(--ok-fg)]" : "bg-[var(--bad-fg)]"}`}
                title={c.healthy ? t("healthy") : c.last_error ?? t("unhealthy")}
              />
              <span className="font-medium text-ink-900">{c.label || c.type}</span>
              {c.role && (
                <span className="rounded-full bg-brand-50 px-2 py-0.5 text-[11px] font-medium text-brand-700">
                  {c.role}
                </span>
              )}
              {c.environment_id != null && envName(c.environment_id) && (
                <span className="rounded-full bg-[var(--panel2)] px-2 py-0.5 text-[11px] text-ink-500">
                  {envName(c.environment_id)}
                </span>
              )}
              <span className="rounded-full bg-[var(--panel2)] px-2 py-0.5 text-[11px] text-ink-500">
                {c.type === "password" ? `${t("session")}: ${c.username}` : t("session")}
              </span>
              {c.is_active && <Badge status="passed">{t("active")}</Badge>}
              <span className="ml-auto text-xs text-ink-500">{relTime(c.created_at)}</span>
              <Button size="sm" variant="outline" onClick={() => openEdit(c)}>
                {t("Edit")}
              </Button>
              {c.type === "password" && (
                <Button size="sm" variant="outline" disabled={rechecking === c.id} onClick={() => recheck(c.id)}>
                  {rechecking === c.id ? t("Checking…") : t("Re-check")}
                </Button>
              )}
              {!c.is_active && (
                <Button size="sm" variant="outline" onClick={() => api.activateCredential(c.id).then(load)}>
                  {t("Activate")}
                </Button>
              )}
              <Button
                size="sm"
                variant="danger"
                onClick={() => api.deleteCredential(c.id).then(load).catch((e) => fail(e))}
              >
                {t("Delete")}
              </Button>
            </div>
          ))}
        </div>
      )}

      <div className="space-y-3 rounded-lg border border-dashed border-[var(--line)] p-3">
        <div className="flex items-center gap-3">
          <Field label={t("Type")}>
            <Select
              value={type}
              onChange={(e) => setType(e.target.value as "storage_state" | "password")}
            >
              <option value="password">{t("Test account — auto login (recommended)")}</option>
              <option value="storage_state">{t("Session snapshot (advanced)")}</option>
            </Select>
          </Field>
          <Field label={t("Role (optional)")}>
            <Select value={role} onValueChange={setRole}>
              <option value="">{t("— default account —")}</option>
              {roles.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </Select>
          </Field>
          {envs.length > 0 && (
            <Field label={t("Environment")}>
              <Select value={envId} onValueChange={setEnvId}>
                <option value="">{t("— any environment —")}</option>
                {envs.map((e) => (
                  <option key={e.id} value={e.id}>{e.name}</option>
                ))}
              </Select>
            </Field>
          )}
          <div className="flex-1">
            <Field label={t("Label (optional)")}>
              <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. staging session" />
            </Field>
          </div>
        </div>

        {type === "password" ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("Username")}>
              <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="robot test account" />
            </Field>
            <Field label={t("Password")}>
              <Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="••••••••" />
            </Field>
          </div>
        ) : (
          <Field label={t("storage_state JSON (upload auth.json or paste)")}>
            <div className="space-y-2">
              <Button variant="outline" size="sm" onClick={() => fileRef.current?.click()}>
                {t("Upload auth.json")}
              </Button>
              <input
                ref={fileRef}
                type="file"
                accept="application/json,.json"
                className="hidden"
                onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
              />
              <Textarea
                rows={3}
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder='{"cookies":[…],"origins":[…]}'
              />
            </div>
          </Field>
        )}

        <Button onClick={add} disabled={saving}>
          {saving
            ? type === "password"
              ? t("Logging in…")
              : t("Saving…")
            : type === "password"
              ? t("Log in & capture session")
              : t("Save credential")}
        </Button>
        <p className="text-xs text-ink-500">
          {type === "password"
            ? t("The server logs into the project's Base URL with this account and captures the session (simple captchas are read automatically). No local setup needed.")
            : "Advanced: paste/upload a storage_state you captured elsewhere. Requires POTATO_SECRET_KEY on the server."}
        </p>
      </div>

      {editing && (
        <Modal
          onClose={() => {
            setEditing(null);
          }}
          className="max-w-lg"
        >
          {(close) => (
            <Card className="space-y-4 p-5">
              <div>
                <h2 className="text-base font-semibold text-ink-900">{t("Edit account")}</h2>
                <p className="mt-1 text-sm text-ink-500">{editing.label || editing.type}</p>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <Field label={t("Label (optional)")}>
                  <Input value={eLabel} onChange={(e) => setELabel(e.target.value)} />
                </Field>
                <Field label={t("Role (optional)")}>
                  <Select value={eRole} onValueChange={setERole}>
                    <option value="">{t("— default account —")}</option>
                    {roles.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </Select>
                </Field>
                {editing.type === "password" && (
                  <>
                    <Field label={t("Username")}>
                      <Input value={eUsername} onChange={(e) => setEUsername(e.target.value)} />
                    </Field>
                    <Field label={t("New password (leave blank to keep the current one)")}>
                      <Input
                        type="password"
                        value={eSecret}
                        onChange={(e) => setESecret(e.target.value)}
                        placeholder="••••••••"
                      />
                    </Field>
                  </>
                )}
                {envs.length > 0 && (
                  <Field label={t("Environment")}>
                    <Select value={eEnvId} onValueChange={setEEnvId}>
                      <option value="">{t("— any environment —")}</option>
                      {envs.map((e) => (
                        <option key={e.id} value={e.id}>
                          {e.name}
                        </option>
                      ))}
                    </Select>
                  </Field>
                )}
              </div>

              {editing.type === "password" && (
                <>
                  <label className="flex cursor-pointer select-none items-center gap-2 text-sm text-ink-700">
                    <Checkbox checked={eVerify} onChange={(e) => setEVerify(e.target.checked)} />
                    {t("Verify the login right after saving")}
                  </label>
                  <p className="text-xs text-ink-500">
                    {t(
                      "Changing the username or password discards this account's cached session — the next run logs in again.",
                    )}
                  </p>
                </>
              )}

              <div className="flex items-center justify-end gap-2">
                <Button variant="outline" onClick={close}>
                  {t("Cancel")}
                </Button>
                <Button onClick={() => saveEdit(close)} disabled={eSaving}>
                  {eSaving ? t("Saving…") : t("Save")}
                </Button>
              </div>
            </Card>
          )}
        </Modal>
      )}

      {capture && (
        <CaptureProgressModal
          pid={pid}
          username={capture.username}
          password={capture.password}
          label={capture.label}
          onClose={() => setCapture(null)}
          onDone={() => {
            reset();
            load();
          }}
        />
      )}
    </Card>
  );
}
