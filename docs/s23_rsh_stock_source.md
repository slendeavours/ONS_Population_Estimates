# S23 — RSH registered provider social housing stock by local authority

<!-- repo-meta
status: active
last-reviewed: 2026-10-10
type: source
consumed-by: scripts/s23_rsh_stock_editions.py, scripts/s23_rsh_stock_editions_verify.py
-->

| | |
|---|---|
| Publisher | Regulator of Social Housing |
| Series | Registered provider social housing stock and rents in England, registered providers look-up tool |
| Collection | https://www.gov.uk/government/collections/registered-provider-social-housing-stock-and-rents-in-england (each year has its own release page, found through the collection) |
| Cadence | Annual, published autumn; stock as at 31 March. Next: 27 October 2026, stock at 31 March 2026 |
| Live table | `rsh_rp_stock_by_la` (the latest-edition layer) |
| Editions | `rsh_rp_stock_by_la_editions` (append-only) and `rsh_rp_stock_by_la_editions_file_checks` (ledger) |
| Natural key | `(stock_date, rp_code, lad24cd)`; inside one stock date `(rp_code, lad24cd)` |
| Held | one stock date, 2025-03-31: 10,171 provider-by-authority rows, 296 of 296 English local authorities, 1,407 providers |
| Loader | `scripts/s23_rsh_stock_editions.py`; gates `scripts/s23_rsh_stock_editions_verify.py` (21) |
| Record | `docs/decisions/2026-10-09-s23-editions-first-load.md` |

Not a W1 or map input: nothing in `sql/w1/`, the export or `index.html` reads this table, and the registry has `publish_map` false.

## Why this source matters

The first **direct** supply-side measure in the pipeline. Everything before it is indirect: S11 counts CQC-registered locations, S8 counts Housing Benefit Specified Accommodation caseload. This counts units owned, by provider, by authority.

## The two returns, and why they are one table

The release draws on two collections (technical notes, "Data sources"): the **SDR**, the Statistical Data Return, from private registered providers (PRPs), and the **LADR**, the Local Authority Data Return, from local authority registered providers (LARPs). The look-up tool's Introduction says it collates both, to give a summary overview of all English registered providers, and `STOCK_BY_LA` carries both in the same five stock columns, with the publisher's own per-authority subtotals reconciling across both. They are held in one table with a `provider_type` column (`PRP` or `LARP`) so they stay separable.

## Discovery

The release page URL carries the years, so it changes every year and nothing is hard-coded.

1. The GOV.UK content API for the collection lists `links.documents` titled `Registered provider social housing stock and rents in England <yyyy> to <yyyy>` (2019 to 2020 through 2024 to 2025 today). The newest by closing year is used. Exactly one per year, otherwise the loader halts listing the titles; no matching title halts (a renamed series fails loudly).
2. The release page must carry exactly one attachment titled `Registered providers look-up tool` with an `.xlsx` URL; none or several halt, listing the titles. The page's `change_history` is printed.
3. The file is downloaded with redirects followed and the final URL recorded, to `data/raw/s23_rsh/` (git-ignored; also in a preview, which writes nothing to the database). A same-named file with the same sha256 is kept; different content is saved beside as `<stem>-<sha8><suffix>` and that is read; nothing is overwritten. It must open as `.xlsx` with a `STOCK_BY_LA` sheet before it is kept.

## File identity (rule 3): from the file, on every path

Never from the file name, the link or the page. The loader reads, from the workbook itself:

- `Introduction and Contents`: the title `RP social housing by local authority area (SDR and LADR data) <yyyy>`, the source line `Statistical Data Return (SDR)/Local Authority Data Return (LADR) 1 April <y1> to 31 March <y2>` (with `yyyy = y2`), `Publication date: <month year>` and `Version: <n>`.
- `Version History`: the last row must equal that publication month and version.
- The release page's years (when the page is read) must equal `y1` and `y2`. The file decides `stock_date` (31 March `y2`).

The held file is `RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx` (sha256 starting `6c237c79`): title year 2025, source 1 April 2024 to 31 March 2025, publication November 2025, version 1.1. Its Version History reads version 1, October 2025, "Original release."; version 1.1, November 2025, "Corrected an issue which affected England stock figures only." Version 1.0 is not on the release page and not in the Wayback Machine (the earliest capture of the page, 23 November 2025, already lists 1.1), so what changed is **not recoverable**. The release page records the first publication date, 28 October 2025, which is what the live `publication_date` holds for the held 2025 rows. **From the next load that changes**: a new load writes `publication_date` as the first day of the file's own publication month (the loader reads the month from the workbook, not the day), so the 27 October 2026 release will carry 2026-10-01, a day on which nothing was published. The two meanings are not the same; read `publication_date` of a 2026 row as "the file's publication month", and use the release page for the day.

A file's rank is its own (publication month, version). A file older than the one held for its stock date is skipped and listed (a run of only older files halts; `--allow-older-file` overrides and says so in the run log); equal rank with different content stops.

## Where the local authority breakdown lives

Not in a data file: the **`STOCK_BY_LA` sheet inside the look-up tool**, which exists to drive the workbook's search box. It is an internal sheet with no publication guarantee, so the loader asserts the exact header row and halts, naming what it saw, if it differs:

`RP_Name, RP_Code, RP_Type, SDR_Size, Survey_Status, LA_Nm, LA_Code, Concat, Total Social Stock, LA_GN_SC_Own, LA_GN_BSp_Own, LA_SHHOP, LA_LCHO_Less_100_Eqty_Own`

The 2024 tool has a **different layout** (stock headers prefixed `CLCRR025_LA_GN_SC_Own` and so on; `SDR_Size` and `Survey_Status` worded differently; provider-by-region rows with `LA_Code` `N/A`, 249 duplicate provider and code pairs; one provider, Medway Council 00LC, whose total, 3,037, is not the sum of its parts, 3,036). The loader halts on it. Back-filling 2024 is out of scope and would need an alias map and a decision on the region rows. It also shows the 2026 file may change layout; the fix is a deliberate alias map committed with the evidence.

## Grain — the trap in this sheet

`STOCK_BY_LA` mixes three grains in one column layout, told apart only by `RP_Type` (2025 counts):

| `RP_Type` | Rows | `SDR_Size` | Loaded |
|---|---:|---|---|
| `Large` | 7,205 | Long Form | Yes: `PRP` |
| `Small` | 2,738 | Short Form | Yes: `PRP` |
| `LARP` | 228 | LARP | Yes: `LARP` |
| `LA` | 296 | — | No: the publisher's per-authority subtotals (`RP_Name` holds the authority name, `RP_Code` and `LA_Code` both hold its code, `LA_Nm` its region; `SDR_Size` is `LA` and `Survey_Status` is `NA`) |
| `Region` | 9 | — | No: regional aggregates (`RP_Code` `England`; the only rows with formulas) |

A provider row is `RP_Type` `Large`, `Small` or `LARP` **and** an E06 to E09 `LA_Code`; a provider-typed row without one halts, any other `RP_Type` halts, and a row with no `RP_Code` must be entirely empty (1,797 trailing rows of the 2025 sheet are). A load that does not filter counts roughly three times over: the unfiltered sheet sums to 13,599,165 units against 4,533,055. Large and Small are the glossary's terms: a Large PRP owns 1,000 or more units and completes the long SDR form; a Small PRP owns fewer than 1,000 and completes the short form.

## Columns

| Target column | Source header | Meaning |
|---|---|---|
| `total_social_stock` | `Total Social Stock` | The four below, summed. The glossary: total social stock is "the number of self-contained units plus bedspaces". |
| `general_needs_self_contained` | `LA_GN_SC_Own` | General needs, self-contained units |
| `general_needs_bedspaces` | `LA_GN_BSp_Own` | General needs, non-self-contained units (bedspaces) |
| `supported_housing_and_older_people` | `LA_SHHOP` | The tool labels this "Supported housing/housing for older people" |
| `low_cost_home_ownership` | `LA_LCHO_Less_100_Eqty_Own` | Low cost home ownership, less than 100% equity |

The publisher defines the terms (glossary and technical notes) but does not define the sheet's header abbreviations; the mapping of `GN_SC`, `GN_BSp` and `SHHOP` to these words is ours, from the tool's section titles ("Supported housing/housing for older people (social rent)") and the abbreviations. "Owned" is the publisher's term: an RP owns property when it holds the freehold or a leasehold interest of any length and is the body with a direct legal relationship with the occupants. So stock is recorded where it is **owned**, not where it is managed; a provider owning units in an authority is not evidence that it operates there.

`total = the four components` holds on all 10,171 rows as published, is checked on the published cells before anything else, and a row where it fails halts. The CHECK constraint is NULL-aware: where `low_cost_home_ownership` is NULL (not counted, below) the total equals the other three parts, which is what the publisher gave.

## What the supported housing column is, and is not

The tool labels the column "Supported housing/housing for older people" and gives one figure. **The publisher does not split it at local authority level**, and its notes (the technical notes, the data quality note, the tool glossary) do not say how the figure divides between supported housing and housing for older people. This document therefore makes no statement about the share.

What is stated:

- National additional **Table 1.1** (stock owned by registered providers, PRP data weighted) gives supported housing as two lines: social rent 484,312 and Affordable Rent supported housing 22,897, together 507,209. Whether the tool's combined label and Table 1.1 cover exactly the same units is not stated; the tool's own note says its general needs and "SH/HOP" figures "include intermediate and Affordable Rent units".
- Table 1.1 also splits stock nationally into general needs social rent, Affordable Rent general needs, supported housing social rent, Affordable Rent supported housing and low cost home ownership; the **loaded** column has no such split, nationally or by authority.
- Additional **Table 1.20** gives weighted owned social stock by local authority (one total; the 296 values sum to 4,537,377).
- The glossary: units count as supported housing only if they meet the definition of supported housing in the policy statement on rents for social housing, and the fact that a tenant receives support services in their home does not make it supported housing.

So read the column under the tool's label. The 504,902 national units are the unweighted sum of that column; 507,209 is Table 1.1's weighted supported housing.

## Values (rule 1)

The publisher documents no suppression marker or missing-data notation for these columns (the tool notes, glossary, technical notes and data quality note say none), and every stock cell in the 2025 file is an integer: all 50,855 stock cells of the 10,171 provider rows and the 296 subtotal rows likewise; no blank, string, negative or non-integer. So a stock cell must be a non-negative whole number, and **a blank, text, a negative or a non-integer halts**, naming sheet, row and column. No value goes through `or 0` or `COALESCE(x, 0)`. Four stock columns stay `NOT NULL`; `low_cost_home_ownership` is nullable for one reason only, the not-counted rule below, and a CHECK holds every NULL there to its reason in `null_reasons` (and a reason to a NULL). A blank `SDR_Size` or `Survey_Status` is stored NULL, never an empty string.

Published zeros are zeros: 12 rows have a total of 0, all LARPs, which the tool's Area Summary note describes as "registered but does not currently own any stock". The old build's `num()` returned 0 for a blank cell; it never fired.

Two things to know about zeros:

- **Low cost home ownership for Small PRPs is not counted, so it is NULL (decided by Scott, 2026-10-10).** The tool's Area Summary, under Tables 1 and 2 (the stock tables), says: "Unit counts for LCHO are for LARPs and Large PRPs only." The tool's "LARPs and PRPs in region" sheet heads its LCHO column the same way ("LARPs and large PRPs only - unweighted"). All 2,738 Small PRP (`RP_Type` `Small`, Short Form) rows publish 0 there, and on the publisher's own note that 0 means "not counted", not "owns none". Small PRPs do hold LCHO: Additional Table 1.1 puts PRP LCHO at 276,352, against 267,072 for the loaded Large PRPs (274,171 less LARP 7,099), which leaves about 9,280 units held by Small PRPs (derived by review, not published). Rule 1 says not counted is NULL, never 0, so the loader stores a Small PRP's LCHO cell as NULL with `null_reasons` `low_cost_home_ownership=not_counted_for_this_provider_type`. It is a transformation rule in the parser, applied on every load, so a re-read never turns the cells back into 0. The note names LCHO only: general needs and supported housing/housing for older people are counted for every provider type and are not touched. (The tool's other "LARPs and Large PRPs only" notes sit under the rent tables 3 to 6, which are not loaded.) LARP and Large PRP LCHO zeros (125 and 2,700 rows in 2025) are counted zeros and stay 0. The rule halts, rather than guess, on a Small PRP row that publishes an LCHO number, or on a file whose Area Summary no longer carries the note.
- **`total_social_stock` for those 2,738 rows is the publisher's total, kept as given: it does not include the provider's LCHO**, because the publisher did not count it. Read it as general needs plus supported housing and housing for older people for those providers. Summing LCHO by authority gives the LARP and Large PRP count only, which is what the publisher counted.
- Edition 1 of 2025-03-31 ("as loaded") keeps the published zeros, as the file gave them; edition 2 holds the rule's NULLs (see Editions).
- `survey_status` is stored as published, in two spellings: `Signed_Off` (9,943 PRP rows) and `Signed-Off` (228 LARP rows).

## Unweighted versus weighted

Loaded rows are **unweighted**: the tool's Area Summary says its tables "comprise all LARPs & PRPs - unweighted". The publisher's national tables (Table 1.1 and Table 1.4, titled "PRP data weighted"; note 2 of Table 1.1: "All PRP figures weighted for non-responses") are weighted. The data quality note gives the reason: in 2025 the SDR non-response rate was 3.7%, all the excluded PRPs owned fewer than 1,000 units, and "data is weighted to account for this small proportion". The LADR response rate was 100% and nothing was imputed.

| Measure | Loaded (unweighted) | Published (Table 1.1, weighted) |
|---|---:|---:|
| Total social stock | 4,533,055 | 4,546,653 |
| Supported housing (and older people) | 504,902 | 507,209 |
| Low cost home ownership | 274,171 | 283,451 |

The verify script reconciles against the publisher's unweighted LA subtotals, which is a real comparison, and shows the weighted figures as context without asserting equality. Table 1.20's weighted authority totals sum to 4,537,377, equal to neither.

## Reconciliation, from the file on every load

Exact, with no tolerance: provider rows sum to the 296 LA subtotal rows on all five measures for every authority; the LA subtotals sum to the 9 region rows; the region rows sum to England (4,533,055 total; 3,738,818 general needs self-contained; 15,164 bedspaces; 504,902 supported housing and older people; 274,171 LCHO). A failure rejects the file.

## Barnsley and Sheffield

The 2025 file (stock at 31 March 2025) carries them as E08000016 and E08000019 only (`DATASET_FORM['23'] = 'old'`; 29 and 47 rows). Codes are resolved with `geography.resolve('23', ...)` (rule 4.5); `la_boundaries` holds 296 codes and a code outside it and the recode rows is UNEXPLAINED and stops the load.

**Expectation for the 2026 file:** it describes 31 March 2026, after the 1 April 2025 change, so it may carry E08000038 and E08000039. If it does, `check_forms` fails on `old` and the load stops. The fix is deliberate and is a declaration change with evidence, not a guess: set `DATASET_FORM['23']` to `mixed` (2025 old, 2026 new, one form per period), commit with the evidence, re-run.

## Editions and revisions

An edition is what one look-up tool file says about one stock date. Edition 1 of 2025-03-31 is the data exactly as it was held on 2026-10-09 (10,171 rows), proved equal to a re-read of the held file as published (0 differences in 122,052 cells). Edition 2 (2026-10-10) is the same file read with the not-counted rule: it differs from edition 1 in exactly 2,738 cells, every Small PRP's `low_cost_home_ownership` 0 to NULL with its reason, and in nothing else; the live table equals edition 2. The compared columns are the five stock columns plus `rp_name`, `provider_type`, `rp_size_band`, `survey_status`, `publisher_la_code`, `la_name` and `null_reasons`; the five provenance columns (`edition`, `publication_date`, `source_url`, `source_file`, `release_page_url`) are stored per edition and are set on every live row of a period, but never decide whether a file is new. A same-content file adds a ledger row only, never an edition. A file with the bytes of the file the tip came from (by sha256 in the ledger) keeps the tip's provenance, however it was read (so a `--file --no-page` re-read does not record "not read").

The live table moves only by inserting a **new stock date** (edition 1, with its ledger row, in one transaction) and by `refresh-latest --commit`, which copies the tip edition into the live rows of a period (and `loaded_at` from the edition). A reissued tool for a held stock date is stored as the next edition and waits for `refresh-latest`; if it adds or drops providers, `refresh-latest --accept-key-changes YYYY-MM-DD` is needed (the preview lists the keys).

Revision evidence: the publisher has no scheduled revisions; it says it will republish "in the April of the year following the initial publication, if the aggregate changes made by providers require a major revision", corrects substantial errors "as soon as is practical", and "Revisions will normally only be made to the previous year's data". No April 2026 republication happened (the release page's change history is "First published."). The 2025 release revised LARP stock for 2020 to 2024, headline figures only, marked R in the additional tables; "the overall impact on total stock in any year was less than 0.4%". Earlier years' look-up tools were not reissued. The additional tables workbook changed three times under new media ids (versions 1.1, 1.2 and 1.3) with only its own Version History saying so, which is why the ledger keys on the final URL **and** the sha256.

### Stop conditions (enforced in `load`)

Two kinds of stop. A **halt** (identity, header, value, reconciliation or geography failure, or a release title that does not fit) stops the whole run before any stock date is planned: nothing is stored, no ledger row, no run-log row, exit 1, and the message names what was seen. A stock date breaking a **threshold** is REJECTED: nothing stored for it, no ledger row, exit 1, and on `--commit` the run log records the run as partial with the period named. The thresholds are constants in the loader.

| Case | Rejected when |
|---|---|
| any (a halt: no run-log row) | identity, header, value, reconciliation or geography failure |
| new stock date | fewer than 296 authorities; national total social stock moves more than 5% from the previous period, or supported housing and older people more than 10%; any authority's total moves more than 25% |
| revised stock date, 0/NULL | any cell going from 0 to NULL or NULL to 0 against the tip (rule 1.10). Released only by a named, decided acknowledgement, `--acknowledge-flips NAME`, which covers exactly the cells it records in `ACKNOWLEDGED_FLIPS` (stock date, column, direction, reason and count); any other change, or any other count, is still rejected. One exists: `not-counted-lcho-2025` (2025-03-31, `low_cost_home_ownership` 0 to NULL with the not-counted reason, 2,738 cells; Scott, 2026-10-10) |
| revised stock date | the national total of any stock column moves more than 1%; more than 30 authorities' totals change; any authority's total moves more than 10%; more than 5% of provider rows are added or removed; an authority of the tip missing from the file (a missing authority always stops). A reissued file that drops providers by 5% or less passes these checks and is stored as the next edition; `refresh-latest` then needs `--accept-key-changes PERIOD` before it reaches the live table, and the preview lists the keys |

For scale, 2024 to 2025 moved the national total by +0.96%, supported housing by -1.0% and the largest authority by +10.0%.

## How to run

Run from `ONS_Population_Estimates`; see the S23 section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s23_rsh_stock_editions.py status
python scripts/s23_rsh_stock_editions.py load            # preview; downloads the newest release
python scripts/s23_rsh_stock_editions.py load --commit
python scripts/s23_rsh_stock_editions.py refresh-latest   # preview; --commit applies a revision
python scripts/s23_rsh_stock_editions_verify.py
```

Other commands: `ddl`, `restore-edition YYYY-MM-DD N`, and the one-off `migrate-legacy FILE` (already run). `load` takes `--release Y1-Y2`, `--file PATH` (with `--no-page`), `--recheck YYYY-MM-DD`, `--allow-older-file` and `--acknowledge-flips NAME`; every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**If S23 is ever wired into W1:** run `refresh-latest --commit` in the same session as `load --commit` and before any W1 run. `refresh-latest` copies the edition's time into the live `loaded_at`; if W1 ran in between, `refresh_map.py --check` would call the map current while it still showed the unrevised stock date. Today none of this applies, because the map does not read this table.

## Known traps

- **Not filtering `RP_Type`.** Counts roughly three times over.
- **Reading `LA_SHHOP` as supported housing alone.** The tool labels it "Supported housing/housing for older people"; the publisher does not split it by authority.
- **Treating the release page URL as stable.** It carries the years and changes annually.
- **Treating `stock_date` as current.** The return is a snapshot at 31 March; publication follows about seven months later (28 October 2025 for the 2025 snapshot), so a figure is up to about nineteen months old before the next release.
- **Comparing loaded totals to the published headline.** Unweighted against weighted.
- **Reading a Small PRP's NULL LCHO as 0, or its total as including LCHO.** The publisher did not count it; the total it gave leaves it out.
- **Treating a file name or link as evidence of which release a file is.** Read the cover and the Version History.

## History

The first build (2026-08-14) was two scripts, `s23_rsh_stock_build.py` and `s23_rsh_stock_verify.py`, committed together in ad8e349 and now in `scripts/historical/` with RETIRED guards. It upserted (`INSERT ... ON CONFLICT DO UPDATE`, setting `loaded_at`), created the table on `--load` and wrote with no preview and no `--commit`, kept a same-named raw download whatever its content, and read the edition from one hard-coded release page (the 2024 to 2025 one). It was run once, on 2026-08-14 (run-log id 95, 10,171 rows); every held row carries that run's `loaded_at`, so nothing has been rewritten since, and no earlier S23 load existed, so nothing was overwritten in practice. The code was committed 23 minutes after the run; the run-log row matches the committed code's format, which does not prove the code was identical. Its `num()` would have stored a blank stock cell as 0 and rounded a non-integer; it never fired on the 2025 file.

Earlier text in this document, the old build's docstring and the registry said that a large share of the supported housing and older people figure is sheltered and retirement housing and that it would overstate supported provision by a probably large margin, and that the publisher splits supported housing "nationally, but not the stock". The publisher's notes do not say how the figure divides, and Table 1.1 does split stock nationally; those statements were removed on 2026-10-09. No number changed.
