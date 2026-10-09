# S22 Council Taxbase and Live Table 615: first load

Status: done 2026-10-09. Works like [S4](2026-10-09-s4-editions-first-load.md) and
[S11](2026-10-09-s11-editions-first-load.md). Loader: `scripts/s22_ctb_editions.py` (three editions specs in one
module); gates: `scripts/s22_ctb_editions_verify.py` (24 gates, all PASS, exit 0). Design:
`docs/superpowers/specs/2026-10-09-s22-editions-design.md` (audit record, not approval).

No held value changed. The three live tables are exactly as they were, apart from a new `null_reasons` column
(NULL in every row). The map is unchanged.

## Before-state (read-only, confirmed against the live tables before the migration)

Each hash is the md5 of every live row as jsonb, without `loaded_at` and `null_reasons`, ordered by key and period.
They are the loader's `LEGACY_LIVE`; edition 1 of each table hashes to the same value, which gate 18 checks.

before-state hash (ctb): 31ca63b26dc8041cad663380dea77c66
before-state hash (classes): 983c3b130d7c6d5c878d9c85a82e73be
before-state hash (615): 27ffb076aa646811f998538d7745379f

- `la_council_taxbase_empties` 296 rows (2025), `la_ctb_exemption_classes` 3,256 (11 classes x 296),
  `la_vacant_dwellings_615` 7,170 (2004-2025). One `loaded_at` in all three: 2026-08-13 01:19:03.996 UTC.
- NULL and 0 counts: CTB no NULL, `empty_homes_premium_count` 0 in 5 authorities; classes no NULL, `dwellings` 0 in
  537 rows; 615 `vacant_dwellings` 1 NULL (Wandsworth 2004, published `[x]`) and 5 zeros,
  `long_term_vacant_dwellings` 27 zeros, `lad24cd` NULL in 891 rows (80 abolished district codes, kept
  `unmapped`).
- Latest W1 run (run 25, `staging_la_signals`): 296 rows; md5 of the five `ctb_*` columns per authority
  `e83631c0c53dcf44053cdf7ed75e2abe`.
- After the migration the same checks give the same row counts, hashes and one `loaded_at`; the only difference is
  `null_reasons`, NULL in all 296 + 3,256 + 7,170 rows.

## What the old build did

Four scripts, committed together in 8c472a1 on 2026-08-13: `s22_ctb_discover.py`, `s22_ctb_empties_build.py`,
`s22_run.py`, `s22_verify.py`.

- It was written to overwrite: `INSERT ... ON CONFLICT DO UPDATE ... loaded_at = now()` on all three tables. It
  had no preview and no `--commit` (it wrote when run), kept a same-named raw download whatever its content, and
  took file identity from the landing page rather than the file.
- **It ran once.** Run-log row 83, status `complete`, 2026-08-13 01:18:50 to 01:25:04 UTC, 10,724 rows written
  (296 + 3,256 + 7,170 + 2). Every held row carries one `loaded_at`, 2026-08-13 01:19:03.996 UTC, so nothing has
  been rewritten since. No earlier S22 load existed, so **nothing was overwritten in practice**; the code
  would have overwritten on a second run.
- `build_reports/s22_build_state.json` records gate 4 (a second full load in a second transaction, sums compared)
  as PASS with identical sums before and after.
- **The code that ran differed in small ways from the code committed.** The committed `s22_verify.py` writes status
  `success`, not `complete`, and gate 4's second transaction would have given the rows a later `loaded_at`
  than the one they hold. So the wording is: the old build upserted by design; the held data comes from its single
  run of 2026-08-13 and has not been rewritten since.
- `ctb_series_breaks` and `v_la_empty_homes_rates` came from the same run and are not touched by this work.

## F4: the latent rule 1 defect, and why no value changed

The old class total was built with `unocc += int(v) if isinstance(v, (int, float)) else 0` and the England target
with `... or 0`: a suppressed or blank class cell would have become 0 inside a total (rule 1.4, 1.7). It never
fired: in the held 2025 workbook every cell of the 11 unoccupied class columns is a published integer for all 296
authorities and England (many are genuine zeros), `la_ctb_exemption_classes` has no NULL `dwellings`, and the 296
totals equal the class sums. The new loader makes `unoccupied_exemptions_total` NULL unless all 11 classes are
published, `empty_under_6_months` NULL unless both parts are, and writes each reason to `null_reasons`. Code
fix, no data change.

A consequence to know about: a suppressed `[x]` cell in a used CTB column makes the authorities fail to sum to the
England row, so `load` halts at the reconciliation. That is deliberate; a suppressed cell then needs a decision
before it is loaded.

## The proof run

`migrate-legacy` with the two held files (sha256 starting 9fd74444 and 16d404d2), in one transaction:

- The new parser, on the held files, reproduces every live cell: 2,368 CTB cells, 6,512 class cells and 35,850
  Table 615 cells, **0 differences**.
- Dacorum (E07000096) 2012 is published as 1415.57798165137 and 494.577981651376 on `All_vacants` and
  `All_long_term_vacants`, and is held rounded half up as 1416 and 495. England and the East region carry the same
  fraction; the file gives no reason. It is the one declared non-integer (`NON_INTEGER_HELD`); any other
  non-integer halts.
- The authorities sum to England exactly in every used CTB column and in every Table 615 year on both sheets.
- Table 615 per-year rows match live: 353 (2004), 355 (2005-2008), 326 (2009-2018), 317, 314, 309, 309, then 296
  for 2023-2025.
- Barnsley and Sheffield: the CTB 2025 uses E08000038/39; 615 carries E08000016/19 numbers 2004-2024 and
  E08000038/39 numbers in 2025. Both resolve through `geography.resolve` and the `la_code_lookup` recode under
  rule 4.5. 615 note 10 says the two districts' boundaries were changed on 1 April 2025; this note does not call
  them "the same area". The 80 abolished 615 districts stay `unmapped` with `lad24cd` NULL, never mapped to
  successors.

## The January 2026 revision of the 2025 workbook

The 2025 workbook was first published on 6 November 2025 and revised on 21 January 2026: "corrections to data
from 22 authorities", England level changed "by less than 1%". Eighteen authorities carry `[r]` on the tables
used. The held workbook (9fd74444) is the revised one. **The November file cannot be recovered** (its URL, a
different file name, now answers 301 to the revised file, and no archive holds it), so the size of the change per
cell is not measured. The ledger records the final URL after redirects and the sha of the bytes read.

| Compared | What changed |
|---|---|
| CTB 2025, November 2025 to January 2026 | 22 authorities corrected, England under 1%; original not recoverable, so per-cell size unknown |
| 615, June 2025 to January 2026 | 2025 column added (860 cells); 2004-2024: 0 cells changed (6 Dacorum 2012 cells differ only below the ninth decimal) |
| 615, January 2026 to June 2026 (held) | 0 cells changed on the two sheets loaded (the June update revised other sheets) |

The CTB cover says "No revisions have been made to previous years", so the registry's `revises_back_series = true`
stands for the latest year only. Table 615's two loaded sheets follow the CTB; a back-year change would be stored as
a new edition.

## Table 615 and the CTB are reconciled (a documentation correction)

Several documents say the CTB and Table 615 "use different definitions and different snapshot dates, and are not
reconciled". The publisher says the opposite. 615's cover defines October 2025 all-vacants as Line 15 plus the
exemption classes B, D to L and Q on the CTB form, "or tables 1.18 and 2.01", and long-term vacants as Line 18,
"or table 1.22". The 2025 column is dated 06/10/2025, the CTB snapshot. Checked in the migration proof: 615 2025
all-vacants equals CTB `empty_total + unoccupied_exemptions_total` in **297 of 297** rows (296 authorities and
England; the loader's authority-level proof reports 296 of 296).

The wrong sentences were not corrected in the commit that loaded this. They were corrected afterwards, in the commit
`refactor: retire the old S22 build; docs, registry, RULES`, in the places below (and `docs/S22_BUILD_SUMMARY.md`'s
refresh procedure and geography sentence were corrected with them):

- `docs/METHODOLOGY.md` line 83
- `docs/S22_BUILD_SUMMARY.md` line 139 and its copy `outputs/S22_BUILD_SUMMARY.md` line 139
- the registry caveat in `scripts/backfill_source_registry.py` (about line 1152)

## The long-term-empty measure (Scott's decision D1)

The map's "Long-Term Empty Rate" and "Dwellings empty 6+ months" use CTB table 1.19 (Line 16, every dwelling
classed as empty for more than six months). MHCLG's own "long term vacant" figure (615, dwelling stock release) is
Line 18 (table 1.22), which excludes empties on the class D discount and flood empties. England: 309,889 here
against 303,185 for MHCLG; 179 of 296 councils differ. The label describes Line 16 accurately.

**Recorded ruling: option (a), keep the map's measure as it is.** Nothing changed in W1, the view or the map. The
source documentation will say how the measure differs from MHCLG's. Option (b), a new Line 18 column read by the
view and W1, was not taken and can follow if Scott asks.

## What was written (2026-10-09)

- `ddl --commit`: new tables `la_council_taxbase_empties_editions`, `la_ctb_exemption_classes_editions`,
  `la_vacant_dwellings_615_editions` (append-only, triggers refuse UPDATE, DELETE and TRUNCATE) and the ledgers
  `la_council_taxbase_empties_editions_file_checks` and `la_vacant_dwellings_615_editions_file_checks`; new
  column `null_reasons text` on the three live tables.
- `migrate-legacy ... --commit` (after a preview and a `--simulate` that rolled back, both equal to the earlier
  read-only preview): edition 1 "as loaded" for CTB 2025 (296 main rows and 3,256 class rows) and for the 22
  Table 615 years (7,170 rows); ledger rows for both files (1 for the CTB workbook, 22 for the Table 615 file,
  outcome `unchanged`). Run-log row 270, 10,722 rows. Live untouched.
- `load` and `load-615` previews: both files are already in the ledger by (final URL, sha256), nothing to parse.
  `load --recheck 2025` and `load-615 --recheck 2025`: unchanged, 0 areas changed against edition 1. So no
  `--commit` of `load` or `load-615` was made. `refresh-latest` preview: nothing planned. `status`: OK.
- The raw files are kept in `data/raw/s22_ctb` (git-ignored): `2025_Local_Authority_Drop_Down.xlsx`,
  `Live_Table_615.ods`. Gate 19 finds them by sha256 and re-reads them. The two `Tables_1_-_5_2025*.ods` files
  beside them are not loaded.
- W1, `refresh_map.py`, the export, `push.py` and `git push` were not run. No W1 input value changed;
  `refresh_map.py --check` should still say the map is current for S22 (not run here).

## Each November (CTB)

From `ONS_Population_Estimates`:

1. `python scripts/s22_ctb_editions.py load` finds the newest `Council Taxbase <yyyy> in England` release in the
   GOV.UK collection and its one local authority level attachment, downloads it to `data/raw/s22_ctb` (also in a
   preview; the database is not written) and previews. Read the whole preview. Stop conditions are enforced in
   `load` (REJECTED, exit 1). `--release YYYY` or `--file PATH` take other files, with the same identity and
   older-file checks.
2. `load --commit`, then `refresh-latest` (preview), `refresh-latest --commit`, `status`, and
   `python scripts/s22_ctb_editions_verify.py` (exit 0).
3. A revised file for the same year (January to May) is read the same way: if it differs it is stored as the next
   edition (main and classes together) and reaches the live table only through `refresh-latest`, which copies the
   edition's `loaded_at` so `refresh_map.py` sees it.
4. A 0 to NULL or NULL to 0 flip against the tip needs `--acknowledge YEAR`.

## Each Table 615 update

`python scripts/s22_ctb_editions.py load-615` (preview), `load-615 --commit`, `refresh-latest`, `refresh-latest
--commit`, `status`, verify. 615 is re-issued two or three times a year (next update November 2026 to February
2027); every year from 2004 is read, a changed back year is a new edition of that year, a new year is a new
period.

## What November 2026 brings

Council Taxbase 2026 in England arrives in November 2026: `taxbase_year` 2026 is a new period (edition 1), and it
becomes W1's year, so **W1 and `refresh_map.py` are run then**, not before. A revised 2026 file may follow
between January and May 2027. Table 615 gains a 2026 column. Expect Barnsley and Sheffield as E08000038/39 in both.
Table numbers inside the workbook can shift; the parser finds each block by number and title wording and halts
naming what it saw, and the block map is then corrected deliberately.

## How to reverse

`python scripts/s22_ctb_editions.py restore-edition PART PERIOD N` (preview by default) stores edition N's rows as
the next edition; `refresh-latest --commit` then applies it to the live table. To return a period to the held
state, restore edition 1 and refresh. Nothing is ever deleted from the editions tables.

## Notes

- The old scripts (`s22_ctb_discover.py`, `s22_ctb_empties_build.py`, `s22_run.py`, `s22_verify.py`) were not retired
  in the commit that loaded this; they were retired afterwards, in the commit `refactor: retire the old S22 build;
  docs, registry, RULES` (now in `scripts/historical/`, each with a RETIRED guard and a subprocess test).
- The 615 row-count lines `ROW COUNT 2004: 353 rows, expected 326` printed by `status` before the migration were
  the engine's generic expectation of the 2009-2018 count; they are real year-to-year differences in the number of
  published districts, and `status` reports OK after the migration.
- `status` on a fresh database says "run sync-new" before `migrate-legacy`; that wording is the engine's, and
  S22 has no `sync-new`.
