# The source registry

<!-- repo-meta
status: active
last-reviewed: 2026-08-14
type: reference
consumed-by: scripts/generate_methodology.py, scripts/verify_source_registry.py
-->

Infrastructure, not a data source. The registry records what every source is,
how it is acquired, when it is next due, and whether anyone has checked. It
holds nothing about local authorities.

Three objects, defined in [`sql/source_registry.sql`](../sql/source_registry.sql):

| Object | What it is |
| --- | --- |
| `source_registry` | One row per registered source. The authority for how a source is acquired and when it is due. |
| `source_check_log` | One row per attempt to detect a new edition, including attempts that failed. |
| `vw_source_due` | The due list. Derived; stores nothing. |

## What is the authority for what

`docs/METHODOLOGY.md` is the source register. It decides which sources exist
and what number each carries. The registry does not get a vote on that, and
`scripts/verify_source_registry.py` fails if the two disagree.

`pipeline_run_log` is the authority for what actually ran. The registry has no
`last_success_at` column, deliberately — success is derived from the run log in
`vw_source_due`, so there is exactly one truth about what ran and it cannot
drift from a copy.

The registry is the authority for everything else: landing pages, credentials,
cadence, acquisition traps, caveats, and the tier that decides whether a
refresh can be automated.

## A null means the repository does not document it

Every value in `source_registry` is derived from this repository — METHODOLOGY,
the source documentation files, the node documentation files, the build
scripts, and the live primary keys of the target tables. Nothing is derived
from general knowledge of the publisher.

Where the repository does not document a field, the value is `NULL`. A visible
null is honest; an inferred value is not. The open list is
[SOURCE_REGISTRY_GAPS.md](SOURCE_REGISTRY_GAPS.md), and that report is the
work list for hardening the registry rather than a defect log.

Three exceptions are structural. `acquisition_method`, `cadence`,
`refresh_tier` and `status` are `NOT NULL` and cannot hold a null, so a source
whose acquisition mechanics are undocumented carries the cautious default —
`acquisition_method = 'manual'`, `refresh_tier = 'C'` — and says so in
`completeness_note`. Read those as "not established", not "established as
manual".

A null is not always a gap. S20 is commercial in confidence, and its nulls are
policy: the counterparty's name appears in its table names, so recording
`target_table`, `build_script_path` or `source_doc_path` would turn
`source_registry` itself into an artefact that discloses the counterparty
without ever naming it. `completeness_note` says which kind of null a row
carries.

## Column semantics

### Identity

| Column | Meaning |
| --- | --- |
| `source_code` | The register number from METHODOLOGY, including letters: `3b`, `8b`, `9a`, `9b`. Primary key. |
| `source_name` | The register's own name for the source. |
| `metrics` | What the source actually gives you, one element per metric. |
| `publisher` | Recorded even where every other field is null — the map badge is a publisher count. `Withheld` for confidential sources. |
| `series_name` | The publisher's title for the dataset, table or measure. |

`metrics` was backfilled from the `Metric(s)` column of the hand-written
register, splitting each cell only at parenthesis depth zero and only on
semicolons where the cell uses them at top level. The split is proved
reversible before it is stored: a cell that does not rejoin to its original
text exactly is kept whole rather than silently reshaped. All 24 round-trip.

### Acquisition

| Column | Meaning |
| --- | --- |
| `landing_page_url` | The stable page. Never a file URL — file URLs move. |
| `acquisition_method` | `api`, `landing_page`, `manual` or `derived`. |
| `api_endpoint` | The API root, where acquisition is an API. |
| `auth_required` / `auth_env_var` | Whether a credential is needed and the environment variable that carries it. **The variable name only. Never a value.** |
| `known_gotchas` | Recorded verbatim from the documentation. Sources whose URL changes every edition say so here. |

### Cadence

`next_due_at` is derived from `next_expected_at` where set, otherwise from the
last successful run plus `cadence_months` plus `expected_lag_days`.
`publication_window` carries the publisher's stated window verbatim — "late
January", "autumn", "November, revised the following January". A window is not
a date, so it is never converted into `next_expected_at`.

### Target

`target_table` is the principal table the source loads, and `natural_key` is
that table's live primary key, read from `pg_constraint` rather than
transcribed from prose. Sources that write several tables list the rest in
`caveats` or `completeness_note`: `target_table` is not a complete table list.

### Caveats

`caveats` carries any completeness or interpretation caveat the source
documentation records. Caveats travel with the data. An empty `caveats` array
is a risk signal, not a tidy row.

## The tiers

Assigned by acquisition mechanics, not by importance. S14 LHA rates are
central to the analysis and sit in tier C, because the edition URL is replaced
by hand.

| Tier | Mechanics | What can be automated |
| --- | --- | --- |
| **A** | Stable machine-readable endpoint, stable schema | Unattended ingestion is safe |
| **B** | Landing page plus file download; the file URL changes per edition but the layout is stable | Detection is safe, ingestion is gated |
| **C** | Manual only, unstable schema, third-party or confidential | Nothing. `vw_source_due` reports these as `manual_only` |

Where the documentation does not make the mechanics clear, the source is tier
C. Defaulting to the most cautious tier is correct: the cost of a wrong C is a
manual refresh that could have been automated, and the cost of a wrong A is an
unattended job loading a file it does not understand.

## `due_status`

| Value | Condition |
| --- | --- |
| `manual_only` | `refresh_tier = 'C'` — never chased automatically |
| `never_loaded` | No successful run resolves to this source |
| `not_due` | `next_due_at` is in the future and the source was checked within 45 days; or `cadence = 'static'` with no expected date |
| `due` | `next_due_at` is today or up to 30 days past |
| `overdue` | `next_due_at` is more than 30 days past |
| `check_stale` | Otherwise eligible, but `last_check_at` is null or older than 45 days |

`check_stale` also catches the case where no due date can be derived at all —
`cadence_months` and `next_expected_at` both null. An underivable due date is
not evidence that nothing is owed, so it never falls through to `not_due`.

`check_stale` does not outrank `due` or `overdue`. A source that is known to be
due is due, whether or not the check job has been near it.

## How a run resolves to a source

`vw_source_due` joins the registry to `pipeline_run_log` in three steps:

1. On `pipeline_run_log.source_code`, where populated.
2. Falling back to `source_number`, but **only** for numbers that carry no
   populated `source_code` on any row. Once a number has been positively
   resolved on at least one row, its remaining unresolved rows are known to be
   contested and are not attributed by number.
3. Neither resolves: the source reports `never_loaded`. Another source's run is
   never borrowed to fill the gap.

Step 2's restriction is not theoretical. Run 60 is logged as
`source_number = '19'` with `agent_name = 'Source 19 - Land Registry UK HPI'` —
an S15 build, logged under 19 before the July 2026 renumbering. Its number says
one thing and its agent name says another, so it keeps a null `source_code` and
is excluded from attribution rather than being read as an S19 run.

`pipeline_run_log.source_code` has **no foreign key** to `source_registry`.
Historical rows may name sources that are later deprecated, and the log is an
immutable audit record: a constraint that could block an insert, or that
invites rewriting history to satisfy it, is the wrong tool for an audit table.

Successful statuses are `success` and `complete` — both appear in the live log
and both mean success. Gate 6b fails if a status outside that vocabulary
appears, so a new value cannot silently turn a loaded source into
`never_loaded`.

History is not normalised. The two `complete` rows are an accurate record of
what those builds wrote and they stand. New writes are constrained to
`success` by `pipeline_run_log_status_new_writes_chk`, added `NOT VALID` so
existing rows are never re-checked while inserts and updates are. That
codifies the convention already in force rather than narrowing it: no failure
has ever been logged, because a build that fails rolls its transaction back
and exits non-zero without writing a row.

## Checking for new editions

`scripts/check_sources.py` fetches each source's landing page, finds the
newest matching link, fingerprints it, and writes one `source_check_log` row
per source per run — including for checks that failed.

Two things it deliberately does not do. It never picks `links[0]` as the
newest: NHS England and NHS Digital both list oldest first, so document order
is not release order, and the newest is chosen by parsed edition instead. And
it excludes periods after the current month, because NHS Digital publishes
scheduled publication pages ahead of the data and a page for next month is a
calendar entry, not an edition.

A check is only recorded as `no_change` when something was actually compared —
a fingerprint against the stored one, or a detected edition against
`latest_period_loaded`. Where neither comparison is possible the outcome is
`check_failed` with the reason, because reaching a page proves the page
exists, not that the data behind it is the data already loaded.

### The principle

**Due status is decided by comparing the source against the database, never
against the last observation.** The fingerprint is corroborating evidence, not
the test. Anything that short-circuits on prior state can go green while being
wrong, and a monitoring layer that reports `no_change` while sources sit
unloaded is worse than no monitoring, because it manufactures confidence.

Two bugs in this file have been that same mistake wearing different clothes.
The fingerprint short-circuit was one. The other was S18, where the detected
period is the publication date from the URL slug while `latest_period_loaded`
is the reference period — the 22 July 2026 edition contains data to June 2026
— so comparing them as though they were the same kind of thing reported a new
edition forever. Both are now settled against the database: the period against
the loaded period, or the edition slug against the target table's own `source`
column.

**The load gap is asked before the fingerprint, and the order matters.** They
answer different questions: the fingerprint says whether the publisher has
changed anything since the last look, the gap says whether what is published
is ahead of what is loaded. Checking the fingerprint first — which this script
did until 2026-08-14 — means that once an edition has been detected the
fingerprint matches on every later check, so a genuinely pending edition
reports `no_change` forever and drops off the due list without ever being
loaded. That bug was masking three pending editions on its second run.

`latest_period_loaded` is derived from the target table's own period key
rather than from documentation. It is the one field where the database is the
authority: it records what is actually loaded, not what a source document said
was loaded when it was written.

## Revisions

`vw_source_due` asks whether a newer period has appeared. It cannot ask
whether an already-loaded period has been republished, and for a revising
source that second question is the one that silently corrupts analysis — the
row count stays complete, every gate still passes, and the numbers are simply
no longer what the publisher says they are.

| Column | Meaning |
| --- | --- |
| `revises_back_series` | True where the publisher is documented to revise already-published periods. Left NULL where not established: `false` asserts more than the documentation supports. |
| `revision_note` | Where the publisher announces revisions, and what the most recent one covered. |

`source_check_log.outcome` carries `revision_detected` for this. A republished
period is neither a new edition nor no change, and collapsing it into either
loses the only signal that matters.

Detection uses the per-period `source` column the target table already
records, where it has one. That column says which file each loaded period
actually came from, so a republished file is visible from the link list alone
— the `-Revised` suffix on the DRD filenames is the whole signal, and nothing
is downloaded. Six sources are currently flagged as revising: S6, S8b, S9a,
S15, S18 and S22.

S18 is immune by accident: every edition republishes the full back series, so
loading the latest edition finalises prior months automatically. S9a is not —
monthly files, revised in place, no signal in the row count.

## How a new source build writes its own row

A build is expected to register itself, in the same transaction that loads its
data. Anything else leaves the registry describing the pipeline as it was.

1. **Take the number from METHODOLOGY**, not from `pipeline_run_log`. Check the
   log only as a contradiction test — if they disagree, that disagreement is
   the finding, and neither number is used until it is explained.
2. **Insert the registry row** with `status = 'pending_build'` when the source
   is registered but not yet built, and `'active'` once it loads.
3. **Write what is documented, and null what is not.** If a field is being
   filled from memory of how the publisher behaves rather than from a file in
   this repository, it belongs in the gap report, not the registry.
4. **Record `auth_env_var`, never a credential.**
5. **Set the tier from the mechanics.** If the build hardcodes an edition URL,
   that is tier C however good the source is.
6. **Set `confidential`, `publish_github` and `publish_map` deliberately.**
   `confidential = true` requires both publish flags false, and gate 7 enforces
   it.
7. **Log the run** with `source_code` populated, so the due view resolves the
   source by code rather than by the number fallback.

Upserts use `COALESCE(EXCLUDED.col, source_registry.col)`, so a re-run never
overwrites a non-null value with a null and hand-edited fields survive. Gate 9
proves this by running the backfill twice and diffing every field.

## Regenerating METHODOLOGY

`scripts/generate_methodology.py` writes the source-inventory block of
`docs/METHODOLOGY.md` between `<!-- BEGIN GENERATED SOURCE INVENTORY -->` and
`<!-- END GENERATED SOURCE INVENTORY -->`. It emits only
`publish_github = true` rows, renders nulls as an em dash, is dry-run by
default, and asserts that nothing outside the sentinel block changes. It does
not touch `index.html` or the map badge.

The generated block keeps the register's column order — `S#`, `Source`,
`Metric(s)`, `Publisher`, `Frequency` — because `scripts/register_lib.py`
parses those positionally and `scripts/sync_readme_sources.py` builds the
README source table from them. Reordering the columns would break README
generation silently.

As of 2026-08-14 the sentinels are **not yet placed**, and the write has not
been applied. The `Metric(s)` blocker is closed: `metrics` now carries it and
nothing is dropped. Run with `--init-sentinels` to review the diff. It reports
every changed cell, triaged, and currently stands at:

| Class | Count |
| --- | ---: |
| LOSS — the register said more than the registry holds | 0 |
| enrichment — register text retained, detail added | 5 |
| punctuation only — content identical | 3 |

## Precedent — widening a CHECK constraint by drop-and-add

`sql/source_registry.sql` is additive-only: every `CREATE` is `IF NOT EXISTS`
and every `ALTER` is guarded. Adding `revision_detected` to
`source_check_log.outcome` broke that rule, because Postgres has no `ALTER`
that widens a `CHECK`. It was dropped and re-added.

This is a **bounded exception, not a precedent for looser DDL**. It is only
available when all three conditions hold:

1. **Strictly widening.** The new vocabulary is a superset of the old, so no
   existing row can violate the replacement.
2. **In the same transaction.** Drop and add are in one `DO` block inside the
   script's transaction, so there is no window in which the table is
   unconstrained.
3. **Asserted afterwards.** Gate 4 reads `pg_get_constraintdef` and fails
   unless every expected token is present, so a widening that reached the code
   but not the database is caught.

Anything that narrows a vocabulary, changes a column type, or would let an
existing row fail the new constraint does not qualify and needs a migration
with a stated plan for the rows that fail.

## Verification

`scripts/verify_source_registry.py` runs twelve hard gates: row count against
METHODOLOGY, two-way agreement between register and registry, empty strings in
`NOT NULL` columns, controlled vocabularies and the CHECK constraints that
enforce them, one view row per registry row, run attribution (asserted by run
id, never by timestamp — runs 57 and 58 share one), the run-log status
vocabulary, confidential-implies-unpublished, `superseded_by` integrity,
idempotency, that no withheld source reaches generated output, and that every
script in `scripts/` resolves `.env` from the published checkout.

That last gate exists because the `.env` defect appeared twice — once in
`register_lib.py`, then in six scripts at once. Two incidents is a class, and
the next script written would have reintroduced it. `scripts/_db.py` is the
reference implementation, so the gate has something concrete to assert
against. It was proved by injecting the defect: the gate failed and the suite
exited non-zero.

Any failure aborts. Nothing is published from a red table.
