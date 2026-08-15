# S24 Node 4: Load Regulatory Judgements and Enforcement Notices

- **Type:** Code + Postgres upsert
- **Purpose:** Load the gradings table and the enforcement notices sheet, keeping "not assessed" distinguishable from a grade.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`.

## Expected headers

Both asserted exactly; any change halts.

**Regulatory Judgements** (21 columns):

```
Reg Code | Landlord | Landlord Type | Name and Reg Code Change Details |
Other landlords included in the judgement | Status | Consumer grade |
Consumer Grade Change | Consumer Grade Date | Governance Grade |
Governance Grade Change | Governance Date | Viability Grade |
Viability Grade Change | Viability Grade Date | Rent | Rent Date |
Rent Change | Type of Publication | Publication Date | Engagement Process
```

**Enforcement Notices** (10 columns):

```
Reg Code | Provider | Name and Reg Code Change Details |
Other providers included in the notice | Status | Type of Publication |
Publication Date | Route | Explanation | Date of Enforcement Notice
```

Note the column order in the source: `Rent` (15), `Rent Date` (16),
`Rent Change` (17) — date before change, the opposite of the other three
grades. The extractor reads them positionally by name, so the inconsistency is
handled rather than propagated.

## Unassessed grades

RSH writes `-` where a grade has not been assessed:

```python
def text(v):
    s = str(v).strip() if v is not None else ""
    return None if s in ("", "-", "None") else s
```

Stored as NULL — never an empty string, never a literal `-` — so "not
assessed" stays distinguishable from a real grading. The paired change
description ("Not assessed yet", "Assessed and unchanged") is retained, so the
reason survives alongside the null.

Local authority providers receive **consumer gradings only**, so governance
and viability are legitimately null for all 100 of them rather than missing.

## Excel serial dates

Some grade dates arrive as raw serials rather than typed dates, because the
publisher's workbook leaves them unformatted:

```python
if isinstance(v, (int, float)) and not isinstance(v, bool):
    if not 20000 <= v <= 60000:
        halt(...)
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(v))).isoformat()
```

The range guard stops a stray count being read as a date. This was found in
testing: the first run halted on `45987`, which is 2025-11-26.

## Query

```sql
INSERT INTO rsh_regulatory_judgements (
    registration_number, publication_date, landlord_name, landlord_type,
    status, consumer_grade, consumer_grade_change, consumer_grade_date,
    governance_grade, governance_grade_change, governance_grade_date,
    viability_grade, viability_grade_change, viability_grade_date,
    rent_grade, rent_grade_change, rent_grade_date, publication_type,
    engagement_process, name_or_code_change, other_landlords, edition_date,
    source_url, source_file, release_page_url)
VALUES %s
ON CONFLICT (registration_number, publication_date) DO UPDATE SET ...;
```

Enforcement notices follow the same shape on
`(registration_number, publication_date)`.

## Identity caveat

A registration code is not a stable identity. `L4331` appears under
"Chelmer Housing Partnership Limited" (published 2025-11-26) and
"Delta Housing Limited" (published 2026-07-29). The publisher's "Name and Reg
Code Change Details" column is stored verbatim in `name_or_code_change`
rather than resolved, because resolving it would be inventing a history RSH
does not publish.

## Behaviour

- **Conflict handling:** Upsert on the composite key.
- **Re-run safety:** Idempotent, proved by gate 6.
- **Failure:** One transaction with nodes 3 and 6.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

Judgements edition 2026-08-12.

- 308 judgements, 305 distinct providers, 308 distinct composite keys.
- 208 private registered providers, 100 local authorities.
- All 308 `Status = Current`.
- 200 carry a governance grade; 108 do not.
- Grades seen: consumer C1/C1\*/C2/C2\*/C3/C4, governance G1/G1\*/G2/G2\*/G3,
  viability V1/V1\*/V2/V3.
- 600 `-` markers in the source grade columns; 600 NULLs stored; 0 empty
  strings; 0 literal dashes.
- 2 enforcement notices, both Economic Standards via Reactive Engagement.

Verified 2026-08-14.
