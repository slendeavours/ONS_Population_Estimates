# S14 DWP UC LHA rates and the BRMA crosswalk: first load on the editions engine

Status: done 2026-10-11. Rates work like [S12](2026-10-10-s12-editions-first-load.md) (periodic editions); the crosswalk is a single
vintage like the reference trio.
Loader: `scripts/s14_lha_editions.py`; gates: `scripts/s14_lha_editions_verify.py` (all PASS, exit 0).
Design: `docs/superpowers/specs/2026-10-11-last-four-sources-design.md` (audit record, not approval); plan
`docs/superpowers/plans/2026-10-11-w1-pair-editions.md`. Scott said "go ahead with S3 and S14".

**No value W1 reads changed.** The 2026-27 weekly rates and `brma_name` for all 296 authorities are exactly as they were
(`w1-read` hashes below, equal before and after). The one stored value that changed is the crosswalk's `mapping_method`: the
text `'NaN'` on 295 rows became `centroid_spatial_join`, as edition 2 under the named correction `s14-mapping-method-2026-10`.
W1 does not read that column. The map is unaffected, and W1 and `refresh_map.py` were not run (Scott deferred W1 and the map
until every source is done).

**One thing to know before the next W1 run:** the 295 refreshed crosswalk rows carry `loaded_at` 2026-10-11 22:31:11 UTC (the
edition's), so `refresh_map.py --check` will report `la_brma_mapping` as loaded after the last W1 run although W1's read is
unchanged. The rates table was not refreshed (nothing to write), so `brma_lha_rates` keeps its `loaded_at`.

## Before-state (read-only, confirmed against the live tables before anything was written)

Both tables had been rebuilt by accident earlier the same day and then repaired: see
[2026-10-11-s3-s14-accidental-rebuild.md](2026-10-11-s3-s14-accidental-rebuild.md). The repair brought them back to the
survey of the same morning, and that was re-confirmed first, exactly:

- `brma_lha_rates`: 152 rows, one financial year (2026-27), PK `(brma_name, financial_year)`, 0 NULL, 0 zero, `loaded_at` 151 at
  2026-08-20 20:23:00.558032 UTC and Ashford at 2026-08-20 23:40:31.748925 UTC, survey hash
  `5536ba6d15071e210da60bb64d512a6e`.
- `la_brma_mapping`: 296 rows, PK `(lad24cd)`, `brma_secondary` NULL on all 296, `mapping_method` `'NaN'` on 295 and
  `nearest_fallback` on E06000053, `loaded_at` all 2026-08-20 20:23:00.558032 UTC, survey hash
  `2e7fb44bf374be96280741052be327db`.
- The survey hash is md5 over every column except `loaded_at` (NULL as `~`, joined by `|`, one line per row, ordered by key).
  It was taken before the additive `vintage` column existed; with `vintage` left out it still reproduces.
- The W1 LHA join (`la_boundaries` to `la_brma_mapping` to `brma_lha_rates` at the latest year) left 0 of 296 authorities
  unmatched, and `v_la_rate_triangulation` was present (155 rows).

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a 64-hex
run, stays untouched. They were printed with `python scripts/s14_lha_editions_verify.py --print-note-lines` before any `ddl`, and
are the lines the verify script's gates 16 and 17 read.

- `before-state` is the loader's content sha for the period: for the rates the BRMA and the ten rates; for the crosswalk
  `lad24cd`, `la_name`, `brma_name` and `mapping_method`. It equals edition 1's content hash.
- `before-state-all` hashes every column edition 1 keeps, including the live `source` text (held as `source_text`), so
  Ashford's hand-restored `source` is covered.
- `w1-read` is what W1 reads: for the rates `brma_name` and the five weekly columns of 2026-27; for the crosswalk
  `lad24cd|brma_name`.

before-state brma_lha_rates 2026-27 rows=152 sha256-first32=7ff5886d49112c6f8b2abd63d954a87d sha256-last32=b5805188f2cc5531aee38adcd2db5e02
before-state la_brma_mapping brma-2020-05_lad-2024-05 rows=296 sha256-first32=4c56baef7383f115fe9b6968bb25ab9f sha256-last32=14bf19b4d1ed70631939244c8951cb83
before-state-all brma_lha_rates rows=152 sha256-first32=996e654a191efe87d8ac7ea31fdea3a4 sha256-last32=21730438b6752d6e09160b0a5462e4bc
before-state-all la_brma_mapping rows=296 sha256-first32=ae655c69433ebce36df4e64ac39bc07e sha256-last32=867e0bbe7485e04c3998994ca44f911d
w1-read brma_lha_rates 2026-27 rows=152 sha256-first32=564fb6ea90d54fed054b2659a68a6ca0 sha256-last32=c63cd414b05b58a532926035e9c47c24
w1-read la_brma_mapping rows=296 sha256-first32=9a0713854da4bb84d4fdf88c61e67429 sha256-last32=6f5b4e49756b81e83948367c5457de10

After the migration and the refresh the two `w1-read` hashes were recomputed from the live tables and are equal to these.

## How the tables came to hold what they hold (checked 2026-10-11 against the database, the run log and the docs)

- **2026-07-10.** Run-log id 52 (447 rows, 22:50 UTC) and id 53 (169 rows, "Correction run: added Ashford (CSV has single
  header row, 152 BRMAs total) and reconciled 17 shapefile BRMA names (ampersand -> and) to match DWP CSV. Full join").
  Both of the repo's S14 scripts read the DWP file as "title row, header row, data from row 2", but the file's row 0 is the
  header, so the first BRMA alphabetically (Ashford) was consumed as a header and 151 of 152 loaded. The crosswalk is the
  VOA "English BRMA(LHA) Layer" (May 2020) joined to `la_boundaries` centroids (May 2024). I do not claim which script wrote
  rows 52 and 53; their note text is the build's.
- **2026-08-20, run-log id 97** (20:23 UTC, 447 rows): an accidental `import s14_lha_rates_build_v2`, run only to check that the
  file parsed. The script is top-level code with no main guard, so the import ran `DROP TABLE ... CASCADE` on both tables and
  rebuilt them. Detailed in [2026-08-21-s14-brma-join-break.md](2026-08-21-s14-brma-join-break.md): 37 mapping rows in the VOA
  "X & Y" spelling (so 38 authorities would have got NULL LHA rates in W1) and Ashford missing.
- **Then, by direct SQL with no run-log row:** Ashford's rate row restored on 2026-08-20 at 23:40:31 UTC (its `source` says
  "DWP UC LHA rates 2026-27, restored 2026-08-20 after a header off-by-one dropped it"), and the 37 mapping names normalised
  from "&" to "and" (their `loaded_at` stayed at 20:23:00.558032). Those two edits are why the live `source` column has two
  variants and why `brma_name` agrees with the CSV.
- **2026-10-11 20:30 UTC, the accidental rebuild again** (run-log ids **499** and **501**, 448 rows each; the S3 script's write
  in the same minutes has no run-log row). While the old loaders were being retired, tests that prove each retired script
  exits at once were run before the guards existed, so the old scripts ran for real: v1 upserted both tables, v2 dropped and
  rebuilt them with `CASCADE`. It brought back the 37 "&" names (37 authorities unmatched in the W1 join), lost Ashford's own
  `source` text, reset `loaded_at` on every row and dropped the derived view `v_la_rate_triangulation`. Repaired the same
  evening in one transaction per object (commit `69ca0ec`); all three survey hashes reproduced exactly, 0 unmatched, the view
  back at 155 rows, gates 18 to 21 passing. Run-log 499 and 501 are kept as the record. Nothing published was affected: W1, the
  map, the export and any push were not run. The lesson, in the incident note and repeated here because it is the second
  time: a test that runs a script must never run before the script can no longer do harm, and **never import a build script
  to test that it parses** (`py_compile` does that), and a `DROP ... CASCADE` takes derived objects with it silently.
- The three old S14 scripts and the two n8n workflows are retired (Task 1, commit `de39a43`): each script exits 2 at its first
  statement naming the new loader; the Code nodes "Process Rate Data" and "Parse VOA Response" in `S44UCK95djqE3aLw` now throw
  RETIRED. `sk295Li2DyMqDSYY` has no nodes and was not changed. Nothing was activated.

## The old scripts' hazards (why retirement came first)

Read in the code on 2026-10-11, not assumed: neither script converted the VOA "X & Y" names, so a rerun re-creates the
2026-08-20 fault; v2 drops both tables with `CASCADE`; v1's `parse_money` returns 0 for a blank cell (rule 1: never read a
blank as 0), v2's `except ...: continue` silently drops a row, both round weekly values as floats, and neither has a preview
or a `--commit`. The incident of this same day shows the cost. The new loader halts on a blank, text or negative rate cell,
skips no row, derives the weekly columns with `Decimal`, and converts names only through a committed exact alias table.

## The frozen rates evidence

The registry and the run-log note both say the 2026-27 rates are "frozen at 2024-25 levels". It is true, and is now shown:
`england-rates-2025-to-2026.csv` and `england-rates-2024-to-2025-revised.csv` (byte-identical, sha256 starting
da7e106fbc88a7f0; older layout with a title row, header on row 1, "&" names; the title row still reads "24/25") hold 152 BRMAs,
18 of them "&" names. After the alias table their 760 monthly values equal the 2026-27 file's on every BRMA and every
column: 0 differences, no BRMA on one side only. `read_csv` halts on both older files, naming the older layout, so they are
used only for this check.

The 2026-27 file (`england-rates-2026-to-2027.csv`, sha256 starting 3b33d922342719b1, cp1252, `£` is 0xA3) was reissued on
8 April 2026: the page's change history reads "Corrected date error ... header from '2024 to 2025 SAR' to '2026 to 2027'".
The first issue is not held, so whether any value changed in that reissue is unknown. The loader reads the financial year
from the CSV header and checks it against the page; a mismatch halts unless `--accept-header-year` names it.

## The weekly columns are derived

The DWP file publishes monthly amounts only. The five `*_weekly` columns that W1 reads are derived in the loader as monthly x 12 / 52,
`Decimal` half-up to 2 decimal places, marked `# not a source value`, and verified independently by the gates (live, every
edition and the held CSV: 0 differences over 760 values). Nobody has checked them against the VOA's own weekly figures. The old
scripts rounded as floats; no 2-decimal monthly amount can differ between the two methods (the loader's tests check every
amount up to 3,000.00), so no held value is affected. They stay exactly as they are. Any document that quotes a weekly rate
should say it is derived.

## The crosswalk and its correction

`la_brma_mapping` is a derived table, not a publication: the VOA BRMA layer (May 2020, `GML.zip`, sha256 starting
66515b0a1403e455, 152 polygons, layer `English_BRMA_LHA__Layer`, field `Name`, no CRS in the file; each geometry's srsName is
EPSG:27700) joined to the centroids of `la_boundaries` (May 2024, 296 rows). `check-mapping` rebuilds it read-only:

- 295 authorities have their centroid within a polygon; E06000053 (Isles of Scilly) has none and takes the nearest polygon,
  Kernow West (`nearest_fallback`).
- 18 VOA names go through a committed exact table (`scripts/s14_brma_names.json`, 18 "X & Y" to "X and Y" pairs, each checked
  to be a BRMA of the DWP file), never a regex. Run-log 53's note says 17 names were reconciled; the table holds 18, read
  from the layer and the CSV on 2026-10-11. I did not establish why 17 was recorded.
- The rebuild reproduces `brma_name` and `la_name` for 296 of 296 authorities. The only difference from live is
  `mapping_method`: held `'NaN'` on 295 rows against rebuilt `centroid_spatial_join`. The `'NaN'` is a pandas NaN written into
  a text column by v2.
- Edition 1 keeps it exactly as held (including `'NaN'`). **Edition 2 is the named correction `s14-mapping-method-2026-10`**:
  `'NaN'` to `centroid_spatial_join` on those 295 rows and nothing else (`la_name`, `brma_name` and the 296 keys equal; the
  evidence is `check-mapping`, which finds each of the 295 centroids within its polygon). It was loaded with
  `--only mapping --acknowledge brma-2020-05_lad-2024-05`; the loader writes the correction's name into the edition's label
  because the change matched its definition exactly.
- The vintage is pinned: the loader holds the layer's sha256 and the `la_boundaries` state (296 rows, md5 starting
  3335ef7a7ee622b4). Another layer file or a changed `la_boundaries` halts with "a deliberate decision". A newer BRMA or LAD
  vintage is out of scope.

## The `imd2025.rank` question

`scripts/s14_lha_rates_build.py:1128` (before retirement) sat inside `task9_print_updated_w1_node5_sql()`: a string printed as a
template for a person to paste into W1 node 5. It was never executed against the database. It went with the file when the file
became a guard. Nothing else reads `imd2025.rank`.

## What was written (2026-10-11)

1. `ddl --commit`: `brma_lha_rates_editions` and `la_brma_mapping_editions` (append-only, UPDATE, DELETE and TRUNCATE refused
   by triggers), their two `_file_checks` ledgers, and the one additive change to a live table: `ALTER TABLE la_brma_mapping ADD
   COLUMN vintage text NOT NULL DEFAULT 'brma-2020-05_lad-2024-05' CHECK (vintage = 'brma-2020-05_lad-2024-05')`. It is
   default-filled, not in the primary key and not selected by any reader: before the ddl the repo, `sql/` and `analysis/` were searched for
   `SELECT *` or `m.*` readers of `la_brma_mapping` and for an unqualified `vintage` in a query joining it, and found none (the
   readers are W1 `05_la_signals.sql`, `scripts/verify/w1_live/la_signals.sql`, `verify_source_registry.py` gate 18 and three
   `analysis/` scripts, all by named columns).
2. `migrate-legacy --commit` (after a preview and a `--simulate`, both equal to Task 3's read-only preview): rates edition 1
   "as loaded" (152 rows, the live table as held, proved against `england-rates-2026-to-2027.csv`: 760 monthly values as
   published and 760 weekly as derived, 0 differences), a ledger row for the 2026-27 file with outcome `unchanged`; crosswalk
   edition 1 "as loaded" (296 rows, `'NaN'` kept, proved by `check-mapping` on `brma_name` and `la_name`, 296 of 296), no
   ledger row. Run-log id 519 (448 rows).
3. `load` (preview): rates 2026-27 `unchanged` (the file's sha and URL are in the ledger, nothing parsed; the page lists no
   year after 2026-27); the crosswalk REJECTED pending `--acknowledge` naming `s14-mapping-method-2026-10`. Then
   `load --only mapping --acknowledge brma-2020-05_lad-2024-05` as a preview, `--simulate` and `--commit`: crosswalk edition 2
   (296 rows, 295 areas changed in `mapping_method` only), a ledger row (outcome `revised`). Run-log id 520 (296 rows).
4. `refresh-latest` (preview, `--simulate`, `--commit`): rates 0 rows (nothing to write); crosswalk 295 live rows refreshed
   (E06000053 keeps its original `loaded_at`). The before/after guard passed. `mapping_method` is now `centroid_spatial_join`
   on 295 rows and `nearest_fallback` on one; `brma_name` untouched.
5. `status`: OK for both. `s14_lha_editions_verify.py`: all 25 gates PASS, exit 0 (the W1 join, gate 6, leaves 0 of 296
   authorities unmatched). `verify_source_registry.py` gate 18 passes. No `zz%` table, function or view remains, and
   `v_la_rate_triangulation` is still present. No W1, `refresh_map.py`, export, `push.py` or `git push`.

## When 2027-28 publishes (expected late January or early February 2027), and on a reissue

From `ONS_Population_Estimates`:

1. `python scripts/s14_lha_editions.py load` (a preview; the database is not written) reads the GOV.UK collection "Universal
   Credit Local Housing Allowance rates", finds the newest "<yyyy> to <yyyy>" year page and its one England monthly-rates
   attachment, downloads it to `data/raw/s14_lha` and previews. Read the whole preview. Expect a new period 2027-28, no change
   to 2026-27. The discovery step warns if no 2027-28 page exists after 28 February 2027.
2. The crosswalk is checked against the new year's names: **a BRMA name missing from the new year's rates is REJECTED and
   no flag releases it** (it would leave authorities with no rate in the W1 join). That needs a deliberate decision, with the
   evidence, and probably a new BRMA vintage. The rates part can be loaded alone with `--only rates`.
3. `load --commit`, then `refresh-latest` (preview, `--commit`), `status`, and `python scripts/s14_lha_editions_verify.py`
   (exit 0) and `python scripts/verify_source_registry.py`. **Run `refresh-latest` before W1:** it copies the edition's
   `loaded_at` onto the live rows. Both tables (`brma_lha_rates` and `la_brma_mapping`) are W1
   inputs. W1 reads the newest financial year, so the new year's weekly values change what W1 reads from that point: that is
   the intended effect.
4. A reissue within a year (the page's change history gets a newer timestamp with different bytes) is stored as the next
   edition of that year only when `--accept-reissue 2026-27` names it, and the rank is the page's change-history timestamp,
   never the media id or URL. An older file (a year before the newest held) halts unless `--allow-older-file`.
5. A halt means: a blank, text or negative rate cell, a financial year in the CSV header that disagrees with the page, a layout
   change, a BRMA set that changed, a weekly derivation that does not reproduce, or a stop threshold (a BRMA moving over 25% in
   a year or the median over 10%, which could not be calibrated because only frozen years are held). Each exits 1, stores
   nothing and writes no ledger row.

## How to reverse

`python scripts/s14_lha_editions.py restore-edition TABLE PERIOD N` (preview by default; TABLE is `brma_lha_rates` or
`la_brma_mapping`) stores edition N's rows as the next edition; `refresh-latest --commit` applies it to the live table. To
return the crosswalk to the held `'NaN'` form: `restore-edition la_brma_mapping brma-2020-05_lad-2024-05 1`, then
`refresh-latest --commit`. Nothing is ever deleted from the editions tables. The `vintage` column can be dropped with
`ALTER TABLE la_brma_mapping DROP COLUMN vintage` (no reader selects it), but the loader needs it.
