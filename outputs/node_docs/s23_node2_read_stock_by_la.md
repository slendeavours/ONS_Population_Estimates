# S23 Node 2: Read the STOCK_BY_LA Sheet

- **Type:** Code (parse and assert)
- **Purpose:** Read the local authority breakdown out of the look-up tool workbook, assert its header set has not changed, and separate the three grains the sheet mixes.
- **Credential:** None.

## Where the breakdown lives

RSH publishes no flat local-authority data file. The breakdown is the
**`STOCK_BY_LA` sheet inside the look-up tool workbook**, which exists to
drive the workbook's own search box. It is an internal sheet with no
publication guarantee, so the header set is asserted exactly and any change
halts the build.

Other sheets in the same workbook: `Introduction and Contents`, `Glossary`,
`Version History`, `Area Summary`, `LARPs and PRPs in region`, `How to use the
search function`, `Flat_File`, `Lookup source`, `totals_&_RP_counts`, `Search
box config`.

`Flat_File` carries rents by bedroom size and is a different question;
`Area Summary` is the interactive front end.

## Expected header

Exact, in order. Any unrecognised header, or any expected header absent, halts.

| Header | Target column |
|---|---|
| `RP_Name` | `rp_name` |
| `RP_Code` | `rp_code` |
| `RP_Type` | `provider_type` (mapped) |
| `SDR_Size` | `rp_size_band` |
| `Survey_Status` | `survey_status` |
| `LA_Nm` | `la_name` |
| `LA_Code` | `publisher_la_code` |
| `Concat` | not stored — the workbook's own search key |
| `Total Social Stock` | `total_social_stock` |
| `LA_GN_SC_Own` | `general_needs_self_contained` |
| `LA_GN_BSp_Own` | `general_needs_bedspaces` |
| `LA_SHHOP` | `supported_housing_and_older_people` |
| `LA_LCHO_Less_100_Eqty_Own` | `low_cost_home_ownership` |

`SC` is self-contained and `BSp` is bedspaces, per the workbook Glossary:
total social stock is "the number of self-contained units plus bedspaces".

## The three grains — the trap in this sheet

`RP_Type` is the only thing distinguishing them:

| `RP_Type` | Rows | Return | Loaded |
|---|---:|---|---|
| `Large` | 7,205 | SDR long form | Yes, as `PRP` |
| `Small` | 2,738 | SDR short form | Yes, as `PRP` |
| `LARP` | 228 | LADR | Yes, as `LARP` |
| `LA` | 296 | — | **No.** Publisher's own per-authority subtotals |
| `Region` | 9 | — | **No.** Regional aggregates |

A load that does not filter triple-counts: the unfiltered sheet sums to
13,599,165 units against a true 4,533,055.

The subtotal rows are returned by this node rather than discarded, because
they are the verification — gate 7 asserts the loaded provider rows sum to
them exactly.

## Logic

1. Load the workbook read-only with `data_only=True`, so formulas resolve to
   values.
2. Halt if `STOCK_BY_LA` is absent, naming the sheets that are present.
3. Compare the header row against the expected set in both directions.
4. Keep rows with a non-null `RP_Code`.
5. Split on `RP_Type` into provider rows, LA subtotals keyed by publisher LA
   code, and everything else.

## Behaviour

- **Conflict handling:** Not applicable — read-only.
- **Re-run safety:** Pure function of the file.
- **Failure:** Halts on a changed header rather than shifting columns. This is
  the same control as S1b node 2, for the same reason.

## Connection

None. Operates on the file fetched by node 1.

## Verified Output

- 10,476 data rows: 10,171 provider, 296 LA subtotal, 9 regional.
- Header matched exactly, 13 of 13 columns.
- 296 distinct publisher LA codes on provider rows, plus `England` on the
  regional rows.
- 1,704 distinct provider codes.
- Verified 2026-08-14.
