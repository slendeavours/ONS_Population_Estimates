# S9 reproduction tables dropped (R3)

Status: done 2026-10-09. Work plan item R3.

`nhs_drd_discharge_delays_repro` and `nhs_mh_crfd_repro` were staging tables written by the old S9a and S9b
build scripts' `--reproduce` mode (2026-08-14), to prove that a rebuild matched the live table cell for cell.
S9a and S9b now load through editions ([S9a](2026-10-09-s9a-editions-first-load.md),
[S9b](2026-10-09-s9b-editions-first-load.md)), the old scripts are retired to `scripts/historical/`, and the
editions tables and their `verify` scripts are the reproducibility evidence. The tables were frozen at the
2026-08-14 live content, which no longer matches live (new months and edition 2 values).

## Checks before the drop (read-only, 2026-10-09)

- `pg_depend`: the only objects depending on either table are its own column defaults and primary key (plus
  its row type and TOAST table). No view, rule, trigger, function or foreign key refers to either table;
  no view or function text contains the name.
- grep of `scripts/`, `scripts/tests/`, `docs/` and `sql/`: no live reader. The remaining mentions are the
  retired scripts in `scripts/historical/`, `scripts/fix_lad24cd_resolve_to_canonical.py` (now skips a table
  that does not exist), and dated decision notes, which stay as written.
- Contents at the time of the drop (row hash = md5 over the md5 of each row's columns except `loaded_at`,
  ordered by key):

| Table | Key | Rows | Row hash |
|---|---|---|---|
| `nhs_drd_discharge_delays_repro` | `reporting_period, utla_code` | 3,978 | 6a8b83be3a4b802855d4b88f563eee5d |
| `nhs_mh_crfd_repro` | `reporting_period, lad24cd, measure_id` | 11,248 | d4bb7981843caad17bc067c5c212123a |

## The drop

One transaction, `DROP TABLE` on both, no `CASCADE`, committed. Neither table exists afterwards.
