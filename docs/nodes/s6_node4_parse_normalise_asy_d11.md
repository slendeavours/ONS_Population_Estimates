# Node 4 — Parse and Normalise Asy_D11

## Type
Spreadsheet parse + normalisation + classification + aggregation

## Purpose
Turn the published Asy_D11 sheet into three disjoint, keyed datasets: allocated
England, unallocated, and non-England.

## Code
```python
df = pd.read_excel(path, sheet_name="Data_Asy_D11", header=1, engine="openpyxl")
df[c_date] = pd.to_datetime(df[c_date], format="%d %b %Y").dt.date
df = df[df[c_date] >= FLOOR]

def normalise_accommodation(value):
    raw = (value or "").strip()
    if raw.upper().startswith("N/A"):
        return NOT_STATED, raw
    return " ".join(word.capitalize() for word in raw.split()), None
```

## Logic
1. Read `Data_Asy_D11` with `header=1` — row 0 is the table title, row 1 the
   column headers.
2. Parse `Date (as at…)` from `%d %b %Y` to a date.
3. **Filter to `>= 2018-01-01`.** Section 4 carries no LA geography before 2018,
   so earlier quarters cannot be aggregated consistently across support types.
4. **Normalise accommodation type** — trim and title-case, collapsing the
   source's `Subsistence Only` and `Subsistence only` into one value. `N/A -…`
   markers become the sentinel `not_stated`, with the verbatim string retained.
5. **Classify each row into exactly one destination:**
   - Local authority or LAD code is an `N/A` marker, **or** the LA name is the
     literal `Unknown` → unallocated. The `Unknown` case matters: those rows
     carry a bare `N/A` in the code column and would be missed by a name-prefix
     test alone.
   - Code prefix `S12`, `W06`, `N09` → non-England.
   - Code prefix `E06`, `E07`, `E08`, `E09` → resolve via Node 3 → England.
   - Anything else → hard stop.
6. **Aggregate with `SUM` on the natural key** before upsert. Several source
   rows legitimately share a key.
7. Classify each collision so the halt threshold measures the right thing:
   - `reorganisation_merge` — source rows carry **different** LAD codes and land
     on one successor unitary. Expected.
   - `duplicate_key` — source rows carry the **same** LAD code. A source defect.

## Query Parameters

| Parameter | Value |
|---|---|
| Sheet | `Data_Asy_D11` |
| Header row | index 1 (0-based) |
| Date format | `%d %b %Y` |
| Floor date | `2018-01-01` |
| Sentinel | `not_stated` |
| Engine | `openpyxl` |

## Behaviour
- **Summing, not overwriting.** `ON CONFLICT DO UPDATE` alone would keep one row
  of a colliding set and silently discard the rest — 49 rows and their people in
  this edition.
- The published `UK Region / Nation` column is **not stored**. Five LAD codes are
  assigned to more than one region across the window, so it is unreliable.
  Region is derived from `la_boundaries` where needed.
- Hard stop on any unresolved English geography.
- Re-run safe: parsing is pure, with no database or filesystem side effects.

## Connection
Input: `s6_d11.xlsx` from Node 2, `resolve()` from Node 3.
Output: England, unallocated and non-England dictionaries plus the classified
collision list, passed to Nodes 6 and 7.

## Verified Output
- Raw rows 28,439 → 23,433 in scope after the floor (5,006 excluded)
- England 20,926 keys, 2,164,730 people
- Unallocated 84 keys, 225,515 people
- Non-England 2,374 keys, 330,601 people
- Collisions 35: 34 reorganisation merges, 1 duplicate key
- Row reconciliation: 23,433 − 49 absorbed = 23,384 landed
- Accommodation values after normalisation: Contingency Accommodation - Hotel,
  Contingency Accommodation - Other, Dispersal Accommodation, Initial
  Accommodation, Other Accommodation, Subsistence Only
- Verified 2026-07-25 (initial build)
