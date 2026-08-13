# Node 3 — Extract Table 615 and Resolve Geography

## Type

Code (Python). `scripts/s22_ctb_empties_build.py`, functions `extract_table_615` and `resolve_615_geography`.

## Purpose

Turn the two Table 615 sheets into a long district-year series, and label each published code as `direct`, `resolved_via_lookup` or `unmapped` without aggregating abolished districts into their successors.

## Credential

Postgres `exempt_pipeline` (read only, for `la_boundaries` and `la_code_lookup`).

## Query / Code / URL (full content)

```python
def extract_table_615(src_b):
    frames = {}
    for sheet, field in (("All_vacants", "vacant_dwellings"),
                         ("All_long_term_vacants",
                          "long_term_vacant_dwellings")):
        df = pd.read_excel(src_b["path"], sheet_name=sheet, engine="odf",
                           header=None)
        header = df.iloc[2].tolist()
        years = {i: _year_from_header(header[i])
                 for i in range(2, len(header))}
        if not any(years.values()):
            disco.halt(f"Table 615 sheet {sheet}: no year headers parsed — "
                       "structure has changed")
        rec = {}
        for _, row in df.iloc[3:].iterrows():
            code = str(row[0]).strip()
            if not re.match(r"^E0[6789]\d{6}$", code):
                continue
            name = str(row[1]).strip()
            for i, yr in years.items():
                if yr is None:
                    continue
                v = _num(row[i])
                if v is None:
                    continue
                rec[(code, yr)] = (name, v)
        frames[field] = rec
    ...
```

Suppressed cells:

```python
def _num(v):
    if isinstance(v, str):
        if v.strip() in ("[x]", "[c]", "[z]", "[w]", ":", "-", ""):
            return None
        ...
```

Geography:

```python
def resolve_615_geography(conn, rows):
    """direct | resolved_via_lookup | unmapped, via la_code_lookup only."""
    cur = conn.cursor()
    cur.execute("SELECT lad24cd FROM la_boundaries")
    current = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT old_code, new_code, change_type FROM la_code_lookup")
    lookup = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    for r in rows:
        code = r["published_la_code"]
        if code in current:
            r["lad24cd"], r["mapping_status"] = code, "direct"
            continue
        hit = lookup.get(code)
        if hit and hit[1] == "recode" and hit[0] in current:
            r["lad24cd"], r["mapping_status"] = hit[0], "resolved_via_lookup"
            continue
        r["lad24cd"], r["mapping_status"] = None, "unmapped"
    return rows
```

## Logic (step by step)

1. Read `All_vacants` and `All_long_term_vacants`. Header row is row 3; each data column header is that year's snapshot date.
2. Parse a year from each column header, accepting either a `dd/mm/yyyy` string or a real date value.
3. Keep only rows whose code matches `E06`, `E07`, `E08` or `E09`. England (`E92000001`) and the nine regions (`E12...`) are excluded — they are not districts.
4. Treat `[x]` and the other published suppression markers as absent, not as zero. A district with no value for a year produces no row rather than a row of zero.
5. Outer-join the two sheets on `(code, year)` so a district present in one sheet and not the other still produces a row with the other measure null.
6. Resolve geography against `la_boundaries` first: a code that is already a current LAD is `direct`.
7. Otherwise consult `la_code_lookup`. Only `change_type = 'recode'` resolves — a renumbering of the same area. That covers the 1 April 2025 Barnsley and Sheffield recodes.
8. Everything else is `unmapped` with a null `lad24cd`. That includes `new_unitary` and `merger` entries the lookup does hold. **Abolished districts are deliberately not folded into successor unitaries**: mapping six Somerset districts onto E06000066 would make any downstream sum count Somerset six times over.
9. Nothing is written back to `la_code_lookup`.

## Behaviour

Pure extraction and labelling, no writes. Halts if no year header can be parsed from either sheet. Suppression and reorganisation are distinguished in the output: a suppressed cell yields no row, an abolished district yields a row with `mapping_status = 'unmapped'`.

## Connection

- Input: Node 1 (Discover Source Files)
- Output: Node 5 (Create Tables)

## Verified Output

2026-08-13. 7,170 district-year rows, years 2004 to 2025. `direct` 6,277 rows across 296 codes; `unmapped` 891 rows across 80 codes, all years 2004 to 2022; `resolved_via_lookup` 2 rows, both 2025 (Barnsley, Sheffield).
