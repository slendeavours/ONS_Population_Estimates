# Node 6 — Create Tables and View

## Type
DDL execution

## Credential
`exempt_pipeline` as `PG_USER` from `.env`.

## Purpose
Create four tables, one view and the series-breaks reference table. Idempotent
via `CREATE TABLE IF NOT EXISTS` and `CREATE OR REPLACE VIEW`.

## Query
```sql
CREATE TABLE IF NOT EXISTS la_asylum_support (
    period_ending       DATE        NOT NULL,
    lad24cd             TEXT        NOT NULL REFERENCES la_boundaries(lad24cd),
    published_la_name   TEXT        NOT NULL,
    support_type        TEXT        NOT NULL,
    accommodation_type  TEXT        NOT NULL,
    people              INTEGER     NOT NULL,
    source_marker       TEXT        NULL,
    source_edition      TEXT        NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, lad24cd, support_type, accommodation_type)
);

CREATE TABLE IF NOT EXISTS la_asylum_support_unallocated (
    period_ending       DATE        NOT NULL,
    support_type        TEXT        NOT NULL,
    accommodation_type  TEXT        NOT NULL,
    people              INTEGER     NOT NULL,
    na_reason           TEXT        NOT NULL,
    source_edition      TEXT        NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, support_type, accommodation_type, na_reason)
);

CREATE TABLE IF NOT EXISTS asylum_support_non_england (
    period_ending       DATE        NOT NULL,
    lad_code            TEXT        NOT NULL,
    country             TEXT        NOT NULL,
    published_la_name   TEXT        NOT NULL,
    support_type        TEXT        NOT NULL,
    accommodation_type  TEXT        NOT NULL,
    people              INTEGER     NOT NULL,
    source_edition      TEXT        NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, lad_code, support_type, accommodation_type)
);

CREATE TABLE IF NOT EXISTS la_immigration_groups (
    period_ending             DATE         NOT NULL,
    lad24cd                   TEXT         NOT NULL REFERENCES la_boundaries(lad24cd),
    published_la_name         TEXT         NOT NULL,
    pathway                   TEXT         NOT NULL,
    sub_pathway               TEXT         NOT NULL,
    people                    INTEGER      NULL,
    suppressed                BOOLEAN      NOT NULL DEFAULT FALSE,
    source_marker             TEXT         NULL,
    population                INTEGER      NULL,
    percentage_of_population  NUMERIC(8,4) NULL,
    source_edition            TEXT         NOT NULL,
    loaded_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, lad24cd, pathway, sub_pathway)
);

CREATE TABLE IF NOT EXISTS asylum_series_breaks (
    break_id        SERIAL PRIMARY KEY,
    first_period    DATE NOT NULL,
    last_period     DATE NULL,
    support_type    TEXT NULL,
    dimension       TEXT NOT NULL,
    description     TEXT NOT NULL,
    comparability   TEXT NOT NULL
);
```

Indexes on `(lad24cd)` and `(period_ending)` for both LA-keyed tables, plus a
composite `(period_ending, lad24cd)` on `la_asylum_support`.

## The view
```sql
CREATE OR REPLACE VIEW vw_la_asylum_support_totals AS
SELECT s.period_ending, s.lad24cd, b.lad24nm AS la_name,
    SUM(s.people) AS total_supported,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Dispersal Accommodation')           AS dispersal,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Initial Accommodation')             AS initial_accommodation,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Contingency Accommodation - Hotel') AS contingency_hotel,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Contingency Accommodation - Other') AS contingency_other,
    SUM(s.people) FILTER (WHERE s.accommodation_type LIKE 'Contingency Accommodation%')     AS contingency_all,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Other Accommodation')               AS other_accommodation,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Subsistence Only')                  AS subsistence_only,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'not_stated')                        AS accommodation_not_stated,
    SUM(s.people) FILTER (WHERE s.support_type = 'Section 4')  AS section_4,
    SUM(s.people) FILTER (WHERE s.support_type = 'Section 95') AS section_95,
    SUM(s.people) FILTER (WHERE s.support_type = 'Section 98') AS section_98,
    MAX(s.source_edition) AS source_edition
FROM la_asylum_support s
JOIN la_boundaries b ON b.lad24cd = s.lad24cd
GROUP BY s.period_ending, s.lad24cd, b.lad24nm;
```

## Design notes

**No `suppressed` column on `la_asylum_support`.** All 28,439 source `People`
values are numeric — no markers, no blanks, minimum 1. Adding a suppression flag
that is always `FALSE` would imply a distinction the source does not make.
`la_immigration_groups` does carry one, because Reg_02 genuinely suppresses.
The asymmetry is deliberate.

**No `region` column.** Five LAD codes are assigned to more than one UK region
across the window. Storing an unreliable field invites its use.

**`na_reason` is in the unallocated primary key.** Three distinct reasons occur,
and different reasons are different facts, not duplicates of one another.

**No `COALESCE` through `la_code_lookup` in the view.** The prompt specified it,
and this build deliberately departs. Node 3 resolves every code to a live
LAD24CD before insert and the foreign key enforces it, so a view-level
`COALESCE` would be dead code that implies unresolved codes can reach the table.
The two existing LA views, `vw_drd_discharge_delays_lad` and
`v_la_rate_triangulation`, join `la_boundaries` directly; this matches them.

**`accommodation_not_stated` is required, not optional.** Without it the named
accommodation columns fall short of `total_supported` for the 20 periods where
Section 98 carries no accommodation type. Check 12 enforces the identity.

## Behaviour
- Fully idempotent. Re-running creates nothing and drops nothing.
- Foreign keys to `la_boundaries` on both LA-keyed tables.
  `asylum_support_non_england` deliberately has none — those codes are not in
  `la_boundaries` by design.

## Connection
Input: none. Output: schema for Node 7.

## Verified Output
- 5 tables and 1 view created
- `vw_la_asylum_support_totals`: 7,868 rows
- Verified 2026-07-25 (initial build)
