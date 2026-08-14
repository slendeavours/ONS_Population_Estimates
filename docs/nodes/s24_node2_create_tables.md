# S24 Node 2: Create the Three Entity Tables

- **Type:** Postgres DDL (additive only)
- **Purpose:** Create the register snapshot, judgements and enforcement notice tables. Entity-level throughout — no geography column exists on any of them, deliberately.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`.

## Query

```sql
CREATE TABLE IF NOT EXISTS rsh_registered_providers (
    snapshot_date       date        NOT NULL,
    registration_number text        NOT NULL,
    organisation_name   text        NOT NULL,
    registration_date   date,
    designation         text,
    corporate_form      text,
    notes               text,
    source_url          text        NOT NULL,
    source_file         text        NOT NULL,
    release_page_url    text        NOT NULL,
    loaded_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_date, registration_number)
);

CREATE TABLE IF NOT EXISTS rsh_regulatory_judgements (
    registration_number     text        NOT NULL,
    publication_date        date        NOT NULL,
    landlord_name           text        NOT NULL,
    landlord_type           text,
    status                  text,
    consumer_grade          text,
    consumer_grade_change   text,
    consumer_grade_date     date,
    governance_grade        text,
    governance_grade_change text,
    governance_grade_date   date,
    viability_grade         text,
    viability_grade_change  text,
    viability_grade_date    date,
    rent_grade              text,
    rent_grade_change       text,
    rent_grade_date         date,
    publication_type        text,
    engagement_process      text,
    name_or_code_change     text,
    other_landlords         text,
    edition_date            date        NOT NULL,
    source_url              text        NOT NULL,
    source_file             text        NOT NULL,
    release_page_url        text        NOT NULL,
    loaded_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (registration_number, publication_date)
);

CREATE TABLE IF NOT EXISTS rsh_enforcement_notices (
    registration_number  text        NOT NULL,
    publication_date     date        NOT NULL,
    provider_name        text        NOT NULL,
    status               text,
    publication_type     text,
    route                text,
    explanation          text,
    notice_date          date,
    name_or_code_change  text,
    other_providers      text,
    edition_date         date        NOT NULL,
    source_url           text        NOT NULL,
    source_file          text        NOT NULL,
    release_page_url     text        NOT NULL,
    loaded_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (registration_number, publication_date)
);
```

Guarded index:

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = 'rsh_registered_providers_regnum_idx') THEN
        CREATE INDEX rsh_registered_providers_regnum_idx
            ON rsh_registered_providers (registration_number, snapshot_date);
    END IF;
END $$;
```

## Design notes

**No geography column, on purpose.** RSH publishes no provider addresses, so
there is nothing to apportion to a local authority. The absence is not an
oversight to be filled in later — it is the correct representation of what the
publisher publishes, and gate 2 of the verification suite fails if a geography
column appears or if any S24 column reaches `staging_la_signals`.

**`snapshot_date` leads the register's primary key** so each month is stored
alongside the last rather than replacing it. The register page carries only
the current month and there is no archive, so this table is the only record of
what changed. A current-state table would have destroyed the evidence on every
load.

**Judgements key on `(registration_number, publication_date)`**, not on the
registration number alone. 308 judgements cover 305 providers: three have two,
including one provider whose registration code appears under two different
landlord names. 308 of 308 are distinct on the composite key.

**The index on `(registration_number, snapshot_date)`** serves the
change-detection query, which walks one provider across snapshots.

## Behaviour

- **Conflict handling:** `CREATE TABLE IF NOT EXISTS`; the index guarded.
- **Re-run safety:** Idempotent.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

- Three tables created; primary keys as above.
- 1 index present.
- 0 geography columns across all three, asserted by gate 2.
- Verified 2026-08-14.
