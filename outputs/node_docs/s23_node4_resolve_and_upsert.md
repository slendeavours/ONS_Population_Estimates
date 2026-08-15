# S23 Node 4: Resolve Geography and Upsert

- **Type:** Code + Postgres upsert
- **Purpose:** Resolve publisher LA codes through `la_code_lookup`, assert the assumed grain, and upsert the provider rows.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`.

## Logic

1. **Resolve geography.** Every publisher LA code on a provider row:

   ```sql
   SELECT old_code, new_code FROM la_code_lookup WHERE old_code = ANY(%s)
   ```

   Any unresolved code halts with the list. RSH uses `E08000016` and
   `E08000019` for Barnsley and Sheffield, which are the pipeline's canonical
   codes and resolve to themselves — so unlike S1b there is no recode here,
   but the resolution still runs, because "no recode needed this edition" is
   not a reason to skip the rule.

2. **Assert the grain.** Every `(rp_code, lad24cd)` pair must be unique. A
   duplicate halts, because the assumed grain would be wrong and the primary
   key would silently drop a row.

3. **Assert the component sum** per row before it is built, so the failure
   names the provider and authority rather than surfacing as a constraint
   violation on a batch of a thousand.

4. **Map `RP_Type` to `provider_type`:** `Large` and `Small` → `PRP`,
   `LARP` → `LARP`. The original band is kept in `rp_size_band`, which is what
   distinguishes long-form from short-form SDR returns and therefore which
   rows the publisher weights nationally.

## Query

```sql
INSERT INTO rsh_rp_stock_by_la (
    stock_date, rp_code, lad24cd, rp_name, provider_type, rp_size_band,
    survey_status, publisher_la_code, la_name, total_social_stock,
    general_needs_self_contained, general_needs_bedspaces,
    supported_housing_and_older_people, low_cost_home_ownership,
    publication_date, edition, source_url, source_file, release_page_url)
VALUES %s
ON CONFLICT (stock_date, rp_code, lad24cd) DO UPDATE SET
    rp_name                            = EXCLUDED.rp_name,
    provider_type                      = EXCLUDED.provider_type,
    rp_size_band                       = EXCLUDED.rp_size_band,
    survey_status                      = EXCLUDED.survey_status,
    publisher_la_code                  = EXCLUDED.publisher_la_code,
    la_name                            = EXCLUDED.la_name,
    total_social_stock                 = EXCLUDED.total_social_stock,
    general_needs_self_contained       = EXCLUDED.general_needs_self_contained,
    general_needs_bedspaces            = EXCLUDED.general_needs_bedspaces,
    supported_housing_and_older_people = EXCLUDED.supported_housing_and_older_people,
    low_cost_home_ownership            = EXCLUDED.low_cost_home_ownership,
    publication_date                   = EXCLUDED.publication_date,
    edition                            = EXCLUDED.edition,
    source_url                         = EXCLUDED.source_url,
    source_file                        = EXCLUDED.source_file,
    release_page_url                   = EXCLUDED.release_page_url,
    loaded_at                          = now();
```

`psycopg2.extras.execute_values`, page size 1000. Parameterised throughout.

## Query Parameters

| Parameter | Source |
|---|---|
| `stock_date` | Node 1 — 31 March of the edition's closing year |
| `publication_date`, `edition` | Node 1 |
| `source_url`, `source_file`, `release_page_url` | Node 1 |
| `lad24cd` | `la_code_lookup.new_code` |
| `publisher_la_code` | `LA_Code` as RSH wrote it |

## Behaviour

- **Conflict handling:** Upsert on `(stock_date, rp_code, lad24cd)`. Re-running
  an edition replaces it in place; a new edition adds a new `stock_date`
  without touching the old one, so the series accumulates.
- **Re-run safety:** Idempotent, proved by gate 6.
- **Failure:** One transaction for the whole edition. Any halt rolls back
  everything.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

- 10,171 rows written for `stock_date` 2025-03-31.
- 9,943 PRP (SDR), 228 LARP (LADR).
- 296 distinct authorities; 295 carry supported housing above zero.
- 504,902 supported housing and older people units; 4,533,055 total social
  stock.
- 0 unresolved codes, 0 duplicate grain pairs, 0 component-sum failures.
- Verified 2026-08-14.
