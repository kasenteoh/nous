// Server-side query embedding for semantic search (E-2). This module loads a
// native ONNX runtime and (possibly) a 34MB model — it must never reach the
// client bundle. `server-only` makes that a build-time guarantee; the model
// weights additionally stay out of client chunks by construction because
// next.config.ts keeps @huggingface/transformers server-external and only
// output-file-tracing (a server mechanism) ever touches models/.
//
// Model contract (spike-verified, 2026-07-11 — do not re-derive):
// - Same weights as the pipeline's fastembed BAAI/bge-small-en-v1.5
//   (migration 0033) converted to ONNX; pinned by HF revision sha in
//   lib/embed-model.json so the query space can never silently drift from
//   the stored document vectors.
// - pooling: "cls", normalize: true — fastembed CLS-pools this model;
//   parity measured at cosine 0.9974 (q8 quantization noise only).
//   `pooling: "mean"` produces a DIFFERENT embedding space (~0.96 cosine,
//   broken rankings) — never change it.
// - Queries are embedded as raw text with no instruction prefix: fastembed
//   embeds documents and queries identically, and documents are embedded as
//   `name\nshort\nlong` plain text.
// - transformers.js v4 on Node runs onnxruntime-node natively (the v3-era
//   `device: "wasm"` option no longer exists); warm inference is 2–3ms.

import "server-only";

import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  env,
  pipeline,
  type FeatureExtractionPipeline,
} from "@huggingface/transformers";

import modelPin from "./embed-model.json";

const MODEL_ID: string = modelPin.modelId;
const MODEL_REVISION: string = modelPin.revision;
export const EMBEDDING_DIMS: number = modelPin.dims;
// The JSON pin can't carry the literal type; the runtime value is validated
// against the file names below (q8 ⇔ model_quantized.onnx).
const MODEL_DTYPE = modelPin.dtype as "q8";

/**
 * Hard cap on end-to-end query-embedding time. A search request must never
 * hang on the model: past this, embedQuery resolves null and /companies
 * serves pure lexical results. 4s comfortably covers the worst supported
 * cold path (runtime hub download of the ~34MB model on a warmish
 * connection) while still bounding a hung download well below typical
 * function timeouts.
 */
export const QUERY_EMBED_TIMEOUT_MS = 4000;

// ── Model location ───────────────────────────────────────────────────────────
// Build-vs-runtime download decision (E-2): PREFER the build-time bundle.
// scripts/download-model.mjs (prebuild) populates models/ in transformers.js's
// own cache layout, and next.config.ts traces it into the /companies function,
// giving a deterministic ~0.2s cold start with zero runtime network
// dependency. When the bundle is absent (bundling is fail-soft: secret-free
// CI without network, local dev before the first build), fall back to a
// hub download cached in /tmp — the only writable path on Vercel — bounded by
// QUERY_EMBED_TIMEOUT_MS and degrading to null (= lexical search) on failure.
//
// q8 ⇔ model_quantized.onnx is transformers.js's dtype→filename mapping; the
// probe in download-model.mjs exercises the real resolution, this existence
// check only decides which cache directory to point the library at.
const BUNDLED_MODEL_DIR = path.join(process.cwd(), "models");
const BUNDLED_ONNX = path.join(
  BUNDLED_MODEL_DIR,
  MODEL_ID,
  MODEL_REVISION,
  "onnx",
  "model_quantized.onnx",
);
env.cacheDir = existsSync(BUNDLED_ONNX)
  ? BUNDLED_MODEL_DIR
  : path.join(os.tmpdir(), "nous-model-cache");

// ── Singleton pipeline ───────────────────────────────────────────────────────
// Cached at module level so a warm serverless instance (Vercel Fluid Compute)
// reuses the loaded model across invocations — only cold starts pay the load.
let extractorPromise: Promise<FeatureExtractionPipeline> | null = null;

function getExtractor(): Promise<FeatureExtractionPipeline> {
  if (extractorPromise === null) {
    const created = pipeline("feature-extraction", MODEL_ID, {
      dtype: MODEL_DTYPE,
      revision: MODEL_REVISION,
    });
    extractorPromise = created;
    // A rejected load (network down, corrupt cache) must not wedge the
    // instance forever: clear the cached promise so the next request retries.
    created.catch(() => {
      if (extractorPromise === created) extractorPromise = null;
    });
  }
  return extractorPromise;
}

async function embed(text: string): Promise<number[]> {
  const extractor = await getExtractor();
  const output = await extractor(text, { pooling: "cls", normalize: true });
  const vector = Array.from(output.data as Float32Array);
  if (vector.length !== EMBEDDING_DIMS) {
    throw new Error(
      `expected ${EMBEDDING_DIMS}-dim embedding, got ${vector.length}`,
    );
  }
  return vector;
}

// Unique sentinel so a timeout is distinguishable from any embed result.
const TIMED_OUT: unique symbol = Symbol("embed-query-timeout");

/**
 * Embed a search query into the 384-dim space of companies.embedding, or
 * null when embedding is unavailable for ANY reason (empty query, model
 * load/download failure, timeout, dimension mismatch). Callers treat null as
 * "no semantic results" and serve lexical search — a broken model must never
 * break /companies. Never throws.
 *
 * The timeout only abandons the wait: a still-loading model keeps loading in
 * the background (the singleton is preserved), so a query that times out
 * during a cold start typically leaves the NEXT query with a warm ~3ms path.
 */
export async function embedQuery(query: string): Promise<number[] | null> {
  const text = query.trim();
  if (!text) return null;

  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<typeof TIMED_OUT>((resolve) => {
      timer = setTimeout(() => resolve(TIMED_OUT), QUERY_EMBED_TIMEOUT_MS);
    });
    const result = await Promise.race([embed(text), timeout]);
    if (result === TIMED_OUT) {
      console.warn(
        `[embedQuery] timed out after ${QUERY_EMBED_TIMEOUT_MS}ms; ` +
          "degrading to lexical search",
      );
      return null;
    }
    return result;
  } catch (err) {
    console.warn(
      "[embedQuery] failed; degrading to lexical search:",
      err instanceof Error ? err.message : err,
    );
    return null;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}
