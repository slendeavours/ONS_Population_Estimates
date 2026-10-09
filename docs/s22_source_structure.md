# S22 — MHCLG Council Taxbase and Live Table 615

Source note, rewritten 2026-10-09 when S22 moved to the editions loader. The earlier version of this file was the structure report written by the retired `scripts/historical/s22_ctb_discover.py` on 2026-08-13. Every definition below is taken from the publisher's own notes in the two files held in `data/raw/s22_ctb` (git-ignored), except the two structural-break dates (1 April 2024 and 1 April 2025), which come from the release's technical notes on GOV.UK and are in neither file.

| Field | Value |
|---|---|
| Publisher | Ministry of Housing, Communities and Local Government (MHCLG) |
| Files | Council Taxbase (CTB) local authority level workbook, one taxbase year; Live Table 615, vacant dwellings by local authority district, every year from 2004 |
| Landing pages | `https://www.gov.uk/government/collections/council-taxbase-statistics`; `https://www.gov.uk/government/statistical-data-sets/live-tables-on-dwelling-stock-including-vacants` (both read through the GOV.UK content API; no file URL is stored) |
| Cadence | CTB annual, first published in November and revised in the following January to May; Table 615 re-issued two or three times a year |
| Live tables | `la_council_taxbase_empties` (296 rows, 2025), `la_ctb_exemption_classes` (3,256 rows, 11 classes x 296), `la_vacant_dwellings_615` (7,170 rows, 2004 to 2025); each is the latest-edition layer of its `*_editions` table |
| Loader | `scripts/s22_ctb_editions.py`; gates `scripts/s22_ctb_editions_verify.py` (24) |
| Geography | CTB: 296 billing authorities on ONS codes. Table 615: districts as they existed each year |
| Readers | W1 (`sql/w1/05_la_signals.sql`, latest `taxbase_year`), `v_la_empty_homes_rates`, `scripts/export_map_data.py`. Nothing reads the class or 615 tables |
| Record | `docs/decisions/2026-10-09-s22-editions-first-load.md` |

## The two files held

| File | Identity, read from the file itself | sha256 (first 8) | Bytes |
|---|---|---|---|
| `2025_Local_Authority_Drop_Down.xlsx` | Cover: "Council Taxbase: Local Authority Level Data for 2025"; "Published ... on 6 November 2025 (originally) and revised on 21 January 2026" | `9fd74444` | 1,800,242 |
| `Live_Table_615.ods` | Cover: "Live Table 615: Vacant Dwellings by Local Authority District, England, from 2004 to 2025"; Latest Update 25 June 2026; Next Update November 2026 to February 2027 | `16d404d2` | 311,603 |

Identity comes from the cover, never from the URL, the file name or the landing page (rule 3). A file is ranked by its own cover date (the CTB revised or first published date; the 615 Latest Update), so an older file never replaces a newer one. `Tables_1_-_5_2025.ods` and a second copy of it (different content under a new media id, with no change note on the page) sit beside the two files and are not loaded.

A GOV.UK asset URL is not stable evidence. The November 2025 workbook's URL (a different file name) now answers 301 to the revised file, and no archive holds the original. The ledger (`la_council_taxbase_empties_editions_file_checks`, `la_vacant_dwellings_615_editions_file_checks`) records the final URL after redirects and the sha256 of the bytes read.

## The CTB workbook

Sheets: `Cover`, `Contents`, `Notes`, the form sheets, and the data sheets `Council Taxbase Data`, `Supplementary Data`, `Council Tax Support Data`, `Family Annexe Data`, `Empty Properties Data`, `Second Homes Data`, `LA list`. On the data sheets row 5 carries the table label, row 6 the headers, row 7 the England row and rows 8 to 303 the 296 billing authorities. Blocks sit side by side and their positions differ per table, so the loader finds each by **number and title wording** and halts, naming what it saw, if one is missing or retitled.

| Table | Published title (2025, shortened) | Column read | Stored as |
|---|---|---|---|
| 1.01 | Total Number of Dwellings on Valuation List (Line 1) | Total | `total_dwellings` |
| 1.11 | Number of dwellings in line 7 classed as second homes (Line 11) | Total | `second_homes` |
| 1.17 | Number of dwellings in line 7 classed as empty and being charged the Empty Homes Premium (Line 14) | Total | `empty_homes_premium_count` |
| 1.18 | Total number of dwellings in line 7 classed as empty (Line 15) | Total | `empty_total` |
| 1.19 | Number of dwellings classed as empty and have been for more than 6 months (Line 16) | Total | `empty_6_months_plus` |
| 2.01 | Number of dwellings on the Valuation List that were in exempt classes B, D to W (`Supplementary Data`) | Classes B, D to L and Q | `la_ctb_exemption_classes.dwellings`, one row per class |

Classes A and C are published as "category not in use". `empty_under_6_months` is derived, `empty_total - empty_6_months_plus`. `unoccupied_exemptions_total` is derived, the sum of the 11 classes.

## What the lines mean (publisher's own notes)

- **Line 15 (table 1.18):** "Total number of dwellings in line 7 classed as empty ... (lines 12+13+14)".
- **Line 16 (table 1.19):** "Number of dwellings that are classed as empty ... and have been for more than 6 months." This is what `empty_6_months_plus` holds, and what the map's Long-Term Empty Rate uses.
- **Line 18 (table 1.22):** empty for more than 6 months "excluding those that are subject to empty homes discount class D or empty due to flooding (Line 16 - line 16a - line 17)". This is the figure Table 615 calls long-term vacants. It is not loaded.
- **Table 615, All vacants:** "empty properties as classified for council tax purposes", including empties liable for council tax and empties that receive an exemption. For October 2025 "the equivalent of Line 15 and the exemption classes B,D,E,F,G,H,I,J,K,L and Q on the CTB form, or tables 1.18 and 2.01 from the CTB release". Before 2013 classes A and C were also included; since 2013 they are in the Line 15 equivalent.
- **Table 615, All long-term vacants:** "properties liable for council tax that have been empty for more than six months and that are not subject to Empty Homes Discount class D or empty due to specific flooding events", for October 2025 "Line 18 ... or table 1.22".

So the two files are reconciled by the publisher, and the earlier statement in this repository that they use different definitions and snapshot dates and are not reconciled was wrong (corrected 2026-10-09). The measure the map shows, table 1.19, differs from MHCLG's long-term vacant figure (table 1.22): England 309,889 against 303,185, and 178 of 296 authorities differ (179 rows if England is counted). The map's measure is unchanged while Scott's decision D1 on it is pending, and the label describes Line 16 accurately.

## Markers (publisher's own notes)

| Marker | CTB `Notes` sheet | Table 615 cover | Stored |
|---|---|---|---|
| `[x]` | data "not available or has been suppressed" | "not available" | NULL, reason `suppressed_or_not_available` |
| `[z]` | "not applicable ... not required or ... not appropriate to aggregate" | "not applicable" | NULL, reason `not_applicable` |
| `[r]` | revised since original publication; flagged in the `Notes` column, not in the value cells | not used | not a value marker |
| `[i]` | not used | "imputed" | halts if it appears in a value cell (it has not) |

A blank, any other marker, a negative or a non-integer halts, except the declared cell below. An authority that does not exist in a Table 615 year is published `[x]` on both sheets and gets no row. `null_reasons` holds one `column=reason` pair per NULL value, sorted and joined with `;`; `built_from_null` marks a derived column that is NULL because a part is. None of the held 2025 values is NULL.

**Dacorum (E07000096) 2012** is published as 1415.57798165137 (all vacants) and 494.577981651376 (long-term); England and the East region carry the same fraction and the file gives no reason. It is the one declared non-integer, held rounded half up as 1416 and 495.

## Reconciliation, from the file on every load

- Inside each file the sum of the authorities equals the England row, for every used column and year (CTB columns; 615 both sheets, every year).
- Across the two files, 615's 2025 all-vacants equals CTB `empty_total + unoccupied_exemptions_total` for every authority (296 of 296 on the held files).
- Rows: 296 CTB authorities and 11 classes each; 615 rows per year 353 (2004), 355 (2005 to 2008), 326 (2009 to 2018), 317, 314, 309, 309, then 296 for 2023 to 2025. An authority of the held latest CTB year missing from a new year, or a count other than 296, stops the load.

## Barnsley and Sheffield

CTB 2025 uses E08000038 and E08000039 only. Table 615 carries numbers under E08000016/19 for 2004 to 2024 (and `[x]` for 2025) and under E08000038/39 for 2025 (and `[x]` for 2004 to 2024). Table 615 note 10 says that on 1 April 2025 the boundaries of the two metropolitan districts were changed and that the new boundaries are reflected by the new codes. The repository resolves the pair through `scripts/geography.py` (`22` is `mixed`) and the recode rows of `la_code_lookup`, under rule 4.5 (canonical key E08000016/19 while `la_boundaries` is LAD May 2024). A year with numbers on both forms of one area stops the load. Abolished Table 615 districts (80 codes, 891 rows) stay `unmapped` with `lad24cd` NULL and are never mapped to successors, because a sum would count a successor once per predecessor.

## Structural breaks stated by the publisher

Recorded in `ctb_series_breaks` (unchanged). The dates are from the release's technical notes on GOV.UK, not from the two files:

- **1 April 2024:** authorities could charge an Empty Homes Premium of up to 100% on properties empty for between 1 and 2 years (previously only 2 or more). `empty_homes_premium_count` is not comparable across this date.
- **1 April 2025:** authorities could charge a Second Homes Premium of up to 100%. `second_homes` is affected by reclassification from this date.

The 2025 CTB cover adds that some authorities could not split the 1 to 2 and 2 to 5 year empty-premium bands and reported them all in 2 to 5 years (`Notes` [note e]); that affects tables not loaded here.

## Editions and revisions

An edition is what one file says about one period. The CTB main and class tables of a year are always stored together with the same edition number (one file, one savepoint). Edition 1 of 2025 (296 main rows, 3,256 class rows) and of each of the 22 Table 615 years (7,170 rows) is the data exactly as held on 2026-10-09, proved equal to a re-read of the held files (0 differences). A revised file is read, compared with the tip and stored as the next edition; the live table moves only through `refresh-latest --commit`, which copies the time the edition was stored into the live `loaded_at`. `refresh_map.py --check` compares that with the latest W1 run, so run `refresh-latest --commit` in the same session as `load --commit` and before any W1 run: if W1 ran in between, the copied time is earlier than W1's run and the map is called current while it still shows the unrevised year.

The CTB cover says "No revisions have been made to previous years", so a revision is a re-issued workbook for the latest year. The 2025 workbook was revised on 21 January 2026 after "corrected data from 22 authorities"; the November original cannot be recovered, so the change per cell is not measured. Table 615: June 2025 to January 2026 added the 2025 column and changed nothing in 2004 to 2024; January 2026 to June 2026 changed nothing on the two sheets loaded. A back-year change in 615 would be stored as a new edition of that year.

## How to run

See the S22 section of `docs/QUARTERLY_REFRESH.md`. In short: each November `python scripts/s22_ctb_editions.py load` (preview) then `--commit`; a revised workbook the same way and then `refresh-latest --part ctb`; each Table 615 update `load-615` then `refresh-latest --part 615`. `w1_run.py` and `refresh_map.py` are run only when the latest taxbase year or its values changed.

## History

The first build (2026-08-13) was four scripts, now in `scripts/historical/` with RETIRED guards. They upserted (`INSERT ... ON CONFLICT DO UPDATE`, setting `loaded_at`), wrote when run with no preview, and the load ran once (run-log id 83); the verify step was run at least twice and its duplicate run-log row (id 84) was deleted. Nothing was overwritten in practice. Its total of the unoccupied exemption classes would have counted a suppressed class as 0; it never did on the 2025 file, where all 11 classes are published for every authority.
