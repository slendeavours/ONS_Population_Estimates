# S1b — MHCLG statutory homelessness, Table A3 support needs

<!-- repo-meta
status: active
last-reviewed: 2026-08-14
type: source
consumed-by: scripts/s1b_support_needs_build.py, scripts/s1b_support_needs_verify.py
-->

| | |
|---|---|
| Publisher | MHCLG |
| Series | Statutory homelessness in England: detailed local authority-level tables, Table A3 |
| Landing page | https://www.gov.uk/government/collections/homelessness-statistics |
| Cadence | Quarterly, roughly four months after quarter end |
| Target table | `la_homelessness_support_needs` |
| Natural key | `(lad24cd, period, category_code)` |
| Rows | 101,232 across eleven quarters |
| Coverage | 296 of 296 English local authorities, every quarter |
| Built | 2026-08-14 |

## What this is, and what it is not

S1b is an extension of S1, not a replacement. S1 keeps the temporary
accommodation series that feeds `staging_la_signals`; S1b loads the whole of
Table A3, which S1 samples six figures from.

It is not wired into Workflow 1 and adds no signals column. It is queried
directly.

## What A3 publishes

Twenty-four individual support-need categories, six mutually exclusive
household-count columns, and a total count of needs reported. From the
January–March 2026 edition a further column carries the prevention-or-relief
duty total.

| `category_group` | Members | Arithmetic |
|---|---|---|
| `support_need` | 24 categories | **Multi-response — never sum these** |
| `needs_breakdown` | no needs, unknown, one, two, three or more, one-or-more total | Mutually exclusive, sums to the duty total |
| `needs_total` | total count of support needs reported | A count of needs, not of households |
| `duty_total` | households owed a prevention or relief duty | 2025Q4 onward only |

The 24 categories are `young_person_16_17`, `young_person_18_25`,
`young_parent`, `care_leaver_18_20`, `care_leaver_21_24`,
`care_leaver_25_plus`, `care_leaver_legacy_combined`,
`physical_ill_health_disability`, `mental_health_history`,
`learning_disability`, `sexual_abuse`, `domestic_abuse`,
`non_domestic_abuse`, `drug_dependency`, `alcohol_dependency`,
`offending_history`, `repeat_homelessness_history`, `rough_sleeping_history`,
`former_asylum_seeker`, `old_age`, `served_in_hm_forces`,
`access_to_education_employment_training`, `modern_slavery_victim`,
`difficulties_budgeting`.

## The multi-response caveat

The publisher's own note, verbatim from the A3 footnote block:

> Multiple support needs can be reported per household, but each support need
> only once. Households can therefore be represented across multiple support
> needs columns.

So the 24 categories do not sum to the household total and adding them
produces the needs count, not a headcount. This is why the table carries
`category_group` rather than leaving a consumer to know it: the caveat is
structural, so it lives in the schema.

`care_leaver_legacy_combined` overlaps the two current care-leaver bands. The
21+ option was retired for cases assessed on or after 1 April 2023 and split
into 21–24 and 25+, and authorities are still migrating, so the three should
not be added.

## Two sheet layouts

MHCLG restructured A3 in the January–March 2026 release, published 13 August
2026.

| | Quarters to 2025Q3 | 2025Q4 onward |
|---|---|---|
| Columns | 37 | 34 |
| Header | four merged rows, unlabelled code column | one labelled row |
| Footnotes | trailing bare digits | `[note n]` markers |
| Category labels | "Young person aged 16-17 years" | "Young person aged 16 to 17" |
| Suppression | `..` and `-` | `[x]`, `[c]`, `[z]` |
| Duty total column | absent | present |

The category set did not change. Every label did. This is the reason the table
is long-format keyed on a canonical `category_code`: a column-per-category
schema would have needed a migration for a rename that changed no data.

## Suppression

Stored distinctly from zero, always. A flagged cell carries `value = NULL` and
a `value_flag`; a real zero carries `value = 0` and a null flag. A CHECK
constraint enforces that exactly one of the two is populated, so the pair
cannot drift.

| Marker | Layout | `value_flag` | Publisher's meaning |
|---|---|---|---|
| `..` | legacy | `missing` | Authority with missing data |
| `-` | legacy | `suppressed` | Breakdown suppressed, fewer than 5 households with support needs |
| `[x]` | v2026 | `missing` | Data missing due to non-submission or data quality issues |
| `[c]` | v2026 | `suppressed` | Data suppressed, to protect identification of households |
| `[z]` | v2026 | `not_applicable` | Not applicable |

Both vocabularies are documented in the publisher's own files — the legacy
meanings in the footnote block beneath the A3 data, the v2026 meanings on the
Notes sheet. Neither was assumed.

Loaded: 1,620 missing, 772 suppressed, 18,724 genuine zeros, 0 coerced.

## Geography

296 authorities every quarter. Quarters to 2024Q4 use the pre-2025 Barnsley
and Sheffield codes `E08000016` and `E08000019`; 2025Q1 onward use
`E08000038` and `E08000039`. Both resolve through `la_code_lookup` to the
pipeline's canonical `E08000016` and `E08000019`, so each authority is one
series across all eleven quarters.

S1 does not do this and splits both authorities across two codes. See
[the decision record](decisions/2026-08-14-s1-support-need-column-misalignment.md).

England and regional rows are excluded. They are weighted to impute for
non-submitting authorities, so they are not the sum of the LA rows and must
not be loaded as areas.

## Editions and revisions

`revises_back_series` is true. Quarters are revised and republished in place,
and the currently linked attachment is not the original for three of the
eleven: July–September 2023 (`_fixed`), April–June 2024 (`_fix`) and
April–June 2025 (`_corrected`).

Editions are resolved live from each release page, preferring
corrected > revised > fix > original, and never read from
`homelessness_quarter_urls`. That table records `_revised` assets for four
quarters which GOV.UK still serves but no longer links from any release page,
so it does not describe what the publisher currently publishes.

`source_url`, `source_edition` and `edition_variant` are stored on every row,
so which file produced any figure is answerable from the row itself.

## Reconciliation

The publisher's England total is weighted and rounded to the nearest 10, and
the LA rows are unrounded with nulls where suppressed. They are not expected
to be equal, and the suite does not assert that they are. It asserts the LA
sum never exceeds the published England figure, and reports the gap:

| Period | England published | Sum of LA rows | Gap | Authorities not reported |
|---|---:|---:|---:|---:|
| 2023Q2 | 42,860 | 42,112 | 1.75% | 4 |
| 2023Q3 | 43,080 | 41,972 | 2.57% | 8 |
| 2023Q4 | 46,430 | 45,230 | 2.58% | 4 |
| 2024Q1 | 46,090 | 44,813 | 2.77% | 7 |
| 2024Q2 | 46,200 | 45,137 | 2.30% | 5 |
| 2024Q3 | 44,480 | 43,397 | 2.43% | 7 |
| 2024Q4 | 48,700 | 48,467 | 0.48% | 2 |
| 2025Q1 | 46,960 | 46,960 | 0.00% | 0 |
| 2025Q2 | 48,940 | 47,669 | 2.60% | 5 |
| 2025Q3 | 47,520 | 46,872 | 1.36% | 2 |
| 2025Q4 | 51,820 | 50,524 | 2.50% | 8 |

The gaps sit inside the 2.0%–3.2% imputation the publisher states on its cover
sheets, which is the corroboration. 2025Q1 reconciles exactly because the
corrected edition carries a complete return.

## Known traps

- **Reading A3 by column position.** Two layouts and a full relabelling make
  positional extraction fragile, and it has already failed once in this
  pipeline. Match labels and refuse to load anything unaccounted for.
- **Trusting `homelessness_quarter_urls` for the current edition.** It records
  what was fetched, not what is published, and the two have diverged.
- **Summing the 24 categories.** Gives the needs count, not households.
- **Loading the England or regional rows as areas.** They are imputed.
- **Not resolving Barnsley and Sheffield.** Splits both series at 2025Q1.

## Reproducing

```bash
python scripts/s1b_support_needs_build.py --discover
python scripts/s1b_support_needs_build.py --load
python scripts/s1b_support_needs_verify.py
```

`--discover` resolves and parses every edition and reports what would be
written without touching the database. The verification suite never commits.
