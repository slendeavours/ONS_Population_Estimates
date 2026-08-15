# S23 Node 5: Verification Suite

- **Type:** Code (read-only, seven hard gates)
- **Purpose:** Prove the load against the source sheet and the publisher's own per-authority subtotals.
- **Credential:** `PG_READONLY_USER` / `PG_READONLY_PASSWORD` where configured, otherwise `PG_USER` with the session forced read-only.

## Run

```bash
python scripts/s23_rsh_stock_verify.py
```

## The gates

| # | Gate | Method |
|---|---|---|
| 1 | Row count | Counted from the source sheet by `RP_Type`, with the two excluded grains stated explicitly rather than left implicit |
| 2 | Geographic coverage | 296 of 296; shortfall enumerated by name; also reports how many carry supported housing above zero |
| 3 | Code resolution | Every `publisher_la_code` resolves, and the stored `lad24cd` equals what the lookup gives |
| 4 | Per-row provenance | `source_url`, `source_file`, `release_page_url`, `edition`, `publication_date`, `stock_date` populated on every row |
| 5 | No silent coercion | Every stock cell in the source inspected; blank proved to mean zero |
| 6 | Idempotency | Re-upsert inside a rolled-back transaction, checksum either side |
| 7 | Reconciliation | Loaded rows summed against the publisher's own 296 LA subtotal rows on all five measures |

## Gate 5 — proving blank means zero

This source publishes **no suppression or missing-data notation at all**, which
is a claim that has to be checked rather than assumed. The gate reads every
stock cell in the source sheet and requires each to be numeric or empty; any
non-numeric marker is reported and fails.

Empty is then proved to mean zero rather than unknown:

```sql
SELECT COUNT(*) FROM rsh_rp_stock_by_la WHERE stock_date = %s
  AND total_social_stock <> general_needs_self_contained
                          + general_needs_bedspaces
                          + supported_housing_and_older_people
                          + low_cost_home_ownership;
```

A blank standing for a withheld figure could not sum correctly. Zero rows
differ, so blank is zero.

That is the honest form of gate 5 for a source with no suppression: not
"skipped, not applicable", but "checked, and here is what makes the answer
safe".

## Gate 7 — a real reconciliation

The sheet carries 296 subtotal rows the publisher computed itself
(`RP_Type = 'LA'`). Every authority's loaded rows must sum to its subtotal
exactly, on `total_social_stock` and all four components. This is arithmetic
the publisher published, not a check invented for the occasion.

The publisher's **national** headline figures are weighted to impute for small
providers filing the short SDR form, so they are slightly higher than the
unweighted sums:

| Measure | Loaded (unweighted) | Published (weighted) |
|---|---:|---:|
| Total social stock | 4,533,055 | 4,546,653 |
| Supported housing + older people | 504,902 | 507,209 |

No equality is asserted against those. The difference is reported with its
reason.

## Behaviour

- **Writes:** None committed. No `conn.commit()` in the file. The idempotency
  probe rolls back in a `finally` block, and the upsert SQL is imported from
  the build module rather than copied.
- **Exit code:** 0 if all seven pass, 1 otherwise.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`, session read-only. HTTPS to
GOV.UK to re-resolve the edition.

## Verified Output

7 of 7 gates passed, 2026-08-14.

- Gate 1: 10,171 provider rows in file, 10,171 loaded; 296 subtotal and 9
  regional rows excluded by design.
- Gate 2: 296/296 authorities; 295/296 carry supported housing above zero.
- Gate 3: 0 unresolved, 0 mis-resolved.
- Gate 4: 0 rows missing provenance.
- Gate 5: 50,855 stock cells inspected, 0 non-numeric markers, 0 blanks,
  0 component-sum failures, 0 NULLs.
- Gate 6: checksum `5dfeda4cf5dc2455a18004904052a63f` before and after;
  0 cells differing.
- Gate 7: 296/296 authorities reconcile exactly to the publisher's subtotals,
  0 disagreements.
