# Node 1 — Discover PIP Schema

## Type
HTTP GET + JSON parse (Stat-Xplore REST API `/schema`)

## Purpose
Walk the Stat-Xplore schema tree programmatically to discover every ID needed for table queries. Nothing is hardcoded from memory; the script selects the correct database, measure, geography valueset, daily living dimension, and latest available month from the live API.

## Credential
DWP Stat-Xplore API key, loaded from `.env` (`Stat-Xplore_Token`). Sent in the `ApiKey` request header only — never logged, printed, or written to file.

## Query / Code / URL
```
GET https://stat-xplore.dwp.gov.uk/webapi/rest/v1/schema
```
Recursive descent through `children` arrays. Each child has a `type` (DATABASE, COUNT, MEASURE, FIELD, GROUP, VALUESET, VALUE) used to select the right branches.

## Logic
1. Enumerate PIP databases; select "PIP Cases with Entitlement **from** 2019" (`str:database:PIP_Monthly_new`)
2. Find the COUNT measure (`str:count:PIP_Monthly_new:V_F_PIP_MONTHLY`)
3. Locate the geography GROUP → geography FIELD → enumerate valuesets → select "Local Authority" valueset
4. Paginate through LA valueset members (API paginates at 100; follows `Link: rel="next"` headers) → filter to 296 English codes (E-prefixed)
5. Locate the Daily Living Award Type field → select "Enhanced" member
6. Locate the Date field → select the most recent month member

## Parameters
- `API_ROOT`: `https://stat-xplore.dwp.gov.uk/webapi/rest/v1`
- Throttle: minimum 1 second between schema requests
- Pagination: automatic via HTTP `Link` header

## Behaviour
- Deterministic given the same API state
- All schema responses cached locally in `s19_cache/` (gitignored, re-fetchable)
- Discovery result saved as `s19_cache/discovery.json` — used as a checkpoint by subsequent phases
- If `discovery.json` exists, Phase 1 is skipped entirely on re-run

## Verified Output
- Database: PIP Cases with Entitlement from 2019 (`str:database:PIP_Monthly_new`)
- Measure: `str:count:PIP_Monthly_new:V_F_PIP_MONTHLY`
- LA valueset: `str:valueset:PIP_Monthly_new:V_F_PIP_MONTHLY:COA_CODE:V_C_MASTERGEOG21_LA_TO_REGION` — 352 total members, 296 English
- Enhanced DL member: `str:value:PIP_Monthly_new:V_F_PIP_MONTHLY:DL_AWARD_TYPE:C_PIP_DL_AWARD_TYPE:1`
- Latest month: Apr-26 (`str:value:PIP_Monthly_new:F_PIP_DATE:DATE2:C_PIP_DATE:202604`)
- Date valueset: 88 periods available
- Verified 2026-07-16 (initial build)
