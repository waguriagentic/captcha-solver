/**
 * Console visualisations.
 *
 * Same principle as the landing page's hero: the console's own subject is
 * solves resolving over time, so the live view renders exactly that rather than
 * shipping a generic chart. Everything here is a pure function of the snapshot
 * it is handed, so it re-renders from the stream with no local state to drift.
 */
import { useEffect, useRef } from "react";
import { motion, useReducedMotion } from "motion/react";
import type { SeriesPoint, SolveEvent } from "../lib/api";

const ACCENT = "47, 191, 133";
const DANGER = "224, 87, 91";
const HAIRLINE = "232, 234, 237";

/**
 * Live activity field: one column per recent solve, newest on the right,
 * height by latency, colour by outcome. A ticker of the product's own work.
 */
export function ActivityField({
  points,
  className,
  max = 90,
}: {
  points: SeriesPoint[];
  className?: string;
  max?: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    function draw() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas!.getBoundingClientRect();
      const width = rect.width;
      const height = rect.height;
      if (width < 2 || height < 2) return;
      canvas!.width = Math.max(1, Math.round(width * dpr));
      canvas!.height = Math.max(1, Math.round(height * dpr));
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx!.clearRect(0, 0, width, height);

      const visible = points.slice(-max);
      if (!visible.length) {
        ctx!.strokeStyle = `rgba(${HAIRLINE}, 0.08)`;
        ctx!.lineWidth = 1;
        ctx!.setLineDash([3, 4]);
        ctx!.beginPath();
        ctx!.moveTo(0, height - 0.5);
        ctx!.lineTo(width, height - 0.5);
        ctx!.stroke();
        return;
      }

      // Latency sets the height, so the shape of the field is the shape of the
      // work: a slow solve is a tall column, a failure is a red one.
      const latencies = visible.map((p) => p.elapsed ?? 0);
      const peak = Math.max(1, ...latencies);
      const slot = width / max;
      const barWidth = Math.max(1.5, slot - 2);
      const offset = width - visible.length * slot;

      visible.forEach((point, i) => {
        const x = offset + i * slot;
        const ratio = Math.max(0.06, (point.elapsed ?? 0) / peak);
        const barHeight = Math.max(2, ratio * (height - 6));
        const y = height - barHeight;
        ctx!.fillStyle = point.ok
          ? `rgba(${ACCENT}, ${0.35 + ratio * 0.5})`
          : `rgba(${DANGER}, 0.75)`;
        ctx!.fillRect(x, y, barWidth, barHeight);
      });

      // Baseline rule, so short bars still read as sitting on something.
      ctx!.fillStyle = `rgba(${HAIRLINE}, 0.14)`;
      ctx!.fillRect(0, height - 1, width, 1);
    }

    draw();

    // The canvas is CSS-sized, so a viewport change would otherwise stretch the
    // existing bitmap until the next data frame happened to arrive.
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [points, max, reduced]);

  return <canvas ref={canvasRef} aria-hidden="true" className={className} />;
}

/**
 * Latency trace over the same window, with a p95 rule. Drawn as an area so the
 * tail of slow solves is visible rather than averaged away.
 */
export function LatencyTrace({ points }: { points: SeriesPoint[] }) {
  const series = points
    .map((p) => p.elapsed)
    .filter((v): v is number => typeof v === "number");

  if (series.length < 2) {
    return (
      <div className="grid h-[72px] place-items-center font-mono text-[11px] text-faint">
        awaiting data
      </div>
    );
  }

  const width = 600;
  const height = 72;
  const peak = Math.max(...series);
  const step = width / (series.length - 1);
  const yOf = (value: number) => height - 6 - (value / (peak || 1)) * (height - 16);

  const line = series
    .map((value, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${yOf(value).toFixed(1)}`)
    .join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;

  // p95 of the same window, so the rule matches the number shown in the KPIs.
  const ordered = [...series].sort((a, b) => a - b);
  const p95 = ordered[Math.min(ordered.length - 1, Math.round(0.95 * (ordered.length - 1)))];

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-[72px] w-full"
      preserveAspectRatio="none"
      role="img"
      aria-label={`Latency across the last ${series.length} solves, peak ${peak.toFixed(1)}s, p95 ${p95.toFixed(1)}s`}
    >
      <defs>
        <linearGradient id="latency-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={`rgb(${ACCENT})`} stopOpacity="0.22" />
          <stop offset="100%" stopColor={`rgb(${ACCENT})`} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#latency-fill)" />
      <path d={line} fill="none" stroke={`rgb(${ACCENT})`} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      <line
        x1="0"
        x2={width}
        y1={yOf(p95)}
        y2={yOf(p95)}
        stroke={`rgb(${HAIRLINE})`}
        strokeOpacity="0.25"
        strokeDasharray="4 4"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

/** Success ring. The single accent colour, used as a proportion, not a glow. */
export function SuccessRing({ rate, size = 74 }: { rate: number; size?: number }) {
  const reduced = useReducedMotion();
  const stroke = 5;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(1, rate));

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke={`rgba(${HAIRLINE}, 0.12)`}
        strokeWidth={stroke}
      />
      <motion.circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke={`rgb(${ACCENT})`}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={circumference}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        initial={reduced ? false : { strokeDashoffset: circumference }}
        animate={{ strokeDashoffset: circumference * (1 - clamped) }}
        transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1] }}
      />
    </svg>
  );
}

/** Inline row for the in-flight list: the type, target and a live elapsed. */
export function RunningRow({ event, now }: { event: SolveEvent; now: number }) {
  const elapsed = Math.max(0, now - event.timestamp);
  return (
    <div className="flex items-center gap-3 border-b border-line px-5 py-2.5 last:border-b-0">
      <span className="relative inline-block size-1.5 shrink-0 rounded-full bg-accent">
        <span className="live-dot absolute inset-0 rounded-full bg-accent opacity-60" />
      </span>
      <span className="font-mono text-[12px] text-fg">{event.type}</span>
      <span className="truncate font-mono text-[11.5px] text-faint">
        {event.url || event.sitekey || "—"}
      </span>
      <span className="num ml-auto shrink-0 text-[11.5px] text-muted">
        {elapsed.toFixed(1)}s
      </span>
    </div>
  );
}
