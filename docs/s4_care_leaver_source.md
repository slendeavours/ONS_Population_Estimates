# Source 4 — DfE Care Leaver Accommodation (SSDA903)

| Field | Value |
|---|---|
| Publisher | Department for Education (DfE) |
| Dataset | Children looked after in England including adoptions — care leaver activity and accommodation (SSDA903 return) |
| Cadence | Annual (reporting year ends 31 March; published the following November) |
| Landing page | `https://explore-education-statistics.service.gov.uk/find-statistics/children-looked-after-in-england-including-adoptions` |
| Geography | Upper-tier local authorities: 155 in the 2025 release, including 24 county councils |
| Join key | `lad24cd` for unitary and metropolitan authorities; county councils are carried on their own `E10` code |
| Target table | `care_leaver_accommodation` (grain: lad24cd × reporting_year × age_group) |
| Years held | 17-21 accommodation: 2019 to 2025. 22-25 suitability: 2023 to 2025 |
| Refresh | n8n S4 workflow, 7 nodes — see [docs/nodes/](nodes/) |

## What it provides

Counts of care leavers by accommodation type at local authority level, for the 17-21 cohort, plus accommodation suitability for the 22-25 cohort. The 17-21 accommodation breakdown is the cohort most directly exposed to youth homelessness, and is the figure used in external reporting.

## Two measures are stored, and they are not interchangeable

This is the single most important thing to know about this table.

| Column | Definition | Use |
|---|---|---|
| `semi_independent_published` | DfE's published category **Semi-independent, transitional accommodation**, and nothing else | **Default for anything external.** Reproducible directly from DfE |
| `semi_independent` | Semi-independent transitional **plus `foyers` plus `supported_lodgings`** | The data layer and the demand map. The wider supported-accommodation population |
| `foyers` | DfE **Foyers** category, held separately | Component of `semi_independent`; lets the aggregate be broken down |
| `supported_lodgings` | DfE **Supported lodgings** category, held separately | Component of `semi_independent`; lets the aggregate be broken down |

By identity, `semi_independent = semi_independent_published + foyers + supported_lodgings`. For
Liverpool in the year to March 2025 that is 188 + 10 + 10 = 208. Holding the two components
means any output can show the split rather than leaving the 20-person gap between the two
measures unexplained.

**Where each measure is used.** External documents (the LandAid paper, its summary and deck)
quote `semi_independent_published` and state the breakdown in words. The demand map layer shows
`semi_independent`, the wider aggregate, which is the pipeline's data-layer measure. The two
therefore differ by design, and each product says which it uses.

The wider aggregate is a deliberate pipeline definition, not a DfE one. It is a reasonable measure of supported accommodation for care leavers, but a reader who checks DfE will not find it.

**Decision (2026-08-20): external documents quote `semi_independent_published`, and may cite the wider aggregate alongside provided it is labelled as a combined measure with its three components named.** The difference is material: Liverpool 2025 is 188 on the published definition and 208 on the aggregate, ranking 11th and 20th of 155 respectively.

## Coverage — county councils

Care leaver duties sit with upper-tier authorities, so DfE publishes 155 local authorities including 24 county councils on `E10` codes. County councils have no LAD24 district code.

Until 2026-08-20 the upsert joined `la_code_lookup` on an inner join, which holds no `E10` entries, so all 24 counties were silently dropped and the table held 132 authorities. England totals and every national rank were computed over 132 of 155. Counties are large: four of the six highest authorities in 2025 are counties.

Counties are now carried on their own `E10` code in `lad24cd`. They will not join to `la_boundaries`, which is a LAD24 boundary set, so any query that inner-joins boundaries still excludes them. Ranking and mapping that must include counties needs an upper-tier geography, which does not yet exist in this pipeline.

## Suppression

DfE suppresses small counts with `c`, and uses `k`, `z` and `x` for other unavailable values.

The 17-21 bucketing adds suppressed cells as zero rather than propagating null, because a bucket is a sum of several categories and one suppressed component should not void the whole bucket. The consequence is that bucket counts are **minima**, not exact counts. `suppressed_flag` is true wherever any cell contributing to `semi_independent` was suppressed.

`total_care_leavers` is the sum of the buckets and therefore inherits the same understatement. `total_published` is read from DfE's own Total row and is the correct figure to quote. For Liverpool 2024 these are 759 and 776 respectively.

## Definitional break at 2024

From reporting year 2024, **Semi-independent, transitional accommodation** means Ofsted-registered supported accommodation only. Before 2024 the label also included unregistered provision.

Counts either side of that boundary are not comparable and no trend statement should cross it.

## Acquisition gotchas

**A new dataset UUID per release.** EES assigns a fresh UUID to the 17-21 accommodation dataset each year; the UUID does not update in place. The 22-25 suitability dataset is the exception and is persistent.

**Column names changed in the 2025 release.** Editions to 2024 use `age`, `accommodation_type` and `number`. The 2025 release renamed these to `care_leaver_age`, `breakdown` and `care_leaver_count`, harmonising with the 22-25 file. Code that reads the old names against the new file will match no category, route every row to `other`, and return zero for every bucket **without raising an error**. Any change of edition must be accompanied by a column-name check that fails loudly.

**Overlapping years between editions.** Each release republishes several prior years with revisions applied. Where editions overlap, load the newer one. The pre-2026-08-20 build deduplicated on first-occurrence-wins with the older file first, so it retained superseded figures for 7 rows across 2020-2023.

**No usable content API.** EES does not expose a working content API path for this publication. Dataset CSVs are fetched from `https://explore-education-statistics.service.gov.uk/data-catalogue/data-set/{uuid}/csv`. The data catalogue front end is a JavaScript application and cannot be scraped for UUIDs; take them from the release's data guidance page.

## Caveats

1. **Point-in-time count.** Figures are a snapshot at 31 March, not a count of young people passing through a setting over the year. Annual need is higher.
2. **Upper-tier only.** District councils have no care leaver figure and are absent, not zero.
3. **Suppression understates.** See above. Bucket counts are minima.
4. **2024 registration change.** See above. Do not trend across it.
5. **Hampshire 2024.** DfE flagged Hampshire for data quality problems in 2024 following a records system change. Retained without adjustment.
6. **22-25 cohort is partial.** Data covers only young people who contacted the authority and requested support. DfE notes 2023 may undercount 24-year-olds by around 3% and 25-year-olds by around 10%.

## Verification, 2026-08-20

The build was replicated independently against the source CSVs. 808 of 815 rows reproduced exactly under the documented bucketing rule; the 7 differences were all overlapping years where the older edition had been retained. Liverpool 2024 reproduced exactly at 259 aggregate and 759 total.

After the rebuild, `semi_independent_published` reconciles to DfE at **155 of 155 authorities for 2025 with zero mismatches**.

## Dual-model note

- **HSS lens (primary)**: care leavers in supported accommodation are the clearest published proxy for youth supported-housing demand at local authority level.
- **UCWS lens (context)**: cohort size indicates the scale of local commissioning activity, not the commercial characteristics of any scheme.
