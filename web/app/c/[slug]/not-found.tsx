import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center px-6 py-24">
      <h1 className="text-3xl font-semibold tracking-tight text-ink">
        Company not found
      </h1>
      <p className="mt-4 max-w-md text-center text-ink-muted">
        We don&apos;t have a record matching this slug. It may have been removed
        or never existed.
      </p>
      <Link
        href="/companies"
        className="mt-8 text-sm font-medium text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
      >
        Back to the index
      </Link>
    </main>
  );
}
