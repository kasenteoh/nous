/**
 * E-2 FEASIBILITY SPIKE — NOT PRODUCTION. Do not link from UI.
 *
 * Proves that query-time embedding with the SAME model family the pipeline
 * uses (bge-small-en-v1.5, 384-dim) runs inside a Next 16 route handler on
 * Vercel. Embeds a hardcoded query and reports timings; returns no DB data.
 *
 * Evidence this encodes (see the E-2 spike report / worklog):
 * - fastembed (pipeline, migration 0033) pools bge-small with the CLS token
 *   and normalizes; `pooling: "cls", normalize: true` below reproduces its
 *   vectors at cosine ~0.9974 (q8 quantization noise only). `pooling: "mean"`
 *   would be a DIFFERENT space (~0.96) — never change it.
 * - fastembed embeds queries and documents identically (no bge instruction
 *   prefix), so the raw query text is embedded as-is.
 * - The extractor promise is cached at module level: on Vercel Fluid Compute
 *   the warm instance reuses the loaded model (~2-5ms/inference measured
 *   locally); only cold starts pay model load (~0.1s from disk cache, ~1.5s
 *   when the ~34MB q8 ONNX must be fetched from the HF hub).
 */
import { NextResponse } from "next/server";
import {
  pipeline,
  type FeatureExtractionPipeline,
} from "@huggingface/transformers";

// Same weights as pipeline's BAAI/bge-small-en-v1.5, converted to ONNX for
// transformers.js; dtype q8 selects the ~34MB int8-quantized variant.
const MODEL = "Xenova/bge-small-en-v1.5";
const SPIKE_QUERY = "startups doing AI for logistics";

let extractorPromise: Promise<FeatureExtractionPipeline> | null = null;

function getExtractor(): Promise<FeatureExtractionPipeline> {
  extractorPromise ??= pipeline("feature-extraction", MODEL, { dtype: "q8" });
  return extractorPromise;
}

export async function GET(): Promise<NextResponse> {
  const t0 = performance.now();
  const extractor = await getExtractor();
  const t1 = performance.now();
  const output = await extractor(SPIKE_QUERY, {
    pooling: "cls",
    normalize: true,
  });
  const t2 = performance.now();
  const vector = Array.from(output.data as Float32Array);
  return NextResponse.json({
    spike: "E-2 query-time embedding probe",
    query: SPIKE_QUERY,
    dims: vector.length,
    modelLoadMs: Math.round(t1 - t0),
    inferenceMs: Math.round(t2 - t1),
    vectorHead: vector.slice(0, 8),
  });
}
