# Node 2 — Resolve Geography

## Type
Database query + set comparison

## Purpose
Map the 296 English LA codes from the Stat-Xplore schema to the pipeline's canonical LAD24CD codes. Determines whether direct, historical-code-summing, or fallback geography is needed.

## Logic
1. Extract English LA codes (E-prefixed) from the discovered valueset members
2. Check each code against `la_boundaries.lad24cd` for a direct match
3. For unmatched codes, check `la_code_lookup.old_code → new_code` for historical mappings
4. Classify coverage: direct match count, historical match count, unresolvable count
5. Assign confidence rating:
   - **High**: 296/296 direct or direct+historical
   - **Medium**: 250–295 resolved
   - **Low**: <250 resolved
6. Build `lad_to_uris` mapping (LAD24CD → list of member URIs for query construction)

## Parameters
- Expected coverage: 296 English LAs
- Hard stop if any codes are unresolvable (unknown codes are a stop, not a mapping exercise)

## Behaviour
- Read-only on the database — never inserts into `la_code_lookup`
- If historical codes are found, multiple source URIs map to a single LAD24CD; values are summed in Phase 3
- Confidence rating is stored in table and column comments

## Connection
Reads from: `la_boundaries` (lad24cd), `la_code_lookup` (old_code, new_code)

## Verified Output
- 296 English codes extracted
- Direct: 296, Historical: 0
- Coverage: 296/296 (100.0%)
- Confidence: High
- Census 2021 MASTERGEOG21 geography — current LAD24 codes including 2023 LGR
- Verified 2026-07-16 (initial build)
