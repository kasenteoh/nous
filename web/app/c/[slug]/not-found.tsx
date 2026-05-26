import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center px-6 py-24">
      <h1 className="text-3xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
        Company not found
      </h1>
      <p className="mt-4 max-w-md text-center text-zinc-500 dark:text-zinc-400">
        We don&apos;t have a record matching this slug. It may have been removed
        or never existed.
      </p>
      <Link
        href="/"
        className="mt-8 text-sm font-medium text-zinc-700 underline underline-offset-4 hover:text-zinc-900 dark:text-zinc-300 dark:hover:text-zinc-100"
      >
        Back to the index
      </Link>
    </main>
  );
}
