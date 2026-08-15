# 2026-08-14 — Barnsley and Sheffield fall out of Workflow 1, and a NULL is published as "falling strongly"

## What was asked

S1b discovery noted in passing that `la_statutory_homelessness` stores Barnsley
and Sheffield under `E08000016`/`E08000019` up to 2024Q4 and under
`E08000038`/`E08000039` from 2025Q2. The question was whether that reaches
published output, given that W1 pins the current quarter to 2025Q2 — after the
change — and that `households_in_ta` is published.

It does, and the mechanism is worse than a split figure.

## The mechanism

W1 node 5 drives off `la_boundaries`, whose canonical code for these two
authorities is the **old** one. `la_code_lookup` agrees: it maps
`E08000038 → E08000016` and `E08000039 → E08000019`, with the note "MHSDS uses
E08000038 from Jun 2025; la_boundaries retains E08000016".

Sources that store the publisher's code without resolving it through
`la_code_lookup` therefore produce **no matching row at all** on the join.
Nothing is split across two rows and nothing is halved — the LEFT JOIN yields
NULL.

Then this fires:

```sql
CASE
    WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap'
    WHEN ta_prev.households_in_ta IS NULL OR ... THEN 'no_prior_year'
    WHEN ... > 10  THEN 'rising_strongly'
    ...
    ELSE 'falling_strongly'
END AS ta_trend_label
```

`NULL = 0` is NULL, not true, so a missing row does not reach
`submission_gap`. Every arithmetic comparison against a NULL current figure is
also NULL. All of them fall through to the catch-all, and **an absent
measurement is published as the strongest available downward signal.**

That is the defect worth fixing beyond the two authorities. The pipeline has a
label for exactly this case — `submission_gap`, correctly applied to 13
authorities — and it only catches an explicit zero. Any future source row that
goes missing will be labelled `falling_strongly` rather than flagged.

## Scope — it is not only S1

A scan of all 40 tables carrying `lad24cd`:

| Table | Old codes | New codes | State |
|---|---:|---:|---|
| `la_statutory_homelessness` | 14 | 4 | split — TA is NULL for both from 2025Q2 |
| `la_rough_sleeping` | 0 | 2 | **new codes only** — rough sleeping is NULL for both, in every run |
| `nhs_mh_crfd` | 52 | 24 | split, but neutralised — `vw_mh_crfd_lad` already normalises to 76 canonical rows |
| `nhs_mh_crfd_repro` | 52 | 24 | reproduction table, mirrors the live one |

Every other table is canonical. So there are **two live defects, not one**: S1
temporary accommodation and S10 rough sleeping. S9b has the same split in its
base table and is saved by its view — which is the argument for resolving at
load time rather than relying on a view existing.

`staging_la_signals` confirms it: Barnsley and Sheffield carry
`rough_sleeping_current = NULL` and `rough_sleeping_prev_year = NULL` as well
as `ta_households_current = NULL`, in all seven runs, 4 through 12. Exactly two
of 296 authorities are affected, and they are the same two every time.

## The correct figures

There is no combining to do — the two codes never coexist within a quarter.

| | 2025Q2 (published as current) | 2024Q2 (prior year) | True YoY | Published label | Correct label |
|---|---:|---:|---:|---|---|
| Barnsley | **50** | 51 | −1.96% | `falling_strongly` | **`flat`** |
| Sheffield | **642** | 718 | −10.58% | `falling_strongly` | `falling_strongly` |

Sheffield's label is right by coincidence — −10.58% clears the −10 threshold.
Its `ta_households_current` and `ta_yoy_pct` are still NULL and still wrong.
Barnsley's label is wrong outright.

## Published exposure

| Artefact | Affected | Detail |
|---|---|---|
| Map feed `data/signals/staging_la_signals_latest.json` | **Yes, live** | Both authorities: `ta_households_current: null`, `ta_yoy_pct: null`, `ta_trend_label: "falling_strongly"`, rough sleeping null. Committed to the public repo and served by the map. |
| SPB Edition 1 Workbook | **Yes** | Convergence Table shows both with TA Households blank and TA Trend `falling_strongly`. Sheffield also appears in **Priority Markets (INTERNAL)** with the same blank. |
| SPB Report and Deck narrative | No | Neither carries a TA claim about these two. Their Sheffield and Barnsley text is PIP rates and convergence profile. |
| SPB convergence profile | No | `assign_profile()` uses PIP rate, SL density, DRD and CRFD only. TA is not an input, so "Demand-led" (Sheffield) and "Pressure building" (Barnsley) stand. |
| Council briefings | No | Only Hull (E06000010) and St Helens (E08000013) were produced. |
| **YADA Run 1** | **Yes** | See below. |

## YADA — now checked, and it corrects the expectation

YADA could not be checked when this came up before. It is not in the
repository; it exists as loose files in `C:\Users\slewi\Downloads`
(`YADA_Run1_results_data.csv`, `YADA_Run1_Data_Dictionary.xlsx`). That it is
uncontrolled is a separate risk and is worth its own decision.

It **does** consume the affected fields: `ta_current`, `ta_yoy_pct`,
`ta_trend`, and a percentile rank `pr_ta_households_current` that feeds
`demand_score`, which feeds `yads` and `yads_rank`. It does **not** touch the
mis-mapped support-need columns, so the column quarantine has no YADA
exposure.

Both authorities are missing **two of five** demand components —
`pr_ta_households_current` and `pr_rough_sleeping_current`:

| | demand_score | yads | yads_rank | True TA | TA percentile | Mean of populated components | Mean including TA |
|---|---:|---:|---:|---:|---:|---:|---:|
| Sheffield | 86.4 | 44.99 | **17** | 642 | 86.1 | 84.28 | 84.72 |
| Barnsley | 52.1 | 28.43 | 64 | 50 | 30.6 | 49.55 | 44.81 |

**Sheffield is not materially understated.** Its true TA sits at the 86th
percentile, almost exactly its existing four-component mean, so restoring it
moves the score by about +0.4. The expectation that a priority market was being
understated does not hold on this component.

**Barnsley is overstated**, by roughly 4.7 points of mean percentile — its true
TA is a bottom-third figure being replaced by an average of its higher
components.

Two caveats on those numbers. The published `demand_score` (86.4, 52.1) does
not equal the plain mean of the populated percentiles (84.28, 49.55), so the
real formula is weighted and these deltas are directional rather than exact
restatements. And rough sleeping is missing for both as well, so a proper
restatement needs both sources fixed before YADA is re-run.

## What follows

1. Resolve the publisher codes through `la_code_lookup` at load time in
   `la_statutory_homelessness` and `la_rough_sleeping`, which is what
   constraint 5 has always required and what S1b already does.
2. Replace the `ELSE 'falling_strongly'` catch-all with an explicit unknown
   branch, so a missing row is never again rendered as a trend.
3. Re-run W1, re-export the map feed, and restate the SPB workbook cells.
4. Re-run YADA once both sources are fixed.

None of 1–4 is applied yet. This record is the evidence, taken before anything
was changed.
