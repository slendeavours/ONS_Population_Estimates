# S12 — MHCLG Exceptional Financial Support, and section 114 notices

<!-- repo-meta
status: active
last-reviewed: 2026-10-10
type: source
consumed-by: scripts/s12_financial_stress_editions.py, scripts/s12_financial_stress_editions_verify.py
-->

| | |
|---|---|
| Publisher | EFS: MHCLG (the Ministry of Housing, Communities and Local Government; the earlier year pages also list the Department for Levelling Up, Housing and Communities). S.114 notices: each council that issued one; there is no central register |
| Series | "Exceptional Financial Support for local authorities", one GOV.UK page for each financial year; and a curated register of section 114 notices |
| Collection | https://www.gov.uk/government/collections/exceptional-financial-support-for-local-authorities (seven documents titled "Exceptional Financial Support for local authorities for YYYY-YY", 2020-21 to 2026-27) |
| Cadence | EFS: no schedule. Each year's page is updated as decisions are taken (the 2026-27 page: first published 23 February 2026, updated 30 July and 18 August 2026). S.114: as issued |
| Live tables | `la_efs_support` and `la_s114_notices` (the latest-edition layers) |
| Editions | `la_efs_support_editions` and `la_s114_notices_editions` (append-only), each with a `_file_checks` ledger |
| Natural keys | `(lad24cd, financial_year)` and `(lad24cd, notice_date)` |
| Held | `la_efs_support`: 111 rows, 49 authorities, 2020-21 to 2026-27 (9, 8, 4, 8, 18, 29, 35 rows). `la_s114_notices`: 14 notices, 10 authorities, 2000-01 to 2024-25, all with evidence (2026-10-11) |
| Loader | `scripts/s12_financial_stress_editions.py` (two specs, `s12_efs` and `s12_s114`); gates `scripts/s12_financial_stress_editions_verify.py` (21). Manual-input helper `scripts/manual_input.py` |
| Record | `docs/decisions/2026-10-10-s12-editions-first-load.md` (with the before-state hashes, the S.114 evidence and the two removals) |

A W1 and map input, and only as two flags: `sql/w1/05_la_signals.sql` sets `efs_flag` for an authority with any row in `la_efs_support` and `s114_flag` for an authority with any row in `la_s114_notices`. The amounts, statuses and evidence are not read by W1.

## What the figures are

The year pages are lists of decisions by the department. The wording is the publisher's:

- Each page says the government "has agreed to provide" councils with support "to manage financial pressures via the Exceptional Financial Support process" (the earlier pages say "the previous government agreed to provide" or "provided"). The table is headed "Exceptional Financial Support requests from local authorities: YYYY-YY", so a row is a request and a decision about it.
- The 2024-25, 2025-26 and 2026-27 pages say "Councils were provided with in-principle capitalisation support in February" (2024, 2025 and 2026 respectively) "ahead of their budget setting", and that "This page will be updated with final amounts of capitalisation agreed and capitalisation directions issued once confirmed". So an amount marked "(support agreed in-principle)" is an in-principle figure, and a capitalisation direction under the page's "Capitalisation directions" heading is the issued document (PDF).
- The pages do not state amounts drawn or spent.
- "Decisions can relate to prior financial years and can amend the profile of support in those prior years. The pages for previous years have been updated to reflect the most recent decisions" (2026-27 page); the collection page says the presentation "reflects the point at which requests were agreed and pages for each year have been updated to show the most recent decisions". This is why a year's page, not the page of the year a decision was taken, is the authority for that year.
- A request can be withdrawn: the cell reads "Council provided with in-principle support but withdrew its request". The 2021-22 page counts them separately ("4 councils received in-principle support but later withdrew their request").

A section 114 notice is the report that the council's own documents cited in the register call a section 114 report or notice (section 114 of the Local Government Finance Act 1988); the EFS pages do not define it. The pipeline holds the date, the financial year, the reason as compiled, the attribution and the evidence; it does not hold what a notice said.

## Discovery

`load` asks the GOV.UK content API (`https://www.gov.uk/api/content`) for the collection, whose title must still be "Exceptional Financial Support for local authorities", and takes from it one document for each year titled exactly "Exceptional Financial Support for local authorities for YYYY-YY". A document of another shape is listed; a held year with no page, two pages for one year, or a retitled collection halt. Each page is saved as the content API's JSON to `data/raw/s12_efs/efs_YYYY-YY_content_<date>.json` (git-ignored; a preview saves too and writes nothing to the database) under the same-name rule: a file of that name with different content is never overwritten, the new one is saved with a hash suffix. `--page-file PATH` (repeatable, with `--no-page`) reads saved JSON instead; `--year YYYY-YY` limits the run.

## File identity (rule 3): from the page, on every path

Nothing is taken from the base path or link. The loader reads and checks:

- the page title, "Exceptional Financial Support for local authorities for YYYY-YY";
- the main table's header, "Local authority" and "Exceptional Financial Support requests from local authorities: YYYY-YY", with the page's own year (a header for another year halts);
- the rank of a page: the later of `public_updated_at` and the newest change-history timestamp.

## Table headings

A page can hold several tables, told apart by the heading before each:

| Heading | Treatment |
|---|---|
| (the main table) | stored |
| "Housing Revenue Account" | stored with `hra_only` true (Lambeth 2025-26, City of London 2026-27) |
| "Police Force" | not stored (Kent Police and Crime Commissioner, South Yorkshire Mayoral Combined Authority; 2025-26 and 2026-27) |
| "Capitalisation support for 2024-25" | not stored; the one row is Norfolk, a county council |
| a heading containing "Revised Exceptional Financial Support for previous financial years" | not stored; a statement about other years (Birmingham on the 2026-27 page) |

Any other heading halts, naming it.

## The cell grammar

A cell is read by a closed grammar that covers only the forms on the seven pages held on 2026-10-10. Anything else halts naming the page, the row and the cell; nothing is parsed by first match.

- A first amount, "£63.0m", optionally with "(support agreed in-principle)" or "(support agreed in-principle for YYYY-YY)".
- "This was subsequently revised to: £50.0m (...)" (also without the colon): the last such amount is the year's figure. "Thurrock 2023-24 £234.5m ... then subsequently reprofiled to £184.0m" is read the same way (the 184.0).
- "£Nm for YYYY-YY" and "(support agreed in-principle for YYYY-YY)": a statement about another year. A cell with such statements only is stored with a NULL amount and status `other-years-only` (Plymouth 2024-25, Shropshire 2025-26).
- "Note: For support agreed in-principle for YYYY-YY, this has been revised to £Nm (from £Nm)": another year.
- A total over several years, "£Nm covering:" or "£Nm agreed in-principle covering:", followed by one line per year ("2018-19: £78.015m"): the total and its year lines are statements about other years (Slough 2022-23, whose own figure is the first amount, £56.614m; Birmingham 2024-25). The year's figure is the amount stated for the year itself.
- "£50.0m (covering 2023-24 to 2024-25)" (Lambeth 2023-24): one figure that the page says spans two years; it is stored against the page's year.
- "Note: Provisionally includes revised support agreed in YYYY-YY, subject to final confirmation": a note, no figure.
- The 2020-21 qualifiers "(in the form of grant)" and "(original capitalisation of £100m in 2017-18 was extended by £25m)".
- A cell containing "withdr...": the withdrawn request.

## Values and status

`amount_m` is the year's own figure in £ millions, three decimals, NULL for a withdrawn request or an other-years-only cell (a database check enforces both, and only both). `status` is one of:

| status | Meaning |
|---|---|
| `agreed-in-principle` | the cell says "(support agreed in-principle)" |
| `capitalisation-direction` | the cell is a bare amount and the page lists a capitalisation direction for that authority and year. This is the loader's reading of the page, not a word the publisher uses |
| `grant` | Redcar and Cleveland 2020-21, "(in the form of grant)"; the page lists a grant determination, not a direction |
| `capitalisation-extended` | Lambeth 2020-21, "(original capitalisation of £100m in 2017-18 was extended by £25m)" |
| `withdrawn` | "Council provided with in-principle support but withdrew its request" |
| `other-years-only` | the cell states support for other years only |

Edition 2 has 111 rows: 67 `agreed-in-principle`, 34 `capitalisation-direction`, 6 `withdrawn`, 2 `other-years-only`, 1 `grant`, 1 `capitalisation-extended`.

## Names (rule 4)

A page name must equal a `la_boundaries.lad24nm`, or an alias, or an exclusion in `scripts/s12_efs_names.json`, exactly; anything else halts, listing the `lad24nm` values that share a whole word with it and never choosing one. There is no fuzzy matching: the n8n dictionary sent "Haringey" to E09000013 (Hammersmith and Fulham), and the near-misses are Woking/Wokingham and Gloucester/South Gloucestershire (`docs/decisions/2026-08-20-s12-efs-misattribution.md`). Aliases record the page and year each was seen on ("Redcar & Cleveland", "Windsor & Maidenhead", "Kensington & Chelsea" and others). Copeland (abolished 1 April 2023) is stored against its successor Cumberland (E06000063), through `la_code_lookup`, as the n8n load did. Excluded by name, counted and listed: East Sussex, Worcestershire and Norfolk county councils, Kent Police and Crime Commissioner and South Yorkshire Mayoral Combined Authority.

S12 is declared `none` in `scripts/geography.py`: the data has no Barnsley or Sheffield rows.

## Withdrawn requests and the flag

W1's `efs_flag` is true for any row, so a withdrawn request flags an authority. Three authorities have only withdrawn rows: Bexley (2020-21 and 2021-22), Bournemouth, Christchurch and Poole (2022-23) and North Northamptonshire (2024-25). By Scott's decision of 2026-10-10, as a named rule in the loader (`WITHDRAWN_ONLY_NOT_SUPPORT`):

- BCP and North Northamptonshire are not stored by the editions (their pages say "withdrew its request" and list no capitalisation direction, and they appear on no other year's page). They lose the flag on the next W1 run. Edition 1 keeps both rows as held.
- Bexley is kept: its 2020-21 page lists a "Bexley capitalisation direction 2020-21" and an amended direction (change note of 13 March 2025, "Added varied directions for: Bexley, ..."), and a capitalisation direction is a form of EFS support. The two directions have not been read.
- An authority not named whose every request was withdrawn halts until it is named. The withdrawn rows of authorities that have support in another year (Peterborough, Luton, Nottingham) stay.

The `efs_flag` rule itself is W1's and is not changed by this source.

## Statements about other years

A page can state a figure for another year. Each is compared with that year's own page; where they disagree the own page wins, the disagreement is listed in every preview, and the year stops until `--acknowledge YYYY-YY`. They are the publisher's own inconsistencies and the loader does not judge them. Read on 2026-10-10: the 2023-24 page gives Croydon 2020-21 as £10m, 2021-22 as £14.4m and 2022-23 as £11.2m against 70.0, 50.0 and 25.0 on those years' pages; the 2025-26 page's note gives Thurrock 2024-25 as £96.0m against 73.02; the 2026-27 page gives Birmingham 2024-25 as £405.7m against 490.0, Birmingham 2025-26 as £36.7m against 180.0, Lambeth 2025-26 as £46.0m against the Housing Revenue Account table's 40.0, and Shropshire 2025-26 as £71.4m against NULL.

## The S.114 register

Notices are issued and published by individual councils, so the input is `data/reference/la_s114_notices.csv`, read through `scripts/manual_input.py` (`read_s114`):

- a first line `register_as_at,YYYY-MM-DD` (the rank of the register; an older one than the held edition stops the load);
- the exact header `la_name, lad24cd, notice_date, financial_year, reason, date_confirmed, attribution, successor_codes, attribution_note, evidence_url, evidence_title, checked_on` (`successor_codes` as `E06000061;E06000062`);
- `date_confirmed` is `exact` or `approximate - month only confirmed`; every `lad24cd` must be in `la_boundaries` unless `attribution` is `predecessor` with successors and a note (gate 14 of 2026-08-16, `docs/decisions/2026-08-16-s114-attribution-and-gate-14.md`);
- a row without all three evidence columns is reported as unevidenced and still loaded; it is never invented or dropped. The register is research-tier (rule 6.4): not publisher data, dated, and evidenced row by row.

**The loader cannot remove a notice.** A held notice missing from the register stops the year, and no flag releases that. A removal exists only as a named entry in `S114_REMOVALS` with what was searched. A month-only date corrected to an exact date in the same month is a listed date correction (the key changes, so `refresh-latest` needs `--accept-key-changes YYYY-YY`). A notice moved, unchanged, from one financial year to another is a named entry in `S114_REFILES` (from, to, decided, why): the next edition of the old year omits it and the next edition of the new year gains it, both or neither, and `refresh-latest` needs `--accept-key-changes` for both years. Nothing is deleted from the editions tables.

The register as at 2026-10-11 holds 14 notices for 10 authorities, all evidenced: 10 backed by a council's own report or meeting papers, 1 by a council statement (Woking) and 3 by a press report quoting the council (Hackney, and Northamptonshire twice). Three month-only dates were corrected from the council papers (Nottingham 2021-12-15, Northumberland 2022-05-23, Barnet 2025-01-20); one month-only date remains (Northamptonshire, July 2018). Two held notices were removed on Scott's decision as unsupported: Hillingdon 2000-07 and Croydon 2022-01. The 15 notices as held remain as edition 1. On 2026-10-11 (Scott's decisions) Croydon's second notice of 2 December 2020 was added (the council's own report under section 114(3), dated 2 December 2020) and Northumberland's notice of 23 May 2022 was re-filed from 2021-22 to 2022-23, the April-March year of its date (`S114_REFILES`); the Hillingdon removal stands. The evidence, searches and removals are in the decision note.

Decided by Scott (2026-10-11): Hillingdon stays removed (reversible: `restore-edition s12_s114 2000-01 1`, or re-add the row with evidence); Croydon 2 December 2020 added; Northumberland re-filed under 2022-23. Still open: the day of Hackney's notice (17 October 2000) is not stated in the report found. Attribution: the two Northamptonshire County Council notices (E10000021, abolished 31 March 2021) are `predecessor` and are never propagated to the successors.

## Blanks and zeros (rule 1)

No amount is ever coerced to 0. A withdrawn request and a cell with no figure for the year are NULL with the reason in `status`; a value going to or from NULL against the held edition is released only by a named entry in `ACKNOWLEDGED_FLIPS` (`--acknowledge-flips NAME`; the first load used `s12-efs-own-year-2026-10` for Plymouth 2024-25 and Shropshire 2025-26). An amount cell that does not parse halts. A blank register cell is NULL, never an empty string.

## Editions and revisions

An edition is what the year's own page says about that year (EFS) or what the register says about that year's notices (S.114). Edition 1 is each live table exactly as held on 2026-10-10 (113 EFS rows, 15 notices). Edition 2 of every EFS year is the page read on 2026-10-10 (13 amount changes, 2 values to NULL, 36 status changes, 2 `hra_only` set, 2 rows removed by the withdrawn-only rule); edition 2 of S.114 is the evidenced register. The live tables move only by inserting a new year (edition, live rows and ledger row in one transaction) and by `refresh-latest --commit`, which copies the tip into the live rows and copies the edition's `loaded_at`.

A byte-identical re-read is `unchanged` on every path, including against edition 1 "as loaded". The ledger records each page read by (final URL, canonical sha256 of the JSON). An older page (by its own rank) is skipped and halts unless `--allow-older-file`; equal rank with different content stops unless `--accept-reissue YYYY-YY`.

### Stop conditions (enforced in `load`)

A **halt** (identity, unknown cell form, unknown heading, unmatched name, older page) stops the run before anything is planned: nothing stored, exit 1. A year breaking a condition is REJECTED: nothing stored for it, no ledger row, exit 1; a partial run still writes its run-log row.

| Case | Rejected when |
|---|---|
| EFS held year | an authority of the held edition missing from the page (`PARTIAL PAGE`; only the withdrawn-only rule may remove a row; nothing releases it); any change of amount, status or `hra_only` (`--acknowledge YYYY-YY`); an amount going to or from NULL without a named flip; another page disagreeing with the year's own page (`--acknowledge`) |
| EFS new year | no rows on the page |
| S.114 held year | a notice missing from the register (never removed except by `S114_REMOVALS`, or moved by `S114_REFILES`); a notice added, a new year or a changed value (`--acknowledge`); a named re-filing that disagrees with the register or the held editions, changes a value, or whose other year is rejected |
| both | the set of authorities with any row (the map's flags) may lose an authority only under the withdrawn-only rule or a named S.114 removal |

## How to run

Run from `ONS_Population_Estimates`; see the S12 section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s12_financial_stress_editions.py status
python scripts/s12_financial_stress_editions.py load --only efs       # preview; reads the live collection
python scripts/s12_financial_stress_editions.py load --only s114      # preview; reads the register
python scripts/s12_financial_stress_editions.py load --only efs --commit
python scripts/s12_financial_stress_editions.py refresh-latest         # preview; --commit applies
python scripts/s12_financial_stress_editions_verify.py
```

Other commands: `ddl`, `restore-edition SPEC PERIOD N` (`s12_efs` or `s12_s114`), and the one-off `migrate-legacy` (already run). Every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**Run `refresh-latest` before W1.** Both tables are W1 inputs and a revision reaches the live table only through it.

## Known traps

- **Taking the first amount in a cell.** The n8n load did; 13 held amounts were out of date as a result or through later page revisions.
- **A statement about another year read as this year's.** "£72.0m for 2025-26" on the 2024-25 page is not a 2024-25 amount.
- **Fuzzy name matching.** Haringey went to Hammersmith and Fulham. Names are exact.
- **Reading the Housing Revenue Account or Police Force table as the main table.** They are separate headings.
- **Treating the register as a feed.** It is hand-compiled, evidenced and dated; a new notice is added by hand.
- **Dropping the authority when a request is withdrawn.** The rows stay; only the named rule drops BCP and North Northamptonshire.

## History

- **2026-03-31, run-log 27:** the n8n workflow "LA Financial Stress (12)" loaded 113 EFS rows (the log says 131 written) by regex over the seven pages, the first amount in each cell, a hard-coded name dictionary and "last writer wins"; `hra_only` was never set. The 15 S.114 notices came from a CSV compiled in a chat session ("IfG / Wikipedia / primary sources - compiled manually").
- **2026-08-16:** `attribution`, `successor_codes` and `attribution_note` were added to the notices.
- **2026-08-20:** the two Haringey rows were re-attributed from E09000013 to E09000014 and the 2025-26 amount corrected, by direct SQL with `loaded_at` unchanged (`docs/decisions/2026-08-20-s12-efs-misattribution.md`).
- **2026-09-04, `source_check_log` 63:** Bradford 2025-26 held at 127.1 against a published 113.0; the 2025-26 and 2026-27 pages disagreed on Croydon.
- **2026-10-10:** the n8n workflow is retired (its Code node throws). Edition 1 and edition 2 were written for both tables; the S.114 register gained its evidence and two named removals. The map changes only for BCP, North Northamptonshire and Hillingdon, on the next W1 run.
- **2026-10-11:** Scott's decisions: Croydon's notice of 2 December 2020 added and Northumberland's notice re-filed under 2022-23 (edition 3 of 2020-21, 2021-22 and 2022-23); Hillingdon stays removed. 14 notices, 10 authorities; no flag changes.
