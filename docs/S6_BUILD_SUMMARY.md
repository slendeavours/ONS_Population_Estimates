# S6 Build Summary — Home Office Asylum Support by Local Authority

## Sources

### S6a — Asy_D11 (primary)

- **Publisher:** Home Office
- **Series:** Immigration system statistics, quarterly release
- **Publication page:** `https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-data-tables`
- **Table code:** `Asy_D11` — Asylum seekers in receipt of Home Office support by support type, accommodation type and local authority
- **Status:** Official Statistics
- **Native geography:** LAD code published in the file (99.64% populated in scope)
- **Date range loaded:** 2018 Q1 – 2026 Q1 (33 quarters, 2018-03-31 to 2026-03-31)
- **Floor applied:** 2018-01-01. Section 4 carries no LA geography before 2018, so earlier quarters cannot be aggregated consistently across support types. 5,006 rows excluded.
- **Refresh cadence:** Quarterly, roughly 8 weeks after quarter end
- **Edition loaded:** year ending March 2026
- **Tables:** `la_asylum_support`, `la_asylum_support_unallocated`, `asylum_support_non_england`
- **Natural key:** `(period_ending, lad24cd, support_type, accommodation_type)`
- **Row counts:** 20,926 England · 84 unallocated · 2,374 non-England

### S6b — Reg_02 (secondary)

- **Publisher:** Home Office and MHCLG
- **Series:** Regional and local authority data on immigration groups
- **Publication page:** `https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-regional-and-local-authority-data`
- **Table code:** `Reg_02` — Immigration groups by local authority
- **Native geography:** LTLA (ONS code), 296/296 English LAs present
- **Date range loaded:** single snapshot, 2026-03-31
- **Refresh cadence:** Quarterly
- **Table:** `la_immigration_groups`
- **Natural key:** `(period_ending, lad24cd, pathway, sub_pathway)`
- **Row count:** 3,552 (296 LAs × 12 pathway rows)

### Asy_D09 — verification reference only

Downloaded and read at verify time, **never loaded**. Provides independent
per-period UK-region totals for Checks 8a and 8b, removing the circularity of
reconciling the file against itself. Covers 2014 Q1 – 2026 Q1, so all 33 loaded
periods are available.

Trap for future work: Asy_D09 has two region columns. `Region` is the
*nationality* region (Africa North, Asia East). The UK geography is
`UK Region / Nation`.

## Geography Resolution

Code-first cascade — the source publishes LAD codes, so name matching is a
fallback that is never reached in practice.

| Method | Distinct pairs |
|---|---:|
| 1 — direct against `la_boundaries` | 292 |
| 2 — forward via `la_code_lookup` | 14 |
| 3–5 — name-based | 0 |
| non-England | 61 |
| unallocated | 3 |

England coverage: **292/296** LAs reached at least once; **286/296** at
2026-03-31. The four never appearing are Isle of Wight, Isles of Scilly,
Derbyshire Dales and Cotswold.

### Build-local resolution layer

Three codes are resolved outside `la_code_lookup` because the shared table is
wrong or silent on them:

| Code | Area | S6 resolves to | `la_code_lookup` |
|---|---|---|---|
| E07000027 | Barrow-in-Furness | E06000064 | E06000063 (**wrong**) |
| E07000028 | Carlisle | E06000063 | *absent* |
| E07000189 | South Somerset | E06000066 | *absent* |

Verified against the ONS area pages for each successor plus the Cumbria and
Somerset (Structural Changes) Orders 2022. S6 does not write to
`la_code_lookup`. See
[`decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md`](decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md).

**The database is currently inconsistent across sources on Cumberland
(E06000063) and Westmorland and Furness (E06000064). S6 is the only correct
one.**

## Aggregation

`ON CONFLICT DO UPDATE` would keep one row and silently discard the rest where
several source rows share a natural key, so rows are **summed before upsert**.
35 keys collapse, absorbing 49 rows:

```
 23,433  rows in scope
-    49  absorbed (34 reorganisation merges, 1 duplicate key)
= 23,384  rows landed
```

- **34 reorganisation merges** — different LAD codes forward-resolving onto one
  successor unitary (Mendip + Sedgemoor + South Somerset + Somerset West and
  Taunton → Somerset). Expected behaviour of the approved cascade.
- **1 duplicate key** — Wolverhampton at 2023-03-31 appears twice on the same
  natural key, once mislabelled `North West` and once correctly `West
  Midlands`. Summed 4 + 12 = 16.

Full per-key detail in [`s6_source_anomalies.md`](s6_source_anomalies.md).

## Suppression Conventions

| Source | Markers | Handling |
|---|---|---|
| Asy_D11 | *none* | `People` is numeric in all 28,439 rows. No `suppressed` column. |
| Reg_02 | `*` (under 5, disclosure control), `-` (not applicable) | Coerced to NULL, `suppressed = TRUE`, verbatim marker in `source_marker` |

**Zeros are never published.** The minimum Asy_D11 value is 1 and no blank cells
exist, so an absent LA means "not published", not "none". Coverage is reported
as a count, never gated against 296.

**City of London and Isles of Scilly** have understated all-pathways totals: the
published figure excludes the suppressed Homes for Ukraine pathway rather than
hiding it inside. Both carry an in-band `source_marker` on the
`all_pathways` / `total` row stating the total is a lower bound. No other LA is
affected.

## Series Breaks

Recorded in the `asylum_series_breaks` table so consumers of
`vw_la_asylum_support_totals` see them without reading documentation.

| First | Last | Support type | Effect |
|---|---|---|---|
| 2022-12-31 | ongoing | Section 98 | Gained LA geography. England jumps 53,749 → 98,375 — a reporting change, not 44,000 arrivals. |
| 2023-12-31 | 2024-12-31 | Section 95 | Subsistence Only lost LA geography for five quarters. 32 LAs vanish and return. |

**First fully comparable period for England totals: 2025-03-31.**

Like-for-like (excluding subsistence-only throughout), peak 2023-09-30 to
2026-03-31: England −23.5%, LA count +11.6%, people-per-LA −31.4%. The
contraction-while-spreading pattern is real; the apparent 273 → 237 → 286 LA
swing in the headline series is mostly the second break.

## Verification

All 13 checks pass. Any failure rolls back the whole transaction.

| # | Check | Result |
|---|---|---|
| 1 | Coverage | 0 unmatched; 286/296 at 2026-03-31; range 187–286 |
| 2 | Referential integrity | 0 orphans, 0 non-English codes in the England table |
| 3 | Anchor set | Birmingham 2,142 · Liverpool 2,053 · Coventry 1,712 · Glasgow 3,870 · Belfast 1,607 · UK 97,519 — all exact |
| 4 | Load fidelity | Parsed vs landed, per period, all four tables, tolerance 0 |
| 5 | Reasonableness | 0 negatives; England max 3,488 (Birmingham, 2023-09-30); 181 of 361 LAs under 100 vs ~181 published |
| 6 | Suppression handling | 2 suppressed cells, all NULL, none coerced to 0 |
| 7 | Idempotency | Checksum identical after second load |
| 8a | England vs Asy_D09 | 33/33 periods exact |
| 8b | Unallocated vs Asy_D09 | 33/33 periods exact |
| 9 | Cross-source Reg_02 vs Asy_D11 | 286/286 exact, post-resolution |
| 10 | Reg_02 internal | 296/296 reconcile |
| 11 | Collision log | 1 duplicate key of 5 permitted |
| 12 | View integrity | 7,868 rows, tolerance 0 on both identities |

### Anchor aggregation

Established empirically at Gate 1 by testing three candidate aggregations
against the published anchors. The published headline is **all support types
and all accommodation types, unfiltered**. Excluding subsistence-only, or
restricting to Section 95, both miss by thousands.

### The distribution check

Stated on the **at or above 100** side, which is computed from the source with
no adjustment:

```
LAs at or above 100, from the source : 344 present - 164 under 100 = 180
Published                            : 181 of 361 under 100, therefore 180 at or above
Difference                           : 0
```

Confirming decomposition of the Home Office denominator:

```
361 = 296 England + 32 Scotland + 22 Wales + 11 Northern Ireland

344 present = 286 England + 58 non-England
 17 absent  =  10 England +  7 non-England
```

The denominator is the full UK LAD set, which is why LAs absent from the file
fall in the under-100 bucket — making this an independent confirmation that an
absent LA means "not published" rather than zero, rather than a restatement of
it. The 10 absent English LAs match the named list in Check 9.

## Reconciliation at 2026-03-31

```
England 85,162 + non-England 12,357 + unallocated 0 = 97,519   (published 97,519)
```

| | Count |
|---|---:|
| English LAs in `la_boundaries` | 296 |
| English LAs in Reg_02 | 296 |
| English LAs in Asy_D11 | 286 |
| Matched, exact on supported-asylum total | 286 |
| In Reg_02 but not Asy_D11 | 10 |
| In Asy_D11 but not Reg_02 | 0 |

## W1 Integration

**None.** S6 is standalone — no `staging_la_signals` column, no tenant type, no
map layer, no composite index. Same pattern as S19 PIP.

## Files

- ETL script: `scripts/s6_asylum_build.py`
- Verification suite: `scripts/s6_asylum_verify.py`
- Source summary: `docs/s6_asylum_source.md`
- Anomalies (regenerated each run): `docs/s6_source_anomalies.md`
- Decision record: `docs/decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md`
- Node documentation: `docs/nodes/s6_node1_*` … `s6_node8_*`
