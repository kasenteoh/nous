// Semantic-search embedder health probe (E-2 observability). The query-side
// embedder (lib/embed-query.ts) is fail-soft by design: on ANY failure it
// returns null and /companies silently degrades to pure lexical search. That
// safety is also a blind spot — a deployment whose serverless function is
// missing the bundled model or the onnxruntime native binary looks identical
// to a healthy one from the outside (search still returns lexical results, no
// error). This endpoint makes that failure observable: it reports, from INSIDE
// the running function, whether the two traced artifacts are present and
// whether a real embedding actually succeeds.
//
// Intended use: after any web deploy, curl /api/health/embed and confirm
// `ok: true`. `ok: false` with `nativeBinaryPresent: false` or
// `bundledModelPresent: false` means output-file-tracing (next.config.ts) did
// not land the artifact in this function; `ok: false` with both present and a
// large `elapsedMs` points at a runtime load/timeout instead. Response carries
// no secrets — only booleans, the embedding dimension, timing, and the deploy
// identity (commit/region) needed to tell which deployment answered.

import { existsSync } from "node:fs";
import path from "node:path";

import { embedQuery, EMBEDDING_DIMS } from "@/lib/embed-query";
import modelPin from "@/lib/embed-model.json";

// Always probe the live function, never a cached/prerendered snapshot.
export const dynamic = "force-dynamic";

// The bundled ONNX weights, in transformers.js's revision-keyed cache layout —
// the exact path lib/embed-query.ts checks to decide bundled-vs-runtime load.
const BUNDLED_ONNX = path.join(
  process.cwd(),
  "models",
  modelPin.modelId,
  modelPin.revision,
  "onnx",
  "model_quantized.onnx",
);

// The onnxruntime-node native addon transformers.js dlopens at inference time.
// Missing from the traced function ⇒ session creation throws immediately
// ("cannot find onnxruntime_binding.node") ⇒ embedQuery fast-returns null.
// The platform/arch segment matches whatever runtime answers (linux/x64 on
// Vercel; the local platform in dev).
const NATIVE_BINDING = path.join(
  process.cwd(),
  "node_modules",
  "onnxruntime-node",
  "bin",
  "napi-v6",
  process.platform,
  process.arch,
  "onnxruntime_binding.node",
);

export async function GET() {
  const startedAt = Date.now();
  const vector = await embedQuery("logistics automation platform");
  const elapsedMs = Date.now() - startedAt;

  const dims = vector?.length ?? null;
  const ok = dims === EMBEDDING_DIMS;

  return Response.json(
    {
      ok,
      dims,
      expectedDims: EMBEDDING_DIMS,
      bundledModelPresent: existsSync(BUNDLED_ONNX),
      nativeBinaryPresent: existsSync(NATIVE_BINDING),
      elapsedMs,
      platform: `${process.platform}/${process.arch}`,
      commit: process.env.VERCEL_GIT_COMMIT_SHA ?? null,
      region: process.env.VERCEL_REGION ?? null,
    },
    {
      status: ok ? 200 : 503,
      // Never let a CDN cache a health verdict.
      headers: { "cache-control": "no-store" },
    },
  );
}
