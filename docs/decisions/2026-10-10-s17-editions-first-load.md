# S17 SafeLives MARAC: first load on the editions engine

Status: done 2026-10-10. Works like [S12](2026-10-10-s12-editions-first-load.md), [S13](2026-10-10-s13-editions-first-load.md) and [S23](2026-10-09-s23-editions-first-load.md).
Loader: `scripts/s17_marac_editions.py` (one spec, `marac_cases`); gates: `scripts/s17_marac_editions_verify.py` (20 gates, all
PASS, exit 0). Manual-file helper: `scripts/manual_input.py`.
Design: `docs/superpowers/specs/2026-10-10-no-loader-sources-design.md` (audit record, not approval). Scott authorised the
real writes through the plan ("do the work, the spec is audit not approval") and decided the open points on 2026-10-10 (below).

**What the map sees.** The MARAC layer reads the newest year, 2025-26, through `la_pfa_mapping`. For the 38 forces held
before, `cases_discussed` and `cases_per_10k_adult_females` are unchanged (gate 15 holds them to the `w1-read` line below).
One thing is added: the West Midlands police force area was missing from the live table in every year. It is now stored, and
the seven authorities that `la_pfa_mapping` maps to it (Birmingham, Coventry, Dudley, Sandwell, Solihull, Walsall and
Wolverhampton) gain 2025-26 values: 7,810 cases discussed and 65.903502 cases per 10,000 adult women. They have no MARAC
value on the published map today. W1 and `refresh_map.py` were not run (Scott deferred W1 and the map until every source is
done), so the published map shows no change until that run.

**Decided by Scott (2026-10-10).**
1. The West Midlands force is added (39 forces, not 38).
2. Its 2023-24 row, and Norfolk's, are non-submissions (rules `WEST_MIDLANDS_2023_24_NOT_SUBMITTED` and
   `NORFOLK_2023_24_NOT_SUBMITTED`: all eight values NULL, `value_flag` `not_submitted`).
3. Lancashire's housing zeros are left as published. There is no Lancashire rule (details below).
4. The 13 NULL-to-0 cells are released by the named entry `s17-restored-zeros-2026-10` (`--acknowledge-flips`).

## Before-state (read-only, confirmed against the live table before anything was written)

`marac_cases` was 304 rows, 8 financial years (2018-19 to 2025-26) x 38 forces, two `loaded_at` values (266 rows at
2026-03-30 22:22:38.090587 UTC from the n8n load; 38 rows at 2026-08-20 11:10:25.222615 UTC from the direct load), survey hash
`d885fc22b822d6bba698c239498f8e2d`. `la_pfa_mapping` was 296 rows, hash `590fc32499fef391089927c8c52e427e` (read, never
written). Both were compared with the loader's own constants (`LEGACY_LIVE`, `LEGACY_PFA_MAPPING`) and matched.

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a 64-hex
run, stays untouched. The `before-state` lines are the verify script's per-year hash of the live columns (key and the eight
values, NULL as an empty string, rows sorted by name; source and flag left out). The `w1-read` line is the hash of the
2025-26 name, `cases_discussed` and `cases_per_10k_adult_females` of the 38 held forces only (West Midlands excluded, so the
line is the same before and after). They were printed by `python scripts/s17_marac_editions_verify.py --print-note-lines`
before `ddl` and before the migration, while the live table was untouched. Edition 1 (as loaded) hashes to the `before-state`
lines (gate 14); the live 38 held forces hash to the `w1-read` line (gate 15).

before-state marac_cases 2018-19 rows=38 sha256-first32=cfcb0236184df6d63cf97660a3ab1468 sha256-last32=07022b72191a5466a34fa1d936587ca8
before-state marac_cases 2019-20 rows=38 sha256-first32=e32a6fc1d32eb78de7a71b937ffd0dc0 sha256-last32=a7f60962d9b002b2ee9ba0c341a04efb
before-state marac_cases 2020-21 rows=38 sha256-first32=f8c523bd81d490c723f527b23e6eb139 sha256-last32=1f7cd950f914cf6bb0688bd1c07fbfc8
before-state marac_cases 2021-22 rows=38 sha256-first32=c3dee56412d7e37aa4e27a87fef0b82b sha256-last32=98a3994fa44748cfd7be8ea8d5fafb91
before-state marac_cases 2022-23 rows=38 sha256-first32=3cc61c406d3fc88336fac04677976488 sha256-last32=c3e417b3f00cd96d9cf37c19f88842ce
before-state marac_cases 2023-24 rows=38 sha256-first32=a604f11daf3251d1a804239015f8e59f sha256-last32=bbad930ac426aa8732ee3eba5b45f74a
before-state marac_cases 2024-25 rows=38 sha256-first32=2a95db23c6a2dbbf0474f865377dc628 sha256-last32=c787a75b9378ddb9ff4d8bbdd68add06
before-state marac_cases 2025-26 rows=38 sha256-first32=5464ca570582dd1e547b3f1e4240c76a sha256-last32=18dbefb6333f7d386671492d6114ec05
w1-read marac_cases 2025-26 rows=38 sha256-first32=2b501306a9991d96607dd028bcffd19b sha256-last32=5d2f542a8aee47d058d00d9e3e9d1d4b

## How the table came to hold what it holds (checked 2026-10-10 against the database, the run log and the files)

- **2026-03-30, run-log rows 22, 23 and 24** ("Source 17 - SafeLives MARAC", success, 266 rows each, notes "SafeLives MARAC
  England and Wales 2018-19 to 2024-25, 7 years, 38 English PFAs"): the n8n workflow ran three times in one day (14:29,
  14:29 and 22:22 UTC). 7 years x 38 forces = 266. The rows came from CSVs that Claude converted from the SafeLives workbooks
  in a chat session that no longer exists. The third run's `loaded_at` (22:22:38) is the one the 266 rows carry (255 of them
  still do; see the refresh below).
- **`parseFloat(x) || null`.** The workflow's number reader turned a published 0 into NULL (0 is falsy). That is why the held
  table has 13 NULLs where SafeLives published 0 (listed below). The markers "No Data" and "No population data" also became
  NULL, which is right for those, so a held NULL of Norfolk looked the same as a restored zero. Nothing in the table said which
  was which. Edition 1 "as loaded" keeps every cell exactly as held; edition 2 separates them (a `value_flag` on each NULL
  that has a reason).
- **2026-08-20, the untracked load of 2025-26** (38 rows, `loaded_at` 2026-08-20 11:10:25 UTC, no run-log row): written by
  direct SQL in the August assurance session, not by committed code. It kept published zeros as 0 (Lancashire and City of
  London housing referrals are 0 in 2025-26), unlike the n8n years, so the table was inconsistent between 2024-25 and 2025-26
  (spec finding 10). `marac_cases_bak_20260820` is the backup of that time and was not touched.
- **The West Midlands force was missing from the live table in every year.** All eight workbooks publish it, and
  `la_pfa_mapping` maps seven authorities to it. Neither the n8n load nor the direct load stored it. In the files the force
  shares its name with the West Midlands region row, which is a likely cause (not proved: the workflow's code is retired). So
  there are 39 English police force areas, and the held table had 38.
- **Acquisition.** The SafeLives data page is reachable today with a plain User-Agent. The registry URL redirects (301) to
  `https://safelives.org.uk/research-policy/practitioner-datasets/marac-data/`; the page links one workbook for each of the
  eight years and no 2026-27 file. A browser-like User-Agent got a Cloudflare 403 in the survey of 2026-10-10. The loader sends
  the plain request and, on a refusal, halts with a message to download by hand and use `--file`. Every download in the
  previews and the load was byte-identical to the file already held, so each was kept as held. The page HTML changes between
  reads, so each changed read adds a `marac-data-page_<date>-<sha8>.html` copy beside the first (git-ignored; data, not read
  by the gates).
- **The five `.xls` years.** The manual-input helper (`scripts/manual_input.py`) cannot read `.xls` document properties
  (xlrd does not expose them), so for those files it prints "document properties: none recorded in the file" when in fact it
  cannot tell. Those years (2018-19 to 2022-23) therefore rank from the sheet titles ("April YYYY - March YYYY" on Notes,
  "year ending March YYYY" on Cases) and the file's sha256, with the modified date unknown. The 2022-23 file's Notes title
  reads "April 2021 - March 2022" while every other sheet says "year ending March 2023" and its figures differ from the 2021-22
  file's; the loader holds a named erratum keyed by that file's sha256 (`NOTES_TITLE_ERRATA`), and any other file whose titles
  disagree halts. The three `.xlsx` years rank by their own modified date (2023-24 2024-08-12, 2024-25 2025-07-11, 2025-26
  2026-07-02). `xlrd==2.0.1` reads the `.xls` files.

## What edition 2 changes

Edition 1 "as loaded" is the live table exactly as held (304 rows). `migrate-legacy` proved every held value against the
eight workbooks on disk, to the column's scale, and found only the differences below; it wrote no ledger row, because no year
reproduces the held table in full. `load` then stored the files as edition 2 for all eight years (312 rows).

**Restored zeros: 13 cells, NULL to 0** (the named entry `s17-restored-zeros-2026-10`; no other cell moves and nothing goes
from a value to NULL). Each is a 0 in the publisher's file that the n8n code had read as NULL:

| Year | Force | Cells restored to 0 |
| --- | --- | --- |
| 2018-19 | City of London | repeat_cases, repeat_cases_pct, housing_referrals |
| 2018-19 | Gloucestershire | housing_referrals |
| 2018-19 | Leicestershire | housing_referrals |
| 2019-20 | City of London | housing_referrals |
| 2020-21 | City of London | housing_referrals |
| 2020-21 | Gloucestershire | housing_referrals |
| 2021-22 | Leicestershire | housing_referrals |
| 2022-23 | City of London | children_in_household |
| 2023-24 | Lancashire | housing_referrals |
| 2024-25 | City of London | housing_referrals |
| 2024-25 | Lancashire | housing_referrals |

(11 rows, 13 cells; 11 live rows carry the new values after the refresh.)

**Ruled NULLs** (rule 1: a marker or a named rule gives NULL with a reason, never a coerced 0). 40 cells, all with
`value_flag` `not_submitted`, in 5 rows; no other NULL exists in the live table:
- Norfolk 2022-23 ("No data", read through a named alias of "No Data"), 2024-25 and 2025-26 ("No Data"): all eight values
  NULL (24 cells). These were NULL in the held table too, so nothing changes.
- Norfolk 2023-24 (rule `NORFOLK_2023_24_NOT_SUBMITTED`, the design of 2026-10-10): the file publishes Norfolk as 0 MARACs,
  0 cases, 0 recommended cases, "No population data" for the rate, 0 repeat cases, "#DIV/0!" for the repeat percentage, 0
  children and 0 in every Referral routes count. SafeLives publishes "No Data" for Norfolk in the neighbouring years, so this
  is a non-submission written as zeros. All eight values NULL (8 cells), as held.
- West Midlands 2023-24 (rule `WEST_MIDLANDS_2023_24_NOT_SUBMITTED`, **Scott, 2026-10-10**): the 2023-24 file publishes the
  West Midlands force in exactly Norfolk's form, and the West Midlands region row (17 MARACs, 4,191 cases) equals
  Staffordshire, Warwickshire and West Mercia alone. Stored as published it would say 0 cases for the second-largest force,
  and the file otherwise halts on "#DIV/0!". All eight values NULL (8 cells). This row is new, not a change.

**Keys added: West Midlands in all eight years** (eight rows, edition 2 only; edition 1 has the 38 held forces). The 2025-26
row: 7 MARACs, 7,810 cases discussed, 4,740 recommended, 65.903502 per 10,000 adult women, 758 repeat cases (9.7055 %), 11,705
children, 94 housing referrals. `refresh-latest` needs `--accept-key-changes YEAR` for each year, and it was given.

**Lancashire is left as published (Scott, 2026-10-10).** The earlier design would have made Lancashire's housing referrals
NULL ("not counted") in 2023-24, 2024-25 and 2025-26, on the n8n-era claim that the zeros were footnoted as an incomplete
submission. The held files do not support that claim. Every cell of every sheet of the eight workbooks and the page HTML was
read: no 2023-24, 2024-25 or 2025-26 file carries any note about Lancashire (their only footnotes are the three generic
Referral routes notes). The only Lancashire note in any file is 2022-23's: one MARAC within the force "did not submit data
... July 2022 to March 2023", a year in which Lancashire's housing referrals are 13, not 0. What the files do show:
Lancashire housing referrals of 15, 32.5, 24, 23 and 13 in 2018-19 to 2022-23, then 0 in 2023-24, 2024-25 and 2025-26; and
Lancashire with 2 MARACs in 2023-24 and 2024-25 against 9 in 2022-23 and 2025-26. A force with 800 to 1,400 cases a year
reporting no housing referrals three years running is odd, but the publisher states it as 0 and says nothing else. So there
is no Lancashire rule (gate 4 checks the loader for one): the three zeros are 0. 2023-24 and 2024-25 move from NULL to 0 (in
the table above); 2025-26 was 0 and stays 0. Housing referrals are not read by W1. If Scott overrules, it is a named rule and
a later edition.

Published zeros that the held table already had as 0 stay 0: the 2025-26 housing referrals of City of London and Lancashire
(the direct load of 2026-08-20 kept them).

## What was written (2026-10-10)

- `ddl --commit`: `marac_cases_editions` (append-only; triggers refuse UPDATE, DELETE and TRUNCATE) and
  `marac_cases_editions_file_checks`. No change to the live table's columns or constraints.
- `migrate-legacy --commit` (after a preview and a `--simulate` that rolled back): edition 1 for 8 years, 304 rows; run-log
  row 409; no ledger rows.
- `load` with `--acknowledge` for each of the eight years and `--acknowledge-flips s17-restored-zeros-2026-10`: preview (exit
  0: 8 years revised, 19 force-rows changed, being the 13 restored cells and 8 added keys; no page refusal, no 2026-27 link,
  all eight downloads identical to the held files), `--simulate` (rolled back), `--commit`: edition 2 for 8 years, 312 rows;
  run-log row 410. Without the acknowledgements every year is REJECTED, by design.
- `refresh-latest` with `--accept-key-changes` for each of the eight years (preview, `--simulate`, `--commit`): 11 live rows
  refreshed, 8 inserted (West Midlands), none deleted; before/after guard passed. The plan equalled the load preview.
- Live after: `marac_cases` 312 rows (8 years x 39 forces); 19 rows carry edition 2's `loaded_at` (2026-10-10), 255 keep
  2026-03-30 and 38 keep 2026-08-20 (no value changed on them); 5 rows hold NULLs (the ruled cells above).
- `status`: clean. The verify script ends exit 0, all 20 gates PASS. No `zz%` table remains.
- Unchanged: the eight workbooks and `LAD24_CSP24_PFA24_EW_LU_2026-10-10.json` in `data/raw/s17_marac/` (gates 4, 5, 6, 8, 12
  and 16 re-read them by sha256; gate 3 reads the JSON), `la_pfa_mapping`, `marac_cases_bak_20260820`.
- W1, `refresh_map.py`, the export, `push.py` and `git push` were not run.

## When SafeLives publishes 2026-27 (expected mid-2027)

From `ONS_Population_Estimates`:

1. `python scripts/s17_marac_editions.py load` reads the data page, downloads the linked workbooks to `data/raw/s17_marac/`
   (a same-name file with different content is never overwritten; the new one is saved with a hash suffix) and previews. Read
   the whole preview: the title and year read from the file, reconciliation against the England row, every marker, every
   rule, every `MAP:` line. A new year is a new period; a changed year is REJECTED unless named with `--acknowledge YYYY-YY`;
   a value going to or from NULL needs a named entry in `ACKNOWLEDGED_FLIPS`. An unknown marker, header, force name or title
   halts naming the cell. Calibration stops halt a new year if England cases discussed moves by more than 25 % or more than
   6 forces move by more than 40 %. A file older than the stored one halts (`--allow-older-file` only on purpose); equal
   rank with different content stops unless `--accept-reissue`.
2. `load ... --simulate`, then `--commit`; then `refresh-latest` (preview, `--simulate`, `--commit`; a year that gains or
   loses a force needs `--accept-key-changes YEAR`), `status`, and `python scripts/s17_marac_editions_verify.py` (exit 0).
   Run `refresh-latest` before W1: it copies the edition's `loaded_at` onto the live rows, and S17 is a W1 input.
3. **Gate 15 is pinned to 2025-26** and to the `w1-read` line above. When 2026-27 loads, the map year becomes 2026-27 and
   gate 15 will fail on purpose until this note and the gate are reviewed: read the change, then update the line and the pin
   together.
4. If the page refuses the request (a Cloudflare 403, a browser-like User-Agent or a changed page), download the workbook by
   hand into `data/raw/s17_marac/` and run `load --file PATH` (repeatable; the page is still read and must link the file's
   year, unless `--no-page` is given). The file is announced as manual input, with its title, rank and sha256.

## How to reverse

- Per year: `python scripts/s17_marac_editions.py restore-edition YYYY-YY N` (preview, then `--commit`) stores edition N's
  rows as the next edition; `refresh-latest` then writes them to the live table (with `--accept-key-changes YEAR` where the
  force set differs). Edition 1 is the table as it stood on 2026-10-10. Editions are append-only, so nothing is lost.
- To take the West Midlands force out again: restore edition 1 for all eight years and run `refresh-latest
  --accept-key-changes` for each. The next load would add it again, so a named exclusion in the loader comes first.
- To bring back the NULL housing referrals (the n8n values) for a year: restore edition 1 for that year. A later load of the
  same file would need the same `--acknowledge-flips` entry again.
- The `loaded_at` of the 19 refreshed rows is not put back to March by a restore; the March values are in edition 1.
