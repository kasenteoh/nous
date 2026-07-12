// Unit tests for lib/embed-query.ts with the transformers.js pipeline mocked:
// the singleton, the spike-pinned inference options (cls/normalize), and the
// degrade-to-null contract (empty query, load failure, extractor failure,
// dimension mismatch, timeout). The module keeps state (the cached extractor
// promise and env setup), so every test re-imports it after vi.resetModules().

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  pipelineMock: vi.fn(),
}));

vi.mock("@huggingface/transformers", () => ({
  env: {},
  pipeline: h.pipelineMock,
}));

type EmbedQueryModule = typeof import("@/lib/embed-query");

async function freshModule(): Promise<EmbedQueryModule> {
  vi.resetModules();
  return import("@/lib/embed-query");
}

/** A fake FeatureExtractionPipeline returning a `dims`-dim vector. */
function fakeExtractor(dims: number) {
  return vi.fn(async () => ({ data: new Float32Array(dims).fill(0.5) }));
}

beforeEach(() => {
  h.pipelineMock.mockReset();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("embedQuery", () => {
  it("embeds with the spike-pinned options and returns a 384-dim vector", async () => {
    const extractor = fakeExtractor(384);
    h.pipelineMock.mockResolvedValue(extractor);
    const { embedQuery, EMBEDDING_DIMS } = await freshModule();

    const vector = await embedQuery("  startups doing AI for logistics  ");

    expect(vector).toHaveLength(EMBEDDING_DIMS);
    expect(vector?.every((x) => x === 0.5)).toBe(true);
    // Raw trimmed query, no instruction prefix; CLS pooling + normalize are
    // the parity-critical options (mean pooling breaks the space).
    expect(extractor).toHaveBeenCalledWith("startups doing AI for logistics", {
      pooling: "cls",
      normalize: true,
    });
    // Model pin: id + q8 + revision sha are all passed through.
    expect(h.pipelineMock).toHaveBeenCalledWith(
      "feature-extraction",
      "Xenova/bge-small-en-v1.5",
      expect.objectContaining({
        dtype: "q8",
        revision: expect.stringMatching(/^[0-9a-f]{40}$/),
      }),
    );
  });

  it("loads the pipeline once and reuses it across calls (singleton)", async () => {
    h.pipelineMock.mockResolvedValue(fakeExtractor(384));
    const { embedQuery } = await freshModule();

    await embedQuery("first");
    await embedQuery("second");

    expect(h.pipelineMock).toHaveBeenCalledTimes(1);
  });

  it("returns null for empty/whitespace queries without touching the model", async () => {
    const { embedQuery } = await freshModule();

    expect(await embedQuery("")).toBeNull();
    expect(await embedQuery("   ")).toBeNull();
    expect(h.pipelineMock).not.toHaveBeenCalled();
  });

  it("returns null when the pipeline fails to load, then retries next call", async () => {
    h.pipelineMock
      .mockRejectedValueOnce(new Error("hub unreachable"))
      .mockResolvedValueOnce(fakeExtractor(384));
    const { embedQuery } = await freshModule();

    // Failure degrades to null (lexical search)…
    expect(await embedQuery("query one")).toBeNull();
    // …but must not wedge the warm instance: the rejected promise is dropped
    // and the next request loads the model again.
    expect(await embedQuery("query two")).toHaveLength(384);
    expect(h.pipelineMock).toHaveBeenCalledTimes(2);
  });

  it("returns null when inference itself throws", async () => {
    const extractor = vi.fn(async () => {
      throw new Error("onnx session crashed");
    });
    h.pipelineMock.mockResolvedValue(extractor);
    const { embedQuery } = await freshModule();

    expect(await embedQuery("query")).toBeNull();
  });

  it("returns null on a dimension mismatch", async () => {
    h.pipelineMock.mockResolvedValue(fakeExtractor(10));
    const { embedQuery } = await freshModule();

    expect(await embedQuery("query")).toBeNull();
  });

  it("returns null when embedding exceeds the hard timeout", async () => {
    // A model load that never settles — e.g. a hung hub download.
    h.pipelineMock.mockReturnValue(new Promise(() => {}));
    vi.useFakeTimers();
    const { embedQuery, QUERY_EMBED_TIMEOUT_MS } = await freshModule();

    const pending = embedQuery("query");
    await vi.advanceTimersByTimeAsync(QUERY_EMBED_TIMEOUT_MS + 1);

    expect(await pending).toBeNull();
  });
});
