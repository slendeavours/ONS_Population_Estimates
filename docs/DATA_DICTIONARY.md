# Data Dictionary — UCWS DV Signals

All columns available in `la_boundaries.geojson` (GeoJSON properties) and `staging_la_signals_latest.json`.

---

## Geographic Identifiers

| Column | Type | Range / Values | Description |
|---|---|---|---|
| `lad24cd` | text | E06xxxxx – E09xxxxx | ONS Local Authority District code (2024 boundaries). Primary join key. |
| `la_name` | text | — | Official LA name (from ONS boundary data or pipeline lookup) |
| `population` | integer | ~8,000 – 1,100,000 | Mid-year population estimate (ONS MYE 2024) |

---

## Temporary Accommodation (TA)

Source: DLUHC H-CLIC statutory homelessness return (quarterly)

| Column | Type | Range | Description |
|---|---|---|---|
| `ta_households_current` | integer | 0 – 15,000+ | Households in temporary accommodation at end of latest quarter |
| `ta_households_prev_year` | integer | 0 – 15,000+ | TA households same quarter the prior year (for YoY comparison) |
| `ta_yoy_pct` | numeric(8,2) | -100 – +500+ | Year-on-year percentage change: `((current - prev) / prev) * 100` |
| `ta_trend_label` | text | See below | Trend classification based on YoY movement and data completeness |

**`ta_trend_label` values:**

| Value | Meaning |
|---|---|
| `rising_strongly` | YoY increase > +15% |
| `rising` | YoY increase +5% to +15% |
| `flat` | YoY change within ±5% |
| `falling` | YoY decrease -5% to -15% |
| `falling_strongly` | YoY decrease > -15% |
| `submission_gap` | LA has not submitted data for one or more recent quarters |

---

## Rough Sleeping

Source: DLUHC annual rough sleeping snapshot count

| Column | Type | Range | Description |
|---|---|---|---|
| `rough_sleeping_current` | integer | 0 – 400+ | People sleeping rough on the snapshot night (annual count) |
| `rough_sleeping_prev_year` | integer | 0 – 400+ | Prior year rough sleeping count |

---

## Care Leavers

Source: DfE children in need return (annual)

| Column | Type | Range | Description |
|---|---|---|---|
| `care_leavers_semi_indep` | integer | 0 – 500+ | Care leavers in semi-independent or supported accommodation placements |

---

## Domestic Violence (MARAC)

Source: SafeLives MARAC dataset (annual by LA)

| Column | Type | Range | Description |
|---|---|---|---|
| `marac_cases` | numeric(10,2) | 0 – 2,000+ | MARAC (Multi-Agency Risk Assessment Conference) cases discussed |
| `marac_rate_per_10k` | numeric(10,6) | 0 – 50+ | MARAC cases per 10,000 population — deprivation-adjusted demand intensity indicator |

---

## Housing Benefit

Source: DWP STAT-Xplore Housing Benefit caseload data

| Column | Type | Range | Description |
|---|---|---|---|
| `hb_sa_caseload` | integer | 0 – 5,000+ | Housing Benefit claimants who are asylum seekers (proxy for exempt accommodation pressure) |

---

## Social Housing Register

Source: DLUHC CORE / LA housing register returns

| Column | Type | Range | Description |
|---|---|---|---|
| `housing_register` | integer | 0 – 30,000+ | Households on the social housing waiting list |

---

## Local Authority Expenditure (RO4)

Source: MHCLG RO4 housing revenue account return

All spend figures are in **£ thousands (£000s)**. Multiply by 1,000 for £ sterling.

| Column | Type | Range | Description |
|---|---|---|---|
| `ro4_bb_spend_000` | numeric(12,2) | 0 – 50,000+ | LA gross expenditure on Bed & Breakfast accommodation (£000s) |
| `ro4_nightly_spend_000` | numeric(12,2) | 0 – 100,000+ | LA expenditure on nightly-paid / SWEP accommodation (£000s) |
| `ro4_total_homelessness_000` | numeric(12,2) | 0 – 200,000+ | Total LA gross expenditure on homelessness services (£000s) |

---

## Fiscal Risk Flags

| Column | Type | Values | Description |
|---|---|---|---|
| `efs_flag` | boolean | true / false | LA is receiving Exceptional Financial Support from MHCLG. `true` = currently supported. |
| `s114_flag` | boolean | true / false | LA has issued a Section 114 notice under the Local Government Finance Act 1988 (effective budget declaration of inability to balance). `true` = notice issued. |

---

## Deprivation

Source: MHCLG English Indices of Deprivation 2019 (updated from 2025 supplementary data)

| Column | Type | Range | Description |
|---|---|---|---|
| `imd_rank_of_average_rank` | integer | 1 – 317 | Rank of average rank across all LSOA-level IMD domains. **Lower rank = more deprived overall.** Note: this is at LA level, not LSOA level. |

---

## Data Quality

| Column | Type | Description |
|---|---|---|
| `data_quality` | jsonb | Per-source quality flags as JSON object. Keys match source names. Values include: `"ok"`, `"submission_gap"`, `"estimated"`, `"no_data"`, `"partial"`. |

Example:
```json
{
  "ta": "ok",
  "rough_sleeping": "ok",
  "care_leavers": "estimated",
  "marac": "submission_gap",
  "hb_sa": "ok"
}
```

---

## Notes on NULL Values

- NULL in a numeric column means the data was not available from the source for this LA in this run.
- NULL does **not** mean zero. A NULL `rough_sleeping_current` means no data was ingested; `0` means the LA reported zero rough sleepers.
- The `data_quality` JSONB column explains why a value may be NULL.
- In the map viewer, NULLs are displayed as `—` and do not affect colour scaling (treated as 0 for colour bands).

---

## Join Key

All tables join on `lad24cd` (the ONS LAD 2024 code). This is the canonical join key throughout the pipeline.

```sql
LEFT JOIN staging_la_signals sig
  ON sig.lad24cd = b.lad24cd
  AND sig.run_id = (SELECT MAX(run_id) FROM staging_la_signals)
```
