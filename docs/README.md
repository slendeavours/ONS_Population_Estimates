# UCWS DV — Exempt Accommodation Intelligence Platform

**SL Endeavours Ltd** | [slendeavours.org](https://slendeavours.org)

---

## What Is This?

The UCWS DV pipeline aggregates 17+ official data sources into a unified demand signal for every English local authority (296 in total), focused on exempt accommodation, homelessness pressure, and fiscal risk.

This repository contains the exported data outputs and interactive map viewers, updated automatically after each pipeline run.

---

## What Data Does It Show?

| Category | Metrics |
|---|---|
| Temporary Accommodation | Current households, prior year, YoY %, trend label |
| Rough Sleeping | Current count, prior year count |
| Care Leavers | Semi-independent placement count |
| Domestic Violence | MARAC cases, rate per 10k population |
| Housing Pressure | Housing register (waiting list), HB asylum seekers |
| Expenditure | B&B spend, nightly-paid spend, total homelessness spend (£000s) |
| Fiscal Risk | EFS support flag, S.114 notice flag |
| Deprivation | IMD rank of average rank |

---

## How Often Is It Updated?

After each **Workflow 1 pipeline run** (approximately weekly). The run ID and timestamp are displayed in the map header. The `data/signals/latest.json` file contains the current run metadata.

---

## How to Access the Map

| Viewer | Description |
|---|---|
| [kepler_branded.html](../viewers/kepler_branded.html) | Branded SL Endeavours viewer (recommended) |
| [kepler_basic.html](../viewers/kepler_basic.html) | Minimal viewer, lighter weight |
| [index.html](../viewers/index.html) | Landing page with data dictionary |

**Via GitHub Pages** (if enabled):
- `https://slendeavours.github.io/ONS_Population_Estimates/viewers/kepler_branded.html`

**Via raw GitHub** (always works):
- Download any HTML file and open locally in your browser

---

## Raw Data URLs

| File | URL |
|---|---|
| la_boundaries.geojson | `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/boundaries/la_boundaries.geojson` |
| staging_la_signals_latest.json | `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/signals/staging_la_signals_latest.json` |
| latest.json (metadata) | `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/signals/latest.json` |

---

## Repository Structure

```
/data/
  /boundaries/
    la_boundaries.geojson          Full combined GeoJSON (boundaries + all signals, ~9.5 MB)
  /signals/
    staging_la_signals_latest.json Signal data only, no geometries (~172 KB)
    latest.json                    Metadata (run_id, timestamp, feature count)

/viewers/
  index.html                       Landing page with data dictionary
  kepler_basic.html                Minimal MapLibre viewer
  kepler_branded.html              Branded SL Endeavours viewer

/docs/
  README.md                        This file
  DATA_DICTIONARY.md               All column definitions
  USAGE_GUIDE.md                   How to use the map
  METHODOLOGY.md                   Data sources and calculations

/n8n/
  workflow_nodes.json              n8n automation nodes for Workflow 1 extension
```

---

## Technology Stack

- **Map renderer**: MapLibre GL JS 4.x (open source, no API token required)
- **Base map tiles**: CARTO Dark Matter (free)
- **Data format**: GeoJSON RFC 7946, WGS84
- **Backend pipeline**: n8n + PostgreSQL 16 (Docker)
- **Data hosting**: GitHub (public repository)

---

## Support

For pipeline issues or data questions, contact: [sl@slendeavours.org](mailto:sl@slendeavours.org)

For map or viewer issues, see [USAGE_GUIDE.md](USAGE_GUIDE.md) for troubleshooting steps.
