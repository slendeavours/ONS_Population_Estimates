# Exempt Accommodation Intelligence Platform

<!-- repo-meta
status: active
last-reviewed: 2026-07-22
type: tool
consumed-by: map.slendeavours.org, n8n exempt_pipeline workflows
-->

**SL Endeavours Ltd** | [slendeavours.org](https://slendeavours.org)

---

## What Is This?

This pipeline aggregates official data sources into a unified demand signal for every English local authority (296 in total), focused on exempt accommodation, homelessness pressure, and fiscal risk.

This repository contains the exported data outputs and the interactive map, updated after each pipeline run.

## The Live Map

**[map.slendeavours.org](https://map.slendeavours.org)** — the Demand Map, built on Mapbox GL JS. Served from `index.html` at the repository root via GitHub Pages with a custom domain.

Legacy Kepler.gl viewers are retained in `/viewers/` for reference but are no longer maintained. `viewers/demand_map.html` redirects to the live map.

## What Data Does It Show?

| Category | Metrics |
| --- | --- |
| Temporary Accommodation | Current households, prior year, YoY %, trend label |
| Rough Sleeping | Current count, prior year count |
| Care Leavers | Semi-independent placement count |
| Domestic Violence | MARAC cases, rate per 10k population |
| Housing Pressure | Housing register (waiting list), HB asylum seekers, HB specified accommodation claimants |
| Expenditure | B&B spend, nightly-paid spend, total homelessness spend (£000s) |
| Fiscal Risk | EFS support flag, S.114 notice flag |
| Deprivation | IMD rank of average rank |
| LHA Rates | Shared accommodation and 1–4 bed weekly rates by BRMA, mapped to each LA |
| Care Providers (SL) | Active, non-dormant CQC supported living locations per LA (supply side) |
| Discharge Delays (S9a) | Bed days lost to delayed discharge, % delayed 1+ days (DRD monthly, UTLA→LAD apportioned) |
| CRFD (S9b) | MHS26 clinically ready for discharge days — combined MH+LD/autism (MHSDS monthly, direct LA level) |
| PIP Claimants (S19) | Total PIP caseload and enhanced daily living claimants per LA (DWP Stat-Xplore, monthly) |
| HB Accommodation Type (S8b) | HB claimants by accommodation type: SA, TA, Other, Unknown (DWP Stat-Xplore, monthly) |

## How Often Is It Updated?

After each **Workflow 1 pipeline run**. The run ID and timestamp are displayed in the map, and `data/signals/latest.json` contains the current run metadata. LHA rates refresh annually when DWP publishes new rates (late January).

## Raw Data URLs

| File | URL |
| --- | --- |
| la_boundaries.geojson | `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/boundaries/la_boundaries.geojson` |
| staging_la_signals_latest.json | `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/signals/staging_la_signals_latest.json` |
| latest.json (metadata) | `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/signals/latest.json` |

## Repository Structure

```
index.html                                  Live Demand Map (Mapbox GL JS) — map.slendeavours.org
CNAME                                       Custom domain config for GitHub Pages
/data/
  /boundaries/la_boundaries.geojson         LA boundary polygons + signals
  /signals/staging_la_signals_latest.json   Signal data, no geometries
  /signals/latest.json                      Run metadata
  /processed/                               Load-ready datasets from backfill runs (S18 PIPR)
/docs/
  README.md                                 This file
  DATA_DICTIONARY.md                        Column definitions
  USAGE_GUIDE.md                            Map usage
  METHODOLOGY.md                            Sources and calculations
  s18_pipr_source.md                        Source 18 (ONS PIPR private rents) register entry
  s18_pipr_workbook_structure.md            PIPR workbook spec (n8n S18 build reference)
  s19_pip_source.md                        Source 19 (DWP PIP claimants) register entry
  s19_pip_w1_integration.md               S19 PIP W1 integration summary (run 11)
  geography_dimension.md                    la_geography / la_succession dimension tables
  S9_BUILD_SUMMARY.md                      S9 sources build summary (UCES project knowledge)
  /nodes/                                   Pipeline node documentation
  /decisions/                               Decision records (dated, one per non-obvious decision)
/scripts/                                   Per-source ETL scripts (S18 PIPR; S11 CQC; S19 PIP; S8b HB accommodation type)
/viewers/                                   Legacy Kepler.gl viewers (retained, unmaintained)
/n8n/                                       n8n workflow exports
```

Raw source downloads (`data/raw/`) are kept local and gitignored — they are re-fetchable via `scripts/s18_pipr_fetch.py` (ONS PIPR) and `scripts/s11_cqc_fetch.py` (CQC directory).

## Technology Stack

- **Map renderer**: Mapbox GL JS (URL-restricted public token)
- **Data format**: GeoJSON RFC 7946, WGS84; LAD24CD as the universal join key
- **Backend pipeline**: n8n + PostgreSQL 16 (Docker)
- **Data hosting**: GitHub Pages (public repository)

## Support

For pipeline or data questions: sl@slendeavours.org
