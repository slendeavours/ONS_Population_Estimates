# Claude Code Prompt — Source 19: PIP Claimants (DWP Stat-Xplore)

**Project:** Exempt Accommodation Intelligence Pipeline (UCES)
**Execution model:** Python build (S14 precedent), fully automated, no approval gates. Hard stops on failure conditions only.
**Repo:** `slendeavours/ONS_Population_Estimates` (existing — this is an update, not a first publish)

---

## Context

You are building Source 19 for the exempt accommodation intelligence pipeline. The pipeline is a Postgres database (`exempt_pipeline`) covering all 296 English local authorities, keyed on LAD24CD, with historical code reconciliation through `la_code_lookup`.

S19 loads Personal Independence Payment claimant volumes by local authority from the DWP Stat-Xplore REST API. PIP caseload is a direct demand proxy for supported living placements — disability is the core eligibility criterion for the HSS lens. Under the UCWS lens it is context only.

The acquisition pattern is proven: Source 8 (HB SA caseload) already queries the same API at `https://stat-xplore.dwp.gov.uk/webapi/rest/v1/table` using the recodes pattern. S19 differs in one respect: the query structure is discovered programmatically from the `/schema` endpoint rather than exported from the GUI.

**Known unknowns this build must resolve, not assume:**
- Whether the PIP database offers full 296-LA English coverage, partial coverage (possibly ~100 LAs on a Census 2021 geography), or coverage on pre-2023 LA codes. The geography decision tree in Phase 2 handles all three outcomes automatically.
- The exact database, field, valueset, and member IDs. All discovered in Phase 1. Nothing is hardcoded from memory.

**Rules that apply throughout:**
- Parameterised SQL only (`$1`-style or psycopg placeholders). No string concatenation. One logical operation per statement execution.
- Never print, log, or write the API key or database password to any file. The key travels in the request header only.
- Never hardcode URLs beyond the Stat-Xplore API root (proven from S8). All IDs come from schema discovery.
- Throttle API calls: minimum 1 second between schema requests; cache schema responses locally in the working directory (not the repo).
- Uncertainty is documented in table/column comments, not papered over.
- UK English in all prose documentation.

---

## Hard-stop conditions

Stop immediately and report (do not attempt workarounds) if any of the following occur:

1. `STAT_XPLORE_API_KEY` missing from environment, or Postgres unreachable.
2. No PIP database found in the Stat-Xplore schema, or the caseload measure / LA-level geography / daily living award field / date field cannot be located after a full tree walk.
3. Geography codes that resolve neither directly to `la_boundaries` nor through `la_code_lookup`. Never insert new rows into `la_code_lookup` in this build — unknown codes are a stop, not a mapping exercise.
4. Any Phase 5 verification check fails.
5. Secrets scan hit before push.

Everything else — including partial LA coverage — proceeds with documentation.

---

## Phase 0 — Preflight

1. Load the `.env` file in the working directory. Confirm `STAT_XPLORE_API_KEY` exists (check presence only — never echo the value). Confirm Postgres connection variables are present and connect to `exempt_pipeline` as `pipeline_user`. Run `SELECT count(*) FROM la_boundaries;` — expect 296.
2. `gh auth status` — confirm authenticated as slendeavours.
3. Pull latest on the local clone of `ONS_Population_Estimates`. If no local clone exists, `git clone --depth 1`.
4. Locate the UCES project-memory folder inside the repo by finding where existing project-memory markdown files live (`find` for existing memory/source-summary files — do not hardcode a path). Record the path for Phase 6.
5. Confirm `.gitignore` covers `.env` and any local cache/response-dump directories you will create. If not, add before any commit.
6. Check `command -v gitleaks`; note which secrets-scan route Phase 7 will use.

---

## Phase 1 — Schema discovery (fully automated)

1. GET `https://stat-xplore.dwp.gov.uk/webapi/rest/v1/schema` with the `APIKey` header.
2. Walk the schema tree (folders → databases) to locate the PIP database. Search names/labels for "Personal Independence Payment" / "PIP". If multiple PIP databases exist (e.g. cases with entitlement vs clearances vs registrations), select **cases with entitlement** (the live caseload). Record the alternatives considered in the discovery report.
3. Within the selected database, discover and record:
   - The caseload **measure** ID (count of cases with entitlement)
   - The **geography field** and its valuesets. Enumerate every geography valueset and record its member count — this is the evidence base for Phase 2
   - The **daily living award** field and the member ID for **Enhanced**
   - The **date field**, its valueset, and the **latest available month** member ID (determined at runtime from the valueset — never hardcoded)
4. Cache all schema responses locally (outside the repo). Throttle: ≥1 second between calls.
5. Write `s19_schema_discovery_report.md`: every discovered ID, the geography valuesets found with member counts, the month selected, and the decision trail. IDs only — no key material. This file is committed in Phase 7.

Retry logic for all API calls in this build: 3 attempts, exponential backoff (5s / 25s / 125s), treating 503 as retryable (Stat-Xplore maintenance windows).

---

## Phase 2 — Geography resolution (automated decision tree)

1. From the chosen LA-level geography valueset, extract every member code (the GSS code at the end of each URI, per the S8 parsing pattern). Filter to English codes (`E` prefix).
2. Load `la_boundaries.lad24cd` and the `la_code_lookup` mappings from Postgres.
3. Classify every extracted code:
   - **Direct:** present in `la_boundaries` → use as-is
   - **Historical:** absent from `la_boundaries` but resolvable through an existing `la_code_lookup` mapping → map to current LAD24CD. If multiple historical codes map to one current code, sum claimant counts on load and record this in the column comment
   - **Unresolvable:** neither → hard stop (condition 3)
4. Compute coverage: resolved current LAD24CDs as a count and percentage of 296.
5. Set data confidence automatically: **High** if coverage ≥ 95%, **Medium** if 50–94%, **Low** below 50%. Coverage below 296 is not a failure — it is documented. LAs with no data get no row (absence, not zero); this must be stated in the table comment so downstream consumers never read absence as zero.
6. Append the branch taken, coverage figures, and confidence rating to the discovery report.

---

## Phase 3 — Query build and fetch

1. Build table queries using the S8 recodes pattern: explicit map of the resolved English LA member URIs, latest month only, `"total": false` throughout.
   - **Query 1 — total caseload:** geography × latest month, caseload measure
   - **Query 2 — enhanced daily living:** identical, plus the daily living award field recoded to the Enhanced member only
2. Save both query bodies to the working directory as `s19_query_total.json` and `s19_query_enhanced_dl.json` (these commit to the repo, matching the `s8_query.json` precedent — they contain no secrets).
3. POST each to `/webapi/rest/v1/table` with retry logic. ≥1 second between the two calls.
4. Validate response structure before parsing: `fields`, `cubes` present; geography item count matches the query map length. Parse per the S8 pattern (dynamic cube key via `Object.keys` equivalent — in Python, take the first key of `cubes`; extract the GSS code from the tail of each member URI).
5. Capture `annotationMap` and any rounding/suppression annotations from both responses. If DWP applies rounding (PIP figures are commonly rounded) or suppression, record the exact annotation text — it goes into the column comments in Phase 4.
6. Null values load as NULL, not zero. Apply historical-code summing from Phase 2 where applicable (sum only non-null values; if all constituent values are null, result is NULL).
7. Do not commit raw API response dumps. Responses stay in the local cache directory.

---

## Phase 4 — Table create and load

1. Create table (idempotent):

```sql
CREATE TABLE IF NOT EXISTS la_pip_claimants (
    lad24cd text NOT NULL,
    month text NOT NULL,
    pip_total_claimants integer,
    pip_enhanced_daily_living integer,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, month)
);
```

2. Add comments (adjust wording to actual findings):
   - Table comment: source, database ID used, month loaded, coverage achieved (n of 296 and %), confidence rating, and the explicit statement that **absence of a row means no published data, not zero**.
   - Column comments on both measure columns: rounding/suppression annotations captured in Phase 3; note that enhanced daily living is a sharper HSS demand signal than total caseload; note any historical-code summing applied.
3. Upsert both measures in a single batch using the established pattern:

```sql
INSERT INTO la_pip_claimants (lad24cd, month, pip_total_claimants, pip_enhanced_daily_living)
SELECT r.lad24cd, r.month, r.pip_total_claimants, r.pip_enhanced_daily_living
FROM json_to_recordset(%s::json)
    AS r(lad24cd text, month text, pip_total_claimants int, pip_enhanced_daily_living int)
ON CONFLICT (lad24cd, month) DO UPDATE SET
    pip_total_claimants = EXCLUDED.pip_total_claimants,
    pip_enhanced_daily_living = EXCLUDED.pip_enhanced_daily_living,
    loaded_at = NOW();
```

4. Log to `pipeline_run_log`: agent_name `'Source 19 - PIP Claimants'`, source_number `19`, rows_written from the actual insert count, status `'success'`, notes stating the month, coverage, and confidence.

---

## Phase 5 — Verification suite (automated — replaces manual gates)

All six must pass. Any failure is a hard stop with a report of which check failed and the offending rows.

1. **Row count** equals the resolved LA count from Phase 2.
2. **Integrity:** no NULL `lad24cd`; every `lad24cd` exists in `la_boundaries`.
3. **Consistency:** `pip_enhanced_daily_living <= pip_total_claimants` for every row where both are non-null.
4. **Range:** all non-null values ≥ 0; national sum of `pip_total_claimants` is plausible for an English PIP caseload (order of millions, not thousands — if it is wildly off, the measure or geography level is wrong).
5. **Independent spot check:** pick three LAs spanning the size distribution, re-fetch each with a fresh single-LA table query, and confirm the values match what was loaded.
6. **Idempotency:** re-run the Phase 4 upsert with the same batch. Row count unchanged; `loaded_at` updated; second `pipeline_run_log` entry NOT written (log once per build, not per upsert).

---

## Phase 6 — Documentation

Produce the standard markdown set. This is a Python build, so "node" means logical build step (S14 precedent). Standard format per project rules: Type / Purpose / Credential / Query-Code-URL / Logic / Parameters / Behaviour / Connection / Verified Output (with date and actual verified figures).

1. `s19_node1_discover_schema.md`
2. `s19_node2_resolve_geography.md`
3. `s19_node3_fetch_pip_data.md`
4. `s19_node4_create_table.md`
5. `s19_node5_upsert_data.md`
6. `s19_node6_log_run.md`
7. `s19_source_summary.md` — publisher, database ID, cadence, month loaded, coverage and confidence, rounding/suppression caveats, refresh procedure (which single ID changes for a newer month), and the dual-lens note: **HSS Primary** (disability is the core eligibility criterion for supported living placement demand), **UCWS Context**. Never collapse the two lenses.
8. **UCES project-memory markdown** — saved into the memory folder located in Phase 0. Cover: what was built, the geography branch taken and why, coverage achieved, design decisions made automatically (measure selection, confidence rating, null handling, any historical-code summing), verification results, and what a future refresh or remediation (e.g. LGR 2028) needs to know.

Prose in these documents follows the humaniser skill where available. Facts only — no status commentary, no snapshots of intent.

---

## Phase 7 — GitHub publish (explicit named phase — do not skip or merge into Phase 6)

Use the **github-publishing skill**. This is an update to an existing repository, so phase three of that skill applies in full:

1. **Sanitisation pass** on every file staged: no API keys, no connection strings, no raw response dumps. Confirm `.env` and the cache directory are gitignored.
2. **Secrets scan:** gitleaks if available, otherwise the fallback pattern list from the skill's sanitisation manifest.
3. **Drift check:** diff incoming changes against the repo README; fix any claim made stale by this build.
4. **CHANGELOG entry** written before the push, Keep a Changelog format.
5. **Review stamp:** update the last-reviewed date in the README metadata block if present.
6. Commit with Conventional Commits — e.g. `feat(s19): add PIP claimants source build, docs and project memory` — atomic commits if the change set naturally splits (build docs vs project memory).
7. Push.
8. **Verify visible on GitHub:** confirm via `gh api` (or raw URL fetch) that the S19 node docs, source summary, query JSONs, discovery report, and project-memory file are all present on the remote at the pushed commit. List each verified path in the final report. A push without remote verification is an incomplete phase.

Never force push. Never rewrite history.

---

## Phase 8 — Final report

Report back in one block:

- Geography branch taken, coverage achieved (n/296, %), confidence rating
- Month loaded and the database/measure IDs used
- Verification suite results (all six, with figures)
- `pipeline_run_log` entry ID
- Every file pushed, with confirmation each is visible on GitHub
- Any caveats recorded in column comments (rounding, suppression, summed historical codes)
- Anything a human should know before this source is consumed downstream (W1 integration and map visualisation are explicitly out of scope for this build)
