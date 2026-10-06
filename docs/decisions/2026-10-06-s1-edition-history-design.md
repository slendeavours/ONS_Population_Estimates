# S1 edition history: store revisions as new editions, then reload the seven stale quarters

Status: implemented 2026-10-06. See the execution log at the foot.

## Why

`la_statutory_homelessness` is keyed on `(lad24cd, period)` with no release column, so a revised
figure can only overwrite the earlier one. Three consequences are live:

1. 2023Q2–2024Q4 no longer match what MHCLG publishes (200–230 of 296 authorities differ on the A1
   measures). They were last loaded 2026-04-01. See the decisions index, open item 1.
2. 108 `households_in_ta` zeros in those quarters are suppressed values stored as real zeros (open
   item 3). From 2025Q1 the same values are NULL.
3. The 2025Q2 revision (MHCLG, 30 Apr 2026) was applied in place on 2026-10-05. Only a CSV
   before-image survives. That departed from the additive rule.

## Rule being adopted

A revision is data. A published revision of a figure we already hold is stored as a new edition,
exactly as a new quarter is stored as new rows. No row is updated or deleted to record it. What
analysis reads by default is the latest edition; every earlier edition stays queryable.

## Design

**1. `la_statutory_homelessness_editions` (new, append-only).**
Key `(lad24cd, period, edition)`. Columns: the same measure columns as the current table, plus
`edition` (integer, 1 = first loaded), `release_label` (e.g. "Revised"), `published_date` (from the
release page or file Last-Modified, whichever is stated), `source_url`, `source_file`,
`source_sha256`, `loaded_at`, `supersedes` (the edition it replaces, NULL for the first). Missing
values (`..`, `-`) are NULL in every edition. Updates and deletes are blocked by trigger.

**2. Backfill what is stored today.** The 11 current quarters enter as edition 1, keeping their
`loaded_at`. The 2025Q2 before-image CSV enters as edition 1 for 2025Q2 and the 5 Oct state as
edition 2, so the 2025Q2 history is complete. The pre-revision zeros in 2023Q2–2024Q4 are kept
as-is in edition 1: that is what was loaded, and the problem is recorded, not erased.

**3. Reload the seven quarters as new editions.**
For each of 2023Q2–2024Q4, the candidate sources are the local copy in `data/raw/s1b_a3/`, the URL
in `homelessness_quarter_urls`, and the attachment the GOV.UK release page lists now. Every
distinct file (by sha256) becomes its own edition, ordered by published date. Each load:
- extracts with the existing S1 parser, recoded for Barnsley and Sheffield through `la_code_lookup`
  before insert;
- stores `..`/`-` as NULL: `s1_extract_ods.extract()` already returns None for markers, so this
  load does not depend on the n8n S1 node 2 `|| 0` defect, which remains a separate item;
- must show 296 authorities and a clean reconciliation against S1b's independent A3 extraction;
- produces a diff against the previous edition (rows and cells changed, by measure) kept with the
  decision record.

**4. The current table becomes the latest-edition layer.**
`la_statutory_homelessness` is repopulated from the newest edition per `(lad24cd, period)` by one
script, so W1, the five analysis scripts, `export_map_data.py` and `v_la_statutory_homelessness`
need no change. Nothing is lost by the repopulation because every value it replaces survives in the
editions table. `source_file` is filled for the seven quarters now that a single file produced each
row. `homelessness_quarter_urls.reproduces_from_source` is re-evaluated and recorded.

**5. Standing instructions.** Revisions are stored as editions like new data. Every quarterly
refresh re-checks each loaded quarter against its current published file and, where a value
differs, writes a `source_check_log` row with `revision_detected` and loads a new edition. A
comparison against what was sent (see the existing "what changes versus what was sent" rule) is
produced whenever an edition changes a figure used in a delivered output. Changes to:
- memory `additive-data-never-overwrite` (private);
- `docs/decisions/README.md` (closes open items 1 and 3), `CHANGELOG.md`, `docs/METHODOLOGY.md`
  (revision handling paragraph), `docs/s1b_support_needs_source.md` and the S1 procedure notes;
- `source_registry` S1 `revision_note` (generated field: set by the backfill script, not by hand).

**6. Safety.**
- Verified `pg_backup` set taken before the first write (restore tested to a scratch database).
- All DDL and loads run in one transaction per step; any gate failure rolls it back.
- Gates: 296 authorities per edition; key uniqueness; no code outside `la_code_lookup`; edition
  counts per quarter; latest-layer equals latest edition exactly; W1 `la_signals` and
  `export_map_data.py` output unchanged except for the measures that were revised.
- The 21 existing verification gates must still pass.

## Files

Newly obtained source files go in `data/raw/s1b_a3/`, named `<period>_<original name>` as the
existing copies are, with sha256 and source URL recorded in the editions table. `data/reference/`
holds provenance CSVs and reference inputs; these are raw publisher files, so they belong in
`raw`. The `statutory_homelessness_2025Q2*` CSVs already in `reference` stay where they are.

Files known not to be local: the seven URLs in `homelessness_quarter_urls` for 2023Q2–2024Q4
differ in size from the local copies (for example 2024Q1: 1,252,627 bytes online against 2,334,141
local). Seven files, roughly 12 MB in all, are to be fetched. The exact list is confirmed with the
user before download.

## Out of scope

W1 workflow edits (the stale `2025Q2` label and `|| 0` in S1 node 2 are separate items), S10
suppression markers, and the support-needs table (its key already holds one edition per period by
design; handled in the same pattern in a later change if wanted).

## Open questions

None for the design. The download list is the only gate before the load step.

## Execution log

Added after the work was done; the design above is left as written.

**Backup.** A verified `pg_backup` set was taken before any write:
`db-backups/manual-pre-s1-edition/daily/2026-10-06/exempt_pipeline.dump` (12,245,364 bytes,
restored to a scratch database and counts matched). It predates the editions table. Before the
live layer was refreshed, `la_statutory_homelessness_bak_20261006` was created (3,256 rows,
identical to live); it is kept, because gates 10 and 11 compare against it.

**Editions held** (`la_statutory_homelessness_editions`, 5,624 rows).

| Quarters | Editions | Edition 2 source |
|---|---|---|
| 2023Q2, 2023Q3, 2023Q4, 2024Q1, 2024Q2, 2024Q3, 2024Q4 | 1 and 2 | the registry 'revised' file |
| 2025Q2 | 1 and 2 | MHCLG revision of 30 April 2026 |
| 2025Q1, 2025Q3, 2025Q4 | 1 | none |

Authorities changed, edition 2 against edition 1: 2023Q2 193, 2023Q3 184, 2023Q4 195, 2024Q1 194,
2024Q2 190, 2024Q3 190, 2024Q4 191. Full diffs are in `s1-edition-diffs-2026-10-06.md`.

**Gates.** The `s1_editions_verify.py` gates (1 to 11, 3a to 3g, plus the seeded gates 6s to 8s,
10s and 11s that prove a gate can fail) all pass after the live layer was refreshed. Gate 5
(live layer equals the latest edition) was expected to be red between the edition load and the
refresh, and was. `verify_source_registry.py`: 21 of 21 pass. W1 national figures are unchanged
(TA 130,775; prior year 115,431; year-on-year 13.29%); eleven authorities' prior-year TA moves
from 0 to NULL, with no change to their trend label.

**What the diffs showed.** The "200 to 230 authorities differ" divergence in the design's Why
section was measured against the older release-page files, an older vintage. Edition 1's A1 figures already matched the registry revised files numerically: the only A1 differences are zero to NULL for authorities that did not submit (36 authority-quarters, 144 cells: 3, 8, 4, 7, 5, 7 and 2 authorities in 2023Q2 to 2024Q4, each across all four A1 measures); no A1 figure is a numeric revision. The genuine value revisions in edition 2 are the A3 `support_needs_total` (about 175 to 190 authorities per quarter). Edition 2 also turns suppressed zeros into NULL: `households_in_ta` (95
cells across the seven quarters; the 108 in the Why section is the count of stored zeros, of which 95 were suppressed values and 13 are published zeros that stay) and the A1 cells above.

**Rulings made during execution.**
1. Task 4 was split: a dry run first (no inserts, because the table is append-only and a wrong
   edition cannot be deleted), then the real load after the diffs were read.
2. Only the registry 'revised' file is loaded as edition 2 for each of the seven quarters. The
   release-page files are an older vintage, not what any output used. They are not editions (the
   supersedes chain cannot place an older vintage before an existing edition); they are retained in
   `data/raw/s1b_a3` and listed in `scripts/s1_editions_manifest.json` with their sha256.
3. Ordering is by the supersedes chain: latest is the one edition no other edition supersedes
   (a fork, or more than one root, is an error). `published_date` is informational. Date ordering
   would have ranked edition 1 above the 2024Q1 to 2024Q4 revised files, whose Last-Modified
   (2026-02-24) precedes the 2026-04-01 load.
4. Gate 8 was redefined. The new edition's `support_needs_total` must equal an independent
   re-extraction of A3 "households with one or more support needs" from the same file, 296 of
   296. It no longer compares with `la_homelessness_support_needs`, which was built from the
   older files and is stale against the revised ones.
5. `published_date` is the file's HTTP Last-Modified date for the loaded editions (labelled so
   in `release_label`) and the load date for edition 1 (labelled "as loaded; date is load date").
6. A TRUNCATE trigger was added beside the update and delete triggers, closing a gap in the
   append-only guarantee.

**Left open.** See `README.md`, item 3. That includes the fact that `refresh-latest` is specific to this reload and must be generalised before the next S1 quarterly load.
