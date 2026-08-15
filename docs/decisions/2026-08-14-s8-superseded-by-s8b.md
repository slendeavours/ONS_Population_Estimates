# 2026-08-14 — S8 superseded by S8b, and DWP revises silently

## The decision

S8 (DWP Housing Benefit claimants in specified accommodation) is
`status = 'superseded'`, `superseded_by = '8b'`. `hb_sa_caseload` will be
sourced from S8b. `la_hb_sa_caseload` is kept rather than dropped, so the
provenance of W1 runs 4 to 12 stays readable.

## Why, established rather than assumed

S8 was tier A and 105 days overdue with no build script. Rather than
reconstruct a loader, the reproduction gate was run first, and it answered a
different question.

S8 and S8b read the **same measure from the same Stat-Xplore database**:
`str:count:hb_new:V_F_HB_NEW`, filtered to accommodation type
`C_SATA:1` (Specified Accommodation). A live probe of 202511 settled whether
they are the same measurement:

| Comparison | Match |
|---|---:|
| Live API vs S8 stored (loaded 2026-04-01) | 11 / 296 |
| Live API vs S8b SA (loaded 2026-07-22) | **296 / 296** |

Totals: live 223,856 = S8b 223,856; S8 stored 223,323.

S8b already carries six months (202509–202602) against S8's one. Two sources
maintaining one number will diverge, and nothing in the pipeline would surface
it — the row counts stay complete either way. Removing the duplicate is the
honest way to close the overdue flag; feeding it would have preserved the
divergence.

## The reproduction gate did its job by failing

The method is reproduce-exactly-before-advancing. 202511 could not reproduce,
and the three-way ambiguity resolved to a fourth answer: not a reconstruction
error, not undocumented original behaviour, but **the source revised**.

That was only distinguishable because S8b is an independent second load of the
same field. Comparing against it turned a failed reproduction into a definite
finding rather than an unexplained mismatch.

The reconstruction was still worth building: it validated the recodes pattern,
the geography resolution and the response parse against a known-good load,
296/296. That corroboration is recorded on the S8b registry row. No second
loader was built.

## DWP revises the HB caseload in place, and says nothing

202511 SA moved on **285 of 296 LAs** between the S8 load on 2026-04-01 and the
S8b load on 2026-07-22. Birmingham went 31,117 → 34,101, +9.6%. No revision
note appeared in any check, and none is published anywhere the checker looks.

`revises_back_series` is set on S8b.

**This is not a blanket Stat-Xplore property.** S19 PIP was probed the same
way: Apr-26 reproduced exactly, 296 of 296, delta 0.000%. So the flag is set on
the dataset that demonstrably revises, not on the platform.

The two tests are **not like-for-like** and that is worth stating: HB was
compared over a four-month window, PIP over one. PIP is therefore left
unflagged rather than asserted not to revise — absence of revision over one
month is not evidence of a source that does not revise.

## Downstream: this is a correction, not a cleanup

Birmingham's `hb_sa_caseload` is **31,117 in every run from 4 to 12**. The
current figure is **34,101** — a 9.6% understatement in a signal that reaches
the map and the briefings, and 284 other LAs move too.

Repointing to S8b corrects published figures. It is bundled with the W1 period
pin so both land as one correction event with one re-export and one
restatement, rather than two.

## Geography: nothing was lost to the unresolved codes

S8b's build recorded E07000028 (Carlisle) and E07000189 (South Somerset) as
unresolvable, because it ran on 2026-07-22, before the 2026-07-26
`la_code_lookup` correction. A code that cannot resolve at load time may be
dropped rather than deferred, which would have undercounted Cumberland and
Somerset for every month held.

Checked directly against the API for 202509, 202511 and 202602: **both codes
carry zero claimants in every month**, and S8b's stored successor values match
the API exactly. Nothing was lost. Geography now resolves 296/296 with zero
unresolvable codes.

## `vw_source_due` gained `retired`

A superseded source kept reporting `overdue` against a cadence nobody intends
to meet — S8 read 106 days overdue while the measurement it carried had already
moved. `due_status` now returns `retired` for `superseded` and `deprecated`,
ahead of every other branch.
