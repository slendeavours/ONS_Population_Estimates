# Quarterly refresh of the homelessness tables (S1 and S1b)

For whoever runs the quarterly update. No programming is needed; every step is
a command, with what to expect and what a failure means. Written 2026-10-07.

Two live tables are covered: `la_statutory_homelessness` (S1) and
`la_homelessness_support_needs` (S1b). Each has an append-only editions table
(`..._editions`) that records every published version of every quarter. The
live table always holds the **latest edition** of each quarter. Why it works
this way is in
[decisions/2026-10-06-s1-edition-history-design.md](decisions/2026-10-06-s1-edition-history-design.md),
[decisions/2026-10-06-s1b-edition-history.md](decisions/2026-10-06-s1b-edition-history.md)
and [decisions/2026-10-07-editions-quarterly-refresh.md](decisions/2026-10-07-editions-quarterly-refresh.md).

Run every command from the repository root. Every writing command is a **dry
run unless you add `--commit`**; `--simulate` does everything including the
safety checks and then rolls back, so it is the rehearsal before `--commit`.
`--commit` and `--simulate` cannot be given together. A command that stops
prints a line starting `HALT:` and writes nothing.

## The two ideas to hold on to

- **Never put a revised figure straight into a live table.** A revision goes in
  as a new edition first, then the refresh copies it into the live table.
- **The refresh only touches quarters whose live rows differ from their latest
  edition.** Inside the same transaction it compares every other quarter before
  and after, and rolls back if any of them changed. S1 also checks the
  workflow 1 signal outputs.

## Step 1. Check the starting state (both tables)

```
python scripts/s1_editions.py status
python scripts/s1b_editions.py status
```

Expected, for each: `status: OK, nothing to do` and exit code 0. If either
says `ACTION NEEDED`, read the line above it and go to the matching case:

| Status line | Meaning | Go to |
| --- | --- | --- |
| `NEW, no editions recorded` | A quarter is in the live table but has no edition 1 (normal straight after a new quarter is loaded). | Step 2 |
| `NEWER EDITION NOT YET IN LIVE` | A later edition exists but the live table still holds an earlier one (normal after loading a revision). | Step 3 |
| `DRIFT` | The live rows differ from the latest edition and match no stored edition: somebody changed the live table directly. | Step 4 |
| `CHAIN ERROR` | An edition's `supersedes` link is broken or forked. Do not load or refresh. Stop and ask for it to be investigated. | stop |
| `ROW COUNT` | A quarter has too few or too many rows (a missing authority, or a missing category for S1b). Counts are worked out from the data, not typed, so the quarter is genuinely short. Stop and investigate the file. | stop |

## Step 2. A NEW quarter has been published

1. **Load it into the live tables as before.** S1: the n8n S1 workflow. S1b:
   `python scripts/s1b_support_needs_build.py --load --period 2026Q1` (use the
   new quarter).

   **Always give `--period` for S1b. Without it the builder reloads all 11
   quarters from the release-page files, overwrites the revised quarters in the
   live table with the older release-page values and resets `loaded_at`.**
   (`--period` can be repeated to load several quarters.)

   The builder only knows the quarters listed in its `RELEASES` dictionary
   (`scripts/s1b_support_needs_build.py`, around line 84, which currently ends
   at `"2025Q4"`). A new quarter needs **one line added to `RELEASES`** before
   `--period 2026Q1` will run (otherwise it halts `unknown period`). The line is
   the release-page slug and the publisher's calendar quarter, in the pattern of
   the others, for example:
   `"2026Q1": ("april-to-june-2026", "2026-06"),`. Check the slug against the
   release page. This is a code change: commit it.
   Downloading a file needs Scott's permission.
2. **Record each new quarter as edition 1 ('as loaded').** For each table:
   ```
   python scripts/s1_editions.py sync-new               # dry run
   python scripts/s1_editions.py sync-new --simulate    # full checks, rolled back
   python scripts/s1_editions.py sync-new --commit
   python scripts/s1b_editions.py sync-new
   python scripts/s1b_editions.py sync-new --simulate
   python scripts/s1b_editions.py sync-new --commit
   ```
   The dry run only lists `periods with no editions`. It is safe to repeat: a
   second run finds nothing. `--simulate` and `--commit` refuse (`HALT`) a
   quarter that is short: one authority missing, one cell missing, or (S1b) a
   different category set from the previous quarter. (The dry run does not
   check; run `--simulate` before `--commit`.) If the category set changed because the publisher
   changed the layout, confirm that against the release notes and add
   `--accept-categories 2026Q1` (S1b only). `--expected-authorities N` is only
   needed the very first time a table has no editions at all; leave it out.
3. **Check:** run both `status` commands again. Expect `OK`.

## Step 3. A published quarter has been REVISED

Never upsert into the live table. The editions machinery does it in this
order.

1. **Get the revised file** into `data/raw/s1b_a3/` (Scott's permission is
   needed for any download). Keep the naming convention: the file name must
   start with the quarter, e.g. `2026Q1_Detailed_LA_202606_revised.xlsx`.
2. **Add it to `scripts/s1_editions_manifest.json`** as one more entry. Both S1
   and S1b read this one file. The fields:

   | Field | What to put |
   | --- | --- |
   | `period` | The quarter, `YYYYQn` (financial-year quarter, as in the live table). |
   | `file` | The file name exactly as saved in `data/raw/s1b_a3/`. |
   | `url` | The publisher's download link for the file. |
   | `release_label` | `registry`. Only `registry` entries load as editions. |
   | `sha256` | The file's checksum. Windows PowerShell: `Get-FileHash -Algorithm SHA256 data\raw\s1b_a3\<file>`; Git Bash: `sha256sum data/raw/s1b_a3/<file>`. Lower case. |
   | `last_modified` | The `Last-Modified` header of the download, as `YYYY-MM-DDTHH:MM:SSZ`: `curl -sI <url>` shows it (convert to UTC). It becomes the edition's informational published date. |

   The loader refuses a file whose checksum does not match the manifest.
3. **S1 first: load the revised file as a new edition** (rehearse first):
   ```
   python scripts/s1_editions.py load --period 2026Q1 --manifest-label registry
   python scripts/s1_editions.py load --period 2026Q1 --manifest-label registry --simulate
   python scripts/s1_editions.py load --period 2026Q1 --manifest-label registry --commit
   ```
   If the quarter has several `registry` entries, `--manifest-label` halts
   with that message; use `--manifest-entry N` instead (N counts from 0).
   The dry run prints the difference from the previous edition: read it. A
   `--commit` inserts one new edition (append-only, cannot be undone) after the
   quarter's own checks pass. The live table is **not** changed yet.
4. **S1: refresh the live table.** `status` now reports `NEWER EDITION NOT YET
   IN LIVE` for the quarter.
   ```
   python scripts/s1_editions.py refresh-latest             # lists the rows it would write
   python scripts/s1_editions.py refresh-latest --simulate
   python scripts/s1_editions.py refresh-latest --commit
   ```
   The dry run must name only the quarter you loaded. If it names others,
   stop: each should be explained by a revision you meant to load. A `HALT`
   with `guard:` means a quarter that was not being refreshed would have
   changed, so everything was rolled back; do not work around it, investigate.
   S1 will also halt if the workflow 1 signal outputs would move for an
   authority whose temporary accommodation figures did not change. A
   `file_url` in `homelessness_quarter_urls` that differs from the edition's
   source URL is printed as a `NOTE` and recorded; it is not a failure (only a
   cell-by-cell difference from the edition's file is).
5. **Then S1b: load the revised file as a new edition.** The S1b load
   cross-checks its support-need totals against the live S1
   `support_needs_total`, so the S1 revision must already be in the live S1
   table (steps 3 and 4) or the S1b load halts at that check.
   ```
   python scripts/s1b_editions.py load --period 2026Q1 --manifest-label registry
   python scripts/s1b_editions.py load --period 2026Q1 --manifest-label registry --simulate
   python scripts/s1b_editions.py load --period 2026Q1 --manifest-label registry --commit
   ```
6. **S1b: refresh the live table**, with the same reading of the dry run as
   in step 4:
   ```
   python scripts/s1b_editions.py refresh-latest
   python scripts/s1b_editions.py refresh-latest --simulate
   python scripts/s1b_editions.py refresh-latest --commit
   ```
7. **Check:** both `status` commands report `OK`.
8. Re-run workflow 1 and re-export the map data as for any other change to S1.

## Step 4. `status` reports DRIFT

Drift means the live rows for a quarter were changed outside the editions
machinery (typically a revised file upserted straight into the live table, or a
hand-run `UPDATE`). The refresh will not hide it: it halts on that quarter.

1. **Find out who changed it and why.** Compare the live rows with the latest
   edition (S1: `python scripts/s1_editions.py diff` compares two stored
   editions; for live against edition, a query on the two tables). Check the
   n8n execution history and recent commits.
2. **Decide:**
   - *The live values are right* (a real revision): put the source file into
     the manifest and load it as an edition (Step 3, items 1 to 3), then
     refresh. The refresh then finds live equal to the new edition.
   - *The live values are wrong:* overwrite them with the latest edition,
     naming the quarter so it is deliberate:
     `python scripts/s1_editions.py refresh-latest --accept-drift 2026Q1 --commit`
     (or the `s1b_editions.py` equivalent). `--accept-drift` for a quarter that is
     not drifted halts.
3. **Check:** `status` reports `OK`.

## Step 5. After any change, run the verification scripts

Run them **one at a time, never together**: their seeded checks take exclusive
locks on the editions tables, so a second one waits or fails.

| Order | Command | Typical run time |
| --- | --- | --- |
| 1 | `python scripts/verify_source_registry.py` | a few seconds |
| 2 | `python scripts/verify_national_ta.py` | under a second |
| 3 | `python scripts/s1_editions_verify.py` | about 1.5 minutes |
| 4 | `python scripts/s1b_support_needs_verify.py` | about 3 minutes |
| 5 | `python scripts/s1b_editions_verify.py` | about 4 minutes (3 min 59 s measured) |

Each exits 0 when everything passes; any `FAIL` exits non-zero. Never run two
at once: a parallel run waits on database locks held by the other (an earlier
figure of about 14 minutes for the last one was that wait, not its real run
time). `verify_national_ta.py`, `s1_editions_verify.py`, `s1b_editions_verify.py`
and `s1b_support_needs_verify.py` leave the tables unchanged (they finish by
rolling back). **`verify_source_registry.py` is different: its gate 9
regenerates the registry notes and commits them.**

## A change in the number of authorities

Counts are derived from the data, so a quarter with a different number of
authorities from the others is refused, not absorbed. If the number of
authorities genuinely changes (a local government reorganisation), there is
**no override flag yet**: handling it is a code change (the derived expected
count and the lad24cd lookups), to be made and reviewed before loading that
quarter.

## What is still manual

- **Spotting that the publisher has revised a quarter.** Nothing detects it
  automatically yet; someone has to re-check each loaded quarter against the
  release page and, if it changed, follow Step 3. Recording it as
  `revision_detected` in `source_check_log` is also by hand.
- **Downloading files.** Each download needs Scott's permission.
- **Editing the manifest** (Step 3, item 2).
- **Loading a brand-new quarter** (Step 2, item 1) still uses the n8n S1
  workflow and the S1b build script; only the recording of edition 1 is
  automated.

Not covered here: the RO4 housing expenditure table, which has no edition
treatment yet (see the open items in [decisions/README.md](decisions/README.md)).
