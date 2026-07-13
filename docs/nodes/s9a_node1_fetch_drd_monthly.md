# S9a Node 1: Fetch DRD Monthly Files

- **Type:** HTTP Download
- **Purpose:** Download NHS England Discharge Ready Date (DRD) monthly data webfiles for all reporting periods from April 2024 to the latest available month.
- **Credential:** None (publicly available).

## Source URL

Each monthly file is discovered from the live publication page:

```
https://www.england.nhs.uk/statistics/statistical-work-areas/discharge-delays/discharge-delays-acute-data/
```

File URLs follow the pattern `https://www.england.nhs.uk/statistics/wp-content/uploads/sites/2/YYYY/MM/Discharge-Ready-Date-monthly-data-webfile-MonthName-YYYY[-Revised].xlsx` but **must be discovered from the live page**, never constructed — URLs include a publication-date path component that varies.

## Logic

1. Scrape the DRD acute data publication page for all monthly file download links.
2. Download each `.xlsx` file for the target date range (April 2024 onward).
3. Files are cached locally; re-downloads are skipped if a file of >10 KB already exists.

## Behaviour

- **Conflict handling:** Idempotent — re-downloading the same file overwrites with identical content.
- **Re-run safety:** Safe to re-run; cached files prevent unnecessary downloads.

## Connection

HTTPS to `www.england.nhs.uk`.

## Verified Output

- 26 monthly files downloaded (April 2024 – May 2026).
- File sizes range from ~1.07 MB to ~1.27 MB each.
- Verified 2024-07-13.
