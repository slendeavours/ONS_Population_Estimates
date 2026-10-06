"""Gates for the S1b support-needs edition-history table.

Mirrors scripts/s1_editions_verify.py. Prints `GATE n name: PASS/FAIL`; exits 1
on any FAIL. Gates 1-3 cover table shape, immutability and the supersedes-chain
helpers; 4-5 cover the backfill (coverage, 2025Q2 original vs the before-revision
CSV, latest edition vs the live table). Gates ending in `s` are seeded: they
prove the check itself flags a planted fault.

Usage:
    python scripts/s1b_editions_verify.py

Everything that writes runs inside a savepoint that is always rolled back and
the run ends in a rollback, so the table is left unchanged.
"""
import sys
from datetime import date
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402
from s1b_editions import (TABLE, LIVE, DATA_COLS,  # noqa: E402
                          BACKFILL_ROWS_TOTAL, check_coverage,
                          check_latest_equals_live, check_q2_pair,
                          insert_edition, latest_edition,
                          load_before_revision_csv)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = []
TRIGGERS = ("s1b_editions_immutable", "s1b_editions_no_truncate")


def report(n, name, ok, detail="", pending=False):
    RESULTS.append(ok)
    verdict = "PASS" if ok else ("FAIL (pending load)" if pending else "FAIL")
    print(f"GATE {n} {name}: {verdict}" + (f"  [{detail}]" if detail else ""))


def table_exists(cur):
    cur.execute("SELECT to_regclass(%s)", (f"public.{TABLE}",))
    return cur.fetchone()[0] is not None


def _norm(defn):
    """Constraint definition with whitespace collapsed (the definitions do not
    name the table, so equal text means the same CHECK)."""
    return " ".join(defn.split())


def gate_1_table_shape(cur):
    name = "table exists with PK, FK, CHECKs and three triggers"
    if not table_exists(cur):
        return report(1, name, False, f"{TABLE} absent")
    cur.execute("""SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass""", (f"public.{TABLE}",))
    cons = cur.fetchall()
    pk = [d for t, d in cons if t == "p"]
    fk = [d for t, d in cons if t == "f"]
    chk = {_norm(d) for t, d in cons if t == "c"}
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass AND contype = 'c'""",
                (f"public.{LIVE}",))
    live_chk = {_norm(r[0]) for r in cur.fetchall()}
    extra = chk - live_chk
    period_chk = [d for d in extra if "Q[1-4]" in d]
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (TABLE,))
    cols = {r[0] for r in cur.fetchall()}
    need = {"edition", "release_label", "published_date", "source_file",
            "source_sha256", "supersedes", "loaded_at", *DATA_COLS}
    cur.execute("""SELECT t.tgname, t.tgtype FROM pg_trigger t
                   WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal""",
                (f"public.{TABLE}",))
    trg = {n: ty for n, ty in cur.fetchall()}
    # tgtype bits: 1 row, 2 before, 4 insert, 8 delete, 16 update, 32 truncate
    upd_del = trg.get(TRIGGERS[0], 0)
    trunc = trg.get(TRIGGERS[1], 0)
    trig_ok = (upd_del & 1 and upd_del & 2 and upd_del & 8 and upd_del & 16
               and not upd_del & 4 and trunc & 2 and trunc & 32)
    ok = (len(pk) == 1 and "lad24cd, period, category_code, edition" in pk[0]
          and len(fk) == 1 and "la_boundaries" in fk[0] and need <= cols
          and len(live_chk) == 4 and live_chk <= chk
          and len(period_chk) == 1 and extra == set(period_chk)
          and bool(trig_ok))
    report(1, name, ok, f"checks: {len(live_chk)} live all present={live_chk <= chk}, "
           f"extra={len(extra)}; pk={len(pk)} fk={len(fk)} triggers={sorted(trg)} "
           f"missing_cols={sorted(need - cols)}")


def _first_lad(cur):
    cur.execute("SELECT lad24cd FROM la_boundaries ORDER BY lad24cd LIMIT 1")
    return cur.fetchone()[0]


def _rec(lad, v, cat="hh_no_support_needs"):
    return {"lad24cd": lad, "period": "2099Q1", "category_code": cat,
            "value": v, "value_flag": None, "category_group": "needs_breakdown",
            "category_label": "t", "reference_quarter": "2099-03",
            "source_url": "x", "source_edition": "t.ods",
            "edition_variant": "original", "release_page_url": "x",
            "layout_version": "t", "publisher_la_code": lad}


def _kw(label, d, sha):
    return dict(release_label=label, published_date=d, source_url=None,
                source_file="gate-throwaway", source_sha256=sha,
                supersedes=None)


def _in_savepoint(cur, fn):
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


def _expect_raises(cur, n, name, stmt):
    """Insert a throwaway row in a savepoint; stmt on it must raise."""
    if not table_exists(cur):
        return report(n, name, False, f"{TABLE} absent")

    def body(cur):
        lad = _first_lad(cur)
        insert_edition(cur, [_rec(lad, 1)], "2099Q1", **_kw("t", None, "h0"))
        cur.execute("SAVEPOINT stmt")
        try:
            cur.execute(stmt)
            return False, "no error raised"
        except psycopg2.Error as e:
            cur.execute("ROLLBACK TO SAVEPOINT stmt")
            return True, str(e).splitlines()[0]
    raised, msg = _in_savepoint(cur, body)
    report(n, name, raised and "append-only" in msg, msg)


def gate_2_update(cur):
    _expect_raises(cur, 2, "UPDATE raises",
                   f"UPDATE public.{TABLE} SET value = 2 "
                   "WHERE period = '2099Q1'")


def gate_3_delete(cur):
    _expect_raises(cur, 3, "DELETE raises",
                   f"DELETE FROM public.{TABLE} WHERE period = '2099Q1'")


def gate_3t_truncate(cur):
    _expect_raises(cur, "3t", "TRUNCATE raises", f"TRUNCATE public.{TABLE}")


def _chain(cur, specs):
    """Insert editions of 2099Q1; specs = [(supersedes, published, value)]."""
    lad = _first_lad(cur)
    for i, (sup, d, v) in enumerate(specs):
        insert_edition(cur, [_rec(lad, v)], "2099Q1",
                       **dict(_kw("t", d, f"h{i}"), supersedes=sup))


def _chain_gate(cur, n, name, fn):
    """Run fn in a savepoint; None (after reporting FAIL) if table absent."""
    if not table_exists(cur):
        report(n, name, False, f"{TABLE} absent")
        return None
    return _in_savepoint(cur, fn)


def gate_3a_chain_tip(cur):
    name = "latest_edition: chain tip wins even with an older published_date"

    def body(cur):
        _chain(cur, [(None, date(2099, 3, 1), 1), (1, date(2099, 1, 1), 2)])
        return latest_edition(cur, "2099Q1")
    got = _chain_gate(cur, "3a", name, body)
    if got is not None:
        report("3a", name, got == 2, f"latest = edition {got}, expected 2")


def gate_3b_fork_raises(cur):
    name = "latest_edition: fork (two editions supersede one) raises"

    def body(cur):
        _chain(cur, [(None, date(2099, 1, 1), 1), (1, date(2099, 2, 1), 2),
                     (1, date(2099, 3, 1), 3)])
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    r = _chain_gate(cur, "3b", name, body)
    if r is not None:
        report("3b", name, r[0] and "tips" in r[1], r[1])


def gate_3e_superseded_never_returned(cur):
    name = "latest_edition: a superseded edition is never returned"

    def body(cur):
        _chain(cur, [(None, date(2099, 1, 1), 1), (1, date(2099, 6, 1), 2),
                     (2, date(2099, 2, 1), 3)])
        return latest_edition(cur, "2099Q1")
    got = _chain_gate(cur, "3e", name, body)
    if got is not None:
        report("3e", name, got == 3, f"latest = edition {got}, expected 3")


def gate_3f_two_roots_raise(cur):
    name = "latest_edition: two roots with no supersedes raise"

    def body(cur):
        _chain(cur, [(None, date(2099, 1, 1), 1), (None, date(2099, 1, 1), 1)])
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    r = _chain_gate(cur, "3f", name, body)
    if r is not None:
        report("3f", name, r[0] and "no single root" in r[1], r[1])


def gate_3g_single_edition(cur):
    name = "latest_edition: a single edition returns itself"

    def body(cur):
        _chain(cur, [(None, None, 1)])
        return latest_edition(cur, "2099Q1")
    got = _chain_gate(cur, "3g", name, body)
    if got is not None:
        report("3g", name, got == 1, f"latest = edition {got}, expected 1")


def gate_3h_cycle_raises(cur):
    name = "latest_edition: a cycle (no root, no tip) raises"

    def body(cur):
        lad = _first_lad(cur)
        # editions are inserted directly: 1 supersedes 2 and 2 supersedes 1
        cols = list(DATA_COLS) + ["edition", "supersedes", "source_sha256"]
        for ed, sup in ((1, 2), (2, 1)):
            r = _rec(lad, 1)
            vals = [r[c] for c in DATA_COLS] + [ed, sup, f"c{ed}"]
            cur.execute(f"INSERT INTO public.{TABLE} ({', '.join(cols)}) "
                        f"VALUES ({', '.join(['%s'] * len(cols))})", vals)
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    r = _chain_gate(cur, "3h", name, body)
    if r is not None:
        report("3h", name, r[0], r[1])


def _raw_chain(cur, rows):
    """Insert editions of 2099Q1 directly with arbitrary supersedes values;
    rows = [(edition, supersedes)]."""
    lad = _first_lad(cur)
    cols = list(DATA_COLS) + ["edition", "supersedes", "source_sha256"]
    for ed, sup in rows:
        r = _rec(lad, 1)
        cur.execute(f"INSERT INTO public.{TABLE} ({', '.join(cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))})",
                    [r[c] for c in DATA_COLS] + [ed, sup, f"r{ed}"])


def gate_3j_mutual_supersede(cur):
    name = "latest_edition: 1 root plus two editions superseding each other raises"

    def body(cur):
        _raw_chain(cur, [(1, None), (2, 3), (3, 2)])
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    r = _chain_gate(cur, "3j", name, body)
    if r is not None:
        report("3j", name, r[0], r[1])


def gate_3k_self_supersede(cur):
    name = "latest_edition: an edition superseding itself raises"

    def body(cur):
        _raw_chain(cur, [(1, None), (2, 1), (3, 3)])
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    r = _chain_gate(cur, "3k", name, body)
    if r is not None:
        report("3k", name, r[0], r[1])


def gate_3c_empty_recs(cur):
    name = "insert_edition: empty rows halts and writes nothing"

    def body(cur):
        r = _raises(lambda: insert_edition(cur, [], "2099Q1",
                                           **_kw("t", None, "h0")), SystemExit)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = '2099Q1'")
        return r, cur.fetchone()[0]
    out = _chain_gate(cur, "3c", name, body)
    if out is not None:
        (ok, msg), n = out
        report("3c", name, ok and n == 0, f"{msg}; rows={n}")


def gate_3d_bad_supersedes(cur):
    name = "insert_edition: supersedes must name an existing edition"

    def body(cur):
        lad = _first_lad(cur)
        insert_edition(cur, [_rec(lad, 1)], "2099Q1",
                       **_kw("t", date(2099, 1, 1), "h0"))
        bad = dict(_kw("t", date(2099, 2, 1), "h1"), supersedes=7)
        r = _raises(lambda: insert_edition(cur, [_rec(lad, 2)], "2099Q1",
                                           **bad), SystemExit)
        good = dict(_kw("t", date(2099, 2, 1), "h1"), supersedes=1)
        ed = insert_edition(cur, [_rec(lad, 2)], "2099Q1", **good)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = '2099Q1'")
        return r, ed, cur.fetchone()[0]
    out = _chain_gate(cur, "3d", name, body)
    if out is not None:
        (ok, msg), ed, n = out
        report("3d", name, ok and "supersedes" in msg and ed == 2 and n == 2,
               f"{msg}; valid supersedes -> edition {ed}, rows={n}")


def gate_3i_idempotent(cur):
    name = "insert_edition: same (period, sha256) twice adds nothing"

    def body(cur):
        lad = _first_lad(cur)
        a = insert_edition(cur, [_rec(lad, 1)], "2099Q1", **_kw("t", None, "h0"))
        b = insert_edition(cur, [_rec(lad, 1)], "2099Q1", **_kw("t", None, "h0"))
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = '2099Q1'")
        return a, b, cur.fetchone()[0]
    out = _chain_gate(cur, "3i", name, body)
    if out is not None:
        report("3i", name, out == (1, 1, 1),
               f"editions {out[0]},{out[1]}; rows={out[2]}")


def gate_4_coverage(cur):
    name = "every period: edition 1, 296 authorities, expected category rows"
    if not table_exists(cur):
        return report(4, name, False, f"{TABLE} absent")
    bad, n1, total = check_coverage(cur)
    report(4, name, not bad, "; ".join(bad[:5]) if bad else
           f"edition-1 rows={n1}")


def gate_4t_total(cur):
    name = f"backfill row count {BACKFILL_ROWS_TOTAL}"
    if not table_exists(cur):
        return report("4t", name, False, f"{TABLE} absent")
    cur.execute(f"""SELECT COUNT(*) FROM public.{TABLE}
                    WHERE edition = 1 OR (period = '2025Q2' AND edition = 2)""")
    n = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
    total = cur.fetchone()[0]
    later = total - n
    if later:
        # the whole-table total is only fixed until Task 3 loads more editions;
        # the backfilled editions themselves must still be intact
        return report("4t", name, n == BACKFILL_ROWS_TOTAL,
                      f"pending: {later} rows of later editions exist, so the "
                      f"table total no longer applies; backfilled editions "
                      f"hold {n}, expected {BACKFILL_ROWS_TOTAL}")
    report("4t", name, total == BACKFILL_ROWS_TOTAL,
           f"table holds {total}, expected {BACKFILL_ROWS_TOTAL}")


def gate_4q_q2_pair(cur):
    name = ("2025Q2 edition 1 equals before-revision CSV (9,176 rows); "
            "edition 2 equals live")
    if not table_exists(cur):
        return report("4q", name, False, f"{TABLE} absent")
    bad = check_q2_pair(cur)
    report("4q", name, not bad, "; ".join(bad[:5]) if bad else "match")


def gate_4s_seeded(cur):
    name = "seeded: gate 4q flags a changed CSV cell; gate 4 flags a short edition"
    if not table_exists(cur):
        return report("4s", name, False, f"{TABLE} absent")
    csv_rows = load_before_revision_csv()
    k = sorted(csv_rows, key=str)[0]
    tweaked = (csv_rows - {k}) | {(k[0], k[1], (k[2] or 0) + 1, k[3])}
    bad = check_q2_pair(cur, csv_rows=tweaked)

    def short(cur):
        lad = _first_lad(cur)
        cur.execute(f"""INSERT INTO public.{TABLE} ({', '.join(DATA_COLS)},
                        edition) SELECT {', '.join(DATA_COLS)}, 2
                        FROM public.{TABLE} WHERE period = '2025Q3'
                          AND edition = 1 AND lad24cd <> %s""", (lad,))
        return check_coverage(cur)[0]
    sh = _in_savepoint(cur, short)
    ok = (any("2025Q2 edition 1" in x for x in bad)
          and any("2025Q3 ed2" in x for x in sh))
    report("4s", name, ok, f"q2={bad[:1]}; coverage={sh[:1]}")


def gate_5_latest_equals_live(cur):
    name = "latest edition equals the live table (all columns bar loaded_at)"
    if not table_exists(cur):
        return report(5, name, False, f"{TABLE} absent")
    try:
        bad = check_latest_equals_live(cur)
    except (ValueError, LookupError) as e:
        return report(5, name, False, str(e))
    report(5, name, not bad, "; ".join(bad[:5]) if bad else
           "11 periods match row-for-row")


def gate_5s_seeded(cur):
    name = "seeded: gate 5 flags a value changed in the latest edition"
    if not table_exists(cur):
        return report("5s", name, False, f"{TABLE} absent")

    # immutability blocks UPDATE, so the changed row is planted at insert time
    def planted(cur):
        cols = ", ".join(c for c in DATA_COLS if c != "value")
        cur.execute(f"""INSERT INTO public.{TABLE} ({cols}, value, edition,
                        supersedes, source_sha256)
                        SELECT {cols},
                               CASE WHEN lad24cd = 'E06000001'
                                     AND category_code = 'hh_no_support_needs'
                                    THEN COALESCE(value, 0) + 1 ELSE value END,
                               2, 1, 'seed' FROM public.{TABLE}
                        WHERE period = '2025Q3' AND edition = 1""")
        return check_latest_equals_live(cur)
    try:
        bad = _in_savepoint(cur, planted)
    except psycopg2.Error as e:
        return report("5s", name, False, str(e).splitlines()[0])
    report("5s", name, any("2025Q3 ed2" in x and "1 live-only" in x
                           and "1 edition-only" in x for x in bad),
           "; ".join(bad[:2]))


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            gate_1_table_shape(cur)
            gate_2_update(cur)
            gate_3_delete(cur)
            gate_3t_truncate(cur)
            gate_3a_chain_tip(cur)
            gate_3b_fork_raises(cur)
            gate_3e_superseded_never_returned(cur)
            gate_3f_two_roots_raise(cur)
            gate_3g_single_edition(cur)
            gate_3h_cycle_raises(cur)
            gate_3j_mutual_supersede(cur)
            gate_3k_self_supersede(cur)
            gate_3c_empty_recs(cur)
            gate_3d_bad_supersedes(cur)
            gate_3i_idempotent(cur)
            gate_4_coverage(cur)
            gate_4t_total(cur)
            gate_4q_q2_pair(cur)
            gate_4s_seeded(cur)
            gate_5_latest_equals_live(cur)
            gate_5s_seeded(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
