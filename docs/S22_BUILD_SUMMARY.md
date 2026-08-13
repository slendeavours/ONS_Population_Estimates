# S22 Build Summary — MHCLG Council Taxbase Empty Homes

## Sources

### Source A — Local authority Council Taxbase in England

- **Publisher:** MHCLG
- **Series:** Council Taxbase in England (CTB and CTB Supplementary forms)
- **Collection page:** `https://www.gov.uk/government/collections/council-taxbase-statistics`
- **Release page:** `https://www.gov.uk/government/statistics/council-taxbase-2025-in-england`
- **Release used:** Council Taxbase 2025 in England
- **First published:** 6 November 2025
- **Revised:** 21 January 2026 — Tables 1, 2, 3a, 3b and 4 revised following corrections from 22 authorities
- **File:** `2025_Local_Authority_Drop_Down.xlsx`, 1,800,242 bytes, resolved at run time from the release page
- **Technical notes:** `https://www.gov.uk/government/statistics/council-taxbase-2025-in-england/local-authority-council-taxbase-in-england-2025-technical-notes`
- **Status:** Accredited Official Statistics
- **Snapshot:** dwelling counts as at 10 September 2025 from the VOA council tax list; discounts, exemptions and premiums derived at 6 October 2025
- **Native geography:** 296 English billing authorities, published on ONS codes
- **Refresh cadence:** annual, published November, revised the following January
- **Tables used:** 1.01, 1.11, 1.17, 1.18, 1.19 on the `Council Taxbase Data` sheet; 2.01 on the `Supplementary Data` sheet

### Source B — Live Table 615

- **Publisher:** MHCLG
- **Series:** Live tables on dwelling stock (including vacants)
- **Landing page:** `https://www.gov.uk/government/statistical-data-sets/live-tables-on-dwelling-stock-including-vacants`
- **File:** `Live_Table_615.ods`, 311,603 bytes, resolved at run time from the landing page
- **Landing page last updated:** 25 June 2026
- **Native geography:** local authority districts as they existed in each year
- **Date range loaded:** 2004 to 2025
- **Sheets used:** `All_vacants`, `All_long_term_vacants`

Neither file URL is stored in the build. Both are discovered from the publisher landing page on every run through the GOV.UK content API.

## Tables Built

| Table | Rows | Natural key | Contents |
|---|---|---|---|
| `la_council_taxbase_empties` | 296 | `(lad24cd, taxbase_year)` | Total dwellings, empties under and over six months, empty total, empty homes premium count, second homes, unoccupied exemptions total |
| `la_ctb_exemption_classes` | 3,256 | `(lad24cd, taxbase_year, exemption_class)` | The eleven unoccupied exemption classes (B, D, E, F, G, H, I, J, K, L, Q) per authority |
| `la_vacant_dwellings_615` | 7,170 | `(published_la_code, year)` | Vacant and long-term vacant dwellings by district, 2004 to 2025 |
| `ctb_series_breaks` | 2 | `break_id` | Documented structural breaks, machine-readable |

### View

`v_la_empty_homes_rates` over the latest `taxbase_year`:

| Column | Definition |
|---|---|
| `lte_rate_pct` | `empty_6_months_plus / total_dwellings × 100` |
| `lte_share_of_empties_pct` | `empty_6_months_plus / empty_total × 100` |
| `premium_coverage_pct` | `empty_homes_premium_count / empty_6_months_plus × 100` |
| `second_homes_rate_pct` | `second_homes / total_dwellings × 100` |

Every denominator is guarded with `NULLIF`. Rounded to two decimal places. Rates are derived here and are never stored in a table.

## Column Derivation

| Column | Source |
|---|---|
| `total_dwellings` | Table 1.01, Total |
| `empty_total` | Table 1.18, Total |
| `empty_6_months_plus` | Table 1.19, Total |
| `empty_under_6_months` | Table 1.18 minus Table 1.19 |
| `empty_homes_premium_count` | Table 1.17, Total |
| `second_homes` | Table 1.11, Total |
| `unoccupied_exemptions_total` | Table 2.01, sum of classes B, D, E, F, G, H, I, J, K, L, Q |

The unoccupied class set was verified, not assumed. Summed across England those eleven classes give 212,004, reproducing the release page's "212,000 dwellings that were receiving an exemption that were unoccupied".

## Coverage

**296 of 296 authorities for taxbase year 2025. Complete.**

Only the current taxbase year is published in the release workbook — there are no prior-year columns in the same file — so a single year is loaded. The series builds up one release at a time each November.

**Table 615 coverage carries a caveat that travels with the figure.** 7,170 district-year rows spanning 2004 to 2025, but the series is not complete for any single geography across the full period:

| `mapping_status` | Rows | Distinct published codes | Years |
|---|---|---|---|
| `direct` | 6,277 | 296 | 2004–2025 |
| `unmapped` | 891 | 80 | 2004–2022 |
| `resolved_via_lookup` | 2 | 2 | 2025 |

`unmapped` rows are districts abolished under local government reorganisation — Bournemouth/Christchurch/Poole and Suffolk 2019, Buckinghamshire 2020, Northamptonshire 2021, Somerset, North Yorkshire and Cumbria 2023, and others. They keep a null `lad24cd` by design and are **not** aggregated into successor unitaries: mapping six Somerset districts onto E06000066 would make any downstream sum count Somerset six times over. `la_code_lookup` was read, never written.

## Geography

Resolution runs through `la_code_lookup`. A pure recode resolves; an abolition into a successor unitary does not.

MHCLG publishes Barnsley and Sheffield under the codes recoded on 1 April 2025 (SI 1328/2024) — E08000038 and E08000039 — while `la_boundaries` is LAD December 2024 and carries E08000016 and E08000019. Both resolve as `change_type = 'recode'`: same area, new number. The same two codes appear in Table 615 and are resolved the same way there.

## W1 Integration

### New `staging_la_signals` columns

| Column | Type | Source |
|---|---|---|
| `ctb_total_dwellings` | INTEGER | `la_council_taxbase_empties.total_dwellings` |
| `ctb_empty_6m_plus` | INTEGER | `la_council_taxbase_empties.empty_6_months_plus` |
| `ctb_empty_homes_premium` | INTEGER | `la_council_taxbase_empties.empty_homes_premium_count` |
| `ctb_second_homes` | INTEGER | `la_council_taxbase_empties.second_homes` |
| `ctb_lte_rate_pct` | NUMERIC(6,2) | `v_la_empty_homes_rates.lte_rate_pct` |

Added by an additive `DO $$ ... IF NOT EXISTS` migration. The table was not dropped or recreated.

The four counts come from the table and the rate comes from the view, so `lte_rate_pct` has exactly one definition and is not recomputed anywhere else.

### New tenant types

None. Empty homes is a supply-side indicator, not a cohort, so node 6 is unchanged.

### W1 run

Run 12, 2026-08-13. 296 rows. All five new columns populated 296/296. Every pre-existing column reproduces run 11's coverage exactly.

Node 5 was revised in the stored n8n workflow and the full revised SQL is at `docs/s22_w1_node5_revised.md`. That revision also folded in the S9 and S19 columns, which were in the database but not in the stored node because runs 10 and 11 had been applied by direct SQL.

## Map Layer

One layer: **Long-Term Empty Rate**, driven by `ctb_lte_rate_pct`, LAD-level choropleth across all 296 authorities.

No layer for total empties — it bundles second homes and short-term turnover, so a choropleth of it would misrepresent the picture. No layer for premium application. Both stay in the database and are readable in the map's detail panel.

The "Gov Sources" badge is unchanged. It is publisher-count framing and MHCLG is already counted.

## Structural Breaks

Recorded in `ctb_series_breaks`, machine-readable, so consumers of the data see them without reading documentation. Both are cited to the MHCLG technical notes.

| Date | Affected column | What changed |
|---|---|---|
| 1 April 2024 | `empty_homes_premium_count` | The Empty Homes Premium threshold moved from 2 years to 1 year. Counts are not comparable across this date. England rose 27.9% between the 2024 and 2025 taxbase years — a widened eligible population, not more empty homes. |
| 1 April 2025 | `second_homes` | The Second Homes Premium was introduced, applied by 211 of 296 authorities. Authorities reported reviewing empty properties and second homes ahead of it, which moves dwellings between the two categories independently of anything changing on the ground. |

## Known Caveats

- **`premium_coverage_pct` is directional only and can never reach 100.** Long-term empty starts at six months, the premium starts at twelve, so the numerator is drawn from a strictly narrower population than the denominator. It is not a compliance rate. The caveat is carried as a column comment on the view.
- **Long-term empty is not the same as vacant.** Source A counts dwellings a billing authority classes as empty for council tax; Source B counts vacant dwellings on a different definition and a different snapshot date. The two are held in separate tables and are not reconciled to each other.
- **The 2025 release is a revision.** MHCLG corrected data from 22 authorities on 21 January 2026, affecting the taxbase, second homes, empty homes, discounts and premiums. Any figure quoted from the November 2025 original will differ.
- **Five authorities charge no empty homes premium** — Amber Valley, Bolsover, Castle Point, Gravesham, Ribble Valley — and report zero, matching the release statement that 291 of 296 applied a premium.
- **No national figure is published for six-month-plus empties.** The release page prints headline figures for total dwellings, all empties, premium counts, second homes and unoccupied exemptions, but none for dwellings empty more than six months. That is NOT FOUND on the release page, not unchecked; those two measures reconcile against the publisher's own England total row in the same workbook, and the substitution is stated wherever the figure appears.
- **Empty homes is a supply-side indicator.** Like S11, it records stock, not need. The pipeline stores it and does not score, rank or weight it.

## Refresh Procedure

The next release is due **November 2026** (Council Taxbase 2026), with a revision expected January 2027.

1. Run `python scripts/s22_ctb_empties_build.py` via `scripts/s22_run.py`. Discovery is automatic — it reads the collection page, takes the most recent `Council Taxbase <year> in England` release, and finds the local-authority-level workbook by title. No URL needs editing.
2. The run halts if the release structure has changed: a missing table number, a block without a `Total` column, or a missing exemption class each stop the build with the reason. Confirm the table numbers against the new workbook's `Contents` sheet before assuming a code change is needed.
3. The load upserts on `(lad24cd, taxbase_year)`, so a new year adds 296 rows rather than replacing the existing ones. Re-running the same year is a no-op.
4. Update `RELEASE_PAGE_TARGETS` in `scripts/s22_run.py` with the new release's national headline figures. They are the reconciliation targets and must come from the release page itself.
5. Re-run `scripts/s22_w1_wire.py` to produce a new W1 run, then `scripts/export_map_data.py`, then publish.
6. Check whether MHCLG has added a further structural break. Any new premium threshold or category change goes into `ctb_series_breaks` in the same run that loads the affected year.

Table 615 refreshes on its own cadence with the dwelling stock live tables. Re-running the build picks up whatever is current; the upsert is on `(published_la_code, year)`, so revised historic years are corrected in place.

## Verification

Six hard gates, all pass. Four soft checks, all reported. Full results in `docs/s22_verification.md`.

The Empty Homes Network's November 2025 report was used as an independent spot-check for five authorities. All five agree with MHCLG exactly on all three measures. No loaded value was adjusted to match a secondary source.
