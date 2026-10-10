# S17 — SafeLives Marac data by police force area

<!-- repo-meta
status: active
last-reviewed: 2026-10-10
type: source
consumed-by: scripts/s17_marac_editions.py, scripts/s17_marac_editions_verify.py
-->

| | |
|---|---|
| Publisher | SafeLives, a charity (the workbooks' own title: "SafeLives Marac data England and Wales") |
| Series | "Data by police force area, region and country (England and Wales)": one workbook for each year ending March. The page says "A summary of Marac data by police force area, region and country is available annually for England and Wales" |
| Data page | https://safelives.org.uk/research-policy/practitioner-datasets/marac-data/ (the registry's earlier URL, `.../practice-support/resources-marac-meetings/latest-marac-data/`, answers with a 301 redirect to it) |
| Cadence | Annual. The page states no release date. The three `.xlsx` workbooks carry document-modified dates of 12 August 2024 (2023-24), 11 July 2025 (2024-25) and 2 July 2026 (2025-26); the five `.xls` workbooks record none that the reader can see (below) |
| Live table | `marac_cases` (the latest-edition layer) |
| Editions | `marac_cases_editions` (append-only) and `marac_cases_editions_file_checks` (ledger) |
| Natural key | `(pfa_name_safelives, financial_year)` |
| Held | 312 rows: eight years (2018-19 to 2025-26) for 39 English police force areas |
| Loader | `scripts/s17_marac_editions.py`; gates `scripts/s17_marac_editions_verify.py` (20). Manual-input helper `scripts/manual_input.py`; `xlrd==2.0.1` reads the `.xls` files; the repo has no requirements file, so the pin exists only in docs (open item 6 in `docs/decisions/README.md`) |
| Record | `docs/decisions/2026-10-10-s17-editions-first-load.md` (with the before-state hashes, the restored zeros and the West Midlands finding) |

A W1 and map input: `sql/w1/05_la_signals.sql` reads `cases_discussed` and `cases_per_10k_adult_females` at the newest `financial_year` through `la_pfa_mapping`. The other six columns are not read by W1.

## What the figures are

The publisher's wording, from the data page and the workbooks:

- "A MARAC (multi-agency risk assessment conference) is a meeting where information is shared on victims at the highest risk of serious harm or murder as a result of domestic abuse. It is attended by representatives of local agencies such as police and health."
- "MARAC data is data submitted to SafeLives, by individual MARACs, on a quarterly basis." The annual workbook is a summary by police force area, region and country.
- The page says the data "should not be used as an official measure of high-risk domestic abuse prevalence"; it is "a vital tool for understanding how MARACs are operating".
- The `Cases` sheet is "Cases discussed at multi-agency risk assessment conferences (MARACs), by police force area and region, year ending March YYYY", with eight columns: Number of MARACs, Number of cases discussed, Recommended number of cases, Number of cases per 10,000 adult females, Number of repeat cases, Percentage of repeat cases, Number of children in household. The workbooks do not define "recommended number of cases" or "repeat cases", or say which population estimate is behind "per 10,000 adult females"; the pipeline stores the published figure and computes none (the old description in `METHODOLOGY.md`, a rate from `population`, was not what W1 reads).
- The `Referral routes` sheet gives the sources of referrals, with a Number and a % for each of: Police, Independent Domestic Violence Advisor (IDVA), Children's Social Care Services, Adult Social Care Services, Primary Care Services, Secondary Care/ Acute trust Services, Mental Health Services, Substance Abuse Services, Probation, Voluntary Sector, Multi Agency Safeguarding Hub (MASH), Education, Housing and Other. Note 1: "The number of MARAC referrals by each source are not presented as whole numbers, because the referral can be made by more than one service", and a case referred by the police and a health service counts as 0.5 for each. So referral counts, and the `housing_referrals` column built from the Housing Number, can be fractional. Only the Housing Number is loaded.
- The `Gender` and `Demographics` sheets are not loaded.

The grain is the police force area, not the local authority. Every authority in a force area carries the same force-wide figure through `la_pfa_mapping`.

## Discovery

`load` asks the data page for its workbook links with a plain `User-Agent` that names the pipeline ("ucws-pipeline S17 loader ..."). It takes links ending `.xlsx` or `.xls` whose names carry two years (`YYYY-YYYY` or `YYYY-to-YYYY`). Two links for one year, none for a held year, or a workbook link whose years do not fit halt, listing every link. On 2026-10-10 the page linked one workbook for each of the eight years and none for 2026-27. Each file is downloaded to `data/raw/s17_marac/` (git-ignored; a preview downloads too and writes nothing to the database) under the same-name rule: a file with different content is never overwritten; it is saved as `<stem>-<sha8><suffix>` beside it. The page HTML is saved there as `marac-data-page_<date>.html`, with a hash suffix when it changes between reads (it did, on 2026-10-10).

**If SafeLives refuses the request.** A browser-like `User-Agent` got a Cloudflare 403 in the survey of 2026-10-10; the plain one was answered in every read that day. A 403, a Cloudflare challenge page or a body that is not a workbook halts with: "SafeLives refused the request: download <url> by hand into data/raw/s17_marac/ and rerun with --file". Then `load --file PATH` (repeatable; the page is still read and must link the file's year, unless `--no-page`) takes the hand-downloaded file. The path must be under `data/raw` or `data/reference`, a regular file and not a symlink. The file is announced as manual input, with its titles, rank and sha256.

Files held (sha256, first 16): 2018-19 `0f92d67dfe9c8efc` (`.xls`), 2019-20 `8d031fea4f37c3dc` (`.xls`), 2020-21 `474aa4dff5fbd2d0` (`.xls`), 2021-22 `d2b4e0f7bf2f5acd` (`.xls`), 2022-23 `b78d639b77366c5a` (`.xls`), 2023-24 `60a18547cfff9b71`, 2024-25 `59bd0e870a0ed051`, 2025-26 `3c1f06380fce49f5`.

## File identity (rule 3): from the file, on every path

Nothing is taken from the file name or link. The loader reads and checks:

- the `Notes` title, "SafeLives Marac data England and Wales April YYYY - March YYYY", and the `Cases` title, "... year ending March YYYY": they must agree with each other and with the year asked for or linked;
- the `Cases` header, exactly the eight columns above (one named alias: the 2019-20 file writes "Number of Maracs");
- the `Housing` column of `Referral routes`, found by its header text, not by position.

The 2022-23 file's `Notes` title says "April 2021 - March 2022" while every other sheet in it says "year ending March 2023" and its figures differ from the 2021-22 file's; the loader holds a named erratum keyed by that file's sha256, and any other file whose titles disagree halts. The rank of a file is its own (title year, document-properties modified date), never its name. The `.xls` reader (xlrd) does not expose document properties, so the manual-input helper prints "document properties: none recorded in the file" for them when it cannot tell; the five `.xls` years rank from the titles and the sha256 with the modified date unknown.

## Markers and zeros (rule 1)

A value cell is a number or one of the files' markers: "No Data" (the 2022-23 file writes "No data", a named alias) gives NULL with `value_flag` `not_submitted`; "No population data" gives NULL with `no_population`. Any other text, a blank included, halts naming the sheet, row and column. **A published 0 stays 0**, with two named exceptions, each with its evidence, listed in every preview (`ZERO_RULES`):

- `NORFOLK_2023_24_NOT_SUBMITTED`: the 2023-24 file publishes Norfolk as 0 MARACs, 0 cases, 0 recommended cases, "No population data" for the rate, 0 repeat cases, "#DIV/0!" for the percentage, 0 children and 0 in every Referral routes count; SafeLives publishes "No Data" for Norfolk in 2022-23, 2024-25 and 2025-26. All eight values NULL, `not_submitted`.
- `WEST_MIDLANDS_2023_24_NOT_SUBMITTED` (Scott, 2026-10-10): the 2023-24 file publishes the West Midlands force in exactly that form, and the West Midlands region row (17 MARACs, 4,191 cases) equals Staffordshire, Warwickshire and West Mercia alone. All eight values NULL, `not_submitted`.

There is **no Lancashire rule**. Lancashire's housing referrals are 0 in 2023-24, 2024-25 and 2025-26 and stay 0, by Scott's ruling of 2026-10-10: no 2023-24, 2024-25 or 2025-26 file carries any note about Lancashire. The only Lancashire note in any held file is 2022-23's ("one MARAC within the Lancashire police force is not included as this Marac did not submit data ... July 2022 to March 2023"), a year in which the housing figure is 13. What the files do show: housing referrals of 15, 32.5, 24, 23 and 13 for 2018-19 to 2022-23, then 0 three years running, and 2 MARACs in 2023-24 and 2024-25 against 9 in 2022-23 and 2025-26. The earlier claim that the zeros were footnoted as incomplete is not supported by the files.

`value_flag` is an editions-only column: the reason for the NULLs of a row, one reason per row (two in one row halt). It is `not_submitted` on 5 rows of edition 2 (Norfolk 2022-23, 2023-24, 2024-25, 2025-26; West Midlands 2023-24).

The n8n workflow read every number with `parseFloat(x) || null`, which made a published 0 NULL: 13 cells in the held table (edition 2 restores them to 0; the list is in the decision note).

## Names and geography (rule 4)

The English police force areas are exactly `la_pfa_mapping.pfa_name_safelives`, 39 of them. An English force missing, an unknown name, or a Welsh force taken as English halts; region and national rows are read for reconciliation and not stored. Footnote markers on names ("Lancashire2", "Metropolitan Police4") are stripped only when the sheet defines that footnote. In every `Cases` sheet "West Midlands" is both a region row and a force row. The held table had no West Midlands force in any year; the cause is probably this shared name, but the n8n code is retired and it is not proved. The loader stores the force, and the seven authorities that `la_pfa_mapping` maps to it (Birmingham, Coventry, Dudley, Sandwell, Solihull, Walsall, Wolverhampton) gain 2025-26 MARAC values on the map after the next W1 run.

`la_pfa_mapping` (296 English authorities) is read, never written. It matches the ONS `LAD24_CSP24_PFA24_EW_LU` lookup (a copy is in `data/raw/s17_marac`) on every code; one force-name spelling differs ("City of London" against ONS's "London, City of"). S17 is declared `none` in `scripts/geography.py`: it is keyed by force, not by authority.

## Reconciliation (from the file, on every load)

Inside the file the English police force areas must sum to the England row on five measures (MARACs, cases discussed, recommended cases, repeat cases, children in household), wherever every part is published; otherwise it is reported as not checked. A difference halts.

## What the files leave out

The footnotes say what is missing: Wigan in 2018-19, 2019-20 and 2020-21; one Marac within the Metropolitan Police area from July 2021 to March 2022; three Maracs within the Metropolitan Police area from September 2022 to March 2023 and one within Lancashire from July 2022 to March 2023. The figures for those forces are therefore low for those years by the publisher's own account.

## Editions and revisions

An edition is what one workbook says about one financial year. Edition 1 is the table exactly as held on 2026-10-10 (304 rows, 38 forces a year); edition 2 is the eight files read by the loader (312 rows, 39 forces). The live table moves only by inserting a new year (edition, live rows and ledger row in one transaction) and by `refresh-latest --commit` (a year that gains or loses a force needs `--accept-key-changes YYYY-YY`), which copies the tip into the live rows and copies the edition's `loaded_at`.

**Revision evidence:** none observed; the registry has `revises_back_series` false. A reissued file for a year already held (same title year, a later modified date, different content) stops the load unless `--accept-reissue YYYY-YY`. An older file halts unless `--allow-older-file`. A byte-identical re-read is `unchanged` on every path, including against edition 1 "as loaded". The ledger records each file read by (final URL, sha256).

### Stop conditions (enforced in `load`)

A **halt** (identity, header, marker, force name, title, reconciliation) stops the run before anything is planned: nothing stored, exit 1. A year breaking a threshold is REJECTED: nothing stored for it, no ledger row, exit 1; a partial run still writes its run-log row.

| Case | Rejected when |
|---|---|
| new year | England's cases discussed (the sum of the forces) move more than 25%, or more than 6 forces move more than 40% (measured on the held files: at most 12.3% a year, and at most 4 forces) |
| held year restated | any change at all against the held edition (`--acknowledge YYYY-YY`); a value going to or from NULL needs a named entry in `ACKNOWLEDGED_FLIPS` (the first load used `s17-restored-zeros-2026-10` for the 13 restored zeros) |
| any year | fewer forces than the held edition (a partial file never replaces a fuller one) |

## How to run

Run from `ONS_Population_Estimates`; see the S17 section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s17_marac_editions.py status
python scripts/s17_marac_editions.py load            # preview; reads the data page, downloads new workbooks
python scripts/s17_marac_editions.py load --file data/raw/s17_marac/<workbook> --no-page   # by hand
python scripts/s17_marac_editions.py load --commit
python scripts/s17_marac_editions.py refresh-latest   # preview; --commit applies
python scripts/s17_marac_editions_verify.py
```

Other commands: `ddl`, `restore-edition YYYY-YY N`, and the one-off `migrate-legacy` (already run). Every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**Run `refresh-latest` before W1.** It copies the edition's time into the live `loaded_at`, and S17 is a W1 input. Gate 15 of the verify script holds the 2025-26 values W1 reads for the 38 forces held before; when 2026-27 loads it fails on purpose until the decision note and the gate are reviewed together.

## Known traps

- **Reading a zero as NULL.** The n8n code did. A published 0 is a count.
- **Reading "No Data" as a zero.** It is NULL with a reason; "No Data" and "No data" are the same marker.
- **Taking the West Midlands region row for the force.** The name is the same; the force row follows the region row.
- **Taking a rate from the population table.** The rate is SafeLives' own, per 10,000 adult females.
- **Browser-like request headers.** They drew a Cloudflare 403; the plain `User-Agent` was answered.
- **Assuming whole numbers.** Referral counts can be fractional.
- **Expecting document properties on the `.xls` files.** They are not read.

## History

- **2026-03-30, run-log 22 to 24:** the n8n workflow ran three times in one day and loaded 2018-19 to 2024-25 (7 years x 38 forces, 266 rows) from CSVs converted from the SafeLives workbooks in a chat session that no longer exists. It read numbers with `parseFloat(x) || null` and stored no West Midlands force.
- **2026-08-20:** 2025-26 was loaded directly (38 rows, no run-log row, no committed code), keeping published zeros (commit 33c5ec4 records it), so the table was inconsistent between 2024-25 and 2025-26.
- **2026-10-10:** the n8n workflow "SafeLives MARAC (S17)" is retired (its Code node throws). Edition 1 and edition 2 were written for all eight years: 13 zeros restored, the West Midlands force added in all eight years, Norfolk and West Midlands 2023-24 read as not submitted. The 38 forces held before keep their 2025-26 `cases_discussed` and `cases_per_10k_adult_females` unchanged.
