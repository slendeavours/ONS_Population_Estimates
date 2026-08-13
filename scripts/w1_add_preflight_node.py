"""Insert the signal-column pre-flight node into Workflow 1.

The check must live inside W1, not only in the export path: W1 has been run
without exporting, and an export-time check alone lets a divergence sit
undetected until the next publish.

The node compares staging_la_signals against staging_signal_contract in both
directions and aborts the run on divergence. It sits between "Create Staging
Tables" and "Create Run", so it fires before a run id is issued and a failed
pre-flight leaves no half-finished run behind.

Idempotent: re-running updates the node in place rather than adding a second.
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from w1_contract_check import W1_ID, n8n_conn  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BACKUP = REPO / "build_reports" / "w1_workflow_backup.json"

NODE_NAME = "Signal Column Pre-flight"
UPSTREAM = "Create Staging Tables"
DOWNSTREAM = "Create Run"

PREFLIGHT_SQL = """-- W1 pre-flight: does node 5 still write every column of
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
"""


def main():
    conn = n8n_conn()
    cur = conn.cursor()
    cur.execute('SELECT nodes, connections FROM workflow_entity WHERE id = %s',
                (W1_ID,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"HALT: workflow {W1_ID} not found in n8ndb")
    nodes, connections = row

    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(
        {"backed_up_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
         "workflow_id": W1_ID, "nodes": nodes,
         "connections": connections}, indent=1), encoding="utf-8")

    upstream = next((n for n in nodes if n["name"] == UPSTREAM), None)
    if upstream is None:
        sys.exit(f"HALT: '{UPSTREAM}' not found in Workflow 1")

    existing = next((n for n in nodes if n["name"] == NODE_NAME), None)
    if existing is not None:
        if existing["parameters"].get("query") == PREFLIGHT_SQL:
            print("pre-flight node already present and current, no change")
            conn.close()
            return
        existing["parameters"]["query"] = PREFLIGHT_SQL
        print("pre-flight node present, SQL updated")
    else:
        pos = upstream.get("position", [0, 0])
        nodes.append({
            "parameters": {"operation": "executeQuery",
                           "query": PREFLIGHT_SQL,
                           "options": {}},
            "type": "n8n-nodes-base.postgres",
            "typeVersion": upstream.get("typeVersion", 2.6),
            "position": [pos[0] + 180, pos[1] + 160],
            "id": "b22c0de0-5c22-4a11-9f22-c0de5c22a11e",
            "name": NODE_NAME,
            "credentials": upstream.get("credentials", {}),
        })
        connections[UPSTREAM] = {
            "main": [[{"node": NODE_NAME, "type": "main", "index": 0}]]}
        connections[NODE_NAME] = {
            "main": [[{"node": DOWNSTREAM, "type": "main", "index": 0}]]}
        print(f"pre-flight node inserted between '{UPSTREAM}' and "
              f"'{DOWNSTREAM}'")

    cur.execute('UPDATE workflow_entity SET nodes = %s, connections = %s, '
                '"updatedAt" = now() WHERE id = %s',
                (json.dumps(nodes), json.dumps(connections), W1_ID))
    conn.commit()

    cur.execute('SELECT nodes, connections FROM workflow_entity WHERE id = %s',
                (W1_ID,))
    n2, c2 = cur.fetchone()
    stored = next((n for n in n2 if n["name"] == NODE_NAME), None)
    if stored is None or stored["parameters"]["query"] != PREFLIGHT_SQL:
        sys.exit("HALT: pre-flight node readback failed")
    chain, seen, cursor_name = [], set(), "When clicking ‘Execute workflow’"
    while cursor_name and cursor_name not in seen:
        seen.add(cursor_name)
        chain.append(cursor_name)
        nxt = c2.get(cursor_name, {}).get("main", [[]])
        cursor_name = nxt[0][0]["node"] if nxt and nxt[0] else None
    print("workflow order:")
    for i, name in enumerate(chain, 1):
        print(f"  {i}. {name}")
    conn.close()


if __name__ == "__main__":
    main()
