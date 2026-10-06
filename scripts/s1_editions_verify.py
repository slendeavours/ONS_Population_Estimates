"""Gates for the S1 edition-history table.

Prints `GATE n name: PASS/FAIL`; exits 1 on any FAIL. Gates 1-3 cover the
table shape and immutability. Later steps add gates 4-11.

Usage:
    python scripts/s1_editions_verify.py

The UPDATE/DELETE gates run inside a savepoint that is always rolled back and
the whole run ends in a rollback, so the table is left unchanged.
"""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402
from s1_editions import (TABLE, MEASURES, insert_edition,  # noqa: E402
                         latest_edition)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = []


def report(n, name, ok, detail=""):
    RESULTS.append(ok)
    print(f"GATE {n} {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))


def table_exists(cur):
    cur.execute("SELECT to_regclass(%s)", (f"public.{TABLE}",))
    return cur.fetchone()[0] is not None


def gate_1_table_shape(cur):
    name = "table exists with PK and period CHECK"
    if not table_exists(cur):
        return report(1, name, False, f"{TABLE} absent")
    cur.execute("""SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass""", (f"public.{TABLE}",))
    cons = cur.fetchall()
    pk = [d for t, d in cons if t == "p"]
    chk = [d for t, d in cons if t == "c" and "period" in d]
    fk = [d for t, d in cons if t == "f"]
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (TABLE,))
    cols = {r[0] for r in cur.fetchall()}
    need = {"lad24cd", "period", "edition", "release_label", "published_date",
            "source_url", "source_file", "source_sha256", "loaded_at",
            "supersedes", *MEASURES}
    ok = (len(pk) == 1 and "lad24cd, period, edition" in pk[0]
          and len(chk) == 1 and "Q[1-4]" in chk[0]
          and len(fk) == 1 and "la_boundaries" in fk[0] and need <= cols)
    report(1, name, ok,
           f"pk={len(pk)} check={len(chk)} fk={len(fk)} "
           f"missing={sorted(need - cols)}")


def _first_lad(cur):
    cur.execute("SELECT lad24cd FROM la_boundaries ORDER BY lad24cd LIMIT 1")
    return cur.fetchone()[0]


def _expect_raises(cur, n, name, stmt):
    """Insert a throwaway row inside a savepoint; stmt on it must raise."""
    if not table_exists(cur):
        return report(n, name, False, f"{TABLE} absent")
    cur.execute("SAVEPOINT g")
    try:
        cur.execute(f"""INSERT INTO public.{TABLE} (lad24cd, period, edition,
                        total_assessments, source_file)
                        VALUES (%s, '2099Q1', 1, 1, 'gate-throwaway')""",
                    (_first_lad(cur),))
        raised, msg = False, ""
        cur.execute("SAVEPOINT stmt")
        try:
            cur.execute(stmt)
        except psycopg2.Error as e:
            raised, msg = True, str(e).splitlines()[0]
            cur.execute("ROLLBACK TO SAVEPOINT stmt")
        report(n, name, raised and "append-only" in msg,
               msg or "no error raised")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT g")
        cur.execute("RELEASE SAVEPOINT g")


def gate_2_update(cur):
    _expect_raises(cur, 2, "UPDATE raises",
                   f"UPDATE public.{TABLE} SET total_assessments = 2 "
                   "WHERE period = '2099Q1'")


def gate_3_delete(cur):
    _expect_raises(cur, 3, "DELETE raises",
                   f"DELETE FROM public.{TABLE} WHERE period = '2099Q1'")


def _rec(lad, v):
    return [{"lad24cd": lad, "total_assessments": v}]


def _kw(label, d, sha):
    return dict(release_label=label, published_date=d, source_url=None,
                source_file="gate-throwaway", source_sha256=sha,
                supersedes=None)


def _in_savepoint(cur, fn):
    """Run fn(cur) inside a savepoint that is always rolled back."""
    cur.execute("SAVEPOINT h")
    try:
        return fn(cur)
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT h")
        cur.execute("RELEASE SAVEPOINT h")


def _raises(fn, exc):
    try:
        fn()
    except exc as e:
        return True, str(e)
    return False, "no error raised"


def gate_3a_three_way_tie(cur):
    from datetime import date
    name = "latest_edition: 3 tied editions, third differs, raises"
    if not table_exists(cur):
        return report("3a", name, False, f"{TABLE} absent")

    def body(cur):
        lad, d = _first_lad(cur), date(2099, 1, 1)
        for i, v in enumerate((1, 1, 2)):
            insert_edition(cur, _rec(lad, v), "2099Q1", **_kw("t", d, f"h{i}"))
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    ok, msg = _in_savepoint(cur, body)
    report("3a", name, ok and "differ" in msg, msg)


def gate_3b_mixed_dates(cur):
    from datetime import date
    name = "latest_edition: dated and undated mixed, raises"
    if not table_exists(cur):
        return report("3b", name, False, f"{TABLE} absent")

    def body(cur):
        lad = _first_lad(cur)
        insert_edition(cur, _rec(lad, 1), "2099Q1",
                       **_kw("t", date(2099, 1, 1), "h0"))
        insert_edition(cur, _rec(lad, 2), "2099Q1", **_kw("t", None, "h1"))
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    ok, msg = _in_savepoint(cur, body)
    report("3b", name, ok and "no published_date" in msg, msg)


def gate_3c_empty_recs(cur):
    name = "insert_edition: empty recs halts and writes nothing"
    if not table_exists(cur):
        return report("3c", name, False, f"{TABLE} absent")

    def body(cur):
        r = _raises(lambda: insert_edition(cur, [], "2099Q1",
                                           **_kw("t", None, "h0")), SystemExit)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = '2099Q1'")
        return r, cur.fetchone()[0]
    (ok, msg), n = _in_savepoint(cur, body)
    report("3c", name, ok and n == 0, f"{msg}; rows={n}")


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            gate_1_table_shape(cur)
            gate_2_update(cur)
            gate_3_delete(cur)
            gate_3a_three_way_tie(cur)
            gate_3b_mixed_dates(cur)
            gate_3c_empty_recs(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
