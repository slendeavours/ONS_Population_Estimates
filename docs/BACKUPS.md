# Backup register

Every `_bak_` table, and every data backup kept as a file, is listed here under [RULES.md rule 9](RULES.md#9-backups). Gate 22 of `python scripts/verify_source_registry.py` reads the table below and fails for a backup table that is missing from it, past its expiry, read by a view, function or foreign key, or holding a different row count.

How to use it:

- **Before** the one-off change, add a row: status `active`, rows and size as counted at creation, reason linking the decision note, who asked, expiry 30 days after creation.
- **Extend** (once, up to 30 days): fill `Extended to` and add a dated line to the log below naming the table and the word "extended".
- **Drop** (only with Scott's approval, each time): set status `dropped` and fill `Dropped` with the date, the row count and a sha256 of the content at the drop. The row stays as history; rows are never deleted from this register.
- Status `modified` means the backup's rows changed after creation. It should never be used; a modified backup is no longer a copy of anything.

## Tables

The 19 tables below were made before the rule existed and are **grandfathered** (registered 2026-10-12 at Scott's request). Their created date is read from the name; rows and size were counted read-only on 2026-10-12, not at creation. The editions work they protected was signed off on 2026-10-12, so each expires 30 days later, on **2026-11-11**. None of them is read by W1, the export or any view (checked by gate 22). "Not recorded" means no note says who asked for the copy.

| Table | Created | Source table | Rows | Size MB | Reason | Asked by | Expiry | Extended to | Status | Dropped |
|---|---|---|---|---|---|---|---|---|---|---|
| `care_leaver_accommodation_bak_20260820` | 2026-08-20 | `care_leaver_accommodation` | 1,481 | 0.33 | S4: made by the August rebuild, which recreated it on every run, so it holds the state after the first run ([S4 note](decisions/2026-10-09-s4-editions-first-load.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `commercial_rate_card_bak_20260820` | 2026-08-20 | `commercial_rate_card` | 203 | 0.04 | S20 (private rate card): copy from the August 2026 assurance programme, renamed under the neutral-names decision ([S20 note](decisions/2026-08-20-s20-neutral-object-names.md)). Private tier: name and count only here | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_efs_support_bak_20260820` | 2026-08-20 | `la_efs_support` | 113 | 0.04 | S12: before the Haringey re-attribution; holds the two wrong rows ([misattribution](decisions/2026-08-20-s12-efs-misattribution.md), [S12 note](decisions/2026-10-10-s12-editions-first-load.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_hb_sa_caseload_bak_20260820` | 2026-08-20 | `la_hb_sa_caseload` | 296 | 0.03 | S8 (superseded by S8b): August 2026 assurance programme copy; no note gives a specific reason ([S8 note](decisions/2026-08-14-s8-superseded-by-s8b.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_homelessness_support_needs_bak_20261006` | 2026-10-06 | `la_homelessness_support_needs` | 101,232 | 54.98 | S1b: before the value refresh of the edition-history reload ([S1b note](decisions/2026-10-06-s1b-edition-history.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_homelessness_support_needs_bak_20261006b` | 2026-10-06 | `la_homelessness_support_needs` | 101,232 | 59.18 | S1b: before the label refresh, same reload ([S1b note](decisions/2026-10-06-s1b-edition-history.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_house_prices_bak_20260820` | 2026-08-20 | `la_house_prices` | 15,340 | 1.68 | S15: August 2026 assurance programme, when house prices were found to come from a second, stale source ([assurance note](decisions/2026-08-20-demand-map-assurance.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_housing_register_bak_20260820` | 2026-08-20 | `la_housing_register` | 3,256 | 0.40 | S13: before three 2025 rows were set by direct SQL to the June 2026 revision ([S13 note](decisions/2026-10-10-s13-editions-first-load.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_housing_register_bak_20260930` | 2026-09-30 | `la_housing_register` | 3,256 | 0.40 | S13: before Telford and Wrekin's values were set NULL ([Telford note](decisions/2026-09-30-telford-no-housing-register.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_private_rents_bak_20260820` | 2026-08-20 | `la_private_rents` | 74,088 | 9.48 | S18: August 2026 assurance programme copy; no note gives a specific reason ([assurance note](decisions/2026-08-20-demand-map-assurance.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_rough_sleeping_bak_20260820` | 2026-08-20 | `la_rough_sleeping` | 296 | 0.03 | S10: holds the table as before the 2026-08-19 rewrite, with the wrong 2021 and 2020 snapshots under 2025 and 2024 ([S10 note](decisions/2026-10-10-s10-editions-first-load.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `la_statutory_homelessness_bak_20261006` | 2026-10-06 | `la_statutory_homelessness` | 3,256 | 0.56 | S1: before the live layer was refreshed from the new editions table ([S1 note](decisions/2026-10-06-s1-edition-history-design.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `marac_cases_bak_20260820` | 2026-08-20 | `marac_cases` | 266 | 0.07 | S17: before the 2025-26 rows were loaded ([S17 note](decisions/2026-10-10-s17-editions-first-load.md)) | not recorded | 2026-11-11 | - | grandfathered | - |
| `ro4_housing_expenditure_bak_20260820` | 2026-08-20 | `ro4_housing_expenditure` | 296 | 0.07 | S2 (RO4): August 2026 assurance programme copy; no note gives a specific reason | not recorded | 2026-11-11 | - | grandfathered | - |
| `staging_convergence_bak_20261001` | 2026-10-01 | `staging_convergence` | 296 | 0.04 | W1 output as at run 21 (2026-10-01 22:41 UTC); no note records why it was taken | not recorded | 2026-11-11 | - | grandfathered | - |
| `staging_la_signals_bak_20261001` | 2026-10-01 | `staging_la_signals` | 296 | 0.27 | W1 output as at run 21; as above | not recorded | 2026-11-11 | - | grandfathered | - |
| `staging_national_bak_20261001` | 2026-10-01 | `staging_national` | 1 | 0.01 | W1 output as at run 21; as above | not recorded | 2026-11-11 | - | grandfathered | - |
| `staging_runs_bak_20261001` | 2026-10-01 | `staging_runs` | 1 | 0.02 | W1 output as at run 21 (the one row is run 21); as above | not recorded | 2026-11-11 | - | grandfathered | - |
| `staging_tenant_type_rankings_bak_20261001` | 2026-10-01 | `staging_tenant_type_rankings` | 1,006 | 0.16 | W1 output as at run 21; as above | not recorded | 2026-11-11 | - | grandfathered | - |

Total on 2026-10-12: 19 tables, 127.8 MB.

## Files

A data backup kept as a file follows the same rule (name, row here, expiry); gate 22 reads the database only, so these rows are checked by hand.

| File | Created | Copy of | Rows | Reason | Expiry | Status |
|---|---|---|---|---|---|---|
| `data/reference/statutory_homelessness_2025Q2_support_needs_before_revision.csv` (gitignored, local) | 2026-10-05 | `la_homelessness_support_needs` 2025Q2 | 9,176 | S1b before-image of the in-place 2025Q2 revision load, now held as edition 1 in `la_homelessness_support_needs_editions` ([S1b note](decisions/2026-10-06-s1b-edition-history.md)) | 2026-11-11 | grandfathered |

**Exempt: code backups.** `build_reports/w1_node_backups/` (10 files), `build_reports/w1_workflow_backup.json`, `build_reports/w1_node5_backup_20260815T153051Z.json` and `build_reports/s22_w1_node5_backup.json` are committed copies of n8n node and workflow code taken when a node was patched or retired. They hold no data, are never executed ([assurance note](decisions/2026-08-20-demand-map-assurance.md)), and are the evidence that a retirement read back what it replaced, so they are kept and have no expiry. The n8n workflow backups in the local n8n-era archive (outside git) are the same kind and are exempt for the same reason.

## Log

Dated lines for extensions and drops, newest last.

- 2026-10-12: register created; 19 tables and one file registered as grandfathered, expiry 2026-11-11 (Scott).
