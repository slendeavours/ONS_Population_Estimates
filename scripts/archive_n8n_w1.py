"""Archive the n8n Workflow 1 now that W1 runs from the repo.

W1 lives in sql/w1 and runs with scripts/w1_run.py. This one-shot script backs
up the old n8n workflow row, then archives and renames it so nobody runs it.

Preview by default (writes nothing). With --commit:
  1. backs up id, name, nodes, connections, active, isArchived as JSON to
     docs/n8n-era-source-docs/ (outside this repo, never committed);
  2. in one transaction, sets isArchived = true, active = false and renames it;
  3. reads the row back and prints it.
Only this one row is touched. Nothing is deleted.

Usage:
    python scripts/archive_n8n_w1.py            # preview
    python scripts/archive_n8n_w1.py --commit
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

W1_ID = "IrrglXLYcphSg5bC"
NEW_NAME = "DO NOT RUN - replaced by scripts/w1_run.py (W1)"
BACKUP_DIR = Path("C:/Users/slewi/ucws-repo/docs/n8n-era-source-docs")
COLS = 'id, name, active, "isArchived", nodes, connections'


def n8n_conn():
    return psycopg2.connect(
        host="localhost", port=int(ENV.get("PG_PORT", "5432")),
        dbname="n8ndb", user=ENV.get("PG_USER"), password=ENV.get("PG_PASSWORD"))


def read_row(cur):
    cur.execute(f"SELECT {COLS} FROM workflow_entity WHERE id = %s", (W1_ID,))
    row = cur.fetchone()
    if row is None:
        sys.exit(f"workflow {W1_ID} not found")
    return dict(zip(["id", "name", "active", "isArchived", "nodes", "connections"], row))


def summary(r):
    nodes = r["nodes"] if isinstance(r["nodes"], list) else json.loads(r["nodes"])
    return (f"id={r['id']} name={r['name']!r} active={r['active']} "
            f"isArchived={r['isArchived']} nodes={len(nodes)}")


def main(argv=None, conn=None, backup_dir=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args(argv)
    backup_dir = Path(backup_dir) if backup_dir else BACKUP_DIR
    conn = conn or n8n_conn()
    cur = conn.cursor()
    row = read_row(cur)
    stamp = f"{datetime.date.today():%Y-%m-%d}"
    backup = backup_dir / f"W1_n8n_workflow_backup_{stamp}.json"
    print("current :", summary(row))
    if row["isArchived"] and row["name"] == NEW_NAME:
        print("already archived; nothing to do")
        return 0
    print(f"would back up to {backup}")
    print(f"would set isArchived=true, active=false, name={NEW_NAME!r}")
    if not args.commit:
        print("PREVIEW only; nothing written. Re-run with --commit.")
        return 0
    if backup.exists():
        print(f"ERROR: backup already exists, refusing to overwrite: {backup}")
        return 1
    text = json.dumps(row, indent=2, default=str)
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        fh = open(backup, "x", encoding="utf-8")
    except FileExistsError:
        print(f"ERROR: backup already exists, refusing to overwrite: {backup}")
        return 1
    try:
        with fh:
            fh.write(text)
    except BaseException:
        # remove only the partial file this run created
        backup.unlink(missing_ok=True)
        raise
    print(f"backup written: {backup}")
    try:
        cur.execute('UPDATE workflow_entity SET "isArchived" = true, active = false, '
                    'name = %s, "updatedAt" = now() WHERE id = %s', (NEW_NAME, W1_ID))
        if cur.rowcount != 1:
            raise RuntimeError(f"expected 1 row updated, got {cur.rowcount}")
        after = read_row(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print("read-back:", summary(after))
    return 0


if __name__ == "__main__":
    sys.exit(main())
