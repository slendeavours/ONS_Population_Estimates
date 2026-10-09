# S9 Build Summary — NHS Discharge Delays + MHSDS CRFD

## Sources

### S9a — NHS DRD Discharge Delays

- **Publisher:** NHS England
- **Series:** Discharge Ready Date (DRD) Monthly Data
- **Publication page:** `https://www.england.nhs.uk/statistics/statistical-work-areas/discharge-delays/discharge-ready-date/` (corrected 2026-10-09; the earlier address was the acute discharge sitrep page, a different publication)
- **Status:** Official Statistics (derived from SUS extract)
- **Native geography:** Upper-tier local authority (UTLA) — E06 unitaries, E08 metropolitan boroughs, E09 London boroughs, E10 counties
- **Date range loaded:** April 2024 – August 2026 (29 months)
- **Refresh cadence:** Monthly, approximately 6 weeks after the reporting month
- **Table:** `nhs_drd_discharge_delays` (the latest edition of each month); every edition is kept in `nhs_drd_discharge_delays_editions`, with a ledger of files read in `nhs_drd_discharge_delays_editions_file_checks`
- **Natural key:** `(reporting_period, utla_code)`
- **Row count:** 4,437 live (153 UTLAs × 29 months); 4,590 edition rows
- **Revisions:** annual waves; a revised month is a `-Revised` webfile, stored as the next edition

### S9b — MHSDS MHS26 CRFD

- **Publisher:** NHS Digital (now NHS England)
- **Series:** Mental Health Services Monthly Statistics
- **Publication page:** `https://digital.nhs.uk/data-and-information/publications/statistical/mental-health-services-monthly-statistics`
- **Measure:** MHS26 — Clinically Ready for Discharge (CRFD) delayed discharge bed days
- **Cohort:** Combined mental health + learning disability + autism (no disaggregation available)
- **Native geography:** Local Authority of Responsibility or Residence (E06/E07/E08/E09 directly — no UTLA apportionment required)
- **Date range loaded:** April 2023 – August 2026 (41 months; mandatory CRFD reporting started April 2023)
- **Refresh cadence:** Monthly, approximately 6 weeks after the reporting month
- **Table:** `nhs_mh_crfd` (the latest edition of each month); every edition is kept in `nhs_mh_crfd_editions`, with a ledger of files read in `nhs_mh_crfd_editions_file_checks`
- **Natural key:** `(reporting_period, lad24cd, measure_id)`
- **Row count:** 12,136 live (296 LAs × 41 months); 22,792 edition rows
- **Files:** each month is first a Performance file, later reissued (`vN`) and, for April 2023 to March 2026, replaced by the year-end Final file (36 months revised, 5,038 area values changed, on 2026-10-09). Final files are loaded as edition 2; the old build script had no Final filter of its own; the Finals were kept out by the discovery in `verify_load_crfd.py` and the n8n-era load
- **MHS26 is NOT in timeseries files** — only available in individual monthly data files

## Mapping and Apportionment

### UTLA-to-LAD Mapping

- **Table:** `utla_lad_mapping`
- **Method:** E06/E08/E09 map 1:1 to LAD24CD (weight 1.0, method `direct`). E10 counties apportion to constituent E07 districts using 2024 mid-year population weights (method `population_weighted`).
- **Source:** ONS `LAD24_CTY24_EN_LU` lookup + `la_population` table
- **Row count:** 296

### LAD-Level Views

- **`vw_drd_discharge_delays_lad`** — Joins DRD data through `utla_lad_mapping`. Count columns are population-weighted; percentage and average columns pass through at UTLA level (districts under a county inherit the county value).
- **`vw_mh_crfd_lad`** — Resolves Barnsley (E08000038→E08000016) and Sheffield (E08000039→E08000019) code transitions via `la_code_lookup`. No further apportionment needed — MHSDS publishes at LAD level directly.

## Suppression Conventions

| Source | Markers | Handling |
|---|---|---|
| DRD | `-` (not applicable), `*` (suppressed) | Coerced to NULL |
| MHSDS | `*` (small number suppression) | Coerced to NULL |

MHSDS suppression rate: 28–46% of LAs per month (82–136 NULLs out of 296).

## W1 Integration

### New `staging_la_signals` Columns

| Column | Source | Coverage |
|---|---|---|
| `drd_bed_days_lost` | `vw_drd_discharge_delays_lad` (latest period) | 296/296 |
| `drd_pct_delayed_1plus_days` | `vw_drd_discharge_delays_lad` (latest period) | 296/296 |
| `crfd_days` | `vw_mh_crfd_lad` MHS26 (latest period) | 205/296 |

### New Tenant Types

| Tenant Type | Primary Signal | Signal Label | Confidence | Ranked LAs |
|---|---|---|---|---|
| `mental_health` | `crfd_days` | `combined_mh_ld_autism_crfd_days` | Medium | 205 |
| `learning_disability` | `crfd_days` | `combined_mh_ld_autism_crfd_days` | Medium | 205 |

Both types use the same MHS26 signal because MHSDS does not disaggregate by cohort at sub-national level.

### W1 Run

First run with S9 data: **run 10**.

## Data Confidence: Medium

- Combined MH+LD/autism cohort (no disaggregation)
- CRFD days is a volume metric (not rate-adjusted for population)
- 28–46% suppression rate
- DRD percentage columns are UTLA-level pass-through (resolution limitation for county districts)

## Known Caveats

1. **Acute sitrep deliberately deferred** — the NHS Acute Discharge Situation Report has no UTLA geography (Trust/ICB/Region only). DRD monthly files used as sole S9a source.
2. **UTLA Unacceptable sheets excluded** — only UTLA Acceptable sheets loaded. Unacceptable trusts have data quality issues flagged by NHSE.
3. **May 2024 definitions break** — DRD data definitions changed 27 May 2024. Data prior to April 2024 excluded from loading (not comparable).
4. **Cohort disaggregation gap** — MHS26 covers MH+LD/autism combined. No disaggregated source identified. Deferred until available, not a design limitation.
5. **Barnsley/Sheffield recode** — The form is per file, not per month (declared 'mixed' in `scripts/geography.py`, evidence in its `DATASET_FORM`): the year-end Final files carry E08000016/019 to March 2025 and E08000038/039 from April 2025, while the Performance files carry E08000016/019 to May 2025 (so the April and May 2025 Performance files still have the old codes) and E08000038/039 from June 2025. The loader resolves both to the old codes; `vw_mh_crfd_lad` also handles the transition via `la_code_lookup`.
6. **Apportionment resolution loss** — DRD percentage and average columns pass through at UTLA level for county districts. All districts under a county inherit the same percentage/average.

## Refresh Procedure

Both sources use one loader each on the editions engine (see `docs/QUARTERLY_REFRESH.md`, S9 section, for the monthly steps).

1. S9a: `python scripts/s9a_drd_editions.py load` previews; `load --commit` stores new months and revisions as editions.
2. S9b: `python scripts/s9b_crfd_editions.py load` previews (a preview reads about 40 files); `load --commit` stores them.
3. If anything was revised, `refresh-latest` previews, then `--commit` copies the latest edition to the live table (and moves live `source` to the new file URL).
4. `status` and the `*_editions_verify.py` scripts should pass.
5. Re-run W1 (and `refresh_map.py`) only if the latest month changed: W1 reads the latest month only.

The old upsert scripts are retired to `scripts/historical/`.

## Outstanding Maintenance Items

- Monthly refresh not yet scheduled in n8n — flag as a future n8n workflow.
- Node 9 GeoJSON export query needs patching to include `drd_bed_days_lost`, `drd_pct_delayed_1plus_days`, and `crfd_days` in properties.
- `la_boundaries` retains old Barnsley/Sheffield codes; when updated, `la_code_lookup` entries should be reviewed.
