# S10 — MHCLG rough sleeping snapshot in England

<!-- repo-meta
status: active
last-reviewed: 2026-10-10
type: source
consumed-by: scripts/s10_rough_sleeping_editions.py, scripts/s10_rough_sleeping_editions_verify.py
-->

| | |
|---|---|
| Publisher | MHCLG (the file's own source line: "MHCLG Rough Sleeping Snapshot"). The register and registry still say DLUHC, the department's earlier name |
| Series | Rough sleeping snapshot in England, annual, autumn |
| Collection | https://www.gov.uk/government/collections/homelessness-statistics (the same collection as S1; each year is a release titled "Rough sleeping snapshot in England: autumn YYYY") |
| Cadence | Annual. The snapshot is taken on one night each autumn; the release follows the next February (autumn 2024: 27 February 2025; autumn 2025: 26 February 2026). The autumn 2025 Cover gives "Next Release: Winter 2026/2027" |
| Live table | `la_rough_sleeping` (the latest-edition layer) |
| Editions | `la_rough_sleeping_editions` (append-only) and `la_rough_sleeping_editions_file_checks` (ledger) |
| Natural key | `(lad24cd, snapshot_year)` |
| Held | one snapshot year, 2025: 296 of 296 English local authorities, `rough_sleeping` (autumn 2025) and `rough_sleeping_prev_year` (autumn 2024) |
| Loader | `scripts/s10_rough_sleeping_editions.py`; gates `scripts/s10_rough_sleeping_editions_verify.py` (21) |
| Record | `docs/decisions/2026-10-10-s10-editions-first-load.md` |

A W1 and map input: `sql/w1/04_national_aggregates.sql` sums both columns at the newest `snapshot_year`, `sql/w1/05_la_signals.sql` joins on the newest year, and the export labels the year from it. The first load changed no held value.

## What the figure is

The count of people sleeping rough on a single night. The publisher's wording (Table_1_Total notes 1 to 3 and the Cover):

- People sleeping rough are those sleeping or about to bed down in open air locations and in other places including tents and makeshift shelters. The snapshot does not include people in hostels or shelters, or in recreational or organised protest, squatter or traveller campsites.
- It records only people seen, or thought to be, sleeping rough on one night. It does not include everyone in an area with a history of sleeping rough, or everyone sleeping rough across the October to November period.
- Each local authority chooses the date, between 1 October and 30 November. It uses one of three approaches, decided with local partners: a count-based estimate of visible rough sleeping, an evidence-based estimate meeting, or an evidence-based estimate meeting with a spotlight count in specific areas. The snapshot is independently verified by Homeless Link.
- The Cover says the figures are estimates subject to some uncertainty, and describes the annual snapshot as the official and most robust measure of rough sleeping on a single night.

The loader reads only `Table_1_Total`. The release also has gender, nationality and age (2017 onward), approach (Table_3_Approach), consultation of agencies (2025 only) and rates per 100,000 people (Table_5_Rates); none is loaded.

## Discovery

`load` asks the GOV.UK content API (`https://www.gov.uk/api/content`) for the Homelessness statistics collection (its title must still be "Homelessness statistics"), takes the newest document titled "Rough sleeping snapshot in England: autumn YYYY" and from its page the one attachment titled "Rough sleeping snapshot in England: autumn YYYY - tables" with an `.ods` file. An accessible duplicate titled "... - tables (accessible)" is listed and ignored. None, or several, matching documents or attachments halt, listing the titles seen. A title that looks like the series but does not match the pattern also halts (the loader never passes over a release for an older one). The page's change history is printed in every preview. `--release YYYY` takes another year's page; `--file PATH` takes a local file (`--no-page` skips the page).

The file is downloaded to `data/raw/s10_rough_sleeping/` (git-ignored; a preview downloads too and writes nothing to the database). A download never overwrites a same-named file with different content: it is saved as `<stem>-<sha8><suffix>` beside it and that is read. Redirects are followed and the final URL is recorded.

## File identity (rule 3): from the file, on every path

Nothing is taken from the file name, link or media id. The loader reads and checks:

- the Cover's title, "Annual rough sleeping snapshot in England: autumn YYYY"; the line "Autumn 2010 to autumn YYYY"; "Publication Date: ..." (and the Cover's date cell, when present, must be the same day); "Next Release: ...";
- `Table_1_Total`'s title, "Table 1: Estimated number of people sleeping rough, by local authority district and region, 2010 - YYYY", with the Cover's year;
- the shorthand line (below);
- the header row exactly: Local Authority Code, Local Authority Name, Region Code, Region Name, then 2010 to YYYY.

The release page's year, and `--release`, must equal the file's year. Any mismatch halts and names what was seen. The rank of a file is its own (publication date, year).

## Markers and zeros (rule 1)

The shorthand line on `Table_1_Total` reads: "[x] = Not Available. [z] = Not Applicable. [n] = No data available as the authority was created through reorganisation." In the file `[z]` appears 23 times, all in the code, name and region-code cells that the England, region and "Rest of England" rows do not have, and nowhere else. **None of the three appears in any local authority cell of any year, 2010 to 2025** (checked 2026-10-10: 296 authorities by 16 years, every cell a whole number).

So the zeros are counts: 15 authorities show 0 in 2025 and 12 in 2024, and they stay 0. The table has no `value_flag` because it needs none. An earlier note recorded 22 and 27 zeros "with no NULL" as the signature of S1's defect; those were the zeros of the wrong values then held (below), and the question is closed.

The loader enforces the rule: an LA cell of a loaded year must be a non-negative whole number. A marker (named with its meaning from the file's shorthand line), a blank, text, a negative or a non-integer halts and is never read as 0. A published 0 stays 0. Flag columns would be a follow-up if a marker ever appeared.

## Reconciliation (from the file, on every load)

For every year read the authorities must sum to the England row and to each region row, and "Rest of England" must equal England less London. They do, for every year; England is 4,793 in 2025 and 4,667 in 2024. A difference halts.

## Geography (rule 4)

Declared `new` in `scripts/geography.py`. The autumn 2025 file carries Barnsley and Sheffield as E08000038 and E08000039 for every year 2010 to 2025, so the older years use the new codes too. The table holds E08000016 and E08000019 because `la_boundaries` is LAD May 2024; the loader resolves through `geography.resolve('10', ...)` and the `la_code_lookup` recode rows. An E08000016 or E08000019 in a file halts against the declaration. A code outside `la_boundaries` and the recode rows is UNEXPLAINED and stops.

The autumn 2024 file (sha256 starting ed47513a695d1892, kept in `data/raw/s10_rough_sleeping`) carried E08000016 and E08000019, has no Cover sheet (its note says "Last Update: 27 February 2025") and has a shifted Table_1_Total header: the column headed "Local authority" holds the codes and "Local authority ONS code" holds the names. The loader halts on it at the header check, naming what it saw; it never reads it. The two files agree on every local authority cell for 2010 to 2024 (296 authorities by 15 years, 4,440 cells, 0 differences after the recode), a one-off check recorded in the decision note.

## Editions and revisions

An edition is what one file says about one snapshot year: per authority `rough_sleeping` (the file's column for that year) and `rough_sleeping_prev_year` (its column for the year before). One file restates every year from 2010, so a new file states its newest year (a new period) and every held year it covers; each held year is compared with the held edition.

Edition 1 of 2025 is the data exactly as held on 2026-10-10 (296 rows), proved equal to a re-read of the held file (592 values, 0 differences). The live table moves only by inserting a new year (edition 1, live rows and ledger row in one transaction) and by `refresh-latest --commit`, which copies the tip edition into the live rows and copies the edition's `loaded_at`.

**Revision evidence:** none seen. The autumn 2025 release's change history says only "First published"; the two files compared agree (above). The registry therefore has `revises_back_series` false, on that evidence from one comparison. Every held year in a new file is compared regardless.

A byte-identical re-read is `unchanged` on every path (page, `--release`, `--file`), including against edition 1 "as loaded". The ledger records each file read by (final URL, sha256); a load skips a file only when the ledger holds that pair for every period it covers. A file with the held file's rank but different content stops unless `--accept-reissue YEAR`. An older file (by its own rank) is skipped on every path and halts unless `--allow-older-file` (logged); a file whose newest year is before the newest held year is treated as older, so back-filling needs that flag too.

### Stop conditions (enforced in `load`)

A **halt** (identity, header, marker, blank, non-integer, reconciliation or geography failure, or a release title that does not fit) stops the run before anything is planned: nothing stored, no ledger row, exit 1. A year breaking a **threshold** is REJECTED: nothing stored for it, no ledger row, exit 1; a partial run still writes its run-log row naming the years stored, unchanged, skipped and rejected. The thresholds are constants in the loader, calibrated on the file's own series.

| Case | Rejected when |
|---|---|
| new year | the national total moves more than 30% from the previous held year, or any authority moves by more than 150 |
| held year restated | any change at all against the held edition; the national total of either column moving more than 1% and more than 10 authorities changing are listed on top |
| any year (never released) | fewer than 296 authorities, or an authority of the held edition missing (`PARTIAL FILE`): a partial file never replaces a fuller edition |

`--acknowledge YEAR` releases a year's thresholds and any change to a held year, but never a partial file. Naming a year the run does not compare halts before anything is stored.

For scale, the national total moved -9.1% (2020 to 2021), +25.6%, +27.0%, +19.7% and +2.7% (2024 to 2025); the largest move of one authority in those years was Westminster +111 (2024). The pandemic year 2019 to 2020 (-37.0%) would have stopped on the national limit and needed `--acknowledge`.

## How to run

Run from `ONS_Population_Estimates`; see the S10 section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s10_rough_sleeping_editions.py status
python scripts/s10_rough_sleeping_editions.py load            # preview; downloads the newest release
python scripts/s10_rough_sleeping_editions.py load --commit
python scripts/s10_rough_sleeping_editions.py refresh-latest   # preview; --commit applies a revision
python scripts/s10_rough_sleeping_editions_verify.py
```

Other commands: `ddl`, `restore-edition YYYY N`, and the one-off `migrate-legacy FILE` (already run). Every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**Run `refresh-latest` before W1.** It copies the edition's time into the live `loaded_at`. A new year's live rows take `loaded_at` = now, but a revision reaches the live table only through `refresh-latest`; if W1 ran in between, `refresh_map.py --check` (which compares the tables' `loaded_at` with the last run) would call the map current while it still showed the unrevised figures. S10 is a W1 input, so do both in the same session.

## Known traps

- **Reading the autumn 2024 layout.** No Cover, shifted headers, old Barnsley and Sheffield codes. The loader halts.
- **Expecting the old codes.** The 2025 file is on E08000038/39 for all years; the table is on E08000016/19.
- **Reading a blank or marker as 0.** The old n8n code did (`|| 0`); it did no harm only because the file has none.
- **Comparing years across files by column position.** Columns are read by their header year.
- **Treating the count as everyone sleeping rough.** See "What the figure is".

## History

- **2026-03-26, run-log 17:** the n8n workflow "Rough Sleeping Snapshot (S10)" upserted a CSV converted from the publisher's file in a chat session that no longer exists. Its columns headed 2025 and 2024 held the **autumn 2021 and 2020 snapshots**: the backup `la_rough_sleeping_bak_20260820` sums to 2,443 and 2,688, which are exactly the file's England row for 2021 and 2020 (the figures for 2025 and 2024 are 4,793 and 4,667). The run-log note nevertheless says "autumn 2025 snapshot".
- **2026-08-19:** the table was rewritten from the publisher's autumn 2025 file by a load that left no run-log row and no committed code. Every row carries that one `loaded_at`. W1 runs 4 to 17 had carried the wrong numbers; run 18 (2026-08-20) carries 4,793.
- **2026-10-10:** the n8n workflow is retired (its Code node throws); this loader is the only way in. Edition 1 "as loaded" and a ledger row were written; no held value changed. The `data/reference/rough_sleeping_snapshot_2025.csv` copy is a separate matter (`docs/decisions/2026-09-30-reference-csvs-out-of-step.md`).
- The record, with the before-state hashes and the full history checked against the database, is `docs/decisions/2026-10-10-s10-editions-first-load.md`.
