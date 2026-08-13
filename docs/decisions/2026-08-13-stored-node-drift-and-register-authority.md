# 2026-08-13 — Stored node drift, and which artefact is the source register

## Context

The S22 Council Taxbase build needed to add five columns to `staging_la_signals` and revise W1 node 5. Reading the stored node out of `n8ndb` to revise it exposed two defects that had nothing to do with S22, and a third turned up while assigning the source number.

## What was found

**1. The stored n8n node was two builds behind the database.**

`staging_la_signals` carried `drd_bed_days_lost`, `drd_pct_delayed_1plus_days`, `crfd_days` (S9, run 10) and `pip_total_claimants`, `pip_enhanced_daily_living`, `pip_rate_per_1000` (S19, run 11). None of the six appeared in W1 node 5 as stored. Both integrations had been applied by direct SQL against the database and never written back to the workflow, a fact recorded at the time in `docs/nodes/s19_w1_node5_pip_patch.md` as "n8n API was not accessible for direct Node 5 update" and then not followed up.

The consequence is not cosmetic. The next genuine run of Workflow 1 would have produced a `staging_la_signals` row set with those six columns null, and the export would have carried the nulls to the map. The data would not have been wrong so much as quietly absent.

**2. The `staging_runs` sequence trailed the data for the same reason.**

Runs 10 and 11 exist in `staging_la_signals` with no matching `staging_runs` row, because they were created by direct SQL that skipped the Create Run node. `staging_runs_run_id_seq` was therefore at 10 while the signals table already held run 11. The next `nextval()` would have returned 10 and written a second, unrelated set of signals under an existing run id.

**3. `pipeline_run_log` is not a reliable source register.**

The S22 build prompt directed source-number assignment at `pipeline_run_log`. That log did not hold S15 at all — Land Registry HPI was built on 2026-07-14 and never logged — and held S9a, S9b and S8b under keys (`s9a`, `s9b`, `8`) that did not match their register entries. Read literally, 9, 15 and 16 all appeared free when only 16 was. This was the second near-collision in source numbering; the S15 renumbering in July was the first.

**4. `docs/METHODOLOGY.md` had two factual errors of its own.**

It listed S19 as a standalone source not wired into Workflow 1, which was false from run 11 onward. It also recorded the boundary dataset as "Local Authority Districts (December 2024) Boundaries UK BUC" in two places, where both the `la_boundaries.source_date` column (`2024-05-01`, all 296 rows) and the S7 run log note ("LA boundaries loaded — May 2024 BGC — England only") say May 2024 BGC.

## Root cause

One cause behind the first three: **work applied to the database without being applied to the thing that regenerates the database.** A direct SQL patch is the fastest way to get a column populated and the easiest way to leave the pipeline unable to reproduce it. Nothing in the process required the write-back, so it did not happen, twice.

The fourth is ordinary documentation drift, but it matters here because METHODOLOGY is now being made the register. A register that is wrong about which sources are wired is not yet trustworthy; the correction is part of promoting it.

Worth noting which artefact was right about what. The run log held the correct boundary vintage that METHODOLOGY got wrong. `docs/README.md` had S19's wiring status right throughout while METHODOLOGY had it wrong. No single artefact was reliable on its own.

## Decision

Four standing rules, recorded in `docs/METHODOLOGY.md`:

1. **Any direct SQL against `staging_la_signals` updates W1 node 5 in `n8ndb` in the same session, or it is not applied.** The n8n REST API needs an interactive login, but the node is readable and writable in `n8ndb.workflow_entity.nodes`, which is where n8n reads it at execution time. Inaccessibility of the API is not a reason to skip the write-back.
2. **Anything that creates a `staging_runs` row outside the workflow uses the Create Run node's query**, so the sequence stays ahead of the data. `scripts/s22_w1_wire.py` now advances the sequence past the highest run id in either table before creating a run, which repairs the existing gap but does not remove the rule.
3. **Geography resolves through `la_code_lookup` during extraction, before the orphan gate runs.** Assume any source published after 1 April 2025 uses the recoded Barnsley and Sheffield codes E08000038 and E08000039, because `la_boundaries` is May 2024 and carries E08000016 and E08000019. This pair has now appeared in S9b, S18, S21 and S22. Only `change_type = 'recode'` resolves; `new_unitary` and `merger` are abolitions and stay unmapped.
4. **`docs/METHODOLOGY.md` is the source register. `pipeline_run_log` is an execution record.** A build takes its source number from the register and checks the log only as a contradiction test. If they disagree, the disagreement is the finding and no number is used until it is explained.

## What was changed

- W1 node 5 revised in `n8ndb` to carry the S9, S19 and S22 columns. Previous node JSON backed up to `build_reports/s22_w1_node5_backup.json` on the pipeline host; the full revised query is at `docs/s22_w1_node5_revised.md`.
- `staging_runs_run_id_seq` advanced from 10 to 11.
- `pipeline_run_log` backfilled and normalised: S15 inserted from `la_house_prices` load provenance; `s9a` → `9a`, `s9b` → `9b`, and the S8b row moved from `8` to `8b`. Each amended row carries a note recording the backfill; no run was altered.
- `docs/METHODOLOGY.md`: S19 corrected to wired, boundary vintage corrected to May 2024 BGC in both places, register authority restated, four standing rules added.

## What was not changed

The S9 and S19 columns were left exactly as they are. They are correct — the PIP columns were verified against `la_pip_claimants` at Apr-26 for Birmingham, Kingston upon Hull and Kensington and Chelsea and match to the unit. The defect was that the workflow could not have reproduced them, not that they were wrong.
