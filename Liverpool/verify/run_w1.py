"""Re-run Workflow 1 against the live node queries.

The queries are read from n8n's own database and executed in connection order,
so what runs here is what the workflow runs. An earlier version of this script
read them from a checked-in workflow backup, which had drifted: the backup's
LA Signals query was an older, shorter one whose la_population join was not
pinned to the latest vintage, and it omitted the pre-flight node entirely.
Reading from n8ndb removes that whole class of error, and is the same source
scripts/w1_contract_check.py uses.
"""
import json
import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(r"C:\Users\slewi\ucws-repo\.env")
W1_ID = "IrrglXLYcphSg5bC"


def connect(dbname):
    return psycopg2.connect(host="localhost", port=os.getenv("PG_PORT"), dbname=dbname,
                            user=os.getenv("PG_USER"), password=os.getenv("PG_PASSWORD"))


def load_nodes():
    """Return [(name, query)] in connection order, skipping nodes with no SQL."""
    with connect("n8ndb") as nc, nc.cursor() as cur:
        cur.execute("SELECT nodes, connections FROM workflow_entity WHERE id = %s", (W1_ID,))
        nodes, conns = cur.fetchone()
    nodes = json.loads(nodes) if isinstance(nodes, str) else nodes
    conns = json.loads(conns) if isinstance(conns, str) else conns

    queries = {n["name"]: (n.get("parameters") or {}).get("query") for n in nodes}
    nxt = {}
    for src, spec in conns.items():
        for outputs in spec.get("main", []):
            if outputs:
                nxt[src] = outputs[0]["node"]
    targets = set(nxt.values())
    start = next(n for n in nxt if n not in targets)

    order, seen = [], set()
    cur_node = start
    while cur_node and cur_node not in seen:
        seen.add(cur_node)
        if queries.get(cur_node):
            order.append((cur_node, queries[cur_node]))
        cur_node = nxt.get(cur_node)
    return order


def main():
    order = load_nodes()
    print("Live W1 node order: %s\n" % " -> ".join(n for n, _ in order))

    conn = connect(os.getenv("PG_DATABASE"))
    cur = conn.cursor()
    run_id = int(os.getenv("RUN_ID")) if os.getenv("RUN_ID") else None
    if run_id:
        print("Resuming open run   -> run_id %s" % run_id)

    for name, query in order:
        if name == "Create Run" and run_id:
            continue
        # n8n binds the run id as $1; psycopg2 does not. Substituting the
        # integer we hold is safe and avoids psycopg2 reading the % in the
        # aggregates query as a format specifier.
        q = query.replace("$1", str(run_id)) if run_id else query
        if "$1" in q:
            sys.exit("%s needs a run id before it can execute" % name)
        cur.execute(q)
        if name == "Create Run":
            run_id = cur.fetchone()[0]
        conn.commit()
        print("  %-26s ok%s" % (name, "  -> run_id %s" % run_id if name == "Create Run" else ""))

    c2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c2.execute("SELECT COUNT(*) n FROM staging_la_signals WHERE run_id=%s", (run_id,))
    print("\nstaging_la_signals   -> %d rows" % c2.fetchone()["n"])
    c2.execute("SELECT run_id, status FROM staging_runs WHERE run_id=%s", (run_id,))
    print("staging_runs         -> %s" % dict(c2.fetchone()))
    conn.close()
    return run_id


if __name__ == "__main__":
    print("\nrun_id written: %s" % main())
