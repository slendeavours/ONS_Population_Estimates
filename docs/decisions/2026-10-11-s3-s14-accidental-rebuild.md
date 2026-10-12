# S3 and S14 live tables rewritten by accident, and repaired, 2026-10-11

## What happened

At 20:30 UTC on 2026-10-11, during Task 1 of the S3 and S14 editions plan
(retiring the old loaders), the old loaders ran for real against
`exempt_pipeline`. The new subprocess tests that prove each retired script
exits at once were run "failing first", before the guards had been written,
so each test started the old script itself:

- `scripts/s14_lha_rates_build.py` (v1) upserted `brma_lha_rates` (152 rows)
  and `la_brma_mapping` (296 rows). Run-log row 499.
- `scripts/s3_mye_refresh.py` re-upserted the 296 rows of 2025 in
  `la_population`. The values written were the same as those held. It wrote
  no run-log row (there is no row 500). Likely cause (not proved: the failed
  statement left no trace): the old script inserted its run-log row with status
  `'complete'`, and `pipeline_run_log` now has
  `CHECK (status = 'success') NOT VALID`
  (`pipeline_run_log_status_new_writes_chk`), which refuses every new row with
  another status. The script had already committed its upsert (twice: the load,
  then its idempotency re-load), so the failed INSERT came after the writes and
  used up id 500. Checked read-only on 2026-10-12: the INSERT in
  `git show de39a43^:scripts/s3_mye_refresh.py` (line 297) writes `'complete'`,
  the constraint exists and no rule on the table changes that. The script's
  `MIGRATION` DDL (a no-op `SET NOT NULL`) also ran. The id gap is not a
  rolled-back test transaction.
- `scripts/s14_lha_rates_build_v2.py` ran `DROP TABLE ... CASCADE` on
  `la_brma_mapping` and `brma_lha_rates` and rebuilt both. Run-log row 501.

This is the 2026-08-20 fault again (see
[2026-08-21-s14-brma-join-break.md](2026-08-21-s14-brma-join-break.md)), set
off by a different route. The guards now in the three files stop all of them
at their first statement.

## What was damaged

Assessed read-only first, against the plan's survey of the same day (survey
hash: md5 over every column except `loaded_at`, each value as text with NULL
as `~`, joined by `|`, one line per row ordered by period then key).

| Object | Damage |
| --- | --- |
| `la_brma_mapping` | 37 `brma_name` values back in the VOA "X & Y" spelling, so **37 authorities matched no rate row** in the W1 LHA join (`sql/w1/05_la_signals.sql`); gate 18 failed. `loaded_at` reset on all 296 rows. |
| `brma_lha_rates` | Ashford's own `source` text lost (all 152 rows carried the script's one text). `loaded_at` reset on all 152 rows. Rate values unchanged. |
| `la_population` | `loaded_at` of the 296 rows of 2025 reset. Values unchanged: the survey hash still reproduced. |
| `v_la_rate_triangulation` | **Dropped by the CASCADE.** It reads both S14 tables. A run of the suite earlier the same day had found 8 of 8 derived views present; afterwards gate 19 failed with this view absent. |

Survey hashes before the repair: `la_brma_mapping`
`05626af17c5ead6d6b5a0fad2cf487bb` (survey `2e7fb44bf374be96280741052be327db`),
`brma_lha_rates` `cabd0e670de7d3c141a1babee540b03f` (survey
`5536ba6d15071e210da60bb64d512a6e`), `la_population`
`fe07de1ef4526faa77db179ec7eae1b9` (equal to the survey).

Nothing else depended on the two tables. They have no foreign keys in either
direction, no triggers, no comments and no indexes beyond their primary keys,
and nothing in `sql/`, `scripts/` or the earlier decision notes creates one.
The `SELECT` grant to `ucws_readonly` came back on its own through the
schema's default privileges. Columns and primary keys are as surveyed.

## What was repaired

Each object was repaired in its own transaction, with checks before and
after inside it. A failed check rolled that transaction back. A simulated run
was rolled back first. These were the only database writes.

1. **`la_brma_mapping`**, the 2026-08-21 recipe: `brma_name` set to
   `replace(brma_name, ' & ', ' and ')` only where the replaced name exists in
   `brma_lha_rates` (37 rows). `loaded_at` set back to the surveyed
   2026-08-20 20:23:00.558032 UTC on all 296 rows.
2. **`brma_lha_rates`**: Ashford's `source` set back to the text written when
   its row was restored on 2026-08-20, "DWP UC LHA rates 2026-27, restored
   2026-08-20 after a header off-by-one dropped it". That text was taken from
   the record of that session. It is shown to be exact because, with it, the
   table reproduces the survey hash. `loaded_at` set back to 2026-08-20
   20:23:00.558032 UTC on 151 rows and 23:40:31.748925 UTC on Ashford.
3. **`la_population`**: `loaded_at` of the 2025 rows set back to 2026-08-13
   21:11:31.051713 UTC (2024 untouched at 2026-03-26 09:40:44.303564 UTC).
4. **`v_la_rate_triangulation`**: recreated from the `VIEW_SQL` literal in
   `s20_ratecard_neutralise_object_names.py`. That is the definition it was
   restored from, verbatim, on 2026-08-21 (see
   [2026-08-21-derived-view-dropped-unnoticed.md](2026-08-21-derived-view-dropped-unnoticed.md)).
   The literal was read from the file. The module was not imported.

## How it was verified

- All three survey hashes reproduce exactly: `la_population`
  `fe07de1ef4526faa77db179ec7eae1b9`, `brma_lha_rates`
  `5536ba6d15071e210da60bb64d512a6e`, `la_brma_mapping`
  `2e7fb44bf374be96280741052be327db`.
- `loaded_at` sets equal the survey on all three tables. Row counts are
  unchanged (592, 152, 296), and so are the population sums (2024
  58,620,101; 2025 58,834,812). `mapping_method` is still 'NaN' on 295 rows
  and `nearest_fallback` on E06000053, and `brma_secondary` is still NULL on
  all 296.
- The W1 LHA join (`la_boundaries` to `la_brma_mapping` to `brma_lha_rates`
  at the latest financial year) leaves 0 of 296 authorities unmatched.
- The view returns 155 rows, the count recorded on 2026-08-21. It is pinned
  to the latest rate card, and every row carries an LHA SAR.
- `scripts/verify_source_registry.py`: all 22 gate lines pass, 18 to 21
  included (before the repair, 18 and 19 failed and 21 was skipped).

## What was not affected

W1, `refresh_map.py`, the export, `push.py` and `git push` were not run
between the incident and the repair, so nothing published or sent was
affected. Every value W1 reads is again as surveyed. The n8n edit made
in the same task is a separate change and did not touch these tables.

## The run log

Rows 499 and 501 in `pipeline_run_log` record the two accidental S14 runs as
'success' with 448 rows each. They are kept, not deleted, because they are
the record that these writes happened; this note explains them. The S3
script's write has no run-log row, and this note is its record.

## What could not be restored exactly

- The view's owner before the incident is not recorded. It is now owned by
  `n8nuser`, the role the repo's connection settings use and the role that
  owns both S14 tables, and `ucws_readonly` holds `SELECT` on it. Whether the view
  carried a comment before the incident is not recorded either. The
  2026-08-21 restoration was made from the literal, which has none, so none
  was added.
- The S14 tables are owned by `n8nuser` with the default grants, as the
  2026-08-20 rebuild left them. Any grant made by hand between then and
  today would not be in any record. None is known.

## The lesson

**A test that runs a script must never be run before the script can no
longer do harm.** "Failing first" is right for code under test. It is wrong
for a test whose subject is a live loader, because the red run is a real
run. Write the guard first, or move the scripts aside for the first red run,
or run the subprocess against a broken environment from the start. The
committed tests now do the last of these for every test that executes a retired
script (`scripts/tests/retired_sandbox.py`, used by all ten retired-script test
files, 2026-10-12). Each test copies the script alone into a fresh temporary
directory and runs it there with `python -E -s`, so `scripts/`, `_db` and the
repo `.env` are not reachable. The child environment is built from nothing:
`PATH` and `SystemRoot`, dummy `PG_USER` and `PG_PASSWORD`, and
`PG_HOST` and `PG_HOST_OVERRIDE` set to `invalid.invalid` with `PG_PORT=1`. If
a guard is ever lost, the script fails at its first import or connection and
the test goes red without reaching the database. The first repair (2026-10-11)
had made only two of seven S3/S14 tests safe (they used an invalid host; the
other three still inherited the real environment, and the broken `PYTHONPATH`
protected nothing because a script run directly has its own directory on its
path). A static test also checks that each of the three S3/S14 files has,
after its docstring, only `import sys` and the guard.

And again, from the 2026-08-21 notes: **a `DROP ... CASCADE` takes derived
objects with it silently.** Gates 18 and 19 caught both effects at once, so
they are working as intended.
