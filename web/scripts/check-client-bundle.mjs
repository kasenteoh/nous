// Client-bundle safety gate: fail if anything that ships to the browser could
// expose the Supabase service-role key or the Postgres connection string.
//
// Two layers of detection over every client-visible build artifact:
//   1. Identifier scan — the literal strings `SUPABASE_SERVICE_ROLE_KEY`,
//      `SUPABASE_URL`, or `DATABASE_URL` in a client chunk mean server-only
//      code (lib/db.ts / lib/queries.ts) was bundled for the browser.
//   2. Canary-value scan — CI builds with fake values planted in those env
//      vars; if a planted value shows up in a client-visible artifact, the
//      build inlined or serialized a server secret (e.g. a NEXT_PUBLIC_
//      rename, or a secret passed as a prop into a client component and
//      serialized into the RSC payload). The scan reads the values from the
//      running environment, so it works with real values locally too.
//
// Client-visible means: `.next/static/**` (chunks, CSS, media) plus the
// prerendered payloads under `.next/server/app/**` with extensions .html,
// .rsc and .body — those files are served to browsers verbatim. Everything
// else under `.next/server` is server-side and MAY legitimately contain the
// identifiers (that's where db.ts is supposed to live).
//
// Usage: npm run check:bundle   (requires a completed `next build`)

import { readdirSync, readFileSync, existsSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import process from "node:process";

const webRoot = new URL("..", import.meta.url).pathname;
const nextDir = join(webRoot, ".next");

if (!existsSync(nextDir)) {
  console.error("check-client-bundle: no .next directory — run `npm run build` first.");
  process.exit(2);
}

/** Recursively collect files under dir, filtered by predicate on the path. */
function collect(dir, predicate, out = []) {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) collect(full, predicate, out);
    else if (predicate(full)) out.push(full);
  }
  return out;
}

const clientVisible = [
  // Everything under .next/static ships to the browser.
  ...collect(join(nextDir, "static"), () => true),
  // Prerendered route payloads are served verbatim: HTML documents, RSC
  // flight payloads, and route-handler bodies (sitemap.xml, robots.txt).
  ...collect(join(nextDir, "server", "app"), (p) =>
    /\.(html|rsc|body)$/.test(p),
  ),
];

if (clientVisible.length === 0) {
  console.error("check-client-bundle: found no client-visible artifacts — unexpected build layout?");
  process.exit(2);
}

// Layer 1: identifiers that mark server-only code. Present in a client-visible
// artifact ⇒ the server/client boundary was breached.
const forbiddenIdentifiers = [
  "SUPABASE_SERVICE_ROLE_KEY",
  "SUPABASE_URL",
  "DATABASE_URL",
];

// Layer 2: live secret/canary values from the environment. Skip unset/short
// values (a short placeholder would false-positive on unrelated text).
const forbiddenValues = [
  ["SUPABASE_SERVICE_ROLE_KEY value", process.env.SUPABASE_SERVICE_ROLE_KEY],
  ["SUPABASE_URL value", process.env.SUPABASE_URL],
  ["DATABASE_URL value", process.env.DATABASE_URL],
].filter(([, v]) => typeof v === "string" && v.length >= 12);

const failures = [];
for (const file of clientVisible) {
  const content = readFileSync(file, "utf8");
  for (const ident of forbiddenIdentifiers) {
    if (content.includes(ident)) {
      failures.push({ file, match: ident, kind: "identifier" });
    }
  }
  for (const [label, value] of forbiddenValues) {
    if (content.includes(value)) {
      failures.push({ file, match: label, kind: "value" });
    }
  }
}

if (failures.length > 0) {
  console.error("check-client-bundle: FAIL — server secrets reachable from the browser:\n");
  for (const { file, match, kind } of failures) {
    console.error(`  [${kind}] ${match}\n    in ${relative(webRoot, file)}`);
  }
  console.error(
    "\nA server-only identifier or secret value is present in a client-visible " +
      "artifact. Check that lib/db.ts / lib/queries.ts are only imported from " +
      "server code and that no secret is passed into a client component.",
  );
  process.exit(1);
}

console.log(
  `check-client-bundle: OK — scanned ${clientVisible.length} client-visible artifacts, ` +
    `${forbiddenIdentifiers.length} identifiers + ${forbiddenValues.length} env values, no leaks.`,
);
