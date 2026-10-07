import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, setCsrfToken } from "./api";

type AuthStatus = "loading" | "authenticated" | "anonymous" | "unconfigured";

type AuthValue = {
  status: AuthStatus;
  user: string | null;
  expiresAt: number | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Called when any request reports 401, so the UI stops believing it is in. */
  markExpired: () => void;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);

  const adopt = useCallback((session: { user?: string; csrf_token?: string; expires_at?: number }) => {
    setCsrfToken(session.csrf_token ?? null);
    setUser(session.user ?? null);
    setExpiresAt(session.expires_at ?? null);
    setStatus("authenticated");
  }, []);

  const clear = useCallback(() => {
    setCsrfToken(null);
    setUser(null);
    setExpiresAt(null);
    setStatus("anonymous");
  }, []);

  // Boot probe. A 401 here is not an error: it is the normal anonymous state,
  // so it resolves to "anonymous" rather than surfacing a failure to the user.
  useEffect(() => {
    const controller = new AbortController();
    api
      .session(controller.signal)
      .then((session) => {
        if (!session.dashboard) {
          setStatus("unconfigured");
          return;
        }
        if (session.authenticated) adopt(session);
        else setStatus("anonymous");
      })
      .catch((error: unknown) => {
        if ((error as Error)?.name === "AbortError") return;
        setStatus("anonymous");
      });
    return () => controller.abort();
  }, [adopt]);

  const login = useCallback(
    async (username: string, password: string) => {
      adopt(await api.login(username, password));
    },
    [adopt],
  );

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      clear();
    }
  }, [clear]);

  const value = useMemo<AuthValue>(
    () => ({ status, user, expiresAt, login, logout, markExpired: clear }),
    [status, user, expiresAt, login, logout, clear],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
