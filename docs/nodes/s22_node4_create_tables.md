# Node 4 — Create Tables

> **Superseded 2026-10-09.** The old S22 scripts this note describes (`s22_ctb_discover.py`, `s22_ctb_empties_build.py`, `s22_run.py`, `s22_verify.py`) were loaded once, on 2026-08-13 (run-log id 83; the verify step was run at least twice and its duplicate run-log row, id 84, was deleted), and are retired to `scripts/historical/`, where they stop with a RETIRED message. S22 is now loaded by `scripts/s22_ctb_editions.py`, which finds both files itself, reads each file's identity from its own cover and keeps each file's statement about each year as an edition (`la_council_taxbase_empties_editions`, `la_ctb_exemption_classes_editions`, `la_vacant_dwellings_615_editions`). The source note is `docs/s22_source_structure.md`. The text below is left as written and describes the old method.

## Type

Postgres — Execute Query. `scripts/s22_ctb_empties_build.py`, constant `DDL`, executed by `load_all`.

## Purpose

Create the four S22 tables and their indexes if they do not already exist. Nothing is dropped.

## Credential

Postgres `exempt_pipeline`.

## Query / Code / URL (full content)

```sql
CREATE TABLE IF NOT EXISTS la_council_taxbase_empties (
    lad24cd                     VARCHAR(9)  NOT NULL,
    la_name                     VARCHAR(100),
    taxbase_year                INTEGER     NOT NULL,
    total_dwellings             INTEGER,
    empty_under_6_months        INTEGER,
    empty_6_months_plus         INTEGER,
    empty_total                 INTEGER,
    empty_homes_premium_count   INTEGER,
    second_homes                INTEGER,
    unoccupied_exemptions_total INTEGER,
    source_publication          TEXT,
    loaded_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, taxbase_year)
);

CREATE TABLE IF NOT EXISTS la_ctb_exemption_classes (
    lad24cd               VARCHAR(9)  NOT NULL,
    taxbase_year          INTEGER     NOT NULL,
    exemption_class       VARCHAR(2)  NOT NULL,
    exemption_description TEXT,
    dwellings             INTEGER,
    loaded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, taxbase_year, exemption_class)
);

CREATE TABLE IF NOT EXISTS la_vacant_dwellings_615 (
    published_la_code          VARCHAR(9)  NOT NULL,
    published_la_name          VARCHAR(100),
    year                       INTEGER     NOT NULL,
    vacant_dwellings           INTEGER,
    long_term_vacant_dwellings INTEGER,
    lad24cd                    VARCHAR(9),
    mapping_status             VARCHAR(20) NOT NULL,
    loaded_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (published_la_code, year)
);

CREATE TABLE IF NOT EXISTS ctb_series_breaks (
    break_id        SERIAL PRIMARY KEY,
    first_period    DATE NOT NULL,
    last_period     DATE,
    affected_column TEXT NOT NULL,
    dimension       TEXT,
    description     TEXT,
    comparability   TEXT,
    source_url      TEXT
);

CREATE INDEX IF NOT EXISTS ix_ctb_empties_year
    ON la_council_taxbase_empties (taxbase_year);
CREATE INDEX IF NOT EXISTS ix_ctb_615_lad24cd
    ON la_vacant_dwellings_615 (lad24cd);
```

## Query Parameters

None. Pure DDL with no interpolated values.

## Behaviour

`IF NOT EXISTS` throughout, so re-running is a no-op on an existing schema. Executed inside the same transaction as the load, so a hard gate failure rolls the DDL back with the data and leaves the database exactly as it was.

`la_vacant_dwellings_615.lad24cd` is nullable by design — abolished districts have no current successor code that can be recorded without aggregating. `ctb_series_breaks` follows the shape of `asylum_series_breaks`, with `affected_column` naming the column each break makes non-comparable.

## Connection

- Input: Node 2 (Extract Council Taxbase), Node 3 (Extract Table 615)
- Output: Node 5 (Upsert Council Taxbase and Exemption Classes)

## Verified Output

2026-08-13. Four tables and two indexes created in `exempt_pipeline`. Re-run on the second (idempotency) load was a no-op.
