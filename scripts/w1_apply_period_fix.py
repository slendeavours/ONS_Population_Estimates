"""Apply the W1 period-pin correction to both stored nodes, then re-run W1.

Modelled on scripts/s22_w1_wire.py, which handles one node. This handles two,
because the audit found National Aggregates carried the same defect and had
never been looked at.

What is being corrected:

  * 14 hardcoded period literals, eight in LA Signals and six in National
    Aggregates. Two were already stale - TA pinned to 2025Q2 with 2025Q3
    loaded since 2026-07-06, and EFS restricted to two financial years while
    la_efs_support carries 2026-27. The rest were correct on the day they were
    typed and would have gone stale silently on the next load.
  * The TA comparison quarter is derived as latest-minus-four rather than
    typed.
  * S8 is superseded by S8b; both nodes now read la_hb_accom_type_caseload.

Every step reads its result back. The node is re-fetched after the update and
compared, the run is created through the Create Run node rather than by
inserting into staging_runs, and the row counts are checked after commit.

Usage:
    python scripts/w1_apply_period_fix.py --dry-run
    python scripts/w1_apply_period_fix.py --apply
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, get_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO / "build_reports"
W1_ID = "IrrglXLYcphSg5bC"

TARGETS = {
    "LA Signals": REPORT_DIR / "w1_la_signals_periods_revised.sql",
    "National Aggregates": REPORT_DIR / "w1_national_aggregates_periods_revised.sql",
}
BACKUP = REPORT_DIR / "w1_period_fix_backup.json"
RESULT = REPORT_DIR / "w1_period_fix_run.json"

RUN_ORDER = ["National Aggregates", "LA Signals", "Tenant Type Rankings",
             "Section 3 Top 3 LAs"]


def log(m):
    print(f"{datetime.datetime.now():%H:%M:%S} {m}", flush=True)


def n8n_conn():
    return psycopg2.connect(
        host="localhost", port=int(ENV.get("PG_PORT", "5432")),
        dbname="n8ndb", user=ENV.get("PG_USER"), password=ENV.get("PG_PASSWORD"))


def _pg(sql):
    """n8n $1 placeholders -> psycopg2 named parameters, per s22_w1_wire."""
    return sql.replace("%", "%%").replace("$1", "%(run_id)s")


def load_revised():
    out = {}
    for name, path in TARGETS.items():
        if not path.exists():
            sys.exit(f"HALT: {path} not found")
        out[name] = path.read_text(encoding="utf-8")
    return out


def apply_nodes(revised, dry_run):
    conn = n8n_conn()
    cur = conn.cursor()
    cur.execute('SELECT nodes FROM workflow_entity WHERE id = %s', (W1_ID,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"HALT: workflow {W1_ID} not found in n8ndb")
    nodes = row[0]

    backup = {"backed_up_at": datetime.datetime.now(
                  datetime.timezone.utc).isoformat(),
              "workflow_id": W1_ID, "nodes": {}}
    changed = []
    for name, sql in revised.items():
        target = next((n for n in nodes if n["name"] == name), None)
        if target is None:
            sys.exit(f"HALT: node '{name}' not found in Workflow 1")
        backup["nodes"][name] = target["parameters"].get("query")
        if target["parameters"].get("query") == sql:
            log(f"  {name}: already carries the revised SQL")
        else:
            target["parameters"]["query"] = sql
            changed.append(name)

    BACKUP.write_text(json.dumps(backup, indent=1), encoding="utf-8")
    log(f"  previous SQL backed up to {BACKUP.name}")

    if dry_run:
        log(f"  DRY RUN: would update {changed or 'nothing'}")
        conn.close()
        return changed

    if changed:
        cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now()'
                    ' WHERE id = %s', (json.dumps(nodes), W1_ID))
        conn.commit()
        log(f"  updated in n8ndb: {changed}")

    # Read back. An UPDATE that reported success is not the same as the stored
    # node carrying the SQL - run 12 executed against different SQL than what
    # was stored, and nothing noticed.
    cur.execute('SELECT nodes FROM workflow_entity WHERE id = %s', (W1_ID,))
    stored = {n["name"]: n["parameters"].get("query") for n in cur.fetchone()[0]}
    for name, sql in revised.items():
        if stored.get(name) != sql:
            sys.exit(f"HALT: {name} readback does not match the revised SQL")
    log("  readback confirms both nodes match the revised SQL")
    conn.close()
    return changed


def align_run_sequence(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT GREATEST(COALESCE((SELECT MAX(run_id) FROM staging_runs), 0),
                        COALESCE((SELECT MAX(run_id) FROM staging_la_signals), 0))
    """)
    high = cur.fetchone()[0]
    cur.execute("SELECT last_value FROM staging_runs_run_id_seq")
    seq = cur.fetchone()[0]
    if seq <= high:
        cur.execute("SELECT setval('staging_runs_run_id_seq', %s, true)", (high,))
        conn.commit()
        log(f"  sequence advanced from {seq} to {high} so the new run cannot "
            f"collide with existing staging data")


def rerun(conn, nodes_by_name):
    align_run_sequence(conn)
    cur = conn.cursor()
    # Through the Create Run node, never a direct staging_runs insert.
    cur.execute(nodes_by_name["Create Run"])
    run_id = cur.fetchone()[0]
    log(f"  run_id {run_id} created through the Create Run node")
    p = {"run_id": run_id}
    for name in RUN_ORDER:
        cur.execute(_pg(nodes_by_name[name]), p)
        log(f"  {name}: {cur.rowcount} row(s)")
    cur.execute(_pg(nodes_by_name["Mark Run Complete"]), p)
    conn.commit()
    log("  run marked complete")
    return run_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        sys.exit("choose --apply or --dry-run")

    revised = load_revised()
    log("step 1: apply revised SQL to the stored nodes")
    apply_nodes(revised, args.dry_run)

    log("step 2: node contract check")
    import w1_contract_check
    errors, warns, contract = w1_contract_check.check()
    for w in warns:
        log(f"  WARN {w}")
    if errors:
        for e in errors:
            log(f"  ERROR {e}")
        sys.exit("HALT: node 5 and staging_la_signals diverge.")
    log(f"  contract OK, {len(contract)} columns, {len(warns)} warning(s)")

    if args.dry_run:
        log("DRY RUN complete - nothing run, nothing exported")
        return 0

    conn = get_conn()
    conn.autocommit = False
    nc = n8n_conn()
    ncur = nc.cursor()
    ncur.execute('SELECT nodes FROM workflow_entity WHERE id = %s', (W1_ID,))
    by_name = {n["name"]: n["parameters"].get("query") for n in ncur.fetchone()[0]}
    nc.close()

    log("step 3: re-run W1")
    run_id = rerun(conn, by_name)

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COUNT(ta_households_current), "
                "COUNT(hb_sa_caseload) FROM staging_la_signals WHERE run_id=%s",
                (run_id,))
    total, ta, hbsa = cur.fetchone()
    log(f"step 4: run {run_id}: {total} rows, ta_households_current {ta}/296, "
        f"hb_sa_caseload {hbsa}/296")
    if total != 296:
        sys.exit(f"HALT: expected 296 rows, got {total}")

    RESULT.write_text(json.dumps(
        {"run_id": run_id, "rows": total,
         "ta_populated": ta, "hb_sa_populated": hbsa,
         "at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        indent=2), encoding="utf-8")
    conn.close()
    log(f"done - run {run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
