# Node 8 — Hard Gates

## Type

Postgres — Execute Query, run inside the load transaction. `scripts/s22_run.py`, function `run`.

## Purpose

Prove the load before it is committed. Any failure rolls the whole transaction back so the database is left in its pre-load state.

## Credential

Postgres `exempt_pipeline`.

## Query / Code / URL (full content)

Gate 1 — row count:

```sql
SELECT COUNT(*) FROM la_council_taxbase_empties WHERE taxbase_year = %s;
```

Gate 2 — orphan codes:

```sql
SELECT COUNT(*) FROM la_council_taxbase_empties e
 WHERE e.taxbase_year = %s
   AND NOT EXISTS (SELECT 1 FROM la_boundaries bd
                    WHERE bd.lad24cd = e.lad24cd);
```

Gate 3 — national reconciliation, one execution per measure:

```sql
SELECT SUM(<column>) FROM la_council_taxbase_empties WHERE taxbase_year = %s;
```

Gate 5 — value sanity:

```sql
SELECT COUNT(*) FROM la_council_taxbase_empties
 WHERE taxbase_year = %s
   AND (total_dwellings < 0 OR empty_under_6_months < 0
        OR empty_6_months_plus < 0 OR empty_total < 0
        OR empty_homes_premium_count < 0 OR second_homes < 0
        OR unoccupied_exemptions_total < 0);

SELECT COUNT(*) FROM v_la_empty_homes_rates
 WHERE lte_rate_pct < 0 OR lte_rate_pct > 100;

SELECT COUNT(*) FROM la_vacant_dwellings_615
 WHERE vacant_dwellings < 0 OR long_term_vacant_dwellings < 0;
```

Rollback:

```python
    if any(not g["ok"] for g in gates):
        conn.rollback()
        conn.close()
        sys.exit("HALT: hard gate failed — transaction rolled back, database "
                 "left in its pre-load state. See the gate output above.")
    conn.commit()
```

Gate 4 — idempotency, after the commit:

```python
    before = snapshot()
    b.load_all(conn, records, class_rows, rows615, src_a["technical_notes_url"])
    conn.commit()
    after = snapshot()
    same = before == after
```

## Query Parameters

| Parameter | Source |
|---|---|
| `taxbase_year` | release year resolved in Node 1 |
| reconciliation targets | the national headline figures printed on the MHCLG release page, held in `RELEASE_PAGE_TARGETS` |

## Logic (step by step)

1. **Gate 1** — exactly 296 rows for the latest taxbase year.
2. **Gate 2** — every `lad24cd` exists in `la_boundaries`. Zero orphans.
3. **Gate 3** — each national sum within 0.5% of the release-page figure. Five measures have a figure printed on the release page. Two do not: `empty_6_months_plus` and `empty_under_6_months` are **NOT FOUND on the release page**, which is different from unchecked, and for those two the target is the publisher's own England total row in the same workbook. Each row of the reconciliation table records which target source was used.
4. **Gate 5** — no negative counts in either table, and no `lte_rate_pct` outside 0 to 100.
5. Roll back and halt if any of 1, 2, 3 or 5 failed. Otherwise commit.
6. **Gate 4** — snapshot row counts and value sums, run the entire load a second time, snapshot again, and compare. Halt if anything moved.
7. **Gate 6** runs later, in Node 9, after the W1 re-run.

## Behaviour

Gates 1, 2, 3 and 5 run against uncommitted data inside the load transaction, so a failure genuinely leaves the pre-load state rather than requiring a cleanup. Gate 4 has to run after the commit, because it is testing what a second run does to a committed state.

## Connection

- Input: Node 7 (Create Rates View)
- Output: Node 9 (Wire W1 and Re-run)

## Verified Output

2026-08-13. All five in-transaction gates passed on the second attempt; the first attempt failed gate 2 with two orphan codes (Barnsley E08000038 and Sheffield E08000039), rolled back cleanly, and the recode resolution in Node 2 was added in response.

| Measure | Target | Target source | Loaded | Deviation |
|---|---|---|---|---|
| total dwellings | 25,800,000 | release page | 25,817,220 | 0.0667% |
| empty dwellings (all) | 542,000 | release page | 542,260 | 0.0480% |
| empty homes charged a premium | 153,000 | release page | 152,928 | 0.0471% |
| second homes | 268,000 | release page | 267,894 | 0.0396% |
| unoccupied exempt dwellings | 212,000 | release page | 212,004 | 0.0019% |
| empty 6 months plus | 309,889 | workbook England row | 309,889 | 0.0000% |
| empty under 6 months | 232,371 | workbook England row | 232,371 | 0.0000% |

Gate 4: row counts and value sums identical after the second full load.
