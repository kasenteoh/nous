import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { ThemeToggle } from "@/components/ThemeToggle";
import { CompareBar } from "@/components/CompareBar";
import { SITE_NAME, repoIssueUrl, siteOrigin } from "@/lib/site";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  // Resolves relative canonical/OG URLs site-wide. Falls back to localhost
  // when neither NEXT_PUBLIC_SITE_URL nor Vercel's production URL is set.
  metadataBase: new URL(siteOrigin()),
  title: {
    default: "nous — US software startup discovery",
    // Page-level titles are bare ("About", company name, …); the suffix is
    // applied here exactly once.
    template: "%s — nous",
  },
  description: "US software startup discovery, from VC portfolios and funding news.",
  openGraph: {
    siteName: SITE_NAME,
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
  },
};

// Dark (Tokyo Night) is the default for every visitor: `.dark` is server-
// rendered on <html>, and this script removes it before first paint when the
// visitor has explicitly chosen light. It must run synchronously before any
// content is parsed, so it is inlined as the first element of <body> —
// next/script can't inline beforeInteractive scripts, and the App Router
// reserves <head> for the Metadata API.
const noFlashScript = `try{if(localStorage.theme==="light")document.documentElement.classList.remove("dark")}catch(e){}`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      // suppressHydrationWarning: the no-flash script may strip `.dark` before
      // hydration, which is an expected server/client class mismatch.
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} dark h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-canvas text-ink-soft">
        <script dangerouslySetInnerHTML={{ __html: noFlashScript }} />

        {/* Skip link — first focusable element, lets keyboard/SR users jump
            past the masthead to the page content. */}
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-3 focus:z-50 focus:rounded-md focus:bg-ink focus:px-3 focus:py-2 focus:text-sm focus:text-canvas"
        >
          Skip to main content
        </a>

        {/* ── Masthead (site-wide) ────────────────────────────────────── */}
        <header className="sticky top-0 z-10 border-b border-edge bg-canvas/90 backdrop-blur-sm">
          <div className="max-w-6xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between gap-3">
            {/* Wordmark */}
            <Link
              href="/"
              className="text-lg font-semibold tracking-tight text-ink hover:opacity-80 transition-opacity"
            >
              nous
            </Link>

            <div className="flex items-center gap-3 sm:gap-5 text-sm">
              {/* Compact search — GET form into /companies. Collapses to a ⌕
                  link on small screens. */}
              <form action="/companies" method="GET" className="hidden md:block">
                <input
                  type="search"
                  name="q"
                  placeholder="Search"
                  aria-label="Search companies"
                  className="w-36 rounded-md border border-edge bg-transparent px-2.5 py-1 text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:border-ink-muted focus-visible:ring-2 focus-visible:ring-accent focus:w-44 transition-all"
                />
              </form>
              <Link
                href="/companies"
                aria-label="Search companies"
                className="md:hidden text-ink-muted hover:text-ink transition-colors"
              >
                ⌕
              </Link>

              <nav aria-label="Primary">
                <ul className="flex items-center gap-3 sm:gap-5 text-ink-muted">
                  <li>
                    <Link
                      href="/companies"
                      className="hover:text-ink transition-colors"
                    >
                      Browse
                    </Link>
                  </li>
                  <li>
                    <Link
                      href="/investors"
                      className="hover:text-ink transition-colors"
                    >
                      Investors
                    </Link>
                  </li>
                  <li>
                    <Link
                      href="/themes"
                      className="hover:text-ink transition-colors"
                    >
                      Themes
                    </Link>
                  </li>
                  <li>
                    <Link
                      href="/surprise"
                      className="hover:text-ink transition-colors whitespace-nowrap"
                    >
                      Surprise me
                    </Link>
                  </li>
                  <li>
                    <Link
                      href="/about"
                      className="hover:text-ink transition-colors"
                    >
                      About
                    </Link>
                  </li>
                </ul>
              </nav>

              <ThemeToggle />
            </div>
          </div>
        </header>

        {/* Skip-link target + content region. flex-1 flex-col keeps each
            page's <main className="flex-1"> filling the space as before. */}
        <div id="main-content" tabIndex={-1} className="flex-1 flex flex-col">
          {children}
        </div>

        {/* ── Footer (site-wide) ──────────────────────────────────────── */}
        <footer className="border-t border-edge">
          <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 flex flex-col gap-5 text-sm">
            <nav
              aria-label="Footer"
              className="flex flex-wrap gap-x-5 gap-y-2 text-ink-muted"
            >
              <Link href="/companies" className="hover:text-ink transition-colors">
                Browse
              </Link>
              <Link href="/investors" className="hover:text-ink transition-colors">
                Investors
              </Link>
              <Link href="/themes" className="hover:text-ink transition-colors">
                Themes
              </Link>
              <Link href="/new" className="hover:text-ink transition-colors">
                New this week
              </Link>
              <Link href="/about" className="hover:text-ink transition-colors">
                About
              </Link>
            </nav>

            {/* Browse-by-location entry point — these pages exist and are
                paginated but were otherwise unreachable by clicking. */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-ink-faint">
              <span className="text-ink-muted">Hubs:</span>
              <Link href="/location/CA" className="hover:text-ink-soft transition-colors">California</Link>
              <Link href="/location/NY" className="hover:text-ink-soft transition-colors">New York</Link>
              <Link href="/location/MA" className="hover:text-ink-soft transition-colors">Massachusetts</Link>
              <Link href="/location/TX" className="hover:text-ink-soft transition-colors">Texas</Link>
              <Link href="/location/WA" className="hover:text-ink-soft transition-colors">Washington</Link>
            </div>

            <p className="text-ink-muted leading-relaxed max-w-2xl">
              {SITE_NAME} is an automated directory of US software startups,
              assembled from public sources. Figures may be incomplete or out of
              date — this is information, not investment advice. Spotted an error?{" "}
              <a
                href={repoIssueUrl(
                  "Data correction",
                  "Page URL:\n\nWhat's incorrect:\n",
                )}
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-2 decoration-ink-faint hover:text-ink"
              >
                Report it
              </a>
              .
            </p>

            <p className="text-ink-faint">© 2026 {SITE_NAME}</p>
          </div>
        </footer>

        {/* Compare selection bar — sticky to the viewport bottom, site-wide, so
            a selection survives navigation. Client island; renders nothing until
            the visitor has ticked ≥1 company (and nothing during SSR), so it's
            safe to include in this server layout. */}
        <CompareBar />
      </body>
    </html>
  );
}
