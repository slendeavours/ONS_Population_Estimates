# Source 15 — Land Registry UK House Price Index

| Field | Value |
|---|---|
| Publisher | HM Land Registry / Office for National Statistics |
| Dataset | UK House Price Index: average prices and property type breakdowns |
| Cadence | Monthly (published ~6 weeks after the reference month) |
| Landing page | `https://www.gov.uk/government/collections/uk-house-price-index-reports` |
| Geography | English local authorities (295 of 296 — Isles of Scilly not published due to low transaction volumes) |
| Join key | `lad24cd` (via `la_code_lookup` and the `scripts/geography.py` recode for post-boundary-change Barnsley/Sheffield) |
| Target table | `la_house_prices` (grain: lad24cd × period) |
| MIN_PERIOD | 2022-01-01 |
| First load | April 2026 edition, loaded 2026-07-14 (Claude Code run, `pipeline_run_log`) |
| Loader | `scripts/s15_hpi_editions.py` (verify: `scripts/s15_hpi_editions_verify.py`); the old `s15_hpi_build.py` is archived in `scripts/historical/` |
| Refresh | Monthly: `python scripts/s15_hpi_editions.py load`, then `--commit` (steps in `docs/QUARTERLY_REFRESH.md`) |
| Edition history | `la_house_prices_editions`; first load 2026-10-09 (see Revision status) |

## What it provides

Monthly average house price (all properties) and by property type (detached, semi-detached, terraced, flat), plus seasonally adjusted all-property price and year-on-year percentage change, per English local authority. This is the property-cost dimension of the operating-model analysis — lower average prices indicate areas where acquisition costs are more favourable.

## Acquisition pattern

The file URL changes every edition. Resolve it dynamically:
1. Fetch the collections page (stable URL above)
2. Extract the first `/government/statistical-data-sets/uk-house-price-index-data-downloads-*` link
3. From that page, extract `Average-prices-{YYYY}-{MM}.csv` and `Average-prices-Property-Type-{YYYY}-{MM}.csv`

Every edition republishes the full back series from 1968 (all-property) / 1995 (by type). Only data from 2022-01-01 onward is loaded.

## Loading

`scripts/s15_hpi_editions.py` reads the collections page, follows the newest data downloads page and downloads the two CSVs to `data/raw/`, or reads two files given with `--avg-prices` and `--property-type`. Both file names and the data's latest `Date` must name the same edition, or it halts before anything is stored. `load` compares every held month cell by cell and previews it (new, unchanged or revised); `load --commit` stores a new month as edition 1 together with its live rows, and a revised month as the next edition; `refresh-latest --commit` copies the latest edition of each month into `la_house_prices`. A short month (fewer than 295 authorities) or an unresolved area code is refused. Each month is held as numbered editions in `la_house_prices_editions` (append-only), and `la_house_prices` is the latest edition of each month.

## Revision status

Established: this source revises. The first comparison, on 2026-10-09, of the July 2026 release against what was held found 14 of 55 months revised (May 2025 to June 2026): 3,540 area-month rows and 20,513 changed cells. The 41 months before May 2025 and the latest month were unchanged. Revisions are small (the largest, 8.92%, was the City of London in March 2026) but grow towards the recent months. Whether older months have settled for good is a reading of this one comparison, not something the source states. Record: [decisions/2026-10-08-s15-editions-first-load.md](decisions/2026-10-08-s15-editions-first-load.md).

## Code handling

From the April 2025 edition onward, Land Registry publishes Barnsley as E08000038 and Sheffield as E08000039 (post-boundary-change codes). These are recoded to the pipeline's canonical LAD24 codes (E08000016, E08000019) through `scripts/geography.py` (source `15` declaration) before loading; a file in the other form halts before any write. All other codes are reconciled via `la_code_lookup` or matched directly against `la_boundaries`.

## Caveats

1. **Open-market prices only**: HPI reflects open-market sale prices. Right-to-buy, shared ownership, and sub-market transactions are excluded where identifiable.
2. **Suppression in small LAs**: the Land Registry suppresses average prices where transaction volumes are too low for statistical reliability. These are stored as NULL, not estimated.
3. **Isles of Scilly excluded**: E06000053 has no HPI data published (population ~2,200, negligible transaction volume).
4. **Publication lag**: editions are published ~6 weeks after the reference month, so the most recent period in the table will typically be 2–3 months behind the current date.
