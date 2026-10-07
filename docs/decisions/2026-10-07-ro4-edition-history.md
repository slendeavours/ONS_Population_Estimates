# RO4 edition history: store releases as editions and bring the live table to the third release of 2024-25

Status: implemented 2026-10-07. Follows [the S1b edition work](2026-10-06-s1b-edition-history.md), which
left the RO4 key as an open item.

## What

New append-only table `ro4_housing_expenditure_editions` (update, delete and truncate blocked by trigger),
key `(lad24cd, financial_year, edition)`, 888 rows (296 authorities in each of three editions). Latest is
the edition no other edition supersedes. The live table `ro4_housing_expenditure` is the latest-edition
layer, refreshed by `python scripts/ro4_editions.py refresh-latest --commit` (the eleven measures,
`la_name`, `data_missing` and `source`; `loaded_at` untouched). `status`, `sync-new` and `load` work as for
S1 and S1b. The releases we hold are listed, with checksums, in `scripts/ro4_editions_manifest.json`; a
later release is loaded with `python scripts/ro4_editions.py load` from a file placed in `data/reference/`,
then `refresh-latest`. `scripts/s2_ro4_load.py apply` stays insert-only and is for a first-ever release of
a financial year. Procedure: [../QUARTERLY_REFRESH.md](../QUARTERLY_REFRESH.md).

| Financial year | Edition | Release | Supersedes |
| --- | --- | --- | --- |
| 2024-25 | 1 | What the live table held, recorded 'as loaded' (source label 'published 18 Sep 2025') | none |
| 2024-25 | 2 | Third release, published 11 Jun 2026 | 1 |
| 2025-26 | 1 | First release, published 17 Sep 2026 | none |

## Why

The key `(lad24cd, financial_year)` holds one release per year, so a revision could only overwrite. The
publisher issues each financial year more than once, a second 2025-26 release is expected, and the 2024-25
rows turned out to differ from the third release.

## The finding: 2024-25 edition 1 is not the third release

The live 2024-25 rows were not the third-release file. Against that file they differed in 74 measure
cells (measures other than the hra column), one name (a trailing space), eight `data_missing` flags
and the whole `hra_admin_prevention_relief_net_exp_000` column on 250 authorities (the known defect in
[2026-09-30-reference-csvs-out-of-step.md](2026-09-30-reference-csvs-out-of-step.md)). So the third release
is a genuine edition 2, not a copy of edition 1.

Edition 2 against edition 1: 258 of 296 authorities change; 324 cells (250 hra, 74 others); one name; eight
authorities flip from reported to not reported and none the other way. In the file every spending cell for
those eight is `[x]` (publisher: data missing), where the live rows held figures from the earlier release:
Birmingham, Slough, Warwick, Ashfield, North West Leicestershire, Guildford, Amber Valley and North
Warwickshire. Their edition 2 and live values are NULL with `data_missing` true (NULL, not 0, including
Amber Valley's earlier 0.00 bed and breakfast), and their earlier figures remain in edition 1. Leicester
City, Enfield and Islington have small changes. National 2024-25 sums (£000): bed and breakfast gross
710,748.00 to 664,821.57; nightly paid gross 1,070,834.36 to 1,029,950.73. The `source` label is corrected
to 'third release, published 11 Jun 2026'.

**Correction.** The 2026-09-30 record and the CHANGELOG say the third release populates eight authorities
that the second release lacked. For these eight the third-release file says the opposite: they are `[x]`.
Those records are left as written; this is the correction. The local CSV copy
`data/reference/ro4_housing_expenditure_2024-25.csv` (not tracked) still holds figures for the eight and has
not been changed.

## Rulings

1. **Scott chose to refresh the live table** (2026-10-07), accepting the eight authorities becoming NULL
   and the hra column change, rather than loading edition 2 and leaving live at edition 1.
2. **The map and the W1 national aggregates are unchanged.** Both read only the latest financial year
   (2025-26), whose live rows are unchanged. The 2024-25 changes are visible only to queries that read
   2024-25 directly. Workflow 1 and the map export were not re-run.
3. **Earlier 2024-25 releases are not held and were not fetched.** The editions are a chain: each
   supersedes the one before and a new edition goes on the end, so the chain model cannot place the first
   or second release before the existing edition 1.
4. **A later release goes through `ro4_editions.py load`**, with the file in `data/reference/` and a
   manifest entry; `s2_ro4_load.py` remains the parser and the first-load tool.
5. **NULL, not 0.** An authority that has not reported or is marked `[x]` is NULL with `data_missing` true;
   a published zero stays 0. Counts are derived (296 per year), and a short year is refused.

## Safety

`python scripts/ro4_editions_verify.py` (41 checks, about 35 seconds) includes seeded gates in rolled-back
savepoints: a fake second 2025-26 release refreshes exactly that year and leaves 2024-25 unchanged;
tampering with the other year, a short year, a one-sided authority, a forked chain and drift all halt. The
S1 and S1b verifications and `verify_source_registry.py` pass.

## Left open

- **New RO4 releases are not detected automatically.** Someone has to notice them on the GOV.UK collection
  page.
- **The second 2025-26 release is expected later** and will be loaded as edition 2.
- **No insert-time trigger enforces `supersedes` = chain tip** (only `load` checks it), as for S1 and S1b.
