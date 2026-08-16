# Which fields are generated, and which are yours

`source_registry` is a **generated table**. It is not a place to record
findings by hand — the record goes in the generator, and the table is its
output.

This document exists because that was not obvious. On 2026-08-16 a carefully
written `revision_note` for S1 was typed straight into the table, and the next
backfill reverted it. Gate 9 caught it. The same shape had already occurred
with the README source block, which is generated from `METHODOLOGY.md`.

## The rule

**Never write to `source_registry` directly.** Edit the source of truth and
regenerate. The database now enforces this: a trigger rejects any INSERT or
UPDATE from a connection that has not declared itself a writer.

```
ERROR:  source_registry is a generated table and must not be edited directly.
```

A writer declares itself with `SET ucws.registry_writer = 'on'` on its own
connection. Only two scripts do so, and both are listed below.

## Where each field comes from

### `scripts/backfill_source_registry.py`

The source of truth for the registry's content. Edit the `SOURCES` list or
`TIER_C_FINDINGS`, then run the script.

**Regenerated for every source (12).** A hand edit here always reverts,
because the writer supplies a value — its own default if the source declares
none.

`source_code`, `source_name`, `publisher`, `acquisition_method`, `cadence`,
`geography_level`, `publish_github`, `publish_map`, `refresh_tier`, `status`,
`auth_required`, `confidential`, `metrics`

**Regenerated only for the sources that declare them (27).** These are the
dangerous ones. A hand edit to a source that declares no value **persists** —
and keeps working until somebody adds a declaration for that source, at which
point it silently disappears. It works until it doesn't, and nothing says
when it stopped.

| Field | Sources declaring it |
| --- | ---: |
| `known_gotchas` | 26 / 27 |
| `target_table` | 26 / 27 |
| `completeness_note` | 25 / 27 |
| `landing_page_url` | 23 / 27 |
| `cadence_months` | 23 / 27 |
| `caveats` | 20 / 27 |
| `detected_period_type` | 18 / 27 |
| `publication_window` | 17 / 27 |
| `series_name` | 16 / 27 |
| `api_endpoint` | 16 / 27 |
| `n8n_workflow_name` | 16 / 27 |
| `build_script_path` | 13 / 27 |
| `join_path` | 12 / 27 |
| `latest_period_loaded` | 12 / 27 |
| `revision_note` | 12 / 27 |
| `node_docs_path` | 11 / 27 |
| `revises_back_series` | 11 / 27 |
| `source_doc_path` | 9 / 27 |
| `verification_checks` | 7 / 27 |
| `expected_lag_days` | 6 / 27 |
| `ucws_lens` | 5 / 27 |
| `hss_lens` | 5 / 27 |
| `auth_env_var` | 2 / 27 |
| `superseded_by` | 1 / 27 |

`revision_note` sitting at 12 of 27 is exactly why the S1 edit reverted while a
similar edit elsewhere would have survived.

### `scripts/check_sources.py`

Two fields, written when a source is checked:

`last_check_at`, `last_seen_fingerprint`

### Not written by any script (4)

`next_expected_at`, `natural_key`, `created_at`, `updated_at`

`created_at` and `updated_at` are timestamps. `next_expected_at` and
`natural_key` are genuinely unmanaged today — which means they are the fields
most likely to be quietly declared later. Treat them as generated-in-waiting:
if they need a value, add it to the backfill.

## Other generated artefacts

| Artefact | Generated from | Regenerate with | Staleness check |
| --- | --- | --- | --- |
| The source table in `docs/README.md`, between the `generated:sources` markers | `docs/METHODOLOGY.md` plus the live database | `python scripts/sync_readme_sources.py` | `--check` fails if stale |
| `docs/SOURCE_REGISTRY_GAPS.md` | `source_registry` | `python scripts/backfill_source_registry.py` | — |
| `docs/METHODOLOGY.md` source register rows | hand-maintained; it **is** the source of truth for the register | — | gates 1 and 2 |

## How this was established

Not by reading the code. The regex read got it wrong — it reported
`revision_note` as hand-maintained when the backfill demonstrably writes it.

It was established as a data test: snapshot the table, perturb every column on
every row with a type-appropriate sentinel, run the backfill once, and see
what came back. Anything restored is generated. The snapshot was then restored
and verified exact against every data column.

The per-source counts come from the backfill's own declarations, which explain
*why* a column reverts for one source and not another.

## The gates that hold this together

- **Gate 9** — the backfill is idempotent. Catches a hand edit to a *declared*
  cell, after the fact.
- **Gate 17** — the writer-only trigger is present and enabled. Catches the
  protection itself being dropped or disabled. Proved by injection: disabling
  the trigger fails the gate and exits 1.
