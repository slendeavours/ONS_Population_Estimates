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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = "la_statutory_homelessness_editions"
MEASURES = ("total_assessments", "owed_duty", "prevention_duty", "relief_duty",
            "households_in_ta", "support_needs_total",
            "mental_health_suspect", "learning_disability_suspect",
            "drug_dependency_suspect", "alcohol_dependency_suspect",
            "rough_sleeping_history_suspect")

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


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            gate_1_table_shape(cur)
            gate_2_update(cur)
            gate_3_delete(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
