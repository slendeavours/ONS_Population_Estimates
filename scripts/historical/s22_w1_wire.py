"""S22 Phase 4 — wire Council Taxbase empty homes into Workflow 1.

Three steps, in order:

  1. Additive migration on staging_la_signals (DO $$ ... IF NOT EXISTS).
     The table is never dropped or recreated.
  2. Apply build_reports/s22_w1_node5_revised.sql to the stored n8n workflow
     "Workflow 1 - Pre-Computation", node 5 "LA Signals". The n8n REST API
     needs an interactive login this build does not have, so the workflow is
     updated in n8n's own database, which is where n8n reads it from at
     execution time. The previous node JSON is backed up first.
  3. Re-run Workflow 1 end to end against exempt_pipeline, executing each
     node's query in the workflow's connection order.

No tenant type ranking is added. Empty homes is a supply-side indicator, not
a cohort, so node 6 is untouched.
"""
import datetime
import json
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, get_conn  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO / "build_reports"
NODE5_SQL = REPORT_DIR / "s22_w1_node5_revised.sql"
BACKUP = REPORT_DIR / "s22_w1_node5_backup.json"

W1_ID = "IrrglXLYcphSg5bC"
NODE5_NAME = "LA Signals"

NEW_COLUMNS = [
    ("ctb_total_dwellings", "INTEGER"),
    ("ctb_empty_6m_plus", "INTEGER"),
    ("ctb_empty_homes_premium", "INTEGER"),
    ("ctb_second_homes", "INTEGER"),
    ("ctb_lte_rate_pct", "NUMERIC(6,2)"),
]

MIGRATION = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'staging_la_signals'
                      AND column_name = %(col)s) THEN
        EXECUTE format('ALTER TABLE staging_la_signals ADD COLUMN %%I %%s',
                       %(col)s, %(type)s);
    END IF;
END $$;
"""


def log(m):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {m}", flush=True)


def n8n_conn():
    return psycopg2.connect(host="localhost", port=5432, dbname="n8ndb",
                            user=ENV["PG_USER"], password=ENV["PG_PASSWORD"])


def step1_migrate(conn):
    cur = conn.cursor()
    for col, typ in NEW_COLUMNS:
        cur.execute(MIGRATION, {"col": col, "type": typ})
    conn.commit()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'staging_la_signals'
           AND column_name = ANY(%s) ORDER BY column_name
    """, ([c for c, _ in NEW_COLUMNS],))
    present = [r[0] for r in cur.fetchall()]
    if len(present) != len(NEW_COLUMNS):
        sys.exit(f"HALT: migration did not add all columns, present={present}")
    log(f"step 1: staging_la_signals columns present {present}")


def step2_apply_node5(sql):
    conn = n8n_conn()
    cur = conn.cursor()
    cur.execute('SELECT nodes FROM workflow_entity WHERE id = %s', (W1_ID,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"HALT: workflow {W1_ID} not found in n8ndb")
    nodes = row[0]
    target = next((n for n in nodes if n["name"] == NODE5_NAME), None)
    if target is None:
        sys.exit(f"HALT: node '{NODE5_NAME}' not found in Workflow 1")

    BACKUP.write_text(json.dumps(
        {"backed_up_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
         "workflow_id": W1_ID, "node": target}, indent=1), encoding="utf-8")

    if target["parameters"].get("query") == sql:
        log("step 2: node 5 already carries the revised SQL, no change")
    else:
        target["parameters"]["query"] = sql
        cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now()'
                    ' WHERE id = %s', (json.dumps(nodes), W1_ID))
        conn.commit()
        log("step 2: node 5 updated in n8ndb")

    cur.execute('SELECT nodes FROM workflow_entity WHERE id = %s', (W1_ID,))
    stored = next(n for n in cur.fetchone()[0] if n["name"] == NODE5_NAME)
    if stored["parameters"]["query"] != sql:
        sys.exit("HALT: node 5 readback does not match the revised SQL")
    log("step 2: readback confirms node 5 matches "
        f"{NODE5_SQL.relative_to(REPO)}")
    conn.close()


def _pg(sql):
    """n8n Postgres node placeholders -> psycopg2 named parameters.

    The node passes queryReplacement into $1. psycopg2 does not understand
    $1, so it becomes a named parameter, which also handles the queries that
    use $1 ten times. Literal per-cent signs (comments, RAISE format strings)
    are escaped first so they are not read as placeholders. The query stays
    parameterised; nothing is concatenated in.
    """
    return sql.replace("%", "%%").replace("$1", "%(run_id)s")


def align_run_sequence(conn, warnings):
    """staging_runs.run_id must lead every run_id already in the staging data.

    Runs 10 and 11 were written to staging_la_signals by direct SQL without a
    matching staging_runs row, so the sequence trails the data and the next
    nextval() would collide with an existing signals run.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT GREATEST(COALESCE((SELECT MAX(run_id) FROM staging_runs), 0),
                        COALESCE((SELECT MAX(run_id) FROM staging_la_signals), 0))
    """)
    high = cur.fetchone()[0]
    cur.execute("SELECT last_value FROM staging_runs_run_id_seq")
    seq = cur.fetchone()[0]
    if seq <= high:
        cur.execute("SELECT setval('staging_runs_run_id_seq', %s, true)",
                    (high,))
        conn.commit()
        msg = (f"staging_runs sequence was at {seq} but staging_la_signals "
               f"already holds run_id {high}; runs 10 and 11 were written by "
               "direct SQL with no staging_runs row. Sequence advanced to "
               f"{high} so the new run does not collide.")
        warnings.append(msg)
        log(f"  {msg}")


def step3_rerun_w1(conn, node5_sql, nodes_by_name, warnings):
    align_run_sequence(conn, warnings)
    cur = conn.cursor()

    cur.execute(nodes_by_name["Create Run"])
    run_id = cur.fetchone()[0]
    log(f"step 3: W1 run_id {run_id}")
    p = {"run_id": run_id}

    cur.execute(_pg(nodes_by_name["National Aggregates"]), p)
    log(f"  National Aggregates: {cur.rowcount} row")

    cur.execute(_pg(node5_sql), p)
    log(f"  LA Signals: {cur.rowcount} rows")

    cur.execute(_pg(nodes_by_name["Tenant Type Rankings"]), p)
    log(f"  Tenant Type Rankings: {cur.rowcount} rows")

    cur.execute(_pg(nodes_by_name["Section 3 Top 3 LAs"]), p)
    log(f"  Section 3 Top 3 LAs: {cur.rowcount} rows")

    cur.execute(_pg(nodes_by_name["Mark Run Complete"]), p)
    conn.commit()
    log("  Mark Run Complete: run marked complete")
    return run_id


def main():
    sql = NODE5_SQL.read_text(encoding="utf-8")

    conn = get_conn()
    conn.autocommit = False
    step1_migrate(conn)
    step2_apply_node5(sql)

    nc = n8n_conn()
    ncur = nc.cursor()
    ncur.execute('SELECT nodes FROM workflow_entity WHERE id = %s', (W1_ID,))
    nodes = ncur.fetchone()[0]
    nc.close()
    by_name = {n["name"]: n["parameters"].get("query") for n in nodes}

    # The contract is refreshed from the stored node and checked in both
    # directions before any run happens. The W1 pre-flight node enforces the
    # table half inside the workflow; this enforces the node half, which
    # needs the workflow JSON and so cannot be done in SQL.
    import w1_contract_check
    errors, contract_warnings, contract = w1_contract_check.check()
    for w in contract_warnings:
        log(f"  contract WARN {w}")
    if errors:
        for e in errors:
            log(f"  contract ERROR {e}")
        conn.close()
        sys.exit("HALT: W1 node 5 and staging_la_signals diverge. Fix the "
                 "node before running the workflow.")
    log(f"step 2: contract check OK, {len(contract)} columns, "
        f"{len(contract_warnings)} warning(s)")

    warnings = []
    run_id = step3_rerun_w1(conn, sql, by_name, warnings)

    cur = conn.cursor()
    cols = [c for c, _ in NEW_COLUMNS]
    sel = ", ".join(f"COUNT({c})" for c in cols)
    cur.execute(f"SELECT COUNT(*), {sel} FROM staging_la_signals "
                "WHERE run_id = %s", (run_id,))
    row = cur.fetchone()
    total, counts = row[0], row[1:]
    log(f"step 3: run {run_id} has {total} rows; populated "
        + ", ".join(f"{c}={n}" for c, n in zip(cols, counts)))

    if total != 296 or any(n != 296 for n in counts):
        conn.close()
        sys.exit("HALT: W1 gate 6 failed — expected 296 rows with all five "
                 f"S22 columns populated, got {total} rows and {counts}")

    (REPORT_DIR / "s22_w1_run.json").write_text(json.dumps(
        {"run_id": run_id, "rows": total,
         "populated": dict(zip(cols, counts)),
         "warnings": warnings,
         "node5_sql": str(NODE5_SQL.relative_to(REPO)),
         "at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        indent=2), encoding="utf-8")
    conn.close()
    log("Phase 4 complete")


if __name__ == "__main__":
    main()
