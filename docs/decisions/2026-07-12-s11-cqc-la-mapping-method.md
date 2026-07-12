# S11 CQC locations must be mapped to districts spatially, not by the file's LA name

Date: 2026-07-12
Status: accepted

## Context

The CQC Care directory with filters carries a `Location Local Authority` column, which looks like the obvious join key to the pipeline's 296 LAD24CD districts. Profiling the July 2026 file showed it holds only 155 distinct names: county names like Suffolk, Kent and Hampshire appear while their districts (Ipswich, Ashfield) do not. It is an upper-tier field of unknown vintage.

## Decision / root cause

Point-in-polygon join of each location's ONSPD-derived lat/long into the full `la_boundaries` polygons is the primary method (the S14 centroid-into-BRMA pattern inverted). Tiered fallbacks, each recorded in `mapping_method`: nearest polygon for coastal points (2 rows, both under 700 m), postcodes.io for rows without coordinates (3 rows), and the terminated-postcodes endpoint for dead postcodes that still carry coordinates (1 row). Every code is validated against `la_boundaries` before storage, with `la_code_lookup` reconciliation for historical codes. Name matching was used only as a cross-check: 19,517 of 19,519 comparable rows agreed with the spatial assignment, and both disagreements were boundary-adjacent postcodes, which confirms the spatial route rather than undermining it.

Five rows (0.016%) had postcodes unknown even to the terminated endpoint and were excluded rather than guessed. They are listed in the Node 3 doc and should re-resolve once ONSPD catches up.

## Consequences

Any S11 refresh must run the spatial mapping; joining on the LA name column will silently collapse districts into counties. If a future file drops lat/long, the postcode fallbacks become the primary route and the run should be re-verified from scratch. If the unmapped count ever grows past single digits, check whether CQC's new digital system has changed the coordinate or postcode columns before blaming ONSPD lag.
