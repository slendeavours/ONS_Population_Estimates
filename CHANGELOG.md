# Changelog

All notable changes to this project are documented here.
Format follows Keep a Changelog. Versioning follows semver where tags are used.

## [Unreleased]

### Added
- **S1's ODS extraction step, which existed nowhere.** Node 1 fetched a pre-processed CSV from the public repo and the ODS-to-CSV conversion behind it was never committed, so `la_statutory_homelessness` could not be rebuilt from its source. Now `scripts/s1_extract_ods.py`, reproducing the stored table exactly for 2025Q2 and 2025Q3.
- **2025Q1 (Apr–Jun 2025) and 2025Q4 (Jan–Mar 2026) loaded.** Eleven quarters at 296 rows each, 2023Q2 to 2025Q4. The 2025Q1 gap is closed. `period` is a **financial-year** quarter — read from the files' own table titles, not their names — so 2026Q1 is Apr–Jun 2026 and is not yet published.
- `source_file` and `extracted_at` on `la_statutory_homelessness` (additive, guarded). The seven historical quarters carry NULL, which is honest: nobody can say which edition produced them.
- An `.xlsx` reader. MHCLG published 2023Q4, 2024Q1 and 2024Q2 as `.xlsx` while everything either side is `.ods`. Container format is not a property of the data and is now not a property of the pipeline.

### Fixed
- **`..` was being stored as `0`, so suppressed and zero were the same value.** Of the eight authorities `submission_gap` flagged in run 16, seven were suppression markers and one — Isles of Scilly — is a genuine zero. **54 cells corrected to NULL across 2025Q2 and 2025Q3 only**, the two quarters that reproduce exactly and therefore the only two where the verdict is evidence rather than inference. 129 stored zeros in the earlier quarters remain ambiguous.
- **Column resolution is header-driven and now refuses ambiguity.** 2025Q4 redesigned all three sheets and shifted the A3 support-need block three columns left, putting *domestic abuse* where *mental health* had been — a fixed-offset reader would have loaded one as the other and reported success. This is the probable mechanism behind the quarantined support-need columns. Candidates are ordered most-specific first, only an exactly-one match wins, and rate columns are excluded before matching.
- Barnsley and Sheffield resolve through `la_code_lookup` on `change_type = 'recode'` only; abolitions stay unmapped so no successor is counted once per predecessor.
- **Gate 13 green.** `homelessness_quarter_urls` carries rows for 2025Q3 and 2025Q4 with URLs verified by `Content-Length` against the stored files rather than assumed, and `loaded` re-derived from actual row counts rather than set by intent. Known-red entry removed.
- The README generated source block was stale — S1b, S23 and S24 were registered in METHODOLOGY on 2026-08-14 and never regenerated.

### Changed
- **Run 17 supersedes run 16; the published TA headline moves from 124,142 to 130,775** as the feed catches up to 2025Q4 (Jan–Mar 2026), with the prior-year comparison moving to 2024Q4 and year-on-year at +13.29%. Run 16 was snapshotted in full to `build_reports/run_snapshots/` and deleted whole — 1,600 rows across five staging tables — with readback before and after. `submission_gap` falls from 8 to 0, which is the marker fix showing through: seven of the eight were `..`, and no authority reported a genuine zero in Jan–Mar 2026. Twelve authorities now carry `no_current_data` and a NULL `ta_yoy_pct` instead of a fabricated trend.
- **Gate 15 is stricter.** It asserted that a derived column must be NULL when an input is absent — right for a numeric value, wrong for a categorical label whose purpose is to name the absence. It now requires the label to be one of the declared absence sentinels, and any direction word is a violation. `undetermined` is deliberately not a sentinel. Proved by injection: `falling_strongly` over an absent TA fails, `no_current_data` passes.
- **The seven-quarter divergence is registered in the database, not only in a decision record.** `source_registry` S1's `revision_note` records the measured instance rather than an inference from file names. `homelessness_quarter_urls` gains `reproduces_from_source`, `reproduction_checked_at`, `reproduction_diff_cells` and `reproduction_note`. `v_la_statutory_homelessness` joins that status onto the data, so a query against 2023Q2 discovers it without knowing to write the join.

### Known issues
- **Seven quarters (2023Q2–2024Q4) no longer reproduce from the current published files**, differing on 200–230 of 296 authorities for the assessment measures, while 2025Q2 and 2025Q3 match exactly. Not an extraction fault — the 2023Q2 file itself disagrees with the stored value. Almost certainly MHCLG revising the back series after the 2026-04-01 bulk load. Left unrestated pending a dedicated reload.
- **`la_statutory_homelessness.support_needs_total` holds the wrong column for 2023Q2–2024Q4** — "households with one support need" instead of "one or more", running at 45–47% of the correct total and matching the adjacent `hh_one_support_need` column on 148–164 of 296 authorities. Arbitrated by S1b, which agrees with a fresh extraction 296/296 in every quarter while the stored value agrees on 1–3. This is misalignment, not revision: the same defect that quarantined the five `*_suspect` columns, which `support_needs_total` escaped. Not published — absent from `staging_la_signals` — so it is contained to the database. The correct values are already known exactly for all seven quarters.
- **S10 `la_rough_sleeping` carries S1's signature** — 22 and 27 zeros with no NULL anywhere, so a suppression marker could not be distinguished from a genuine zero. A genuine zero is plausible for a snapshot count, so this is lower risk than S1 was, but the table cannot prove it either way. Settles the way S1 did: extract from source and compare.
- **`la_housing_register.reasonable_preference` is NULL in all 3,256 rows** because it is empty in the source extract too — all 3,471 rows of `lahs_waiting_list_2015_2025.csv`, every year. A schema artefact, not a load failure. Whether LAHS publishes a figure that could fill it has not been established.

### Added
- **S3 population refreshed to mid-2025**, closing the denominator lag that gated external use of the HSS material. From *Estimates of the population for England and Wales*, edition "Mid-2025: 2023 local authority boundaries", released 29 July 2026, resolved from the landing page at run time. `pip_rate_per_1000` moves from a mid-2024 base to mid-2025: England 58,620,101 → 58,834,812, Birmingham 78.74 → 79.18 per 1,000, Kingston upon Hull 87.85 → 88.54. Six hard gates, all pass; the England total reconciles exactly to the publisher's own England row. Build script `scripts/s3_mye_refresh.py`.
- `la_population` is now **multi-year** — key widened from `(lad24cd)` to `(lad24cd, reference_year)`, mid-2024 retained rather than overwritten, 592 rows.
- **A weekly scheduled source register audit** (`UCWS\Source register audit`, Mondays 08:17). It exits non-zero on any finding and keeps the previous report so drift is one diff away, and treats "could not run" as distinct from "clean". Verified end to end.

### Fixed
- **Adding a second population vintage fanned out everywhere `la_population` is joined, not only in the obvious place.** Both node 5 and the `la_population` join inside `v_la_pip_rates` needed pinning to `MAX(reference_year)`. Unpinned it did not silently double-count — it killed the statement with `ON CONFLICT ... cannot affect row a second time` — but W1 was genuinely broken between the load and the pin. Anything joining `la_population` in future must pin the vintage.
- **The Barnsley/Sheffield expectation was wrong for S3, and the reason is now a refinement to the geography standing rule.** The release postdates the April 2025 recode by sixteen months but is built on *2023 local authority boundaries*, so it uses E08000016/E08000019 and matched `la_boundaries` with zero orphans in either direction. Where a release declares its boundary vintage that is the predictor, not the publication date.

### Added
- **Source 22 (MHCLG Council Taxbase empty homes).** Two MHCLG publishers, both resolved from their landing pages at run time with no stored file URL: *Council Taxbase 2025 in England* (first published 6 November 2025, **revised 21 January 2026** after corrections from 22 authorities) and *Live Table 615: vacant dwellings by local authority district, England, from 2004*.
- Four tables: `la_council_taxbase_empties` (296 rows, one per LA per taxbase year), `la_ctb_exemption_classes` (3,256 rows, the eleven unoccupied exemption classes at LA level), `la_vacant_dwellings_615` (7,170 district-year rows, 2004 to 2025) and `ctb_series_breaks` (2 rows).
- `v_la_empty_homes_rates` — long-term empty rate, long-term share of empties, premium coverage and second homes rate over the latest taxbase year. Every denominator guarded with `NULLIF`. Rates are derived here and never stored.
- Five `staging_la_signals` columns via an additive `DO $$ ... IF NOT EXISTS` migration: `ctb_total_dwellings`, `ctb_empty_6m_plus`, `ctb_empty_homes_premium`, `ctb_second_homes`, `ctb_lte_rate_pct`. The table was not dropped or recreated. W1 run 12, 296/296 on all five.
- Map layer **Long-Term Empty Rate**, driven by `ctb_lte_rate_pct`. One layer only — total empties bundles second homes and short-term turnover and is held in the database rather than mapped, as is premium application. The detail panel gains an Empty Homes section.
- Build scripts `scripts/s22_ctb_discover.py`, `s22_ctb_empties_build.py`, `s22_run.py`, `s22_w1_wire.py`, `s22_verify.py`, and the shared connection helper `scripts/_db.py`.
- Documentation: `docs/S22_BUILD_SUMMARY.md`, `docs/s22_source_structure.md`, `docs/s22_verification.md`, `docs/s22_w1_node5_revised.md`, and `docs/nodes/s22_node1..10*.md`.
- `docs/METHODOLOGY.md`: S22 source register row, the four S22 tables in the architecture listing, and a full S22 section covering both publishers, coverage caveats, structural breaks and geography.

### Changed
- `docs/METHODOLOGY.md` and `docs/README.md`: S22 recorded; review stamp updated to 2026-08-13.
- `scripts/export_map_data.py`: the five `ctb_*` columns added to the post-export presence check, so a future export that silently drops them fails loudly.
- **W1 node 5 revised in the stored n8n workflow.** The revision also folds in the S9 (`drd_*`, `crfd_days`) and S19 (`pip_*`) columns, which were present in the database but absent from the stored node because runs 10 and 11 had been applied by direct SQL. Without that the next workflow run would have silently dropped six columns. Full SQL at `docs/s22_w1_node5_revised.md`.

### Fixed
- **`docs/METHODOLOGY.md` listed S19 as standalone and not wired into Workflow 1. That was false from run 11 onward.** `staging_la_signals` has carried `pip_total_claimants`, `pip_enhanced_daily_living` and `pip_rate_per_1000` since July 2026, verified against `la_pip_claimants` at Apr-26 (Birmingham 93,196 / 50,002; Kingston upon Hull 24,195 / 12,078; Kensington and Chelsea 7,166 / 4,039, all exact). They are PIP columns, not a tail left by the S15 renumbering — S15 house prices live in `la_house_prices` with no staging column. `docs/README.md` had it right throughout.
- **Boundary vintage corrected from "December 2024 ... BUC" to "May 2024 ... BGC"** in both the S7 register row and the Boundary Data section. Two independent artefacts agree: `la_boundaries.source_date` is `2024-05-01` for all 296 rows, and the S7 run log note reads "LA boundaries loaded — May 2024 BGC — England only". The LAD24 code list is identical across vintages, so this was settled from load provenance rather than inferred from the data.
- **`pipeline_run_log` backfilled and normalised so it agrees with the source register.** S15 (Land Registry HPI, built 2026-07-14) had never been logged at all; S9a, S9b and S8b were logged under `s9a`, `s9b` and `8`, which did not match their register entries. Read literally the log made 9, 15 and 16 all look free when only 16 was — the second near-collision in source numbering. Each amended row carries a note recording the backfill; no run was altered.
- `scripts/_db.py` resolves `PG_USER` and `PG_PASSWORD` through a `_require` helper that stops with a clear error rather than falling back to a literal, and tolerates a missing `.env` instead of raising on import.
- The `staging_runs` sequence trailed the data. Runs 10 and 11 exist in `staging_la_signals` with no matching `staging_runs` row, so `nextval()` would have returned 10 and collided with an existing run. The sequence is now advanced past the highest run id in either table before a run is created.

- **Four standing rules added to `docs/METHODOLOGY.md`**, all from defects the S22 build surfaced rather than caused: direct SQL against `staging_la_signals` must write back to W1 node 5 in the same session; `staging_runs` rows must be created through the Create Run node so the sequence stays ahead of the data; geography resolves through `la_code_lookup` before the orphan gate, assuming post-April-2025 sources use the recoded Barnsley and Sheffield codes; and `docs/METHODOLOGY.md` is the source register while `pipeline_run_log` is an execution record checked only as a contradiction test. Decision record: `docs/decisions/2026-08-13-stored-node-drift-and-register-authority.md`.
- Three fully-merged local branches removed (`fix-credential-exposure`, `s15-renumbering-tail`, `s6-asylum`), each confirmed contained in main by commit comparison, `git cherry` and `git branch --merged` rather than by last-touched date. None existed on the remote.

- **Node 5 / `staging_la_signals` divergence is now enforced in three places**, because the standing rule alone is what failed twice. `Signal Column Pre-flight`, a new Postgres node inside W1 between Create Staging Tables and Create Run, aborts the run on divergence — it lives in the workflow rather than only in the export path because W1 has been run without exporting, and an export-time check alone would let a divergence sit until the next publish. `scripts/w1_contract_check.py` adds the node half of the comparison (which needs the workflow JSON and cannot be done in SQL), including positional misalignment between the INSERT and SELECT lists. `scripts/export_map_data.py` carries a backstop copy. Verified against a deliberately corrupted node: an added table column was caught by the pre-flight, and swapping two same-type SELECT expressions was caught as "position 37: inserts into `ctb_second_homes` but expression resolves to `ctb_empty_homes_premium`" — the failure that would not throw on its own.
- `staging_signal_contract` — the columns node 5 writes, parsed from the stored workflow, with the node query SHA. This is what the in-workflow pre-flight compares against.
- **Eight `staging_la_signals` columns had no `EXCLUDED` refresh in node 5's ON CONFLICT clause** — `la_name`, `population`, `rough_sleeping_prev_year`, `marac_rate_per_10k`, `housing_register` and the three `ro4_*`. Latent rather than harmless: it slept only because every run takes a fresh `run_id`, and the one time anyone re-runs into an existing `run_id` — to recover from a failed pre-flight, say — those eight would have gone stale in silence. All eight added. `scripts/w1_contract_check.py` now treats a missing `EXCLUDED` clause as an error rather than a warning, using the same positional parser that already knows the INSERT list. Verified by nulling all eight for one authority in run 12, re-running node 5 into that same `run_id`, and confirming every one restored.
- **Standing rule five: anything enumerating tables, columns or schema is scanned for counterparty names before it is staged** — before `git add`, not before push. S20's counterparty name is in its table names, so a schema listing discloses it without ever naming the source. `confidentiality_scan.py` enforces it and lives outside every git working tree, because a list of names you must not publish is itself a thing you must not publish. Verified clean across all 135 tracked files, and verified to fire on the audit report.
- **The audit artefacts were moved outside every git working tree.** "Kept local" turned out to mean "untracked inside a second working copy of this public repository", one `git add -A` from publication. A `.gitignore` entry would not have been enough — it can be overridden with `-f`. The publishable half of the logic was split into `scripts/register_lib.py`, which names no tables.
- **Open dependency recorded: the S3 population refresh now gates external use of the HSS material.** `pip_rate_per_1000` is Apr-26 claimants over a mid-2024 base. ONS published mid-2025 estimates with local authority breakdowns on **29 July 2026**, so the fix is available; `la_population` still holds `reference_year` 2024 loaded 2026-03-26. Recorded in `docs/METHODOLOGY.md` with the two traps that refresh carries: the release postdates April 2025 so it uses the recoded Barnsley and Sheffield codes, and it covers 318 England-and-Wales authorities so England must be filtered.
- **`v_la_pip_rates`** — `pip_rate_per_1000` was a stored column whose definition existed only inline in node 5. It now comes from a view, the same treatment `ctb_lte_rate_pct` gets. The view reproduces run 12's stored values for all 296 LAs with zero mismatches. It also exposes `population_reference_year`, because PIP refreshes monthly and population annually: the current rate is Apr-26 claimants over a **2024** mid-year estimate, and that mismatch is now visible in the data rather than only in prose.
- **A full two-way source register audit** was run, tracing every table in `exempt_pipeline` to a source number and a register entry, and every register entry back to a table. It found S20 and S21 had tables but no register row. Both are now registered; S20 is entered as a private commercial rate card with its metrics withheld, since this repository is public. The audit report and its script are **deliberately not published here**: they enumerate table names, and the S20 tables carry the counterparty's name for a commercial-in-confidence source. Both are kept on the pipeline host under `build_reports/` and `scripts/`. The audit also separates "wired into W1" from "has a map layer", which had been conflated: S19 PIP is in the signals JSON with no map layer, S15 house prices are a map layer with no signals column, and S3b, S6, S18, S20 and S21 are Postgres-only.
- **README's source table is now generated** from METHODOLOGY plus the live database by `scripts/sync_readme_sources.py`, inside a marked block, with `--check` failing when stale. README was right about S19 and METHODOLOGY was wrong, which is evidence about which document gets maintained; now that METHODOLOGY is the register, two hand-maintained copies would diverge again and the contradiction test would not know which to believe.

### Notes
- **Two structural breaks**, recorded machine-readably in `ctb_series_breaks` and cited to the MHCLG technical notes. 1 April 2024: the Empty Homes Premium threshold moved from 2 years to 1 year, so `empty_homes_premium_count` is not comparable across that date — the England rise of 27.9% is a widened eligible population, not more empty homes. 1 April 2025: the Second Homes Premium was introduced, so `second_homes` is affected by reclassification.
- **`premium_coverage_pct` can never reach 100** and is directional only: long-term empty starts at six months, the premium at twelve. Carried as a column comment on the view, not only in prose.
- **No national figure is published for six-month-plus empties.** That is NOT FOUND on the release page, not unchecked. Those two measures reconcile against the publisher's own England total row in the same workbook, and the substitution is stated wherever the figure appears.
- Abolished districts in Table 615 keep a null `lad24cd` and are **not** aggregated into successor unitaries; folding six Somerset districts onto E06000066 would make any downstream sum count Somerset six times. `la_code_lookup` was read, never written.
- The "Gov Sources" badge is unchanged. It is publisher-count framing and MHCLG is already counted.

### Security
- **A database credential was supplied as a fallback default in `s15_hpi_build.py` and has been removed. The credential has been rotated.** The value is not restated here or anywhere else in the repository. It had been present across 25 commits under two filenames, and the account it belonged to is a Postgres superuser owning four databases, so the exposure was cluster-wide rather than limited to `exempt_pipeline`.
- Every build script now resolves `PG_USER` and `PG_PASSWORD` through a `_require_env` helper that stops with a clear error rather than falling back to a literal. Host, port and database name keep defaults; they are addressing, not credentials. `scripts/s11_cqc_load.py` and `scripts/s18_pipr_load.py` already behaved this way and are unchanged.
- Hardcoded absolute `.env` path removed from `scripts/s8b_hb_accom_type_build.py`. It resolved relative to a specific machine and leaked a local username.
- `.gitleaks.toml` added. **The default gitleaks ruleset does not catch this class of leak** — verified, it reports "no leaks found" against the history containing the live credential, because its rules target high-entropy secrets and this was a short dictionary word. Four custom rules close the gap: credential defaults in `os.getenv`/`environ.get`, inline database credential literals, connection URIs with embedded credentials, and hardcoded local paths.
- History has **not** been rewritten. Rotation makes the exposed value inert, and rewriting published history would break every existing clone and fork without recovering a secret that must be assumed compromised regardless.
- Decision record: `docs/decisions/2026-07-25-credential-default-exposure.md`.

### Added
- Source 6 (Home Office asylum support by local authority). Two tables from the quarterly immigration system statistics release: `Asy_D11` as a time series and `Reg_02` as a single snapshot. Loaded into `la_asylum_support` (20,926 rows, 33 quarters from 2018 Q1), `la_asylum_support_unallocated` (84 rows), `asylum_support_non_england` (2,374 rows) and `la_immigration_groups` (3,552 rows, 296 LAs × 12 pathway rows).
- `vw_la_asylum_support_totals` — one row per (period, LA) with the accommodation and support-type split, including `accommodation_not_stated` so the breakdown columns sum to the total.
- `asylum_series_breaks` — machine-readable record of the two structural breaks, so consumers of the view see them without reading documentation.
- Build script `s6_asylum_build.py` and verification suite `s6_asylum_verify.py`: landing-page discovery, download, parse, code-first geography resolution, SUM aggregation on the natural key, single-transaction upsert, 13 halting checks, run logging.
- Node documentation `docs/nodes/s6_node1..9*.md`, source summary `docs/s6_asylum_source.md`, build summary `docs/S6_BUILD_SUMMARY.md`, and generated anomalies record `docs/s6_source_anomalies.md`.
- Decision record `docs/decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md`.
- `docs/METHODOLOGY.md`: source register rows for **S8b** (DWP Stat-Xplore HB accommodation type, built 2026-07-22) and **S15** (Land Registry UK HPI, built 2026-07-14), neither of which had been added when those sources landed. Their tables `la_hb_accom_type_caseload` and `la_house_prices` added to the architecture listing.
- `docs/README.md`: `s15_hpi_build.py` and `s15_hpi_source.md` added to the repository structure.

### Changed
- `docs/METHODOLOGY.md`: S6 added to the source register in source-number order, marked standalone; S6 caveats and geography dependency recorded; pipeline-wide standing rule on unresolved codes added.
- `docs/README.md`: S6 scripts and documents added to the repository structure; review stamp updated to 2026-07-25.

### Fixed
- **`la_code_lookup` contains a confirmed error.** `E07000027` (Barrow-in-Furness) maps to `E06000063` Cumberland; the correct successor is `E06000064` Westmorland and Furness. `E07000028` (Carlisle) and `E07000189` (South Somerset) have no row at all. Verified against the ONS area pages for E06000063, E06000064 and E06000066 plus the Cumbria and Somerset (Structural Changes) Orders 2022. S6 works around this in a build-local resolution layer and does **not** write to the shared table; the repair is tracked as a separate remediation task.
- This supersedes the note in the 2026-07-22 entry below, which recorded `E07000028` and `E07000189` as "extinct LAs with no successor in `la_code_lookup`" and treated them as harmless. They are not extinct: both have successors, and the reason they were missing was a transcription error that also misrouted a third code. That mis-classification is why the pipeline now has a standing rule that unresolved codes are reported as UNEXPLAINED, never as harmless, benign or expected.
- **Source 15 renumbering completed.** `s15_hpi_build.py` still wrote `source_number = 19` to `pipeline_run_log`, so the collision with S19 (DWP PIP) that the renumbering was meant to resolve was still live under a filename that said otherwise. The script's docstring, temp directory, run-log `agent_name` and `source_number` now all read 15, and `s15_hpi_source.md` names the correct refresh script. `index.html` carries a one-line comment change only — no layer definition, legend rule, field name or data source URL is affected, so nothing the map renders changes.

### Notes
- **S6 is standalone.** No Workflow 1 integration, no `staging_la_signals` column, no tenant type, no map layer, no composite index — the same pattern as S19 PIP. Its data is not exported to this repository; it lives only in Postgres.
- **Loaded from 2018 Q1, not the full 2014 series.** Section 4 carries no LA geography before 2018, so earlier quarters cannot be aggregated consistently across support types.
- **Two structural breaks make England totals non-comparable before 2025-03-31.** Section 98 gained LA geography at 2022-12-31, so England rises from 53,749 to 98,375 as a reporting change rather than arrivals. Subsistence Only lost LA geography for five quarters to 2024-12-31, which accounts for most of the apparent swing in LA coverage.
- **Zeros are never published**, so an absent LA means "not published" rather than "none". Confirmed independently by the distribution check: 344 present − 164 under 100 = 180 at or above, against a published 181 of 361 under 100 implying 180.
- Figures use the person's registered address, not necessarily where they reside, and exclude unaccompanied asylum-seeking children. S6 is not a count of all asylum seekers in an area.
- `Asy_D09` is downloaded at verification time as an independent per-period reference and is never loaded into a table.
- **Until the `la_code_lookup` remediation lands, the database is inconsistent across sources on Cumberland (E06000063) and Westmorland and Furness (E06000064), and S6 is the only correct one.**
- The S8 register row still reads "Housing Benefit asylum seeker caseload" and is deliberately unchanged here. Correcting it belongs with the map layer relabel, not with the renumbering.

## [2026-07-22]
### Added
- Source 8b (DWP HB Accommodation Type Breakdown): `la_hb_accom_type_caseload` table with SA, TA, OTHER, UNKNOWN categories across 296 English LAs, 6 months (202509-202602). National SA total ~230k, TA ~112k. Schema discovered from Stat-Xplore REST API.
- Build script `scripts/s8b_hb_accom_type_build.py` — full pipeline: metadata discovery, batched table queries (50 LAs/batch), geography resolution via `la_code_lookup`, upsert via `json_to_recordset`, verification suite (coverage, boundary, anchor, consistency, reasonableness).
- New map layer: "HB Specified Accommodation" choropleth (mid-blue sequential ramp), inserted below "TA Households" in the layer list. Driven by `hb_sa_claimants_latest` in the signals JSON.
- `hb_sa_claimants_latest` field added to `staging_la_signals_latest.json` (296/296 LAs, latest month Feb-26).

### Changed
- W1 run 11: `staging_la_signals` gains `pip_total_claimants`, `pip_enhanced_daily_living`, `pip_rate_per_1000` — wired from `la_pip_claimants` (S19, Apr-26). Completes the HSS three-layer package (S11 supply + S19 demand + S9 flow) in a single row per LA.
- `docs/DATA_DICTIONARY.md`: `hb_sa_claimants_latest` added to Housing Benefit section.
- `docs/README.md`: HB Accommodation Type row added to data table; S8b script added to repo structure; review stamp updated to 2026-07-22.

### Notes
- Stat-Xplore returns monthly granularity for the accommodation type breakdown, not quarterly as the DWP release note implied. Refresh cadence recorded as monthly.
- The existing S8 table `la_hb_sa_caseload` (filtered to C_SATA:1) is unchanged. S8b stores the full breakdown in a separate table.
- Two historical geography codes (E07000028, E07000189) were unresolvable — these are extinct LAs with no successor in `la_code_lookup`. All 296 current LAD24CD codes are covered.
- Birmingham SA consistency check: 9.6% difference vs `la_hb_sa_caseload` for Nov-25 — within the 10% threshold, attributable to retrospective revisions.

## [2026-07-16]
### Added
- Source 19 (DWP PIP Claimants): `la_pip_claimants` table with `pip_total_claimants` and `pip_enhanced_daily_living` columns, 296 English LAs, month Apr-26. National total 3,710,753. Schema discovered programmatically from Stat-Xplore REST API `/schema` endpoint.
- Build script `scripts/s19_pip_build.py` — full pipeline: schema discovery with checkpoint caching, geography resolution, batched table queries (15 LAs/batch, exponential backoff on 504), upsert via `json_to_recordset`, 6-check verification suite.
- Representative query bodies: `s19_query_total.json`, `s19_query_enhanced_dl.json`.
- Node documentation: `docs/nodes/s19_node1..6*.md`.
- Source summary: `docs/s19_pip_source.md` (publisher, cadence, coverage, dual-lens note, refresh procedure).

### Changed
- `docs/METHODOLOGY.md`: S19 (DWP PIP) added to source register; `la_pip_claimants` added to architecture table list.
- `docs/README.md`: PIP Claimants row added to data table; S19 script and docs added to repo structure; review stamp updated.
- `.gitignore`: `s19_cache/` added (Stat-Xplore API response cache, local only).

### Notes
- DWP applies statistical disclosure control: values below rounding threshold appear as NULL (not zero).
- Stat-Xplore `/schema` paginates valueset members at 100; the build script follows `Link: rel="next"` headers automatically.
- Enhanced daily living is the sharper HSS-lens demand proxy (disability as core eligibility criterion for supported living). Total caseload is the broader measure.
- PIP caseload is not yet wired into Workflow 1 or the demand map. W1 integration and map visualisation are explicitly out of scope for this build.

## [2026-07-13]
### Added
- Source 9a (NHS DRD monthly discharge delays): 26 months (Apr 2024 – May 2026) loaded into `nhs_drd_discharge_delays` (3,978 rows, 153 UTLAs). UTLA→LAD apportionment via population-weighted `utla_lad_mapping` (296 rows) and `vw_drd_discharge_delays_lad` view.
- Source 9b (MHSDS MHS26 CRFD): 38 months (Apr 2023 – May 2026) loaded into `nhs_mh_crfd` (11,248 rows, 296 LAs). Barnsley/Sheffield code transition (E08000016→E08000038, E08000019→E08000039 from June 2025) handled via `la_code_lookup` and `vw_mh_crfd_lad` view.
- `staging_la_signals` gains three columns: `drd_bed_days_lost` (296/296), `drd_pct_delayed_1plus_days` (296/296), `crfd_days` (205/296 — 91 suppressed at source).
- Two new tenant types in `staging_tenant_type_rankings`: `mental_health` and `learning_disability`, both ranked by MHS26 CRFD days (205 LAs each). Both share the same signal — MHSDS does not disaggregate by cohort at sub-national level.
- W1 re-run as run 10 with all S9 columns populated. Seven tenant types now active.
- Node documentation: `docs/nodes/s9a_node1..3*.md`, `docs/nodes/s9b_node1..3*.md`, `docs/nodes/s9_utla_lad_mapping.md`, `docs/nodes/s9_w1_node5_patch.md`, `docs/nodes/s9_w1_node6_tenant_types.md`.
- Build summary: `docs/S9_BUILD_SUMMARY.md`.

### Changed
- `docs/DATA_DICTIONARY.md`: added Care Providers (Supply Side) section (`supported_living_locations`) and Discharge Delays section (`drd_bed_days_lost`, `drd_pct_delayed_1plus_days`, `crfd_days`).
- `docs/METHODOLOGY.md`: S9a and S9b added to source register; `nhs_drd_discharge_delays`, `nhs_mh_crfd`, `utla_lad_mapping` added to architecture table list; stale "NHS integration pending" gap replaced with three specific S9 limitations (CRFD disaggregation, DRD apportionment resolution, CRFD suppression rate).
- `docs/README.md`: Discharge Delays and CRFD rows added to data table; `S9_BUILD_SUMMARY.md` added to repo structure; review stamp updated.

### Notes
- CRFD cohort disaggregation is a known data gap — both `mental_health` and `learning_disability` tenant types share the same MHS26 signal. Deferred until a disaggregated source is available.
- DRD percentage columns are UTLA-level pass-through for county districts (all E07 districts under an E10 county inherit the same value).
- Monthly refresh not yet scheduled in n8n. Node 9 GeoJSON export query needs patching to include the three new columns.

## [2026-07-12]
### Added
- Demand map: "Care Providers (SL)" layer — active, non-dormant CQC supported living locations per LA, cream-to-amber quantile ramp, sidebar entry separated as the only supply-side layer, "Care Supply (CQC)" section in the detail panel, legend ends None/High. Dual-registered locations count twice by design (both registrations are regulated entities).
- W1 pre-computation now carries S11 counts: `staging_la_signals` gains `supported_living_locations` (active, non-dormant CQC supported-living locations per LA), W1 Node 5 joins `cqc_locations`, and the Node 9 export SQL in `n8n/workflow_nodes.json` includes the new column. W1 re-run completed (run 9); map data files (`data/boundaries/la_boundaries.geojson`, `data/signals/*.json`) re-exported from it.
- Source 11 (CQC registered care providers), the pipeline's only supply-side source: ETL scripts under `scripts/` (fetch with streaming ODS conversion, process, spatial LA mapping, load, verify) loading `cqc_locations` in the pipeline database - 30,492 Adult social care locations from the 1 July 2026 Care directory with filters, with supported living, personal care, care home and service-user-band flags for the dual UCWS/HSS lens.
- Refresh model: upsert on location ID with a deactivation sweep, so locations dropping off the monthly register keep a `deregistered_seen_date` instead of being deleted (supply-contraction signal).
- Node documentation `docs/nodes/s11_node1..7*.md` and decision record `docs/decisions/2026-07-12-s11-cqc-la-mapping-method.md` (the file's LA column is upper-tier only; mapping is spatial).
- Processed dataset `data/processed/cqc_locations_mapped.csv` (30,492 rows).

### Changed
- `docs/METHODOLOGY.md`: source register renumbered to match `pipeline_run_log.source_number`, the authoritative numbering (the table's own row numbers had drifted); S11 and Census tenure (3b) rows added; EFS and S.114 rows merged into source 12 (LA financial stress); `cqc_locations` added to the architecture table list.
- `docs/README.md`: repository structure updated for the S11 scripts and `docs/decisions/`; review stamp updated.

### Notes
- CQC is migrating its directory to a new digital system (their file README, July 2026): deregistrations can appear late and multi-service locations show as Not Rated. The monthly refresh should re-verify page and file layout each run.
- Five locations (0.016%) were excluded from the July 2026 load because their postcodes are unknown to ONSPD; they are listed in the Node 3 doc and will resolve on a later run.

## [2026-07-11]
### Added
- Source 18 (ONS Price Index of Private Rents): backfill ETL scripts under `scripts/` (fetch, inspect, transform, load, verify) loading `la_private_rents` in the pipeline database, periods 2024-03 onward, England LAs, bedroom and property-type breakdowns.
- Geography dimension tables `la_geography` (seeded from the ONS Code History Database, June 2026) and `la_succession` (seeded from `la_code_lookup` historical mappings) ahead of the 2027/2028 LGR wave.
- Processed dataset `data/processed/la_private_rents_17june2026.csv` (71,442 rows).
- Documentation: `docs/s18_pipr_source.md`, `docs/s18_pipr_workbook_structure.md` (n8n S18 build spec), `docs/geography_dimension.md`.
- Repository `.gitignore` and this changelog (repo predates both).

### Changed
- `docs/METHODOLOGY.md`: data-source register gains S14 (LHA rates, previously missing) and S18 (PIPR); database table list updated with the S14 and S18 tables.
- `docs/README.md`: repository structure updated for `scripts/` and new docs; machine-readable metadata block added.

### Notes
- Raw ONS downloads (`data/raw/`) are not committed — re-fetchable via `scripts/s18_pipr_fetch.py`.
- Raw rent levels are deliberately not added to the demand map; the derived LHA-vs-market-rent spread is separate future work.
