# Full audit of la_code_lookup: thirteen defects across four reorganisations

**Date:** 2026-07-26
**Status:** accepted, applied
**Widens the scope of:** `2026-07-25-la-code-lookup-cumbria-off-by-one.md`

## Why a full audit

The Cumbria record closed on a single transcription error. That framing was too
narrow. Auditing every row found the same class of defect in three further
reorganisations. **One typo is a mistake; four is a table that was never
verified against a source.**

## Coverage

| | Rows | Authority |
|---|---:|---|
| Total | 333 | |
| Identity (`old_code = new_code`) | 296 | `la_boundaries` — exact set equality, zero name disagreements |
| Non-identity, ONS "replaced" sentence | 23 | ONS area pages for E06000063/64/65/66, E08000016/19 |
| Non-identity, secondary sources | 14 | GOV.UK, council sites, findthatpostcode — ONS publishes no predecessor sentence for the 2019, 2020 and 2021 changes |
| **Not audited** | **0** | |

Structural integrity was checked across the whole table: no duplicate
`old_code`, no chained mappings, no target missing from `la_boundaries`, no
live LAD used as a source. All clean.

## The thirteen defects

Three found earlier, fixed 2026-07-25:

| Code | Area | Was | Corrected to |
|---|---|---|---|
| E07000027 | Barrow-in-Furness | E06000063 | E06000064 |
| E07000028 | Carlisle | *absent* | E06000063 |
| E07000189 | South Somerset | *absent* | E06000066 |

Ten found by this audit, applied 2026-07-26:

| Code | Area | Was | Corrected to |
|---|---|---|---|
| E07000150 | Corby | E06000062 West Northants | **E06000061 North Northants** |
| E07000151 | Daventry | E06000061 North Northants | **E06000062 West Northants** |
| E07000201 | Forest Heath | E07000244 East Suffolk | **E07000245 West Suffolk** |
| E07000004 | Aylesbury Vale | E07000245 West Suffolk | **E06000060 Buckinghamshire** |
| E07000005 | Chiltern | E07000245 West Suffolk | **E06000060 Buckinghamshire** |
| E07000204 | St Edmundsbury | *absent* | E07000245 West Suffolk |
| E07000206 | Waveney | *absent* | E07000244 East Suffolk |
| E07000006 | South Bucks | *absent* | E06000060 Buckinghamshire |
| E07000007 | Wycombe | *absent* | E06000060 Buckinghamshire |
| E10000002 | Buckinghamshire county | *absent* | E06000060 Buckinghamshire |

Corby and Daventry were **transposed** — the identical failure mode to the
Cumbria off-by-one, in a different county. Aylesbury Vale and Chiltern are
Buckinghamshire districts that had been filed under **West Suffolk**, a county
they have no relationship with.

The whole 2020 Buckinghamshire reorganisation was absent; only the identity row
`E06000060 → E06000060` existed. It was included in full even though only two
of its five predecessors were actively wrong, because fixing Aylesbury Vale and
Chiltern alone would have assembled Buckinghamshire from part of its districts.
**Partial coverage of a reorganisation is worse than none** — it produces
plausible totals that are silently short.

## Why no reload was needed

Every one of the ten was **latent**. No loaded source presents any of those
codes within its loaded window:

- **S6 asylum** publishes E07000150, E07000004, E07000005 and E07000007 — but
  only in periods from 2014 to 2017, all below its 2018-01-01 floor. From 2018
  the Home Office publishes current codes for those areas.
- **S13 housing register** holds exactly **296 distinct LAs in every year from
  2015**, an identical set to 2025. England had roughly 326 districts in 2015,
  so that source cannot ever have carried historical geography.
- **S4 care leavers** holds 130–132 upper-tier LAs, all current.
- **S12 EFS** has no rows before 2023-24.

Contrast with the Cumbria defect, which **was** live: Barrow-in-Furness carried
386 people across 14 rows inside S6's loaded window and would have been
attributed to Cumberland.

The retirement of S6's build-local workaround is the control. With the
workaround deleted and resolution going wholly through the corrected lookup,
the reload reproduced checksum `667f97f0a47bc2090dc55190a1d1c377`
byte-identically, and the cascade reported 16 codes via
`code_historical_forward` and 0 via `build_local_pending_remediation`.

## Column naming

`old_code` and `new_code` do not mean chronologically older and newer. They
mean **code as published by a source** and **code used by this pipeline**. The
Barnsley row proves it: ONS states *"In 2025, this area was replaced by the new
Barnsley (E08000038)"*, so E08000038 is the newer code — yet it sits in
`old_code`, mapping to E08000016, the LAD24 code this pipeline keys on.

Both columns and the table now carry `COMMENT ON` text saying so, with the
Barnsley example. **Renaming was deliberately deferred**: the columns are
referenced by every view and geography-resolving query in the pipeline, and a
rename belongs in its own change. Misleading names are part of how two of these
defects survived review.

## Consequences

- Any future source publishing pre-2019, pre-2020 or pre-2021 district codes
  now resolves correctly. Before this, such a source would have been silently
  misattributed for Northamptonshire and Suffolk, and would have failed
  outright for Buckinghamshire.
- The standing rule from the Cumbria record — unresolved codes are UNEXPLAINED
  until explained — is what surfaced this. It deserves an extension:
  **a shared lookup table is not evidence of itself.** `la_succession` was
  checked as corroboration and turned out to have been migrated from
  `la_code_lookup` on 2026-07-11, inheriting the same errors. A second table is
  only a second opinion if it has an independent origin.
- `scripts/fix_la_code_lookup_2026_07_26.py` applies the ten corrections in a
  single transaction with before/after reporting and an integrity re-check that
  rolls back on failure.
