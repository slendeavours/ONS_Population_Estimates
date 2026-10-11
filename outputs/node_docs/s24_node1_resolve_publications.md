# S24 Node 1: Resolve the Register and Judgements Publications

> **Superseded 2026-10-11.** The old S24 scripts this note describes (`s24_rsh_register_build.py`, `s24_rsh_register_verify.py`) loaded the register and judgements twice (run-log ids 96 and 101) and are retired to `scripts/historical/`, where they stop with a RETIRED message. S24 is now loaded by `scripts/s24_rsh_register_editions.py`, which reads each file's identity from the file and the publisher's page and keeps the register per snapshot and the judgements and notices per published edition, with the reason for every NULL (`rsh_registered_providers_editions`, `rsh_regulatory_judgements_editions`, `rsh_enforcement_notices_editions`). The source note is `docs/s24_rsh_register_source.md`. The text below is left as written and describes the old method.

- **Type:** HTTP GET (GOV.UK Content API) + download
- **Purpose:** Resolve two separate publications — the monthly register snapshot and the regulatory judgements table — and take each one's date from the publisher rather than from the run date.
- **Credential:** None (public).

## Source URLs

Two pages, not one:

```
https://www.gov.uk/government/publications/registered-providers-of-social-housing
https://www.gov.uk/government/publications/regulatory-judgements-and-enforcement-notices
```

Resolved through `https://www.gov.uk/api/content` + the base path.

## Logic — register

1. GET the Content API document for the register page.
2. Take the first `.xlsx` attachment whose title carries a parseable
   `{day} {Month} {year}`.
3. **`snapshot_date` comes from the attachment title, not from today.** The
   file is published around mid-month; a run on any later day must still
   record the publisher's date, or the change-detection history acquires a
   date the publisher never used.
4. Download to `data/raw/s24_rsh/`.

## Logic — judgements

1. GET the Content API document for the judgements page.
2. Take the first `.xlsx` attachment.
3. `edition_date` is parsed from the `YYYYMMDD` filename prefix, falling back
   to `public_updated_at` if the prefix is absent.
4. Download to `data/raw/s24_rsh/`.

## What discovery established

The build brief allowed for regulatory judgements being available only as
individual documents per provider, in which case they were to be recorded as a
limitation rather than built. **They are not.** The judgements page carries a
machine-readable table with grades, grade dates and change descriptions, plus
a second sheet of enforcement notices. So the gradings tables were built.

The register page carries **only the current month**. There is no archive of
past snapshots, which is why node 3 stores snapshots rather than a
current-state table — history exists only because this pipeline keeps it.

## Behaviour

- **Conflict handling:** Idempotent. Cached files reused.
- **Re-run safety:** Safe. Writes nothing to the database.
- **Failure:** Halts if either publication has no dated spreadsheet.

## Connection

HTTPS to `www.gov.uk` and `assets.publishing.service.gov.uk`.
User-Agent `ucws-pipeline/s24 (+sl@slendeavours.org)`.

## Verified Output

| Publication | Date | File | Bytes |
|---|---|---|---|
| Register snapshot | 2026-07-24 | `Copy_of_List_of_registered_providers_24_July_2026.xlsx` | 90,458 |
| Judgements | 2026-08-12 | `20260812_RegulatoryJudgementsNotices_Published.xlsx` | 82,398 |

Verified 2026-08-14.
