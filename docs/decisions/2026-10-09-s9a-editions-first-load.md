# S9a edition history: first load

Status: done 2026-10-09. Works like [S18](2026-10-09-s18-editions-first-load.md),
[S15](2026-10-08-s15-editions-first-load.md), [S8b](2026-10-08-s8b-editions-first-load.md) and
[S1b](2026-10-06-s1b-edition-history.md). Loader: `scripts/s9a_drd_editions.py`; gates:
`scripts/s9a_drd_editions_verify.py` (24 gates, all PASS, exit 0).

## What was recorded

S9a is the NHS England Discharge Ready Date (DRD) monthly publication, by upper-tier local authority
(UTLA): discharges, bed days lost and the spread of delay lengths. It publishes one workbook per month.

New append-only table `nhs_drd_discharge_delays_editions` (update, delete and truncate blocked by
triggers `s9a_editions_immutable` and `s9a_editions_no_truncate`), keyed by UTLA, month and edition, and a
file-check ledger `nhs_drd_discharge_delays_editions_file_checks` (also append-only) that records every
file read, with its sha256 and whether it was new, unchanged or revised. The live table
`nhs_drd_discharge_delays` is still one row per month and UTLA, now defined as the latest edition of each
month.

- Edition 1, recorded 'as loaded' from the live table: 28 months (April 2024 to July 2026), 153 UTLAs
  (including the 21 county areas), 4,284 rows. `total_discharges` is NULL in every row, as in the
  publication; `avg_days_drd_to_discharge_exc_zero` is NULL in 9.
- June 2026 edition 2 (153 rows): the same month read from the NHS England webfile instead of the CSV that
  `scripts/verify/verify_load_drd.py` loaded on 2026-08-20. This is a file-form correction, not a
  publisher revision. The two forms agree to nine decimal places; the webfile carries full floating-point
  precision (734 cells differ, largest difference 3.9e-9, none above the 1e-8 precision threshold, no
  count differs). The release label says 'precision-only'. Edition 1 of June 2026 names the CSV; edition 2
  names the webfile published 2026-08-13.
- August 2026 loaded as a new month: edition 1 and 153 live rows in one transaction, from the webfile
  published 2026-10-08 (sha256 starting d7269efd031f7e4a). Total bed days lost 294,514. Two UTLAs have no
  average-days-excluding-zero figure (NULL, as published).
- The editions table now holds 4,590 rows (29 months; 28 at edition 1 plus June 2026 edition 2, plus August
  2026). The file-check ledger holds 29 rows: 27 unchanged, 1 revised, 1 new.

Before and after, the 27 untouched months are identical: per-month rows and total bed days are the same
for every month from April 2024 to May 2026 and for July 2026. The live table went from 4,284 rows and
28 months to 4,437 rows and 29 months (latest August 2026); June 2026 was rewritten by `refresh-latest`
with the precision-only values. The pre-load content hash of all rows by key (without `loaded_at`) was
c2f8d600...a588d.

## What the first comparison found

`load --recheck-all` re-read all 28 held months from their current webfile and compared each with edition
1: 27 unchanged (0 areas changed), June 2026 revised precision-only, August 2026 new. No NULL replaced a
number, no number replaced a NULL, no 0 became NULL or the reverse, no bed-days revision above 50%, no
area on one side only, no negative count, no percentage outside 0 to 1, no unexpected code. Every month
had 153 areas. The files dropped one row each marked NULL and, in June 2026,
78 rows marked '#N/A'; both are counted by the loader and never stored as 0. Barnsley and Sheffield keep
their old codes (E08000016 and E08000019), S9a's declaration being 'old'.

Preview and commit agreed; every month was committed in its own transaction.

Two publisher revision waves exist in the sheets: 2025-07-10 (April 2024 to March 2025) and 2026-07-09
(April 2025 to March 2026), shown in each workbook's `Revised:` date. Both predate this first load, and
the held values were read from the post-revision files, so their size cannot be measured from held data.
They are recorded as the identity of the files only. The next wave is expected about July 2027.

## The live source column

Live `source` holds the URL of the file that supplied the row. After a partly revised month it can be
mixed (some rows from the original, some from the revised file) because the old loader upserted. In this
load June 2026 was rewritten in full by `refresh-latest`, so each of June, July and August 2026 now has
one uniform `source` (the webfile URL), and `source` equals the editions table's `source_file` for the
latest edition of every month. From the same date `refresh-latest` sets `source` for every row of a
refreshed month (an opt-in engine setting that S9a and S9b turn on), so a partly revised month no longer
carries mixed sources; values are still only rewritten where they changed. The views and `check_sources.py` read `source`.

## The monthly check

From `ONS_Population_Estimates`:

1. `python scripts/s9a_drd_editions.py load` reads the page, picks each month's current webfile (a
   `-Revised` file beats the original), skips files already in the ledger, and previews new, unchanged
   and revised months. Nothing is written to the database (downloads go to `data/raw/s9a_drd/`).
2. Read the preview. The stop conditions are enforced in `load`, with no override flag other than
   `--allow-older-file` (an older file or earlier latest month than held) and `--acknowledge PERIOD` (a
   0 to NULL or NULL to 0 change): fewer or more than 153 areas, an area of the held latest month
   missing, an unexpected code, a negative count, a percentage outside 0 to 1, NULL replacing a number in
   more than 5 areas, bed days revised by more than 50% in more than 10 areas, and a Barnsley and
   Sheffield form that disagrees. A rejected month stores nothing.
3. `python scripts/s9a_drd_editions.py load --commit` stores new months (edition 1 with live rows) and
   revisions as the next edition.
4. If anything was revised, `refresh-latest` previews, then `--commit` copies the latest edition to live.
5. `python scripts/s9a_drd_editions.py status` should say OK and
   `python scripts/s9a_drd_editions_verify.py` should pass all 24 gates.
6. Occasionally (and each July) run `load --recheck-all` to re-read every month against its webfile.

## Notes

- August 2026 is now the latest month, so the next W1 run changes `drd_bed_days_lost` and
  `drd_pct_delayed_1plus_days`. W1, `refresh_map.py` and the export were not run here.
- The first `sync-new` needed `--expected-areas 153`.
- The April 2026 CSV is a version-2 reissue; the loader reads the webfile only and says so.
- The old scripts (`s9a_drd_build.py`, `verify_load_drd.py`) are retired in a later step of this plan.
