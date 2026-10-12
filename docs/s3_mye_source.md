# S3: ONS mid-year population estimates, England and Wales

<!-- repo-meta
status: active
last-reviewed: 2026-10-11
type: source
consumed-by: scripts/s3_mye_editions.py, scripts/s3_mye_editions_verify.py
-->

| | |
|---|---|
| Publisher | Office for National Statistics (ONS) |
| Series | Estimates of the population for England and Wales, dataset "Mid-Year Population Estimates, England and Wales" (the workbook Cover title is "Mid-Year Population Estimates, England and Wales, June <year>") |
| Landing page | https://www.ons.gov.uk/peoplepopulationandcommunity/populationandmigration/populationestimates/datasets/estimatesofthepopulationforenglandandwales (the loader reads its `/data` JSON) |
| Cadence | Annual, in summer. The Covers give 9.30am on 30 July 2025 (mid-2024) and 29 July 2026 (mid-2025); the 2026 Cover says "The next publication is scheduled for summer 2027" |
| Live table | `la_population` (the latest-edition layer) |
| Editions | `la_population_editions` (append-only) and `la_population_editions_file_checks` (ledger) |
| Natural key | `(lad24cd, reference_year)` |
| Held | two years, 2024 and 2025: 296 of 296 English local authorities each, `population` and `la_name`; 592 rows |
| Loader | `scripts/s3_mye_editions.py`; gates `scripts/s3_mye_editions_verify.py` (24) |
| Record | `docs/decisions/2026-10-11-s3-editions-first-load.md` |

A W1 and map input: `sql/w1/05_la_signals.sql` reads `population` at the newest `reference_year` (2025), the PIP rate view and `scripts/check_sources.py` read it the same way. The first load changed no 2025 value.

## What the figure is

The estimated resident population of a local authority at mid-year. The publisher's wording (Notes sheet of the July 2026 workbook):

- Note 3, usual residence: the estimated resident population of an area includes all those people who usually live there, regardless of nationality. Arriving international migrants are included if they remain in the UK for at least a year; emigrants are excluded if they remain outside the UK for at least a year; armed forces stationed outside the UK are excluded; students are taken to be usually resident at their term-time address.
- Note 1, accuracy: estimates are presented unrounded, and "should not be taken to be accurate to the level of detail provided". Confidence intervals are in table MYE0 (not loaded).
- Note 16: these are accredited official statistics.

The loader reads `MYE2 - Persons` (the "All ages" column, for the newest year) and `MYE4` (the summary table, mid-2011 to the newest year, for every earlier year). MYE0 (uncertainty), MYE1, MYE3 (components of change), MYE5 (density), MYE6 (median age) and the female and male sheets are not loaded.

## Discovery

`load` asks the ONS landing page JSON for the dataset's editions. An edition is named "Mid-<yyyy>: <yyyy> local authority boundaries", its URI matches `mid<yyyy><yyyy>localauthorityboundaries`, and it has exactly one download, an `.xlsx` workbook (`mye<yy>tablesew.xlsx`). The newest mid-year is taken. Any other URI under the dataset that names a newer mid-year than the newest parsed halts the run, listing what was seen; older irregular URIs are listed and ignored. None, or two, downloads halt. If nothing newer than the held year is listed, the preview says so and, after 30 September of the year after the newest mid-year held, warns that the next release is overdue (30 September 2027 for mid-2026).

The workbook is saved to `data/raw/s3_mye/` (git-ignored; a preview downloads too and writes nothing to the database). A download never overwrites a same-named file with different content: it is saved as `<stem>-<sha8><suffix>` beside it and that is read. Redirects are followed and the final URL recorded. `--release YYYY` takes another mid-year's edition; `--file PATH` takes a local workbook (`--no-page` skips the landing page).

**The API date is not the release date.** The edition page's `releaseDate` for mid-2025 is 2026-07-28T23:00Z, which is 29 July in London; the Cover says "originally published at 9.30am 29 July 2026". The loader prints the API date and ranks the file by the Cover.

## File identity (rule 3): from the file, on every path

Nothing is taken from the file name, link or API. The loader reads and checks:

- the Cover's title, "Mid-Year Population Estimates, England and Wales, June <yyyy>", and its line "The data tables in this spreadsheet were originally published at 9.30am <date>." (the rank, with the mid-year). The mid-2024 workbook also carries "Release number: MYE24" and "Date published: 30 July 2025". The next-publication line is kept;
- the Notes sheet's geography note (note 4: "the population estimates reflect boundaries in place as of April 2024") and any "Revisions to mid-..." note, kept verbatim;
- `MYE2 - Persons`: title "MYE2: Persons by single year of age and sex for local authorities in England and Wales, mid-<yyyy>" and a header starting exactly "Code, Name, Geography, All ages";
- `MYE4`: title "MYE4: Population estimates: Summary for England and Wales, mid-2011 to mid-<yyyy>" and header "Code, Name, Geography, Mid-<yyyy>, ..., Mid-2011";
- `MYE7` when it is the revisions table: its title "July 2026 revisions to mid-2022 to mid-2024 estimates ..." and the published and revised column pairs.

The mid-year in the Cover, MYE2 and MYE4 must agree, and must equal the edition's (and `--release`). Any mismatch halts and names what was seen.

**MYE7 changes meaning between years.** In the July 2025 workbook it is "Research-based statistical measures of uncertainty for local authority mid-year population estimates from 2021 to 2024" (the revisions are in MYE8 and MYE9); in the July 2026 workbook it is "July 2026 revisions to mid-2022 to mid-2024 estimates". The loader reads the revisions table where it is one and names, without reading, the layout where it is not.

## Markers and zeros (rule 1)

Neither workbook held has a marker, a blank or a zero in any English local authority cell: MYE2 "All ages" is a positive whole number for all 296 authorities in both files (checked 2026-10-11), and so is every MYE4 year. The loader therefore requires a positive whole number. A blank, text, a zero, a negative or a fraction halts, naming the cell, and is never read as 0 or stored. No zero has ever been published here, so there is no zero rule to apply.

## Reconciliation (from the file, on every load)

The 296 English authorities (164 non-metropolitan districts, 63 unitary authorities, 36 metropolitan districts and 33 London boroughs, identified by the codes E06, E07, E08 and E09) must sum to the E92000001 England row in MYE2 and in every MYE4 year read, MYE2 and MYE4 must agree on the newest year, and MYE4 must equal MYE7's revised column for each revised year. They do: mid-2025 sums to 58,834,812; mid-2024 in the July 2026 workbook to 58,607,170 and in the July 2025 workbook to 58,620,101. The 22 Welsh authorities and the Wales row (23 rows), the E10, E11 and E12 aggregates and the E92 and K04 rows are read and set aside. A difference halts.

## Geography (rule 4)

Declared `old` in `scripts/geography.py`: both workbooks carry Barnsley and Sheffield as E08000016 and E08000019, the codes `la_boundaries` (LAD May 2024) holds. The edition is named "2023 local authority boundaries" and the Notes say the estimates "reflect boundaries in place as of April 2024". The release date is after the 1 April 2025 recode, so the date would have predicted the new codes; the file does not use them. The loader resolves through `geography.resolve('3', ...)`, and an E08000038 or E08000039 in a file halts against the declaration. Any code outside `la_boundaries` is UNEXPLAINED and stops the load.

## Editions and revisions

An edition is what one workbook says about one mid-year: per authority `population` and `la_name`. One file states its newest year (a new period, from MYE2) and every held year MYE4 covers (compared with the held edition).

**Revisions are the rule, not the exception.** Both workbooks held revise earlier years: the July 2025 workbook's Note 6 says mid-2022 and mid-2023 were revised for updated international and internal migration (MYE8 and MYE9); the July 2026 workbook's Notes 6 and 22 say mid-2022, mid-2023 and mid-2024 were revised for updated international migration (MYE7). Measured on the files: 296 authorities changed in mid-2022 and 295 in mid-2023 between the two workbooks' MYE4 (neither year is held in this table), and 295 in mid-2024.

**The July 2026 revision of mid-2024 (edition 2, loaded 2026-10-11).** The July 2025 workbook's mid-2024 (live 2024 as loaded) became edition 1; the July 2026 workbook's MYE4 mid-2024 is edition 2, equal to MYE7's "revised July 2026" column on all 296 authorities, with MYE7's "published July 2025" column equal to edition 1 on all 296. 295 authorities change (E07000203 is the one that does not); England goes from 58,620,101 to 58,607,170, -12,931 (85 authorities up, 210 down). The largest moves are Westminster +5,698 (+2.71%), Newham +4,956, Haringey +4,674, Tower Hamlets +4,267, Waltham Forest +4,045 and Enfield +3,987; Bradford -3,787 is the largest fall. The full list is in the decision note. The live 2024 rows were refreshed from edition 2; edition 1 stays in `la_population_editions`. Mid-2025 was not restated against itself: 0 differences.

Edition 1 of each year is the live table exactly as held on 2026-10-11 (296 rows each), proved equal to a re-read of each year's own workbook (0 unexplained differences; four 2024 names are held in the NOMIS form, below). A new year is inserted as edition 1 with its live rows and ledger row in one transaction. A restated held year is stored as the next edition and reaches the live table only through `refresh-latest --commit`, which copies the edition and its `loaded_at`, with a before-and-after guard that refuses any change outside the keys it names.

A byte-identical re-read is `unchanged` on every path (page, `--release`, `--file`), including against edition 1 "as loaded". The ledger records each file read by (final URL, sha256); a load skips a file only when the ledger holds that pair for every period it covers. The mid-2024 workbook has no ledger row because it does not reproduce edition 1 on every column (the four names). An older file (by the Cover's own rank) is skipped on every path and halts unless `--allow-older-file` (logged). A file with the held file's rank but different content stops unless `--accept-reissue YYYY`.

**Names.** The 2024 load of 2026-03-26 came from NOMIS (`NM_2002_1`), whose names drop the comma, so four held names differ from the workbook's: "Bristol City of", "Herefordshire County of", "Kingston upon Hull City of" and "Bournemouth Christchurch and Poole". Edition 1 keeps them as held; edition 2 and the live 2024 rows carry the workbook's.

### Stop conditions (enforced in `load`)

A **halt** (identity, header, blank, text, zero, reconciliation or geography failure, or a release title that does not fit) stops the run before anything is planned: nothing stored, no ledger row, exit 1. A year breaking a **threshold** is REJECTED: nothing stored for it, no ledger row, exit 1; a partial run still writes its run-log row naming the years stored, unchanged, skipped and rejected. The thresholds are constants in the loader, calibrated on the mid-2025 workbook.

| Case | Rejected when |
|---|---|
| new year | England moves more than 2.41% from the previous held year (the largest annual move in MYE4, mid-2011 to mid-2025, is +1.4008%, mid-2022 to mid-2023, plus one point), or any authority moves more than 15% (City of London moved +38.34% in 2022 and +20.75% in 2023 and would have needed `--acknowledge`; the largest in 2025 is Rugby +2.39%) |
| held year restated | any change at all against the held edition needs `--acknowledge YYYY`; a move of more than 7.68% at any authority or 0.5% nationally is LARGE and also needs `--accept-large-revision YYYY` |
| any year (never released) | fewer than 296 authorities, or an authority of the held edition missing (`PARTIAL FILE`): a partial file never replaces a fuller edition |

The 7.68% authority limit is the largest observed revision plus one point. MYE7 shows City of London revised +3.487% (mid-2022) and +6.674% (mid-2023) and Westminster +2.713% (mid-2024); a 2% limit would have rejected all three revision years, including this one, for ordinary revisions. England's largest revision seen is +0.1007% (mid-2023), so 0.5% is about five times it. The July 2026 revision therefore needed only `--acknowledge 2024`.

## How to run

Run from `ONS_Population_Estimates`; see the S3 section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s3_mye_editions.py status
python scripts/s3_mye_editions.py load            # preview; downloads the newest workbook
python scripts/s3_mye_editions.py load --acknowledge 2025 --simulate   # as the preview directs
python scripts/s3_mye_editions.py load --acknowledge 2025 --commit
python scripts/s3_mye_editions.py refresh-latest   # preview; --commit applies a revision
python scripts/s3_mye_editions_verify.py
```

Other commands: `ddl`, `restore-edition YYYY N`, and the one-off `migrate-legacy FILE FILE` (already run). Every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**Run `refresh-latest` before W1, in the same session.** A new year's live rows take `loaded_at` = now, but a revision reaches the live table only through `refresh-latest`, which copies the edition's `loaded_at`; if W1 ran in between, `refresh_map.py --check` would call the map current while it still showed the unrevised figures. `la_population` is a W1 input. After the 2026-10-11 refresh of 2024, `refresh_map.py --check` reports `la_population` as loaded after the last W1 run although the value W1 reads (2025) did not change; that is expected.

## Known traps

- **Believing the API's release date.** It is the evening before the Cover's (above).
- **Reading MYE7 as a revisions table in every year.** It was the uncertainty table in July 2025.
- **Joining without pinning the year.** Two years are held: an unpinned join fans out (W1 node 5 and `v_la_pip_rates` were broken by this in August 2026 until pinned to `MAX(reference_year)`).
- **Comparing years across files by column position.** MYE4's columns are read by their header year.
- **Quoting mid-2024 without the edition.** The figure changed by -12,931 for England between the July 2025 and July 2026 workbooks.
- **Expecting E08000038 and E08000039.** The files are on E08000016 and E08000019.
- **A repeated request.** ONS answered HTTP 429 to repeated requests on 2026-10-11; the loader halted before writing and a retry a few minutes later worked.

## History

- **2026-03-26:** the n8n workflow "ONS Population Estimates (S3)" loaded mid-2024 from the NOMIS API plus a "supplement" CSV made in a chat session for 11 authorities NOMIS did not give (now `data/reference/mye_2024_supplement.csv`); run-log ids 2 to 16, id 16 recording 296 rows. The populations equal `mye24tablesew.xlsx` on all 296. The workflow could not have run again as it stood (its upsert named `ON CONFLICT (lad24cd)`, which no longer matches the key).
- **2026-08-13, run-log 86:** `scripts/s3_mye_refresh.py` loaded mid-2025 from `mye25tablesew.xlsx` and widened the key to `(lad24cd, reference_year)`.
- **2026-10-11:** the workflow is retired (its Code node throws) and the script is a guard that exits 2. **An accident the same day:** tests written to prove the guards ran before the guards existed, so `s3_mye_refresh.py` ran for real and re-upserted the 296 rows of 2025 with identical values; it wrote no run-log row, and `loaded_at` of 2025 was set back to its previous value in the repair. Nothing published was affected. Record: `docs/decisions/2026-10-11-s3-s14-accidental-rebuild.md`.
- **2026-10-11:** edition tables created; edition 1 "as loaded" for 2024 and 2025 (run-log 517); edition 2 of 2024 from the July 2026 workbook (run-log 518); `refresh-latest` applied it to 295 live rows. The record, with the before-state hashes and the 295-authority list, is `docs/decisions/2026-10-11-s3-editions-first-load.md`.
