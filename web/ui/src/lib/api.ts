/**
 * Typed client for the dashboard API.
 *
 * Security notes that must survive refactors:
 *  - The CSRF token lives in a module variable, never in localStorage. It is
 *    re-read from /auth/session on every page load, so a reload or a new tab
 *    re-derives it from the session cookie instead of from disk.
 *  - `credentials: "same-origin"` is explicit: cookies go to this origin only.
 *    The API host is a different origin and never receives them.
 *  - 401 clears local auth state and lets the router redirect; it never
 *    retries, so a revoked session cannot loop.
 */

const BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly retryAfter?: number;

  constructor(status: number, message: string, retryAfter?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }

  /** True when the caller must authenticate again. */
  get isAuthError(): boolean {
    return this.status === 401;
  }

  /** True when the request was rejected for a missing/stale CSRF token. */
  get isCsrfError(): boolean {
    return this.status === 403;
  }
}

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | undefined | null>;
};

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = `${BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };

  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  // Safe methods are exempt from CSRF by design; sending the header anyway
  // would be harmless, but keeping the split makes the contract obvious.
  if (method !== "GET" && method !== "HEAD" && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (cause) {
    if ((cause as Error)?.name === "AbortError") throw cause;
    throw new ApiError(0, "Cannot reach the dashboard service.");
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const detail =
      (payload as { detail?: string } | null)?.detail ??
      response.statusText ??
      "Request failed";
    const retryAfter = Number(response.headers.get("Retry-After") ?? "") || undefined;
    throw new ApiError(response.status, detail, retryAfter);
  }

  return payload as T;
}

// ── Types ──────────────────────────────────────────────────────────
export type SolveEvent = {
  type: string;
  sitekey: string;
  url: string;
  token: boolean;
  error: string | null;
  elapsed: number | null;
  method: string | null;
  timestamp: number;
  success: boolean;
};

export type TypeStat = {
  type: string;
  solves: number;
  succeeded: number;
  failed: number;
  avg_elapsed: number | null;
  last_at: number;
  last_error: string | null;
  success_rate: number;
};

export type Stats = {
  window: number;
  solves: number;
  succeeded: number;
  failed: number;
  success_rate: number;
  avg_elapsed: number | null;
  p95_elapsed: number | null;
  by_type: TypeStat[];
};

export type SeriesPoint = {
  t: number;
  ok: boolean;
  elapsed: number | null;
  type: string;
};

export type CurrentTask = {
  type: string;
  sitekey: string;
  url: string;
  version?: string | null;
  started_at: number;
};

export type Overview = {
  generated_at: number;
  stats: Stats;
  lifetime: { solves: number; in_flight: number };
  recent: SolveEvent[];
  series: SeriesPoint[];
  current: CurrentTask[];
  system: SystemInfo;
};

export type Session = {
  authenticated: boolean;
  dashboard?: boolean;
  user?: string;
  csrf_token?: string;
  created_at?: number;
  expires_at?: number;
};

export type TypeSpec = {
  type: string;
  label: string;
  requires: string[];
  fields: string[];
  hint: string;
};

export type SystemInfo = {
  service: {
    version: string;
    uptime_s: number;
    python: string;
    public_url: string;
    supported_types: string[];
    allow_private_targets: boolean;
  };
  browser: {
    headless: boolean;
    binary: string | null;
    display: string | null;
    pool: Record<string, unknown>;
  };
  auth: {
    user: string | null;
    sessions: number;
    session_ttl_s: number;
    trust_proxy: boolean;
  };
};

export type SolveResult = Record<string, unknown> & {
  type: string;
  solved?: boolean;
  error?: string;
  elapsed?: number;
  method?: string;
};

// ── Endpoints ──────────────────────────────────────────────────────
export type PublicMeta = {
  service: string;
  version: string;
  api_base_url: string;
  types: Array<{ type: string; label: string }>;
};

export type ReferenceType = {
  type: string;
  label: string;
  requires: string[];
  fields: string[];
  hint: string;
  variants: string[];
  returns: string[];
};

export type ReferenceField = {
  type: string;
  required: boolean;
  default: string | number | boolean | null;
  description: string;
  examples: Array<string | number | boolean>;
};

export type Reference = {
  service: { name: string; version: string };
  api_base_url: string;
  endpoints: Array<{
    method: string;
    path: string;
    auth: string;
    summary: string;
    detail: string;
  }>;
  fields: Record<string, ReferenceField>;
  types: ReferenceType[];
};

export const api = {
  meta: (signal?: AbortSignal) => request<PublicMeta>("/meta", { signal }),

  reference: (signal?: AbortSignal) => request<Reference>("/reference", { signal }),

  session: (signal?: AbortSignal) =>
    request<Session>("/auth/session", { signal }),

  async login(username: string, password: string): Promise<Session> {
    const session = await request<Session>("/auth/login", {
      method: "POST",
      body: { username, password },
    });
    setCsrfToken(session.csrf_token ?? null);
    return session;
  },

  async logout(): Promise<void> {
    try {
      await request<void>("/auth/logout", { method: "POST" });
    } finally {
      setCsrfToken(null);
    }
  },

  types: (signal?: AbortSignal) =>
    request<{ types: TypeSpec[] }>("/types", { signal }),

  overview: (lines = 40, signal?: AbortSignal) =>
    request<Overview>("/overview", { query: { lines }, signal }),

  /**
   * Live console stream.
   *
   * EventSource cannot set headers, so this is a hand-rolled SSE reader over
   * fetch: it keeps the session cookie (same-origin) and, unlike EventSource,
   * a 401 surfaces as an error the caller can act on instead of an endless
   * silent reconnect.
   *
   * Returns a teardown function.
   */
  stream(
    onSnapshot: (snapshot: Overview) => void,
    onError: (error: unknown) => void,
    signal: AbortSignal,
  ): () => void {
    let stopped = false;

    (async () => {
      let response: Response;
      try {
        response = await fetch(`${BASE}/stream`, {
          headers: { Accept: "text/event-stream" },
          credentials: "same-origin",
          cache: "no-store",
          signal,
        });
      } catch (cause) {
        if (!stopped) onError(cause);
        return;
      }

      if (!response.ok) {
        onError(new ApiError(response.status, "Live stream unavailable"));
        return;
      }
      if (!response.body) {
        onError(new ApiError(0, "Streaming unsupported by this browser"));
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      try {
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          // Frames are separated by a blank line; keep the trailing partial.
          let split: number;
          while ((split = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0, split);
            buffer = buffer.slice(split + 2);
            for (const line of frame.split("\n")) {
              if (!line.startsWith("data:")) continue; // comments are keep-alives
              try {
                onSnapshot(JSON.parse(line.slice(5).trim()) as Overview);
              } catch {
                // A malformed frame is not worth tearing the stream down.
              }
            }
          }
        }
      } catch (cause) {
        if (!stopped && (cause as Error)?.name !== "AbortError") onError(cause);
      }
    })();

    return () => {
      stopped = true;
    };
  },

  solves: (
    params: { lines?: number; type?: string; failed?: boolean } = {},
    signal?: AbortSignal,
  ) => request<{ logs: SolveEvent[]; total: number }>("/solves", { query: params, signal }),

  system: (signal?: AbortSignal) => request<SystemInfo>("/system", { signal }),

  solve: (payload: Record<string, unknown>, signal?: AbortSignal) =>
    request<SolveResult>("/solve", { method: "POST", body: payload, signal }),
};
