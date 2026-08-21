# A derived view was dropped and nothing noticed, 2026-08-21

## What happened

A YADA build failed on `relation "v_la_rate_triangulation" does not exist`.

The view had gone some time after the 2026-08-20 S20 object rename
(`s20_ratecard_neutralise_object_names.py`), which drops the view, renames
`commercial_rate_card` and `commercial_rate_area_mapping`, and rebuilds the view
from a `VIEW_SQL` literal. The table renames survived. The view did not, so the
rebuild half of that migration did not stick.

It has been restored verbatim from that literal: 155 rows, every one carrying a
four-bed rate and an LHA SAR, pinned to a single rate card date.

## Why the suite was green the whole time

Every existing gate reads base tables. Row counts, key uniqueness and coverage
all passed, correctly, because the base tables were healthy — the rename had
worked. Not one of the 18 gates read a derived object.

A view is not implied by the tables under it. It is a separate object with its
own lifetime, and dropping it leaves no mark on anything it read. There was no
degraded signal to spot: right up until the build ran, everything a check could
look at was genuinely fine.

So the detector was a downstream crash. That reports after the fact, on
somebody else's schedule, and only for the one view that happened to be read
first. Seven other views were equally unguarded.

## Gates 19-21

- **19** — every view in `DERIVED_VIEWS` exists and returns at least one row.
  It reads the catalogue for the object and then reads the object, because a
  view can exist and still be unrunnable: Postgres tracks a view's table
  dependencies but not its function ones, so dropping a function a view calls
  leaves the view standing and broken. That case is caught per-view on a
  savepoint rather than aborting the suite.
- **20** — no view has lost a column it is declared to be read for. A view
  rebuilt from a stale definition comes back existing and populated, so gate 19
  passes while a column something downstream selects is simply absent. Extra
  columns are allowed; adding one is additive, losing one is not.
- **21** — `v_la_rate_triangulation` carries exactly one `rate_card_date`, and
  that date is `MAX(rate_card_date)` in `commercial_rate_card`. The view pins
  itself to the current card by construction. Several editions are loaded, so a
  rebuild that lost the pin would fan every authority across all of them and
  read as though superseded rates were current — and the row count would go
  *up*, which looks like coverage improving. Asserted against the table rather
  than a written-down date, so loading a new card needs no edit here.

All eight views the pipeline reads are covered, not just the one that broke.

## Proving they can go red

Faults were injected by mutating the manifest, so the code under test is the
shipped code:

- a declared view absent from the database — gate 19 fails, exit 1;
- a view missing a declared column — gate 20 fails, exit 1;
- the view pinned to a superseded card (asserted against the 20260820 backup,
  whose latest edition is older) — gate 21 fails, exit 1.

Two branches of gate 19 are not exercised live and should be treated as
unproven: the zero-row path, because no relation in the database is currently
empty, and the unrunnable-view path, because the connecting role can read
everything.

## The general lesson

**A check on the inputs is not a check on the derived object.** Healthy base
tables say nothing about whether the view over them still exists, still runs, or
still has the shape its readers assume.

**If the only thing that detects a fault is a downstream crash, there is no
control.** The crash found one view. It could not have found the other seven,
and it reported after the damage rather than before it.
