# Claude Code Prompt — S19 Land Registry UK House Price Index

## Context

This is Source 19 in the exempt accommodation intelligence pipeline. The pipeline
is a Postgres database (`exempt_pipeline`, user `pipeline_user`) running in Docker
on WSL2. Prior sources are built; this is a new addition.

**Do not skip the phase gates.** Build Phase 1 completely and report verified
output before starting Phase 2.

---

## Phase 1 — S19 ETL: average house price per LA into Postgres

### What you are building

A Python ETL script that:
1. Fetches the current Land Registry UK HPI data from GOV.UK
2. Filters to English local authorities only
3. Loads monthly average house prices (all-property + by property type) into a
   new Postgres table `la_house_prices`
4. Verifies the load
5. Logs to `pipeline_run_log`
6. Produces node documentation files
7. Pushes everything to the `slendeavours/ONS_Population_Estimates` GitHub repo

### Step 0 — Inspect the repo before writing any code

Clone the repo at depth 1 and read the following before writing any code:

```bash
git clone --depth 1 https://github.com/slendeavours/ONS_Population_Estimates.git /tmp/ons_repo
```

Then read:
- `/tmp/ons_repo/index.html` (the Mapbox map — needed for Phase 2)
- `/tmp/ons_repo/README.md` (understand current repo state)

Do not modify any files yet. Note what you observe about how the map fetches its
data (URLs, source names, paint properties used for existing layers).

---

### Step 1 — URL acquisition (landing page first)

Fetch the collections page to find the most recent edition:

```
https://www.gov.uk/government/collections/uk-house-price-index-reports
```

From the page content, extract the first link matching the pattern:
`/government/statistical-data-sets/uk-house-price-index-data-downloads-*`

This is the most recent data downloads page. Fetch it.

From that page, extract the two CSV file URLs with these name patterns:
- `Average-prices-{YYYY}-{MM}.csv` — all-property average price + index
- `Average-prices-Property-Type-{YYYY}-{MM}.csv` — price by property type

Do not construct or hardcode these URLs. Extract them from the page.

**Sanity gate on download:** reject any CSV under 1 KB (indicates redirect or error
page). Print the edition name and file sizes before proceeding.

---

### Step 2 — Inspect both CSVs before processing

Download both CSVs to a temp directory. Print:
- Column names (exact)
- First 5 data rows
- Total row count
- Unique values in any geography-type column

Do not load to Postgres until inspection output is confirmed. **Pause here and
show me the inspection output before proceeding.**

---

### Step 3 — Table creation

Connect to Postgres: `host=localhost port=5432 dbname=exempt_pipeline
user=pipeline_user` (password from environment variable `PIPELINE_DB_PASSWORD`).

Create the table:

```sql
CREATE TABLE IF NOT EXISTS la_house_prices (
    lad24cd            VARCHAR(9)      NOT NULL,
    period             DATE            NOT NULL,
    avg_price_all      NUMERIC(12,2),
    avg_price_detached NUMERIC(12,2),
    avg_price_semi     NUMERIC(12,2),
    avg_price_terraced NUMERIC(12,2),
    avg_price_flat     NUMERIC(12,2),
    index_all          NUMERIC(8,4),
    sales_volume       INTEGER,
    loaded_at          TIMESTAMPTZ     DEFAULT NOW(),
    PRIMARY KEY (lad24cd, period)
);

COMMENT ON TABLE la_house_prices IS
  'Land Registry UK HPI average prices per English LA per month.
   Source: HM Land Registry / ONS UK HPI. Grain: lad24cd × period.
   avg_price_* values are NULL where the Land Registry suppresses
   due to low transaction volumes. sales_volume NULL for most recent
   2 months (Land Registry suppresses new-build figures).
   First loaded: <date of first load>.';
```

---

### Step 4 — Process and load

**Geography filter:** keep only rows where the area code starts with E06, E07,
E08, or E09. This gives English LAs. Drop Wales (W), Scotland (S), Northern
Ireland (N), regions (E12), England overall (E92), and UK (K02/K03).

**Code reconciliation:** join through `la_code_lookup` to resolve any historical
GSS codes to the pipeline's canonical LAD24CD. The join:

```sql
SELECT canonical_lad24cd FROM la_code_lookup
WHERE source_code = '<area_code from file>'
```

If the area code is already a valid LAD24CD (present in `la_boundaries`), use it
directly. If it maps through `la_code_lookup`, use the canonical code. If it maps
to nothing (i.e. absent from both tables), log it as unresolved — do not drop
silently. Print a summary of unresolved codes after processing.

**Known recode needed:** from April 2025 editions onward, Land Registry publishes
Barnsley as E08000038 and Sheffield as E08000039 (post-boundary-change codes).
The pipeline canonical codes are E08000016 (Barnsley) and E08000019 (Sheffield).
These WILL appear as unresolved if not explicitly handled — add them to the
recode logic before running: E08000038 → E08000016, E08000039 → E08000019. Verify
against `la_code_lookup` that these canonical codes exist in `la_boundaries` before
proceeding.

**Period filter:** load only rows where period >= 2022-01-01. The Land Registry
publishes the full back series from 1995 in every edition; we do not need it all.

**Suppression handling:** the Land Registry uses empty strings or NaN for
suppressed values. Store these as NULL — do not fill, estimate, or substitute.

**Upsert logic:**

```sql
INSERT INTO la_house_prices (
    lad24cd, period,
    avg_price_all, avg_price_detached, avg_price_semi,
    avg_price_terraced, avg_price_flat,
    index_all, sales_volume
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (lad24cd, period) DO UPDATE SET
    avg_price_all      = EXCLUDED.avg_price_all,
    avg_price_detached = EXCLUDED.avg_price_detached,
    avg_price_semi     = EXCLUDED.avg_price_semi,
    avg_price_terraced = EXCLUDED.avg_price_terraced,
    avg_price_flat     = EXCLUDED.avg_price_flat,
    index_all          = EXCLUDED.index_all,
    sales_volume       = EXCLUDED.sales_volume,
    loaded_at          = NOW();
```

Batch in chunks of 500 rows.

---

### Step 5 — Verification suite (automated, all must pass)

Run all checks before proceeding. Print PASS or FAIL for each.

**CHECK 1 — Row count**
Expected: > 260 LAs × at least 30 periods (2022-01-01 to present) = > 7,800 rows.
Print actual count.

**CHECK 2 — All 296 LAs represented**
```sql
SELECT COUNT(DISTINCT lad24cd) FROM la_house_prices;
```
Expected: 296. If < 296, print the missing LAD24CDs.

**CHECK 3 — Target market spot-check**
```sql
SELECT lad24cd, period, avg_price_all
FROM la_house_prices
WHERE lad24cd IN (
    'E08000025', -- Birmingham
    'E08000012', -- Liverpool
    'E06000018', -- Nottingham
    'E08000003', -- Manchester
    'E06000009'  -- Blackpool
)
AND period = (SELECT MAX(period) FROM la_house_prices)
ORDER BY lad24cd;
```
All 5 must return a non-null avg_price_all.

**CHECK 4 — No implausible prices**
```sql
SELECT COUNT(*) FROM la_house_prices
WHERE avg_price_all IS NOT NULL
AND (avg_price_all < 50000 OR avg_price_all > 2000000);
```
Expected: 0. If any rows fail, print them.

**CHECK 5 — Period coverage**
```sql
SELECT MIN(period), MAX(period), COUNT(DISTINCT period)
FROM la_house_prices;
```
Print and confirm MIN(period) <= 2022-01-01, MAX(period) is within 3 months
of today's date.

**CHECK 6 — Barnsley and Sheffield check**
```sql
SELECT lad24cd, COUNT(*) FROM la_house_prices
WHERE lad24cd IN ('E08000016', 'E08000019')
GROUP BY lad24cd;
```
Both must be present with the same period count as the overall average.
If E08000038 or E08000039 appear in the table, that is a recode failure — fail
this check.

Print OVERALL PASS or FAIL. If FAIL on any check, do not proceed to Step 6.

---

### Step 6 — Log to pipeline_run_log

```sql
INSERT INTO pipeline_run_log
    (agent_name, source_number, rows_written, started_at, completed_at, status, notes)
VALUES
    ('Source 19 - Land Registry UK HPI',
     19,
     <row_count>,
     <start_time>,
     NOW(),
     'success',
     'Land Registry UK HPI monthly average prices per English LA. Edition: <edition>.
      Period range: <min_period> to <max_period>. Recode applied: E08000038→E08000016,
      E08000039→E08000019.');
```

---

### Step 7 — Produce node documentation

Write one markdown file per logical processing step. Save to the current working
directory (not `/tmp`). Follow this exact format:

```
# Node [N] — [Name]

## Type
Python script task

## Purpose
[one sentence]

## Logic
[numbered steps]

## Key parameters
[table: parameter, value]

## Behaviour
[idempotency, suppression handling, re-run safety]

## Verified output
[confirmed result and date]
```

Files to produce:
- `s19_node1_fetch_collection_page.md`
- `s19_node2_fetch_data_downloads_page.md`
- `s19_node3_download_csvs.md`
- `s19_node4_process_and_load.md`
- `s19_node5_verification_suite.md`
- `s19_node6_log_run.md`

Also produce:
- `s19_hpi_source.md` — source summary document in the format used by S18
  (`s18_pipr_source.md`). Include: publisher, dataset name, cadence, landing page
  URL, geography, join key, target table, MIN_PERIOD, first load date, what it
  provides, acquisition pattern, caveats (suppression in small LAs; open-market
  prices only; new-build suppression for 2 most recent months).

---

### Step 8 — Push Phase 1 to GitHub

Use the `gh` CLI authenticated as `slendeavours`.

Push the following files to `slendeavours/ONS_Population_Estimates`:
- The Python ETL script (save as `s19_hpi_build.py`)
- All 7 node documentation files
- `s19_hpi_source.md`

Commit message: `feat(s19): Land Registry UK HPI — monthly average prices per English LA`

Verify the push: run `gh api repos/slendeavours/ONS_Population_Estimates/commits --jq '.[0].commit.message'` and confirm the commit appears.

**Stop here. Report Phase 1 verified output before starting Phase 2.**

---

## Phase 2 — Map layer

### Prerequisite

Phase 1 must be complete and all verification checks must have passed.

You already read `index.html` in Step 0. Now you will extend the map to show
house prices as a new choropleth layer.

---

### Step 9 — Understand the current map data pattern

From your earlier inspection of `index.html`, identify:
- How data is loaded into the map (fetch from GitHub raw? inline GeoJSON?
  Mapbox tileset? Postgres API?)
- What URL(s) are used for existing layers
- What property names exist on features (e.g. `lad24cd`, signal columns)
- What the existing paint expressions look like (step, interpolate, match?)

Report your findings before making any changes.

---

### Step 10 — Export house price data for the map

The map cannot read from Postgres directly. You need to publish the house price
data as a file that the map can fetch.

**Produce a JSON file** with the following structure:

```json
{
  "generated": "<ISO timestamp>",
  "edition": "<LR edition month, e.g. 2026-03>",
  "data": {
    "E08000025": {
      "avg_price_all": 195000,
      "annual_change_pct": 3.2
    },
    ...
  }
}
```

One key per `lad24cd`, keyed on the most recent period in `la_house_prices`.

Calculate `annual_change_pct` as:

```sql
SELECT
    h_cur.lad24cd,
    h_cur.avg_price_all AS current_price,
    ROUND(
        (h_cur.avg_price_all - h_prev.avg_price_all)
        / NULLIF(h_prev.avg_price_all, 0) * 100
    , 1) AS annual_change_pct
FROM la_house_prices h_cur
JOIN la_house_prices h_prev
    ON h_prev.lad24cd = h_cur.lad24cd
    AND h_prev.period = h_cur.period - INTERVAL '12 months'
WHERE h_cur.period = (SELECT MAX(period) FROM la_house_prices)
AND h_cur.avg_price_all IS NOT NULL;
```

Save this file as `hpi_la_prices.json` in the repo root.

---

### Step 11 — Add the Mapbox layer to index.html

Following the **exact same pattern** as existing layers in `index.html` (match
how sources are defined and how layers are added — do not invent a new pattern):

Add:
1. A new source that fetches `/hpi_la_prices.json` from the GitHub raw URL
   (same base URL pattern as existing sources)
2. A new fill layer (`id: 'hpi-layer'`) that paints each LA by `avg_price_all`
   using a quantile-like step expression

**Colour scale for house prices:**

Use a step or interpolate expression. The signal here is affordability for
sourcing — lower prices are operationally significant (affordable stock). Use
a scale that distinguishes the bottom third clearly:

```
< £150,000  → #C9A96E  (gold — notable: very affordable)
< £200,000  → #D4B483
< £250,000  → #DFC69A
< £300,000  → #E8D8BA
< £400,000  → #C8C0B0
≥ £400,000  → #8C8880  (muted — London/SE, de-emphasised)
```

These use the brand palette; the progression mutes as prices rise because high-
price geographies are less relevant to the operating model.

3. A hover tooltip that shows:
   - LA name
   - Average house price (formatted as £XXX,XXX)
   - Annual change % (formatted as +X.X% or −X.X%)
   - Edition month

Match the hover/tooltip pattern already in use for other layers. Do not add
a toggle if other layers don't have one — match the UI pattern exactly.

---

### Step 12 — Push Phase 2 to GitHub

Push the following to `slendeavours/ONS_Population_Estimates`:
- `hpi_la_prices.json`
- Updated `index.html`

Commit message: `feat(map): add Land Registry HPI choropleth layer`

Wait ~5 minutes for GitHub Pages cache to clear. Then verify by fetching:
`https://raw.githubusercontent.com/slendeavours/ONS_Population_Estimates/main/hpi_la_prices.json`

Confirm the JSON is accessible and contains data for the 5 target markets
(Birmingham, Liverpool, Nottingham, Manchester, Blackpool).

---

## Final deliverables checklist

After Phase 2, confirm each item:

- [ ] `la_house_prices` table exists with 296 LAs × 30+ months
- [ ] Verification suite: all 6 checks PASS
- [ ] `pipeline_run_log` entry written for source 19
- [ ] 7 node docs + 1 source doc pushed to GitHub
- [ ] `s19_hpi_build.py` pushed to GitHub
- [ ] `hpi_la_prices.json` pushed to GitHub
- [ ] `index.html` updated with HPI layer and pushed
- [ ] GitHub raw URL for `hpi_la_prices.json` is live and parseable
- [ ] Node docs and source doc listed here for upload to project knowledge

Present all files for download.
