"""Apply the like-for-like national TA year-on-year to Workflow 1.

The National Aggregates node divided the all-reporting current total by the
all-reporting prior-year total, so authorities present in only one of the two
quarters moved the percentage: run 22 reported +13.29% where the authorities
reporting in both quarters give about +3.14%. The node now computes the
percentage over the matched set and records the matched count and totals next
to it. ta_households_current and ta_households_prev_year keep their meaning
(England totals of authorities reporting in each period) and are documented as
not comparable with each other.

Three steps, each additive and read back:
  1. ALTER TABLE staging_national ADD COLUMN IF NOT EXISTS (three columns);
  2. back up and replace the National Aggregates node SQL, and add the three
     columns to the Create Staging Tables node so a fresh database matches;
  3. leave the re-run to scripts/w1_rerun.py (a new run; earlier runs stay).

Usage:
    python scripts/w1_national_ta_like_for_like.py --dry-run
    python scripts/w1_national_ta_like_for_like.py --apply
    python scripts/w1_rerun.py
    python scripts/verify_national_ta.py
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402
from w1_apply_period_fix import BACKUP_DIR, REPO, W1_ID, log, n8n_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEW_SQL = REPO / "sql" / "w1_national_aggregates_like_for_like.sql"
NEW_COLUMNS = [("ta_matched_authorities", "INTEGER"),
               ("ta_households_current_matched", "INTEGER"),
               ("ta_households_prev_year_matched", "INTEGER")]
DDL_ANCHOR = "    ta_yoy_pct                      NUMERIC(8,2),\n"
DDL_ADD = "".join(f"    {n:<32}{t},\n" for n, t in NEW_COLUMNS)


def patched_create_tables(sql):
    if all(n in sql for n, _ in NEW_COLUMNS):
        return sql
    if sql.count(DDL_ANCHOR) != 1:
        sys.exit("HALT: Create Staging Tables node does not carry exactly one "
                 "staging_national ta_yoy_pct line; edit by hand.")
    return sql.replace(DDL_ANCHOR, DDL_ANCHOR + DDL_ADD)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        sys.exit("choose --apply or --dry-run")

    new_sql = NEW_SQL.read_text(encoding="utf-8")
    nc = n8n_conn()
    cur = nc.cursor()
    cur.execute('SELECT nodes FROM workflow_entity WHERE id = %s', (W1_ID,))
    nodes = cur.fetchone()[0]
    by = {n["name"]: n for n in nodes}
    for name in ("National Aggregates", "Create Staging Tables"):
        if name not in by:
            sys.exit(f"HALT: node '{name}' not found in Workflow 1")

    old_na = by["National Aggregates"]["parameters"]["query"]
    old_ct = by["Create Staging Tables"]["parameters"]["query"]
    new_ct = patched_create_tables(old_ct)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    backup = BACKUP_DIR / f"w1_node_backup_{stamp}.json"
    backup.write_text(json.dumps(
        {"backed_up_at": stamp, "workflow_id": W1_ID,
         "nodes": {"National Aggregates": old_na,
                   "Create Staging Tables": old_ct}}, indent=1), encoding="utf-8")
    log(f"previous SQL backed up to {backup.relative_to(REPO)}")

    if args.dry_run:
        log(f"DRY RUN: National Aggregates {'would change' if old_na != new_sql else 'unchanged'}; "
            f"Create Staging Tables {'would change' if old_ct != new_ct else 'unchanged'}; "
            f"would add columns {[n for n, _ in NEW_COLUMNS]}")
        nc.close()
        return 0

    conn = get_conn()
    conn.autocommit = False
    c2 = conn.cursor()
    for n, t in NEW_COLUMNS:
        c2.execute(f"ALTER TABLE staging_national ADD COLUMN IF NOT EXISTS {n} {t}")
    conn.commit()
    log("staging_national carries the three new columns")

    by["National Aggregates"]["parameters"]["query"] = new_sql
    by["Create Staging Tables"]["parameters"]["query"] = new_ct
    cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now() '
                'WHERE id = %s', (json.dumps(nodes), W1_ID))
    nc.commit()
    cur.execute('SELECT nodes FROM workflow_entity WHERE id = %s', (W1_ID,))
    stored = {n["name"]: n["parameters"].get("query") for n in cur.fetchone()[0]}
    if stored["National Aggregates"] != new_sql or stored["Create Staging Tables"] != new_ct:
        sys.exit("HALT: readback does not match what was written")
    log("readback confirms both nodes; now run: python scripts/w1_rerun.py")
    nc.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
