# CLAUDE CODE TASK: Build Source 11 — CQC Registered Care Providers (Supported Living Filter)

Self-contained prompt. Run in a clean Claude Code session in the pipeline working directory. No project memory assumed — everything needed is here or discoverable by the steps below.

---

## Skills — load before anything else

Read these skill files in full before Phase 0. They govern this task and are not optional:

1. `github-publishing` — governs Phase 6. All publishing gates (sanitisation, secrets scan, drift check, CHANGELOG, review stamp, no force push) apply.
2. `n8n-house-rules` — governs any node configuration written. The one rule above all others: never write a node type, parameter, or URL from memory. Verify first.
3. `humaniser` — applies to any prose written for the README, CHANGELOG, or decision records.

---

## Context

The Exempt Accommodation Intelligence Pipeline is a Postgres database (`exempt_pipeline`) covering all 296 English local authorities, joined on LAD24CD (ONS May 2024 codes). Twelve demand-side sources are built (S1–S17, gaps intentional). S11 is the thirteenth build and the **only supply-side indicator in the pipeline**: everything else measures need; this measures existing provision.

What S11 is for (context only — see Pipeline Principle below):
- Shows where CQC-registered care providers already operate — the provider landscape councils expect a credible operator to understand
- Location-level records support partnership sourcing (which providers, where, at what rating)
- Low provider density is a gap signal the platform can surface to councils

**Dual model lens.** Every source is read through two lenses and stored so both remain visible:
- **UCWS lens:** resettlement cohort, tenancy sustainment support, no personal care, HMO-basis, room-by-room HB. S11 is *secondary* here — provider landscape context and partnership sourcing.
- **HSS lens:** complex needs cohort, personal care, CQC-registered provision. S11 is *primary* here — supported living registration and personal care regulated activity are the direct signals.

Never collapse the two into one view. Store flags that let each lens query what it needs.

**Pipeline Principle — agnostic storage.** The pipeline stores indicators. It does not score, rank, weight, or express a view on markets. The only question for every step: does this collect and store the data correctly?

---

## Source facts (verified 2026-07-12 — RE-VERIFY at run time)

- Publisher: Care Quality Commission. Data page: `https://www.cqc.org.uk/about-us/transparency/using-cqc-data`
- File: **"Care directory with filters"** — complete register of active CQC-regulated locations in England. Updated roughly monthly. Downloads as a file whose name contains `HSCA_Active_Locations`. Published as ODS and/or XLSX. Open Government Licence.
- The file carries per-location service-type flag columns (Y/blank), including a **Supported living service** flag, regulated activity flags (including **Personal care**), service user band flags, provider and location IDs and names, postcode, latitude/longitude (ONSPD-derived), local authority and region fields, and rating columns.
- **CQC has stated it is migrating its directory to a new digital system.** The page layout, file URL, and column names may have changed. Do not trust any of the above blindly.
- A Syndication API exists as an alternative but requires a subscription key and TLS 1.2+. The monthly file is the preferred route — no key, complete register, simple refresh. Note the API as fallback only.

**Run-time verification (mandatory, project rule):**
1. Fetch the using-cqc-data page and locate the current Care directory with filters download URL. Never hardcode a file URL — extract it from the page each run, exactly as other pipeline sources extract from GOV.UK collection pages.
2. Download the file and inspect the actual structure: sheet names, header row position, exact column names. CQC files historically have metadata rows above the header — find the real header row programmatically, do not assume row 1.
3. List the exact service-type, regulated-activity, and service-user-band columns found, and the geographic columns available (LA name, region, postcode, lat/long). Report this before designing the schema.

If the page or file cannot be found where expected, stop and report. Do not guess an alternative URL.

---

## Phase 0 — Preflight

1. Confirm working directory is the pipeline repo working copy. Run `gh repo list --limit 200` and identify the existing pipeline repository (node documentation for S1–S17 lives there). This is an **update to an existing repo** — github-publishing phase three gates apply, not first-publish.
2. Confirm Postgres connectivity to `exempt_pipeline` on the local Docker stack using the same connection approach as previous source builds (connection details from the local environment — never commit them, never print secrets into any file that will be staged).
3. Confirm baseline tables exist and inspect schemas: `la_boundaries` (note whether it stores full polygon geometry or centroids only — this decides the geographic join method in Phase 2), `la_code_lookup`, `pipeline_run_log`.
4. Confirm Python environment has pandas, openpyxl, odfpy (for ODS if needed), geopandas + shapely (only if the spatial join route is chosen), psycopg2/psycopg3.
5. Report preflight results. Stop if anything fails.

---

## Phase 1 — Acquire and profile the source

1. Fetch the CQC data page, extract the current file URL, download the file.
2. Profile it: total rows, England-only check, distinct location IDs, count of rows with Supported living service flag set, count with Personal care regulated activity, null rates on postcode / lat-long / LA name, distinct LA names and how many there are (upper-tier vs. district ambiguity check — this matters: if the LA field is upper-tier only, it cannot join to LAD24CD districts directly and the spatial route is required).
3. Save the raw file to the standard raw-data location used by previous builds (outside anything committed, per the .gitignore conventions).
4. Report the profile. **Do not proceed to build.**

---

## Phase 2 — Design gate (STOP AND CONFIRM)

Present the following for confirmation before writing any DDL or load code:

**A. Scope decision.** Recommend: store *all* active adult-social-care-relevant locations with flag columns, not just supported-living-flagged rows, so both lenses can filter without a rebuild. Present the row-count implication. Alternative: supported living + personal care rows only. State a recommendation, wait for the choice.

**B. Geographic join method.** Based on Phase 0 finding:
- If `la_boundaries` stores polygon geometry: point-in-polygon join of location lat/long into LA polygons (geopandas, EPSG:4326), nearest-fallback for coastal/null-coordinate edge cases, `mapping_method` column recording which route each row took — the exact pattern S14 used, inverted (points into polygons rather than centroids into polygons).
- If it stores centroids only: fetch the ONS Geoportal BGC May 2024 boundary GeoJSON (as the S7 build did — verify the current Geoportal URL, never from memory) and join against that, still writing only lad24cd into the pipeline table.
- Name-matching the file's LA field to `lad24nm` is acceptable only as a cross-check, never as the primary method, because CQC LA names are not guaranteed to be district-level or May-2024-vintage.
- Verification standard: every stored row has a non-null `lad24cd` that exists in `la_boundaries`; report how many LAs of 296 have zero locations (legitimate finding, not an error).

**C. Refresh strategy.** The file is a complete register of *active* locations each month. Two options:
- **Full refresh:** delete-then-insert (two separate operations per the one-operation rule), latest state only. Simple, idempotent, loses deregistration history.
- **Upsert with deactivation:** upsert on `location_id`, then mark rows absent from the current file as inactive with a `deregistered_seen_date`. Preserves supply-contraction signal over time.
State a recommendation with reasoning, wait for the choice.

**D. Proposed schema.** One table, `cqc_locations`, keyed on CQC location ID, columns to include (adjust to the actual file): location_id (PK), provider_id, provider_name, location_name, postcode, latitude, longitude, lad24cd, region, supported_living (boolean), personal_care (boolean), other service-type/user-band booleans found relevant to either lens, care_home flag, latest overall rating, publication/registration dates, mapping_method, source_file_date, is_active + deregistered_seen_date if option C2 chosen. Parameterised queries throughout. `exempt_pipeline` database only — never n8ndb.

**E. Node sequence.** Following the established per-source convention (fetch → process → create table → upsert → log run), with the mapping step as its own node as S14 did. One operation per node. Present the full sequence with what each node does.

Wait for explicit confirmation on A–E before Phase 3.

---

## Phase 3 — Build

On confirmation:

1. Create table(s) — idempotent DDL (`CREATE TABLE IF NOT EXISTS`), separate step.
2. Process: filter per the confirmed scope, normalise flags to booleans, resolve lad24cd per the confirmed method.
3. Load per the confirmed refresh strategy — separate delete and insert operations if full refresh; parameterised upsert if not.
4. Log to `pipeline_run_log`: agent_name `'Source 11 - CQC Care Providers'`, source_number `11`, rows_written, status, and a notes field naming the source file date.
5. Build one step at a time, verifying output at each step before the next.

---

## Phase 4 — Verification (gate before documentation)

All must pass or be explicitly explained:

1. Row count in `cqc_locations` matches processed count.
2. Zero null `lad24cd`; every `lad24cd` exists in `la_boundaries`; count of `nearest_fallback` mappings is small and each one listed.
3. All 296 LAs accounted for: locations present or a zero-count confirmed as genuine.
4. Target-market sanity check: report supported-living-flagged location counts for Birmingham (E08000025), Liverpool (E08000012), Nottingham, Manchester, Blackpool — expect non-trivial counts in Birmingham and Manchester; flag anything surprising rather than smoothing it over.
5. Re-run the load once and confirm idempotency (no duplication, stable counts).
6. Cross-check: sample 5 location IDs against their live CQC pages (`cqc.org.uk/location/<id>`) — name, LA, and supported living status match.

Report results in full. Do not proceed on silent failures.

---

## Phase 5 — Documentation

1. Node documentation, one markdown file per node, named `s11_node[N]_[short_name].md`, exact house format:

```
# Node [N] — [Name]
## Type
## Purpose
## Credential (if applicable)
## Query / Code / URL (full content)
## Logic (for code nodes — step by step)
## Query Parameters (for Postgres nodes — parameter mapping table)
## Behaviour (conflict handling, re-run safety)
## Connection
- Input: [previous node]
- Output: [next node]
## Verified Output (confirmed result and date)
```

2. A project memory file for S11 (same convention as the S14 build): source facts, design decisions taken at the Phase 2 gate, verification results, refresh cadence, known caveats (CQC system migration, LA field vintage).
3. If the Phase 2 geographic-join investigation produced a non-obvious root cause or decision (it will — the join method choice qualifies), write a decision record to `docs/decisions/` per the github-publishing skill.
4. No status commentary in permanent documents — facts only.

---

## Phase 6 — GitHub (MANDATORY — this step was missed on a previous build; it is the reason it is spelled out)

Apply github-publishing phase three to the existing pipeline repo:

1. Sanitisation pass on every file to be committed (no connection strings, no credentials, no absolute local paths that leak the environment).
2. Secrets scan (`gitleaks` if installed, otherwise the fallback pattern list).
3. Drift check: diff incoming changes against the README; fix or flag stale claims.
4. CHANGELOG entry written now, not later. Update the README review stamp.
5. Commit — Conventional Commits, atomic. Example: `feat(s11): add CQC care provider source with LA spatial mapping`.
6. Push.
7. **Verify: fetch the repo file listing from GitHub (gh or raw URL) and confirm every new s11 file and the changed files are visible remotely. Print the confirmation.** Files on disk do not count as published. This task is not complete until this check passes.

---

## Phase 7 — Handover notes (report, do not action without asking)

1. **W1 re-run required.** The pre-computation workflow must re-run after any new source load. If W1 Node 5 (`staging_la_signals`) is to carry S11 counts (e.g. supported_living_locations per LA), propose the exact SQL amendment for confirmation first — do not modify W1 unilaterally.
2. **Map layer.** A provider-density layer on map.slendeavours.org is a candidate follow-up. Do not build it in this session; flag it, note it follows the screenshot review-then-patch cycle.
3. List anything deferred, ambiguous, or worth a decision record that wasn't written.

---

## Standing rules for the whole task

- Verify every URL, code, and column name against the live source before use. Never guess and proceed — flag uncertainty explicitly.
- Parameterised queries only. One operation per statement/step.
- Propose before executing anything with meaningful consequences.
- All data stored joins on LAD24CD through the established code system; historical codes reconcile via `la_code_lookup` only after confirming absence from `la_boundaries`.
- Acknowledge errors plainly, fix root causes, move on.
