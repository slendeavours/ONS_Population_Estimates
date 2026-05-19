# Methodology — UCWS DV Pipeline

---

## Data Sources

| # | Source | Metric(s) | Publisher | Frequency |
|---|---|---|---|---|
| 1 | DLUHC H-CLIC | TA households (current + prev year), trend label | DLUHC | Quarterly |
| 2 | DLUHC Rough Sleeping Snapshot | Rough sleeping counts | DLUHC | Annual (autumn) |
| 3 | DfE SEN2 / Children in Need | Care leavers in semi-independent housing | DfE | Annual |
| 4 | SafeLives MARAC data | MARAC cases, rate per 10k | SafeLives | Annual |
| 5 | DWP STAT-Xplore | Housing Benefit asylum seeker caseload | DWP | Monthly/quarterly |
| 6 | DLUHC CORE/CoRE | Social housing waiting list (register) | DLUHC | Annual |
| 7 | MHCLG RO4 | Homelessness expenditure (B&B, nightly, total) | MHCLG | Annual |
| 8 | MHCLG EFS | Exceptional Financial Support recipients | MHCLG | Published as issued |
| 9 | Published S.114 notices | Section 114 / budget insolvency notices | LAs / MHCLG | Published as issued |
| 10 | MHCLG IMD 2019 | Index of Multiple Deprivation | MHCLG | Every ~5 years |
| 11 | ONS Mid-Year Estimates | Population by LA | ONS | Annual |
| 12 | ONS Open Geography Portal | LA boundary polygons (LAD Dec 2024) | ONS | On boundary changes |

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
| NHS integration pending | NHS data (A&E, mental health) not yet integrated. Future pipeline version. |
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
