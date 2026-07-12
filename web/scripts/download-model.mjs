// Build-time model bundling for semantic search (E-2). Runs as `prebuild`.
//
// Downloads the pinned query-embedding model (see lib/embed-model.json) into
// models/ — the directory lib/embed-query.ts points transformers.js at and
// next.config.ts traces into the /companies serverless function. Bundling at
// build time (instead of downloading from the HF hub on first cold start)
// makes the cold-start cost deterministic: ~0.2s model load from the traced
// filesystem versus ~1.5s (and a network dependency) for a hub fetch. The
// spike measured the traced function at ~58MB + ~34MB for this model — far
// under Vercel's 250MB uncompressed budget.
//
// Population strategy: rather than hand-listing model files (and silently
// breaking when a transformers.js upgrade changes what it loads), this runs
// the real pipeline once with env.cacheDir pointed at models/ — the library
// itself downloads exactly the files the runtime will need, into exactly the
// cache layout the runtime will read (revision-keyed:
// models/<modelId>/<revision>/{config.json,tokenizer.json,...,onnx/model_quantized.onnx}).
// A probe embedding validates the bundle end to end (dims must match).
//
// FAIL-SOFT by design: any failure (no network, HF outage) warns and exits 0
// so the build proceeds. lib/embed-query.ts then falls back to a /tmp hub
// download at runtime, and if that also fails it returns null and /companies
// degrades to pure lexical search — which is exactly the path secret-free CI
// exercises. A missing model must never block a deploy.
//
// Idempotent: transformers.js checks the cache first, so a re-run with the
// bundle already present touches no network and finishes in <1s.

import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pin = JSON.parse(
  readFileSync(path.join(webRoot, "lib", "embed-model.json"), "utf8"),
);
const modelDir = path.join(webRoot, "models");

try {
  const { pipeline, env } = await import("@huggingface/transformers");
  env.cacheDir = modelDir;

  const t0 = Date.now();
  const extractor = await pipeline("feature-extraction", pin.modelId, {
    dtype: pin.dtype,
    revision: pin.revision,
  });
  const output = await extractor("bundle probe", {
    pooling: "cls",
    normalize: true,
  });
  if (output.data.length !== pin.dims) {
    throw new Error(
      `probe embedding has ${output.data.length} dims, expected ${pin.dims}`,
    );
  }
  console.log(
    `[download-model] bundled ${pin.modelId}@${pin.revision.slice(0, 8)} ` +
      `(${pin.dtype}) into models/ in ${Date.now() - t0}ms; probe ok (${pin.dims} dims)`,
  );
} catch (err) {
  console.warn(
    `[download-model] bundling failed (${err instanceof Error ? err.message : err}); ` +
      "continuing — semantic search will fall back to a runtime hub download " +
      "or degrade to lexical search (see lib/embed-query.ts).",
  );
}
