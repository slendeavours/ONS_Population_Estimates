# Exempt Accommodation Intelligence Platform

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
| Housing Pressure | Housing register (waiting list), HB asylum seekers |
| Expenditure | B&B spend, nightly-paid spend, total homelessness spend (£000s) |
| Fiscal Risk | EFS support flag, S.114 notice flag |
| Deprivation | IMD rank of average rank |
| LHA Rates | Shared accommodation and 1–4 bed weekly rates by BRMA, mapped to each LA |

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
/docs/
  README.md                                 This file
  DATA_DICTIONARY.md                        Column definitions
  USAGE_GUIDE.md                            Map usage
  METHODOLOGY.md                            Sources and calculations
  /nodes/                                   Pipeline node documentation
/viewers/                                   Legacy Kepler.gl viewers (retained, unmaintained)
/n8n/                                       n8n workflow exports
```

## Technology Stack

- **Map renderer**: Mapbox GL JS (URL-restricted public token)
- **Data format**: GeoJSON RFC 7946, WGS84; LAD24CD as the universal join key
- **Backend pipeline**: n8n + PostgreSQL 16 (Docker)
- **Data hosting**: GitHub Pages (public repository)

## Support

For pipeline or data questions: sl@slendeavours.org
