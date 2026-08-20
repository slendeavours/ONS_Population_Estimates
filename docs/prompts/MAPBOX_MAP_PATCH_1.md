# Demand Map — Design Patch 1

## File to Edit
`viewers/demand_map.html` in the `slendeavours/ONS_Population_Estimates` repository.

Do not rebuild from scratch. Edit the existing file only.

---

## Change 1 — Make This the Root Index

Create a new file at the repo root: `index.html`

Content:
```html
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="0; url=viewers/demand_map.html">
<title>SL Endeavours Demand Map</title>
</head>
<body></body>
</html>
```

This makes `https://map.slendeavours.org/` redirect immediately to the demand map.

---

## Change 2 — Background Colour (Warm Professional Light Theme)

Replace the dark background throughout with a warm light professional palette.

### New Colour Palette

```
Page background:         #f5f0eb   (warm off-white parchment)
Sidebar background:      #faf7f4   (slightly lighter warm white)
Right panel background:  #faf7f4   (same as sidebar)
Map area background:     transparent (map fills it)

Primary text:            #1a1510   (near-black warm brown — main headings, values)
Secondary text:          #5c5248   (warm mid-brown — labels, descriptions)
Tertiary text:           #9c8f85   (warm light brown — metadata, codes)

Gold accent:             #b8924a   (slightly deeper gold — readable on light bg)
Gold muted:              rgba(184,146,74,0.4)   (borders, dividers)
Gold subtle:             rgba(184,146,74,0.08)  (hover backgrounds)

Section headers:         #8a7a6e   (warm grey-brown, uppercase labels)
Border colour:           rgba(26,21,16,0.1)     (dividers, input borders)
Input background:        #f0ebe4   (slightly darker than page bg)
Active layer bg:         rgba(184,146,74,0.12)  (selected layer row)
```

### Mapbox Basemap
Switch from `mapbox://styles/mapbox/dark-v11` to `mapbox://styles/mapbox/light-v11`

This gives a clean warm grey basemap that England sits on cleanly. The choropleth colours will read better against a light base.

---

## Change 3 — Per-Layer Colour Scales

Each layer must use its own distinct colour scale. Replace the single shared scale with these per-layer palettes. Each is a 7-step array from low to high.

```javascript
const LAYER_COLOR_SCALES = {
  ta_households_current: [
    '#fff7f7', '#fecaca', '#f87171', '#ef4444', '#dc2626', '#b91c1c', '#7f1d1d'
  ],
  ro4_total_homelessness_000: [
    '#fffbeb', '#fde68a', '#fbbf24', '#f59e0b', '#d97706', '#b45309', '#78350f'
  ],
  housing_register: [
    '#fff7ed', '#fed7aa', '#fb923c', '#f97316', '#ea580c', '#c2410c', '#7c2d12'
  ],
  care_leavers_semi_indep: [
    '#fef2f2', '#fecaca', '#f87171', '#ef4444', '#dc2626', '#991b1b', '#7f1d1d'
  ],
  marac_cases: [
    '#fdf4ff', '#f0abfc', '#e879f9', '#d946ef', '#c026d3', '#a21caf', '#701a75'
  ],
  rough_sleeping_current: [
    '#ecfeff', '#a5f3fc', '#22d3ee', '#06b6d4', '#0891b2', '#0e7490', '#164e63'
  ],
  imd_rank_of_average_rank: [
    '#166534', '#15803d', '#16a34a', '#4ade80', '#bbf7d0', '#dcfce7', '#f0fdf4'
  ],
  hb_sa_caseload: [
    '#eef2ff', '#c7d2fe', '#a5b4fc', '#818cf8', '#6366f1', '#4f46e5', '#312e81'
  ]
};
```

Note: `imd_rank_of_average_rank` scale is already reversed (rank 1 = most deprived = dark green, rank 296 = least deprived = pale green).

Update the choropleth layer paint expression to use the active layer's colour scale from this object whenever the active layer changes.

---

## Change 4 — Sidebar Design Refinement

Apply these changes to the left sidebar:

**General spacing:**
- Reduce padding from current values. Target: `16px` horizontal padding, `12px` vertical between sections.
- Remove heavy borders between sections. Replace with a single `1px` line using `border-colour` from palette above.

**Header section:**
- Title "Demand Map": `font-family: 'Cormorant Garamond', serif; font-size: 26px; font-weight: 700; color: #1a1510; letter-spacing: -0.02em;`
- Subtitle: `font-size: 12px; color: #5c5248; font-family: 'DM Sans', sans-serif;`
- Badges: smaller pills. `font-size: 9px; padding: 2px 8px; border-radius: 10px;`
  - "296 LAS": border `1px solid #b8924a`, text `#b8924a`, background transparent
  - "17 GOV SOURCES": same
  - "PIPELINE VERIFIED": background `#b8924a`, text `#faf7f4`

**Section labels (POSTCODE LOOKUP, LOCAL AUTHORITY, MAP LAYERS):**
- `font-family: 'DM Sans', sans-serif; font-size: 9px; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase; color: #8a7a6e;`
- `margin-bottom: 6px;`

**Input fields:**
- Background: `#f0ebe4`
- Border: `1px solid rgba(26,21,16,0.15)`
- Text: `#1a1510`
- Placeholder: `#9c8f85`
- Border-radius: `6px`
- Padding: `8px 12px`
- Font: `DM Sans, 13px`
- Focus border: `1px solid #b8924a`

**Search button:**
- Background: `#b8924a`
- Text: `#faf7f4`
- Border: none
- Border-radius: `6px`
- Padding: `8px 16px`
- Font: `DM Sans, 13px, weight 500`
- Hover: `background: #a07840`

**Layer list rows:**
- Height: `auto`, `padding: 8px 10px`
- Inactive row: no background, `border-radius: 6px`
- Active row: `background: rgba(184,146,74,0.1)`, `border-radius: 6px`
- Dot: `8px` diameter (smaller than current)
- Layer name: `font-size: 13px; font-weight: 500; color: #1a1510;`
- Description (active only): `font-size: 11px; color: #5c5248; margin-top: 2px;`
- Radio button: replace with a simple filled dot indicator: when active, show a small `8px` gold dot; when inactive, show an `8px` circle with `border: 1.5px solid #9c8f85`

**Verified sources section:**
- Label: same as section labels above
- Collapsed by default
- Expanded text: `font-size: 10px; color: #9c8f85; line-height: 1.6;`

---

## Change 5 — Right Panel (Detail Panel) Design Refinement

Apply these changes to the LA detail panel that slides in on click:

**Panel background:** `#faf7f4` (same as sidebar)
**Panel border-left:** `1px solid rgba(26,21,16,0.1)`

**LA name heading:**
- `font-family: 'Cormorant Garamond', serif; font-size: 24px; font-weight: 700; color: #1a1510; letter-spacing: -0.01em;`
- `margin-bottom: 2px;`

**LAD24CD code:**
- `font-family: 'DM Sans', sans-serif; font-size: 10px; color: #9c8f85; letter-spacing: 0.1em; text-transform: uppercase;`

**Section headers (TEMPORARY ACCOMMODATION, HOUSING PRESSURE, etc.):**
- `font-family: 'DM Sans', sans-serif; font-size: 9px; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase; color: #8a7a6e;`
- `margin-top: 16px; margin-bottom: 8px;`
- Add a `1px` bottom border in `rgba(26,21,16,0.08)` below each header

**Metric rows:**
- Display as two-column: label left, value right
- Label: `font-size: 12px; color: #5c5248; font-family: 'DM Sans';`
- Value: `font-size: 12px; font-weight: 600; color: #1a1510; font-family: 'DM Sans'; text-align: right;`
- Row padding: `6px 0`
- Row border-bottom: `1px solid rgba(26,21,16,0.05)`

**YoY Change value:**
- Positive: `color: #15803d; font-weight: 700;` (dark green)
- Negative: `color: #b91c1c; font-weight: 700;` (dark red)
- Neutral: `color: #5c5248;`

**Flag alerts (S114, EFS):**
- S114: `background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; border-radius: 6px; padding: 6px 10px; font-size: 11px; font-weight: 600;`
- EFS: `background: #fff7ed; border: 1px solid #fed7aa; color: #c2410c; border-radius: 6px; padding: 6px 10px; font-size: 11px; font-weight: 600;`
- Both flags stacked vertically with `4px` gap

**Close button (X):**
- `color: #9c8f85;`
- Hover: `color: #1a1510;`

**Panel width:** Keep at `360px`

**Panel scroll:** The panel should scroll internally if content is taller than viewport. Add `overflow-y: auto` to the panel content area.

---

## Change 6 — Legend Refinement

**Legend panel:**
- Background: `rgba(250,247,244,0.92)` (warm white, semi-transparent)
- Border: `1px solid rgba(26,21,16,0.1)`
- Border-radius: `8px`
- Padding: `10px 14px`
- Box-shadow: `0 2px 8px rgba(0,0,0,0.08)`

**Legend title:**
- `font-size: 11px; font-weight: 600; color: #1a1510; font-family: 'DM Sans'; margin-bottom: 6px;`

**Gradient bar:** Keep as-is but ensure it uses the active layer's colour scale

**Labels (Low / Critical):**
- `font-size: 10px; color: #5c5248;`

---

## Change 7 — Map Fill Opacity

On the light basemap, increase fill opacity from `0.75` to `0.82` so the choropleth reads clearly against the lighter background.

Keep financial stress borders unchanged: S114 red `#ef4444` width `3`, EFS orange `#f97316` width `3`.

---

## Commit and Deploy

After editing:
1. Commit `viewers/demand_map.html`
2. Commit new `index.html` at repo root
3. Push to `main`
4. Verify at `https://map.slendeavours.org/` — should redirect to demand map
5. Verify at `https://map.slendeavours.org/viewers/demand_map.html` — direct URL should still work
6. Verify right panel opens on click, data is readable, no grey-on-blue text
7. Verify switching layers changes both the choropleth colour AND the legend gradient
8. Verify light basemap is loading (warm grey, not dark)
