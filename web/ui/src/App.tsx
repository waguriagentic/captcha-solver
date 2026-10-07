import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth";
import { Spinner } from "./components/ui";
import { LandingPage } from "./pages/Landing";
import { LoginPage } from "./pages/Login";
import { DashboardPage } from "./pages/Dashboard";
import { DocsPage } from "./pages/Docs";

/** Full-viewport boot state. Shown only while the session probe is in flight. */
function Booting() {
  return (
    <div className="grid min-h-[100dvh] place-items-center">
      <div className="flex items-center gap-2.5 text-sm text-faint">
        <Spinner />
        Checking session
      </div>
    </div>
  );
}

/** Gate for /dashboard. Never renders children for an anonymous visitor, so a
 *  protected view cannot flash its contents before the redirect lands. */
function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();
  if (status === "loading") return <Booting />;
  if (status !== "authenticated") return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** /login is pointless for an authenticated admin — send them to the console. */
function LoginRoute() {
  const { status } = useAuth();
  if (status === "loading") return <Booting />;
  if (status === "authenticated") return <Navigate to="/dashboard" replace />;
  return <LoginPage />;
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route path="/docs" element={<DocsPage />} />
          <Route path="/login" element={<LoginRoute />} />
          <Route
            path="/dashboard"
            element={
              <RequireAdmin>
                <DashboardPage />
              </RequireAdmin>
            }
          />
          {/* The console is a single view; anything else returns to the landing page. */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
