# Node 3 — Resolve Geography

## Type
Database read + code-first resolution cascade

## Purpose
Map every published LAD code to a live LAD24CD. The Home Office publishes
pre-2023 district codes for historical periods, so forward resolution is
required across most of the window.

## Credential
`exempt_pipeline` as `PG_USER` from `.env`. Read-only in this node.

## Query
```sql
SELECT lad24cd, lad24nm FROM la_boundaries;
SELECT old_code, new_code FROM la_code_lookup;
```

## Code
```python
BUILD_LOCAL_RECODES = {
    "E07000028": "E06000063",  # Carlisle           -> Cumberland
    "E07000189": "E06000066",  # South Somerset     -> Somerset
    "E07000027": "E06000064",  # Barrow-in-Furness  -> Westmorland and Furness
}

def resolve(self, code):
    code = (code or "").strip()
    if code in self.boundaries:
        return code, "code_direct"
    if code in BUILD_LOCAL_RECODES:
        return BUILD_LOCAL_RECODES[code], "build_local_pending_remediation"
    target = self.lookup.get(code)
    if target and target in self.boundaries:
        return target, "code_historical_forward"
    return None, "unresolved"
```

## Logic
1. **Method 1** — direct match against `la_boundaries.lad24cd`.
2. **Build-local layer** — three codes `la_code_lookup` handles wrongly or not
   at all, applied *ahead* of the lookup so the wrong Barrow mapping cannot win.
3. **Method 2** — forward resolution through `la_code_lookup.old_code →
   new_code`, accepted only if the target exists in `la_boundaries`.
4. **Methods 3–5** — exact name, normalised name, then historical name. Present
   in the design for robustness; never reached in practice because 99.64% of
   in-scope rows carry a usable code and the remainder are unallocated by
   construction.
5. Rows whose code begins `S12`, `W06` or `N09` are routed to
   `asylum_support_non_england` rather than resolved.
6. Any unresolved English code is a **hard stop**. Unknown codes are a stop, not
   a mapping exercise.

## Query Parameters

| Parameter | Value |
|---|---|
| Live LAD source | `la_boundaries` (296 rows) |
| Historical mappings | `la_code_lookup` (331 rows) |
| English prefixes | `E06`, `E07`, `E08`, `E09` |
| Non-England prefixes | `S12` → Scotland, `W06` → Wales, `N09` → Northern Ireland |

## Behaviour
- **Never writes to `la_code_lookup`.** Correcting a shared table as a side
  effect of loading one source would change other sources' geography without
  their verification suites running.
- Where several predecessor districts resolve onto one successor unitary,
  multiple source rows collapse onto a single natural key. Node 4 sums them.
- Match method is retained per resolution so
  `build_local_pending_remediation` can be counted and reported.

## Connection
Reads from: `la_boundaries`, `la_code_lookup`.
Output: `resolve()` callable used by Nodes 4 and 5.

## Verified Output

| Method | Distinct pairs |
|---|---:|
| 1 — `code_direct` | 292 |
| 2 — `code_historical_forward` | 14 |
| 3–5 — name-based | 0 |
| non-England | 61 |
| unallocated | 3 |
| **unresolved** | **0** |

- 16 pre-2023 district codes plus the two Barnsley/Sheffield recodes resolve forward
- England coverage 292/296 reached at least once, 286/296 at 2026-03-31
- Verified 2026-07-25 (initial build)

## Known dependency
`la_code_lookup` maps E07000027 (Barrow-in-Furness) to E06000063 Cumberland,
which is wrong — the correct successor is E06000064 Westmorland and Furness —
and has no row for E07000028 (Carlisle) or E07000189 (South Somerset). Verified
against the ONS area pages for E06000063, E06000064 and E06000066, plus the
Cumbria and Somerset (Structural Changes) Orders 2022.

**Until remediation lands the database is inconsistent across sources on
Cumberland and Westmorland and Furness, and S6 is the only correct one.** See
[`decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md`](../decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md)
for the retirement procedure.
