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

The RO4 housing expenditure table has the same arrangement but is annual and
independent of S1 and S1b; its steps are in
[RO4 housing expenditure](#ro4-housing-expenditure-a-new-release-of-a-financial-year)
below.

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
| 6 | `python scripts/ro4_editions_verify.py` | about 35 seconds (41 checks, measured alone 2026-10-07) |

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

## RO4 housing expenditure: a new release of a financial year

`ro4_housing_expenditure` (S2) has an append-only editions table,
`ro4_housing_expenditure_editions` (key `lad24cd, financial_year, edition`),
and the live table holds the latest edition of each financial year. Why:
[decisions/2026-10-07-ro4-edition-history.md](decisions/2026-10-07-ro4-edition-history.md).
This is independent of S1 and S1b: it can be done before, after or without
them, and nothing in it touches their tables. The map and the W1 national
aggregates read only the latest financial year, so a release of an older year
changes the table but not them.

**When it applies.** MHCLG publishes each financial year of RO4 more than
once. The table holds 2024-25 (edition 1 as loaded, edition 2 the third
release of 11 June 2026) and 2025-26 (edition 1, first release of 17 September
2026). The second 2025-26 release is expected later and is a new edition.
Nothing detects a new release automatically yet: someone has to notice it on
the GOV.UK collection page.

1. **Get the file** into `data/reference/` (the folder is gitignored; Scott's
   permission is needed for any download). Keep the publisher's file name.
2. **Add an entry to `scripts/ro4_editions_manifest.json`.** The fields:

   | Field | What to put |
   | --- | --- |
   | `financial_year` | `2025-26` style. |
   | `file` | The file name exactly as saved in `data/reference/`. |
   | `sheet` | The data sheet's name, e.g. `RO4_LA_Data_202526` (open the workbook to read it). |
   | `release_label` | Plain words, e.g. `second release, published 14 Jan 2027`. You pass this text to `load`. |
   | `published_date` | The publication date shown on the GOV.UK release page, `YYYY-MM-DD`. Informational: it does not decide which edition is latest. |
   | `source_url` | The download link, or `null`. |
   | `sha256` | The file's checksum, lower case. PowerShell: `Get-FileHash -Algorithm SHA256 data\reference\<file>`; Git Bash: `sha256sum data/reference/<file>`. |

   The loader refuses a file whose checksum does not match the manifest.
3. **Check where things stand:** `python scripts/ro4_editions.py status` (exit
   0, `OK`, if there is nothing to do).
4. **Load it as a new edition** (dry run first, then the rehearsal, then the
   real load):
   ```
   python scripts/ro4_editions.py load --financial-year 2026-27 --manifest-label "second release, published 14 Jan 2027"
   python scripts/ro4_editions.py load --financial-year 2026-27 --manifest-label "second release, published 14 Jan 2027" --simulate
   python scripts/ro4_editions.py load --financial-year 2026-27 --manifest-label "second release, published 14 Jan 2027" --commit
   ```
   The dry run prints the difference from the previous edition: authorities
   changed, cells changed per measure, values that become NULL or appear,
   `data_missing` flips both ways, name changes and national sums. **Read it.**
   A published revision can blank an authority that had figures (the 2024-25
   third release did, for eight authorities); decide before `--commit`, which
   inserts one edition that cannot be undone. The live table is **not**
   changed yet. A file already recorded reports `already loaded`; `--supersedes
   N` must equal the latest edition's number.
5. **Refresh the live table** (`status` now says a newer edition is not yet in
   live):
   ```
   python scripts/ro4_editions.py refresh-latest             # lists the rows it would write
   python scripts/ro4_editions.py refresh-latest --simulate
   python scripts/ro4_editions.py refresh-latest --commit
   ```
   The dry run must name only the financial year you loaded. A `HALT` with
   `guard:` means a year that was not being refreshed would have changed and
   everything was rolled back; investigate, do not work around it. If live
   differs from its latest edition without an edition explaining it (drift),
   it halts unless you name the year with `--accept-drift 2025-26`. After a
   refresh of the latest year, re-run workflow 1 and the map export as for any
   new data.
6. **Check:** `python scripts/ro4_editions.py status` reports `OK`, then
   `python scripts/ro4_editions_verify.py` (see Step 5).

**First-ever release of a financial year.** If the live table has no rows for
that year, `python scripts/s2_ro4_load.py apply` loads it (insert only; it
never overwrites). Then `python scripts/ro4_editions.py sync-new` (dry run,
`--simulate`, `--commit`) records the year as edition 1. Use `load`, not
`apply`, for any later release of a year that already has rows. Counts are
derived (296 authorities per year at present); a short year is refused.

**Limitation.** The editions form a chain: each edition supersedes the one
before, and a new edition can only be added at the end. 2024-25 edition 1 is
what the live table held ('as loaded', labelled 'published 18 Sep 2025'); the
first and second releases of 2024-25 are not held and have not been fetched,
and the chain cannot place an earlier release before an existing edition.

## What is still manual

- **Spotting a new RO4 release.** Nothing detects it; see the RO4 section.
- **Spotting that the publisher has revised a quarter.** Nothing detects it
  automatically yet; someone has to re-check each loaded quarter against the
  release page and, if it changed, follow Step 3. Recording it as
  `revision_detected` in `source_check_log` is also by hand.
- **Downloading files.** Each download needs Scott's permission.
- **Editing the manifest** (Step 3, item 2).
- **Loading a brand-new quarter** (Step 2, item 1) still uses the n8n S1
  workflow and the S1b build script; only the recording of edition 1 is
  automated.
