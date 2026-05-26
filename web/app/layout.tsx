import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
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
  title: "nous",
  description: "US software startup discovery, indexed from SEC filings.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-white text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
        {/* ── Site header ─────────────────────────────────────────────── */}
        <header className="sticky top-0 z-10 border-b border-zinc-200 dark:border-zinc-800 bg-white/90 dark:bg-zinc-950/90 backdrop-blur-sm">
          <div className="max-w-6xl mx-auto px-6 h-14 flex items-center justify-between">
            {/* Wordmark */}
            <Link
              href="/"
              className="text-lg font-semibold tracking-tight text-zinc-900 dark:text-zinc-100 hover:opacity-80 transition-opacity"
            >
              nous
            </Link>

            {/* Nav links */}
            <nav>
              <ul className="flex items-center gap-6 text-sm text-zinc-500 dark:text-zinc-400">
                <li>
                  {/* /about lands in M5 per spec §7.1 */}
                  <Link
                    href="/about"
                    className="hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors"
                  >
                    About
                  </Link>
                </li>
              </ul>
            </nav>
          </div>
        </header>

        {children}
      </body>
    </html>
  );
}
