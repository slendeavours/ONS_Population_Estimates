# Node 9 — Wire W1 and Re-run

## Type

Postgres — Execute Query, plus a write to the n8n workflow store. `scripts/s22_w1_wire.py`.

## Purpose

Add the five S22 columns to `staging_la_signals`, revise W1 node 5 in the stored workflow, and re-run Workflow 1 end to end.

## Credential

Postgres `exempt_pipeline` for the migration and the run; Postgres `n8ndb` for the workflow update.

## Query / Code / URL (full content)

Additive migration, one execution per column:

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'staging_la_signals'
                      AND column_name = %(col)s) THEN
        EXECUTE format('ALTER TABLE staging_la_signals ADD COLUMN %I %s',
                       %(col)s, %(type)s);
    END IF;
END $$;
```

| Column | Type |
|---|---|
| `ctb_total_dwellings` | INTEGER |
| `ctb_empty_6m_plus` | INTEGER |
| `ctb_empty_homes_premium` | INTEGER |
| `ctb_second_homes` | INTEGER |
| `ctb_lte_rate_pct` | NUMERIC(6,2) |

The new fragments in node 5. Everything already in the node is retained:

```sql
    ctb.total_dwellings           AS ctb_total_dwellings,
    ctb.empty_6_months_plus       AS ctb_empty_6m_plus,
    ctb.empty_homes_premium_count AS ctb_empty_homes_premium,
    ctb.second_homes              AS ctb_second_homes,
    ctbr.lte_rate_pct             AS ctb_lte_rate_pct,
```

```sql
LEFT JOIN la_council_taxbase_empties ctb
    ON ctb.lad24cd = b.lad24cd
    AND ctb.taxbase_year = (SELECT MAX(taxbase_year)
                              FROM la_council_taxbase_empties)
LEFT JOIN v_la_empty_homes_rates ctbr
    ON ctbr.lad24cd = b.lad24cd
```

```sql
    ctb_total_dwellings     = EXCLUDED.ctb_total_dwellings,
    ctb_empty_6m_plus       = EXCLUDED.ctb_empty_6m_plus,
    ctb_empty_homes_premium = EXCLUDED.ctb_empty_homes_premium,
    ctb_second_homes        = EXCLUDED.ctb_second_homes,
    ctb_lte_rate_pct        = EXCLUDED.ctb_lte_rate_pct,
```

The full revised node 5 query is `build_reports/s22_w1_node5_revised.sql`, published as `docs/s22_w1_node5_revised.md`.

Placeholder translation for executing the stored node queries from Python:

```python
def _pg(sql):
    """n8n Postgres node placeholders -> psycopg2 named parameters."""
    return sql.replace("%", "%%").replace("$1", "%(run_id)s")
```

## Query Parameters

| Parameter | Source | Notes |
|---|---|---|
| `run_id` | `staging_runs` `RETURNING run_id` from the Create Run node | the node's `queryReplacement` is `{{ $('Create Run').first().json.run_id }}`, which arrives as `$1` |
| `col`, `type` | the five column definitions above | passed to `format('%I %s')`, so the identifier is quoted by Postgres rather than interpolated by the client |

## Logic (step by step)

1. Run the additive migration. `IF NOT EXISTS` per column; the table is never dropped or recreated. Confirm all five columns are present or halt.
2. Read the stored W1 workflow from `n8ndb`, back the current node 5 JSON up to `build_reports/s22_w1_node5_backup.json`, write the revised SQL into node 5, and read it back to confirm it matches the file byte for byte.
3. Align the `staging_runs` sequence. Runs 10 and 11 had been written into `staging_la_signals` by direct SQL with no matching `staging_runs` row, so the sequence trailed the data and the next `nextval()` would have collided with an existing run. The sequence is advanced past the highest run id present in either table and the discrepancy is recorded as a warning.
4. Execute the workflow's node queries in connection order against `exempt_pipeline`: Create Run, National Aggregates, LA Signals (revised), Tenant Type Rankings, Section 3 Top 3 LAs, Mark Run Complete.
5. **Gate 6** — confirm the new run holds 296 rows with all five S22 columns populated for all 296. Halt otherwise.

The revised node also folds in the S9 and S19 columns. Those were in the database but absent from the stored node, because runs 10 and 11 were applied by direct SQL; leaving them out would have silently dropped `drd_*`, `crfd_days` and `pip_*` from the next run.

No tenant type ranking is added. Empty homes is a supply-side indicator, not a cohort, so node 6 is untouched.

## Behaviour

The migration is idempotent. The node 5 write is compared before and after and skipped if already current. Re-running produces a new `run_id` and a new set of 296 rows; existing runs are untouched.

The n8n REST API requires an interactive login this build does not have, so the workflow is updated in n8n's own database, which is where n8n reads it from at execution time.

## Connection

- Input: Node 8 (Hard Gates)
- Output: Node 10 (Verification Suite and Log Run)

## Verified Output

2026-08-13. Five columns added to `staging_la_signals`. Node 5 updated in `n8ndb` and confirmed on readback. Sequence advanced from 10 to 11. W1 run 12: National Aggregates 1 row, LA Signals 296 rows, Tenant Type Rankings 1,285 rows, Section 3 Top 3 LAs 296 rows, run marked complete.

Gate 6 passed: 296 rows, `ctb_total_dwellings` 296/296, `ctb_empty_6m_plus` 296/296, `ctb_empty_homes_premium` 296/296, `ctb_second_homes` 296/296, `ctb_lte_rate_pct` 296/296. Every pre-existing column reproduces run 11's coverage exactly, including the known partials: `care_leavers_semi_indep` 132/296, `crfd_days` 205/296, `marac_cases` 282/296.
