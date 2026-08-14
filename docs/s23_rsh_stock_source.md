# S23 — RSH registered provider social housing stock by local authority

<!-- repo-meta
status: active
last-reviewed: 2026-08-14
type: source
consumed-by: scripts/s23_rsh_stock_build.py, scripts/s23_rsh_stock_verify.py
-->

| | |
|---|---|
| Publisher | Regulator of Social Housing |
| Series | Registered provider social housing stock and rents in England, registered providers look-up tool |
| Landing page | https://www.gov.uk/government/statistics/registered-provider-social-housing-stock-and-rents-in-england-2024-to-2025 |
| Cadence | Annual, published autumn; stock as at 31 March |
| Target table | `rsh_rp_stock_by_la` |
| Natural key | `(stock_date, rp_code, lad24cd)` |
| Rows | 10,171 provider-by-authority rows |
| Coverage | 296 of 296 English local authorities |
| Built | 2026-08-14 |

## Why this source matters

The first **direct** supply-side measure in the pipeline. Everything before it
is indirect: S11 counts CQC-registered locations, S8 counts Housing Benefit
Specified Accommodation caseload. This counts units, by owner, by authority.

## The two returns, and why they are one table

The release draws on two collections:

- **SDR**, the Statistical Data Return, covering private registered providers
- **LADR**, the Local Authority Data Return, covering local authority
  registered providers

They are held in one table with a `provider_type` column. The schema decision
was made after confirming structural compatibility rather than assumed: the
publisher has already merged them. LARP rows sit in the same sheet as PRP rows
with the same five stock columns, and the per-authority subtotals reconcile
across both. Merging is reading the file as published.

`provider_type` keeps them separable, because the analysis sometimes wants
them apart — a local authority landlord is not a competitor in the way a
private provider is.

## Where the local authority breakdown lives

Not in a data file. It is the **`STOCK_BY_LA` sheet inside the look-up tool
workbook** (`RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx`, 2.3 MB), which exists to
drive the workbook's own search box.

That makes it an internal sheet with no publication guarantee, so the build
asserts the exact header set and stops if it changes rather than shifting
columns silently.

The sheet extracts cleanly. One row per provider per authority, no merged
headers, no suppression notation, ONS codes already present.

## Grain — the trap in this sheet

`STOCK_BY_LA` mixes three grains in one column layout, distinguished only by
`RP_Type`:

| `RP_Type` | Rows | Loaded |
|---|---:|---|
| `Large`, `Small` | 9,943 | Yes — PRP, from SDR |
| `LARP` | 228 | Yes — LARP, from LADR |
| `LA` | 296 | **No** — the publisher's own per-authority subtotals |
| `Region` | 9 | **No** — regional aggregates |

A load that does not filter on `RP_Type` triple-counts: the unfiltered sheet
sums to 13,599,165 units against a true 4,533,055.

The subtotal rows are not discarded — they are the verification. Gate 7
asserts the loaded provider rows sum to the publisher's own LA subtotal on all
five measures for all 296 authorities. They reconcile exactly.

## Columns

| Target column | Source | Meaning |
|---|---|---|
| `supported_housing_and_older_people` | `LA_SHHOP` | Supported housing and housing for older people |
| `general_needs_self_contained` | `LA_GN_SC_Own` | General needs, self-contained units |
| `general_needs_bedspaces` | `LA_GN_BSp_Own` | General needs, non-self-contained bedspaces |
| `low_cost_home_ownership` | `LA_LCHO_Less_100_Eqty_Own` | LCHO, less than 100% equity |
| `total_social_stock` | `Total Social Stock` | The four above, summed |

The identity `total = the four components` holds on all 10,171 rows and is
enforced by a CHECK constraint.

## The supported housing caveat

**`LA_SHHOP` combines supported housing with housing for older people, and RSH
does not split them at local authority level.** A large share of the 504,902
national units is sheltered and retirement housing, not the supported
accommodation this pipeline is concerned with.

So the figure is an **upper bound** on relevant provision, not a count of it.
Read as a count of exempt-accommodation-style units it would overstate by an
unknown and probably large margin. The publisher does split supported housing
rents from general needs rents nationally (additional tables 1.7 and 1.8), but
not the stock, and not by authority.

RSH also notes that a tenant receiving support services at home does not make
the unit supported housing; it must meet the definition in the Government
Policy Statement on Rents for Social Housing.

## Stock date versus publication date

Both are stored, and the distinction matters. The return is a **snapshot at 31
March**; publication follows roughly seven months later. The 2024 to 2025
edition describes 31 March 2025 and was published 28 October 2025, so by the
time the next edition lands the newest available figure is up to nineteen
months old.

A stock figure read as though it were current is wrong by up to a year, which
is why `stock_date` and `publication_date` are separate columns rather than a
single date.

## Weighted versus unweighted

Loaded rows are **unweighted** — they are the returns as submitted. The
publisher's headline national figures are **weighted** to impute for small
providers filing the short SDR form:

| Measure | Loaded (unweighted) | Published (weighted) |
|---|---:|---:|
| Total social stock | 4,533,055 | 4,546,653 |
| Supported housing + older people | 504,902 | 507,209 |

The suite reconciles against the publisher's unweighted LA subtotals, which is
a real comparison, and reports the weighted figures as context without
asserting equality.

## Ownership, not management

Stock is recorded where it is **owned**. A provider owning units in an
authority is not evidence that it manages or operates there, and the reverse
holds too — a managing agent with no ownership does not appear.

## Revisions

`revises_back_series` is true, established from the publisher's technical
notes rather than assumed: RSH states it will republish in the April following
initial publication where aggregate changes made by providers require a major
revision, and makes non-scheduled corrections for substantial errors or
methodological issues.

## Known traps

- **Not filtering `RP_Type`.** Triple-counts.
- **Reading `LA_SHHOP` as supported housing.** It includes older people's
  housing.
- **Treating the landing page URL as stable.** It carries the edition years
  and changes annually.
- **Treating `stock_date` as current.** It is up to nineteen months old.
- **Comparing loaded totals to the published headline.** Unweighted against
  weighted.

## Reproducing

```bash
python scripts/s23_rsh_stock_build.py --discover
python scripts/s23_rsh_stock_build.py --load
python scripts/s23_rsh_stock_verify.py
```
