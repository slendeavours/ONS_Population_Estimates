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

## Exposure of published output — none from the gap, but something else

Established before the reload, while the evidence of what the outputs were
built on is still intact.

**Nothing published touched 2025Q1.** W1 node 5 does not select the latest
quarter; it **hardcodes** `ta_cur.period = '2025Q2'` and
`ta_prev.period = '2024Q2'`. 2025Q1 is not referenced by any query in the
pipeline. The System Pressure Briefing and the council briefings read
`ta_households_current` from `staging_la_signals`, never from
`la_statutory_homelessness` directly, so they inherit those pinned quarters.
Verified across every run: Birmingham's `ta_households_current` is 5,196 in
runs 4 through 12, which is exactly its 2025Q2 value.

**YADS Run 1 cannot be verified from this repository.** No YADS script,
artefact or output exists here — the only related reference is S18's note that
yield-adjusted demand analysis is future work. If it reads
`staging_la_signals` it is equally unaffected; if it queries
`la_statutory_homelessness` directly with a rolling window it could touch the
gap. That has to be checked wherever YADS actually lives.

**A different live issue was found instead.** Those hardcoded periods have not
moved since 2025Q3 was loaded on 2026-07-06. Every output since — including
W1 run 12 on 2026-08-13 and everything exported from it — publishes TA figures
for **July to September 2025 when October to December 2025 has been sitting in
the database for five weeks**. Birmingham publishes 5,196; the current quarter
is 5,151.

That is the same failure shape as the gap itself and as the fingerprint
short-circuit: **state taken from intent rather than from the thing itself.**
A hardcoded period is an assertion about what the latest quarter was on the
day someone wrote it.

## The defect is bounded, and the run log is clean

`pipeline_run_log` was swept for the same class — a run recorded successful
without evidence of rows written. **Zero of 90 successful runs** have a null or
zero `rows_written`, and `homelessness_quarter_urls` is the only side register
of loaded periods in the database. The attempt-versus-evidence defect is
bounded to S1's register.

Gate 13 now enforces the general rule: where a side register of loaded periods
exists, it must agree with the target table in both directions. It is
currently **red**, correctly, and stays red until 2025Q1 is reloaded and
2025Q3 is recorded.

## The reload is blocked, and by a bigger problem

**S1 has no build script.** Reloading 2025Q1 "through the S1 build path" is not
possible because there is no path — the same condition S9a and S9b were in
until they were reconstructed.

S1 is not alone. **Thirteen of twenty-three published sources have no build
script**: S1, S2, S3b, S4, S5, S7, S8, S10, S12, S13, S14, S17, S21. S9a and
S9b were not the case, they were the tip. Loading 2025Q1 by hand would put
data in the table with no reproducible route to it, which is the condition
that made this audit necessary in the first place.

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
