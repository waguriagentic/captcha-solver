/**
 * Shared UI primitives.
 *
 * Shape lock (see index.css): cards 10px, controls 8px, status chips pill.
 * Nothing here invents a new radius, and the accent color is used for exactly
 * one thing per surface — primary action or live state, never both.
 */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, Ref } from "react";
import clsx from "clsx";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "outline" | "danger";
  size?: "sm" | "md";
  busy?: boolean;
};

export function Button({
  variant = "outline",
  size = "md",
  busy = false,
  className,
  children,
  disabled,
  ...rest
}: ButtonProps) {
  const variants: Record<string, string> = {
    primary:
      "bg-accent text-[#04150e] font-semibold hover:bg-[#3ad295] active:bg-[#2aa876]",
    outline:
      "border border-line-strong text-fg hover:border-faint hover:bg-raised active:bg-panel",
    ghost: "text-muted hover:text-fg hover:bg-raised active:bg-panel",
    danger:
      "border border-danger/40 text-danger hover:bg-danger/10 active:bg-danger/20",
  };
  return (
    <button
      {...rest}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={clsx(
        "inline-flex shrink-0 items-center justify-center gap-2 rounded-[8px]",
        "transition-[background-color,border-color,transform] duration-150",
        "active:translate-y-[1px] disabled:pointer-events-none disabled:opacity-45",
        size === "sm" ? "h-8 px-3 text-[13px]" : "h-9.5 px-3.5 text-sm",
        variants[variant],
        className,
      )}
    >
      {busy && <Spinner />}
      {children}
    </button>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={clsx(
        "size-3.5 shrink-0 animate-spin rounded-full border-[1.5px]",
        "border-current border-t-transparent",
        className,
      )}
    />
  );
}

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  mono?: boolean;
  ref?: Ref<HTMLInputElement>;
};

export function Field({ label, hint, error, mono, id, className, ref, ...rest }: FieldProps) {
  const fieldId = id ?? `f-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={fieldId} className="text-[13px] font-medium text-muted">
        {label}
      </label>
      <input
        {...rest}
        ref={ref}
        id={fieldId}
        aria-invalid={error ? true : undefined}
        aria-describedby={hint ? `${fieldId}-hint` : undefined}
        className={clsx(
          "h-9.5 w-full rounded-[8px] border bg-raised px-3 text-sm text-fg",
          "placeholder:text-faint",
          "transition-colors duration-150",
          "disabled:opacity-50",
          error ? "border-danger/60" : "border-line-strong hover:border-faint",
          mono && "font-mono text-[13px]",
          className,
        )}
      />
      {error ? (
        <p className="text-[12px] text-danger">{error}</p>
      ) : hint ? (
        <p id={`${fieldId}-hint`} className="text-[12px] text-faint">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function Card({
  children,
  className,
  as: Tag = "section",
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "div" | "article";
}) {
  return (
    <Tag className={clsx("rounded-[10px] border border-line bg-panel", className)}>
      {children}
    </Tag>
  );
}

export function CardHeader({
  title,
  meta,
  action,
}: {
  title: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
      <div className="min-w-0">
        <h2 className="truncate text-sm font-medium text-fg">{title}</h2>
        {meta ? <p className="mt-0.5 truncate text-[12px] text-faint">{meta}</p> : null}
      </div>
      {action}
    </header>
  );
}

type Tone = "ok" | "warn" | "bad" | "idle";

export function Chip({ tone = "idle", children }: { tone?: Tone; children: ReactNode }) {
  const tones: Record<Tone, string> = {
    ok: "border-accent/35 bg-accent/10 text-accent",
    warn: "border-warn/35 bg-warn/10 text-warn",
    bad: "border-danger/35 bg-danger/10 text-danger",
    idle: "border-line-strong bg-raised text-muted",
  };
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5",
        "font-mono text-[11px] leading-5 whitespace-nowrap",
        tones[tone],
      )}
    >
      {children}
    </span>
  );
}

/** A live status dot. Static under prefers-reduced-motion. */
export function StatusDot({ tone = "ok" }: { tone?: Tone }) {
  const colors: Record<Tone, string> = {
    ok: "bg-accent",
    warn: "bg-warn",
    bad: "bg-danger",
    idle: "bg-faint",
  };
  return (
    <span
      aria-hidden="true"
      className={clsx("relative inline-block size-1.5 rounded-full", colors[tone])}
    />
  );
}

export function Metric({
  label,
  value,
  sub,
  tone = "idle",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
}) {
  return (
    <div className="flex flex-col gap-1 px-4 py-3.5">
      <span className="text-[12px] tracking-wide text-faint">{label}</span>
      <span
        className={clsx(
          "num text-[22px] leading-none font-medium",
          tone === "ok" && "text-accent",
          tone === "bad" && "text-danger",
          tone === "warn" && "text-warn",
          tone === "idle" && "text-fg",
        )}
      >
        {value}
      </span>
      {sub ? <span className="text-[12px] text-faint">{sub}</span> : null}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon?: ReactNode;
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      {icon ? <div className="mb-1 text-faint">{icon}</div> : null}
      <p className="text-sm font-medium text-fg">{title}</p>
      <p className="max-w-[46ch] text-[13px] leading-relaxed text-faint">{body}</p>
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function SkeletonRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={clsx("flex flex-col gap-2 p-4", className)} aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton h-5 rounded-[6px]" style={{ width: `${92 - i * 7}%` }} />
      ))}
    </div>
  );
}

/** Inline error surface. Used for load failures inside a panel. */
export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-[8px] border border-danger/35 bg-danger/8 px-3 py-2.5 text-[13px] text-danger"
    >
      {children}
    </div>
  );
}
