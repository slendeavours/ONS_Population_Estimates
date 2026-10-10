# Decision records — what is closed and what is still open

Each record was written against the state at the time and is left as written;
a record that gets quietly edited later is no longer evidence of anything.
This index is the reconciliation. **Read it first** — it says which findings
are settled and which are still live.

Status as at **2026-10-07** (the RO4 edition history and the quarterly refresh generalisation; before that, **2026-10-06**: S1 items 1 and 3, the S1b edition work and the open items below; the rest as at 2026-09-30). The verification suite exits **0**, all **21**
gates pass, and `known_red.json` is empty. The eight records of 2026-08-20
and 2026-08-21 were reconciled on 2026-09-30, each checked against the
live database, git tree or Pages runs rather than taken from its own text.

## Still open

Three things are registered but unresolved; the bullet under item 2 is latent. Each is discoverable from the data
as well as from here. S1 items 1 and 3 of the earlier list closed on 2026-10-06 and now sit in the Closed table.

### 1. S10 rough sleeping — suppression markers unverified

`la_rough_sleeping` holds 22 and 27 zeros with **no NULL anywhere** — the same
signature as S1's `..`-stored-as-zero defect. A genuine zero is plausible for a
snapshot count, so the table cannot settle it either way. It settles the way S1
did: extract from source and compare. S10 also fetches a pre-processed CSV with
no committed extraction code, so the extraction likely has to be written first.

### 2. A latent database defect, not affecting the map (two others closed 2026-10-07)

Found in the 2026-09-30 reference-CSV reconciliation; assessed the same day.
Nothing reads any of them, so none is urgent. See
`2026-09-30-reference-csvs-out-of-step.md`.

- `la_housing_register` keeps one arbitrary predecessor row for eleven
  reorganised authorities, 2015 to 2023. The map reads the latest year only,
  so it matters only if someone builds a waiting-list trend.
- **Closed 2026-10-07:** `ro4_housing_expenditure.hra_admin_prevention_relief_net_exp_000`
  held TA administration net spend. 2024-25 edition 3 (the third release) carries
  the correct line and the live table was refreshed from it: 258 authorities
  changed (250 value to value, 8 NULL to value).
- **Closed 2026-10-07:** the `total_housing_gross_exp_000` / `_net` mismatch (eight
  authorities with a homelessness total but a NULL housing total; Islington and
  Leicester differing). Edition 3 supplies the eight and corrects the others:
  21 cells. See `2026-10-07-ro4-edition-history.md`.

### 3. Left open by the S1 edition work

None of these was fixed by it.

- **The stale W1 period labels** remain. (The n8n S1 `|| 0` loader defect was closed 2026-10-07: see the Closed table.)
- **RO4 releases are not detected automatically.** A new release, such as the
  second 2025-26 release expected later, is spotted by hand and loaded as a new
  edition with `scripts/ro4_editions.py load` (procedure in
  [../QUARTERLY_REFRESH.md](../QUARTERLY_REFRESH.md)). The first and second
  releases of 2024-25 are not held and cannot be added before edition 1.
- **`scripts/s1b_support_needs_build.py` still prefers the release-page file.** A later S1b edition must be loaded with `scripts/s1b_editions.py load`; the build script would write the linked file over it.
- **The quarterly revision re-check is not automated.** Re-checking each loaded quarter for revision, and logging `revision_detected` in `source_check_log`, still has to be done by hand.
- **No insert-time trigger enforces `supersedes` = chain tip.** Only `load` checks it. A fork inserted another way would make `latest_edition` raise permanently.
- **Snapshot tables retained.** `la_statutory_homelessness_bak_20261006`,
  `la_homelessness_support_needs_bak_20261006` and `_bak_20261006b` are kept, but since
  2026-10-07 no code or gate depends on them (the refresh compares per-period hashes
  inside its own transaction). Scott may drop them when comfortable.

## Closed

| Record | What it settled |
| --- | --- |
| [2026-10-08-barnsley-sheffield-rule.md](2026-10-08-barnsley-sheffield-rule.md) | Barnsley and Sheffield handled per dataset: each source declares whether it publishes E08000016/19 (old), E08000038/39 (new), either by period (mixed), neither (none) or is not yet checked (unverified), in `scripts/geography.py`; loaders resolve through it and a load stops on a disagreement. Survey of all 27 registered sources; S15 adopted; three loaders (S9b, S15 build, S21) flagged by `check_loaders.py` for private recode dictionaries. Five sources unverified. |
| [2026-10-08-s19-month-keys.md](2026-10-08-s19-month-keys.md) | S19 (PIP claimants) months relabelled from text labels (`Apr-26`) to sortable `yyyymm` keys (rule 8) and revisions stored as editions (`la_pip_claimants_editions`, append-only, 1,184 rows: edition 1 for 202604 to 202607). A recheck of 202604 and 202607 a week after their first load found no change; revision is still not established. |
| [2026-10-09-s9a-editions-first-load.md](2026-10-09-s9a-editions-first-load.md) | S9a (NHS England Discharge Ready Date) is on the loader standard: revisions stored as editions (`nhs_drd_discharge_delays_editions` plus an append-only file-check ledger; 4,284 rows as edition 1 for 28 months, 153 UTLAs). First load: 27 months unchanged, June 2026 edition 2 is a CSV-to-webfile precision-only correction (734 cells, below 4e-9), August 2026 new (now the latest month). The two publisher revision waves (2025-07-10, 2026-07-09) predate the load and cannot be sized; next expected about July 2027. |
| [2026-10-09-s9b-editions-first-load.md](2026-10-09-s9b-editions-first-load.md) | S9b (NHS England MHSDS measure MHS26, delayed-discharge days by local authority) is on the loader standard: revisions stored as editions (`nhs_mh_crfd_editions` plus an append-only file-check ledger; 11,840 rows as edition 1 for 40 months, 296 areas). First load: the publisher's year-end Final files stored as edition 2 for 36 months (April 2023 to March 2026; 5,038 area values changed, national totals up in 35 months, largest +33.0%); April and May 2026 unchanged; August 2026 new (now the latest month). Next large event: the 2026-27 Finals in spring 2027. |
| [2026-10-09-s11-editions-first-load.md](2026-10-09-s11-editions-first-load.md) | S11 (CQC register of Adult social care locations, one file a month) is on the loader standard: dated snapshots (`cqc_location_snapshots` plus editions, an append-only file-check ledger and an unresolved table). The July and August 2026 snapshots the old upsert had overwritten were rebuilt from the files on disk (0 differences against the legacy residue rows) and September taken as loaded; October loaded as new (30,555 rows, 179 gone, 173 new, 4 unresolved; supported-living counts change in 87 authorities by at most 3). `cqc_locations` is now a view over the snapshots (legacy table kept as `cqc_locations_legacy`), equal to the old table and with W1's S11 counts unchanged by the swap. The five old scripts are retired to `scripts/historical/`; dropping `cqc_locations_legacy` is left as a separate later decision. |
| [2026-10-09-s4-editions-first-load.md](2026-10-09-s4-editions-first-load.md) | S4 (DfE care leaver accommodation, 17-21 and 22-25, two files a release) is on the loader standard: editions per reporting year (`care_leaver_accommodation_editions` plus a file-check ledger and live `null_reasons`). Edition 1 is the held table exactly (1,481 rows, hash recorded); edition 2 is the rule 1 correction, proved (5,289 cells, 0 differences; 396 of 396 22-25 rows were age 25 only). 2020 revised from the 2024 release (23 cells, 3 totals). Live refreshed with key changes (66 added, 25 removed): 1,522 rows, many cells now NULL instead of 0. Scott chose D1 (a): the map uses `semi_independent_published` (W1 SQL changed on the same branch; W1 not run). |
| [2026-10-09-s22-editions-first-load.md](2026-10-09-s22-editions-first-load.md) | S22 (MHCLG Council Taxbase and Live Table 615) is on the loader standard: three editions tables (`la_council_taxbase_empties`, `la_ctb_exemption_classes`, `la_vacant_dwellings_615`) plus two file-check ledgers and live `null_reasons`. Edition 1 is the held data exactly (296, 3,256 and 7,170 rows; hashes recorded); the held files read again reproduce every cell (0 differences) and Table 615 2025 all-vacants equals CTB empty plus unoccupied exemptions for 296 of 296 authorities, so the two sources are reconciled and the old "not reconciled" wording is wrong. The old build's load ran once (2026-08-13, run-log 83) and overwrote nothing, though its code would have; its verify step ran at least twice and a duplicate run-log row (id 84) was deleted. No value changed; the map is unchanged. The January 2026 revision of the 2025 workbook (22 councils) cannot be recovered. The long-term-empty measure is unchanged pending Scott's decision D1 (ours 309,889 against MHCLG's 303,185, 178 of 296 councils differ, 179 rows with England). The four old scripts are retired to `scripts/historical/` and the wrong "not reconciled" wording is corrected in the docs and the registry. |
| [2026-10-09-s23-editions-first-load.md](2026-10-09-s23-editions-first-load.md) | S23 (RSH registered provider social housing stock by local authority) is on the loader standard: `rsh_rp_stock_by_la_editions` plus a file-check ledger. Edition 1 is the held data exactly (10,171 rows; before-state hash recorded); the held 2025 look-up tool (V1.1) read again reproduces every cell (122,052 cells, 0 differences) and reconciles inside itself. The old build's load ran once (2026-08-14, run-log 95) and overwrote nothing, though its code would have; its `num(None) -> 0` defect never fired (every stock cell published). V1.0 of the tool is not recoverable. No value changed; S23 is not a W1 input, so the map is unaffected. The two old scripts are retired to `scripts/historical/` and the unsupported "large share is sheltered and retirement housing" wording is removed from the docs, the registry and the old build's docstring. Next release 27 October 2026: `load` preview, then `--commit`. |
| [2026-10-10-s6-editions-first-load.md](2026-10-10-s6-editions-first-load.md) | S6 (Home Office asylum support by local authority: Asy_D11 and Reg_02) is on the loader standard: four editions tables (`la_asylum_support`, `la_asylum_support_unallocated`, `asylum_support_non_england`, `la_immigration_groups`) plus four file-check ledgers. Edition 1 is the held data exactly (21,953 + 84 + 2,553 + 7,104 rows over 34 + 28 + 34 + 2 periods; 98 per-period before-state hash lines recorded); the held files read again reproduce every stored column (0 differences) and reconcile against their own pivot cache, Asy_D09 and each other. The old build ran 15 times (run-log 69 to 82 on 25 and 26 July 2026, 98 on 4 September; seven runs before its code was first committed) and upserted every period each run, but the March and June 2026 files agree cell for cell, so the 4 September overwrite lost no value. Its Reg_02 discovery would have skipped an `.xlsx` release (latent); old asset URLs 301 to the newest file; the publisher's November 2025 Reg_02 reissues kept their cover dates (`--accept-reissue`). Barnsley and Sheffield `mixed`; a merged authority's name comes from the highest publisher code; the held LOWER BOUND wording is the held marker, not a publisher statement. No value changed; S6 is not a W1 input, so the map is unaffected. Next release 26 November 2026: `load` preview, then `--commit`. Old scripts retired in a later commit. |
| [2026-10-09-s9-repro-tables-dropped.md](2026-10-09-s9-repro-tables-dropped.md) | The two S9 reproduction tables (`nhs_drd_discharge_delays_repro`, `nhs_mh_crfd_repro`) dropped (work plan R3) after checks showed nothing depends on or reads them; row counts and hashes recorded. |
| [2026-10-09-s18-editions-first-load.md](2026-10-09-s18-editions-first-load.md) | S18 (ONS Price Index of Private Rents) is on the loader standard (old pipeline retired to `scripts/historical/`): revisions stored as editions (`la_private_rents_editions`, append-only, 79,380 rows as edition 1 for 30 months, 294 areas, nine breakdown blocks). First comparison against the 16 September 2026 workbook found no change in any cell, so no edition 2 and the live table is unchanged (latest month August 2026, still provisional). Stop conditions are enforced in `load`; the live `source` column names the first-inserting edition. |
| [2026-10-08-s15-editions-first-load.md](2026-10-08-s15-editions-first-load.md) | S15 (UK House Price Index) is on the loader standard: revisions stored as editions (`la_house_prices_editions`, append-only, 20,355 rows); first comparison found 14 months revised (3,540 rows), latest month unchanged. |
| [2026-10-08-s8b-editions-first-load.md](2026-10-08-s8b-editions-first-load.md) | S8b (Housing Benefit by accommodation type) is on the loader standard: revisions stored as editions (`la_hb_accom_type_caseload_editions`, append-only, 8,288 rows); the live table is the latest-edition layer. |
| [2026-10-07-s1-n8n-loaders-retired.md](2026-10-07-s1-n8n-loaders-retired.md) | Former open item "n8n S1 node 2 `|| 0` defect". The two n8n steps that loaded S1 ('Merge & Build Batch' and 'Process Homelessness Data') stored every suppressed figure as 0, read columns by fixed position (the 2025Q4 layout moved every column, which once put support-needs figures in the wrong columns) and wrote straight over existing live rows. Replaced by `python scripts/s1_editions.py load-new`, which reads columns by header text, stores suppressed as NULL and a real zero as 0, checks the file's own cover sheet, re-reads the raw cells of all six stored measures (each cell classified independently; the A1 and TA1 column lookup and the code recode are shared with the loader, so a renamed header matching the wrong column would still pass for those) and records edition 1 and the live rows in one gated transaction; it refuses a quarter that already exists. Both n8n steps are retired with a hard stop (running either now fails with a `RETIRED` message), the old code is backed up in `build_reports/w1_node_backups/`, and the n8n workflow is no longer a loader for S1. Nothing else in the workflow changed. Procedure: [../QUARTERLY_REFRESH.md](../QUARTERLY_REFRESH.md). |
| [2026-10-07-ro4-edition-history.md](2026-10-07-ro4-edition-history.md) | Former open item "RO4 key needs edition treatment" and two of the three latent database defects. RO4 revisions are stored as editions (`ro4_housing_expenditure_editions`, append-only, 1,184 rows); the live table is the latest-edition layer. 2024-25: edition 1 is what the live table held (published 18 Sep 2025), edition 2 is **actually the second release (4 Dec 2025), stored under a wrong 'third release' label**, edition 3 is the real third release (11 Jun 2026) and is in live: the `hra_admin_prevention_relief_net_exp_000` column is corrected on 258 authorities and 21 `total_housing` cells are fixed; the eight authorities (Birmingham, Slough, Warwick, Ashfield, North West Leicestershire, Guildford, Amber Valley, North Warwickshire) are populated, as the earlier records said. **Incident:** for under an hour on 2026-10-07 live held second-release values (those eight blank; B&B gross 664,821.57 instead of 710,748.00); no stored or sent deliverable used them. Cause: a release label in a loader constant was trusted without opening the file's cover sheet; every manifest entry now records stored and actual labels and a cover-sheet gate refuses a file whose front page disagrees. The map and W1 read 2025-26 only and are unchanged. The first release of 2024-25 is not held. |
| [2026-10-07-editions-quarterly-refresh.md](2026-10-07-editions-quarterly-refresh.md) | Former open item "`refresh-latest` is specific to the 2026-10-06 reload" (S1 and S1b). `refresh-latest` now updates any quarter whose live rows differ from its latest edition; `sync-new` records a new quarter as edition 1; `status` says what needs action; drift (live differs from the latest edition and matches no stored edition) halts unless `--accept-drift`; the snapshot tables no longer back any gate. Procedure: [../QUARTERLY_REFRESH.md](../QUARTERLY_REFRESH.md). |
| [2026-10-06-s1b-edition-history.md](2026-10-06-s1b-edition-history.md) | S1b revisions are stored as editions (`la_homelessness_support_needs_editions`, append-only, 174,640 rows); the live table is the latest-edition layer and now holds the revised files for 2023Q2 to 2024Q4 and 2025Q2, label included. Closes the open item that S1b was stale. **The earlier statement that `s1b_support_needs_verify.py` "fails 6 of 7 gates" was wrong:** six of seven passed, and gate 6 failed because the table held a revision the release-page resolver does not link. Gates 6 and 7 repaired; all 7 pass. |
| [2026-10-06 S1 back-series revision (former open item 1)](2026-10-06-s1-edition-history-design.md) | Closed 2026-10-06 with the edition work and [s1-edition-diffs-2026-10-06.md](s1-edition-diffs-2026-10-06.md). **The premise recorded in the 2026-08-16 record was wrong, and that record is left as written.** The "200 to 230 authorities differ" figure was measured against the older release-page files. Edition 1's A1 figures already matched the registry 'revised' files numerically: the only A1 differences are zero to NULL for authorities that did not submit (36 authority-quarters, 144 cells: 3, 8, 4, 7, 5, 7 and 2 authorities in 2023Q2 to 2024Q4, each across all four A1 measures); no A1 figure is a numeric revision. The 200 to 230 divergence was against the older release-page files. The genuine value revisions in edition 2 are the A3 `support_needs_total` (about 175 to 190 authorities per quarter); `households_in_ta` also changes zero to NULL (95 cells). The seven quarters reproduce from source (`reproduces_from_source` true, 0 cells differ). The 2025Q2 revision (MHCLG, 30 April 2026) was applied in place on 2026-10-05 (`pipeline_run_log` 135), an acknowledged departure from the additive rule; it is now edition 2 of 2025Q2 and the live layer matches it. |
| [2026-10-06 National TA year-on-year not like-for-like](../../sql/w1_national_aggregates_like_for_like.sql) | Closed 2026-10-06 (W1 run 23). The National Aggregates node divided the all-reporting current total (130,775) by the all-reporting prior-year total (115,431), so authorities present in only one quarter moved the percentage: **+13.29% was reported; the 271 authorities reporting TA in both 2025Q4 and 2024Q4 give 114,471 against 110,989, +3.14%**. The node now computes the percentage over that matched set and `staging_national` carries `ta_matched_authorities`, `ta_households_current_matched` and `ta_households_prev_year_matched` beside it; `ta_households_current` and `ta_households_prev_year` keep their meaning (England totals of authorities reporting in each period) and are not comparable with each other. The rough-sleeping prior-year line no longer hardcodes `snapshot_year = 2025`. Gate 16 now asserts the matched relationship; `scripts/verify_national_ta.py` recomputes it independently. **An interim figure of "about -0.8%" given on 2026-10-06 was wrong:** it removed the authorities with no prior year from the current total but left the 12 authorities with no current figure (4,442 households) in the prior total. Run 22 and earlier are left as they were. No delivered report carried the figure (the only stored report is run 4, April 2026, unsent). The LLM Report Generation workflow (inactive) was changed the same day so a report quotes only the matched figures and a derived period label (`scripts/report_workflow_national_ta.py`). |
| [2026-10-06 S1 stored zeros (former open item 3)](2026-10-06-s1-edition-history-design.md) | Closed 2026-10-06. 108 `households_in_ta` zeros were stored across the seven quarters: 95 were suppressed values, now NULL in edition 2 and the live layer, and 13 are published zeros, kept. The 129 stored zeros in the 2026-08-16 record and the earlier changelog is that record's own earlier figure and was not reproduced. |
| [2026-10-06-s1-edition-history-design.md](2026-10-06-s1-edition-history-design.md) | S1 revisions are stored as editions (`la_statutory_homelessness_editions`, append-only); the live table is the latest-edition layer. Seven back quarters loaded as edition 2, 2025Q2 revision represented as edition 2. Closes former open items 1 and 3; diffs in [s1-edition-diffs-2026-10-06.md](s1-edition-diffs-2026-10-06.md). |
| [2026-09-30-telford-no-housing-register.md](2026-09-30-telford-no-housing-register.md) | Telford has no housing register; its reported 0 is not applicable. Set to NULL, run 20 exported, map shows No data. |
| [2026-08-21-s14-brma-join-break.md](2026-08-21-s14-brma-join-break.md) | An accidental import rebuilt S14 and left a name-spelling mismatch and a consumed header row. 152 rate rows and 0 ampersand mappings verified live 2026-09-30. Gate 18 added. |
| [2026-08-21-derived-view-dropped-unnoticed.md](2026-08-21-derived-view-dropped-unnoticed.md) | A derived view dropped by the S20 rename and found only by a downstream crash. View present, 155 rows, one rate card date, verified live. Gates 19 to 21 added; two gate-19 branches (zero-row, unrunnable view) remain unexercised. |
| [2026-08-20-s3b-tenure-rebasing-error.md](2026-08-20-s3b-tenure-rebasing-error.md) | Census tenure wrong for the four 2023 unitaries. Cumberland verified at 125,424, the published figure. |
| [2026-08-20-s20-neutral-object-names.md](2026-08-20-s20-neutral-object-names.md) | S20 tables and views renamed so the counterparty is not disclosed. `commercial_rate_card` and `commercial_rate_area_mapping` are the neutral names and exist live. |
| [2026-08-20-s12-efs-misattribution.md](2026-08-20-s12-efs-misattribution.md) | EFS rows attributed to Hammersmith and Fulham belonged to Haringey. Verified: Haringey holds both rows (£40.6m, £84.0m), Hammersmith and Fulham none. |
| [2026-08-20-repo-scope.md](2026-08-20-repo-scope.md) | The repository is the reproducibility record, not business analysis; 27 files removed and history purged. Verified: no removed path is tracked. |
| [2026-08-20-nojekyll.md](2026-08-20-nojekyll.md) | Jekyll broke Pages publication on n8n `{{` syntax; `.nojekyll` added. Verified: file present, latest Pages builds succeeded (2026-09-30). |
| [2026-08-20-demand-map-assurance.md](2026-08-20-demand-map-assurance.md) | Map re-exported from corrected data (run 18): stale workflow backup, stale house-price file, source count, MARAC and care-leaver labelling. Canvas was never rendered for a visual check (no WebGL in the browser pane). |
| [2026-09-04-s6-reg02-hardcoded-snapshot-period.md](2026-09-04-s6-reg02-hardcoded-snapshot-period.md) | Reg_02's snapshot period was fixed in source, so each S6 refresh overwrote the previous snapshot in place. Caught by Check 9 on the first refresh; no row count, key or coverage test was sensitive to it. Period now derived from the edition. |
| [2026-08-16-s114-attribution-and-gate-14.md](2026-08-16-s114-attribution-and-gate-14.md) | S.114 notices attributed to the issuing authority, never propagated. Gate 14 narrowed to require an unresolved code to declare itself. Last red gate cleared. |
| [2026-08-16-s1-reconstruction-markers-and-revision.md](2026-08-16-s1-reconstruction-markers-and-revision.md) | S1 extraction rebuilt; `period` is a financial-year quarter; markers corrected for the reproducible quarters; `support_needs_total` corrected across all seven. **Partly open — see above.** |
| [2026-08-16-data-quality-derived-from-every-column.md](2026-08-16-data-quality-derived-from-every-column.md) | `data_quality` derived from all 33 signal columns rather than 4. Gate 16 added. |
| [2026-08-15-w1-null-safety-audit.md](2026-08-15-w1-null-safety-audit.md) | Every W1 label made NULL-safe. Gate 15 added. |
| [2026-08-15-w1-period-pin-restatement.md](2026-08-15-w1-period-pin-restatement.md) | Hardcoded period literals removed from both W1 nodes. |
| [2026-08-14-s1-support-need-column-misalignment.md](2026-08-14-s1-support-need-column-misalignment.md) | Five support-need columns quarantined. **Mechanism identified 2026-08-16**: the 2025Q4 A3 restructure shifted the block three columns left, which is why the misalignment varied by quarter rather than being a constant offset. |
| [2026-08-14-s1-quarter-gap-and-provenance.md](2026-08-14-s1-quarter-gap-and-provenance.md) | The 2025Q1 gap and unrecorded 2025Q3 provenance. Both closed 2026-08-16. |
| [2026-08-14-s8-superseded-by-s8b.md](2026-08-14-s8-superseded-by-s8b.md) | S8 deprecated in favour of S8b. |
| [2026-08-14-barnsley-sheffield-code-split.md](2026-08-14-barnsley-sheffield-code-split.md) | E08000038/39 resolved through `la_code_lookup` on `recode` only. |
| [2026-08-13-stored-node-drift-and-register-authority.md](2026-08-13-stored-node-drift-and-register-authority.md) | The stored n8n node is the authority; write-back in the same session. |
| [2026-07-26-la-code-lookup-full-audit.md](2026-07-26-la-code-lookup-full-audit.md) | Full lookup audit. Dorset 2019 districts still absent. |
| [2026-07-25-la-code-lookup-cumbria-off-by-one.md](2026-07-25-la-code-lookup-cumbria-off-by-one.md) | Cumbria/Somerset mapping error. |
| [2026-07-25-credential-default-exposure.md](2026-07-25-credential-default-exposure.md) | Shipped-default credentials. `N8N_ENCRYPTION_KEY` rotation still outstanding. |
| [2026-07-22-hb-accom-type-publication-lag.md](2026-07-22-hb-accom-type-publication-lag.md) | S8b publication lag is monthly, not quarterly. |
| [2026-07-12-s11-cqc-la-mapping-method.md](2026-07-12-s11-cqc-la-mapping-method.md) | CQC locations mapped by point-in-polygon. |

## The recurring defect, across ten of these records

**State from evidence, never from intent.** Every instance took the same shape:
something recorded what was *meant* to be true and nothing checked whether it
*was*.

`homelessness_quarter_urls.loaded = true` with zero rows. A fingerprint
short-circuit reporting `no_change` without fetching. A hardcoded period pin
that was right the day it was typed. `..` stored as `0`, collapsing absent and
zero. `data_quality` certifying 4 columns while claiming to cover the row.
A `falling_strongly` label over an absent measure. A known-red entry outliving
its defect and absorbing the next real failure. A hand-edited registry field
that reverts for 12 sources and persists for 15. A backup note asserting work
was outstanding when it had been running for three weeks. A snapshot period
fixed at the date of the build that wrote it, silently destroying a quarter of
history on every refresh.

The countermeasure is the same each time: derive the claim from the data, and
have a gate assert it.

## Two diagnostics worth reusing

**Rate is a signature, not just a magnitude.** A divergence hitting 99% of
authorities on one column while hitting 75% on the others is not a revision —
revisions touch the authorities that resubmitted, misalignment touches
everyone. That is what separated the two S1 defects, which had been read as
one.

**Test the data, not the code.** A code read reported `revision_note` as
hand-maintained when the backfill demonstrably writes it. Perturbing every
column and seeing what the backfill restored got it right, and revealed that
management is per *source* as well as per column.

## Where the controls live

- `scripts/verify_source_registry.py` — 21 gates. Exit 0 clean, 2 known-red
  only, 1 stop.
- `scripts/push.py` — the only sanctioned push. Scan, verify, then push, in one
  place so the order cannot be forgotten. `--install-hook` gates a bare
  `git push` too.
- `docs/GENERATED_FIELDS.md` — which registry fields are generated and from
  where.
- `docs/KNOWN_RED.md` — currently empty, and why a stale entry is dangerous.
