# S18 edition history: first load

Status: done 2026-10-09. Works like [S15](2026-10-08-s15-editions-first-load.md),
[S8b](2026-10-08-s8b-editions-first-load.md), [S1b](2026-10-06-s1b-edition-history.md) and
[RO4](2026-10-07-ro4-edition-history.md). Loader: `scripts/s18_pipr_editions.py`; gates:
`scripts/s18_pipr_editions_verify.py`.

## What was recorded

S18 is the ONS Price Index of Private Rents, by local authority and month: average (mean) rent, rent
index and annual percentage change, for nine breakdowns (bedrooms, property type and so on).

New append-only table `la_private_rents_editions` (update, delete and truncate blocked by triggers
`s18_editions_immutable` and `s18_editions_no_truncate`), keyed by authority, breakdown, category, month
and edition. The live table `la_private_rents` is still one row per key, now defined as the latest
edition of each month.

- Edition 1, recorded 'as loaded' from the live table: 30 months (March 2024 to August 2026), 294 areas,
  nine breakdown blocks, 2,646 rows a month, 79,380 rows in all. Where a month had been loaded on several
  days, edition 1 carries the latest of those dates.
- No edition 2 yet. The editions table holds 79,380 rows.
- The latest month (August 2026) is flagged provisional in 2,646 live rows. The `provisional` flag is a
  compared value, so ONS making that month final later will be stored as a new edition.

Before and after, the live table is identical: 79,380 rows, 30 months, 294 areas, no NULL rents, the same
per-month rent totals, and the same content hash of all rows by key (f2a62a7a...3f1c).

## What the first comparison found

The newest ONS workbook found by the loader is the 16 September 2026 edition
(`pipr_16september2026.xlsx`, 18,640,156 bytes, sha256 starting ab1fdfbb27738b20; 370,440 English
area-month-block rows; latest month August 2026). It was compared cell by cell with edition 1 for all 30
months.

- **Nothing changed.** New months 0, unchanged months 30, revised months 0. No NULL replaced a number, no
  number replaced a NULL, no rent was zero or negative, no revision exceeded 50%, no key appeared on one
  side only.
- This agrees with the earlier read-only comparison (Task 1 of the S18 plan): the 16 September workbook
  equals the live table in all 79,380 cells. August 2026 was already loaded from this edition and is still
  provisional, so there was no provisional-to-final change to record.
- Because there was nothing to store, `load --commit` was not run (no edition 2 and no run log row).
  `refresh-latest` previewed zero rows. The first revision will show on a later monthly run.
- The latest period is unchanged (August 2026). W1, `refresh_map.py` and the export were not run, and
  none is needed: `la_private_rents` is not read by W1, `refresh_map.py` or `export_map_data.py` (see
  Notes).

## The monthly check

From `ONS_Population_Estimates`:

1. `python scripts/s18_pipr_editions.py load` downloads the newest workbook, checks its file name, Cover
   sheet and latest month agree, and previews every held month (new, unchanged or revised, with area and
   cell counts). Nothing is written.
2. Read the preview. The loader enforces these stop conditions in `load` with no override flag: a month
   with fewer areas than held (294) or a missing breakdown block, a NULL replacing a number in more than 5
   areas of a month, a zero or negative rent, a revision above 50% on more than 10 areas of a month, an
   unresolved area code, and a Barnsley and Sheffield form that disagrees with S18's declaration. A
   rejected month stores nothing; the other months still go through. The only override is
   `--allow-older-file`, for an older edition or earlier latest month than held, which is otherwise
   refused.
3. `python scripts/s18_pipr_editions.py load --commit` stores new months (edition 1, with their live
   rows, in one transaction) and revised months as the next edition.
4. If anything was revised, `refresh-latest` previews the live rows that would change; then run it with
   `--commit`.
5. `python scripts/s18_pipr_editions.py status` should say OK, and
   `python scripts/s18_pipr_editions_verify.py` should pass all 23 gates.

## Notes

- The live `source` column names the first-inserting edition. After `refresh-latest` it is not changed
  (source and `loaded_at` are kept), so for example August 2026 reads 'ONS PIPR 16september2026 edition'
  and the earlier months 'ONS PIPR 19august2026 edition', even when a later edition revised their values.
  The edition that supplied the current values is in the editions table (`release_label`, `source_file`,
  `source_sha256`).
- The first `sync-new` needed `--expected-areas 294`, because no month had editions yet.
- Barnsley and Sheffield codes (E08000038 and E08000039) resolve through `scripts/geography.py`, with
  S18's declaration 'new'; a file carrying the old codes halts before any write.
- `la_private_rents` is not read by Workflow 1 (`sql/w1/`), `scripts/refresh_map.py` or
  `scripts/export_map_data.py`; no view or function reads it either (searched the repository and the
  database on 2026-10-09; the registry has `publish_map` false). So no W1 run or map refresh follows an
  S18 load, including `refresh-latest --commit`. This corrects the first version of this note, which said
  to run `refresh_map.py` if the latest month changed.
- The registry keeps `revises_back_series` true on the strength of the publisher's practice (the latest
  month is provisional and re-published), worded as expected and not yet observed in the held months.
- The old pipeline (`s18_pipr_fetch.py`, `transform`, `load`, `verify`) is archived in `scripts/historical/`
  with a RETIRED guard. It inserted new rows and ignored revisions of rows already held
  (`ON CONFLICT DO NOTHING`); it did not overwrite. `s18_pipr_inspect.py` is kept as a read-only tool.
