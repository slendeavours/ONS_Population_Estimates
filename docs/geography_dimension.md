# Geography Dimension — `la_geography` and `la_succession`

Two tables added ahead of Local Government Reorganisation (LGR) so the pipeline can survive GSS code churn without rewriting every source workflow. Created in the S18 backfill run (11 July 2026); `la_code_lookup` remains untouched and authoritative for the existing one-to-one reconciliations that every current source workflow depends on.

## Why now

- **East and West Surrey unitaries vest 1 April 2027.**
- **Most remaining new unitaries in the current LGR wave vest 1 April 2028.**
- Some of those changes will be **one-to-many splits** (a district's area divided between new unitaries). `la_code_lookup` assumes one-to-one (`old_code → new_code`) and cannot represent a split; `la_succession` can, with population apportionment.
- The April 2025 Barnsley/Sheffield boundary change (SI 1328/2024) has already shown ONS re-coding whole back series onto new codes — sources will increasingly arrive on codes that postdate LAD24.

## `la_geography`

One row per GSS code per validity window. `boundary_set` names the boundary vintage the code belongs to (currently `'LAD24'`); `valid_from` is the code's official operational start date; `valid_to IS NULL` means the code is still live.

**Seeding state (11 July 2026): seeded — 296 rows**, one per LAD24 English LA, names joined from `la_boundaries`. Start and termination dates come from the ONS **Code History Database (June 2026)** (`ChangeHistory.csv`, Open Geography Portal) — no dates were assumed or invented. 294 rows are live; 2 rows carry `valid_to = 2025-03-31` because their codes were terminated by the Barnsley/Sheffield order (E08000016, E08000019) even though they remain the pipeline's canonical LAD24 keys.

## `la_succession`

One row per (predecessor, successor, change date). `apportionment` = share of the predecessor's population assigned to that successor; 1.0 for whole-area transfers; rows for a genuine split sum to ~1.0 across successors.

**Seeding state (11 July 2026): 33 rows**, migrated verbatim from `la_code_lookup`'s non-current rows (7 `merger`, 26 `new_unitary`, 2019–2023 reorganisations), all `apportionment = 1.0` since all were whole-area transfers.

**Known change not yet entered**: the April 2025 Barnsley/Sheffield recode (E08000016 → E08000038 + a small transfer into E08000039; E08000019 → E08000039). The Barnsley split needs an authoritative apportionment figure before insertion — deliberately deferred rather than defaulted to 1.0, which would double-count. See handover items in the S18 run report.

## Rules

1. **No 2027/2028 codes enter either table until ONS publishes them.** No provisional or guessed codes, ever. Watch for East/West Surrey GSS codes ahead of 1 April 2027 vesting — those become the first forward-looking `la_succession` entries.
2. Apportionment values must come from an authoritative source (ONS/MHCLG population splits), never estimated locally.
3. `la_code_lookup` is not modified. When the 2027 wave lands, source workflows migrate to resolving codes via `la_geography`/`la_succession`; until then the lookup remains the operational reconciliation path.
4. Refresh path for both tables: re-download the current Code History Database from the Open Geography Portal (verify the download URL on the portal at run time — item URLs change per edition, like every ONS product).
