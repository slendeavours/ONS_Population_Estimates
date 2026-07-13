# S9b Node 1: Fetch MHSDS Monthly Data Files

- **Type:** HTTP Download (two-stage: page scrape + file download)
- **Purpose:** Download NHS Digital MHSDS monthly data files containing MHS26 CRFD measure for all reporting periods from April 2023 to the latest available month.
- **Credential:** None (publicly available).

## Source URL

Each monthly file is discovered from its publication page on NHS Digital:

```
https://digital.nhs.uk/data-and-information/publications/statistical/mental-health-services-monthly-statistics/performance-{month}-{year}
```

Older publications (pre-October 2023) use the slug format `performance-{month}-provisional-{next_month}-{year}`.

The data file download URL is on `files.digital.nhs.uk` with unpredictable hash paths — **must be discovered from the publication page HTML**, never constructed.

## Logic

1. For each target month, fetch the publication page HTML.
2. Extract the MHSDS Data performance file URL using regex (matches `MHSDS%20Data_*Prf*` or `MHSDS%20Data_*Perf*` patterns in `.zip` or `.csv` format).
3. Exclude Final, Restrictive Interventions, OAPs, ASCOF, and 4WW variants.
4. Download the file (ZIP or CSV). If ZIP, extract the data CSV.
5. Cache locally; re-downloads skipped if file exists.

## File Naming Variations

| Period | Format | Example |
|---|---|---|
| Apr–Aug 2023 | CSV with `%20v` suffix | `MHSDS%20Data_AprPrf_2023%20v3.csv` |
| Sep–Dec 2023 | CSV, no version | `MHSDS%20Data_SepPrf_2023.csv` |
| Jan 2024+ | ZIP containing CSV | `MHSDS%20Data_JanPrf_2024.zip` |
| Jul 2025 | ZIP with long name | `MHSDS%20Monthly%20Performance%20July%202025%20MHSDS%20Data%20File.zip` |

## Encoding

April 2023 file contains non-UTF-8 bytes (0xA0 non-breaking spaces). Opened with `utf-8-sig` encoding and `errors='replace'` fallback.

## Verified Output

- 38 monthly files downloaded (April 2023 – May 2026).
- File sizes range from ~68 MB to ~176 MB (uncompressed CSV).
- Verified 2024-07-13.
