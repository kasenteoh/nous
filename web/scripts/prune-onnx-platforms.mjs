// Build-time trim of onnxruntime-node's unused platform binaries (E-2 deploy
// fix). @huggingface/transformers pulls onnxruntime-node, whose
// bin/napi-v6/{darwin,win32,linux} tree ships a native runtime for EVERY
// platform (~211MB total: win32 124MB, darwin 35MB, linux/x64 34MB,
// linux/arm64 18MB). Only the binary matching the running platform is ever
// dlopen'd, so the rest is dead weight in the serverless function.
//
// next.config.ts already tries to exclude the unused platforms via
// outputFileTracingExcludes, and that works locally — but on Vercel those
// globs do not take effect (Vercel resolves the file-tracing root to the repo
// root, not web/, so the root-relative patterns miss), and the ENTIRE package
// lands in the /companies function. Combined with onnxruntime-web, that pushed
// the function to 415MB — over Vercel's 250MB uncompressed limit — so every
// deploy from E-2 (#155) onward failed to build and prod froze at the last
// pre-E-2 commit.
//
// Physically deleting the unused binaries before `next build` is invariant to
// how tracing resolves: bytes that aren't on disk can't be traced. Runs in
// `prebuild` AFTER download-model.mjs (whose probe needs the current-platform
// binary). Safe by construction: it only ever removes platform/arch dirs that
// are neither the Vercel production target (linux/x64) nor the machine running
// the build, so local dev keeps its own binary and prod keeps linux/x64.
//
// onnxruntime-web (~130MB, browser/wasm backend) is a separate offender but is
// referenced by the transformers node entry, so it is left to the config-level
// exclude rather than deleted here.

import { rmSync, readdirSync, existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const napiRoot = path.join(
  webRoot,
  "node_modules",
  "onnxruntime-node",
  "bin",
  "napi-v6",
);

// Only prune in automated builds (Vercel deploy + CI). A local interactive
// `npm run build` leaves the developer's node_modules untouched — CI validates
// the pruned build before Vercel, and Vercel is where the size limit bites.
if (!process.env.VERCEL && !process.env.CI) {
  console.log("[prune-onnx] not a CI/Vercel build; leaving node_modules intact.");
  process.exit(0);
}

if (!existsSync(napiRoot)) {
  console.log(`[prune-onnx] ${napiRoot} not found; nothing to prune.`);
  process.exit(0);
}

// Keep the production target (Vercel is linux/x64) and whatever platform is
// building right now (so local dev keeps a working binary).
const keep = new Set(["linux/x64", `${process.platform}/${process.arch}`]);

function dirBytes(dir) {
  let total = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    total += entry.isDirectory() ? dirBytes(p) : statSync(p).size;
  }
  return total;
}

let reclaimed = 0;
const removed = [];
for (const platform of readdirSync(napiRoot)) {
  const platformDir = path.join(napiRoot, platform);
  if (!statSync(platformDir).isDirectory()) continue;
  for (const arch of readdirSync(platformDir)) {
    const archDir = path.join(platformDir, arch);
    if (!statSync(archDir).isDirectory()) continue;
    if (keep.has(`${platform}/${arch}`)) continue;
    reclaimed += dirBytes(archDir);
    rmSync(archDir, { recursive: true, force: true });
    removed.push(`${platform}/${arch}`);
  }
}

console.log(
  `[prune-onnx] kept {${[...keep].join(", ")}}; removed ` +
    `[${removed.join(", ") || "none"}]; reclaimed ` +
    `${(reclaimed / 1e6).toFixed(1)}MB from ${napiRoot}`,
);

// onnxruntime-web (~130MB) is the browser/wasm backend. transformers' node
// entry references it (so the package can't be deleted), but on Node the
// onnxruntime-node backend is used and the web WASM binaries are never
// instantiated. Its dist/ is dominated by 4 browser .wasm files (~73MB) plus
// source maps — none of which are read at runtime on the server. Stripping
// them keeps the JS entry importable while removing the bulk that would
// otherwise push the /companies function back over the limit.
const webDist = path.join(
  webRoot,
  "node_modules",
  "onnxruntime-web",
  "dist",
);
if (existsSync(webDist)) {
  let webReclaimed = 0;
  const webRemoved = [];
  for (const entry of readdirSync(webDist)) {
    if (!/\.(wasm|map)$/.test(entry)) continue;
    const p = path.join(webDist, entry);
    webReclaimed += statSync(p).size;
    rmSync(p, { force: true });
    webRemoved.push(entry);
  }
  console.log(
    `[prune-onnx] onnxruntime-web/dist: removed ${webRemoved.length} ` +
      `wasm/map file(s); reclaimed ${(webReclaimed / 1e6).toFixed(1)}MB`,
  );
}
