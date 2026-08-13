# Node 2 — Fetch Data Downloads Page

## Type
Python script task

## Purpose
Extract the two CSV download URLs from the edition's data downloads page.

## Logic
1. Fetch the data downloads page identified in Node 1
2. Extract the URL matching `Average-prices-{YYYY}-{MM}.csv` (all-property prices)
3. Extract the URL matching `Average-prices-Property-Type-{YYYY}-{MM}.csv` (prices by property type)
4. Reject any CSV under 1 KB (indicates redirect or error page)

## Key parameters
| Parameter | Value |
|---|---|
| File 1 pattern | `Average-prices-{YYYY}-{MM}.csv` |
| File 2 pattern | `Average-prices-Property-Type-{YYYY}-{MM}.csv` |
| Host | `publicdata.landregistry.gov.uk` |
| Min file size | 1 KB |

## Behaviour
Idempotent. URLs change with each edition but the pattern is stable.

## Verified output
Two CSVs extracted for April 2026 edition (7.2 MB and 16 MB). 2026-07-14.
