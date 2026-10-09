# S9b edition history: first load

Status: done 2026-10-09. Works like [S9a](2026-10-09-s9a-editions-first-load.md),
[S18](2026-10-09-s18-editions-first-load.md), [S15](2026-10-08-s15-editions-first-load.md),
[S8b](2026-10-08-s8b-editions-first-load.md) and [S1b](2026-10-06-s1b-edition-history.md). Loader:
`scripts/s9b_crfd_editions.py`; gates: `scripts/s9b_crfd_editions_verify.py` (24 gates, all PASS, exit 0).

## What was recorded

S9b is NHS England's Mental Health Services Data Set (MHSDS) measure MHS26: days of delayed discharge for
patients clinically ready for discharge, by local authority, one data file per month. The live table
`nhs_mh_crfd` holds one row per month and local authority (296 areas; the county areas and the UNKNOWN row
are dropped and counted).

New append-only table `nhs_mh_crfd_editions` (update, delete and truncate blocked by triggers
`s9b_editions_immutable` and `s9b_editions_no_truncate`), keyed by area, month and edition, and a
file-check ledger `nhs_mh_crfd_editions_file_checks` (also append-only) recording every file read with its
sha256 and whether it was new, unchanged or revised.

- Edition 1, recorded 'as loaded' from the live table: 40 months (April 2023 to July 2026), 296 areas,
  11,840 rows. Edition 1 is the figures as first loaded, which for April 2023 to March 2026 are the
  publisher's Performance figures (the early, quick-turnaround submissions).
- Edition 2 of each of the 36 months April 2023 to March 2026 (36 months of 296 areas, 10,656 rows) is the publisher's year-end Final file. The old loader excluded Final files on purpose and so
  never took them.
- August 2026 loaded as a new month: edition 1 and 296 live rows in one transaction, from the Performance
  file (sha256 starting 37db297e500b51f5). National total 42,450.
- The editions table now holds 22,792 rows (41 months; 12,136 at edition 1, 10,656 at edition 2). The live
  table holds 12,136 rows. The file-check ledger holds 39 rows: 36 revised, 2 unchanged, 1 new.

What the publisher says Final is (MHSDS publication page, March 2026): Final files "reflect revised 2025-26
Multiple Submission Window Model submissions" made after the Performance month was published.

## What the first comparison found

`load` compared each month's current file with edition 1: 36 Performance-to-Final revisions, April and May
2026 unchanged (their v2 reissues equal what was held), August 2026 new. The read-only preview of Task 4 and
the real preview were identical, line for line (August 2026 included). No stop condition tripped: every
month had 296 areas, none was missing, no unexpected code, no negative value, no national total moved by more
than 50% (the largest move is +33.0%, April 2024), no 0 became NULL or the reverse, identity (period,
status) agreed in every file, and Barnsley and Sheffield followed the declared 'mixed' forms (old codes
E08000016/E08000019 to March 2025, new codes E08000038/E08000039 from April 2025).

Across the 36 months: 5,038 area values changed (104 to 175 per month), 683 areas went from NULL (suppressed) to a number, 316 went from a number to NULL, 949 were revised
by more than 50%. Revisions are large by nature: Final usually adds late submissions, so national totals rise
in 35 of 36 months (March 2024 falls 1.5%). By financial year, national total of non-NULL values, Performance
to Final:

- 2023-24 months: +10.8% (April 2023) falling to +2.0% (February 2024), -1.5% (March 2024).
- 2024-25 months: the largest, +33.0% (April 2024), +31.6%, +24.3%, +22.2% (to August 2024), then +12.4% to
  +9.2% in the autumn, +9.9%, +15.6% and +12.5% in January to March 2025.
- 2025-26 months: +1.4% to +6.8%.

The five largest relative changes (small counts to large ones, mostly publisher revisions of an area's late
submissions): 2024-06 E09000012 5 to 315; 2024-10 E09000025 10 to 460; 2025-08 E06000058 15 to 565; 2024-06
E07000226 5 to 145; 2024-05 E07000039 5 to 140.

Labels: 10 months differ from edition 1 in one area name, because the publisher's file swaps or recases a
name (E06000030 SWINDON to Swindon; E07000083 Tewkesbury and North Gloucestershire exchanged between files).
The code is the key and is unchanged; the live name follows the latest edition.

April and May 2026: their v2 files equal edition 1 value for value (ledger: unchanged); no edition 2. June
and July 2026 were not re-read (already in the ledger). April and May stay Performance until the 2026-27
year-end Finals.

Before and after, the untouched months are identical: June, July (and April, May) 2026 have the same rows,
NULL counts and totals as before (41,845 and 43,630 for June and July). The live table went from 11,840 rows
and 40 months to 12,136 rows and 41 months. The pre-load hash of all live rows (including `source`) was
c0eac3da...c4680.

## The live source column

Live `source` holds the URL of the file that supplied the row. `refresh-latest` set it for every row of each
refreshed month (opt-in engine setting `refresh_source_whole_period`, which S9a and S9b turn on), so each of
the 41 months now has one uniform `source`, equal to the latest edition's `source_file`. 5,048 live rows had
a value or label change (5,038 areas changed by value plus the 10 relabelled); the before/after guard passed.
The views and `check_sources.py` read `source`.

## The monthly check

From `ONS_Population_Estimates`:

1. `python scripts/s9b_crfd_editions.py load` reads each month's publication page, picks the highest-ranking
   file (Final above Performance, then the version), skips files already in the ledger and previews new,
   unchanged and revised months. Nothing is written to the database (downloads go to `data/raw/s9b_mhsds/`,
   a preview reads about 40 files and takes a couple of minutes).
2. Read the preview. Stop conditions are enforced in `load` (a stopped month is REJECTED and exit is 1): a
   month with other than 296 areas or missing an area of the held latest month, an unexpected code, a negative
   value, a Performance-to-Final national total moving over 50%, any other revision with NULL replacing a
   number in more than 5 areas or a value revised above 50% in more than 10, a 0 to NULL or the reverse
   (`--acknowledge PERIOD`), identity disagreement, Barnsley/Sheffield form disagreement, and an older file
   (`--allow-older-file`).
3. `python scripts/s9b_crfd_editions.py load --commit` stores new months and revisions.
4. If anything was revised, `refresh-latest` previews, then `--commit` copies the latest edition to live.
5. `status` should say OK and `python scripts/s9b_crfd_editions_verify.py` should pass all 24 gates.
6. Occasionally run `load --recheck-all`.

What September brings: the September 2026 Performance file when NHS England publishes it (its page had no file yet); the next large event is the 2026-27 year-end Final in spring 2027 (April 2026 to March 2027),
when the Performance months become Final and the same Performance-to-Final pattern of 100 to 175 changed
areas a month recurs. April and May 2026, held as Performance v2, then become Final.

## Notes

- W1 reads the latest month only, so the Final revisions change no ranking. August 2026 as the new latest
  month changes `crfd_days` and the `mental_health` and `learning_disability` tenant types at the next W1
  run. W1, `refresh_map.py` and the export were not run here.
- The first `sync-new` needed `--expected-areas 296`.
- The old loader (`s9b_crfd_build.py`) is retired in a later step of this plan.
