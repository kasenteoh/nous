import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Semantic search (E-2): lib/embed-query.ts (imported by the /companies
  // page) runs transformers.js, which loads onnxruntime-node — a native
  // .node addon that cannot be webpack-bundled. Keep the packages external
  // and let output file tracing copy them into the serverless function.
  serverExternalPackages: ["@huggingface/transformers", "onnxruntime-node"],
  // Trace only the linux-x64 onnxruntime binary Vercel actually runs
  // (34MB); darwin/win32/linux-arm64 would otherwise push the function
  // toward the 250MB uncompressed limit (onnxruntime-node ships ~211MB of
  // per-platform binaries).
  outputFileTracingExcludes: {
    "/companies": [
      "node_modules/onnxruntime-node/bin/napi-v6/darwin/**",
      "node_modules/onnxruntime-node/bin/napi-v6/win32/**",
      "node_modules/onnxruntime-node/bin/napi-v6/linux/arm64/**",
      "node_modules/onnxruntime-web/**",
    ],
  },
  // Two things static analysis cannot see (spike-verified against .nft.json):
  // - the dlopen'd native addon — without the include the deployed function
  //   throws "cannot find onnxruntime_binding.node";
  // - models/ — the build-time model bundle (scripts/download-model.mjs),
  //   read at runtime via env.cacheDir (lib/embed-query.ts). The glob simply
  //   matches nothing when bundling was skipped (fail-soft path). Traced
  //   function lands ~58–92MB of Vercel's 250MB uncompressed budget.
  outputFileTracingIncludes: {
    "/companies": [
      "node_modules/onnxruntime-node/bin/napi-v6/linux/x64/**",
      "models/**",
    ],
  },
};

export default nextConfig;
