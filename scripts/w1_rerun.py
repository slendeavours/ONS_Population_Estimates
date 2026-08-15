"""Re-run Workflow 1 from the stored n8n node definitions.

Runs the workflow the way s22_w1_wire.py step 3 does - reading each node's
query out of n8ndb and executing it against exempt_pipeline in connection
order - without that script's S22-specific migration and node patch. The
helpers are imported rather than copied so there is one definition of how W1
is executed from a script.

Reading the queries from n8ndb rather than from the repository is the point.
The stored node is what n8n executes, and it has drifted from the repository
twice before. A re-run that used a file would prove nothing about the
workflow.

Usage:
    python scripts/w1_rerun.py
    python scripts/w1_rerun.py --dry-run
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402
import s22_w1_wire as wire  # noqa: E402
import w1_contract_check  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ORDER = ["National Aggregates", "LA Signals", "Tenant Type Rankings",
         "Section 3 Top 3 LAs"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="check the contract and the node set, run nothing")
    args = ap.parse_args()

    nc = wire.n8n_conn()
    ncur = nc.cursor()
    ncur.execute("SELECT nodes FROM workflow_entity WHERE id = %s",
                 (wire.W1_ID,))
    nodes = ncur.fetchone()[0]
    if isinstance(nodes, str):
        nodes = json.loads(nodes)
    nc.close()
    by_name = {n["name"]: (n.get("parameters") or {}).get("query")
               for n in nodes}

    missing = [n for n in ORDER + ["Create Run", "Mark Run Complete"]
               if not by_name.get(n)]
    if missing:
        sys.exit(f"HALT: stored workflow is missing node(s): {missing}")

    # The NULL-safe patch must be in the node this run is about to execute,
    # not merely in the repository copy of it.
    sig = by_name["LA Signals"]
    if "WHEN ta_cur.households_in_ta IS NULL THEN 'no_current_data'" not in sig:
        sys.exit("HALT: the stored LA Signals node does not carry the "
                 "NULL-first trend branch. Run w1_null_safe_labels_patch.py "
                 "before re-running the workflow.")
    if "ELSE 'falling_strongly'" in sig:
        sys.exit("HALT: the stored LA Signals node still has a catch-all "
                 "ELSE yielding 'falling_strongly'.")
    wire.log("stored node carries the NULL-safe labels")

    errors, warnings, _ = w1_contract_check.check()
    for w in warnings:
        wire.log(f"  contract WARN {w}")
    if errors:
        for e in errors:
            wire.log(f"  contract ERROR {e}")
        sys.exit("HALT: the node/table column contract does not hold")
    wire.log("column contract holds in both directions")

    if args.dry_run:
        wire.log("--dry-run, nothing executed")
        return 0

    conn = get_conn()
    conn.autocommit = False
    try:
        wire.align_run_sequence(conn, [])
        cur = conn.cursor()
        cur.execute(by_name["Create Run"])
        run_id = cur.fetchone()[0]
        wire.log(f"W1 run_id {run_id}")
        p = {"run_id": run_id}
        for name in ORDER:
            cur.execute(wire._pg(by_name[name]), p)
            wire.log(f"  {name}: {cur.rowcount} row(s)")
        cur.execute(wire._pg(by_name["Mark Run Complete"]), p)
        conn.commit()
        wire.log("  Mark Run Complete: run marked complete")

        cur.execute("""
            SELECT ta_trend_label, COUNT(*) FROM staging_la_signals
            WHERE run_id = %s GROUP BY 1 ORDER BY 2 DESC
        """, (run_id,))
        wire.log("  trend labels this run:")
        for label, n in cur.fetchall():
            wire.log(f"     {label:<18} {n}")
        cur.close()
        print(run_id)
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
