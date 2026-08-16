# Known-red gates

Gates that fail today for a defect that is understood, owned and dated.

**This is not a tolerance list.** A suite that exits 1 as its normal state
stops being a signal: within a few weeks nobody reads the output, they read
the exit code and shrug. Every entry here has an owner and a date, and
`verify_source_registry.py` escalates an entry to a hard failure once its date
passes. Expiry is the mechanism that stops this file becoming a place where
defects go to be forgotten.

A gate is never weakened to clear it. It goes green because the data is fixed.

Machine-readable copy: [`known_red.json`](known_red.json). This document is the
explanation; the JSON is what the suite reads.

## Current entries

**None.** The suite exits 0 with all 17 gates passing.

## A stale entry is not harmless

Discovered 2026-08-16 while proving gate 14 by injection. Gate 14 had gone
green, but its `known_red.json` entry had not yet been removed — and an
injected undeclared orphan, a genuine failure, was absorbed as known-red and
the suite exited **2 instead of 1**.

An entry that outlives its defect does not merely go unread. It suppresses the
next real failure of that gate. **Remove an entry the moment its gate goes
green**, in the same change that fixes it.

## Resolved

### Gate 13 — S1 quarter gap — resolved 2026-08-16

`homelessness_quarter_urls` and `la_statutory_homelessness` disagreed in both
directions: 2025Q1 marked loaded with zero rows, 2025Q3 loaded with no register
row. Blocked on S1 having no build script and the ODS-to-CSV extraction having
no committed code.

Extraction rebuilt as `scripts/s1_extract_ods.py`. Eleven quarters loaded,
296 rows each. Register rows added for 2025Q3 and 2025Q4 with URLs verified by
`Content-Length` against the stored files, and `loaded` re-derived from actual
row counts rather than set by intent. Detail in
[2026-08-14-s1-quarter-gap-and-provenance.md](decisions/2026-08-14-s1-quarter-gap-and-provenance.md)
and
[2026-08-16-s1-reconstruction-markers-and-revision.md](decisions/2026-08-16-s1-reconstruction-markers-and-revision.md).

### Gate 14 — Northamptonshire S.114 attribution — resolved 2026-08-16

Two S.114 notices dated 2018-02-02 and 2018-07-01 sit against E10000021.
Northamptonshire County Council issued them; it was abolished on 31 March 2021
and its area split between North Northamptonshire (E06000061) and West
Northamptonshire (E06000062).

**This was a judgement, not a mapping, which is why it was not fixed in
passing.** Attributing the notices to both successors implies two authorities
issued notices when one did. Attributing to neither loses the event.

Resolved by retaining the notices against the issuing code with an explicit
`attribution` declaration, and by narrowing gate 14 so an unresolved code must
either resolve or explain itself. Detail in
[2026-08-16-s114-attribution-and-gate-14.md](decisions/2026-08-16-s114-attribution-and-gate-14.md).
