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

**National row (`staging_national`).** The same three columns exist at England level with a different rule, because summing every authority that reports in each quarter compares two different sets of authorities:

| Column | Type | Description |
|---|---|---|
| `ta_households_current` | integer | England total of authorities reporting TA (> 0) in the latest quarter |
| `ta_households_prev_year` | integer | England total of authorities reporting TA (> 0) in the same quarter a year earlier. **Not comparable with `ta_households_current`**: the sets of authorities differ |
| `ta_matched_authorities` | integer | Authorities reporting TA (> 0) in both quarters |
| `ta_households_current_matched` | integer | Latest-quarter TA households in those authorities |
| `ta_households_prev_year_matched` | integer | Prior-year TA households in the same authorities |
| `ta_yoy_pct` | numeric(8,2) | `((current_matched - prev_year_matched) / prev_year_matched) * 100`: like-for-like. Quote this, not a percentage from the first two columns. Runs 22 and earlier used the all-reporting totals (run 22: +13.29%; matched set +3.14%) |

**`ta_trend_label` values:**

| Value | Meaning |
|---|---|
| `rising_strongly` | YoY increase > +15% |
| `rising` | YoY increase +5% to +15% |
| `flat` | YoY change within ±5% |
| `falling` | YoY decrease -5% to -15% |
| `falling_strongly` | YoY decrease > -15% |
| `submission_gap` | LA has not submitted data for one or more recent quarters |

### Statutory homelessness editions (S1)

`la_statutory_homelessness_editions` is append-only (update, delete and truncate are blocked by trigger). Key `(lad24cd, period, edition)`, `period` a financial-year quarter (`YYYYQn`; 2023Q2 is July to September 2023).

| Column | Description |
|---|---|
| `edition` | Integer, 1 = first loaded. |
| `supersedes` | The edition this one replaces; NULL for the first. |
| measure columns | The six measures held in `la_statutory_homelessness` (`total_assessments`, `owed_duty`, `prevention_duty`, `relief_duty`, `households_in_ta`, `support_needs_total`). Suppressed values (`..`, `-`) are NULL, never 0. |
| `release_label`, `published_date` | Description and date of the release. Informational only: for loaded revisions the file's HTTP Last-Modified date, for edition 1 the load date. They do not decide which edition is latest. |
| `mental_health_suspect`, `learning_disability_suspect`, `drug_dependency_suspect`, `alcohol_dependency_suspect`, `rough_sleeping_history_suspect` | The five quarantined support-need columns; all NULL on the editions table. |
| `source_url`, `source_file`, `source_sha256`, `loaded_at` | Provenance. `source_sha256` is the checksum of the source file for an edition loaded from a file; for edition 1 of a quarter loaded 'as loaded' (no source file in hand) it is the sha256 of a canonical text rendering of the stored rows, sorted by `lad24cd`. |

**Live-layer rule.** `la_statutory_homelessness` holds, for each authority and period, the latest edition, where latest is the edition that no other edition supersedes. It is refreshed with `python scripts/s1_editions.py refresh-latest --commit` and changes only the six measures, `source_file` and `extracted_at`; `*_suspect` and `loaded_at` are never touched. A quarter with one edition is identical in both tables. Since 2026-10-07 `refresh-latest` is general (any quarter whose live rows differ from its latest edition, with before/after guards in the same transaction), `sync-new` records a new live quarter as edition 1 'as loaded', and `status` reports what needs action; the procedure is in [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md). A brand-new quarter is loaded with `python scripts/s1_editions.py load-new` (since 2026-10-07; the n8n S1 loader steps are retired), which stores suppressed figures as NULL and a real zero as 0 and records edition 1 and the live rows together. The October 2026 snapshot table `la_statutory_homelessness_bak_20261006` is retained but nothing depends on it.

### Support-needs editions (S1b)

`la_homelessness_support_needs_editions` is append-only (update, delete and truncate are blocked by trigger). Key `(lad24cd, period, category_code, edition)`; 174,640 rows. `period` is a financial-year quarter (`YYYYQn`).

| Column | Description |
|---|---|
| `edition` | Integer, 1 = first loaded. |
| `supersedes` | The edition this one replaces; NULL for the first. |
| `value`, `value_flag` | The A3 cell: a count, or NULL with a flag (`suppressed`, `missing`, `not_applicable`). A real zero is `value` 0 with no flag; a suppressed cell is never 0. |
| `category_code`, `category_group`, `category_label` | The A3 category, its group (`support_need`, `needs_breakdown`, `needs_total`, `duty_total`) and the sheet header text. The label embeds that file's England total, so it differs between editions. |
| `reference_quarter`, `layout_version`, `publisher_la_code` | As in the live table; `publisher_la_code` is the code as published (Barnsley and Sheffield resolve to E08000016 and E08000019 in `lad24cd`). |
| `source_url`, `source_edition`, `edition_variant`, `release_page_url` | The file the edition was read from; `edition_variant` is `original`, `revised`, `corrected` or `fixed`. |
| `release_label`, `published_date` | Description and date of the release. Informational only; they do not decide which edition is latest. |
| `source_file`, `source_sha256`, `loaded_at` | The source file name and the insert time. `source_sha256` is the checksum of the local source file for an edition loaded from a file; for edition 1 of a quarter loaded 'as loaded' (no source file in hand) it is the sha256 of a canonical text rendering of the stored rows, sorted by `lad24cd`, `category_code`. |

**Live-layer rule.** `la_homelessness_support_needs` holds, for each authority, period and category, the latest edition (the one no other edition supersedes). It is refreshed with `python scripts/s1b_editions.py refresh-latest --commit` and changes only `value`, `value_flag`, `source_url`, `source_edition`, `edition_variant` and `category_label`; `loaded_at` is never touched. A quarter with one edition is identical in both tables. Edition 2 exists for 2023Q2 to 2024Q4 (the registry 'revised' files) and 2025Q2 (the revision of 30 April 2026). Since 2026-10-07 `refresh-latest`, `sync-new` and `status` are general, as for S1; see [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md). The snapshot tables `la_homelessness_support_needs_bak_20261006` and `_bak_20261006b` are retained but nothing depends on them.

---

## Rough Sleeping

Source: DLUHC annual rough sleeping snapshot count

| Column | Type | Range | Description |
|---|---|---|---|
| `rough_sleeping_current` | integer | 0 – 400+ | People sleeping rough on the snapshot night (annual count) |
| `rough_sleeping_prev_year` | integer | 0 – 400+ | Prior year rough sleeping count |

---

## Care Leavers

Source: DfE Children Looked After in England including adoptions, SSDA903 return (annual, year ending 31 March). Full source note: [s4_care_leaver_source.md](s4_care_leaver_source.md)

| Column | Type | Range | Description |
|---|---|---|---|
| `care_leavers_semi_indep` | integer | 0 – 500+ | Care leavers aged 17-21 in supported accommodation. Pipeline aggregate of three DfE categories: semi-independent transitional, foyers, supported lodgings. **Not** DfE's published category |

### Which measure to quote

Two measures are held on `care_leaver_accommodation` and they are not interchangeable.

| Column | Definition | Use |
|---|---|---|
| `semi_independent_published` | DfE's published `Semi-independent, transitional accommodation` alone | **External documents.** Reproducible directly from DfE |
| `semi_independent` | The above plus foyers plus supported lodgings | Internal analysis only, and only when labelled as a combined measure with its components named |

Liverpool 2025 is 188 on the published definition and 208 on the aggregate, ranking 11th and 20th of 155 respectively. The gap is material and quoting the wrong one is a reputational risk.

Suppressed cells are added as zero on the 17-21 path, so `semi_independent` and `total_care_leavers` are minima. `suppressed_flag` marks affected rows and `total_published` carries DfE's own Total.

From reporting year 2024 the DfE category means Ofsted-registered supported accommodation only. Do not trend across 2023/2024.

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
| `hb_sa_claimants_latest` | integer | 0 – 36,000+ | HB claimants in Specified Accommodation, latest month (S8b accommodation type breakdown) |

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

### Housing expenditure editions (RO4)

`ro4_housing_expenditure_editions` is append-only (update, delete and truncate are blocked by trigger). Key `(lad24cd, financial_year, edition)`; 1,184 rows (296 authorities in each of 2024-25 editions 1, 2 and 3 and 2025-26 edition 1). `financial_year` is `YYYY-YY` (e.g. `2024-25`). All spend figures are £ thousands, `numeric(12,2)`.

| Column | Description |
|---|---|
| `edition` | Integer, 1 = first recorded for that financial year. |
| `supersedes` | The edition this one replaces; NULL for the first. |
| `lad24cd`, `la_name` | Canonical authority code (Barnsley and Sheffield stay E08000016 and E08000019, as in the live table) and name. |
| `nightly_paid_ta_gross_exp_000`, `nightly_paid_ta_net_exp_000`, `hostels_gross_exp_000`, `hostels_net_exp_000`, `bb_gross_exp_000`, `bb_net_exp_000`, `hra_admin_prevention_relief_net_exp_000`, `total_homelessness_gross_exp_000`, `total_homelessness_net_exp_000`, `total_housing_gross_exp_000`, `total_housing_net_exp_000` | The eleven measures. A real zero is 0; an authority that has not reported, or whose cells the publisher marks `[x]`, is NULL. |
| `data_missing` | True where the authority's spending is not published in that release (equivalent to a NULL total homelessness gross). |
| `source` | Text label of the release the row came from. |
| `release_label`, `published_date` | Description and date of the release. Informational only; they do not decide which edition is latest. |
| `source_file`, `source_sha256`, `loaded_at` | The source file name and the insert time. `source_sha256` is the checksum of the local source file for an edition loaded from a file; for 2024-25 edition 1, recorded 'as loaded' (no source file in hand), it is the sha256 of a canonical text rendering of the stored rows, sorted by `lad24cd`. |

**Live-layer rule.** `ro4_housing_expenditure` holds, for each authority and financial year, the latest edition (the one no other edition supersedes). It is refreshed with `python scripts/ro4_editions.py refresh-latest --commit` and changes only the eleven measures, `la_name`, `data_missing` and `source`; `loaded_at` is never touched. A financial year with one edition is identical in both tables. 2024-25 has three editions: edition 1 is what the live table held before 2026-10-07 (published 18 Sep 2025), edition 2 is actually the second release (4 Dec 2025; its stored label wrongly says third release, and the append-only table keeps it), and edition 3 is the third release (published 11 Jun 2026), which is now in live. Eight authorities are NULL in every measure in edition 2 (the second release marks them `[x]`) and have figures in edition 3; in edition 1 their other measures have figures but their `hra_admin_prevention_relief_net_exp_000` and `total_housing_*` cells are NULL. The temporary-accommodation-administration defect in the hra column is in edition 1 only; editions 2 and 3 carry the right line. The first release of 2024-25 is not held; the chain records the order releases were loaded, not published. The manifest holds each file's stored and actual label. Procedure: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md).

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

## LHA Rates

Source: DWP Universal Credit Local Housing Allowance rates (FY 2026-27, frozen at April 2024 levels), published monthly per Broad Rental Market Area (BRMA). Weekly conversion: `weekly = monthly × 12 ÷ 52`, rounded to 2 dp. BRMA-to-LA mapping built via centroid spatial join against the VOA BRMA boundary layer (May 2020) — each LA is assigned the BRMA containing its centroid.

| Column | Type | Range | Description |
|---|---|---|---|
| `lha_brma_name` | text | — | Broad Rental Market Area the LA maps to (DWP CSV spelling) |
| `lha_sar_weekly` | numeric(8,2) | ~55 – 200 | Shared Accommodation Rate, £/week |
| `lha_1bed_weekly` | numeric(8,2) | ~90 – 340 | 1-bedroom LHA rate, £/week |
| `lha_2bed_weekly` | numeric(8,2) | ~110 – 440 | 2-bedroom LHA rate, £/week |
| `lha_3bed_weekly` | numeric(8,2) | ~130 – 520 | 3-bedroom LHA rate, £/week |
| `lha_4bed_weekly` | numeric(8,2) | ~170 – 720 | 4-bedroom LHA rate, £/week |

---

## Care Providers (Supply Side)

Source: CQC Care directory with filters (monthly). Only supply-side column in the pipeline — every other signal measures demand.

| Column | Type | Range | Description |
|---|---|---|---|
| `supported_living_locations` | integer | 0 – 800+ | Active, non-dormant CQC-registered supported living locations per LA. Dual-registered locations count twice (both registrations are regulated entities). |

---

## Discharge Delays

### S9a — DRD (Acute Discharge Ready Date)

Source: NHS England DRD monthly data (Official Statistics, SUS extract). Native geography is UTLA; apportioned to LAD via population-weighted `utla_lad_mapping`.

| Column | Type | Range | Description |
|---|---|---|---|
| `drd_bed_days_lost` | integer | 0 – 15,000+ | Total bed days lost to delayed discharge in the latest reporting month. Population-weighted for county→district apportionment. |
| `drd_pct_delayed_1plus_days` | numeric | 0 – 100 | Percentage of discharges delayed 1+ days. **UTLA-level pass-through:** districts under a county share the same value (no district-level breakdown published). |

### S9b — MHSDS CRFD (MHS26)

Source: NHS Digital Mental Health Services Monthly Statistics. MHS26 — Clinically Ready for Discharge delayed bed days (combined MH + LD/autism). Published at LAD level directly; no apportionment required.

| Column | Type | Range | Description |
|---|---|---|---|
| `crfd_days` | integer | 0 – 2,500+ | MHS26 CRFD delayed discharge days in the latest reporting month. NULL where suppressed at source (28–46% of LAs per month). |

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
