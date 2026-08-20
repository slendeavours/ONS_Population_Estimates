# Source 15 — Land Registry UK House Price Index

| Field | Value |
|---|---|
| Publisher | HM Land Registry / Office for National Statistics |
| Dataset | UK House Price Index: average prices and property type breakdowns |
| Cadence | Monthly (published ~6 weeks after the reference month) |
| Landing page | `https://www.gov.uk/government/collections/uk-house-price-index-reports` |
| Geography | English local authorities (295 of 296 — Isles of Scilly not published due to low transaction volumes) |
| Join key | `lad24cd` (via `la_code_lookup` and hard recodes for post-boundary-change Barnsley/Sheffield) |
| Target table | `la_house_prices` (grain: lad24cd × period) |
| MIN_PERIOD | 2022-01-01 |
| First load | April 2026 edition, loaded 2026-07-14 (Claude Code run, `pipeline_run_log`) |
| Refresh | Re-run `scripts/s15_hpi_build.py` when a new edition is published |

## What it provides

Monthly average house price (all properties) and by property type (detached, semi-detached, terraced, flat), plus seasonally adjusted all-property price and year-on-year percentage change, per English local authority. This is the property-cost dimension of the operating-model analysis — lower average prices indicate areas where acquisition costs are more favourable.

## Acquisition pattern

The file URL changes every edition. Resolve it dynamically:
1. Fetch the collections page (stable URL above)
2. Extract the first `/government/statistical-data-sets/uk-house-price-index-data-downloads-*` link
3. From that page, extract `Average-prices-{YYYY}-{MM}.csv` and `Average-prices-Property-Type-{YYYY}-{MM}.csv`

Every edition republishes the full back series from 1968 (all-property) / 1995 (by type). Only data from 2022-01-01 onward is loaded. The monthly upsert both inserts new months and revises any previously provisional values.

## Code handling

From the April 2025 edition onward, Land Registry publishes Barnsley as E08000038 and Sheffield as E08000039 (post-boundary-change codes). These are hard-recoded to the pipeline's canonical LAD24 codes (E08000016, E08000019) before loading. All other codes are reconciled via `la_code_lookup` or matched directly against `la_boundaries`.

## Caveats

1. **Open-market prices only**: HPI reflects open-market sale prices. Right-to-buy, shared ownership, and sub-market transactions are excluded where identifiable.
2. **Suppression in small LAs**: the Land Registry suppresses average prices where transaction volumes are too low for statistical reliability. These are stored as NULL, not estimated.
3. **Isles of Scilly excluded**: E06000053 has no HPI data published (population ~2,200, negligible transaction volume).
4. **Publication lag**: editions are published ~6 weeks after the reference month, so the most recent period in the table will typically be 2–3 months behind the current date.
