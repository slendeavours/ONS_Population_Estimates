# 2026-08-16 — S.114 attribution: retain against the issuer, never propagate

Gate 14's last red. Two S.114 notices, dated 2018-02-02 and 2018-07-01, sit
against `E10000021` — Northamptonshire County Council, abolished 31 March 2021,
its area split between North Northamptonshire (`E06000061`) and West
Northamptonshire (`E06000062`). The code is absent from `la_boundaries`, so the
gate reported an orphan.

## The decision

**Retain against the historical code. Do not propagate to successors.**

Attributing to both successors would say two authorities issued notices when
one did, and would attribute a financial-distress event to two bodies that did
not exist at the time. Attributing to neither, by dropping the code, loses a
real event — the county's failure is substantially why the local government
reorganisation happened. The notice stays attached to the entity that issued
it.

## The premise, checked before it was written into the record

The argument rests on Northamptonshire being two-tier in 2018, with the county
holding social care and education while housing and homelessness sat with the
seven district councils — so the issuing authority did not hold the functions
S12 exists to signal about.

**Confirmed from this pipeline's own coverage rather than asserted:**

| Function | Authorities carrying a figure |
| --- | ---: |
| Statutory homelessness (S1) | **296 / 296** |
| Housing register (S13) | **296 / 296** |
| Care leavers in semi-independent accommodation (S4) | **132 / 296** |

Housing and homelessness are held by every authority including districts.
Children's social care is held by 132 — the upper tier only. `E10000021` is an
`E10` code, which is definitionally a county council, so it sat in the 132 and
not in the 296.

`la_code_lookup` corroborates the structure directly: the seven Northamptonshire
districts `E07000150`–`E07000156` are recorded as `new_unitary` predecessors of
the two successors. It was the *districts* that were merged, and they are the
authorities that held the housing function.

## Two checks before the fix

**Is E10000021 unique?** Yes, and the answer is a rule rather than a special
case. Every other issuer is a housing authority in its own right — four
unitaries, one district, one metropolitan district, four London boroughs — and
all resolve in `la_boundaries`. E10000021 is the only abolished code and the
only county code in the table. The rule is therefore: *the issuer either is or
is not a current authority*, and where it is not, the row must say so.

**Does anything join by predecessor?** No. No view or materialised view depends
on `la_s114_notices`; the only consumer is Workflow 1's LA Signals node, which
joins `SELECT DISTINCT lad24cd` against `la_boundaries`. `E10000021` is also
absent from `la_succession`, so no propagation path exists today.

## Implementation

`E10000021` was deliberately **not** added to `la_code_lookup` as a successor
mapping. One predecessor with two successors doubles on any join by
predecessor — the `la_succession` fan-out defect already found and fixed here.
That would have cleared an orphan and created a duplication.

Instead, three additive columns on `la_s114_notices`:

- `attribution` — `direct` where the issuing authority still exists (13
  notices, 10 authorities), `predecessor` where it does not (2, both
  Northamptonshire).
- `successor_codes text[]` — **reference only, never a join path.**
- `attribution_note` — why this row is attributed as it is.

A CHECK constraint enforces the pairing: a `predecessor` row cannot exist
without both successor codes and a note. Proved — a half-declaration is
rejected by the constraint.

## Gate 14 is narrower, not weaker

Before, a code was judged only against `la_boundaries`. Now an unresolved code
must **either resolve or explain itself**, and the exemption is a positive
row-level declaration rather than a tolerated code: `attribution =
'predecessor'` with successor codes and a note. An orphan that declares nothing
still fails. The gate also now reports which predecessors it accepted, so the
exemption is visible in the output rather than silent.

The mechanism is generic — any table carrying all three columns can declare —
so if a second abolished issuer ever appears, the rule already covers it.

**Proved by injection.** An undeclared orphan (`E10000099`, `attribution =
'direct'`) fails gate 14 and exits **1**. Removed, the suite exits **0** with
all 17 gates passing.

## The successors carry no flag

Confirmed in run 17: `E06000061` and `E06000062` both hold `s114_flag = false`,
and 10 of 296 authorities are flagged. Filtering the W1 join to
`attribution = 'direct'` was tested and matches the same 10 authorities, so the
join is already correct — the predecessor cannot match `la_boundaries`. The
node was left unchanged rather than altered for a provably identical result,
with a run already created today.

If the county's distress should ever be visible against its successor area, it
belongs as a **separate predecessor-distress indicator**, never folded into
`s114_flag` — that flag reads "this authority issued a S.114", and for North
and West Northamptonshire that is false.

## A finding about known-red itself

Gate 14 was proved by injection *before* its `known_red.json` entry was
removed. The injected failure — a genuine one — was absorbed as known-red, and
the suite exited **2 instead of 1**.

**A stale known-red entry does not merely go unread; it suppresses the next
real failure of that gate.** The entry must be removed in the same change that
turns the gate green. Recorded in `KNOWN_RED.md` and in the JSON comment.

`known_red.json` is now empty and the suite exits 0.
