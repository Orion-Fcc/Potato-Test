import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type AuthUser } from "./api";

type AuthState = {
  loading: boolean;
  authEnabled: boolean;
  sharedWorkspace: boolean;
  publicBaseUrl: string;
  gitlabEnabled: boolean;
  feishuEnabled: boolean;
  /** Effective admin: a real admin when auth is on; everyone when it is off.
   *  With AUTH_ENABLED=false there is no user row, so `user?.is_admin` is always
   *  falsy — that used to hide 系统设置 (the model/API-key page) from the sidebar
   *  entirely, making the LLM config unreachable in a local install. */
  isAdmin: boolean;
  user: AuthUser | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
};

const Ctx = createContext<AuthState>(null as unknown as AuthState);

export function useAuth() {
  return useContext(Ctx);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [sharedWorkspace, setSharedWorkspace] = useState(true);
  const [publicBaseUrl, setPublicBaseUrl] = useState("");
  const [gitlabEnabled, setGitlabEnabled] = useState(false);
  const [feishuEnabled, setFeishuEnabled] = useState(false);
  const [user, setUser] = useState<AuthUser | null>(null);

  const refresh = async () => {
    const cfg = await api
      .config()
      .catch(() => ({
        auth_enabled: false,
        shared_workspace: true,
        public_base_url: "",
        gitlab_enabled: false,
        feishu_enabled: false,
      }));
    setAuthEnabled(cfg.auth_enabled);
    setSharedWorkspace(cfg.shared_workspace);
    setPublicBaseUrl(cfg.public_base_url ?? "");
    setGitlabEnabled(cfg.gitlab_enabled ?? false);
    setFeishuEnabled(cfg.feishu_enabled ?? false);
    if (cfg.auth_enabled) setUser(await api.me().catch(() => null));
    else setUser(null);
  };

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, []);

  const login = async (email: string, password: string) => {
    setUser(await api.login(email, password));
  };
  const logout = async () => {
    await api.logout().catch(() => {});
    setUser(null);
  };

  // No-auth installs have no user row but are owned by whoever runs them, so they
  // get the admin pages (model/API-key settings). With auth on, honour the flag.
  const isAdmin = !authEnabled || Boolean(user?.is_admin);

  return (
    <Ctx.Provider
      value={{
        loading,
        authEnabled,
        sharedWorkspace,
        publicBaseUrl,
        gitlabEnabled,
        feishuEnabled,
        isAdmin,
        user,
        login,
        logout,
        refresh,
      }}
    >
      {children}
    </Ctx.Provider>
  );
}
