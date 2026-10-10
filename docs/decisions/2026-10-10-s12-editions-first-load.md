# S12 MHCLG Exceptional Financial Support and S.114 notices: first load on the editions engine

Status: done 2026-10-10. Works like [S13](2026-10-10-s13-editions-first-load.md), [S23](2026-10-09-s23-editions-first-load.md) and [S10](2026-10-10-s10-editions-first-load.md).
Loader: `scripts/s12_financial_stress_editions.py` (two specs, `s12_efs` and `s12_s114`); gates:
`scripts/s12_financial_stress_editions_verify.py` (all PASS, exit 0).
Design: `docs/superpowers/specs/2026-10-10-no-loader-sources-design.md` (audit record, not approval). Scott authorised the
real writes through the plan ("do the work, the spec is audit not approval").

**What the map sees.** The set of authorities with an EFS row (`efs_flag`) goes from 51 to 49: Bournemouth, Christchurch and
Poole (E06000058) and North Northamptonshire (E06000061) lose the flag, by Scott's decision of 2026-10-10 (below). Nothing
else leaves it. The set with an S.114 notice (`s114_flag`) is unchanged at 11 authorities (15 notices). W1 and
`refresh_map.py` were not run (Scott deferred W1 and the map until every source is done), so the published map still shows
51 until that run. `refresh_map.py --check` will show `la_efs_support` as loaded after the last W1 run, through the
`loaded_at` of the 52 refreshed rows.

**Decided by Scott (2026-10-10): Bexley kept, BCP and North Northamptonshire dropped.** The withdrawn-flag question (spec
finding 8) had three authorities whose only EFS rows are withdrawn requests. Bexley keeps its flag: its 2020-21 page lists a
Bexley capitalisation direction and an amended direction (December 2024), and a capitalisation direction is a form of EFS
support. The documents were not read (the PDFs were not downloaded), so what the amended direction says is unverified.
BCP (2022-23 page: "Council provided with in-principle support but withdrew its request") and North Northamptonshire
(2024-25 page, same wording) appear on no other year page and have no direction listed, so they are dropped under the named
rule `WITHDRAWN_ONLY_NOT_SUPPORT`. Edition 1 keeps both rows as held; the editions from the pages do not store them;
`refresh-latest --accept-key-changes 2022-23 --accept-key-changes 2024-25` removed them from the live table. A new
withdrawn-only authority halts until it is named. The withdrawn rows of authorities with other support (Peterborough, Luton,
Nottingham) stay. The rule lives in the loader; W1's SQL still treats any row as the flag.

## Before-state (read-only, confirmed against the live tables before anything was written)

Confirmed first: `la_efs_support` 113 rows over 51 authorities, 2020-21 to 2026-27 (9, 8, 5, 8, 19, 29, 35 rows), 8 NULL
`amount_m` (all `withdrawn`), `hra_only` false on every row, one `loaded_at` (2026-03-31 22:34:16.793348 UTC), survey hash
`2e581ba40c423195d40e0ab76345e382`; `la_s114_notices` 15 rows over 11 authorities in 8 financial years, one `loaded_at`
(2026-03-31 22:34:16.827293 UTC), survey hash `0fad67960e1fce4119ef17ec7c373d8c`.

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a
64-hex run, stays untouched. The `before-state` lines are the verify script's per-year hash of the live columns (key and
values; `source` and `loaded_at` left out); the `w1-before` line is the distinct `lad24cd` set of `la_efs_support` before the
load (what W1's `efs_flag` read); the two `w1-read` lines are the sets W1 reads after this load (EFS: the before set less the
two codes; S.114: unchanged). They were printed by `python scripts/s12_financial_stress_editions_verify.py
--print-note-lines` before `ddl` and before the migration, while the live tables were untouched. Edition 1 (as loaded)
hashes to the `before-state` lines (gate 17); the live sets hash to the `w1-read` lines (gate 9).

before-state la_efs_support 2020-21 rows=9 sha256-first32=10026d226b4f0f34fe0068be85efa42a sha256-last32=5b57e22b4f2c1154b06a651553b0b9c7
before-state la_efs_support 2021-22 rows=8 sha256-first32=9c504f23839d9e5b134057def72e5f83 sha256-last32=74d24c2290ee82e99a9ef9ecff7ca52d
before-state la_efs_support 2022-23 rows=5 sha256-first32=37b8392162fc0a7b689923b205f01366 sha256-last32=1c7b05cc94eb9f940c9afa457d369bfe
before-state la_efs_support 2023-24 rows=8 sha256-first32=3146ac6d3c42a1d322200cb2cb2fe750 sha256-last32=ce6218f0098fe5f3bcdf50802cc4c25c
before-state la_efs_support 2024-25 rows=19 sha256-first32=a12a61ecd461db95c64b43b86fea9967 sha256-last32=94cfef8f0f2e9a03efa60237ac721424
before-state la_efs_support 2025-26 rows=29 sha256-first32=557318c071ee371ad41422d3498f2e87 sha256-last32=983580493698dbd1302afa31fdaaf7c7
before-state la_efs_support 2026-27 rows=35 sha256-first32=05548dace31367c7e971e244c5a2f17c sha256-last32=c4381392675914967213f576b9803aa7
before-state la_s114_notices 2000-01 rows=2 sha256-first32=1f56875bdd6e51fd476a4d6e45a1671e sha256-last32=33f6f2d46f4860b8f0fd53f7c31fee8b
before-state la_s114_notices 2017-18 rows=1 sha256-first32=caa6e7aa0ace5520787ab7e1b86453fa sha256-last32=b4a88fb0d17127c5f7b061c4a881af5e
before-state la_s114_notices 2018-19 rows=1 sha256-first32=32140d59c8a1eff05027a724e1960458 sha256-last32=8d8e13c375457a9f7f0ca7a5cd955178
before-state la_s114_notices 2020-21 rows=1 sha256-first32=096198127cad0ebdcf8da88c43c9948b sha256-last32=84b75b812b8b348508fde048d3cfe66f
before-state la_s114_notices 2021-22 rows=4 sha256-first32=772ed992879d1995ead1739355d42a07 sha256-last32=58b9ba32578ce670dd225758cf1a5ba2
before-state la_s114_notices 2022-23 rows=2 sha256-first32=968986a8c56d8be0af73f49e638172b4 sha256-last32=0bb2330c1ab930ed29ef00c3a0357f2a
before-state la_s114_notices 2023-24 rows=3 sha256-first32=04e0bdb3d1a2c62d282aa7b7b5671b99 sha256-last32=c9f677c37ef6f4b2b6b1aea54fd69f44
before-state la_s114_notices 2024-25 rows=1 sha256-first32=544be56f1a1bb8bec4687cb4c7582c83 sha256-last32=40b7fdd3227aef61fa9d5762b9a2378c
w1-before la_efs_support lad24cd-set rows=51 sha256-first32=5c77d2ae27f8569a6d7fa9d28d258732 sha256-last32=0945a7c78631007ed2e81ea3fd7d76be
w1-read la_efs_support lad24cd-set rows=49 sha256-first32=68660011660571d6ae84ce98c7a522b9 sha256-last32=f8ef0624e759d337fed31994a31ea7f0
w1-read la_s114_notices lad24cd-set rows=11 sha256-first32=0f73bd85f0e758ea2edca2fdf3f8539e sha256-last32=9ebda63bd1c7307ba56b01db4c20719b

## How the tables came to hold what they hold (checked 2026-10-10 against the database, the run log and the docs)

- **2026-03-31, run-log row 27** ("Source 12 - LA Financial Stress", status success, 131 rows written, notes "MHCLG EFS
  2020-21 to 2026-27 and S114 notices static lookup"): the n8n workflow fetched the seven GOV.UK year pages, parsed the
  tables by regex, took the first amount in each cell, resolved duplicates "last writer wins", and mapped names to codes
  through a hard-coded dictionary. 113 EFS rows and 15 S.114 rows landed (128; the log's 131 is the workflow's own count and
  nothing in the database explains the difference). `hra_only` was never set. The S.114 rows came from a CSV compiled in a
  chat session ("IfG / Wikipedia / primary sources - compiled manually"); that session no longer exists.
- **The Haringey misattribution.** The dictionary mapped "Haringey" to E09000013, which is Hammersmith and Fulham. The backup
  `la_efs_support_bak_20260820` still holds the two wrong rows (E09000013, 2025-26 37.000 and 2026-27 84.000). On 2026-08-20
  both rows were re-attributed to E09000014 and the 2025-26 amount corrected to 40.6, by direct SQL, with no run-log row, and
  **`loaded_at` was not changed** (all 113 rows still carried 2026-03-31 22:34:16.793348 UTC before this load). The decision
  note is [2026-08-20-s12-efs-misattribution.md](2026-08-20-s12-efs-misattribution.md). The loader matches names exactly
  against `la_boundaries.lad24nm` or an alias in `scripts/s12_efs_names.json`; there is no fuzzy matching (the near-misses
  are Woking/Wokingham and Gloucester/South Gloucestershire).
- **2026-08-16, S.114 attribution** (`attribution`, `successor_codes`, `attribution_note` added; gate 14 rule; decision note
  [2026-08-16-s114-attribution-and-gate-14.md](2026-08-16-s114-attribution-and-gate-14.md)). The two Northamptonshire
  County Council notices (E10000021) stay against the issuer.
- **2026-09-04, `source_check_log` check 63** (method manual, outcome `revision_detected`, period 2025-26): both pages
  carried `public_updated_at` 2026-08-18, after the load; 2026-27 matched the table exactly; **2025-26 Bradford was held at
  127.1 against a published 113.0** and the other 28 rows matched; **Croydon 2025-26 was unresolved**: the 2026-27 page said
  it was revised to 110.3m from 136.0m while the 2025-26 page still showed 136.0m. On the page read today
  (`public_updated_at` 2026-08-18T14:12:33Z) the 2025-26 cell for Croydon reads "136.0m ... subsequently revised to 110.3m",
  so the two pages now agree. The check did not look at the other amount changes below.
- **18 August 2026 page changes.** The page change notes say what changed. 2026-27: "2026-27 external assurance reviews
  added" (Barnet, Cheshire, Cumberland, East Sussex, Enfield, Gloucester, Halton, Medway, Peterborough, Shropshire).
  2025-26: "Added 2025-26 capitalisation directions for: Bradford, Enfield, Haringey, Solihull, Stoke-on-Trent,
  Worcestershire." Those five LADs are exactly the 2025-26 rows whose status becomes `capitalisation-direction` in edition 2,
  and of them only Bradford's amount differs from the March load (127.1 to 113.0). That the 18 August edit caused the
  differences is consistent with the notes but not proved: no copy of either page from before 18 August is held. The
  2026-08-20 HTML (`scripts/verify/src/efs_2026_27.html`) has a table body byte-identical to today's 2026-27 page (45 table
  rows, headings, links and directions all equal), so it shows nothing of the change.
- The n8n workflow is retired (plan 1, Task 1): its Code node now throws. The repo had no S12 code before
  `scripts/s12_financial_stress_editions.py`.

## What edition 2 changes (per year and authority; the page text in brief)

Edition 1 "as loaded" is the live tables exactly as held (113 EFS rows, 15 notices). Edition 2 of every EFS year comes from
that year's own page, read on 2026-10-10 (the seven JSON files in `data/raw/s12_efs/`; the live content API returned
byte-identical content in the load preview, so no page has changed since). The year's own page always wins over a statement
on another year's page. No year page reproduced the March load on all values, so the migration wrote no EFS ledger rows and
`load` stored edition 2 for all seven years. Every authority on every page is stored or excluded by name; the excluded
non-LAD bodies are Norfolk (2024-25), Worcestershire and South Yorkshire Mayoral Combined Authority (2025-26), and East
Sussex, Worcestershire, Kent Police and Crime Commissioner and South Yorkshire MCA (2026-27). The load revised 54 areas;
52 live rows were refreshed and 2 removed.

Amounts (£m), edition 1 to edition 2, with the cell text:

| Year | Authority | Held | Page | Cell text (shortened) |
| --- | --- | ---: | ---: | --- |
| 2023-24 | Thurrock | 180.170 | 184.000 | "£234.5m ... (February 2024), then subsequently reprofiled to £184.0m ... (February 2025)" |
| 2023-24 | Croydon | 63.000 | 50.000 | "This was subsequently revised to: £50.0m (support agreed in-principle for 2023-24)" |
| 2024-25 | Thurrock | 68.600 | 73.020 | "subsequently revised to: £73.02m (support agreed in-principle)" |
| 2024-25 | Woking | 95.600 | 93.600 | "subsequently revised to: £93.6m (support agreed in-principle for 2024-25)" |
| 2024-25 | Birmingham | 685.000 | 490.000 | "subsequently revised to: £490.0m (support agreed in-principle for 2024-25)" |
| 2024-25 | Croydon | 38.000 | 51.000 | "subsequently revised to: £51.0m (support agreed in-principle for 2024-25)" |
| 2025-26 | Thurrock | 72.000 | 62.110 | "subsequently revised to £62.11m (support agreed in-principle)" |
| 2025-26 | Medway | 18.484 | 28.469 | "subsequently revised to £28.469m (support agreed in-principle)" |
| 2025-26 | West Berkshire | 3.000 | 20.000 | "subsequently revised to £20.0m (support agreed in-principle)" |
| 2025-26 | Somerset | 63.000 | 45.118 | "subsequently revised to £45.118m (support agreed in-principle)" |
| 2025-26 | Worthing | 2.000 | 4.750 | "subsequently revised to £4.75m (support agreed in-principle)" |
| 2025-26 | Bradford | 127.100 | 113.000 | "£113.0m" (a capitalisation direction is listed for 2025-26) |
| 2025-26 | Croydon | 136.000 | 110.300 | "subsequently revised to £110.3m (support agreed in-principle)" |

Amount to NULL, status `other-years-only` (two rows; the cell states a figure for another year only, and the rows are kept
so the authority keeps its flag, these being their only rows). Released by the named entry `s12-efs-own-year-2026-10` of
`ACKNOWLEDGED_FLIPS` (`--acknowledge-flips`), not by `--acknowledge`:

- 2024-25 Plymouth, held 72.000: the cell is "£72.0m for 2025-26" only.
- 2025-26 Shropshire, held 26.900: the cell is "£26.9m for 2024-25" only.

Status changes from the n8n default `agreed-in-principle`, 36 rows (Bradford 2025-26 is also in the amounts table).
`capitalisation-direction` where the cell is a bare amount (no in-principle qualifier) and the page lists a direction for
that authority and year under its Capitalisation directions heading:

- 2020-21: Nottingham, Luton, Eastbourne, Wirral, Croydon. Two more from the page text: Redcar and Cleveland `grant`
  ("£3.7m (in the form of grant)", no direction listed) and Lambeth `capitalisation-extended` ("£125m (original
  capitalisation of £100m in 2017-18 was extended by £25m)", no direction listed).
- 2021-22: Cumberland, Eastbourne, Wirral, Croydon. 2022-23: Slough, Cumberland, Kensington and Chelsea.
- 2023-24: Slough, Cumberland, Westmorland and Furness, Kensington and Chelsea, Lambeth.
- 2024-25: Middlesbrough, Nottingham, Stoke-on-Trent, Medway, Slough, Cheshire East, West Northamptonshire, Cumberland,
  Somerset, Eastbourne, Bradford, Havering.
- 2025-26: Stoke-on-Trent, Solihull, Bradford, Enfield, Haringey.

**`hra_only` now set (two rows true; it was false on all 113):** Lambeth 2025-26 ("£40.0m (support agreed in-principle)",
in the page's Housing Revenue Account table) and City of London 2026-27 ("£2.65m (support agreed in-principle)", the HRA
table).

**Removed from the live table (the withdrawn rule only):** BCP 2022-23 and North Northamptonshire 2024-25.

Statements on one page about another year that disagree with that year's own page (listed in the preview, own page wins,
accepted year by year with `--acknowledge`): the 2023-24 page says Croydon 2020-21 £10m, 2021-22 £14.4m and 2022-23 £11.2m
against 70.0, 50.0 and 25.0; the 2025-26 page's note says Thurrock 2024-25 £96.0m against 73.02 (the 2026-27 page agrees
with 73.02); the 2026-27 page says Birmingham 2024-25 £405.7m against 490.0, Birmingham 2025-26 £36.7m against 180.0,
Lambeth 2025-26 £46.0m against the HRA 40.0, and Shropshire 2025-26 £71.4m against NULL. These are the publisher's own
inconsistencies. The loader stores each year's own page and does not judge which figure is right.

After the refresh the live table has 111 rows (9, 8, 4, 8, 18, 29, 35), 49 authorities, 8 NULL `amount_m` (6 `withdrawn`,
2 `other-years-only`), 2 `hra_only`; 52 rows carry edition 2's `loaded_at` (2026-10-10) and 59 keep 2026-03-31 (no value
changed on them). Status counts: agreed-in-principle 67, capitalisation-direction 34, capitalisation-extended 1, grant 1,
other-years-only 2, withdrawn 6.

## S.114 notices: loaded as held, not evidenced, nothing removed

`migrate-legacy` stored edition 1 "as loaded" of `data/reference/la_s114_notices.csv` (the six-column legacy file; the proof
reproduced all 15 notices, 15 of 15 without evidence). The file has no `register_as_at` line and no evidence columns until
Task 7 turns it into the 12-column register. `status` reports "15 notices without evidence" and verify gate 8 counts them
(PASS with the count; it must be 0 or each listed once Task 7 is done). **Nothing was removed and the loader cannot remove a
notice** (`S114_REMOVALS` is empty; a missing notice is REJECTED). The evidence search and any removal are Task 7. One data
finding, held as it is: Northumberland (E06000057) has `notice_date` 2022-05-01 with `financial_year` 2021-22; May 2022 is in
2022-23. Six of the 15 dates are month-only (`date_confirmed` "approximate - month only confirmed").

## What was written (2026-10-10)

- `ddl --commit`: `la_efs_support_editions`, `la_s114_notices_editions` (append-only; triggers refuse UPDATE, DELETE and
  TRUNCATE) and their `_file_checks` ledgers. No change to the live tables' columns or constraints. The EFS editions table
  has editions-only columns the live table does not (`cell_text`, `page_updated_at`).
- `migrate-legacy --commit` (after a preview and a `--simulate` that rolled back): edition 1 for 7 EFS years and 8 S.114
  years, 128 rows; run-log row 407. Live untouched.
- `load --only efs`, every year acknowledged (`--acknowledge` for 2020-21 to 2026-27) plus
  `--acknowledge-flips s12-efs-own-year-2026-10`: preview (without the acknowledgements all seven years are REJECTED, exit
  1), `--simulate` (rolled back), `--commit`: edition 2 for 7 years, 111 rows, 54 areas revised; run-log row 408.
- `refresh-latest --accept-key-changes 2022-23 --accept-key-changes 2024-25` (preview, `--simulate`, `--commit`): 52 live
  EFS rows refreshed, two deleted (the withdrawn-only rule), before/after guard passed. S.114: nothing to refresh. The plan
  matched the load preview exactly.
- `status`: clean for both tables. The verify script ends exit 0, all PASS. No `zz%` table remains.
- Unchanged: the seven EFS page JSONs, `scripts/s12_efs_names.json` and `data/reference/la_s114_notices.csv`; the
  `la_efs_support_bak_20260820` backup.
- W1, `refresh_map.py`, the export, `push.py` and `git push` were not run.

## When a page changes, or a new year appears

From `ONS_Population_Estimates`:

1. `python scripts/s12_financial_stress_editions.py load --only efs` reads the collection and the year pages, saves each page
   (content API JSON) to `data/raw/s12_efs/` under the same-name rule (also in a preview) and previews against the stored
   edition. Read the whole preview, including the "statement on the N page" lines and the capitalisation directions per
   year. A new year page is a new period; a changed year is REJECTED unless named with `--acknowledge YYYY-YY`; a value
   going to or from NULL needs a named entry in `ACKNOWLEDGED_FLIPS`. An unknown cell form, table heading or unmatched name
   halts, naming the cell; a new alias goes in `scripts/s12_efs_names.json` with the page and year it was seen on. A page
   whose `public_updated_at` is older than the stored edition's stops (`--allow-older-file` only on purpose); equal rank with
   different content stops unless `--accept-reissue`.
2. `load --only efs ... --simulate`, then `--commit`; then `refresh-latest` (preview, `--simulate`, `--commit`; a year that
   gains or loses an authority needs `--accept-key-changes YEAR`), `status`, and
   `python scripts/s12_financial_stress_editions_verify.py` (exit 0). Run `refresh-latest` before W1: it copies the
   edition's `loaded_at` onto the live rows, and S12 is a W1 input (the flags). If a page change moves the EFS or S.114
   authority set, gate 9 fails by design: read the change, then update the `w1-read` lines deliberately.
3. S.114 is a curated register, not a publisher feed: `load --only s114` reads `data/reference/la_s114_notices.csv` once it
   is a register (Task 7) and stores a new edition for any year it changes. A row can only leave through a named
   `S114_REMOVALS` entry recording what was searched.

## How to reverse

- Per year: `python scripts/s12_financial_stress_editions.py restore-edition SPEC PERIOD N` (preview, then `--commit`)
  stores edition N's rows as the next edition; `refresh-latest` then writes them to the live table. Edition 1 is the table as
  it stood on 2026-10-10. Editions are append-only, so nothing is lost.
- To bring back the BCP and North Northamptonshire flags: restore edition 1 for 2022-23 and 2024-25 the same way, then
  `refresh-latest --accept-key-changes 2022-23 --accept-key-changes 2024-25` (and remove the two codes from
  `WITHDRAWN_ONLY_NOT_SUPPORT` in the loader before the next page load, or the next load will drop them again).
- The `loaded_at` of the 52 refreshed rows is not put back to March by a restore; the March values are in edition 1.
