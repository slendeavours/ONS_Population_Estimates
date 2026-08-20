# S9a + S9b Build — NHS Acute Discharge SitRep + MHSDS Clinically Ready for Discharge

Standalone Claude Code prompt. Run locally where england.nhs.uk, digital.nhs.uk and files.digital.nhs.uk are reachable. Execute in phases. **Stop at every GATE and wait for explicit approval before continuing.** Do not run ahead.

---

## Context

This build adds two sources to the `exempt_pipeline` Postgres database (Docker-hosted, database `exempt_pipeline`, user `pipeline_user`):

- **S9a** — NHS England Acute Discharge Situation Report (monthly). Hospital patients no longer meeting criteria to reside, delayed discharges, reasons for delay. Published at trust level and upper-tier local authority (UTLA) level.
- **S9b** — MHSDS Clinically Ready for Discharge (CRFD) measures from the Mental Health Services Monthly Statistics publication. Covers mental health, learning disability and autism inpatients who are clinically ready for discharge but delayed. CRFD replaced the old mental health DTOC metric.

These populate the two deferred tenant types in the pipeline: `mental_health` and `learning_disability`. S9a additionally provides the acute bed-blocking signal (Secondary UCWS / Primary HSS).

### Decisions already made — do not revisit

1. **Geography:** store each source at its native published geography. Build population-weighted mapping tables down to LAD24CD with a `mapping_method` column on every mapped row. This is the same pattern as S17 (PFA→LA) and S14 (BRMA→LA).
2. **W1 integration:** included in this build as a gated phase. New `staging_la_signals` columns are proposed together with the W1 Node 5 SQL amendment **and** the Node 9 export patch in one gate — never separately.
3. **Date range:** June 2024 onward is the primary S9a series. The acute sitrep data definitions changed 27 May 2024; earlier data is not comparable and is excluded. For S9b, load whatever CRFD history exists under the current definition (CRFD reporting became mandatory from April 2023) — confirm actual coverage at the discovery gate.
4. **Documentation:** node docs per project convention, a source summary, and a UCES project-memory markdown file, all saved for upload to project knowledge, plus a GitHub push with remote verification.

---

## Hard rules

- Never write URLs, measure IDs, sheet names, column names or ONS/NHS organisation codes from memory. Discover everything from the live publication pages and the downloaded files themselves.
- Parameterised SQL only (`$1`, `$2` style in documented queries; psql `\copy` or parameterised INSERT in execution). No string concatenation of values into SQL.
- One operation per statement. No stacked DELETE+INSERT.
- Every table joins on LAD24CD. Every cross-source join passes through `la_code_lookup`.
- Agnostic storage: store indicators, never scores, rankings or market commentary.
- Idempotent loads: every upsert must be safe to re-run without duplication. Use `ON CONFLICT ... DO UPDATE` on a declared natural key.
- Every load logs to `pipeline_run_log` on completion.
- Suppression handling: NHS files mark suppressed or missing values with symbols (`-`, `*`, blanks). Coerce to NULL, never to zero. Record the convention found in the source doc.
- Flag uncertainty explicitly. If a file does not match what this prompt expects, stop and report — do not improvise a workaround silently.
- UK English throughout all documentation.

---

## Phase 0 — Preflight

1. Confirm Docker is running and Postgres is reachable:
   `docker exec <postgres-container> psql -U pipeline_user -d exempt_pipeline -c "SELECT count(*) FROM la_boundaries;"` — expect 296.
2. Confirm the population table from S3 exists and holds current LAD24CD-level totals (needed for apportionment weights). Report the table name, reference year and row count.
3. Confirm `la_code_lookup` and `pipeline_run_log` exist.
4. `gh auth status` — confirm authenticated as slendeavours.
5. `gh repo list --limit 200` — locate the existing pipeline repository (expected: the repo currently holding the pipeline node docs, historically `ONS_Population_Estimates`). Confirm its name and default branch. This build is a **phase three update** under the github-publishing skill, not a first publish.
6. Report all findings. **GATE 0 — stop.**

---

## Phase 1 — S9a discovery

1. Fetch the NHS England discharge delays statistical work area index:
   `https://www.england.nhs.uk/statistics/statistical-work-areas/discharge-delays/`
   and from it the acute page (currently at `/discharge-delays-acute-data/` — verify the live link from the index, do not assume).
2. From the live page, identify:
   - the current **monthly data webfile** (latest month) and its URL
   - the current **timeseries webfile** (April 2021 onward) and its URL
3. Download both. Enumerate for each:
   - workbook sheet names
   - per sheet: header rows, column names, geography columns present (trust codes, UTLA codes/names), metrics present (NCtR counts, discharge-ready-date delay metrics, reasons for delay, bed days), reporting-period granularity (daily snapshots vs monthly aggregates)
   - date coverage, and where the 27 May 2024 definitions break falls in the files
   - the UTLA code scheme in use (expect E06/E08/E09 unitaries and E10 counties) — list every distinct code and name found
   - suppression/missing-value conventions
4. Cross-check every UTLA code found against `la_boundaries` (unitaries should match LAD24CD directly) and identify the E10 county codes that require district apportionment.
5. Report findings in full, including anything that does not match expectations. **GATE 1 — stop.**

---

## Phase 2 — S9b discovery

1. Fetch the Mental Health Services Monthly Statistics landing page:
   `https://digital.nhs.uk/data-and-information/publications/statistical/mental-health-services-monthly-statistics`
   Identify the latest performance publication and open it.
2. Download the **metadata file** and the **main monthly data CSV** (and the measures/reference-tables file if separate).
3. From the metadata, enumerate:
   - every CRFD / delayed-discharge measure ID and its full name (do not guess IDs — read them from the file)
   - the geography breakdowns actually published for each CRFD measure (England / Provider / Commissioning Region / ICB / Sub-ICB / CASSR-LA)
   - whether the measures split mental health from learning disability and autism cohorts, and at which geographies that split survives
   - date coverage under the current CRFD definition
4. **Decision logic at this gate:**
   - If CRFD measures are published at CASSR/LA level → use that as native geography (CASSRs are the ~153 upper-tier councils; unitaries map 1:1 to LAD24CD, counties apportion — same mapping table as S9a).
   - If the best available is ICB or Sub-ICB → native geography is ICB/Sub-ICB, and a second mapping table (ICB or Sub-ICB → LAD24CD, population-weighted) is required. Source the LAD-to-ICB lookup from the ONS Open Geography Portal — find the current lookup product on the portal, verify vintage against LAD24CD, and record the exact product name and URL.
   - If the MH vs LD/autism split is not available in MHSDS at sub-national level, check the Learning Disability Services Monthly Statistics publication (Assuring Transformation / MHSDS-based) on digital.nhs.uk as the LD/autism source, and report what it offers before proposing anything.
5. Report findings and the recommended geography route in full. **GATE 2 — stop.**

---

## Phase 3 — Schema design

Based only on verified structures from Gates 1 and 2, propose full DDL for approval. Expected shape (adjust to reality, do not force):

- `utla_lad_mapping` — utla_code, utla_name, lad24cd, weight NUMERIC, mapping_method TEXT (`direct` for unitaries with weight 1.0; `population_weighted` for county→district rows), population_reference_year, source. Natural key (utla_code, lad24cd).
- `icb_lad_mapping` (only if Gate 2 requires it) — same pattern.
- `nhs_acute_discharge_delays` (S9a) — native geography rows: reporting period, utla_code (and trust-level table only if the data warrants it — propose, do not assume), metric columns as found, suppression-aware NULLs. Natural key (reporting_period, utla_code, metric grain as found).
- `nhs_mh_crfd` (S9b) — native geography rows: reporting period, org code + org type, measure_id, cohort (mh / ld_autism if split exists), value. Natural key (reporting_period, org_code, measure_id[, cohort]).
- LAD-level apportioned **views** (not tables) over each source joined through the mapping tables, so apportionment logic lives in one place and raw data stays untouched — consistent with agnostic storage.
- All DDL idempotent (`CREATE TABLE IF NOT EXISTS`), no data loss on re-run.

Present the DDL, the natural keys, the apportionment SQL for the views, and the verification suite design (below). **GATE 3 — stop.**

---

## Phase 4 — Mapping tables + verification suite

1. Build `utla_lad_mapping` (and `icb_lad_mapping` if required) using S3 population weights.
2. Automated verification — all must pass before proceeding:
   - weights sum to 1.0 (±0.0001) for every source unit
   - every one of the 296 LAD24CDs appears in the mapping exactly once per source geography
   - every UTLA/ICB code found in the downloaded data files resolves in the mapping — zero orphans
   - unitary rows have mapping_method `direct` and weight 1.0
3. Report verification output. Do not proceed on any failure — diagnose root cause first.

---

## Phase 5 — Load S9a

1. Parse the timeseries webfile from June 2024 onward. Coerce suppressed values to NULL.
2. Idempotent upsert into `nhs_acute_discharge_delays`.
3. Verify: row counts per reporting period, distinct UTLA count per period, spot-check three UTLAs (one unitary, one county, one metropolitan) against the raw file values.
4. Create/confirm the LAD-level apportioned view and spot-check that a county's districts sum back to the county figure (±rounding).
5. Log to `pipeline_run_log`.

---

## Phase 6 — Load S9b

1. Parse the CRFD measures per the Gate 2/3 design. Load full available history under the current definition.
2. Idempotent upsert into `nhs_mh_crfd`.
3. Verify: row counts, measure coverage per period, cohort split integrity if present, spot-checks against raw file.
4. Create/confirm the LAD-level apportioned view.
5. Log to `pipeline_run_log`.

---

## Phase 7 — W1 integration (gated)

1. Propose, as one package:
   - new columns for `staging_la_signals` (expect approximately: acute NCtR / delayed-discharge signal from S9a; MH CRFD signal; LD/autism CRFD signal — name them from what was actually built)
   - the amended W1 Node 5 SQL (full query, retaining all existing columns and joins, adding the new LEFT JOINs to the LAD-level views and the new ON CONFLICT SET entries)
   - the matching **Node 9 export SQL patch** — this ships in the same gate as the Node 5 change, never separately
   - the W1 Node 6 addition for the two new tenant types (`mental_health`, `learning_disability`) with primary signal, signal label and data confidence rating (state the confidence honestly given the geography apportionment — Medium at best)
2. **GATE 4 — stop for approval.**
3. On approval: apply the staging DDL changes, update the workflow SQL, re-run W1 end to end (this becomes run 10), and verify the new columns and tenant-type rankings are populated for the expected LA counts.

---

## Phase 8 — Documentation

Produce, in the repo working copy and copied to an outputs folder for project-knowledge upload:

1. **Node docs** — one markdown file per build step, project convention format (`s9a_node1_fetch_acute_sitrep.md` etc.): Type / Purpose / Credential / Query-Code-URL in full / Logic / Query Parameters / Behaviour (conflict handling, re-run safety) / Connection / Verified Output with date.
2. **Mapping doc** — `s9_utla_lad_mapping.md` (and ICB doc if built): method, weights source, verification results.
3. **W1 patch docs** — updated `w1_node5_la_signals.md`, `w1_node6_tenant_type_rankings.md` and the Node 9 export patch doc reflecting the applied changes.
4. **UCES project memory file** — `S9_BUILD_SUMMARY.md`, written for upload to the UCES (Universal Credit Eco System) Claude project knowledge. Contents: what S9a and S9b are, exact table and view names, native geographies and the mapping/apportionment design, date ranges loaded, measure IDs used, suppression conventions, data confidence ratings, refresh cadence (both monthly) and the refresh procedure, known caveats (management-data status of the sitrep, May 2024 definitions break, apportionment resolution loss), W1 run number after integration, and outstanding maintenance items. Facts only — no status commentary, no snapshots of in-flight work.

---

## Phase 9 — GitHub publish (github-publishing skill, phase three update path)

1. Drift check: diff incoming changes against the repo README; flag stale claims and fix before push.
2. CHANGELOG entry written before the push, Keep a Changelog format.
3. Update the last-reviewed date in the README metadata block.
4. Sanitisation pass per the skill's manifest on every file; local secrets scan (gitleaks if installed, fallback pattern list otherwise).
5. Conventional Commits, atomic. Example: `feat(s9): add NHS acute discharge and MHSDS CRFD sources with UTLA-LAD mapping`.
6. Push. **Then verify remotely**: list the new files via the GitHub API (`gh api repos/<owner>/<repo>/contents/<path>`) and confirm every doc and script is visible on the remote — files on disk do not count. This step is mandatory (S14 lesson).

---

## Phase 10 — Final report

Report to the user:
- tables, views and mapping tables created, with row counts and date coverage
- verification results summary
- W1 run 10 outcome and the new tenant-type coverage
- the list of markdown files staged for UCES project-knowledge upload, with paths
- remote GitHub verification output
- any deviations from this prompt and why, and any maintenance items created (e.g. monthly refresh not yet scheduled in n8n — flag as a future n8n workflow, do not build it here)
