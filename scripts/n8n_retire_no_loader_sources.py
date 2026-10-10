"""Retire the seven n8n loader workflows for the sources that had no loader in
the repo (S3b, S5, S7, S10, S12, S13, S17). All seven are inactive, but three
can still run (S3b's ONS API fetch, S7's ArcGIS fetch, S12's GOV.UK page
fetches) and each upserts over live rows. S7's workflow also runs
DELETE FROM la_code_lookup WHERE change_type != 'current' and re-inserts its
own March 2026 list, which would undo the July and August 2026 lookup fixes.

In each workflow the one Code node that processes the fetched data has its
jsCode replaced by a hard stop that names the table and the loader to use.
From each workflow's `connections` the script proves that every Postgres node
that writes is downstream of that Code node (so it cannot run once the Code
node throws). Any write node that is not downstream is retired too: its query
becomes a SELECT that raises. Nothing else changes (asserted by comparing the
nodes list with the replaced values blanked, before and after). The workflows
are never run and never activated.

Follows scripts/s1_n8n_retire_loaders.py: dry run by default; the backup of
the old values is written only on --apply; the UPDATE is read back inside the
transaction (a mismatch rolls back); idempotent on the RETIRED prefix.

Usage:
    python scripts/n8n_retire_no_loader_sources.py --dry-run    # default
    python scripts/n8n_retire_no_loader_sources.py --apply
"""
import argparse
import copy
import datetime
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
BACKUP_DIR = REPO / "build_reports" / "w1_node_backups"

DATE = "2026-10-10"
PREFIX = f'throw new Error("RETIRED {DATE}'
QUERY_PREFIX = f'SELECT 1/0 AS "RETIRED {DATE}'

# id -> (workflow name, Code node, defect marker the old code must still
# contain, new loader file under scripts/). The loaders are named before they
# exist: the message is the pointer.
WORKFLOWS = {
    "9QtgP8Gl3JPri6Um": ("Census 2021 TS054 Tenure (S3b)", "Process Tenure Data",
                         "|| 0", "s3b_tenure_editions.py"),
    "zCMCwdF24AZgkyna": ("Indices of Deprivation (S5)", "Process IMD Data",
                         "parseFloat", "s5_imd_editions.py"),
    "moZB3CYE96j4OcFp": ("LA Boundaries (S7)", "Process Boundaries",
                         "features", "s7_boundaries_editions.py"),
    "yeIcdi9WM8QoFVpC": ("Rough Sleeping Snapshot (S10)", "Process Rough Sleeping Data",
                         "|| 0", "s10_rough_sleeping_editions.py"),
    "gbgnThonagiTgvaR": ("LA Financial Stress (12)", "Process EFS + S114",
                         "NAME_TO_LAD24CD", "s12_financial_stress_editions.py"),
    "pQdnfy7IpJXdxzIM": ("Social housing waiting lists (S13)", "Process Waiting List Data",
                         "safeInt", "s13_lahs_editions.py"),
    "OYMTEK8A9j8bdpkf": ("SafeLives MARAC (S17)", "Process MARAC Data",
                         "parseFloat", "s17_marac_editions.py"),
}
# the table(s) each workflow upserted
TABLES = {
    "9QtgP8Gl3JPri6Um": "la_tenure_2021",
    "zCMCwdF24AZgkyna": "la_imd_2025",
    "moZB3CYE96j4OcFp": "la_boundaries",
    "yeIcdi9WM8QoFVpC": "la_rough_sleeping",
    "gbgnThonagiTgvaR": "la_efs_support and la_s114_notices",
    "pQdnfy7IpJXdxzIM": "la_housing_register",
    "OYMTEK8A9j8bdpkf": "marac_cases and la_pfa_mapping",
}
# nodes checked by name in S7 (they have to exist; whether they are retired
# separately follows from the graph)
S7_LOOKUP_NODES = ("Seed Code Lookup from Boundaries", "Delete Historical Code Changes",
                   "Insert Historical Code Changes")

WRITE_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|TRUNCATE)\b", re.I)

HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const loader = process.argv[3];
const item = { json: {}, binary: {} };
const fake = { all: () => [item], first: () => item, last: () => item, item,
               itemMatching: () => item, isExecuted: false };
let msg = null;
try { new Function('$input', '$', '$json', '$node', code)(fake, () => fake, {}, {}); }
catch (e) { if (e instanceof Error) msg = e.message; }
const ok = msg !== null && msg.includes('RETIRED') && msg.includes(loader);
console.log(JSON.stringify({ threw: msg !== null, ok }));
process.exit(ok ? 0 : 1);
"""


class Halt(Exception):
    pass


def log(m):
    print(f"{datetime.datetime.now():%H:%M:%S} {m}", flush=True)


def new_code(wid):
    loader = WORKFLOWS[wid][3]
    msg = (f"RETIRED {DATE}: this workflow loaded {TABLES[wid]} by upsert from a chat-made "
           f"CSV or a page with no edition history. Use python scripts/{loader} "
           "(preview by default).")
    return "throw new Error(" + json.dumps(msg) + ");\n"


def new_query(wid):
    return f'{QUERY_PREFIX} use scripts/{WORKFLOWS[wid][3]}"'


def node_test(js, loader):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "code.js").write_text(js, encoding="utf-8")
        (d / "h.js").write_text(HARNESS, encoding="utf-8")
        p = subprocess.run(["node", str(d / "h.js"), str(d / "code.js"), loader],
                           capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()


def downstream(connections, start):
    """Every node reachable from `start` along `connections` (start itself only
    if a cycle leads back to it)."""
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        for outs in (connections.get(n) or {}).values():
            for branch in outs or []:
                for link in branch or []:
                    t = link["node"]
                    if t not in seen:
                        seen.add(t)
                        stack.append(t)
    return seen


def write_nodes(nodes):
    """Names of Postgres nodes whose query writes."""
    out = []
    for n in nodes:
        if not n["type"].endswith(".postgres"):
            continue
        q = n["parameters"].get("query")
        if not isinstance(q, str):
            raise Halt(f"Postgres node {n['name']!r} has no query text; cannot judge whether it writes")
        if WRITE_RE.search(q):
            out.append(n["name"])
    return out


def _one(nodes, name):
    found = [n for n in nodes if n["name"] == name]
    if len(found) != 1:
        raise Halt(f"expected exactly one node {name!r}, found {len(found)}")
    return found[0]


def plan(workflow):
    """(code node name, write nodes not downstream of it)."""
    wid = workflow["id"]
    code_name = WORKFLOWS[wid][1]
    _one(workflow["nodes"], code_name)
    if wid == "moZB3CYE96j4OcFp":
        for name in S7_LOOKUP_NODES:
            _one(workflow["nodes"], name)
    down = downstream(workflow["connections"], code_name)
    extras = [w for w in write_nodes(workflow["nodes"]) if w not in down]
    return code_name, extras


def blanked(nodes, names):
    """nodes with the replaced values (jsCode or query) of `names` blanked, as
    canonical JSON."""
    n = copy.deepcopy(nodes)
    for x in n:
        if x["name"] in names:
            key = "jsCode" if "jsCode" in x["parameters"] else "query"
            x["parameters"][key] = ""
    return json.dumps(n, sort_keys=True)


def rewrite(workflow):
    """-> (new nodes, names changed, {name: old value}). Halts on anything not
    as surveyed. A workflow already retired returns no changes."""
    wid = workflow["id"]
    name, code_name, marker, _ = WORKFLOWS[wid]
    if workflow["name"] != name:
        raise Halt(f"workflow {wid} is {workflow['name']!r}, expected {name!r}")
    if workflow["active"]:
        raise Halt(f"workflow {name!r} is active; refusing to edit it")
    nodes = workflow["nodes"]
    code_name, extras = plan(workflow)
    new_nodes = copy.deepcopy(nodes)
    by = {n["name"]: n for n in new_nodes}
    old, changed = {}, []

    cur = by[code_name]["parameters"].get("jsCode")
    if cur is None:
        raise Halt(f"node {code_name!r} has no jsCode")
    want = new_code(wid)
    if cur == want:
        pass
    elif cur.startswith(PREFIX):
        raise Halt(f"{code_name!r} carries a different retirement text; not touching it")
    else:
        if marker not in cur:
            raise Halt(f"{code_name!r} no longer contains its defect marker {marker!r}; the node "
                       "was edited since the survey, refusing to retire it")
        old[code_name] = cur
        by[code_name]["parameters"]["jsCode"] = want
        changed.append(code_name)

    for x in extras:
        q = by[x]["parameters"]["query"]
        old[x] = q
        by[x]["parameters"]["query"] = new_query(wid)
        changed.append(x)

    assert blanked(new_nodes, changed) == blanked(nodes, changed), \
        "change is not confined to the replaced values"
    assert sorted(a["name"] for a, b in zip(nodes, new_nodes) if a != b) == sorted(changed)
    return new_nodes, changed, old


def retire(cur, apply, backup_dir=BACKUP_DIR):
    """Plan all seven (any halt writes nothing), then on apply back up, update
    in one transaction, read back, commit. Returns one result dict per workflow."""
    results, loaded = [], {}
    for wid, (name, code_name, marker, loader) in WORKFLOWS.items():
        cur.execute("SELECT name, active, nodes, connections FROM workflow_entity WHERE id = %s",
                    (wid,))
        rows = cur.fetchall()
        if len(rows) != 1:
            raise Halt(f"workflow {wid} / {name!r} not found")
        wname, active, nodes, conns = rows[0]
        wf = {"id": wid, "name": wname, "active": active, "nodes": nodes, "connections": conns}
        new_nodes, changed, old = rewrite(wf)
        loaded[wid] = wf
        results.append({"id": wid, "name": name, "code_node": code_name,
                        "write_nodes": write_nodes(nodes), "changed": changed,
                        "old": old, "new_nodes": new_nodes, "nodes": nodes})
    todo = [r for r in results if r["changed"]]
    if not apply or not todo:
        return results

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    backup = backup_dir / f"n8n_no_loader_sources_{stamp}.json"
    backup.write_text(json.dumps(
        {"backed_up_at": stamp,
         "workflows": {r["id"]: {"workflow": r["name"], "nodes": r["old"]} for r in todo}},
        indent=1), encoding="utf-8")
    log(f"old node values backed up to {backup}")

    for r in todo:
        cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now() WHERE id = %s',
                    (json.dumps(r["new_nodes"]), r["id"]))
    for r in todo:
        cur.execute("SELECT name, active, nodes, connections FROM workflow_entity WHERE id = %s",
                    (r["id"],))
        _, active, stored, conns = cur.fetchall()[0]
        again, changed2, _ = rewrite({"id": r["id"], "name": r["name"], "active": active,
                                      "nodes": stored, "connections": conns})
        if (changed2 or blanked(stored, r["changed"]) != blanked(r["nodes"], r["changed"])
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

    for wid, (_, _, _, loader) in WORKFLOWS.items():
        rc, out = node_test(new_code(wid), loader)
        log(f"offline node test of the retirement code for {loader}: {'PASS' if rc == 0 else 'FAIL'} {out}")
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
    except Halt as e:
        nc.rollback()
        sys.exit(f"HALT: {e}")
    for r in results:
        log(f"{r['id']} {r['name']}: Code node {r['code_node']!r}; write nodes {r['write_nodes']}")
        extras = [c for c in r["changed"] if c != r["code_node"]]
        log(f"    write nodes NOT downstream of the Code node (also retired): {extras or 'none'}")
        log(f"    {'to change: ' + str(r['changed']) if r['changed'] else 'already retired; nothing to change'}")
    if not any(r["changed"] for r in results):
        log("all seven already retired; nothing written, no backup")
    elif not args.apply:
        log("DRY RUN: nothing written")
    else:
        log("APPLIED; readback confirms every other part of each nodes list is unchanged")
    nc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
