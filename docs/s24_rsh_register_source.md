# S24 — RSH register of registered providers, regulatory judgements and enforcement notices

<!-- repo-meta
status: active
last-reviewed: 2026-10-11
type: source
consumed-by: scripts/s24_rsh_register_editions.py, scripts/s24_rsh_register_editions_verify.py
-->

| | |
|---|---|
| Publisher | Regulator of Social Housing (RSH) |
| Series | Registered providers of social housing (the register); Regulatory judgements and enforcement notices |
| Landing pages | https://www.gov.uk/government/publications/registered-providers-of-social-housing<br>https://www.gov.uk/government/publications/regulatory-judgements-and-enforcement-notices |
| Cadence | Register: a snapshot "around the middle of each month" (the page's words); the held files are dated 24 July, 17 August and 17 September 2026. Judgements: a new table every one to three weeks in 2026 (the page's change history) |
| Target tables | `rsh_registered_providers`, `rsh_regulatory_judgements`, `rsh_enforcement_notices` (live), each with an editions table and a file-check ledger |
| Geography | **None** |
| Loader | `scripts/s24_rsh_register_editions.py`; gates `scripts/s24_rsh_register_editions_verify.py` (21) |
| First load | 2026-08-14 (old build); on the editions engine since 2026-10-11 |
| Record | [S24 first load](decisions/2026-10-11-s24-editions-first-load.md) |

## Purpose: risk management, not analysis

The income route runs through a registered provider partner. A non-compliant regulatory judgement, an enforcement notice or a
de-registration is a material event for the business, and the point of holding this is to be told rather than to find out.
Secondary use: a de-registration is a business development signal, because the failed operator's landlords need a new manager.
Both are our uses of the data, not RSH statements.

## Deliberately not wired into Workflow 1

S24 has no local authority geography and must not acquire one. The register page says: "We do not publish or share addresses and
contact details of registered providers." So there is nothing to apportion. It is not in `staging_la_signals`, it is not a map layer
and `publish_map` is false; `geography.DATASET_FORM['24']` is `none`, and gate 3 of the verify script fails if a geography column
appears on these tables or an S24 column reaches `staging_la_signals`. A registered office is not where a provider's stock is.
For provider stock by authority use S23.

## What is published, and how the loader finds it

Two GOV.UK pages, read through the content API. Nothing is taken from a URL: a GOV.UK media URL can later serve another file (below).

- **The register page** carries one `.xlsx` attachment titled `List of registered providers – <d Month yyyy>` and an accessible HTML
  version of it. The page lists "Registered provider name, Registration number, Registration date, Local authority, non-profit or
  profit-making designation, Legal form" as what the monthly list holds. Its change history gives the monthly updates back to 2014.
  Only the current month's file is attached: there is no archive of past lists on the page, so earlier snapshots exist only where
  this pipeline kept them. The snapshot date is the date in the attachment title, never the run date.
- **The judgements page** carries one `.xlsx` attachment titled `Regulatory Judgements and Enforcement Notices Table`, whose file name
  begins with the issue date (`20260930_RegulatoryJudgementsNotices_published.xlsx`). It has two sheets read by exact title:
  `Regulatory Judgements` and `Enforcement Notices`. The page's change history lists each issue (30 September, 9 September, 26 August,
  18 August, 12 August, 29 July 2026 and earlier). The page also points to an archived table of all judgements and notices published
  between 2012 and March 2024; that archive is not loaded.
- **Registrations and de-registrations pages** (below) are linked from the register page and are not loaded.

## Identity (rule 3)

The identity comes from the file and the publisher's page, never from a URL.

- Register: the date in the attachment title (`--title-date` with `--file`), checked against the workbook's own modified date (between
  the title date and 14 days after); the one visible sheet whose title begins `Registered Providers - list`; the header row exactly.
- Judgements: the file name's `yyyymmdd` prefix, which must be a date of the page's change history (or its last-updated date), and the
  workbook's modified date within 7 days of it; the two sheet titles and header rows exactly.
- **A media URL can later serve a different file.** The register URL recorded for 17 August now serves the 17 September file (the same
  bytes as the September URL). The August file cannot be obtained, so its rows cannot be proved against a file.
- The ledger (`<editions table>_file_checks`) records every file read with its final URL and sha256, so a file seen before is skipped
  only on the pair. An older file than the one held (by title or file date, then modified time) halts unless `--allow-older-file`; an
  equal date with different content is a reissue and stops unless `--accept-reissue PERIOD`.

### The hidden sheet

Each register workbook carries a hidden sheet (set to very hidden) that holds what looks like a stray account token from the
publisher's own tooling. It is never opened: the loader reads the workbook's own `xl/workbook.xml` for sheet names and states, names
only visible sheets and opens the wanted sheet by its exact title. Nothing from the hidden sheet is read, printed, hashed or stored
(the file's sha256 covers the whole file, as for any file). The raw workbooks are in `data/raw/s24_rsh/`, which is git-ignored; do not
commit them. Verify gate 6 enforces the rule by reading the loader's own source.

## The three tables

Each live table keeps its original name, key and data columns and is the latest-edition layer. The editions tables
(`rsh_registered_providers_editions`, `rsh_regulatory_judgements_editions`, `rsh_enforcement_notices_editions`) are append-only and
add `null_reasons` (jsonb `{column: reason}`, editions only), `edition`, `supersedes`, the file's sha256 and provenance.

| Live table | Period | What a period is | Live holds |
|---|---|---|---|
| `rsh_registered_providers` | `snapshot_date` | one monthly list | every held snapshot |
| `rsh_regulatory_judgements` | `edition_date` | one issue of the judgements table | the newest edition date only |
| `rsh_enforcement_notices` | `edition_date` | the same issue | the newest edition date only |

- **Register** (key snapshot date and registration number): organisation name, registration date, designation, corporate form, notes.
  Held: 24 July 2026 (1,579 providers), 17 August (1,578), 17 September (1,577). At 17 September: 232 local authority, 1,253 non-profit,
  92 profit. The only notes with text are Pivotal Housing Association's (24 July and 17 September; the page's change history records
  "Updated with note about Pivotal Housing Association" on 1 July 2026): it says RSH notified Pivotal of its decision to deregister it
  on 27 May 2026, an appeal to the High Court has been lodged and it remains on the register pending the appeal.
- **Judgements** (key registration number and publication date): one row per judgement, with consumer, governance, viability and rent
  gradings, each with its change description and date, the type of publication, the engagement process, and the publisher's "Name and
  Reg Code Change Details" and "Other landlords included in the judgement" columns, stored verbatim. The 30 September edition has 311
  rows for 308 providers, publication dates from 9 July 2024 to 30 September 2026, all with status `Current`; by type of publication
  302 `Narrative RJ`, 6 `Interim RJ` and 3 `Grading Under Review`.
- **Enforcement notices**: 2 in the 30 September issue.

Only the 12 August and 30 September judgements issues are held. The page's change history lists issues of 18 August, 26 August and
9 September too; they were never loaded and cannot be obtained (the page offers the latest file only).

## What the grades are (RSH's own guidance)

Source: the guidance page [How we approach regulatory judgements and gradings](https://www.gov.uk/guidance/how-we-approach-regulatory-judgements-and-gradings)
(last updated 8 October 2026; held as `data/raw/s24_rsh/guidance_judgements_gradings.json`, read 2026-10-11). Everything below is its wording,
paraphrased; nothing is added from outside it.

- A regulatory judgement is RSH's published view of how well a landlord delivers the outcomes of its standards.
- **For private registered providers** a judgement can cover any of the standards; their governance, viability and consumer grades are the
  view of delivery of the Governance and Financial Viability Standard and the consumer standards.
- **For local authority landlords** a judgement can cover "the outcomes of our consumer standards and Rent Standard"; their one grade is the
  view of delivery of the consumer standards.
- Grades are issued to large landlords; the guidance says RSH does not issue grades to small landlords, though it may publish a judgement
  where it sees serious weaknesses or failings.
- The guidance says there are four governance grades and four consumer grades, and it names V1 to V4 for viability, so the grades are
  G1 to G4, V1 to V4 and C1 to C4. An asterisk marks a grade for a landlord designated on the register as for-profit (for example C2*).
- For large private registered providers RSH "usually" reflects its view of the Rent Standard and Value for Money Standard within the
  governance and viability gradings and in some circumstances publishes a separate statement on rents.
- Interim judgements may be issued after mergers or significant restructures (the guidance says RSH "may issue" one); a grade that is a regrade (V1 to V2 or the reverse) is not
  called an upgrade or downgrade. Each grade is the view at a point in time.

The guidance does not define the `-` marker in the judgements table, the Rent column's `Y`, or what a blank means. What each stored
reason below rests on is stated beside it.

## Markers and reasons (rule 1)

A NULL is never 0 and always carries its reason in `null_reasons` (editions tables). The reasons, with the 30 September issue's counts
(the 12 August issue in brackets):

| Cell | Reason | Cells | Evidence |
|---|---|---:|---|
| Governance or viability `-` on a row with landlord type `Local Authority` | `not_applicable_local_authority` | 206 (200) | The file marks `-` on all 103 local authority rows (206 cells). The guidance says a local authority's grade is its consumer-standards view. **Scott accepted the reading "not applicable" on 2026-10-11**; the file itself does not say it. |
| Consumer `-` whose change text begins `Not assessed yet` | `not_assessed_yet` | 66 (74) | The publisher's own change text in the same row. |
| Any other grade `-`: rent `-` (305), and consumer, governance and viability `-` on nine private providers (27) | `marked_dash_no_reason` | 332 (326) | **Nothing in the file or the guidance explains these.** They are stored NULL with this reason, flagged to Scott on 2026-10-11 as unexplained, and no meaning is guessed. The Rent column holds `Y` on 6 rows and `-` on the rest. |
| A change, date or text cell `-` beside a flagged grade | `marked_dash` | 1,140 (1,124) | Every `-` governance or viability grade has a `-` change in the file. |
| An empty cell | `blank` | 3 (3) | Two empty date cells and one empty "Other landlords" cell. |
| The text `None` in a descriptive column | `publisher_text_none` | 573 (568) | Kept verbatim. |

Register: empty Notes cells are `blank` (1,578 of 1,579 on 24 July, 1,576 of 1,577 on 17 September). A grade outside C1 to C4, G1 to G4,
V1 to V4 (each optionally `*`) or `Y` (rent) halts, naming the cell; a date must be a typed date, an Excel serial (some arrive as
serials) in a plausible range, `-` or blank, else it halts.

The old build turned `-`, an empty cell and the text `None` into one bare NULL with no reason (rule 1.5). Edition 1 of the judgements
and notices keeps that as held, with every empty cell marked `reason_not_recorded_as_loaded`; **edition 2 holds the same values with the
real reasons**, and the live tables show edition 2.

## Editions: what is held

| | Edition 1 | Edition 2 |
|---|---|---|
| Register 24 July 2026 | 1,579 rows as held (proved against the file) | not made |
| Register 17 August 2026 | 1,578 rows as held, notes text `None`; file not obtainable | 1,578 `notes` `None` made NULL, reason `stored_text_none_not_from_file` (correction `s24-aug-notes-none-2026-10`) |
| Register 17 September 2026 | 1,577 (proved against the file) | not made |
| Judgements and notices 12 August | 308 and 2, from the held file read with the old rules | same values with the reasons |
| Judgements and notices 30 September | 311 and 2 = the old live rows (proved) | same values with the reasons |

The 17 August register was loaded on 2026-08-20 by code that is not in the repo, with no run-log row. Its notes cells hold the text
`None` on all 1,578 rows, which the old build could not have written (its `text()` turned `None` into NULL), and Pivotal's note is
absent. **Scott decided on 2026-10-11 to keep the August snapshot as held** and to make the `None` notes NULL as a later edition; the
note on Pivotal is not restored because no August file is held to show it.

The newest-period-only rule: the judgements and notices live tables hold the newest edition date only (the engine opt-in
`EditionSpec.live_newest_period_only`); older issues stay in the editions tables as history. The old upsert had left 15 rows of the
12 August issue beside the 30 September rows; `refresh-latest --accept-key-changes 2026-09-30` removed them from live (each equals a
row of its stored edition).

## Registrations and de-registrations: RSH does publish dated de-registrations

The register page links yearly pages: "Registrations and de-registrations" for 2021 to 2026 and "De-registrations" for 2016 to 2020.
Each lists, by month and by designation group (for profit, not for profit, local authority), the provider name, registered number and
the date: for example "Trafford Housing Trust Limited, Registered number: L4440. De-registered: 14.08.2026" (2026 page, held as
`dereg_2026.json`) or "AHF FPRP Limited, Registered number: 5249. Registered: 03.09.2026". The page says RSH publishes "a list of
changes to the register due to new registrations and de-registrations" at the same time as the monthly list.

An earlier version of this note and of the registry said a de-registration has no published date and can only be inferred from two
snapshots. **That was wrong.** The yearly pages are not loaded by this source (later work). Comparing two held snapshots still shows
which providers left between two dates, and the pages give the date: L4440 is in the 24 July snapshot and not in 17 August, and the
page dates its de-registration 14 August 2026.

```sql
-- on the register at one snapshot and not at the next
SELECT p.registration_number, p.organisation_name
FROM rsh_registered_providers p
WHERE p.snapshot_date = '2026-07-24'
  AND NOT EXISTS (
    SELECT 1 FROM rsh_registered_providers q
    WHERE q.snapshot_date = '2026-08-17'
      AND q.registration_number = p.registration_number);
```

## Known traps

- **Wiring this into W1.** It has no geography.
- **Taking the date from the run date or from a URL.** The date is in the attachment title (register) or the file name and the page's
  change history (judgements). The August media URL now serves the September file.
- **Reading `None` in the August register's notes as publisher text.** It is not; it is NULL in edition 2.
- **Reading absence of a judgement as a clean bill of health.** 308 providers have a judgement in the 30 September table against
  1,577 on the register; the guidance says grades go to large landlords.
- **Reading a NULL grade as one thing.** It is one of three reasons (above); 332 cells are unexplained.
- **A registration number is not a stable identity.** L4331 appears in the judgements under two landlord names; "Name and Reg Code
  Change Details" is stored verbatim, not resolved.
- **Excel serial dates** in some date cells.
- **Committing the raw workbooks.** They carry the hidden sheet. `data/raw/` is git-ignored. Keep the four held files: verify gates 5, 6,
  7, 13 and 17 find them by sha256.

## How to run

From `ONS_Population_Estimates`. Every command that writes previews by default; `--simulate` runs the `--commit` path and rolls back.

```bash
python scripts/s24_rsh_register_editions.py status
python scripts/s24_rsh_register_editions.py load            # preview, read it whole
python scripts/s24_rsh_register_editions.py load --simulate
python scripts/s24_rsh_register_editions.py load --commit
python scripts/s24_rsh_register_editions.py refresh-latest  # after a reissue of a held period
python scripts/s24_rsh_register_editions_verify.py          # 21 gates, exit 0, writes nothing
```

A new register date is a new period; a new judgements issue is a new period and replaces the live judgements and notices. A judgements
file must carry enforcement notices (a file with none is rejected; see "What halts a load" below). `--file PATH [--title-date YYYY-MM-DD] [--no-page]` takes a file
you hold, `--only register|judgements` limits a run, `restore-edition TABLE PERIOD N` brings an earlier edition back as the next one.
`python scripts/check_sources.py 24` reads the newest register title against the newest held snapshot; nothing runs the load for you.

### What halts a load, and what to do

- **An identity or header failure** (title, sheet, header, modified date, change history): the file or the page changed. Read it,
  then fix the loader in a small commit with the evidence.
- **An older file** than the one held: skipped; a run of only older files halts. `--allow-older-file` overrides, and it does two different things: a judgements period not yet held is stored as edition 1 only (history, live unchanged); a period already held gets the file as its next edition, the new tip, and a later `refresh-latest` would copy its rows into live and roll it back.
- **A judgements file with no enforcement notices:** REJECTED, no release flag, exit 1. This is a known limitation, deliberately not changed. The file is kept in `data/raw/s24_rsh/` (do not delete it); live judgements and notices stay on the last issue that had notices, so the judgements and downgrades in the rejected issue do not arrive, and every later `load` meets the same rejection until the rule is changed. The page offers the latest file only, so a missed issue cannot be fetched later. Ask Scott: the rule can be changed deliberately, with evidence (open item 8 in `docs/decisions/README.md`). A sheet with no header at all halts earlier as an identity failure, with the same effect.
- **An empty live judgements table with a newer issue held:** loading an older issue stops (nothing stored); load the newest issue. `status` reports an editions issue newer than the live one as a fault (`live is behind`), not history.
- **A reissue of a held period** with different content: stop. `--accept-reissue PERIOD`, and a changed row needs `--acknowledge PERIOD`. Where a register and a judgements issue share a date, name the part (`register:2026-11-18`, `judgements:2026-11-18`); a bare date is accepted when only one part of the run has it and refused when both do.
- **A stop condition** (rejected, nothing stored, exit 1): a register with fewer than 95% of the previous snapshot's providers or more
  than 30 providers added or removed; a judgements issue with fewer than 90% of the previous issue's rows. Calibrated on the held data
  (24 July to 17 August +4/-5; 17 August to 17 September +1/-2). Read the cause, then `--acknowledge PERIOD`.
- **A grade or date outside the allowed forms:** the cell is named; read the cell before deciding.
- **A new judgements issue while live still holds old rows:** `refresh-latest` halts and names the flag it needs (`--accept-key-changes PERIOD`).
- **A register reissue that adds or removes a provider:** it can be stored, but `refresh-latest` halts and `--accept-key-changes` is refused for the register (the register spec does not opt in to key changes). There is no flag for it; the register's live rows are changed only by a code change decided with evidence.
- **Gate 13 after a reissue of 2026-09-17:** by design it fails until the gate is reviewed.

## Retired

`scripts/historical/s24_rsh_register_build.py` and `s24_rsh_register_verify.py` stop with a RETIRED message. The build's load ran twice
(run-log 96, 14 August 2026, and 101, 1 October 2026) and upserted in place with no preview. Do not run them. The old verify script's gate
5 compared one file's `-` markers with a table that mixed two issues; the decision note explains it.
