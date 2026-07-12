// Build-time trim of onnxruntime bloat that would otherwise blow Vercel's
// 250MB function limit (E-2 deploy fix). This is the load-bearing size lever on
// Vercel, because Vercel's builder copies the whole serverExternalPackages dirs
// into the function and IGNORES next.config's outputFileTracingExcludes (those
// only take effect in a local webpack build). Deleting the unused bytes from
// node_modules before `next build` is the one thing Vercel can't ignore — bytes
// that aren't on disk can't be copied.
//
// Removes, on CI/Vercel builds only (local dev keeps its node_modules intact):
// - onnxruntime-node's per-platform binaries except the one the runtime
//   dlopens (linux/x64 on Vercel; ~177MB of win32/darwin/linux-arm64 dropped);
// - onnxruntime-web's browser .wasm backends and source maps (~98MB) — the node
//   runtime uses onnxruntime-node and never instantiates the web WASM, but the
//   transformers node entry references the package so it can't be deleted whole.
//
// Runs in `prebuild` after download-model.mjs (whose probe needs the current
// platform's binary). Safe by construction: only ever removes platform/arch
// dirs that are neither the Vercel prod target (linux/x64) nor the building
// machine's own platform. Requires the webpack build (`next build --webpack`);
// under Turbopack the function is assembled from bundled assets, not
// node_modules, so this prune has no effect there.

import { rmSync, readdirSync, existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// Only prune in automated builds (Vercel deploy + CI). A local interactive
// `npm run build` leaves the developer's node_modules untouched.
if (!process.env.VERCEL && !process.env.CI) {
  console.log("[prune-onnx] not a CI/Vercel build; leaving node_modules intact.");
  process.exit(0);
}

function dirBytes(dir) {
  let total = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    total += entry.isDirectory() ? dirBytes(p) : statSync(p).size;
  }
  return total;
}

// ── onnxruntime-node: keep only the running platform + the Vercel prod target ──
const napiRoot = path.join(
  webRoot,
  "node_modules",
  "onnxruntime-node",
  "bin",
  "napi-v6",
);
if (existsSync(napiRoot)) {
  const keep = new Set(["linux/x64", `${process.platform}/${process.arch}`]);
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
    `[prune-onnx] onnxruntime-node: kept {${[...keep].join(", ")}}; removed ` +
      `[${removed.join(", ") || "none"}]; reclaimed ${(reclaimed / 1e6).toFixed(1)}MB`,
  );
} else {
  console.log(`[prune-onnx] ${napiRoot} not found; skipping node prune.`);
}

// ── onnxruntime-web: strip the browser wasm/maps (never loaded on the server) ──
const webDist = path.join(webRoot, "node_modules", "onnxruntime-web", "dist");
if (existsSync(webDist)) {
  let reclaimed = 0;
  let count = 0;
  for (const entry of readdirSync(webDist)) {
    if (!/\.(wasm|map)$/.test(entry)) continue;
    const p = path.join(webDist, entry);
    reclaimed += statSync(p).size;
    rmSync(p, { force: true });
    count += 1;
  }
  console.log(
    `[prune-onnx] onnxruntime-web/dist: removed ${count} wasm/map file(s); ` +
      `reclaimed ${(reclaimed / 1e6).toFixed(1)}MB`,
  );
}
