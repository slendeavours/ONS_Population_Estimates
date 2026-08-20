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

Both YADA scripts are tracked from this point. The two prompt files are reworded
to refer to the S20 commercial rate card and are tracked.

The rate figures themselves remain in `exempt_pipeline` only, and the source
files remain untracked. That has not changed and must not.

## What to watch

A future rate card build must not reintroduce the name into an object, a column
or a filename. Two cheap checks:

- `git grep -i` for the counterparty across tracked files returns nothing
- the same query against `information_schema.tables` and
  `information_schema.columns` returns no rows

Both were clean when this record was written.
