# S6 source anomalies

Anomalies found in the Home Office source files during the S6 load, and the aggregation decisions taken in response. Written by `s6_asylum_verify.py` on every run.

## Row count reconciliation

```
 24,639  rows in scope (Asy_D11, 2018-01-01 forward)
-    49  absorbed by SUM aggregation across 35 collision keys
         (34 reorganisation merges, 1 duplicate key)
=24,590  rows landed across la_asylum_support, la_asylum_support_unallocated and asylum_support_non_england
```

People totals are unaffected: aggregation preserves `SUM`. The per-key row counts below make the 49 derivable rather than asserted.

## How collisions arise

Several source rows can collapse onto one `(period_ending, lad24cd, support_type, accommodation_type)` key after geography resolution and accommodation-type normalisation. They are summed before upsert, because `ON CONFLICT DO UPDATE` would otherwise keep one row and silently discard the rest. Two distinct causes:

## Duplicate keys (source defects)

Source rows carrying the **same** LAD code on the same natural key. These count against the halt threshold.

### `2023-03-31` · `E08000031` · `Section 98` · `Dispersal Accommodation`

| Date | Support | Region | LA | LAD Code | Accommodation | People |
|---|---|---|---|---|---|---|
| 2023-03-31 | Section 98 | North West | Wolverhampton | E08000031 | Dispersal Accommodation | 4 |
| 2023-03-31 | Section 98 | West Midlands | Wolverhampton | E08000031 | Dispersal Accommodation | 12 |

Summed to **16**.

## Reorganisation merges (expected)

Source rows carrying **different** LAD codes that resolve forward onto one successor unitary. This is the geography cascade working as designed, not a defect, so these do not count against the halt threshold.

34 merge(s) across 4 successor unitaries, absorbing 48 rows.

| Successor | Merges | Rows absorbed | Predecessor codes seen |
|---|---:|---:|---|
| E06000063 | 4 | 4 | E07000028, E07000029 |
| E06000064 | 9 | 13 | E07000027, E07000030, E07000031 |
| E06000065 | 9 | 10 | E07000163, E07000164, E07000165, E07000168, E07000169 |
| E06000066 | 12 | 21 | E07000187, E07000188, E07000189, E07000246 |

### Per-key detail

| Period | Successor | Support | Accommodation | Source rows | Absorbed | Summed to |
|---|---|---|---|---:|---:|---:|
| 2021-06-30 | E06000063 | Section 95 | Subsistence Only | 2 | 1 | 2 |
| 2021-06-30 | E06000064 | Section 95 | Subsistence Only | 2 | 1 | 2 |
| 2021-06-30 | E06000066 | Section 95 | Subsistence Only | 2 | 1 | 5 |
| 2021-09-30 | E06000063 | Section 95 | Subsistence Only | 2 | 1 | 3 |
| 2021-09-30 | E06000064 | Section 95 | Subsistence Only | 3 | 2 | 4 |
| 2021-09-30 | E06000066 | Section 95 | Subsistence Only | 2 | 1 | 5 |
| 2021-12-31 | E06000064 | Section 95 | Subsistence Only | 3 | 2 | 4 |
| 2021-12-31 | E06000065 | Section 95 | Subsistence Only | 2 | 1 | 3 |
| 2021-12-31 | E06000066 | Section 95 | Subsistence Only | 2 | 1 | 5 |
| 2022-03-31 | E06000064 | Section 95 | Subsistence Only | 3 | 2 | 4 |
| 2022-03-31 | E06000065 | Section 95 | Subsistence Only | 2 | 1 | 4 |
| 2022-03-31 | E06000066 | Section 95 | Dispersal Accommodation | 4 | 3 | 7 |
| 2022-03-31 | E06000066 | Section 95 | Subsistence Only | 2 | 1 | 5 |
| 2022-06-30 | E06000064 | Section 95 | Subsistence Only | 3 | 2 | 4 |
| 2022-06-30 | E06000065 | Section 95 | Subsistence Only | 2 | 1 | 4 |
| 2022-06-30 | E06000066 | Section 95 | Dispersal Accommodation | 4 | 3 | 15 |
| 2022-06-30 | E06000066 | Section 95 | Subsistence Only | 2 | 1 | 5 |
| 2022-09-30 | E06000064 | Section 95 | Subsistence Only | 2 | 1 | 3 |
| 2022-09-30 | E06000065 | Section 95 | Subsistence Only | 2 | 1 | 4 |
| 2022-09-30 | E06000066 | Section 95 | Dispersal Accommodation | 3 | 2 | 22 |
| 2022-09-30 | E06000066 | Section 95 | Subsistence Only | 2 | 1 | 5 |
| 2022-12-31 | E06000063 | Section 98 | Contingency Accommodation - Hotel | 2 | 1 | 266 |
| 2022-12-31 | E06000064 | Section 95 | Subsistence Only | 2 | 1 | 3 |
| 2022-12-31 | E06000065 | Section 95 | Subsistence Only | 2 | 1 | 3 |
| 2022-12-31 | E06000065 | Section 98 | Contingency Accommodation - Hotel | 2 | 1 | 297 |
| 2022-12-31 | E06000066 | Section 95 | Dispersal Accommodation | 4 | 3 | 28 |
| 2022-12-31 | E06000066 | Section 95 | Subsistence Only | 2 | 1 | 5 |
| 2023-03-31 | E06000063 | Section 98 | Contingency Accommodation - Hotel | 2 | 1 | 240 |
| 2023-03-31 | E06000064 | Section 95 | Dispersal Accommodation | 2 | 1 | 58 |
| 2023-03-31 | E06000064 | Section 95 | Subsistence Only | 2 | 1 | 3 |
| 2023-03-31 | E06000065 | Section 95 | Dispersal Accommodation | 2 | 1 | 32 |
| 2023-03-31 | E06000065 | Section 95 | Subsistence Only | 2 | 1 | 3 |
| 2023-03-31 | E06000065 | Section 98 | Contingency Accommodation - Hotel | 3 | 2 | 231 |
| 2023-03-31 | E06000066 | Section 95 | Dispersal Accommodation | 4 | 3 | 29 |

## Region column reliability

Five LAD codes are assigned to more than one UK region across the loaded window: Middlesbrough (E06000002), Herefordshire (E06000019), South Cambridgeshire (E07000012), North Devon (E07000043) and Wolverhampton (E08000031). The region column is therefore not stored. Region is derived from `la_boundaries` where needed.
