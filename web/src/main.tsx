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
const AssistantPage = lazy(() => import("./pages/AssistantPage").then((m) => ({ default: m.AssistantPage })));
const CasesPage = lazy(() => import("./pages/CasesPage").then((m) => ({ default: m.CasesPage })));
const ComparePage = lazy(() => import("./pages/ComparePage").then((m) => ({ default: m.ComparePage })));
const InvitePage = lazy(() => import("./pages/InvitePage").then((m) => ({ default: m.InvitePage })));
const IssuesPage = lazy(() => import("./pages/IssuesPage").then((m) => ({ default: m.IssuesPage })));
const LoginPage = lazy(() => import("./pages/LoginPage").then((m) => ({ default: m.LoginPage })));
const MembersPage = lazy(() => import("./pages/MembersPage").then((m) => ({ default: m.MembersPage })));
const OverviewPage = lazy(() => import("./pages/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const Projects = lazy(() => import("./pages/Projects").then((m) => ({ default: m.Projects })));
const ResetPasswordPage = lazy(() => import("./pages/ResetPasswordPage").then((m) => ({ default: m.ResetPasswordPage })));
const RunReport = lazy(() => import("./pages/RunReport").then((m) => ({ default: m.RunReport })));
const RunsPage = lazy(() => import("./pages/RunsPage").then((m) => ({ default: m.RunsPage })));
const SuitesPage = lazy(() => import("./pages/SuitesPage").then((m) => ({ default: m.SuitesPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((m) => ({ default: m.SettingsPage })));

const S = (node: React.ReactNode) => <Suspense fallback={<RouteFallback />}>{node}</Suspense>;

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { loading, authEnabled, user } = useAuth();
  if (loading) return <RouteFallback />;
  if (authEnabled && !user) return <Navigate to="/login" replace />;
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

const router = createBrowserRouter([
  { path: "/login", element: S(<LoginPage />) },
  { path: "/invite/:token", element: S(<InvitePage />) },
  { path: "/reset/:token", element: S(<ResetPasswordPage />) },
  {
    element: (
      <RequireAuth>
        <Layout />
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
      { path: "/projects/:pid/assistant", element: S(<AssistantPage />) },
      { path: "/projects/:pid/cases", element: S(<CasesPage />) },
      { path: "/projects/:pid/suites", element: S(<SuitesPage />) },
      { path: "/projects/:pid/runs", element: S(<RunsPage />) },
      { path: "/projects/:pid/runs/:rid", element: S(<RunReport />) },
      { path: "/projects/:pid/compare", element: S(<ComparePage />) },
      { path: "/projects/:pid/issues", element: S(<IssuesPage />) },
      { path: "/projects/:pid/members", element: S(<MembersPage />) },
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
