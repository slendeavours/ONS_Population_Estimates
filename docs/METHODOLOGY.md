# Methodology — UCWS DV Pipeline

---

## Data Sources

**This table is the source register.** Numbers are assigned from here (gaps intentional). `pipeline_run_log.source_number` is an execution record, not the register — it has been the wrong authority to ask twice, and was backfilled on 2026-08-13 to agree with this table. See the standing rule below.

| S# | Source | Metric(s) | Publisher | Frequency |
|---|---|---|---|---|
| 1 | DLUHC H-CLIC | TA households (current + prev year), trend label | DLUHC | Quarterly |
| 2 | MHCLG RO4 | Homelessness expenditure (B&B, nightly, total) | MHCLG | Annual |
| 3 | ONS Mid-Year Estimates | Population by LA | ONS | Annual |
| 3b | Census 2021 TS054 | Tenure | ONS | Decennial |
| 4 | DfE SEN2 / Children in Need | Care leavers in semi-independent housing | DfE | Annual |
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
| 22 | MHCLG Council Taxbase (CTB form) + Live Table 615 | Dwellings empty six months or more, all empties, empty homes premium counts, second homes, unoccupied exemptions by class per LA; vacant and long-term vacant dwellings by district from 2004 | MHCLG | Annual (November, revised the following January); Table 615 updated with the dwelling stock live tables |
| 9a | NHS DRD monthly | Bed days lost to delayed discharge, % delayed 1+ days (UTLA→LAD apportioned) | NHSE | Monthly |
| 9b | MHSDS MHS26 | CRFD delayed discharge days — combined MH+LD/autism (direct LA level) | NHS Digital | Monthly |

S11 is the pipeline's only supply-side source: every other source measures need, S11 records existing CQC-registered provision. It is stored agnostically like everything else; the pipeline does not score or rank markets.

**Standalone sources.** S6 and S15 are loaded but not wired into Workflow 1: they add no `staging_la_signals` column and no tenant type. S6 is queried directly from its own tables; S15 reaches the map through its own `hpi_la_prices.json` rather than through the signals JSON.

**S19 is wired, not standalone.** This document previously listed S19 as standalone. That was wrong. `staging_la_signals` has carried `pip_total_claimants`, `pip_enhanced_daily_living` and `pip_rate_per_1000` since run 11, verified against `la_pip_claimants` at Apr-26 — Birmingham 93,196 / 50,002, Kingston upon Hull 24,195 / 12,078, Kensington and Chelsea 7,166 / 4,039, all exact. They are PIP columns, not a tail left by the S15 renumbering; S15's house prices live in `la_house_prices` and have no staging column at all. Corrected 2026-08-13 during the S22 build. `docs/README.md` had it right throughout.

**S6 caveats.** Asylum support figures are based on the person's registered address, which is not necessarily where they regularly reside, and **exclude unaccompanied asylum-seeking children**, who are supported by local authority children's services rather than Home Office asylum support. S6 is not a count of all asylum seekers in an area. Two structural breaks make the England series non-comparable before 2025-03-31; they are recorded in the `asylum_series_breaks` table and explained in `docs/s6_asylum_source.md`.

**S6 geography.** Resolved entirely through `la_code_lookup`. The build-local workaround S6 carried for three codes was retired on 2026-07-26 once the lookup was corrected; the reload reproduced the prior checksum byte-identically, confirming the workaround and the corrected lookup agree. See `docs/decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md` and `docs/decisions/2026-07-26-la-code-lookup-full-audit.md`.

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

**Standing rule — direct SQL against `staging_la_signals` updates the stored node in the same session, or it is not applied.** Any change to the columns of `staging_la_signals` must be written back to W1 node 5 in `n8ndb` before the session ends. Applying it to the data alone leaves the stored node behind, and the next genuine workflow run silently drops every column the node does not know about. This is not hypothetical: runs 10 and 11 added the S9 and S19 columns by direct SQL and never wrote them back, so the stored node was two builds stale until the S22 build in August 2026 found it. The same rule covers anything that creates a `staging_runs` row outside the workflow — the row must be created through the Create Run node's query so the sequence stays ahead of the data. Runs 10 and 11 skipped that too, leaving the sequence trailing by two and the next `nextval()` set to collide with an existing run.

**Standing rule — resolve geography before the orphan gate, not after it fails.** Every build resolves published codes through `la_code_lookup` as part of extraction, and only then checks for orphans against `la_boundaries`. Running the gate first wastes a gate on a known, predictable condition.

Assume any source published **after 1 April 2025** uses the recoded Barnsley and Sheffield codes E08000038 and E08000039, because `la_boundaries` is May 2024 and carries E08000016 and E08000019. This pair has now appeared in S9b, S18, S21 and S22. It is predictable, not surprising. Resolution is `change_type = 'recode'` only: a recode renumbers the same area and resolves, while `new_unitary` and `merger` are abolitions and must stay unmapped, because folding predecessor districts onto a successor makes every downstream sum count that successor once per predecessor.

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
| Care leaver data granularity | DfE data is at upper-tier LA level; district-level LAs may show NULL or estimated values. |
| IMD version | IMD 2019 is used (supplemented by 2025 LA summary). No full LSOA-level 2025 IMD released yet. |
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
