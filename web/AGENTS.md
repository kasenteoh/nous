<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Server-only boundary

`lib/db.ts` and `lib/queries.ts` hold the Supabase **service-role** path — the
key bypasses RLS and must never reach the browser. The boundary is enforced,
not aspirational:

- Both files `import "server-only"`: adding them to any `"use client"` module
  graph fails `next build` outright. New modules that touch
  `createSupabaseServerClient()` or `process.env.SUPABASE_*` must also import
  `server-only`.
- CI additionally builds with planted canary values in
  `SUPABASE_SERVICE_ROLE_KEY` / `DATABASE_URL` and scans every client-visible
  artifact (`.next/static/**` plus prerendered `.html`/`.rsc`/`.body` payloads)
  for the canary values and for the env-var identifier names
  (`npm run check:bundle`, see `scripts/check-client-bundle.mjs`).

Rules of thumb: never pass a secret (or anything derived from one) as a prop
into a client component; never rename a secret to `NEXT_PUBLIC_*`; new secrets
must be added to the identifier list in `scripts/check-client-bundle.mjs`.
