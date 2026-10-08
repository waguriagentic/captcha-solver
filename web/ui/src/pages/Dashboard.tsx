import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  CheckCircle,
  CircleNotch,
  Copy,
  MagnifyingGlass,
  ShieldCheck,
  SignOut,
  Warning,
  XCircle,
} from "@phosphor-icons/react";
import {
  api,
  ApiError,
  type Overview,
  type SolveEvent,
  type SolveResult,
  type TypeSpec,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  Button,
  Card,
  CardHeader,
  Chip,
  EmptyState,
  ErrorNote,
  Field,
  SkeletonRows,
  StatusDot,
} from "../components/ui";
import { Select } from "../components/select";
import {
  ActivityField,
  LatencyTrace,
  RunningRow,
  SuccessRing,
} from "../components/console-viz";
import {
  clockTime,
  copyText,
  duration,
  percent,
  relativeTime,
  seconds,
  truncate,
} from "../lib/format";

/** Full-bleed shell: the console uses the whole viewport, not a centred box. */
const SHELL = "w-full px-6 sm:px-8 lg:px-14 xl:px-20";

/** Fields the solve form never renders as free text. */
const HIDDEN_FIELDS: Record<string, true> = { sitekey: true, url: true };

function Wordmark() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="grid size-7 place-items-center rounded-[8px] border border-line-strong bg-raised">
        <ShieldCheck size={15} weight="regular" className="text-accent" />
      </span>
      <span className="text-[13px] font-medium tracking-tight text-fg">
        Sonogami <span className="text-muted">Solver</span>
      </span>
    </div>
  );
}

/**
 * Connection indicator. Two orthogonal facts, never conflated:
 *
 *   connection — is the stream up?  connected | disconnected (retrying)
 *   activity   — is work arriving?  live (recent solves) | idle (none)
 *
 * "Idle" means the backend is healthy and simply has nothing to report; it
 * sends no snapshots while nothing changes, by design. "Reconnecting" is
 * reserved for a genuine transport failure, which the stream layer reports and
 * retries with backoff.
 *
 * The counter appears ONLY while reconnecting, where it answers the one
 * question that matters then: how long has the data been stale. Showing it next
 * to "live" was noise — an incrementing number that told the reader nothing.
 */
function LiveBadge({
  connected,
  active,
  paused,
  disconnectedFor,
}: {
  connected: boolean;
  active: boolean;
  paused: boolean;
  /** Seconds since the stream dropped, or null while it is up. */
  disconnectedFor: number | null;
}) {
  const state = !connected ? "offline" : paused ? "paused" : active ? "live" : "idle";
  const tones = {
    live: "border-accent/35 bg-accent/10 text-accent",
    idle: "border-line-strong bg-raised text-muted",
    paused: "border-line-strong bg-raised text-faint",
    offline: "border-danger/35 bg-danger/10 text-danger",
  } as const;
  const dots = { live: "ok", idle: "idle", paused: "idle", offline: "bad" } as const;
  const labels = { live: "live", idle: "idle", paused: "paused", offline: "reconnecting" } as const;
  const hints = {
    live: "Connected. Solves are arriving on the stream.",
    idle: "Connected, no solves recently. The stream only pushes when something changes.",
    paused: "Stream paused. The last snapshot stays on screen; resume to catch up.",
    offline: "The stream dropped. Retrying with backoff; the snapshot on screen is stale.",
  } as const;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10.5px] tracking-[0.1em] uppercase ${tones[state]}`}
      title={hints[state]}
    >
      <StatusDot tone={dots[state]} />
      {labels[state]}
      {state === "offline" && disconnectedFor !== null ? (
        <span className="text-danger/80 normal-case">{disconnectedFor}s</span>
      ) : null}
    </span>
  );
}

/** A single KPI with an optional inline ring or figure. */
function Kpi({
  label,
  value,
  sub,
  tone = "idle",
  children,
}: {
  label: string;
  value?: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "ok" | "bad" | "warn" | "idle";
  children?: React.ReactNode;
}) {
  const tones = {
    ok: "text-accent",
    bad: "text-danger",
    warn: "text-warn",
    idle: "text-fg",
  } as const;
  return (
    <div className="flex items-center justify-between gap-4 px-5 py-4">
      <div className="min-w-0">
        <span className="font-mono text-[10.5px] tracking-[0.12em] text-faint uppercase">
          {label}
        </span>
        {value !== undefined ? (
          <span className={`num mt-1.5 block text-[26px] leading-none font-medium ${tones[tone]}`}>
            {value}
          </span>
        ) : null}
        {sub ? <span className="mt-1.5 block text-[11.5px] text-faint">{sub}</span> : null}
      </div>
      {children}
    </div>
  );
}

// ── Solve form ──────────────────────────────────────────────────────
type FormState = { type: string; sitekey: string; url: string; extras: Record<string, string> };

function SolveForm({
  types,
  onResult,
}: {
  types: TypeSpec[];
  onResult: () => void;
}) {
  const [form, setForm] = useState<FormState>({ type: "", sitekey: "", url: "", extras: {} });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SolveResult | null>(null);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!form.type && types.length) setForm((f) => ({ ...f, type: types[0].type }));
  }, [types, form.type]);

  const spec = useMemo(() => types.find((t) => t.type === form.type), [types, form.type]);
  const requires = spec?.requires ?? [];
  const optional = (spec?.fields ?? []).filter((f) => !HIDDEN_FIELDS[f]);

  const needsSitekey = requires.includes("sitekey");
  const needsUrl = requires.includes("url");
  const needsSceneId = requires.includes("scene_id");
  const needsPrefix = requires.includes("prefix");
  const needsPublicKey = requires.includes("public_key");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    setResult(null);

    // Only send what the selected solver declares: an unknown key is a 400.
    const payload: Record<string, unknown> = { type: form.type };
    if (needsSitekey) payload.sitekey = form.sitekey.trim();
    if (needsUrl && form.url.trim()) payload.url = form.url.trim();
    if (needsSceneId) payload.scene_id = form.extras.scene_id?.trim();
    if (needsPrefix) payload.prefix = form.extras.prefix?.trim();
    if (needsPublicKey) payload.public_key = form.extras.public_key?.trim();
    for (const key of optional) {
      if (HIDDEN_FIELDS[key]) continue;
      const raw = form.extras[key]?.trim();
      if (!raw) continue;
      if (raw === "true" || raw === "false") payload[key] = raw === "true";
      else if (key === "timeout_s" && /^\d+$/.test(raw)) payload[key] = Number(raw);
      else payload[key] = raw;
    }

    try {
      setResult(await api.solve(payload));
      onResult();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Solve request failed.");
    } finally {
      setBusy(false);
    }
  }

  const requiredMissing =
    (needsSitekey && !form.sitekey.trim()) ||
    (needsSceneId && !form.extras.scene_id?.trim()) ||
    (needsPrefix && !form.extras.prefix?.trim()) ||
    (needsPublicKey && !form.extras.public_key?.trim());

  return (
    <Card className="flex flex-col">
      <CardHeader title="Run a solve" meta="Exercises the live solvers end to end" />
      <form onSubmit={submit} className="flex flex-col gap-4 p-5">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="solve-type" className="text-[13px] font-medium text-muted">
            Type
          </label>
          <Select
            value={form.type}
            onValueChange={(next) => {
              setForm({ type: next, sitekey: "", url: "", extras: {} });
              setResult(null);
              setError(null);
            }}
            options={types.map((t) => ({ value: t.type, label: t.type, hint: t.label }))}
            placeholder="Choose a captcha type"
            ariaLabel="Captcha type"
            className="[&>span]:truncate"
          />
          {spec?.hint ? <p className="text-[12px] text-faint">{spec.hint}</p> : null}
        </div>

        {needsSitekey ? (
          <Field
            label="Sitekey"
            mono
            placeholder="0x4AAAAAAA..."
            value={form.sitekey}
            onChange={(e) => setForm((f) => ({ ...f, sitekey: e.target.value }))}
          />
        ) : null}

        {needsUrl ? (
          <Field
            label="Target URL"
            mono
            type="url"
            placeholder="https://target.com"
            value={form.url}
            onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))}
          />
        ) : null}

        {needsSceneId ? (
          <Field
            label="Scene ID"
            mono
            value={form.extras.scene_id ?? ""}
            onChange={(e) => setForm((f) => ({ ...f, extras: { ...f.extras, scene_id: e.target.value } }))}
          />
        ) : null}

        {needsPrefix ? (
          <Field
            label="Prefix"
            mono
            value={form.extras.prefix ?? ""}
            onChange={(e) => setForm((f) => ({ ...f, extras: { ...f.extras, prefix: e.target.value } }))}
          />
        ) : null}

        {needsPublicKey ? (
          <Field
            label="Public key"
            mono
            value={form.extras.public_key ?? ""}
            onChange={(e) =>
              setForm((f) => ({ ...f, extras: { ...f.extras, public_key: e.target.value } }))
            }
          />
        ) : null}

        {optional.length ? (
          <div className="rounded-[8px] border border-line">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              className="flex w-full items-center justify-between px-3 py-2.5 text-[13px] text-muted transition-colors hover:text-fg"
            >
              Optional parameters
              <CaretToggle open={open} />
            </button>
            {open ? (
              <div className="grid grid-cols-1 gap-3 border-t border-line p-3 sm:grid-cols-2">
                {optional.map((name) => (
                  <Field
                    key={name}
                    label={name}
                    mono
                    value={form.extras[name] ?? ""}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, extras: { ...f.extras, [name]: e.target.value } }))
                    }
                  />
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        {error ? <ErrorNote>{error}</ErrorNote> : null}

        <Button
          type="submit"
          variant="primary"
          busy={busy}
          disabled={Boolean(requiredMissing)}
          className="w-full"
        >
          {busy ? "Solving" : "Solve"}
        </Button>
        <p className="text-[12px] leading-relaxed text-faint">
          A solve holds a browser for its duration and counts against the same concurrency
          budget as API callers.
        </p>
      </form>

      {result ? (
        <div className="border-t border-line">
          <div className="flex items-center justify-between gap-3 px-5 py-3">
            <div className="flex items-center gap-2">
              {result.solved ? (
                <CheckCircle size={15} weight="fill" className="text-accent" />
              ) : (
                <XCircle size={15} weight="fill" className="text-danger" />
              )}
              <span className="text-[13px] font-medium">
                {result.solved ? "Solved" : "Not solved"}
              </span>
              {typeof result.elapsed === "number" ? (
                <span className="num text-[12px] text-faint">{seconds(result.elapsed)}</span>
              ) : null}
              {result.method ? (
                <span className="font-mono text-[11px] text-faint">{result.method}</span>
              ) : null}
            </div>
            <button
              type="button"
              onClick={async () => {
                if (await copyText(JSON.stringify(result, null, 2))) {
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1600);
                }
              }}
              className="flex items-center gap-1 rounded-[6px] px-1.5 py-1 text-[11px] text-faint transition-colors hover:bg-raised hover:text-fg"
            >
              {copied ? <CheckCircle size={12} weight="bold" /> : <Copy size={12} />}
              {copied ? "Copied" : "Copy JSON"}
            </button>
          </div>
          <pre className="max-h-[280px] overflow-auto border-t border-line px-5 py-3 font-mono text-[12px] leading-relaxed text-muted">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      ) : null}
    </Card>
  );
}

function CaretToggle({ open }: { open: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`text-[11px] text-faint transition-transform duration-150 ${open ? "rotate-180" : ""}`}
    >
      ▾
    </span>
  );
}

// ── Activity table ──────────────────────────────────────────────────
function ActivityTable({
  events,
  onFilterType,
}: {
  events: SolveEvent[];
  onFilterType: (type: string) => void;
}) {
  if (!events.length) {
    return (
      <EmptyState
        icon={<MagnifyingGlass size={20} />}
        title="No solves in this view"
        body="The buffer holds the last 100 solves. Run one from the panel and it appears here the moment the stream pushes it."
      />
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr className="border-b border-line font-mono text-[10.5px] tracking-[0.12em] text-faint uppercase">
            <th className="px-5 py-2.5 font-normal">Time</th>
            <th className="px-3 py-2.5 font-normal">Type</th>
            <th className="px-3 py-2.5 font-normal">Target</th>
            <th className="px-3 py-2.5 font-normal">Result</th>
            <th className="px-5 py-2.5 text-right font-normal">Latency</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {events.map((event, index) => (
            <tr
              key={`${event.timestamp}-${index}`}
              className="transition-colors hover:bg-raised/40"
            >
              <td className="num px-5 py-2.5 text-[12px] whitespace-nowrap text-faint">
                {clockTime(event.timestamp)}
              </td>
              <td className="px-3 py-2.5">
                <button
                  type="button"
                  onClick={() => onFilterType(event.type)}
                  className="font-mono text-[12px] text-muted transition-colors hover:text-accent"
                >
                  {event.type}
                </button>
              </td>
              <td className="px-3 py-2.5">
                <span
                  className="font-mono text-[12px] text-muted"
                  title={event.url || event.sitekey}
                >
                  {truncate(event.url || event.sitekey || "—", 46)}
                </span>
              </td>
              <td className="px-3 py-2.5">
                {event.success ? (
                  <Chip tone="ok">solved</Chip>
                ) : (
                  <span title={event.error ?? undefined}>
                    <Chip tone="bad">failed</Chip>
                  </span>
                )}
              </td>
              <td className="num px-5 py-2.5 text-right text-[12px] whitespace-nowrap text-muted">
                {seconds(event.elapsed)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Page ────────────────────────────────────────────────────────────
export function DashboardPage() {
  const { user, logout, markExpired } = useAuth();
  const [snapshot, setSnapshot] = useState<Overview | null>(null);
  const [types, setTypes] = useState<TypeSpec[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<"connecting" | "connected">("connecting");
  const [lastEvent, setLastEvent] = useState<number | null>(null);
  const [droppedAt, setDroppedAt] = useState<number | null>(null);
  const [typeFilter, setTypeFilter] = useState("");
  const [failedOnly, setFailedOnly] = useState(false);
  const [paused, setPaused] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const handleError = useCallback(
    (cause: unknown) => {
      if (cause instanceof ApiError && cause.isAuthError) {
        markExpired();
        return;
      }
      setError(cause instanceof Error ? cause.message : "Request failed.");
    },
    [markExpired],
  );

  // One initial snapshot, then the stream takes over. The fetch also covers
  // browsers or proxies where SSE is unavailable.
  const refresh = useCallback(async () => {
    try {
      const next = await api.overview(10);
      setSnapshot(next);
      // Seed the activity clock from the newest event the fetch returned, so a
      // fallback load does not read as "live" merely because we just fetched.
      const newest = next.recent[0]?.timestamp;
      setLastEvent(newest ? newest * 1000 : null);
      setError(null);
    } catch (cause) {
      handleError(cause);
    }
  }, [handleError]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh();

    const stop = api.stream(
      {
        onSnapshot: (next) => {
          if (pausedRef.current) return;
          setSnapshot(next);
          setLastEvent(Date.now());
          setError(null);
        },
        onConnection: (connected) => {
          setConnection(connected ? "connected" : "connecting");
          // Stamp the moment the transport FIRST went down and keep it across
          // retries, so the counter reads "stale for N seconds" rather than
          // resetting on every reconnect attempt.
          setDroppedAt((previous) => (connected ? null : (previous ?? Date.now())));
        },
        onError: (cause) => {
          if ((cause as Error)?.name === "AbortError") return;
          if (cause instanceof ApiError && cause.isAuthError) {
            markExpired();
            return;
          }
          // Transport errors already move the badge via onConnection; only
          // surface something the operator can act on.
          if (cause instanceof ApiError && cause.status !== 0) {
            setError(cause.message);
          }
        },
      },
      controller.signal,
    );

    return () => {
      stop();
      controller.abort();
    };
  }, [refresh, markExpired]);

  useEffect(() => {
    const controller = new AbortController();
    api
      .types(controller.signal)
      .then((r) => setTypes(r.types))
      .catch((cause: unknown) => {
        if ((cause as Error)?.name !== "AbortError") handleError(cause);
      });
    return () => controller.abort();
  }, [handleError]);

  // Drives the in-flight elapsed counters between snapshots. It does NOT touch
  // the connection state: the transport layer owns that, and an absence of
  // solves is idleness, not disconnection.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const stats = snapshot?.stats;
  const events = useMemo(() => {
    let rows = snapshot?.recent ?? [];
    if (typeFilter) rows = rows.filter((e) => e.type === typeFilter);
    if (failedOnly) rows = rows.filter((e) => !e.success);
    return rows;
  }, [snapshot, typeFilter, failedOnly]);

  const latencies = useMemo(
    () =>
      (snapshot?.series ?? [])
        .map((p) => p.elapsed)
        .filter((v): v is number => typeof v === "number"),
    [snapshot],
  );

  const inFlight = snapshot?.current ?? [];
  const system = snapshot?.system;

  // "Live" is an activity fact, not a transport one: a solve landed within the
  // last 90s. An idle service sends nothing at all, which is correct — the
  // stream pushes on change only.
  const ACTIVE_WINDOW_MS = 90_000;
  const active =
    lastEvent !== null && Date.now() - lastEvent < ACTIVE_WINDOW_MS;

  return (
    <div className="min-h-[100dvh]">
      <header className="sticky top-0 z-40 border-b border-line bg-base/85 backdrop-blur-md">
        <div className={`${SHELL} flex h-15 items-center justify-between gap-4`}>
          <div className="flex items-center gap-3.5">
            <Wordmark />
            <LiveBadge
              connected={connection === "connected"}
              active={active}
              paused={paused}
              disconnectedFor={
                droppedAt === null ? null : Math.max(0, Math.round(now - droppedAt / 1000))
              }
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden font-mono text-[12px] text-faint sm:block">{user}</span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setPaused((v) => !v)}
              title={paused ? "Resume the live stream" : "Pause the live stream"}
            >
              {paused ? "Resume" : "Pause"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => void logout()}>
              <SignOut size={13} weight="regular" />
              Sign out
            </Button>
          </div>
        </div>
      </header>

      <main className={`${SHELL} py-6`}>
        {error ? (
          <div className="mb-5">
            <ErrorNote>
              <Warning size={15} weight="regular" className="mt-0.5 shrink-0" />
              {error}
            </ErrorNote>
          </div>
        ) : null}

        {/* ── Live field: the console's own subject ─────────────── */}
        <Card className="mb-5 overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
            <div className="flex items-center gap-2.5">
              <StatusDot
                tone={!connection.includes("connected") ? "bad" : paused ? "idle" : active ? "ok" : "idle"}
              />
              <span className="font-mono text-[10.5px] tracking-[0.14em] text-faint uppercase">
                solve activity
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1 font-mono text-[10.5px] text-faint">
              <span className="flex items-center gap-1.5">
                <span className="inline-block size-2 rounded-[2px] bg-accent/70" />
                solved
              </span>
              <span className="flex items-center gap-1.5">
                <span className="inline-block size-2 rounded-[2px] bg-danger/80" />
                failed
              </span>
              <span>height = latency</span>
              <span className="hidden sm:inline">
                {lastEvent
                  ? `last event ${relativeTime(lastEvent / 1000)}`
                  : connection === "connected"
                    ? "connected, no events yet"
                    : "connecting"}
              </span>
            </div>
          </div>
          <ActivityField points={snapshot?.series ?? []} className="h-[124px] w-full" />
        </Card>

        {/* ── KPI row ───────────────────────────────────────────── */}
        <Card className="mb-5">
          <div className="grid grid-cols-1 divide-line sm:grid-cols-2 lg:grid-cols-4 lg:divide-x">
            <Kpi
              label="Success rate"
              value={stats ? percent(stats.success_rate, 0) : "—"}
              sub={stats ? `${stats.succeeded} of ${stats.solves} in window` : undefined}
              tone={stats && stats.success_rate >= 0.8 ? "ok" : "warn"}
            >
              {stats ? <SuccessRing rate={stats.success_rate} /> : null}
            </Kpi>
            <Kpi
              label="Latency"
              value={stats ? seconds(stats.avg_elapsed, 1) : "—"}
              sub={stats?.p95_elapsed ? `p95 ${seconds(stats.p95_elapsed, 1)}` : "mean, last 100"}
            />
            <Kpi
              label="Failed"
              value={stats ? String(stats.failed) : "—"}
              sub="in current window"
              tone={stats && stats.failed > 0 ? "bad" : "idle"}
            />
            <Kpi
              label="In flight"
              value={String(inFlight.length)}
              sub={inFlight.length ? "browser held" : "idle"}
              tone={inFlight.length ? "ok" : "idle"}
            >
              {inFlight.length ? (
                <CircleNotch size={18} className="animate-spin text-accent" />
              ) : null}
            </Kpi>
          </div>
          <div className="grid grid-cols-2 gap-px border-t border-line bg-line lg:grid-cols-4">
            <div className="bg-panel px-5 py-3">
              <span className="font-mono text-[10px] tracking-[0.12em] text-faint uppercase">
                lifetime solves
              </span>
              <span className="num mt-1 block text-[15px] text-fg">
                {snapshot ? snapshot.lifetime.solves : "—"}
              </span>
            </div>
            <div className="bg-panel px-5 py-3">
              <span className="font-mono text-[10px] tracking-[0.12em] text-faint uppercase">
                uptime
              </span>
              <span className="num mt-1 block text-[15px] text-fg">
                {system ? duration(system.service.uptime_s) : "—"}
              </span>
            </div>
            <div className="bg-panel px-5 py-3">
              <span className="font-mono text-[10px] tracking-[0.12em] text-faint uppercase">
                browser
              </span>
              <span className="mt-1 block text-[15px]">
                {system ? (
                  system.browser.headless ? (
                    <Chip tone="warn">headless</Chip>
                  ) : (
                    <Chip tone="ok">headful</Chip>
                  )
                ) : (
                  "—"
                )}
              </span>
            </div>
            <div className="bg-panel px-5 py-3">
              <span className="font-mono text-[10px] tracking-[0.12em] text-faint uppercase">
                private targets
              </span>
              <span className="mt-1 block text-[15px]">
                {system ? (
                  system.service.allow_private_targets ? (
                    <Chip tone="warn">allowed</Chip>
                  ) : (
                    <Chip tone="ok">blocked</Chip>
                  )
                ) : (
                  "—"
                )}
              </span>
            </div>
          </div>
        </Card>

        {/* ── Layout note ───────────────────────────────────────────
            The activity table is by far the tallest element (~40 rows), so it
            spans the full width instead of sitting in a column beside a short
            card. The remaining cards are paired by similar height, which is
            what keeps the grid from leaving dead space.
            ────────────────────────────────────────────────────────── */}

        {/* Row: form (tall) beside per-type (tall). */}
        <div className="mb-5 grid grid-cols-1 gap-5 lg:grid-cols-12">
          <div className="lg:col-span-4">
            <SolveForm types={types} onResult={refresh} />
          </div>

          <div className="lg:col-span-8">
            <Card>
              <CardHeader
                title="Per-type performance"
                meta="Click a row to filter the activity table"
              />
              {!stats || stats.by_type.length === 0 ? (
                <EmptyState
                  title="No per-type data yet"
                  body="Once solves land they roll up here by type, with success rate and mean latency."
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full border-collapse text-left">
                    <thead>
                      <tr className="border-b border-line font-mono text-[10.5px] tracking-[0.12em] text-faint uppercase">
                        <th className="px-5 py-2.5 font-normal">Type</th>
                        <th className="px-3 py-2.5 text-right font-normal">Solves</th>
                        <th className="px-3 py-2.5 font-normal">Success</th>
                        <th className="px-3 py-2.5 text-right font-normal">Mean</th>
                        <th className="px-5 py-2.5 text-right font-normal">Last</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line">
                      {stats.by_type.map((row) => (
                        <tr
                          key={row.type}
                          onClick={() => setTypeFilter((c) => (c === row.type ? "" : row.type))}
                          className="cursor-pointer transition-colors hover:bg-raised/40"
                        >
                          <td className="px-5 py-2.5 font-mono text-[12px] text-fg">{row.type}</td>
                          <td className="num px-3 py-2.5 text-right text-[12px] text-muted">
                            {row.solves}
                          </td>
                          <td className="px-3 py-2.5">
                            <div className="flex items-center gap-2">
                              <span className="h-1 w-16 overflow-hidden rounded-full bg-raised">
                                <span
                                  className={`block h-full ${
                                    row.success_rate >= 0.8
                                      ? "bg-accent"
                                      : row.success_rate >= 0.5
                                        ? "bg-warn"
                                        : "bg-danger"
                                  }`}
                                  style={{ width: `${Math.round(row.success_rate * 100)}%` }}
                                />
                              </span>
                              <span className="num text-[12px] text-muted">
                                {percent(row.success_rate, 0)}
                              </span>
                            </div>
                          </td>
                          <td className="num px-3 py-2.5 text-right text-[12px] text-muted">
                            {seconds(row.avg_elapsed, 1)}
                          </td>
                          <td className="num px-5 py-2.5 text-right text-[12px] text-faint">
                            {relativeTime(row.last_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </div>
        </div>

        {/* Row: the tallest element gets the whole width. */}
        <Card className="mb-5">
          <CardHeader
            title="Activity"
            meta={
              typeFilter || failedOnly
                ? `${events.length} shown, filtered`
                : "Last 10 solves, pushed live"
            }
            action={
              <div className="flex items-center gap-1.5">
                {typeFilter ? (
                  <button type="button" onClick={() => setTypeFilter("")}>
                    <Chip tone="ok">{typeFilter} ✕</Chip>
                  </button>
                ) : null}
                <Button
                  size="sm"
                  variant={failedOnly ? "primary" : "ghost"}
                  onClick={() => setFailedOnly((v) => !v)}
                >
                  Failed only
                </Button>
              </div>
            }
          />
          <ActivityTable
            events={events}
            onFilterType={(type) => setTypeFilter((c) => (c === type ? "" : type))}
          />
        </Card>

        {/* Row: three short cards, sized so the row fills completely. */}
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-12">
          <div className="lg:col-span-5">
            <Card className="h-full">
              <CardHeader title="Latency trace" meta="Per-solve duration, oldest to newest" />
              <div className="px-5 py-4">
                <LatencyTrace points={snapshot?.series ?? []} />
                {latencies.length >= 2 ? (
                  <div className="mt-3 flex justify-between font-mono text-[11px] text-faint">
                    <span className="num">min {seconds(Math.min(...latencies), 1)}</span>
                    <span className="num">max {seconds(Math.max(...latencies), 1)}</span>
                  </div>
                ) : null}
              </div>
            </Card>
          </div>

          <div className="lg:col-span-4">
            <Card className="h-full">
              <CardHeader title="Service" meta="Runtime and hardening state" />
              {system ? (
                <dl className="divide-y divide-line">
                  {(
                    [
                      ["Version", <span className="font-mono">{system.service.version}</span>],
                      ["Python", <span className="num">{system.service.python}</span>],
                      ["Sessions", <span className="num">{system.auth.sessions}</span>],
                      ["Display", system.browser.display ? (
                        <span className="font-mono text-[12px]">{system.browser.display}</span>
                      ) : (
                        <Chip tone="idle">unset</Chip>
                      )],
                      ["Proxy headers", system.auth.trust_proxy ? (
                        <Chip tone="warn">trusted</Chip>
                      ) : (
                        <Chip tone="ok">ignored</Chip>
                      )],
                      ["Stream", <span className="font-mono text-[12px]">
                        {!connection.includes("connected") ? "reconnecting"
                          : paused ? "paused"
                          : active ? "live" : "idle"}
                      </span>],
                    ] as Array<[string, React.ReactNode]>
                  ).map(([label, value]) => (
                    <div key={label} className="flex items-center justify-between gap-4 px-5 py-2.5">
                      <dt className="font-mono text-[10.5px] tracking-[0.12em] text-faint uppercase">
                        {label}
                      </dt>
                      <dd className="text-[12px] text-fg">{value}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <SkeletonRows rows={5} />
              )}
            </Card>
          </div>

          <div className="lg:col-span-3">
            <Card className="flex h-full flex-col">
              <CardHeader
                title="Running now"
                meta={inFlight.length ? `${inFlight.length} in flight` : "nothing in flight"}
              />
              <div className="flex-1">
                {inFlight.length ? (
                  inFlight.map((task, i) => (
                    <RunningRow
                      key={`${task.type}-${i}`}
                      event={{
                        type: task.type,
                        sitekey: task.sitekey,
                        url: task.url,
                        token: false,
                        error: null,
                        elapsed: null,
                        method: null,
                        timestamp: task.started_at,
                        success: true,
                      }}
                      now={now}
                    />
                  ))
                ) : (
                  <div className="flex h-full flex-col items-center justify-center gap-2 px-5 py-8 text-center">
                    <StatusDot tone="idle" />
                    <p className="text-[12px] text-faint">
                      Idle. A solve appears here the moment it starts.
                    </p>
                  </div>
                )}
              </div>
              <Link
                to="/docs"
                className="flex items-center justify-between gap-3 border-t border-line px-5 py-3 text-[12.5px] text-muted transition-colors hover:text-fg"
              >
                API reference
                <ArrowRight size={12} weight="bold" />
              </Link>
            </Card>
          </div>
        </div>
      </main>
    </div>
  );
}
