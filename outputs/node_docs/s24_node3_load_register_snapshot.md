# S24 Node 3: Load the Monthly Register Snapshot

- **Type:** Code + Postgres upsert
- **Purpose:** Load every current registered provider as one row per provider per snapshot date, so month-on-month change is answerable from the table.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`.

## Expected header

Asserted exactly; any change halts the build.

```
Organisation name | Registration number | Registration date | Designation |
Corporate form | Notes
```

That is the whole of what RSH publishes. No address, no contact details, no
stock figure.

## The hidden sheet

The workbook carries a second, hidden sheet holding what appears to be a stray
account token from the publisher's own tooling. **It is not read and nothing
from it is stored.** The sheet selector matches on the "Registered Providers"
title rather than taking the first sheet, and `data/raw/` is gitignored so the
raw workbook is never committed.

## Logic

1. Read the register sheet, asserting the header.
2. Keep rows with a non-empty first column.
3. Assert every registration number is unique within the snapshot; a duplicate
   halts.
4. Emit one row per provider carrying `snapshot_date` from node 1 and the full
   provenance set.

## Query

```sql
INSERT INTO rsh_registered_providers (
    snapshot_date, registration_number, organisation_name, registration_date,
    designation, corporate_form, notes, source_url, source_file,
    release_page_url)
VALUES %s
ON CONFLICT (snapshot_date, registration_number) DO UPDATE SET
    organisation_name = EXCLUDED.organisation_name,
    registration_date = EXCLUDED.registration_date,
    designation       = EXCLUDED.designation,
    corporate_form    = EXCLUDED.corporate_form,
    notes             = EXCLUDED.notes,
    source_url        = EXCLUDED.source_url,
    source_file       = EXCLUDED.source_file,
    release_page_url  = EXCLUDED.release_page_url,
    loaded_at         = now();
```

## Change detection — the reason for the schema

The register page carries only the current month, so history exists only
because this table keeps every snapshot. De-registrations between two
snapshots:

```sql
SELECT p.registration_number, p.organisation_name
FROM rsh_registered_providers p
WHERE p.snapshot_date = %s
  AND NOT EXISTS (
    SELECT 1 FROM rsh_registered_providers q
    WHERE q.snapshot_date = %s
      AND q.registration_number = p.registration_number)
ORDER BY p.organisation_name;
```

New registrations are the same query with the dates reversed.

**De-registration is an absence, not an event.** The snapshot lists current
providers only, so a de-registration has no published date of its own and is
inferred from two snapshots. That is a limitation of the source, stated rather
than papered over. RSH does publish annual registrations and de-registrations
notes as HTML pages, which would give dates; they are not loaded here.

## Behaviour

- **Conflict handling:** Upsert on `(snapshot_date, registration_number)`.
  Re-running the same month corrects it in place; a new month adds rows
  without touching the previous snapshot.
- **Re-run safety:** Idempotent, proved by gate 6.
- **Failure:** One transaction with nodes 4 and 6.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

Snapshot 2026-07-24: 1,579 providers.

| Designation | Count |
|---|---:|
| Non-profit | 1,260 |
| Local authority | 232 |
| Profit | 87 |

Verified 2026-08-14. Only one snapshot is loaded, so no month-on-month
comparison is possible yet; the second monthly run is what makes the change
query meaningful.
