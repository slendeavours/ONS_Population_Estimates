-- W1 pre-flight: does node 5 still write every column of
-- staging_la_signals, and only columns that exist?
--
-- The contract is refreshed from the stored node by
-- scripts/w1_contract_check.py. This node enforces the table half inside the
-- workflow so a divergence cannot wait for the next publish to be noticed.
DO $$
DECLARE
    missing_from_node  TEXT;
    missing_from_table TEXT;
    contract_rows      INTEGER;
    contract_age       INTERVAL;
BEGIN
    SELECT COUNT(*), now() - MIN(recorded_at)
      INTO contract_rows, contract_age
      FROM staging_signal_contract;

    IF contract_rows = 0 THEN
        RAISE EXCEPTION
          'W1 pre-flight: staging_signal_contract is empty. Run '
          'scripts/w1_contract_check.py before the workflow — the contract '
          'is what this check compares against.';
    END IF;

    SELECT string_agg(c.column_name, ', ' ORDER BY c.ordinal_position)
      INTO missing_from_node
      FROM information_schema.columns c
     WHERE c.table_schema = 'public'
       AND c.table_name = 'staging_la_signals'
       AND NOT EXISTS (SELECT 1 FROM staging_signal_contract sc
                        WHERE sc.column_name = c.column_name);

    SELECT string_agg(sc.column_name, ', ' ORDER BY sc.ordinal)
      INTO missing_from_table
      FROM staging_signal_contract sc
     WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns c
                        WHERE c.table_schema = 'public'
                          AND c.table_name = 'staging_la_signals'
                          AND c.column_name = sc.column_name);

    IF missing_from_node IS NOT NULL THEN
        RAISE EXCEPTION
          'W1 pre-flight ABORT: staging_la_signals has column(s) node 5 does '
          'not write: %. A run in this state writes them null and nothing '
          'would tell you. Update node 5, then re-run '
          'scripts/w1_contract_check.py.', missing_from_node;
    END IF;

    IF missing_from_table IS NOT NULL THEN
        RAISE EXCEPTION
          'W1 pre-flight ABORT: node 5 writes column(s) absent from '
          'staging_la_signals: %.', missing_from_table;
    END IF;

    RAISE NOTICE 'W1 pre-flight OK: % columns, contract recorded % ago.',
                 contract_rows, contract_age;
END $$;

SELECT COUNT(*) AS contract_columns,
       MAX(recorded_at) AS contract_recorded_at,
       MAX(node_query_sha256) AS node_query_sha256
  FROM staging_signal_contract;
