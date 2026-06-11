"use client";

// Theme toggle (◐) — flips the `.dark` class on <html> and persists the choice
// to localStorage. Dark is the site default: the no-flash script in layout.tsx
// only removes `.dark` when localStorage.theme === "light", so "dark" and
// unset behave identically.
export function ThemeToggle() {
  const toggle = () => {
    const root = document.documentElement;
    const toLight = root.classList.contains("dark");
    root.classList.toggle("dark", !toLight);
    try {
      localStorage.theme = toLight ? "light" : "dark";
    } catch {
      // localStorage unavailable (private mode) — the flip still applies for
      // this page view, it just won't survive a reload.
    }
  };

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label="Toggle color theme"
      title="Toggle color theme"
      className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-edge text-ink-soft hover:text-ink hover:border-ink-muted transition-colors"
    >
      ◐
    </button>
  );
}
