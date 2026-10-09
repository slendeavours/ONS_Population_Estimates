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

## What is still manual

- **Spotting a new RO4 release.** Nothing detects it; see the RO4 section.
- **Running the S8b monthly check.** `check_sources.py 8b` detects a newer month but nothing runs `load` for you; see the S8b section.
- **Running the S19 monthly check.** Nothing runs `s19_pip_editions.py load` for you; see the S19 section.
- **Running the S15 monthly check.** Nothing runs `s15_hpi_editions.py load` for you; see the S15 section.
- **Running the S18 monthly check.** Nothing runs `s18_pipr_editions.py load` for you; see the S18 section.
- **Running the S9a and S9b monthly checks.** Nothing runs `s9a_drd_editions.py load` or `s9b_crfd_editions.py load` for you; see the S9 section.
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
