# S15 edition history: first load

Status: done 2026-10-09 (loader built and tested 2026-10-08). Works like
[S8b](2026-10-08-s8b-editions-first-load.md), [S1b](2026-10-06-s1b-edition-history.md) and
[RO4](2026-10-07-ro4-edition-history.md).

## What was recorded

S15 is the UK House Price Index average prices by local authority and month (Land Registry and ONS):
all-property average price, seasonally adjusted price, annual change, and average price by detached,
semi-detached, terraced and flat.

New append-only table `la_house_prices_editions` (update, delete and truncate blocked by trigger),
keyed by authority, month and edition. The live table `la_house_prices` is still one row per authority
and month, now defined as the latest edition of each month.

- Edition 1, recorded 'as loaded' from the live table: 55 months (January 2022 to July 2026), 295
  authorities each, 16,225 rows. Where a month's rows had been loaded on several days, edition 1 carries
  the latest of those dates.
- Edition 2: stored by the first comparison against the July 2026 release (below), 14 months, 4,130 rows.

The editions table now holds 20,355 rows. Suppressed prices (low-volume property-type prices) are NULL,
never zero. The seasonally adjusted price is NULL throughout, as published in this file.

## What the first comparison found

The July 2026 files (the latest published; the downloads page and the data both name July 2026) were
compared cell by cell with edition 1. Nothing was missing, no price turned from a number into a blank,
no price was zero or negative, and no revision exceeded 9%.

- **14 of 55 months were revised**, from May 2025 to June 2026. The 41 months before May 2025 and the
  latest month (July 2026) are unchanged.
- **3,540 area-month rows revised**, 20,513 changed cells. Edition 2 stores the whole of each revised
  month (14 x 295 = 4,130 rows), including the 590 rows that did not change.
- May and June 2025: 2 authorities each (E08000016 and E08000019, Barnsley and Sheffield). July 2025 to April 2026:
  all 295 authorities. May and June 2026: 293 authorities.
- The July 2026 average price is unchanged, so the map's average-price layer (which reads the latest
  month) does not move because of this load.

Largest five changes to the all-property average price, in the month with the largest single
revision (March 2026):

| Authority | Edition 1 | Edition 2 | Change |
| --- | --- | --- | --- |
| E09000001 (City of London) | 626,489 | 682,379 | +8.92% |
| E07000123 | 184,398 | 187,559 | +1.71% |
| E07000074 | 392,755 | 399,074 | +1.61% |
| E07000117 | 129,250 | 131,133 | +1.46% |
| E07000128 | 189,057 | 191,407 | +1.24% |

For April 2026 the largest are the City of London (+6.42%), E07000198 (+2.19%), E09000019 (+1.77%),
E07000079 (+1.70%) and E07000123 (+1.66%). The City of London, with very few sales, accounts for the
biggest moves in every recent month.

Pattern by age of month (mean absolute change in the all-property price across 295 authorities):

| Months | Mean absolute change |
| --- | --- |
| May to June 2025 | 0.001% (two authorities only) |
| July to September 2025 | 0.04% to 0.15% |
| October 2025 to January 2026 | 0.27% to 0.40% |
| February 2026 | 0.20% |
| March to June 2026 | 0.35% to 0.44% |

Revisions are small but grow towards the recent months: the four months before the newest move most.
Whether older months have settled for good is a reading of this one comparison, not something the
source states.

`refresh-latest` then copied edition 2 into the live table for the 3,540 revised rows (before and
after guard passed). The live table still has 16,225 rows, 55 months and 295 authorities; its
content hash changed as expected, only in those revised rows.

## The monthly check

From `ONS_Population_Estimates`:

1. `python scripts/s15_hpi_editions.py load` downloads the latest two files, checks they name the same
   edition, and previews every held month (new, unchanged or revised, with area and cell counts).
   Nothing is written.
2. Read the preview. Stop and look if a month has fewer than 295 authorities, a NULL replaces a number
   in more than five areas, a price is zero or negative, more than ten areas of a month revise by over
   50%, or an area code is unresolved. The loader refuses short months and unresolved codes.
3. `python scripts/s15_hpi_editions.py load --commit` stores new months (edition 1, with their live rows,
   in one transaction) and revised months as the next edition.
4. If any month was revised, `python scripts/s15_hpi_editions.py refresh-latest` previews the live rows
   that would change; then run it with `--commit`.
5. `python scripts/s15_hpi_editions.py status` should say OK, and
   `python scripts/s15_hpi_editions_verify.py` should pass all 20 gates.

After a `refresh-latest --commit` that wrote rows, run `scripts/w1_run.py` and then
`scripts/refresh_map.py` (the loaded-at follow-up in the S8b note applies here too). That was not done
as part of this load.

## Notes

- The first `sync-new` needed `--expected-authorities 295`, because no month had editions yet.
- Barnsley and Sheffield codes resolve through `scripts/geography.py`, with S15's own declaration; a
  file in the other form halts before any write.
