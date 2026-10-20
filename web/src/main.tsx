import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import "./index.css";
import "./i18n";
import { initTheme } from "./lib/theme";
import { AuthProvider, useAuth } from "./lib/auth";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/toast";
import { RouteFallback } from "./components/ui";

// Every page is split into its own chunk so heavy deps (recharts, vidstack,
// radix, papaparse) only load when the user actually visits that route.
const AdminSettingsPage = lazy(() => import("./pages/AdminSettingsPage").then((m) => ({ default: m.AdminSettingsPage })));
const AdminUsersPage = lazy(() => import("./pages/AdminUsersPage").then((m) => ({ default: m.AdminUsersPage })));
const CasesPage = lazy(() => import("./pages/CasesPage").then((m) => ({ default: m.CasesPage })));
const ComparePage = lazy(() => import("./pages/ComparePage").then((m) => ({ default: m.ComparePage })));
const InvitePage = lazy(() => import("./pages/InvitePage").then((m) => ({ default: m.InvitePage })));
const IssuesPage = lazy(() => import("./pages/IssuesPage").then((m) => ({ default: m.IssuesPage })));
const LoginPage = lazy(() => import("./pages/LoginPage").then((m) => ({ default: m.LoginPage })));
const MembersPage = lazy(() => import("./pages/MembersPage").then((m) => ({ default: m.MembersPage })));
const OverviewPage = lazy(() => import("./pages/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const ProposalsPage = lazy(() =>
  import("./pages/ProposalsPage").then((m) => ({ default: m.ProposalsPage })),
);
const Projects = lazy(() => import("./pages/Projects").then((m) => ({ default: m.Projects })));
const RecoveryPage = lazy(() => import("./pages/RecoveryPage").then((m) => ({ default: m.RecoveryPage })));
const ResetPasswordPage = lazy(() => import("./pages/ResetPasswordPage").then((m) => ({ default: m.ResetPasswordPage })));
const RunReport = lazy(() => import("./pages/RunReport").then((m) => ({ default: m.RunReport })));
const RunsPage = lazy(() => import("./pages/RunsPage").then((m) => ({ default: m.RunsPage })));
const SuitesPage = lazy(() => import("./pages/SuitesPage").then((m) => ({ default: m.SuitesPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((m) => ({ default: m.SettingsPage })));
const WorkspacePage = lazy(() => import("./pages/WorkspacePage").then((m) => ({ default: m.WorkspacePage })));

const S = (node: React.ReactNode) => <Suspense fallback={<RouteFallback />}>{node}</Suspense>;

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { loading, authEnabled, user } = useAuth();
  if (loading) return <RouteFallback />;
  if (authEnabled && !user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** 密钥不可用时把所有工作区页面拦到恢复页。
 *  keyStatus 为 null 表示"还不知道"（请求失败/未登录）—— 放行，避免网络抖动把应用锁死。 */
function RequireKeyReady({ children }: { children: React.ReactNode }) {
  const { loading, keyStatus } = useAuth();
  if (loading) return <RouteFallback />;
  if (keyStatus && !keyStatus.can_use_credentials) return <Navigate to="/recovery" replace />;
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { loading, authEnabled, isAdmin } = useAuth();
  if (loading) return <RouteFallback />;
  if (authEnabled && !isAdmin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

// User management presupposes users. Without auth there is no login, so the page (invites,
// roles, password resets) has nothing to act on — hiding the nav item alone would leave it
// reachable by typing the URL.
function RequireAuthEnabled({ children }: { children: React.ReactNode }) {
  const { loading, authEnabled } = useAuth();
  if (loading) return <RouteFallback />;
  if (!authEnabled) return <Navigate to="/" replace />;
  return <>{children}</>;
}

// Per-project membership only decides anything when there are users AND the workspace is
// not shared: with auth off there is nobody to grant access to, and with a shared workspace
// every logged-in user already reaches every project. Same reasoning as the nav item.
function RequirePerProjectRbac({ children }: { children: React.ReactNode }) {
  const { loading, authEnabled, sharedWorkspace } = useAuth();
  if (loading) return <RouteFallback />;
  if (!authEnabled || sharedWorkspace) return <Navigate to="/" replace />;
  return <>{children}</>;
}

const router = createBrowserRouter([
  { path: "/login", element: S(<LoginPage />) },
  { path: "/invite/:token", element: S(<InvitePage />) },
  { path: "/reset/:token", element: S(<ResetPasswordPage />) },
  // 恢复页必须留在 RequireKeyReady 之外，否则密钥坏了就再也进不去修。
  { path: "/recovery", element: <RequireAuth>{S(<RecoveryPage />)}</RequireAuth> },
  {
    element: (
      <RequireAuth>
        <RequireKeyReady>
          <Layout />
        </RequireKeyReady>
      </RequireAuth>
    ),
    children: [
      { path: "/", element: S(<Projects />) },
      {
        path: "/admin/users",
        element: (
          <RequireAdmin>
            <RequireAuthEnabled>{S(<AdminUsersPage />)}</RequireAuthEnabled>
          </RequireAdmin>
        ),
      },
      { path: "/admin/settings", element: <RequireAdmin>{S(<AdminSettingsPage />)}</RequireAdmin> },
      { path: "/projects/:pid", element: <Navigate to="overview" replace /> },
      { path: "/projects/:pid/overview", element: S(<OverviewPage />) },
      { path: "/projects/:pid/docs", element: S(<WorkspacePage />) },
      { path: "/projects/:pid/proposals", element: S(<ProposalsPage />) },
      { path: "/projects/:pid/assistant", element: S(<WorkspacePage />) },
      { path: "/projects/:pid/cases", element: S(<CasesPage />) },
      { path: "/projects/:pid/suites", element: S(<SuitesPage />) },
      { path: "/projects/:pid/runs", element: S(<RunsPage />) },
      { path: "/projects/:pid/runs/:rid", element: S(<RunReport />) },
      { path: "/projects/:pid/compare", element: S(<ComparePage />) },
      { path: "/projects/:pid/issues", element: S(<IssuesPage />) },
      {
        path: "/projects/:pid/members",
        element: <RequirePerProjectRbac>{S(<MembersPage />)}</RequirePerProjectRbac>,
      },
      { path: "/projects/:pid/settings", element: S(<SettingsPage />) },
    ],
  },
]);

initTheme();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </AuthProvider>
  </React.StrictMode>,
);
