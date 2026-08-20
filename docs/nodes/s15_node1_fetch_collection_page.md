# Node 1 — Fetch Collection Page

## Type
Python script task

## Purpose
Resolve the most recent UK HPI data downloads edition from the GOV.UK collections page.

## Logic
1. Fetch `https://www.gov.uk/government/collections/uk-house-price-index-reports`
2. Extract the first link matching `/government/statistical-data-sets/uk-house-price-index-data-downloads-*`
3. This gives the most recent edition's download page URL

## Key parameters
| Parameter | Value |
|---|---|
| Collections URL | `https://www.gov.uk/government/collections/uk-house-price-index-reports` |
| Link pattern | `/government/statistical-data-sets/uk-house-price-index-data-downloads-*` |

## Behaviour
Idempotent — always resolves to the latest published edition. No state stored.

## Verified output
April 2026 edition resolved on 2026-07-14.
