// Global not-found boundary. App Router renders this inside the root layout, so
// unmatched routes (and any notFound() call without a closer boundary — e.g.
// unknown /tag and /location values) get the nous masthead + a way back instead
// of Next.js's bare unstyled "404 / This page could not be found." screen.
import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Page not found",
};

export default function NotFound() {
  return (
    <main className="flex-1 flex flex-col items-center justify-center px-6 py-24 text-center">
      <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-ink-muted">
        404
      </p>
      <h1 className="mt-4 text-4xl font-bold tracking-tight text-ink">
        Page not found
      </h1>
      <p className="mt-4 max-w-md text-ink-muted leading-relaxed">
        That page doesn&rsquo;t exist or may have moved. Try browsing the index,
        or let chance pick one for you.
      </p>
      <div className="mt-8 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-sm">
        <Link
          href="/companies"
          className="text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
        >
          Browse companies
        </Link>
        <Link
          href="/investors"
          className="text-ink-soft hover:text-ink transition-colors"
        >
          Investors
        </Link>
        <Link
          href="/surprise"
          className="text-ink-soft hover:text-ink transition-colors"
        >
          Surprise me
        </Link>
        <Link href="/" className="text-ink-soft hover:text-ink transition-colors">
          Home
        </Link>
      </div>
    </main>
  );
}
