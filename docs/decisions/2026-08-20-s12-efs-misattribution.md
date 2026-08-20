# Exceptional Financial Support rows were attributed to the wrong council

**Date:** 2026-08-20
**Status:** Closed

## What was wrong

`la_efs_support` held two rows against Hammersmith and Fulham (`E09000013`):

| Year | Held against | Amount held |
| --- | --- | ---: |
| 2025-26 | Hammersmith and Fulham | £37.0m |
| 2026-27 | Hammersmith and Fulham | £84.0m |

Hammersmith and Fulham appears on neither the 2025-26 nor the 2026-27 MHCLG
guidance page. Haringey (`E09000014`) appears on both and had no rows at all.

The 2026-27 page gives Haringey £84.0m of in-principle support, the exact figure
held against Hammersmith and Fulham. The 2025-26 page gives Haringey £40.6m,
against the £37.0m held.

Both rows belonged to Haringey. The 2025-26 amount was also wrong.

## The fix

Both rows re-attributed to `E09000014` and the 2025-26 amount corrected to
£40.6m. The source string on each row records the correction.

After the fix the 2026-27 authority set reconciles exactly: 35 LAD-coded bodies
on the page, 35 in the table, none on one side only.

The page also lists East Sussex, Worcestershire, Kent Police and Crime
Commissioner and South Yorkshire Mayoral Combined Authority. None has a LAD24
code and none is expected in a LAD-keyed table, so their absence is correct.

## Effect on published work

None. Liverpool has never received Exceptional Financial Support and appears on
no edition of the page, so `efs_flag` was false before and remains false. The
LandAid paper omits the fiscal-risk paragraph on that basis, correctly.

The error mattered for any national or London comparison, where one borough's
distress was recorded against a neighbouring borough.

## How it was found

Only by comparing the full authority list against the publication. Row counts,
key integrity and Liverpool's own value all looked correct, because the row
count was right and the wrong code is a valid code. Nothing short of a
name-by-name reconciliation would have surfaced it.

## What to watch

The names on these pages are not always LAD24 names, and fuzzy matching them is
unsafe: an early pass in this audit matched Woking to Wokingham and Gloucester
to South Gloucestershire. Match exactly against `la_boundaries.lad24nm`, and
treat anything that fails to match as a body needing judgement rather than a
near-miss to be resolved automatically.
