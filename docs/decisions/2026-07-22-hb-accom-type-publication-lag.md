# Stat-Xplore HB accommodation type breakdown lags ~5 months behind current date

**Date:** 2026-07-22
**Status:** accepted

## Context

The S8b build (`la_hb_accom_type_caseload`) loaded HB caseload accommodation type data (SA/TA/OTHER/UNKNOWN, via `V_F_HB_NEW:SATA`) for the six months Stat-Xplore had available: September 2025 through February 2026. At build time (22 July 2026) that meant the newest data point was already ~5 months old — longer than the 2-3 month lag typical of DWP admin data — which raised the question of whether the discovery script had truncated the available date range rather than genuinely hit the ceiling.

## Decision / root cause

Confirmed genuine, not a script fault. Two direct API checks:

1. Queried the general HB caseload date valueset with no SATA dimension applied. It returned 95 date members, all in a single response with no pagination markers (no `Link` header, no `X-Total-Count`, no `hasMore`). Latest month overall: 202602 (Feb-26) — same ceiling as the SATA breakdown. Confirmed via a live query (Birmingham total HB caseload, Feb-26: 59,862 claims).
2. Tested July-25 and August-25 against the SATA dimension specifically. Both return SA=0, TA=0, OTHER=0, with the full caseload sitting in UNKNOWN — i.e. the accommodation type field simply did not exist for those months, rather than data being missing or suppressed. This matches DWP's own note that the breakdown was introduced in the February 2026 release.

Conclusion: Stat-Xplore's HB caseload data (accommodation-type breakdown or otherwise) is currently running ~5 months behind the calendar, and September 2025 is the correct floor for this specific field — there is nothing earlier to backfill and nothing later currently available to load.

## Consequences

* Do not re-run S8b expecting more recent months until Stat-Xplore's general HB caseload date valueset advances past 202602. Check the general (non-SATA) date valueset first — if it has moved on, the SATA breakdown should be re-queried for the newly available months.
* If this symptom recurs (a field appears to be several months behind where you'd expect), repeat step 1 above before assuming a script or pagination bug: confirm whether the general caseload data has also stalled, which points to a DWP publication delay rather than a build fault.
* The `hb_sa_claimants_latest` map property will read as "as of Feb-26" until the next successful reload — worth keeping that date visible in the map's own metadata/legend if a viewer might assume it's current-month.
* Suggested next check-in: 4-6 weeks (early September 2026), not on a fixed quarterly cycle — this field's cadence hasn't been observed for long enough to confirm quarterly vs monthly-with-lag as the steady-state pattern.
