# S11 CQC snapshots: first load

Status: done 2026-10-09. Works like [S9b](2026-10-09-s9b-editions-first-load.md),
[S9a](2026-10-09-s9a-editions-first-load.md), [S18](2026-10-09-s18-editions-first-load.md) and
[S15](2026-10-08-s15-editions-first-load.md). Loader: `scripts/s11_cqc_editions.py`; gates:
`scripts/s11_cqc_editions_verify.py` (22 gates, all PASS, exit 0). The mapping method itself is unchanged
([2026-07-12 note](2026-07-12-s11-cqc-la-mapping-method.md)).

## What the old loader did

S11 is the CQC register of Adult social care locations (the "HSCA Active Locations" file, one per month).
The old load step upserted with `INSERT ... ON CONFLICT (location_id) DO UPDATE`, setting `loaded_at`, so each
new file overwrote the previous values of every location still present. The July 2026 snapshot was
overwritten on 2026-08-13 and the August snapshot on 2026-09-04 (about 30,400 locations each time); only the
locations that dropped out kept their last values (128 from July, 108 from August). The July file was loaded
twice on 2026-07-12 (two run-log rows a minute apart, 30,492 rows each). The map step wrote
`cqc_unresolved_locations` on every run, in place, with no preview, from August (the July five were never
recorded). `is_active = false` was called a deregistration signal; it only ever meant "absent from the Adult
social care scope of the next file".

## What was written

- New tables: `cqc_location_snapshots` (live, one row per location per dated snapshot, the "as at" date taken
  from the file's README), `cqc_location_snapshots_editions` (append-only),
  `cqc_location_snapshots_editions_file_checks` (the file-check ledger, append-only) and
  `cqc_location_snapshots_unresolved` (append-only, one row per unresolved location per snapshot).
- Four snapshots, edition 1 each: 2026-07-01 30,492 rows, 2026-08-04 30,563, 2026-09-01 30,561 and
  2026-10-01 30,555 (122,171 rows). Live and editions agree cell for cell; every live row of a snapshot
  carries that snapshot's file as `source_file`; each snapshot has all 296 authorities. Unresolved rows: 5, 5,
  5 and 4 (19).
- `cqc_locations` is now a view over the snapshots with the same columns and types. The old table is kept as
  `cqc_locations_legacy` (30,797 rows; not dropped) and `cqc_unresolved_locations` (the 5 open rows) is
  untouched.

## How July and August were rebuilt

The three held files are still on disk in `data/raw/` (sha256 starting 9c85abe4, ae87c164, 1b640244), so the
snapshots were rebuilt by parsing them, not guessed.

- Every legacy row of a date was compared with its file on every publisher column: July 128 rows, August 108
  rows, September 30,561 rows, 0 differences, none missing. For July and August these residue rows are the
  only legacy evidence left, and they equal the file.
- Row counts equal the old run log: 30,492 / 30,563 / 30,561.
- September was taken as loaded (30,561 rows, legacy mapping); its unresolved set is the 5 open legacy rows.
- Mapping: nearly every row carries coordinates, and point-in-polygon reproduced the held `lad24cd`. The rows
  without usable coordinates were mapped as the July note describes. July: 4 reconstructed (3 by postcodes.io,
  1 by the terminated-postcode endpoint) and 2 nearest-polygon rows (379 m and 669 m). August: 2
  reconstructed (1 postcodes.io, 1 terminated) and 2 nearest. That matches the July note (nearest 2,
  postcodes.io 3, terminated 1) for the July snapshot; August simply has fewer no-coordinate rows. Codes go
  through `geography.py`.
- Locations the old loader recorded as unresolved are kept unresolved "as loaded" in every rebuilt snapshot.
  One of the five (1-28257167158, Tealwood Grange, LN5 9WQ) now resolves through postcodes.io (E07000139,
  North Kesteven). Mapping it in July to September would have put into those snapshots a row they never
  held, so it stays unresolved there; it is mapped in October, which is why October has 4 unresolved.

## The view, and why

W1 (`sql/w1/05_la_signals.sql`), the map export and the registry read `cqc_locations` with the old columns
(`is_active`, `deregistered_seen_date`, `source_file_date`). None of those readers was changed. The view
returns one row per location ever seen, with its values from the latest snapshot containing it,
`source_file_date` that snapshot's date, `is_active` true only for locations in the latest snapshot and
`deregistered_seen_date` the first later snapshot. In the migration transaction the view was compared with the
legacy table on every column but `loaded_at` (EXCEPT ALL both ways, 0 and 0 rows), and W1's S11 subquery gave
the same count per authority before and after the swap (291 authorities, 4,765 locations). Gates 16 and 17
repeat both checks; once a newer snapshot exists, `is_active` equals the latest snapshot's row count (now
30,555).

## October 2026 loaded

The CQC page labels the file 02 October 2026; its README says as at 2026-10-01
(`01_October_2026_HSCA_Active_Locations.ods`, sha256 starting 4b6435b946696db2). 57,097 rows, of which 30,559
Adult social care. Mapping: 30,552 point-in-polygon, 2 nearest (the same two Cheshire West and Chester rows),
1 postcodes.io; 4 unresolved. Against September: 30,561 to 30,555 rows, 179 gone, 173 new, 1,669 common rows
changed (longitude 831, latitude 815, rating publication date 418, latest overall rating 222, dormant 176,
postcode 111, inherited rating 96, mental health band 49). No stop condition tripped, and the real preview
equalled the earlier read-only preview in everything but the time. Supported-living (active, non-dormant)
counts change in 87 authorities, by at most 3; the total over 291 authorities goes from 4,765 to 4,791 (net
+26). The 179 locations gone now show `is_active` false; they are absent from this file, not necessarily
deregistered.

## The 2 July "deregistered" that moved to Hospitals

Of the 128 July locations the old loader marked inactive, 2 were still in the August file under the
Hospitals directorate (outside the Adult social care scope S11 takes). The snapshots record what is true: they
left the S11 scope, they were not deregistered. Leave and return still behaves as before: one location marked
inactive from August (108 before, 107 now) is back in the October file.

## Engine gap

`refresh-latest` cannot repair a snapshot whose latest edition has a different set of locations from live. A
reissued same-date file that added or dropped locations would be stored but never reach live, so `load` stops
it (REJECTED, nothing stored). This has not happened (four files, four dates). Follow-up: a whole-period
replace in the engine, for when it is first needed.

## The monthly check

From `ONS_Population_Estimates`:

1. `python scripts/s11_cqc_editions.py load` reads the CQC page, picks the filters file, skips files already
   in the ledger and previews the new snapshot. Nothing is written to the database (downloads go to
   `data/raw/`; postcodes.io is read for rows without coordinates).
2. Read the preview. Stop conditions are enforced in `load` (REJECTED, exit 1): identity (README title and
   as-at date against the file name), a header change, an unexpected marker, a Barnsley or Sheffield new
   code in the file, a location over 2 km from every polygon, rows more than 2% from the previous snapshot,
   over 1,000 gone or new, over 10 unresolved, an authority with no location, supported living over 50% in
   more than 5 authorities with at least 10, and an older file (`--allow-older-file`).
3. `python scripts/s11_cqc_editions.py load --commit`.
4. `status` says OK; `python scripts/s11_cqc_editions_verify.py` passes all 22 gates.
5. There is no `refresh-latest` step unless a recheck stored a second edition.

## Notes

- W1, `refresh_map.py` and the export were not run here. The next W1 run will change
  `supported_living_locations` in about 87 authorities (by at most 3) and the map label (September to
  October 2026).
- The old loader scripts are retired in a later step of this plan.
