# 2026-08-15 — W1 NULL-safety audit, and what `data_quality` actually checks

Two related but distinct defects. The audit hunts the first; the second is
reported and left for a decision.

## Scope limit — read this before quoting any figure here

**This audit certifies forward behaviour only.** Run 12 executed SQL the stored
node does not contain, so reading the stored nodes cannot establish what
historical runs actually did. The data tests below do not share that weakness
and are exhaustive over stored output — but they can only show what the runs
*produced*, not which SQL produced it.

A retrospective scope figure would have to be derived from published outputs,
per run, not from workflow definitions. None is attempted here. Nothing in this
document should be read as certifying runs 4 to 12.

## The mechanism

Three-valued logic, not a wrong `ELSE` literal. A comparison such as
`ta_current < ta_prior` evaluates to **NULL** when either side is NULL — not
false. A NULL predicate is not-matched, so every `WHEN` arm declines and the
`ELSE` catches it as a positive assertion. The catch-all then publishes an
absent measurement as whatever it says — here, the strongest downward signal
on the scale.

`submission_gap`, the label meant for this, only ever caught an explicit zero.
Absent and zero were collapsed and the absent case took the worst label
available.

## Phase 1 — detection by data

Primary method: exhaustive in one pass, and independent of reading the stored
nodes correctly.

| Derived column | Input measures | Rows over an absent input | Runs |
| --- | --- | ---: | --- |
| `ta_trend_label` | `ta_households_current`, `ta_households_prev_year` | **14** | 4, 5, 7, 9, 10, 11, 12 — two each |
| `ta_yoy_pct` | same | 0 | — |
| `marac_rate_per_10k` | `marac_cases`, `population` | 0 | — |
| `pip_rate_per_1000` | `pip_total_claimants`, `population` | 0 | — |
| `ctb_lte_rate_pct` | `ctb_empty_6m_plus`, `ctb_total_dwellings` | 0 | — |
| `staging_national.ta_yoy_pct` | national TA current, prior | 0 | — |

The 14 rows are Sheffield and Barnsley in every run from 4 to 12, each labelled
`falling_strongly` against a NULL current figure. **Run 15 is clean.**

Every ratio column is clean in every run, because NULL propagates through
arithmetic to NULL. That is the correct behaviour and it is why only the
`CASE`-based label failed.

## Phase 2 — code read

The working hypothesis was that the construction had been copied. **It had
not.** Eleven `CASE` blocks were read across both nodes:

| Node | Blocks | Verdict |
| --- | ---: | --- |
| LA Signals | 7 | All safe. `ta_trend_label`, and all four `data_quality` sub-cases, branch on `IS NULL` before any comparison. `efs_flag` and `s114_flag` are presence tests with no comparison. |
| National Aggregates | 5 | All safe. Every one is a conditional-aggregation pivot comparing `period`, which is part of the primary key and cannot be NULL. |

No disagreement between the data test and the code read. Phase 1 shows the
defect no longer fires; Phase 2 shows no latent copy of it remains. **Both were
needed** — Phase 1 alone would not distinguish a safe construction from an
unsafe one that has never met a NULL.

## Phase 3 — absent-as-zero

| Construction | Where | Verdict |
| --- | --- | --- |
| `NULLIF(ta_prev, 0)` | LA Signals, denominator | **Safe and correct.** Converts a zero denominator to NULL to avoid divide-by-zero, and the CASE guards NULL first. |
| `COALESCE(s11.sl_count, 0)` | `supported_living_locations` | **Safe in context, with a latent risk.** The subquery counts CQC locations, so an absent row genuinely means zero locations. But if S11 ever failed to load wholesale, every authority would read a confident 0 rather than NULL. Absence of a count and a count of zero are indistinguishable here. |
| `SUM(CASE WHEN period = … THEN measure ELSE 0 END)` | National Aggregates, ×5 | **Latent.** The `ELSE 0` correctly handles non-matching periods. But a NULL measure in a *matching* period yields NULL, and `SUM` skips NULLs, so an authority with absent data contributes nothing and the national total is understated with no indication of incompleteness. Not currently firing: the latest period has 0 NULL measures across 296 rows. |

## A third finding: national and LA figures disagreed by 692 households

Not a label defect, found while testing Phase 3.

National Aggregates reads `la_statutory_homelessness` directly. LA Signals
reaches it through a join that failed for two authorities. The two were
therefore computed from different populations, and **nothing reconciled them**:

| Run | National total | Sum of its own LA rows | Difference |
| ---: | ---: | ---: | ---: |
| 4, 5, 7, 9, 12 | 119,219 | 118,527 | **692** |
| 15 | 124,142 | 124,142 | **0** |

692 is exactly Sheffield (642) plus Barnsley (50). A briefing quoting the
national figure and a briefing summing the authority figures would have
disagreed, and neither would have been obviously wrong.

## Phase 4 — what was changed

**Nothing in the node SQL.** The labels were already hardened, Phase 2 found no
latent copy, and Phase 3 found no unsafe substitution in a directional
expression. Changing SQL to demonstrate activity would have been the wrong
outcome. No write-back was required and none was made.

## Phase 5 — the permanent gate

**Gate 15: no derived label stands on an absent measure.** Six derived columns
across both staging tables are checked against their input measures.

Asserted against the latest run only. Earlier runs are immutable audit data and
several are not reproducible, so gating them would create a permanent red that
no fix could clear.

Proved by injection: setting Sheffield's `ta_households_current` to NULL while
leaving its label in place — the exact historical state — turned gate 15 red
and the suite exited 1. Restored, it exits 2 with only the two known-red
entries.

---

# `data_quality` — reported, not fixed

A label computed from an absent measurement is one defect. A quality flag
reading `ok` beside it is a second and independent one, because the flag is
evidently not derived from what it appears to vouch for.

## What it actually reads

```sql
jsonb_build_object(
  'ta_current',     CASE WHEN ta_cur.households_in_ta IS NULL THEN 'missing'
                         WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap'
                         ELSE 'ok' END,
  'ta_trend',       CASE WHEN ta_cur.households_in_ta IS NULL THEN 'no_current_data'
                         WHEN ta_prev.households_in_ta IS NULL THEN 'no_prior_year'
                         ELSE 'ok' END,
  'rough_sleeping', CASE WHEN rs.rough_sleeping IS NULL THEN 'missing' ELSE 'ok' END,
  'marac',          CASE WHEN mc.cases_discussed IS NULL THEN 'missing' ELSE 'ok' END
)
```

**It reads 4 columns. `staging_la_signals` carries 36 signal columns.** It
vouches for none of the other 31 — not S9 discharge delays, not S11 supported
living, not S19 PIP, not S22 Council Taxbase, not HB, not LHA, not IMD, not the
housing register.

Each individual sub-case is NULL-safe. The defect is coverage, not logic.

## The data test

Rows carrying no "missing" marker anywhere in `data_quality`, while at least
one signal column on that row is NULL:

| Run | Rows | Clean flag over an absent measure |
| ---: | ---: | ---: |
| 4–11 | 296 | 267 |
| 12 | 296 | 173 |
| **15** | 296 | **173** |

**In the current published run, 173 of 296 authorities — 58% — carry a
`data_quality` object with no missing marker while holding at least one absent
signal.**

## It is published

`data_quality` is in `data/signals/staging_la_signals_latest.json` and
referenced by `index.html`. This is not an internal diagnostic; it reaches the
map.

## The decision required

The name and the placement assert a row-level verdict. The implementation
checks a fixed subset chosen when the table had far fewer columns, and it has
not grown as sources were added — every source since S9 is unvouched.

Two coherent options. **This is stopped for a decision rather than chosen:**

1. **Make it mean what it says.** Derive it from every signal column on the
   row, so a missing PIP or CTB figure shows as missing. Honest, and the flag
   becomes genuinely useful — but far more rows will show as incomplete,
   because far more rows *are* incomplete, and that changes what the map
   communicates.
2. **Rename it to what it checks.** Something like `core_signal_quality`, with
   the four covered columns named explicitly in the key set. Cheaper, no change
   to the map's meaning, and it stops the flag implying coverage it does not
   have.

Option 1 is more work and tells the truth about coverage. Option 2 is honest
about scope while leaving 31 columns unwatched. Doing neither leaves a flag
that will certify the next class of gap exactly as it certified this one.
