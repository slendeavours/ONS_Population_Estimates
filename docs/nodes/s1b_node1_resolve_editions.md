# S1b Node 1: Resolve A3 Editions from the Release Pages

- **Type:** HTTP GET (GOV.UK Content API)
- **Purpose:** For each of the eleven quarters, resolve the detailed local authority tables attachment that MHCLG currently publishes, and record which edition variant it is.
- **Credential:** None (public).

## Source URL

The collection is the stable entry point; file URLs are never constructed.

```
https://www.gov.uk/government/collections/homelessness-statistics
```

Each quarter resolves through the Content API by release slug:

```
https://www.gov.uk/api/content/government/statistics/statutory-homelessness-in-england-{slug}
```

| Period | Reference quarter | Slug |
|---|---|---|
| 2023Q2 | 2023-09 | `july-to-september-2023` |
| 2023Q3 | 2023-12 | `october-to-december-2023` |
| 2023Q4 | 2024-03 | `january-to-march-2024` |
| 2024Q1 | 2024-06 | `april-to-june-2024` |
| 2024Q2 | 2024-09 | `july-to-september-2024` |
| 2024Q3 | 2024-12 | `october-to-december-2024` |
| 2024Q4 | 2025-03 | `january-to-march-2025` |
| 2025Q1 | 2025-06 | `april-to-june-2025` |
| 2025Q2 | 2025-09 | `july-to-september-2025` |
| 2025Q3 | 2025-12 | `october-to-december-2025` |
| 2025Q4 | 2026-03 | `january-to-march-2026` |

`period` is the pipeline's financial-year quarter key, matching
`la_statutory_homelessness`, where 2023Q2 is July to September 2023.
`reference_quarter` is the publisher's calendar quarter end.

## Logic

1. GET the Content API document for the release.
2. Filter `details.attachments` to those whose filename matches
   `detailed_la` or `detailed_local_authority`, case-insensitively.
3. Exclude Multiple Disadvantage (`MDIS`, `multiple_disadvantage`) files —
   a different table.
4. Exclude `accessible` variants — the same data in a second rendering, which
   would double-count if both were taken.
5. Rank the survivors `corrected` (4) > `revised` (3) > `fix`/`fixed` (2) >
   `original` (1) and take the highest.
6. Record the URL, filename, variant, and the release page URL.
7. Download to `data/raw/s1b_a3/{period}_{filename}`, skipping if present.

## Why the URL register is not used

`homelessness_quarter_urls` records `_revised` assets for four quarters that
GOV.UK still serves but no longer links from any release page. It says what
was fetched once, not what is published now, and the two have diverged. The
operating rule is to resolve, never to hardcode; this node resolves.

## Behaviour

- **Conflict handling:** Idempotent. Cached files are reused; re-resolution
  returns the same attachment unless the publisher has replaced it.
- **Re-run safety:** Safe. Nothing is written to the database by this node.
- **Failure:** Halts if a release has no detailed local authority attachment,
  rather than falling back to a neighbouring quarter.

## Connection

HTTPS to `www.gov.uk` and `assets.publishing.service.gov.uk`.
User-Agent `ucws-pipeline/s1b (+sl@slendeavours.org)`.

## Verified Output

Eleven editions resolved, three of them not the original:

| Period | Variant | File |
|---|---|---|
| 2023Q2 | fixed | `Detailed_LA_202309_fixed.ods` |
| 2023Q3 | original | `Detailed_LA_202312.ods` |
| 2023Q4 | original | `Detailed_LA_202403.xlsx` |
| 2024Q1 | fixed | `Detailed_LA_202406_fix.xlsx` |
| 2024Q2 | original | `Detailed_LA_202409.xlsx` |
| 2024Q3 | original | `Detailed_LA_202412.ods` |
| 2024Q4 | original | `Detailed_LA_202503.ods` |
| 2025Q1 | corrected | `Statutory_Homelessness_Detailed_Local_Authority_Data_202506_corrected.ods` |
| 2025Q2 | original | `Statutory_Homelessness_Detailed_Local_Authority_Data_202509.ods` |
| 2025Q3 | original | `Statutory_Homelessness_Detailed_Local_Authority_Data_202512.ods` |
| 2025Q4 | original | `Statutory_Homelessness_Detailed_Local_Authority_Data_202603.ods` |

Verified 2026-08-14.
