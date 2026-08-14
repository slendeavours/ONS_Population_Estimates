# S1b Node 3: Create `la_homelessness_support_needs`

- **Type:** Postgres DDL (additive only)
- **Purpose:** Create the long-format target table and the constraints that make suppression handling and category grouping structural rather than conventional.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`. Never a literal.

## Query

```sql
CREATE TABLE IF NOT EXISTS la_homelessness_support_needs (
    lad24cd            varchar(9)  NOT NULL,
    period             varchar(6)  NOT NULL,
    category_code      text        NOT NULL,
    value              integer,
    value_flag         text,
    category_group     text        NOT NULL,
    category_label     text        NOT NULL,
    reference_quarter  varchar(7)  NOT NULL,
    source_url         text        NOT NULL,
    source_edition     text        NOT NULL,
    edition_variant    text        NOT NULL,
    release_page_url   text        NOT NULL,
    layout_version     text        NOT NULL,
    publisher_la_code  varchar(9)  NOT NULL,
    loaded_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, period, category_code)
);
```

Guarded additions, wrapped so a re-run is a no-op:

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'la_homelessness_support_needs_value_flag_chk') THEN
        ALTER TABLE la_homelessness_support_needs
            ADD CONSTRAINT la_homelessness_support_needs_value_flag_chk
            CHECK (value_flag IS NULL
                   OR value_flag IN ('missing','suppressed','not_applicable'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'la_homelessness_support_needs_value_xor_flag_chk') THEN
        ALTER TABLE la_homelessness_support_needs
            ADD CONSTRAINT la_homelessness_support_needs_value_xor_flag_chk
            CHECK (num_nonnulls(value, value_flag) = 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'la_homelessness_support_needs_category_group_chk') THEN
        ALTER TABLE la_homelessness_support_needs
            ADD CONSTRAINT la_homelessness_support_needs_category_group_chk
            CHECK (category_group IN
                   ('support_need','needs_breakdown','needs_total','duty_total'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'la_homelessness_support_needs_edition_variant_chk') THEN
        ALTER TABLE la_homelessness_support_needs
            ADD CONSTRAINT la_homelessness_support_needs_edition_variant_chk
            CHECK (edition_variant IN ('original','revised','corrected','fixed'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = 'la_homelessness_support_needs_period_category_idx') THEN
        CREATE INDEX la_homelessness_support_needs_period_category_idx
            ON la_homelessness_support_needs (period, category_code);
    END IF;
END $$;
```

## Design notes

**Why long format.** Option (a) was to widen `la_statutory_homelessness` by
nineteen columns. Option (b) was this. Three things decided it: A3 publishes
24 categories and putting the publisher's list into the schema makes every
publisher change a migration; MHCLG rewrote every label in the 2026 release,
which a wide schema would have absorbed as a rename; and the existing five
columns were found to be mis-mapped, so extending them would build on top of
columns whose contents do not match their names.

**`num_nonnulls(value, value_flag) = 1`** is the constraint that makes
suppression structural. A row carries either a number or a reason it has none,
never both and never neither. A suppressed cell cannot become a zero by
accident because a zero with a flag will not insert.

**`category_group`** separates the multi-response categories from the
mutually exclusive household counts. A3's 24 categories do not sum to the
household total — the publisher's own note says a household can appear across
several — so a consumer needs to know which arithmetic is legitimate without
having read the documentation.

**`publisher_la_code` alongside `lad24cd`** records what the publisher wrote
before `la_code_lookup` resolved it. That is what makes the Barnsley and
Sheffield recode auditable rather than invisible.

## Behaviour

- **Conflict handling:** `CREATE TABLE IF NOT EXISTS`; every `ALTER` guarded
  by an existence check. Additive only, per the standing DDL rule.
- **Re-run safety:** Fully idempotent, proved by the verification suite's
  gate 6.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`, credentials from `.env`
resolved by `scripts/_db.py` from both the repository root and its parent.

## Verified Output

- Table created with 15 columns, primary key `(lad24cd, period, category_code)`.
- 4 CHECK constraints and 1 index present.
- Verified 2026-08-14.
