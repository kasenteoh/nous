#!/usr/bin/env node
/**
 * check-function-size — CI gate for the /companies serverless function's
 * traced size (the embedding route: transformers.js + onnxruntime + the
 * bundled ONNX model).
 *
 * WHY: Vercel rejects any function whose uncompressed bundle exceeds 250MB.
 * The E-2 incident froze prod for ~a day because the size blowup was only
 * discovered when DEPLOYS started failing. This script reproduces the
 * function's effective traced content locally — the .nft.json trace, minus
 * next.config's outputFileTracingExcludes, plus its outputFileTracingIncludes
 * — and fails the build when it crosses a budget set well UNDER the platform
 * limit, so a dependency bump that re-adds weight dies in PR CI, not in a
 * frozen prod deploy.
 *
 * The number computed here is an APPROXIMATION of Vercel's accounting (their
 * bundle adds shared runtime chunks and their tracing root differs — see the
 * depth-independent-glob note in next.config.ts). That is fine: the gate's
 * job is drift detection against a locally-reproducible baseline, not
 * replicating Vercel's ledger. Budget rationale:
 *   - measured baseline (2026-07-17, next 16.2.6 + transformers 4.2): ~92MB
 *   - budget 180MB: >90% headroom over baseline noise, comfortably under the
 *     250MB platform limit even after Vercel's overhead is added on top.
 *
 * Run AFTER `next build --webpack` (needs .next/server/app + .nft.json):
 *   node scripts/check-function-size.mjs
 * Exit codes: 0 ok · 1 over budget · 2 missing/unreadable build artifacts.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// The traced entrypoint of the embedder route. If more routes ever join
// next.config's EMBEDDER_ROUTES, list their .nft.json manifests here too.
const NFT_MANIFESTS = [".next/server/app/companies/page.js.nft.json"];

// next.config's outputFileTracingExcludes are ALREADY applied by the tracer —
// the .nft.json is post-exclude output (verified: no darwin/win32/arm64 onnx
// or onnxruntime-web entries appear in it). Deliberately NO re-filtering
// here: a stale local copy of the exclude list would silently mask someone
// removing an exclude from next.config.ts (the function grows ~130MB, the
// gate keeps filtering it out of the measurement — a false pass of the exact
// E-2 class). Measuring the manifest verbatim means such a change shows up
// as a budget breach, which is the point.
//
// The includes below MUST mirror next.config.ts's includeGlobs: those files
// ship in the function but static analysis cannot see them, so they are
// absent from the manifest and have to be walked directly.
const INCLUDE_DIRS = [
  "node_modules/onnxruntime-node/bin/napi-v6/linux/x64",
  "models/Xenova",
];

const BUDGET_BYTES = 180 * 1024 * 1024;
// A correct measurement can never be tiny: the forced-include onnx linux-x64
// binary alone is ~34MB. A total below this means the measurement itself is
// broken (nft path-resolution drift, schema change) — fail loudly rather
// than false-pass with a zeroed total.
const SANITY_FLOOR_BYTES = 30 * 1024 * 1024;
const MB = (n) => (n / (1024 * 1024)).toFixed(1);

async function* walk(dir) {
  let entries;
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch {
    return; // an include dir may legitimately be absent (model bundling skipped)
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (entry.isFile()) yield full;
  }
}

async function main() {
  /** @type {Map<string, number>} absolute path -> size */
  const files = new Map();

  for (const manifest of NFT_MANIFESTS) {
    const manifestPath = path.join(projectRoot, manifest);
    let parsed;
    try {
      parsed = JSON.parse(await fs.readFile(manifestPath, "utf8"));
    } catch (err) {
      console.error(
        `check-function-size: cannot read ${manifest} — run \`next build --webpack\` first (${err.message})`,
      );
      process.exit(2);
    }
    if (!Array.isArray(parsed.files)) {
      console.error(
        `check-function-size: ${manifest} has no "files" array — the .nft.json ` +
          `schema changed; update this script before trusting the gate.`,
      );
      process.exit(2);
    }
    const baseDir = path.dirname(manifestPath);
    for (const rel of parsed.files) {
      files.set(path.resolve(baseDir, rel), 0);
    }
    // The manifest's own entrypoint ships too.
    files.set(manifestPath.replace(/\.nft\.json$/, ""), 0);
  }

  for (const dir of INCLUDE_DIRS) {
    for await (const file of walk(path.join(projectRoot, dir))) {
      files.set(file, 0);
    }
  }

  let total = 0;
  /** @type {Map<string, number>} top-level package/dir -> bytes */
  const byBucket = new Map();
  for (const abs of files.keys()) {
    let size = 0;
    try {
      size = (await fs.stat(abs)).size;
    } catch {
      continue; // symlink target gone etc. — skip, never crash the gate
    }
    files.set(abs, size);
    total += size;
    const relToRoot = path.relative(projectRoot, abs);
    const m = relToRoot.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/);
    const bucket = m ? m[1] : relToRoot.split(path.sep).slice(0, 2).join("/");
    byBucket.set(bucket, (byBucket.get(bucket) ?? 0) + size);
  }

  const top = [...byBucket.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
  console.log(
    `check-function-size: /companies traced ≈ ${MB(total)}MB across ${files.size} files (budget ${MB(BUDGET_BYTES)}MB)`,
  );
  for (const [bucket, bytes] of top) console.log(`  ${MB(bytes).padStart(8)}MB  ${bucket}`);

  if (total < SANITY_FLOOR_BYTES) {
    console.error(
      `\ncheck-function-size: SANITY CHECK FAILED — ${MB(total)}MB is implausibly ` +
        `small (the forced-include onnx binary alone is ~34MB). Stat failures or ` +
        `.nft.json path-resolution drift likely zeroed the measurement; fix the ` +
        `script before trusting the gate.`,
    );
    process.exit(2);
  }

  if (total > BUDGET_BYTES) {
    console.error(
      `\ncheck-function-size: OVER BUDGET — ${MB(total)}MB > ${MB(BUDGET_BYTES)}MB.\n` +
        `A dependency bump likely re-added onnx/model weight. Check the bucket list above,\n` +
        `next.config.ts outputFileTracing globs, and the E-2 postmortem in the worklog\n` +
        `before raising the budget: Vercel hard-fails deploys at 250MB uncompressed.`,
    );
    process.exit(1);
  }
}

await main();
