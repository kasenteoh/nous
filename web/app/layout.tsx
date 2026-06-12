import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { ThemeToggle } from "@/components/ThemeToggle";
import { SITE_NAME, siteOrigin } from "@/lib/site";
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
                  className="w-36 rounded-md border border-edge bg-transparent px-2.5 py-1 text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:border-ink-muted focus:w-44 transition-all"
                />
              </form>
              <Link
                href="/companies"
                aria-label="Search companies"
                className="md:hidden text-ink-muted hover:text-ink transition-colors"
              >
                ⌕
              </Link>

              <nav>
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

        {children}
      </body>
    </html>
  );
}
