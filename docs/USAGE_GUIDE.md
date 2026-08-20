# Usage Guide — UCWS DV Interactive Map

---

## Opening the Map

### Option A — GitHub Pages (if enabled)
Navigate to:
`https://slendeavours.github.io/ONS_Population_Estimates/viewers/kepler_branded.html`

### Option B — Download and Open Locally
1. Go to [github.com/slendeavours/ONS_Population_Estimates](https://github.com/slendeavours/ONS_Population_Estimates)
2. Navigate to `viewers/kepler_branded.html`
3. Click **Raw** → Save the page locally
4. Open the saved `.html` file in any modern browser (Chrome, Edge, Firefox, Safari)

> **Note**: The HTML file loads GeoJSON from GitHub on first open. You need an internet connection the first time. Once loaded, zoom/pan works offline.

---

## Map Controls

| Control | Location | Action |
|---|---|---|
| Zoom in / out | Top-left buttons | Increase or decrease zoom level |
| Pan | Click & drag anywhere | Move the map |
| Zoom to area | Mouse scroll wheel / trackpad pinch | Zoom in or out |
| Rotate | Right-click & drag (desktop) | Rotate the map view |
| Full-screen | Top-right button | Expand map to full browser window |
| Scale bar | Bottom-right | Shows distance in km |
| Navigation | Top-left | Compass + zoom buttons |

---

## Viewing a Local Authority

1. **Find an LA**: Zoom into any area on the map. All 296 English LAs are shown as coloured polygons.
2. **Hover**: The LA boundary highlights white as you hover over it.
3. **Click**: Click any LA polygon to open the detail panel (right side on desktop, bottom sheet on mobile).
4. **Detail panel**: Shows all 22+ signals for the selected LA. Scroll down within the panel to see all metrics.
5. **Close panel**: Click the ✕ in the top-right of the panel.

---

## Understanding Colours

The map colours each LA by **TA households (current)** — the number of households currently in temporary accommodation:

| Colour | TA Households |
|---|---|
| Light yellow | < 100 |
| Amber | 100 – 500 |
| Orange | 500 – 1,500 |
| Red | 1,500 – 5,000 |
| Dark red / maroon | > 5,000 |

> Grey or missing colour indicates the LA has no data for the current run (NULL value).

---

## Reading the Detail Panel

When you click an LA, the panel shows:

**Temporary Accommodation**
- Current and prior year household counts
- Year-on-year % change (negative = improving, positive = worsening)
- Trend label (e.g. `rising strongly`, `flat`, `submission gap`)

**Rough Sleeping**
- Current and prior year snapshot counts

**Other Demand Signals**
- Care leavers in supported accommodation. The map layer uses the combined
  measure (DfE's semi-independent category plus foyers plus supported lodgings).
  External documents quote the published category alone, which is lower. See
  `docs/s4_care_leaver_source.md`
- MARAC domestic violence cases, published by police force area rather than
  local authority
- Housing Benefit claimants in specified accommodation
- Social housing waiting list size
- IMD deprivation rank

**Expenditure**
- B&B, nightly-paid, and total homelessness spend in £000s

**Risk Flags**
- EFS support: `YES` (red) = LA receiving emergency financial support
- S.114 notice: `YES` (red) = LA has declared effective budget insolvency

---

## Mobile Usage

- **Tap** any LA polygon to open the detail panel
- **Pinch** to zoom in or out
- **Drag** to pan
- On small screens, the legend is hidden by default — tap the **↓ Legend** button in the bottom-left to show it
- The detail panel appears as an overlay on mobile — scroll within it to see all data

---

## Data Freshness

The run ID and last-updated date are shown in the map header bar (branded viewer) or title bar (basic viewer). If the map is loaded with stale data, refresh the browser tab.

The `latest.json` metadata file always reflects the most recent pipeline run:
- URL: `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/signals/latest.json`

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Map shows "Loading…" indefinitely | Check internet connection. Try refreshing. GitHub raw CDN may be temporarily slow. |
| Error: "Failed to fetch GeoJSON" | GitHub raw URL may be temporarily unavailable. Wait 1–2 minutes and refresh. |
| Map loads but no colours visible | The GeoJSON loaded but signal data is NULL for all LAs. This happens if the pipeline has not run yet. |
| LA is missing from the map | All 296 LAs should be present. If one is missing, the geometry may be outside the current view — zoom out to see all of England. |
| Popup shows all dashes (—) | The LA has no signal data for the current run. Check `data_quality` field or wait for next pipeline run. |
| Map is slow on mobile | The GeoJSON is ~9.5 MB. On slow connections, initial load may take 10–30 seconds. |
| Legend not visible | On mobile, tap the **↓ Legend** button. On desktop, check if it is hidden behind the detail panel — close the panel first. |

---

## Exporting Data

- **Screenshot**: Use your browser's built-in screenshot tool or OS screenshot shortcut.
- **Download GeoJSON**: Right-click the raw URL and Save As.
- **Open in QGIS**: Load `la_boundaries.geojson` directly from the raw GitHub URL as a vector layer.
- **Use in Tableau/Power BI**: Connect to the raw GitHub JSON URL as a data source.

---

## Sharing the Map

You can share:
- A direct link to `kepler_branded.html` on GitHub Pages (if enabled)
- The landing page URL (`index.html`) — includes the data dictionary

The map always shows the latest run data. There is no need to update the link when new data is published.
