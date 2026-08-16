# Decision records — what is closed and what is still open

Each record was written against the state at the time and is left as written;
a record that gets quietly edited later is no longer evidence of anything.
This index is the reconciliation. **Read it first** — it says which findings
are settled and which are still live.

Status as at **2026-08-16**. The verification suite exits **0**, all **18**
gates pass, and `known_red.json` is empty.

## Still open

Three things are registered but unresolved. Each is discoverable from the data
as well as from here.

### 1. The S1 A1 back-series revision — seven quarters

**2023Q2 to 2024Q4 no longer match what MHCLG publishes.** 200–230 of 296
authorities differ per quarter, 790–863 cells, on `total_assessments`,
`owed_duty`, `prevention_duty` and `relief_duty`. Confirmed as publisher
revision rather than an extraction fault: the 2023Q2 file itself gives
Hartlepool 172 where the table holds 193; `homelessness_quarter_urls.notes`
already said "Revised" for exactly those periods; the Oct–Dec 2024 release was
republished in June 2026, after the 2026-04-01 bulk load; and 2025Q2 came from
that same load and reproduces exactly.

**Not restated.** A reload is its own item. Until then the seven rows carry
**mixed provenance** — `support_needs_total` from the current edition, the A1
measures from the 2026-04-01 load — which is why `source_file` is deliberately
NULL rather than asserting the whole row came from one file.

Discoverable without reading this: `homelessness_quarter_urls.reproduces_from_source`,
joined onto the data by `v_la_statutory_homelessness`.

Record: [2026-08-16-s1-reconstruction-markers-and-revision.md](2026-08-16-s1-reconstruction-markers-and-revision.md) §3, §10, §12.

### 2. S10 rough sleeping — suppression markers unverified

`la_rough_sleeping` holds 22 and 27 zeros with **no NULL anywhere** — the same
signature as S1's `..`-stored-as-zero defect. A genuine zero is plausible for a
snapshot count, so the table cannot settle it either way. It settles the way S1
did: extract from source and compare. S10 also fetches a pre-processed CSV with
no committed extraction code, so the extraction likely has to be written first.

### 3. Historical stored zeros in S1, seven quarters

129 stored zeros across 2023Q2–2024Q4 remain **ambiguous**. Markers were
corrected only for 2025Q2 and 2025Q3, the quarters that reproduce exactly and
therefore the only ones where the verdict is evidence rather than inference.
Resolved by the same reload as item 1.

## Closed

| Record | What it settled |
| --- | --- |
| [2026-08-16-s114-attribution-and-gate-14.md](2026-08-16-s114-attribution-and-gate-14.md) | S.114 notices attributed to the issuing authority, never propagated. Gate 14 narrowed to require an unresolved code to declare itself. Last red gate cleared. |
| [2026-08-16-s1-reconstruction-markers-and-revision.md](2026-08-16-s1-reconstruction-markers-and-revision.md) | S1 extraction rebuilt; `period` is a financial-year quarter; markers corrected for the reproducible quarters; `support_needs_total` corrected across all seven. **Partly open — see above.** |
| [2026-08-16-data-quality-derived-from-every-column.md](2026-08-16-data-quality-derived-from-every-column.md) | `data_quality` derived from all 33 signal columns rather than 4. Gate 16 added. |
| [2026-08-15-w1-null-safety-audit.md](2026-08-15-w1-null-safety-audit.md) | Every W1 label made NULL-safe. Gate 15 added. |
| [2026-08-15-w1-period-pin-restatement.md](2026-08-15-w1-period-pin-restatement.md) | Hardcoded period literals removed from both W1 nodes. |
| [2026-08-14-s1-support-need-column-misalignment.md](2026-08-14-s1-support-need-column-misalignment.md) | Five support-need columns quarantined. **Mechanism identified 2026-08-16**: the 2025Q4 A3 restructure shifted the block three columns left, which is why the misalignment varied by quarter rather than being a constant offset. |
| [2026-08-14-s1-quarter-gap-and-provenance.md](2026-08-14-s1-quarter-gap-and-provenance.md) | The 2025Q1 gap and unrecorded 2025Q3 provenance. Both closed 2026-08-16. |
| [2026-08-14-s8-superseded-by-s8b.md](2026-08-14-s8-superseded-by-s8b.md) | S8 deprecated in favour of S8b. |
| [2026-08-14-barnsley-sheffield-code-split.md](2026-08-14-barnsley-sheffield-code-split.md) | E08000038/39 resolved through `la_code_lookup` on `recode` only. |
| [2026-08-13-stored-node-drift-and-register-authority.md](2026-08-13-stored-node-drift-and-register-authority.md) | The stored n8n node is the authority; write-back in the same session. |
| [2026-07-26-la-code-lookup-full-audit.md](2026-07-26-la-code-lookup-full-audit.md) | Full lookup audit. Dorset 2019 districts still absent. |
| [2026-07-25-la-code-lookup-cumbria-off-by-one.md](2026-07-25-la-code-lookup-cumbria-off-by-one.md) | Cumbria/Somerset mapping error. |
| [2026-07-25-credential-default-exposure.md](2026-07-25-credential-default-exposure.md) | Shipped-default credentials. `N8N_ENCRYPTION_KEY` rotation still outstanding. |
| [2026-07-22-hb-accom-type-publication-lag.md](2026-07-22-hb-accom-type-publication-lag.md) | S8b publication lag is monthly, not quarterly. |
| [2026-07-12-s11-cqc-la-mapping-method.md](2026-07-12-s11-cqc-la-mapping-method.md) | CQC locations mapped by point-in-polygon. |

## The recurring defect, across nine of these records

**State from evidence, never from intent.** Every instance took the same shape:
something recorded what was *meant* to be true and nothing checked whether it
*was*.

`homelessness_quarter_urls.loaded = true` with zero rows. A fingerprint
short-circuit reporting `no_change` without fetching. A hardcoded period pin
that was right the day it was typed. `..` stored as `0`, collapsing absent and
zero. `data_quality` certifying 4 columns while claiming to cover the row.
A `falling_strongly` label over an absent measure. A known-red entry outliving
its defect and absorbing the next real failure. A hand-edited registry field
that reverts for 12 sources and persists for 15. A backup note asserting work
was outstanding when it had been running for three weeks.

The countermeasure is the same each time: derive the claim from the data, and
have a gate assert it.

## Two diagnostics worth reusing

**Rate is a signature, not just a magnitude.** A divergence hitting 99% of
authorities on one column while hitting 75% on the others is not a revision —
revisions touch the authorities that resubmitted, misalignment touches
everyone. That is what separated the two S1 defects, which had been read as
one.

**Test the data, not the code.** A code read reported `revision_note` as
hand-maintained when the backfill demonstrably writes it. Perturbing every
column and seeing what the backfill restored got it right, and revealed that
management is per *source* as well as per column.

## Where the controls live

- `scripts/verify_source_registry.py` — 18 gates. Exit 0 clean, 2 known-red
  only, 1 stop.
- `scripts/push.py` — the only sanctioned push. Scan, verify, then push, in one
  place so the order cannot be forgotten. `--install-hook` gates a bare
  `git push` too.
- `docs/GENERATED_FIELDS.md` — which registry fields are generated and from
  where.
- `docs/KNOWN_RED.md` — currently empty, and why a stale entry is dangerous.
