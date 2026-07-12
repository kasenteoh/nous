import type { NextConfig } from "next";

// /companies imports lib/embed-query.ts (transformers.js → onnxruntime-node),
// which pulls a native .node addon that cannot be webpack-bundled, plus
// onnxruntime's ~211MB of per-platform binaries and onnxruntime-web's ~130MB of
// browser wasm. Left untrimmed the function blows Vercel's 250MB uncompressed
// limit (this froze prod after E-2 — see the fable5 worklog). The rules live
// here once so a second embedder route (should one be added) can reuse them.
// NB: outputFileTracing* only shapes PAGE routes reliably (a route handler that
// imports the embedder is NOT trimmed by these keys), and the build MUST run
// under webpack (`next build --webpack`) — Turbopack bundles the onnx assets
// into the function and ignores these rules entirely, reintroducing the blowup.
const EMBEDDER_ROUTES = ["/companies"] as const;

// Trace only the linux-x64 onnxruntime binary Vercel actually runs (34MB);
// darwin/win32/linux-arm64 (~177MB) and onnxruntime-web (~130MB, browser-only)
// would otherwise push the function past the limit.
const excludeGlobs = [
  "node_modules/onnxruntime-node/bin/napi-v6/darwin/**",
  "node_modules/onnxruntime-node/bin/napi-v6/win32/**",
  "node_modules/onnxruntime-node/bin/napi-v6/linux/arm64/**",
  "node_modules/onnxruntime-web/**",
];

// Two things static analysis cannot see (spike-verified against .nft.json):
// - the dlopen'd native addon — without the include the deployed function
//   throws "cannot find onnxruntime_binding.node";
// - models/ — the build-time model bundle (scripts/download-model.mjs), read at
//   runtime via env.cacheDir (lib/embed-query.ts). The glob simply matches
//   nothing when bundling was skipped (fail-soft path). Traced function lands
//   ~92MB of Vercel's 250MB uncompressed budget.
const includeGlobs = [
  "node_modules/onnxruntime-node/bin/napi-v6/linux/x64/**",
  "models/**",
];

const perRoute = (globs: string[]): Record<string, string[]> =>
  Object.fromEntries(EMBEDDER_ROUTES.map((route) => [route, globs]));

const nextConfig: NextConfig = {
  // Keep the native packages external and let output file tracing copy them in.
  serverExternalPackages: ["@huggingface/transformers", "onnxruntime-node"],
  outputFileTracingExcludes: perRoute(excludeGlobs),
  outputFileTracingIncludes: perRoute(includeGlobs),
};

export default nextConfig;
