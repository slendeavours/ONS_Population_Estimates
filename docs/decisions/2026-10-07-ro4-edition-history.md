# RO4 edition history: store releases as editions and bring the live table to the third release of 2024-25

Status: implemented 2026-10-07, corrected the same day (see the incident note). Follows [the S1b edition work](2026-10-06-s1b-edition-history.md), which
left the RO4 key as an open item.

## What

New append-only table `ro4_housing_expenditure_editions` (update, delete and truncate blocked by trigger),
key `(lad24cd, financial_year, edition)`, 1,184 rows (296 authorities in each of four editions). Latest is
the edition no other edition supersedes. The live table `ro4_housing_expenditure` is the latest-edition
layer, refreshed by `python scripts/ro4_editions.py refresh-latest --commit` (the eleven measures,
`la_name`, `data_missing` and `source`; `loaded_at` untouched). `status`, `sync-new` and `load` work as for
S1 and S1b. The releases we hold are listed, with checksums, in `scripts/ro4_editions_manifest.json`; a
later release is loaded with `python scripts/ro4_editions.py load` from a file placed in `data/reference/`,
then `refresh-latest`. `scripts/s2_ro4_load.py apply` stays insert-only and is for a first-ever release of
a financial year. Procedure: [../QUARTERLY_REFRESH.md](../QUARTERLY_REFRESH.md).

| Financial year | Edition | What it is | Stored label | Supersedes |
| --- | --- | --- | --- | --- |
| 2024-25 | 1 | What the live table held, recorded 'as loaded' (published 18 Sep 2025) | 'as loaded; ... published 18 Sep 2025' | none |
| 2024-25 | 2 | **Actually the second release, published 4 December 2025** (the file's own cover sheet) | 'third release, published 11 Jun 2026' (wrong, see below) | 1 |
| 2024-25 | 3 | The third release, published 11 June 2026 (file `RO4_LA_Data_2024-25_third_release.ods`) | 'third release, published 11 Jun 2026' | 2 |
| 2025-26 | 1 | First release, published 17 September 2026 | 'first release, published 17 Sep 2026' | none |

## Why

The key `(lad24cd, financial_year)` holds one release per year, so a revision could only overwrite. The
publisher issues each financial year more than once, a second 2025-26 release is expected, and the 2024-25
rows turned out to differ from the later releases.

## Incident note (2026-10-07): the second release was filed as the third

Edition 2 was loaded from `RO4_LA_Data_2024-25_data_by_LA.ods` under the label 'third release, published
11 Jun 2026'. That label came from a constant in `scripts/s2_ro4_load.py`; nobody opened the file. Its cover
sheet says **second release, published 4 December 2025**. The table is append-only, so the stored label
stays wrong; the manifest now records the stored and the actual label for every entry.

Consequence: for under an hour on 2026-10-07 (edition 2 was loaded at 09:38 UTC, edition 3 at 10:02 UTC) the live 2024-25 rows held second-release values. Eight
authorities (Birmingham, Slough, Warwick, Ashfield, North West Leicestershire, Guildford, Amber Valley,
North Warwickshire), which the second release marks `[x]` (publisher: data missing), were NULL with
`data_missing` true, and the national 2024-25 sums were bed and breakfast gross 664,821.57 and nightly paid
gross 1,029,950.73 (£000) instead of 710,748.00 and 1,070,834.36. **No stored or sent deliverable used those
values:** the stored outputs predate the refresh and used the edition 1 equivalent values, and Workflow 1 and
the map export read only 2025-26. The real third release was then loaded as edition 3 and live refreshed from
it; the eight authorities have figures again and the two national sums are back to the edition 1 values.

Cause: a release label held in a loader constant was trusted without opening the file's own cover sheet.
Prevention: every manifest entry now carries `release_label_stored` / `published_date_stored` (what the
table says) and `release_label_actual` / `published_date_actual` (what the file's cover sheet says), and a
cover-sheet gate (in `load` and in `ro4_editions_verify.py`) refuses a file whose front page disagrees with
its manifest entry. `s2_ro4_load.py` no longer carries the wrong label.

The earlier wording that the third release populates those eight authorities (2026-09-30 record, CHANGELOG)
was right. They are `[x]` only in the second release.

## The finding: 2024-25 edition 1 is not the third release

The live 2024-25 rows were an earlier release, 'as loaded' (published 18 Sep 2025). Edition 3 against
edition 1: 258 of 296 authorities change, 279 cells, all in two columns plus one name. The
`hra_admin_prevention_relief_net_exp_000` column changes on 258 authorities (250 value to value, the known
defect in [2026-09-30-reference-csvs-out-of-step.md](2026-09-30-reference-csvs-out-of-step.md) where the
column held temporary-accommodation administration net spend under the wrong name, and 8 NULL to value for
the eight authorities whose hra cell is NULL in edition 1; their other measures have figures there). 21
`total_housing_*` cells change (11 gross, 10 net): the same eight authorities, NULL in edition 1, to value, and Leicester City, Enfield (a 0.01 rounding difference) and Islington value to
value; this is the 'incomplete refresh' recorded on 2026-09-30. One name changes (`Newark & Sherwood `, a
trailing space). Nothing else differs; `data_missing` flips in neither direction and the national bed and
breakfast and nightly paid sums equal edition 1. The `source` label in live is corrected to the third release.
Edition 3 against edition 2: 11 authorities, 103 cells (the eight returning, plus Leicester City, Enfield and
Islington).

## Rulings

1. **Scott chose to refresh the live table** (2026-10-07), rather than loading the new edition and leaving
   live at edition 1.
2. **The map and the W1 national aggregates are unchanged.** Both read only the latest financial year
   (2025-26), whose live rows are unchanged. The 2024-25 changes are visible only to queries that read
   2024-25 directly. Workflow 1 and the map export were not re-run.
3. **Earlier releases of 2024-25 are not all held.** The first release is not held and was not fetched. The
   chain records the order we loaded releases, not the order they were published: here the second release was
   loaded after edition 1 and before the third, which happened to be right, but the chain model cannot place
   a release before an existing edition, and what protects against a mislabelled file is the manifest and the
   cover-sheet gate, not the chain.
4. **A later release goes through `ro4_editions.py load`**, with the file in `data/reference/` under a
   release-specific name and a manifest entry; `s2_ro4_load.py` remains the parser and the first-load tool.
   The reference CSV `data/reference/ro4_housing_expenditure_2024-25.csv` is tracked and matches the real
   third release; it was not changed.
5. **NULL, not 0.** An authority that has not reported or is marked `[x]` is NULL with `data_missing` true;
   a published zero stays 0. Counts are derived (296 per year), and a short year is refused.

## Safety

`python scripts/ro4_editions_verify.py` (44 checks, about 1 minute 18 seconds alone, including the
cover-sheet gate) includes seeded gates in rolled-back savepoints: a fake second 2025-26 release refreshes
exactly that year and leaves 2024-25 unchanged; tampering with the other year, a short year, a one-sided
authority, a forked chain and drift all halt. The S1 and S1b verifications and `verify_source_registry.py`
pass.

## Left open

- **New RO4 releases are not detected automatically.** Someone has to notice them on the GOV.UK collection
  page.
- **The second 2025-26 release is expected later** and will be loaded as edition 2, after its cover sheet is
  read.
- **No insert-time trigger enforces `supersedes` = chain tip** (only `load` checks it), as for S1 and S1b.
