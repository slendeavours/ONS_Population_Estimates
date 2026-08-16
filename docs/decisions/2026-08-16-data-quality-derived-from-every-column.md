# 2026-08-16 — `data_quality` derived from every column, and a reconciliation gate

Two changes to published output. Run 16 supersedes run 15.

## `data_quality` now checks what it claims to

### What it was

Four columns: `ta_current`, `ta_trend`, `rough_sleeping`, `marac`.
`staging_la_signals` carries **36 signal columns**, so 31 were unvouched —
every source from S9 onward, including PIP, Council Taxbase, CQC supported
living, discharge delays, Housing Benefit, LHA, IMD and the housing register.

Each sub-case was individually NULL-safe. The defect was coverage, not logic.

### Before and after, run 15 to run 16

| | Reads clean | Flags something |
| --- | ---: | ---: |
| **Before** — 4-column flag | **274 of 296** | 22 |
| **After** — all 33 checked | **105 of 296** | 191 |

**The feed reported 274 authorities as clean when 105 have complete data.** For
169 authorities the flag said nothing was wrong while something was.

Distribution of absent measures per authority:

| Absent measures | Authorities |
| ---: | ---: |
| 0 | 114 |
| 1 | 158 |
| 2 | 8 |
| 3 | 9 |
| 4 | 5 |
| 5 | 2 |

Most frequently absent: `care_leavers_semi_indep` (164 authorities — S4 is
upper-tier only, so district-level authorities have no figure), then MARAC (14)
and RO4 spend (9).

### What it emits

```json
{
  "core": { "ta_current": "ok", "ta_trend": "ok",
            "rough_sleeping": "ok", "marac": "missing" },
  "signals_checked": 33,
  "absent_count": 5,
  "absent": ["marac_cases", "marac_rate_per_10k", "ro4_bb_spend_000",
             "ro4_nightly_spend_000", "ro4_total_homelessness_000"],
  "suppressible_absent_count": 0,
  "suppressible_absent": [],
  "verdict": "incomplete",
  "not_checked": ["supported_living_locations", "efs_flag", "s114_flag"]
}
```

### Four design decisions, and why

**A count and a named list, not a severity band.** The requirement offered
severity tied to the dual-lens classification as an alternative. That
classification covers **5 of 27 sources** in `source_registry`, and none of the
signal-bearing ones except S19, so a primary-versus-context severity would have
meant inventing classifications the registry deliberately leaves NULL. The
named list carries the same information without asserting a judgement that has
not been made.

**Absent and suppressed are counted separately.** DWP applies disclosure
control to PIP, and NHS suppresses MHS26 for 28–46% of authorities per month.
Both land as NULL and neither is a gap of the same kind as an unreturned
submission. **91 authorities carry at least one possibly-suppressed absence.**

The distinction is made **per column, not per value**, because the stored data
cannot tell them apart — a suppressed PIP figure and an absent one are both
NULL. Claiming otherwise per row would be inventing knowledge.

**`core` preserves the previous four-key verdict verbatim.** Nothing in
`index.html` depends on it — see below — but it is in the published JSON feed
and something outside this repository may read it.

**Three columns are excluded and say so.** `supported_living_locations` is
`COALESCE(…, 0)` and can never be NULL, so counting it would report present
regardless of whether S11 loaded at all. `efs_flag` and `s114_flag` are
booleans carrying their own absent-as-false problem. Listing them under
`not_checked` is honest; silently including them would have inflated the
completeness figure.

### A correction to the brief

The brief said `data_quality` is "referenced by `index.html`" and asked for the
legend to be updated. **`index.html` references it exactly once, to exclude
it:**

```javascript
if (k === 'data_quality') return;
```

It is copied out of the signal object and never rendered — no legend, no
tooltip, no display. So there is no published legend to update, and no
distribution the map depends on.

That is worth stating plainly: **the flag reaches the JSON feed, which anyone
can consume, but the map itself has never displayed it.** The overstatement was
in the feed, not on the map.

## Gate 16 — national reconciles to the sum of its own LA rows

`staging_national` reads the source tables directly; `staging_la_signals`
reaches them through a join. For two authorities the join failed, so the two
were computed from different populations and **nothing compared them**.

| Run | National TA | Sum of its own LA rows | Gap |
| ---: | ---: | ---: | ---: |
| 4–12 | 119,219 | 118,527 | **692** |
| 15, 16 | 124,142 | 124,142 | 0 |

692 is exactly Sheffield (642) plus Barnsley (50). No NULL test would catch
this — it needed the arithmetic checked against itself.

Eight sum measures are asserted. `ta_yoy_pct` is a ratio, and summing
per-authority percentages is meaningless, so it is **asserted on its correct
relationship** — the national percentage must equal the change between the two
national totals — rather than skipped.

Proved by injection: adding 692 to the national TA total reproduced the
historical shape, turned gate 16 red and exited 1. Restored, exit 2.

## Apply record

Node 5 updated in `n8ndb` with readback confirmation, contract check passing at
40 columns with zero warnings, run 16 created through the Create Run node,
W1 re-run, map feed re-exported and verified carrying `run_id 16` and the new
field.

Gate 15 (no label over an absent measure) and gate 16 both pass on run 16. The
suite exits 2 on the two known-red entries, both within date.
