# Custom Demand Map Build — Mapbox GL JS

## What You Are Building

Replace the existing Kepler.gl map at `viewers/kepler_branded.html` with a custom-built Mapbox GL JS interactive demand map. The result is a single HTML file (`viewers/demand_map.html`) that replaces the current viewer. Same GitHub repo, same GitHub Pages hosting, same data files, same URL structure.

This is not a wrapper around an existing tool. You are building a custom web application from scratch.

---

## Environment

- **Repository**: `slendeavours/ONS_Population_Estimates` (already exists, already has GitHub Pages enabled)
- **Branch**: `main`
- **Mapbox token**: Read from `.env` file in project root as `MAPBOX_TOKEN`. The token starts with `pk.`. Hard-code the token value directly into the HTML file (it is a public token with URL restrictions, safe to embed).
- **Submark logo**: `Submark.png` in project root. Base64 encode it and embed in the HTML. This is the heraldic horse/shield crest. White background PNG — render against dark background at reduced opacity.
- **Existing data files on GitHub** (already published, do not recreate):
  - `data/boundaries/la_boundaries.geojson` (~6 MB) — 296 English LA polygons, WGS84
  - `data/signals/staging_la_signals_latest.json` (~200 KB) — 296 rows, all demand metrics

---

## Data Structure

### la_boundaries.geojson
Standard GeoJSON FeatureCollection. 296 features. Each feature:
```json
{
  "type": "Feature",
  "geometry": { "type": "Polygon", "coordinates": [[[lng, lat], ...]] },
  "properties": {
    "lad24cd": "E09000001",
    "lad24nm": "City of London",
    "longitude": -0.092,
    "latitude": 51.518,
    "shape_area": 3074916.0
  }
}
```

### staging_la_signals_latest.json
JSON array of 296 objects. Each object:
```json
{
  "lad24cd": "E09000001",
  "la_name": "City of London",
  "population": 9000,
  "ta_households_current": 25,
  "ta_households_prev_year": 23,
  "ta_yoy_pct": 8.7,
  "ta_trend_label": "rising",
  "rough_sleeping_current": 3,
  "rough_sleeping_prev_year": 2,
  "care_leavers_semi_indep": 5,
  "marac_cases": 12,
  "marac_rate_per_10k": 13.3,
  "hb_sa_caseload": 45,
  "housing_register": 1200,
  "ro4_bb_spend_000": 500,
  "ro4_nightly_spend_000": 300,
  "ro4_total_homelessness_000": 2500,
  "efs_flag": false,
  "s114_flag": false,
  "imd_rank_of_average_rank": 150,
  "data_quality": { "missing_sources": [] }
}
```

The map must load BOTH files and join them client-side on `lad24cd`. The GeoJSON provides geometry; the signals JSON provides the metric values.

---

## Brand Identity

### Colour Palette
```
Background primary:    #0e0d0b   (deep charcoal)
Background secondary:  #0a0c0f   (near-black cool tint)
Gold accent:           #c9a96e   (primary gold — headlines, CTAs, active states)
Gold muted:            rgba(201,169,110,0.5)  (labels, borders, inactive states)
Gold subtle:           rgba(201,169,110,0.08) (hover backgrounds, dividers)
Primary text:          #f0ece4   (warm cream — headings, active labels)
Secondary text:        rgba(240,236,228,0.5)  (body, descriptions)
Tertiary text:         rgba(240,236,228,0.3)  (metadata, footnotes)
```

### Typography (Web Fonts)
Load from Google Fonts:
```
Serif display:   'Cormorant Garamond', serif   (headings, section titles)
Sans-serif body: 'DM Sans', sans-serif          (labels, body text, UI controls)
```

### Logo Placement
- Position: Bottom-right of sidebar panel
- Height: 85px, width auto
- Opacity: 0.7
- Do NOT place centrally or top-left

---

## Page Layout

Single HTML file. No external CSS files. No external JS files except Mapbox GL JS CDN and Google Fonts. Everything self-contained.

### Structure
```
+------------------------------------------+
|  SIDEBAR (380px)  |       MAP            |
|                   |                      |
|  [Header]         |                      |
|  [Search]         |  Mapbox GL JS        |
|  [Layers]         |  Dark style          |
|  [Sources]        |  296 LA polygons     |
|  [Logo]           |  Choropleth          |
|                   |       [Legend]        |
+------------------------------------------+
```

### Sidebar (Left, 380px wide, scrollable)
Background: `#0a0c0f`. Full viewport height. Fixed position.

**Header section:**
- Title: "Demand Map" — Cormorant Garamond, 28px, bold, cream
- Subtitle: "England — Exempt Housing Investment Intelligence" — DM Sans, 13px, secondary text
- Three badges below subtitle:
  - "296 LAS" — small pill, gold border, gold text
  - "17 GOV SOURCES" — small pill, gold border, gold text
  - "PIPELINE VERIFIED" — small pill, gold background, dark text

**Postcode Lookup section:**
- Label: "POSTCODE LOOKUP" — DM Sans, 10px, uppercase, letter-spacing 0.15em, gold muted
- Input field: dark background (#141210), cream text, placeholder "e.g. B1 1BB, M1 1AA"
- "Search" button: gold background, dark text
- On submit: Call `https://api.postcodes.io/postcodes/{postcode}` (free, no auth). Extract latitude/longitude from response. Fly map to that location at zoom 12. If the postcode falls within an LA polygon, highlight that LA and show its popup.

**LA Search section:**
- Search input: "Search local authority..." placeholder
- Filters the 296 LA names as user types (client-side, no API)
- Click a result: fly to that LA centroid, highlight polygon, show popup

**Map Layers section:**
- Label: "MAP LAYERS" — DM Sans, 10px, uppercase, letter-spacing 0.15em, gold muted
- List of toggleable layers. Each layer is a row:
  - Colored dot (circle, 10px) on left
  - Layer name — DM Sans, 14px, cream text
  - Description below name — DM Sans, 11px, secondary text (only for selected layer)
  - Checkbox/toggle on right — gold when active
- Only ONE layer active at a time (radio behaviour, not multi-select)
- Selecting a layer recolors the choropleth to that metric

**Layer definitions (in order):**

| # | Dot Color | Name | Description | Signal Field | Color Scale |
|---|-----------|------|-------------|--------------|-------------|
| 1 | `#4ade80` | TA Households | Temporary accommodation demand | `ta_households_current` | cream→gold→red |
| 2 | `#f59e0b` | Homelessness Spend | Total annual spend (2024-25) | `ro4_total_homelessness_000` | cream→gold→red |
| 3 | `#f97316` | Housing Register | Social housing waiting lists | `housing_register` | cream→gold→red |
| 4 | `#ef4444` | Care Leavers | Semi-independent housing need | `care_leavers_semi_indep` | cream→gold→red |
| 5 | `#ec4899` | Domestic Violence (MARAC) | Urgent rehousing cases | `marac_cases` | cream→gold→red |
| 6 | `#06b6d4` | Rough Sleeping | Single-night snapshot | `rough_sleeping_current` | cream→gold→red |
| 7 | `#a3e635` | Deprivation (IMD) | Index of Multiple Deprivation 2025 | `imd_rank_of_average_rank` | REVERSED (red=rank 1 most deprived, cream=rank 296) |
| 8 | `#818cf8` | HB Asylum Seekers | Supported accommodation caseload | `hb_sa_caseload` | cream→gold→red |

Default on load: Layer 1 (TA Households) active.

**Verified Sources section:**
- Collapsible section at bottom of sidebar
- Label: "VERIFIED DATA SOURCES (17)" with expand/collapse arrow
- When expanded, small text listing: "MHCLG Statutory Homelessness · MHCLG RO4 Expenditure · ONS Mid-Year Population · DfE Care Leavers · MHCLG IMD 2025 · Home Office Asylum · DWP Stat-Xplore · MHCLG Rough Sleeping · SafeLives MARAC · MHCLG LAHS · VOA LHA Rates · MoJ Offender Stats · OHID Substance Misuse · CQC Providers · NHS Discharge · ONS Boundaries · ONS Census 2021"
- DM Sans, 10px, tertiary text

**Logo:**
- Submark.png, base64 embedded
- Bottom of sidebar, right-aligned
- Height 85px, opacity 0.7
- Below the sources section

### Map Area (Right of sidebar, fills remaining viewport)

**Mapbox GL JS setup:**
- Style: `mapbox://styles/mapbox/dark-v11` (dark basemap)
- Center: `[-1.5, 52.8]` (center of England)
- Zoom: `6`
- Min zoom: `5`
- Max zoom: `14`
- Pitch: `0` (flat, no 3D)
- Navigation controls: top-right (zoom +/- only, no compass)

**Choropleth rendering:**
- Add boundaries GeoJSON as a source
- Join signal data to features by matching `lad24cd`
- Render as `fill` layer with data-driven `fill-color`
- Color scale: 7-step quantile breaks based on the active metric
- Base palette for most metrics: `['#f0ece4', '#e8d5a8', '#d4a574', '#c9a96e', '#d4764e', '#c94444', '#8b0000']` (cream through gold to deep red)
- IMD palette (reversed): `['#8b0000', '#c94444', '#d4764e', '#c9a96e', '#d4a574', '#e8d5a8', '#f0ece4']`
- Fill opacity: `0.75`
- Stroke: `rgba(201,169,110,0.3)`, width `0.5`

**Financial stress borders (always visible, all layers):**
- If `s114_flag === true`: thick red border (`#ef4444`, width `3`)
- If `efs_flag === true`: thick orange border (`#f97316`, width `3`)
- These borders render ON TOP of the choropleth fill, visible regardless of which layer is selected

**Hover behaviour:**
- On hover over a polygon: increase opacity to `0.9`, thicken border to `1.5`
- Show cursor as pointer
- Show small tooltip near cursor with LA name only

**Click behaviour:**
- On click: open a popup/panel showing full LA details (see Popup Design below)
- Highlight the clicked polygon with gold border (`#c9a96e`, width `2`)

### Legend (Bottom-left of map area, overlaying map)

- Floating panel, semi-transparent dark background (`rgba(10,12,15,0.85)`)
- Rounded corners (8px), padding 12px
- Title: current layer name — DM Sans, 13px, bold, cream
- Color gradient bar: 200px wide, 12px tall, showing the 7-step color scale
- Labels: "Low" on left, "Critical" on right — DM Sans, 10px, secondary text
- "No data" indicator: small grey circle + "No data" label
- Legend updates when layer changes

### Popup (Right side panel or floating popup)

When user clicks an LA polygon, show a detailed panel. Two options (choose whichever renders better):

**Option A: Slide-in panel from right (preferred)**
- 360px wide, full height, dark background
- Close button (X) top right
- Slides in with CSS transition

**Option B: Large floating popup**
- Anchored to clicked polygon
- Max-width 380px

**Popup content:**

```
[LA NAME]                              (Cormorant Garamond, 22px, cream)
[LAD24CD]                              (DM Sans, 10px, tertiary text)

── TEMPORARY ACCOMMODATION ──────────
Current Households        [value]
Previous Year            [value]
Year-on-Year Change      [+X.X%] ▲    (green if positive, red if negative)
Trend                    [rising/falling/flat]

── HOUSING PRESSURE ─────────────────
Housing Register         [value]
Rough Sleeping           [value]
HB Asylum Seekers        [value]

── COHORT DEMAND ────────────────────
Care Leavers             [value]
MARAC Cases              [value]
MARAC Rate (per 10k)     [value]

── EXPENDITURE (2024-25) ────────────
B&B Spend                £[X.X]m      (divide ro4_bb_spend_000 by 1000, format to 1dp)
Nightly Paid Spend       £[X.X]m
Total Homelessness       £[X.X]m

── CONTEXT ──────────────────────────
Population               [value]
Deprivation Rank         [value] / 296  (1 = most deprived)
[If s114_flag true:]     ⚠ SECTION 114 NOTICE
[If efs_flag true:]      ⚠ EMERGENCY FINANCIAL SUPPORT
```

- Section headers: DM Sans, 9px, uppercase, letter-spacing 0.12em, gold muted
- Labels: DM Sans, 12px, secondary text
- Values: DM Sans, 12px, bold, cream — right-aligned
- Expenditure values: divide `_000` fields by 1000, display as `£X.Xm` (e.g., `ro4_bb_spend_000: 13902` displays as `£13.9m`)
- Warning flags: red text (#ef4444), bold, with warning triangle emoji

---

## Technical Requirements

1. **Single HTML file**. All CSS inline in `<style>`. All JS inline in `<script>`. Only external resources: Mapbox GL JS CDN, Google Fonts CDN.

2. **Data loading**: Fetch both files from GitHub raw URLs on page load:
   - `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/boundaries/la_boundaries.geojson`
   - `https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/signals/staging_la_signals_latest.json`

3. **Client-side join**: After loading both, merge signal data into GeoJSON features by matching `properties.lad24cd` to `lad24cd` in signals. Add all signal fields to each feature's properties.

4. **Loading state**: Show a loading spinner/message while data loads. Dark background, gold spinner, "Loading 296 local authorities..." text.

5. **Error handling**: If either data file fails to load, show an error message in the map area. Don't crash silently.

6. **Responsive**: On viewports < 768px, collapse sidebar into a hamburger menu that slides over the map. Map fills full width. Legend stays visible.

7. **Quantile breaks**: Calculate color breaks dynamically from the data using quantile distribution (7 buckets). This ensures even color distribution regardless of data skew.

8. **NULL handling**: Some LAs may have null values for certain metrics. Render these as grey (`#333333`) with "No data" in tooltips.

9. **Performance**: The GeoJSON is ~6 MB. Load it efficiently. Consider using `map.addSource` with the full FeatureCollection once, then use `filter` expressions to handle different views rather than reloading data.

---

## Files to Create/Modify

1. **CREATE**: `viewers/demand_map.html` — the new custom map (this is the main deliverable)
2. **MODIFY**: `viewers/index.html` — update links to point to `demand_map.html` instead of `kepler_branded.html`
3. **KEEP**: `viewers/kepler_branded.html` — do not delete, keep as fallback
4. **KEEP**: `viewers/kepler_basic.html` — do not delete

---

## Testing Checklist

After building, verify:

- [ ] Page loads without console errors
- [ ] Both data files fetch successfully
- [ ] All 296 LA polygons render on map
- [ ] Default layer (TA Households) shows correct choropleth colors
- [ ] Switching layers recolors the map correctly
- [ ] Financial stress borders (red/orange) visible on all layer views
- [ ] Clicking an LA shows full popup with all metrics
- [ ] Expenditure values display as £X.Xm (not raw thousands)
- [ ] Postcode search works (test: B1 1BB, M1 1AA, SW1A 1AA)
- [ ] LA search works (test: Birmingham, Manchester, Blackpool)
- [ ] Legend updates when layer changes
- [ ] IMD layer colors are reversed (red = most deprived)
- [ ] Hover shows LA name tooltip
- [ ] Logo visible bottom-right of sidebar
- [ ] Mobile responsive (sidebar collapses on narrow viewport)
- [ ] Quantile breaks produce visually distinct color bands
- [ ] NULL values show as grey with "No data" label

---

## What NOT to Do

- Do NOT use React, Vue, or any framework. Vanilla HTML/CSS/JS only.
- Do NOT use Kepler.gl, Deck.gl, or Leaflet. Mapbox GL JS only.
- Do NOT create multiple files. Single HTML file (except the `index.html` update).
- Do NOT add composite scoring, investment opportunity rankings, or any opinionated analysis. The map shows raw data. The user interprets it.
- Do NOT hardcode any signal values. All data comes from the JSON files at runtime.
- Do NOT add analytics, tracking, cookies, or third-party scripts beyond Mapbox and Google Fonts.

---

## Commit and Deploy

After building:
1. Commit `viewers/demand_map.html` to the repository
2. Update `viewers/index.html` with link to new map
3. Push to `main` branch
4. Verify at: `https://map.slendeavours.org/viewers/demand_map.html`
5. Verify at: `https://slendeavours.github.io/ONS_Population_Estimates/viewers/demand_map.html`

---

## Success Criteria

The map is done when a first-time user can:
1. Open the URL and see England covered in colored polygons
2. Understand what the colors mean (legend is clear)
3. Switch between 8 different metrics using the sidebar
4. Search by postcode and see the map zoom to that area
5. Search by LA name and see the map zoom to that authority
6. Click any LA and see all 25+ metrics in a clean popup
7. See which LAs have financial stress (red/orange borders)
8. Read expenditure values in millions, not raw thousands
9. Use the map on mobile without the sidebar blocking the view
10. See the SL Endeavours branding (logo, colors, typography)
