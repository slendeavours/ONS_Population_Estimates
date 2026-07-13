# S9 Build Summary — NHS Discharge Delays + MHSDS CRFD

## Sources

### S9a — NHS DRD Discharge Delays

- **Publisher:** NHS England
- **Series:** Discharge Ready Date (DRD) Monthly Data
- **Publication page:** `https://www.england.nhs.uk/statistics/statistical-work-areas/discharge-delays/discharge-delays-acute-data/`
- **Status:** Official Statistics (derived from SUS extract)
- **Native geography:** Upper-tier local authority (UTLA) — E06 unitaries, E08 metropolitan boroughs, E09 London boroughs, E10 counties
- **Date range loaded:** April 2024 – May 2026 (26 months)
- **Refresh cadence:** Monthly, approximately 6 weeks after the reporting month
- **Table:** `nhs_drd_discharge_delays`
- **Natural key:** `(reporting_period, utla_code)`
- **Row count:** 3,978 (153 UTLAs × 26 months)

### S9b — MHSDS MHS26 CRFD

- **Publisher:** NHS Digital (now NHS England)
- **Series:** Mental Health Services Monthly Statistics
- **Publication page:** `https://digital.nhs.uk/data-and-information/publications/statistical/mental-health-services-monthly-statistics`
- **Measure:** MHS26 — Clinically Ready for Discharge (CRFD) delayed discharge bed days
- **Cohort:** Combined mental health + learning disability + autism (no disaggregation available)
- **Native geography:** Local Authority of Responsibility or Residence (E06/E07/E08/E09 directly — no UTLA apportionment required)
- **Date range loaded:** April 2023 – May 2026 (38 months; mandatory CRFD reporting started April 2023)
- **Refresh cadence:** Monthly, approximately 6 weeks after the reporting month
- **Table:** `nhs_mh_crfd`
- **Natural key:** `(reporting_period, lad24cd, measure_id)`
- **Row count:** 11,248 (296 LAs × 38 months)
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
5. **Barnsley/Sheffield recode** — MHSDS switched from E08000016/019 to E08000038/039 from June 2025. Handled via `la_code_lookup` and `vw_mh_crfd_lad`.
6. **Apportionment resolution loss** — DRD percentage and average columns pass through at UTLA level for county districts. All districts under a county inherit the same percentage/average.

## Refresh Procedure

1. Download new monthly DRD file from the NHSE discharge delays page.
2. Parse UTLA Acceptable sheet, extract UTLA Aggregate rows.
3. Upsert into `nhs_drd_discharge_delays`.
4. Download new monthly MHSDS data file from the NHS Digital publication page.
5. Filter for MHS26 at LA level (E06/E07/E08/E09, SECONDARY_LEVEL=NONE).
6. Upsert into `nhs_mh_crfd`.
7. Re-run W1 to update `staging_la_signals` and `staging_tenant_type_rankings`.

## Outstanding Maintenance Items

- Monthly refresh not yet scheduled in n8n — flag as a future n8n workflow.
- Node 9 GeoJSON export query needs patching to include `drd_bed_days_lost`, `drd_pct_delayed_1plus_days`, and `crfd_days` in properties.
- `la_boundaries` retains old Barnsley/Sheffield codes; when updated, `la_code_lookup` entries should be reviewed.
