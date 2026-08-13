# Node 10 — Verification Suite and Log Run

## Type

Postgres — Execute Query. `scripts/s22_verify.py`.

## Purpose

Re-state every hard gate against the committed database, run the four soft checks, write the verification report, and log the run.

## Credential

Postgres `exempt_pipeline`.

## Query / Code / URL (full content)

Soft check 7 — spot-check, one execution per authority:

```sql
SELECT lad24cd, la_name, empty_6_months_plus, total_dwellings,
       empty_homes_premium_count
  FROM la_council_taxbase_empties
 WHERE taxbase_year = %s AND la_name = %s;
```

Soft check 8 — authorities charging no premium:

```sql
SELECT la_name, empty_homes_premium_count
  FROM la_council_taxbase_empties
 WHERE taxbase_year = %s
   AND (empty_homes_premium_count IS NULL
        OR empty_homes_premium_count = 0)
 ORDER BY la_name;
```

Soft check 9 — Table 615 geography:

```sql
SELECT mapping_status, COUNT(*), COUNT(DISTINCT published_la_code),
       MIN(year), MAX(year)
  FROM la_vacant_dwellings_615 GROUP BY 1 ORDER BY 2 DESC;
```

Soft check 10 — exemption class breakdown:

```sql
SELECT COUNT(*), COUNT(DISTINCT lad24cd) FROM la_ctb_exemption_classes;
```

Run log:

```sql
INSERT INTO pipeline_run_log
    (run_id, agent_name, source_number, status, rows_written,
     error_message, started_at, completed_at, duration_ms, notes)
VALUES (gen_random_uuid(), %s, %s, %s, %s, NULL, %s, now(), NULL, %s)
RETURNING id, run_id;
```

## Query Parameters

| Parameter | Value |
|---|---|
| `agent_name` | `Source 22 - MHCLG Council Taxbase Empty Homes` |
| `source_number` | `22` |
| `status` | `complete` when all six hard gates pass, `failed` otherwise |
| `rows_written` | sum of rows across the four S22 tables |
| `started_at` | build start, carried through `build_reports/s22_build_state.json` |
| `notes` | release date, revision date, both resolved URLs, per-table row counts, W1 run id, and both structural breaks |

## Logic (step by step)

1. Re-run hard gates 1, 2, 3 and 5 against the committed data, and read gate 4's result from the build state file. Gate 6 is re-checked against the recorded W1 run.
2. **Soft check 7** — compare five authorities against the Empty Homes Network's November 2025 report on the 2025 Council Taxbase. That report is a derived secondary source with known transcription defects, and MHCLG revised the release in January 2026. Where they differ MHCLG is correct; the difference is reported and no loaded value is adjusted.
3. **Soft check 8** — count authorities with a zero or null empty homes premium count. The release states 291 of 296 applied a premium, so five is expected.
4. **Soft check 9** — Table 615 rows by `mapping_status`, with the earliest and latest year loaded.
5. **Soft check 10** — report whether `la_ctb_exemption_classes` was built or NOT FOUND.
6. Write `build_reports/s22_verification.md` and insert the run log row.

## Behaviour

Read-only apart from the single run log insert. Re-running produces a fresh report and an additional log row; it does not modify any loaded data. Exits non-zero if a hard gate fails on re-verification, so a later regression is caught rather than reported quietly.

## Connection

- Input: Node 9 (Wire W1 and Re-run)
- Output: publish (GeoJSON export, index.html layer registration, GitHub push)

## Verified Output

2026-08-13. Six hard gates, all pass. Four soft checks, all reported.

| Check | Result |
|---|---|
| 7 — Empty Homes Network spot-check | 5 of 5 authorities match MHCLG exactly on all three measures |
| 8 — no empty homes premium | 5 of 296: Amber Valley, Bolsover, Castle Point, Gravesham, Ribble Valley. Matches the release's 291 of 296 |
| 9 — Table 615 mapping | 7,170 rows, 2004 to 2025: 6,277 `direct`, 891 `unmapped`, 2 `resolved_via_lookup` |
| 10 — exemption class breakdown | **BUILT** — 3,256 rows across 296 authorities, from Table 2.01 on the Supplementary Data sheet |

Logged to `pipeline_run_log` with `source_number` 22, status `complete`, 10,724 rows written.
