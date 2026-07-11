import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  readWatchlist,
  toggleWatch,
  WATCHLIST_KEY,
  WatchlistButton,
} from "@/components/WatchlistButton";
import {
  getSavedSearchesSnapshot,
  readSavedSearches,
  SaveSearch,
  SEARCHES_KEY,
  writeSavedSearches,
  type SavedSearch,
} from "@/components/SaveSearch";

beforeEach(() => {
  window.localStorage.clear();
});

describe("watchlist store", () => {
  it("readWatchlist returns [] when nothing is stored", () => {
    expect(readWatchlist()).toEqual([]);
  });

  it("readWatchlist tolerates garbage payloads without throwing", () => {
    window.localStorage.setItem(WATCHLIST_KEY, "%%%not-json");
    expect(readWatchlist()).toEqual([]);
    window.localStorage.setItem(WATCHLIST_KEY, JSON.stringify({ nope: true }));
    expect(readWatchlist()).toEqual([]);
  });

  it("readWatchlist keeps only string entries from a tampered array", () => {
    window.localStorage.setItem(
      WATCHLIST_KEY,
      JSON.stringify(["acme", 7, null, ["x"], "globex"]),
    );
    expect(readWatchlist()).toEqual(["acme", "globex"]);
  });

  it("toggleWatch adds then removes a slug", () => {
    toggleWatch("acme");
    expect(readWatchlist()).toEqual(["acme"]);
    toggleWatch("acme");
    expect(readWatchlist()).toEqual([]);
  });

  it("WatchlistButton toggles aria-pressed and its label on click", () => {
    render(<WatchlistButton slug="acme" name="Acme" />);
    const button = screen.getByRole("button", {
      name: "Add Acme to watchlist",
    });
    expect(button).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveAccessibleName("Remove Acme from watchlist");
    expect(readWatchlist()).toEqual(["acme"]);

    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(readWatchlist()).toEqual([]);
  });
});

describe("saved-search store", () => {
  it("readSavedSearches returns [] for missing or unparseable payloads", () => {
    expect(readSavedSearches()).toEqual([]);
    window.localStorage.setItem(SEARCHES_KEY, "{{{{");
    expect(readSavedSearches()).toEqual([]);
  });

  it("readSavedSearches drops entries missing the query/savedAt shape", () => {
    const good: SavedSearch = { query: "industry=AI", savedAt: 1000 };
    window.localStorage.setItem(
      SEARCHES_KEY,
      JSON.stringify([
        good,
        { query: 42, savedAt: 1000 }, // wrong query type
        { query: "x" }, // missing savedAt
        "just-a-string",
        null,
      ]),
    );
    expect(readSavedSearches()).toEqual([good]);
  });

  it("getSavedSearchesSnapshot returns the same array instance until the stored value changes", () => {
    writeSavedSearches([{ query: "q=db", savedAt: 1 }]);
    const first = getSavedSearchesSnapshot();
    const second = getSavedSearchesSnapshot();
    expect(second).toBe(first);

    writeSavedSearches([{ query: "q=db", savedAt: 2 }]);
    const third = getSavedSearchesSnapshot();
    expect(third).not.toBe(first);
    expect(third[0].savedAt).toBe(2);
  });
});

describe("SaveSearch button", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("saves the current query on click, flashes 'Saved', then settles disabled as 'Search saved'", () => {
    vi.useFakeTimers();
    render(<SaveSearch query="industry=AI&state=CA" />);
    const button = screen.getByRole("button", { name: "Save search" });

    fireEvent.click(button);
    expect(readSavedSearches().map((s) => s.query)).toEqual([
      "industry=AI&state=CA",
    ]);
    expect(button).toHaveAccessibleName("Saved");

    act(() => {
      vi.advanceTimersByTime(1600); // flash lasts 1.5s
    });
    expect(button).toHaveAccessibleName("Search saved");
    expect(button).toBeDisabled();
  });

  it("does not duplicate an already-saved query", () => {
    writeSavedSearches([{ query: "q=infra", savedAt: 123 }]);
    render(<SaveSearch query="q=infra" />);
    const button = screen.getByRole("button");
    expect(button).toBeDisabled(); // already saved — save is unreachable
    fireEvent.click(button);
    expect(readSavedSearches()).toHaveLength(1);
  });

  it("caps the saved list at 30, newest first", () => {
    const many: SavedSearch[] = Array.from({ length: 30 }, (_, i) => ({
      query: `q=old-${i}`,
      savedAt: i,
    }));
    writeSavedSearches(many);
    render(<SaveSearch query="q=newest" />);
    fireEvent.click(screen.getByRole("button", { name: "Save search" }));

    const saved = readSavedSearches();
    expect(saved).toHaveLength(30);
    expect(saved[0].query).toBe("q=newest");
    expect(saved.map((s) => s.query)).not.toContain("q=old-29");
  });
});
