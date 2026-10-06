# Decision records — what is closed and what is still open

Each record was written against the state at the time and is left as written;
a record that gets quietly edited later is no longer evidence of anything.
This index is the reconciliation. **Read it first** — it says which findings
are settled and which are still live.

Status as at **2026-10-06** (S1 items 1 and 3 and the new open items below; the rest as at 2026-09-30). The verification suite exits **0**, all **21**
gates pass, and `known_red.json` is empty. The eight records of 2026-08-20
and 2026-08-21 were reconciled on 2026-09-30, each checked against the
live database, git tree or Pages runs rather than taken from its own text.

## Still open

Three things are registered but unresolved; the three bullets under item 2 are latent. Each is discoverable from the data
as well as from here. S1 items 1 and 3 of the earlier list closed on 2026-10-06 and now sit in the Closed table.

### 1. S10 rough sleeping — suppression markers unverified

`la_rough_sleeping` holds 22 and 27 zeros with **no NULL anywhere** — the same
signature as S1's `..`-stored-as-zero defect. A genuine zero is plausible for a
snapshot count, so the table cannot settle it either way. It settles the way S1
did: extract from source and compare. S10 also fetches a pre-processed CSV with
no committed extraction code, so the extraction likely has to be written first.

### 2. Three latent database defects, none affecting the map

Found in the 2026-09-30 reference-CSV reconciliation; assessed the same day.
Nothing reads any of them, so none is urgent. See
`2026-09-30-reference-csvs-out-of-step.md`.

- `la_housing_register` keeps one arbitrary predecessor row for eleven
  reorganised authorities, 2015 to 2023. The map reads the latest year only,
  so it matters only if someone builds a waiting-list trend.
- `ro4_housing_expenditure.hra_admin_prevention_relief_net_exp_000` holds TA
  administration net spend. Unread; ignore unless something starts using it.
- `total_housing_gross_exp_000` and `_net` were not refreshed with the rest of
  the third release: eight authorities are NULL and Islington and Leicester
  differ. Unread by the map.

### 3. Left open by the S1 edition work

None of these was fixed by it.

- **National TA year-on-year is not like-for-like.** In the W1 national
  aggregates, 13 authorities with a current value and no prior-year value add
  16,304 households to the current total only: 13.29% reported against about
  -0.8% like-for-like. Unchanged by the edition work (130,775 / 115,431 before and after).
- **`la_homelessness_support_needs` (S1b) is stale for 2023Q2 to 2024Q4.** Those
  rows were built from the older files, not the revised ones, and
  `scripts/s1b_support_needs_verify.py` fails 6 of 7 gates. It needs its own reload decision.
- **n8n S1 node 2 `|| 0` defect and the stale W1 period labels** remain.
- **Key changes before the next revision.** The RO4 table key
  (`lad24cd, financial_year`) and the S1b table key also need edition treatment before
  either source is next revised, or the revision can only overwrite.
- **`refresh-latest` is specific to the 2026-10-06 reload.** The editions table and the latest-edition rule are general, but `scripts/s1_editions.py refresh-latest` hardcodes the stale periods, the snapshot table and the row count (`STALE_PERIODS`, `BAK`, `n != 3256`), and gates 4, 5, 9 and 10 assume it. It must be generalised (new quarters write edition 1 first; gates parameterised) before the next S1 quarterly load.
- **The quarterly revision re-check is not automated.** Re-checking each loaded quarter for revision, and logging `revision_detected` in `source_check_log`, still has to be done by hand.
- **No insert-time trigger enforces `supersedes` = chain tip.** Only `load` checks it. A fork inserted another way would make `latest_edition` raise permanently.
- **Backup tables.** `la_statutory_homelessness_bak_20261006` is kept as a snapshot;
  edition gates 10 and 11 depend on it, so drop it only deliberately.

## Closed

| Record | What it settled |
| --- | --- |
| [2026-10-06 S1 back-series revision (former open item 1)](2026-10-06-s1-edition-history-design.md) | Closed 2026-10-06 with the edition work and [s1-edition-diffs-2026-10-06.md](s1-edition-diffs-2026-10-06.md). **The premise recorded in the 2026-08-16 record was wrong, and that record is left as written.** The "200 to 230 authorities differ" figure was measured against the older release-page files. Edition 1's A1 figures already matched the registry 'revised' files numerically: the only A1 differences are zero to NULL for authorities that did not submit (36 authority-quarters, 144 cells: 3, 8, 4, 7, 5, 7 and 2 authorities in 2023Q2 to 2024Q4, each across all four A1 measures); no A1 figure is a numeric revision. The 200 to 230 divergence was against the older release-page files. The genuine value revisions in edition 2 are the A3 `support_needs_total` (about 175 to 190 authorities per quarter); `households_in_ta` also changes zero to NULL (95 cells). The seven quarters reproduce from source (`reproduces_from_source` true, 0 cells differ). The 2025Q2 revision (MHCLG, 30 April 2026) was applied in place on 2026-10-05 (`pipeline_run_log` 135), an acknowledged departure from the additive rule; it is now edition 2 of 2025Q2 and the live layer matches it. |
| [2026-10-06 S1 stored zeros (former open item 3)](2026-10-06-s1-edition-history-design.md) | Closed 2026-10-06. 108 `households_in_ta` zeros were stored across the seven quarters: 95 were suppressed values, now NULL in edition 2 and the live layer, and 13 are published zeros, kept. The 129 stored zeros in the 2026-08-16 record and the earlier changelog is that record's own earlier figure and was not reproduced. |
| [2026-10-06-s1-edition-history-design.md](2026-10-06-s1-edition-history-design.md) | S1 revisions are stored as editions (`la_statutory_homelessness_editions`, append-only); the live table is the latest-edition layer. Seven back quarters loaded as edition 2, 2025Q2 revision represented as edition 2. Closes former open items 1 and 3; diffs in [s1-edition-diffs-2026-10-06.md](s1-edition-diffs-2026-10-06.md). |
| [2026-09-30-telford-no-housing-register.md](2026-09-30-telford-no-housing-register.md) | Telford has no housing register; its reported 0 is not applicable. Set to NULL, run 20 exported, map shows No data. |
| [2026-08-21-s14-brma-join-break.md](2026-08-21-s14-brma-join-break.md) | An accidental import rebuilt S14 and left a name-spelling mismatch and a consumed header row. 152 rate rows and 0 ampersand mappings verified live 2026-09-30. Gate 18 added. |
| [2026-08-21-derived-view-dropped-unnoticed.md](2026-08-21-derived-view-dropped-unnoticed.md) | A derived view dropped by the S20 rename and found only by a downstream crash. View present, 155 rows, one rate card date, verified live. Gates 19 to 21 added; two gate-19 branches (zero-row, unrunnable view) remain unexercised. |
| [2026-08-20-s3b-tenure-rebasing-error.md](2026-08-20-s3b-tenure-rebasing-error.md) | Census tenure wrong for the four 2023 unitaries. Cumberland verified at 125,424, the published figure. |
| [2026-08-20-s20-neutral-object-names.md](2026-08-20-s20-neutral-object-names.md) | S20 tables and views renamed so the counterparty is not disclosed. `commercial_rate_card` and `commercial_rate_area_mapping` are the neutral names and exist live. |
| [2026-08-20-s12-efs-misattribution.md](2026-08-20-s12-efs-misattribution.md) | EFS rows attributed to Hammersmith and Fulham belonged to Haringey. Verified: Haringey holds both rows (£40.6m, £84.0m), Hammersmith and Fulham none. |
| [2026-08-20-repo-scope.md](2026-08-20-repo-scope.md) | The repository is the reproducibility record, not business analysis; 27 files removed and history purged. Verified: no removed path is tracked. |
| [2026-08-20-nojekyll.md](2026-08-20-nojekyll.md) | Jekyll broke Pages publication on n8n `{{` syntax; `.nojekyll` added. Verified: file present, latest Pages builds succeeded (2026-09-30). |
| [2026-08-20-demand-map-assurance.md](2026-08-20-demand-map-assurance.md) | Map re-exported from corrected data (run 18): stale workflow backup, stale house-price file, source count, MARAC and care-leaver labelling. Canvas was never rendered for a visual check (no WebGL in the browser pane). |
| [2026-09-04-s6-reg02-hardcoded-snapshot-period.md](2026-09-04-s6-reg02-hardcoded-snapshot-period.md) | Reg_02's snapshot period was fixed in source, so each S6 refresh overwrote the previous snapshot in place. Caught by Check 9 on the first refresh; no row count, key or coverage test was sensitive to it. Period now derived from the edition. |
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

## The recurring defect, across ten of these records

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
was outstanding when it had been running for three weeks. A snapshot period
fixed at the date of the build that wrote it, silently destroying a quarter of
history on every refresh.

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

- `scripts/verify_source_registry.py` — 21 gates. Exit 0 clean, 2 known-red
  only, 1 stop.
- `scripts/push.py` — the only sanctioned push. Scan, verify, then push, in one
  place so the order cannot be forgotten. `--install-hook` gates a bare
  `git push` too.
- `docs/GENERATED_FIELDS.md` — which registry fields are generated and from
  where.
- `docs/KNOWN_RED.md` — currently empty, and why a stale entry is dangerous.
