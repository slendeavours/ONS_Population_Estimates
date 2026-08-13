# Node 2 — Extract Council Taxbase

## Type

Code (Python). `scripts/s22_ctb_empties_build.py`, function `extract_council_taxbase`.

## Purpose

Turn the local-authority-level workbook into 296 authority records and 3,256 exemption class records, locating every column by its published label rather than by a fixed offset.

## Credential

Postgres `exempt_pipeline` (read only, for the recode lookup at the end of the step).

## Query / Code / URL (full content)

```python
def _block_total_col(label_row, header_row, table_number):
    """0-indexed column of the `Total` header inside a named table block."""
    starts = sorted(i for i, v in enumerate(label_row) if v is not None)
    pattern = re.compile(r"Table\s+" + re.escape(table_number) + r"[.\s]")
    for k, s in enumerate(starts):
        if pattern.search(str(label_row[s])):
            end = starts[k + 1] if k + 1 < len(starts) else len(label_row)
            for i in range(s, end):
                if str(header_row[i]).strip() == "Total":
                    return i, str(label_row[s]).strip()
            disco.halt(f"Table {table_number} block found but it has no "
                       "'Total' column — structure has changed")
    disco.halt(f"Table {table_number} not found on the sheet — structure has "
               "changed")


wanted = {
    "total_dwellings":           "1.01",
    "second_homes":              "1.11",
    "empty_homes_premium_count": "1.17",
    "empty_total":               "1.18",
    "empty_6_months_plus":       "1.19",
}

UNOCCUPIED_CLASSES = ["B", "D", "E", "F", "G", "H", "I", "J", "K", "L", "Q"]
```

Exemption class columns are located the same way: find the `Table 2.01` block on the `Supplementary Data` sheet, then read the `Class <letter>` headers inside it, skipping any marked "not in use".

```python
    missing = [c for c in UNOCCUPIED_CLASSES if c not in class_cols]
    if missing:
        disco.halt(f"Table 2.01 is missing exemption classes {missing} — "
                   "LA-level class breakdown structure has changed")
```

Recode resolution, applied after extraction:

```python
    recodes_applied = []
    if conn is not None:
        current, recodes = resolve_recodes(conn)
        for rec in records:
            new = recodes.get(rec["lad24cd"])
            if new:
                recodes_applied.append({"published": rec["lad24cd"],
                                        "lad24cd": new,
                                        "la_name": rec["la_name"]})
                rec["lad24cd"] = new
        ...
        unresolved = [(r["lad24cd"], r["la_name"]) for r in records
                      if r["lad24cd"] not in current]
        if unresolved:
            disco.halt(
                "UNEXPLAINED codes not present in la_boundaries and not held "
                f"as a recode in la_code_lookup: {unresolved}. An unresolved "
                "code is a hard stop; establish what it is against an "
                "authoritative source before loading.")
```

## Logic (step by step)

1. Open the `Council Taxbase Data` sheet from row 5. Row 5 carries the table label spanning each block, row 6 the column headers, row 7 the England total, rows 8 to 303 the 296 billing authorities.
2. For each of the five wanted tables, find the block whose row 5 label matches `Table <number>` and take the column inside that block whose row 6 header is exactly `Total`. Block widths vary — Table 1.06 carries an extra disabled-relief column — so offsets are never assumed.
3. Open the `Supplementary Data` sheet and locate the `Table 2.01` block. Read the `Class A` to `Class W` headers within it. Halt if any of the eleven unoccupied classes is absent.
4. Capture the England row (`E92000001`) separately. It is the reconciliation target for the two measures the release page does not print, and is never loaded as an authority.
5. For each authority row, read the five totals, derive `empty_under_6_months` as Table 1.18 minus Table 1.19, and sum the eleven unoccupied classes into `unoccupied_exemptions_total`.
6. Emit one long-format record per authority per exemption class, carrying the class description.
7. Resolve codes. Any published code held in `la_code_lookup` as `change_type = 'recode'` is mapped to its current code — same area, new number. Abolitions are not resolved here; the Council Taxbase publishes current authorities only.
8. Check for a recode collision, then check that every resulting code exists in `la_boundaries`. An unresolved code is a hard stop, reported as UNEXPLAINED rather than as expected or harmless.

## Behaviour

Pure extraction, no writes. Deterministic: the same workbook produces byte-identical records on every run. Halts on four conditions — missing table block, block without a `Total` column, missing exemption class, unresolved geography code.

## Connection

- Input: Node 1 (Discover Source Files)
- Output: Node 5 (Create Tables)

## Verified Output

2026-08-13. 296 authority records and 3,256 exemption class records from taxbase year 2025. England row captured: total dwellings 25,817,220, empty total 542,260, empty 6+ months 309,889, empty homes premium 152,928, second homes 267,894, unoccupied exemptions 212,004. Two recodes applied: Barnsley E08000038 to E08000016, Sheffield E08000039 to E08000019. Zero unresolved codes.
