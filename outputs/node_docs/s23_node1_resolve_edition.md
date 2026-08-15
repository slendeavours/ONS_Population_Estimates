# S23 Node 1: Resolve the RSH Look-up Tool Edition

- **Type:** HTTP GET (GOV.UK Content API) + download
- **Purpose:** Resolve the current registered providers look-up tool workbook from the release page, and derive the stock date, publication date and edition identifier from the publisher rather than from assumption.
- **Credential:** None (public).

## Source URL

```
https://www.gov.uk/government/statistics/registered-provider-social-housing-stock-and-rents-in-england-2024-to-2025
```

Resolved through the Content API:

```
https://www.gov.uk/api/content/government/statistics/registered-provider-social-housing-stock-and-rents-in-england-2024-to-2025
```

**The landing page URL is not stable.** It carries the edition years, so it
changes annually and must be updated when a new edition lands. This is why the
source sits at refresh tier B: detection is automatable, ingestion is gated.

## Logic

1. GET the Content API document.
2. Find the attachment whose URL matches `COMBINED_TOOL`, case-insensitively.
   Halt if none — do not fall back to the additional tables, which are a
   different file with a different grain.
3. Parse the edition years out of `base_path` with `(\d{4})-to-(\d{4})`.
4. Derive:
   - `edition` — `"2024 to 2025"`
   - `stock_date` — `{closing year}-03-31`, because the return is a snapshot
     at 31 March
   - `publication_date` — `first_published_at`
   - `release_page_url` — `https://www.gov.uk` + `base_path`
5. Download to `data/raw/s23_rsh/`, skipping if present.

## Why both dates are stored

The return is a snapshot at **31 March**; publication follows roughly seven
months later. The 2024 to 2025 edition describes 31 March 2025 and was
published 28 October 2025, so before the next edition lands the newest
available figure is up to nineteen months old.

A stock figure read as though it were current is wrong by up to a year. Two
columns make that impossible to miss; one date would have hidden it.

## Behaviour

- **Conflict handling:** Idempotent. Cached file reused.
- **Re-run safety:** Safe. Writes nothing to the database.
- **Failure:** Halts if the look-up tool is absent or the edition years cannot
  be read from the path.

## Connection

HTTPS to `www.gov.uk` and `assets.publishing.service.gov.uk`.
User-Agent `ucws-pipeline/s23 (+sl@slendeavours.org)`.

## Verified Output

| Field | Value |
|---|---|
| edition | 2024 to 2025 |
| stock_date | 2025-03-31 |
| publication_date | 2025-10-28 |
| file | `RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx` (2,398,227 bytes) |
| release page | `https://www.gov.uk/government/statistics/registered-provider-social-housing-stock-and-rents-in-england-2024-to-2025` |

Verified 2026-08-14.
