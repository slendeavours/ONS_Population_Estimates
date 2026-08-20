# S6 — Home Office asylum support by local authority

## Publisher and series

| | S6a | S6b |
|---|---|---|
| **Publisher** | Home Office | Home Office and MHCLG |
| **Series** | Immigration system statistics, quarterly release | Regional and local authority data on immigration groups |
| **Table code** | `Asy_D11` | `Reg_02` |
| **Publication page** | `https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-data-tables` | `https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-regional-and-local-authority-data` |
| **Format** | `.xlsx`, 1.28 MB | `.ods`, 266 KB |
| **Edition loaded** | year ending March 2026 | year ending March 2026 |
| **Shape** | Quarterly time series | Single snapshot |
| **Native geography** | LAD code published in the file | LTLA (ONS code) |
| **Refresh cadence** | Quarterly, roughly 8 weeks after quarter end | Quarterly |

Download URLs are **discovered from the landing page at run time**. GOV.UK asset
URLs change with every release, so none are hardcoded.

A third table, `Asy_D09` (asylum seekers in receipt of support by nationality,
support type, accommodation type and UK region), is downloaded and read at
verification time only. It is **never loaded into a table**. It provides the
independent per-period reconciliation reference for Checks 8a and 8b.

## Date range and floor

Asy_D11 carries 2014 Q1 to 2026 Q1. S6 loads **2018 Q1 forward — 33 quarters,
2018-03-31 to 2026-03-31**.

The floor is applied because Section 4 carries no local authority geography
before 2018: those rows are published as a single national aggregate marked
`N/A - Section 4 (pre-2018)`. Earlier quarters cannot be aggregated consistently
across support types, so loading them would produce an England series that
silently omits one support type. 5,006 of 28,439 rows are excluded; 23,433
remain in scope.

## Tables

| Table | Rows | People | Natural key |
|---|---:|---:|---|
| `la_asylum_support` | 20,926 | 2,164,730 | `(period_ending, lad24cd, support_type, accommodation_type)` |
| `la_asylum_support_unallocated` | 84 | 225,515 | `(period_ending, support_type, accommodation_type, na_reason)` |
| `asylum_support_non_england` | 2,374 | 330,601 | `(period_ending, lad_code, support_type, accommodation_type)` |
| `la_immigration_groups` | 3,552 | 615,954 | `(period_ending, lad24cd, pathway, sub_pathway)` |
| `asylum_series_breaks` | 2 | — | `break_id` |
| `vw_la_asylum_support_totals` | 7,868 | — | view |

### Row count reconciliation

```
 23,433  rows in scope (Asy_D11, 2018-01-01 forward)
-    49  absorbed by SUM aggregation across 35 collision keys
         (34 reorganisation merges, 1 duplicate key)
= 23,384  rows landed across the three Asy_D11 tables
```

People totals are unaffected — aggregation preserves `SUM`. Per-key detail is in
[`s6_source_anomalies.md`](s6_source_anomalies.md).

## Dimensions

**Support type:** Section 4, Section 95, Section 98.

**Accommodation type**, normalised to title case so the source's
`Subsistence Only` and `Subsistence only` collapse to one value:
Dispersal Accommodation, Initial Accommodation, Contingency Accommodation -
Hotel, Contingency Accommodation - Other, Other Accommodation, Subsistence Only.

**Reg_02 pathways:** `homes_for_ukraine`, `afghan_resettlement`,
`supported_asylum`, `all_pathways`, each with a `total` sub-pathway plus the
published "of which" breakdowns — 12 rows per LA.

## Series breaks

**Two structural breaks make the England series non-comparable across parts of
the window.** They are recorded machine-readably in `asylum_series_breaks`, not
only in this document, because anyone querying `vw_la_asylum_support_totals`
will not read prose.

| First period | Last period | Support type | What changed |
|---|---|---|---|
| 2022-12-31 | *(ongoing)* | Section 98 | Gained LA geography. Before this, all Section 98 people were a single national row. |
| 2023-12-31 | 2024-12-31 | Section 95 | Subsistence Only lost LA geography for five consecutive quarters. |

Consequences:

- England rises from **53,749 to 98,375** between 2022-09-30 and 2022-12-31.
  That is a reporting change, not 44,000 arrivals. Do not plot the England
  total across that boundary without a break marker.
- LA counts and England totals are depressed for the five periods from
  2023-12-31 to 2024-12-31. 32 English LAs that appeared only via
  subsistence-only claimants at 2023-09-30 vanish entirely from 2023-12-31 and
  return at 2025-03-31. The apparent 273 → 237 → 286 swing in LA coverage is
  substantially this artefact, not dispersal contracting and recovering.

**The first fully comparable period for England totals is 2025-03-31.** From
that quarter onward every support type carries LA geography and the unallocated
table is empty.

On a like-for-like basis excluding subsistence-only throughout, the underlying
pattern is real: from the 2023-09-30 peak to 2026-03-31, England fell 23.5%
while the LA count rose 11.6% and people-per-LA fell 31.4%. The supported
population contracted while spreading across more councils.

## Suppression conventions

The two sources differ, deliberately.

**Asy_D11 has no suppression.** All 28,439 `People` values are numeric, with no
markers and no blank cells. The minimum is 1 and zero never appears.
`la_asylum_support` therefore carries **no `suppressed` column**.

**An absent LA means "not published", not "none".** Because zeros are never
published, a local authority missing from a period had no published figure —
which is not the same as having no supported asylum seekers. Coverage is
reported as a count, never gated against 296.

### The distribution check confirms this independently

Check 5 tests the published distribution shape on the **at or above 100** side,
because that quantity is computed from the source with no adjustment:

```
LAs at or above 100, from the source : 344 present - 164 under 100 = 180
Published                            : 181 of 361 under 100, therefore 180 at or above
Difference                           : 0
```

Confirming decomposition of the Home Office denominator:

```
361 = 296 England + 32 Scotland + 22 Wales + 11 Northern Ireland
```

The denominator is the full UK LAD set, which is why LAs absent from the file
fall in the under-100 bucket — and that is what makes this an independent
confirmation of the zero-handling finding rather than a restatement of it.

```
344 present = 286 England + 58 non-England
 17 absent  =  10 England +  7 non-England
```

The 10 absent English LAs are Isle of Wight, Isles of Scilly, Derbyshire Dales,
Mid Devon, Cotswold, Tewkesbury, Hart, New Forest, Malvern Hills and East
Hertfordshire — the same ten Check 9 reports as present in Reg_02 but absent
from Asy_D11.

**Reg_02 does have suppression.** `*` marks fewer than 5 people withheld for
disclosure control; `-` marks not applicable. `la_immigration_groups` retains
`suppressed`, with `people` set to NULL and the verbatim marker in
`source_marker`.

### Understated totals — City of London and Isles of Scilly

For these two LAs the published all-pathways total **excludes** the suppressed
Homes for Ukraine pathway rather than hiding it inside. City of London's
published total of 7 is 6 (Afghan) + 1 (Asylum); the true figure is higher by
between 1 and 4.

Both are flagged in-band: the `all_pathways` / `total` row for each carries a
`source_marker` stating that the total is a lower bound. No other LA is
affected — 294 of 296 all-pathways rows carry no flag.

### Geography sentinels

Where a row has no resolvable local authority, the key columns carry
`not_stated` and the verbatim source string is preserved in `na_reason`. Three
distinct reasons occur:

| Reason | Rows | People | Periods |
|---|---:|---:|---|
| `N/A - Section 98 (pre-Dec 2022)` | 19 | 202,558 | 2018-03-31 → 2022-09-30 |
| `N/A - Subsistence Only (Dec 2023 - Dec 2024)` | 5 | 18,598 | 2023-12-31 → 2024-12-31 |
| `Unknown` | 60 | 4,359 | 2018-06-30 → 2023-09-30 |

Section 98 rows carry **no accommodation type before 2023** — 2018 Q1 through
2022 Q4 of the loaded window, five years. The accommodation breakdown is
structurally incomplete for that support type across that span. Those rows are
in the unallocated table, so `la_asylum_support` never contains `not_stated` in
`accommodation_type` in the current edition, but the column permits it.

## Geography resolution

Code-first cascade. 99.64% of in-scope rows carry a usable LAD code, so
name-based matching is never reached.

| Method | Distinct pairs |
|---|---:|
| 1 — direct match against `la_boundaries` | 292 |
| 2 — forward via `la_code_lookup` | 14 |
| 3–5 — name-based | 0 |
| non-England (routed to `asylum_support_non_england`) | 61 |
| unallocated | 3 |

Sixteen pre-2023 district codes and the two Barnsley/Sheffield recodes resolve
forward. Merging several abolished districts onto one successor unitary
collapses 34 natural keys, which are summed before upsert.

### Dependency — geography is correct here and inconsistent elsewhere

**S6 uses a build-local resolution layer for three codes that `la_code_lookup`
handles wrongly or not at all.** See
[`decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md`](decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md).

| Code | Area | S6 resolves to | `la_code_lookup` resolves to |
|---|---|---|---|
| E07000027 | Barrow-in-Furness | E06000064 Westmorland and Furness | E06000063 Cumberland (**wrong**) |
| E07000028 | Carlisle | E06000063 Cumberland | *no entry* |
| E07000189 | South Somerset | E06000066 Somerset | *no entry* |

Until the remediation lands, **the database is inconsistent across sources on
Cumbria, and S6 is the only correct one**. Affected local authorities:
**Cumberland (E06000063)** and **Westmorland and Furness (E06000064)**. Do not
reconcile S6 against another source on those two LAs. Somerset (E06000066) is
affected only by the omission, so other sources under-count rather than
misattribute.

## Known caveats

- Figures are based on the **registered address** of the person, which is not
  necessarily where they regularly reside.
- **Unaccompanied asylum-seeking children are excluded.** UASC are supported by
  local authority children's services, not Home Office asylum support. This is
  not a count of all asylum seekers in an area. DfE publishes UASC by local
  authority separately.
- Both sources cover the **whole UK**. This pipeline is England only; the
  361-LA universe is filtered to 296, with Scotland, Wales and Northern Ireland
  retained in `asylum_support_non_england` so the counts reconcile.
- The Home Office has revised these tables historically — accommodation type in
  June 2024, geographic distribution in August 2024, accommodation types again
  in November 2025. Each load is a **full replace** of the periods it covers.
  Do not assume prior periods are immutable.
- The published **`UK Region / Nation` column is unreliable**: five LAD codes
  are assigned to more than one region across the window. It is not stored.
  Region is derived from `la_boundaries`.
- Data is extracted on the last day of the quarter, or the closest possible
  date, and can change daily. Treat as provisional.

## Refresh procedure

1. Run `python scripts/s6_asylum_build.py` from the repository root. Discovery,
   download, parse, resolve, validate, upsert and logging all happen in one
   transaction.
2. The script discovers the current edition from both landing pages. No URL or
   edition label needs editing between quarters.
3. All 13 verification checks must pass. Any failure rolls the whole
   transaction back and exits non-zero — partial or suspect data is never left
   behind.
4. Update the anchor constants in `scripts/s6_asylum_verify.py` when a new edition
   lands: `ANCHOR_PERIOD`, `ANCHORS_ENGLAND`, `ANCHORS_NON_ENGLAND`,
   `ANCHOR_UK_TOTAL`, `PUBLISHED_LA_UNIVERSE`, `PUBLISHED_UNDER_100`. Source
   them from that release's "How many people are in the UK asylum system?"
   narrative page.
5. If a new series break appears, add it to `SERIES_BREAKS` in
   `scripts/s6_asylum_build.py` and update the first fully comparable period here.

## Scope

S6 is **standalone**. It is not wired into Workflow 1, adds no column to
`staging_la_signals`, adds no tenant type, and adds no map layer — the same
pattern as S19 PIP. No composite index, score or ranking combines S6 with any
other source.
