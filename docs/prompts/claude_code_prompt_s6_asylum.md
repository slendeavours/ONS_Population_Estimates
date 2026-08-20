# Claude Code Prompt — S6: Home Office Asylum Support by Local Authority

Paste the whole of this file into Claude Code as a single prompt.

---

## Role and context

You are building source **S6** for the Exempt Accommodation Intelligence Pipeline, an ETL that loads UK government housing and demand data into a PostgreSQL 16 database used for supported housing market intelligence across the 296 English local authorities.

**Environment**
- PostgreSQL 16 in Docker. Database `exempt_pipeline`, user `pipeline_user`.
- Python 3 with `psycopg` (or `psycopg2`), `pandas`, `odfpy`, `openpyxl`.
- Git repo: `slendeavours/ONS_Population_Estimates`, `gh` CLI authenticated.
- Geographic key throughout the pipeline: **LAD24CD** (ONS May 2024 codes).
- `pipeline_run_log.source_number` for this build is **6**. Numbers 1, 2, 3, 3b, 4, 5, 7, 8, 8b, 9a, 9b, 10, 11, 12, 13, 14, 15, 17, 18, 19 are already taken. Do not reuse them.

**Standing pipeline rules that apply to this build**
1. **No hardcoded download URLs.** Every file is discovered from its GOV.UK landing page at run time. GOV.UK asset URLs change with every quarterly release.
2. **Parameterised SQL only.** `%s` / `$1` placeholders, never string concatenation into SQL.
3. **Every LA code resolves through `la_code_lookup`.** Never insert a historical code as a live key. Never invent a successor code.
4. **Idempotent.** Re-running must not duplicate rows. Upsert on the natural key.
5. **`pipeline_run_log` is written once per build**, at the end, not per upsert.
6. **The pipeline stores, it does not score.** No ranking, weighting, or judgement about which LAs are good markets. Load the data correctly and stop.
7. **Verification gates halt the build.** If a check fails, stop and report. Do not load partial or suspect data and flag it afterwards.
8. **Suppressed values are NULL, not zero.** A suppression marker means "not published", which is different from "none".

---

## What you are building

Two related Home Office datasets, both quarterly, both local-authority level.

### S6a — Asy_D11 (primary)

*Asylum seekers in receipt of Home Office support by support type, accommodation type and local authority.*

- Landing page: `https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-data-tables`
- Section: **Asylum → Asylum support**
- Link text to look for: "Asylum seekers in receipt of Home Office support by local authority detailed datasets, year ending [MONTH YEAR]"
- Format: `.xlsx`, roughly 1.2–1.3 MB
- The table code inside the file is `Asy_D11`

Note: at the time this prompt was written the landing page HTML was serving links for **year ending December 2025** while a **year ending March 2026** edition had been published. Discover whatever is actually current. If the two editions disagree, prefer the most recent `year ending` label and record which edition you loaded.

### S6b — Reg_02 (secondary)

*Immigration groups, by Local Authority.* Wider but shallower: asylum support alongside Homes for Ukraine and the Afghan Resettlement Programme, with per-capita percentages.

- Landing page: `https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-regional-and-local-authority-data`
- Latest at time of writing: year ending March 2026, `.ods`, around 266 KB
- Sheets: `Reg_01` (region level, ignore) and `Reg_02` (LA level, load this one)

### Known data caveats to carry into the documentation

- Figures are based on the **registered address** of the person, which is not necessarily where they regularly reside.
- **Unaccompanied asylum-seeking children (UASC) are excluded.** They are supported by local authority children's services, not Home Office asylum support. Do not describe this table as covering all asylum seekers in an area.
- Both datasets cover the **whole UK** (roughly 361 LAs). This pipeline is England only.
- Home Office has revised these tables historically (June 2024 revision to accommodation type, August 2024 revision to geographic distribution, November 2025 revision to accommodation types). Treat each load as a full replace of the periods it covers rather than assuming prior periods are immutable.
- City of London and Isles of Scilly totals in Reg_02 exclude Homes for Ukraine arrivals because of suppression.

---

## Phase 1 — Discovery (GATE)

Do not write any schema or loader yet.

1. Fetch both landing pages. Extract the current download URL for the latest Asy_D11 file and the latest Reg_02 file, plus the previous editions listed (you may want two or three prior quarters for a short time series).
2. Download to a working directory. Do not commit raw source files to the repo.
3. Inspect and report, for each file:
   - Filename, edition label ("year ending X"), file size, sheet names
   - For the data sheet: the header row index, exact column headers, total row count
   - Whether ONS geography codes are present as a column, or only LA names
   - The distinct values of the support type column and the accommodation type column
   - The distinct time periods present in the file (is it a single snapshot or a time series?)
   - Every suppression or "not applicable" marker used, verbatim
   - Whether a national or England total row exists that can be used for reconciliation

**GATE 1.** Stop. Present the discovery report as a table. Wait for approval before continuing.

---

## Phase 2 — Geography resolution (GATE)

This is the highest risk part of the build. The Home Office publishes local authority names, and its LA list may lag behind reorganisations. Do not guess a single mapping.

1. Build a resolution cascade from published LA name to LAD24CD:
   - Method 1: direct match on ONS code, if the file carries one
   - Method 2: exact case-insensitive name match against `la_boundaries.la_name`
   - Method 3: normalised name match (strip punctuation, "City of", "County of", "Borough of", "UA", trailing qualifiers, collapse whitespace)
   - Method 4: match against `la_code_lookup` historical names, then resolve forward to the live LAD24CD
   - Method 5: explicit manual mapping table for anything left, populated only after you have verified the area against MapIt (`https://mapit.mysociety.org`) or the ONS area page
2. Filter to England only. Drop Scotland (`S12`), Wales (`W06`), Northern Ireland (`N09`) rows into a separate rejected list rather than silently discarding them, so the counts reconcile.
3. Write the mapping to a persistent table `asylum_la_name_mapping` with columns: `published_la_name`, `lad24cd`, `match_method`, `verified_source`, `notes`. This is the audit trail.
4. Report: total published names, matched by each method, unmatched list, English LAs in `la_boundaries` with no corresponding published row.

**Watch specifically for:**
- Local government reorganisation names that no longer exist as districts: Cumbria, North Yorkshire, Somerset, Buckinghamshire, Northamptonshire. If the Home Office still publishes an abolished district name, resolve it forward through `la_code_lookup` and record `match_method = 'historical_forward'`.
- Barnsley and Sheffield recodes (E08000038 → E08000016, E08000039 → E08000019), applied as standard elsewhere in this pipeline.
- Any published name that could match more than one English LA. Flag it as ambiguous rather than picking one.

**GATE 2.** Stop. Present the mapping report, including the full unmatched list and any ambiguity. Wait for approval. Do not load data with unresolved names.

---

## Phase 3 — Schema

Propose, then create after approval.

```sql
CREATE TABLE IF NOT EXISTS la_asylum_support (
    period_ending       DATE        NOT NULL,
    lad24cd             TEXT        NOT NULL REFERENCES la_boundaries(lad24cd),
    published_la_name   TEXT        NOT NULL,
    support_type        TEXT        NOT NULL,
    accommodation_type  TEXT        NOT NULL,
    people_supported    INTEGER,          -- NULL where suppressed
    suppressed          BOOLEAN     NOT NULL DEFAULT FALSE,
    source_edition      TEXT        NOT NULL,   -- e.g. 'year ending March 2026'
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, lad24cd, support_type, accommodation_type)
);

CREATE TABLE IF NOT EXISTS la_immigration_groups (
    period_ending       DATE        NOT NULL,
    lad24cd             TEXT        NOT NULL REFERENCES la_boundaries(lad24cd),
    published_la_name   TEXT        NOT NULL,
    pathway             TEXT        NOT NULL,   -- asylum_support | homes_for_ukraine | afghan_resettlement | total
    people              INTEGER,
    per_capita_pct      NUMERIC(8,4),
    suppressed          BOOLEAN     NOT NULL DEFAULT FALSE,
    source_edition      TEXT        NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, lad24cd, pathway)
);
```

Adjust the natural keys if Phase 1 discovery shows the source is shaped differently. Do not invent columns the source does not contain. Add indexes on `(lad24cd)` and `(period_ending)`.

Also create a convenience view, `vw_la_asylum_support_totals`, giving one row per `(period_ending, lad24cd)` with total people supported and a breakdown into dispersal, hotel or contingency, initial accommodation, and subsistence-only, using whatever the actual accommodation type values turn out to be. Use `COALESCE` through `la_code_lookup` in line with the other views in this database.

**GATE 3.** Present the final schema and the view definition. Wait for approval before creating.

---

## Phase 4 — Load

Write `s6_asylum_build.py` in the repo root, structured as: discover, download, parse, resolve geography, validate, upsert, log.

- Parse with `pandas.read_excel` (openpyxl) for xlsx and `pandas.read_excel(..., engine='odf')` for ods.
- Coerce every suppression marker to `None` and set `suppressed = TRUE` on those rows.
- Upsert with `ON CONFLICT (…) DO UPDATE`, so re-running is safe and revisions overwrite cleanly.
- Wrap the whole load in a single transaction. Roll back on any verification failure.
- Write one `pipeline_run_log` row at the end: `source_number = 6`, agent name `s6_asylum_build`, row counts, source editions loaded, status.

---

## Phase 5 — S6 Asylum Dispersal Verification Suite (halting)

Every check must pass. On any failure, roll back and report. Do not proceed to commit.

**Check 1 — Coverage.** Every English LA name published in the source resolves to a LAD24CD. Unmatched count must be zero. Report how many of the 296 English LAs appear in the latest period (fewer than 296 is expected and acceptable; unmatched names are not).

**Check 2 — Referential integrity.** Every `lad24cd` in both tables exists in `la_boundaries`. No Scottish, Welsh or Northern Irish codes present. Zero orphans.

**Check 3 — Anchor set.** For the year ending March 2026 edition, the Home Office reported these as the local authorities with the highest numbers of supported asylum seekers, and a UK total of 97,519 people in receipt of asylum support (Asy_D09):

| Local authority | Supported asylum seekers |
|---|---|
| Glasgow City | 3,870 |
| Birmingham | 2,142 |
| Liverpool | 2,053 |
| Coventry | 1,712 |
| Belfast | 1,607 |

Check Birmingham, Liverpool and Coventry against the loaded English data. They should match exactly, or the discrepancy should be fully explained by the support type or accommodation type filter applied. If you loaded a different edition, source the equivalent anchor figures from that release's "How many people are in the UK asylum system?" narrative page before running this check. Do not skip the check because the edition differs.

**Check 4 — Internal reconciliation.** For each `(period_ending, lad24cd)`, the sum across accommodation types equals the sum across support types, within the tolerance created by suppression. Report any LA where they diverge and suppression does not explain it.

**Check 5 — Reasonableness.** No negative values. No LA above 10,000 supported asylum seekers (well above the observed maximum; a breach means a total row has been loaded as an LA row). Approximately half of all UK LAs should have fewer than 100 supported asylum seekers, which is the published distribution shape; report the actual proportion.

**Check 6 — Suppression handling.** Count rows where `suppressed = TRUE`. Confirm `people_supported IS NULL` on every one of them, and that no suppression marker has been coerced to 0.

**Check 7 — Idempotency.** Run the loader a second time against the same files. Row counts, and a checksum over `(period_ending, lad24cd, support_type, accommodation_type, people_supported)`, must be identical. `loaded_at` may change.

**GATE 4.** Present the full verification output as a pass/fail table. Wait for approval before Phase 6.

---

## Phase 6 — Documentation

Produce, in the repo:

1. `docs/nodes/s6_node1_discover_asylum_sources.md` through `s6_node[N]_…md`, one file per logical step, following the existing node documentation format used across this pipeline: Type, Purpose, Credential, Query/Code/URL in full, Logic step by step, Query Parameters table, Behaviour (conflict handling, re-run safety), Connection (input/output), Verified Output with date.
2. `docs/s6_asylum_source.md` — the source summary: publisher, series, publication pages, table codes, native geography, date range loaded, refresh cadence, table names, natural keys, row counts, suppression conventions, the full caveat list from this prompt, and a refresh procedure.
3. `docs/S6_BUILD_SUMMARY.md` — matching the shape of `docs/S9_BUILD_SUMMARY.md`.
4. Add S6 to `docs/METHODOLOGY.md` in the source register table, in source-number order.
5. A project memory markdown file for upload to the UCES project knowledge.

Prose in UK English. No status commentary in permanent documents.

---

## Phase 7 — GitHub push (named phase, do not skip)

This is an update to an existing public repository, so the update gates apply.

1. **Drift check.** Diff the incoming changes against `README.md` and `docs/METHODOLOGY.md`. Fix or flag any claim that S6 makes stale.
2. **Sanitisation and secrets scan.** Run `gitleaks` if installed, otherwise the fallback pattern list. Confirm no database credentials, no API keys, and no commercial rate card data are in anything being committed. Nothing from the S20 commercial rate card work goes near this repo.
3. **CHANGELOG entry**, written now, not reconstructed later.
4. **Review stamp.** Update the last-reviewed date in the README metadata block.
5. Commit with Conventional Commits, atomic. Suggested: `feat(s6): add Home Office asylum support by local authority` and `docs(s6): add source, node and methodology documentation`.
6. Push, then **verify remotely via the GitHub API** that every intended file is present at the expected path. Report the commit SHA and the verified file list.

**GATE 5.** Confirm the push before executing it.

---

## Explicitly out of scope

Do not do these in this build. They are separate decisions.

- **Workflow 1 integration.** Do not add columns to `staging_la_signals` and do not add a tenant type. S6 lands as standalone tables, the same pattern as S19 PIP.
- **Node 9 GeoJSON export and any map layer.** Nothing is added to `index.html`.
- **Any composite index, score, or ranking combining S6 with other sources.**

If you think one of these is necessary to make the build work, stop and say so rather than doing it.
