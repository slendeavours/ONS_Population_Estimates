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
run unless you add `--commit`** (the one exception is `ddl`, which creates a
missing editions table when run and is not part of a refresh); `--simulate` does everything including the
safety checks and then rolls back, so it is the rehearsal before `--commit`.
`--commit` and `--simulate` cannot be given together. A command that stops
prints a line starting `HALT:` and writes nothing.

S1, S1b and RO4 now run on one shared editions core (`scripts/editions_core.py`), so the three loaders offer the same commands: `status` (read-only), and `load`, `sync-new` and `refresh-latest`, each a dry run unless given `--commit`. The steps below are unchanged. `python scripts/check_loaders.py` shows which loaders meet the loader standard (see `RULES.md`, section 5).

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
| `NEW, no editions recorded` | A quarter is in the live table but has no edition 1. Normal for S1b straight after a new quarter is loaded; for S1, `load-new` records edition 1 itself, so it means rows were put in some other way. | S1: Step 2a (S1b: Step 2b) |
| `NEWER EDITION NOT YET IN LIVE` | A later edition exists but the live table still holds an earlier one (normal after loading a revision). | Step 3 |
| `DRIFT` | The live rows differ from the latest edition and match no stored edition: somebody changed the live table directly. | Step 4 |
| `CHAIN ERROR` | An edition's `supersedes` link is broken or forked. Do not load or refresh. Stop and ask for it to be investigated. | stop |
| `ROW COUNT` | A quarter has too few or too many rows (a missing authority, or a missing category for S1b). Counts are worked out from the data, not typed, so the quarter is genuinely short. Stop and investigate the file. | stop |

## Step 2. A NEW quarter has been published

Load S1 first, then S1b. The S1b load checks its support-need totals against
the S1 figures, so S1 must already be in. Downloading a file needs Scott's
permission.

### 2a. S1 (statutory homelessness): `load-new`

S1 no longer has an n8n loader. The two n8n steps that used to load it were
retired on 2026-10-07: they stored suppressed figures as zero, read columns by
position, and wrote straight over live rows (see
[decisions/2026-10-07-s1-n8n-loaders-retired.md](decisions/2026-10-07-s1-n8n-loaders-retired.md)).
Do not use the n8n S1 workflow; if either of those steps is run it stops with a
`RETIRED` message. `load-new` reads each column by its header text, keeps a
suppressed figure as blank (NULL) and a real zero as 0, and refuses a quarter
that already exists.

1. **Save the file** (the detailed local-authority tables on the GOV.UK
   homelessness release page) into `data/raw/s1b_a3/`, named
   `<period>_<original name>`, for example `2026Q1_Detailed_LA_202610.ods`.
   The period is the financial-year quarter: April to June 2026 is `2026Q1`,
   July to September `2026Q2`, October to December `2026Q3`, January to March
   2027 `2026Q4`. Give each release its own file name; never save over an
   earlier file.
2. **Open the file's cover or contents sheet** (the first sheet) and note two
   things: the release name (for example "Statutory homelessness: Detailed
   local authority-level tables, April to June 2026, England") and the release
   date ("Released: 29 October 2026"). `load-new` reads the same sheet itself
   and refuses the file if the quarter or date does not agree with what you
   enter next, so a file saved under the wrong quarter is caught.
3. **Add one entry to `scripts/s1_editions_manifest.json`.** All eight fields
   are needed; `load-new` stops and names any that is missing.

   | Field | What to put |
   | --- | --- |
   | `period` | The quarter, `YYYYQn`, as in step 1. |
   | `file` | The file name exactly as saved in `data/raw/s1b_a3/`. |
   | `url` | The GOV.UK download link for the file. It is stored as the source. |
   | `sha256` | The file's checksum, lower case. PowerShell: `Get-FileHash -Algorithm SHA256 data\raw\s1b_a3\<file>`; Git Bash: `sha256sum data/raw/s1b_a3/<file>`. |
   | `release_label` | How the file was found, for example `release page`. |
   | `last_modified` | The `Last-Modified` header of the download as `YYYY-MM-DDTHH:MM:SSZ` (`curl -sI <url>` shows it; convert to UTC). |
   | `release_label_actual` | Free text that **contains the release date exactly as the cover prints it, `D Month YYYY`**, for example `April to June 2026 release, released 29 October 2026`. It becomes the edition's label. |
   | `published_date_actual` | The same date as `yyyy-mm-dd`, for example `2026-10-29`. |

   Select the entry with `--manifest-file <file name>`, or with
   `--manifest-entry N` (N counts from 0).
4. **Run `load-new`**: a dry run, then `--simulate`, then `--commit`.
   ```
   python scripts/s1_editions.py load-new --period 2026Q1 --manifest-file 2026Q1_Detailed_LA_202610.ods
   python scripts/s1_editions.py load-new --period 2026Q1 --manifest-file 2026Q1_Detailed_LA_202610.ods --simulate
   python scripts/s1_editions.py load-new --period 2026Q1 --manifest-file 2026Q1_Detailed_LA_202610.ods --commit
   ```
   The dry run and `--simulate` both run every database check inside a
   transaction that is then rolled back, so they are safe to repeat. `--commit`
   records edition 1 (from the file, with its checksum), writes the live rows
   and marks the quarter as loaded in `homelessness_quarter_urls`, all in one
   transaction (append-only, cannot be undone). It stops, writing nothing, if:
   the quarter already exists (use Step 3 for a revision); the cover sheet
   disagrees with the quarter or the manifest date; the authority count is
   short (it is worked out from the earlier quarters; add
   `--expected-authorities N` only if that cannot be worked out); an authority
   code is not recognised; or a figure re-read straight from the file's cells
   differs from what would be stored (a suppressed figure shown as zero, or a
   published figure missing). Fix the cause and re-run; do not edit the check.

   **What that re-read does and does not prove.** It covers all six stored measures and classifies each cell on its own (its own list of suppression markers; an unknown cell format stops the load), but the column for each A1 and TA1 measure is found by the same header-text reading as the loader, and the authority-code recode is shared; the support-needs column (A3) is read by the S1b reader, which has its own header mapping. So a header renamed by the publisher so that it matches the wrong column would still pass for A1 and TA1: read the dry run's figures against the published sheet.
5. **Check:** `python scripts/s1_editions.py status` should say
   `status: OK, nothing to do`.
6. Re-run workflow 1 and re-export the map data as for any other change to S1.
   Until you do, `python scripts/verify_national_ta.py` fails: it compares the
   stored national figures with the newest live quarter.

### 2b. S1b (support needs)

1. **Load it into the live table.** S1b new quarters still use the build
   script: `python scripts/s1b_support_needs_build.py --load --period 2026Q1`
   (use the new quarter).

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
2. **Record the new quarter as edition 1 ('as loaded').**
   ```
   python scripts/s1b_editions.py sync-new               # dry run
   python scripts/s1b_editions.py sync-new --simulate    # full checks, rolled back
   python scripts/s1b_editions.py sync-new --commit
   ```
   The dry run only lists `periods with no editions`. It is safe to repeat: a
   second run finds nothing. `--simulate` and `--commit` refuse (`HALT`) a
   quarter that is short: one authority missing, one cell missing, or a
   different category set from the previous quarter. (The dry run does not
   check; run `--simulate` before `--commit`.) If the category set changed because the publisher
   changed the layout, confirm that against the release notes and add
   `--accept-categories 2026Q1`. `--expected-authorities N` is only
   needed the very first time a table has no editions at all; leave it out.
3. **Check:** `python scripts/s1b_editions.py status`. Expect `OK`.

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

   **`load` does not read the file's cover sheet the way `load-new` does.** Before
   running it, open the file's cover or contents sheet yourself and check the
   release name and release date against the manifest entry (and against the
   quarter you are revising). A file saved under the wrong name would otherwise
   be stored without question.
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
   editions; for live against edition, a query on the two tables). Check
   recent commits and the database's own logs.
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
| 2 | `python scripts/verify_national_ta.py` | under a second. **Expected to fail between a new quarter's load (or a refresh) and the workflow 1 re-run**; it passes again once workflow 1 has run |
| 3 | `python scripts/s1_editions_verify.py` | about 2 minutes (2 min 6 s measured alone 2026-10-07, 45 checks) |
| 4 | `python scripts/s1b_support_needs_verify.py` | about 3 minutes |
| 5 | `python scripts/s1b_editions_verify.py` | about 4 minutes (3 min 59 s measured) |
| 6 | `python scripts/ro4_editions_verify.py` | about 1 minute 18 seconds (44 checks incl. the cover-sheet gate, measured alone 2026-10-07) |

Each exits 0 when everything passes; any `FAIL` exits non-zero. Never run two
at once: a parallel run waits on database locks held by the other (an earlier
figure of about 14 minutes for the last one was that wait, not its real run
time). `verify_national_ta.py`, `s1_editions_verify.py`, `s1b_editions_verify.py`
and `s1b_support_needs_verify.py` leave the tables unchanged (they finish by
rolling back). **`verify_source_registry.py` is different: its gate 9
regenerates the registry notes and commits them.**

## Step 6. Refresh the map data

After the loads (and the verification above), run:

```
python scripts/refresh_map.py
```

It runs Workflow 1 only if a W1 input was loaded after the latest complete
run (or no complete run exists), then exports the map data, then stops with "Not pushed. Review, then:
python scripts/push.py". `python scripts/refresh_map.py --check` reports
whether the map at git HEAD is behind the database, without changing
anything. Review the export; the push is a separate approved step.

The export checks before it writes: exactly 296 features, each with
`lad24cd`, `la_name` and a Polygon or MultiPolygon geometry, `lad24cd`
unique, and one signals row per feature. If any check fails it writes no
file and `refresh_map.py` exits non-zero. A NULL signal value is not a
failure (RULES.md rule 1).

After changing anything in `sql/w1/`, run `python scripts/w1_run.py`
directly: `refresh_map.py` only looks at when source tables were loaded, so
it treats the latest run as current if no source table changed. W1 runs once
per day, so a source loaded on the same day after that day's W1 run waits
until tomorrow (`w1_run.py` stops with "A completed run already exists for
today", and `refresh_map.py` stops before the export).

Rebuilding W1 on a fresh, empty database is not supported: step 01's
`CREATE TABLE IF NOT EXISTS` for the staging tables is older than the live
`staging_la_signals` and lacks the columns added since, so step 02 would stop.

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
once. The table holds 2024-25 (edition 1 as loaded, edition 2 the second release of
4 December 2025, edition 3 the third release of 11 June 2026) and 2025-26
(edition 1, first release of 17 September 2026). The second 2025-26 release is expected later and is a new edition.
Nothing detects a new release automatically yet: someone has to notice it on
the GOV.UK collection page.

1. **Get the file** into `data/reference/` (the folder is gitignored; Scott's
   permission is needed for any download). **Save it under a release-specific
   name**, e.g. `RO4_LA_Data_2025-26_second_release.ods`. The publisher reuses
   the same file name for every release of a year; saving the second release as
   `RO4_LA_Data_2025-26_data_by_LA.ods` would overwrite the first-release
   original that edition 1's provenance checks re-read.
2. **Read the file's own cover sheet before anything else.** Open the workbook,
   go to the `Front_Page` sheet, and note the release ordinal ("first",
   "second", "third") and the publication date in the sentence that says "...
   release ... which was published on ...". Do not take the release from the
   file name, the download page title or any label in the code: the 2024-25
   second release was once filed as the third from a label in a loader
   constant. Then **add an entry to `scripts/ro4_editions_manifest.json`**.
   The fields:

   | Field | What to put |
   | --- | --- |
   | `financial_year` | `2025-26` style. |
   | `file` | The file name exactly as saved in `data/reference/`. |
   | `sheet` | The data sheet's name, e.g. `RO4_LA_Data_202526` (open the workbook to read it). |
   | `release_label_stored`, `published_date_stored` | What the editions table will say, in plain words, e.g. `second release, published 14 Jan 2027` and `2027-01-14`. |
   | `release_label_actual`, `published_date_actual` | What the cover sheet says, as read in the previous step, e.g. `second release, published 14 January 2027` and `2027-01-14`. Normally the same facts as the stored fields; they differ only to record a stored label that is wrong. |
   | `source_url` | The download link, or `null`. |
   | `sha256` | The file's checksum, lower case. PowerShell: `Get-FileHash -Algorithm SHA256 data\reference\<file>`; Git Bash: `sha256sum data/reference/<file>`. |

   The loader refuses a file whose checksum does not match the manifest, and a
   cover-sheet gate refuses a file whose front page says a different release
   or date from the manifest entry. The stored label is permanent once loaded
   (the table is append-only), so check it before `--commit`.
3. **Check where things stand:** `python scripts/ro4_editions.py status` (exit
   0, `OK`, if there is nothing to do).
4. **Load it as a new edition** (dry run first, then the rehearsal, then the
   real load):
   ```
   python scripts/ro4_editions.py load --financial-year 2025-26 --manifest-file RO4_LA_Data_2025-26_second_release.ods
   python scripts/ro4_editions.py load --financial-year 2025-26 --manifest-file RO4_LA_Data_2025-26_second_release.ods --simulate
   python scripts/ro4_editions.py load --financial-year 2025-26 --manifest-file RO4_LA_Data_2025-26_second_release.ods --commit
   ```
   Select the entry by `--manifest-file` (or `--manifest-entry N`, counting
   from 0): `--manifest-label` is refused as ambiguous when two entries share a
   label, as the two 2024-25 entries do.
   The dry run prints the difference from the previous edition: authorities
   changed, cells changed per measure, values that become NULL or appear,
   `data_missing` flips both ways, name changes and national sums. **Read it.**
   A published release can mark an authority as not reported where an earlier
   one had figures (for 2024-25 the second release did, for eight authorities,
   and the third release supplied them again); decide before `--commit`, which
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
   `python scripts/ro4_editions_verify.py` (see Step 5), which re-checks every
   manifest file's cover sheet.

**First-ever release of a financial year.** If the live table has no rows for
that year, `python scripts/s2_ro4_load.py apply` loads it (insert only; it
never overwrites). Then `python scripts/ro4_editions.py sync-new` (dry run,
`--simulate`, `--commit`) records the year as edition 1. Use `load`, not
`apply`, for any later release of a year that already has rows. Counts are
derived (296 authorities per year at present); a short year is refused.

**Limitation.** The editions form a chain: each edition supersedes the one
before, and a new edition can only be added at the end. The chain records the
order releases were loaded, not the order they were published. 2024-25 edition
1 is what the live table held ('as loaded', labelled 'published 18 Sep 2025');
edition 2 is actually the second release (4 Dec 2025) stored under a wrong
'third release' label, and edition 3 is the third release (11 Jun 2026). The
first release of 2024-25 is not held and has not been fetched, and the chain
cannot place an earlier release before an existing edition. What guards
against a mislabelled file is the manifest and the cover-sheet check, not the
chain.

## S8b Housing Benefit by accommodation type: the monthly check

`la_hb_accom_type_caseload` (S8b, DWP Stat-Xplore) has an append-only editions
table, `la_hb_accom_type_caseload_editions` (key `lad24cd, month, accom_type,
edition`), and the live table holds the latest edition of each month. It is
monthly, independent of S1, S1b and RO4, and reads the Stat-Xplore API, so no
file is downloaded. The API key is read from the environment and is never
printed. Record:
[decisions/2026-10-08-s8b-editions-first-load.md](decisions/2026-10-08-s8b-editions-first-load.md).

Run from `ONS_Population_Estimates`:

1. `python scripts/s8b_hb_editions.py load` (preview). It asks the API which
   months exist, fetches every available month that is not held and is
   later than the earliest held month (so a gap is filled; nothing before the
   first held month is fetched; with nothing held, `--months` must be given),
   plus the latest six held months as a revision check, and says for each
   whether it is new, unchanged or revised. Nothing is written. `status` is
   read-only.
2. Read the preview. A short month (fewer than 296 authorities, or a type
   missing) is refused. Before committing, also stop and look if NULLs appear
   where there were numbers, if any value is negative, or if a revision is
   large on many authorities.
3. `python scripts/s8b_hb_editions.py load --commit` stores a new month as
   edition 1 and inserts its live rows in the same transaction (checked equal
   cell for cell, 296 authorities x 4 types, or the month is rolled back); a
   revised month is stored as the next edition and reaches live only through
   step 4; an unchanged month stores nothing. A month with editions but no
   live rows (`status` reports it as "editions month missing from live") has
   its live rows inserted, with no new edition, when its latest edition equals
   the fetch. Each month is its own transaction. A run that succeeds,
   including one that finds nothing new, writes one `pipeline_run_log` row
   (source `8b`), which is what moves the due date in `vw_source_due`;
   `--simulate` rehearses everything and writes nothing.
4. `python scripts/s8b_hb_editions.py refresh-latest` previews the live rows
   that would change; run it again with `--commit` (`--accept-drift MONTH`
   as for S1). After any `refresh-latest --commit` that wrote rows, run
   `python scripts/w1_run.py` (one run a day) and then
   `python scripts/refresh_map.py`: `refresh-latest` leaves `loaded_at`
   alone, so `refresh_map.py`, which judges staleness by the latest
   `loaded_at`, cannot see a refreshed revision by itself.
5. `python scripts/s8b_hb_editions.py status` should say OK, and
   `python scripts/s8b_hb_editions_verify.py` should pass all 16 gates.

**Full re-check.** `load --recheck-all` rechecks every held month, not just the
latest six. Use it after a long gap, or when DWP is known to have restated a
back series.

**First time on a table with no editions.** The first `sync-new` needs
`--expected-authorities 296`, because with no month recorded the count cannot
be derived; later runs derive it.

**Is S8b current?** Yes when the API's latest month equals the latest month
held. `python scripts/check_sources.py 8b --dry-run` shows the API's newest
month against the registry's `latest_period_loaded` (`no_change` means equal);
without `--dry-run` it records the check in `source_check_log` and sets
`last_check_at`. The registry's `overdue` flag reflects cadence (a month since
the last logged run), not a missed load: the source can be fully current and
still read overdue until a run is logged. Running `load --commit` after the
monthly check logs it.

**A revert.** If DWP restores figures to an earlier edition's content, the
loader stores it as a new, later edition (A, B, A becomes editions 1, 2, 3);
the chain records what was fetched and when, not which edition DWP now
considers right.

## S19 PIP claimants: the monthly check

`la_pip_claimants` (S19, DWP Stat-Xplore, Personal Independence Payment cases
with entitlement) has an append-only editions table,
`la_pip_claimants_editions` (key `lad24cd, month, edition`), and the live table
holds the latest edition of each month. It is monthly, independent of the
other sources, and reads the Stat-Xplore API through the shared
`scripts/statxplore_client.py`, so no file is downloaded. The API key is read
from the environment and is never printed. **Months are `yyyymm` keys**
(`202607`), not the old labels such as `Apr-26`. Record:
[decisions/2026-10-08-s19-month-keys.md](decisions/2026-10-08-s19-month-keys.md).

Run from `ONS_Population_Estimates`:

1. `python scripts/s19_pip_editions.py load` (preview). It asks the API which
   months exist, fetches every available month that is not held and is later
   than the earliest held month (so a gap is filled; nothing before the first
   held month is fetched), plus the latest six held months as a revision check,
   and says for each whether it is new, unchanged or revised. Nothing is
   written. API calls are throttled: rechecking four months took about 9
   minutes, and a run over many months takes longer. `status` is read-only.
2. Read the preview. A short month (fewer than 296 authorities, or a measure
   missing) is refused. Before committing, also stop and look if NULLs appear
   where there were numbers, if any value is negative, or if a revision is
   large on many authorities.
3. `python scripts/s19_pip_editions.py load --commit` stores a new month as
   edition 1 and inserts its live rows in the same transaction; a revised month
   is stored as the next edition and reaches live only through step 4; an
   unchanged month stores nothing. Each month is its own transaction. A run
   that succeeds, including one that finds nothing new, writes one
   `pipeline_run_log` row (source `19`), which is what moves the due date in
   `vw_source_due`; `--simulate` rehearses everything and writes nothing.
4. `python scripts/s19_pip_editions.py refresh-latest` previews the live rows
   that would change; run it again with `--commit`. After any
   `refresh-latest --commit` that wrote rows, run `python scripts/w1_run.py`
   (one run a day) and then `python scripts/refresh_map.py`: `refresh-latest`
   leaves `loaded_at` alone, so `refresh_map.py` cannot see a refreshed
   revision by itself.
5. `python scripts/s19_pip_editions.py status` should say OK, and
   `python scripts/s19_pip_editions_verify.py` should pass all 19 gates.

**Full re-check.** `python scripts/s19_pip_editions.py load --recheck-all`
rechecks every held month, not just the latest six. Use it after a long gap,
or when DWP is known to have restated a back series. Whether PIP is ever
revised is **not established** (two months were compared once, a week after
their first load, and did not differ); the recheck on every load is what builds
the evidence.

**First time on a table with no editions.** The first `sync-new` needs
`--expected-authorities 296`, because with no month recorded the count cannot
be derived; later runs derive it.

**Is S19 current?** Yes when the API's latest month equals the latest month
held. The API's latest month appears in the `load` preview and in
`python scripts/check_sources.py 19 --dry-run` (which compares it with the
registry's `latest_period_loaded`); `status` makes no API call and reports the
held months and whether anything needs action. The
registry's `overdue` flag reflects cadence, not a missed load.

**Never run the old loader.** `scripts/historical/s19_pip_build.py` writes text
month labels and would undo the `yyyymm` keys.

## S15 House prices: the monthly check

`la_house_prices` (S15, HM Land Registry / ONS UK House Price Index) has an
append-only editions table, `la_house_prices_editions` (key
`lad24cd, period, edition`), and the live table holds the latest edition of
each month. It is monthly and independent of the other sources. Every release
republishes the full back series and the July 2026 comparison showed recent
months are revised (14 of 55), so every `load` compares every held month.
Record: [decisions/2026-10-08-s15-editions-first-load.md](decisions/2026-10-08-s15-editions-first-load.md).

Run from `ONS_Population_Estimates`:

1. `python scripts/s15_hpi_editions.py load` (preview). It reads the
   collections page, downloads the two newest CSVs to `data/raw/`, checks that
   they name the same edition and compares every held month cell by cell
   (new, unchanged or revised, with area and cell counts). Nothing is written.
   To work from files you already have, add `--avg-prices FILE` and
   `--property-type FILE` (offline).
2. Read the preview. Stop and look if a month has fewer than 295 authorities, a
   NULL replaces a number in more than five areas, a price is zero or
   negative, more than ten areas of a month revise by over 50%, or an area code
   is unresolved. The loader refuses short months and unresolved codes.
3. `python scripts/s15_hpi_editions.py load --commit` stores a new month as
   edition 1 with its live rows in one transaction, and a revised month as the
   next edition. A run that succeeds writes one `pipeline_run_log` row
   (source `15`); `--simulate` rehearses everything and writes nothing.
4. If any month was revised, `python scripts/s15_hpi_editions.py
   refresh-latest` previews the live rows that would change; run it again with
   `--commit`. After any `refresh-latest --commit` that wrote rows, run
   `python scripts/w1_run.py` (one run a day) and then
   `python scripts/refresh_map.py`: `refresh-latest` leaves `loaded_at` alone,
   so `refresh_map.py` cannot see a refreshed revision by itself.
5. `python scripts/s15_hpi_editions.py status` should say OK, and
   `python scripts/s15_hpi_editions_verify.py` should pass all 20 gates.

**First time on a table with no editions.** The first `sync-new` needs
`--expected-authorities 295`, because with no month recorded the count cannot
be derived; later runs derive it.

**Never run the old loader.** `scripts/historical/s15_hpi_build.py` inserted new
months only and discarded revisions of months already held; it now stops with a
RETIRED message.

**An older file halts.** `load` stops, with nothing written, if the file's
edition or latest month is earlier than what is held (for example last month's
file passed by mistake). `--allow-older-file` overrides it for a deliberate
re-load.

## S18 Private rents: the monthly check

`la_private_rents` (S18, ONS Price Index of Private Rents) has an append-only
editions table, `la_private_rents_editions` (key
`lad24cd, period, breakdown_type, category, edition`), and the live table holds
the latest edition of each month. It is monthly and independent of the other
sources. Every workbook republishes the full back series and the latest month
is provisional, so every `load` compares every held month. The first
comparison (2026-10-09) found no change; revision in the held months is
expected but not yet observed. Record:
[decisions/2026-10-09-s18-editions-first-load.md](decisions/2026-10-09-s18-editions-first-load.md).

Run from `ONS_Population_Estimates`:

1. `python scripts/s18_pipr_editions.py load` (preview). It reads the dataset
   page, downloads the newest workbook to `data/raw/`, checks that its file
   name, Cover sheet and latest month agree, and compares every held month
   cell by cell (new, unchanged or revised, with a count of rows changed: one
   row per area and breakdown block, so 2,646 rows is all 294 areas, not 2,646
   areas). Nothing is written. If a file of that edition name is already in
   `data/raw/` with different content it is never replaced: the new download
   is saved beside it as `pipr_<edition>-<sha8>.xlsx`, the message says so, and
   the new one is the file compared. To work from a file you already have, add
   `--file PATH` (offline).
2. Read the preview. A month with fewer than 294 areas or a missing breakdown
   block, a NULL replacing a number in more than five areas, a zero or
   negative rent, a revision above 50% on more than ten areas, an unresolved
   area code, or a Barnsley and Sheffield form that disagrees with
   `scripts/geography.py` is rejected: nothing is stored for that month. A
   workbook older than what is held halts; `--allow-older-file` overrides it.
3. `python scripts/s18_pipr_editions.py load --commit` stores a new month as
   edition 1 with its live rows in one transaction, and a revised month
   (including provisional to final) as the next edition. A run that succeeds
   writes one `pipeline_run_log` row; `--simulate` rehearses and writes
   nothing.
4. If any month was revised, `python scripts/s18_pipr_editions.py
   refresh-latest` previews the live rows that would change; run it again with
   `--commit`.
5. `python scripts/s18_pipr_editions.py status` should say OK, and
   `python scripts/s18_pipr_editions_verify.py` should pass all 23 gates.

**First time on a table with no editions.** The first `sync-new` needs
`--expected-areas 294`, because with no month recorded the count cannot be
derived; later runs derive it.

**The map and W1.** `la_private_rents` is not read by Workflow 1 (`sql/w1/`),
by `scripts/refresh_map.py` or by `scripts/export_map_data.py` (checked by
search on 2026-10-09; the registry has `publish_map` false, since raw rent
levels do not go on the demand map). No W1 run or map refresh is needed after
an S18 load, including after `refresh-latest --commit`. If a future source or
export starts to read it, this paragraph needs changing.

**Text rule.** Repeated spaces in a text cell collapse to one (ODS `<text:s/>`),
as in the retired loader; this is deliberate, so held rows and joins stay stable.

**Rechecks of July, August and September 2026 are refused.** Location
1-28257167158 is kept unresolved in those snapshots as loaded but postcodes.io
now resolves it, so the set differs because the mapping changed, not the file.
The held editions stay as loaded; do nothing unless CQC reissues one of those
files, which needs the engine's whole-snapshot replace first.

**Never run the old scripts.** `scripts/historical/s18_pipr_fetch.py`,
`s18_pipr_transform.py`, `s18_pipr_load.py` and `s18_pipr_verify.py` upserted
rows in place (`ON CONFLICT DO UPDATE`) from 2026-07-12 until commit ab14ef9
(2026-10-01), so the 22 July and 19 August 2026 loads overwrote held rows; after
that they inserted new rows only and ignored revisions of rows already held
(`ON CONFLICT DO NOTHING`). They now stop with a RETIRED message.

## S9 Discharge delays (S9a) and mental health delayed discharge (S9b): the monthly check

`nhs_drd_discharge_delays` (S9a, NHS England Discharge Ready Date, by upper-tier
authority) and `nhs_mh_crfd` (S9b, MHSDS measure MHS26, by local authority) each
have an append-only editions table (`..._editions`) and a ledger of every file
read (`..._editions_file_checks`). The live table holds the latest edition of
each month. Both publish one file per month. Records:
[S9a](decisions/2026-10-09-s9a-editions-first-load.md),
[S9b](decisions/2026-10-09-s9b-editions-first-load.md).

Run from `ONS_Population_Estimates`, for each of `s9a_drd_editions.py` and
`s9b_crfd_editions.py`:

1. `python scripts/<loader> load` (preview). It reads the publication pages,
   picks each month's current file (S9a: a `-Revised` webfile beats the
   original; S9b: Final beats Performance, then the higher `vN`), skips files
   already in the ledger and previews new, unchanged and revised months.
   Nothing is written to the database; downloads go to `data/raw/s9a_drd/` or
   `data/raw/s9b_mhsds/` (an S9b preview can read around 40 files and take a
   couple of minutes).
2. Read the preview. A month with other than the expected number of areas, an
   unexpected code, a negative value, a NULL replacing a number in more than
   five areas, a value revised by more than 50% in more than ten areas (S9a:
   total bed days), a Barnsley and Sheffield form that disagrees with
   `scripts/geography.py`, a 0 to NULL change (or the reverse) or an older file
   than held (page link or `--file`) is rejected and nothing is stored for that
   month. `--allow-older-file` overrides the older-file stop. `--acknowledge
   PERIOD` releases every stop condition of that month that can be released:
   the 0/NULL change, the NULL-in-more-than-five and the more-than-50% stops
   together, after you have read the whole preview (it prints each message it
   released). It does not release the structural checks (area count, missing
   area, code, negative, percentage, duplicate key, file identity, Barnsley and
   Sheffield form). S9b Performance-to-Final changes are large by nature (100
   to 175 changed areas a month), so only a move of the national total by more
   than 50% blocks, and `--acknowledge` releases that too.
3. `python scripts/<loader> load --commit` stores a new month as edition 1 with
   its live rows in one transaction, and a revised month as the next edition.
4. If any month was revised, `refresh-latest` previews, then run it again with
   `--commit`; this also moves live `source` to the new file URL.
5. `status` should say OK, and `python scripts/s9a_drd_editions_verify.py` and
   `python scripts/s9b_crfd_editions_verify.py` should each pass all 24 gates.

`load --recheck-all` reads every held month again. Do it occasionally for S9a,
whose publisher revises in waves (the files carry a `Revised:` date on the
cover sheet).

**When revisions arrive.** S9a: a wave each July (2025-07-10, 2026-07-09), so
run `load --recheck-all` in July. S9b: the year-end Final files for the
financial year just ended appear around September to spring; the 2026-27 Finals
are expected in spring 2027. Check in September for any Finals and v-number
reissues, which the ordinary `load` finds by itself.

**First time on a table with no editions.** The first `sync-new` needs
`--expected-areas 153` (S9a) or `--expected-areas 296` (S9b).

**The map and W1.** W1 reads the latest month only. Run `w1_run.py`, then
`refresh_map.py`, only if the latest month changed (a new month was loaded);
a Final revision of an older month changes no ranking and needs neither. Note
that a newer latest month changes `drd_bed_days_lost`,
`drd_pct_delayed_1plus_days` and `crfd_days`.

**Never run the old scripts.** `scripts/historical/s9a_drd_build.py`,
`s9b_crfd_build.py`, `verify_load_drd.py` and `verify_load_crfd.py` upserted rows
in place (`ON CONFLICT DO UPDATE`), so loading a republished file overwrote held
rows (as far as `loaded_at` and the files show, no held month was reloaded from
a different file after its first load; the one exception is that the held S9b
Barnsley/Sheffield rows were re-keyed in place on 2026-08-14 and 2026-10-01,
which left `loaded_at` unchanged). The S9b build took the file URL and period on
the command line and had no Final filter of its own; the year-end Final files
were kept out by the discovery in `verify_load_crfd.py` and the n8n-era load. The two `verify_load_*` scripts loaded June
2026 on 2026-08-20 the same way. They now stop with a RETIRED message.

## S11 CQC register of care locations: the monthly check

`cqc_location_snapshots` holds one snapshot of the CQC register of Adult social
care locations per monthly file (the period is the "as at" date on the file's
README sheet), with an append-only editions table
(`cqc_location_snapshots_editions`), a ledger of every file read
(`cqc_location_snapshots_editions_file_checks`) and an append-only unresolved
table. `cqc_locations` is a view over the snapshots that W1 and the map read.
Record: [S11](decisions/2026-10-09-s11-editions-first-load.md).

Run from `ONS_Population_Estimates`, monthly (CQC publishes in the first days of
the month; the page label can be a day after the file's own date):

1. `python scripts/s11_cqc_editions.py load` (preview). It finds the filters
   file on the CQC page, downloads it to `data/raw/` (also in a preview; rows
   without coordinates are looked up on postcodes.io), reads the README
   identity and prints the snapshot, what changed against the previous one and
   the unresolved locations. Nothing is written to the database.
2. Read the preview. A snapshot is REJECTED (nothing stored, exit 1) on an
   identity mismatch, a header change, an unexpected marker, a Barnsley or
   Sheffield code in the file, a location over 2 km from every polygon, rows
   more than 2% from the previous snapshot, more than 1,000 locations gone or
   new, more than 10 unresolved, an authority with no location, supported
   living over 50% in more than 5 authorities, or an older file than held (page
   or `--file`; `--allow-older-file` overrides).
3. `python scripts/s11_cqc_editions.py load --commit` stores the snapshot, its
   live rows, its unresolved rows and the ledger row in one transaction.
4. `status` should say OK, and `python scripts/s11_cqc_editions_verify.py`
   should pass all 22 gates.
5. There is no `refresh-latest` step unless a recheck stored a second edition
   of a held snapshot (`load --recheck PERIOD`); then preview it, and run it
   again with `--commit`.
6. After a new snapshot, run `w1_run.py`, then `refresh_map.py`: W1 counts
   supported-living locations from the latest snapshot, so the next run moves
   `supported_living_locations`. Neither is run by the loader.

**Never run the old scripts.** `scripts/historical/s11_cqc_fetch.py`,
`s11_cqc_process.py`, `s11_cqc_map.py`, `s11_cqc_load.py` and `s11_cqc_verify.py`
now stop with a RETIRED message. The load step upserted with `INSERT ... ON
CONFLICT (location_id) DO UPDATE`, so each new file replaced the row of every
location it contained (locations that dropped out of a file kept their earlier
values). The July and August 2026 snapshots were rebuilt from the files on
2026-10-09, and the residue rows (128 July, 108 August) also remain in
`cqc_locations_legacy`; the map step wrote
`cqc_unresolved_locations` on every run with no preview; the fetch step
overwrote a same-named download and took the date from the file name.

## S4 DfE care leaver accommodation: each November

`care_leaver_accommodation` is the latest-edition layer of
`care_leaver_accommodation_editions` (one release's figures for one reporting
year, both cohorts of that release: 17-21 accommodation and 22-25
suitability), with a ledger of every file read
(`care_leaver_accommodation_editions_file_checks`). DfE publishes once a year,
in November, for the year ending 31 March, and restates earlier years each
time. Record: [S4](decisions/2026-10-09-s4-editions-first-load.md). Source
note: [s4_care_leaver_source.md](s4_care_leaver_source.md).

Run from `ONS_Population_Estimates`, once a year (`check_sources.py 4` says
when a new release is out):

1. `python scripts/s4_care_leaver_editions.py load` (preview). It reads the
   latest release from the content API, the two dataset ids from the release's
   data guidance page, downloads both files to `data/raw/s4_cla/` (also in a
   preview) and prints, per year, whether it is new, revised, unchanged or
   skipped as older than the tip, and the cells that change. Nothing is
   written to the database.
2. Read the preview. A year is REJECTED (nothing stored, exit 1) on an
   identity or header mismatch, an unknown marker (`k`, a blank), a Barnsley
   or Sheffield new code, an authority that cannot be resolved or declared, an
   authority with a published number missing from the release, a revised year
   whose `total_published` moves by more than 25% in more than three
   authorities or whose national total moves by more than 5%, or a revised year
   covered for fewer cohorts than its tip holds (a file for one age group
   alone never replaces a year whose latest edition holds the other).
   An older release than the tip is not rejected: its years are skipped as
   older, a mixed run carries on with the newer years, and a run of only
   older years halts with exit 1 (also with `--file-17-21`/`--file-22-25`;
   `--allow-older-file` overrides).
   About 100 cells a release go between 0 and NULL (rule 1.10): after reading
   them, repeat the command with `--acknowledge YEAR` for each year.
3. `load --commit` (with the same `--acknowledge` options) stores new years as
   edition 1 with their live rows, and revised years as the next edition; each
   year's edition, ledger rows (and live rows, for a new year) go in one
   transaction. A revised year reaches the live table through step 4.
4. `refresh-latest` (preview), then `refresh-latest --commit` to copy the
   latest editions into the live table. If a year gains or loses authorities
   the preview lists them and the command halts until that year is named:
   `refresh-latest --commit --accept-key-changes YEAR` (repeat for each year).
5. `status` should say OK and `python scripts/s4_care_leaver_editions_verify.py`
   should pass all 23 gates.
6. Run `w1_run.py` and `refresh_map.py` **only if W1's input changed**: a
   new latest reporting year (a November release normally brings one) or a
   revision of the latest year. `refresh_map.py --check` is the test. W1
   reads `semi_independent_published`, 17-21, for the latest year.

**November 2026** (reporting year 2026): 2026 is a new year; 2022 to 2025 are
republished as revised editions; the dataset ids, titles and header schema may
change (the loader halts and lists the columns); Barnsley and Sheffield may
arrive as E08000038/39 (the loader halts, and `DATASET_FORM['4']` in
`scripts/geography.py` is then corrected deliberately). If the page's embedded
JSON changes shape, give the ids by hand:
`load --release YYYY --dataset-17-21 ID --dataset-22-25 ID` (every file check
still applies).

To go back for a year: `restore-edition YEAR N` (preview by default) stores
edition N's rows as the next edition, then `refresh-latest --commit
--accept-key-changes YEAR`. Nothing is deleted from the editions.

**Never run the old rebuild.** `scripts/historical/s4_rebuild_care_leavers.py`
(formerly `scripts/verify/rebuild_care_leavers.py`) now stops with a RETIRED
message. It upserted with `INSERT ... ON CONFLICT ... DO UPDATE`, setting
`loaded_at`, so it overwrote the 17-21 rows already held (it ran at least
twice on 2026-08-20), added suppressed cells as 0, resolved codes through
every `la_code_lookup` row (which recoded Bournemouth and Poole 2019 to BCP)
and recreated its backup table on every run.

## S22 MHCLG Council Taxbase and Live Table 615: each November, and each Table 615 update

`la_council_taxbase_empties` and `la_ctb_exemption_classes` are the latest-edition layers of `*_editions` tables (one Council Taxbase workbook's statement about one taxbase year; main and classes always together, with a ledger of every file read), and `la_vacant_dwellings_615` is the layer of `la_vacant_dwellings_615_editions` (one Table 615 file's statement about one year). MHCLG publishes the Council Taxbase in November and revises the latest year in the following January to May; Table 615 is re-issued two or three times a year (next update November 2026 to February 2027). Record: [S22](decisions/2026-10-09-s22-editions-first-load.md). Source note: [s22_source_structure.md](s22_source_structure.md).

Run from `ONS_Population_Estimates`. `check_sources.py 22` detects a new Council Taxbase release; nothing detects a new Table 615 file.

1. Council Taxbase, each November: `python scripts/s22_ctb_editions.py load` (preview). It finds the newest `Council Taxbase <yyyy> in England` release and its one local authority level attachment through the GOV.UK content API, downloads it to `data/raw/s22_ctb/` (also in a preview; the database is not written), reads the year and release dates from the workbook's own cover, and prints what is new, revised, unchanged or skipped as older. Read the whole preview. A year is REJECTED (nothing stored, exit 1) on an identity, marker, reconciliation or geography failure, or when a new year does not have 296 authorities or loses one the held year had; a revised year is rejected when a national total moves by more than 2% or more than 30 authorities change. An older file than the tip is skipped, and a run of only older files halts (`--allow-older-file` overrides). A 0 to NULL or NULL to 0 flip needs `--acknowledge YEAR`. `--release YYYY` and `--file PATH` take other files with the same checks.
2. `load --commit` stores a new year as edition 1 with its live rows (main and classes) and ledger row in one transaction, and a revised year as the next edition (main and classes together).
3. A revised workbook (January to May): the same `load`, then `refresh-latest --part ctb` (preview) and `refresh-latest --part ctb --commit`, which copies the edition into the live tables with the time the edition was stored as `loaded_at`. **Run `refresh-latest --commit` in the same session as `load --commit`, before any W1 run.** If W1 ran in between, the copied `loaded_at` is earlier than that W1 run and `refresh_map.py --check` calls the map current while it still shows the unrevised year.
4. Table 615, each update: `python scripts/s22_ctb_editions.py load-615` (preview), `load-615 --commit`, then `refresh-latest --part 615` and `--commit`. Every year from 2004 is read; a changed back year is a new edition of that year, a new year is a new period.
5. `status` should say OK and `python scripts/s22_ctb_editions_verify.py` should pass all 25 gates.
6. Run `w1_run.py` and `refresh_map.py` **only when the latest taxbase year or its values changed**: Council Taxbase 2026 arrives in November 2026 and becomes W1's year; a revision of the latest year reaches W1 through `refresh-latest`. A Table 615 update does not change W1's input. `refresh_map.py --check` is the test.

**November 2026:** `taxbase_year` 2026 is a new period (edition 1), Table 615 gains a 2026 column and may revise earlier years, and Barnsley and Sheffield are expected as E08000038/39 in both. Table numbers inside the workbook can shift; the loader finds each block by number and title and halts naming what it saw, and the block map is then corrected deliberately.

To go back for a period: `restore-edition ctb|615 PERIOD N` (preview by default) stores edition N's rows as the next edition, then `refresh-latest --commit`. Nothing is deleted from the editions.

**Never run the old scripts.** `scripts/historical/s22_ctb_discover.py`, `s22_ctb_empties_build.py`, `s22_run.py` and `s22_verify.py` now stop with a RETIRED message. They upserted with `INSERT ... ON CONFLICT ... DO UPDATE`, setting `loaded_at`, wrote when run with no preview, and the load ran once, on 2026-08-13 (run-log id 83); the verify step was run at least twice and its duplicate run-log row (id 84) was deleted.

## S23 RSH registered provider social housing stock by local authority: each autumn

`rsh_rp_stock_by_la` is the latest-edition layer of `rsh_rp_stock_by_la_editions` (one look-up tool file's statement about one stock date, 31 March; a file-check ledger, `rsh_rp_stock_by_la_editions_file_checks`, records every file read). The Regulator of Social Housing publishes once a year, in autumn; the next release is **27 October 2026, 09:30** (stock at 31 March 2026, a new period `2026-03-31`). The publisher has no scheduled revisions, but a look-up tool can be reissued (the 2025 tool was reissued as version 1.1 in November 2025). Record: [S23](decisions/2026-10-09-s23-editions-first-load.md). Source note: [s23_rsh_stock_source.md](s23_rsh_stock_source.md).

Run from `ONS_Population_Estimates`. `check_sources.py 23` reads the collection and the table's latest period; nothing runs the load for you.

1. Each autumn release: `python scripts/s23_rsh_stock_editions.py load` (preview). It finds the newest `Registered provider social housing stock and rents in England <yyyy> to <yyyy>` release in the GOV.UK collection and its one `Registered providers look-up tool` attachment, downloads it to `data/raw/s23_rsh/` (also in a preview; the database is not written), reads the file's identity from the workbook (title year, source line, publication month, version, Version History) and prints what is new, revised, unchanged or skipped as older. Read the whole preview. `--release Y1-Y2` and `--file PATH` take other files with the same checks.
2. `load --commit` stores a new stock date as edition 1 with its live rows and ledger row in one transaction, and a revised stock date as the next edition.
3. A reissued tool for a held stock date: the same `load`, then `refresh-latest` (preview) and `refresh-latest --commit`, which copies the edition into the live rows and sets the five provenance columns on every row of the period. If the preview shows providers added or dropped, add `--accept-key-changes YYYY-MM-DD` (the preview lists the keys).
4. `status` should say OK and `python scripts/s23_rsh_stock_editions_verify.py` should pass all 21 gates.
5. An identity, header, value, reconciliation or geography failure is a halt (exit 1, nothing stored, no ledger row, no run-log row). A stock date is REJECTED (nothing stored, no ledger row, exit 1, partial run-log row on `--commit`) for a new stock date when fewer than 296 authorities, the national total moves more than 5% (supported housing more than 10%) or any authority's total moves more than 25%; for a revision when a national stock total moves more than 1%, more than 30 authorities' totals change, an authority moves more than 10% or more than 5% of provider rows are added or removed (a reissued file that drops 5% or fewer providers passes; `refresh-latest` then needs `--accept-key-changes`). An older file than the one held is skipped and a run of only older files halts (`--allow-older-file` overrides).
6. **Halts are fixed deliberately, with evidence, in a small commit, then the load is re-run.** A header change in `STOCK_BY_LA` (the 2024 file had different headers; the 2026 file may): add an alias map with the evidence. Barnsley and Sheffield as E08000038 and E08000039 (the file describes 31 March 2026, after the 1 April 2025 change): `geography.DATASET_FORM['23']` to `mixed` with the evidence (2025 old, 2026 new, one form per period). A release title or attachment that no longer fits (no or several look-up tools): fix the match, naming what the page showed. Discovery halts, exit 1, naming the titles it saw, when the collection lists anything that looks like the series (any wording, e.g. "Registered providers social housing ...", "2025-26", "2025/26") with a later year or no year but does not fit the strict title; `--release` does the same and never falls back to another title.
   **"Nothing newer than the held release" is not proof there is nothing new.** The preview says which release it saw as newest and that none newer is listed. On or after the announced date (`ANNOUNCED_RELEASES` in the loader: 27 October 2026; with no announced date held, the annual cadence gives 1 December of the stock year, derived from the newest release listed, so the warning works every cycle) it adds a WARNING: the release may not be listed yet, or discovery is missing it. Check the GOV.UK collection by hand and, if the tool is there, download it and use `--file PATH`. A blank stock cell halts (the publisher documents no marker): read the cell in the file before deciding anything.
7. S23 is not a W1 or map input, so `refresh_map.py` is not needed. If it is ever wired into W1, run `refresh-latest --commit` in the same session as `load --commit` and before W1: `refresh-latest` copies the edition's time into the live `loaded_at`, and a W1 run in between would hide the revision from `refresh_map.py --check`.

To go back for a stock date: `restore-edition YYYY-MM-DD N` (preview by default) stores edition N's rows as the next edition, then `refresh-latest --commit`. Nothing is deleted from the editions.

**Never run the old scripts.** `scripts/historical/s23_rsh_stock_build.py` and `s23_rsh_stock_verify.py` now stop with a RETIRED message. The build upserted with `INSERT ... ON CONFLICT ... DO UPDATE`, setting `loaded_at`, wrote when run with no preview, read the edition from one hard-coded release page and was run once, on 2026-08-14 (run-log id 95).

## S6 Home Office asylum support by local authority: each quarter

`la_asylum_support`, `la_asylum_support_unallocated`, `asylum_support_non_england` and `la_immigration_groups` are the latest-edition layers of four `*_editions` tables (one file's statement about one quarter; each has a file-check ledger, `*_editions_file_checks`, that records every file read). Asy_D11 (a time series that restates every quarter since 2014) feeds the first three, applied together one quarter at a time; Reg_02 (one snapshot per file) feeds the fourth. The Home Office publishes quarterly on Thursdays at 09:30; the next release is **26 November 2026** (year ending September 2026, a new period `2026-09-30`). Record: [S6](decisions/2026-10-10-s6-editions-first-load.md). Source note: [s6_asylum_source.md](s6_asylum_source.md).

Run from `ONS_Population_Estimates`. `check_sources.py 6` reads the newest "year ending" in the data tables page's change notes; nothing runs the load for you. Downloading a file needs Scott's permission, and a preview downloads.

1. Each quarterly release: `python scripts/s6_asylum_editions.py load` (preview). It finds the newest Asy_D11, Asy_D09 and Reg_02 releases on their GOV.UK pages through the content API, downloads them to `data/raw/s6_asylum/` (also in a preview; the database is not written), prints which release it saw as newest for each, reads each file's identity from its own cover sheet and checks the reconciliations (pivot cache, Asy_D09, Reg_02 against Asy_D11). It then prints what is new, revised, unchanged or skipped as older, with the NULL, `*` and zero counts per column. Read the whole preview. `--release "Month YYYY"` and `--file PATH` (with `--d09-file PATH` or `--no-d09` for Asy_D11) take other files with the same checks.
2. `load --commit` stores a new quarter as edition 1 with its live rows and ledger rows (the three Asy_D11 tables in one savepoint) and a restated quarter as the next edition. Run `status` and `python scripts/s6_asylum_editions_verify.py` (22 gates) afterwards.
3. A restatement of a held quarter: the same `load`, then `refresh-latest` (preview) and `refresh-latest --commit`, which copies the edition into the live rows and sets `source_edition` on every row of the quarter. If the preview lists authorities added or dropped, add `--accept-key-changes YYYY-MM-DD`. A restatement beyond the stop thresholds is REJECTED until the period is named with `--acknowledge YYYY-MM-DD`, after reading what moved; a partial file is never released. In the small unallocated and non-England tables, fewer rows than the held edition (a publisher reassigning an `Unknown` row) is a key change that `--acknowledge YYYY-MM-DD` releases; a Reg_02 back-fill of a quarter earlier than the newest held one halts unless `--allow-older-file` is given.
4. A Reg_02 file reissued under the same cover (the publisher's November 2025 reissues kept their old cover dates): the load stops on equal rank with different content. Read the page's change note, then `load --only reg02 --file PATH --accept-reissue YYYY-MM-DD`.
5. What a halt means: an older file (by its own cover) is skipped per period and the run halts if every period is older (`--allow-older-file` overrides and is logged). A blank, zero or text `People` cell in Asy_D11 halts; a Reg_02 cell other than a whole number or `*` halts; a Reg_02 cell going between 0 and `*` needs `--acknowledge`. A changed header, a new support or accommodation type, or an unexplained code is **fixed deliberately, with evidence, in a small commit, then the load is re-run.** Barnsley and Sheffield switch from E08000016/19 to E08000038/39 by publication (Asy_D11 from December 2025, Reg_02 from June 2026): `geography.DATASET_FORM['6']` is `mixed`, one form per table per period.
   **"Nothing newer than the held release" is not proof there is nothing new.** The preview says which release it saw as newest. After the held file's "Next update" date (26 November 2026) it prints a WARNING if nothing newer is listed: the release may not be listed yet, or discovery is missing it. Check the GOV.UK pages by hand and use `--file PATH`. The data tables page lists only the newest Asy_D11, so a missed quarter has to come from an archived copy.
6. S6 is not a W1 or map input, so `refresh_map.py` is not needed. If it is ever wired into W1, run `refresh-latest --commit` in the same session and before W1: refresh copies the edition's time into the live `loaded_at`, and a W1 run in between would hide the revision from `refresh_map.py --check`.

To go back for a quarter: `restore-edition TABLE YYYY-MM-DD N` (preview by default; TABLE is a live table name) stores edition N's rows as the next edition, then `refresh-latest --commit`. Nothing is deleted from the editions.

**Never run the old scripts.** `scripts/historical/s6_asylum_build.py` and `s6_asylum_verify.py` now stop with a RETIRED message. The build upserted every period of the file on every run (`INSERT ... ON CONFLICT ... DO UPDATE`, setting `loaded_at`), wrote when run with no preview, took the edition from link text, and was run 15 times (run-log ids 69 to 82 and 98).

## S10 MHCLG rough sleeping snapshot: each winter

`la_rough_sleeping` is the latest-edition layer of `la_rough_sleeping_editions` (one snapshot file's statement about one snapshot year; a file-check ledger, `la_rough_sleeping_editions_file_checks`, records every file read). The snapshot is taken each autumn and the release follows the next February: the autumn 2025 release was published 26 February 2026 and its Cover gives "Next Release: Winter 2026/2027", so the next is the autumn 2026 snapshot, a new period `2026`. No revision of a back year has been seen (the autumn 2024 and autumn 2025 files agree on every local authority cell for 2010 to 2024). Record: [S10](decisions/2026-10-10-s10-editions-first-load.md). Source note: [s10_rough_sleeping_source.md](s10_rough_sleeping_source.md).

Run from `ONS_Population_Estimates`. `check_sources.py 10` reads the Homelessness statistics collection for the newest autumn release; nothing runs the load for you. Downloading a file needs Scott's permission, and a preview downloads.

1. `python scripts/s10_rough_sleeping_editions.py load` (preview). It finds the newest "Rough sleeping snapshot in England: autumn YYYY" release in the collection and its one "- tables" attachment, downloads it to `data/raw/s10_rough_sleeping/` (also in a preview; the database is not written), reads the file's identity from the Cover and `Table_1_Total` (rule 3), checks that the authorities sum to the England and region rows, and prints the new year and every held year compared. Read the whole preview. `--release YYYY` and `--file PATH` take other files with the same checks. Expect a new period 2026 and 2025 `unchanged`.
2. `load --commit` stores the new year as edition 1 with its live rows and ledger row in one transaction. Then `refresh-latest` (preview) and `refresh-latest --commit` if any held year was revised, `status`, and `python scripts/s10_rough_sleeping_editions_verify.py` (21 gates, exit 0).
3. **Run `refresh-latest` before W1, in the same session.** S10 is a W1 input (the national aggregates and the LA signals read the newest `snapshot_year`). `refresh-latest` copies the edition's time into the live `loaded_at`; a W1 run made before it would not carry a revision, and `refresh_map.py --check` is the safeguard. W1 and the map are run only when Scott says (they are deferred until every source is done).
4. What a halt means (exit 1, nothing stored, no ledger row): a changed header or layout (the autumn 2024 file had one); a marker (`[x]`, `[z]`, `[n]`), blank, text, negative or non-integer in a local authority cell, which is never read as 0; reconciliation or identity failing; Barnsley and Sheffield in the wrong form (source 10 is declared `new`); an unknown code; a collection, page or attachment title that no longer fits (discovery lists what it saw; use `--file PATH` if the tables are there). A year is REJECTED when the national total moves more than 30% or an authority more than 150 (new year), when a held year changes at all (`--acknowledge YEAR` after reading the preview), or when the file is partial (fewer than 296 authorities or a held authority missing: never released). An older file stops unless `--allow-older-file`; a file with the held file's rank but other content stops unless `--accept-reissue YEAR`.
   **"Nothing newer than the held release" is not proof there is nothing new.** After the announced date (from 1 March 2027) the preview warns if the held year is still the newest; check the GOV.UK collection by hand and use `--file PATH`.

To go back for a year: `restore-edition YYYY N` (preview by default) stores edition N's rows as the next edition, then `refresh-latest --commit`. Nothing is deleted from the editions.

**Never run the old workflow.** The n8n workflow "Rough Sleeping Snapshot (S10)" is retired (its Code node throws). It turned a blank or marker into 0 and upserted a chat-converted CSV.

## S13 MHCLG Local Authority Housing Statistics (housing register): each winter, and after a June revision

`la_housing_register` is the latest-edition layer of `la_housing_register_editions` (one open data file's statement about one reporting year, the year ending 31 March; a file-check ledger records every file read). A new year is published November to February: the 2024-25 Cover gives "Next Update: November 2026 to February 2027" for 2025-26, a new period `2026`. The 2024-25 return was revised on 25 June 2026 (cc1a for three authorities, cc5a for five), so a held year can change; the publisher revises each June: the open data page's change history records a scheduled June revisions update every year from 2021 to 2026 (read on 2026-10-10 from the GOV.UK page "Local Authority Housing Statistics open data": 22 June 2021 "June 2021 revisions", 23 June 2022 "Updated open data", 27 June 2023 "Scheduled revisions of LAHS data", 27 June 2024, 26 June 2025 and 25 June 2026 "Updated following scheduled revisions period for <year> returns"). Record: [S13](decisions/2026-10-10-s13-editions-first-load.md). Source note: [s13_lahs_source.md](s13_lahs_source.md).

Run from `ONS_Population_Estimates`. `check_sources.py 13` reads the collection for a newer "data returns" year; nothing detects a revision of the open data file in place, and nothing runs the load for you. Downloading a file needs Scott's permission, and a preview downloads.

1. `python scripts/s13_lahs_editions.py load` (preview). It reads the open data page and the newest "data returns" year page, downloads the CSV and that year's accessible ODS to `data/raw/s13_lahs/` (also in a preview; the database is not written), reads the ODS Cover and Data_Dictionary, checks the CSV's newest year against the ODS cell for cell, and prints the new year and every held year compared. Read the whole preview. A changed definition of cc1a or cc5a, or a missing symbol line, halts. `--release YYYY-YY` insists on a year; `--file PATH --ods PATH` takes local files.
2. `load --simulate`, then `load --commit` stores a new year as edition 1 with its live rows and ledger row in one transaction and a revised year as the next edition. Then `refresh-latest` (preview, `--simulate`, `--commit`), `status`, and `python scripts/s13_lahs_editions_verify.py` (24 gates, exit 0).
3. **Run `refresh-latest` before W1, in the same session.** S13 is a W1 input (the national aggregates and the LA signals read `households_on_register` at the newest `reporting_year`). Gate 17 of the verify script holds the 2025 `households_on_register` that W1 reads and fails by design once a revision moves it: read the change, then update the recorded `w1-read` line deliberately.
4. A revised year is REJECTED unless the preview is read and the year named with `--acknowledge YYYY` (England total moving more than 2%, or more than 20 authorities changing, or a value going to or from NULL). A 0-to-NULL or NULL-to-0 change is never released by `--acknowledge`: it needs a named, decided correction (`--acknowledge-correction NAME`, recorded in the loader with its counts). A new year is REJECTED when the England total moves more than 15% or more than 30 authorities move by more than 50%. A partial file (fewer than 296 authorities, or fewer than the held edition) is never released. An older file stops unless `--allow-older-file`; equal rank with other content stops unless `--accept-reissue YYYY`.
5. A new authority code is UNEXPLAINED until `geography.resolve` or `la_code_lookup` covers it (rule 4): add the lookup row with its evidence, as for Dorset (five rows on 2026-10-10). The CSV's `LAD24CD` for Barnsley and Sheffield changed between the February and June 2026 files; the loader keys on `local_authority_code`.
6. Open for Scott: whether the `reasonable_preference` zeros for Allerdale 2015 to 2018 and Telford and Wrekin should be NULL like their household counts. They stay as published until he decides; that would be a second named correction.

To go back for a year: `restore-edition YYYY N` (preview by default) stores edition N's rows as the next edition, then `refresh-latest --commit`. To undo edition 2 for a year, restore edition 1 and refresh. Nothing is deleted from the editions.

**Never run the old workflow.** The n8n workflow "Social housing waiting lists (S13)" is retired (its Code node throws). Its insert kept one arbitrary predecessor row (`DISTINCT ON`) for reorganised authorities.

## What is still manual

- **Spotting a new RO4 release.** Nothing detects it; see the RO4 section.
- **Running the S8b monthly check.** `check_sources.py 8b` detects a newer month but nothing runs `load` for you; see the S8b section.
- **Running the S19 monthly check.** Nothing runs `s19_pip_editions.py load` for you; see the S19 section.
- **Running the S15 monthly check.** Nothing runs `s15_hpi_editions.py load` for you; see the S15 section.
- **Running the S18 monthly check.** Nothing runs `s18_pipr_editions.py load` for you; see the S18 section.
- **Running the S9a and S9b monthly checks.** Nothing runs `s9a_drd_editions.py load` or `s9b_crfd_editions.py load` for you; see the S9 section.
- **Running the S11 monthly check.** Nothing runs `s11_cqc_editions.py load` for you; see the S11 section.
- **Running the S4 November load.** `check_sources.py 4` detects a new release but nothing runs `s4_care_leaver_editions.py load` for you; see the S4 section.
- **Running the S22 November load and the Table 615 updates.** `check_sources.py 22` detects a new Council Taxbase release, nothing detects a new Table 615 file, and nothing runs `s22_ctb_editions.py load` or `load-615` for you; see the S22 section.
- **Running the S23 autumn load.** `check_sources.py 23` detects a newer release but nothing runs `s23_rsh_stock_editions.py load` for you; see the S23 section.
- **Running the S6 quarterly load.** `check_sources.py 6` detects a newer release but nothing runs `s6_asylum_editions.py load` for you; see the S6 section.
- **Running the S10 winter load.** `check_sources.py 10` detects a newer release but nothing runs `s10_rough_sleeping_editions.py load` for you; see the S10 section.
- **Running the S13 winter load and spotting a revision.** `check_sources.py 13` detects a newer return year but nothing detects an in-place revision of the open data file or runs `s13_lahs_editions.py load` for you; see the S13 section.
- **Spotting that the publisher has revised a quarter.** Nothing detects it
  automatically yet; someone has to re-check each loaded quarter against the
  release page and, if it changed, follow Step 3. Recording it as
  `revision_detected` in `source_check_log` is also by hand.
- **Downloading files.** Each download needs Scott's permission.
- **Editing the manifest** (Step 2a, item 3; Step 3, item 2).
- **Spotting that a new quarter has been published.** Nothing detects it;
  someone checks the release page.
- **Loading a brand-new S1b quarter** (Step 2b, item 1) still uses the S1b
  build script; only the recording of edition 1 is automated. A new S1 quarter
  uses `load-new` (Step 2a); the n8n S1 workflow is no longer a loader.
