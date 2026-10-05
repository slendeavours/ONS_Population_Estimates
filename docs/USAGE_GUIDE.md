# Usage Guide — Demand Map

How to use the live demand map at [map.slendeavours.org](https://map.slendeavours.org). It covers all 296 English local authorities and is built from `index.html` at the root of this repository.

---

## Opening the Map

Open [map.slendeavours.org](https://map.slendeavours.org) in any modern browser (Chrome, Edge, Firefox, Safari). The map needs an internet connection: the boundaries, the signal data and the basemap are all loaded when the page opens.

The boundary file is about 9.5 MB, so on a slow connection the "Loading 296 local authorities..." message can stay up for 10–30 seconds.

---

## The Screen

| Area | Where | What it holds |
|---|---|---|
| Sidebar | Left (behind the ☰ button on mobile) | Postcode lookup, local authority search, map layers, list of data sources |
| Map | Centre | Every authority shaded by the selected layer |
| Zoom buttons | Top right of the map | Zoom in and out |
| Legend | Bottom left of the map | The selected layer's colour scale, the no-data colour and the S114 / EFS border key |
| Detail panel | Right, opens when you select an area | Every figure held for that authority |

Drag to pan. Scroll or pinch to zoom.

---

## Finding an Area

There are three ways to select an authority:

1. **Click it on the map.** Hovering shows a small label with the authority's name and its value on the current layer.
2. **Search by name.** Type at least two letters in **Local Authority**. Up to eight matches appear, and choosing one zooms to that authority.
3. **Look up a postcode.** Enter a postcode and press **Search** or Enter. The map zooms to the postcode and selects the authority it sits in. Postcodes are looked up through postcodes.io. A postcode outside England is found but has no authority to select, because the map covers England only.

### What happens when you select an area

- **The selected authority stays in full colour and every other authority fades.** This shows which area the panel is describing. The fainter red and orange borders of other S114 and EFS councils fade too.
- **The map moves the selection into view beside the panel.** A click pans only if the panel would cover the area. A search by name or postcode zooms in. On a phone, where the panel covers almost the whole map, the area is centred instead.
- **The detail panel opens** with the authority's name and code at the top.

Close the panel with the ✕ in its top-right corner. The normal colours return.

---

## Map Layers

Choose a layer in the sidebar to shade the map by that measure. Each layer shows the period of its data underneath its name, taken from the latest export.

| Layer | What it shows |
|---|---|
| TA Households | Households in temporary accommodation |
| HB Specified Accommodation | Housing Benefit claimants in specified (exempt) accommodation |
| Homelessness Spend | Total annual homelessness spend (RO4) |
| Housing Register | Social housing waiting list size |
| Care Leavers | Care leavers in semi-independent accommodation (upper-tier councils only) |
| Domestic Violence (MARAC) | MARAC cases, published by police force area |
| Rough Sleeping | Single-night rough sleeping snapshot |
| Deprivation (IMD) | Index of Multiple Deprivation 2025 rank |
| LHA Shared Rate | Local Housing Allowance shared accommodation rate, £/week |
| LHA 1-Bed Rate | Local Housing Allowance one-bedroom rate, £/week |
| Care Providers (SL) | CQC-registered supported living locations (the only supply-side layer) |
| Avg House Price | Land Registry average price, all property types |
| Long-Term Empty Rate | Share of dwellings empty six months or more (Council Taxbase) |

### Reading the colours

- Darker means more on the layer's own scale. The legend's labels say what "more" means, for example *Lower demand* to *Higher demand*. On **Deprivation**, darker means more deprived.
- Most layers split the authorities into seven bands with roughly equal numbers of authorities in each. The colours therefore rank areas against each other and are not fixed thresholds. **Avg House Price** is the exception: it uses fixed bands at £150k, £200k, £250k, £300k and £400k.
- **Dark grey** means no data for that authority on that layer.
- A **red border** marks a council that has issued a Section 114 notice. An **orange border** marks a council receiving Exceptional Financial Support.

### Two layers to read with care

- **Care Leavers** shades upper-tier councils only. Corporate parenting is a county or unitary duty, so districts inside a county are left unshaded because the duty isn't theirs, not because their data is missing.
- **Domestic Violence (MARAC)** is published by police force area. Every authority in a force area carries the same force-wide total, so these aren't counts for individual authorities.

The legend repeats these notes when either layer is selected.

---

## Reading the Detail Panel

The panel shows every figure held for the selected authority, whichever layer is on:

- **Temporary Accommodation**: current households, the previous year, year-on-year change (green ▲ for a rise, red ▼ for a fall) and the trend label.
- **Housing Pressure**: housing register, rough sleeping, HB specified accommodation claimants.
- **Cohort Demand**: care leavers (upper tier), MARAC cases and the MARAC rate per 10,000 (force area).
- **Expenditure**: B&B, nightly paid and total homelessness spend, in £m.
- **LHA Rates**: the Broad Rental Market Area, the shared rate and the one-bed rate.
- **Care Supply (CQC)**: supported living locations.
- **House Prices (LR HPI)**: average price and annual change.
- **Empty Homes (Council Taxbase)**: long-term empty rate, homes empty six months or more, homes charged the empty homes premium, second homes.
- **Context**: population and deprivation rank (out of 296).

Where it applies, a warning at the bottom flags a **Section 114 notice** or Exceptional Financial Support (labelled **Emergency Financial Support** in the panel).

A dash (—) means the figure isn't held for that authority. For example, some councils haven't yet reported the latest RO4 spend, and districts have no care leaver figure.

---

## Data Sources and Freshness

Open **Verified Data Sources** at the bottom of the sidebar to see the fifteen government and official sources behind the map.

The map always loads the latest export from this repository, so the link never needs updating after a pipeline run. The period under each layer name shows how current that layer is. Different sources publish on different cycles, so the layers won't all show the same date. If a figure looks out of date, refresh the page.

The run behind the current export is recorded in [`data/signals/latest.json`](https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/signals/latest.json) (`run_id` and `generated_at`).

---

## Troubleshooting

| Problem | What to do |
|---|---|
| "Loading 296 local authorities..." stays up | Wait up to 30 seconds on a slow connection, then refresh. |
| "Failed to load map data" | The data files on GitHub couldn't be fetched. Check your connection, wait a minute and refresh. |
| "Mapbox token was rejected" | The basemap service refused the map's key, which is restricted to approved addresses. A copy of the page opened from anywhere else shows this. On the live site, report it. |
| An authority shows dark grey | No data on that layer for that authority. Check the panel: other layers may still have figures. |
| A postcode finds the place but no panel opens | The postcode is outside England. |
| The legend is hidden | On a narrow screen the panel can cover it. Close the panel. |

---

## Using the Data Elsewhere

- **Screenshot**: use your browser's or operating system's screenshot tool.
- **Boundaries**: [`data/boundaries/la_boundaries.geojson`](https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/boundaries/la_boundaries.geojson) opens directly in QGIS as a vector layer.
- **Signals**: [`data/signals/staging_la_signals_latest.json`](https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/data/signals/staging_la_signals_latest.json) holds every figure in the panel, one row per authority. Join it to the boundaries on `lad24cd`.
