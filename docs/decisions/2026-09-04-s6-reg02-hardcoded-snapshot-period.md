# Reg_02's snapshot period was fixed in source, 2026-09-04

## What happened

S6 was refreshed to the year-ending-June-2026 edition — the first refresh since
the original build of 25 July 2026. The load failed verification twice before it
committed, and both failures were real.

`parse_reg_02` set `period = ANCHOR_PERIOD`, a date fixed in source at
2026-03-31. Reg_02 is a **single snapshot per edition**, not a time series, so
its `period_ending` is a property of whichever file was discovered. Stamping
every edition with the same date meant each refresh wrote the new snapshot onto
the primary key of the old one and **overwrote a quarter of history in place**.

## Why nothing would have caught it

`la_immigration_groups` has the primary key
`(period_ending, lad24cd, pathway, sub_pathway)`. With the period constant, an
overwrite is a legal upsert. After the refresh the table would have held 3,552
rows, exactly as before; every key unique; all 296 authorities present; the
internal reconciliation of pathways against totals passing for all 296, because
the June data reconciles against itself perfectly well.

Row count, key uniqueness, coverage and internal consistency were all
insensitive to the fault. This is the same shape as the six errors found in the
August 2026 source assurance: the structural tests were green and the data was
wrong.

**Check 9 caught it** — the cross-source reconciliation of Reg_02's
supported-asylum total against the Asy_D11 aggregate — failing with 263 of 286
authorities divergent. It was comparing a June snapshot against March Asy_D11.

The generalisable point: **a cross-source gate earns its keep on the second
edition, not the first.** On a first build there is only one edition, every
constant is trivially correct, and a gate that compares two sources looks like
belt and braces. Nothing internal to Reg_02 could ever have found this, because
Reg_02 on its own was never inconsistent.

## One constant doing two jobs

`ANCHOR_PERIOD` was serving as both a *verification anchor* — a period whose
published headline figures had been sourced by hand, used by checks 3, 5, 8a and
8b — and as Reg_02's *data period*. Only the second was wrong.

The anchor legitimately stays fixed. Asy_D11 is a full time series and each new
edition still contains the anchored period, so an unchanged anchor that still
reconciles is positive evidence the back series was not silently revised. It did
reconcile: Birmingham 2,142, Liverpool 2,053, Coventry 1,712 at 2026-03-31, all
exact against the June edition.

The fix separates them. `_edition_period()` derives the snapshot period from the
edition label and **hard-stops on a label it cannot parse** rather than falling
back to an assumed date. Checks 9 and 10 take that period as a parameter; the
`ANCHOR_*` constants are untouched.

## The second failure, which the first fix caused

Check 4 then failed. It counted `la_immigration_groups` with no period filter
and compared the total against the rows just parsed. That assertion was only
ever correct *because* the period was hardcoded and every load overwrote in
place — the bug was holding up the test. Now that the table accumulates one
snapshot per edition, which is what `period_ending` is in the key for, the check
counts at the period being loaded and reports how many snapshots are retained.

Worth noticing on its own: **a test can be passing because of the defect, not in
spite of it.** Fixing the defect broke the test, and the test was the thing that
needed correcting.

## A third defect, found in passing

`_write_anomalies` built its output path from
`os.path.dirname(os.path.abspath(__file__))` + `docs`. That was correct while
the script sat at the repository root. Since the 2026-08-20 tidy moved it into
`scripts/`, it had been writing `scripts/docs/s6_source_anomalies.md` and
leaving the tracked `docs/s6_source_anomalies.md` frozen at the 26 July run.

This is the same defect class as the `.env` resolution that gate 11 exists for,
and gate 11 does not cover it: **gate 11 asserts how scripts resolve
credentials, not how they resolve output paths.** Nothing checks the latter. The
path now resolves from the repository root, as `scripts/_db.py` does.

## What was not done

The anchor constants were **not** moved to June 2026. Checks 3, 5, 8a and 8b
therefore verified the March quarter, which the June file still contains and
reproduced exactly. The June quarter itself is verified by checks 1, 2, 4, 6, 7,
9, 10, 11 and 12, but its England and UK headline totals have **not** been
compared against anything the Home Office published in prose. Re-anchoring means
sourcing six figures by hand from the release narrative; the position is
recorded in the refresh procedure in `docs/s6_asylum_source.md` rather than left
implicit.

## Result

`la_asylum_support` 21,953 rows across 34 quarters to 2026-06-30.
`la_immigration_groups` 7,104 rows across two retained snapshots. All 13 checks
pass. The March snapshot that earlier refreshes would have destroyed is intact,
because the fault was caught before the first refresh committed rather than
after several had.
