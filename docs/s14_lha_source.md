# S14: DWP Universal Credit Local Housing Allowance rates, and the BRMA crosswalk

<!-- repo-meta
status: active
last-reviewed: 2026-10-11
type: source
consumed-by: scripts/s14_lha_editions.py, scripts/s14_lha_editions_verify.py
-->

| | |
|---|---|
| Publisher | DWP, on GOV.UK (the rates), and the Valuation Office Agency (VOA), which publishes the BRMA boundary layer. The register and registry say VOA/DWP |
| Series | Universal Credit Local Housing Allowance (LHA) rates, England: monthly amounts by Broad Rental Market Area (BRMA), one page and one England file for each financial year |
| Collection | https://www.gov.uk/government/collections/universal-credit-local-housing-allowance-rates (a page for each year, "Universal Credit Local Housing Allowance rates: <yyyy> to <yyyy>") |
| Cadence | Annual. First-published dates: 31 January 2024 (2024-25), 3 February 2025 (2025-26), 30 January 2026 (2026-27). Pages can be reissued in the year |
| Live tables | `brma_lha_rates` (rates) and `la_brma_mapping` (crosswalk), the latest-edition layers |
| Editions | `brma_lha_rates_editions` and `la_brma_mapping_editions` (append-only), each with a `_file_checks` ledger |
| Natural keys | rates `(brma_name, financial_year)`; crosswalk `(lad24cd)`, one vintage `brma-2020-05_lad-2024-05` |
| Held | rates: 152 BRMAs for 2026-27; crosswalk: 296 of 296 English authorities |
| Loader | `scripts/s14_lha_editions.py`; gates `scripts/s14_lha_editions_verify.py` (25) |
| Record | `docs/decisions/2026-10-11-s14-editions-first-load.md` (and `2026-10-11-s3-s14-accidental-rebuild.md`) |

**Both S14 tables are W1 inputs** (`brma_lha_rates` and `la_brma_mapping`). `sql/w1/05_la_signals.sql` reads `brma_name` from `la_brma_mapping` and the five `*_weekly` columns of `brma_lha_rates` at the latest `financial_year`; `scripts/export_map_data.py` labels the year from it. The first load changed no value W1 reads.

## What the figure is

The monthly Local Housing Allowance rate that applies in a BRMA, for the Shared Accommodation Rate (SAR) and for 1 to 4 bedrooms. The year page's table of terms defines BRMA (Broad Rental Market Area) as an area relating to access to facilities and services, containing a variety of residential lettings, across which Local Housing Allowances are determined, and SAR as the Shared Accommodation Rate. The VOA's layer page says LHA rates are based on private market rents being paid in the BRMA, which VOA Rent Officers collect from letting agents, landlords and tenants.

**The weekly columns are derived, not published.** The England file gives monthly amounts only. The five `*_weekly` columns W1 reads are computed in the loader as monthly x 12 / 52, an exact decimal rounded half-up to 2 places (marked `# not a source value` in the code), and checked independently by the gates over live, every edition and the held CSV (760 values, 0 differences). They have not been compared with the VOA's own weekly figures. The old scripts rounded as floats; no 2-decimal monthly amount can differ between the two methods (the loader's tests check every amount up to 3,000.00), so no held value was affected. Any document that quotes a weekly rate should say it is derived.

**The rates have not moved for three years.** `england-rates-2024-to-2025-revised.csv` and `england-rates-2025-to-2026.csv` are byte-identical (sha256 starting da7e106fbc88a7f0; same 9,132 bytes). After converting the 18 "&" names, their 760 monthly values equal `england-rates-2026-to-2027.csv`'s on every BRMA and every column: 0 differences, no BRMA on one side only (checked 2026-10-11). The files give no reason. The 2025-26 file's title row still reads "Monthly UC LHA rates 24/25".

## Discovery

`load` asks the GOV.UK content API for the collection (its title must still be "Universal Credit Local Housing Allowance rates"), takes the newest document titled "Universal Credit Local Housing Allowance rates: <yyyy> to <yyyy>" and from its page the one attachment titled "England: Universal Credit Local Housing Allowance monthly rates <yyyy> to <yyyy>". The page also carries Scotland and Wales files; they are not loaded. None, or several, matches halt, listing the titles seen. The assets URL changes every edition and is never stored. If the collection lists nothing newer than the held year, the preview says so and, after 28 February before the year starts, warns that the next year is overdue.

The file is downloaded to `data/raw/s14_lha/` (git-ignored; a preview downloads too and writes nothing to the database). A download never overwrites a same-named file with different content: it is saved as `<stem>-<sha8><suffix>` beside it. Redirects are followed and the final URL recorded. `--release YYYY-YY` takes another year's page; `--file PATH` takes a local CSV (`--no-page` skips the page); `--only rates|mapping` limits a load.

## File identity (rule 3): from the file, on every path

- **The header row** must be exactly `BRMA`, `Monthly UC LHA rates <yyyy> to <yyyy> - SAR`, then `1 Bed <yyyy> to <yyyy>` to `4 Bed ...`. The year it names must equal the page's (and `--release`). **A header can name the wrong year**: the 2026-27 page's change history says "Corrected date error on Monthly Universal Credit Local Housing Allowance rates header from '2024 to 2025 SAR' to '2026 to 2027' for England, Scotland and Wales" (8 April 2026), and the page's own opening text still says "from 2025 to 2026". A mismatch halts unless `--accept-header-year YYYY-YY` names it (logged).
- **The layout changes between years.** The 2026-27 file has its header on row 1 and no title row (152 data rows). The 2024-25 and 2025-26 files have a title row, ",Monthly UC LHA rates 24/25,,,,", and the header "BRMA,SAR,1 Bed,2 bed,3 bed,4 Bed" on row 2. Reading row 1 of the older layout as the header silently drops the first BRMA (Ashford); the loader halts on the older layout, naming what it saw, and the older files are used only for the frozen-rates check.
- **Names:** the 2026-27 file spells "X and Y"; the older files and the VOA layer spell "X & Y" (18 BRMAs). A name containing "&" in a rates file halts.
- **Encoding:** the CSV is cp1252, not UTF-8: the pound sign is byte 0xA3, and amounts of 1,000 or more are quoted with a thousands separator ("£1,097.80").
- The crosswalk's layer: the zip member `English BRMA(LHA) Layer.gml`, OGR layer `English_BRMA_LHA__Layer`, field `Name`, 152 polygons each with `srsName` EPSG:27700 (the layer's CRS reads as absent in the file and is set to EPSG:27700), and the zip's sha256 a known vintage.

**Rank** (the older-file guard, every path): the financial year the header names, then, within a year, the newest change-history timestamp of the year's page (only for the page's current file); never the media id, URL or file name. An older file is skipped (all older halts) unless `--allow-older-file`; equal rank with different content stops unless `--accept-reissue YYYY-YY`.

## Markers and zeros (rule 1)

The file has no markers. Every rate cell in `england-rates-2026-to-2027.csv` is a money amount (152 BRMAs by 5 rates = 760 values, the lowest £288.96). The loader requires a money amount (`£` and thousands separators stripped, at most two decimal places) and halts on a blank, text, zero or negative cell, naming the cell. No row is ever skipped and nothing is coerced to 0. The old v1 `parse_money` returned 0 for a blank and v2 skipped a row it could not parse; neither ever fired on the held file.

## Geography (rule 4) and the crosswalk

Rates are keyed by BRMA name; there are no authority codes in the file. `la_brma_mapping` is a derived table, not a publication: the VOA layer "English BRMA(LHA) Layer" (page title "Broad Rental Market Area boundary layer for Geographical Information System (GIS) applicable May 2020", first published 4 June 2020; `GML.zip`, sha256 starting 66515b0a1403e455, 152 polygons) joined to the centroids of `la_boundaries` (LAD May 2024, 296 rows). `check-mapping` rebuilds it read-only:

- 295 authorities have their centroid within a polygon (`centroid_spatial_join`); E06000053 (Isles of Scilly) has none and takes the nearest polygon, Kernow West (`nearest_fallback`). An authority that spans more than one BRMA is not split; `brma_secondary` is not used (NULL on all 296).
- 18 layer names ("Bradford & South Dales", "Hull & East Riding", ...) go through the committed exact table `scripts/s14_brma_names.json` (18 "X & Y" to "X and Y" pairs, each checked to be a BRMA of the DWP file), never a pattern. Run-log 53's note says 17 names were reconciled; the table holds 18, read from the layer and the CSV on 2026-10-11. Both are right: 17 of the 18 aliased BRMAs appear in the crosswalk, and "Richmond and Hambleton" is the one that no authority centroid falls in. Those 17 account for exactly the 37 rows that were normalised (re-checked read-only 2026-10-12).
- Every mapped `brma_name` must be a BRMA of the latest rate year, or W1's LHA join leaves an authority without rates. A name missing from a new year's rates is REJECTED and no flag releases it; since 2026-10-12 the same holds for a reissue of the newest year (before that a reissue that dropped a mapped BRMA was only KEY CHANGES, which `--accept-key-changes` could release, leaving authorities without rates until gate 18 was run), and `refresh-latest` rolls back if the newest live year would lack a mapped name. The W1 join is gate 6 of the verify script and gate 18 of `verify_source_registry.py`: 0 of 296 authorities unmatched.
- The vintage is pinned: the loader holds the layer's sha256 and the `la_boundaries` state (296 rows, md5 starting 3335ef7a7ee622b4). Another layer file or a changed `la_boundaries` halts with "a deliberate decision". A newer BRMA or LAD vintage is stored as editions only and never promoted by a load.
- The layer is the one published as applicable May 2020; this repository has not checked whether BRMAs have changed since.

## Editions and revisions

**Rates.** An edition is what one England CSV says about its financial year: per BRMA the five monthly rates as published and the five weekly rates derived. **Revision evidence:** within a year, yes. The 2024-25 page's change history lists "Published an updated version of the English rates for 2024 and 2025" (31 January 2024) and "Revised England: Universal Credit Local Housing Allowance monthly rates 2024 to 2025" (20 May 2024); the 2026-27 page lists the header correction of 8 April 2026. The first issue of 2026-27 is not held, so whether any value changed in that reissue is not known. Between years nothing has been restated. A reissue of a held year is stored as the next edition of that year and reaches live only through `refresh-latest --commit`.

**Crosswalk.** Edition 1 is the live table as held on 2026-10-11, including the text `NaN` in `mapping_method` on 295 rows (a pandas missing value written into a text column by v2). **Edition 2 is the named correction `s14-mapping-method-2026-10`**: `NaN` to `centroid_spatial_join` on those 295 rows and nothing else (`la_name`, `brma_name` and the 296 keys equal; the evidence is `check-mapping`, which finds each of the 295 centroids within its polygon). It was loaded with `--only mapping --acknowledge brma-2020-05_lad-2024-05` and refreshed into live. W1 does not read `mapping_method`. The live table gained one additive column, `vintage` (default `brma-2020-05_lad-2024-05`, a CHECK allowing only that value), by `ddl`.

A byte-identical re-read is `unchanged` on every path, including against edition 1 "as loaded". The ledger records each file read by (final URL, sha256); a load skips a file only when the ledger holds that pair. The crosswalk has no ledger row from `migrate-legacy`.

### Stop conditions (enforced in `load`)

A **halt** (identity, header, layout, money, "&" name or geography failure) stops the run before anything is planned: nothing stored, no ledger row, exit 1. A period breaking a **threshold** is REJECTED the same way. A committed run writes a run-log row, partial runs included.

| Case | Rejected when |
|---|---|
| rates, new year | any BRMA moves more than 25% on any monthly rate, or a rate type's median moves more than 10%, against the previous held year (`--acknowledge YYYY-YY`); these could not be calibrated because only frozen years are held |
| rates, new year or reissue | a BRMA set different from the previous year's or the tip's: KEY CHANGES (`--accept-key-changes YYYY-YY`) |
| rates, new year | a crosswalk name with no rate row in the new latest year: JOIN; no flag releases it |
| rates, reissue | any value changed (`--acknowledge YYYY-YY`) |
| crosswalk | any change against the held edition (`--acknowledge VINTAGE`; a change equal to a named correction is labelled with its name); an authority set different from the tip: KEY CHANGES, which nothing releases |

## How to run

Run from `ONS_Population_Estimates`; see the S14 section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s14_lha_editions.py status
python scripts/s14_lha_editions.py load            # preview; downloads the newest England CSV
python scripts/s14_lha_editions.py load --commit
python scripts/s14_lha_editions.py refresh-latest   # preview; --commit applies a revision
python scripts/s14_lha_editions.py check-mapping    # read-only rebuild of the crosswalk
python scripts/s14_lha_editions_verify.py
```

Other commands: `ddl`, `restore-edition TABLE PERIOD N`, and the one-off `migrate-legacy` (already run). Every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**Run `refresh-latest` before W1, in the same session.** It copies the edition's `loaded_at` onto the live rows, and both tables are W1 inputs. After the 2026-10-11 refresh of the crosswalk (295 rows), `refresh_map.py --check` reports `la_brma_mapping` as loaded after the last W1 run although W1's read did not change; `brma_lha_rates` keeps its old `loaded_at` because there was nothing to refresh.

## Known traps

- **Running the old scripts.** `s14_lha_rates_build.py` and `s14_lha_rates_build_v2.py` are guards now. They converted no "&" name (so a rerun re-created the 2026-08-20 fault: 37 authorities without a rate row), v2 dropped both tables with `CASCADE`, and neither previewed. **Never import a build script to test that it parses** (`py_compile` does that), and a `DROP ... CASCADE` takes derived views with it silently (`v_la_rate_triangulation`).
- **Trusting the header year or the page text.** Both have been wrong.
- **Reading the older layout as the new one.** The first BRMA is lost.
- **Reading the file as UTF-8.** The pound sign is cp1252.
- **Quoting a weekly rate as published.** It is derived.
- **The `imd2025.rank` line.** `scripts/s14_lha_rates_build.py` (before retirement) held, inside `task9_print_updated_w1_node5_sql()`, a printed template for a person to paste into W1 node 5. It was never executed and went with the file; nothing else reads `imd2025.rank`.

## History

- **2026-07-10, run-log 52 and 53:** the first load of 2026-27 and the crosswalk. Both scripts read the file as "title row, header row, data from row 2", but the file's row 0 is the header, so Ashford was consumed as a header and 151 of 152 loaded; run 53's note says Ashford was added and 17 shapefile names reconciled. I do not claim which script wrote rows 52 and 53.
- **2026-08-20, run-log 97:** an accidental `import s14_lha_rates_build_v2` (to check that the file parsed) dropped and rebuilt both tables: 37 mapping names back in the "&" form and Ashford missing. Repaired by direct SQL with no run-log row (Ashford restored, its `source` says so; the 37 names normalised). Record: `docs/decisions/2026-08-21-s14-brma-join-break.md`.
- **2026-10-11, run-log 499 and 501:** the same scripts ran by accident again, started by tests written before the guards. v1 upserted both tables and v2 dropped and rebuilt them: 37 authorities again unmatched in W1's join, Ashford's own `source` text lost, `loaded_at` reset on every row, and the view `v_la_rate_triangulation` dropped by the `CASCADE`. Repaired the same evening (commit `69ca0ec`): all survey hashes reproduced exactly, 0 unmatched, the view recreated at 155 rows (its owner and comment before the incident are not recorded). Nothing published was affected: W1, the map, the export and any push were not run. Run-log 499 and 501 are kept as the record. Record: `docs/decisions/2026-10-11-s3-s14-accidental-rebuild.md`.
- **2026-10-11:** the scripts and the n8n workflows "LHA Rates + List of Rents (S14)" and "VOA/DWP LHA Rates (S14)" are retired (the second has no nodes); editions tables created; edition 1 "as loaded" for both (run-log 519); crosswalk edition 2 (run-log 520). The record, with the before-state hashes, is `docs/decisions/2026-10-11-s14-editions-first-load.md`.

**When 2027-28 loads, update `v_la_rate_triangulation`.** The view hard-codes `lha.financial_year = '2026-27'` (it is defined in `s20_ratecard_neutralise_object_names.py` and `s20_ratecard_integration_build.py`, and pinned to the current rate card by gate 21 of `verify_source_registry.py`, which does not check the LHA year). After 2027-28 is the newest year in `brma_lha_rates`, the view silently keeps comparing the rate card with 2026-27 LHA. Change the year in the view definition (and in the gate, if the card pin changes) as a deliberate step of the 2027-28 load; it was not changed on 2026-10-12.
