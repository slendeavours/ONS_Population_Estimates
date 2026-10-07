# Editions: a general quarterly refresh for S1 and S1b

Status: implemented 2026-10-07. Follows the [S1](2026-10-06-s1-edition-history-design.md) and
[S1b](2026-10-06-s1b-edition-history.md) edition work, which left `refresh-latest` specific to the
2026-10-06 reload. Those records are left as written.

## What

- `refresh-latest` (both tables) takes no period list. It updates any quarter whose live rows differ from
  its latest edition, in one transaction, and only the columns it always changed (S1: the six measures,
  `source_file`, `extracted_at`; S1b: `value`, `value_flag`, `source_url`, `source_edition`,
  `edition_variant`, `category_label`; never `loaded_at`, never the S1 `*_suspect` columns).
- `sync-new` records any quarter present in the live table with no editions as edition 1 'as loaded'.
  It is idempotent and refuses a short quarter (a missing authority or cell, and for S1b a different
  category set unless `--accept-categories PERIOD`).
- `status` reports new quarters, pending refreshes, drift, chain errors and row-count faults, and exits 1 if
  anything needs action.
- Expected row counts are derived from the data (authorities per quarter; for S1b authorities times the
  categories of the quarter's edition 1, 31 in the legacy layout and 32 in the 2025Q4 layout), not typed.
  A quarter with 295 authorities or a missing category fails instead of being absorbed.
- The procedure for an operator is [../QUARTERLY_REFRESH.md](../QUARTERLY_REFRESH.md).

## Why

Both refreshes hardcoded the seven stale quarters, the snapshot tables and the row counts, so the next
quarterly load could not use them, and several gates only passed because of the snapshots.

## The drift rule

Drift is a quarter whose live rows differ from the latest edition **and** match no stored edition: someone
changed the live table outside the editions machinery. `status` names it, and `refresh-latest` halts on it
unless that quarter is named with `--accept-drift PERIOD` (the operator either loads the live values as an
edition, or accepts the overwrite). A quarter whose live rows equal an earlier edition is a **pending
refresh**, not drift: a newer edition has been loaded and the live table has not caught up. This narrowing
of "live differs from latest" is deliberate; without it a normal load-then-refresh would look like
tampering.

## No more snapshots

The refresh no longer takes a snapshot table. Inside the transaction it hashes every quarter before and
after (all columns except `loaded_at`, and a second hash excluding the refresh columns), and rolls back if
a quarter outside the set it meant to refresh changed, if a refreshed quarter changed outside its refresh
columns or does not equal its edition, or if a row count moved. S1 also compares the workflow 1 signal
outputs before and after, which may differ only for authorities whose current-quarter or prior-year
temporary-accommodation figure changed. The retired snapshot gates are replaced by seeded gates that
insert a fault inside a rolled-back savepoint and show the guard catching it (tampering with another
quarter, `loaded_at`, a `*_suspect` column, the row count, a layout field). A scan gate fails if the refresh
path ever reintroduces a snapshot table name, a fixed row count or a period list.

The three October snapshot tables (`la_statutory_homelessness_bak_20261006`,
`la_homelessness_support_needs_bak_20261006` and `_bak_20261006b`) are retained and unused. Scott may drop
them when comfortable.

## Verification run time

`python scripts/s1b_editions_verify.py` takes about **14 minutes** (gate 7 re-reads the raw files);
`s1b_support_needs_verify.py` about 3 minutes. Run the verify scripts one at a time: the seeded gates take
exclusive locks.

## Still open

- **Detecting a publisher revision is manual.** Nothing watches the release pages for a revised file.
- **RO4 table key** (`lad24cd, financial_year`) still needs edition treatment before the source is next
  revised.
- **n8n S1 node 2 `|| 0` defect** (and the stale W1 period labels) remain.
- `s1b_support_needs_build.py` still prefers the release-page file, and no trigger enforces
  `supersedes` = chain tip (see the index).
- Metadata drift (for example a changed `source_url`) is not shown by `status`; the verify gates and the
  refresh's own post-check catch it.
