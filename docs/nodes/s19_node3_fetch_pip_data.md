# Node 3 — Fetch PIP Data

## Type
HTTP POST (Stat-Xplore REST API `/table`)

## Purpose
Fetch PIP caseload data for all 296 English LAs: total claimants and enhanced daily living claimants for the latest available month.

## Credential
DWP Stat-Xplore API key in `ApiKey` header (same as Node 1).

## Query / Code / URL
```
POST https://stat-xplore.dwp.gov.uk/webapi/rest/v1/table
```
Two query series using the recodes pattern:
1. **Total caseload** — measure × geography × date (no daily living filter)
2. **Enhanced daily living** — measure × geography × date × daily living award type (Enhanced only)

Representative query bodies saved to `scripts/`: `s19_query_total.json`, `s19_query_enhanced_dl.json`.

## Logic
1. Build query with explicit member URI maps in `recodes`; dimensions reference field IDs only (not valueset URIs — avoids DUPLICATE_RECODES error)
2. Batch LAs into groups of 15 (larger batches cause 504 timeouts)
3. 3-second pause between batches
4. Parse response: each batch returns a cube with values indexed by field items
5. Extract LA code from the member URI suffix
6. Repeat for enhanced daily living with an additional recode on the DL award type field
7. Resolve to LAD24CD: for each LAD, sum values from all mapped source codes (handles historical-code summing if needed)

## Parameters
- `BATCH_SIZE`: 15 LAs per API call
- Retry: 5 attempts with exponential backoff (`10 × 2^attempt` seconds) on 500/502/503/504
- Timeout: 120 seconds per request
- Pause: 3 seconds between batches

## Behaviour
- 20 batches per query series (296 LAs ÷ 15 = 20, last batch has 11)
- 40 total API calls for both series
- Stat-Xplore frequently returns 504 Gateway Timeout; all recovered on first retry during the initial build
- DWP annotation captured: `".."` denotes nil or negligible claimant counts (below DWP's rounding threshold)

## Verified Output
- 296 total caseload values parsed
- 296 enhanced daily living values parsed
- 296 LAD rows resolved
- Sample values: E06000053 (Isles of Scilly): 46 total; E07000143 (Breckland): 8,762 total; E08000025 (Birmingham): 93,196 total
- Verified 2026-07-16 (initial build)
