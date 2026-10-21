import { ChevronRight, PanelLeft } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, type Project, type ProjectGroup } from "../lib/api";
import { useOnboarding } from "../lib/useOnboarding";
import { OnboardingFlow } from "./onboarding/OnboardingFlow";
import { CommandPalette } from "./CommandPalette";
import { NotificationBell } from "./NotificationBell";
import { Sidebar } from "./Sidebar";
import { Select } from "./ui";

// 面包屑不再显示章节名（2026-10-13）：它和页面 H1 一字不差，只留项目切换器。
// SECTION_LABEL 因此删掉了 —— 留着会变成没人用的死表。

export function Layout() {
  const { pid: pidParam } = useParams();
  const loc = useLocation();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  // 「大系统」（2026-10-13）：顶栏项目切换器 + 侧栏都按它分组，别再给一列平铺的项目。
  const [groups, setGroups] = useState<ProjectGroup[]>([]);
  const [palette, setPalette] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("tp.sidebar") === "collapsed");
  const guide = useOnboarding();

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => {});
    api
      .listProjectGroups()
      .then(setGroups)
      .catch(() => {
        /* 大系统拉不到就退回平铺，不该让整个壳子起不来 */
      });
  }, [loc.pathname]);

  const toggleSidebar = () =>
    setCollapsed((v) => {
      localStorage.setItem("tp.sidebar", v ? "expanded" : "collapsed");
      return !v;
    });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((v) => !v);
      }
      // ⌘/Ctrl+B — the near-universal "toggle sidebar" shortcut
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        toggleSidebar();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const { t } = useTranslation();
  const pid = pidParam ? Number(pidParam) : null;
  const parts = loc.pathname.split("/");
  const section = parts[1] === "projects" ? (parts[3] ?? "overview") : null;
  const current = projects.find((p) => p.id === pid);

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar
        projects={projects}
        groups={groups}
        pid={pid}
        section={section}
        onSearch={() => setPalette(true)}
        collapsed={collapsed}
      />
      <CommandPalette
        open={palette}
        onClose={() => setPalette(false)}
        projects={projects}
        groups={groups}
        pid={pid}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="tp-topbar sticky top-0 z-30 flex h-12 shrink-0 items-center gap-2 border-b border-[var(--line)] px-4 text-[13px]">
          <button
            onClick={toggleSidebar}
            title={`${t(collapsed ? "Expand sidebar" : "Collapse sidebar")} ⌘B`}
            aria-label={t(collapsed ? "Expand sidebar" : "Collapse sidebar")}
            className="-ml-1 rounded-md p-1.5 text-ink-400 outline-none transition-colors hover:bg-[var(--panel2)] hover:text-ink-900"
          >
            <PanelLeft className="h-4 w-4" />
          </button>
          <span className="font-semibold tracking-tight text-ink-900">Potato Test</span>
          {pid == null ? (
            <>
              <ChevronRight className="h-3.5 w-3.5 text-ink-400/70" />
              <span className="text-ink-700">{t("Projects")}</span>
            </>
          ) : (
            <>
              <ChevronRight className="h-3.5 w-3.5 text-ink-400/70" />
              {/* ★ 面包屑到这里就结束（2026-10-13）：**不再重复章节名**。
                  章节名下面紧接着就是页面的 H1（也是章节名），而项目名侧栏也有一份 ——
                  三段里两段是重复的。这里只留"项目切换器"这一件只有它能做的事。 */}
              <Select
                variant="bare"
                value={String(pid)}
                onChange={(e) => navigate(`/projects/${e.target.value}/${section ?? "overview"}`)}
              >
                {groups.length === 0
                  ? projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))
                  : (() => {
                      // 按大系统分组：归属明确的进各自 optgroup，剩下的进「未归类」。
                      const ungrouped = projects.filter((p) => !p.group_id);
                      return (
                        <>
                          {groups.map((g) => {
                            const ps = projects.filter((p) => p.group_id === g.id);
                            if (!ps.length) return null;
                            return (
                              <optgroup key={g.id} label={g.name}>
                                {ps.map((p) => (
                                  <option key={p.id} value={p.id}>
                                    {p.name}
                                  </option>
                                ))}
                              </optgroup>
                            );
                          })}
                          {ungrouped.length > 0 && (
                            <optgroup label={t("未归类")}>
                              {ungrouped.map((p) => (
                                <option key={p.id} value={p.id}>
                                  {p.name}
                                </option>
                              ))}
                            </optgroup>
                          )}
                        </>
                      );
                    })()}
              </Select>
            </>
          )}
          <span className="ml-2 hidden truncate font-mono text-[11px] text-ink-400 md:inline">
            {current?.base_url ?? ""}
          </span>
          <div className="ml-auto">
            <NotificationBell />
          </div>
        </div>
        <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <div className="tp-page mx-auto flex h-full min-h-0 w-full max-w-[80rem] flex-col overflow-y-auto px-6 py-7 lg:px-10 lg:py-9">
            <Outlet />
          </div>
        </main>
      </div>
      {guide.open && <OnboardingFlow onClose={guide.close} />}
    </div>
  );
}
