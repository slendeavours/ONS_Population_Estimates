# S24 Node 6: Log the Run

> **Superseded 2026-10-11.** The old S24 scripts this note describes (`s24_rsh_register_build.py`, `s24_rsh_register_verify.py`) loaded the register and judgements twice (run-log ids 96 and 101) and are retired to `scripts/historical/`, where they stop with a RETIRED message. S24 is now loaded by `scripts/s24_rsh_register_editions.py`, which reads each file's identity from the file and the publisher's page and keeps the register per snapshot and the judgements and notices per published edition, with the reason for every NULL (`rsh_registered_providers_editions`, `rsh_regulatory_judgements_editions`, `rsh_enforcement_notices_editions`). The source note is `docs/s24_rsh_register_source.md`. The text below is left as written and describes the old method.

- **Type:** Postgres INSERT
- **Purpose:** Record the load in `pipeline_run_log` with `source_code` populated so `vw_source_due` resolves S24 by code.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`.

## Query

```sql
INSERT INTO pipeline_run_log
    (run_id, source_number, source_code, agent_name, status,
     rows_written, notes, started_at, completed_at)
VALUES (gen_random_uuid(), %s, %s, %s, 'success', %s, %s, now(), now());
```

## Query Parameters

| Parameter | Value |
|---|---|
| `source_number` | `24` |
| `source_code` | `24` |
| `agent_name` | `Source 24 - RSH register and judgements` (39 chars; the column is `varchar(50)`) |
| `status` | `success` |
| `rows_written` | Register + judgement + notice rows combined |
| `notes` | Register snapshot date, judgements edition date, and the standing note that this source is entity-level with no LA geography and is deliberately not wired into `staging_la_signals` |

## Why the no-W1 decision is repeated in the run log

It appears in the source documentation, the registry `caveats`, the node
documentation and here. That is deliberate. A design decision that depends on
nobody later noticing "S24 has no signals column, that looks like an
oversight" has to be legible from wherever someone lands, and the run log is
one of the places a reader lands when asking what a source did.

## Behaviour

- **Conflict handling:** None — append-only, one row per run.
- **Re-run safety:** A re-run appends a second row, which is correct.
- **Transaction:** Written inside the same transaction as all three table
  loads, so no log row can claim a load that did not land.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

One row logged: `source_code = '24'`, `rows_written = 1889`
(1,579 + 308 + 2), status `success`, notes recording register snapshot
2026-07-24 and judgements edition 2026-08-12.

Verified 2026-08-14.
