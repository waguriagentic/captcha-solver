import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { motion, useReducedMotion } from "motion/react";
import { ArrowLeft, LockSimple, Warning } from "@phosphor-icons/react";
import { useAuth } from "../lib/auth";
import { ApiError } from "../lib/api";
import { Button, Card, Field } from "../components/ui";

export function LoginPage() {
  const { status, login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [lockout, setLockout] = useState(0);
  const [busy, setBusy] = useState(false);
  const userRef = useRef<HTMLInputElement>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    userRef.current?.focus();
  }, []);

  // Countdown for the lockout window so the form explains itself instead of
  // failing silently until the timer runs out server-side.
  useEffect(() => {
    if (lockout <= 0) return;
    const timer = window.setInterval(() => setLockout((n) => Math.max(0, n - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [lockout]);

  const unconfigured = status === "unconfigured";
  const locked = lockout > 0;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || locked) return;
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
      // The route guard redirects once the provider reports "authenticated".
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (cause.status === 429) {
          setLockout(cause.retryAfter ?? 300);
          setError(null);
        } else if (cause.status === 401) {
          setError("Incorrect username or password.");
        } else if (cause.status === 0) {
          setError("Cannot reach the dashboard service.");
        } else {
          setError(cause.message);
        }
      } else {
        setError("Sign in failed.");
      }
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative grid min-h-[100dvh] place-items-center px-5 py-12">
      <div className="bg-grid fade-edges pointer-events-none absolute inset-0 opacity-40" />

      <motion.div
        className="relative w-full max-w-[380px]"
        initial={reduced ? undefined : { opacity: 0, y: 6 }}
        animate={reduced ? undefined : { opacity: 1, y: 0 }}
        transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      >
        <Link
          to="/"
          className="mb-6 inline-flex items-center gap-1.5 text-[13px] text-faint transition-colors hover:text-fg"
        >
          <ArrowLeft size={13} weight="regular" />
          Back
        </Link>

        <Card className="p-6">
          <div className="mb-6">
            <span className="grid size-9 place-items-center rounded-[8px] border border-line-strong bg-raised">
              <LockSimple size={16} weight="regular" className="text-accent" />
            </span>
            <h1 className="mt-4 text-[19px] font-medium tracking-tight">Admin console</h1>
            <p className="mt-1 text-[13px] leading-relaxed text-faint">
              Single operator access. Sessions expire after 12 hours.
            </p>
          </div>

          {unconfigured ? (
            <div className="flex items-start gap-2.5 rounded-[8px] border border-warn/35 bg-warn/8 px-3 py-3 text-[13px] leading-relaxed text-warn">
              <Warning size={15} weight="regular" className="mt-0.5 shrink-0" />
              <div>
                <p className="font-medium">Console not configured.</p>
                <p className="mt-1 text-warn/85">
                  Set <span className="font-mono text-[12px]">SOLVER_ADMIN_USER</span> and{" "}
                  <span className="font-mono text-[12px]">ADMIN_PASSWORD_HASH</span> on the
                  service, then restart it.
                </p>
              </div>
            </div>
          ) : (
            <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
              <Field
                ref={userRef}
                label="Username"
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={busy || locked}
              />
              <Field
                label="Password"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={busy || locked}
              />

              {locked ? (
                <div
                  role="alert"
                  className="rounded-[8px] border border-danger/35 bg-danger/8 px-3 py-2.5 text-[13px] text-danger"
                >
                  Too many failed attempts. Locked for another{" "}
                  <span className="num">{lockout}s</span>.
                </div>
              ) : error ? (
                <div
                  role="alert"
                  className="rounded-[8px] border border-danger/35 bg-danger/8 px-3 py-2.5 text-[13px] text-danger"
                >
                  {error}
                </div>
              ) : null}

              <Button
                type="submit"
                variant="primary"
                busy={busy}
                disabled={locked || !username || !password}
                className="mt-1 w-full"
              >
                {busy ? "Verifying" : "Sign in"}
              </Button>
            </form>
          )}
        </Card>

        <p className="mt-5 text-center text-[12px] leading-relaxed text-faint">
          Credentials are verified server-side against a scrypt digest. Failed attempts are
          throttled per IP.
        </p>
      </motion.div>
    </div>
  );
}
