# Census 2021 tenure was wrong for the four 2023 unitary authorities

**Date:** 2026-08-20
**Status:** Closed

## What was wrong

`la_tenure_2021` held Census 2021 TS054 tenure counts re-based from predecessor
districts onto the four unitary authorities created in April 2023. Three of the four
were substantially wrong:

| Authority | Held | Published | Understated by |
| --- | ---: | ---: | ---: |
| Cumberland | 106,020 | 125,424 | 19,404 |
| Westmorland and Furness | 72,270 | 103,529 | 31,259 |
| Somerset | 174,184 | 250,124 | 75,940 |
| North Yorkshire | 274,385 | 274,381 | overstated by 4 |

Somerset was understated by roughly 30 per cent of its households.

## Why it happened

The re-basing was unnecessary. ONS publishes TS054 on 2023 local authority boundaries
through NOMIS, so the four unitaries can be queried directly on their current codes.
Re-basing from predecessor districts introduced arithmetic that nothing checked, and
the check that would have caught it, comparing the result against the published figure
for the same code, was never run because the assumption was that no published figure
existed at that geography.

## The fix

All four authorities reloaded directly from NOMIS `NM_2072_1` on current codes. The
remaining 292 authorities were already exact and were left alone.

Verified after the fix: all nine tenure categories across all 296 authorities, 2,664
cells, zero mismatches against NOMIS.

`social_rented_total` and `private_rented_total` are generated columns in Postgres and
recompute from their components, so they were not written directly.

## What to watch

When a source publishes at a geography the pipeline thinks it does not, the pipeline
will quietly invent its own version. Before re-basing, apportioning or aggregating any
source onto successor codes, query the publisher for those codes first. If the answer
comes back, use it.

The same trap applies to any other source currently re-based across the 2023
reorganisation. This audit checked tenure only.
