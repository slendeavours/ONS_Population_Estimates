# S13 — MHCLG Local Authority Housing Statistics: households on the housing register

<!-- repo-meta
status: active
last-reviewed: 2026-10-10
type: source
consumed-by: scripts/s13_lahs_editions.py, scripts/s13_lahs_editions_verify.py
-->

| | |
|---|---|
| Publisher | MHCLG (contact housing.statistics@communities.gov.uk). The register and registry still say DLUHC, the department's earlier name |
| Series | Local Authority Housing Statistics (LAHS): the open data file and the regional and local authority data tables of each year |
| Collection | https://www.gov.uk/government/collections/local-authority-housing-data (a page per return year, "Local authority housing statistics data returns for YYYY to YYYY") and the open data page https://www.gov.uk/government/statistical-data-sets/local-authority-housing-statistics-open-data |
| Cadence | Annual. A new year is published November to February (the 2024-25 Cover: "Next Update: November 2026 to February 2027"). The 2024-25 and 2023-24 files both give "Latest update 25 June 2026", and, in the measures held here, the open data file differs from February 2026 in 2024-25 only |
| Live table | `la_housing_register` (the latest-edition layer) |
| Editions | `la_housing_register_editions` (append-only) and `la_housing_register_editions_file_checks` (ledger) |
| Natural key | `(lad24cd, reporting_year)` |
| Held | 11 reporting years, 2015 to 2025 (that is 2014-15 to 2024-25), 296 authorities each, 3,256 rows; two editions of each year |
| Loader | `scripts/s13_lahs_editions.py`; gates `scripts/s13_lahs_editions_verify.py` (24) |
| Record | `docs/decisions/2026-10-10-s13-editions-first-load.md` |

A W1 and map input: `sql/w1/04_national_aggregates.sql` sums `households_on_register` at the newest `reporting_year`, `sql/w1/05_la_signals.sql` joins on the newest year, and the export labels the year from it. The first load changed no value W1 reads (2025 `households_on_register`, England 1,340,527).

## What is loaded, and what the columns mean

LAHS is the annual return from local authorities. The reporting year yyyy-yy ends on 31 March of the second year, so 2024-25 is `reporting_year` 2025. Question codes are from the publisher's `Data_Dictionary`:

| Column | LAHS question | The publisher's description |
|---|---|---|
| `households_on_register` | `cc1a` | "Households on housing register (or waiting list)" |
| `reasonable_preference` | `cc5a` | "Households on housing register (or waiting list) with reasonable preference" |
| `jointly_managed_register` | `cc2a` | "Have you changed your housing register (or waiting list) criteria since last year in light of the changes in the Localism Act 2011?" (character: Yes or No) |
| `source` | | The release label (and, for Telford and Wrekin, the rule note), set by `refresh-latest` |

**`jointly_managed_register` is not a jointly-managed flag.** The column kept the name it was given when the table was first loaded; it holds `cc2a`, a Yes/No answer about changing register criteria after the Localism Act 2011, stored as boolean (Yes true, No false). The data dictionary of the 2023-24 file defines it as above. The 2024-25 file has no `cc2a` column and no definition; the open data CSV gives `[z]` for it for every authority in 2024-25, so 2025 is NULL for all 296 (reason `not_applicable`). The loader reads the CSV's `cc2a` and, when the year file lacks the column, requires every `cc2a` of the newest year to be `[z]`, else the cross-check halts.

The publisher's notes for `cc1a` and `cc5a`:

- **The count is not the number waiting for social housing** (Notes, note 9): authorities periodically review their registers to remove households that no longer need housing, the frequency varies, and some households are on the register of more than one authority, so the total is likely to overstate the households that still need social housing. The figures exclude existing council tenants seeking a transfer and include existing tenants of other social housing providers seeking a transfer.
- **Reference date.** Up to 2017-18 the reference date was 1 April rather than 31 March (dictionary, `cc1a` note), so `reporting_year` 2015 to 2018 are as at 1 April of that year.
- The dictionary's `cc1a` note also says the Localism Act 2011 contributed to a decrease in the size of waiting lists because authorities could set their own qualification criteria, that the Homelessness Act 2002 removed the statutory duty to maintain a register, and that "from 31 March 2021" Telford and Wrekin does not operate a housing register.
- **Imputation.** Note 3 points to the `Imputations` sheet for cells that were imputed; note 10 says that where an authority gave a total for `cc1a` and `cc5a` but not a complete breakdown, the sub-category figures are imputed from the previous year's proportions (sub-categories are not loaded). In 2024-25 the sheet lists `cc1a` for Broxtowe (original `[x]`, imputed 1,121) and Rotherham (`[x]`, 7,033) and `cc5a` for Exeter, Guildford and Sunderland. The loader stores `imputed_cc1a` (editions table only) from the sheet for the newest year's file; for earlier years it is NULL, meaning not checked.
- **Note 32 (a change of interpretation).** After the 2023-24 return Epping Forest clarified that its register counts up to 2021-22 included transfer applicants, and from 2022-23 include new home seekers and the sheltered waiting list but exclude transfer applicants.
- `status` is described as "whether the form was submitted or saved by the local authority in the year shown". Of the 3,471 rows for 2014-15 to 2024-25, 3,443 are "Submitted", 21 "Saved" and 7 "Not submitted"; all 28 carry figures. The notes do not say how to read "Saved" or "Not submitted". The loader stores the value as `return_status` (editions table only) and does not act on it.

## Discovery

Two files make one load.

1. **The open data CSV**, "Local Authority Housing Statistics open data 1978-79 to YYYY-YY" (16,075 rows by 618 columns in the June 2026 file; all years since 1978-79, `Year` column; counts are text with thousands separators). It is restated in place and has no title or date of its own.
2. **The newest year's accessible ODS**, "Local Authority Housing Statistics regional and local authority data YYYY to YYYY", on the "data returns" page of the collection. Its Cover gives the identity of the pair.

`load` asks the content API (`https://www.gov.uk/api/content`) for the open data page and the collection (its title must still be "Local authority housing data"), takes the newest return-year page and its ODS attachment (the open data page also lists an accessible .ods copy, which is listed and not read), and prints both pages' change histories. None or several matches halt, listing what was seen. `--release YYYY-YY` asserts the CSV's newest year; `--file PATH --ods PATH` takes local files (`--no-page` skips the year page). Both files are downloaded to `data/raw/s13_lahs/` (git-ignored; a preview downloads too and writes nothing to the database). A download never overwrites a same-named file with different content (`<stem>-<sha8><suffix>` beside it); redirects are followed and the final URL recorded.

## File identity (rule 3): from the file, on every path

The CSV has no identity of its own, so it is identified by the ODS:

- the ODS Cover title "Local Authority Housing Statistics (LAHS) Data: YYYY-YY" must be the CSV's newest year;
- the Cover's symbols line must define `[x]`, `[z]` and `[s]` ("[x] = not available, [z] = not applicable, [s] = suppressed due to data quality concerns");
- the Cover's "Latest update" date is the rank of the pair (never a file name, link or media id); "Next Update" is read and printed;
- the `Data_Dictionary` definitions of `cc1a` and `cc5a` (and `cc2a` where the sheet has it) must equal the surveyed text exactly; a changed meaning halts;
- **cross-check:** the CSV's newest year must equal the ODS's `Local_Authority_Data` on `cc1a`, `cc2a` and `cc5a` for every authority (296 of 296 in the June 2026 pair). A difference halts.

## Markers and zeros (rule 1)

The markers are `[x]` (not available), `[z]` (not applicable) and `[s]` (suppressed due to data quality concerns). Each is NULL with its reason in `value_flag` (`not_available`, `not_applicable`, `suppressed`), stored as `column=reason`, several joined with "; ". Any other non-number (a blank, "-", "x", "n/a", a negative, a decimal) halts. A published 0 stays 0, except under exactly two parser rules, each a constant in the loader with its evidence, listed in every preview:

| Rule | Applies to | Evidence |
|---|---|---|
| `TELFORD_NO_REGISTER` | Telford and Wrekin (E06000020) `cc1a` from 2022 | The dictionary says it has not operated a register since 31 March 2021; LAHS reports `cc1a` 0 for 2021-22 and 2022-23 and 2024-25 (2023-24 is `[x]`). So a 0 means not applicable: NULL, reason `not_applicable`, for 2022, 2023 and 2025. Decision of 2026-09-30 (`docs/decisions/2026-09-30-telford-no-housing-register.md`) |
| `ALLERDALE_ZERO` | Allerdale (E07000026) `cc1a`, reporting years 2015 to 2018 | Allerdale reported 0 for 2014-15 to 2017-18 and 2,028 for 2018-19 (1,137 in 2013-14). A run of zeros followed by a count looks like not counted: NULL, reason `not_counted`, applied under Scott's standing blanks ruling on 2026-10-10 and listed for him to overrule. Cumberland 2015 to 2018 is then NULL because a part is NULL |

The rules touch `cc1a` only. **For Scott's decision (not decided here):** `cc5a` is also 0 for Allerdale 2014-15 to 2017-18 (then 516 in 2018-19), where it sums as 0 into Cumberland's `reasonable_preference` (3,110 in 2015), and for Telford and Wrekin in 2014-15 and 2020-21 to 2023-24 (49 in the years between). They are stored as published. In all, 24 `cc5a` cells of edition 2 are 0. If the same reading applied, they would be NULL, and Cumberland's `cc5a` NULL for 2015 to 2018; that would be a second correction with its own edition.

**A total built from parts is NULL unless every part is present (rule 1.7):** a reorganised authority's figure is the sum of its predecessors' rows and is NULL (`part_missing`) if any predecessor has no figure; `cc2a` is taken only when every predecessor gives the same answer, else NULL (`parts_disagree`).

## Geography (rule 4)

Declared `old` in `scripts/geography.py`. The loader keys on the CSV's `local_authority_code` (the publisher's code at the start of the financial year the data relates to, per the dictionary), where Barnsley and Sheffield are E08000016 and E08000019 in every year. The CSV's own `LAD24CD` column gives E08000038 and E08000039 for them in the June 2026 file (47 rows each across all years; the February 2026 file's gave E08000016 and E08000019); the loader accepts only that one difference when it checks `LAD24CD` against the resolved code. Codes resolve through `geography.resolve('13', ...)` and then the `la_code_lookup` rows of type `new_unitary` or `merger` with exactly one target in `la_boundaries`. Anything else is UNEXPLAINED and stops.

**Reorganised authorities.** The CSV has one row per publisher code, so for 11 authorities created by reorganisation the successor's row for a year before it existed is built from its predecessors' rows: Bournemouth, Christchurch and Poole (E06000058), Dorset (E06000059), Buckinghamshire (E06000060), North Northamptonshire (E06000061), West Northamptonshire (E06000062), Cumberland (E06000063), Westmorland and Furness (E06000064), North Yorkshire (E06000065), Somerset (E06000066), East Suffolk (E07000244) and West Suffolk (E07000245). Households can be on more than one authority's register, so a sum can double count.

**Dorset.** The five district codes abolished on 1 April 2019 (East Dorset E07000049, North Dorset E07000050, Purbeck E07000051, West Dorset E07000052, Weymouth and Portland E07000053) appear for 2014-15 to 2018-19. `la_code_lookup` had no row for them, so they were UNEXPLAINED. Five `new_unitary` rows (to E06000059) were added to `la_code_lookup` on 2026-10-10, evidenced by the CSV's own `LAD24CD`; nothing else in that table changed. A private table inside the loader was tried and removed (rule 4: no private recode dictionary). See the decision note.

## Editions and revisions

An edition is what one open data file says about one reporting year. Edition 1 of each of the 11 years is the table exactly as held on 2026-10-10 (3,256 rows), proved against the file the n8n load was made from: the n8n rules on the February 2026 file reproduce every one of the 3,256 held rows, 0 differences (3,196 from the first row of their key, 57 from another predecessor's row, 3 from the June 2026 file, 3 NULL under the Telford rule). Edition 2 (2026-10-10, the named correction `lahs-correction-2026-10`) is the June 2026 file read by this loader. It differs from edition 1 in exactly these cells, and no others:

| Group | Cells |
|---|---|
| Predecessor sums (76 keys, 11 authorities, 2015 to 2023) | 76 `households_on_register`, 29 `jointly_managed_register` |
| Allerdale rule (Cumberland 2015 to 2018, inside the 76) | 4 `households_on_register` NULL, and 2017 `cc2a` NULL |
| `reasonable_preference` (`cc5a`) filled | 3,241 (the other 15 are published `[x]`) |

2025 `households_on_register` is unchanged (0 differences in 296), so no W1 value moved. After the refresh the live table has 9 NULL `households_on_register`, 341 NULL `jointly_managed_register` and 15 NULL `reasonable_preference`.

**Revision evidence.** In the three questions held (`cc1a`, `cc2a`, `cc5a`) the February and June 2026 open data files differ in 2024-25 only (2015 to 2024 unchanged): `cc1a` for three authorities (Bromley 3,201 to 3,430, Hillingdon 3,192 to 2,551, Mansfield 4,507 to 5,011) and `cc5a` for five (Bromley, Gravesham, Hillingdon, Rochdale, Three Rivers); the England total of `cc1a` moved by +0.007%. The publisher's note 32 records Epping Forest's changed interpretation. Whether the publisher revises the previous year each June is not established by the files: one June update (25 June 2026) has been seen. The registry has `revises_back_series` true.

The live table moves only by inserting a new year (edition 1, live rows and ledger row in one transaction) and by `refresh-latest --commit`, which copies the tip edition into the live rows and `loaded_at`. A byte-identical re-read is `unchanged` on every path. The ledger records each file read by (final URL, sha256) and a load skips a file only when it holds that pair for every period the file covers. Equal rank with different content stops unless `--accept-reissue YEAR`; an older file halts unless `--allow-older-file` (logged).

### Stop conditions (enforced in `load`)

A **halt** (identity, header, value, cross-check or geography failure) stops the run before anything is planned: nothing stored, exit 1. A year breaking a **threshold** is REJECTED: nothing stored for it, no ledger row, exit 1; a partial run still writes its run-log row. The thresholds are constants in the loader, calibrated on the file's own series.

| Case | Rejected when |
|---|---|
| new year | the England total (over authorities with a figure in both years) moves more than 15% against the latest earlier held year, or more than 30 authorities move by more than 50% |
| revised year | the England total moves more than 2%, more than 20 authorities change, or any value goes to or from NULL |
| revised year, 0 and NULL | a value going from 0 to NULL or NULL to 0 (rule 1.10) is released only by a named, decided correction (`--acknowledge-correction NAME`, which covers exactly the counts it records); `--acknowledge YEAR` never releases it |
| any year (never released) | fewer than 296 authorities or fewer than the held edition: a partial file never replaces a fuller edition |

For scale, the England total moves between -5.6% (2016) and +6.0% (2023), and 11 to 28 authorities a year move by more than 50% (28 in 2022). Without `--acknowledge-correction lahs-correction-2026-10` all 11 years of the first edition-2 load were REJECTED.

## How to run

Run from `ONS_Population_Estimates`; see the S13 section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s13_lahs_editions.py status
python scripts/s13_lahs_editions.py load            # preview; downloads the newest files
python scripts/s13_lahs_editions.py load --commit
python scripts/s13_lahs_editions.py refresh-latest   # preview; --commit applies a revision
python scripts/s13_lahs_editions_verify.py
```

Other commands: `ddl`, `restore-edition YYYY N`, and the one-off `migrate-legacy FILE` (already run). `load` takes `--release YYYY-YY`, `--file PATH [--ods PATH] [--no-page]`, `--recheck YYYY`, `--allow-older-file`, `--acknowledge YYYY`, `--accept-reissue YYYY` and `--acknowledge-correction NAME`. Every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**Run `refresh-latest` before W1.** It copies the edition's time into the live `loaded_at`. A new year's live rows take `loaded_at` = now, but a revision reaches the live table only through `refresh-latest`; if W1 ran in between, `refresh_map.py --check` would call the map current while it still showed the unrevised figures. S13 is a W1 input, so do both in the same session. The verify script's gate 17 fails by design when a revision moves 2025 `households_on_register`; read the change, then update the recorded `w1-read` line deliberately.

## Known traps

- **Reading `cc2a` as "jointly managed".** It is the Localism Act criteria question.
- **Keying on `LAD24CD`.** Use `local_authority_code`; `LAD24CD` changed between the February and June 2026 files for Barnsley and Sheffield.
- **Taking one predecessor's row for a successor.** The n8n load did (`DISTINCT ON`); it is wrong for 76 keys.
- **Reading a zero as a count** for Telford and Wrekin (no register) and Allerdale 2015 to 2018, and, pending Scott's decision, `cc5a`.
- **Comparing the count with the number waiting for social housing.** See note 9.
- **Reading thousands separators.** The CSV's counts are text such as "2,679".
- **Trusting the open data file's name or date.** It has none inside; the ODS Cover gives the identity.
- **Looking for a successor in a code that `la_code_lookup` lacks.** A new authority code is UNEXPLAINED until the lookup or `geography` covers it; add the lookup row with its evidence, as for Dorset.

## History

- **2026-04-01, run-log 28:** the n8n workflow "Social housing waiting lists (S13)" upserted a CSV converted in a chat session from the February 2026 open data file (`data/reference/LAHS_open_data_1978-79_to_2024-25.csv`, sha256 starting addaa0035e2fdc97). Its insert joined `la_code_lookup` and kept one row per (new code, year) with `DISTINCT ON`, so for the 11 reorganised authorities one arbitrary predecessor's figure survived for 2015 to 2023 (76 keys), not the sum. `cc5a` was never loaded.
- **2026-08-20:** three 2025 rows (Bromley, Hillingdon, Mansfield) were set by direct SQL to the 25 June 2026 revision, with no run-log row.
- **2026-09-30:** Telford and Wrekin's 2022, 2023 and 2025 households were set NULL by direct SQL.
- **2026-10-10:** the n8n workflow is retired (its Code node throws); edition 1 "as loaded" and edition 2 were written, and `refresh-latest` applied edition 2 to 3,241 live rows. The `data/reference/lahs_waiting_list_2015_2025.csv` copy is a separate matter (`docs/decisions/2026-09-30-reference-csvs-out-of-step.md`).
- The record, with the before-state hashes and the history checked against the database, is `docs/decisions/2026-10-10-s13-editions-first-load.md`.
