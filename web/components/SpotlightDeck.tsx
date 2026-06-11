"use client";

// Spotlight deck — the front page's only nontrivial client component. Receives
// the server-built daily pool as props and only handles cycling: ‹ › buttons,
// ← → keys, and horizontal touch swipes. No auto-advance (spec §2).

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import type { Spotlight } from "@/lib/spotlight";

interface Props {
  spotlights: Spotlight[];
}

/** Minimum horizontal travel (px) for a touch to count as a swipe. */
const SWIPE_THRESHOLD = 48;

export function SpotlightDeck({ spotlights }: Props) {
  const [index, setIndex] = useState(0);
  const touchStart = useRef<{ x: number; y: number } | null>(null);
  const count = spotlights.length;

  const cycle = useCallback(
    (delta: number) => {
      if (count === 0) return;
      setIndex((i) => (i + delta + count) % count);
    },
    [count],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      // Leave form fields alone — the masthead search uses the same keys.
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (event.key === "ArrowLeft") cycle(-1);
      else if (event.key === "ArrowRight") cycle(1);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [cycle]);

  const onTouchStart = (event: React.TouchEvent) => {
    const touch = event.touches[0];
    if (touch) touchStart.current = { x: touch.clientX, y: touch.clientY };
  };

  const onTouchEnd = (event: React.TouchEvent) => {
    const start = touchStart.current;
    touchStart.current = null;
    const touch = event.changedTouches[0];
    if (!start || !touch) return;
    const dx = touch.clientX - start.x;
    const dy = touch.clientY - start.y;
    // Horizontal-dominant moves only, so vertical page scrolls don't cycle.
    if (Math.abs(dx) >= SWIPE_THRESHOLD && Math.abs(dx) > Math.abs(dy)) {
      cycle(dx < 0 ? 1 : -1);
    }
  };

  const current = spotlights[index];
  if (!current) return null;

  return (
    <section
      role="group"
      aria-roledescription="carousel"
      aria-label="Today's spotlight"
      onTouchStart={onTouchStart}
      onTouchEnd={onTouchEnd}
    >
      <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-ink-muted">
        Today&rsquo;s spotlight{" "}
        <span className="text-ink-faint">
          · {index + 1}/{count}
        </span>
      </p>

      {/* Stable polite live region; the keyed child remounts per spotlight so
          the enter animation replays (motion-safe only). */}
      <div aria-live="polite">
        <div
          key={current.slug}
          className="motion-safe:animate-[spotlight-in_240ms_ease-out]"
        >
          <h1 className="mt-5 text-4xl sm:text-5xl font-bold tracking-tight text-ink">
            {current.name}
          </h1>
          <p className="mt-4 text-lg text-ink-soft leading-relaxed max-w-lg">
            {current.oneLiner}
          </p>
          {current.facts.length > 0 && (
            <p className="mt-5 font-mono text-sm text-ink-muted">
              {current.facts.join(" · ")}
            </p>
          )}
          <p className="mt-7">
            <Link
              href={`/c/${current.slug}`}
              className="text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
            >
              Read profile
            </Link>
          </p>
        </div>
      </div>

      <div className="mt-10 flex items-center gap-4 text-ink-muted">
        <button
          type="button"
          onClick={() => cycle(-1)}
          aria-label="Previous spotlight"
          className="px-1 text-xl leading-none hover:text-ink transition-colors"
        >
          ‹
        </button>
        <span className="flex items-center gap-1.5" aria-hidden="true">
          {spotlights.map((spotlight, i) => (
            <span
              key={spotlight.slug}
              className={
                i === index
                  ? "h-1 w-3 rounded-full bg-accent motion-safe:transition-all"
                  : "h-1 w-1 rounded-full bg-ink-faint motion-safe:transition-all"
              }
            />
          ))}
        </span>
        <button
          type="button"
          onClick={() => cycle(1)}
          aria-label="Next spotlight"
          className="px-1 text-xl leading-none hover:text-ink transition-colors"
        >
          ›
        </button>
        <span
          className="hidden sm:inline font-mono text-[11px] text-ink-faint"
          aria-hidden="true"
        >
          ⌨ ← →
        </span>
      </div>
    </section>
  );
}
