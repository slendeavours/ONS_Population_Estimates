"""Retire the n8n loader workflow for RO4 (S2, MHCLG revenue outturn).

"Revenue Outturn RO4 (S2)" (L55sz2PN4W9V0E0I) is inactive with a manual trigger,
but it can still run: its Code node "Process RO4 Data" turns a blank into 0
(`parseFloat(cols[n]) || 0` on all 11 measures) and its "Upsert RO4 Data" node
writes straight into the live ro4_housing_expenditure, bypassing the editions
tables (F4 zero audit, docs/f4-zero-audit-2026-10.md section 2; rule 1).

The Code node's jsCode is replaced by a hard stop that names the table and the
loader (scripts/ro4_editions.py). From the workflow's `connections` the script
proves that every Postgres node that writes is downstream of the Code node (so
it cannot run once the Code node throws). A write node that is not downstream is
retired too: its query becomes a SELECT that raises. Nothing else changes
(asserted by comparing the nodes list with the replaced values blanked, before
and after). The workflow is never run and never activated. The fetch node
upstream is not touched, so a manual run would still fetch before it throws.

Follows scripts/n8n_retire_s3_s14.py: dry run by default; the backup of the old
values is written only on --apply; the UPDATE is read back inside the
transaction (a mismatch rolls back); idempotent on the RETIRED prefix.

Usage:
    python scripts/n8n_retire_ro4.py --dry-run    # default
    python scripts/n8n_retire_ro4.py --apply
"""
import argparse
import copy
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from n8n_retire_no_loader_sources import (  # noqa: E402
    Halt, _one, blanked, downstream, log, node_test, write_nodes)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
BACKUP_DIR = REPO / "build_reports" / "w1_node_backups"

DATE = "2026-10-12"
PREFIX = f'throw new Error("RETIRED {DATE}'
QUERY_PREFIX = f'SELECT 1/0 AS "RETIRED {DATE}'

WID = "L55sz2PN4W9V0E0I"
NAME = "Revenue Outturn RO4 (S2)"
CODE_NODE = "Process RO4 Data"
MARKER = "|| 0"          # the defect: a blank becomes 0
LOADER = "ro4_editions.py"


def new_code():
    msg = (f"RETIRED {DATE}: this workflow turned blanks into 0 and upserted "
           "ro4_housing_expenditure directly, bypassing the editions tables. "
           f"Use python scripts/{LOADER} (preview by default).")
    return "throw new Error(" + json.dumps(msg) + ");\n"


def new_query():
    return f'{QUERY_PREFIX} use scripts/{LOADER}"'


def plan(workflow):
    """Write nodes that are not downstream of the Code node."""
    _one(workflow["nodes"], CODE_NODE)
    down = downstream(workflow["connections"], CODE_NODE)
    return [w for w in write_nodes(workflow["nodes"]) if w not in down]


def rewrite(workflow):
    """-> (new nodes, names changed, {name: old value}). Halts on anything not
    as surveyed. A workflow already retired returns no changes."""
    if workflow["id"] != WID or workflow["name"] != NAME:
        raise Halt(f"workflow {workflow['id']} is {workflow['name']!r}, expected {WID} {NAME!r}")
    if workflow["active"]:
        raise Halt(f"workflow {NAME!r} is active; refusing to edit it")
    nodes = workflow["nodes"]
    extras = plan(workflow)
    new_nodes = copy.deepcopy(nodes)
    by = {n["name"]: n for n in new_nodes}
    old, changed = {}, []
    want = new_code()

    cur = by[CODE_NODE]["parameters"].get("jsCode")
    if cur is None:
        raise Halt(f"node {CODE_NODE!r} has no jsCode")
    if cur != want:
        if cur.startswith(PREFIX):
            raise Halt(f"{CODE_NODE!r} carries a different retirement text; not touching it")
        if MARKER not in cur:
            raise Halt(f"{CODE_NODE!r} no longer contains {MARKER!r}; the workflow was edited "
                       "since the survey, refusing to retire it")
        old[CODE_NODE] = cur
        by[CODE_NODE]["parameters"]["jsCode"] = want
        changed.append(CODE_NODE)

    for x in extras:
        p = by[x]["parameters"]
        old[x] = p["query"]
        p["query"] = new_query()
        changed.append(x)

    assert blanked(new_nodes, changed) == blanked(nodes, changed), \
        "change is not confined to the replaced values"
    assert sorted(a["name"] for a, b in zip(nodes, new_nodes) if a != b) == sorted(changed)
    return new_nodes, changed, old


def _fetch(cur):
    cur.execute("SELECT name, active, nodes, connections FROM workflow_entity WHERE id = %s", (WID,))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise Halt(f"workflow {WID} not found")
    return rows[0]


def retire(cur, apply, backup_dir=BACKUP_DIR):
    """Plan (any halt writes nothing), then on apply back up, update in one
    transaction, read back, commit. Returns a result dict."""
    wname, active, nodes, conns = _fetch(cur)
    wf = {"id": WID, "name": wname, "active": active, "nodes": nodes, "connections": conns}
    new_nodes, changed, old = rewrite(wf)
    res = {"id": WID, "name": NAME, "write_nodes": write_nodes(nodes), "changed": changed,
           "old": old, "new_nodes": new_nodes, "nodes": nodes, "connections": conns}
    if not apply or not changed:
        return res

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    backup = backup_dir / f"n8n_ro4_{stamp}.json"
    backup.write_text(json.dumps(
        {"backed_up_at": stamp, "workflows": {WID: {"workflow": NAME, "nodes": old}}},
        indent=1), encoding="utf-8")
    log(f"old node values backed up to {backup}")

    cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now() WHERE id = %s',
                (json.dumps(new_nodes), WID))
    _, active2, stored, conns2 = _fetch(cur)
    _, changed2, _ = rewrite({"id": WID, "name": NAME, "active": active2,
                              "nodes": stored, "connections": conns2})
    if (active2 or changed2 or conns2 != conns
            or blanked(stored, changed) != blanked(nodes, changed)
            or len(stored) != len(nodes)):
        cur.connection.rollback()
        raise Halt(f"readback of {NAME!r} does not match what was written; rolled back")
    cur.connection.commit()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="default")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        sys.exit("choose one of --apply or --dry-run")

    rc, out = node_test(new_code(), LOADER)
    log(f"offline node test of the retirement code: {'PASS' if rc == 0 else 'FAIL'} {out}")
    if rc != 0:
        sys.exit("HALT: node test failed; nothing written")

    import psycopg2
    from _db import ENV
    nc = psycopg2.connect(
        host="localhost", port=int(ENV.get("PG_PORT", "5432")),
        dbname="n8ndb", user=ENV.get("PG_USER"), password=ENV.get("PG_PASSWORD"))
    cur = nc.cursor()
    try:
        r = retire(cur, args.apply)
    except Halt as e:
        nc.rollback()
        sys.exit(f"HALT: {e}")
    log(f"{r['id']} {r['name']}: Code node {CODE_NODE!r}")
    log(f"    write nodes (with query text): {r['write_nodes']}")
    extras = [c for c in r["changed"] if c != CODE_NODE]
    log(f"    write nodes NOT downstream of the Code node (also retired): {extras or 'none'}")
    log(f"    {'to change: ' + str(r['changed']) if r['changed'] else 'already retired; nothing to change'}")
    by = {n["name"]: n["parameters"] for n in r["new_nodes"]}
    for c in r["changed"]:
        log(f"    new value of {c!r}: {(by[c].get('jsCode') or by[c].get('query')).strip()}")
    if not r["changed"]:
        log("already retired; nothing written, no backup")
    elif not args.apply:
        log("DRY RUN: nothing written")
    else:
        log("APPLIED; readback confirms every other part of the nodes list is unchanged")
    nc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
