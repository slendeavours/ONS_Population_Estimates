# S5 — MHCLG English indices of deprivation 2025: Local Authority District summaries (lower tier)

<!-- repo-meta
status: active
last-reviewed: 2026-10-11
type: source
consumed-by: scripts/s5_imd_editions.py, scripts/s5_imd_editions_verify.py
-->

| | |
|---|---|
| Publisher | MHCLG (the register and registry say MHCLG; the release page is on GOV.UK) |
| Series | English indices of deprivation, the 2025 update (IoD2025). The file loaded is File 10, "Local Authority District summaries - lower tier" |
| Collection | https://www.gov.uk/government/collections/english-indices-of-deprivation (the IoD2025 page is https://www.gov.uk/government/statistics/english-indices-of-deprivation-2025) |
| Cadence | About every five years. The collection lists releases for 2010, 2015, 2019 and 2025. IoD2025 was first published on 30 October 2025 |
| Live table | `la_imd_2025` |
| Editions | `la_imd_2025_editions` (append-only) and `la_imd_2025_editions_file_checks` (ledger) |
| Natural key | `lad24cd` (the live table holds one vintage; the vintage is the engine period) |
| Held | 296 districts, one vintage (`IoD2025`), the ten IMD summary measures; edition 1 "as loaded" |
| Loader | `scripts/s5_imd_editions.py`; gates `scripts/s5_imd_editions_verify.py` (17) |
| Record | `docs/decisions/2026-10-10-reference-trio-editions-first-load.md` |

A W1 and map input: `sql/w1/05_la_signals.sql` reads `imd_rank_of_average_rank` from `la_imd_2025`, and `scripts/export_map_data.py` labels the vintage. Nothing else of this table is read by W1 or the map. The first load changed no held value.

## What is loaded, and what the columns mean

File 10 has a sheet for each domain; only the `IMD` sheet (the Index of Multiple Deprivation, all domains combined) is loaded. Its columns, as the publisher names them in the header row (the loader's `FILE10_HEADERS`, verified against the held file):

| Column in `la_imd_2025` | File 10 `IMD` sheet header | Meaning |
|---|---|---|
| `lad24cd`, `la_name` | Local Authority District code (2024), name (2024) | The district. 296 districts, codes E06, E07, E08, E09 |
| `imd_average_rank` | IMD - Average rank | Average of the district's LSOA ranks |
| `imd_rank_of_average_rank` | IMD - Rank of average rank | The district's rank on that measure, 1 to 296. **The only column W1 and the map read** |
| `imd_average_score` | IMD - Average score | Average of the district's LSOA scores |
| `imd_rank_of_average_score` | IMD - Rank of average score | Rank on that measure |
| `imd_proportion_most_deprived_10` | IMD - Proportion of LSOAs in most deprived 10% nationally | Proportion of the district's LSOAs among the most deprived 10% of LSOAs in England (0 to 1) |
| `imd_rank_of_proportion` | IMD - Rank of proportion of LSOAs in most deprived 10% nationally | Rank on that measure |
| `imd_extent`, `imd_rank_of_extent` | IMD25 - Extent, IMD25 - Rank of extent | The publisher's measure of that name (see below) |
| `imd_local_concentration`, `imd_rank_of_local_concentration` | IMD25 - Local concentration, IMD25 - Rank of local concentration | The publisher's measure of that name (see below) |
| `source` | | The release label, set by the loader from the edition (`MHCLG English Indices of Deprivation 2025, File 10 LA District Summaries (lower-tier) v2, published 30 October 2025`) |
| `vintage` | | `IoD2025` on every row; added 2026-10-11 (see "The `vintage` column") |

What the publisher says about the measures (the statistical release, https://www.gov.uk/government/statistics/english-indices-of-deprivation-2025/english-indices-of-deprivation-2025-statistical-release, read through the GOV.UK content API on 2026-10-11):

- **Direction.** The indices rank each small area (LSOA) in England from most deprived to least deprived, from 1st (most deprived) to 33,755th (least deprived). At district level the file ranks the 296 districts the same way: Blackpool is 1 on the rank of average rank and the rank of average score, Middlesbrough is 1 on the rank of proportion, in the held file.
- **(Rank of) average rank** summarises the average level of deprivation across an area from the population-weighted ranks of all its neighbourhoods; **(rank of) average score** does the same from the scores, so highly polarised districts score higher on the average score than on the average rank; **proportion of LSOAs in most deprived 10% nationally** counts only the neighbourhoods in the most deprived tenth, so a district with none "may not appear to be so deprived". The publisher says there is no single best summary measure and each leads to a different ranking.
- **Relative, not absolute.** A neighbourhood ranked 100th is more deprived than one ranked 200th, but not twice as deprived.
- **Zeros.** "104 of the 296 districts (35%) do not contain any highly deprived neighbourhoods and are therefore equally ranked on this measure". The held file has exactly 104 zeros in the proportion column.
- **Extent and local concentration are not defined here.** The statistical release does not describe them. The technical report (https://www.gov.uk/government/publications/english-indices-of-deprivation-2025-technical-report) does, and it has not been read for this note (it is a PDF that was not downloaded). The columns hold what File 10 publishes under those names and nothing in the pipeline reads them.

## Discovery

`load` asks the content API (`https://www.gov.uk/api/content`) for the collection (its title must still be "English indices of deprivation"), takes the newest "English indices of deprivation YYYY" page, finds the attachment titled "File 10: Local Authority District summaries - lower tier" and downloads it to `data/raw/s5_imd/` (git-ignored; a preview downloads too and writes nothing to the database). Several or no matches halt, listing what was seen. `--release YYYY` asserts the page's year; `--file PATH` takes a local copy (`--no-page` skips the page). A download never overwrites a same-named file with different content.

## File identity (rule 3): from the file, on every path

File 10 has **no cover sheet and its Notes sheet is empty**, so nothing in the workbook states the release in words. The loader relies on what the file does carry:

- the sheet set, exactly `Notes`, `IMD`, `Income`, `Employment`, `Education`, `Health`, `Crime`, `Barriers`, `Living`, `IDACI`, `IDAOPI` (11);
- the exact `IMD` header row, one declared in `FILE10_HEADERS` (trailing spaces ignored). The last four measures are named `IMD25 - ...`, which ties the file to IoD2025; any other header halts;
- 296 district rows with codes E06 to E09;
- docProps `modified` (`docProps/core.xml`; 2025-11-14T12:48:06Z for the held file), which is the file's **rank**. A file name ("v2"), a link or a download date is never evidence;
- the release page, when it is read: the page's year and the attachment title corroborate. `--release` must equal the file's vintage.

The held file is `data/raw/s5_imd/File_10_-_IoD2025_Local_Authority_District_Summaries__lower-tier__v2.xlsx` (sha256 starting 6c38a2fd3998e0ca). The same bytes are the `data/reference` copy and `scripts/verify/src/imd2025_file10.xlsx`.

## Checks inside the file

Each rank column is a ranking of 1 to 296 and follows its measure (a higher measure, which is more deprived, never ranks after a lower one). Ties share the lowest rank number, as the publisher ranks: the 104 zero proportions share rank 193 and the 15 zero extents share rank 282 in the held file, so `imd_rank_of_proportion` and `imd_rank_of_extent` are **not** permutations of 1 to 296. Two pairs of average scores that are equal at the published precision rank apart, because the publisher ranks the unrounded values. Proportions and extents lie in 0 to 1.

## Blanks and zeros (rule 1)

Every IMD cell must be a number; a blank or text halts and is never read as 0. A value with more decimal places than its live column holds halts (storing it would round it). **Published zeros stay 0:** 104 districts have a proportion of 0 and 15 have an extent of 0, with the ranks above. There are no NULLs in the file or the table. The old n8n code did not coerce a blank to 0: it read a CSV and applied `parseFloat` or `parseInt`, so a blank would have become NaN and then NULL when serialised. Nothing in the held values is affected.

## Geography (rule 4)

Declared `old` in `scripts/geography.py`: File 10 carries Barnsley and Sheffield as E08000016 and E08000019 (the header says "(2024)"). The loader resolves codes through `geography.resolve('5', ...)`; an E08000038 or E08000039 halts, and each canonical code must be in `la_boundaries`. Anything else is UNEXPLAINED and stops.

## The `vintage` column and one vintage in live

The live table holds one vintage. On 2026-10-11 it gained one additive column, `vintage text NOT NULL DEFAULT 'IoD2025' CHECK (vintage = 'IoD2025')`: default-filled (all 296 rows read `IoD2025`), not in the key, and the CHECK makes a second vintage in the live table impossible. Before it was added the readers of `la_imd_2025` were searched for a `SELECT *`, a positional read or an unqualified `vintage`; none is in W1, the export or the map (the one `SELECT *` is in a one-off audit script, `analysis/liverpool_paper/audit.py`, which prints the row as a dict).

A different IoD year (IoD2030, say) is stored as **editions only** (`EditionSpec.live_single_period`) and never reaches `la_imd_2025` through a load. Its file would halt on the header until its header is added to `FILE10_HEADERS` deliberately, with the evidence. Adopting a new IoD is a deliberate change to the CHECK and W1's layer, decided by Scott.

## Editions and revisions

An edition is what one File 10 workbook says about its vintage: per district `la_name` and the ten measures. Edition 1 is the table as held, proved against the held file: 296 districts x 11 values = 3,256, **0 differences**.

**The publisher has reissued this file.** The IoD2025 page's change history (read 2026-10-11): first published 30 October 2025; a note of 13 November 2025 that ONS had corrected and republished an LSOA-to-LAD look-up on 26 August 2025, an error in the allocation of LSOA E01027305 (West Northamptonshire 048E), which had been allocated to Cherwell (E07000177) instead of West Northamptonshire (E06000062), that "the Indices production team accessed this ONS look-up file prior to its republication to meet production timescales", and that remaining impacted files would be reissued; and a note of 17 November 2025 that Files 1 to 9 had been corrected "with no change to the data" and Files 10, 11 and 14 "updated with minor changes", with the instruction to use the updated "v2" workbooks. The held File 10 is v2 (docProps modified 2025-11-14 12:48:06 UTC); the page offers v2 only and v1 is not held, so the size of the change to File 10 is not measured here. The registry has `revises_back_series` true.

A reissue of IoD2025 (a later docProps modified) is stored as the next edition; it reaches the live table only through `refresh-latest`, which also sets `source` on every live row from the edition's release label. Equal docProps modified with different content stops unless `--accept-reissue IoD2025`; an older file stops unless `--allow-older-file`; a reissue that drops or adds a district is PARTIAL and no flag releases it.

## How to run

Run from `ONS_Population_Estimates`; see the "Reference data" section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s5_imd_editions.py status
python scripts/s5_imd_editions.py load            # preview; downloads File 10
python scripts/s5_imd_editions.py load --commit
python scripts/s5_imd_editions.py refresh-latest   # preview; --commit applies a reissue
python scripts/s5_imd_editions_verify.py
```

Other commands: `ddl` (already run), `restore-edition IoD2025 N`, and the one-off `migrate-legacy FILE` (already run). `load` takes `--release YYYY`, `--file PATH [--no-page]`, `--recheck IoD2025`, `--allow-older-file` and `--accept-reissue IoD2025`. Every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**Run `refresh-latest` before W1, in the same session.** Gate 17 of the verify script fails by design when a refreshed reissue changes `imd_rank_of_average_rank`: read the change, then update the `w1-read` line in the decision note deliberately.

## Known traps

- **Calling the ranks permutations.** Two rank columns share ranks (the zeros).
- **Reading 317.** The file ranks 296 districts. The earlier data dictionary text said 1 to 317 and IMD 2019; both were wrong.
- **Trusting a file name.** "v2" is in the name of the held file and also in the page's instruction, but the rank of a file is its docProps modified.
- **Expecting a cover sheet.** There is none; the Notes sheet is empty.
- **Treating the summary measures as interchangeable.** They rank the same districts differently; W1 reads the rank of average rank.
- **Putting a new IoD year in `la_imd_2025`.** The CHECK refuses it.

## History

- **2026-04-01, run-log 30:** the n8n workflow "Indices of Deprivation (S5)" upserted 296 rows from `imd_2025_la_summary.csv`, a CSV converted from File 10 in a chat session that no longer exists and fetched from a GitHub raw URL. The repo had no S5 code. The proof run of 2026-10-11 shows the live values equal File 10 v2 on every cell, so the CSV was made from v2 or from values equal to it.
- **2026-08-20:** the table was verified against the published file at 296 of 296 districts (`docs/METHODOLOGY.md`, "IMD version"). An earlier note saying IMD 2019 was used was wrong.
- **2026-10-10:** the n8n workflow is retired (its Code node throws).
- **2026-10-11:** `ddl` (editions table, ledger and the `vintage` column) and `migrate-legacy` (edition 1, run-log 449). The record, with the before-state hashes, is `docs/decisions/2026-10-10-reference-trio-editions-first-load.md`.
