# 2026-08-14 — S1 has a missing quarter, and no per-row provenance

## What was asked

S1 was flagged as revising in place: `homelessness_quarter_urls` already
contained `Detailed_LA_202309_revised.ods` and
`Detailed_LA_202312_Revised_No_Dropdowns.ods`, sitting in the database
unremarked since the table was built. The question was the S9a question — does
`la_statutory_homelessness` hold the revised or the original for those
quarters?

## What was found

**The revised/original question is clean.** No loaded quarter is superseded.
Seven of the eight registered quarters were loaded from revised files. The two
loaded from originals are still the current published editions: `2023Q4` from
`Detailed_LA_202403.xlsx`, which GOV.UK still serves as the only detailed
attachment on that release, and `2025Q3` from the `202512` file, likewise
current.

**Two other things were found on the way, and both matter more.**

### 1. A whole quarter is missing, and the pipeline believes it loaded

| | |
|---|---|
| `homelessness_quarter_urls` | `2025Q1`, `loaded = true`, note "Revised", file `Statutory_Homelessness_Detailed_Local_Authority_Data_202506_revised.ods`, stamped 2026-04-01 |
| `la_statutory_homelessness` | **no rows at all for `2025Q1`** |
| GOV.UK, today | the release now serves `..._202506_corrected.ods` — a different file from the one recorded |

Every other loaded quarter has exactly 296 rows. `2025Q1` has none. The
register says a revised file was loaded; no data landed; and the publisher has
since replaced "revised" with "corrected", so the file the register names is
not the file that exists.

The likeliest reading is that the download failed or the file was withdrawn
mid-load, and the row was marked loaded optimistically rather than on evidence
of rows written. Whatever the cause, **April to June 2025 is absent from the
TA series**, and S1 feeds `ta_households` into `staging_la_signals` and the
map.

This was invisible because nothing reconciled the two tables. The register was
trusted as a record of what loaded, rather than checked against what is
actually there.

### 2. `2025Q3` is loaded with no provenance

`2025Q3` has 296 rows and **no row in `homelessness_quarter_urls`**, so
nothing records which file it came from. It matches the currently published
`202512` original, but that is inference from the period, not a record.

## The provenance requirement, retrofit

`la_statutory_homelessness` has no `source` column. This is the first concrete
case where the per-row provenance requirement is a retrofit rather than a
greenfield decision, and the difference is visible: S9a settled the same
question in one query, and S1 needed a three-way reconciliation across two
tables and a live publisher check to reach a weaker answer.

`homelessness_quarter_urls` is a per-quarter provenance table, which is the
same idea at coarser grain, and it was genuinely useful — it is why the
revised/original question could be answered at all. But it is a side table
that can disagree with the data, and it does: one row claims a load that did
not happen, and one load has no row.

**A column on the row cannot disagree with the row.** That is the whole
argument for the requirement, and this is the case that demonstrates it.

## Consequences for S1b

S1b is Table A3 from the same quarterly release. It inherits all of this:

- `revises_back_series` is **true before it is built**, established here rather
  than discovered later
- the build must prefer `-revised` and `-corrected` editions over originals,
  and record which it took
- it must carry the per-row source URL from the start
- it must mark a period loaded on **evidence of rows written**, not on having
  attempted the load

## Open

Reloading `2025Q1` from `..._202506_corrected.ods`, and recording provenance
for `2025Q3`. Not done here: this is a data load, and it should be done with
the S1 build path rather than by hand.
