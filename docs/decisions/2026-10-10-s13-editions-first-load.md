# S13 MHCLG LAHS housing register: first load on the editions engine

Status: done 2026-10-10. Works like [S23](2026-10-09-s23-editions-first-load.md) and [S10](2026-10-10-s10-editions-first-load.md).
Loader: `scripts/s13_lahs_editions.py`; gates: `scripts/s13_lahs_editions_verify.py` (all PASS, exit 0).
Design: `docs/superpowers/specs/2026-10-10-no-loader-sources-design.md` (audit record, not approval).
Scott authorised the real writes through the plan ("do the work, the spec is audit not approval") and ruled blanks, not
zeros, for Allerdale's housing-register zeros (his standing blanks ruling). The rest is listed below for him to overrule.

**No value W1 reads changed.** 2025 `households_on_register` is identical before and after for all 296 authorities and the
England 2025 sum is 1,340,527 both times. What changed is the history: edition 2 corrects 2015 to 2023 (predecessor sums,
Allerdale) and fills `reasonable_preference` for every year. The live table `la_housing_register` has the same columns,
constraints and 3,256 rows; 3,241 rows were refreshed from edition 2. W1 and `refresh_map.py` were not run (Scott deferred
W1 and the map until every source is done). `refresh_map.py --check` will show this table as loaded after the last W1 run,
and only through the `loaded_at` of the 3,241 refreshed rows; no value W1 reads moved.

## Before-state (read-only, confirmed against the live table before anything was written)

Confirmed first: 3,256 rows, 11 `reporting_year` values (2015 to 2025, 296 each), two `loaded_at` values (3,253 at
2026-04-01 01:25:26.774235 UTC; 3 at 2026-08-20 08:17:46.988105 UTC), `households_on_register` 5 NULL and 1 zero,
`jointly_managed_register` 312 NULL, `reasonable_preference` 3,256 NULL, survey hash `cf73fea93da41c4b3a0461754b27b448`
(the spec's definition: every column except `loaded_at`, `coalesce(col::text,'~')`, joined by `|`, rows by LF, ordered by
period then key). England 2025 `households_on_register` 1,340,527.

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a
64-hex run, stays untouched. The `before-state` lines are the verify script's `live_columns_sha` per year (`lad24cd`, the
three values and `source`, sorted); the `w1-read` line is `w1_read_sha` over `(lad24cd, households_on_register)` for 2025.
They were printed by `python scripts/s13_lahs_editions_verify.py --print-note-lines` before `ddl` and before the
migration, while the live table was untouched. Edition 1 (as loaded) hashes to the `before-state` lines (gate 16) and the
live 2025 households still hash to the `w1-read` line (gate 17).

before-state la_housing_register 2015 rows=296 sha256-first32=8a720083bf31cb38803f0e7348b481b4 sha256-last32=634a4a3d482ad3dd9b86792d3b4e7c43
before-state la_housing_register 2016 rows=296 sha256-first32=1b3dbc5e0587e148d5342663dee5d3fa sha256-last32=40fc03588b38c6987d5f80a08d642b7b
before-state la_housing_register 2017 rows=296 sha256-first32=572f3352cd3a24859412ba2e98820561 sha256-last32=9d6f410c6b45a42677ad8eb43fbd3357
before-state la_housing_register 2018 rows=296 sha256-first32=67ac3b5e57563f11ad4f24c2eda8d054 sha256-last32=367b23ff53b9301de9c50d83f36674df
before-state la_housing_register 2019 rows=296 sha256-first32=5a09173432b282767ca99c791fced9e7 sha256-last32=7409e234119cdaffb2d4654f3ae0f91a
before-state la_housing_register 2020 rows=296 sha256-first32=f649922b622e1c315e04939a6d237afc sha256-last32=72f617b08f7dbb47921c811df4ba1188
before-state la_housing_register 2021 rows=296 sha256-first32=ca382e0642b67f4974dbea9774e33cef sha256-last32=828172096921a4089be7fcc4291862b4
before-state la_housing_register 2022 rows=296 sha256-first32=023e09ec14ac12283b12c9f91d2ddb92 sha256-last32=f82515a65ca90fed7d2683e01f4eec5e
before-state la_housing_register 2023 rows=296 sha256-first32=e9f1a6150822b6f4209edae35fe3e4fa sha256-last32=82435e16913643411672d198228ffa3f
before-state la_housing_register 2024 rows=296 sha256-first32=4d1ab787133725ca8c9d5fa0d399a89e sha256-last32=2869028e815b12411dd5f07e789ccc05
before-state la_housing_register 2025 rows=296 sha256-first32=a69a899bf1ca418e53045409620493ec sha256-last32=df098074923614ea808b2ac1acfca80c
w1-read la_housing_register 2025 rows=296 sha256-first32=3a6111bc8f00a22b0f9e74a2477ba57a sha256-last32=d5b2f6e6c8b80d3900d6f1f2aa93d6d4

## How the table came to hold what it holds (checked 2026-10-10 against the database, the run log and the docs)

- **2026-04-01, run-log row 28** ("Source 13 - LAHS Social Housing Waiting Lists", 3,471 rows written, 296 authorities,
  "MHCLG LAHS Section C, housing register totals 2014-15 to 2024-25"): the n8n workflow upserted a CSV converted from the
  publisher's file in a chat session that no longer exists (the February 2026 open data file, which the repo holds as
  `data/reference/LAHS_open_data_1978-79_to_2024-25.csv`, sha256 starting addaa0035e2fdc97). 3,253 live rows carry that
  `loaded_at`.
- **The `DISTINCT ON` predecessor defect.** The workflow joined the rows to `la_code_lookup` and kept `DISTINCT ON
  (new_code, year)`, so for a reorganised authority one arbitrary predecessor row survived, not the sum. The CSV has 3,471
  authority rows for 2014-15 to 2024-25; the table holds 3,256 (296 x 11). 76 keys (11 authorities, 2015 to 2023) held one
  predecessor's figure. **Worked example, Bournemouth, Christchurch and Poole 2015:** the three predecessors are Bournemouth
  (E06000028), Poole (E06000029) and Christchurch (E07000048). The table holds row 3, Poole's 946, as the 2015 figure for
  BCP; the sum of the three is the figure BCP should have. The proof run reproduces all 76 such keys, and for 57 of them
  the surviving row is not the first in file order (n8n's order within the join cannot be reproduced), so edition 1 stores
  live as held.
- **2026-08-20, three rows set by direct SQL** (loaded_at 2026-08-20 08:17:46 UTC): Bromley (E09000006), Hillingdon
  (E09000017) and Mansfield (E07000174), 2025, to the 25 June 2026 revision of the 2024-25 return. No run-log row. The
  proof run reproduces them from the June 2026 file (sha256 starting 6b5f0105d34488c9), not the February one.
- **2026-09-30, Telford's NULLs by direct SQL:** `households_on_register` for Telford and Wrekin (E06000020) 2022, 2023
  and 2025 set NULL (2024 was already NULL in the file), with a reason appended to `source`
  ([2026-09-30-telford-no-housing-register.md](2026-09-30-telford-no-housing-register.md); backup
  `la_housing_register_bak_20260930`, and `_bak_20260820` from the August change). The proof run reproduces the three NULLs
  under the named rule `TELFORD_NO_REGISTER`, and the `source` text verbatim.
- The reference file `data/reference/lahs_waiting_list_2015_2025.csv` is a copy of what was published and is not touched
  (see [2026-09-30-reference-csvs-out-of-step.md](2026-09-30-reference-csvs-out-of-step.md)).
- The n8n workflow is retired (Task 1 of the plan): its Code node now throws. The repo had no S13 code before
  `scripts/s13_lahs_editions.py`.

## Dorset: five rows added to `la_code_lookup` (2026-10-10)

The five Dorset districts abolished on 1 April 2019 (East Dorset E07000049, North Dorset E07000050, Purbeck E07000051,
West Dorset E07000052, Weymouth and Portland E07000053) appear in the open data for 2014-15 to 2018-19, and
`la_code_lookup` had no row for any of them, so they were UNEXPLAINED. The first version of the loader carried a private
table for them (`LOOKUP_GAPS`), which broke rule 4 (no private recode dicts); the controller ruled it out and it is
removed (commit "fix: S13 resolves Dorset through la_code_lookup").

- **Before (read-only):** `la_code_lookup` has columns `old_code` (PK), `new_code`, `la_name`, `change_type`,
  `effective_date`, `notes`, `loaded_at`; no triggers; 338 rows (296 `current`, 33 `new_unitary`, 7 `merger`, 2
  `recode`); no row for E07000049 to E07000053. The comparable rows are Buckinghamshire's abolished districts
  (`new_unitary`, `la_name` = the successor, `effective_date` the reorganisation date, notes
  "<old name> -> <successor>. Was absent.").
- **Written, one transaction, five INSERTs:** `old_code` E07000049..E07000053, `new_code` E06000059, `la_name` Dorset,
  `change_type` `new_unitary`, `effective_date` 2019-04-01, notes "<old name> -> Dorset. Was absent. Added 2026-10-10
  (S13 LAHS loader): ..." naming the evidence. Evidence: the LAHS open data's own `LAD24CD` column gives E06000059 for each
  in 2014-15 to 2018-19 (February and June 2026 files agree), and the ONS reorganisation of 1 April 2019 created Dorset
  Council. Read back: 5 rows; a hash over all 338 other rows (every column including `loaded_at`) was identical before and
  after (md5 starting c06ed7e07f17b4f4), so nothing else in the table changed.
- **No other loader's result changed:** the verify scripts of S10, S22, S23, S4, S6, S9a, S9b, S11, S15, S18 and S19 were run
  one at a time afterwards, each exit 0 with no FAIL; `check_loaders.py` is unchanged at 14 of 18.
- Reverse (if ever needed): `DELETE FROM la_code_lookup WHERE old_code IN (...)` for those five codes. Nothing else
  referenced them.

## What edition 2 changes (counts by group; the June 2026 files read against edition 1)

Edition 1 "as loaded" is the live table exactly as held, for all 11 periods (3,256 rows; the February file's ledger rows
are `unchanged`). Edition 2 (3,256 rows, one per authority-year) comes from the June 2026 open data CSV (sha256 starting
6b5f0105d34488c9) cross-checked against the 2024-25 year ODS (sha256 starting 6ceb638b59751989; 296 of 296 equal on
`cc1a`, `cc2a` and `cc5a`), stored under the named correction `lahs-correction-2026-10`, which releases exactly these
groups and no others. Every value difference between edition 1 and edition 2:

| Group | Cells | Detail |
| --- | --- | --- |
| Predecessor sums | 76 `households_on_register` + 29 `jointly_managed_register` | 76 keys, 11 authorities (below). Per year: 2015 to 2018 ten each (plus Cumberland, below), 2019 eleven, 2020 seven, 2021 six, 2022 four, 2023 four. A sum is NULL (`part_missing`) if any predecessor has no figure (rule 1.7); `jointly_managed_register` is NULL (`parts_disagree`) where the parts disagree (28 rows, plus one that is both). |
| Allerdale rule | 4 | Cumberland 2015 to 2018 `households_on_register`: held 0, 1,473, 791 and 1,390 (one predecessor's figure), now NULL `part_missing` because Allerdale (E07000026) is a predecessor and its published zeros for 2014-15 to 2017-18 are read as not counted. Counted inside the 76. Also Cumberland 2017 `jointly_managed_register` True to NULL (`parts_disagree`), inside the 29. |
| `reasonable_preference` (`cc5a`) filled | 3,241 | 2015 to 2021: 296 each; 2022: 295; 2023: 292; 2024: 292; 2025: 290. The other 15 cells stay NULL: all 15 are published `[x]`, each on a single-code authority. Edition 1 had none (the n8n workflow never loaded `cc5a`). |
| Anything else | 0 | In every year. |

The 11 reorganised authorities: Bournemouth, Christchurch and Poole (E06000058), Dorset (E06000059), Buckinghamshire
(E06000060), North Northamptonshire (E06000061), West Northamptonshire (E06000062), Cumberland (E06000063), Westmorland and
Furness (E06000064), North Yorkshire (E06000065), Somerset (E06000066), East Suffolk (E07000244) and West Suffolk
(E07000245).

After the refresh the live table has 9 NULL `households_on_register` (was 5, plus the four Cumberland years), no zero
(was 1: Cumberland 2015, now NULL), 341 NULL `jointly_managed_register` (was 312), and 15 NULL
`reasonable_preference` (was 3,256). 3,241 rows now carry `loaded_at` 2026-10-10 (edition 2's); 15 keep 2026-04-01 (the
`cc5a`-`[x]` rows: no value changed on the refresh). `jointly_managed_register` holds the publisher's `cc2a`, the Localism Act criteria question (the 2023-24 data dictionary:
"Have you changed your housing register (or waiting list) criteria since last year in light of the changes in the Localism
Act 2011?"); it is not a jointly-managed flag, whatever the column name suggests. The 2024-25 ODS has no `cc2a` column or definition, so the loader reads the CSV's
column and, where the ODS lacks it, requires every `cc2a` in the newest year to be `[z]` (they are).

**Not changed by the load: 2025 `households_on_register`** (0 differences for 296 of 296) and the England 2025 sum,
1,340,527. Edition 2 still revises 2025 for `cc5a` and the new flag columns, which W1 does not read.

## Rules applied, for Scott to overrule

- **`ALLERDALE_ZERO` (applied under the standing blanks ruling, cc1a households):** Allerdale (E07000026) reported
  `cc1a` 0 for 2014-15 to 2017-18 and 2,028 from 2018-19. A zero in the first four years followed by a count looks like not
  counted, so the zero is NULL `not_counted` and Cumberland's sum for 2015 to 2018 is NULL. To reverse the ruling, the
  constant is the single place to change; the previous edition is kept. The households total is the only column the rule
  touches.
- **`TELFORD_NO_REGISTER`** (E06000020, `cc1a`, 2022 onward; the decision of 2026-09-30): fires for 2022, 2023 (a 0 in the
  file) and 2025. Telford's 2024 `cc1a` is `[x]`, so NULL already.
- **`cc5a` zeros are left as published, and they need Scott's decision.** The two rules cover `cc1a` only. Allerdale's
  `cc5a` (reasonable preference) is also 0 for 2014-15 to 2017-18 (then 516 in 2018-19) and is summed, as 0, into
  Cumberland's `reasonable_preference` for 2015 to 2018 (3,110 in 2015, for example); Telford's `cc5a` is 0 in 2015 and
  from 2021 to 2024 (49 in 2016 to 2020), and the 2025 value is `[x]`. If the same reading applies, those cells would be
  NULL (and Cumberland's `cc5a` sums NULL for 2015 to 2018). Not done: it would be a second correction, with its own
  edition. In edition 2 the `cc5a` zeros number 24 cells in all: 5 are Telford's, none is Allerdale's own (its four zeros
  sit inside Cumberland's 2015 to 2018 sums), and the other 19 (4 of them Milton Keynes E06000042 2015 to 2018, the rest
  single cells or short runs on single-code authorities) are published zeros.
- **Observations from the files, not acted on:** the publisher's 2021 and 2022 figures for Hartlepool (2,744 / 530) are
  identical, and so are Redcar and Cleveland's (2,926 / 708); in `la_code_lookup` the note on E07000246 reads
  "Mendip -> Somerset" although E07000246 is Somerset West and Taunton in the national list (the target Somerset is right).

## The proof run

`migrate-legacy data/reference/LAHS_open_data_1978-79_to_2024-25.csv` (sha256 starting addaa0035e2fdc97), one transaction:
the file read with the n8n rules reproduces **every one of the 3,256 held rows, 0 differences**: 3,196 from the first row of
their key in file order; 57 from another predecessor's row (the `DISTINCT ON` defect; listed in the preview); 3 from the
June 2026 file (Bromley, Hillingdon, Mansfield 2025); 3 NULL under `TELFORD_NO_REGISTER` with the `source` text matching
verbatim. Edition 1 "as loaded": 3,256 rows; `published_date` 2026-04-01 for 2015 to 2024 (the load date) and 2026-08-20
for 2025 (the latest load date); one ledger row per year for the February file (latest update 2026-02-12, outcome
`unchanged`). The load preview matched Task 4's read-only preview in everything but time, and the page files are byte
identical to the held ones.

## What was written (2026-10-10)

- `la_code_lookup`: five rows (above). Nothing else in the table changed.
- `ddl --commit`: `la_housing_register_editions` (append-only; triggers `s13_editions_immutable` and
  `s13_editions_no_truncate` refuse UPDATE, DELETE and TRUNCATE) and `la_housing_register_editions_file_checks`. No change
  to the live table's columns or constraints. The editions table has five editions-only columns the live table does not
  (`value_flag`, `return_status`, `imputed_cc1a`, `predecessor_codes`, `live_source`).
- `migrate-legacy ... --commit` (after a preview and a `--simulate` that rolled back): edition 1 for 11 periods, 3,256
  rows; run-log row 368. Live untouched.
- `load --acknowledge-correction lahs-correction-2026-10`: preview (revised 11 months / 3,256 areas; each year
  "ACKNOWLEDGED ... exactly the changes"), `--simulate` (rolled back), `--commit`: edition 2 for 11 periods, 3,256 rows and
  ledger rows (outcome `revised`); run-log row 369. Without the acknowledgement all 11 years are REJECTED (0/NULL changes,
  the England move over 2%, more than 20 authorities changing), exit 1.
- `refresh-latest`: preview (2015 to 2021 296 rows each, 2022 295, 2023 292, 2024 292, 2025 290; total 3,241), `--simulate`,
  `--commit`: 3,241 live rows refreshed, before/after guard passed. It writes no run-log row. `status`: OK, nothing to do.
- `scripts/s13_lahs_editions_verify.py`: all gates PASS, exit 0 (gate 17, the map's year, included). No `zz%` table remains.
- The three raw files are unchanged: `data/raw/s13_lahs/LAHS_open_data_1978-79_to_2024-25.csv` (6b5f0105d34488c9),
  `data/raw/s13_lahs/LAHS_accessible_2024-25_tables.ods` (6ceb638b59751989) and
  `data/reference/LAHS_open_data_1978-79_to_2024-25.csv` (addaa0035e2fdc97). Keep them: the verify gates find them by sha.
- W1, `refresh_map.py`, the export, `push.py` and `git push` were not run. No W1-read value changed.

## When LAHS 2025-26 publishes (the file says "November 2026 to February 2027") and at the June revision

From `ONS_Population_Estimates`:

1. `python scripts/s13_lahs_editions.py load` reads the open data page and the newest "data returns" year page, downloads
   the CSV and the year ODS to `data/raw/s13_lahs` (also in a preview; the database is not written) and previews. Read the
   whole preview. `--release YYYY-YY` insists on a year; `--file PATH --ods PATH` takes local files with the same identity
   and older-file checks (`--no-page` skips the year page). Expect a new period 2026 and every held year compared. Check that
   the 2025-26 year ODS (identity, "Latest update", the `cc1a`/`cc5a` definitions) is as the loader expects; a changed
   definition halts.
2. `load --simulate`, then `load --commit`; then `refresh-latest` (preview, `--simulate`, `--commit`), `status`, and
   `python scripts/s13_lahs_editions_verify.py` (exit 0). **Run `refresh-latest` before W1:** it copies the edition's
   `loaded_at` onto the live rows, so a W1 run made before it would not carry the release, and `refresh_map.py --check`
   is the safeguard. S13 is a W1 input (national aggregates and LA signals read the newest year).
3. The June revision (the publisher revises the previous year's return each June (the open data page's change history records a scheduled June update every year 2021 to 2026; see the source note); 2024-25 changed on 25 June 2026): a file
   restating held years is compared per year; unchanged years are logged only. A revised year is REJECTED unless the
   preview is read and the year named with `--acknowledge YYYY` (England total moving over 2%, or more than 20 authorities
   changing). A 0-to-NULL or NULL-to-0 change is never released by `--acknowledge`; it needs a named, decided correction.
   A partial file (fewer authorities than the tip) is never released. The 2025 `households_on_register` is W1's year: gate
   17 will FAIL by design once a revision moves it, so read the change, then update the `w1-read` line deliberately.
4. A file with the held file's rank but different content stops unless `--accept-reissue YYYY`. An older file stops unless
   `--allow-older-file`.
5. A new authority code in a future file is UNEXPLAINED until `geography.resolve` or `la_code_lookup` covers it (rule 4);
   add the lookup row with its evidence, as for Dorset.

## How to reverse

`python scripts/s13_lahs_editions.py restore-edition PERIOD N` (preview by default) stores edition N's rows as the next
edition; `refresh-latest --commit` then applies it to the live table. To undo edition 2 for a year, restore edition 1 and
refresh. Nothing is ever deleted from the editions tables. To reverse the Allerdale reading alone, change
`ALLERDALE_ZERO` (or remove it), load with a new named correction, and refresh.

## Addendum 2026-10-10: the zero rules reach `reasonable_preference` (edition 3)

The sections above are left as written. This addendum records a later decision the same day; where it and the
"Rules applied, for Scott to overrule" section disagree on `cc5a`, this addendum is the current state.

**Decision (Scott, 2026-10-10).** The two zero rules apply to `cc5a` (`reasonable_preference`) as well as `cc1a`, for
exactly their keys and years: `ALLERDALE_ZERO` (E07000026, 2014-15 to 2017-18, reporting years 2015 to 2018) reads
Allerdale's `cc5a` zeros as NULL `not_counted`, so Cumberland's `reasonable_preference` sum for 2015 to 2018 is NULL
`part_missing`; `TELFORD_NO_REGISTER` (E06000020, 2022 onward) reads Telford and Wrekin's `cc5a` zeros as NULL
`not_applicable`. Telford's 2015 and 2021 `cc5a` zeros are before the rule's period and stay 0 as published.

**What changed in the loader.** Each rule keeps `column` (`households_on_register`, the column first decided) and gains
`extra_columns` with `reasonable_preference` and the note a row carries when the rule fires there. A second named
correction, `lahs-cc5a-2026-10`, is pinned to the June 2026 file (sha256 starting 6b5f0105d34488c9): it is refused on
any other file, compares only its seven years again although the file is in the ledger, releases exactly one
`cc5a_zero_rule` cell per year and no other content change, stores whole or not at all, and has nothing left to release
once stored. The preview, run before anything was written, showed a revised result for exactly the seven expected cells
and nothing else; 2019 to 2021 and 2025 were skipped as already checked.

**Edition 3 (7 periods, 296 rows each, 2,072 edition rows; run-log row 382).** Every value difference against edition 2:

| Year | Authority | Edition 2 `reasonable_preference` | Edition 3 |
| --- | --- | --- | --- |
| 2015 | Cumberland E06000063 | 3,110 | NULL `part_missing` |
| 2016 | Cumberland E06000063 | 2,288 | NULL `part_missing` |
| 2017 | Cumberland E06000063 | 1,794 | NULL `part_missing` |
| 2018 | Cumberland E06000063 | 1,602 | NULL `part_missing` |
| 2022 | Telford and Wrekin E06000020 | 0 | NULL `not_applicable` |
| 2023 | Telford and Wrekin E06000020 | 0 | NULL `not_applicable` |
| 2024 | Telford and Wrekin E06000020 | 0 | NULL `not_applicable` |

Four Allerdale zeros and three Telford zeros were made NULL by the rules; seven cells changed. Nothing else changed: no
`households_on_register`, no `jointly_managed_register`, no 2019, 2020, 2021 or 2025 row. `refresh-latest` wrote those 7
live rows (their `source` now ends with the cc5a rule note as well). Live `reasonable_preference`: 22 NULL (was 15) and 21
zeros (was 24): Telford 2015 and 2021, and the 19 published zeros on other single-code authorities, all left as published.

**Unchanged, checked before and after:** 2025 `households_on_register` for all 296 authorities hashes to the `w1-read`
line above both times (the line is untouched), the England 2025 sum is 1,340,527, and an md5 over every edition 1 and
edition 2 row is identical before and after (9d39ce9aa939ada6...). Editions 1 and 2 are untouched; edition 3 supersedes
edition 2 in each of the seven years. `status`: OK. `scripts/s13_lahs_editions_verify.py`: 24 of 24 PASS, exit 0 (gate
23 checks both corrections are recorded where they belong; gate 24 checks that after the migration only the seven listed
`cc5a` cells are NULL and the 21 published zeros stay 0). W1, `refresh_map.py`, the export and `git push` were not run; no
W1-read value changed.

**To reverse:** `restore-edition YYYY 2` for each of the seven years (preview first), then `refresh-latest --commit`, and
remove `extra_columns` from the two rules so a later load does not reapply them.
