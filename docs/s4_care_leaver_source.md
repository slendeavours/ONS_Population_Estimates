# Source 4 — DfE Care Leaver Accommodation (SSDA903)

| Field | Value |
|---|---|
| Publisher | Department for Education (DfE) |
| Dataset | Children looked after in England including adoptions — care leaver activity and accommodation (SSDA903 return) |
| Cadence | Annual (reporting year ends 31 March; published the following November) |
| Landing page | `https://explore-education-statistics.service.gov.uk/find-statistics/children-looked-after-in-england-including-adoptions` |
| Geography | Upper-tier local authorities: 153 in 2025, of which 21 are county councils (24 in 2019) |
| Join key | `lad24cd`; county councils that are still counties are carried on their own `E10` code |
| Target table | `care_leaver_accommodation` (grain: lad24cd × reporting_year × age_group), the latest-edition layer of `care_leaver_accommodation_editions` |
| Years held | 17-21 accommodation: 2019 to 2025. 22-25 suitability: 2023 to 2025 |
| Loader | `scripts/s4_care_leaver_editions.py`; gates `scripts/s4_care_leaver_editions_verify.py` (23). Steps: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md), "S4 care leaver accommodation" |
| Record | [decisions/2026-10-09-s4-editions-first-load.md](decisions/2026-10-09-s4-editions-first-load.md) |

> Since 2026-10-09 this source is loaded by `scripts/s4_care_leaver_editions.py`, not by the n8n workflow described in [nodes/](nodes/) (each node note carries a superseded banner) or by `scripts/verify/rebuild_care_leavers.py` (now `scripts/historical/s4_rebuild_care_leavers.py`, which stops with a RETIRED message).

## What it provides

Counts of care leavers by accommodation type at local authority level, for the 17-21 cohort, plus accommodation suitability for the 22-25 cohort. The 17-21 accommodation breakdown is the cohort most directly exposed to youth homelessness. Each release restates earlier years, so every release's statement about a year is stored as its own edition (an edition is one release's figures for one reporting year, both cohorts of that release); the live table holds the latest edition of each year.

## Which measure to quote

| Column | Definition | Use |
|---|---|---|
| `semi_independent_published` | DfE's published category **Semi-independent, transitional accommodation**, and nothing else | **The default, for external documents and for the map.** Reproducible directly from DfE |
| `foyers` | DfE **Foyers** category, held separately | Component of the aggregate |
| `supported_lodgings` | DfE **Supported lodgings** category, held separately | Component of the aggregate |
| `semi_independent` | Semi-independent transitional **plus `foyers` plus `supported_lodgings`**, NULL unless every one of those parts is published in both age bands | A pipeline aggregate. Not used by W1 or the map since 2026-10-09 |

W1 (`sql/w1/05_la_signals.sql`) and the map read `semi_independent_published`, as decided by Scott on 2026-10-09 (option (a) in the decision note). Until W1 run 25 they read the aggregate. The aggregate is NULL whenever any of its six DfE parts (three categories, two age bands) is suppressed, so in 2025 it has a value for only 17 of the 296 mapped authorities, while the published category has one for 121. Of the 132 values run 25 mapped, 49 are the same under the published category and 83 differ (a lower number, or NULL).

Liverpool 2025: 188 on the published category. Edition 1 (the table as held before the correction) held 208 as the aggregate, which was 188 plus 10 plus 10: the foyers and supported lodgings cells are suppressed for one age band, so the 10s were the other band only, with the suppressed band added as zero. Under rule 1 the aggregate is NULL, so the difference between 188 and 208 is the difference between the published category and a partial sum, not a real second measure. The next W1 run shows 188 on the map for Liverpool where run 25 showed 208.

**Decision (2026-08-20): external documents quote `semi_independent_published`.** The aggregate may be cited alongside only where it is published for every part and is labelled as a combined measure with its three components named; in practice it is now rarely available.

## Coverage — county councils

Care leaver duties sit with upper-tier authorities, so DfE publishes county councils on `E10` codes alongside the unitary and metropolitan authorities. Counties are 24 of 152 (2019), 23 (2020), 23 (2021), 22 (2022), 22 (2023), 21 of 153 (2024) and 21 (2025) in the 17-21 cohort; the 22-25 cohort has 22 (2023), 21 (2024) and 21 (2025). The 2024 release restated North Yorkshire and Somerset on their unitary codes back to 2020 (the 2023 release used E10000023 and E10000027).

Until 2026-08-20 the old builds joined `la_code_lookup` on an inner join, which holds no `E10` entries, so every county was dropped and the table held 132 authorities; England totals and national ranks were computed over 132. Counties are now carried on their own `E10` code. They do not join `la_boundaries`, a LAD24 boundary set, so a query that inner-joins boundaries still excludes them (the 21 current counties are accepted through `utla_lad_mapping` by gate 14 of `verify_source_registry.py`, which also declares the predecessors Bournemouth and Poole, E06000028/29, and E10000006, E10000009 and E10000021). Ranking and mapping that must include counties needs an upper-tier geography, which does not yet exist in this pipeline.

The 22-25 cohort sums the four single ages 22, 23, 24 and 25; before 2026-10-09 the table held the age-25 row only (Liverpool 2025 held 117, the whole cohort is 634).

## Suppression

DfE suppresses small counts with `c`, marks a figure that cannot exist (an authority in a year it did not exist) with `z`, and a figure not available with `x`; the loader stores these as NULL with the reason `suppressed`, `not_applicable` or `not_available`. A `k`, a blank or any other marker in a count halts the load. Under rules 1.1 to 1.7 and 1.10 (`docs/RULES.md`):

- A suppressed cell is **NULL**, never 0. A published 0 stays 0.
- A column built from several DfE categories (a bucket, `semi_independent`, `unsuitable`, `total_care_leavers`) is NULL unless every part is published. `total_care_leavers` is NULL unless every bucket is published; `total_published` is DfE's own Total row.
- The reason is stored in `null_reasons` (for example `foyers=suppressed;semi_independent=suppressed`). `suppressed_flag` is true where a cell contributing to the aggregate was suppressed. `unsuitable_pct` is no longer written.
- A row that is all `z` (an authority that did not exist in that year) is not stored. A row whose every stored column is NULL is stored if DfE publishes it: nine live 22-25 rows are like that (City of London 2023-25, Rutland 2024-25, North and West Northamptonshire 2023-24). They carry published numbers, but each stored column sums four ages and at least one age's cell is suppressed or not available, so every sum is NULL (rule 1.7).
- A 0 changing to NULL, or the reverse, between releases (about 100 cells a release, normal publisher behaviour) is listed in the `load` preview and needs `--acknowledge YEAR`.

The old builds added suppressed cells as 0, so about 7,700 cells held a number where the rule gives NULL; edition 2 corrects that and edition 1 keeps the table as it was held. See the decision note.

## Acquisition

`load` needs no hand-copied ids.

- The content API publication endpoint (`https://content.explore-education-statistics.service.gov.uk/api/publications/children-looked-after-in-england-including-adoptions`) works and gives `latestRelease` and `nextReleaseDate` (`check_sources.py` uses it). The release-level content endpoints return 404.
- The release's data guidance page (`/find-statistics/<publication>/<release>/data-guidance`) is server-rendered with a `__NEXT_DATA__` JSON block holding the release version and the two datasets (`dataSetFileId`, title, years, row count). The loader reads both dataset ids from it and halts if the block is missing, unparseable, or lists other than one candidate per cohort. `__NEXT_DATA__` is not a published contract, so `--release YYYY --dataset-17-21 ID --dataset-22-25 ID` is kept as a manual fallback, with every file check still applied.
- The CSV is `https://explore-education-statistics.service.gov.uk/data-catalogue/data-set/<id>/csv` (no auth). The file name is the same every release, so downloads are saved in `data/raw/s4_cla/` with the release year, cohort and dataset id in the name, and a same-named file with different content is kept beside it, never overwritten.

**Both dataset ids change every release**, the 22-25 one too (a96ea38e in 2024, bd5240e0 in 2025), and the dataset titles change ("17-21 year old care leavers accommodation - LA" in 2023 and 2024, "Care leavers (now 17-21 years) - accommodation - by local authority" in 2025).

**Three header schemas.** The 2023 and 2024 17-21 files use `age`, `accommodation_type` and `number`; the 2024 22-25 file uses its own wording (`Aged 22`, `not suitable`, `Suitability total`); the 2025 files use `care_leaver_age`, `breakdown` and `care_leaver_count`. The loader reads the file's header and halts, listing the columns, on any other. Code that read the old names against a new file used to route every row to `other` and return zero for every bucket without an error; that cannot happen here.

**Overlapping years between releases.** Each release republishes several prior years with revisions. Release rank is the file's own maximum `time_period`, never its name or link, and an older release never replaces a newer tip for a year (page, `--release` and `--file` paths alike; `--allow-older-file` overrides and is logged). The pre-2026-08-20 build deduplicated first-occurrence-wins with the older file first, so it retained superseded figures for 7 rows across 2020-2023.

## Caveats

1. **Measurement date.** Figures are a point-in-time count, not a count of young people passing through a setting over the year; annual need is higher. For the 17-21 cohort DfE's 2024 and 2025 data guidance describes the count as measured on or around the care leaver's birthday, not on 31 March (the loader does not check it). The 22-25 suitability measure is taken at latest contact during the year.
2. **Upper-tier only.** District councils have no care leaver figure and are absent, not zero.
3. **Suppression gives NULL.** See above. A NULL is an unpublished figure.
4. **No definition-change statement.** DfE's care leaver notes for the 2024 and 2025 releases (checked 2026-10-09) carry no statement that the accommodation categories changed definition. The Ofsted registration wording on those pages belongs to the looked-after children placement data, a different collection and category. Say nothing about a break in this series unless DfE publishes one.
5. **Hampshire 2024.** DfE flagged Hampshire for data quality problems in 2024 following a records system change. Retained without adjustment.
6. **22-25 cohort is partial.** Data covers only young people who contacted the authority and requested support. DfE notes 2023 may undercount 24-year-olds by around 3% and 25-year-olds by around 10%.

## Verification

`python scripts/s4_care_leaver_editions_verify.py` (read-only, 23 gates, exit 0 when all pass): tables, triggers and the ledger; edition 1 of every year and the latest edition equal to live; every code in `la_boundaries`, `utla_lad_mapping` or a declared predecessor; NULL against 0 and built columns; the 22-25 sums; file identity; the older-release rule; the stop conditions; one transaction per year; revision and `refresh-latest` (key changes only when named); the before-state hash recorded in the decision note; the correction proof; the W1 reading; rerun and stranded-year repair; `restore-edition`; no network and no secrets.

The migration of 2026-10-09 was proved before it was written (`migrate-legacy`): the 5,289 non-NULL corrected 17-21 cells equal the held cells with 0 differences, the 396 held 22-25 rows each equalled the file's age-25 row, and every key dropped or added was accounted for. Details and the numbers per year are in the decision note.

The 2026-08-20 verification of the old build (808 of 815 rows reproduced under the documented bucketing rule, which added suppressed cells as zero; `semi_independent_published` reconciled to DfE at 155 of 155 authorities in 2025) described a table that has since been corrected and is superseded. 2025 now has 153 authorities in each cohort, not 155.

## Refresh

Each November: `python scripts/s4_care_leaver_editions.py load` (preview; nothing is written to the database), read it, `load --commit`, then `refresh-latest` (preview) and `refresh-latest --commit --accept-key-changes YEAR` for each year whose authorities change. Then `w1_run.py` and `refresh_map.py` only if the latest year changed. Full steps and the stop conditions: [QUARTERLY_REFRESH.md](QUARTERLY_REFRESH.md). To go back for a year: `restore-edition YEAR N`, then refresh.

`check_sources.py` (`find_s4`) reports the latest release from the content API. It does not read the data guidance page; the loader does that, and halts if the page has changed shape.

## Dual-model note

- **HSS lens (primary)**: care leavers in supported accommodation are the clearest published proxy for youth supported-housing demand at local authority level.
- **UCWS lens (context)**: cohort size indicates the scale of local commissioning activity, not the commercial characteristics of any scheme.
