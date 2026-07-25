# la_code_lookup misroutes Barrow-in-Furness and omits Carlisle and South Somerset

**Date:** 2026-07-25
**Status:** accepted, with a live workaround pending remediation
**Affects:** every source that resolves geography through `la_code_lookup`

## Context

The S6 asylum support build resolves published LAD codes to live LAD24CD codes
with a code-first cascade: direct match against `la_boundaries`, then forward
resolution through `la_code_lookup`. Asy_D11 publishes pre-2023 district codes
for periods before the April 2023 local government reorganisation, so the
lookup is exercised heavily — 14 of 16 absent codes resolve through it.

Two codes did not resolve at all. Chasing them exposed a third problem.

## Root cause

`la_code_lookup` contains one wrong mapping and two omissions in the 2023
reorganisation block:

| Code | Area | Table says | Correct successor |
|---|---|---|---|
| E07000027 | Barrow-in-Furness | E06000063 Cumberland | **E06000064 Westmorland and Furness** |
| E07000028 | Carlisle | *absent* | **E06000063 Cumberland** |
| E07000189 | South Somerset | *absent* | **E06000066 Somerset** |

The Cumbria pair is a single off-by-one transcription. The Cumberland
predecessors are E07000026, E07000028, E07000029. Whoever populated the table
typed E07000026, E07000027, E07000029 — one digit out on the middle entry.
That one slip simultaneously created the wrong Barrow mapping and left Carlisle
with no row at all. South Somerset is an independent omission in the Somerset
block, which otherwise carries all its siblings.

## Verification

Confirmed against primary sources before proposing any mapping. No mapping was
inferred from sibling patterns alone.

- ONS area page for E06000063 Cumberland: "In 2023, this area replaced
  Allerdale (E07000026), Carlisle (E07000028) and Copeland (E07000029)."
- ONS area page for E06000064 Westmorland and Furness: "In 2023, this area
  replaced Barrow-in-Furness (E07000027), Eden (E07000030) and South Lakeland
  (E07000031)."
- ONS area page for E06000066 Somerset: "In 2023, this area replaced Mendip
  (E07000187), Sedgemoor (E07000188), South Somerset (E07000189) and Somerset
  West and Taunton (E07000246)."
- The Cumbria (Structural Changes) Order 2022, legislation.gov.uk.
- The Somerset (Structural Changes) Order 2022, legislation.gov.uk.

MapIt returns 404 for all three codes: it serves live areas only, so it cannot
adjudicate abolished districts. The ONS area pages are the usable authority
here.

## Decision

S6 does **not** write to `la_code_lookup`. Correcting a shared table as a side
effect of loading one source would silently change other sources' geography
without their verification suites running. Instead S6 carries a build-local
resolution layer, `BUILD_LOCAL_RECODES` in `s6_asylum_build.py`, applied ahead
of the lookup in the cascade and reported in the run log as match method
`build_local_pending_remediation`.

The three corrections were raised as a separate remediation task covering the
`la_code_lookup` repair, a full audit of every row in the table against the ONS
area page for its successor, the blast radius across already-loaded tables, and
whether W1 must re-run.

## Consequences

- **The database is currently inconsistent across sources on Cumbria.** S6 has
  Barrow-in-Furness under E06000064, which is correct. Every other source that
  went through `la_code_lookup` has it under E06000063, which is wrong. S6 is
  the only correct one. Do not reconcile S6 against another source on
  Cumberland or Westmorland and Furness until remediation lands.
- Carlisle (541 people across 19 rows) and South Somerset (67 across 14) would
  have been dropped as unresolvable without the workaround, and the build would
  have halted rather than loading partial data.
- The build-local layer is a workaround, not a design. It is removed once
  `la_code_lookup` is corrected and the audit confirms no other mapping is
  wrong. Removal is the precondition for S6's Phase 7 push.
- This gap had been seen before and misread. `outputs/s8b_project_memory.md`
  records "2 extinct LA codes (E07000028, E07000189) have no successor in
  `la_code_lookup`. These are harmless." They were harmless for S8b, whose
  source publishes current codes only. They are not harmless for any source
  publishing pre-2023 districts, and the note did not prompt anyone to check
  *why* two codes were missing — which would have surfaced the Barrow error
  two weeks earlier. The standing rule below generalises that lesson.

## Standing rule — unresolved codes are UNEXPLAINED until explained

**This is a pipeline-wide convention, not an S6 one. It applies to every build
in this repository.**

> Any build encountering codes it cannot resolve reports them as
> **UNEXPLAINED**, never as harmless, benign, expected, or safe to ignore. The
> explanation is a **gate**, not a note.

Classifying a gap without investigating it is precisely what let the Barrow
mapping stand for two weeks after it was already visible. The S8b build saw two
unresolvable codes, judged them harmless for its own purposes, and recorded that
judgement. The judgement was locally true and globally wrong, and because it was
written as a conclusion rather than an open question, nobody asked why a
reorganisation block was missing two of its members.

In practice, for any build:

1. An unresolved code is a **hard stop**, not a filtered row. Do not proceed
   with partial geography and flag it afterwards.
2. Investigate *why* the code is unresolved before deciding what to do about it.
   A missing entry in a lookup table is evidence about the lookup table, not
   only about the current source.
3. Record the finding as UNEXPLAINED until an authoritative source resolves it —
   for geography, the ONS area page for the successor, or the relevant
   structural changes order. Sibling patterns are a hypothesis, not a
   verification.
4. If the code genuinely does not matter for the source in hand, say *that*
   explicitly and say what would make it matter. Never write "harmless" without
   the scope it is harmless within.

## How to retire the workaround

1. Remediation task corrects E07000027 and inserts E07000028 and E07000189.
2. Full-table audit reports zero remaining mismatches against ONS.
3. Delete `BUILD_LOCAL_RECODES` from `s6_asylum_build.py`.
4. Re-run S6 and test against the criteria below.

### Primary criterion — the data must not move

**The Check 7 idempotency checksum must equal
`667f97f0a47bc2090dc55190a1d1c377` exactly, byte-identical.**

This is the criterion that matters. If the corrected `la_code_lookup`
reproduces what the build-local layer was already doing, the loaded data
*cannot* change — the three codes resolve to the same three targets either way.
Any difference means the remediation did not reproduce the build-local
behaviour, and diffing `la_asylum_support` against the pre-retirement state
localises the problem to specific periods and local authorities.

The checksum is `md5` over `period_ending : lad24cd : support_type :
accommodation_type : people`, ordered, across the whole table. It is computed
by `checksum()` in `s6_asylum_build.py` and reported by Check 7 on every run.

### Secondary criteria — the cascade report

These confirm the mechanism rather than the outcome:

- `code_historical_forward` resolves **16 codes** (equivalently **16
  `(code, name)` pairs** — see the note below).
- `build_local_pending_remediation` resolves **0 codes**.
- Check 9 still reports **286/286** English LAs exact.
- All 13 verification checks still pass.

### Note on 16 versus 17

An earlier draft of these criteria said 17. That was wrong, and the arithmetic
is worth recording so it is not reintroduced.

Sixteen distinct English LAD codes appear in the source but are absent from
`la_boundaries`, so all sixteen require forward resolution. Under the current
build they split:

| Route | Codes | Which |
|---|---:|---|
| `code_historical_forward` | 13 | E07000029, E07000030, E07000031, E07000163, E07000164, E07000165, E07000168, E07000169, E07000187, E07000188, E07000246, E08000038, E08000039 |
| `build_local_pending_remediation` | 3 | E07000027, E07000028, E07000189 |
| **total** | **16** | |

The 17 came from adding the build-local 3 to the figure 14, which was the Gate 2
count taken *before* the build-local layer existed. That 14 already contained
E07000027 — resolved then, wrongly, to Cumberland. Adding 3 double-counted it.
After remediation all 16 route through `code_historical_forward`.

**Codes and pairs are 1:1 here.** No LAD code in the loaded window carries more
than one published name variant, so "16 codes" and "16 `(code, name)` pairs"
are the same statement. (Two *names* map to two codes each — Barnsley to
E08000016 and E08000038, Sheffield to E08000019 and E08000039 — but that is the
opposite direction and does not affect the count.) The criterion is unambiguous
in either unit.
