"""Refresh the map data: run Workflow 1 if its inputs have moved, then export.

Usage:
    python scripts/refresh_map.py --check   read-only: is the live map (the
                                            latest.json at git HEAD) behind?
                                            Exit 0 fresh, 1 behind. Writes
                                            nothing at all.
    python scripts/refresh_map.py           the refresh (see below)

Default mode:
  1. If a completed W1 run exists and no W1 input table was loaded after it,
     W1 is not run again; the latest run is exported.
  2. Otherwise W1 runs first (w1_run.main). If w1_run refuses (for example a
     second run on the same day) its message is passed on and this stops with
     a non-zero exit.
  3. export_map_data.main() writes data/signals and data/boundaries (local
     files only).
  4. A short summary is printed. Nothing is committed and nothing is pushed.
     The push is a separate, approved step: python scripts/push.py

W1 input tables are discovered, not listed: every real table name in
information_schema that appears in a W1 step file, less the outputs
(staging_*) and la_boundaries. Of those, the ones with a loaded_at column are
tested for loads after the latest complete run's run_date. Every W1 source
table has one (checked 2026-10-08); a table without one is reported as not
checkable rather than silently ignored.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_readonly_conn  # noqa: E402
import export_map_data  # noqa: E402
import w1_contract_check  # noqa: E402
import w1_run  # noqa: E402
from w1_steps import STEPS, read_step  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
LATEST_JSON = REPO / "data" / "signals" / "latest.json"
LATEST_REL = "data/signals/latest.json"
SIGNALS_REL = "data/signals/staging_la_signals_latest.json"
SIGNALS_JSON = REPO / SIGNALS_REL
PUSH_LINE = "Not pushed. Review, then: python scripts/push.py"
# Outputs and reference geometry, not inputs that make a run stale.
EXCLUDED_EXACT = {"la_boundaries"}
EXCLUDED_PREFIX = ("staging_",)


def is_excluded(name):
    return name in EXCLUDED_EXACT or name.startswith(EXCLUDED_PREFIX)


def table_tokens(sql, names):
    """Names from `names` that appear as whole identifiers in the SQL."""
    return {w for w in re.findall(r"[a-z_][a-z0-9_]*", sql.lower())
            if w in names}


def _real_tables(cur):
    """{base table: has a loaded_at column} for schema public, read-only."""
    cur.execute("""
        SELECT t.table_name,
               EXISTS (SELECT 1 FROM information_schema.columns c
                        WHERE c.table_schema = t.table_schema
                          AND c.table_name = t.table_name
                          AND c.column_name = 'loaded_at')
          FROM information_schema.tables t
         WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
    """)
    return dict(cur.fetchall())


def _w1_tables(real):
    used = set()
    for step in STEPS:
        used |= table_tokens(read_step(step), set(real))
    return {t for t in used if not is_excluded(t)}


def input_tables(cur):
    """(table, has_loaded_at) for each W1 input base table, read-only."""
    real = _real_tables(cur)
    return sorted((t, real[t]) for t in _w1_tables(real))


def export_only_tables(cur):
    """(table, has_loaded_at) for tables the export reads that W1 does not.

    Found by the same token match over export_map_data.py. They never make W1
    stale (the export re-reads them live); they only make the map behind.
    """
    real = _real_tables(cur)
    src = Path(export_map_data.__file__).read_text(encoding="utf-8")
    used = {t for t in table_tokens(src, set(real)) if not is_excluded(t)}
    return sorted((t, real[t]) for t in used - _w1_tables(real))


def latest_complete_run(cur):
    cur.execute("SELECT run_id, run_date FROM staging_runs "
                "WHERE status = 'complete' ORDER BY run_id DESC LIMIT 1")
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def stale_inputs(cur, run_date, tables=None):
    """One line per input table loaded after run_date, with its latest date."""
    out = []
    for table, has_col in (input_tables(cur) if tables is None else tables):
        if not has_col:
            out.append(f"{table}: no loaded_at column, cannot be checked")
            continue
        cur.execute(f"SELECT MAX(loaded_at) FROM {table}")  # name is from information_schema
        latest = cur.fetchone()[0]
        if latest is not None and latest > run_date:
            out.append(f"{table} loaded after the run (latest {latest:%Y-%m-%d %H:%M})")
    return out


def head_published():
    """(info, error): the latest.json committed at git HEAD, which is what the
    live map reads. info is (run_id, generated_at) or None; error is a message
    when git or the file is unavailable."""
    try:
        r = subprocess.run(["git", "show", f"HEAD:{LATEST_REL}"], cwd=REPO,
                           capture_output=True, text=True, encoding="utf-8")
    except (FileNotFoundError, OSError) as e:
        return None, f"git is unavailable ({e}); cannot read the published map"
    if r.returncode != 0:
        return None, (f"{LATEST_REL} is not readable at git HEAD; cannot "
                      "tell what the live map shows")
    try:
        d = json.loads(r.stdout)
        return (int(d["run_id"]),
                datetime.datetime.fromisoformat(d["generated_at"])), None
    except (ValueError, KeyError, TypeError) as e:
        return None, f"{LATEST_REL} at git HEAD is malformed ({e})"


def local_exported(path=LATEST_JSON):
    """(run_id, generated_at) of the local working latest.json, or None."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return (int(d["run_id"]),
                datetime.datetime.fromisoformat(d["generated_at"]))
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None


def staleness(cur, published, local=None, published_at=None):
    """Reasons the live map is behind. Empty means fresh.

    published: run_id in latest.json at git HEAD (None if there is none).
    published_at: its generated_at; export-only tables loaded after it count.
    local: (run_id, generated_at) of the local working file, if any.
    """
    run = latest_complete_run(cur)
    if run is None:
        return ["no completed W1 run exists"]
    run_id, run_date = run
    msgs = []
    if published is None:
        msgs.append(f"no published map file (latest complete run is {run_id})")
    elif published < run_id:
        msgs.append(f"published run {published} is older than the latest "
                    f"complete run {run_id}")
    if local is not None and published is not None and (
            local[0] > published
            or (published_at is not None and local[1] > published_at)):
        msgs.append(f"exported but not pushed: run {local[0]} is exported "
                    f"locally; the live map still shows run {published}")
    msgs += [m for m in stale_inputs(cur, run_date)
             if "cannot be checked" not in m]
    if published_at is not None:
        msgs += [m + " (export-only input)" for m in
                 stale_inputs(cur, published_at, export_only_tables(cur))
                 if "cannot be checked" not in m]
    return msgs


def changed_cells(old, new):
    """{field: number of areas whose value differs}, areas matched on lad24cd."""
    before = {r["lad24cd"]: r for r in old}
    counts = {}
    for r in new:
        o = before.get(r["lad24cd"])
        if o is None:
            continue
        for k, v in r.items():
            if k in o and o[k] != v:
                counts[k] = counts.get(k, 0) + 1
    return counts


def export_summary():
    latest = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    new = json.loads(SIGNALS_JSON.read_text(encoding="utf-8"))["signals"]
    lines = [f"Run exported: {latest['run_id']}",
             f"Areas: {len(new)} (296 expected)"]
    if len(new) != 296:
        lines.append("WARNING: area count is not 296")
    r = subprocess.run(["git", "show", f"HEAD:{SIGNALS_REL}"], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        lines.append("Changes: no committed version to compare with")
        return lines
    old = json.loads(r.stdout)["signals"]
    counts = changed_cells(old, new)
    if not counts:
        lines.append("Changes against git HEAD: none in any exported field")
    else:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        lines.append(f"Changes against git HEAD: {sum(counts.values())} cells "
                     f"in {len(counts)} fields; top {len(top)}:")
        lines += [f"  {k}: {n}" for k, n in top]
    return lines


def _check():
    conn = get_readonly_conn()
    try:
        cur = conn.cursor()
        head, err = head_published()
        if err:
            print(f"Behind: {err}")
            return 1
        msgs = staleness(cur, head[0], local_exported(), head[1])
        run = latest_complete_run(cur)
        unchecked = [t for t, has in input_tables(cur) + export_only_tables(cur)
                     if not has]
    finally:
        conn.close()
    print(f"Published (git HEAD): run {head[0]} ({head[1]:%Y-%m-%d %H:%M})")
    if run:
        print(f"Latest complete run: {run[0]} ({run[1]:%Y-%m-%d %H:%M})")
    if unchecked:
        print("Not checkable (no loaded_at): " + ", ".join(unchecked))
    if not msgs:
        print("Fresh: the published map is current.")
        return 0
    print("Behind:")
    for m in msgs:
        print(f"  - {m}")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="report staleness, write nothing; exit 1 if behind")
    args = ap.parse_args(argv)
    if args.check:
        return _check()

    conn = get_readonly_conn()
    try:
        cur = conn.cursor()
        run = latest_complete_run(cur)
        stale = stale_inputs(cur, run[1]) if run else []
    finally:
        conn.close()

    if run is not None and not stale:
        print(f"latest run {run[0]} is current; W1 not run again")
    else:
        if run is None:
            print("No completed W1 run; running W1")
        else:
            print("Inputs loaded after the latest run; running W1:")
            for m in stale:
                print(f"  - {m}")
        rc = w1_run.main([])
        if rc:
            print("W1 did not run; stopping before the export.",
                  file=sys.stderr)
            return rc

    export_map_data.main()
    print()
    for line in export_summary():
        print(line)
    print(PUSH_LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
