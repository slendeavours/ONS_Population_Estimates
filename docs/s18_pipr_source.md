# Source 18 — ONS Price Index of Private Rents (PIPR)

| Field | Value |
|---|---|
| Publisher | Office for National Statistics (ONS) |
| Dataset | Price Index of Private Rents, UK: monthly price statistics |
| Cadence | Monthly (published mid-month, covering the previous calendar month) |
| Landing page | `https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/priceindexofprivaterentsukmonthlypricestatistics` |
| Geography | English local authorities (294 of 296 — Isles of Scilly and City of London are not published) |
| Join key | `lad24cd` (via `la_code_lookup`, plus the shared `scripts/geography.py` recode — see below) |
| Target table | `la_private_rents` (grain: lad24cd × period × breakdown_type × category) |
| Editions table | `la_private_rents_editions` (append-only; key `lad24cd, period, breakdown_type, category, edition`). `la_private_rents` holds the latest edition of each month |
| Related tables | `la_geography`, `la_succession` (geography dimension, built in the same run) |
| Earliest period loaded | 2024-03-01 (`--min-period` widens it) |
| First load | 17 June 2026 edition, backfilled 11 July 2026 (Claude Code run, `pipeline_run_log` id 54) |
| Refresh | `python scripts/s18_pipr_editions.py load` (preview), then `--commit`; procedure in [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md). Gates: `scripts/s18_pipr_editions_verify.py` (23). Record: [decisions/2026-10-09-s18-editions-first-load.md](decisions/2026-10-09-s18-editions-first-load.md) |

## What it provides

Average monthly private rent (£), rent index (January 2023 = 100) and annual percentage change per LA, broken down by bedroom count (1, 2, 3, 4+; studios counted in 1-bed) and property type (detached, semi-detached, terraced, flat/maisonette), plus an all-properties total. This is the cost side of the yield-adjusted demand analysis (Layer 3).

## Acquisition gotcha — URL changes every edition

The landing page URL is stable; the file URL is not. Each monthly edition gets a new slug (publication date) **and** an unpredictable numeric filename suffix. Never hardcode the file URL — fetch the landing page, take the first (newest) xlsx link. Full pattern and workbook layout: [s18_pipr_workbook_structure.md](s18_pipr_workbook_structure.md).

Every edition republishes the full back series from January 2015, and the latest month is provisional and is re-published later, so every `load` compares every held month with the new workbook. The first comparison, on 2026-10-09, found no change in any held cell: the 16 September 2026 workbook is the one August 2026 was loaded from, so it is not yet shown that revisions occur in the months held. A change, including provisional to final, is stored as the next edition rather than overwriting.

The old pipeline (`s18_pipr_fetch.py`, `s18_pipr_transform.py`, `s18_pipr_load.py`, `s18_pipr_verify.py`, now in `scripts/historical/`) inserted new rows and ignored revisions of rows already held (`ON CONFLICT DO NOTHING`), so a revision would have been discarded. It must not be run: it stops with a RETIRED message. `scripts/s18_pipr_inspect.py`, which lists a workbook's sheets and headers, is kept as a read-only tool.

## Code handling

PIPR publishes the entire back series on the GSS codes current at publication. From the April 2025 editions onward this means Barnsley = `E08000038` and Sheffield = `E08000039` (The Barnsley and Sheffield (Boundary Changes) Order 2024, SI 1328/2024). S18 declares the form `new` in `scripts/geography.py`; the loader resolves them to the pipeline's canonical LAD24 codes (`E08000016`, `E08000019`) through it, and halts if a file carries the old codes. `la_code_lookup` is deliberately unchanged. The successor relationships live in `la_geography` / `la_succession`.

## Caveats

1. **Housing-benefit tenancies excluded**: ONS removes tenancies in receipt of housing benefit where identifiable. Figures represent open-market opportunity cost, **not** HB-supported rents.
2. **Stock-based measure**: new and existing tenancies are blended, so PIPR lags the price of a newly agreed lease in a rising market.

Both caveats are encoded in the `COMMENT ON TABLE la_private_rents`.

## Dual-model note

- **UCWS lens (primary)**: market rent is the cost side of operator margin — what the operator forgoes (or pays) relative to LHA/exempt income on the same unit. Feeds the LHA-vs-market-rent spread (S14 `brma_lha_rates` × `la_brma_mapping` × `la_private_rents`).
- **HSS lens (context)**: local rent pressure as a driver of homelessness demand and out-of-area placement flows.

Raw rent levels do **not** go on the demand map; the derived income-vs-rent spread will, as separate future work.
