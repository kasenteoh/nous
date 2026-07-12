import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // E-2 spike: transformers.js loads onnxruntime-node (native .node addon)
  // at runtime — it cannot be webpack-bundled, so keep it external and let
  // output file tracing copy it into the serverless function.
  serverExternalPackages: ["@huggingface/transformers", "onnxruntime-node"],
  // Trace only the linux-x64 onnxruntime binary Vercel actually runs
  // (34MB); darwin/win32/linux-arm64 would otherwise push the function
  // toward the 250MB uncompressed limit (onnxruntime-node ships ~211MB of
  // per-platform binaries).
  outputFileTracingExcludes: {
    "/api/spike-embed": [
      "node_modules/onnxruntime-node/bin/napi-v6/darwin/**",
      "node_modules/onnxruntime-node/bin/napi-v6/win32/**",
      "node_modules/onnxruntime-node/bin/napi-v6/linux/arm64/**",
      "node_modules/onnxruntime-web/**",
    ],
  },
  // Static analysis does NOT find the dlopen'd native addon (verified: the
  // .nft.json lists only onnxruntime-node/dist/*.js) — without this include
  // the deployed function throws "cannot find onnxruntime_binding.node".
  outputFileTracingIncludes: {
    "/api/spike-embed": [
      "node_modules/onnxruntime-node/bin/napi-v6/linux/x64/**",
    ],
  },
};

export default nextConfig;
