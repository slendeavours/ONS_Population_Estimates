# S23 Node 3: Create `rsh_rp_stock_by_la`

- **Type:** Postgres DDL (additive only)
- **Purpose:** Create the provider-by-authority stock table, with the component-sum identity enforced rather than trusted.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`.

## Query

```sql
CREATE TABLE IF NOT EXISTS rsh_rp_stock_by_la (
    stock_date                          date        NOT NULL,
    rp_code                             text        NOT NULL,
    lad24cd                             varchar(9)  NOT NULL,
    rp_name                             text        NOT NULL,
    provider_type                       text        NOT NULL,
    rp_size_band                        text,
    survey_status                       text,
    publisher_la_code                   varchar(9)  NOT NULL,
    la_name                             text        NOT NULL,
    total_social_stock                  integer     NOT NULL,
    general_needs_self_contained        integer     NOT NULL,
    general_needs_bedspaces             integer     NOT NULL,
    supported_housing_and_older_people  integer     NOT NULL,
    low_cost_home_ownership             integer     NOT NULL,
    publication_date                    date        NOT NULL,
    edition                             text        NOT NULL,
    source_url                          text        NOT NULL,
    source_file                         text        NOT NULL,
    release_page_url                    text        NOT NULL,
    loaded_at                           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_date, rp_code, lad24cd)
);
```

Guarded additions:

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'rsh_rp_stock_by_la_provider_type_chk') THEN
        ALTER TABLE rsh_rp_stock_by_la
            ADD CONSTRAINT rsh_rp_stock_by_la_provider_type_chk
            CHECK (provider_type IN ('PRP','LARP'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'rsh_rp_stock_by_la_components_sum_chk') THEN
        ALTER TABLE rsh_rp_stock_by_la
            ADD CONSTRAINT rsh_rp_stock_by_la_components_sum_chk
            CHECK (total_social_stock = general_needs_self_contained
                                      + general_needs_bedspaces
                                      + supported_housing_and_older_people
                                      + low_cost_home_ownership);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = 'rsh_rp_stock_by_la_lad24cd_idx') THEN
        CREATE INDEX rsh_rp_stock_by_la_lad24cd_idx
            ON rsh_rp_stock_by_la (lad24cd, stock_date);
    END IF;
END $$;
```

## Design notes

**One table with `provider_type`, not two.** SDR and LADR are separate returns
covering different provider types, so whether they merge was a real question.
They do: the publisher has already merged them into one sheet with an
identical column set, and the per-authority subtotals reconcile across both.
Merging is reading the file as published rather than a judgement imposed on
it. `provider_type` keeps them separable for the analyses that want them
apart.

**`stock_date` leads the primary key** because the source is annual and future
editions add years rather than replace them. Keying on `(rp_code, lad24cd)`
alone would have made the 2026 edition overwrite the 2025 one.

**The component-sum CHECK** turns an observed identity into an enforced one.
It holds on all 10,171 rows, and it is what proves a blank cell means zero
rather than unknown — a withheld figure could not sum correctly. That is how
gate 5 answers the suppression question for a source with no suppression
notation.

**`publisher_la_code` alongside `lad24cd`** records what RSH wrote before
`la_code_lookup` resolved it.

## Behaviour

- **Conflict handling:** `CREATE TABLE IF NOT EXISTS`; every `ALTER` guarded.
- **Re-run safety:** Idempotent.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

- Table created, 20 columns, primary key `(stock_date, rp_code, lad24cd)`.
- 2 CHECK constraints and 1 index present.
- Verified 2026-08-14.
