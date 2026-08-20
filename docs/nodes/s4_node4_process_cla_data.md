# S4 Node 4 — Process CLA Data

**Type:** Code (JavaScript)

## Purpose

Parses the three CSVs from the Merge node, filters to LA-level rows, buckets the 17-21 accommodation types into analytical categories, handles suppression symbols, and produces a single deduplicated batch JSON for upsert. `new_la_code` is passed through as-is; Node 6 resolves it.

## Bucketing

| Bucket | DfE categories |
|---|---|
| `semi_independent` | Semi-independent, transitional accommodation **+ Foyers + Supported lodgings** |
| `independent_living` | Independent living |
| `with_family` | With parents or relatives + With former foster carers |
| `community_home` | Community home |
| `unsuitable` | Bed and breakfast + Emergency accommodation + No fixed abode/homeless |
| `other` | In custody + Gone abroad + Deported + Ordinary lodgings + Other accommodation |
| `not_known` | Residence not known + Total information not known + LA not in touch |

`Total` rows are skipped, and `total` is derived from the bucket sum after the loop to avoid double counting.

## semi_independent is a pipeline definition, not a DfE one

The three-category grouping is deliberate. It is **not** the same as DfE's published `Semi-independent, transitional accommodation` category, which is narrower.

External documents must quote the published category alone, held in `semi_independent_published`. See [s4_care_leaver_source.md](../s4_care_leaver_source.md) for the decision and the numbers.

## Suppression handling — code behaviour differs from the original note

`safeInt` returns null for `c`, `k`, `z` and `x`. The 17-21 path then applies `safeInt(r.number) || 0`, so on that path **suppressed values are added as zero, not carried as null**. The 22-25 path uses `safeInt` directly and does preserve null.

The consequence is that 17-21 bucket counts and `total_care_leavers` are **minima**. `total_published`, read from DfE's own Total row, is the correct total to quote. Liverpool 2024: 759 derived against 776 published.

An earlier version of this note said suppression symbols are "treated as null" throughout. That is true only of the 22-25 path.

## Deduplication

Deduplicates on `(new_la_code, reporting_year, age_group)`, first occurrence wins. Because Node 1 is input index 0 and its file overlaps Node 2 on 2020–2023, **the older edition's figures win for overlapping years**, discarding DfE revisions. Seven rows were affected. When editions overlap, the newer file should be ordered first.

## Column names

Reads `age`, `accommodation_type` and `number`. The 2025 release renamed these to `care_leaver_age`, `breakdown` and `care_leaver_count`. Against a renamed file every row falls through to `other` and every bucket returns zero **with no error raised**. Any edition change requires a column-name assertion that fails loudly.

## Output Fields

| Field | Value |
|---|---|
| `row_count` | 1413 |
| `batch_json` | JSON array string for the Node 6 `$1` parameter |
| `years_1721` | [2019, 2020, 2021, 2022, 2023, 2024] |
| `years_2225` | [2023, 2024, 2025] |

## Connection

- Input: Merge node (3 items)
- Output: Create Table (Node 5)

## Verified Output

`row_count: 1413`. (2026-03-31)
