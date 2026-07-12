# S18 — ONS PIPR Workbook Structure

Specification for the ONS Price Index of Private Rents (PIPR) monthly workbook, recorded from programmatic inspection of the **17 June 2026 edition**. This document is the build specification for the recurring n8n S18 monthly workflow. Facts below are properties of the publication format; where something is edition-specific it is labelled as such.

## Acquisition

- Landing page (stable URL, never changes):
  `https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/priceindexofprivaterentsukmonthlypricestatistics`
- The page lists editions newest-first. The **first** xlsx link is the current edition. Href pattern:
  `/file?uri=/economy/inflationandpriceindices/datasets/priceindexofprivaterentsukmonthlypricestatistics/<edition-slug>/<filename>.xlsx`
- The edition slug is the publication date (e.g. `17june2026`). The filename carries an unpredictable numeric suffix (this edition: `priceindexofprivaterentsukmonthlypricestatistics13.xlsx`) — **never hardcode the file URL**; always re-extract it from the landing page.
- File size ~17–18 MB (this edition: 18,262,007 bytes). Sanity gate: reject downloads under 10 MB.
- Every edition contains the **full back series from January 2015** and revises prior provisional months, so only the latest edition is ever needed.

## Worksheets

| Sheet | Content |
|---|---|
| `Cover sheet` | Title, publication date ("originally published at 9:30am on 17 June 2026"), contacts, related links. No data. |
| `Contents` | Table of contents; a single entry, "Table 1". |
| `Notes` | 8 numbered notes (see Conventions below). |
| `Table 1` | The only data sheet. All geographies, all periods, wide format. |

## Table 1 layout

- Row 1: title. Row 2: sheet description. **Row 3: column headers** (1-indexed; `header=2` in pandas `read_excel`). Data from row 4.
- This edition: 48,909 data rows × 40 columns (A–AN). No merged cells in the data area; parses clean with pandas.
- 4 identity columns then 9 measure blocks of 4 columns each, in fixed order:

| Cols | Block |
|---|---|
| 1–4 | `Time period`, `Area code`, `Area name`, `Region or country name` |
| 5–8 | `Index`, `Monthly change`, `Annual change`, `Rental price` (all properties) |
| 9–12 | same four, suffix ` one bed` |
| 13–16 | suffix ` two bed` |
| 17–20 | suffix ` three bed` |
| 21–24 | suffix ` four or more bed` |
| 25–28 | suffix ` detached` |
| 29–32 | suffix ` semidetached` |
| 33–36 | suffix ` terraced` |
| 37–40 | suffix ` flat maisonette` |

- `Time period` is a true datetime, first of month.
- Periods in this edition: 2015-01-01 to 2026-05-01 (137 months). Latest month = period one month before publication month.

## Geographies present

| Code prefix | Geography | Count (this edition) |
|---|---|---|
| K02 / K03 | UK / Great Britain | 1 each |
| E92 / W92 / S92 / N92 | Countries | 1 each |
| E12 | English regions | 9 |
| E06 / E07 / E08 / E09 | **English LAs — the target rows** | 294 (62 / 164 / 36 / 32) |
| W06 | Welsh LAs | 22 |
| S33 | Scottish Broad Rental Market Areas | 18 |
| `[z]` in `Area code` | Northern Ireland BRMAs (named in `Area name`) | 8 |

Quirks:

- **Isles of Scilly (E06000053) and City of London (E09000001) are absent entirely** — 294 English LAs, not 296. Expect coverage of 294 per period, every period.
- Sub-national data for Scotland and NI is BRMA-level, not LA-level. NI BRMA rows have the literal string `[z]` as their area code — filter on GSS prefix, not on non-null code.
- **Area codes are the codes current at publication, applied to the whole back series.** This edition uses `E08000038` (Barnsley) and `E08000039` (Sheffield) — the post-April-2025 codes from The Barnsley and Sheffield (Boundary Changes) Order 2024 (SI 1328/2024). The LAD24 codes `E08000016`/`E08000019` never appear. These two do not resolve through `la_code_lookup`; the S18 transform maps them to their LAD24 predecessors (`E08000038 → E08000016`, `E08000039 → E08000019`), verified against the ONS Code History Database (June 2026). Caveat: the areas differ by the small territory transferred from Barnsley to Sheffield on 1 April 2025, so pre/post-2025 figures are on marginally different footprints — immaterial at LA-rent granularity but recorded here. When the pipeline moves to a LAD25+ boundary set this remapping must be revisited.

## Conventions and markers

- `[x]` = not available (e.g. no annual change for months without a year-earlier comparison; within England LA rows this edition, only in change columns for 2015/2016 periods). `[z]` = not applicable.
- **No in-file provisional marker exists** (no `[p]` anywhere). The convention, per the accompanying ONS bulletin, is: **the latest period only is provisional** and is revised in the following edition. S18 sets `provisional = TRUE` for the latest period in the file, and each monthly re-run's upsert flips the prior month to `FALSE` when the new edition finalises it.
- Index reference: January 2023 = 100 (note 2). Not seasonally adjusted (note 1).
- Rounding (note 6): rental prices rounded to nearest £1 (integers in file); index and change values stored to 6 dp.
- Bedroom categories: **ONS combines studios into the one-bedroom category.**
- No suppression observed in the load window (period ≥ 2024-03-01): zero `[x]`/`[z]`/blank values in any measure column for English LA rows.

## Mapping to `la_private_rents`

Target grain: one row per `(lad24cd, period, breakdown_type, category)`.

| Source block | `breakdown_type` | `category` |
|---|---|---|
| unsuffixed | `all` | `all` |
| ` one bed` | `bedroom` | `1_bed` |
| ` two bed` | `bedroom` | `2_bed` |
| ` three bed` | `bedroom` | `3_bed` |
| ` four or more bed` | `bedroom` | `4_plus_bed` |
| ` detached` | `property_type` | `detached` |
| ` semidetached` | `property_type` | `semi_detached` |
| ` terraced` | `property_type` | `terraced` |
| ` flat maisonette` | `property_type` | `flat_maisonette` |

Per block: `Rental price` → `mean_rent`, `Index` → `rent_index`, `Annual change` → `annual_pct_change`. The `Monthly change` columns are **not loaded** (not in the target schema). Values are rounded to 2 dp on load to match the `NUMERIC(8,2)`/`NUMERIC(6,2)` schema — observed ranges (window ≥ 2024-03: rents £376–£5,669; index ≈ 81–130; annual change within ±10) fit the schema as specified in the build prompt; no precision change was needed.

Row filter: GSS prefix in (E06, E07, E08, E09) AND period ≥ `MIN_PERIOD` (default 2024-03-01). Expected volume: 294 LAs × 9 categories × months in window (27 months for this edition = 71,442 rows).
