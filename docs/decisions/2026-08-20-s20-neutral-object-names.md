# S20 database objects renamed to neutral names

**Date:** 2026-08-20
**Status:** Closed

## The problem

Source 20's `completeness_note` has always recorded that `target_table`,
`build_script_path` and `source_doc_path` are deliberately null, because the
counterparty is named by its own table and file names. Recording them would make
`source_registry` an artefact that discloses the counterparty without ever
naming it.

The rule was sound but only half enforced. The tables themselves still carried
the counterparty's name, so anything that queried them carried it too:

- `scripts/yada_run2_build.py`, 11 references
- `scripts/yada_run2_deliverables.py`, 1 reference

Both are ordinary pipeline scripts with no rate figures in them, and both were
untrackable purely because of what the objects were called. Two prompt files
mentioned the counterparty in passing for the same reason.

This surfaced on 2026-08-20 when a second checkout of this repository was folded
into this one. The move brought the rate card files into the working tree, and
the `.gitignore` rule written to exclude them matched on the counterparty's
name, putting it into a tracked file. That rule was itself the disclosure the
standing note exists to prevent. Nothing had been pushed.

## The decision

Rename the objects so the rule is enforced by the schema rather than by
remembering it. The names now in use:

| Object | Name |
| --- | --- |
| Rate table | `commercial_rate_card` |
| Area to authority mapping | `commercial_rate_area_mapping` |
| Triangulation view, rate columns | `ratecard_1bed_weekly` … `ratecard_6bed_weekly` |
| Triangulation view, area count | `ratecard_area_count` |
| `yada_results`, area count | `ratecard_area_count` |

Local files carrying the rate data are named `s20_ratecard_*`, and the ignore
rule matches that prefix. The prefix names the source number, which is already
public in the registry as "Commercial rate card (private)", so it discloses
nothing.

## Migration

Renames plus one view rebuild. No data change.

The migration script is held locally as `s20_ratecard_neutralise_object_names.py`
and is not tracked, for the same reason the scripts above were not: it has to
name the old objects in order to rename them, so tracking it would reintroduce
exactly what this record closes. It is idempotent and exits cleanly once the new
names are in place, so it is safe to re-run against a checkout that has already
been migrated.

It captures row counts and full view output before and after and asserts they
are identical. Verified on 2026-08-20: 328 rate rows, 177 mapping rows and 155
view rows unchanged either side, and zero tables or columns left carrying the
counterparty's name.

## Consequence

The two prompt files are reworded to refer to the S20 commercial rate card and
are tracked.

The stated consequence for the YADA scripts did not happen, and should not have
been written as though it had. They are not tracked here and are not going to
be: the repo-scope decision taken the same day (`2026-08-20-repo-scope.md`) puts
business analysis on the home drive, so `yada_run2_build.py` and
`yada_run2_deliverables.py` correctly live in `analysis/yada/` outside this
checkout. Two decisions made on one day pulled in opposite directions and this
record recorded only its own half. What the rename actually bought them is real
but smaller: they no longer carry the counterparty's name, so their object
references can be read and reviewed. Verified 2026-08-21 — both use the neutral
names throughout and neither mentions the counterparty.

The rate figures themselves remain in `exempt_pipeline` only, and the source
files remain untracked. That has not changed and must not.

## What to watch

A future rate card build must not reintroduce the name into an object, a column
or a filename. Two cheap checks:

- `git grep -i` for the counterparty across tracked files returns nothing
- the same query against `information_schema.tables` and
  `information_schema.columns` returns no rows

Both were clean when this record was written.

## Addendum, 2026-08-21: the check was too narrow

Reopened and closed the same day. The two checks above are correct as far as
they go, but `information_schema.tables` and `information_schema.columns` only
see table names and column names. Three objects carrying the counterparty's name
survived the migration because nothing looked at them:

- the `commercial_rate_card` primary key, still carrying its pre-migration name
- the `commercial_rate_area_mapping` primary key, likewise
- the `source` column default on `commercial_rate_card`, which wrote the
  counterparty's name into every row inserted without an explicit value

The old names are not reproduced here. Writing them down is the disclosure, and
this record was drafted once with them spelled out before the scan below caught
it.

A primary key name is not cosmetic here: `\d commercial_rate_card` prints it, so
the one command anyone runs to understand the table disclosed the counterparty.
The column default is worse, because it kept writing the name into new data
rather than merely displaying it.

Renamed to `commercial_rate_card_pkey` and `commercial_rate_area_mapping_pkey`,
and the default set to a neutral string. Verified after: 328 rate rows, 177
mapping rows, both primary keys intact, both tables still owned by
`pipeline_user`, and the Dartford row through the triangulation view unchanged.

The watch list above is replaced by `scripts/schema_name_scan.py`, which covers
every object class that can carry a name — tables, columns, column defaults,
constraints, indexes, view names and definitions, materialized views, sequences,
comments, function bodies, triggers and roles — and the tracked tree in the same
run. Checking two object classes and calling the schema clean is what let this
sit for a day; a scan retyped from memory has a different definition every time
it is run.

The term is never hardcoded, because a tracked file naming the counterparty is
the disclosure itself. Pass it in:

    python scripts/schema_name_scan.py <term> [<term> ...]
    python scripts/schema_name_scan.py --terms-file .name_scan_terms

Exit 0 clean, 1 on any hit. Verified on 2026-08-21 against a positive control
before being trusted on a clean result: a term known to be present returned
hits across five object classes, and the counterparty term returned none.

Two things this addendum does not change, both deliberate:

- Existing `source` values in `commercial_rate_card` still name the counterparty
  on rows loaded before today. That is provenance inside a private table, it is
  not reachable from the triangulation view or `staging_la_signals`, and
  rewriting historical provenance is a worse outcome than keeping it.
- The original loader still refers to the pre-migration object names throughout
  and would create empty, wrongly-named tables if re-run. Recorded here so it is
  not discovered during a card load; fixing it is separate work.
