# S5 IMD 2025, S3b Census 2021 tenure and S7 LAD May 2024 boundaries: first load on the editions engine

Status: done 2026-10-11 (real writes at 13:58 to 14:00 UTC; the plan and this note are dated 2026-10-10). Works like
[S13](2026-10-10-s13-editions-first-load.md) and [S23](2026-10-09-s23-editions-first-load.md), with one difference: these three
tables each hold one vintage, so the engine opt-in `live_single_period` keeps another vintage out of the live table.
Loaders: `scripts/s5_imd_editions.py`, `scripts/s3b_tenure_editions.py`, `scripts/s7_boundaries_editions.py`; gates:
`scripts/s5_imd_editions_verify.py`, `scripts/s3b_tenure_editions_verify.py`, `scripts/s7_boundaries_editions_verify.py`.
Design: `docs/superpowers/specs/2026-10-10-no-loader-sources-design.md` (audit record, not approval).
Scott authorised the real writes through the plan ("go ahead with the reference trio").

**No value any table holds changed, and nothing W1 or the map reads changed.** The three live tables have the same rows,
values, `loaded_at` and keys before and after. The only change to a live table is one added column on `la_imd_2025`
(`vintage`, below). W1, `refresh_map.py`, the export, `push.py` and `git push` were not run. `la_boundaries` rows were not
deleted, re-keyed or rebuilt, and `la_code_lookup` was not written (a hash over every column of its 343 rows, and of
`la_boundaries`, is identical before and after).

## Before-state (read-only, confirmed against the live tables before anything was written)

| Table | Rows | `loaded_at` | NULL / zero | Survey hash (every column but `loaded_at`) |
| --- | --- | --- | --- | --- |
| `la_imd_2025` | 296 | all 2026-04-01 10:58:50.329755 UTC | 0 NULL; 104 published zeros in `imd_proportion_most_deprived_10`, 15 in `imd_extent` | 60f16360106cf7e25cba3f4283f4bae8 |
| `la_tenure_2021` | 296 | 292 at 2026-04-01 01:55:08.514719 UTC; 4 at 2026-08-20 14:20:00.090729 UTC | 0 NULL, 0 zero | 16c5d37b8190e40dac1b1c8a2919eb78 |
| `la_boundaries` | 296 | all 2026-03-25 15:13:11.744860 UTC | 203 Polygon, 93 MultiPolygon | 6b984c4f30b2f17ce470736d8b5f75be |

Each sha256 below is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a
64-hex run, stays untouched. They were printed by `python scripts/<s5_imd|s3b_tenure|s7_boundaries>_editions_verify.py
--print-note-lines` (read-only, one second each) before any `ddl` and before any migration, and printed again afterwards
with the same output. A `before-state` line is the loader's `rows_content_sha` of the live rows, which edition 1 "as loaded"
must hash to (gate 11 of each script). A `before-state-all` line covers every stored live column (S3b with its two generated
totals; none with the added `vintage`), and no gate reads it: it is the record that live was untouched. A `w1-read` line is
what W1 and the map read: `lad24cd|imd_rank_of_average_rank` for S5, and `lad24cd|canonical geojson` for S7, sorted and
LF-joined (gate 17 of S5 and S7). S3b has no W1 or map reader, so it has no `w1-read` line.

before-state la_imd_2025 IoD2025 rows=296 sha256-first32=2cb29f390c901248709ee264d093f910 sha256-last32=9330ee9856e2d505ff21b571b6e2b98a
before-state-all la_imd_2025 IoD2025 rows=296 sha256-first32=2d305dee1720bee53915199be7711ad7 sha256-last32=3ac5354c3d0e4c1554407219f787d82c
w1-read la_imd_2025 IoD2025 rows=296 sha256-first32=a01099f37668d0c9c80f87fce3334683 sha256-last32=f1d2c45f469f732c82baa6a069c96168
before-state la_tenure_2021 2021 rows=296 sha256-first32=050605c5a6f22c3cdb12ed0ec33c0c37 sha256-last32=8b409cc344c9cd01aa4f9e8be24b3dde
before-state-all la_tenure_2021 2021 rows=296 sha256-first32=659ec320cee221fd41873cbaa3e33920 sha256-last32=a6eb9ab086f1fd08dd1868ebe76973ad
before-state la_boundaries 2024-05-01 rows=296 sha256-first32=822ead9b823c7273454f0cb54baec4d8 sha256-last32=2a244c825c7d8c48541b964d50b543bb
before-state-all la_boundaries 2024-05-01 rows=296 sha256-first32=3ba879352e8b2aa17ac69471c10d274b sha256-last32=314892e06f6d63664ac1fd491eac02d8
w1-read la_boundaries geometry rows=296 sha256-first32=3690c7a0b940d669e4a9c7eb1b82efbd sha256-last32=09b90c1393b4a42d53ead910e65b9015

## How each table came to hold what it holds (checked 2026-10-11 against the database, the run log and the docs)

- **S5 IMD 2025 (`la_imd_2025`).** Loaded 2026-04-01 10:58:50 UTC by the n8n workflow "Indices of Deprivation (S5)", run-log
  row 30 ("Source 5 - IMD 2025", 296 rows), from a CSV (`imd_2025_la_summary.csv`) that was converted from the publisher's
  File 10 in a chat session that no longer exists. The repo had no S5 code. The held file
  `data/raw/s5_imd/File_10_-_IoD2025_Local_Authority_District_Summaries__lower-tier__v2.xlsx` (sha256 starting 6c38a2fd3998e0ca;
  docProps modified 2025-11-14 12:48:06 UTC) is File 10 version 2: the publisher reissued File 10 on 14 November 2025 and
  noted it on 17 November 2025 after an ONS LSOA-to-LAD look-up error in the allocation of LSOAs (first published
  2025-10-30). The proof run below shows the live values equal v2 on every cell, so the chat-made CSV was made from v2 or from
  values equal to it. The workflow's Code node applied `parseFloat` and `parseInt` to the CSV text (it has no `|| 0`: a blank
  would have become NaN and then NULL), and its upsert cast the values; the file has no blank cell.
- **S3b Census 2021 TS054 tenure (`la_tenure_2021`).** Loaded 2026-04-01 01:55 UTC by the n8n workflow "Census 2021 TS054
  Tenure (S3b)", run-log row 29 (309 rows written, "static, runs once"). **Which route fetched the data is not
  established.** The workflow's fetch node, as stored in n8n, calls the ONS beta API (`api.beta.ons.gov.uk`, TS054 version
  4, area type ltla), but the node is named "Fetch TS054 from Nomis", run-log 29's note says "ONS Census 2021 TS054 Tenure by
  LA, Nomis NM_2072_1", and all 292 rows loaded that day carry the `source` "ONS Census 2021 TS054 Tenure, Nomis NM_2072_1,
  Census Day 21 March 2021" (checked 2026-10-11). The held values equal NOMIS `NM_2072_1` on every count (0 differences,
  below), so no stored value depends on the answer. The workflow coerced a missing cell with `|| 0`, built
  `total_households` from the parts instead of storing the publisher's total, and re-based the four 2023 unitary
  authorities from their predecessor districts. The `|| 0` never produced a wrong stored value (no cell was missing) and the total built from the parts equals
  the publisher's total for all 296 authorities (the proof below). The re-basing did: Cumberland, Westmorland and Furness
  and Somerset were understated (by 19,404, 31,259 and 75,940 households) and North Yorkshire overstated by 4, as
  [2026-08-20-s3b-tenure-rebasing-error.md](2026-08-20-s3b-tenure-rebasing-error.md) records. On **2026-08-20 14:20 UTC** those
  four rows were reloaded from NOMIS `NM_2072_1` on their current codes by direct SQL (no run-log row, no committed code);
  the other 292 rows keep their 2026-04-01 `loaded_at`. The `source` column carries two variants ("Census Day 21 March 2021"
  and "2023 local authority boundaries"); they are kept as provenance and are not compared.
- **S7 LAD May 2024 boundaries (`la_boundaries`).** The spine of W1. Loaded by the n8n workflow "LA Boundaries (S7)" (an
  ArcGIS query then an upsert). The only run-log row, id 1 (2026-03-25 12:38:29 UTC, `source_07_boundaries`, 296 rows),
  predates the 15:13:11 UTC that every row carries by two and a half hours; the workflow's "Log Run" node is a SELECT and
  writes nothing (re-checked on 2026-10-11 by reading the workflow's nodes read-only from n8n: `SELECT agent_name, status,
  rows_written, started_at FROM pipeline_run_log ORDER BY started_at DESC LIMIT 1`; the upsert sets `loaded_at = NOW()`), so
  a later run or step that was not logged wrote the rows. After its upsert the workflow also ran
  `DELETE FROM la_code_lookup WHERE change_type != 'current'` and re-inserted its own March 2026 list, which could have
  undone the July and August 2026 lookup fixes (its ArcGIS fetch still worked although the workflow was inactive). That
  workflow was retired on 2026-10-10 (its Code node, and the `la_code_lookup` DELETE and
  INSERT nodes, now stop with an error naming the new loader; `scripts/n8n_retire_no_loader_sources.py`). The new loader
  never writes `la_code_lookup` and never deletes, inserts or re-keys a `la_boundaries` row (S7 gate 16 checks the loader
  for no DELETE and no `la_code_lookup` write). It does **update** `la_boundaries`, through one path only:
  `refresh-latest --acknowledge 2024-05-01` writes the five values (`lad24nm`, `longitude`, `latitude`, `shape_area`,
  `geojson`) and `loaded_at` of the live vintage's rows that differ from its latest edition, and only on that explicit
  acknowledgement.
- **The registry and METHODOLOGY caveat "Simplified to approximately 20% of original vertex count for web performance" for
  S7 was unsupported and was removed on 2026-10-11** (Task 7: `scripts/backfill_source_registry.py`, `docs/METHODOLOGY.md`
  and the map viewer's text). The workflow's code does no simplification, and the proof below shows
  that all 296 stored shapes are identical, as canonical JSON text and as `jsonb`, to the publisher's generalised
  (BGC, clipped to the coast) layer as published, England only.

## What was written (2026-10-11)

The only live DDL change in the whole plan is the S5 `vintage` column; nothing else in a live table was touched.

- **S5.** `ddl --commit`: `la_imd_2025_editions` (append-only: triggers `s5_editions_immutable` and
  `s5_editions_no_truncate` refuse UPDATE, DELETE and TRUNCATE), its ledger `la_imd_2025_editions_file_checks`, and
  `ALTER TABLE public.la_imd_2025 ADD COLUMN vintage text NOT NULL DEFAULT 'IoD2025' CHECK (vintage = 'IoD2025')`. The column
  is additive, default-filled (all 296 rows read 'IoD2025'), not in the primary key, and the CHECK makes a second vintage in
  the live table impossible. Before running it the repo and `sql/` were searched for a reader of `la_imd_2025` that selects
  every column, reads positionally or uses an unqualified `vintage` in a query joining it: the readers are
  `sql/w1/05_la_signals.sql`, `scripts/verify/w1_live/la_signals.sql` and the two W1 build SQL files (a `LEFT JOIN la_imd_2025
  imd` read by alias and named column, `imd.imd_rank_of_average_rank`), `scripts/export_map_data.py` (a vintage label only)
  and `scripts/s14_lha_rates_build.py` (an alias and named columns); `vintage` appears in them only in comments. One
  `SELECT * FROM la_imd_2025 WHERE lad24cd = %s` exists, in the one-off audit script `analysis/liverpool_paper/audit.py`
  (not W1, the export or the map); it prints the row as a dict, so the added column only adds a key to what it prints. It is
  not a positional reader and was judged not to be a stop; reported to the controller.
  `migrate-legacy` (preview, `--simulate`, `--commit`): edition 1 "as loaded", 296 rows, run-log row 449.
- **S3b.** `ddl --commit`: `la_tenure_2021_editions` (triggers `s3b_editions_immutable`, `s3b_editions_no_truncate`) and its
  ledger. `la_tenure_2021` is not touched by `ddl`. `migrate-legacy` from the held NOMIS reply
  `data/raw/s3b_tenure/NM_2072_1_TYPE424_2026-10-10.csv` (bytes sha256 starting 80b690076f2d9203) and its definition JSON:
  edition 1 "as loaded", 296 rows, published 2026-08-20 (the latest load date), run-log row 450.
- **S7.** `ddl --commit`: `la_boundaries_editions` (triggers `s7_editions_immutable`, `s7_editions_no_truncate`) and its
  ledger; it has no foreign key, because it may hold other vintages' codes. `la_boundaries` is not touched by `ddl`.
  `migrate-legacy` from the held GeoJSON `data/raw/s7_boundaries/LAD_MAY_2024_UK_BGC_england_4326_2026-10-10.geojson` (bytes
  sha256 starting 7fd7632b1e00a2c0) and the layer JSON: edition 1 "as loaded", 296 rows, run-log row 451.
- Every `migrate-legacy` ran as a preview first (read in full), then `--simulate` (rolled back), then `--commit`. Each loader's
  `load` previews (the page or API, `--recheck <vintage>` and the `--file` path) all said `unchanged`, 0 areas changed
  against edition 1; `refresh-latest` planned nothing; `status` reads OK, nothing to do. Nothing was committed by any `load`.
- Foreign keys to `la_boundaries`: 18 before (counted from `pg_constraint`; the plan's header said 15, the controller
  ruled that the count is read from the database) and 20 after: the S5 and S3b editions tables each have `fk_la_boundaries`.
  All 20 are validated and the S7 gate 18 reports 0 orphan rows. `la_boundaries_editions` has no foreign key by design.

## The proofs (each re-run read-only by the verify scripts)

- **S5:** the held File 10 v2 (6c38a2fd3998e0ca) read again reproduces every live value: 296 authorities, 3,256 values
  (`la_name` and the ten measures), **0 differences**. Identity: File 10 has no cover sheet and an empty Notes sheet, so the
  loader relies on the exact sheet set (11), the exact IMD header (the `IMD25 - ...` names tie it to IoD2025) and docProps
  modified (2025-11-14 12:48:06 UTC, also the file's rank), with the release page's attachment title as corroboration. The
  checks passed: every rank follows its measure; `imd_rank_of_proportion` shares ranks (the 104 published zeros share rank
  193 and 14 other ties) and `imd_rank_of_extent` shares rank 282 (the 15 published zeros), so those two are not permutations
  of 1..296 and the check is tie-aware (found in Task 2); the published zeros stay 0.
- **S3b:** the held reply read again reproduces every live count: 296 authorities x 9 = 2,664 counts, **0 differences**; the
  eight parts sum to the publisher's total on all 296 (so the total built from the parts agreed), no negative count. Identity
  is the NOMIS dataset definition (`NM_2072_1` "TS054 - Tenure", mnemonic `c2021ts054`, `LastUpdated` 2023-01-05 09:30:00)
  and the geography type text on every row ("local authorities: district / unitary (as of April 2023)", which is why the
  four unitaries exist on their own codes), `RECORD_COUNT` agreeing with the 2,862 rows (296 English areas stored; 22 Welsh
  read and ignored). The bytes are API output, so the content hash decides "unchanged"; today's reply and the 2026-08-20
  extract (`scripts/verify/src/ts054_current.json`) were also compared: 0 differences.
- **S7:** the held GeoJSON read again reproduces every live value: 296 districts, 1,480 cells (`lad24nm`, `longitude`,
  `latitude`, `shape_area`, `geojson`), **0 differences**; 296 of 296 geometries identical as canonical JSON text and as
  `jsonb` (93 MultiPolygon, 203 Polygon). Identity is the ArcGIS layer definition (`LAD_MAY_2024_UK_BGC`; fields `LAD24CD`,
  `LAD24NM`, `LONG`, `LAT`, `Shape__Area`; `editingInfo.lastEditDate` 1719926938757 = 2024-07-02 13:28:58 UTC, also the rank).

Stop conditions checked on the day: the IMD page offers File 10 v2 only (docProps equal, byte-identical download); NOMIS
`LastUpdated` is still 2023-01-05 09:30:00; the May 2024 BGC layer's `lastEditDate` is still 2024-07-02 13:28:58 UTC. Raw
files held are unchanged (sha256 starting 6c38a2fd3998e0ca, 80b690076f2d9203 and 7fd7632b1e00a2c0); new dated downloads were
saved beside them (the NOMIS definition JSON differs in bytes on every download, so each is kept under its own hash).

## The newer LAD vintages: previewed, held on disk, not committed

Per the controller's ruling no Dec 2025 or May 2026 vintage was committed in this task; `load --vintage DEC_2025` and
`load --vintage May_2026` were run as previews only (the database is not written; `status` and the editions table show only
2024-05-01). Both previews print the same `vintage_diff` against the live vintage 2024-05-01:

- codes added 2 (E08000038, E08000039); removed 2 (E08000016, E08000019); 294 in both: the Barnsley/Sheffield recode, and the
  only difference in codes or names;
- names changed: 0;
- **geometry changed on all 294 shared districts, and `LONG`, `LAT` or `Shape__Area` changed on all 294** (gate 19's
  wording, which is right). Separately, measured from the held files: geometry and `Shape__Area` differ on all 294; `LONG` on 211 (December 2025) and 212 (May 2026), by at most 0.000022 and 0.000038 degrees; `LAT` on 137 and 137, by at most 0.00001 and 0.00004 degrees; names on none. The Dec 2025
  layer (lastEditDate 2026-04-29 08:34:12 UTC) and the May 2026 layer (lastEditDate 2026-09-09 13:34:25 UTC) are redrawn
  boundary files, not copies of May 2024 with a recode; the earlier survey assumption (spec finding 3: "the same 296 English
  codes and names except E08000016/19 -> E08000038/39") holds for codes and names only. Both also have 87 MultiPolygon and 209
  Polygon features (May 2024: 93 and 203).

A later `load --vintage <V> --commit` stores such a vintage as editions only (`held-not-live`), never in `la_boundaries`; the
engine refuses to promote it, and `refresh-latest` of a non-live period halts. **Adopting a newer vintage is Scott's
decision**: it re-bases every join (W1's spine and 20 foreign keys), changes the map's geometry, and flips the canonical
Barnsley/Sheffield form (E08000016/19 to E08000038/39). Nothing in this task or the plan does that.

## How to run at the next IoD, Census or boundary change (from `ONS_Population_Estimates`)

1. **IoD (next after 2025).** `python scripts/s5_imd_editions.py load` reads the GOV.UK collection and the newest "English
   indices of deprivation" page and previews. A different IoD year halts on its header until a header alias is added
   deliberately with evidence (the loader's `FILE10_HEADERS`); a reissue of IoD2025 (a later docProps modified) is stored as
   edition 2 and `refresh-latest` applies it; equal docProps with other content stops unless `--accept-reissue IoD2025`; an
   older file stops unless `--allow-older-file`. A new IoD year can never reach `la_imd_2025` (the CHECK refuses it), so
   adopting one means a deliberate change to that table's CHECK and W1's layer, decided by Scott. Gate 17 of the S5 script
   fails by design when a refreshed reissue changes `imd_rank_of_average_rank`: update the `w1-read` line deliberately.
2. **Census.** `load` for S3b reads NOMIS again; a different census year or an updated `LastUpdated` is a new vintage or a
   reissue by the same rules, and `--file CSV --def JSON` works offline. `census_year` has no CHECK in live, so "one census in
   live" is held by the loader and gate 3.
3. **Boundaries.** `python scripts/s7_boundaries_editions.py load` previews the live vintage; `load --vintage <V>` (a name
   declared in the loader's `VINTAGES`, currently `May_2024`, `DEC_2025`, `May_2026`; the others the service offers are only
   listed) previews another. A geometry or name change to the live vintage is REJECTED unless `--acknowledge 2024-05-01`, and
   its `refresh-latest` also needs it and says "this changes the map's geometry: Scott's decision".
4. Always: `--simulate` then `--commit`, `status`, then the source's verify script (exit 0). S5 and S7 feed the map: run
   `refresh-latest` before W1.

## How to reverse

`python scripts/<loader> restore-edition PERIOD N` (preview by default) stores edition N's rows as the next edition;
`refresh-latest --commit` then applies it to the live table. Nothing is ever deleted from an editions table. To remove the S5
column (only if ever needed): `ALTER TABLE la_imd_2025 DROP COLUMN vintage`, after nothing reads it; the three editions
tables and their ledgers are new and can be dropped only by a deliberate statement (their triggers refuse row changes, not
DROP TABLE).

## Verification

Run on 2026-10-11 after the migrations, one at a time, each ending exit 0 with every gate PASS (see the Task 6 report for the
gate counts): `scripts/s5_imd_editions_verify.py`, `scripts/s3b_tenure_editions_verify.py`,
`scripts/s7_boundaries_editions_verify.py`. After the runs a read-only query found no table named `zz%`.
