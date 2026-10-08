# Rules for loading and publishing data

One page for the rules that apply to every source. Each rule says what to do and where the detail is. If a source needs an exception, it is written in that source's own doc and says why.

Status: drafted 2026-10-07 from the review. Rules 1 and 2 are new decisions, and rule 8 (period keys) was added 2026-10-08; the rest collect existing rules that were spread across `METHODOLOGY.md`, the decision notes and session notes. The loader standard is described under rule 5.

---

## 1. Blanks and zeros

A zero is a measured count. A blank is a count nobody gave us. They are never swapped.

1. **A published 0 is stored as 0**, with no flag.
2. **A figure the publisher withholds is stored as NULL**: suppressed (`..`, `-`, `*`, `[x]`, `[c]`, `c`, `k`, `z`, `x` and similar), not reported, or not applicable.
3. **What each marker means is read from the publisher's own notes and written in the source doc.** It is not assumed from another source. The same symbol can mean different things in different files.
4. **Loaders never turn a blank into 0.** No `or 0`, `|| 0`, `COALESCE(x, 0)` or `else 0` on a source value.
5. **Where a source gives different reasons for a blank, the reason is stored** next to the value (`value_flag` with `suppressed`, `missing`, `not_applicable`, as S1b does). Sources with a single marker document it instead.
6. **An authority the publication does not cover has no row.** It is absent, not zero (for example district councils in the care leaver data).
7. **A total built from parts is NULL unless every part is present.** The publisher's own published total is the headline figure. A sum of the known parts is never stored as a total.
8. **Calculations:** NULL in gives NULL out. Divide using `NULLIF(denominator, 0)`. A year-on-year change needs both years, otherwise NULL. A national total compared across years uses the same set of authorities in both years.
9. **Maps and reports:** NULL displays as "No data" or a dash. 0 displays as 0.
10. **Every load reports** how many cells were NULL, flagged, and 0, per column. A load where zeros appear in cells the previous edition held as blank, or the reverse, is listed in the preview for review and is accepted only by a stated acknowledgement. Genuine publisher revisions do occur (for example RO4 2024-25), so this is a check for a person to decide, not an automatic stop. The acknowledgement mechanism is built when each loader adopts the check.

Known breaches until corrected: S4 (care leavers) stores suppressed cells as 0 and builds totals from them; S22 (council taxbase) treats suppressed exemption classes as 0 in its total. S10 is unchecked. Detail: `DATA_DICTIONARY.md` (S1b `value_flag`), `docs/s1b_support_needs_source.md`, `docs/decisions/2026-10-06-s1-edition-history-design.md`.

## 2. Revisions are data

1. A new release **and** a revision of a figure already held are both stored as new rows (a new edition). Nothing is overwritten.
2. The live table is the latest-edition layer, refreshed from the editions table. The latest edition is the one no other edition supersedes.
3. A sent or published deliverable is never overwritten. A new version is written and what changed is stated.
4. Sources the registry marks `revises_back_series` must keep editions. Today only S1, S1b and RO4 do; the other revising sources are being brought across.

Detail: `METHODOLOGY.md` (Revision Handling), `QUARTERLY_REFRESH.md`.

## 3. Check the file is what it says

1. Before a file is loaded, its identity is read **from the file itself** (the cover sheet or notes tab): publisher, release, period, boundary vintage.
2. A label in code or a filename is not evidence. The loader records the stored label and the actual label and refuses a mismatch.

Why: a second release was once loaded and labelled as the third. See `docs/decisions/2026-10-07-ro4-edition-history.md`.

## 4. Geography

1. The canonical key is `lad24cd` (Barnsley E08000016, Sheffield E08000019). Publisher codes are resolved through `la_code_lookup` **before** the orphan check.
2. An unresolved code is **UNEXPLAINED** and a hard stop until it is explained against an authoritative source. It is never called harmless.
3. A release's stated boundary vintage predicts its codes. Publication date is only the fallback.
4. Financial-year quarters: 2025Q4 is January to March 2026.

Detail: `METHODOLOGY.md` (Boundary Data), `docs/decisions/2026-08-14-barnsley-sheffield-code-split.md`, `docs/geography_dimension.md`.

## 5. Loading

1. Loaders preview by default and write only with an explicit commit option. The one exception is the schema command `ddl`, which creates a missing editions table and its triggers when run.
2. Loads stop on unexpected input; they do not carry on with a warning.
3. Every source has a verify script that is run after the load.
4. The source register (`source_registry`) is the authority for source numbers, not the run log.

### The loader standard

Loaders are to meet one standard, built from shared parts in `scripts/`. The parts below exist and are tested; adoption by the loaders is under way and not finished.

- **Editions core** (`editions_core.py`): the append-only editions table, the supersedes chain, the latest-edition layer and the gates around them. A loader describes its table in a spec and supplies its own parser. S1, S1b and RO4 run on it today. Their commands are `status` (read-only health check), `load` (a revised or first edition of a period), `sync-new` (a newly published period) and `refresh-latest` (copy the latest edition into the live table).
- **Blank reader** (`blank_reader.py`): turns the publisher's markers into NULL or a number as rule 1 requires. No loader uses it yet; loaders are to adopt it in place of their own conversion code.
- **Shared checks** (`load_checks.py`): the per-column NULL, flagged and zero report, the check against the previous edition (rule 1.10), file identity (rule 3), geography resolution (rule 4), row counts, and the check that the live table equals the latest editions. Today only RO4 calls one of them (`check_latest_equals_live`); the per-column report and the previous-edition check are not yet called by any loader. Loaders are to adopt them.
- **Preview by default:** in S1, S1b and RO4 every command that writes is a dry run unless given `--commit`, with one exception: `ddl` (create the editions table and its triggers if absent) writes when run. `status` is read-only. `--simulate` runs everything and rolls back.
- **Conformance checker:** `python scripts/check_loaders.py` lists each loader as PASS or with the reasons it does not conform. 3 of 18 loaders conform today; the checker lists the rest. It reports only and does not yet gate a push. Its checks are heuristics and do not yet test use of the blank reader or the shared checks; the verify script remains the real test.

The other loaders are to be brought across one at a time; the checker's FAIL list is that worklist. A source that is never revised may declare `NO_EDITIONS = "<reason>"` instead of using the core, but is still to use the blank reader and shared checks. Related: `docs/decisions/2026-10-07-editions-quarterly-refresh.md`.

## 6. Publishing and privacy

1. This repository is public. Nothing private goes into it: no rate-card figures, no counterparty names, no personal data, no credentials.
2. Anything that lists tables, columns or schema is scanned for counterparty names before it is staged.
3. Pushes go through `python scripts/push.py` only.
4. Research-tier data (desk research, not publisher data) is held in separate tables, dated, and never mixed with source data.

## 7. Derived values and the map data

1. Source tables never store a rate. Rates live in views. `staging_la_signals` is the one documented exception and takes each definition from a view.
2. The map data is refreshed with `python scripts/refresh_map.py`, which runs Workflow 1 if a W1 input was loaded after the latest complete run (or no complete run exists), then exports the map data. `python scripts/refresh_map.py --check` says whether the map at git HEAD is behind the database. The published run should match the latest run; a gap means the map is out of date.

Workflow 1's SQL lives in `sql/w1/`. A change to the columns of `staging_la_signals` is made there and checked by `scripts/w1_contract_check.py`. Detail: `METHODOLOGY.md`.

## 8. Period keys

1. **Periods are stored as sortable keys:** `yyyymm` for months (`202604`), `yyyy-yy` for financial years (`2025-26`), `yyyyQn` for quarters (`2025Q4`).
2. **A display label is produced when needed and is never stored as the key.** A label such as `Apr-26` sorts as text by its first letter, so `max()` and `ORDER BY` give the wrong latest month (`Jul-26` sorts after `Aug-26`).
3. **A source whose periods are stored as labels is relabelled in one documented migration** that records the mapping both ways (label to key and key to label), so it can be checked and reversed.

S19 (PIP claimants) is the first case: its months were held as `Apr-26` and `Jul-26`. Detail: `docs/decisions/2026-10-08-s19-month-keys.md` (written with the S19 move onto the loader standard).
