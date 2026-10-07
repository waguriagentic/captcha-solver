/**
 * Motion primitives for the marketing surface.
 *
 * The one rule that matters here: a hidden start state must NEVER be reachable
 * without a script to undo it. Every component below renders plain, visible
 * markup when `prefers-reduced-motion` is set — the animation is additive, so
 * the copy is readable even if the animation never runs.
 */
import { motion, useReducedMotion } from "motion/react";
import type { ReactNode } from "react";
import clsx from "clsx";

const EASE_OUT_EXPO = [0.16, 1, 0.3, 1] as const;

/**
 * Display headline built from explicitly authored lines, each wiping up from
 * behind its own clipped edge. Lines are passed in rather than measured: the
 * break points of a display headline are a design decision, not a runtime one.
 *
 * `pb` on the clipping element reserves descender room — padding is inside the
 * overflow box, so `overflow-hidden` clips at the padded edge, not the glyph
 * baseline.
 */
export function MaskedLines({
  lines,
  className,
  lineClassName,
  delay = 0,
  stagger = 0.075,
}: {
  lines: string[];
  className?: string;
  lineClassName?: string;
  delay?: number;
  stagger?: number;
}) {
  const reduced = useReducedMotion();

  if (reduced) {
    return (
      <span className={className}>
        {lines.map((line, i) => (
          <span key={i} className={clsx("block", lineClassName)}>
            {line}
          </span>
        ))}
      </span>
    );
  }

  return (
    <span className={className}>
      {lines.map((line, i) => (
        <span key={i} className={clsx("block overflow-hidden pb-[0.14em]", lineClassName)}>
          <motion.span
            className="block"
            initial={{ y: "118%" }}
            animate={{ y: "0%" }}
            transition={{ duration: 0.9, delay: delay + i * stagger, ease: EASE_OUT_EXPO }}
          >
            {line}
          </motion.span>
        </span>
      ))}
    </span>
  );
}

/** Fade-and-rise on first view. One-shot: re-entering does not replay it. */
export function Reveal({
  children,
  className,
  delay = 0,
  distance = 14,
}: {
  children: ReactNode;
  className?: string;
  delay?: number;
  distance?: number;
}) {
  const reduced = useReducedMotion();
  if (reduced) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: distance }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-12% 0px -8% 0px" }}
      transition={{ duration: 0.6, delay, ease: EASE_OUT_EXPO }}
    >
      {children}
    </motion.div>
  );
}

/**
 * Words resolve in sequence out of a dimmed state, so a statement reads as
 * assembling rather than fading in.
 */
export function WordResolve({
  text,
  className,
  delay = 0,
}: {
  text: string;
  className?: string;
  delay?: number;
}) {
  const reduced = useReducedMotion();
  const words = text.split(" ");

  if (reduced) return <span className={className}>{text}</span>;

  return (
    <span className={className}>
      {words.map((word, i) => (
        <motion.span
          key={`${word}-${i}`}
          className="inline-block"
          initial={{ opacity: 0.1, y: "0.32em" }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-18% 0px" }}
          transition={{ duration: 0.5, delay: delay + i * 0.045, ease: EASE_OUT_EXPO }}
        >
          {word}
          {i < words.length - 1 ? "\u00A0" : ""}
        </motion.span>
      ))}
    </span>
  );
}

/** Card grid that fans in on a perspective tilt, staggered. */
export function FanGrid({ children, className }: { children: ReactNode; className?: string }) {
  const reduced = useReducedMotion();
  if (reduced) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      style={{ perspective: 900 }}
      initial="hidden"
      whileInView="shown"
      viewport={{ once: true, margin: "-15% 0px" }}
      variants={{ hidden: {}, shown: { transition: { staggerChildren: 0.1 } } }}
    >
      {children}
    </motion.div>
  );
}

export function FanItem({ children, className }: { children: ReactNode; className?: string }) {
  const reduced = useReducedMotion();
  if (reduced) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      style={{ transformOrigin: "50% 100%" }}
      variants={{
        hidden: { opacity: 0, y: 56, rotateX: 12 },
        shown: { opacity: 1, y: 0, rotateX: 0, transition: { duration: 0.75, ease: EASE_OUT_EXPO } },
      }}
    >
      {children}
    </motion.div>
  );
}

/**
 * Full-bleed auto-advancing rail.
 *
 * The children are rendered twice inside one track that translates exactly
 * -50%, so the loop is seamless without JS. It pauses on hover and on keyboard
 * focus, and under `prefers-reduced-motion` it degrades to a static, wrapped
 * grid — a moving target nobody can stop is an accessibility failure, not a
 * flourish.
 */
export function Marquee({ children }: { children: ReactNode }) {
  const reduced = useReducedMotion();
  if (reduced) {
    return <div className="grid grid-cols-1 gap-px bg-line md:grid-cols-2">{children}</div>;
  }
  return (
    <div className="marquee-viewport overflow-hidden">
      <div className="marquee-track flex w-max">
        <div className="flex shrink-0">{children}</div>
        <div aria-hidden="true" className="flex shrink-0">
          {children}
        </div>
      </div>
    </div>
  );
}
