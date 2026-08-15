# S1b Node 5: Verification Suite

- **Type:** Code (read-only, seven hard gates)
- **Purpose:** Prove the load against the source files and the publisher's own totals. Any failure aborts and nothing is published.
- **Credential:** `PG_READONLY_USER` / `PG_READONLY_PASSWORD` where configured, otherwise `PG_USER` with the session forced read-only. Via `scripts/_db.py`.

## Run

```bash
python scripts/s1b_support_needs_verify.py
```

## The gates

| # | Gate | Method |
|---|---|---|
| 1 | Row count | Expected count derived from the source files as (LA rows × mapped columns) per quarter, using the same parser the load used — not from the table |
| 2 | Geographic coverage | 296 of 296 per quarter; any shortfall enumerated by authority name from `la_code_lookup`, not summarised as a percentage |
| 3 | Code resolution | Every `publisher_la_code` resolves through `la_code_lookup`, and the stored `lad24cd` equals what the lookup gives; recodes listed individually |
| 4 | Per-row provenance | `source_url`, `source_edition` and `release_page_url` populated on every row; editions reported per quarter |
| 5 | Suppression | No row carries both a value and a flag; none carries neither; no flagged row holds a zero |
| 6 | Idempotency | Re-upsert every row inside a transaction, compare an md5 content checksum either side, always roll back |
| 7 | Reconciliation | LA rows summed against the publisher's England row |

## Gate 5 — the query that would catch a silent coercion

```sql
SELECT
  COUNT(*) FILTER (WHERE value IS NOT NULL AND value_flag IS NOT NULL),
  COUNT(*) FILTER (WHERE value IS NULL     AND value_flag IS NULL),
  COUNT(*) FILTER (WHERE value_flag = 'suppressed'),
  COUNT(*) FILTER (WHERE value_flag = 'missing'),
  COUNT(*) FILTER (WHERE value_flag = 'not_applicable'),
  COUNT(*) FILTER (WHERE value = 0)
FROM la_homelessness_support_needs;

SELECT COUNT(*) FROM la_homelessness_support_needs
WHERE value_flag IS NOT NULL AND value = 0;
```

The last query is the one that matters: a suppressed cell that became a zero
would appear there. It must return 0.

## Gate 6 — writing without committing

```python
probe = get_conn()
pcur = probe.cursor()
try:
    before = checksum(pcur)
    ...re-run the real upsert for every quarter...
    after = checksum(pcur)
finally:
    probe.rollback()
```

The upsert SQL is **imported from the build module**, not copied, so there is
one definition of how a row is written and the test cannot drift from the
thing it tests. The `finally` block guarantees the rollback.

The self-diff uses `IS DISTINCT FROM`, so NULL against NULL counts as equal
and NULL against zero does not — suppression handling is exactly what has to
reproduce.

This shape exists because `s18_pipr_verify.py` once tested idempotency by
re-upserting and committing, and a run that fell back to a stale edition
rewrote 71,442 rows. Gate 12 of `verify_source_registry.py` enforces the rule
across all suites.

## Gate 7 — what is asserted, and what is not

A3 publishes an England total, but it is weighted to impute for non-submitting
authorities and rounded to the nearest 10, while the LA rows are unrounded
with NULLs where suppressed. **They are not supposed to be equal, so equality
is not asserted.** The falsifiable statement is that the LA sum never exceeds
the published England figure, and the gap is reported with the count of
authorities not reporting.

Asserting equality would have been inventing a check the publisher does not
support.

## Behaviour

- **Writes:** None committed, ever. No `conn.commit()` appears in the file.
- **Re-run safety:** Fully safe. Read-only plus one rolled-back probe.
- **Exit code:** 0 if all seven pass, 1 otherwise.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`, session read-only. HTTPS to
GOV.UK for gates 1, 6 and 7, which re-resolve the editions rather than trust a
cached list.

## Verified Output

7 of 7 gates passed, 2026-08-14.

- Gate 1: 101,232 expected, 101,232 loaded.
- Gate 2: 296/296 in all eleven quarters.
- Gate 3: 0 unresolved; recodes `E08000038 → E08000016`, `E08000039 → E08000019`.
- Gate 4: 0 rows missing provenance.
- Gate 5: 0 both, 0 neither, 0 coerced; 772 suppressed, 1,620 missing.
- Gate 6: checksum `87aca46d68fb8d4e64c6c93910c40be5` before and after; 0 cells differing.
- Gate 7: gap 0.00%–2.77%, inside the publisher's stated 2.0%–3.2% imputation.
