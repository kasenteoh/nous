# Terminal theme for nous (portfolio palette)

2026-06-12. Approved in-session (visual preview + token table).

## Goal

Re-skin nous with the user's personal-site palette — true-black neutrals,
terminal green primary accent, red reserved for warnings — so nous shares one
brand with the portfolio that links to it. The semantic token system from
PR #33 is the enabler: this is a token-value swap, not a redesign. Tokyo
Night's colors are fully replaced; the token *system*, ThemeToggle,
dark-by-default behavior, and no-flash script are untouched.

## Token values (`web/app/globals.css`)

| token | role | `.dark` (palette, verbatim) | `:root` light (derived twin) |
|---|---|---|---|
| `--canvas` | page bg | `#0a0a0a` | `#fafafa` |
| `--ink` | primary text | `#e4e4e4` | `#1b1b1b` |
| `--ink-soft` | secondary text | `#9a9a9a` | `#515151` |
| `--ink-muted` | tertiary/meta | `#5f5f5f` | `#8a8a8a` |
| `--ink-faint` | dim/decorative | `#3d3d3d` | `#d4d4d4` |
| `--edge` | borders | `#2a2a2a` | `#e2e2e2` |
| `--accent` | links/active nav | `#7ee787` | `#1a7f37` |
| `--money` | funding amounts | `#a5f4ae` | `#2da44e` |
| `--warn` (new) | low-confidence / errors | `#ff7b72` | `#cf222e` |

Light-twin accents are the GitHub-light equivalents of the dark greens/reds,
chosen for AA contrast on `#fafafa` (`#1a7f37` ≈ 4.9:1, `#cf222e` ≈ 5.4:1).
`--money` uses the palette's hover-green in dark so amounts read slightly
brighter than links; both stay green by design (terminal aesthetic accepts
accent/money convergence).

## Red is semantic-only

`--warn` is used exclusively for the low-confidence funding badge
(`web/components/FundingHistory.tsx`) and future error/empty states. No
decorative red: on a funding site, red near dollar amounts reads as loss.

## Out of scope

- Surface tokens (palette's code `#0e0e0e` / sidebar `#111111` / cards
  `#161616` / hover `#1c1c1c`): nous has no surface-differentiated components
  today. If a card treatment lands later, use `#161616`.
- Any component/layout change beyond the badge class.
- Theme switcher changes (still binary light/dark, dark default).

## Files

1. `web/app/globals.css` — swap token values in `:root` + `.dark`, add
   `--warn` + its `@theme inline` line, rewrite the provenance comment.
2. `web/components/FundingHistory.tsx` — low-confidence badge uses `warn`
   color classes.

## Verification

`npm run build`; dev-preview screenshots of front page, `/companies`, and a
company page with a low-confidence round, in both modes. Accessibility spot
check: ink ramp ≥ AA for its roles in both modes.

## Rollback

Single revert of the theme commit restores Tokyo Night (values only — the
token system is shared by both looks).
