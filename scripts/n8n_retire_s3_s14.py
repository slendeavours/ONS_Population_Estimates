"""Retire the two n8n loader workflows for S3 (ONS mid-year population) and
S14 (DWP LHA rates and the BRMA crosswalk). Both are inactive, but both can
still run and write:

  S3  "ONS Population Estimates (S3)": NOMIS API plus a chat-made supplement CSV,
      upserting la_population with ON CONFLICT (lad24cd), a key the table no
      longer has (it is (lad24cd, reference_year)); the workflow would first run
      CREATE TABLE IF NOT EXISTS and write a run-log row.
  S14 "LHA Rates + List of Rents (S14)": two branches. The CSV branch ends in a
      Code node that is only the n8n template stub; the manual-trigger branch
      reads la_boundaries, calls Postcodes.io and scrapes the VOA lha-direct
      site once per authority, and parses the HTML in "Parse VOA Response" before
      CREATE TABLE IF NOT EXISTS la_lha_rates and an upsert into it. Its table
      does not exist; neither live S14 table (brma_lha_rates, la_brma_mapping)
      is named in it.

In each workflow the Code node(s) that process the fetched data have their
jsCode replaced by a hard stop that names the table and the loader to use.
From each workflow's `connections` the script proves that every Postgres node
that writes is downstream of a replaced Code node (so it cannot run once the
Code node throws). A write node that is not downstream is retired too: its
query becomes a SELECT that raises. A Postgres node with an executeQuery
operation and no query text at all runs an empty query and cannot write; six of
the S14 nodes are like that (listed, not changed). Nothing else changes
(asserted by comparing the nodes list with the replaced values blanked, before
and after). The workflows are never run and never activated. The fetch nodes
upstream of a replaced Code node are not touched (the brief names only the
Code and write nodes), so a manual run would still fetch before it throws.

A third workflow, "VOA/DWP LHA Rates (S14)" (sk295Li2DyMqDSYY), has no nodes:
it is read and reported, never changed; if it ever gains nodes the script halts.

Follows scripts/n8n_retire_no_loader_sources.py: dry run by default; the backup
of the old values is written only on --apply; the UPDATE is read back inside
the transaction (a mismatch rolls back); idempotent on the RETIRED prefix.

Usage:
    python scripts/n8n_retire_s3_s14.py --dry-run    # default
    python scripts/n8n_retire_s3_s14.py --apply
"""
import argparse
import copy
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from n8n_retire_no_loader_sources import (  # noqa: E402
    WRITE_RE, Halt, blanked, downstream, log, node_test)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
BACKUP_DIR = REPO / "build_reports" / "w1_node_backups"

DATE = "2026-10-11"
PREFIX = f'throw new Error("RETIRED {DATE}'
QUERY_PREFIX = f'SELECT 1/0 AS "RETIRED {DATE}'

# id -> (workflow name, Code nodes to replace, defect markers (any one of them
# must still be present in a node url, query or jsCode of the workflow), new
# loader file under scripts/). The loaders are named before they exist: the
# message is the pointer.
WORKFLOWS = {
    "AlIOqA6ARCRyNDai": ("ONS Population Estimates (S3)", ("Process Population Data",),
                         ("supplement",), "s3_mye_editions.py"),
    "S44UCK95djqE3aLw": ("LHA Rates + List of Rents (S14)",
                         ("Process Rate Data", "Parse VOA Response"),
                         ("lha-direct", "la_lha_rates"), "s14_lha_editions.py"),
}
# workflows that must stay exactly as they are: id -> name (no nodes)
NO_NODES = {"sk295Li2DyMqDSYY": "VOA/DWP LHA Rates (S14)"}
# what each workflow did (the first part of the stop message)
WHAT = {
    "AlIOqA6ARCRyNDai": "this workflow loaded la_population from the NOMIS API and a chat-made "
                        "supplement CSV, with an ON CONFLICT (lad24cd) upsert that no longer "
                        "matches the table key.",
    "S44UCK95djqE3aLw": "this workflow scraped the VOA lha-direct site into la_lha_rates, a table "
                        "that does not exist; the live S14 tables are brma_lha_rates and "
                        "la_brma_mapping.",
}
# Postgres operations (other than executeQuery) that write
WRITE_OPERATIONS = {"insert", "update", "upsert", "delete", "deleteTable", "create"}
READ_OPERATIONS = {"select"}


def new_code(wid):
    loader = WORKFLOWS[wid][3]
    msg = f"RETIRED {DATE}: {WHAT[wid]} Use python scripts/{loader} (preview by default)."
    return "throw new Error(" + json.dumps(msg) + ");\n"


def new_query(wid):
    return f'{QUERY_PREFIX} use scripts/{WORKFLOWS[wid][3]}"'


def _is_pg(n):
    return n["type"].endswith(".postgres")


def _query(n):
    q = n["parameters"].get("query")
    return q if isinstance(q, str) and q.strip() else None


def write_nodes(nodes):
    """Names of Postgres nodes that write. A node with query text writes if the
    text has a write keyword. A node with no (or blank) query text runs nothing
    when its operation is executeQuery; any other write operation counts as a
    write; an operation not known to be a read halts."""
    out = []
    for n in nodes:
        if not _is_pg(n):
            continue
        q = _query(n)
        if q is not None:
            if WRITE_RE.search(q):
                out.append(n["name"])
            continue
        op = n["parameters"].get("operation", "executeQuery")
        if op == "executeQuery" or op in READ_OPERATIONS:
            continue
        if op in WRITE_OPERATIONS:
            out.append(n["name"])
            continue
        raise Halt(f"Postgres node {n['name']!r} has no query text and operation {op!r}; "
                   "cannot judge whether it writes")
    return out


def empty_query_nodes(nodes):
    """Postgres executeQuery nodes with no query text: they run an empty query."""
    return [n["name"] for n in nodes if _is_pg(n) and _query(n) is None
            and n["parameters"].get("operation", "executeQuery") == "executeQuery"]


def _one(nodes, name):
    found = [n for n in nodes if n["name"] == name]
    if len(found) != 1:
        raise Halt(f"expected exactly one node {name!r}, found {len(found)}")
    return found[0]


def plan(workflow):
    """(Code node names, write nodes not downstream of any of them)."""
    code_names = list(WORKFLOWS[workflow["id"]][1])
    for c in code_names:
        _one(workflow["nodes"], c)
    down = set()
    for c in code_names:
        down |= downstream(workflow["connections"], c)
    extras = [w for w in write_nodes(workflow["nodes"]) if w not in down]
    return code_names, extras


def marker_found(nodes, markers):
    """Which of `markers` appear in any node's url, query or jsCode."""
    texts = []
    for n in nodes:
        for key in ("url", "query", "jsCode"):
            v = n["parameters"].get(key)
            if isinstance(v, str):
                texts.append(v)
    return [m for m in markers if any(m in t for t in texts)]


def rewrite(workflow):
    """-> (new nodes, names changed, {name: old value}). Halts on anything not
    as surveyed. A workflow already retired returns no changes."""
    wid = workflow["id"]
    name, code_names, markers, _ = WORKFLOWS[wid]
    if workflow["name"] != name:
        raise Halt(f"workflow {wid} is {workflow['name']!r}, expected {name!r}")
    if workflow["active"]:
        raise Halt(f"workflow {name!r} is active; refusing to edit it")
    nodes = workflow["nodes"]
    code_names, extras = plan(workflow)
    new_nodes = copy.deepcopy(nodes)
    by = {n["name"]: n for n in new_nodes}
    old, changed = {}, []
    want = new_code(wid)

    for c in code_names:
        cur = by[c]["parameters"].get("jsCode")
        if cur is None:
            raise Halt(f"node {c!r} has no jsCode")
        if cur == want:
            continue
        if cur.startswith(PREFIX):
            raise Halt(f"{c!r} carries a different retirement text; not touching it")
        if not marker_found(nodes, markers):
            raise Halt(f"none of the defect markers {list(markers)} is present in any node of "
                       f"{name!r}; the workflow was edited since the survey, refusing to retire it")
        old[c] = cur
        by[c]["parameters"]["jsCode"] = want
        changed.append(c)

    for x in extras:
        p = by[x]["parameters"]
        if _query(by[x]) is None:
            raise Halt(f"write node {x!r} is not downstream of a replaced Code node and has no "
                       "query text to replace; retire it by hand")
        old[x] = p["query"]
        p["query"] = new_query(wid)
        changed.append(x)

    assert blanked(new_nodes, changed) == blanked(nodes, changed), \
        "change is not confined to the replaced values"
    assert sorted(a["name"] for a, b in zip(nodes, new_nodes) if a != b) == sorted(changed)
    return new_nodes, changed, old


def _fetch(cur, wid):
    cur.execute("SELECT name, active, nodes, connections FROM workflow_entity WHERE id = %s", (wid,))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise Halt(f"workflow {wid} not found")
    return rows[0]


def no_nodes_status(cur):
    """[(id, name, active, node count)] for the workflows that must stay empty.
    Halts when one is not as surveyed (missing, renamed, active, has nodes)."""
    out = []
    for wid, want in NO_NODES.items():
        name, active, nodes, _ = _fetch(cur, wid)
        if name != want or active or len(nodes) != 0:
            raise Halt(f"workflow {wid} is {name!r} active={active} with {len(nodes)} nodes; "
                       f"expected {want!r}, inactive, no nodes; not touching it")
        out.append((wid, name, active, len(nodes)))
    return out


def retire(cur, apply, backup_dir=BACKUP_DIR):
    """Plan both (any halt writes nothing), then on apply back up, update in one
    transaction, read back, commit. Returns one result dict per workflow."""
    no_nodes_status(cur)
    results = []
    for wid, (name, code_names, markers, loader) in WORKFLOWS.items():
        try:
            wname, active, nodes, conns = _fetch(cur, wid)
        except Halt:
            raise Halt(f"workflow {wid} / {name!r} not found")
        wf = {"id": wid, "name": wname, "active": active, "nodes": nodes, "connections": conns}
        new_nodes, changed, old = rewrite(wf)
        results.append({"id": wid, "name": name, "code_nodes": code_names,
                        "write_nodes": write_nodes(nodes), "empty_query_nodes": empty_query_nodes(nodes),
                        "changed": changed, "old": old, "new_nodes": new_nodes,
                        "nodes": nodes, "connections": conns})
    todo = [r for r in results if r["changed"]]
    if not apply or not todo:
        return results

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    backup = backup_dir / f"n8n_s3_s14_{stamp}.json"
    backup.write_text(json.dumps(
        {"backed_up_at": stamp,
         "workflows": {r["id"]: {"workflow": r["name"], "nodes": r["old"]} for r in todo}},
        indent=1), encoding="utf-8")
    log(f"old node values backed up to {backup}")

    for r in todo:
        cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now() WHERE id = %s',
                    (json.dumps(r["new_nodes"]), r["id"]))
    for r in todo:
        _, active, stored, conns = _fetch(cur, r["id"])
        again, changed2, _ = rewrite({"id": r["id"], "name": r["name"], "active": active,
                                      "nodes": stored, "connections": conns})
        if (active or changed2 or conns != r["connections"]
                or blanked(stored, r["changed"]) != blanked(r["nodes"], r["changed"])
                or len(stored) != len(r["nodes"])):
            cur.connection.rollback()
            raise Halt(f"readback of {r['name']!r} does not match what was written; rolled back")
    cur.connection.commit()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="default")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        sys.exit("choose one of --apply or --dry-run")

    for wid, spec in WORKFLOWS.items():
        rc, out = node_test(new_code(wid), spec[3])
        log(f"offline node test of the retirement code for {spec[3]}: {'PASS' if rc == 0 else 'FAIL'} {out}")
        if rc != 0:
            sys.exit("HALT: node test failed; nothing written")

    import psycopg2
    from _db import ENV
    nc = psycopg2.connect(
        host="localhost", port=int(ENV.get("PG_PORT", "5432")),
        dbname="n8ndb", user=ENV.get("PG_USER"), password=ENV.get("PG_PASSWORD"))
    cur = nc.cursor()
    try:
        results = retire(cur, args.apply)
        empties = no_nodes_status(cur)
    except Halt as e:
        nc.rollback()
        sys.exit(f"HALT: {e}")
    for r in results:
        log(f"{r['id']} {r['name']}: Code nodes {r['code_nodes']}")
        log(f"    write nodes (with query text): {r['write_nodes']}")
        log(f"    executeQuery nodes with no query text (cannot write, not changed): {r['empty_query_nodes']}")
        extras = [c for c in r["changed"] if c not in r["code_nodes"]]
        log(f"    write nodes NOT downstream of a replaced Code node (also retired): {extras or 'none'}")
        log(f"    {'to change: ' + str(r['changed']) if r['changed'] else 'already retired; nothing to change'}")
        by = {n["name"]: n["parameters"] for n in r["new_nodes"]}
        for c in r["changed"]:
            log(f"    new value of {c!r}: {(by[c].get('jsCode') or by[c].get('query')).strip()}")
    for wid, name, active, count in empties:
        log(f"{wid} {name!r}: active={active}, {count} nodes; recorded, nothing to change")
    if not any(r["changed"] for r in results):
        log("both already retired; nothing written, no backup")
    elif not args.apply:
        log("DRY RUN: nothing written")
    else:
        log("APPLIED; readback confirms every other part of each nodes list is unchanged")
    nc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
