// Pure helpers for the /themes pages. Import-safe anywhere (no env access).

import type { ThemeMember } from "@/lib/types";

/** How many of the newest members the theme page's entrants list shows. */
export const NEW_ENTRANTS_LIMIT = 5;

/**
 * Members most recently added to the catalog, newest first — the "new
 * entrants" list on /themes/[slug]. `created_at` is the company's first-seen
 * timestamp; members without one (defensive: the projection always selects
 * it) are unplaceable and dropped rather than sorted arbitrarily.
 */
export function newestEntrants(
  members: readonly ThemeMember[],
  limit = NEW_ENTRANTS_LIMIT,
): ThemeMember[] {
  return members
    .filter((m) => m.created_at)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, limit);
}
