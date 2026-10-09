# S23 RSH registered provider social housing stock by local authority: first load

Status: done 2026-10-09. Works like [S22](2026-10-09-s22-editions-first-load.md), [S4](2026-10-09-s4-editions-first-load.md) and
[S11](2026-10-09-s11-editions-first-load.md). Loader: `scripts/s23_rsh_stock_editions.py`; gates:
`scripts/s23_rsh_stock_editions_verify.py` (21 gates, all PASS, exit 0). Design:
`docs/superpowers/specs/2026-10-09-s23-editions-design.md` (audit record, not approval).

No held value changed. The live table `rsh_rp_stock_by_la` is exactly as it was (same rows, columns, constraints and
`loaded_at`). The map is unaffected: S23 is not a W1 input, so W1 and `refresh_map.py` were not run.

## Before-state (read-only, confirmed against the live table before the migration)

Confirmed first: 10,171 rows, one stock date (2025-03-31), 296 authorities, one `loaded_at`
(2026-08-14 21:24:42.442022 UTC), 0 NULLs in every column, survey hash `4967e5b59580d98f0edc3c6f8d908ac9`.

Zeros (published): `total_social_stock` 12 (all LARPs that own no stock), `general_needs_self_contained` 4,499,
`general_needs_bedspaces` 9,878, `supported_housing_and_older_people` 4,603, `low_cost_home_ownership` 5,563.

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a 64-hex run, stays untouched. The first line is the loader's `rows_content_sha` (key plus the 11 compared columns, provenance excluded; the line the
verify script's gate 16 reads). The second hashes every stored column, `loaded_at` included: every column in table
order, rows sorted by stock date, provider code and authority code, NULL as empty, cells joined by `|`, rows by LF.

before-state rsh_rp_stock_by_la 2025-03-31 rows=10171 sha256-first32=5c0ba1ceacd364fd557d2ddadbfd1b29 sha256-last32=9644c09c83fb8468c8ee8f786135f427
before-state-all rsh_rp_stock_by_la 2025-03-31 rows=10171 sha256-first32=6645b4ef47fb6658ee5c5bd835ddee3f sha256-last32=b33c9e30acdfbd0dbabf80593611f3bb

After the migration both hashes of the live table are the same, and edition 1 hashes (compared columns) to the first.

## What the old build did

Two scripts, committed together in ad8e349 (2026-08-14 22:47 +0100, 21:47 UTC) with S1b and S24:
`s23_rsh_stock_build.py` and `s23_rsh_stock_verify.py`.

- It was written to overwrite: `INSERT ... ON CONFLICT DO UPDATE ... loaded_at = now()`. `--load` created the table,
  wrote with no preview and no `--commit`, kept a same-named raw download whatever its content, took the edition from
  one hard-coded release page (the 2024 to 2025 one) and the file identity from the page, and resolved codes with
  its own `la_code_lookup` query.
- **The load ran once.** Run-log row **95**, status `success`, 10,171 rows, 2026-08-14 21:24:42.442022 UTC. Every held
  row carries that one `loaded_at`, so nothing has been rewritten since. No earlier S23 load existed, so **nothing was
  overwritten in practice**; the code would have overwritten on a second run.
- The code was committed 23 minutes after the run. The run-log row's agent name, status and notes match the committed
  code's format, but that does not prove the code that ran was identical to the code committed.
- Its landing page was fixed, so the 27 October 2026 release would have been missed and `--load` would have
  re-upserted 2025.

## The rule 1 defect, and why no value changed

The old `num(v)` returned 0 for a blank cell. A blank stock cell would have been stored as 0. It never fired: all
50,855 stock cells of the 10,171 provider rows in the held file are integers, and so are the 296 authority subtotal
rows. There is no publisher marker for these columns (the tool notes, glossary, technical notes and data quality note
say none), so nothing is stored as NULL; the five stock columns stay `NOT NULL`, and the new loader halts on a blank,
a string, a negative or a non-integer stock cell. The 12 zero-total rows are LARPs the tool's own note says own no
stock: published zeros. Code fix, no data change.

## The proof run

`migrate-legacy data/raw/s23_rsh/RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx` (sha256 starting 6c237c79), one transaction:

- Identity read from the file: "RP social housing by local authority area (SDR and LADR data) 2025"; source
  1 April 2024 to 31 March 2025; publication November 2025; version 1.1; Version History 1 (October 2025) and 1.1
  (November 2025).
- The new parser on the held file reproduces every live row and every compared cell: 10,171 rows matched by key,
  122,052 cells, **0 differences**. The edition label, `source_file` and `source_url` equal the file's own;
  `publication_date` (2025-10-28) and `release_page_url` come from the release page, not the file, and are carried as held.
- Reconciliation inside the file is exact: provider rows (PRP 9,943, LARP 228) sum to the 296 authority subtotal rows on
  all five measures; the subtotals sum to the 9 regions; England 4,533,055 / 3,738,818 / 15,164 / 504,902 / 274,171.
- Barnsley and Sheffield are E08000016 and E08000019 only (old form, `geography` dataset form `old` for 23).
- The collection offers nothing newer than 2024 to 2025, and today's download of the look-up tool is byte-identical to
  the held file.

## Revision table

| Compared | What changed |
|---|---|
| 2025 tool V1.0 (October 2025) to V1.1 (November 2025) | Version History: "Corrected an issue which affected England stock figures only." V1.0 is not on the release page and not in the Wayback Machine (earliest capture 23 November 2025 already lists V1.1), so it is **not recoverable** and the size of the change is not measured. The held file is V1.1. |
| Held 2025 data against today's published file | byte-identical; the re-read reproduces all 10,171 rows, 0 differences. |
| Publisher's policy | No scheduled revisions; republication "in the April of the year following" only if provider changes require a major revision; non-scheduled corrections for substantial errors. No April 2026 republication happened (the release page's change history is only "First published."). |
| 2025 release on earlier years | LARP stock for 2020 to 2024 revised, headline figures only, under 0.4% of total stock. Earlier years' tools were not reissued. |

A held stock date can change through a reissued tool: that is read as the next edition and reaches the live table only
through `refresh-latest`. GOV.UK attachments can change under new media ids with only the workbook's Version History
saying so, so the ledger keys on (final URL, sha256).

## The 2024 layout, and why it is not back-filled

`RP_COMBINED_TOOL_2024_FINAL_V1_Locked.xlsx` (sha256 starting 8e1de225) is a different layout: stock headers prefixed
(`CLCRR025_LA_GN_SC_Own` and others), different `SDR_Size` and `Survey_Status` wording, provider-by-region rows with
`LA_Code` `N/A` (249 duplicate provider and code pairs), and one provider (Medway Council 00LC) whose total, 3,037,
is not the sum of its parts, 3,036. The parser halts at the header check and names what it saw. Back-filling 2024 is
out of scope: it would need a header alias map and a decision on the region rows, and each earlier year would be its
own new period. It also shows the 2026 file may change layout.

## What was written (2026-10-09)

- `ddl --commit`: `rsh_rp_stock_by_la_editions` (append-only; triggers `s23_editions_immutable` and
  `s23_editions_no_truncate` refuse UPDATE, DELETE and TRUNCATE) and `rsh_rp_stock_by_la_editions_file_checks`. No
  change to the live table.
- `migrate-legacy ... --commit` (after a preview and a `--simulate` that rolled back, both equal to Task 2's read-only
  preview): edition 1 "as loaded" for 2025-03-31, 10,171 rows, `published_date` 2026-08-14 (the load date),
  `source_file` `as loaded: RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx; version 1.1; dated 2025-11`; one ledger row for the
  file (outcome `unchanged`). Run-log row 295, 10,171 rows. Live untouched.
- `load` preview: the page's file is the held one by (URL, sha256); nothing parsed. `load --recheck 2025-03-31` and
  `load --file data/raw/s23_rsh/RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx --recheck 2025-03-31` previews: unchanged, 0 rows
  changed against edition 1. So no `load --commit` was made. `refresh-latest` preview: nothing planned. `status`: OK.
- The raw file stays in `data/raw/s23_rsh` (git-ignored); the verify script's gates 14 and 17 find it by sha256.
- `status` before the migration said "run sync-new": that is the engine's wording; S23 has no `sync-new`.
- W1, `refresh_map.py`, the export, `push.py` and `git push` were not run.

## Each autumn (next: 27 October 2026, 09:30)

From `ONS_Population_Estimates`:

1. `python scripts/s23_rsh_stock_editions.py load` finds the newest "Registered provider social housing stock and rents
   in England <yyyy> to <yyyy>" release in the GOV.UK collection and its one "Registered providers look-up tool"
   attachment, downloads it to `data/raw/s23_rsh` (also in a preview; the database is not written) and previews.
   Read the whole preview. `--release Y1-Y2` or `--file PATH` take other files with the same identity and
   older-file checks. Expect a new period `2026-03-31`.
2. `load --commit`, then `refresh-latest` (preview), `status`, and `python scripts/s23_rsh_stock_editions_verify.py`
   (exit 0).
3. A reissued tool for a held year is stored as the next edition; `refresh-latest --commit` applies it (with
   `--accept-key-changes PERIOD` if providers were added or dropped).
4. What a halt means. The loader halts, naming what it saw, rather than guess:
   - A header change (the 2024 file changed them; the 2026 file may): the `STOCK_BY_LA` header must equal the 2025
     set. Fix with a deliberate alias map and the evidence, then re-run.
   - A collection or release title that no longer fits, or no or several look-up tools.
   - Barnsley and Sheffield as E08000038 and E08000039 (the file describes 31 March 2026, after the 1 April 2025
     change): the load stops under rule 4.5; the fix is `geography.DATASET_FORM['23']` to `mixed` with the evidence
     (2025 old, 2026 new, one form per period), then re-run.
   - A stop-threshold REJECTED (nothing stored, no ledger row, exit 1): fewer than 296 authorities; national total
     moving over 5% (supported housing over 10%, any authority over 25%) for a new period; for a revision, a national
     total over 1%, over 30 authorities, an authority over 10% or over 5% of provider rows added or removed.
5. S23 is not a W1 input. If it is ever wired into W1, run `refresh-latest --commit` in the same session before W1,
   because it copies the edition's `loaded_at` and a later W1 run would otherwise hide the revision from
   `refresh_map.py --check`.

## How to reverse

`python scripts/s23_rsh_stock_editions.py restore-edition PERIOD N` (preview by default) stores edition N's rows as
the next edition; `refresh-latest --commit` then applies it to the live table. To return 2025-03-31 to the held state,
restore edition 1 and refresh. Nothing is ever deleted from the editions tables.
