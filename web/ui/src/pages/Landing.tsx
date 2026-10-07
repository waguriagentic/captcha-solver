import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  ArrowRight,
  Check,
  Copy,
  GithubLogo,
  Globe,
  ShieldCheck,
  Terminal,
} from "@phosphor-icons/react";
import { api, type PublicMeta } from "../lib/api";
import { Button, Chip, StatusDot } from "../components/ui";
import { copyText } from "../lib/format";
import {
  FanGrid,
  FanItem,
  Marquee,
  MaskedLines,
  Reveal,
  WordResolve,
} from "../components/motion";
import { ChallengeField } from "../components/ChallengeField";

/** Public source. Overridable at build time for a fork. */
const REPO_URL = import.meta.env.VITE_REPO_URL ?? "https://github.com/waguriagentic/captcha-solver";

/** Full-bleed shell: content spans the viewport with a gutter, no centred box. */
const SHELL = "w-full px-6 sm:px-8 lg:px-14 xl:px-20";

function Wordmark() {
  return (
    <Link to="/" className="flex items-center gap-2.5">
      <span className="grid size-7 place-items-center rounded-[8px] border border-line-strong bg-raised">
        <ShieldCheck size={15} weight="regular" className="text-accent" />
      </span>
      <span className="text-[13px] font-medium tracking-tight text-fg">
        Sonogami <span className="text-muted">Solver</span>
      </span>
    </Link>
  );
}

function CodeBlock({ lines, copyable }: { lines: string[]; copyable: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="overflow-hidden rounded-[10px] border border-line bg-[#0a0c0f]">
      <div className="flex items-center justify-between border-b border-line px-3.5 py-2">
        <span className="flex items-center gap-1.5 text-[11px] text-faint">
          <Terminal size={13} weight="regular" />
          <span className="font-mono">request</span>
        </span>
        <button
          type="button"
          onClick={async () => {
            if (await copyText(copyable)) {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1600);
            }
          }}
          className="flex items-center gap-1 rounded-[6px] px-1.5 py-1 text-[11px] text-faint transition-colors hover:bg-raised hover:text-fg"
        >
          {copied ? <Check size={12} weight="bold" /> : <Copy size={12} weight="regular" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="overflow-x-auto px-4 py-3 font-mono text-[12.5px] leading-[1.75] text-muted">
        {lines.map((line, i) => (
          <div key={i} className={line.startsWith("$") ? "text-fg" : undefined}>
            {line}
          </div>
        ))}
      </pre>
    </div>
  );
}

function SectionLabel({ label }: { label: string }) {
  return (
    <div className="mb-8 flex items-center gap-3">
      <span className="font-mono text-[10.5px] tracking-[0.16em] text-accent uppercase">
        {label}
      </span>
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}

export function LandingPage() {
  const [meta, setMeta] = useState<PublicMeta | null>(null);
  const [metaFailed, setMetaFailed] = useState(false);


  useEffect(() => {
    const controller = new AbortController();
    api
      .meta(controller.signal)
      .then(setMeta)
      .catch((error: unknown) => {
        if ((error as Error)?.name !== "AbortError") setMetaFailed(true);
      });
    return () => controller.abort();
  }, []);

  const apiBase = meta?.api_base_url ?? "https://api.example.com";
  const types = meta?.types ?? [];

  const curl = [
    `$ curl -X POST ${apiBase}/solve \\`,
    `    -H "Authorization: Bearer $TOKEN" \\`,
    `    -H "Content-Type: application/json" \\`,
    `    -d '{"type":"turnstile","sitekey":"0x4AAA...","url":"https://target.com"}'`,
    "",
    "{",
    '  "type": "turnstile",',
    '  "solved": true,',
    '  "token": "0.hF9k...",',
    '  "elapsed": 4.12',
    "}",
  ].join("\n");

  const origins = [
    {
      host: "dash.example.com",
      icon: Globe,
      title: "The console origin",
      body: "Serves this landing page, the login form and the console. Authenticates with a server-side session cookie that is HttpOnly, SameSite=Strict and scoped to this host alone.",
      chips: ["session cookie", "CSRF header on writes", "no API token"],
      note: "A scripting flaw here cannot reach the solver: /solve does not exist on this host.",
    },
    {
      host: "api.example.com",
      icon: Terminal,
      title: "The solver origin",
      body: "Serves /solve, /status, /logs and the interactive docs. Authenticates with a static Bearer token, sets no cookie, and has no login route to attack.",
      chips: ["Bearer token", "no cookies", "no login route"],
      note: "A leaked token cannot mint a session: the auth endpoints do not exist on this host.",
    },
  ];

  return (
    <div className="min-h-[100dvh] overflow-x-clip">
      <a
        href="#types"
        className="sr-only focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50 focus:rounded-[8px] focus:bg-raised focus:px-3 focus:py-2 focus:text-sm"
      >
        Skip to supported types
      </a>

      {/* ── Navigation: full bleed ───────────────────────────────── */}
      <header className="sticky top-0 z-40 border-b border-line bg-base/80 backdrop-blur-md">
        <nav className={`${SHELL} flex h-15 items-center justify-between`}>
          <Wordmark />
          <div className="flex items-center gap-1">
            <a
              href="/docs"
              className="hidden px-3 py-2 font-mono text-[10.5px] tracking-[0.12em] text-muted uppercase transition-colors hover:text-accent sm:block"
            >
              API reference
            </a>
            <a
              href="#types"
              className="hidden px-3 py-2 font-mono text-[10.5px] tracking-[0.12em] text-muted uppercase transition-colors hover:text-accent sm:block"
            >
              Coverage
            </a>
            <a
              href={REPO_URL}
              target="_blank"
              rel="noreferrer noopener"
              aria-label="Source on GitHub"
              className="flex items-center px-2.5 py-2 text-muted transition-colors hover:text-fg"
            >
              <GithubLogo size={15} weight="regular" />
            </a>
            <Link to="/login" className="ml-1">
              <Button variant="primary" size="sm">
                Console
                <ArrowRight size={13} weight="bold" />
              </Button>
            </Link>
          </div>
        </nav>
      </header>

      {/* ── Hero ─────────────────────────────────────────────────── */}
      <section className="relative overflow-hidden border-b border-line">
        <div className="bg-grid fade-edges pointer-events-none absolute inset-0 opacity-45" />

        <div
          className={`${SHELL} relative grid grid-cols-1 gap-12 pt-16 pb-16 lg:grid-cols-12 lg:gap-14 lg:pt-20 lg:pb-20`}
        >
          <div className="lg:col-span-7 lg:pt-3">
            <Reveal>
              <div className="mb-7 inline-flex items-center gap-2 rounded-full border border-line bg-panel/80 px-2.5 py-1">
                <StatusDot tone="ok" />
                <span className="font-mono text-[10.5px] tracking-[0.1em] text-muted uppercase">
                  self-hosted · {types.length || 11} types
                </span>
              </div>
            </Reveal>

            <h1 className="text-[2.75rem] leading-[0.97] font-semibold tracking-[-0.04em] text-balance sm:text-[3.6rem] lg:text-[4.2rem] xl:text-[4.6rem]">
              <MaskedLines lines={["Every captcha,", "reduced to one", "HTTP call."]} delay={0.06} />
            </h1>

            <Reveal delay={0.42}>
              <p className="mt-7 max-w-[54ch] text-[15px] leading-relaxed text-muted lg:text-[16px]">
                Turnstile, reCAPTCHA, hCaptcha, Cloudflare, AWS WAF, DataDome and five more.
                Driven through a real anti-detect browser, returning tokens you can replay
                immediately.
              </p>
            </Reveal>

            <Reveal delay={0.52}>
              <div className="mt-9 flex flex-wrap items-center gap-3">
                <a href="/docs">
                  <Button variant="primary">
                    Read the API
                    <ArrowRight size={13} weight="bold" />
                  </Button>
                </a>
                <a href="#types">
                  <Button variant="outline">See coverage</Button>
                </a>
              </div>
            </Reveal>
          </div>

          <Reveal delay={0.28} className="lg:col-span-5">
            <div className="flex flex-col gap-4">
              <div className="overflow-hidden rounded-[10px] border border-line bg-panel">
                <div className="flex items-center justify-between border-b border-line px-3.5 py-2.5">
                  <span className="flex items-center gap-2">
                    <StatusDot tone="ok" />
                    <span className="font-mono text-[10.5px] tracking-[0.14em] text-muted uppercase">
                      challenge field
                    </span>
                  </span>
                  <span className="font-mono text-[10.5px] text-faint">live</span>
                </div>
                <div className="h-[196px]">
                  <ChallengeField className="h-full w-full" />
                </div>
              </div>
              <CodeBlock lines={curl.split("\n")} copyable={curl} />
            </div>
          </Reveal>
        </div>
      </section>

      {/* ── 01 Coverage: full-bleed bento ────────────────────────── */}
      <section id="types" className="border-b border-line">
        <div className={`${SHELL} py-16 lg:py-20`}>
          <SectionLabel label="Coverage" />
          <div className="mb-9 grid grid-cols-1 gap-8 lg:grid-cols-12">
            <h2 className="text-[2rem] leading-[1.06] font-semibold tracking-[-0.032em] lg:col-span-7 lg:text-[2.5rem]">
              <WordResolve text="Eleven providers, one dispatch key." />
            </h2>
            <p className="max-w-[50ch] self-end text-[14px] leading-relaxed text-muted lg:col-span-4 lg:col-start-9">
              The request body's <code className="font-mono text-[13px] text-fg">type</code>{" "}
              selects the solver. Dispatch is the only branch a caller ever writes.
            </p>
          </div>

          <div className="grid grid-cols-1 gap-px overflow-hidden rounded-[10px] border border-line bg-line lg:grid-cols-12 lg:grid-rows-2">
            <div className="bg-panel p-7 lg:col-span-7 lg:row-span-2">
              <div className="flex items-baseline justify-between gap-3">
                <h3 className="font-mono text-[10.5px] tracking-[0.14em] text-faint uppercase">
                  supported types
                </h3>
                <span className="num text-[11px] text-accent">{types.length || 11}</span>
              </div>

              {metaFailed ? (
                <p className="mt-5 text-[13px] leading-relaxed text-faint">
                  Provider list unavailable right now. The API host serves it at{" "}
                  <span className="font-mono">{apiBase}/health</span>.
                </p>
              ) : types.length === 0 ? (
                <div className="mt-5 grid grid-cols-1 gap-x-8 sm:grid-cols-2" aria-hidden="true">
                  {Array.from({ length: 11 }, (_, i) => (
                    <div key={i} className="border-b border-line py-2.5">
                      <div className="skeleton h-4 rounded-[5px]" style={{ width: `${70 - i * 3}%` }} />
                    </div>
                  ))}
                </div>
              ) : (
                <FanGrid className="mt-5 grid grid-cols-1 gap-x-8 sm:grid-cols-2">
                  {types.map((entry, index) => (
                    <FanItem key={entry.type}>
                      <div className="group flex items-baseline gap-3 border-b border-line py-2.5">
                        <span className="num w-4 shrink-0 text-[10.5px] text-faint">
                          {String(index + 1).padStart(2, "0")}
                        </span>
                        <span className="font-mono text-[12.5px] text-fg transition-colors group-hover:text-accent">
                          {entry.type}
                        </span>
                        <span className="ml-auto hidden truncate text-[11.5px] text-faint lg:block">
                          {entry.label}
                        </span>
                      </div>
                    </FanItem>
                  ))}
                </FanGrid>
              )}
            </div>

            <div className="flex flex-col bg-panel p-7 lg:col-span-5">
              <div className="flex items-baseline justify-between gap-3">
                <h3 className="font-mono text-[10.5px] tracking-[0.14em] text-faint uppercase">
                  uniform signal
                </h3>
                <span className="num text-[11px] text-accent">1 bool</span>
              </div>
              <p className="mt-3 max-w-[46ch] text-[13px] leading-relaxed text-muted">
                Every response carries the same top level boolean, whatever the provider
                underneath. Callers never branch per type.
              </p>
              <pre className="mt-5 overflow-x-auto rounded-[8px] border border-line bg-[#0a0c0f] px-3.5 py-2.5 font-mono text-[12px] leading-relaxed text-accent">
                {`{ "solved": true }`}
              </pre>
            </div>

            <div className="flex flex-col bg-panel p-7 lg:col-span-5">
              <div className="flex items-baseline justify-between gap-3">
                <h3 className="font-mono text-[10.5px] tracking-[0.14em] text-faint uppercase">
                  failure contract
                </h3>
                <span className="num text-[11px] text-accent">2 rules</span>
              </div>
              <ul className="mt-3 flex flex-col gap-2.5 text-[13px] leading-relaxed text-muted">
                <li className="flex gap-2.5">
                  <span className="num shrink-0 text-faint">200</span>
                  <span>the solve ran but failed. Read solved:false and the error field.</span>
                </li>
                <li className="flex gap-2.5">
                  <span className="num shrink-0 text-faint">4xx</span>
                  <span>the request never solved. Read detail, never both.</span>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      {/* ── 02 Isolation: full-bleed auto-advancing rail ─────────── */}
      <section className="border-b border-line">
        <div className={`${SHELL} py-16 lg:py-20`}>
          <SectionLabel label="Isolation" />
          <div className="mb-9 grid grid-cols-1 gap-8 lg:grid-cols-12">
            <h2 className="text-[2rem] leading-[1.06] font-semibold tracking-[-0.032em] lg:col-span-7 lg:text-[2.5rem]">
              <WordResolve text="The console and the API never share an origin." />
            </h2>
            <p className="max-w-[46ch] self-end text-[14px] leading-relaxed text-muted lg:col-span-4 lg:col-start-9">
              Two hostnames, two credentials, no overlap. A flaw in either surface cannot be
              turned into access to the other.
            </p>
          </div>
        </div>

        <Marquee>
          {origins.map((origin) => (
            <article
              key={origin.host}
              className="w-[min(88vw,660px)] shrink-0 border-r border-line px-7 pb-14 lg:px-10"
            >
              <div className="flex items-center gap-2.5">
                <origin.icon size={15} weight="regular" className="text-faint" />
                <span className="font-mono text-[13px] text-fg">{origin.host}</span>
              </div>
              <h3 className="mt-5 text-[17px] font-medium tracking-tight text-fg">
                {origin.title}
              </h3>
              <p className="mt-3 max-w-[52ch] text-[13.5px] leading-relaxed text-muted">
                {origin.body}
              </p>
              <div className="mt-5 flex flex-wrap gap-2">
                {origin.chips.map((chip, i) => (
                  <Chip key={chip} tone={i === 0 ? "ok" : "idle"}>
                    {chip}
                  </Chip>
                ))}
              </div>
              <p className="mt-5 max-w-[52ch] border-t border-line pt-4 text-[12.5px] leading-relaxed text-faint">
                {origin.note}
              </p>
            </article>
          ))}
        </Marquee>
      </section>

      {/* ── 03 Quickstart: sticky rail ───────────────────────────── */}
      <section className="border-b border-line">
        <div
          className={`${SHELL} grid grid-cols-1 gap-12 py-16 lg:grid-cols-12 lg:gap-16 lg:py-20`}
        >
          <div className="lg:col-span-4">
            <div className="lg:sticky lg:top-24">
              <SectionLabel label="Quickstart" />
              <h2 className="text-[2rem] leading-[1.06] font-semibold tracking-[-0.032em] lg:text-[2.5rem]">
                <WordResolve text="Running in three steps." />
              </h2>
              <p className="mt-4 max-w-[40ch] text-[14px] leading-relaxed text-muted">
                The solver is a systemd unit behind an X display. The console is a static bundle
                served by the same process.
              </p>
            </div>
          </div>

          <ol className="lg:col-span-8">
            {[
              {
                title: "Set the admin credential",
                body: "Generate a digest and put it in the unit environment. Without it the console returns 404 and the solver keeps running unchanged.",
                code: "python -m web.auth generate\npython -m web.auth hash",
              },
              {
                title: "Build the console",
                body: "One Vite build emits a static bundle into web/ui/dist, which the server picks up on the next start.",
                code: "npm --prefix web/ui install\nnpm --prefix web/ui run build",
              },
              {
                title: "Call the API",
                body: "Bearer token on the API origin. Health stays public so monitors and the installer can poll it.",
                code: `curl ${apiBase}/health`,
              },
            ].map((step, index) => (
              <Reveal key={step.title} delay={index * 0.05}>
                <li className="border-t border-line py-8 first:border-t-0 first:pt-0">
                  <div className="flex gap-6">
                    <span className="num mt-1 text-[12px] text-accent">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0 flex-1">
                      <h3 className="text-[16px] font-medium tracking-tight text-fg">
                        {step.title}
                      </h3>
                      <p className="mt-2 max-w-[68ch] text-[13.5px] leading-relaxed text-muted">
                        {step.body}
                      </p>
                      <pre className="mt-4 overflow-x-auto rounded-[8px] border border-line bg-[#0a0c0f] px-3.5 py-3 font-mono text-[12px] leading-relaxed text-muted">
                        {step.code}
                      </pre>
                    </div>
                  </div>
                </li>
              </Reveal>
            ))}
          </ol>
        </div>
      </section>

      {/* ── Close: brand line only. The nav above owns the links. ── */}
      <footer className={`${SHELL} py-9`}>
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
          <div className="flex items-center gap-3">
            <ShieldCheck size={14} weight="regular" className="text-faint" />
            <span className="text-[12px] text-muted">
              Sonogami <span className="text-faint">Solver</span>
            </span>
          </div>
          {meta ? <span className="font-mono text-[11px] text-faint">v{meta.version}</span> : null}
        </div>
      </footer>
    </div>
  );
}
