# S24 RSH register, regulatory judgements and enforcement notices: first load on the editions engine

Status: done 2026-10-11. Works like [S12](2026-10-10-s12-editions-first-load.md), [S13](2026-10-10-s13-editions-first-load.md) and [S23](2026-10-09-s23-editions-first-load.md).
Loader: `scripts/s24_rsh_register_editions.py` (three specs: the register, the judgements, the enforcement notices); gates:
`scripts/s24_rsh_register_editions_verify.py` (all 21 PASS, exit 0).
Design: `docs/superpowers/specs/2026-10-11-last-four-sources-design.md` (audit record, not approval). Scott authorised the real
writes through the plan ("do the work, the spec is audit not approval").

**What the map sees.** Nothing. S24 is entity-level with no geography, is not wired into `staging_la_signals`, and W1, the export and
the map read none of its three tables. W1 and `refresh_map.py` were not run (Scott deferred W1 and the map until every source is
done). The only live value that changed is `notes` on the 1,578 rows of the 17 August register (text 'None' to NULL, below).

## Decided by Scott (2026-10-11)

1. **The 17 August register snapshot is kept as held.** Its rows stay in the live table. The text 'None' in `notes` is made NULL
   as a later edition (edition 2, the named correction `s24-aug-notes-none-2026-10`). Pivotal Housing Association's August note is
   not restored: no August file is held to show it. (Spec Decisions item 2: the default was applied; the alternative of dropping
   the snapshot from live was not chosen.)
2. **The marker reasons table is accepted** (spec Decisions item 3, a blanks-rule judgement). Governance and viability `-` on
   local-authority rows are read as not applicable (RSH guidance "How we approach regulatory judgements and gradings", read
   2026-10-11, says a local authority judgement can cover the consumer standards and the Rent Standard); consumer `-` whose
   change text starts "Not assessed yet" are `not_assessed_yet`.
3. **332 `-` grade cells stay unexplained, flagged for Scott.** In the 30 September file, 305 rent cells and 27 cells on nine
   private providers (consumer, governance and viability) are `-` and the publisher gives no reason. They are stored as NULL with
   the reason `marked_dash_no_reason`; nothing is guessed. The 12 August file has 326 (302 rent and 24).

## Before-state (read-only, confirmed against the live tables before anything was written)

Confirmed first: `rsh_registered_providers` 4,734 rows, survey hash `6a30ad24db177e6f86ea0ea3bb7a6521`, periods 2026-07-24
(1,579), 2026-08-17 (1,578) and 2026-09-17 (1,577); `rsh_regulatory_judgements` 326 rows, survey hash
`39c7cc2adb81b5c9f4ab30464af56dd5`, periods 2026-08-12 (15) and 2026-09-30 (311); `rsh_enforcement_notices` 2 rows, survey hash
`4c22298c60e0a14cfdfd934b407921ae`, period 2026-09-30. The loader's `migrate-legacy` re-checked all of this before it wrote.

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a 64-hex run,
stays untouched. These are the 12 lines printed by `python scripts/s24_rsh_register_editions_verify.py --print-note-lines`
before `ddl` and before the migration, while the live tables were untouched: a `before-state` line (key and values; `source`,
provenance and `loaded_at` left out) and a `before-state-all` line (every stored column) for each live period. Edition 1 (as
loaded) hashes to the `before-state` lines (gate 16). The 12 August judgements line covers the 15 left-over rows only; edition 1 of
that period holds all 308 rows from the file.

```
before-state rsh_registered_providers 2026-07-24 rows=1579 sha256-first32=bbb3f2b42ae344249c5749e161cc63ed sha256-last32=c5826e78d8ef8bcf2bfd11e2a0c62c40
before-state-all rsh_registered_providers 2026-07-24 rows=1579 sha256-first32=30f0e4fdf33acef1116c40358c96c3c3 sha256-last32=b02095224b240e4b44e6615eca89bb3b
before-state rsh_registered_providers 2026-08-17 rows=1578 sha256-first32=b404449947d73c026779ecbe8a006350 sha256-last32=8161f065df9db6bbd40069cf69a63d36
before-state-all rsh_registered_providers 2026-08-17 rows=1578 sha256-first32=3910effa8679ca98cbaf06ad527caa92 sha256-last32=31bf9b9163504cba694af427e8fa1a6d
before-state rsh_registered_providers 2026-09-17 rows=1577 sha256-first32=bac9e18b26ba4151f81c1bceae34d4bc sha256-last32=3af324d4e87301e3aada28ef7f84c77c
before-state-all rsh_registered_providers 2026-09-17 rows=1577 sha256-first32=7b6829c2319a78328ff17d4416a26e46 sha256-last32=a873d5877dc2f6de624ec46653053eea
before-state rsh_regulatory_judgements 2026-08-12 rows=15 sha256-first32=38908267b15a60603d5241f126b46db6 sha256-last32=c0ecef8d7191552a39edb7aada1ad349
before-state-all rsh_regulatory_judgements 2026-08-12 rows=15 sha256-first32=c1a594389445f1b471fcc8f4acc0e23d sha256-last32=4fe86d80ec080b3f894f6bd441638bda
before-state rsh_regulatory_judgements 2026-09-30 rows=311 sha256-first32=32903944148f5c7e5e542a6eb249dd49 sha256-last32=53f01cfef5cca4b0f68dbab12946d8cc
before-state-all rsh_regulatory_judgements 2026-09-30 rows=311 sha256-first32=4d8e78dccb5e5794a1a1b207f90058dc sha256-last32=c8bdfe00d800e163ab381b5ef43b3839
before-state rsh_enforcement_notices 2026-09-30 rows=2 sha256-first32=b45e8609b0f4e5196fa98cc7586780d3 sha256-last32=4cf64f9f43fce26e2465e20710d228d6
before-state-all rsh_enforcement_notices 2026-09-30 rows=2 sha256-first32=c8c819521ac34c255c0bc2504d7cf236 sha256-last32=302b361a3608b4f5982dc3b7fccfc5a5
```

## How the tables came to hold what they hold (checked 2026-10-11 against the database, the run log and the docs)

- **Run-log 96** (2026-08-14 21:26 UTC, the committed build): the register snapshot of 24 July and the judgements edition of
  12 August. It wrote 1,889 rows: 1,579 register rows, 308 judgements and 2 enforcement notices (1,579 + 308 + 2).
- **The 17 August register was loaded by code that is not in the repo, with no run-log row.** The rows carry `loaded_at`
  2026-08-20 14:25:27 UTC. Their `notes` column holds the text 'None' on all 1,578 rows, which the committed build cannot write
  (its `text()` turns 'None' into NULL), and Pivotal's note is absent. The publisher's August media URL now serves the
  17 September file (the same bytes as the September URL), so the August file cannot be obtained and its rows cannot be proved
  against a file.
- **Run-log 101** (2026-10-01 21:59 UTC, the committed build): the register of 17 September and the judgements of 30 September
  (1,890 rows: 1,577 + 311 + 2). The judgements upsert keys on `(registration_number, publication_date)` and overwrites
  `edition_date`, so it re-keyed 293 of the 308 August rows to 2026-09-30 and left 15 August rows in place.
- **Three judgements releases were never kept.** The page's change history lists 18 August, 26 August and 9 September; none was
  loaded and none is held. Only the 12 August and 30 September files are held, so those are the only judgements editions that
  can exist.

### The gate-5 defect: root cause in three parts

The old gate 5 compared the `-` markers in the current file's four grade columns (75 + 112 + 112 + 305 = 604 in the 30 September
file) with the NULL grades in the whole judgements table (633).

1. **The table was neither edition.** The in-place judgements load mixed the September file with 15 stale August rows. Those 15 rows
   carry 29 NULL grades (10 + 2 + 2 + 15), so 604 + 29 = 633. The gate compared one file with a mixture.
2. **Nothing "lost its change description".** The 114 rows the old gate reported (112 September and 2 left-over August) are rows
   whose governance change cell is already `-` in the publisher's own file: every `-` governance or viability grade has a `-`
   change.
3. **The only data defect: rule 1.5.** `text()` turned `-`, an empty cell and the text 'None' into one bare NULL, and no reason was
   stored. The new parse stores NULL with a reason for every NULL.

## What was written (2026-10-11)

Everything below ran from the repo with the loader, each step as a preview, then `--simulate`, then `--commit`.

1. `ddl`: the three editions tables, three ledgers and their append-only triggers. The live tables were untouched.
2. `migrate-legacy` (run-log 485, 5,357 rows): edition 1 "as loaded", proved against the held files on every stored column
   (0 differences).
   - Register 2026-07-24 (1,579), 2026-08-17 (1,578) and 2026-09-17 (1,577). The July and September rows were proved against their
     files. The August rows are kept as held: `source_file` records "file not obtainable: the publisher's media URL now serves the
     17 September file; loaded 2026-08-20 14:25:27 UTC with no run-log row". Edition 1 of the August register has no `null_reasons`
     (its notes hold the text as stored, so no value is NULL).
   - Judgements 2026-09-30 (311, equal to the live rows) and 2026-08-12 (308, the 12 August file read with the old build's rules;
     proved against the 15 live rows still tagged with it and against run-log 96's count).
   - Enforcement notices 2026-09-30 (2, live) and 2026-08-12 (2, from the August file).
   - Six ledger rows marked `as loaded`, at the held media URLs with the held sha256 values.
   - **Edition 1 of the judgements and notices marks every empty cell `reason_not_recorded_as_loaded`.** That is truthful: the old
     build recorded no reason. The real reasons arrive as edition 2.
3. `load --file ... --no-page --acknowledge PERIOD` for the 12 August and 30 September judgements files (run-log 486 and 487):
   **edition 2** of each judgements period and of each notices period. Same values, `null_reasons` filled. The page path skips
   the held files (their URL and sha256 are in the ledger), and the 12 August date is no longer in the page's change history, so
   `--file --no-page` is used.
4. `load --only register --file <17 September file> --title-date 2026-09-17 --no-page --correction s24-aug-notes-none-2026-10
   --acknowledge 2026-08-17` (run-log 488): the 17 September file is `unchanged`; the named correction stores **edition 2 of the
   17 August register**: 1,578 `notes` cells 'None' become NULL, each with the reason `stored_text_none_not_from_file`.
5. `refresh-latest --accept-key-changes 2026-09-30`: live register 2026-08-17 `notes` became NULL on 1,578 rows; the 15 left-over
   12 August judgements rows left the live table (each equals a row of its edition, which keeps them). Live judgements are now
   exactly the 30 September edition (311 rows), and live notices the 30 September edition (2). This step wrote no run-log row.
6. No newer register or judgements file is offered on the pages (newest register 2026-09-17, newest judgements 2026-09-30), so
   nothing else was loaded.

### What edition 2 changes (counts per reason)

Judgements, 30 September file (604 NULL grades, equal to the file's `-` cells: consumer 75, governance 112, viability 112,
rent 305):

| Reason | Cells |
|---|---|
| `not_applicable_local_authority` (103 Local Authority rows, governance and viability) | 206 |
| `not_assessed_yet` (consumer, the publisher's own change text) | 66 |
| `marked_dash_no_reason` (305 rent, 27 on nine private providers) | 332 |

The same file's other reasons: `marked_dash` 1,140 (change and date cells beside a flagged grade), `publisher_text_none` 573 (the text
"None" in the two descriptive columns, kept verbatim in `null_reasons`), `blank` 3 (the two blank date cells the spec names and one
empty "Other landlords" cell). Judgements, 12 August file (600 NULL grades): 200, 74 and 326 on the same three reasons;
`marked_dash` 1,124, `publisher_text_none` 568, `blank` 3. Enforcement notices: `publisher_text_none` 4 in each edition 2.
Register 2026-07-24: `blank` 1,578 (empty Notes); 2026-09-17: `blank` 1,576. The two real notes (Pivotal, July and September)
survive; August has none.

Register 2026-08-17: 1,578 `notes` cells 'None' to NULL (edition 2). It is the only live value this load changed.

## When a new file appears

From `ONS_Population_Estimates`:

- **Each month, a new register:** `python scripts/s24_rsh_register_editions.py load` (preview, read it whole), then `--simulate`,
  then `--commit`. A new register date is a new period. The next snapshot is expected about 17 to 21 October 2026. A held date
  reissued with other content stops unless `--accept-reissue PERIOD`; a changed row needs `--acknowledge PERIOD`.
- **On each judgements issue:** the same `load`. A new judgements file must carry enforcement notices (a file with none is
  REJECTED).
- **A reissue of a held judgements period:** `load`, then `refresh-latest` (preview, `--simulate`, `--commit`; a key change
  needs `--accept-key-changes PERIOD`).
- Then `status` (clean) and `python scripts/s24_rsh_register_editions_verify.py` (exit 0). The four held workbooks in
  `data/raw/s24_rsh/` (git-ignored) must stay: gates 5, 6, 7, 13 and 17 find them by sha256.
- Known limits: a register reissue that adds or removes a provider can be stored but `refresh-latest` halts (the register spec
  does not allow key changes); a later reissue of 2026-09-17 would fail gate 13 by design until the gate is reviewed.

## How to reverse

`python scripts/s24_rsh_register_editions.py restore-edition SPEC PERIOD N` (preview, then `--commit`) stores edition N's rows as
the next edition; `refresh-latest` then writes it to the live table. For example, restoring edition 1 of the register 2026-08-17
brings back the 'None' notes text. Editions are append-only, so nothing is lost.

## Found while checking, for follow-up (not fixed here)

- **RSH does publish dated de-registrations.** The register page links yearly "Registrations and de-registrations" pages (2021 to
  2026, and older "De-registrations" pages) with dated entries (for example Trafford Housing Trust Limited, L4440, de-registered
  14.08.2026). The old registry caveat that a de-registration has no published date of its own is wrong. It is to be corrected in
  plan task 5 (the registry text and `docs/s24_rsh_register_source.md`). Loading those pages is later work, not in scope.
- `status` before the migration printed "ROW COUNT ... expected 1579" for the August and September register periods. That was a
  pre-migration check and cleared once the editions existed.
