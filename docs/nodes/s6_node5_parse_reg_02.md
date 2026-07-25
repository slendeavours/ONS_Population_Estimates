# Node 5 — Parse Reg_02

## Type
Spreadsheet parse + wide-to-long reshape

## Purpose
Reshape the 17-column Reg_02 sheet into 12 pathway rows per local authority,
preserving the two levels of nesting the source publishes.

## Code
```python
df = pd.read_excel(path, sheet_name="Reg_02", header=1, engine="odf")

REG02_PATHWAY_MAP = [
    (3,  "homes_for_ukraine",    "total"),
    (4,  "afghan_resettlement",  "total"),
    (5,  "afghan_resettlement",  "transitional"),
    (6,  "afghan_resettlement",  "settled_la_housing"),
    (7,  "afghan_resettlement",  "settled_prs_housing"),
    (8,  "supported_asylum",     "total"),
    (9,  "supported_asylum",     "initial_accommodation"),
    (10, "supported_asylum",     "dispersal"),
    (11, "supported_asylum",     "contingency"),
    (12, "supported_asylum",     "other"),
    (13, "supported_asylum",     "subsistence_only"),
    (14, "all_pathways",         "total"),
]
```

## Logic
1. Read `Reg_02` with `header=1` and the `odf` engine.
2. Skip the `Unknown` geography row and any non-English code.
3. Resolve the LTLA code through Node 3.
4. Emit one row per entry in the pathway map — 12 per LA.
5. Parse each measure cell: `*` and `-` become NULL with `suppressed = TRUE`
   and the verbatim marker in `source_marker`.
6. **Detect wholly suppressed pathways** and flag the all-pathways total in
   band, because the published total excludes a suppressed pathway rather than
   hiding it inside — so the total is a lower bound.
7. Set `population` on every row (a genuine LA attribute) but
   `percentage_of_population` **only on the `all_pathways` / `total` row**.

## Query Parameters

| Parameter | Value |
|---|---|
| Sheet | `Reg_02` |
| Header row | index 1 (0-based) |
| Engine | `odf` |
| Period | `2026-03-31` (single snapshot) |
| Suppression markers | `*` under 5, `-` not applicable |

## Behaviour
- `percentage_of_population` is published once per LA and computed on the
  all-pathways total. **Repeating it across all 12 rows would invite it to be
  read as a per-pathway share**, so it is populated on the all-pathways total
  row and NULL everywhere else. This is deliberate. Do not "fix" it by
  backfilling the other rows.
- `population` does repeat across a LA's 12 rows. Read it with `DISTINCT` or
  `MAX`, not `SUM`.
- Reg_02 keeps a `suppressed` column while `la_asylum_support` does not. The two
  sources genuinely differ: Asy_D11 has no suppression at all.
- Single snapshot. Prior editions are not downloaded; Reg_02 carries no time
  series.

## Connection
Input: `s6_reg.ods` from Node 2, `resolve()` from Node 3.
Output: 3,552 row dicts passed to Node 6.

## Verified Output
- 362 source rows → 296 English LAs → 3,552 pathway rows
- Skipped: the single `Unknown` geography row
- Suppressed cells: 2, both Homes for Ukraine
- In-band lower-bound flags: 2 (City of London, Isles of Scilly); 294
  all-pathways rows carry no flag
- `percentage_of_population` non-NULL on 296 rows, all `all_pathways` / `total`
- Verified 2026-07-25 (initial build)
