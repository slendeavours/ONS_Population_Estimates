# S8b edition history: first load

Status: done 2026-10-08. Follows the S8b editions design (2026-10-08, kept outside this repository)
and works like [S1b](2026-10-06-s1b-edition-history.md) and [RO4](2026-10-07-ro4-edition-history.md).

## What was loaded

S8b is the DWP Stat-Xplore Housing Benefit caseload by accommodation type (supported accommodation,
temporary accommodation, other, unknown), one figure per authority, month and type.

New append-only table `la_hb_accom_type_caseload_editions` (update, delete and truncate blocked by
trigger), key `(lad24cd, month, accom_type, edition)`. It now holds edition 1 for each of the seven
months the live table held, recorded 'as loaded':

| Month | Edition | Rows | Claimants (all four types) | Live rows loaded |
| --- | --- | --- | --- | --- |
| 202509 | 1 | 1,184 | 1,336,665 | 21 Jul 2026 |
| 202510 | 1 | 1,184 | 1,303,621 | 21 Jul 2026 |
| 202511 | 1 | 1,184 | 1,266,845 | 21 Jul 2026 |
| 202512 | 1 | 1,184 | 1,253,863 | 21 Jul 2026 |
| 202601 | 1 | 1,184 | 1,255,660 | 21 Jul 2026 |
| 202602 | 1 | 1,184 | 1,246,881 | 21 Jul 2026 |
| 202603 | 1 | 1,184 | 1,242,418 | 1 Oct 2026 |

8,288 rows in all: 296 authorities x 4 types x 7 months. No NULLs. Zeros are kept as zeros (mostly in
the unknown type, as published).

## What the recheck found

All seven months were fetched again from the Stat-Xplore API on 2026-10-08 and compared cell by cell
with edition 1. **Every month is unchanged: no revisions since the July load** (and, for 202603, since
the 1 October load). Nothing was stored beyond edition 1, and `refresh-latest` had nothing to write.

The live table `la_hb_accom_type_caseload` was not changed: 8,288 rows before and after, with the same
content hash. The API's latest month is still 202603, so no new month was loaded.

S8b is therefore current with the source; the source registry's overdue flag reflects cadence against the last logged run, not a missed load.

The 2026-08-14 revision finding compared the April S8 load with the July S8b load. That revision was
already in the July data, which is why this recheck finds nothing new.

## The monthly check

From `ONS_Population_Estimates`:

1. `python scripts/s8b_hb_editions.py load` previews: it fetches every month the API offers that is not
   held and is later than the earliest held month (gaps are filled; nothing before the first held month;
   with nothing held, `--months` must be given), plus the latest six held months as a revision check, and says for each month whether
   it is new, unchanged or revised. Nothing is written. `--recheck-all` rechecks every held month.
2. Read the preview. A short month (fewer than 296 authorities or a type missing) is refused by the
   loader. Before committing, also stop and look if NULLs appear where there were numbers, if any value
   is negative, or if a revision is large on many authorities.
3. `python scripts/s8b_hb_editions.py load --commit` stores new months as edition 1 and inserts their live
   rows in the same transaction; revised months are stored as the next edition and reach live through
   step 4. Unchanged months store nothing.
4. `python scripts/s8b_hb_editions.py refresh-latest` previews the live rows that would change; then run it
   with `--commit`.
5. `python scripts/s8b_hb_editions.py status` should say OK, and `python scripts/s8b_hb_editions_verify.py`
   should pass all 16 gates.

The Stat-Xplore key is read from the environment and is never printed or stored.

## Notes

- The first `sync-new` needed `--expected-authorities 296`, because no month had editions yet and the
  count could not be derived. Later runs derive it.
- `python scripts/s8b_hb_editions_verify.py`: all 15 gates passed on the real tables at the first load;
  gate 16 (a new month reaches live with edition 1, in one transaction) was added in the final review fix.

## Follow-ups

- The core's `refresh_latest` does not bump `loaded_at`, so `scripts/refresh_map.py` (which judges W1
  staleness by the latest `loaded_at` of its input tables) cannot see a revision refreshed into live.
  Until that is decided, the procedure says to run `scripts/w1_run.py` and then `refresh_map.py` after any
  `refresh-latest --commit` that wrote rows. Whether the core should stamp `loaded_at`, or W1 should judge
  staleness another way, is a design question for later.
