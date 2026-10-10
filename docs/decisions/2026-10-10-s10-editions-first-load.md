# S10 MHCLG rough sleeping snapshot in England: first load on the editions engine

Status: done 2026-10-10. Works like [S23](2026-10-09-s23-editions-first-load.md) and [S6](2026-10-10-s6-editions-first-load.md).
Loader: `scripts/s10_rough_sleeping_editions.py`; gates: `scripts/s10_rough_sleeping_editions_verify.py` (all PASS, exit 0).
Design: `docs/superpowers/specs/2026-10-10-no-loader-sources-design.md` (audit record, not approval).

No held value changed. The live table `la_rough_sleeping` is exactly as it was (same 296 rows, columns, constraints and
`loaded_at`); the migration wrote edition 1 "as loaded" and a ledger row to two new tables beside it. The map is
unaffected: W1 and `refresh_map.py` were not run (Scott deferred W1 and the map until every source is done).

## Before-state (read-only, confirmed against the live table before anything was written)

Confirmed first: 296 rows, one `snapshot_year` (2025), 296 authorities, one `loaded_at` (2026-08-19 23:59:03.530757 UTC),
0 NULLs in `rough_sleeping` and `rough_sleeping_prev_year`, survey hash `772625863ca8f2596ec7633e0e303a21`.
Sums: England 4,793 (2025 column) and 4,667 (2024 column). Zeros as published: 15 in `rough_sleeping` and 12 in
`rough_sleeping_prev_year`.

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a
64-hex run, stays untouched. The first line is the loader's `rows_content_sha` (key plus the two values, `loaded_at`
excluded; the line the verify script's gate 16 reads, recomputed on 2026-10-10 from the live rows before the migration;
Task 2's earlier figure was the same). The second hashes every stored column, `loaded_at` included: columns in table
order, rows sorted by `snapshot_year` then `lad24cd`, NULL as empty, cells joined by `|`, rows by LF.

before-state la_rough_sleeping 2025 rows=296 sha256-first32=db8ccd4b6df7007a7cfbaaceccb4cc1b sha256-last32=3f72d1073075e6221c8ceca16579de71
before-state-all la_rough_sleeping 2025 rows=296 sha256-first32=6d4c0ede0d6b184908f543df852a7a01 sha256-last32=06c475cc80958b3e6e1fd921feec6e74

After the migration the live table is untouched, so both hashes of the live table are the same, and edition 1 hashes
(compared columns) to the first.

## How the table came to hold what it holds (checked 2026-10-10 against the database, the run log and the docs)

- **2026-03-26, run-log row 17** ("Source 10 - Rough Sleeping Snapshot", 296 rows, started 2026-03-26 10:18:34 UTC): the
  n8n workflow "Rough Sleeping Snapshot (S10)" upserted a CSV that had been converted from the publisher's file in a
  chat session that no longer exists. The row's note says "MHCLG autumn 2025 snapshot published 26 Feb 2026", but the
  values it loaded were the **autumn 2021 and autumn 2020 snapshots** in the columns headed 2025 and 2024: the backup
  `la_rough_sleeping_bak_20260820` (296 rows, one `loaded_at` of 2026-03-26 10:18:28 UTC) sums to 2,443 and 2,688, and
  the publisher's own England row gives exactly 2,443 for 2021 and 2,688 for 2020 (the figure for 2025 is 4,793).
- **W1 runs 4 to 17 carried the wrong numbers.** The `staging_runs` table holds W1 runs 4, 5, 6, 7, 9, 12, 15 and 17
  between 2026-04-01 and 2026-08-16 (other numbers are missing from the table). The live table held the 2,443 values
  from 2026-03-26 until 2026-08-19, so every run in that span read England as 2,443 against a published 4,793;
  `docs/METHODOLOGY.md` states the figure for run 17 (2026-08-16) and the correction in run 18. That every earlier run
  read the same values follows from the table not changing; where the table holds the run (`staging_national`: runs 4, 5, 6, 7, 9, 12, 15 and 17) it records `rough_sleeping_current` 2,443 directly, and only the runs missing from the table are inferred.
- **Rewrite, 2026-08-19 23:59:03 UTC:** the table was rewritten from the publisher's autumn 2025 file
  (`scripts/verify/src/rs_autumn2025.ods`, the same bytes as the file now held in `data/raw/s10_rough_sleeping/`). It
  left **no run-log row** (none was written between 2026-08-19 22:00 and 2026-08-20 02:00 UTC) and **no committed code**
  (the workflow's code was the n8n Code node, and nothing in the repo loaded it). The only evidence is the live data:
  every row carries that one `loaded_at`, and the live table differs from the backup in `rough_sleeping` for 269 of 296
  authorities (271 in `rough_sleeping_prev_year`; 290 of 296 differ in at least one of the two values).
  `docs/METHODOLOGY.md` records the 269, on rough sleeping.
- **Runs 18 onward** (W1 run 18 on 2026-08-20) carry England 4,793, matching the publication.
- **The backup keeps the wrong values.** `la_rough_sleeping_bak_20260820` still holds the 2021 and 2020 numbers under the
  2025 and 2024 headings. It is evidence and was not touched. Nothing should read it as data.
- The n8n workflow is retired (Task 1 of this plan): its Code node now throws. The repo had no S10 code before
  `scripts/s10_rough_sleeping_editions.py`.

## What the old load did, and the rule 1 defect

The n8n Code node converted a blank or a marker to 0 (`|| 0`). It did not matter in the values held, because the
autumn 2025 file contains no marker or blank in any LA cell of any year. The new loader halts on one instead.

## The zeros are published zeros (open item 1 of the decisions index, closed)

The index carried "S10 suppression markers unverified": 22 and 27 zeros with no NULL, the signature of S1's defect. It is
settled from the source, as S1 was:

- **Markers are defined** in the file's shorthand line on Table_1_Total: `[x]` Not Available, `[z]` Not Applicable and
  `[n]` No data available as the authority was created through reorganisation.
- **None appears at LA level in any year 2010 to 2025.** The loader reads every LA cell of all 16 year columns of the
  autumn 2025 file and finds a whole number in each (no marker, blank, text, negative or fraction). The
  autumn 2024 file agrees (below).
- So the zeros are counts: 15 authorities in 2025 and 12 in 2024 reported no one sleeping rough on the snapshot night.
  They stay 0. The earlier figures of 22 and 27 were the zeros of the wrong values then held (2021 and 2020).
  The table has no `value_flag` and needs none.
- The loader keeps the rule: a marker, blank or non-integer in an LA cell of a loaded year halts and names the cell and
  the marker's meaning from the file. Flag columns would be a follow-up if a marker ever appeared.

## The proof run

`migrate-legacy data/raw/s10_rough_sleeping/Rough_sleeping_snapshot_in_England__autumn_2025.ods` (sha256 starting
7b7ed536d0d51f83), one transaction:

- Identity from the file: Cover "Annual rough sleeping snapshot in England: autumn 2025", "Publication Date: 26th
  February 2026", "Autumn 2010 to autumn 2025", next release "Winter 2026/2027"; Table_1_Total title "... 2010 - 2025".
- The parser reproduces every held value: 296 authorities matched by `lad24cd`, 592 values, **0 differences**.
- Reconciliation in the file is exact: the authorities sum to the England row and to every region row, and Rest of
  England equals England less London, for 2024 (4,667) and 2025 (4,793).
- Barnsley and Sheffield are E08000038 and E08000039 in the file and E08000016 and E08000019 in the table; the
  loader resolves them through `geography.resolve` (source 10 is `new`, so the old codes would halt).
- Edition 1 "as loaded": 296 rows, `published_date` 2026-08-19 (the load date), `source_file`
  `as loaded: Rough_sleeping_snapshot_in_England__autumn_2025.ods; autumn 2025; published 2026-02-26`, content hash equal
  to the before-state line above.

## No back-series revision between the autumn 2024 and autumn 2025 files

The two files agree on every LA cell for 2010 to 2024: 296 authorities by 15 years, 4,440 cells, 0 differences, same 296
codes on both sides after the Barnsley and Sheffield recode (a throwaway read-only check, not committed). So the publisher
has not revised the back series between these two releases. A file restates every year from 2010, so the loader compares
each held year in a new file against the held edition and would catch one; any change to a held year needs
`--acknowledge YEAR`.

## The autumn 2024 layout

`Rough_sleeping_snapshot_in_England__autumn_2024.ods` (sha256 starting ed47513a695d1892; kept unchanged in
`data/raw/s10_rough_sleeping`) has no Cover sheet (its note says "Last Update: 27 February 2025") and its Table_1_Total
header is shifted: the column headed "Local authority" holds the codes and "Local authority ONS code" holds the names. It
also carries Barnsley and Sheffield as E08000016 and E08000019. The loader halts on the header check, naming what it saw
(`load --file` of it exits 1, nothing stored). Back-filling 2010 to 2024 is out of scope: it would need a deliberate header
reading and each earlier year would be its own new period. Back-filling a year before the newest held year is treated as an
older file and needs `--allow-older-file`.

## What was written (2026-10-10)

- `ddl --commit`: `la_rough_sleeping_editions` (append-only; triggers `s10_editions_immutable` and
  `s10_editions_no_truncate` refuse UPDATE, DELETE and TRUNCATE) and `la_rough_sleeping_editions_file_checks`. No change
  to the live table.
- `migrate-legacy ... --commit` (after a preview and a `--simulate` that rolled back, both equal to Task 2's read-only
  preview): edition 1 "as loaded" for 2025, 296 rows; one ledger row for the file (outcome `unchanged`; final URL
  `https://assets.publishing.service.gov.uk/media/699daa5807d7bff3604d6c20/Rough_sleeping_snapshot_in_England__autumn_2025.ods`).
  Run-log row 364, 296 rows. Live untouched.
- `load` preview: the page's file is the held one by (URL, sha256), nothing parsed; GOV.UK lists no release newer than
  autumn 2025. `load --recheck 2025` and `load --file ...autumn_2025.ods --recheck 2025` previews: unchanged, 0 areas
  changed against edition 1. `load --file ...autumn_2024.ods` preview: HALT at the header check. So no `load --commit` was
  made. `refresh-latest` preview: nothing to write. `status`: OK, nothing to do.
- `scripts/s10_rough_sleeping_editions_verify.py`: all gates PASS, exit 0. No `zz%` table remains.
- W1, `refresh_map.py`, the export, `push.py` and `git push` were not run. No W1 value changed.

## Winter 2026/27 (the next release; the file says "Winter 2026/2027")

From `ONS_Population_Estimates`:

1. `python scripts/s10_rough_sleeping_editions.py load` finds the newest "Rough sleeping snapshot in England: autumn
   <yyyy>" release in the Homelessness statistics collection and its one "... - tables" attachment, downloads it to
   `data/raw/s10_rough_sleeping` (also in a preview; the database is not written) and previews. Read the whole preview.
   `--release YYYY` or `--file PATH` take other files with the same identity and older-file checks. Expect a new period
   2026 and every held year (2025) compared and unchanged.
2. `load --commit`, then `refresh-latest` (preview, then `--commit`), `status`, and
   `python scripts/s10_rough_sleeping_editions_verify.py` (exit 0). **Run `refresh-latest` before W1:** it copies the
   edition's `loaded_at` onto the live rows, so a W1 run made before it would not carry the revision, and
   `refresh_map.py --check` (which compares tables' `loaded_at` with the last run) is the safeguard. S10 is a W1 input
   (national aggregates and LA signals read the newest year).
3. What a halt means. The loader halts, naming what it saw, rather than guess; exit 1, nothing stored, no ledger row:
   - A header or layout change (the autumn 2024 file had one): fix with a deliberate reading and the evidence, then re-run.
   - A marker, blank, text, negative or non-integer in an LA cell of a loaded year: name the cell and its meaning from the
     file; decide how to store it (rule 1) before loading. Never read as 0.
   - Reconciliation failing (authorities not summing to England or a region), identity failing (Cover year not equal
     to the Table_1 title year or the page), an unknown code (UNEXPLAINED), Barnsley or Sheffield in the wrong form for the
     period (source 10 is declared `new`; if the 2026 file differs, change `geography.DATASET_FORM` with evidence).
   - A collection, page or attachment title that no longer matches: discovery lists what it saw. Use `--file PATH` if the
     tables are there.
   - REJECTED by a stop threshold: for a new period, a national total moving over 30% or an authority over 150; for a
     revised held year, a total over 1% or over 10 authorities, and in practice any change at all needs `--acknowledge
     YEAR`; fewer than 296 authorities or a held authority missing (`PARTIAL FILE`) is never released. A revision of a
     back year has never happened.
4. A file with the held file's rank but different content stops unless `--accept-reissue YEAR`.

## How to reverse

`python scripts/s10_rough_sleeping_editions.py restore-edition PERIOD N` (preview by default) stores edition N's rows as
the next edition; `refresh-latest --commit` then applies it to the live table. To return 2025 to the held state, restore
edition 1 and refresh. Nothing is ever deleted from the editions tables.
