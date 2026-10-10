# S6 — Home Office asylum support by local authority

Rewritten 2026-10-10 when S6 moved onto the period-editions engine. Every
definition below is taken from the publisher's own `Notes` and `List_of_Fields`
sheets in the held files (`data/raw/s6_asylum/`, year ending June 2026) and is
marked with the note number; a statement that is the pipeline's own, not the
publisher's, says so. The record of the move is
[`decisions/2026-10-10-s6-editions-first-load.md`](decisions/2026-10-10-s6-editions-first-load.md).

## Publisher and publications

| | S6a | S6b |
|---|---|---|
| **Publisher** | Home Office | Home Office and MHCLG |
| **Publication** | Immigration system statistics data tables: Asy_D11, *Asylum seekers in receipt of Home Office support by Local Authority* (quarterly time series) | Regional and local authority data on immigration groups: Reg_02, *Immigration groups, by Local Authority* (one snapshot per file) |
| **GOV.UK page** | `https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-data-tables` | `https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-regional-and-local-authority-data` |
| **Format** | `.xlsx` | `.ods`, or `.xlsx` (the year ending March 2025 file) |
| **Held release** | year ending June 2026, published 27 August 2026 | year ending March 2026 and year ending June 2026 |
| **Native geography** | LAD code in the file | LTLA (ONS code) |
| **Cadence** | Quarterly, Thursdays 09:30 (27 November 2025, 26 February, 21 May, 27 August 2026) | Same days |
| **Next update** | 26 November 2026 (year ending September 2026, new period 2026-09-30) | Same |

A third file, **Asy_D09** (*Asylum seekers in receipt of Home Office support*,
by nationality, region, support type, accommodation type and UK region) is
downloaded and read on every `load` as the independent reconciliation for the
Asy_D11 totals. It is never loaded into a table.

## Discovery and file identity

The loader reads both GOV.UK pages through the content API
(`/api/content/government/statistical-data-sets/...`). It does not read link
text from the HTML and it hard-codes no asset URL.

- The data tables page must be titled "Immigration system statistics data
  tables" and carry exactly one `.xlsx` attachment titled "Asylum seekers in
  receipt of Home Office support by local authority detailed datasets, year
  ending <Month YYYY>" (Asy_D11) and exactly one "... support detailed
  datasets, year ending <Month YYYY>" (Asy_D09). **That page lists only the
  newest** of each, so a quarter that is missed cannot be loaded from the page
  later; `--file` with an archived copy is the route.
- The regional page must be titled "Regional and local authority data on
  immigration groups". It lists every quarter since March 2023 (14 attachments
  today, `.ods` or `.xlsx`, one per year-ending); two files claiming one
  quarter, or a title that no longer matches, halt and list the titles seen.
  `--release "Month YYYY"` picks another quarter.
- Every run prints the release it saw as newest for each publication and the
  page's latest change note. After the held file's "Next update" date, if
  nothing newer is listed, it prints a WARNING naming that date.
- **Old asset URLs 301-redirect to the newest file** (the March 2026 Asy_D11
  URL serves the June 2026 file). A URL is therefore not evidence of a
  release. The loader records the final URL and reads the identity from the
  file itself: the cover sheet (title, "year ending <Month yyyy>",
  "Published: <d Month yyyy>", "Next update: ..."), Asy_D11's `Contents`
  "Period covered" and its data sheet's latest date, and Reg_02's title date and
  header row. The rank of a file is (year-ending date, published date) from its
  cover, never its name, URL, media id or the link text.
- **An older file never replaces a newer one**, per period, on every path
  (page, `--release`, `--file`). Equal rank with different content stops unless
  the period is named with `--accept-reissue PERIOD`: the publisher's November
  2025 reissues of the Reg_02 March 2024 and September 2024 files kept their old
  cover dates ("Published: 22 August 2024" and "16 December 2024").
- Files are kept in `data/raw/s6_asylum/` (git-ignored) under their own names;
  a download never overwrites a same-named file with different content.

## Tables, grain and what they hold

Four live tables, each the **latest-edition layer** of an append-only editions
table (`<table>_editions`, with a file-check ledger `<table>_editions_file_checks`).
The live tables keep their names, keys and columns.

| Live table | Source | Grain (natural key with `period_ending`) | Rows | People |
|---|---|---|---:|---:|
| `la_asylum_support` | Asy_D11, English authorities | `lad24cd, support_type, accommodation_type` | 21,953 | 2,245,677 |
| `la_asylum_support_unallocated` | Asy_D11, rows with no local authority | `support_type, accommodation_type, na_reason` | 84 | 225,515 |
| `asylum_support_non_england` | Asy_D11, Scotland, Wales, Northern Ireland | `lad_code, support_type, accommodation_type` | 2,553 | 342,947 |
| `la_immigration_groups` | Reg_02, English authorities | `lad24cd, pathway, sub_pathway` | 7,104 | 1,225,707 |
| `asylum_series_breaks` | the pipeline's own reference rows | `break_id` | 2 | n/a |
| `vw_la_asylum_support_totals` | view over `la_asylum_support` | | 8,157 | n/a |

(Rows and people as held on 2026-10-10, 34 quarters for the three Asy_D11 tables
and two Reg_02 snapshots.) The three Asy_D11 tables are always applied together,
one quarter at a time, in one savepoint.

**Periods.** Asy_D11 is a time series from 31 March 2014 (50 quarters in the
June 2026 file); S6 loads 31 March 2018 onward (34 quarters to 2026-06-30). The
pipeline starts at 2018 because the publisher says local authority and region
data are available for Section 4 only from 2018 (Asy_D11 note 14) and the file
marks the earlier Section 4 rows `N/A - Section 4 (pre-2018)`. 24,639 of the
file's 29,645 rows are in scope; 5,006 are earlier. Reg_02 holds one snapshot
per loaded file, 3,552 rows each at 2026-03-31 and 2026-06-30.

### Row count reconciliation (June 2026 file)

```
 24,639  Asy_D11 rows in scope (2018-01-01 forward)
-    49  absorbed by summing across 35 keys
         (34 reorganisation merges, 1 same-code duplicate)
= 24,590  rows landed across the three Asy_D11 tables
          (21,953 + 84 + 2,553)
```

People totals are unaffected, because merged and duplicate rows are summed. The
merges (predecessor districts onto one successor unitary, from the Cumbria,
North Yorkshire and Somerset reorganisations of 2023) and the one same-code
duplicate (2023-03-31, Wolverhampton E08000031, Section 98, Dispersal: published
twice, under North West 4 and West Midlands 12, summed to 16) are listed in the
`load` preview and in
[`s6_source_anomalies.md`](s6_source_anomalies.md).

## Definitions (the publisher's words, by note number)

These are the Home Office's definitions; the pipeline adds none.

- **Who is counted.** People in receipt of Home Office support, main applicants
  and dependants (Asy_D11 note 1). Unaccompanied asylum seeking children
  supported by local authorities are excluded (note 8; Reg_02 note 29, which
  adds that DfE publishes UASC in England by local authority).
- **What the number is.** The number of people in receipt of support **as at the
  end of the period**, not the total supported through it (notes 2 and 7). The
  number changes daily (note 7).
- **Which authority.** The local authority (and region) of the person's
  **registered address** (`List_of_Fields`).
- **Support type** (note 3): Section 95 is support for asylum seekers with a
  claim or appeal outstanding, and for failed asylum seekers who had children
  in their household when appeal rights were exhausted. Section 98 is temporary
  accommodation for asylum seekers who would otherwise be destitute and are
  awaiting a verdict on a Section 95 application or are on Section 95 waiting
  for dispersal accommodation. Section 4 is support for those whose claim has
  been finally refused who are destitute and temporarily cannot leave the UK.
- **Accommodation type** (note 4): initial accommodation (shelter while a
  support request is assessed), dispersal accommodation (longer term, for those
  whose support claim has been agreed), contingency accommodation (temporary,
  including hotels, used when initial or dispersal accommodation is
  insufficient), other accommodation (alternative sites including Bibby
  Stockholm, Wethersfield and Crowborough, plus a very small number whose
  accommodation type could not be determined). **Subsistence only** is cash
  support without accommodation (note 5).
- **Section 4 before 31 March 2023** is all shown as dispersal accommodation,
  because Section 4 regulations require recipients to be accommodated; the
  publisher adds that since March 2020 some have been housed in contingency
  hotels (note 6).
- **Provisional.** The Atlas casework system began on 12 March 2018; data from
  2018 onward "should be considered provisional" until the transitional work is
  complete (note 12).
- **Geography coverage by support type** (notes 14 to 16): Section 4 has local
  authority and region data from 2018; Section 98 only from 31 December 2022;
  none for subsistence only from 31 December 2023 to 31 December 2024. The
  authority list changes with local government reorganisation (note 9).

The accommodation spellings in the file are normalised to title case, so
`Subsistence only` (252 rows in the June 2026 file) and `Subsistence Only` are
one value. The six stored values are Dispersal Accommodation, Initial
Accommodation, Contingency Accommodation - Hotel, Contingency Accommodation -
Other, Other Accommodation and Subsistence Only. A new value, a changed header
or a new support type stops the load.

### Reg_02 (immigration groups)

Reg_02 is the Home Office's local authority table for groups of interest to
authorities (Reg_02 note 1). It is built from local management information and
"should be treated as provisional" (note 3); the data are as at the last day of
the quarter or the closest date possible, and can change daily (note 4). Each
authority has 12 stored rows: `homes_for_ukraine` (total), `afghan_resettlement`
(total, transitional, settled_la_housing, settled_prs_housing),
`supported_asylum` (total, initial_accommodation, dispersal, contingency,
other, subsistence_only) and `all_pathways` (total).

- **Homes for Ukraine** counts **arrivals**, by the sponsor's or accommodation
  address postcode, and is not wholly comparable with the Afghan and asylum
  columns, which are stock populations (Homes for Ukraine note 1). Super-sponsor
  arrivals have no local authority breakdown in Reg_02 (note 7).
- **Afghan Resettlement Programme** and **supported asylum** are stock
  populations at the last day of the quarter (Afghan note 11; asylum note 22). For supported asylum the
  location is the last recorded correspondence, so it may include people being moved
  or who have recently moved (note 23).
- **Population and percentage.** Each authority row carries the published
  `Population`, stored on every row of the authority. The published
  `Percentage of population (%)` is stored on the `all_pathways` / `total` row
  as published: a **ratio** (a value such as 0.0050, not 0.50), despite the
  "(%)" in the header, rounded to four decimals by the column type
  (`numeric(8,4)`). It is the all-pathways total divided by `Population`
  (City of London: 7 / 15,631 = 0.000448).

## Values and markers

**Asy_D11.** The publisher documents no marker and no zero. In the June 2026
file every `People` value is a whole number of at least 1. The loader therefore
halts on a blank, zero, negative, text or non-integer `People` cell, naming the
sheet, row and column; `people` stays `NOT NULL`. No claim is made about a `-`
in Asy_D11.

**Reg_02.** The publisher defines one marker: `*` means fewer than 5 people,
suppressed for disclosure control, with secondary suppression "where needed";
suppressed figures are left out of "All pathways (total)" (Homes for Ukraine
note 9). A `*` is stored as `people` NULL, `suppressed` true and
`source_marker` `*`; a published 0 stays 0. In the June 2026 file there are 2
`*` cells (both Homes for Ukraine: City of London and Isles of Scilly) and 1,310
published zeros. **Any other non-integer cell in a pathway column (blank, `-`,
`:`, text) halts.** The publisher's notes do not define `-`, a blank or a zero;
`-` appears in the June 2026 file only in the `Unknown` row's population and
percentage cells, and that row is not loaded.

Every load reports, per column, the NULL, `*` and zero counts, and a Reg_02 cell
that moves between 0 and `*` against the held edition needs
`--acknowledge PERIOD`.

**City of London and Isles of Scilly.** The publisher says that in Reg_02 the
all-pathways totals and the per-capita percentages for these two authorities do
not include Homes for Ukraine arrivals, because of suppression; the suppressed
figures are in Reg_01's totals (Reg_02 note 5). The `all_pathways` / `total`
row of each carries a `source_marker` beginning `LOWER BOUND`. **That text is
the pipeline's marker, written by the old build and kept exactly as held; it is
not a publisher statement.** What the publisher states is note 5 and the
`*` definition above; "higher by between 1 and 4" is the held marker's
reading of "fewer than 5", and the publisher's secondary suppression note means
the true figure cannot be assumed to follow it exactly. No other authority is
affected (294 of 296 `all_pathways` rows carry no marker).

**An authority with no row.** Asy_D11 lists an authority in a period only where
it has people in the table, and the minimum published value is 1. The pipeline
stores no row for an authority the file does not list, so absence in these
tables means **no published figure**, and no zero is stored for it. This is how
the data is held; the publisher's notes do not say what an absent row means.
Reg_02, in contrast, lists every English authority (296) and shows 0 supported
asylum for each authority that Asy_D11 does not list (this holds on both held
files). Coverage is therefore reported as a count and never gated against 296
for Asy_D11.

### Rows with no local authority

Where an Asy_D11 row has no usable local authority, the pipeline stores it in
`la_asylum_support_unallocated` with the verbatim source text in `na_reason`,
and `accommodation_type` `not_stated` where the source gives none. Three
reasons occur:

| `na_reason` | Rows | People | Periods |
|---|---:|---:|---|
| `N/A - Section 98 (pre-Dec 2022)` | 19 | 202,558 | 2018-03-31 to 2022-09-30 |
| `N/A - Subsistence Only (Dec 2023 - Dec 2024)` | 5 | 18,598 | 2023-12-31 to 2024-12-31 |
| `Unknown` | 60 | 4,359 | 2018-06-30 to 2023-09-30 |

The first two follow the publisher's notes 15 and 16. The `Unknown` rows are
published as such. Asy_D11 also carries a region column, `UK Region / Nation`,
which is **not stored**: five LAD codes carry more than one region across 2018
onward (Middlesbrough E06000002, Herefordshire E06000019, South Cambridgeshire
E07000012, North Devon E07000043, Wolverhampton E08000031; checked 2026-10-10,
comparing case-insensitively), so region comes from `la_boundaries`.

## Series breaks (as held)

Two reporting changes make the `la_asylum_support` total non-comparable across
parts of the window. They are held in `asylum_series_breaks` (two rows, checked
by the verify script) because someone querying `vw_la_asylum_support_totals`
will not read prose.

| First period | Last period | Support type | What changed |
|---|---|---|---|
| 2022-12-31 | ongoing | Section 98 | Gained local authority geography (note 15). Before this all Section 98 people are in the unallocated table. |
| 2023-12-31 | 2024-12-31 | Section 95 | Subsistence only lost local authority geography for five quarters (note 16); those people are in the unallocated table. |

- The `la_asylum_support` total rises from 53,749 to 98,375 between 2022-09-30
  and 2022-12-31. That is a reporting change (37,142 Section 98 people sat in
  the unallocated table at 2022-09-30), not arrivals.
- The count of authorities present falls from 273 at 2023-09-30 to 244 at
  2023-12-31 and 237 at 2024-03-31, and is 279 at 2025-03-31. 32 English
  authorities appear at 2023-09-30 only through subsistence-only rows; 26 of
  them are absent at 2023-12-31 and 13 are absent in all five periods (checked
  2026-10-10). The held `asylum_series_breaks` text for this break says all 32
  disappear from 2023-12-31, which overstates it by 6; the row is left as held.
- **The first period with local authority data for every support type is
  2025-03-31** (notes 14 to 16), and from then the unallocated table has no new
  rows.

## Reconciliations (from the files, on every `load`)

- The Asy_D11 data sheet equals the pivot sheet's cached figures for the latest
  eight quarters, by region and in the Grand Total (97,519 at 2026-03-31 and
  93,293 at 2026-06-30).
- England + non-England + unallocated equals the data sheet total in every
  period, and England and unallocated equal Asy_D09 in every period.
- Reg_02: each pathway total equals the sum of its "of which" parts, there are
  exactly 296 English authorities, and the supported-asylum column equals
  Asy_D11 for each authority (296 of 296 at March and June 2026).

## Geography

Codes go through `geography.resolve('6', ...)`, per table and period, never a
private dictionary. `DATASET_FORM['6']` is `mixed` (one form per table per
period):

- Asy_D11 uses Barnsley and Sheffield as E08000016 and E08000019 from 31 March
  2014 to 30 September 2025 and E08000038 and E08000039 from 31 December 2025 to
  30 June 2026; never both in one period. The switch follows the publication,
  not the 1 April 2025 boundary date (the June and September 2025 quarters still
  use the old codes). Reg_02 uses the old codes at March 2026 and the new at
  June 2026.
- The tables hold the old codes throughout (`la_boundaries` is LAD May 2024 and
  has no E08000038 or E08000039).
- The district codes of the 2023 Cumbria, North Yorkshire and Somerset
  reorganisations (14 codes seen up to 2023-03-31) resolve forward through
  `la_code_lookup` rows of type `new_unitary` that have exactly one target in
  `la_boundaries`. Anything else is UNEXPLAINED and stops the load.
- Non-England codes (S12, W06, N09) are stored as published in
  `asylum_support_non_england.lad_code`, with `country` from the prefix.

### History: the Cumbria workaround (removed 26 July 2026)

The original build (25 July 2026) carried its own resolution layer for three
codes that `la_code_lookup` then handled wrongly or not at all: E07000027
(Barrow-in-Furness), E07000028 (Carlisle) and E07000189 (South Somerset). Until
the lookup was corrected, S6 was the only source resolving them correctly. The
lookup was corrected on 26 July 2026 (see
[`decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md`](decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md)
and
[`decisions/2026-07-26-la-code-lookup-full-audit.md`](decisions/2026-07-26-la-code-lookup-full-audit.md)),
the local recodes were removed in commit a975f50 that evening, and the reload
reproduced the earlier checksum byte for byte. There is no S6-specific
geography layer now; the three codes resolve through `la_code_lookup` like the
others.

## Editions

An edition is what one file says about one period of one table. A held period
that a file restates unchanged gets a ledger row only; a changed period is
stored as the next edition and reaches the live table only through
`refresh-latest`; a new period is stored as edition 1 with its live rows in the
same transaction. Edition 1 of each table is the data exactly as held on
2026-10-10 (34 + 28 + 34 + 2 periods; 31,694 rows), and the held files read
again reproduce every held row and every stored column (0 differences).

Each live table's `source_edition` is one label, the cover's "year ending
<Month yyyy>", set on every row of a refreshed period; it is never compared.

Where several rows reach one key (a reorganisation merge, or the same code
published twice), `published_la_name` (and `source_marker` and `country`) comes
from the row with the **highest publisher code**, ties broken by the greatest
name and then marker, never from file order. Of the rules tried against the held
names, only this one reproduced every held name; "last row in the file" failed
on the December 2025 file, which orders its rows differently.

What the publisher has revised, so far (and the reason the series is flagged
`revises_back_series`):

| When | What changed |
|---|---|
| Asy_D11 December 2025, March 2026, June 2026 files | No cell in any shared period (48, then 49 periods); each release added one quarter |
| Asy_D11 13 June 2024 | "Second edition": accommodation types revised and corrections to the stated geographical distribution; totals unchanged (also Asy_D09 and Reg_02) |
| Reg_02 22 August 2024 | Earlier files revised (support and accommodation type, geography) |
| Reg_02 16 December 2024 | Afghan figures revised (232 Northern Ireland cases) |
| Reg_02 27 November 2025 | March 2024 and September 2024 files reissued ("minor revision" to accommodation types; totals unaffected), covers not updated |

The Asy_D11 revision of June 2024 predates the first load, so it is not in the
editions tables, and the earlier Reg_02 snapshots are not loaded (follow-up in
the decision note).

## Stop conditions

A period that breaks a condition is REJECTED: nothing is stored for it, no
ledger row is written and the run exits 1. `--acknowledge PERIOD` releases a
threshold breach; it never releases a partial file.

| Condition | Limit |
|---|---|
| New Asy_D11 quarter, England total against the previous period | 15% |
| New quarter, authority count against the previous period | 30 |
| New quarter, any authority's total | 1,500 people |
| Revision, a table's total | 2% |
| Revision, authorities whose totals change | 40 |
| Revision, any authority's total | 500 people |
| Revision, rows added or removed | 10% of the held rows |
| PARTIAL FILE: authorities fewer than the held edition | 10 (never released) |
| PARTIAL FILE: rows fewer than the held edition | 10% (never released) |
| Same-code duplicate keys in one file | more than 5 halts |
| Reg_02 English authorities | exactly 296 |
| Reg_02 reissue, any pathway's England total | 5% |
| Reg_02 reissue, authorities changing | 30 |

These were calibrated on 2024-12-31 to 2026-06-30 (national moves of -9.7% to
+5.5% a quarter; the June 2024 revision left totals unchanged). They are the
loader's own choices, not the publisher's.

## Running it

From `ONS_Population_Estimates`. Everything previews unless `--commit` is given.

```
python scripts/s6_asylum_editions.py status
python scripts/s6_asylum_editions.py load                  # preview; downloads to data/raw/s6_asylum
python scripts/s6_asylum_editions.py load --commit
python scripts/s6_asylum_editions.py refresh-latest        # preview; then --commit
python scripts/s6_asylum_editions_verify.py                # 22 gates, writes nothing
```

The procedure each quarter, and what each halt means, is in
[`QUARTERLY_REFRESH.md`](QUARTERLY_REFRESH.md). There is no `sync-new`:
`migrate-legacy` (one-off, already run) recorded the held periods.

## Known caveats

- Figures are people at the end of each quarter, by registered address; they are
  not a count of all asylum seekers in an area, because UASC supported by local
  authorities are excluded.
- Asy_D11 covers the whole UK; this pipeline holds English authorities in
  `la_asylum_support` and Scotland, Wales and Northern Ireland in
  `asylum_support_non_england`, so the totals reconcile.
- Data from 2018 onward is provisional (Asy_D11 note 12). Reg_02 is extracted
  from local management information and databases and is provisional (note 3);
  the publisher says the Homes for Ukraine and Afghan data have not been quality
  assured to the level of Official Statistics (Homes for Ukraine note 8, Afghan
  note 12).
- Homes for Ukraine in Reg_02 counts arrivals, and it is not comparable with the
  stock columns.

## Scope

S6 is **standalone**. It is not wired into Workflow 1, adds no column to
`staging_la_signals`, adds no tenant type and no map layer, and no score or
ranking combines it with another source. If it is ever wired in, run
`refresh-latest --commit` in the same session before W1, because refresh copies
the edition's `loaded_at` and `refresh_map.py` would otherwise not see a revision
as new.
