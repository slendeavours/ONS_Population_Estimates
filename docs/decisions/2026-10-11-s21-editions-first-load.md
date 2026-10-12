# S21 ONS statistical nearest neighbours: first load on the editions engine

Status: done 2026-10-12. Loader: `scripts/s21_neighbours_editions.py`; gates: `scripts/s21_neighbours_editions_verify.py`.
Design: `docs/superpowers/specs/2026-10-11-last-four-sources-design.md` (audit record, not approval; S21 column,
findings 8, 9 and 12, Decisions item 1); plan `docs/superpowers/plans/2026-10-11-s21-neighbours-editions.md`.

**No live pair, rank or code changed.** `la_statistical_neighbours` holds the same 1,480 rows it held before (296
authorities x ranks 1 to 5; hashes below). The only change to the live table is one additive column, `release_date`.
Nothing W1 or the map reads is involved (S21 is not a W1, export or map input), and W1 and `refresh_map.py` were not run.
The readers are `analysis/council_briefing/council_briefing_build.py` and `analysis/liverpool_paper/extract.py`, both of
which select `statistical_neighbour_lad24cd` by name for one authority; their comparator sets are unchanged.

## Decision (Scott, 2026-10-11): keep the live rule as it is

The live table is the five nearest **English** neighbours of the publisher's twenty, **renumbered 1 to 5** (`LIVE_RULE` in
the loader). Scott decided on 2026-10-11 to keep it. The live `source` text
("ONS Clustering similar LAs and statistical nearest neighbours, Table 7a (LTLA global), Mar-2026 edition") does not name
the rule; it is left as held, and a later refresh writes a label that does.

What the rule does to the publisher's ranking, on the March 2026 file (counted by the loader against the held file and
the live table, 0 differences either way):

- 238 of the 1,480 live pairs carry a rank different from the publisher's, in 71 authorities (the 71 whose publisher top
  five include a Welsh, Scottish or Northern Irish neighbour).
- 105 of the 1,480 pairs come from publisher ranks 6 to 15 (the furthest is rank 15).
- No authority has fewer than five live pairs.

**The alternative, if Scott wants it later:** the publisher's top five as published, English neighbours only (no
renumbering). That leaves 71 authorities with fewer than five comparators. It is a change of the `LIVE_RULE` constant in
the loader plus one `python scripts/s21_neighbours_editions.py refresh-latest --acknowledge 2026-03-18` (preview first). It is
not a reload: the editions keep `publisher_rank` and `england_rank` for every row. The comparator sets the two readers
build for those authorities would change, and gate 2 of the verify script (which pins "English only, ranks 1 to 5, five per
subject") would need a deliberate change as well.

## How the table came to hold what it holds (checked 2026-10-12 against the database, the run log and the old script)

- Loaded 2026-07-24 08:56:26.598029 UTC, run-log id 66 ("Source 21 - ONS Statistical Neighbours", status success, 1,480
  rows) by `scripts/s21_statistical_neighbours_build.py`, from `data/raw/ons_statistical_neighbours_2026.xlsx` (same bytes as
  the held `data/raw/s21_neighbours/2026clusteringsimilarlocalauthoritiesandnearestneighbours.xlsx`, sha256 starting
  26e9ca037015db12), by truncate and insert. The table's `loaded_at` is that one instant on all 1,480 rows.
- That build's rule: take the list of authorities from the latest `staging_la_signals` run (a W1 output), drop every
  neighbour that is not in it (which removes all Welsh, Scottish and Northern Irish neighbours), keep the first five left
  in the publisher's order and renumber them 1 to 5. It recoded Barnsley and Sheffield (E08000038/39 to E08000016/19)
  with a private dict whose comment says the file "still carries the pre-recode codes". Checked on 2026-10-12 against the
  file, `la_boundaries` and the live table: the file carries the new codes E08000038 and E08000039 (as subject rows and in
  93 neighbour cells of the sheet) and never E08000016 or E08000019, while `la_boundaries` and the live table use E08000016
  and E08000019. So the dict maps the right way (file code to canonical code) and its comment is the wrong way round.
  The loader now reproduces the same 1,480 rows with `la_boundaries` as the list of authorities and `geography.resolve`
  for the recode; no private dict. The script was first committed on 2026-08-20 (59ae151), four weeks after its one load.
- The old build was not run or imported in the tasks that wrote the loader. It was retired on 2026-10-12: it exits 2 at its
  first statement (`scripts/s21_statistical_neighbours_build.py`, tested only in the scrubbed sandbox).

## Before-state (read-only, confirmed against the live table before the ddl)

1,480 rows, one `loaded_at` (2026-07-24 08:56:26.598029 UTC), one `source`, no `release_date` column, survey hash
(md5 over `lad24cd|statistical_neighbour_lad24cd|neighbour_rank|source`, ordered by the key) `23bf09bff006bf756ce1a7eedefe73ef`,
as surveyed. No view or function refers to the table. A grep of the repo, `sql/` and `analysis/` found no `SELECT *`, no
positional read and no unqualified `release_date` against it.

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a
64-hex run, stays untouched. They were printed with `python scripts/s21_neighbours_editions_verify.py --print-note-lines`
before the `ddl`, and are the lines the verify script's gates 16 and 17 read.

- `before-state` hashes `lad24cd|neighbour|release|rank` and is compared with `LIVE_RULE` applied to edition 1.
- `before-state-all` hashes every stored column except `release_date` (so it is the same before and after the `ddl`).
- `live-read` hashes `lad24cd|neighbour|rank`, what the two readers use.

before-state la_statistical_neighbours 2026-03-18 rows=1480 sha256-first32=cee807d2e162aca4a8b8da0d28d81aad sha256-last32=ae1db20cb3e98b54aa3aacf4818eed5a
before-state-all la_statistical_neighbours 2026-03-18 rows=1480 sha256-first32=afe75f8396e071896b471e1ff76a7060 sha256-last32=f41d06aaf7872a6a59344880411b2e24
live-read la_statistical_neighbours rows=1480 sha256-first32=c54a1c48121eb1cd35fdbecc2e90c8d5 sha256-last32=ee572bcf461db7b81e402478c87e9782

After the migration the live rows were read again: 1,480 rows, one `loaded_at` (unchanged), survey hash
`23bf09bff006bf756ce1a7eedefe73ef`, `release_date` 2026-03-18 on every row.

## What was written (2026-10-12)

1. `ddl --commit`: `la_statistical_neighbours_editions` (append-only; triggers `s21_editions_immutable` and
   `s21_editions_no_truncate`), `la_statistical_neighbours_editions_file_checks`, and
   `ALTER TABLE la_statistical_neighbours ADD COLUMN release_date date NOT NULL DEFAULT '2026-03-18' CHECK (release_date = '2026-03-18')`.
2. `migrate-legacy` on the held file (preview, `--simulate` rolled back, `--commit`): edition 1 "as loaded" for release
   2026-03-18, 5,920 rows (296 English subjects x 20 neighbours), content sha256 (two halves, join them)
   `26ec5c3c8df08c16cb4df42c084fca60` then `786829274cf715e558f3936f14bfeab8`, proved against a re-read of the held file (`LIVE_RULE`
   reproduces all 1,480 live rows, 0 differences either way); a ledger row marked as loaded. Run-log id 543. Live untouched.
3. `load` (preview): the landing page lists only the 2026 edition (release 2026-03-18, API date agrees); the download is
   byte-identical to the held file; already in the ledger; "nothing to do". `refresh-latest` (preview): "none (live equals
   LIVE_RULE applied to the latest edition)", so nothing was refreshed. The `source` text therefore stays as held. `status`: OK.
4. Kept: the held raw file, `landing.json` and `edition_2026.json` in `data/raw/s21_neighbours`.

## A new ONS edition

`python scripts/s21_neighbours_editions.py load` (preview) reads the landing page, downloads the newest edition to
`data/raw/s21_neighbours` and compares it. A different Cover date is a new release: `load --commit` stores it as editions
only; it never reaches the live table through `load` or `refresh-latest` (the live table has a CHECK allowing one release
date). Adopting it would change the comparator sets of every authority, so it is a decision for Scott: it needs a
deliberate change (the live release, the CHECK and the gates), not a flag. A reissue of the live release (same Cover date,
different content) is the next edition of 2026-03-18 and reaches live only through `refresh-latest`, which needs
`--acknowledge 2026-03-18` when it changes a live pair or rank. A plain `load` follows the newest edition on the landing
page; a reissue of the live edition after a later edition exists must go through `--file --no-page`. Gates 16 and 17 are
migration-time guards: a later `refresh-latest` that changes live will fail gate 17 until this note is updated.

## How to reverse

Nothing is deleted from the editions tables. To change what live holds: edit `LIVE_RULE` (or `restore-edition` to store an
earlier edition's rows as the next edition), then `refresh-latest --acknowledge 2026-03-18` (preview first, then `--commit`).
To undo the migration entirely: the live rows were never changed, so dropping `release_date` from
`la_statistical_neighbours` and the two new tables returns the database to its previous state; the editions tables refuse
UPDATE, DELETE and TRUNCATE, so that drop must be deliberate.
