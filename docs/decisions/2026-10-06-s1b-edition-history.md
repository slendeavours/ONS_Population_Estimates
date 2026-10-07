# S1b edition history: store revisions as editions and bring the live table to the revised files

Status: implemented 2026-10-06. Follows [the S1 edition work](2026-10-06-s1-edition-history-design.md), which
left S1b out of scope and recorded `la_homelessness_support_needs` as stale for 2023Q2 to 2024Q4.

## What

New append-only table `la_homelessness_support_needs_editions` (update, delete and truncate blocked by
trigger), key `(lad24cd, period, category_code, edition)`, 174,640 rows. Edition 1 is each quarter as
loaded (101,232 rows). 2025Q2 edition 1 is the original MHCLG file and edition 2 the revised file of
30 April 2026. 2023Q2 to 2024Q4 edition 2 is the registry 'revised' file. Latest is the edition no other
edition supersedes. The live table `la_homelessness_support_needs` is the latest-edition layer, refreshed by
`python scripts/s1b_editions.py refresh-latest --commit` (`value`, `value_flag`, `source_url`,
`source_edition`, `edition_variant`, `category_label`; `loaded_at` untouched). The quarters 2025Q1, 2025Q3
and 2025Q4 are unchanged.

In each of the seven quarters 2023Q2 to 2024Q4, 1,648 to 2,078 of 9,176 cells changed in 209 to 269
authorities, almost all value-to-value revisions of the A3 counts, with a handful flipping between a number
and a suppression flag. For 2025Q2, edition 2 differs from edition 1 in 2,147 of 9,176 cells (value or flag, NULL-safe) in 236 authorities. 'One or more support needs' equals `la_statutory_homelessness.support_needs_total`
for 296 of 296 authorities in each of the seven quarters.

## Why

The key `(lad24cd, period, category_code)` holds one edition per period, so a revision could only overwrite.
The S1 edition work showed that MHCLG's revised files change the A3 counts, that S1 already holds them, and
that S1b still held the older release-page values, so the two disagreed.

## Rulings

1. **The revised files are used.** They are not linked from the GOV.UK release pages any more, but they are
   newer than the release-page files S1b was built from, and S1 already uses them.
2. **The live label was refreshed too.** `category_label` is the sheet header and embeds that file's England
   total, so it differs between editions; the live label should say what its own source file says. A second
   snapshot was taken first.
3. **Gates 6 and 7 of `s1b_support_needs_verify.py` were repaired.** An earlier note (decisions index, open
   item 3, and `docs/s1b_support_needs_source.md`) said the script "fails 6 of 7 gates". That was wrong: six of
   seven passed. Gate 6 failed because the table held a revision (2025Q2 revised, loaded by hand) that the
   release-page resolver does not link, so the gate's reload overwrote it with the linked original; its
   "cells differing" figure joined the table to itself and could not have detected that. Gates 6 and 7 now
   re-extract each period's latest edition from its recorded local file; all 7 pass. The earlier records are
   left as written; this is the correction.
4. **`latest_edition` was fixed once in `scripts/s1_editions.py`.** It now validates the whole supersedes chain
   (it had missed two malformed cases) and is shared by S1 and S1b.

## Safety

A verified `pg_backup` set was taken before the first write (restore tested to a scratch database). Snapshots
`la_homelessness_support_needs_bak_20261006` (before the value refresh) and `_bak_20261006b` (before the label
refresh) are kept; editions gates 9 and 10 depend on them. `scripts/s1b_editions_verify.py` and
`scripts/s1b_support_needs_verify.py` pass, including seeded gates that prove the checks can fail.

## Left open

- `refresh-latest` is specific to this reload (periods and snapshot tables hardcoded) and must be generalised
  before the next S1b quarterly load, as for S1.
- `scripts/s1b_support_needs_build.py` still resolves the release-page file and prefers it. A later edition
  must be loaded with `s1b_editions.py load`; running the build script would write the linked file over it.
- The RO4 table key still needs edition treatment.
- No insert-time trigger enforces `supersedes` = chain tip (only `load` checks it).

*Update 2026-10-07:* `refresh-latest` is now general, no gate depends on the snapshot tables, and they are retained for Scott to drop when comfortable; see [2026-10-07-editions-quarterly-refresh.md](2026-10-07-editions-quarterly-refresh.md). The text above is left as written.
