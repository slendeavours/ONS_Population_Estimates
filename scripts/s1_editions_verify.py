"""Gates for the S1 edition-history table.

Prints `GATE n name: PASS/FAIL`; exits 1 on any FAIL. Gates 1-3 cover the
table shape and immutability, 4-5 the backfill, 6-8 the revised 2023Q2-2024Q4
editions (registry source, no suppressed value stored as 0, support-need totals
equal to an independent S1b-reader re-read of the same file). Gates 6-8 print `FAIL (pending load)` while any of those seven
quarters has no edition 2 or later, so the exit code is 1 until they are loaded.
Later steps add gates 9-11.

Usage:
    python scripts/s1_editions_verify.py

The UPDATE/DELETE gates run inside a savepoint that is always rolled back and
the whole run ends in a rollback, so the table is left unchanged.
"""
import sys
from datetime import date
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402
from s1_editions import (TABLE, MEASURES, STALE_PERIODS,  # noqa: E402
                         check_no_suppressed_zero, check_registry,
                         check_support_needs, insert_edition, latest_edition)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = []


def report(n, name, ok, detail="", pending=False):
    RESULTS.append(ok)
    verdict = "PASS" if ok else ("FAIL (pending load)" if pending else "FAIL")
    print(f"GATE {n} {name}: {verdict}"
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


def _chain(cur, specs):
    """Insert editions of 2099Q1; specs = [(supersedes, published_date, value)]
    in edition order. Distinct values and a distinct sha per edition."""
    lad = _first_lad(cur)
    for i, (sup, d, v) in enumerate(specs):
        insert_edition(cur, _rec(lad, v), "2099Q1",
                       **dict(_kw("t", d, f"h{i}"), supersedes=sup))


def gate_3a_chain_tip(cur):
    name = "latest_edition: chain tip wins even with an older published_date"
    if not table_exists(cur):
        return report("3a", name, False, f"{TABLE} absent")

    def body(cur):
        _chain(cur, [(None, date(2099, 3, 1), 1), (1, date(2099, 1, 1), 2)])
        return latest_edition(cur, "2099Q1")
    got = _in_savepoint(cur, body)
    report("3a", name, got == 2, f"latest = edition {got}, expected 2")


def gate_3b_fork_raises(cur):
    name = "latest_edition: fork (two editions supersede one) raises"
    if not table_exists(cur):
        return report("3b", name, False, f"{TABLE} absent")

    def body(cur):
        _chain(cur, [(None, date(2099, 1, 1), 1), (1, date(2099, 2, 1), 2),
                     (1, date(2099, 3, 1), 3)])
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    ok, msg = _in_savepoint(cur, body)
    report("3b", name, ok and "tips" in msg, msg)


def gate_3e_superseded_never_returned(cur):
    name = "latest_edition: a superseded edition is never returned"
    if not table_exists(cur):
        return report("3e", name, False, f"{TABLE} absent")

    def body(cur):
        # edition 2 carries the newest date but edition 3 supersedes it
        _chain(cur, [(None, date(2099, 1, 1), 1), (1, date(2099, 6, 1), 2),
                     (2, date(2099, 2, 1), 3)])
        return latest_edition(cur, "2099Q1")
    got = _in_savepoint(cur, body)
    report("3e", name, got == 3, f"latest = edition {got}, expected 3")


def gate_3f_two_roots_raise(cur):
    name = "latest_edition: two roots with no supersedes raise"
    if not table_exists(cur):
        return report("3f", name, False, f"{TABLE} absent")

    def body(cur):
        # identical values and date: the old date/value rule accepted this
        _chain(cur, [(None, date(2099, 1, 1), 1), (None, date(2099, 1, 1), 1)])
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    ok, msg = _in_savepoint(cur, body)
    report("3f", name, ok and "no single root" in msg, msg)


def gate_3g_single_edition(cur):
    name = "latest_edition: a single edition returns itself"
    if not table_exists(cur):
        return report("3g", name, False, f"{TABLE} absent")

    def body(cur):
        _chain(cur, [(None, None, 1)])
        return latest_edition(cur, "2099Q1")
    got = _in_savepoint(cur, body)
    report("3g", name, got == 1, f"latest = edition {got}, expected 1 "
           "(regression guard; the old code also passes this one)")


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


def gate_3d_bad_supersedes(cur):
    name = "insert_edition: supersedes must name an existing edition"
    if not table_exists(cur):
        return report("3d", name, False, f"{TABLE} absent")

    def body(cur):
        lad = _first_lad(cur)
        insert_edition(cur, _rec(lad, 1), "2099Q1",
                       **_kw("t", date(2099, 1, 1), "h0"))
        bad = dict(_kw("t", date(2099, 2, 1), "h1"), supersedes=7)
        r = _raises(lambda: insert_edition(cur, _rec(lad, 2), "2099Q1", **bad),
                    SystemExit)
        good = dict(_kw("t", date(2099, 2, 1), "h1"), supersedes=1)
        ed = insert_edition(cur, _rec(lad, 2), "2099Q1", **good)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = '2099Q1'")
        return r, ed, cur.fetchone()[0]
    (ok, msg), ed, n = _in_savepoint(cur, body)
    report("3d", name, ok and "supersedes" in msg and ed == 2 and n == 2,
           f"{msg}; valid supersedes -> edition {ed}, rows={n}")


def gate_4_coverage(cur):
    name = "every quarter has edition 1 and 296 authorities per edition"
    if not table_exists(cur):
        return report(4, name, False, f"{TABLE} absent")
    cur.execute("SELECT DISTINCT period FROM public.la_statutory_homelessness")
    periods = sorted(r[0] for r in cur.fetchall())
    cur.execute(f"""SELECT period, edition, COUNT(DISTINCT lad24cd), COUNT(*)
                    FROM public.{TABLE} GROUP BY 1, 2""")
    eds = {}
    for p, e, n, rows in cur.fetchall():
        eds.setdefault(p, {})[e] = (n, rows)
    bad = []
    for p in periods:
        e = eds.get(p, {})
        if 1 not in e:
            bad.append(f"{p}: no edition 1")
        bad += [f"{p} ed{k}: {v[0]} authorities/{v[1]} rows"
                for k, v in e.items() if v != (296, 296)]
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
    total = cur.fetchone()[0]
    report(4, name, bool(periods) and not bad,
           f"periods={len(periods)} rows={total} "
           + ("; ".join(bad[:5]) if bad else "ok"))


def gate_5_latest_equals_live(cur):
    name = "latest edition equals live table on the six measures"
    if not table_exists(cur):
        return report(5, name, False, f"{TABLE} absent")
    cur.execute("SELECT DISTINCT period FROM public.la_statutory_homelessness")
    periods = sorted(r[0] for r in cur.fetchall())
    cols = ", ".join(("lad24cd",) + MEASURES[:6])
    bad = []
    for p in periods:
        try:
            ed = latest_edition(cur, p)
        except (LookupError, ValueError) as e:
            bad.append(f"{p}: {e}")
            continue
        q = (f"SELECT COUNT(*) FROM (SELECT {cols} FROM public.{TABLE} "
             "WHERE period = %s AND edition = %s EXCEPT "
             f"SELECT {cols} FROM public.la_statutory_homelessness "
             "WHERE period = %s) x")
        q2 = (f"SELECT COUNT(*) FROM (SELECT {cols} FROM "
              "public.la_statutory_homelessness WHERE period = %s EXCEPT "
              f"SELECT {cols} FROM public.{TABLE} "
              "WHERE period = %s AND edition = %s) x")
        cur.execute(q, (p, ed, p))
        a = cur.fetchone()[0]
        cur.execute(q2, (p, p, ed))
        b = cur.fetchone()[0]
        if a or b:
            bad.append(f"{p} ed{ed}: {a} edition-only, {b} live-only rows")
    cur.execute(f"""SELECT edition, total_assessments FROM public.{TABLE}
                    WHERE period = '2025Q2' AND lad24cd = 'E06000001'
                    ORDER BY edition""")
    hart = cur.fetchall()
    if hart != [(1, 138), (2, 159)]:
        bad.append(f"Hartlepool 2025Q2 total_assessments {hart}, "
                   "expected [(1, 138), (2, 159)]")
    report(5, name, bool(periods) and not bad,
           "; ".join(bad[:5]) if bad else f"{len(periods)} periods match; "
           "Hartlepool 138 -> 159")


def _revised_editions(cur):
    """{period: [editions >= 2]} for the seven stale quarters."""
    cur.execute(f"""SELECT period, edition FROM public.{TABLE}
                    WHERE period = ANY(%s) AND edition >= 2
                    GROUP BY 1, 2 ORDER BY 1, 2""", (list(STALE_PERIODS),))
    out = {p: [] for p in STALE_PERIODS}
    for p, e in cur.fetchall():
        out[p].append(e)
    return out


def _clean_fail(n, name, fn, cur):
    """Run a gate body; a chain error becomes a FAIL line, not a traceback."""
    try:
        fn(cur)
    except (ValueError, LookupError) as e:
        report(n, name, False, str(e))


def gate_6_revised_registry(cur):
    name = "2023Q2-2024Q4 have a registry edition >= 2, 296 authorities"
    if not table_exists(cur):
        return report(6, name, False, f"{TABLE} absent")

    def body(cur):
        revised = _revised_editions(cur)
        missing = [p for p, e in revised.items() if not e]
        bad = []
        for p, eds in revised.items():
            for e in eds:
                bad += check_registry(cur, p, e, require_registry=False)
            if eds and all(check_registry(cur, p, e) for e in eds):
                bad.append(f"{p}: no valid registry edition >= 2")
        if bad:
            return report(6, name, False, "; ".join(bad[:5]))
        if missing:
            return report(6, name, False,
                          f"no edition >= 2 yet for {missing}", pending=True)
        report(6, name, True, f"{len(revised)} periods ok")
    _clean_fail(6, name, body, cur)


def gate_7_no_suppressed_zero(cur):
    name = "no suppressed households_in_ta stored as 0 in editions >= 2"
    if not table_exists(cur):
        return report(7, name, False, f"{TABLE} absent")

    def body(cur):
        revised = _revised_editions(cur)
        missing = [p for p, e in revised.items() if not e]
        bad, checked, zeros = [], 0, 0
        for p, eds in revised.items():
            for ed in eds:
                pr, n, z = check_no_suppressed_zero(cur, p, ed)
                bad += pr
                checked += n
                zeros += z
        if bad:
            return report(7, name, False, "; ".join(bad[:5]))
        if missing:
            return report(7, name, False,
                          f"no edition >= 2 yet for {missing}", pending=True)
        report(7, name, True, f"{checked} rows re-extracted, {zeros} "
               "published zeros confirmed")
    _clean_fail(7, name, body, cur)


def gate_8_support_needs_vs_a3(cur):
    name = "support_needs_total equals independent S1b-reader A3 re-read, 296/296"
    if not table_exists(cur):
        return report(8, name, False, f"{TABLE} absent")

    def body(cur):
        revised = _revised_editions(cur)
        missing = [p for p, e in revised.items() if not e]
        bad, done = [], 0
        for p, eds in revised.items():
            if not eds:
                continue
            bad += check_support_needs(cur, p, latest_edition(cur, p))
            done += 1
        if bad:
            return report(8, name, False, "; ".join(bad[:7]))
        if missing:
            return report(8, name, False,
                          f"no edition >= 2 yet for {missing}", pending=True)
        report(8, name, True, f"{done} periods 296/296")
    _clean_fail(8, name, body, cur)


def gate_6s_seeded(cur):
    name = "seeded: gate 6 check flags an orphan code and a short edition"
    if not table_exists(cur):
        return report("6s", name, False, f"{TABLE} absent")
    cur.execute(f"SELECT lad24cd FROM public.{TABLE} "
                "WHERE period = '2023Q2' AND edition = 1")
    codes = {r[0] for r in cur.fetchall()}
    clean = check_registry(cur, "2023Q2", 1, known_codes=codes,
                           require_registry=False)
    orphan = check_registry(cur, "2023Q2", 1, known_codes=codes - {min(codes)},
                            require_registry=False)

    def short(cur):
        _chain(cur, [(None, None, 1)])
        return check_registry(cur, "2099Q1", 1, require_registry=False)
    sh = _in_savepoint(cur, short)
    # edition 1 has no source_url by design, so only the count/orphan checks
    # are expected to be clean here
    ok = (not [x for x in clean if "source_url" not in x]
          and any("outside la_code_lookup" in x for x in orphan)
          and any("1 authorities/1 rows" in x for x in sh))
    report("6s", name, ok, f"clean={clean}; orphan={orphan[:1]}; short={sh[:1]}")


def gate_7s_seeded(cur):
    name = "seeded: gate 7 check flags a stored 0 over a suppressed cell"
    if not table_exists(cur):
        return report("7s", name, False, f"{TABLE} absent")
    cur.execute(f"""SELECT lad24cd FROM public.{TABLE}
                    WHERE period = '2023Q2' AND edition = 1
                      AND households_in_ta = 0 LIMIT 1""")
    row = cur.fetchone()
    if row is None:
        return report("7s", name, False, "no stored zero to seed against")
    lad = row[0]
    sup, _, _ = check_no_suppressed_zero(cur, "2023Q2", 1,
                                         raw={lad: ("..", None)})
    pub, _, _ = check_no_suppressed_zero(cur, "2023Q2", 1,
                                         raw={lad: ("0", 0)})
    ok = (any(f"{lad}: stored 0, TA1 cell '..'" in x for x in sup)
          and not any(f"{lad}: stored 0" in x for x in pub))
    report("7s", name, ok, f"suppressed cell -> {len(sup)} flag(s) incl. "
           f"{lad}; published zero -> not flagged")


def gate_8s_seeded(cur):
    name = "seeded: gate 8 check flags 295/296"
    if not table_exists(cur):
        return report("8s", name, False, f"{TABLE} absent")
    cur.execute(f"""SELECT lad24cd, support_needs_total FROM public.{TABLE}
                    WHERE period = '2023Q2' AND edition = 1""")
    exp = dict(cur.fetchall())
    clean = check_support_needs(cur, "2023Q2", 1, expected=dict(exp))
    lad = min(exp)
    exp[lad] = -1 if exp[lad] is None else exp[lad] + 1
    bad = check_support_needs(cur, "2023Q2", 1, expected=exp)
    report("8s", name, not clean and any("295/296" in x for x in bad),
           f"clean={clean}; seeded={bad}")


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            gate_1_table_shape(cur)
            gate_2_update(cur)
            gate_3_delete(cur)
            gate_3a_chain_tip(cur)
            gate_3b_fork_raises(cur)
            gate_3e_superseded_never_returned(cur)
            gate_3f_two_roots_raise(cur)
            gate_3g_single_edition(cur)
            gate_3c_empty_recs(cur)
            gate_3d_bad_supersedes(cur)
            gate_4_coverage(cur)
            gate_5_latest_equals_live(cur)
            gate_6_revised_registry(cur)
            gate_7_no_suppressed_zero(cur)
            gate_8_support_needs_vs_a3(cur)
            gate_6s_seeded(cur)
            gate_7s_seeded(cur)
            gate_8s_seeded(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
