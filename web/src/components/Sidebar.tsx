import clsx from "clsx";
import {
  Bug,
  ChevronUp,
  FlaskConical,
  GitCompareArrows,
  Layers,
  LayoutDashboard,
  LayoutGrid,
  KeyRound,
  LogOut,
  MonitorDot,
  PlayCircle,
  Search,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  Users,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import type { AuthUser, Project } from "../lib/api";
import { setLang } from "../i18n";
import { useAuth } from "../lib/auth";
import { ChangePasswordModal } from "./ChangePasswordModal";
import { ACCENTS, accentSwatch, getAccent, getTheme, setAccent, setTheme, type Accent, type ThemeMode } from "../lib/theme";

// Nav is grouped so nine flat rows read as four scannable clusters instead of
// one undifferentiated list. Order inside a group is by expected frequency of use.
const NAV_GROUPS = [
  {
    key: "workspace",
    label: null, // the first group needs no header — it sits right under the project name
    items: [
      { key: "overview", label: "Overview", Icon: LayoutDashboard },
      { key: "assistant", label: "Assistant", Icon: Sparkles },
    ],
  },
  {
    key: "assets",
    label: "Test Assets",
    items: [
      { key: "cases", label: "Test Cases", Icon: FlaskConical },
      { key: "suites", label: "Suites", Icon: Layers },
    ],
  },
  {
    key: "results",
    label: "Results",
    items: [
      { key: "runs", label: "Runs", Icon: PlayCircle },
      { key: "compare", label: "Compare", Icon: GitCompareArrows },
      { key: "issues", label: "Issues", Icon: Bug },
    ],
  },
  {
    key: "project",
    label: "Project",
    items: [
      { key: "members", label: "Members", Icon: Users },
      { key: "settings", label: "Settings", Icon: SettingsIcon },
    ],
  },
] as const;


function Item({
  to,
  active,
  Icon,
  label,
  collapsed,
}: {
  to: string;
  active: boolean;
  Icon: typeof LayoutGrid;
  label: string;
  collapsed: boolean;
}) {
  return (
    <Link
      to={to}
      title={collapsed ? label : undefined}
      aria-current={active ? "page" : undefined}
      className={clsx(
        "group relative flex items-center gap-2.5 rounded-lg py-2 text-[13px] font-medium outline-none transition-[background-color,color] duration-150",
        collapsed ? "justify-center px-0" : "px-2.5",
        active
          ? "tp-nav-active text-brand-700"
          : "text-ink-700 hover:bg-[color-mix(in_oklch,var(--panel2)_85%,transparent)] hover:text-ink-900",
      )}
    >
      {/* active rail — a 2.5px brand bar pinned to the left edge */}
      <span
        className={clsx(
          "absolute left-0 top-1/2 h-5 w-[2.5px] -translate-y-1/2 rounded-r-full bg-brand-600 transition-opacity duration-150",
          active ? "opacity-100" : "opacity-0",
        )}
      />
      <Icon
        className={clsx(
          "h-[17px] w-[17px] shrink-0 transition-transform duration-150",
          active ? "text-brand-600" : "text-ink-500 group-hover:text-ink-700",
          !active && "group-hover:scale-[1.07]",
        )}
      />
      {!collapsed && <span className="truncate">{label}</span>}
      {!collapsed && active && (
        <span className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-brand-500" />
      )}
    </Link>
  );
}


function AccountMenu({ user, collapsed }: { user: AuthUser; collapsed: boolean }) {
  const { t, i18n } = useTranslation();
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [changingPw, setChangingPw] = useState(false);
  const [theme, setThemeState] = useState<ThemeMode>(getTheme);
  const [accent, setAccentState] = useState<Accent>(getAccent);
  const ref = useRef<HTMLDivElement>(null);
  const lang = i18n.language;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const initial = (user.name || user.email).slice(0, 1).toUpperCase();
  const themes: { k: ThemeMode; label: string }[] = [
    { k: "system", label: t("System") },
    { k: "light", label: t("Light") },
    { k: "dark", label: t("Dark") },
  ];
  const seg = (active: boolean) =>
    clsx(
      "flex-1 rounded-md py-1 text-[11px] font-medium transition-colors",
      active ? "bg-brand-50 text-ink-900" : "text-ink-500 hover:text-ink-900",
    );

  return (
    <div ref={ref} className="relative">
      {changingPw && <ChangePasswordModal onClose={() => setChangingPw(false)} />}
      {open && (
        <div
          className={clsx(
            "absolute bottom-full left-0 mb-2 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)] shadow-2xl",
            collapsed ? "w-[228px]" : "right-0",
          )}
        >
          <div className="border-b border-[var(--line)] px-3.5 py-3">
            <div className="truncate text-[13px] font-medium text-ink-900">{user.name || user.email}</div>
            <div className="truncate text-[11px] text-ink-500">{user.email}</div>
          </div>
          <div className="px-3.5 pt-3">
            <div className="mb-1.5 text-[11px] text-ink-500">{t("Interface language")}</div>
            <div className="flex gap-1 rounded-lg bg-[var(--panel2)] p-1">
              {(["zh", "en"] as const).map((l) => (
                <button key={l} onClick={() => setLang(l)} className={seg(i18n.language === l)}>
                  {l === "zh" ? "中文" : "EN"}
                </button>
              ))}
            </div>
          </div>
          <div className="px-3.5 pt-3">
            <div className="mb-1.5 text-[11px] text-ink-500">{t("Appearance")}</div>
            <div className="flex gap-1 rounded-lg bg-[var(--panel2)] p-1">
              {themes.map(({ k, label }) => (
                <button
                  key={k}
                  onClick={() => {
                    setTheme(k);
                    setThemeState(k);
                  }}
                  className={seg(theme === k)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="px-3.5 pb-3 pt-3">
            <div className="mb-2 text-[11px] text-ink-500">{lang === "zh" ? "主题色" : "Theme color"}</div>
            <div className="flex items-center gap-2.5">
              {ACCENTS.map((a) => (
                <button
                  key={a.k}
                  title={lang === "zh" ? a.zh : a.en}
                  aria-label={lang === "zh" ? a.zh : a.en}
                  onClick={() => {
                    setAccent(a.k);
                    setAccentState(a.k);
                  }}
                  className={clsx(
                    "h-6 w-6 rounded-full ring-offset-2 ring-offset-[var(--panel)] transition-all",
                    accent === a.k ? "ring-2 ring-ink-900 scale-105" : "ring-1 ring-[var(--line)] hover:scale-110",
                  )}
                  style={{ background: accentSwatch(a.hue) }}
                />
              ))}
            </div>
          </div>
          <button
            onClick={() => {
              setOpen(false);
              setChangingPw(true);
            }}
            className="flex w-full items-center gap-2.5 border-t border-[var(--line)] px-3.5 py-2.5 text-left text-[13px] text-ink-700 hover:bg-[var(--panel2)]"
          >
            <KeyRound className="h-4 w-4" /> {t("Change password")}
          </button>
          <button
            onClick={async () => {
              await logout();
              navigate("/login");
            }}
            className="flex w-full items-center gap-2.5 border-t border-[var(--line)] px-3.5 py-2.5 text-left text-[13px] text-[var(--bad-fg)] hover:bg-[var(--panel2)]"
          >
            <LogOut className="h-4 w-4" /> {t("Sign out")}
          </button>
        </div>
      )}
      <button
        onClick={() => setOpen((o) => !o)}
        title={collapsed ? user.name || user.email : undefined}
        className={clsx(
          "flex w-full items-center gap-2 rounded-lg border border-transparent py-1.5 outline-none transition-[background-color,border-color] hover:border-[var(--line)] hover:bg-[color-mix(in_oklch,var(--panel2)_70%,transparent)]",
          collapsed ? "justify-center px-0" : "px-1.5",
        )}
      >
        <div className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-gradient-to-br from-brand-500 to-brand-700 text-xs font-semibold text-[var(--on-brand)] shadow-[0_2px_6px_-2px_color-mix(in_oklch,var(--brand-700)_60%,transparent)]">
          {initial}
        </div>
        {!collapsed && (
          <>
            <div className="min-w-0 flex-1 text-left">
              <div className="truncate text-[12.5px] font-medium text-ink-900">{user.name || user.email}</div>
              <div className="truncate text-[11px] text-ink-400">{user.is_admin ? t("Admin") : t("Member")}</div>
            </div>
            <ChevronUp className={clsx("h-3.5 w-3.5 shrink-0 text-ink-400 transition-transform", open && "rotate-180")} />
          </>
        )}
      </button>
    </div>
  );
}

export function Sidebar({
  projects,
  pid,
  section,
  onSearch,
  collapsed = false,
}: {
  projects: Project[];
  pid: number | null;
  section: string | null;
  onSearch: () => void;
  collapsed?: boolean;
}) {
  const current = projects.find((p) => p.id === pid);
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const { authEnabled, isAdmin, user } = useAuth();
  return (
    <aside
      className={clsx(
        "tp-sidebar flex shrink-0 flex-col border-r border-[var(--line)] transition-[width] duration-150",
        collapsed ? "w-[60px]" : "w-[218px]",
      )}
    >
      <Link
        to="/"
        className={clsx(
          "flex h-14 items-center gap-2.5 outline-none",
          collapsed ? "justify-center px-0" : "px-3.5",
        )}
      >
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-[9px] bg-gradient-to-br from-brand-500 to-brand-700 text-[11px] font-bold text-[var(--on-brand)] shadow-[0_2px_7px_-2px_color-mix(in_oklch,var(--brand-700)_65%,transparent)]">
          TP
        </span>
        {!collapsed && (
          <>
            <span className="text-[15px] font-semibold tracking-tight text-ink-900">Potato Test</span>
            <span className="ml-auto rounded-full border border-[var(--line)] px-1.5 py-px text-[10px] font-medium text-ink-400">
              v0.1
            </span>
          </>
        )}
      </Link>

      <button
        onClick={onSearch}
        title={collapsed ? `${t("Go to…")} ⌘K` : undefined}
        className={clsx(
          "group mb-3 flex items-center gap-2 rounded-lg border border-[var(--line)] bg-[color-mix(in_oklch,var(--panel2)_55%,transparent)] py-1.5 text-[13px] text-ink-500 outline-none transition-[border-color,background-color,color] hover:border-brand-200 hover:bg-brand-50 hover:text-ink-700",
          collapsed ? "mx-2 justify-center px-0" : "mx-3 px-2.5",
        )}
      >
        <Search className="h-3.5 w-3.5 shrink-0" />
        {!collapsed && (
          <>
            {t("Go to…")}
            <span className="ml-auto rounded border border-[var(--line)] bg-[var(--panel)] px-1 text-[10px] font-medium text-ink-400">
              ⌘K
            </span>
          </>
        )}
      </button>


      <nav className={clsx("flex-1 overflow-y-auto pb-2", collapsed ? "px-2" : "px-3")}>
        {pid == null ? (
          <>
            <Item to="/" active Icon={LayoutGrid} label={t("Projects")} collapsed={collapsed} />
            {isAdmin && (
              <>
                {!collapsed && (
                  <div className="tp-nav-label px-2 pb-1.5 pt-4">{t("Admin")}</div>
                )}
                <Item
                  to="/admin/users"
                  active={false}
                  Icon={Users}
                  label={t("Users")}
                  collapsed={collapsed}
                />
                <Item
                  to="/admin/settings"
                  active={false}
                  Icon={ShieldCheck}
                  label={t("System settings")}
                  collapsed={collapsed}
                />
              </>
            )}
          </>
        ) : (
          <>
            {!collapsed && (
              <div className="px-2 pb-2 pt-1">
                <div className="tp-nav-label mb-0.5">{t("Project")}</div>
                <div className="truncate text-[13px] font-semibold text-ink-900" title={current?.name ?? ""}>
                  {current?.name ?? `Project #${pid}`}
                </div>
              </div>
            )}
            {NAV_GROUPS.map((g, gi) => (
              <div key={g.key} className={clsx(gi > 0 && "mt-3")}>
                {!collapsed && g.label && <div className="tp-nav-label px-2 pb-1.5">{t(g.label)}</div>}
                {collapsed && gi > 0 && <div className="mx-2 mb-2 border-t border-[var(--line)]" />}
                <div className="space-y-0.5">
                  {g.items.map(({ key, label, Icon }) => (
                    <Item
                      key={key}
                      to={`/projects/${pid}/${key}`}
                      active={section === key}
                      Icon={Icon}
                      label={t(label)}
                      collapsed={collapsed}
                    />
                  ))}
                </div>
              </div>
            ))}
          </>
        )}
      </nav>


      <div className={clsx("border-t border-[var(--line)]", collapsed ? "p-2" : "p-3")}>
        {authEnabled && user ? (
          <AccountMenu user={user} collapsed={collapsed} />
        ) : (
          <>
            {!collapsed && (
              <div className="mb-2 flex items-center gap-1 px-0.5">
                {(["zh", "en"] as const).map((l) => (
                  <button
                    key={l}
                    onClick={() => setLang(l)}
                    className={clsx(
                      "rounded-md px-2 py-0.5 text-xs font-medium transition-colors",
                      lang === l ? "bg-brand-50 text-brand-700" : "text-ink-500 hover:bg-[var(--panel2)]",
                    )}
                  >
                    {l === "zh" ? "中" : "EN"}
                  </button>
                ))}
              </div>
            )}
            <div
              title={collapsed ? t("Local instance") : undefined}
              className={clsx(
                "flex items-center gap-2.5 rounded-lg bg-[color-mix(in_oklch,var(--panel2)_70%,transparent)] py-2 text-ink-500",
                collapsed ? "justify-center px-0" : "px-2.5",
              )}
            >
              <span className="relative grid shrink-0 place-items-center">
                <MonitorDot className="h-[17px] w-[17px]" />
                <span className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full bg-[var(--ok)] ring-2 ring-[var(--panel)]" />
              </span>
              {!collapsed && (
                <div className="min-w-0">
                  <div className="truncate text-[12px] font-medium text-ink-700">{t("Local instance")}</div>
                  <div className="truncate text-[11px] text-ink-400">{t("no sign-in required")}</div>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </aside>
  );
}
