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
| `care_leavers_semi_indep` | integer | 0 – 500+ | Care leavers aged 17-21 in DfE's published category `Semi-independent, transitional accommodation` (`semi_independent_published`), latest year. NULL where DfE suppressed the cell, never 0. Up to W1 run 25 this column held the wider pipeline aggregate `semi_independent` (see below) |

### Which measure to quote

Two measures are held on `care_leaver_accommodation` and they are not interchangeable.

| Column | Definition | Use |
|---|---|---|
| `semi_independent_published` | DfE's published `Semi-independent, transitional accommodation` alone | **External documents, W1 and the map.** Reproducible directly from DfE |
| `semi_independent` | The above plus `foyers` plus `supported_lodgings`; NULL unless all six DfE parts (three categories, two age bands) are published | A pipeline aggregate, **not read by W1 or the map** since 2026-10-09. Cite only when labelled as a combined measure with its components named |

In 2025 the aggregate has a value for 17 of the 296 mapped authorities and the published category for 121. Liverpool 2025: 188 published; the aggregate held before the correction (208, which was 188 plus two partial sums of 10) is NULL because foyers and supported lodgings are suppressed for one age band. W1 run 25 mapped 208 for Liverpool; the next run maps 188.

### Suppression, NULL and the editions tables

Suppressed cells (`c`), not-applicable cells (`z`) and not-available cells (`x`) are NULL, never 0 (rule 1). A column built from several DfE categories is NULL unless every part is published, so `total_care_leavers` is NULL unless every bucket is; `total_published` carries DfE's own Total row. `null_reasons` (text) holds `column=reason` pairs separated by `;`, for example `foyers=suppressed;semi_independent=suppressed`, with the reasons `suppressed`, `not_applicable` and `not_available`. `suppressed_flag` marks a row where a cell contributing to the aggregate was suppressed. `unsuitable_pct` is no longer written: it is NULL in the live table and in editions 2 and 3; edition 1 keeps the 143 values the old workflow had written. Rows that are all `z` are not stored. The 22-25 rows hold the whole cohort (ages 22 to 25 summed); the table held age 25 only until 2026-10-09.

| Table | What it holds |
|---|---|
| `care_leaver_accommodation` | Live layer, the latest edition of each reporting year, keyed (`lad24cd`, `reporting_year`, `age_group`). W1 reads 17-21 for the latest year; the map export reads `MAX(reporting_year)`. `loaded_at` is the edition's (except the 7 Isles of Scilly 17-21 rows, whose values were unchanged by the 2026-10-09 correction and kept their earlier `loaded_at`), so a revision is visible to `refresh_map.py` |
| `care_leaver_accommodation_editions` | Append-only: one release's figures for one reporting year, both cohorts of that release, keyed (`lad24cd`, `age_group`, `reporting_year`, `edition`). Edition 1 of every year is the table exactly as held before 2026-10-09, edition 2 the rule 1 correction, and edition 3 for 2020 is the 2024 release's revision |
| `care_leaver_accommodation_editions_file_checks` | Append-only ledger of every file read |

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

### Housing Benefit accommodation-type editions (S8b)

`la_hb_accom_type_caseload_editions` is append-only (update, delete and truncate are blocked by trigger). Key `(lad24cd, month, accom_type, edition)`; 8,288 rows on 2026-10-08 (296 authorities x 4 accommodation types x 7 months, 202509 to 202603, all edition 1). `month` is `YYYYMM`; `accom_type` is `SA` (supported accommodation), `TA` (temporary accommodation), `OTHER` or `UNKNOWN`.

| Column | Description |
|---|---|
| `edition` | Integer, 1 = first loaded. |
| `supersedes` | The edition this one replaces; NULL for the first. |
| `claimants` | HB claimants, integer. NULL where the API returns no value or a merged area has a part with no value; a returned 0 is stored as 0 (`docs/RULES.md`). Never negative: checked by the verify gate, not by a constraint. |
| `release_label`, `published_date` | For the edition 1 rows recorded from what was held (`sync-new`), `as loaded; published_date is the load date`, and `published_date` is the date the live rows were loaded; for fetched editions, `stat-xplore fetch <date>` and the date of the fetch. Informational only; they do not decide which edition is latest. |
| `source_file`, `source_sha256`, `loaded_at` | Provenance. `source_file` names the Stat-Xplore table query; `source_sha256` is the checksum of the month's records, in two formats: edition 1 recorded by `sync-new` uses the editions core's row hash, fetched editions use the loader's content hash, so the two are not comparable. |

**Live-layer rule.** `la_hb_accom_type_caseload` holds, for each authority, month and type, the latest edition (the one no other edition supersedes). A new month's live rows are inserted by `load --commit` in the same transaction as its edition 1; a revised month reaches live through `python scripts/s8b_hb_editions.py refresh-latest --commit`, which changes only `claimants`; `loaded_at` is never touched. DWP revises this caseload in place with no revision note, so every `load` rechecks the latest six held months against the API; a month whose content differs is stored as the next edition, and a return to earlier content is also a new edition. On 2026-10-08 all seven months were rechecked and none had been revised. Procedure: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md).

---

### PIP claimants (S19)

Source: DWP Stat-Xplore, Personal Independence Payment cases with entitlement, by local authority and month. `la_pip_claimants` holds the latest edition of each month; `la_pip_claimants_editions` holds every release.

**Month key.** `month` is a sortable `YYYYMM` text key (`202607`), per `docs/RULES.md` rule 8. Until 2026-10-08 it held labels such as `Apr-26`, which sort by first letter. The one-off relabel mapped `Apr-26` to `202604` and `Jul-26` to `202607`; the rule is `Mon-yy` to `20yy` + month number. Anything that displays a month builds the label from the key. `v_la_pip_rates` now shows `yyyymm` months.

| Column | Type | Description |
|---|---|---|
| `pip_total_claimants` | integer | PIP cases with entitlement. NULL where DWP publishes `..` (disclosure control); never zero-filled. |
| `pip_enhanced_daily_living` | integer | Cases with the enhanced daily living component. Same NULL rule. |
| `pip_rate_per_1000` | numeric | In `v_la_pip_rates` only: total claimants per 1,000 of the latest mid-year population, with `population_reference_year`. |

`la_pip_claimants_editions` is append-only (update, delete and truncate are blocked by trigger). Key `(lad24cd, month, edition)`; `month` must be six digits. 1,184 rows on 2026-10-08 (296 authorities x 4 months, 202604 to 202607, all edition 1). Columns as for the other editions tables: `edition` (1 = first loaded), `supersedes`, the two measures, `release_label` and `published_date` (informational), and `source_file`, `source_sha256`, `loaded_at` (provenance).

**Live-layer rule.** The live table holds, for each authority and month, the latest edition. It is refreshed with `python scripts/s19_pip_editions.py refresh-latest --commit` and changes only the two measures; `loaded_at` is never touched. Every `load` rechecks the latest six held months; a differing month is stored as the next edition. Whether PIP is revised is not established: 202604 and 202607 were compared once, a week after their first load, and did not differ. Procedure: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md).

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

## House Prices (S15)

Source: HM Land Registry / ONS UK House Price Index, average prices by local authority and month (`period`, first day of the month, from 2022-01-01). `la_house_prices` holds the latest edition of each month; `la_house_prices_editions` holds every release of a month that differed from the one before.

| Column | Type | Description |
|---|---|---|
| `avg_price_all` | numeric(12,2) | Average price, all property types, GBP |
| `avg_price_all_sa` | numeric(12,2) | Seasonally adjusted all-property price. NULL throughout in the current file, as published |
| `annual_change_pct` | numeric(6,2) | Year-on-year change in the all-property price, per cent |
| `avg_price_detached`, `avg_price_semi`, `avg_price_terraced`, `avg_price_flat` | numeric(12,2) | Average price by property type, GBP. NULL where the Land Registry suppresses a low-volume price; never zero-filled |

`la_house_prices_editions` is append-only (update, delete and truncate are blocked by trigger). Key `(lad24cd, period, edition)`. Columns as for the other editions tables: `edition` (1 = first loaded), `supersedes`, the seven values above, `release_label` and `published_date` (informational), and `source_file`, `source_sha256`, `loaded_at` (provenance). 20,355 rows on 2026-10-09: edition 1 for 55 months (January 2022 to July 2026, 295 authorities, 16,225 rows) and edition 2 for the 14 months the July 2026 release revised (4,130 rows).

**Live-layer rule.** The live table holds, for each authority and month, the latest edition. A new month's live rows are inserted by `python scripts/s15_hpi_editions.py load --commit` in the same transaction as its edition 1; a revised month reaches live through `refresh-latest --commit`, which changes only the seven value columns; `loaded_at` is never touched. The source revises: the first comparison (2026-10-09) found 14 of 55 months revised, May 2025 to June 2026. Procedure: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md).

---

## Private Rents (S18)

Source: ONS Price Index of Private Rents (PIPR), by English local authority, month (`period`, first day of the month, from 2024-03-01) and breakdown. `la_private_rents` holds the latest edition of each cell; `la_private_rents_editions` holds every release of a month that differed from the one before.

| Column | Type | Description |
|---|---|---|
| `breakdown_type`, `category` | text | One of nine breakdown blocks (all properties, bedrooms, property type and so on) and the category within it |
| `mean_rent` | numeric | Average (mean) monthly rent, GBP. Never zero; blank cells are NULL |
| `rent_index` | numeric | Rent index (January 2023 = 100) |
| `annual_pct_change` | numeric | Year-on-year change in the rent, per cent |
| `provisional` | boolean | True for the latest month at publication. A change to final is a change and is stored as a new edition |

`la_private_rents_editions` is append-only (update, delete and truncate are blocked by trigger). Key `(lad24cd, period, breakdown_type, category, edition)`. Columns as for the other editions tables: `edition` (1 = first loaded), `supersedes`, the four values above, `release_label` and `published_date` (informational), and `source_file`, `source_sha256`, `loaded_at` (provenance). 79,380 rows on 2026-10-09: edition 1 for 30 months (March 2024 to August 2026), 294 areas, nine breakdown blocks, 2,646 rows a month. There is no edition 2 yet: the first comparison, against the 16 September 2026 workbook, found no change.

**Live-layer rule.** The live table holds, for each cell, the latest edition. A new month's live rows are inserted by `python scripts/s18_pipr_editions.py load --commit` in the same transaction as its edition 1; a revised month reaches live through `refresh-latest --commit`, which changes only the value columns. The live `source` column names the edition that last wrote the row: the first-inserting edition for rows inserted by the new loader (from 2026-10-09), but for the 76,734 legacy rows (2024-03 to 2026-07, first inserted by the 17 June 2026 backfill and overwritten in place by the old loader) the 19 August 2026 edition. It is not changed by `refresh-latest`; the edition that supplied the current values is in the editions table. `la_private_rents` is not read by Workflow 1 or the map export. Procedure: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md).

---

## Empty Homes and Vacant Dwellings (S22)

Source: MHCLG Council Taxbase (CTB) local authority level workbook and Live Table 615. Source note: `docs/s22_source_structure.md`. Each live table is the latest-edition layer of an append-only `*_editions` table, loaded by `scripts/s22_ctb_editions.py`.

| Table | What it holds |
|---|---|
| `la_council_taxbase_empties` | One row per billing authority and taxbase year (`lad24cd`, `taxbase_year`); 296 rows for 2025. W1 reads the latest year; `v_la_empty_homes_rates` derives the rates. `loaded_at` is the edition's, so a revision is visible to `refresh_map.py` |
| `la_ctb_exemption_classes` | The 11 unoccupied exemption classes (B, D to L, Q) per authority and taxbase year (`lad24cd`, `taxbase_year`, `exemption_class`); 3,256 rows |
| `la_vacant_dwellings_615` | Live Table 615, one row per published code and year where either sheet has a number (`published_la_code`, `year`); 7,170 rows for 2004 to 2025. `lad24cd` is NULL and `mapping_status` is `unmapped` for abolished districts (891 rows, 80 codes) |
| `la_council_taxbase_empties_editions`, `la_ctb_exemption_classes_editions`, `la_vacant_dwellings_615_editions` | Append-only: what one file says about one period, keyed by the live key plus `edition`. The CTB main and class editions of a year always carry the same edition number. Edition 1 is the table exactly as held before 2026-10-09 |
| `la_council_taxbase_empties_editions_file_checks`, `la_vacant_dwellings_615_editions_file_checks` | Append-only ledgers of every file read: final URL, sha256, outcome (a file whose pair is held for every period it covers is not parsed again) |

| Column | Definition |
|---|---|
| `total_dwellings` | CTB table 1.01 (Line 1) |
| `empty_total` | Table 1.18 (Line 15): dwellings classed as empty for council tax |
| `empty_6_months_plus` | Table 1.19 (Line 16): dwellings classed as empty for more than six months. **Not** MHCLG's long-term vacant figure, which is Line 18 (table 1.22) and excludes empties on discount class D and flood empties (England 309,889 here against 303,185; 179 of 296 authorities differ). The measure is unchanged |
| `empty_under_6_months` | Derived: `empty_total - empty_6_months_plus`; NULL unless both are published |
| `empty_homes_premium_count` | Table 1.17 (Line 14); not comparable across 1 April 2024 |
| `second_homes` | Table 1.11 (Line 11); affected by reclassification from 1 April 2025 |
| `unoccupied_exemptions_total` | Derived: the sum of the 11 classes; NULL unless all 11 are published |
| `vacant_dwellings` (615) | All vacants; for 2025 equals `empty_total + unoccupied_exemptions_total` |
| `long_term_vacant_dwellings` (615) | MHCLG's long-term vacants (Line 18 / table 1.22 for 2025) |
| `null_reasons` | `column=reason` pairs sorted and joined with `;`: `suppressed_or_not_available` (`[x]`), `not_applicable` (`[z]`) or `built_from_null` (a derived column with a NULL part). NULL when the row has no NULL value; NULL in every held row |

Suppressed and not-applicable cells are NULL, never 0 (rule 1). Dacorum 2012 in Table 615 is published as a non-integer and held rounded (1416 and 495). Rates (`lte_rate_pct`, `premium_coverage_pct`) are derived in `v_la_empty_homes_rates` and never stored.

## Care Providers (Supply Side)

Source: CQC Care directory with filters (monthly). Only supply-side column in the pipeline — every other signal measures demand.

Since 2026-10-09 each monthly file is kept as a dated snapshot of the CQC register (Adult social care locations), loaded by `scripts/s11_cqc_editions.py`:

| Table | What it holds |
|---|---|
| `cqc_location_snapshots` | Live layer: one row per location per snapshot, keyed (`snapshot_date`, `location_id`). `snapshot_date` is the file's own "as at" date from its README sheet. |
| `cqc_location_snapshots_editions` | Append-only editions of each snapshot (edition 1 is the file as first loaded; a later edition only if a file for the same date was reissued and its location set is unchanged; a recheck of the July, August and September 2026 snapshots is refused because location `1-28257167158` is kept unresolved in them as loaded but now resolves through postcodes.io, so the held editions stay as loaded). |
| Text rule | Repeated spaces in text cells (second and later spaces of a run, stored in the ODS as `<text:s/>`) collapse to one space, so a location or provider name with a double space is stored with one. This is a documented transformation, kept from the retired loader so held rows and joins stay stable; 41 July and 43 October cells differ from the file's raw text only by this. |
| `cqc_location_snapshots_editions_file_checks` | Append-only ledger of every file read. |
| `cqc_location_snapshots_unresolved` | Append-only: the locations of each snapshot that could not be given a `lad24cd`. |
| `cqc_locations` | A **view** over the snapshots with the same columns as the old table: one row per location ever seen, with its values from the latest snapshot containing it. W1 and the map export read this view unchanged. The old table is kept as `cqc_locations_legacy` (dropping it is a later, separate decision). `cqc_unresolved_locations` is the old unresolved table, no longer written. |

In the view, `is_active` means the location is in the **latest snapshot**; it does not mean "deregistered". A location absent from the latest file may be deregistered, may have left the Adult social care scope, or may be a late deregistration (CQC's own README says deregistrations can appear late). `deregistered_seen_date` is the date of the first later snapshot that lacks it.

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

Tables: `nhs_drd_discharge_delays` holds the latest edition of each month and UTLA (key `reporting_period, utla_code`; 4,437 rows, 29 months to August 2026). `nhs_drd_discharge_delays_editions` is append-only (update, delete and truncate blocked by trigger) and holds every edition: key `(utla_code, reporting_period, edition)`, the 17 value columns of the live table, `utla_name`, `release_label` and `published_date` (informational), `source_file`, `source_sha256`, `supersedes` and `loaded_at` (provenance). 4,590 rows on 2026-10-09 (edition 1 for 29 months; June 2026 edition 2 is a precision-only file-form correction). `nhs_drd_discharge_delays_editions_file_checks` is the ledger of every file read, also append-only: `reporting_period`, `source_file`, `file_sha256`, `outcome` (new, unchanged, revised or live-missing), `edition`, `checked_at`. `total_discharges` is NULL throughout, as published. The live `source` column holds the URL of the file that supplied the row, and equals the latest edition's `source_file`. Procedure: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md).

### S9b — MHSDS CRFD (MHS26)

Source: NHS Digital Mental Health Services Monthly Statistics. MHS26 — Clinically Ready for Discharge delayed bed days (combined MH + LD/autism). Published at LAD level directly; no apportionment required.

| Column | Type | Range | Description |
|---|---|---|---|
| `crfd_days` | integer | 0 – 2,500+ | MHS26 CRFD delayed discharge days in the latest reporting month. NULL where suppressed at source (28–46% of LAs per month). |

Tables: `nhs_mh_crfd` holds the latest edition of each month and area (key `reporting_period, lad24cd, measure_id`; 12,136 rows, 41 months to August 2026, 296 areas). `nhs_mh_crfd_editions` is append-only and holds every edition: key `(lad24cd, measure_id, reporting_period, edition)`, `measure_value`, `la_name`, `measure_name`, `release_label` and `published_date` (informational), `source_file`, `source_sha256`, `supersedes` and `loaded_at`. 22,792 rows on 2026-10-09: edition 1 for 41 months, and edition 2 (the year-end Final file) for the 36 months April 2023 to March 2026. `nhs_mh_crfd_editions_file_checks` is the append-only ledger of every file read (same columns as the S9a ledger). Edition 1 of a month is normally the Performance figure; Final figures are large revisions by nature (late submissions). The live `source` column holds the URL of the file that supplied the row, and equals the latest edition's `source_file`. Procedure: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md).

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
