import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  Check,
  Copy,
  GithubLogo,
  ShieldCheck,
  Warning,
} from "@phosphor-icons/react";
import { api, type Reference, type ReferenceType } from "../lib/api";
import { Button, Chip, ErrorNote, StatusDot } from "../components/ui";
import { copyText } from "../lib/format";
import { WordResolve } from "../components/motion";

/** Public source. Overridable at build time for a fork. */
const REPO_URL = import.meta.env.VITE_REPO_URL ?? "https://github.com/waguriagentic/captcha-solver";

/** Full-bleed shell, matching the landing page's grid. */
const SHELL = "w-full px-6 sm:px-8 lg:px-14 xl:px-20";

const AUTH_TONE = { public: "ok", bearer: "warn", session: "idle" } as const;

function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        if (await copyText(value)) {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1600);
        }
      }}
      className="flex items-center gap-1 rounded-[6px] px-1.5 py-1 text-[11px] text-faint transition-colors hover:bg-raised hover:text-fg"
    >
      {copied ? <Check size={12} weight="bold" /> : <Copy size={12} weight="regular" />}
      {copied ? "Copied" : label}
    </button>
  );
}

function Code({ children, className }: { children: string; className?: string }) {
  return (
    <pre
      className={`overflow-x-auto rounded-[8px] border border-line bg-[#0a0c0f] px-3.5 py-3 font-mono text-[12px] leading-[1.7] text-muted ${className ?? ""}`}
    >
      {children}
    </pre>
  );
}

function MethodChip({ method }: { method: string }) {
  return (
    <span
      className={`inline-flex w-[3.4rem] shrink-0 justify-center rounded-[6px] border px-1.5 py-0.5 font-mono text-[10.5px] ${
        method === "POST"
          ? "border-accent/35 bg-accent/10 text-accent"
          : "border-line-strong bg-raised text-muted"
      }`}
    >
      {method}
    </span>
  );
}

/** One provider: identity, requirements, and a copyable request body. */
function TypePanel({ spec, apiBase }: { spec: ReferenceType; apiBase: string }) {
  const [open, setOpen] = useState(false);

  const example = useMemo(() => {
    const body: Record<string, unknown> = { type: spec.type };
    for (const field of spec.requires) {
      body[field] =
        field === "url"
          ? "https://target.com"
          : field === "sitekey"
            ? "0x4AAAAAAA..."
            : field === "public_key"
              ? "A0DE7B75-1138-44F2-B132-ED188CEB66F3"
              : field === "scene_id"
                ? "1xxxxxxx"
                : "13lbkb5";
    }
    return JSON.stringify(body, null, 2);
  }, [spec]);

  return (
    <article className="border-t border-line py-6 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h3 className="font-mono text-[14px] text-fg">{spec.type}</h3>
        <span className="text-[13px] text-muted">{spec.label}</span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {spec.variants.map((v) => (
            <Chip key={v}>{v}</Chip>
          ))}
        </div>
      </div>

      <p className="mt-2.5 max-w-[74ch] text-[13px] leading-relaxed text-muted">{spec.hint}</p>

      <div className="mt-4 grid grid-cols-1 gap-x-8 gap-y-3 lg:grid-cols-2">
        <div>
          <p className="font-mono text-[10.5px] tracking-[0.14em] text-faint uppercase">
            required
          </p>
          <p className="mt-1.5 font-mono text-[12.5px]">
            {spec.requires.length ? (
              <span className="text-fg">{spec.requires.join(", ")}</span>
            ) : (
              <span className="text-faint">none. Self-contained flow.</span>
            )}
          </p>
        </div>
        <div>
          <p className="font-mono text-[10.5px] tracking-[0.14em] text-faint uppercase">
            returns
          </p>
          <p className="mt-1.5 font-mono text-[12.5px] text-muted">
            {spec.returns.length ? spec.returns.join(", ") : "solved"}
          </p>
        </div>
      </div>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="mt-4 font-mono text-[11px] tracking-[0.1em] text-faint uppercase transition-colors hover:text-accent"
      >
        {open ? "− hide request" : "+ show request"}
      </button>

      {open ? (
        <div className="mt-3">
          <div className="flex items-center justify-between pb-2">
            <span className="font-mono text-[11px] text-faint">POST {apiBase}/solve</span>
            <CopyButton value={example} label="Copy body" />
          </div>
          <Code>{example}</Code>
        </div>
      ) : null}
    </article>
  );
}

export function DocsPage() {
  const [ref, setRef] = useState<Reference | null>(null);
  const [failed, setFailed] = useState(false);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    api
      .reference(controller.signal)
      .then(setRef)
      .catch((error: unknown) => {
        if ((error as Error)?.name !== "AbortError") setFailed(true);
      });
    return () => controller.abort();
  }, []);

  const apiBase = ref?.api_base_url ?? "https://api.example.com";
  const types = useMemo(
    () =>
      (ref?.types ?? []).filter(
        (t) =>
          !filter ||
          t.type.includes(filter.toLowerCase()) ||
          t.label.toLowerCase().includes(filter.toLowerCase()),
      ),
    [ref, filter],
  );

  const quickstart = [
    `$ export API=${apiBase}`,
    `$ export TOKEN=<your bearer token>`,
    "",
    `$ curl -X POST $API/solve \\`,
    `    -H "Authorization: Bearer $TOKEN" \\`,
    `    -H "Content-Type: application/json" \\`,
    `    -d '{"type":"turnstile","sitekey":"0x4AAA...","url":"https://target.com"}'`,
  ].join("\n");

  const success = [
    "{",
    '  "type": "turnstile",',
    '  "solved": true,',
    '  "token": "0.hF9k...",',
    '  "method": "route",',
    '  "elapsed": 4.12',
    "}",
  ].join("\n");

  return (
    <div className="min-h-[100dvh]">
      <header className="sticky top-0 z-40 border-b border-line bg-base/80 backdrop-blur-md">
        <div className={`${SHELL} flex h-15 items-center justify-between gap-4`}>
          <Link to="/" className="flex items-center gap-2.5">
            <span className="grid size-7 place-items-center rounded-[8px] border border-line-strong bg-raised">
              <ShieldCheck size={15} weight="regular" className="text-accent" />
            </span>
            <span className="text-[13px] font-medium tracking-tight text-fg">
              Sonogami <span className="text-muted">Solver</span>
            </span>
            <span className="ml-1 font-mono text-[10.5px] tracking-[0.12em] text-faint uppercase">
              docs
            </span>
          </Link>
          <div className="flex items-center gap-2">
            <a
              href="/openapi.json"
              className="hidden px-2 py-2 font-mono text-[10.5px] tracking-[0.12em] text-muted uppercase transition-colors hover:text-accent sm:block"
            >
              openapi.json
            </a>
            <a
              href={REPO_URL}
              target="_blank"
              rel="noreferrer noopener"
              aria-label="Source on GitHub"
              className="flex items-center px-2 py-2 text-muted transition-colors hover:text-fg"
            >
              <GithubLogo size={15} weight="regular" />
            </a>
            <Link to="/">
              <Button variant="ghost" size="sm">
                <ArrowLeft size={13} weight="regular" />
                Home
              </Button>
            </Link>
          </div>
        </div>
      </header>

      {/* ── Header ───────────────────────────────────────────────── */}
      <section className="border-b border-line">
        <div className={`${SHELL} grid grid-cols-1 gap-10 py-14 lg:grid-cols-12 lg:py-16`}>
          <div className="lg:col-span-7">
            <h1 className="text-[2.2rem] leading-[1.03] font-semibold tracking-[-0.035em] lg:text-[2.9rem]">
              <WordResolve text="API reference." />
            </h1>
            <p className="mt-5 max-w-[60ch] text-[15px] leading-relaxed text-muted">
              One endpoint solves every supported challenge. Dispatch is by the{" "}
              <code className="font-mono text-[13px] text-fg">type</code> field; every response
              carries the same top-level <code className="font-mono text-[13px] text-fg">solved</code>{" "}
              boolean, so a client never branches per provider.
            </p>
          </div>

          <div className="lg:col-span-5">
            <div className="rounded-[10px] border border-line bg-panel p-5">
              <div className="flex items-center gap-2">
                <StatusDot tone="ok" />
                <span className="font-mono text-[10.5px] tracking-[0.14em] text-faint uppercase">
                  base url
                </span>
              </div>
              <div className="mt-2.5 flex items-center justify-between gap-3">
                <code className="truncate font-mono text-[13px] text-fg">{apiBase}</code>
                <CopyButton value={apiBase} />
              </div>
              <div className="mt-4 flex items-center justify-between gap-3 border-t border-line pt-4">
                <span className="text-[12px] text-faint">Authentication</span>
                <Chip tone="warn">Authorization: Bearer</Chip>
              </div>
            </div>
          </div>
        </div>
      </section>

      {failed ? (
        <div className={`${SHELL} py-8`}>
          <ErrorNote>
            <Warning size={15} weight="regular" className="mt-0.5 shrink-0" />
            The reference could not be loaded. It is served by this same host at{" "}
            <span className="font-mono">/api/v1/reference</span>.
          </ErrorNote>
        </div>
      ) : null}

      {/* ── Endpoints ────────────────────────────────────────────── */}
      <section className="border-b border-line">
        <div className={`${SHELL} py-14 lg:py-16`}>
          <h2 className="mb-6 font-mono text-[10.5px] tracking-[0.16em] text-accent uppercase">
            Endpoints
          </h2>
          <div className="overflow-hidden rounded-[10px] border border-line">
            <table className="w-full border-collapse text-left">
              <thead className="bg-panel">
                <tr className="border-b border-line text-[10.5px] tracking-[0.14em] text-faint uppercase">
                  <th className="px-4 py-3 font-normal">Method</th>
                  <th className="px-4 py-3 font-normal">Path</th>
                  <th className="px-4 py-3 font-normal">Auth</th>
                  <th className="hidden px-4 py-3 font-normal md:table-cell">Description</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {(ref?.endpoints ?? []).map((endpoint) => (
                  <tr key={`${endpoint.method}-${endpoint.path}`} className="align-top">
                    <td className="px-4 py-3">
                      <MethodChip method={endpoint.method} />
                    </td>
                    <td className="px-4 py-3 font-mono text-[12.5px] text-fg">{endpoint.path}</td>
                    <td className="px-4 py-3">
                      <Chip tone={AUTH_TONE[endpoint.auth as keyof typeof AUTH_TONE] ?? "idle"}>
                        {endpoint.auth}
                      </Chip>
                    </td>
                    <td className="hidden px-4 py-3 md:table-cell">
                      <span className="text-[13px] text-muted">{endpoint.summary}</span>
                      <span className="mt-1 block max-w-[70ch] text-[12px] leading-relaxed text-faint">
                        {endpoint.detail}
                      </span>
                    </td>
                  </tr>
                ))}
                {!ref && !failed ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-6">
                      <div className="skeleton h-5 rounded-[6px]" />
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* ── Quickstart ───────────────────────────────────────────── */}
      <section className="border-b border-line">
        <div className={`${SHELL} grid grid-cols-1 gap-10 py-14 lg:grid-cols-12 lg:py-16`}>
          <div className="lg:col-span-4">
            <h2 className="font-mono text-[10.5px] tracking-[0.16em] text-accent uppercase">
              Quickstart
            </h2>
            <p className="mt-5 max-w-[42ch] text-[14px] leading-relaxed text-muted">
              Send the type and its required fields. Read{" "}
              <code className="font-mono text-[12.5px] text-fg">solved</code> — nothing else — to
              decide whether it worked.
            </p>
          </div>
          <div className="lg:col-span-8">
            <div className="flex items-center justify-between pb-2">
              <span className="font-mono text-[11px] text-faint">solve</span>
              <CopyButton value={quickstart} />
            </div>
            <Code>{quickstart}</Code>

            <div className="mt-6 grid grid-cols-1 gap-6 md:grid-cols-2">
              <div>
                <p className="pb-2 font-mono text-[10.5px] tracking-[0.14em] text-accent uppercase">
                  success · 200
                </p>
                <Code>{success}</Code>
              </div>
              <div>
                <p className="pb-2 font-mono text-[10.5px] tracking-[0.14em] text-warn uppercase">
                  never solved · 4xx
                </p>
                <Code>{`{\n  "detail": "sitekey is required for type=turnstile"\n}`}</Code>
                <p className="mt-3 text-[12.5px] leading-relaxed text-faint">
                  A solve that ran but failed is still 200 with{" "}
                  <code className="font-mono">solved:false</code>. A 4xx means the request never
                  reached a solver. Read one or the other, never both.
                </p>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── Types ────────────────────────────────────────────────── */}
      <section className="border-b border-line">
        <div className={`${SHELL} py-14 lg:py-16`}>
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <h2 className="font-mono text-[10.5px] tracking-[0.16em] text-accent uppercase">
                Captcha types
              </h2>
              <p className="mt-4 max-w-[60ch] text-[14px] leading-relaxed text-muted">
                Each type lists what it requires and what it returns. Expand one for a request
                body you can copy.
              </p>
            </div>
            <input
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter types"
              aria-label="Filter captcha types"
              className="h-9 w-full rounded-[8px] border border-line-strong bg-raised px-3 text-[13px] text-fg transition-colors placeholder:text-faint hover:border-faint sm:w-56"
            />
          </div>

          <div className="mt-8">
            {!ref && !failed ? (
              <div className="flex flex-col gap-3" aria-hidden="true">
                {Array.from({ length: 4 }, (_, i) => (
                  <div key={i} className="skeleton h-16 rounded-[8px]" />
                ))}
              </div>
            ) : types.length === 0 ? (
              <p className="py-8 text-[13px] text-faint">
                No type matches “{filter}”.
              </p>
            ) : (
              types.map((spec) => (
                <TypePanel key={spec.type} spec={spec} apiBase={apiBase} />
              ))
            )}
          </div>
        </div>
      </section>

      {/* ── Request fields ───────────────────────────────────────── */}
      <section className="border-b border-line">
        <div className={`${SHELL} py-14 lg:py-16`}>
          <h2 className="font-mono text-[10.5px] tracking-[0.16em] text-accent uppercase">
            Request fields
          </h2>
          <p className="mt-4 max-w-[70ch] text-[14px] leading-relaxed text-muted">
            Every field the solve body accepts. Only <span className="font-mono text-[13px] text-fg">type</span>{" "}
            is unconditionally required; the rest depend on the type you dispatch to.
          </p>

          <div className="mt-7 overflow-x-auto rounded-[10px] border border-line">
            <table className="w-full min-w-[720px] border-collapse text-left">
              <thead className="bg-panel">
                <tr className="border-b border-line text-[10.5px] tracking-[0.14em] text-faint uppercase">
                  <th className="px-4 py-3 font-normal">Field</th>
                  <th className="px-4 py-3 font-normal">Type</th>
                  <th className="px-4 py-3 font-normal">Default</th>
                  <th className="px-4 py-3 font-normal">Description</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {ref
                  ? Object.entries(ref.fields).map(([name, field]) => (
                      <tr key={name} className="align-top">
                        <td className="px-4 py-3 font-mono text-[12.5px] whitespace-nowrap text-fg">
                          {name}
                          {field.required ? (
                            <span className="ml-1.5 text-accent" title="always required">
                              *
                            </span>
                          ) : null}
                        </td>
                        <td className="px-4 py-3 font-mono text-[12px] text-muted">{field.type}</td>
                        <td className="px-4 py-3 font-mono text-[12px] text-faint">
                          {field.default === null || field.default === undefined
                            ? "—"
                            : String(field.default)}
                        </td>
                        <td className="px-4 py-3">
                          <span className="block max-w-[68ch] text-[12.5px] leading-relaxed text-muted">
                            {field.description}
                          </span>
                          {field.examples.length ? (
                            <span className="mt-1.5 block font-mono text-[11.5px] text-faint">
                              e.g. {field.examples.join(" · ")}
                            </span>
                          ) : null}
                        </td>
                      </tr>
                    ))
                  : null}
                {!ref && !failed ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-6">
                      <div className="skeleton h-5 rounded-[6px]" />
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <footer className={`${SHELL} py-9`}>
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
          <span className="text-[12px] text-muted">
            Sonogami <span className="text-faint">Solver</span>
          </span>
          {ref ? <span className="font-mono text-[11px] text-faint">v{ref.service.version}</span> : null}
        </div>
      </footer>
    </div>
  );
}
