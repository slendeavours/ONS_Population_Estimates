# 2026-08-15 — Restatement: what the map showed, and what it should have shown

Workflow 1 run 15 corrects figures that were published on the live map and in
material circulated from it. This is a restatement, not a changelog: it records
what was published, what was wrong with it, and what it should have been.

There are **three states**, not two. A run-to-run diff understates the problem,
because the first state was never reproducible.

## State 1 — runs 4 to 12: not reproducible, and wrong for two authorities

**Sheffield and Barnsley were published with no temporary accommodation figure
at all, `data_quality: ok`, and a trend label of `falling_strongly`.** A trend
direction was published for two major metropolitan authorities, computed from
an absent measurement, and the quality flag beside it said the data was fine.

The underlying data had been present since 1 April 2026. Nothing was missing at
source.

The cause was a catch-all `ELSE` in the trend CASE. `NULL = 0` evaluates to
NULL rather than true, so every comparison against an absent figure fell
through to the ELSE and emerged as the strongest downward signal the scale
carries. `submission_gap`, the label meant for this, only ever caught an
explicit zero. Absent and zero were collapsed, and the absent case took the
worst available label.

**These runs cannot be reproduced.** Run 12 executed against SQL that is not
what the stored node contained — the current stored node produces zero NULLs
against exactly the same data. The node was changed without the run being
repeated, so **nobody can now say with certainty what SQL produced the figures
that went out.** That is a different and worse condition than being superseded:
a superseded figure has a known provenance and a better successor, while these
have neither. They are not recoverable by re-running anything.

Runs 4 to 12 span 1 April to 13 August 2026, and everything exported in that
window carries this property.

## State 2 — run 14, published 15 August 15:33: NULLs fixed, still stale

Run 14 hardened the trend CASE with a leading NULL test and a terminal
`undetermined`, and quarantined the S1 support-need columns. It eliminated the
NULL-as-`falling_strongly` defect: 296 of 296 authorities carried a TA figure.

It remained stale in two respects, because the period pins had not been
touched:

- **Temporary accommodation was still pinned to 2025Q2** (July–September 2025)
  while 2025Q3 (October–December 2025) had been loaded since 6 July 2026 — five
  weeks of published figures a quarter behind the database.
- **Housing Benefit specified accommodation still read S8**, whose single
  loaded month (202511) is pre-revision data from 1 April 2026. DWP revised that
  month in place, moving 285 of 296 authorities, and published no revision note
  anywhere a check looks.

## State 3 — run 15, published 15 August 23:20: the correction

| Authority | Runs ≤12 | Run 14 | Run 15 |
| --- | ---: | ---: | ---: |
| **Barnsley** TA | *NULL, "falling_strongly"* | 50 | **54** |
| **Sheffield** TA | *NULL, "falling_strongly"* | 642 | **691** |
| **Birmingham** TA | 5,196 | 5,196 | **5,151** |
| **Manchester** TA | 2,824 | 2,824 | **2,865** |
| **Kingston upon Hull** TA | 256 | 256 | **224** |
| **Tower Hamlets** TA | 3,092 | 3,092 | **3,096** |
| **Barnsley** HB SA | 620 | 620 | **624** |
| **Sheffield** HB SA | 2,277 | 2,277 | **2,321** |
| **Birmingham** HB SA | 31,117 | 31,117 | **36,364** |
| **Manchester** HB SA | 4,688 | 4,688 | **4,593** |

Trend labels move with the quarter: Barnsley `flat` → `falling`, Sheffield
`falling_strongly` → `falling`, Manchester `flat` → `rising`.

**Birmingham's HB specified accommodation caseload was understated by 5,247
(16.9%)** across every run from 4 to 14 — the largest single correction here.

### Scope of the correction, run 12 to run 15

| Column | Authorities changed |
| --- | ---: |
| `ta_households_current` | 287 of 296 |
| `ta_households_prev_year` | 282 |
| `hb_sa_caseload` | 291 |
| `ta_trend_label` | 148 |
| `efs_flag` | 18 |

## What was actually corrected

**Fourteen hardcoded period literals across two nodes**, eight in LA Signals and
six in National Aggregates. National Aggregates had never been audited; it
carried the same defect and nobody had looked.

Two were already stale. The rest were correct on the day they were typed and
would have gone stale silently at the next load — the same defect not yet
fired. A hardcoded period is an assertion about what the latest edition was on
the day someone typed it.

The EFS flag was restricted to two named financial years while
`la_efs_support` carries 2026-27. The comment directly above it had always read
"any year flagged". Comment and code had disagreed since the line was written,
and 18 authorities carried the wrong flag.

S8 is superseded by S8b. Both read the same DWP measure from the same
Stat-Xplore database; a live probe matched S8b on 296 of 296 authorities and
S8's stored values on 11.

## What was checked and found not to be wrong

Two suspected defects were confirmed absent rather than assumed present:

- `la_statutory_homelessness` and `la_rough_sleeping` were thought to hold
  publisher codes for Barnsley and Sheffield absent from `la_boundaries`. Both
  carry 296 distinct codes with zero orphans and zero rows on E08000038 or
  E08000039. The NULL joins were node drift, not unresolved geography.
- `nhs_mh_crfd` was thought to survive the same defect only because a
  normalising view exists. The table itself is clean.

## Standing consequence

Runs 4 to 12 are not reproducible. Any figure quoted from that window — in a
briefing, a deck or a council conversation — cannot be traced to the SQL that
produced it. Where such a figure has been circulated and still matters, it
should be re-derived from run 15 rather than reconciled against the old export.
