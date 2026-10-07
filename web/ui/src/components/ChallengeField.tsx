/**
 * The hero backdrop: a field of challenge tiles resolving in a diagonal wave.
 *
 * Product-native rather than ambient decoration. A captcha grid being solved is
 * literally what the service does, so the hero subject is the product's own
 * subject, drawn in code with no asset to ship.
 *
 * Two things this deliberately gets right:
 *  - The grid is fitted to the canvas, never cropped. Column and row counts are
 *    derived from the available space so a tile is never half-drawn at an edge,
 *    which is what makes a canvas field look broken.
 *  - Cost is bounded: devicePixelRatio capped at 2, the loop is suspended by an
 *    IntersectionObserver when off screen, and under `prefers-reduced-motion`
 *    exactly one static frame is drawn so the backdrop is never empty.
 */
import { useEffect, useRef } from "react";

const ACCENT = "47, 191, 133";
const HAIRLINE = "232, 234, 237";

const TILE = 15;
const GAP = 7;
const PERIOD = 5.6;
const WAVE = 0.075;

export function ChallengeField({ className }: { className?: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let cols = 0;
    let rows = 0;
    let width = 0;
    let height = 0;
    let offsets: number[] = [];

    function resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas!.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      canvas!.width = Math.max(1, Math.round(width * dpr));
      canvas!.height = Math.max(1, Math.round(height * dpr));
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);

      // Fit whole tiles only: derive the count from the space we actually have.
      const span = TILE + GAP;
      cols = Math.max(1, Math.floor((width + GAP) / span));
      rows = Math.max(1, Math.floor((height + GAP) / span));
      offsets = new Array(cols * rows);
      for (let i = 0; i < offsets.length; i++) offsets[i] = Math.random() * 1.4;
    }

    /** 0 to 1 progress of a tile's own cycle, with a hold while solved. */
    function fillLevel(phase: number): number {
      if (phase < 0.4) return 0;
      if (phase < 0.54) return (phase - 0.4) / 0.14;
      if (phase < 0.88) return 1;
      return 1 - (phase - 0.88) / 0.12;
    }

    function drawCheck(x: number, y: number, alpha: number) {
      const s = TILE / 15;
      ctx!.strokeStyle = `rgba(${ACCENT}, ${alpha})`;
      ctx!.lineWidth = Math.max(1, 1.7 * s);
      ctx!.lineCap = "round";
      ctx!.lineJoin = "round";
      ctx!.beginPath();
      ctx!.moveTo(x + 4 * s, y + 7.9 * s);
      ctx!.lineTo(x + 6.6 * s, y + 10.4 * s);
      ctx!.lineTo(x + 11.2 * s, y + 4.9 * s);
      ctx!.stroke();
    }

    function frame(elapsed: number) {
      ctx!.clearRect(0, 0, width, height);
      const span = TILE + GAP;
      const gridW = cols * TILE + (cols - 1) * GAP;
      const gridH = rows * TILE + (rows - 1) * GAP;
      const ox = (width - gridW) / 2;
      const oy = (height - gridH) / 2;

      for (let r = 0; r < rows; r++) {
        for (let c = 0; c < cols; c++) {
          const i = r * cols + c;
          const phase =
            ((((elapsed - ((c + r) * WAVE + offsets[i])) / PERIOD) % 1) + 1) % 1;
          const fill = fillLevel(phase);
          const x = ox + c * span;
          const y = oy + r * span;

          if (fill > 0) {
            ctx!.fillStyle = `rgba(${ACCENT}, ${0.1 + fill * 0.16})`;
            ctx!.fillRect(x, y, TILE, TILE);
            ctx!.strokeStyle = `rgba(${ACCENT}, ${0.2 + fill * 0.42})`;
            ctx!.lineWidth = 1;
            ctx!.strokeRect(x + 0.5, y + 0.5, TILE - 1, TILE - 1);
            if (phase >= 0.54 && phase < 0.88) drawCheck(x, y, 0.5 + fill * 0.45);
          } else {
            ctx!.strokeStyle = `rgba(${HAIRLINE}, 0.07)`;
            ctx!.lineWidth = 1;
            ctx!.strokeRect(x + 0.5, y + 0.5, TILE - 1, TILE - 1);
          }
        }
      }
    }

    resize();

    if (reduced) {
      frame(2.4);
      const onResizeStatic = () => {
        resize();
        frame(2.4);
      };
      window.addEventListener("resize", onResizeStatic);
      return () => window.removeEventListener("resize", onResizeStatic);
    }

    let raf = 0;
    let start = performance.now();
    let running = false;

    const tick = (now: number) => {
      frame((now - start) / 1000);
      raf = requestAnimationFrame(tick);
    };
    const begin = () => {
      if (running) return;
      running = true;
      start = performance.now() - 900; // start mid-cycle, never on an empty grid
      raf = requestAnimationFrame(tick);
    };
    const stop = () => {
      running = false;
      cancelAnimationFrame(raf);
    };

    const observer = new IntersectionObserver(
      ([entry]) => (entry.isIntersecting ? begin() : stop()),
      { threshold: 0 },
    );
    observer.observe(canvas);
    begin();

    const onResize = () => resize();
    window.addEventListener("resize", onResize);

    return () => {
      stop();
      observer.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={className}
      style={{ width: "100%", height: "100%", display: "block" }}
    />
  );
}
