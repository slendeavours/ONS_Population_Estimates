# S1: the n8n loader steps are retired and a new quarter is loaded with `load-new`

Status: implemented 2026-10-07. Follows [the S1 edition work](2026-10-06-s1-edition-history-design.md) and
[the quarterly refresh](2026-10-07-editions-quarterly-refresh.md), which left the n8n loader defect as a separate
open item. Those records are left as written.

## What

- The two n8n steps that loaded S1 ('Merge & Build Batch' and 'Process Homelessness Data' in the S1 workflow) are
  retired with a hard stop: their code is replaced by a single error that begins `RETIRED 2026-10-07` and points to
  `load-new`. Running either now throws that error and writes no data. It is not instant: in the loop path 'Create Quarter
  URLs Table' and 'Fetch File' (a download) run before the throw. The workflow was already inactive (it has only a
  manual trigger), and nothing else in it was changed. `scripts/s1_n8n_retire_loaders.py` did this; it checks the stored code first, can be run again
  safely (a second run reports both steps already retired and changes nothing) and reads the result back.
- The old code is kept in `build_reports/w1_node_backups/s1_n8n_backup_2026-10-07T112539Z.json`.
- **n8n's saved version history still contains the old code.** The retirement script updated the stored nodes but did
  not create a new saved version, so restoring an earlier version of the workflow in the n8n editor would bring the
  faulty code back. Saving the workflow once in the editor fixes the history. Scott should do that; the workflow is
  inactive and has only a manual trigger, so nothing runs in the meantime.
- A new quarter is loaded with `python scripts/s1_editions.py load-new`. The steps are in
  [../QUARTERLY_REFRESH.md](../QUARTERLY_REFRESH.md), Step 2a.

## Why: the three defects

1. **Suppressed figures were stored as zero.** Both steps turned a published `..` or `-` (the authority did not
   submit, or the figure is withheld) into 0. A zero then looks like a real count of nothing. The same defect class produced
   the stored zeros dealt with on 2026-10-06 (see the S1 edition record).
2. **Columns were read by fixed position.** The steps took each figure from a numbered column of the sheet. The
   publisher changes the layout between quarters: the 2025Q4 file renamed and moved every column, and a position-based
   read once put support-needs numbers in the wrong columns (see
   [2026-08-14-s1-support-need-column-misalignment.md](2026-08-14-s1-support-need-column-misalignment.md)).
3. **They wrote straight to the live table and overwrote existing rows.** A re-run, or a later release of a quarter
   already held, replaced the stored figures with no record of what they had been.

## The replacement

`load-new --period P (--manifest-entry N | --manifest-file NAME) [--expected-authorities N] [--commit|--simulate]`:

- reads each column by its header text (`scripts/s1_extract_ods.py`), stores a suppressed cell as NULL and a real zero
  as 0, and recodes Barnsley and Sheffield to the canonical codes;
- opens the file's own cover or contents sheet and refuses the file unless it names the same quarter and release date
  as the manifest entry (after the RO4 incident of 2026-10-07, where a label written into code was trusted without
  opening the file);
- re-reads the raw cells of all six stored measures (temporary accommodation, support needs, and the four
  assessment and duty counts) and refuses if anything stored differs. Each cell is classified independently of the
  loader (its own marker list; an unknown format stops the load), but the column for the A1 and TA1 measures is found
  by the same header-text reading as the loader and the code recode is shared; the support-needs column is read by
  the S1b reader with its own header mapping. A renamed header that matched the wrong column would still pass for A1
  and TA1;
- checks the authority count (worked out from the earlier quarters) and that every code is recognised;
- records edition 1 from the file, the live rows and the `homelessness_quarter_urls` row in one transaction, and
  checks that the live rows equal edition 1 before it commits;
- refuses a quarter that already has live rows or editions, so it can never overwrite: a revision still goes through
  `load` (Step 3). The dry run runs every database check inside a transaction that is rolled back; `--simulate` does the
  same; `--commit` and `--simulate` cannot be given together.

Checked by `scripts/s1_editions_verify.py` (45 checks, about 2 minutes): seeded files with suppressed cells and real
zeros, each refusal with nothing written, the cover sheet on all 19 files held, and the real 2025Q4 file loaded into a
made-up quarter and compared with the stored 2025Q4 (no differing rows). No real new quarter exists yet, so the real
`--commit` path has not been run.

## What stays

- The rest of the n8n workflow (its other steps) is unchanged and is not a loader for S1.
- The `homelessness_quarter_urls` table stays. `load-new` adds the quarter's row, or marks an existing one loaded, and
  does not change that row's other fields.

## What remains open

- **New releases are not detected automatically.** Someone checks the GOV.UK release page, saves the file, and adds the
  manifest entry by hand.
- **A new S1b quarter still loads with `scripts/s1b_support_needs_build.py`**, which needs a line added to its
  `RELEASES` table and always `--period`. Only the recording of edition 1 is automated.
