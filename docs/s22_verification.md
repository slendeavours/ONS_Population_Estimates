# S22 — verification suite

Run 2026-08-13T01:32:19+00:00. Source A Council Taxbase 2025 in England, revised 2026-01-21. Source B Table 615: vacant dwellings by local authority district: England, from 2004.

## Hard gates

Any failure halts the build and leaves the database in its pre-load state. Gates 1, 2, 3 and 5 run inside the load transaction; gate 4 re-runs the whole load; gate 6 runs after the W1 re-run.

| # | gate | result | detail |
|---|---|---|---|
| 1 | la_council_taxbase_empties has exactly 296 rows for the latest taxbase_year | PASS | 296 rows at taxbase_year 2025 |
| 2 | every lad24cd exists in la_boundaries | PASS | 0 orphan codes |
| 3 | national reconciliation within 0.5% of the release-page figures | PASS | 7 measures, worst deviation 0.0667% |
| 4 | idempotency: a second full load changes nothing | PASS | row counts and value sums identical after the second load |
| 5 | no negative counts; lte_rate_pct within 0-100 | PASS | 0 negative CTB counts, 0 negative Table 615 counts, 0 rates out of range |
| 6 | W1 re-run completes and all five new staging_la_signals columns populate for 296 LAs | PASS | run 12: 296 rows; ctb_total_dwellings 296/296, ctb_empty_6m_plus 296/296, ctb_empty_homes_premium 296/296, ctb_second_homes 296/296, ctb_lte_rate_pct 296/296 |

### Gate 3 — national reconciliation, measure by measure

| measure | target | source of target | loaded | deviation | result |
|---|---|---|---|---|---|
| total dwellings | 25,800,000 | release page | 25,817,220 | 0.0667% | PASS |
| empty dwellings (all, excluding exempt) | 542,000 | release page | 542,260 | 0.048% | PASS |
| empty homes charged a premium | 153,000 | release page | 152,928 | 0.0471% | PASS |
| second homes | 268,000 | release page | 267,894 | 0.0396% | PASS |
| unoccupied exempt dwellings | 212,000 | release page | 212,004 | 0.0019% | PASS |
| empty 6 months plus | 309,889 | workbook England row | 309,889 | 0.0% | PASS |
| empty under 6 months | 232,371 | workbook England row | 232,371 | 0.0% | PASS |

Two of these have no national figure printed on the release page. `empty_6_months_plus` and `empty_under_6_months` are **NOT FOUND on the release page** — that is different from unchecked. For those two the reconciliation target is the publisher's own England total row in the same local-authority-level workbook, which is stated in the table above rather than left implicit.

## Soft checks

Reported, not halting.

| # | check | result |
|---|---|---|
| 7 | spot-check against the Empty Homes Network November 2025 report | 5 of 5 match MHCLG exactly |
| 8 | authorities with a zero or null empty homes premium count | 5 of 296 (the release states 291 of 296 applied a premium, so 5 is expected) |
| 9 | la_vacant_dwellings_615 rows by mapping_status | 7,170 rows, years 2004 to 2025 |
| 10 | la_ctb_exemption_classes built or not found | BUILT — 3,256 rows across 296 authorities |

### Check 7 — Empty Homes Network spot-check

Values on the right are transcribed from the Empty Homes Network's November 2025 report on the 2025 Council Taxbase. That report is a derived secondary source with known transcription defects, and MHCLG revised the release in January 2026. **Where they differ MHCLG is correct.** No loaded value has been adjusted to match.

| LA | measure | MHCLG (loaded) | EHN Nov 2025 | agree |
|---|---|---|---|---|
| Liverpool | long-term empty | 4,551 | 4,551 | yes |
| Liverpool | total dwellings | 242,354 | 242,354 | yes |
| Liverpool | premium | 2,223 | 2,223 | yes |
| Sheffield | long-term empty | 2,657 | 2,657 | yes |
| Sheffield | total dwellings | 262,909 | 262,909 | yes |
| Sheffield | premium | 1,490 | 1,490 | yes |
| Kingston upon Hull | long-term empty | 2,181 | 2,181 | yes |
| Kingston upon Hull | total dwellings | 125,509 | 125,509 | yes |
| Kingston upon Hull | premium | 1,133 | 1,133 | yes |
| St Helens | long-term empty | 1,516 | 1,516 | yes |
| St Helens | total dwellings | 86,313 | 86,313 | yes |
| St Helens | premium | 1,013 | 1,013 | yes |
| Bradford | long-term empty | 3,449 | 3,449 | yes |
| Bradford | total dwellings | 224,156 | 224,156 | yes |
| Bradford | premium | 2,190 | 2,190 | yes |

### Check 8 — authorities not charging an empty homes premium

5 of 296 authorities report a zero or null empty homes premium count. The release states that 291 of 296 authorities applied a premium in 2025, so 5 is the expected figure. Not materially different.

| LA | empty_homes_premium_count |
|---|---|
| Amber Valley | 0 |
| Bolsover | 0 |
| Castle Point | 0 |
| Gravesham | 0 |
| Ribble Valley | 0 |

### Check 9 — Table 615 geography resolution

| mapping_status | rows | distinct published codes | first year | last year |
|---|---|---|---|---|
| direct | 6,277 | 296 | 2004 | 2025 |
| unmapped | 891 | 80 | 2004 | 2022 |
| resolved_via_lookup | 2 | 2 | 2025 | 2025 |

Years loaded: 2004 to 2025.

`unmapped` rows are districts abolished under local government reorganisation (Northamptonshire 2021, Buckinghamshire 2020, Dorset and Bournemouth/Christchurch/Poole 2019, Somerset, North Yorkshire and Cumbria 2023, and others). They keep a null `lad24cd` by design. They are not aggregated into successor unitaries: mapping six Somerset districts onto E06000066 would make any downstream sum count Somerset six times over. `la_code_lookup` was read, never written.

`resolved_via_lookup` covers the two pure recodes of 1 April 2025 (SI 1328/2024): Barnsley E08000038 to E08000016 and Sheffield E08000039 to E08000019. Same area, new number, so they resolve. The same two codes appear in the Council Taxbase release and are resolved the same way there.

### Check 10 — LA-level exemption class breakdown

**BUILT.** Table 2.01 on the `Supplementary Data` sheet of the local-authority-level workbook publishes exemptions by class at local authority level, one column per class A to W. The eleven unoccupied classes are loaded: 3,256 rows across 296 authorities. No regional figure was substituted and nothing was apportioned.

The class set was verified rather than assumed: classes B, D, E, F, G, H, I, J, K, L and Q sum across England to 212,004, reproducing the release page's "212,000 dwellings that were receiving an exemption that were unoccupied".

## Structural breaks recorded

| first period | affected column | dimension |
|---|---|---|
| 2024-04-01 | empty_homes_premium_count | premium threshold |
| 2025-04-01 | second_homes | premium introduction |

Both are cited to the MHCLG technical notes at https://www.gov.uk/government/statistics/council-taxbase-2025-in-england/local-authority-council-taxbase-in-england-2025-technical-notes.

## Run log

Logged to `pipeline_run_log` as id 83, run_id `5eeccdce-b129-445b-9675-c518ef2cf172`, source_number `22`, status `complete`, rows_written 10,724.

This report was regenerated once while hardening `scripts/_db.py`. Re-running `scripts/s22_verify.py` inserts a fresh log row each time; the duplicate (id 84, identical status and row count) was removed so the source has exactly one run log entry.
