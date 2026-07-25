# Node 8 — Verification Suite

## Type
Halting verification, 13 checks

## Credential
`exempt_pipeline` as `PG_USER` from `.env`.

## Purpose
Prove the load is correct before committing. Any failure rolls back the whole
transaction and exits non-zero. Partial or suspect data is never loaded and
flagged afterwards.

## Checks

| # | Check | What it proves |
|---|---|---|
| 1 | Coverage | Every published English name resolved; LA count reported, not gated |
| 2 | Referential integrity | No orphans, no non-English codes in the England table |
| 3 | Anchor set | Loaded totals match the published headline figures |
| 4 | Load fidelity | Parsed dataframe equals what landed, per period, tolerance 0 |
| 5 | Reasonableness | No negatives, no implausible maximum, published distribution shape reproduced |
| 6 | Suppression handling | Suppressed rows are NULL, never coerced to 0 |
| 7 | Idempotency | A second identical load changes nothing |
| 8a | England vs Asy_D09 | England total matches an independent source, per period |
| 8b | Unallocated vs Asy_D09 | Unallocated total matches an independent source, per period |
| 9 | Cross-source | Reg_02 and Asy_D11 agree per LA |
| 10 | Reg_02 internal | Pathway columns reconcile to the published total |
| 11 | Collision log | Aggregation collisions are recorded and bounded |
| 12 | View integrity | View breakdown columns sum to the total |

## Notes on individual checks

**Check 3 — anchor aggregation.** Established empirically at build time by
testing three candidate aggregations against the published anchors. The
published headline is **all support types and all accommodation types,
unfiltered**. Excluding subsistence-only misses Birmingham by 173; restricting
to Section 95 misses by 94. Glasgow City and Belfast are checked against
`asylum_support_non_england`, which tests that table too.

**Check 4 replaced the original internal-reconciliation check**, which was
tautological: every row carries both a support type and an accommodation type,
so summing across each dimension necessarily returns the same total. It could
not fail. Load fidelity — parsed versus landed, per period, tolerance 0 — tests
something that can.

**Check 5 — distribution shape.** Measured on the Home Office's own denominator
of 361 UK local authorities, not England-only and not `(period, LA)` cells.

Stated on the **at or above 100** side, because that quantity is computed from
the source with no adjustment: `344 present - 164 under 100 = 180`. The
published figure of 181 under 100 out of 361 implies 180 at or above.
Difference 0. Stating it on the under-100 side would require adding the absent
LAs back in to reach 181, which reads as fitting the result to the target even
though the arithmetic is sound.

Confirming decomposition: `361 = 296 England + 32 Scotland + 22 Wales + 11
Northern Ireland`; `344 present = 286 England + 58 non-England`; `17 absent =
10 England + 7 non-England`. The denominator is the full UK LAD set, which is
why absent LAs fall in the under-100 bucket — making this an independent
confirmation that an absent LA means "not published" rather than zero.

The England maximum is reported with its LA and period rather than only
pass/fail, because the published UK maximum (Glasgow City, 3,870) is Scottish
and can never appear in `la_asylum_support`.

**Check 11 — the halt threshold applies to duplicate keys only.** Two very
different events collapse onto the natural key. Forward-resolving abolished
districts onto a successor unitary merges rows by design — 34 of them, which is
the approved cascade working. Only rows sharing the *same* LAD code are a
defect. Counting merges against the threshold would halt every future load for a
correct reason.

**Check 12 — the `not_stated` column is load-bearing.** Without
`accommodation_not_stated` in the view, the named accommodation columns fall
short of `total_supported` for the 20 periods where Section 98 has no
accommodation type. Both the accommodation and support-type identities are
enforced at tolerance 0.

## Behaviour
- Every check is halting. `run_all` returns results; the caller rolls back and
  exits 1 if any failed.
- Asy_D09 is read at verify time and **never loaded into a table**. It exists
  purely to make Checks 8a and 8b non-circular — reconciling the file against
  itself would prove nothing.
- Writes `docs/s6_source_anomalies.md` on every run, including the row-count
  reconciliation chain and per-key collision detail.

## Connection
Input: populated tables from Node 7, `s6_d09.xlsx` from Node 2, parsed datasets
and collision list from Nodes 4 and 5.
Output: pass/fail results controlling commit or rollback.

## Verified Output

```
VERIFICATION SUMMARY: 13 / 13 passed
```

- Check 3: Birmingham 2,142 · Liverpool 2,053 · Coventry 1,712 · Glasgow 3,870 ·
  Belfast 1,607 · UK 97,519 — all exact
- Check 5: England max 3,488 (Birmingham, 2023-09-30); 181 of 361 under 100 vs
  ~181 published, difference 0
- Checks 8a/8b: 33/33 periods exact, zero divergence
- Check 9: 286/286 English LAs exact, post-resolution
- Check 11: 1 duplicate key of 5 permitted, 34 merges recorded
- Check 12: 7,868 view rows, tolerance 0 on both identities
- Verified 2026-07-25 (initial build)

## Series breaks
Consumers of `vw_la_asylum_support_totals` should read `asylum_series_breaks`
before plotting any England series. Two breaks are recorded: Section 98 gaining
LA geography at 2022-12-31, and Subsistence Only losing it from 2023-12-31 to
2024-12-31. **The first fully comparable period for England totals is
2025-03-31.**
