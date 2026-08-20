# Methodology — UCWS DV Pipeline

---

## Data Sources

**This table is the source register.** Numbers are assigned from here (gaps intentional). `pipeline_run_log.source_number` is an execution record, not the register — it has been the wrong authority to ask twice, and was backfilled on 2026-08-13 to agree with this table. See the standing rule below.

| S# | Source | Metric(s) | Publisher | Frequency |
|---|---|---|---|---|
| 1 | DLUHC H-CLIC | TA households (current + prev year), trend label | DLUHC | Quarterly |
| 1b | MHCLG statutory homelessness Table A3 | Households owed a duty by support need — 24 published categories, plus the no/unknown/one/two/three-or-more household breakdown and the support-needs count, per LA per quarter | MHCLG | Quarterly |
| 2 | MHCLG RO4 | Homelessness expenditure (B&B, nightly, total) | MHCLG | Annual |
| 3 | ONS Mid-Year Estimates | Population by LA (mid-2025, 2023 LA boundaries edition; mid-2024 retained) | ONS | Annual |
| 3b | Census 2021 TS054 | Tenure | ONS | Decennial |
| 4 | DfE Children Looked After (SSDA903) | Care leavers in supported accommodation (published DfE category); Care leavers in supported accommodation (wider pipeline aggregate) | DfE | Annual |
| 5 | MHCLG IMD | Index of Multiple Deprivation | MHCLG | Every ~5 years |
| 6 | Home Office Asy_D11 / Reg_02 | Asylum seekers in receipt of Home Office support by support type and accommodation type; immigration groups by pathway (standalone) | Home Office / MHCLG | Quarterly |
| 7 | ONS Open Geography Portal | LA boundary polygons (LAD **May 2024** BGC) | ONS | On boundary changes |
| 8 | DWP STAT-Xplore | Housing Benefit claimants in specified accommodation | DWP | Monthly |
| 8b | DWP Stat-Xplore HB (accommodation type) | HB claimants by accommodation type (SA, TA, Other, Unknown) per LA | DWP | Monthly |
| 10 | DLUHC Rough Sleeping Snapshot | Rough sleeping counts | DLUHC | Annual (autumn) |
| 11 | CQC Care directory with filters | Registered care locations with supported living, personal care and care home flags (supply side) | CQC | Monthly |
| 12 | MHCLG EFS / published S.114 notices | EFS support flag, S.114 notice flag | MHCLG / LAs | Published as issued |
| 13 | DLUHC LAHS | Social housing waiting list (register) | DLUHC | Annual |
| 14 | VOA/DWP LHA rates | LHA weekly rates (SAR, 1–4 bed) by BRMA, mapped to LAs | VOA/DWP | Annual (late January) |
| 15 | Land Registry UK HPI | Average house prices per LA (all property types), annual % change | HM Land Registry | Monthly |
| 17 | SafeLives MARAC data | MARAC cases, rate per 10k | SafeLives | Annual |
| 18 | ONS PIPR | Private market rent levels, index, annual change by LA (bedroom + property type) | ONS | Monthly |
| 19 | DWP Stat-Xplore PIP | PIP total claimants and enhanced daily living per LA (demand proxy for supported living) | DWP | Monthly |
| 20 | Commercial rate card (private) | Withheld — commercial in confidence. Held in `exempt_pipeline` only and never exported to this repository, the signals JSON or the map | Withheld | As supplied |
| 21 | ONS Clustering similar local authorities and statistical nearest neighbours in the UK | Five nearest statistical neighbours per LA (Table 7a, LTLA global) | ONS | Irregular (Mar 2026 edition) |
| 22 | MHCLG Council Taxbase (CTB form) + Live Table 615 | Dwellings empty six months or more, all empties, empty homes premium counts, second homes, unoccupied exemptions by class per LA; vacant and long-term vacant dwellings by district from 2004 | MHCLG | Annual (November, revised the following January); Table 615 updated with the dwelling stock live tables |
| 23 | RSH registered provider social housing stock (SDR + LADR) | Owned social stock per registered provider per LA: supported housing and housing for older people, general needs self-contained and bedspaces, low cost home ownership | Regulator of Social Housing | Annual (autumn; stock as at 31 March) |
| 24 | RSH register of registered providers and regulatory judgements | Provider registration number, name, registration date, designation, corporate form; consumer, governance, viability and rent gradings with dates; enforcement notices. Entity-level, no LA geography | Regulator of Social Housing | Monthly register; judgements as issued |
| 9a | NHS DRD monthly | Bed days lost to delayed discharge, % delayed 1+ days (UTLA→LAD apportioned) | NHSE | Monthly |
| 9b | MHSDS MHS26 | CRFD delayed discharge days — combined MH+LD/autism (direct LA level) | NHS Digital | Monthly |

S11 is the pipeline's only supply-side source: every other source measures need, S11 records existing CQC-registered provision. It is stored agnostically like everything else; the pipeline does not score or rank markets.

**Standalone sources.** S6 and S15 are loaded but not wired into Workflow 1: they add no `staging_la_signals` column and no tenant type. S6 is queried directly from its own tables; S15 reaches the map through its own `hpi_la_prices.json` rather than through the signals JSON.

**S19 is wired, not standalone.** This document previously listed S19 as standalone. That was wrong. `staging_la_signals` has carried `pip_total_claimants`, `pip_enhanced_daily_living` and `pip_rate_per_1000` since run 11, verified against `la_pip_claimants` at Apr-26 — Birmingham 93,196 / 50,002, Kingston upon Hull 24,195 / 12,078, Kensington and Chelsea 7,166 / 4,039, all exact. They are PIP columns, not a tail left by the S15 renumbering; S15's house prices live in `la_house_prices` and have no staging column at all. Corrected 2026-08-13 during the S22 build. `docs/README.md` had it right throughout.

**S6 caveats.** Asylum support figures are based on the person's registered address, which is not necessarily where they regularly reside, and **exclude unaccompanied asylum-seeking children**, who are supported by local authority children's services rather than Home Office asylum support. S6 is not a count of all asylum seekers in an area. Two structural breaks make the England series non-comparable before 2025-03-31; they are recorded in the `asylum_series_breaks` table and explained in `docs/s6_asylum_source.md`.

**S6 geography.** Resolved entirely through `la_code_lookup`. The build-local workaround S6 carried for three codes was retired on 2026-07-26 once the lookup was corrected; the reload reproduced the prior checksum byte-identically, confirming the workaround and the corrected lookup agree. See `docs/decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md` and `docs/decisions/2026-07-26-la-code-lookup-full-audit.md`.

### S3 — ONS mid-year population estimates

Refreshed to **mid-2025** on 2026-08-13, from *Estimates of the population for England and Wales*, edition **"Mid-2025: 2023 local authority boundaries"**, released 29 July 2026. Resolved from the dataset landing page at run time; no file URL is stored. Sheet `MYE2 - Persons`, header row 8, `All ages` column.

`la_population` is now **multi-year**. Its key was widened from `(lad24cd)` to `(lad24cd, reference_year)`, and the mid-2024 vintage is retained rather than overwritten: 592 rows, 296 per year. England total 58,834,812, reconciling exactly to the publisher's own England row.

**Two consequences that bit immediately, both worth knowing before the next vintage lands.**

Adding a vintage to a shared dimension table fans out **everywhere that dimension is joined**, not only in the obvious place. Node 5's `la_population` join and the `la_population` join inside `v_la_pip_rates` both had to be pinned to `MAX(reference_year)`. Unpinned, the fan-out did not silently double-count — it killed the statement with an `ON CONFLICT ... cannot affect row a second time` cardinality violation, which is the good failure mode, but W1 was genuinely broken between the load and the pin. Anything joining `la_population` in future must pin the vintage.

The Barnsley and Sheffield expectation was **wrong, and the reason matters**. The geography standing rule says to assume a source published after 1 April 2025 uses the recoded E08000038 and E08000039. This release postdates that by sixteen months and does *not* use them: the edition is explicitly built on **2023 local authority boundaries**, so it publishes E08000016 and E08000019, matching `la_boundaries` directly with no recode resolution needed. All 296 codes matched with zero orphans in either direction.

> **Refinement to the geography standing rule.** Where a release states its boundary vintage, that is the predictor — not the publication date. Publication date is the fallback for releases that do not declare one. Check the file either way; both codes were verified present or absent in the workbook rather than inferred.

The release covers 318 England-and-Wales local authorities. England is filtered on the code prefix (`E06`/`E07`/`E08`/`E09`), never assumed to be the whole file; 23 Welsh rows including the Wales country row are skipped, and `E10`/`E11` counties, `E12` regions and the `E92` country row are excluded as aggregates that would double count.

### S22 — MHCLG Council Taxbase empty homes

Two MHCLG publishers, both resolved from their landing pages at run time. No file URL is stored in the build.

| | Source A | Source B |
|---|---|---|
| Publication | Local authority Council Taxbase in England 2025 | Live Table 615: vacant dwellings by local authority district, England, from 2004 |
| Landing page | [Council Taxbase statistics collection](https://www.gov.uk/government/collections/council-taxbase-statistics) | [Live tables on dwelling stock including vacants](https://www.gov.uk/government/statistical-data-sets/live-tables-on-dwelling-stock-including-vacants) |
| Release date | 6 November 2025 | landing page last updated 25 June 2026 |
| Revision date | **21 January 2026** — Tables 1, 2, 3a, 3b and 4 revised after corrections from 22 authorities | — |
| Snapshot | Dwelling counts as at 10 September 2025 (VOA council tax list); discounts, exemptions and premiums derived at 6 October 2025 | one snapshot date per year, October |
| Geography | 296 English billing authorities, published on ONS codes | local authority districts as they existed in each year |
| Tables used | 1.01, 1.11, 1.17, 1.18, 1.19 (Council Taxbase Data sheet) and 2.01 (Supplementary Data sheet) | All_vacants, All_long_term_vacants |

**Tables.** `la_council_taxbase_empties` (296 rows, one per LA per taxbase year), `la_ctb_exemption_classes` (3,256 rows, the eleven unoccupied exemption classes at LA level), `la_vacant_dwellings_615` (7,170 district-year rows, 2004 to 2025), `ctb_series_breaks` (2 rows). Rates are derived in `v_la_empty_homes_rates` and are never stored.

**Coverage and its caveat.** 296 of 296 authorities for taxbase year 2025, complete. Only the current year is published in the release workbook, so a single year is loaded; the series is built up one release at a time from November each year. Table 615 covers 2004 to 2025, but **that series is not complete for any single geography over the full period**: 891 rows across 80 published codes are districts abolished under local government reorganisation and carry a null `lad24cd`. They are deliberately not aggregated into successor unitaries, because doing so would make any downstream sum count a successor once per predecessor district.

**Long-term empty is not the same as vacant.** The Council Taxbase counts dwellings a billing authority classes as empty for council tax purposes; Table 615 counts vacant dwellings on a different definition and a different snapshot date. They are held in separate tables and are not reconciled to each other.

**Structural breaks.** Both are recorded in `ctb_series_breaks` and cited to the MHCLG technical notes:

- **1 April 2024** — the Empty Homes Premium threshold moved from 2 years to 1 year. `empty_homes_premium_count` is not comparable across this date; the England figure rose 27.9% between the 2024 and 2025 taxbase years, which is a widened eligible population rather than more empty homes.
- **1 April 2025** — the Second Homes Premium was introduced, applied by 211 of 296 authorities. `second_homes` is affected by reclassification from this date: authorities reported reviewing empty properties and second homes ahead of the new premium, which moves dwellings between the two categories independently of anything changing on the ground.

**`premium_coverage_pct` is directional only.** It can never reach 100, because long-term empty starts at six months while the premium starts at twelve, so the numerator is drawn from a strictly narrower population than the denominator. It is not a compliance rate. The caveat is carried as a column comment on the view, not only in this document.

**Map layer.** One layer only, Long-Term Empty Rate, driven by `ctb_lte_rate_pct`. Total empties is held in the database and not mapped: it bundles second homes and short-term turnover, so a choropleth of it would misrepresent the picture. Premium application is likewise held and not mapped.

**Geography.** MHCLG publishes Barnsley and Sheffield under the codes recoded on 1 April 2025 (SI 1328/2024), E08000038 and E08000039, while `la_boundaries` is LAD May 2024 and carries E08000016 and E08000019. Both resolve through `la_code_lookup` as `change_type = 'recode'` — the same area under a new number. Nothing was written back to the lookup.

**`staging_la_signals` is a snapshot and goes stale between runs.** Run 17
(2026-08-16) predates the August 2026 source assurance work and still holds
pre-correction values for rough sleeping, care leavers, MARAC and both NHS
discharge measures. Anything reading staging rather than the base tables is
serving those old numbers, including the exported signals JSON the demand map
loads. The LandAid Liverpool paper was rebuilt to read base tables directly for
this reason. Re-running node 5 picks the corrections up without further work,
because every join selects MAX(period) from its source table. See
`docs/decisions/2026-08-20-s3b-tenure-rebasing-error.md` and
`docs/decisions/2026-08-20-s12-efs-misattribution.md`.

**Standing rule — direct SQL against `staging_la_signals` updates the stored node in the same session, or it is not applied.** Any change to the columns of `staging_la_signals` must be written back to W1 node 5 in `n8ndb` before the session ends. Applying it to the data alone leaves the stored node behind, and the next genuine workflow run silently drops every column the node does not know about. This is not hypothetical: runs 10 and 11 added the S9 and S19 columns by direct SQL and never wrote them back, so the stored node was two builds stale until the S22 build in August 2026 found it. The same rule covers anything that creates a `staging_runs` row outside the workflow — the row must be created through the Create Run node's query so the sequence stays ahead of the data. Runs 10 and 11 skipped that too, leaving the sequence trailing by two and the next `nextval()` set to collide with an existing run.

**The rule is enforced, not just stated.** A rule that relies on remembering is what failed twice, so the check runs in three places:

| Where | What it catches | Fires on |
|---|---|---|
| `Signal Column Pre-flight` node inside W1, between Create Staging Tables and Create Run | a table column the node does not write, or a node column absent from the table, compared against `staging_signal_contract` | **every workflow run**, before a run id is issued |
| `scripts/w1_contract_check.py` | the same, plus positional misalignment between the INSERT column list and the SELECT list, plus columns with no `EXCLUDED` refresh. Refreshes the contract from the stored node | the scripted path, called by `scripts/s22_w1_wire.py` before any run |
| `scripts/export_map_data.py` | backstop copy | every export |

The pre-flight lives inside W1 rather than only in the export path because W1 has been run without exporting; an export-time check alone would let a divergence sit undetected until the next publish. It fails in **both** directions. A column in the table and absent from the node is the failure that actually happened; a node naming a column that does not exist would throw on its own, but a node naming a column that exists and populating it from the wrong expression would not — that is what the positional check is for. Verified against a deliberately corrupted copy of the node: swapping two same-type expressions was reported as "position 37: inserts into `ctb_second_homes` but expression resolves to `ctb_empty_homes_premium`".

Only one path is uncovered: editing node 5 by hand in the n8n editor without re-running `w1_contract_check.py`. The contract then holds a stale `node_query_sha256` and the pre-flight compares the table against the old contract. Re-run the checker after any manual node edit.

**Standing rule — resolve geography before the orphan gate, not after it fails.** Every build resolves published codes through `la_code_lookup` as part of extraction, and only then checks for orphans against `la_boundaries`. Running the gate first wastes a gate on a known, predictable condition.

**Where a release declares its boundary vintage, that is the predictor — not the publication date.** Otherwise, assume any source published **after 1 April 2025** uses the recoded Barnsley and Sheffield codes E08000038 and E08000039, because `la_boundaries` is May 2024 and carries E08000016 and E08000019. This pair has appeared in S9b, S18, S21 and S22 and is predictable, not surprising. But the S3 mid-2025 refresh on 2026-08-13 published on **2023 local authority boundaries** despite postdating the recode by sixteen months, and so used the *old* codes. Verify against the file in either direction; do not infer from the date alone when a vintage is stated. Resolution is `change_type = 'recode'` only: a recode renumbers the same area and resolves, while `new_unitary` and `merger` are abolitions and must stay unmapped, because folding predecessor districts onto a successor makes every downstream sum count that successor once per predecessor.

**Standing rule — derived rates live in views, and `staging_la_signals` is the one documented exception.** Source tables never store a rate. `staging_la_signals` is a point-in-time snapshot, so it does carry derived columns, but every one of them must take its definition from a view rather than from an expression written inline in node 5. Otherwise the definition exists only in the node and cannot be audited or reused. `ctb_lte_rate_pct` comes from `v_la_empty_homes_rates`; `pip_rate_per_1000` was inline in node 5 until 2026-08-13 and now comes from `v_la_pip_rates`.

`v_la_pip_rates` also exposes `population_reference_year` next to the rate, because the numerator refreshes monthly and the denominator annually. A rate whose inputs refresh on different cadences can go stale against its own denominator without any row-level check noticing, so the denominator's vintage is published as data rather than left to documentation. The remaining inline derivations in node 5 — `ta_yoy_pct`, `ta_trend_label`, `data_quality` — are per-row transformations of columns already in the same SELECT, not cross-source rates, and are left as they are.

**Closed 2026-08-13 — the S3 refresh landed.** `pip_rate_per_1000` was Apr-26 claimants over a mid-2024 base; it is now over **mid-2025**. See the S3 section below. The three-layer HSS package (S11 supply, S19 PIP demand, S9 flow — 296/296 on all three at run 12) no longer carries a two-year denominator lag.

**Standing rule — anything that enumerates tables, columns or schema is scanned for counterparty names before it is staged.** Not before it is pushed: before `git add`. This repository is public.

S20 is the reason. It is a commercial rate card held in confidence, and **the counterparty's name is in the table names themselves**, so any artefact that lists the schema discloses it without ever mentioning the source. On 2026-08-13 a source register audit — a governance document with nothing to do with the map — was staged for this repository carrying exactly that. It was caught before the push, but after `git add`, and only because someone thought to look.

Two controls, because the rule alone is what failed:

- `confidentiality_scan.py` scans staged files, or all tracked files with `--all`, against a term list. It lives outside every git working tree, because a list of names you must not publish is itself a thing you must not publish. Verified: 135 tracked files clean, and it fires on the audit report.
- Artefacts that cannot be sanitised live **outside every git working tree**, not behind a `.gitignore` entry. An ignore rule can be overridden with `git add -f`; a different directory cannot. `source_register_audit.py` and its report were moved there on 2026-08-13 after it turned out that "kept local" meant "untracked inside a working copy of this repository", which is one `git add -A` from publication.

The publishable half of the audit's logic was split into `scripts/register_lib.py`, which names no tables, so `sync_readme_sources.py` can still use it.

**Standing rule — this document is the source register; `pipeline_run_log` is not.** Source numbers are assigned from the register table above. The run log is an execution record and has twice been the wrong authority to ask: S15 was built in July 2026 and never logged at all, and S9a, S9b and S8b were logged under keys (`s9a`, `s9b`, `8`) that did not match their register entries, so 9, 15 and 16 all read as free when only 16 was. The log was backfilled and normalised on 2026-08-13 and now agrees with this table. A new build reads the register for the number and checks the log only as a contradiction test — if they disagree, that disagreement is the finding, and neither number is used until it is explained.

**Standing rule — unresolved codes are UNEXPLAINED until explained.** Any build encountering codes it cannot resolve reports them as UNEXPLAINED, never as harmless, benign or expected. The explanation is a gate, not a note: an unresolved code is a hard stop, and the reason it is unresolved must be established against an authoritative source before deciding what to do about it. A missing entry in a shared lookup is evidence about the lookup, not only about the source in hand. Classifying a gap without investigating it is what let the `la_code_lookup` Cumbria error stand for two weeks after it was visible.

---

## Pipeline Architecture

```
Raw Sources (CSV / API)
        │
        ▼
  n8n Workflow 1
  (17 ingestion nodes)
        │
        ▼
  PostgreSQL 16
  exempt_pipeline DB
  ┌─────────────────────────┐
  │ la_boundaries           │ (296 rows, GeoJSON polygons)
  │ staging_la_signals      │ (296 rows per run_id)
  │ staging_runs            │ (1 row per pipeline run)
  │ brma_lha_rates          │ (S14: LHA rates by BRMA)
  │ la_brma_mapping         │ (S14: LA → BRMA crosswalk)
  │ la_private_rents        │ (S18: PIPR rents by LA/period/category)
  │ cqc_locations           │ (S11: CQC-registered care locations)
  │ nhs_drd_discharge_delays│ (S9a: DRD discharge delays at UTLA level)
  │ nhs_mh_crfd             │ (S9b: MHSDS MHS26 CRFD at LA level)
  │ utla_lad_mapping        │ (S9: UTLA→LAD pop-weighted crosswalk)
  │ la_pip_claimants         │ (S19: PIP claimants by LA/month)
  │ la_hb_accom_type_caseload│ (S8b: HB accom type by LA/month)
  │ la_population           │ (S3: multi-year, key (lad24cd, reference_year))
  │ la_house_prices          │ (S15: Land Registry HPI by LA/period)
  │ la_council_taxbase_empties│(S22: CTB empty homes by LA/taxbase year)
  │ la_ctb_exemption_classes │ (S22: unoccupied exemption classes by LA)
  │ la_vacant_dwellings_615  │ (S22: Table 615 vacants by district/year)
  │ ctb_series_breaks        │ (S22: documented structural breaks)
  │ la_geography            │ (geography dimension, code validity)
  │ la_succession           │ (predecessor → successor mappings)
  └─────────────────────────┘
        │
        ▼
  Node 9: Export Query
  (SQL Query 2 — full combined GeoJSON)
        │
        ▼
  Node 10: Validate
  (296 features, no NULLs, RFC 7946)
        │
        ▼
  Node 11: Publish to GitHub
  (git push via HTTPS token)
        │
        ▼
  GitHub raw URLs
  (la_boundaries.geojson, latest.json)
        │
        ▼
  Browser viewers
  (kepler_branded.html, kepler_basic.html)
```

---

## Key Calculations

### Year-on-Year % Change (TA)

```sql
ta_yoy_pct = ((ta_households_current - ta_households_prev_year)
              / NULLIF(ta_households_prev_year, 0)) * 100
```

Rounded to 2 decimal places. NULL when either input is NULL.

### Trend Label Assignment

Assigned from `ta_yoy_pct`:

```
ta_yoy_pct > +15%       → rising_strongly
+5% ≤ ta_yoy_pct ≤ +15% → rising
-5% ≤ ta_yoy_pct ≤ +5%  → flat
-15% ≤ ta_yoy_pct < -5% → falling
ta_yoy_pct < -15%       → falling_strongly
NULL or data gap         → submission_gap
```

### MARAC Rate per 10k Population

```sql
marac_rate_per_10k = (marac_cases / NULLIF(population, 0)) * 10000
```

### IMD Rank

The `imd_rank_of_average_rank` is sourced directly from MHCLG's published IMD LA summary. It ranks LAs from 1 (most deprived) to 317 (least deprived) based on the average rank of constituent LSOAs.

---

## GeoJSON Export

The combined export query joins `la_boundaries` and `staging_la_signals`:

```sql
SELECT jsonb_build_object(
  'type', 'FeatureCollection',
  'metadata', jsonb_build_object(
    'generated_at', NOW()::text,
    'run_id', (SELECT MAX(run_id) FROM staging_la_signals)::text,
    'feature_count', COUNT(*)::text
  ),
  'features', jsonb_agg(
    jsonb_build_object(
      'type', 'Feature',
      'geometry', b.geojson,
      'properties', jsonb_build_object(
        -- all 22 signal columns
      )
    )
  )
)
FROM la_boundaries b
LEFT JOIN staging_la_signals sig
  ON sig.lad24cd = b.lad24cd
  AND sig.run_id = (SELECT MAX(run_id) FROM staging_la_signals)
WHERE b.geojson IS NOT NULL;
```

Always uses `MAX(run_id)` to ensure the latest data is exported. Never hardcodes a run ID.

---

## Known Data Gaps & Limitations

| Issue | Detail |
|---|---|
| CRFD cohort disaggregation | MHS26 covers MH+LD/autism combined. No disaggregated source available. Both `mental_health` and `learning_disability` tenant types share the same signal. |
| DRD apportionment resolution | DRD % and average columns are UTLA-level pass-through for county districts. All E07 districts under an E10 county inherit the same value. |
| CRFD suppression rate | 28–46% of LAs have suppressed MHS26 values per month. These LAs are excluded from tenant-type rankings. |
| MARAC temporal lag | SafeLives publishes MARAC data 6–9 months after reference period. Current run may show prior year figures. |
| Care leaver data granularity | DfE publishes at upper-tier LA level, 155 authorities including 24 county councils. District councils have no figure and are absent, not zero. Counties are carried on their own E10 code and do not join `la_boundaries`, so any boundary-joined query still excludes them. See [s4_care_leaver_source.md](s4_care_leaver_source.md). |
| Care leaver definition | `semi_independent` is a pipeline aggregate of three DfE categories (semi-independent transitional, foyers, supported lodgings) and is **not** DfE's published category. External documents must quote `semi_independent_published`. From reporting year 2024 the DfE category means Ofsted-registered provision only, so counts must not be trended across 2023/2024. |
| Care leaver suppression | Suppressed cells are added as zero on the 17-21 path, so bucket counts and `total_care_leavers` are minima. Quote `total_published`, read from DfE's own Total row. |
| IMD version | IMD 2025 is used: MHCLG English Indices of Deprivation 2025, File 10 Local Authority District Summaries (lower-tier) v2, published 30 October 2025. Verified against the published file at 296 of 296 authorities, 2026-08-20. An earlier note stating IMD 2019 was used was wrong. |
| TA seasonality | H-CLIC is quarterly. The pipeline uses the most recent quarter end, which may vary by LA submission. |
| Rough sleeping count uncertainty | The DLUHC rough sleeping count is a single-night snapshot. Actual levels may be significantly higher. |

---

## Boundary Data

Boundaries are sourced from the ONS Open Geography Portal:
- Dataset: Local Authority Districts (**May 2024**) Boundaries UK **BGC**
- Format: GeoJSON, WGS84 (EPSG:4326)
- Simplified to approximately 20% of original vertex count for web performance
- Only English LAs included (296 districts, unitary authorities, metropolitan boroughs)

This section previously read "December 2024 ... BUC". Both were wrong. Two independent artefacts agree on May 2024 BGC: `la_boundaries.source_date` is `2024-05-01` for all 296 rows, and the S7 run log note reads "LA boundaries loaded — May 2024 BGC — England only". The code list is identical across LAD24 vintages, so the loaded data cannot adjudicate this on its own — it was settled from the load provenance, not inferred. Corrected 2026-08-13.

**The boundary vintage predates the codes some publishers now use.** `la_boundaries` is May 2024 and carries E08000016 and E08000019 for Barnsley and Sheffield. The 1 April 2025 recode (SI 1328/2024) means any source published after that date will use E08000038 and E08000039 instead. See the standing rule below.

---

## Refresh Schedule

| Trigger | Action |
|---|---|
| Workflow 1 completes | n8n Node 9 exports GeoJSON + signals JSON |
| Export validated (296 features, no NULLs) | n8n Node 11 pushes to GitHub via git |
| GitHub receives push | Raw URLs update immediately |
| Browser opens viewer | Fetches latest GeoJSON from GitHub raw URL |

**Total latency from pipeline run to map update**: typically < 2 minutes.

---

## Contact

Pipeline and data questions: [sl@slendeavours.org](mailto:sl@slendeavours.org)

GitHub: [github.com/slendeavours/ONS_Population_Estimates](https://github.com/slendeavours/ONS_Population_Estimates)
