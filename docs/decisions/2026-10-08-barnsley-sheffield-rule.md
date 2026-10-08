# 2026-10-08 — Barnsley and Sheffield: one rule, applied per dataset

## What was asked

Scott: these two areas pass or fail depending on which data is used; can there
be a rule that applies the right handling per dataset?

## Why they pass or fail

On 1 April 2025 (SI 1328/2024) Barnsley E08000016 became E08000038 and
Sheffield E08000019 became E08000039. `la_boundaries` is LAD May 2024, so the
pipeline's canonical codes are the earlier ones, and `la_code_lookup` holds
the two rows as `change_type = 'recode'` (E08000038 to E08000016, E08000039 to
E08000019).

Publishers do not agree on which codes to use, and some do not agree with
themselves:

- some publish the earlier codes (MYE mid-2025, built on 2023 boundaries; IMD
  2025; RSH stock; DRD; Stat-Xplore PIP; LAHS);
- some republish their whole back series on the new codes (UK HPI, PIPR,
  statistical neighbours);
- some switch at a period (S1 and S1b at 2025Q1, RO4 at 2025-26, Table 615 at
  2025, MHSDS at June 2025);
- **one MHCLG release uses both forms on different sheets.** The 2025Q2
  (202509) and 2025Q4 (202603) Detailed LA workbooks carry E08000038/39 on A1,
  TA1 and A3 (the sheets S1 and S1b read) but E08000016/19 on A7, and in
  202603 also on TA3, TA4, TA4c and TA4s. A loader that read one of those
  sheets alongside A1 would see both forms of one authority in one quarter.

Before this, the recode was written separately into eight places (S15 old and
new loaders, S18 transform, S2/RO4 twice, S9b, S21, the S1 extract and
`verify_reference_csvs.py`), each with its own assumption about what the
publisher does. When an assumption was wrong, the area either fell out of a
join (the 2026-08-14 record) or was resolved without anyone knowing the
publisher had changed.

## The rule (RULES.md rule 4.5)

- The canonical key stays E08000016/E08000019 while `la_boundaries` is LAD May
  2024.
- Every source declares its form in `scripts/geography.py` (`DATASET_FORM`),
  with the evidence: `old`, `new`, `mixed` (either, but one period never holds
  both forms of one area), `none` (no area-level data for these two, or keyed
  by something else), or `unverified` (the evidence says what to check).
- Loaders resolve the two areas only through `geography.resolve` (or
  `canonical`), which reads `la_code_lookup`; no private recode dictionaries.
- A load stops if the codes seen disagree with the declaration. The
  declaration is corrected deliberately, with the evidence, never silently.
  An `unverified` source loads, printing a one-line note to confirm and record
  its form.
- Moving `la_boundaries` to a vintage carrying the new codes flips the
  canonical form in one place, the `la_code_lookup` recode rows.

## How the survey was done

Read-only, 2026-10-08. For each of the 27 codes in `source_registry`:

1. The files on disk (`data/raw`, `data/reference`, `s19_cache`) were scanned
   for the four codes; for workbooks, per sheet where it mattered.
2. Where a table keeps the publisher's code
   (`la_homelessness_support_needs_editions.publisher_la_code`,
   `la_vacant_dwellings_615.published_la_code`,
   `rsh_rp_stock_by_la.publisher_la_code`), it was read by period.
3. The registry `join_path` and `known_gotchas`, the source docs, the decision
   notes and the loaders were read.

Every live table now stores the canonical codes, so a table without a
publisher-code column says nothing about the publisher. Where no file or
publisher code was available, the source is `unverified`, not guessed.

## Survey

| Source | Form | Evidence |
|---|---|---|
| 1 | mixed | MHCLG Detailed LA workbooks in data/raw/s1b_a3: 2023Q2-2024Q4 carry E08000016/19 only; 2025Q1-2025Q4 carry E08000038/39 on the three sheets S1 reads (A1, TA1, A3). The same releases carry E08000016/19 on other sheets (A7 in 202509 and 202603; TA3, TA4, TA4c, TA4s in 202603), so a loader reading those sheets must check each sheet separately. |
| 1b | mixed | la_homelessness_support_needs_editions.publisher_la_code: E08000016/19 for 2023Q2-2024Q4, E08000038/39 for 2025Q1-2025Q4, never both in one period; the A3 sheets in data/raw/s1b_a3 agree. |
| 2 | mixed | RO4 2024-25 second and third releases (data/reference .ods) carry E08000016/19 only; the 2025-26 first release carries E08000038/39 only. One form per financial year. |
| 3 | old | data/raw/s3_mye/mye25tablesew.xlsx (mid-2025, built on 2023 LA boundaries) carries E08000016/19 only (METHODOLOGY, S3; registry known_gotchas). |
| 3b | unverified | Census 2021 TS054 via NOMIS: la_tenure_2021 holds E08000016/19 and no recode is recorded, but no source file is on disk. At the next load, record the codes NOMIS returns for Barnsley and Sheffield. |
| 4 | unverified | DfE care leaver files (new_la_code) are not on disk; verify/rebuild_care_leavers.py resolves every code through la_code_lookup, so either form would load. At the next load, record which code new_la_code gives for Barnsley and Sheffield. |
| 5 | old | File_10_-_IoD2025_Local_Authority_District_Summaries__lower-tier__v2.xlsx and imd_2025_la_summary.csv (data/reference) carry E08000016/19 only. |
| 6 | unverified | Asy_D11 and Reg_02 carry E08000038/39 (docs/s6_asylum_source.md: the two recodes resolve forward), but no file is on disk and la_asylum_support keeps no publisher code, so whether earlier periods carry E08000016/19 is not known. At the next load, list the codes by period_ending and record new or mixed. |
| 7 | old | la_boundaries is LAD May 2024 and holds E08000016/19; this source defines the canonical key. |
| 8 | unverified | Superseded by 8b; no loader runs. If it is revived, check the Stat-Xplore HB geography valueset for Barnsley and Sheffield. |
| 8b | unverified | Stat-Xplore HB admin LA valueset (V_C_ADMIN_LA) members are not kept on disk; resolve_geography maps either form through la_code_lookup. At the next load, record which codes the valueset lists for Barnsley and Sheffield. |
| 9a | old | All 27 DRD monthly webfiles in data/raw/s9a_drd (April 2024 to July 2026) carry E08000016/19 only (UTLA codes). |
| 9b | mixed | MHSDS MHS26 uses E08000016/19 to May 2025 and E08000038/39 from June 2025 (docs/S9_BUILD_SUMMARY.md; the 2026-08-14 scan found nhs_mh_crfd split 52/24 before resolution). No file on disk. |
| 10 | new | Rough sleeping snapshot autumn 2025 (data/reference .ods and rough_sleeping_snapshot_2025*.csv) carries E08000038/39 only; the 2026-08-14 scan found la_rough_sleeping on the new codes only before resolution. |
| 11 | none | CQC locations carry no GSS codes for these authorities (data/raw/s11_csv: none of the four codes); lad24cd is assigned by point-in-polygon against la_boundaries. The postcodes.io fallback is a second source and may return E08000038/39; it resolves through la_code_lookup. |
| 12 | none | No EFS or S.114 row for Barnsley or Sheffield in la_efs_support, la_s114_notices or data/reference/la_s114_notices.csv; if either appears, the load stops and the form is declared from that file. |
| 13 | old | data/reference/LAHS_open_data_1978-79_to_2024-25.csv and lahs_waiting_list_2015_2025.csv carry E08000016/19 only, including 2024-25. |
| 14 | none | Keyed by BRMA name; lad24cd comes from la_brma_mapping, built from BRMA polygons. |
| 15 | new | Average-prices-2026-07.csv and Average-prices-Property-Type-2026-07.csv (data/raw, read 2026-10-08) carry E08000038/39 for every month 1995-01 to 2026-07 and never E08000016/19: each edition republishes the whole back series on current codes. |
| 17 | none | Keyed by police force area; data/reference/marac_data_2018_2025.csv carries none of the four codes; lad24cd via la_pfa_mapping. |
| 18 | new | PIPR editions pipr_17june2026, pipr_22july2026, pipr_16september2026 (data/raw) carry E08000038/39 only, for the whole back series (docs/s18_pipr_workbook_structure.md). |
| 19 | old | Stat-Xplore PIP geography valueset V_C_MASTERGEOG21_LA_TO_REGION (s19_cache/discovery.json, 2026-10-01) lists Barnsley and Sheffield as E08000016/19 and has no E08000038/39 member. |
| 20 | none | Withheld source (commercial in confidence), keyed by the supplier's own area names; it carries none of the four codes and no loader resolves publisher codes for it. |
| 21 | new | data/raw/ons_statistical_neighbours_2026.xlsx (Mar-2026 edition) carries E08000038/39 only (registry known_gotchas agrees). |
| 22 | mixed | Council Taxbase 2025 workbook (data/raw/s22_ctb and data/reference) carries E08000038/39 only; Live Table 615 (la_vacant_dwellings_615.published_la_code) carries E08000016/19 for 2004-2024 and E08000038/39 for 2025, one form per year. |
| 23 | old | RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx (data/raw/s23_rsh) and rsh_rp_stock_by_la.publisher_la_code (stock date 2025-03-31) carry E08000016/19 only. |
| 24 | none | RSH register has no geography (registry caveat); the register and judgements files carry none of the four codes. |

The same table is printed by `python scripts/geography.py`; the module is the
authority if the two differ.

## What changed in code

- `scripts/geography.py`: the declarations, `load_recodes`, `canonical`,
  `check_forms`, `resolve`, `confirm_note`, `declaration_report`.
- `scripts/s15_hpi_editions.py` takes its recode map from
  `geography.resolve` and halts on a disagreement. A preview against the
  2026-07 files passed the check. `HARD_RECODES` remains only as the default
  for the pure `build_records` tests.
- `scripts/check_loaders.py` check f: a loader outside `scripts/historical/`
  with a dict literal keyed by E08000038/39 fails with a reason naming
  `geography.py`. It adds that reason to S9b (`s9b_crfd_build.py`), S15
  (`s15_hpi_build.py`, the old loader still listed for S15) and S21; all three
  already failed. `ro4_editions.py` (S2, PASS) carries one in `raw_cells`, the
  independent raw re-read; it is listed in `GEOGRAPHY_PENDING` and reported as
  a note, so no PASS changed. The pass count is unchanged at 5 of 18.

## Not done

- No other loader is migrated. S18 adopts it in its own migration; the others
  as they are moved onto the standard. `s18_pipr_transform.py`,
  `s2_ro4_load.py`, `s1_extract_ods.py` and `verify/verify_reference_csvs.py`
  still carry their own recode, and are not in the checker's loader list.
- The five `unverified` sources (3b, 4, 6, 8, 8b) are checked at their next
  load.
- `docs/geography_dimension.md` records that the Barnsley change also moved a
  small area into Sheffield. `la_code_lookup` treats both as pure recodes; that
  is unchanged here.
