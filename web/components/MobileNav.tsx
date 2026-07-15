"use client";

// Mobile masthead menu (☰) — shown below `lg`, where the full horizontal nav
// would overflow the viewport. A small client island: it toggles a dropdown
// listing the same PRIMARY_NAV links the desktop nav renders. Closes on link
// click (each link's onClick), Escape, and outside click. The desktop nav
// (layout.tsx) is `hidden lg:block`; this button is `lg:hidden`, so exactly one
// is ever visible/in the a11y tree.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { PRIMARY_NAV } from "@/lib/nav";

export function MobileNav() {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // While open, close on Escape or a click outside the menu.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onPointer = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative lg:hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={open ? "Close menu" : "Open menu"}
        aria-expanded={open}
        aria-controls="mobile-nav-menu"
        className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-edge text-ink-soft hover:text-ink hover:border-ink-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent transition-colors"
      >
        {open ? "✕" : "☰"}
      </button>

      {open && (
        <nav
          id="mobile-nav-menu"
          aria-label="Primary"
          className="absolute right-0 top-full mt-2 w-44 rounded-md border border-edge bg-canvas py-1 shadow-lg"
        >
          <ul className="flex flex-col text-sm">
            {PRIMARY_NAV.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  onClick={() => setOpen(false)}
                  className="block px-3 py-2 text-ink-muted hover:bg-edge/30 hover:text-ink transition-colors"
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      )}
    </div>
  );
}
