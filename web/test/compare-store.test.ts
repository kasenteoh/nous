import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import {
  addToCompare,
  clearCompare,
  COMPARE_KEY,
  MAX_COMPARE,
  readCompareSet,
  removeFromCompare,
  toggleCompare,
  useCompareSet,
  useIsComparing,
} from "@/lib/compare";

beforeEach(() => {
  window.localStorage.clear();
});

describe("readCompareSet", () => {
  it("returns [] when nothing is stored", () => {
    expect(readCompareSet()).toEqual([]);
  });

  it("returns [] for unparseable JSON instead of throwing", () => {
    window.localStorage.setItem(COMPARE_KEY, "{not json!!");
    expect(readCompareSet()).toEqual([]);
  });

  it("returns [] when the payload is valid JSON but not an array", () => {
    window.localStorage.setItem(COMPARE_KEY, JSON.stringify({ a: 1 }));
    expect(readCompareSet()).toEqual([]);
    window.localStorage.setItem(COMPARE_KEY, "42");
    expect(readCompareSet()).toEqual([]);
  });

  it("drops non-string and empty entries from a tampered array", () => {
    window.localStorage.setItem(
      COMPARE_KEY,
      JSON.stringify([1, "acme", null, "", { x: 1 }, "globex"]),
    );
    expect(readCompareSet()).toEqual(["acme", "globex"]);
  });

  it("de-duplicates repeated slugs", () => {
    window.localStorage.setItem(
      COMPARE_KEY,
      JSON.stringify(["acme", "acme", "globex"]),
    );
    expect(readCompareSet()).toEqual(["acme", "globex"]);
  });

  it(`caps a tampered oversized payload at MAX_COMPARE (${MAX_COMPARE})`, () => {
    window.localStorage.setItem(
      COMPARE_KEY,
      JSON.stringify(["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"]),
    );
    expect(readCompareSet()).toEqual(["s1", "s2", "s3", "s4"]);
  });
});

describe("add / remove / toggle / clear", () => {
  it("addToCompare appends until the cap, then becomes a no-op", () => {
    addToCompare("a");
    addToCompare("b");
    addToCompare("c");
    addToCompare("d");
    expect(readCompareSet()).toEqual(["a", "b", "c", "d"]);
    addToCompare("e"); // full — must not grow
    expect(readCompareSet()).toEqual(["a", "b", "c", "d"]);
  });

  it("addToCompare is a no-op for a slug already present", () => {
    addToCompare("a");
    addToCompare("a");
    expect(readCompareSet()).toEqual(["a"]);
  });

  it("removeFromCompare removes a present slug and ignores an absent one", () => {
    addToCompare("a");
    addToCompare("b");
    removeFromCompare("a");
    expect(readCompareSet()).toEqual(["b"]);
    removeFromCompare("zzz");
    expect(readCompareSet()).toEqual(["b"]);
  });

  it("toggleCompare returns true when it adds and false when it removes", () => {
    expect(toggleCompare("a")).toBe(true);
    expect(readCompareSet()).toEqual(["a"]);
    expect(toggleCompare("a")).toBe(false);
    expect(readCompareSet()).toEqual([]);
  });

  it("toggleCompare returns false and leaves the set unchanged when full ('blocked', not 'added')", () => {
    for (const s of ["a", "b", "c", "d"]) addToCompare(s);
    expect(toggleCompare("e")).toBe(false);
    expect(readCompareSet()).toEqual(["a", "b", "c", "d"]);
  });

  it("clearCompare empties the set", () => {
    addToCompare("a");
    addToCompare("b");
    clearCompare();
    expect(readCompareSet()).toEqual([]);
  });
});

describe("useCompareSet / useIsComparing", () => {
  it("reflects store changes made through the mutators", () => {
    const { result } = renderHook(() => useCompareSet());
    expect(result.current).toEqual([]);
    act(() => {
      addToCompare("acme");
    });
    expect(result.current).toEqual(["acme"]);
    act(() => {
      removeFromCompare("acme");
    });
    expect(result.current).toEqual([]);
  });

  it("returns a referentially stable snapshot until the stored value changes", () => {
    const { result, rerender } = renderHook(() => useCompareSet());
    act(() => {
      addToCompare("acme");
    });
    const first = result.current;
    rerender();
    // Same array instance — getSnapshot caches on the raw localStorage string,
    // so useSyncExternalStore doesn't loop on fresh arrays.
    expect(result.current).toBe(first);
  });

  it("useIsComparing tracks membership of one slug", () => {
    const { result } = renderHook(() => useIsComparing("acme"));
    expect(result.current).toBe(false);
    act(() => {
      toggleCompare("acme");
    });
    expect(result.current).toBe(true);
    act(() => {
      toggleCompare("acme");
    });
    expect(result.current).toBe(false);
  });
});
