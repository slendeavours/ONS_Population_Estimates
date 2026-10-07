"""Baseline capture/compare for the S1, S1b and RO4 loaders.

Evidence recorded: row count and a SHA-256 over rows (excluding loaded_at,
ordered by primary key) for each table, plus exit code and the normalised
GATE/PASS/FAIL lines of the status and verify scripts. Raw stdout hashes are
not used because output carries timestamps.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

TABLES = [
    "la_statutory_homelessness", "la_statutory_homelessness_editions",
    "la_homelessness_support_needs", "la_homelessness_support_needs_editions",
    "ro4_housing_expenditure", "ro4_housing_expenditure_editions",
]
COMMANDS = {
    "s1_status": ["s1_editions.py", "status"],
    "s1b_status": ["s1b_editions.py", "status"],
    "ro4_status": ["ro4_editions.py", "status"],
    "s1_verify": ["s1_editions_verify.py"],
    "s1b_verify": ["s1b_editions_verify.py"],
    "ro4_verify": ["ro4_editions_verify.py"],
}
_KEEP = re.compile(r"^\s*(GATE|\[PASS\]|\[FAIL\]|PASS|FAIL)")


def table_evidence(cur, table):
    cur.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY a.attname",
        (f"public.{table}",))
    pk = [r[0] for r in cur.fetchall()]
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name<>'loaded_at' "
        "ORDER BY ordinal_position", (table,))
    cols = [r[0] for r in cur.fetchall()]
    if not pk or not cols:
        return {"rows": None, "sha256": None, "note": "missing table or no primary key"}
    sel = ", ".join(f'"{c}"' for c in cols)
    order = ", ".join(f'"{c}"' for c in pk)
    cur.execute(f"SELECT {sel} FROM public.{table} ORDER BY {order}")
    h = hashlib.sha256()
    n = 0
    for row in cur:
        h.update(repr(row).encode("utf-8"))
        h.update(b"\n")
        n += 1
    return {"rows": n, "sha256": h.hexdigest()}


def normalise(stdout, keep_all=False):
    """GATE/PASS/FAIL lines; status output is short and has no timestamps,
    so for status commands every non-blank line (the counts) is kept."""
    return [ln.strip() for ln in stdout.splitlines()
            if ln.strip() and (keep_all or _KEEP.match(ln))]


def run_command(args):
    p = subprocess.run([sys.executable, str(SCRIPTS / args[0])] + args[1:],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(SCRIPTS.parent))
    lines = normalise(p.stdout, keep_all=args[-1] == "status")
    return {"exit_code": p.returncode, "lines": lines, "line_count": len(lines)}


def capture(cur):
    out = {"tables": {t: table_evidence(cur, t) for t in TABLES}, "commands": {}}
    # End our read transaction before the subprocesses run: the verify scripts
    # TRUNCATE inside a rolled-back savepoint and would block forever behind
    # our open ACCESS SHARE locks.
    cur.connection.rollback()
    for key, args in COMMANDS.items():
        out["commands"][key] = run_command(args)
    return out


def _flat(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flat(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


def compare(before, after):
    b, a = dict(_flat(before)), dict(_flat(after))
    diffs = []
    for k in sorted(set(b) | set(a)):
        if b.get(k) != a.get(k):
            diffs.append(f"{k}: before={b.get(k)!r} after={a.get(k)!r}")
    return diffs


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out", required=True)
    m = sub.add_parser("compare")
    m.add_argument("--before", required=True)
    args = ap.parse_args(argv)
    from _db import get_readonly_conn
    conn = get_readonly_conn()
    try:
        cur = conn.cursor()
        now = capture(cur)
    finally:
        conn.close()
    if args.cmd == "capture":
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(now, indent=2), encoding="utf-8")
        for k, v in now["commands"].items():
            print(f"{k}: exit {v['exit_code']} ({v['line_count']} gate/pass/fail lines)")
        print(f"written {p}")
        return 0
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    diffs = compare(before, now)
    print("\n".join(diffs) if diffs else "IDENTICAL")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
