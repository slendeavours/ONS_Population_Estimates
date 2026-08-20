# S18 — ONS PIPR Backfill, Geography Dimension, and Publish

Standalone Claude Code prompt. Runs in a clean session with no project memory. Everything needed is in this file plus the `.env` in the working folder.

---

## Context

This is the Exempt Accommodation Intelligence Pipeline (Postgres database `exempt_pipeline`, 296 English local authorities, geographic join key LAD24CD, code reconciliation via `la_code_lookup`). This run adds Source 18: ONS Price Index of Private Rents (PIPR) — private market rent levels per local authority, by bedroom category and property type. It fills the cost side of the yield-adjusted demand analysis (Layer 3), which is currently the missing layer.

This is a one-off backfill and structure-discovery run. The recurring monthly refresh will be built later as an n8n sub-workflow (S18) following the pipeline's standard pattern. One required output of this run is a workbook structure document that the n8n build will follow.

Scope boundaries, fixed in advance:
- Do NOT add any layers or make any changes to the Mapbox demand map viewer files. Raw rent levels do not go on the map. The map gets the derived income-vs-rent spread later, as a separate piece of work.
- Do NOT modify or extend the W1 pre-computation workflow or `staging_la_signals`. `la_private_rents` is not yet referenced by W1, so no W1 re-run is required after this load.
- England LAs only.

---

## Environment

Working folder: the `UCES-repo` folder this prompt sits in (or `$REPO_DIR` if set in `.env`).

Read `.env` from the working folder root before anything else. Expected variables:

```
PGHOST=
PGPORT=
PGDATABASE=exempt_pipeline
PGUSER=
PGPASSWORD=
REPO_DIR=          # optional, defaults to current folder
MIN_PERIOD=2024-03-01   # earliest period to load; change here if scope widens
```

Postgres runs in Docker on WSL2. Use a Python script with psycopg2 (or psql) — parameterised queries only, never string concatenation. If any expected variable is missing, stop and report; do not guess connection details.

The `github-publishing` skill is installed. Read and follow it for all git/GitHub operations — it is the authority on sanitisation, secrets scanning, commit conventions, README gates, and CHANGELOG. gh CLI is authenticated against the SL Endeavours account.

---

## Hard rules

1. Never hardcode ONS file URLs. Edition URLs change monthly with inconsistent filename suffixes. Always fetch the dataset landing page first and extract the current edition link.
2. Never guess worksheet names, column positions, or header rows. Inspect the workbook programmatically and document what is actually there before writing any transform code.
3. Parameterised SQL throughout. One logical operation per statement. Idempotent upserts (`ON CONFLICT DO UPDATE`).
4. Every code from the source file passes through `la_code_lookup` (`old_code → new_code`) before joining or inserting. Report any unresolved codes; never silently drop without counting.
5. Uncertainty is flagged explicitly in the final report — never papered over. If a verification check fails, stop, report, and do not proceed to the GitHub publish phase with bad data in Postgres.
6. All verification is automated. The user will not manually inspect data. If a check cannot be automated, say so in the report.
7. No dates, codes, or mappings invented from memory. If an authoritative source for a value cannot be fetched, the dependent step is deferred and reported, not approximated.

---

## Phase 0 — Preflight

1. Record the current date (system date) as the run date. All output files and log entries use it.
2. Load `.env`, confirm all required variables present.
3. Test Postgres connectivity: `SELECT COUNT(*) FROM la_boundaries;` — expect 296. Also confirm `la_code_lookup` exists and report its row count (expected 327: 294 current + 33 historical; the two current self-references beyond 294 were consumed by later corrections — report whatever is actually there, do not "fix" it).
4. Inspect `pipeline_run_log` column list via `information_schema.columns` and confirm it matches: `agent_name, source_number, rows_written, started_at, completed_at, status, notes`. If it differs, conform to what exists.
5. Confirm the working folder's git state: is it a clone of an existing SL Endeavours repo (check `git remote -v`), or a fresh folder? This determines whether the github-publishing skill's phase one (first publish) or phase three (update gates) applies later.
6. Create folder structure if absent: `data/raw/`, `data/processed/`, `docs/`, `scripts/`.

---

## Phase 1 — Acquire

1. Fetch the PIPR dataset landing page (stable URL):
   `https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/priceindexofprivaterentsukmonthlypricestatistics`
2. Parse out the **first** (most recent) edition's xlsx link. The href pattern contains `/file?uri=/economy/inflationandpriceindices/datasets/priceindexofprivaterentsukmonthlypricestatistics/<edition-slug>/<filename>.xlsx`. Record the edition date from the slug.
3. Download the xlsx (~17 MB) to `data/raw/pipr_<edition-date>.xlsx`. Verify the download: file size > 10 MB and openable as a valid workbook.
4. Note: every monthly edition contains the full back series and revises prior provisional months, so only the latest edition is ever downloaded. Do not download historical editions.

---

## Phase 2 — Inspect and document the workbook

1. List every worksheet name and, for each, the header row position, column headers, and row count.
2. Identify the sheet(s) containing **local authority level** data for England with:
   - rent **levels** (average monthly rent in £)
   - rent **index** (reference January 2023 = 100)
   - **annual percentage change**
   broken down by (a) bedroom category and (b) property type.
3. Identify how provisional values are marked (e.g. a `[p]` suffix, a footnote flag column, or bulletin convention that the latest month is provisional). If no in-file marker exists, the convention is: latest period only = provisional, and this must be stated in the structure document.
4. Identify the geography code column and confirm the codes are GSS LA codes (E06/E07/E08/E09 prefixes for England).
5. Write `docs/s18_pipr_workbook_structure.md` recording all of the above: sheet names, header rows, column mappings to target schema, provisional-marking convention, edition date, and any quirks (merged cells, suppressed values, notes rows). This document is the specification the future n8n S18 workflow will be built from. Write it as facts, not as a status snapshot.

Do not proceed to Phase 3 until this document exists.

---

## Phase 3 — Transform

1. Extract LA-level rows for England only (GSS code prefixes E06, E07, E08, E09). Wales, Scotland, NI, regions, and national aggregates are excluded from the load (they can remain in the raw file).
2. Reshape to long format, one row per `(la_code, period, breakdown_type, category)`:
   - `breakdown_type` = `'bedroom'` or `'property_type'`
   - bedroom categories normalised to: `1_bed`, `2_bed`, `3_bed`, `4_plus_bed` (ONS combines studios into the one-bedroom category — note this in the structure doc)
   - property type categories normalised from whatever the file uses (e.g. `flat_maisonette`, `terraced`, `semi_detached`, `detached`) — record the exact mapping in the structure doc
   - if the file also carries an all-properties/all-categories total per LA, load it as `breakdown_type='all', category='all'`
3. Filter to `period >= MIN_PERIOD` (default 2024-03-01).
4. Resolve every la code through `la_code_lookup`. Count and list any codes that do not resolve.
5. Set `provisional = TRUE` per the marking convention identified in Phase 2.
6. Write the transformed dataset to `data/processed/la_private_rents_<edition-date>.csv`.

---

## Phase 4 — Load Postgres

### 4.1 Create tables (idempotent, `CREATE TABLE IF NOT EXISTS`)

```sql
CREATE TABLE IF NOT EXISTS la_private_rents (
    lad24cd           VARCHAR(9)   NOT NULL,
    period            DATE         NOT NULL,
    breakdown_type    VARCHAR(20)  NOT NULL,
    category          VARCHAR(30)  NOT NULL,
    mean_rent         NUMERIC(8,2),
    rent_index        NUMERIC(8,2),
    annual_pct_change NUMERIC(6,2),
    provisional       BOOLEAN      DEFAULT FALSE,
    source            TEXT,
    loaded_at         TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (lad24cd, period, breakdown_type, category)
);

COMMENT ON TABLE la_private_rents IS
'ONS Price Index of Private Rents (PIPR), LA-level rent levels and indices, England. CAVEAT 1: tenancies in receipt of housing benefit are excluded by ONS where identifiable — figures represent open-market opportunity cost, not HB-supported rents. CAVEAT 2: PIPR is stock-based (new and existing tenancies blended) — it lags the price of a newly agreed lease in a rising market. Reference period for the index is January 2023 = 100. Latest month is provisional and revised in the following edition.';

COMMENT ON COLUMN la_private_rents.provisional IS
'TRUE for values published as provisional (typically the latest period); overwritten to FALSE when a later edition finalises them.';
```

Adjust `NUMERIC` precision only if actual file values require it, and record any change in the structure doc.

```sql
CREATE TABLE IF NOT EXISTS la_geography (
    gss_code     VARCHAR(9)   NOT NULL,
    la_name      VARCHAR(100),
    boundary_set VARCHAR(10)  NOT NULL,
    valid_from   DATE         NOT NULL,
    valid_to     DATE,
    loaded_at    TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (gss_code, valid_from)
);

COMMENT ON TABLE la_geography IS
'Geography dimension with code validity windows. Built ahead of LGR: East/West Surrey vest 1 April 2027; most remaining new unitaries vest 1 April 2028. valid_to NULL = current. Successor mappings live in la_succession.';

CREATE TABLE IF NOT EXISTS la_succession (
    predecessor_code VARCHAR(9)   NOT NULL,
    successor_code   VARCHAR(9)   NOT NULL,
    change_date      DATE         NOT NULL,
    change_type      VARCHAR(50),
    apportionment    NUMERIC(6,5) DEFAULT 1.0,
    source           TEXT,
    loaded_at        TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (predecessor_code, successor_code, change_date)
);

COMMENT ON TABLE la_succession IS
'Successor mapping supporting one-to-many splits with population apportionment. Supersedes the one-to-one assumption in la_code_lookup for the 2027/2028 LGR wave, where some districts split between new unitaries. apportionment = share of the predecessor assigned to the successor (1.0 for whole-area transfers).';
```

### 4.2 Seed `la_succession` from `la_code_lookup`

Migrate the existing verified historical mappings (`change_type <> 'current'`) with `apportionment = 1.0`, `change_date = effective_date`, `source = 'migrated from la_code_lookup <run-date>'`. `ON CONFLICT DO NOTHING`. `la_code_lookup` itself is left completely untouched — every existing source workflow depends on it.

### 4.3 Seed `la_geography` — only from an authoritative source

Attempt to fetch official code start dates from the ONS Code History Database / Register of Geographic Codes on the Open Geography Portal (geoportal.statistics.gov.uk). Verify the download URL from the portal at run time; do not use a remembered URL.

- If obtained: seed one row per current English LA (`boundary_set = 'LAD24'`, `valid_from` = official operational start date, `valid_to = NULL`), joined against `la_boundaries` for names.
- If it cannot be obtained or the start dates cannot be verified: create the empty table, skip the seed, and record this as a deferred item in the final report. Do not substitute invented or assumed dates.

No 2027/2028 codes are inserted anywhere in this run — ONS has not issued them yet.

### 4.4 Upsert rent data

Batch upsert from the processed dataset:

```sql
INSERT INTO la_private_rents (lad24cd, period, breakdown_type, category,
                              mean_rent, rent_index, annual_pct_change,
                              provisional, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (lad24cd, period, breakdown_type, category) DO UPDATE SET
    mean_rent         = EXCLUDED.mean_rent,
    rent_index        = EXCLUDED.rent_index,
    annual_pct_change = EXCLUDED.annual_pct_change,
    provisional       = EXCLUDED.provisional,
    source            = EXCLUDED.source,
    loaded_at         = NOW();
```

`source` = `'ONS PIPR <edition-date> edition'`.

### 4.5 Verification suite (all automated, all must pass before Phase 5)

1. **Row reconciliation:** rows loaded = rows in processed CSV minus documented exclusions. Zero unexplained loss.
2. **Coverage:** for each period and breakdown_type, distinct `lad24cd` count between 290 and 296 (allowing ONS suppression). Report any period/category below 290 with the missing LAs listed.
3. **Join integrity:** zero source codes unresolved by `la_code_lookup`, or the full list reported.
4. **Range checks:** `mean_rent` between £200 and £10,000; `rent_index` > 0; `annual_pct_change` between −50 and +100. List violations.
5. **Cross-check against published figures:** fetch the latest ONS "Private rent and house prices, UK" bulletin and verify at least three LA-level or headline figures against loaded values (e.g. the highest and lowest LA rents named in the bulletin). Exact match required for finalised figures; provisional figures must match the bulletin's provisional values.
6. **Idempotency:** re-run the upsert on the same batch; total row count in `la_private_rents` must be unchanged.
7. **Succession migration check:** `la_succession` row count equals the count of non-current rows in `la_code_lookup`; zero predecessor codes present in `la_boundaries`.

### 4.6 Log the run

```sql
INSERT INTO pipeline_run_log (agent_name, source_number, rows_written,
                              started_at, completed_at, status, notes)
VALUES ('Source 18 - ONS PIPR Private Rents', 18, %s, %s, NOW(), 'success',
        'Claude Code backfill. ONS PIPR <edition-date> edition, periods <MIN_PERIOD> onward, England LAs, bedroom + property type breakdowns. Geography dimension tables created; la_succession seeded from la_code_lookup. Monthly refresh to follow as n8n S18.');
```

Log only after the verification suite passes. On failure, log a row with `status = 'failed'` and the failing check in `notes`, then stop.

---

## Phase 5 — Save down and document in the repo

1. Confirm present in the working folder: raw xlsx (`data/raw/`), processed CSV (`data/processed/`), workbook structure doc (`docs/`), and all scripts written during this run (`scripts/`), each script with a header comment stating purpose, inputs, outputs.
2. Write `docs/s18_pipr_source.md` — the source documentation, matching the register style of the other pipeline sources: publisher, cadence (monthly), landing page URL, URL-changes-per-edition gotcha, join key, target tables, caveats (the two encoded in the table comment), MIN_PERIOD, and the dual-model note (UCWS lens: cost side of operator margin, primary; HSS lens: context).
3. Write `docs/geography_dimension.md` — purpose of `la_geography` and `la_succession`, the split/apportionment rationale, LGR vesting dates driving it (April 2027 Surrey, April 2028 main wave), seeding state, and the rule that no 2027/28 codes enter until ONS publishes them.

---

## Phase 6 — GitHub publish

Run the github-publishing skill and follow it fully. In particular:

1. Determine first-publish vs update from the Phase 0 git-state check (`gh repo list` duplicate gate if creating).
2. Sanitisation pass on every file before commit — the `.env` file must never be committed; confirm it is in `.gitignore`, adding it if not.
3. Secrets scan before push (gitleaks if available, otherwise the skill's fallback pattern list).
4. If updating an existing repo: run all four phase-three gates (README drift check, CHANGELOG entry written before push, review stamp, stale branch report).
5. README/data dictionary update: wherever this repo (or its README) lists pipeline sources or database tables, add S18 and the three new tables. This is the only "pages" update in scope. Do not touch any map viewer files, and do not modify other repos unless the working folder's remote is itself the map/data repo — in which case the drift check covers whether its source list needs the S18 entry.
6. Conventional Commits, atomic. Suggested shape: `feat(s18): PIPR backfill ETL, rent levels table and geography dimension` plus a separate `docs(...)` commit for documentation.
7. Push. Never force push.

---

## Phase 7 — Final report

Print a closing report containing:

- Edition date loaded, periods covered, total rows in `la_private_rents`
- Verification suite results, check by check
- `la_geography` seed status (seeded from CHD / deferred and why)
- `la_succession` row count
- Any unresolved codes, suppressed LAs, or range violations
- Git: repo, branch, commits pushed
- Handover items (fixed list plus anything discovered):
  1. Build n8n S18 monthly workflow from `docs/s18_pipr_workbook_structure.md`
  2. Extend W1 / `staging_la_signals` with rent-derived columns and re-run W1 (not done in this run, by design)
  3. Compute the LHA-vs-market-rent spread per LA (joins S14 `brma_lha_rates` via `la_brma_mapping` to `la_private_rents`) — the actual Layer 3 output
  4. Map layer for the derived spread — deliberately deferred
  5. If `la_geography` seeding was deferred: retry against the ONS Code History Database
  6. Watch for ONS publishing East/West Surrey GSS codes ahead of 1 April 2027 vesting — first live entries for `la_succession` beyond the migration
