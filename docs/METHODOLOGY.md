# Methodology — UCWS DV Pipeline

---

## Data Sources

Source numbers follow `pipeline_run_log.source_number`, the pipeline's authoritative numbering (gaps intentional). An earlier version of this table used its own row numbers, which had drifted from the pipeline's.

| S# | Source | Metric(s) | Publisher | Frequency |
|---|---|---|---|---|
| 1 | DLUHC H-CLIC | TA households (current + prev year), trend label | DLUHC | Quarterly |
| 2 | MHCLG RO4 | Homelessness expenditure (B&B, nightly, total) | MHCLG | Annual |
| 3 | ONS Mid-Year Estimates | Population by LA | ONS | Annual |
| 3b | Census 2021 TS054 | Tenure | ONS | Decennial |
| 4 | DfE SEN2 / Children in Need | Care leavers in semi-independent housing | DfE | Annual |
| 5 | MHCLG IMD | Index of Multiple Deprivation | MHCLG | Every ~5 years |
| 7 | ONS Open Geography Portal | LA boundary polygons (LAD Dec 2024) | ONS | On boundary changes |
| 8 | DWP STAT-Xplore | Housing Benefit asylum seeker caseload | DWP | Monthly/quarterly |
| 8b | DWP Stat-Xplore HB (accommodation type) | HB claimants by accommodation type (SA, TA, Other, Unknown) per LA | DWP | Monthly |
| 10 | DLUHC Rough Sleeping Snapshot | Rough sleeping counts | DLUHC | Annual (autumn) |
| 11 | CQC Care directory with filters | Registered care locations with supported living, personal care and care home flags (supply side) | CQC | Monthly |
| 12 | MHCLG EFS / published S.114 notices | EFS support flag, S.114 notice flag | MHCLG / LAs | Published as issued |
| 13 | DLUHC LAHS | Social housing waiting list (register) | DLUHC | Annual |
| 14 | VOA/DWP LHA rates | LHA weekly rates (SAR, 1–4 bed) by BRMA, mapped to LAs | VOA/DWP | Annual (late January) |
| 15 | Land Registry UK HPI | Average house prices per LA (all property types), annual % change | HM Land Registry | Monthly |
| 17 | SafeLives MARAC data | MARAC cases, rate per 10k | SafeLives | Annual |
| 18 | ONS PIPR | Private market rent levels, index, annual change by LA (bedroom + property type) | ONS | Monthly |
| 19 | DWP Stat-Xplore PIP | PIP total claimants and enhanced daily living per LA (demand proxy for supported living) | DWP | Monthly |
| 9a | NHS DRD monthly | Bed days lost to delayed discharge, % delayed 1+ days (UTLA→LAD apportioned) | NHSE | Monthly |
| 9b | MHSDS MHS26 | CRFD delayed discharge days — combined MH+LD/autism (direct LA level) | NHS Digital | Monthly |

S11 is the pipeline's only supply-side source: every other source measures need, S11 records existing CQC-registered provision. It is stored agnostically like everything else; the pipeline does not score or rank markets.

---

## Pipeline Architecture

```
Raw Sources (CSV / API)
        │
        ▼
  n8n Workflow 1
  (17 ingestion nodes)
        │
        ▼
  PostgreSQL 16
  exempt_pipeline DB
  ┌─────────────────────────┐
  │ la_boundaries           │ (296 rows, GeoJSON polygons)
  │ staging_la_signals      │ (296 rows per run_id)
  │ staging_runs            │ (1 row per pipeline run)
  │ brma_lha_rates          │ (S14: LHA rates by BRMA)
  │ la_brma_mapping         │ (S14: LA → BRMA crosswalk)
  │ la_private_rents        │ (S18: PIPR rents by LA/period/category)
  │ cqc_locations           │ (S11: CQC-registered care locations)
  │ nhs_drd_discharge_delays│ (S9a: DRD discharge delays at UTLA level)
  │ nhs_mh_crfd             │ (S9b: MHSDS MHS26 CRFD at LA level)
  │ utla_lad_mapping        │ (S9: UTLA→LAD pop-weighted crosswalk)
  │ la_pip_claimants         │ (S19: PIP claimants by LA/month)
  │ la_hb_accom_type_caseload│ (S8b: HB accom type by LA/month)
  │ la_house_prices          │ (S15: Land Registry HPI by LA/period)
  │ la_geography            │ (geography dimension, code validity)
  │ la_succession           │ (predecessor → successor mappings)
  └─────────────────────────┘
        │
        ▼
  Node 9: Export Query
  (SQL Query 2 — full combined GeoJSON)
        │
        ▼
  Node 10: Validate
  (296 features, no NULLs, RFC 7946)
        │
        ▼
  Node 11: Publish to GitHub
  (git push via HTTPS token)
        │
        ▼
  GitHub raw URLs
  (la_boundaries.geojson, latest.json)
        │
        ▼
  Browser viewers
  (kepler_branded.html, kepler_basic.html)
```

---

## Key Calculations

### Year-on-Year % Change (TA)

```sql
ta_yoy_pct = ((ta_households_current - ta_households_prev_year)
              / NULLIF(ta_households_prev_year, 0)) * 100
```

Rounded to 2 decimal places. NULL when either input is NULL.

### Trend Label Assignment

Assigned from `ta_yoy_pct`:

```
ta_yoy_pct > +15%       → rising_strongly
+5% ≤ ta_yoy_pct ≤ +15% → rising
-5% ≤ ta_yoy_pct ≤ +5%  → flat
-15% ≤ ta_yoy_pct < -5% → falling
ta_yoy_pct < -15%       → falling_strongly
NULL or data gap         → submission_gap
```

### MARAC Rate per 10k Population

```sql
marac_rate_per_10k = (marac_cases / NULLIF(population, 0)) * 10000
```

### IMD Rank

The `imd_rank_of_average_rank` is sourced directly from MHCLG's published IMD LA summary. It ranks LAs from 1 (most deprived) to 317 (least deprived) based on the average rank of constituent LSOAs.

---

## GeoJSON Export

The combined export query joins `la_boundaries` and `staging_la_signals`:

```sql
SELECT jsonb_build_object(
  'type', 'FeatureCollection',
  'metadata', jsonb_build_object(
    'generated_at', NOW()::text,
    'run_id', (SELECT MAX(run_id) FROM staging_la_signals)::text,
    'feature_count', COUNT(*)::text
  ),
  'features', jsonb_agg(
    jsonb_build_object(
      'type', 'Feature',
      'geometry', b.geojson,
      'properties', jsonb_build_object(
        -- all 22 signal columns
      )
    )
  )
)
FROM la_boundaries b
LEFT JOIN staging_la_signals sig
  ON sig.lad24cd = b.lad24cd
  AND sig.run_id = (SELECT MAX(run_id) FROM staging_la_signals)
WHERE b.geojson IS NOT NULL;
```

Always uses `MAX(run_id)` to ensure the latest data is exported. Never hardcodes a run ID.

---

## Known Data Gaps & Limitations

| Issue | Detail |
|---|---|
| CRFD cohort disaggregation | MHS26 covers MH+LD/autism combined. No disaggregated source available. Both `mental_health` and `learning_disability` tenant types share the same signal. |
| DRD apportionment resolution | DRD % and average columns are UTLA-level pass-through for county districts. All E07 districts under an E10 county inherit the same value. |
| CRFD suppression rate | 28–46% of LAs have suppressed MHS26 values per month. These LAs are excluded from tenant-type rankings. |
| MARAC temporal lag | SafeLives publishes MARAC data 6–9 months after reference period. Current run may show prior year figures. |
| Care leaver data granularity | DfE data is at upper-tier LA level; district-level LAs may show NULL or estimated values. |
| IMD version | IMD 2019 is used (supplemented by 2025 LA summary). No full LSOA-level 2025 IMD released yet. |
| TA seasonality | H-CLIC is quarterly. The pipeline uses the most recent quarter end, which may vary by LA submission. |
| Rough sleeping count uncertainty | The DLUHC rough sleeping count is a single-night snapshot. Actual levels may be significantly higher. |

---

## Boundary Data

Boundaries are sourced from the ONS Open Geography Portal:
- Dataset: Local Authority Districts (December 2024) Boundaries UK BUC
- Format: GeoJSON, WGS84 (EPSG:4326)
- Simplified to approximately 20% of original vertex count for web performance
- Only English LAs included (296 districts, unitary authorities, metropolitan boroughs)

---

## Refresh Schedule

| Trigger | Action |
|---|---|
| Workflow 1 completes | n8n Node 9 exports GeoJSON + signals JSON |
| Export validated (296 features, no NULLs) | n8n Node 11 pushes to GitHub via git |
| GitHub receives push | Raw URLs update immediately |
| Browser opens viewer | Fetches latest GeoJSON from GitHub raw URL |

**Total latency from pipeline run to map update**: typically < 2 minutes.

---

## Contact

Pipeline and data questions: [sl@slendeavours.org](mailto:sl@slendeavours.org)

GitHub: [github.com/slendeavours/ONS_Population_Estimates](https://github.com/slendeavours/ONS_Population_Estimates)
