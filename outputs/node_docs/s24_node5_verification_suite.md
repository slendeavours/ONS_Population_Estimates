# S24 Node 5: Verification Suite

- **Type:** Code (read-only, seven hard gates)
- **Purpose:** Prove the load against the source files, and enforce the no-geography design decision so it cannot be undone by accident.
- **Credential:** `PG_READONLY_USER` / `PG_READONLY_PASSWORD` where configured, otherwise `PG_USER` with the session forced read-only.

## Run

```bash
python scripts/s24_rsh_register_verify.py
```

## Two gates do not apply, and say so

RSH publishes no provider addresses, so there is no local authority geography
to cover and no publisher geographic code to resolve through
`la_code_lookup`. Gates 2 and 3 are therefore **not applicable** — which is a
different statement from "not checked", and the suite makes the difference
visible rather than silently skipping them. Where a gate does not apply, the
nearest meaningful check runs in its place and is labelled as the substitute
it is.

## The gates

| # | Gate | Method |
|---|---|---|
| 1 | Row counts | All three tables counted against their source sheets |
| 2 | Geography — **not applicable**, and the absence enforced | Asserts 0 geography columns on the S24 tables and 0 S24 columns in `staging_la_signals` |
| 3 | Identifier integrity — **substitute** for code resolution | Every judgement and notice must name a provider on the register snapshot; orphans listed individually |
| 4 | Per-row provenance | `source_url`, `source_file`, `release_page_url` on every row of all three tables |
| 5 | Unassessed distinguishable from graded | `-` markers counted in source, NULLs counted in table, must match; 0 empty strings, 0 literal dashes |
| 6 | Idempotency | Re-upsert all three tables inside a rolled-back transaction, three checksums either side |
| 7 | Reconciliation | Register size against the last published provider count, as context only |

## Gate 2 — enforcing a design decision

```sql
SELECT COUNT(*) FROM information_schema.columns
WHERE table_name IN ('rsh_registered_providers','rsh_regulatory_judgements',
                     'rsh_enforcement_notices')
  AND column_name IN ('lad24cd','la_code','publisher_la_code','region');

SELECT COUNT(*) FROM information_schema.columns
WHERE table_name = 'staging_la_signals'
  AND (column_name LIKE 'rsh%' OR column_name LIKE '%registered_provider%');
```

Both must return 0. This is what stops S24 being wired into Workflow 1 later
by reflex: a provider's registered office is not where its stock is, and
manufacturing a geography from one would put a confident wrong number on a
map. For provider stock **by** authority, S23 exists and has a real geography.

## Gate 3 — why an orphan is a finding, not an error

A judgement whose registration number is absent from the current snapshot
means the provider was de-registered after the judgement was published. That
is exactly the event this source exists to surface, so orphans are listed
individually by code and name rather than counted, and do not fail the gate.

## Gate 7 — no manufactured equality

RSH publishes no headline count alongside the monthly snapshot, so there is no
figure to assert equality against. The nearest published count is Table 1.19
of the stock release — providers registered at 31 March 2025 — which is a
different date and a different basis. It is reported as context with the
difference stated, and the gate passes on plausibility rather than on an
equality the publisher does not support.

## Behaviour

- **Writes:** None committed. No `conn.commit()` in the file. The idempotency
  probe rolls back in a `finally` block; the upsert SQL and the row builders
  are imported from the build module rather than copied.
- **Exit code:** 0 if all seven pass, 1 otherwise.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`, session read-only. HTTPS to
GOV.UK to re-resolve both publications.

## Verified Output

7 of 7 gates passed, 2026-08-14.

- Gate 1: 1,579 / 1,579 providers; 308 / 308 judgements; 2 / 2 notices.
- Gate 2: 0 geography columns; 0 S24 columns in `staging_la_signals`.
- Gate 3: 0 orphan judgements, 0 orphan notices.
- Gate 4: 0 rows missing provenance across all three tables.
- Gate 5: 600 source `-` markers, 600 NULLs stored, 0 empty strings, 0 literal
  dashes. 108 ungraded rows also carry no change description — the local
  authority providers, which receive consumer grades only.
- Gate 6: all three checksums identical either side; 0 grade cells differing.
- Gate 7: 1,579 at 2026-07-24 against 1,581 published at 2025-03-31, a
  difference of 2 across sixteen months.
