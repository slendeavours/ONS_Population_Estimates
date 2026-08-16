# 2026-08-16 — S1 reconstruction: suppression markers, the 2025Q1 gap, and a back-series divergence

Item 4. The extraction step that produced `la_statutory_homelessness` existed
nowhere — node 1 fetched a pre-processed CSV from GitHub and the ODS→CSV
conversion behind it was never committed. It is now
`scripts/s1_extract_ods.py`, and running it against the published files
surfaced three separate things.

## 1. `..` was being stored as zero

MHCLG marks a suppressed or unavailable cell `..`. The path that built the
stored data coerced that to `0`. Absent and zero then became the same value,
and every downstream label read the marker as a real observation.

**`submission_gap` fired on eight authorities in run 16. Seven were markers.
One — E06000053, Isles of Scilly — is a genuine zero**, which is plausible for
roughly 2,200 households. So the label was right once in eight.

`num()` now returns `None` for `""`, `-`, `..`, `:`, `*`, `n/a`, `x`. A marker
stays absent rather than becoming a number.

### What was corrected

**54 cells across 2025Q2 and 2025Q3 changed from `0` to NULL** — 19 TA cells
and 35 across the assessment measures. These two quarters and only these two
were corrected, because only they reproduce exactly from the current published
file (see §3), so only for them is the marker verdict evidence rather than
inference.

| Period | TA households | Before | After |
| --- | ---: | ---: | ---: |
| 2025Q2 | non-null LAs | 296 | 284 |
| 2025Q3 | non-null LAs | 296 | 289 |

**129 stored zeros across the seven earlier quarters are untouched** and are
still ambiguous. They cannot be classified until §3 is settled.

## 2. `period` is a financial-year quarter, and 2026Q1 does not exist

The three quarters queued for loading were 2025Q1, 2025Q4 and 2026Q1. Read from
the files' own table titles rather than from their names:

| Repo period | File title says | Status |
| --- | --- | --- |
| 2025Q1 | April to June 2025 | loaded |
| 2025Q4 | January to March 2026 | loaded |
| 2026Q1 | April to June 2026 | **not published** |

`homelessness_quarter_urls.quarter_label` says the same — `2024Q4` is
`Jan-Mar 2025`. The most recent release MHCLG has published is January to March
2026, issued 13 August 2026, and that **is** 2025Q4. There is no third quarter
to load; 2026Q1 is due around October 2026.

`la_statutory_homelessness` now holds **11 quarters, 296 rows each**, 2023Q2
through 2025Q4. The 2025Q1 gap is closed.

## 3. Seven quarters no longer match what MHCLG publishes — open

The reproduction gate compares the stored table against the current published
file, cell by cell, with `IS DISTINCT FROM` so that NULL ≠ 0 is caught.

| Period | Cells differing (excluding marker fixes) |
| --- | ---: |
| 2023Q2 – 2024Q4 | 1,038 – 1,128 per quarter |
| 2025Q2 | **0** |
| 2025Q3 | **0** |

Keys reconcile exactly — 296 both ways, every quarter. The two most recent
quarters reproduce perfectly. The seven older ones do not, on 200–230 of 296
authorities for the assessment measures.

**This is not an extraction fault.** The 2023Q2 file itself gives Hartlepool
172 initial assessments where the table holds 193; headers, column indices and
row counts are identical across the editions.

Three findings point the same way:

- `homelessness_quarter_urls.notes` already says **"Revised"** against exactly
  2023Q2, 2023Q3, 2024Q1–2024Q4 and 2025Q1 — set when the URLs were discovered.
- The GOV.UK collection dates the October–December 2024 release to **June
  2026**, after the 2026-04-01 bulk load.
- 2025Q2 was loaded in that same bulk load and *does* reproduce, so the cause
  is per-quarter republication rather than anything about the loading run.

The likeliest reading is that MHCLG revised the back series after the load. It
is not proven, because settling it needs the original editions, which are gone.

**Impact is small on the published measure and large on the others.**
`households_in_ta` differs on 9–25 authorities per quarter, totals moving by
40–206 households in ~100,000 (under 0.2%). The assessment measures differ on
roughly three quarters of authorities.

**Not restated. This needs a decision** — restating seven quarters of five
measures is materially more than the reproduction gate this item called for.

## 4. Structural changes the header-driven reader caught

**2025Q4 redesigned all three sheets.** Flat single-row headers replacing
merged multi-row ones, every A3 support-need column prefixed `Support need`,
and the whole A3 block shifted three columns left — mental health moved from
column 21 to 18, where column 21 now holds *domestic abuse*.

**A fixed-offset reader would have loaded domestic abuse as mental health and
reported success.**

### This is the probable mechanism behind the quarantined support-need columns

Stating it as a finding rather than an aside, because it answers the question
the quarantine left open.

The quarantined columns — `mental_health_suspect`, `learning_disability_suspect`,
`drug_dependency_suspect`, `alcohol_dependency_suspect`,
`rough_sleeping_history_suspect` — were quarantined because their values looked
wrong and nobody could say why. The A3 restructure explains it, and explains
the detail that a simple off-by-one never could: **the misalignment varied by
quarter rather than being a constant offset.**

A constant bug produces a constant error. What was actually happening is that
the offset was correct for the editions it was written against and wrong for
the ones that came later, so the size and direction of the error changed
whenever MHCLG moved the block. 2025Q4 moved it three columns left. Every
support-need column read three positions off, which is not a near-miss — at
that offset *mental health* reads *domestic abuse*, *learning disability* reads
*non-domestic abuse*, and *drug dependency* reads *offending history*. Each is
a plausible-looking count for an English local authority, so nothing downstream
had any reason to complain.

This is the reason the columns stay quarantined and deprecated in favour of
S1b rather than being restored: the values are not recoverable by applying a
correction, because there is no single correction to apply. They have to be
re-extracted per edition, which is what S1b already does.

The header-driven resolver below is the control that makes a recurrence
visible: when MHCLG next moves a column, it halts and names the ambiguity
instead of reading whatever now sits at the old index.

Resolution is now: candidates ordered most-specific first, the first candidate
matching *exactly one* column wins, a candidate matching several is abandoned
rather than resolved by preferring the leftmost, and rate columns
(`per thousand`, `(000s)`) are excluded before matching because these tables
are loaded as counts. If nothing resolves uniquely it halts and names the
ambiguity.

**MHCLG also changed container format mid-series** — 2023Q4, 2024Q1 and 2024Q2
are `.xlsx`, everything either side `.ods`. Both readers return the same shape.
Container format is not a property of the data and is not a property of the
pipeline.

## 5. Barnsley and Sheffield

Published on their post-April-2025 codes E08000038/39 against boundaries that
carry E08000016/19. Resolved through `la_code_lookup` on `change_type =
'recode'` only — a recode renumbers one area, whereas `new_unitary` and
`merger` are abolitions, and folding predecessors onto a successor would count
that successor once per predecessor. Anything that still fails to resolve halts.

## 6. Provenance

`la_statutory_homelessness` gains `source_file` and `extracted_at` (additive,
guarded). The seven historical quarters carry NULL there, which is honest —
nobody can say which edition produced them, and that is the whole finding.

## 7. Gate 13

Green. `homelessness_quarter_urls` now carries rows for 2025Q3 and 2025Q4 with
URLs **verified by Content-Length against the stored files**, not assumed from
a page summary. `loaded` is re-derived from actual row counts for all 11 rows
rather than set by whatever intended to load them. The known-red entry is
removed. The suite exits 2 on gate 14 alone.

## 8. Marker sweep across the other MHCLG sources

Run as a data test — does the table hold zeros and NULLs, or only zeros?

| Source | Table | Verdict |
| --- | --- | --- |
| S1b | `la_homelessness_support_needs` | both present — loader distinguishes |
| S2 | `ro4_housing_expenditure` | both present — loader distinguishes |
| S13 | `la_housing_register` | both present — loader distinguishes |
| **S10** | **`la_rough_sleeping`** | **22 and 27 zeros, no NULL anywhere** |

S10 carries S1's signature. A genuine zero is highly plausible for a rough
sleeping snapshot, so this is lower risk than S1 was — but the table cannot
prove it either way, and that is the same blind spot. Open; settling it needs
the source file.

Separately, `la_housing_register.reasonable_preference` is NULL in all 3,256
rows — a column that has never been populated.

## 9. Restatement — run 17 supersedes run 16

Decided: the feed catching up to data that exists is not an error correction,
and leaving it stale to avoid a restatement is the wrong trade.

Run 16 was deleted whole — 1,600 rows across `staging_la_signals`,
`staging_national`, `staging_tenant_type_rankings`, `staging_convergence` and
`staging_runs` — after a full snapshot to
`build_reports/run_snapshots/run16_snapshot_2026-08-16T161122.json`, with the
snapshot read back against live row counts *before* the delete and the absence
read back after. W1 re-ran as run 17 through the Create Run node.

| | Run 16 | Run 17 |
| --- | ---: | ---: |
| Latest quarter | 2025Q3 (Oct–Dec 2025) | **2025Q4 (Jan–Mar 2026)** |
| National TA households | 124,142 | **130,775** |
| Prior year comparison | 2024Q3 | 2024Q4 |
| TA year on year | — | **+13.29%** |
| Authorities with a TA figure | 296 | **284** |
| `submission_gap` | 8 | **0** |
| `data_quality` complete | 105 | 101 |

`submission_gap` falling to zero is the marker fix showing through: seven of
the eight were `..`, and no authority reported a genuine zero in Jan–Mar 2026.
The twelve authorities without a TA figure now carry `no_current_data` and a
NULL `ta_yoy_pct` rather than a fabricated trend.

### Gate 15 was red on run 17, and the gate was wrong

Run 16 predated the marker backfill, so run 17 was the first run to carry a
NULL TA — and gate 15 immediately failed on twelve rows.

It was a false positive, but a useful one. The gate asserted that a derived
column must be **NULL** when an input is absent. That is right for a numeric
value, where a number over an absent measure is a fabrication. It is wrong for
a categorical label, because `ta_trend_label`'s entire purpose is to *name* the
absence — requiring NULL would have forbidden the correct behaviour and made
the gate permanently red the moment TA was legitimately absent.

The assertion is now **stricter, not looser**: when an input is absent the
label must be one of the declared absence sentinels
(`no_current_data`, `no_prior_year`, `submission_gap`), and any direction word
is a violation. `undetermined` is deliberately not a sentinel — over an absent
input it would mean the CASE fell through, which is the exact defect gate 15
exists for.

Proved by injection: setting one absent-TA row to `falling_strongly` turned
gate 15 red and exited 1; restoring `no_current_data` returned it to pass and
exit 2. That is the original Sheffield/Barnsley defect, and the gate still
catches it.

Feed re-exported at 40 columns, carrying `run_id 17`.

## 10. The seven quarters are registered, not restated

Left unrestated for now, but no longer discoverable only by reading this file.

**`source_registry` S1** keeps `revises_back_series = true`, and
`revision_note` now records the *measured instance* rather than an inference
from file names — the per-quarter cell counts, the Hartlepool 172-versus-193
check that rules out an extraction fault, and the three corroborating facts.

**`homelessness_quarter_urls`** gains `reproduces_from_source`,
`reproduction_checked_at`, `reproduction_diff_cells` and `reproduction_note`,
populated from the measured comparison. Seven quarters are `false` with their
cell counts; four are `true`.

**`v_la_statutory_homelessness`** joins that status onto the data, so a query
against 2023Q2 discovers it does not match the publisher without anyone
knowing to write the join.

## 11. `la_housing_register.reasonable_preference` — schema artefact, not a load bug

NULL in all 3,256 stored rows because it is **empty in the source extract too**
— all 3,471 rows of `lahs_waiting_list_2015_2025.csv`, every year 2015–2025.
The column was declared in the extraction and never populated. Nothing was lost
in loading. Whether LAHS publishes a reasonable-preference figure that could
fill it is a separate question and has not been established.

## 12. Item 5 — the S1b cross-check, and a correction to §3

S1b is an independent extraction of the same A3 sheet, built separately and
carrying its own `layout_version` (`legacy_37col` / `v2026_34col`), so it can
arbitrate between the stored table and a fresh extraction.

**The five quarantined `*_suspect` columns are NULL in all eleven quarters.**
The quarantine was executed, not merely flagged, so nothing downstream can read
them and there is no residual bad data. S1b is the only support-need source.

**But `support_needs_total` escaped the quarantine, and it is wrong.**

| | Agrees with S1b |
| --- | ---: |
| Fresh extraction, every quarter | **296 / 296** |
| Stored value, 2025Q2 / Q3 / Q4 | **296 / 296** |
| Stored value, 2023Q2 – 2024Q4 | **1–3 / 296** |

It is not stale. It is a different measure. Stored `support_needs_total` runs
at **45–47% of the correct total** in every one of the seven quarters and
exactly **1.000** in the three that reproduce, and it matches the *adjacent*
`hh_one_support_need` column on 148–164 of 296 authorities. It holds
**households with one support need** where it should hold **one or more**.

### This corrects §3

§3 read the whole seven-quarter divergence as revision. That was right for the
A1 measures and wrong for this one. There are **two defects, not one**:

- `total_assessments`, `owed_duty`, `prevention_duty`, `relief_duty` — A1
  sheet, differing on 200–230 of 296, consistent with revision.
- `support_needs_total` — A3 sheet, differing on ~293 of 296 at a fixed
  ~46% ratio. **Misalignment, the same defect that quarantined the
  `*_suspect` columns.** The near-universal rate was the tell: a revision
  touching 99% of authorities on one column while touching 75% on the others
  is not a revision.

The A3 restructure of §4 is the mechanism for both, which is why S1b — which
tracks layout version explicitly — is unaffected.

**Not published.** `support_needs_total` is absent from `staging_la_signals`,
so the error is contained to the database and has never reached the feed or the
map. Not corrected here, because the seven-quarter reload is its own item — but
it is a **different fix from a reload**: the correct values are already known
exactly, from S1b and from the fresh extraction, for all seven quarters.

Registered in `source_registry` S1's `revision_note` and in the seven
`homelessness_quarter_urls.reproduction_note` rows.

## Still open

1. Reload the seven quarters from the current editions and restate — its own
   backlog item, now registered in the database rather than only here. Note it
   is two fixes: revision for the A1 measures, and a straight column
   correction for `support_needs_total` where the right values are already
   known.
2. S10 `la_rough_sleeping` — 22 and 27 zeros with no NULL anywhere. Settles the
   way S1 did: extract from source and compare. Not actionable from the table.
