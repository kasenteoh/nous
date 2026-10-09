# Runbook: drain the wrong-entity suspect queue

**Written:** 2026-10-09 (PR #258 and the first prod drain)
**Lever:** `ops.yml` → `purge-wrong-entity-batch-{dry-run,apply}`, plus the
single-company `purge-wrong-entity-articles-apply` for held companies.
**Cost:** about $0.01 per company (one DeepSeek adjudication per stored article).

## What it does

1. `audit-round-entities` (free, deterministic) lists every funding round
   whose own coverage doesn't corroborate the company as its subject. On
   2026-10-09 that was 220 suspect rounds across 144 companies.
2. The batch queues each suspect company once, largest suspect round first.
3. For each company, it adjudicates **every** stored article against the
   company's profile with `article_subject_match`.
4. It deletes an article (plus the rounds, total and status sourced only from
   it, and their ✓ verifications) **only on a HIGH-confidence "another
   entity" verdict**. Anything weaker is kept and counted as
   `articles_uncertain_kept`.

## Safety rails and what each one does NOT catch

| Rail | Catches | Misses |
|---|---|---|
| High-confidence delete | Thin-evidence false deletes. The old any-non-attach rule marked 8 of Blue Origin's own funding articles for deletion. | — |
| Hold (≥80% of ≥3 articles mismatched) | A profile whose coverage is all someone else's (built = BUILT protein bars carrying every "Built In" headline, incl. Anthropic's $30B) | A wrong-identity profile with a mixed article set |
| `skip` input | Whatever the operator lists | — |
| Fail-KEEP on LLM error; whole-queue stop on 429 | Transient provider trouble | — |

**The case only a human catches: the profile itself is the wrong entity.**
The site name can belong to the famous company while the resolved website,
and so the description, belongs to a homonym:

- `prometheus`: a prometheus.com profile vs Bezos's $12B Prometheus coverage.
- `humans`: a humans.io personal-CRM site under the name "humans&",
  carrying the Humans& AI lab's $480M.

A purge then deletes the real story the slug is named for. Fix those with
`reresolve-company --set-url`, not a purge.

## Procedure

1. **Dry-run a page:**
   ```sh
   gh workflow run ops.yml -f command=purge-wrong-entity-batch-dry-run \
     -f limit=40 -f offset=0
   ```
   About 20 minutes for 40 companies. The step summary has a table; the JSON
   lists `purged_titles` ("headline — other entity") per company.
2. **Review.** For anything surprising, read the live profile:
   `curl https://nous-umber.vercel.app/c/<slug>.md` (404 means excluded).
   - A would-purge headline that names the company directly ("Linx Security
     raises $50M") is usually a **homonym profile**. If the profile is a
     different same-name company (a Chicago camera installer at
     linx-security.com), the purge is still an improvement: the page becomes
     consistent instead of a chimera.
   - Skip only when the purge would delete the story the slug *should* be
     about, or when the profile and the coverage plausibly are the same
     company.
3. **Apply the same page** with the skip list:
   ```sh
   gh workflow run ops.yml -f command=purge-wrong-entity-batch-apply \
     -f limit=40 -f offset=0 -f skip=prometheus,humans
   ```
4. **Held companies:** review each one. If the coverage really is all
   another entity, purge it with
   `-f command=purge-wrong-entity-articles-apply -f slug=<slug>`.
5. **Serialize.** Every ops dispatch shares the `nous-pipeline-db`
   concurrency group with the cron. Dispatch one run at a time and wait for
   it to finish.

## Re-running

The batch is idempotent. Purged rounds leave the probe's suspect set, so the
queue shrinks: 144 companies became 137 after the first two pages. A company
whose articles all adjudicate as ours is simply re-confirmed for about
$0.01. Re-run the probe after a large ingest to catch recurrence; the #235
ingest guard should keep the queue small.
