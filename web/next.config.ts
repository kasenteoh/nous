import type { NextConfig } from "next";

// /companies imports lib/embed-query.ts (transformers.js → onnxruntime-node),
// which pulls a native .node addon that cannot be webpack-bundled, plus
// onnxruntime's ~211MB of per-platform binaries and onnxruntime-web's ~130MB of
// browser wasm. Left untrimmed the function blows Vercel's 250MB uncompressed
// limit (this froze prod after E-2 — see the fable5 worklog).
//
// Two things that make this fragile, both learned the hard way:
// - The build MUST run under webpack (`next build --webpack`). Turbopack bundles
//   the onnx assets into the function and ignores outputFileTracing* entirely,
//   which reintroduces the 415MB blowup.
//
// Platform context (2026-07-17): VERCEL_SUPPORT_LARGE_FUNCTIONS=1 on the
// project opts into Vercel's Large Functions beta — since 2026-06-29 that
// limit is 5GB uncompressed (was 250MB) and NEW projects are auto-enrolled,
// so a re-created project no longer silently freezes deploys. The 250MB
// number below remains the conservative planning bar (the beta's GA terms
// are unstated), and CI enforces a 180MB budget via `npm run check:size`.
// - The glob patterns MUST be depth-independent (`**/…`). Next's file-tracing
//   root is the project dir locally but the REPO root on Vercel, so a
//   root-relative `node_modules/…` matches locally yet misses `web/node_modules/…`
//   on Vercel — the excludes silently no-op and the function is 406MB there while
//   92MB locally. `**/` matches at any depth, so it holds under both roots.
const EMBEDDER_ROUTES = ["/companies"] as const;

// Trace only the linux-x64 onnxruntime binary Vercel actually runs (34MB);
// darwin/win32/linux-arm64 (~177MB) and onnxruntime-web (~130MB, browser-only)
// would otherwise push the function past the limit.
const excludeGlobs = [
  "**/onnxruntime-node/bin/napi-v6/darwin/**",
  "**/onnxruntime-node/bin/napi-v6/win32/**",
  "**/onnxruntime-node/bin/napi-v6/linux/arm64/**",
  "**/onnxruntime-web/**",
];

// Two things static analysis cannot see (spike-verified against .nft.json):
// - the dlopen'd native addon — without the include the deployed function
//   throws "cannot find onnxruntime_binding.node";
// - models/ — the build-time model bundle (scripts/download-model.mjs), read at
//   runtime via env.cacheDir (lib/embed-query.ts). The glob simply matches
//   nothing when bundling was skipped (fail-soft path). Traced function lands
//   ~92MB of Vercel's 250MB uncompressed budget.
const includeGlobs = [
  "**/onnxruntime-node/bin/napi-v6/linux/x64/**",
  "**/models/Xenova/**",
];

const perRoute = (globs: string[]): Record<string, string[]> =>
  Object.fromEntries(EMBEDDER_ROUTES.map((route) => [route, globs]));

const nextConfig: NextConfig = {
  // Keep the native packages external and let output file tracing copy them in.
  serverExternalPackages: ["@huggingface/transformers", "onnxruntime-node"],
  outputFileTracingExcludes: perRoute(excludeGlobs),
  outputFileTracingIncludes: perRoute(includeGlobs),
  async rewrites() {
    return [
      // /c/<slug>.md → the markdown route (llms.txt convention: a page's .md
      // sibling). A dynamic segment can't carry a literal ".md" suffix, so the
      // rewrite maps it onto app/c/[slug]/md/route.ts. The param regex pins
      // slugs to the slugify alphabet so nothing else matches.
      {
        source: "/c/:slug([a-z0-9-]+)\\.md",
        destination: "/c/:slug/md",
      },
    ];
  },
};

export default nextConfig;
