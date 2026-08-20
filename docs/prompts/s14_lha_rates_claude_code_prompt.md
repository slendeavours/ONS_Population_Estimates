# S14 — VOA/DWP LHA Rates — Claude Code Build Prompt

## Objective

Build Source 14 of the exempt accommodation intelligence pipeline: Local Housing Allowance (LHA) rates for all 296 English local authorities. This replaces the £350/week LHA proxy currently used in reports and map visualisation with actual per-LA rates by bedroom category.

LHA rates are published by Broad Rental Market Area (BRMA) — 152 areas in England — not by local authority (296 areas). The core engineering task is building a verified spatial crosswalk from BRMAs to LAs, then loading the rates and joining them into the pipeline's pre-computation workflow.

---

## Environment

### Postgres
- **Host:** `postgres` (Docker network — if connecting from outside Docker, use `localhost`)
- **Port:** `5432`
- **Database:** `exempt_pipeline`
- **User:** `pipeline_user`
- **Password:** set via environment variable `EXEMPT_PIPELINE_DB_PASSWORD` — do not hardcode

If `EXEMPT_PIPELINE_DB_PASSWORD` is not set, prompt for it once and use it for the session. Never write it to any file.

### Python packages required
Install if not present: `psycopg2-binary`, `geopandas`, `shapely`, `requests`, `fiona` (or `pyogrio`).

### GitHub
The pipeline documentation and supporting files live in the `slendeavours` GitHub account. After building, produce node documentation markdown files for the user to commit.

---

## Data Sources

### Source A — DWP UC LHA Rates (primary ingest)
- **URL:** `https://assets.publishing.service.gov.uk/media/69d654a2e1430e837a86f64a/england-rates-2026-to-2027.csv`
- **Publisher:** Department for Work and Pensions
- **Format:** CSV, ~152 rows (England BRMAs only)
- **Columns:** BRMA name, SAR (monthly £), 1 Bed (monthly £), 2 Bed (monthly £), 3 Bed (monthly £), 4 Bed (monthly £)
- **Currency values have:** £ symbol prefix, comma thousands separators, some values quoted
- **Rates are monthly.** Convert to weekly using: `weekly = monthly × 12 ÷ 52`
- **Financial year:** 2026-27 (rates frozen at 2024-25 levels since the April 2024 re-peg)
- **Note:** Row 1 is a title row (not column headers). Row 2 is column headers. Data starts row 3.

### Source B — VOA BRMA Boundary Shapefiles (for spatial mapping)
- **Publication page:** `https://www.gov.uk/government/publications/broad-rental-market-area-boundary-layer-for-geographical-information-system-gis-applicable-may-2020`
- **Action:** Fetch this page, find the shapefile download link (`.zip` or `.shp`), download and extract
- **Fallback:** If GOV.UK file is unavailable, try Datadaptive BRMA boundaries at `https://www.datadaptive.com/` (released under OGL)
- **Format:** ESRI Shapefile (`.shp` + supporting files), CRS likely EPSG:27700 (British National Grid) — transform to EPSG:4326 (WGS84) for matching against LA centroids

### Source C — LA Centroids (already in Postgres)
- **Table:** `la_boundaries` in `exempt_pipeline`
- **Columns needed:** `lad24cd`, `lad24nm`, `longitude`, `latitude`
- **296 rows** — all current English LAs (LAD24CD, ONS May 2024 geography)
- Longitude and latitude are centroid coordinates in WGS84 (EPSG:4326)

---

## Tasks

### Task 1 — Download and parse the DWP LHA CSV

1. Download Source A CSV
2. Parse it, handling:
   - Skip row 1 (title row) — use row 2 as headers
   - Strip `£` symbols and commas from all numeric values
   - Convert all rate columns from monthly to weekly: `value × 12 ÷ 52`
   - Round weekly values to 2 decimal places
3. Produce a clean Python list/DataFrame: `brma_name`, `sar_weekly`, `one_bed_weekly`, `two_bed_weekly`, `three_bed_weekly`, `four_bed_weekly`
4. Print row count and first 5 rows as a sanity check
5. Verify Birmingham is present and SAR weekly ≈ £78.83 (monthly £341.58 × 12 ÷ 52)

### Task 2 — Download BRMA boundaries, build LA→BRMA mapping, and verify automatically

#### 2a. Build the mapping via spatial join

1. Fetch the GOV.UK publication page for Source B and extract the shapefile download URL
2. Download and extract the shapefile
3. Load into geopandas, reproject to EPSG:4326 if necessary
4. Query `la_boundaries` from Postgres — extract all 296 rows with `lad24cd`, `lad24nm`, `longitude`, `latitude`
5. Create a Point geometry for each LA from its longitude/latitude centroid
6. Spatial join: for each LA point, find which BRMA polygon contains it
7. Where an LA centroid falls outside all BRMA polygons (possible for coastal/edge LAs), use nearest BRMA by distance and flag the `mapping_method` as `'nearest_brma_fallback'`

#### 2b. Automated verification suite — ALL checks must pass before proceeding

The user does not have the domain knowledge to review 296 rows by eye. The following verification layers are mandatory. If any critical check fails, **halt and report** — do not load to Postgres.

**CHECK 1 — Anchor set (known-correct mappings)**

The following BRMA-to-LA mappings are verifiably correct from the DWP CSV naming and GOV.UK source data. Compare the spatial join result against every one of these. If ANY anchor fails, the spatial join is unreliable — halt and investigate.

```python
ANCHOR_SET = {
    # Target markets (highest priority — if these are wrong, nothing else matters)
    'E08000025': 'Birmingham',           # Birmingham LA → Birmingham BRMA
    'E08000012': 'Greater Liverpool',    # Liverpool LA → Greater Liverpool BRMA
    'E06000018': 'Nottingham',           # Nottingham LA → Nottingham BRMA
    'E08000003': 'Central Greater Manchester',  # Manchester LA → Central Greater Manchester BRMA
    'E06000009': 'Fylde Coast',          # Blackpool LA → Fylde Coast BRMA

    # Major cities with obvious BRMA name matches
    'E08000035': 'Leeds',                # Leeds
    'E08000016': 'Barnsley',             # Barnsley
    'E08000019': 'Sheffield',            # Sheffield
    'E08000032': 'Bradford & South Dales', # Bradford
    'E06000015': 'Derby',                # Derby
    'E08000036': 'Wakefield',            # Wakefield
    'E06000014': 'York',                 # York
    'E06000031': 'Peterborough',         # Peterborough
    'E07000008': 'Cambridge',            # Cambridge
    'E07000178': 'Oxford',               # Oxford
    'E06000044': 'Portsmouth',           # Portsmouth
    'E06000045': 'Southampton',          # Southampton
    'E06000043': 'Brighton and Hove',    # Brighton and Hove
    'E06000023': 'Bristol',              # Bristol
    'E06000022': 'Bath',                 # Bath and North East Somerset → Bath BRMA
    'E08000026': 'Coventry',             # Coventry
    'E06000016': 'Leicester',            # Leicester
    'E07000071': 'Colchester',           # Colchester
    'E07000202': 'Ipswich',              # Ipswich (now in East Suffolk or standalone)
    'E06000034': 'Thurrock',             # (check — should be South West Essex or similar)
    'E08000037': 'York',                 # If this code exists
    'E07000105': 'Ashford',              # Ashford

    # London boroughs — must map to London-area BRMAs (names containing 'London')
    'E09000001': 'Central London',       # City of London
    'E09000007': 'Inner South East London',  # Camden → could be Inner North London
    'E09000033': 'Inner West London',    # Westminster → Inner West London
    'E09000030': 'Outer South West London',  # Tower Hamlets → Inner East London
    # Note: London mapping is complex — see CHECK 3 for the broader London rule

    # Black Country BRMAs
    'E08000028': 'Black Country',        # Sandwell
    'E08000029': 'Black Country',        # Solihull → actually Solihull BRMA
    'E08000030': 'Black Country',        # Walsall
    'E08000031': 'Black Country',        # Wolverhampton

    # Other distinctive mappings
    'E06000046': 'Isle of Wight',        # Isle of Wight
    'E07000174': 'Staffordshire North',  # Staffordshire Moorlands → Staffordshire North
    'E08000011': 'Knowsley',             # Knowsley → Greater Liverpool
}
```

**IMPORTANT:** The anchor set above is a starting point. Before using it, Claude Code MUST independently verify each anchor pair by:
1. Confirming the LAD24CD code exists in `la_boundaries` (query Postgres)
2. Confirming the BRMA name exists in the parsed DWP CSV
3. Removing any anchor pair where either side cannot be verified

Do NOT blindly trust the anchor set — it was compiled from memory and may contain errors. The anchors themselves need validating against live data before being used to validate the spatial join. If an anchor code doesn't exist in `la_boundaries`, drop it from the set. If a BRMA name doesn't appear in the CSV, drop it. Only verified anchors count.

After pruning, expect ~25-35 verified anchors. Compare spatial join results against these. If ≥90% match, the spatial join is reliable. If <90% match, halt — something is wrong with the boundaries or the centroid data.

For any mismatches: log them, investigate whether the anchor was wrong or the spatial join was wrong. Correct the anchor set if needed. A small number of genuine mismatches (2-3) is acceptable for edge-case LAs where the centroid falls near a BRMA boundary — override these with the anchor value and set `mapping_method = 'anchor_override'`.

**CHECK 2 — Coverage**

| Check | Expected | Action if failed |
|-------|----------|-----------------|
| Total LAs mapped | 296 | HALT — orphaned LAs mean the spatial join has gaps |
| LAs with NULL brma_name | 0 | HALT |
| Distinct BRMAs assigned | Should be close to 152 (not all BRMAs may have LA centroids in them — some very small BRMAs may not) | WARNING if <140; HALT if <120 |
| Every BRMA in DWP CSV has ≥1 LA | Ideally yes | WARNING for any unmatched BRMAs — list them |
| No LA mapped to a BRMA not in the DWP CSV | 0 mismatches | HALT — means BRMA name mismatch between shapefile and CSV |

**The BRMA name mismatch between shapefile and DWP CSV is the most likely failure mode.** The shapefile may use different BRMA names than the DWP CSV (e.g. "Greater Manchester South" vs "Southern Greater Manchester"). If this happens:
1. Print both name lists side by side, sorted alphabetically
2. Build a fuzzy-match reconciliation (Levenshtein distance or similar)
3. Apply the reconciliation and re-run coverage check
4. Log the reconciliation mappings so they're auditable

**CHECK 3 — London rule**

All 33 London boroughs (LAD24CD starting with `E09`) must map to a BRMA containing the word "London" in its name. The London BRMAs in the DWP CSV are:

- Central London
- Inner East London
- Inner North London
- Inner South East London
- Inner South West London
- Inner West London
- North West London
- Outer East London
- Outer North East London
- Outer North London
- Outer South East London
- Outer South London
- Outer South West London
- Outer West London

Query `la_boundaries` for all E09* codes, check each one's assigned BRMA contains 'London'. If any London borough maps to a non-London BRMA, that's a hard error — HALT.

**CHECK 4 — Geographic coherence**

For each BRMA group (all LAs mapped to the same BRMA), calculate the maximum distance between any two LA centroids in the group. Flag any group where the max inter-centroid distance exceeds 100km. This catches cases where an LA has been misassigned to a BRMA in a completely different region.

```python
from math import radians, cos, sin, asin, sqrt

def haversine_km(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return 2 * 6371 * asin(sqrt(a))
```

If any group exceeds 100km spread, print the outlier LA and investigate. This is a WARNING, not a halt — some large BRMAs (e.g. "Shropshire") legitimately span wide areas.

**CHECK 5 — Rate reasonableness (post-join)**

After the mapping is built and rates are joined (before loading to Postgres), run these sanity checks:

| Check | Rule | Action |
|-------|------|--------|
| SAR floor | Every LA's SAR weekly > £50 | HALT if any below |
| SAR ceiling | Every LA's SAR weekly < £250 | HALT if any above (only Central London should be near £200) |
| London premium | Mean SAR for E09* LAs > mean SAR for all LAs | WARNING if not (London should be above average) |
| Northern discount | Mean SAR for target markets (Birmingham, Liverpool, Nottingham, Manchester, Blackpool) < national mean SAR | WARNING if not |
| LHA < rate card | For every LA where S20 rate card data exists (loaded separately), LHA SAR weekly < rate card 1-bed weekly ÷ 7 × ... | WARNING if violated — exempt rates should exceed LHA |

**Note on the rate card comparison:** The S20 commercial rate card is stored externally (not yet in Postgres). Skip this check if that data isn't accessible. It's a nice-to-have, not a gate.

**CHECK 6 — Pass/fail gate**

Print a summary table:

```
VERIFICATION SUMMARY
====================
Anchor set verified:     [X] of [Y] matched (Z%)
Coverage — LAs mapped:   296 of 296
Coverage — BRMAs used:   [N] of 152
London rule:             PASS/FAIL
Geographic coherence:    [N] warnings
Rate reasonableness:     PASS/FAIL
BRMA name reconciliation: [N] names remapped

OVERALL: PASS / FAIL
```

**If OVERALL = PASS:** proceed to Task 3 (create tables and load data). Print the summary but do not pause for human review.

**If OVERALL = FAIL:** halt. Print all failures. Do not load any data. The user needs to investigate before re-running.

### Task 3 — Create database tables

After all Task 2b verification checks pass (OVERALL = PASS), create two tables:

**Table 1: `la_brma_mapping`**
```sql
CREATE TABLE IF NOT EXISTS la_brma_mapping (
    lad24cd         VARCHAR(9)    PRIMARY KEY,
    la_name         VARCHAR(100),
    brma_name       VARCHAR(100)  NOT NULL,
    brma_secondary  VARCHAR(100),
    mapping_method  TEXT          DEFAULT 'centroid_spatial_join',
    source          TEXT          DEFAULT 'VOA BRMA boundaries May 2020 × la_boundaries centroids',
    loaded_at       TIMESTAMPTZ   DEFAULT NOW()
);
```

**Table 2: `brma_lha_rates`**
```sql
CREATE TABLE IF NOT EXISTS brma_lha_rates (
    brma_name           VARCHAR(100)  NOT NULL,
    financial_year      VARCHAR(7)    NOT NULL,
    sar_weekly          NUMERIC(8,2),
    one_bed_weekly      NUMERIC(8,2),
    two_bed_weekly      NUMERIC(8,2),
    three_bed_weekly    NUMERIC(8,2),
    four_bed_weekly     NUMERIC(8,2),
    sar_monthly         NUMERIC(8,2),
    one_bed_monthly     NUMERIC(8,2),
    two_bed_monthly     NUMERIC(8,2),
    three_bed_monthly   NUMERIC(8,2),
    four_bed_monthly    NUMERIC(8,2),
    source              TEXT,
    loaded_at           TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (brma_name, financial_year)
);
```

### Task 4 — Load data

1. **Upsert mapping data** into `la_brma_mapping` — 296 rows, ON CONFLICT (lad24cd) DO UPDATE
2. **Upsert rate data** into `brma_lha_rates` — ~152 rows, ON CONFLICT (brma_name, financial_year) DO UPDATE
   - `financial_year` = `'2026-27'`
   - `source` = `'DWP UC LHA rates 2026-27, https://www.gov.uk/government/publications/universal-credit-local-housing-allowance-rates-2026-to-2027'`
   - Store both weekly and monthly values (monthly = original CSV values, weekly = converted)

### Task 5 — Add LHA columns to staging_la_signals

The `staging_la_signals` table needs new columns for LHA data. Use ALTER TABLE with IF NOT EXISTS guards (Postgres 9.6+ syntax via DO block):

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'staging_la_signals' AND column_name = 'lha_brma_name') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_brma_name VARCHAR(100);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'staging_la_signals' AND column_name = 'lha_sar_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_sar_weekly NUMERIC(8,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'staging_la_signals' AND column_name = 'lha_1bed_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_1bed_weekly NUMERIC(8,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'staging_la_signals' AND column_name = 'lha_2bed_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_2bed_weekly NUMERIC(8,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'staging_la_signals' AND column_name = 'lha_3bed_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_3bed_weekly NUMERIC(8,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'staging_la_signals' AND column_name = 'lha_4bed_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_4bed_weekly NUMERIC(8,2);
    END IF;
END $$;
```

### Task 6 — Log pipeline run

```sql
INSERT INTO pipeline_run_log (
    agent_name, source_number, rows_written, started_at, completed_at, status, notes
)
VALUES (
    'Source 14 - LHA Rates', 14, <mapping_rows + rate_rows>,
    NOW(), NOW(), 'success',
    'DWP UC LHA rates 2026-27 (frozen at 2024-25 levels). BRMA-to-LA mapping built via centroid spatial join against VOA BRMA boundaries May 2020.'
);
```

### Task 7 — Verification queries

Run and print results for each:

**7a. Rate table row count and sample**
```sql
SELECT COUNT(*) AS brma_count FROM brma_lha_rates WHERE financial_year = '2026-27';
SELECT brma_name, sar_weekly, one_bed_weekly, four_bed_weekly
FROM brma_lha_rates WHERE financial_year = '2026-27'
ORDER BY brma_name LIMIT 10;
```

**7b. Mapping coverage**
```sql
SELECT COUNT(*) AS mapped_las FROM la_brma_mapping;
SELECT COUNT(*) AS unmapped_las
FROM la_boundaries b
LEFT JOIN la_brma_mapping m ON m.lad24cd = b.lad24cd
WHERE m.lad24cd IS NULL;
```
Expect: 296 mapped, 0 unmapped.

**7c. Target market spot-check**
```sql
SELECT m.lad24cd, m.la_name, m.brma_name,
       r.sar_weekly, r.one_bed_weekly, r.two_bed_weekly, r.three_bed_weekly, r.four_bed_weekly
FROM la_brma_mapping m
JOIN brma_lha_rates r ON r.brma_name = m.brma_name AND r.financial_year = '2026-27'
WHERE m.la_name IN ('Birmingham', 'Liverpool', 'Nottingham', 'Manchester', 'Blackpool')
ORDER BY m.la_name;
```

**7d. Full join test (all 296 LAs)**
```sql
SELECT COUNT(*) AS las_with_lha
FROM la_boundaries b
JOIN la_brma_mapping m ON m.lad24cd = b.lad24cd
JOIN brma_lha_rates r ON r.brma_name = m.brma_name AND r.financial_year = '2026-27';
```
Expect: 296.

### Task 8 — Produce node documentation

Generate six markdown files following this format:

```
# Node [N] — [Name]
## Type
## Purpose
## Credential (if applicable)
## Query / Code / URL (full content)
## Logic (for code steps — step by step)
## Query Parameters (for Postgres steps — parameter mapping table)
## Behaviour (conflict handling, re-run safety)
## Connection
- Input: [previous step]
- Output: [next step]
## Verified Output (confirmed result and date)
```

Files to produce:

1. `s14_node1_fetch_lha_csv.md` — HTTP download of DWP CSV
2. `s14_node2_fetch_brma_boundaries.md` — download of BRMA shapefiles
3. `s14_node3_build_brma_la_mapping.md` — spatial join logic
4. `s14_node4_create_tables.md` — both CREATE TABLE statements
5. `s14_node5_upsert_mapping.md` — la_brma_mapping upsert
6. `s14_node6_upsert_lha_rates.md` — brma_lha_rates upsert
7. `s14_node7_log_run.md` — pipeline_run_log insert

Save all files to the working directory for the user to review and upload to project knowledge.

### Task 9 — Print the updated W1 Node 5 SQL

Print the new version of the W1 Node 5 `la_signals` query with LHA joins added. The current query (shown below for reference) needs two new LEFT JOINs and six new SELECT columns.

**New joins to add (after the existing `la_imd_2025` join):**
```sql
LEFT JOIN la_brma_mapping lbm ON lbm.lad24cd = b.lad24cd
LEFT JOIN brma_lha_rates lha ON lha.brma_name = lbm.brma_name AND lha.financial_year = '2026-27'
```

**New SELECT columns to add:**
```sql
lbm.brma_name AS lha_brma_name,
lha.sar_weekly AS lha_sar_weekly,
lha.one_bed_weekly AS lha_1bed_weekly,
lha.two_bed_weekly AS lha_2bed_weekly,
lha.three_bed_weekly AS lha_3bed_weekly,
lha.four_bed_weekly AS lha_4bed_weekly,
```

**New INSERT column list entries:**
```sql
lha_brma_name, lha_sar_weekly, lha_1bed_weekly, lha_2bed_weekly, lha_3bed_weekly, lha_4bed_weekly
```

**New ON CONFLICT SET entries:**
```sql
lha_brma_name       = EXCLUDED.lha_brma_name,
lha_sar_weekly      = EXCLUDED.lha_sar_weekly,
lha_1bed_weekly     = EXCLUDED.lha_1bed_weekly,
lha_2bed_weekly     = EXCLUDED.lha_2bed_weekly,
lha_3bed_weekly     = EXCLUDED.lha_3bed_weekly,
lha_4bed_weekly     = EXCLUDED.lha_4bed_weekly,
```

Print the complete updated query so the user can replace it in their n8n workflow.

Do NOT execute this query — just print it. The user will update the n8n node manually.

---

## Important constraints

- **Verify, don't guess.** If the BRMA shapefile download URL can't be found or the file can't be downloaded, stop and say so. Do not fabricate a mapping from memory.
- **The BRMA-to-LA mapping is the critical step.** The automated verification suite in Task 2b replaces manual review. If all six checks pass, proceed to load without pausing. If any critical check fails, halt and report — the user cannot fix mapping issues by eye, so the failure report must be specific enough for Claude Code to investigate and resolve on a re-run.
- **Verify the anchor set before using it.** The anchor set in this prompt was compiled from memory and may contain errors. Every anchor pair must be validated against live data (LAD24CD exists in `la_boundaries`, BRMA name exists in DWP CSV) before being used as a verification standard. Drop any pair that can't be confirmed.
- **BRMA name matching is fragile.** The shapefile attribute table and the DWP CSV may use different names for the same BRMA. Build a reconciliation layer and log every name remap. Fuzzy matching (Levenshtein or token-set-ratio) is expected and acceptable.
- **Idempotency.** Every table creation and data load must be safe to re-run. Use `CREATE TABLE IF NOT EXISTS` and `ON CONFLICT ... DO UPDATE`.
- **No data in n8ndb.** All tables go in `exempt_pipeline`.
- **LAD24CD is the universal join key.** Every LA-level table uses it as the geographic key, matching `la_boundaries`.
- **Store both weekly and monthly rates.** The DWP publishes monthly; the pipeline and the S20 rate card use weekly. Keep both so no precision is lost in conversion.

---

## Pipeline context (reference only — do not modify these tables)

### la_boundaries (296 rows)
```sql
lad24cd VARCHAR(9) PRIMARY KEY, lad24nm VARCHAR(100), longitude NUMERIC(10,6), latitude NUMERIC(10,6), shape_area NUMERIC(20,2), geojson JSONB, loaded_at TIMESTAMPTZ, source_date DATE
```

### pipeline_run_log
```sql
id SERIAL PRIMARY KEY, agent_name TEXT, source_number INTEGER, rows_written INTEGER, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, status TEXT, notes TEXT
```

### staging_la_signals (keyed by run_id + lad24cd)
Currently has columns for TA, rough sleeping, care leavers, MARAC, HB SA caseload, housing register, RO4 spend, EFS/S114 flags, and IMD rank. The LHA columns (Task 5) are additive — do not alter existing columns.
