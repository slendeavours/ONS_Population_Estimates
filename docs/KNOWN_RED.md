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

| Gate | Defect | Owner | Fix by | Item |
| ---: | --- | --- | --- | --- |
| 13 | `homelessness_quarter_urls` and `la_statutory_homelessness` disagree in both directions: 2025Q1 marked loaded with zero rows, 2025Q3 loaded with no register row. | Scott | 2026-09-30 | Backlog item 6 — S1 reconstruction and the 2025Q1 reload |
| 14 | `la_s114_notices` holds `E10000021` (Northamptonshire County Council, abolished 2021), which is not in `la_boundaries` and has no `la_code_lookup` entry. | Scott | 2026-09-30 | Backlog item 10 — S12 Northamptonshire S.114 attribution |

## Gate 13 — S1 quarter gap

Full detail in
[2026-08-14-s1-quarter-gap-and-provenance.md](decisions/2026-08-14-s1-quarter-gap-and-provenance.md).
Blocked on S1 having no build script and the ODS-to-CSV extraction having no
committed code.

## Gate 14 — Northamptonshire S.114 attribution

Two S.114 notices dated 2018-02-02 and 2018-07-01 sit against E10000021.
Northamptonshire County Council issued them; it was abolished on 1 April 2021
and its functions split between North Northamptonshire (E06000061) and West
Northamptonshire (E06000062).

**This is a judgement, not a mapping, which is why it is not fixed in
passing.** Attributing the notices to both successors implies two authorities
issued notices when one did. Attributing to neither loses the event. Leaving
the county code in place keeps the record accurate and the join broken.

Whatever is decided has to be recorded as a decision, because the reasoning is
the part that will not be obvious later.
