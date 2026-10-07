/** Presentation-only formatting. Kept out of components so the console reads
 *  consistently everywhere and the number handling is testable in one place. */

const RELATIVE_UNITS: Array<[limit: number, divisor: number, suffix: string]> = [
  [60, 1, "s"],
  [3600, 60, "m"],
  [86400, 3600, "h"],
  [604800, 86400, "d"],
];

/** "12s ago" / "3m ago" / "—" for a missing or future timestamp. */
export function relativeTime(seconds: number | null | undefined, now = Date.now() / 1000): string {
  if (!seconds) return "—";
  const delta = Math.max(0, now - seconds);
  if (delta < 5) return "just now";
  for (const [limit, divisor, suffix] of RELATIVE_UNITS) {
    if (delta < limit) return `${Math.floor(delta / divisor)}${suffix} ago`;
  }
  return `${Math.floor(delta / 604800)}w ago`;
}

export function clockTime(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Seconds with a fixed precision — solve latency is sub-second sensitive. */
export function seconds(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : `${value.toFixed(digits)}s`;
}

export function percent(value: number | null | undefined, digits = 1): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

/** Uptime as 1h 04m / 3d 02h — never more than two units. */
export function duration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 86400)}d ${String(Math.floor((s % 86400) / 3600)).padStart(2, "0")}h`;
}

/** Shorten a sitekey or URL for dense table cells without losing the head. */
export function truncate(value: string, max = 42): string {
  if (!value) return "—";
  return value.length <= max ? value : `${value.slice(0, max - 1)}…`;
}

/** Copy helper with a clipboard-API fallback for non-secure origins. */
export async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}
