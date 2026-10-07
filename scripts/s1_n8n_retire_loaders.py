"""Retire the two faulty S1 loader Code nodes in the n8n workflow
"MHCLG Statutory Homelessness (S1)" (id r8QRpmOGaBBvkpmg, inactive).

  * 'Merge & Build Batch': reads columns by fixed __EMPTY_N positions and turns
    every non-numeric cell (a suppressed cell) into 0 (`: 0`, `?? 0`); feeds
    'Upsert Data', which overwrites live rows.
  * 'Process Homelessness Data': parses a CSV and turns suppressed cells into 0
    (`parseInt(...) || 0`).

The jsCode of exactly those two nodes is replaced by a single hard stop that
says why and points to `python scripts/s1_editions.py load-new`. Nothing else in
the workflow changes (asserted by comparing the nodes list with the two jsCode
values blanked, before and after). The workflow is never run.

Follows scripts/w1_national_ta_like_for_like.py: dry run by default; the backup
of both old codes is written only on --apply; the UPDATE is read back;
idempotent (a second --apply says already retired and writes nothing).

Usage:
    python scripts/s1_n8n_retire_loaders.py --dry-run    # default, writes nothing
    python scripts/s1_n8n_retire_loaders.py --apply
"""
import argparse
import copy
import datetime
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from w1_apply_period_fix import BACKUP_DIR, REPO, log, n8n_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WF_ID = "r8QRpmOGaBBvkpmg"
WF_NAME = "MHCLG Statutory Homelessness (S1)"
# node name -> text that must still be in the old code (its defect marker)
NODES = {
    "Merge & Build Batch": "?? 0",
    "Process Homelessness Data": "|| 0",
}
PREFIX = 'throw new Error("RETIRED 2026-10-07'

MESSAGE = ("RETIRED 2026-10-07: this loader is switched off. It stored suppressed cells "
           "as 0 instead of NULL, read columns by fixed position (__EMPTY_N) so a layout "
           "change silently shifts the data, and wrote straight to the live table, "
           "overwriting existing rows. Use: python scripts/s1_editions.py load-new "
           "(see docs/QUARTERLY_REFRESH.md, step 2).")
NEW_CODE = "throw new Error(" + json.dumps(MESSAGE) + ");\n"
assert NEW_CODE.startswith(PREFIX)

HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const item = { json: {}, binary: {} };
const fake = { all: () => [item], first: () => item, last: () => item, item,
               itemMatching: () => item, isExecuted: false };
const $input = fake;
const $ = () => fake;
let msg = null;
try { new Function('$input', '$', '$json', '$node', code)($input, $, {}, {}); }
catch (e) { if (e instanceof Error) msg = e.message; }
const ok = msg !== null && msg.includes('RETIRED') && msg.includes('load-new');
console.log(JSON.stringify({ threw: msg !== null, ok }));
process.exit(ok ? 0 : 1);
"""


def node_test(js):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "code.js").write_text(js, encoding="utf-8")
        (d / "h.js").write_text(HARNESS, encoding="utf-8")
        p = subprocess.run(["node", str(d / "h.js"), str(d / "code.js")],
                           capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()


def blanked(nodes):
    """nodes with the two target jsCode values blanked, as canonical JSON."""
    n = copy.deepcopy(nodes)
    for x in n:
        if x["name"] in NODES:
            x["parameters"]["jsCode"] = ""
    return json.dumps(n, sort_keys=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="default")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        sys.exit("choose one of --apply or --dry-run")

    rc, out = node_test(NEW_CODE)
    log(f"offline node test of the retirement code: {'PASS' if rc == 0 else 'FAIL'} {out}")
    if rc != 0:
        sys.exit("HALT: node test failed; nothing written")

    nc = n8n_conn()
    cur = nc.cursor()
    cur.execute("SELECT name, active, nodes FROM workflow_entity WHERE id = %s", (WF_ID,))
    rows = cur.fetchall()
    if len(rows) != 1 or rows[0][0] != WF_NAME:
        sys.exit(f"HALT: workflow {WF_ID} / {WF_NAME!r} not found as expected")
    _, active, nodes = rows[0]
    if active:
        sys.exit("HALT: workflow is active; refusing to edit it")
    by = {}
    for x in nodes:
        by.setdefault(x["name"], []).append(x)
    for name in NODES:
        if len(by.get(name, [])) != 1:
            sys.exit(f"HALT: expected exactly one node {name!r}, found {len(by.get(name, []))}")

    todo, old = [], {}
    for name, marker in NODES.items():
        code = by[name][0]["parameters"].get("jsCode")
        if code is None:
            sys.exit(f"HALT: node {name!r} has no jsCode")
        if code == NEW_CODE:
            log(f"{name}: already retired")
        elif code.startswith(PREFIX):
            sys.exit(f"HALT: {name!r} carries a different retirement text; not touching it")
        else:
            if marker not in code:
                sys.exit(f"HALT: {name!r} no longer contains its defect marker {marker!r}; "
                         "unexpected node version, refusing to retire it")
            log(f"{name}: defect marker {marker!r} present ({len(code)} chars); to retire")
            todo.append(name)
            old[name] = code

    if not todo:
        log("both nodes already retired; nothing written, no backup")
        nc.close()
        return 0

    new_nodes = copy.deepcopy(nodes)
    for x in new_nodes:
        if x["name"] in todo:
            x["parameters"]["jsCode"] = NEW_CODE
    assert blanked(new_nodes) == blanked(nodes), "change is not confined to the two jsCode values"
    changed = [a["name"] for a, b in zip(nodes, new_nodes) if a != b]
    assert sorted(changed) == sorted(todo)
    log(f"nodes list identical except jsCode of {changed} (18 nodes: {len(nodes)})")

    if not args.apply:
        log(f"DRY RUN: would back up and retire {todo}; nothing written")
        nc.close()
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    backup = BACKUP_DIR / f"s1_n8n_backup_{stamp}.json"
    backup.write_text(json.dumps(
        {"backed_up_at": stamp, "workflow_id": WF_ID, "workflow": WF_NAME,
         "nodes": old}, indent=1), encoding="utf-8")
    log(f"old node code backed up to {backup.relative_to(REPO)}")

    cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now() WHERE id = %s',
                (json.dumps(new_nodes), WF_ID))
    nc.commit()
    cur.execute("SELECT nodes FROM workflow_entity WHERE id = %s", (WF_ID,))
    stored = cur.fetchone()[0]
    sb = {x["name"]: x for x in stored}
    if (any(sb[n]["parameters"]["jsCode"] != NEW_CODE for n in NODES)
            or blanked(stored) != blanked(nodes) or len(stored) != len(nodes)):
        sys.exit("HALT: readback does not match what was written")
    log("readback confirms: both nodes retired, every other part of the nodes list unchanged")
    nc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
