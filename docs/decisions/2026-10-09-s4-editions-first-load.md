# S4 care leaver accommodation: first load

Status: done 2026-10-09. Works like [S11](2026-10-09-s11-editions-first-load.md),
[S9b](2026-10-09-s9b-editions-first-load.md), [S9a](2026-10-09-s9a-editions-first-load.md),
[S18](2026-10-09-s18-editions-first-load.md) and [S15](2026-10-08-s15-editions-first-load.md). Loader:
`scripts/s4_care_leaver_editions.py`; gates: `scripts/s4_care_leaver_editions_verify.py` (23 gates, all PASS,
exit 0). Design: `docs/superpowers/specs/2026-10-09-s4-editions-design.md` (audit record, not approval).

## D1 decision (Scott, 2026-10-09)

Scott chose option (a): the map uses DfE's published category, `semi_independent_published`, not the pipeline
aggregate `semi_independent`. This was recorded here before `refresh-latest --commit`. The W1 SQL change is a
separate task (not made here; W1 was not run).

before-state hash: 96ad4b486af5b6c9c12d3152066b406f

(md5 of every live row as jsonb, without `loaded_at` and `null_reasons`, ordered by key; 1,481 rows. It is the
loader's `LIVE_HASH`; edition 1 of every year hashes to it, which gate 17 checks.)

## What the old builds did

- **n8n S4 workflow, 2026-03-31.** Two run-log rows (ids 25 and 26, 13:47 and 13:48 UTC, 1,413 rows each: the
  data was loaded twice). It inner-joined `la_code_lookup`, so every county council was dropped, and it kept
  only the last age it saw for 22-25. It upserted with `ON CONFLICT DO UPDATE`.
- **`scripts/verify/rebuild_care_leavers.py`, 2026-08-20.** Run at least twice that day. The live `loaded_at` of
  1,083 17-21 rows is 14:07 UTC, while the backup `care_leaver_accommodation_bak_20260820` carries 09:12 UTC, so
  the backup is the state after the first run, not the n8n state; the pre-rebuild table is not preserved
  anywhere. It built 17-21 only, added suppressed cells as zero (`n = v or 0`), resolved codes with a private
  dict of every `la_code_lookup` row, and upserted over the 17-21 rows. It wrote no run-log row.
- So the old builds overwrote by design: the 17-21 rows were overwritten on 2026-08-20; the 22-25 rows were
  not touched after 2026-03-31 (all 396 `loaded_at` 2026-03-31 13:48 UTC). Two 2020 rows (E06000063/64)
  still carried the 2026-03-31 time.

## The five breaks and their sizes

1. **Rule 1, 17-21.** About 7,700 cells in 1,076 of the 1,085 rows held a number where rule 1 gives NULL (zero
   for suppressed cells, or a sum of the known parts). Where rule 1 gives a number it equals the held value in
   every one of 5,289 cells.
2. **22-25 held age 25 only.** All 396 rows equalled the file's "25 years" row (Liverpool 2025: 117 held, the
   22-25 total is 634). 22 county councils a year were missing.
3. **2020 was a superseded release** (taken from the 2023 release; the 2024 release revises it).
4. **Bournemouth and Poole 2019 were lost** to the private BCP recode (held 2019 BCP was all zeros).
5. **Not-applicable rows stored as zeros** (an authority in a year it did not exist: Cumbria 2024-25,
   Northamptonshire 2022-25, the Northamptonshire successors before they existed, Dorset 2020), and the
   hand-written attribution notes said "zero afterwards".

## How the correction was proved

`migrate-legacy` with the three files the old builds read (sha256 starting 6df781a4, 32d76290, 0c52e756),
inside one transaction, stopping and rolling back on any failure:

- 5,289 non-NULL corrected 17-21 cells equal the held cells, 0 differences.
- 396 of 396 held 22-25 rows equal the file's age-25 row.
- Every key the correction drops is all zero held and all `z` in the file, or one of the three declared
  exceptions (E06000063/64 2020 stale n8n rows; E06000058 2019). Every key it adds is a 22-25 county council or
  E06000028/29 2019. Keys dropped / added per year: 2019 4/2, 2020 5/0, 2021 4/0, 2022 3/0, 2023 5/22,
  2024 2/21, 2025 2/21.
- The real preview equalled the earlier read-only preview in everything but the time.

Cells held as a number that the correction makes NULL: community_home 806, foyers 612, independent_living 461,
not_known 721, other 1,042, semi_independent 895, semi_independent_published 157, suitable_count 98,
supported_lodgings 664, total_care_leavers 1,427, total_published 5, unsuitable 998, with_family 341.

## What was written

- New tables `care_leaver_accommodation_editions` (append-only, triggers refuse UPDATE, DELETE and TRUNCATE)
  and `care_leaver_accommodation_editions_file_checks` (ledger, 11 rows). New live column `null_reasons`.
- Editions: edition 1 "as loaded" (exactly the 1,481 held rows) and edition 2 (the correction) for every year
  2019-2025, plus edition 3 for 2020 (15 year-editions, 3,154 rows).
- **2020 revised from the 2024 release** (edition 3): 10 rows changed against edition 2, 23 cells in 10
  authorities; `total_published` changed in 3 authorities (E06000056 192 to 191, E08000032 371 to 372,
  E09000016 208 to 207). Four cells were 0/NULL flips (rule 1.10), for example E06000003 `unsuitable` NULL to 0
  and E09000001 `community_home` 0 to NULL. They are the publisher's own revision between releases, so they were
  acknowledged with `--acknowledge 2020`. 2021-2024 were skipped as older than their tip (the 2025 release).
- Release 2025 (version 8c28aca0-6ab9-400b-b7de-c5fc7a148c2e, published 2025-11-26, updateCount 2; datasets
  a504e4b8 and bd5240e0): both files already in the ledger, nothing to store; `load --recheck 2025` found it
  unchanged.
- `refresh-latest --commit --accept-key-changes 2019 ... 2025`: 1,449 rows refreshed, 66 keys inserted and 25
  deleted, before/after guard passed. The keys equalled the migration preview's list exactly. Live `source`
  is now the release's data guidance URL, uniform per year, and `loaded_at` is the edition's.
- Run-log rows 244 (migrate-legacy, 3,003 rows) and 245 (release 2024, 151 rows).

## Live counts after

| Cohort | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 17-21 | 152 | 151 | 151 | 152 | 152 | 153 | 153 |
| 22-25 | | | | | 152 | 153 | 153 |

Total 1,522 (1,064 + 458), as the spec expected. County councils: 24, 23, 23, 22, 22, 21, 21 in 17-21 and 22,
21, 21 in 22-25. Bournemouth and Poole hold their own 2019 figures; every held code is in `la_boundaries`,
`utla_lad_mapping` or a declared predecessor.

## Cells now NULL (live, NULL count / published-zero count)

| Column | 17-21 | 22-25 |
|---|---:|---:|
| community_home | 809 / 32 | 458 / 0 |
| foyers | 615 / 305 | 458 / 0 |
| independent_living | 462 / 8 | 458 / 0 |
| not_known | 552 / 52 | 258 / 2 |
| other | 1,043 / 12 | 458 / 0 |
| semi_independent | 898 / 7 | 458 / 0 |
| semi_independent_published | 159 / 13 | 458 / 0 |
| suitable_count | 1,064 / 0 | 373 / 5 |
| supported_lodgings | 665 / 45 | 458 / 0 |
| total_care_leavers | 1,057 / 7 | 441 / 2 |
| total_published | 5 / 7 | 10 / 3 |
| unsuitable | 866 / 188 | 440 / 14 |
| with_family | 343 / 8 | 458 / 0 |

(Before: for example 17-21 `foyers` 805 zeros and 2 NULL; `unsuitable` 941 zeros and no NULL.) The zeros that
remain are published zeros. Nine live 22-25 rows have every count NULL (City of London 2023-25, Rutland 2024-25,
and North and West Northamptonshire 2023-24): DfE publishes them with every figure suppressed or not
available, each carries its `null_reasons`, and they are stored, not dropped (rule 1.6 drops only rows that are
all `z`).

## W1 effect (D1 option a)

Not run here. Run 25's `care_leavers_semi_indep` has 132 values (4 of them 0) for the mapped authorities. The
2025 values now in live, for the 296 mapped authorities:

- `semi_independent` (what the unchanged W1 SQL reads): 17 non-NULL (1 zero). The aggregate is NULL whenever
  one of its six DfE parts is suppressed in either age band (rule 1.7), so an unchanged W1 would show 17.
- `semi_independent_published` (what D1 (a) reads): 121 non-NULL (1 zero). Of run 25's 132 values, 49
  authorities show the same number under the published category; 83 differ (a lower published number, or NULL).
  Liverpool: 208 in run 25 (the aggregate), 188 published now.

So the next W1 run changes `care_leavers_semi_indep` for most authorities, and the map's care leaver figure
becomes a different, defined quantity. `refresh_map.py` will see `care_leaver_accommodation` as loaded after run
25 (`loaded_at` now 2026-10-09). The W1 SQL change (`05_la_signals.sql`, `w1_contract_check.py`, the tenant
label, the data dictionary) is Task 4.

## The engine addition

`refresh-latest` can now add and remove keys when a spec opts in (`refresh_key_changes`, S4 only) and each year
is named with `--accept-key-changes YEAR` (repeatable); the six loaders already on the engine are unchanged.
A removed live row stays in its earlier editions.

## Each November

From `ONS_Population_Estimates`:

1. `python scripts/s4_care_leaver_editions.py load` finds the latest release and both dataset ids from the
   content API and the release's data guidance page, downloads both files (to `data/raw/s4_cla`), and previews.
   Nothing is written to the database. Read the preview. Stop conditions are enforced in `load` (REJECTED,
   exit 1). Rule 1.10 0/NULL flips (about 100 cells a release) need `--acknowledge YEAR`.
2. `load --commit`, then `refresh-latest` (preview), then `refresh-latest --commit` with `--accept-key-changes`
   for each year whose keys change. `status` says OK; `s4_care_leaver_editions_verify.py` passes all gates.
3. **November 2026** (reporting year 2026, `nextReleaseDate` 2026-11): 2026 is a new year (edition 1), 2022-2025
   are republished (revised editions, with the 2025 release's years becoming older than the tip), dataset ids
   and the header schema may change (the loader halts and lists the columns), and Barnsley and Sheffield may
   arrive as E08000038/39 (the loader halts; `DATASET_FORM['4']` is then corrected deliberately). 2026 becomes
   W1's year.
4. If the page's embedded JSON changes shape, use `--dataset-17-21 ID --dataset-22-25 ID --release YYYY` (file
   checks still apply).

## How to reverse

`restore-edition YEAR N` (preview by default) stores edition N's rows as the next edition ("restored from
edition N"); `refresh-latest --commit --accept-key-changes YEAR` then applies it. To go back to the pre-correction
state for a year, restore edition 1 (the held rows exactly) and refresh. Nothing is deleted from the editions.
The backup table `care_leaver_accommodation_bak_20260820` was not touched.

## Notes

- `scripts/verify/rebuild_care_leavers.py` is not retired here (a later task). W1, `refresh_map.py`, the
  export, `push.py` and `git push` were not run.
- `status` on a fresh database says "run sync-new" before `migrate-legacy`; that wording is the engine's, and S4
  has no `sync-new`.
- Two small fixes to the verify script were made while running it on the real tables: the decision note path
  pointed at the wrong directory, and gate 5 counted a row with every count NULL as a failure even when
  `null_reasons` explains it (the nine rows above are real, published-as-suppressed data). An all-NULL row
  with no `null_reasons` still fails.
