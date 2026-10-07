"""Gates for the S1b support-needs edition-history table.

Mirrors scripts/s1_editions_verify.py. Prints `GATE n name: PASS/FAIL`; exits 1
on any FAIL. Gates 1-3 cover table shape, immutability and the supersedes-chain
helpers; 4-5 cover the backfill (coverage, 2025Q2 original vs the
before-revision CSV, latest edition vs the live table), 6-8 the seven revised 2023Q2-2024Q4
editions (structure and registry source, flag integrity against the raw cells,
S1 cross-check). Gates 6-8 print `FAIL (pending load)` while any of those seven
quarters has no edition 2 or later, so the exit code is 1 until they are
loaded (the S1 convention). Gates ending in `s` are seeded: they prove the
check itself flags a planted fault.
Gates 9-17 cover the generalised refresh, each as a seeded scenario inside a
rolled-back savepoint (the editions table is append-only, so a seeded edition
can only exist inside one): an edition 2 of one real period is refreshed and
nothing else moves (9); tampering outside the refresh inside the transaction
halts it (10, 10b, 10c: these replace the old snapshot-table gates 9 and 10 and
show the same faults are caught); a new live quarter makes status fail and
sync-new records it as edition 1, idempotently (11); a live row changed
directly is reported as drift and refresh halts unless accepted (12); today's
database is clean (13); a short quarter, a missing cell or a changed category
set is refused by sync-new (14); a forked chain is reported (15); sync-new
needs an explicit count when no period has editions (16); nothing depends on
the retired snapshot tables, typed counts or period lists (17). The per-period
before/after content hash (excluding loaded_at) replaces the snapshot
comparison.

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
from s1_editions import STALE_PERIODS, period_hashes  # noqa: E402
from s1_editions_verify import (RETIRED, _defined_functions,  # noqa: E402
                                scan_retired)
from s1b_editions import (TABLE, LIVE, DATA_COLS, HASH_KEY,  # noqa: E402
                          LIVE_LABEL, REFRESH_COLS, check_coverage,
                          check_flag_integrity, check_loaded, check_s1_cross,
                          check_latest_equals_live, check_q2_pair,
                          edition1_category_counts, expected_authorities,
                          insert_edition, latest_edition, live_rows_sha256,
                          load_before_revision_csv, refresh_counts,
                          refresh_latest, status, sync_new)

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
    name = "every period: edition 1, the derived authority count, authorities x categories rows"
    if not table_exists(cur):
        return report(4, name, False, f"{TABLE} absent")
    bad, n1, total = check_coverage(cur)
    report(4, name, not bad, "; ".join(bad[:5]) if bad else
           f"edition-1 rows={n1}")


def gate_4t_total(cur):
    name = "editions table row count equals the derived rows of every edition"
    if not table_exists(cur):
        return report("4t", name, False, f"{TABLE} absent")
    auths = expected_authorities(cur)
    cats = edition1_category_counts(cur)
    cur.execute(f"SELECT DISTINCT period, edition FROM public.{TABLE}")
    want = sum(auths * cats.get(p, 0) for p, _ in cur.fetchall())
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
    total = cur.fetchone()[0]
    report("4t", name, total == want,
           f"table holds {total}, derived {want} ({auths} authorities x "
           "edition-1 categories, per edition)")


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
    cur.execute(f"SELECT COUNT(DISTINCT period) FROM public.{LIVE}")
    report(5, name, not bad, "; ".join(bad[:5]) if bad else
           f"{cur.fetchone()[0]} periods match row-for-row")


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


def _revised_editions(cur):
    """{period: [editions >= 2]} for the seven quarters restated in the
    2026-10 reload (historical by definition; allow-listed in gate 17)."""
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


def _loaded_gate(n, name, cur, check, ok_detail):
    """Shared shape of gates 6-8: check(period, edition) -> problems over every
    edition >= 2 of the seven quarters; 'pending load' while one is missing."""
    if not table_exists(cur):
        return report(n, name, False, f"{TABLE} absent")

    def body(cur):
        revised = _revised_editions(cur)
        missing = [p for p, e in revised.items() if not e]
        bad, done = [], 0
        for p, eds in revised.items():
            for e in eds:
                bad += check(p, e)
                done += 1
        if bad:
            return report(n, name, False, "; ".join(bad[:5]))
        if missing:
            return report(n, name, False,
                          f"no edition >= 2 yet for {missing}", pending=True)
        report(n, name, True, ok_detail(done))
    _clean_fail(n, name, body, cur)


def gate_6_loaded(cur):
    _loaded_gate(6, "2023Q2-2024Q4 edition 2: derived authorities, category rows, "
                 "codes in la_code_lookup, registry/revised", cur,
                 lambda p, e: check_loaded(cur, p, e),
                 lambda n: f"{n} editions ok")


def gate_7_flag_integrity(cur):
    totals = [0, 0]

    def check(p, e):
        bad, n, z = check_flag_integrity(cur, p, e)
        totals[0] += n
        totals[1] += z
        return bad
    _loaded_gate(7, "no 0 stored over a suppression marker; every cell equals "
                 "an independent raw-cell re-read", cur, check,
                 lambda n: f"{totals[0]} cells re-read, {totals[1]} stored "
                 "zeros all confirmed")


def gate_8_s1_cross(cur):
    _loaded_gate(8, "one-or-more support needs equals S1 support_needs_total, "
                 "for every authority, NULL-safe", cur,
                 lambda p, e: check_s1_cross(cur, p, e),
                 lambda n: f"{n} editions, every authority")


def _stored_raw(cur, period, edition):
    """Raw-cell stand-ins built from the stored cells (clean by construction)."""
    cur.execute(f"""SELECT lad24cd, category_code, value, value_flag
                    FROM public.{TABLE} WHERE period = %s AND edition = %s""",
                (period, edition))
    rev = {"missing": "..", "suppressed": "-", "not_applicable": "[z]"}
    return {(l, c): (str(v) if v is not None else rev[f])
            for l, c, v, f in cur.fetchall()}


def gate_6s_seeded(cur):
    name = "seeded: gate 6 flags an orphan code, a short edition and a wrong label"
    if not table_exists(cur):
        return report("6s", name, False, f"{TABLE} absent")
    cur.execute(f"SELECT DISTINCT lad24cd FROM public.{TABLE} "
                "WHERE period = '2023Q2' AND edition = 1")
    codes = {r[0] for r in cur.fetchall()}
    clean = check_loaded(cur, "2023Q2", 1, known_codes=codes,
                         require_registry=False)
    orphan = check_loaded(cur, "2023Q2", 1, known_codes=codes - {min(codes)},
                          require_registry=False)
    label = check_loaded(cur, "2023Q2", 1, known_codes=codes)

    def short(cur):
        _chain(cur, [(None, None, 1)])
        return check_loaded(cur, "2099Q1", 1, require_registry=False)
    sh = _in_savepoint(cur, short)
    ok = (not clean and any("outside la_code_lookup" in x for x in orphan)
          and any("1 authorities/1 rows" in x for x in sh)
          and any("expected registry" in x for x in label))
    report("6s", name, ok, f"clean={clean}; orphan={orphan[:1]}; "
           f"short={sh[:1]}; label={label[:1]}")


def gate_7s_seeded(cur):
    name = "seeded: gate 7 flags a stored 0 over a suppression marker"
    if not table_exists(cur):
        return report("7s", name, False, f"{TABLE} absent")
    cur.execute(f"""SELECT lad24cd, category_code FROM public.{TABLE}
                    WHERE period = '2023Q2' AND edition = 1 AND value = 0
                    ORDER BY 1, 2 LIMIT 1""")
    row = cur.fetchone()
    if row is None:
        return report("7s", name, False, "no stored zero to seed against")
    raw = _stored_raw(cur, "2023Q2", 1)
    clean, n, zeros = check_flag_integrity(cur, "2023Q2", 1, raw=dict(raw))
    raw[row] = "[c]"
    sup = check_flag_integrity(cur, "2023Q2", 1, raw=raw)[0]
    raw[row] = ".."
    sup2 = check_flag_integrity(cur, "2023Q2", 1, raw=raw)[0]
    ok = (not clean and zeros > 0 and len(sup) == 1 and len(sup2) == 1
          and f"{row[0]} {row[1]}: stored (0, None)" in sup[0])
    report("7s", name, ok, f"clean={len(clean)} problems over {n} cells; "
           f"[c] over zero -> {sup[:1]}")


def gate_8s_seeded(cur):
    name = "seeded: gate 8 flags one authority short and a flag-versus-value disagreement"
    if not table_exists(cur):
        return report("8s", name, False, f"{TABLE} absent")
    cur.execute(f"""SELECT lad24cd, value FROM public.{TABLE}
                    WHERE period = '2023Q2' AND edition = 1
                      AND category_code = 'hh_one_or_more_support_needs'""")
    exp = dict(cur.fetchall())
    clean = check_s1_cross(cur, "2023Q2", 1, s1=dict(exp))
    lad = min(exp)
    changed = dict(exp)
    changed[lad] = -1 if exp[lad] is None else exp[lad] + 1
    bad = check_s1_cross(cur, "2023Q2", 1, s1=changed)
    nulled = dict(exp)
    nulled[lad] = None if exp[lad] is not None else 5
    bad2 = check_s1_cross(cur, "2023Q2", 1, s1=nulled)
    n = expected_authorities(cur)
    ok = (not clean and any(f"{n - 1}/{n}" in x for x in bad)
          and any(f"{n - 1}/{n}" in x for x in bad2))
    report("8s", name, ok, f"clean={clean}; changed={bad[:1]}; "
           f"null/value swap={bad2[:1]}")


LAD = "E06000001"  # a real authority present in every period
CAT = "hh_no_support_needs"  # a category present in every layout


def _fake_edition(cur, period, mutate=None, *, file="gate-fake.ods") -> int:
    """Append a fake next edition of a real period: a copy of the chain tip
    with `mutate({(lad24cd, category_code): row dict})` applied. The editions
    table is append-only, so the caller must be inside a savepoint it rolls
    back."""
    tip = latest_edition(cur, period)
    cols = ", ".join(DATA_COLS)
    cur.execute(f"SELECT {cols} FROM public.{TABLE} "
                "WHERE period = %s AND edition = %s", (period, tip))
    rows = {(r[0], r[2]): dict(zip(DATA_COLS, r)) for r in cur.fetchall()}
    if mutate:
        mutate(rows)
    return insert_edition(cur, list(rows.values()), period,
                          release_label="gate-fake",
                          published_date=date(2099, 1, 1), source_url=None,
                          source_file=file,
                          source_sha256=f"gate-fake-{period}-{tip}",
                          supersedes=tip)


def _bump(lad=LAD, cat=CAT, by=1):
    def f(rows):
        r = rows[(lad, cat)]
        r["value"] = (r["value"] or 0) + by
        r["value_flag"] = None
    return f


def _relabel(lad, cat):
    """Change only refresh columns other than the value."""
    def f(rows):
        r = rows[(lad, cat)]
        r["category_label"] = "gate-fake label"
        r["source_edition"] = "gate-fake.ods"
        r["edition_variant"] = "corrected"
    return f


def _both(*fs):
    def f(rows):
        for g in fs:
            g(rows)
    return f


def _fake_live_period(cur, drop=None):
    """Copy the latest quarter's live rows into a fake 2099Q1 (a brand-new
    quarter). drop: 'authority' (one authority entirely), 'cell' (one cell) or
    'category' (one category for every authority)."""
    cols = [c for c in DATA_COLS if c != "period"]
    cur.execute(f"""INSERT INTO public.{LIVE} ({', '.join(cols)}, period,
                        loaded_at)
        SELECT {', '.join(cols)}, '2099Q1', loaded_at FROM public.{LIVE}
        WHERE period = (SELECT MAX(period) FROM public.{LIVE})""")
    where = {"authority": "AND lad24cd = %s",
             "cell": "AND lad24cd = %s AND category_code = %s",
             "category": "AND category_code = %s"}
    args = {"authority": (LAD,), "cell": (LAD, CAT), "category": (CAT,)}
    if drop:
        cur.execute(f"DELETE FROM public.{LIVE} WHERE period = '2099Q1' "
                    + where[drop], args[drop])


def _hashes(cur):
    return (period_hashes(cur, LIVE, HASH_KEY),
            period_hashes(cur, LIVE, HASH_KEY, exclude=REFRESH_COLS))


def _halts(fn):
    """(True, message) if fn raised SystemExit (a halt)."""
    try:
        fn()
    except SystemExit as e:
        return True, str(e)
    return False, "no halt"


def gate_9_refresh_updates_only_the_revised_period(cur):
    name = "seeded: edition 2 of one real period -> refresh updates exactly it"

    def body(cur):
        _fake_edition(cur, "2025Q3", _both(
            _bump(), _relabel("E06000002", "hh_one_or_more_support_needs")))
        before = status(cur)
        full_b, kept_b = _hashes(cur)
        res = refresh_latest(cur)
        full_a, kept_a = _hashes(cur)
        after = status(cur)
        others = [p for p in full_b if p != "2025Q3"]
        return (before, res, after,
                all(full_b[p] == full_a[p] and kept_b[p] == kept_a[p]
                    for p in others),
                kept_b["2025Q3"] == kept_a["2025Q3"],
                full_b["2025Q3"] != full_a["2025Q3"])
    try:
        before, res, after, others_same, kept_same, changed = _in_savepoint(
            cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(9, name, False, str(e).splitlines()[0])
    ok = (before["pending_refresh"] == ["2025Q3"] and not before["ok"]
          and res["updated"] == {"2025Q3": 2} and others_same
          and kept_same and changed and after["ok"])
    report(9, name, ok, f"pending={before['pending_refresh']} "
           f"updated={res['updated']} other periods unchanged={others_same} "
           f"loaded_at and non-refresh columns unchanged={kept_same} status "
           f"after ok={after['ok']}")


def gate_10_tamper_rolls_back(cur):
    name = "seeded: tampering outside the refresh inside the transaction halts"
    cases = {
        "another period's value":
            "UPDATE public.{live} SET value = COALESCE(value, 0) + 1, "
            "value_flag = NULL WHERE period = '2024Q1' AND lad24cd = '{lad}' "
            "AND category_code = '{cat}'",
        "another period's loaded_at":
            "UPDATE public.{live} SET loaded_at = loaded_at + interval '1 day' "
            "WHERE period = '2024Q1' AND lad24cd = '{lad}' "
            "AND category_code = '{cat}'",
        "another period's row count":
            "DELETE FROM public.{live} WHERE period = '2024Q1' "
            "AND lad24cd = '{lad}' AND category_code = '{cat}'",
        "layout_version of an untouched period":
            "UPDATE public.{live} SET layout_version = 'x' "
            "WHERE period = '2024Q2' AND lad24cd = '{lad}' "
            "AND category_code = '{cat}'",
        "layout_version of the refreshed period":
            "UPDATE public.{live} SET layout_version = 'x' "
            "WHERE period = '2025Q3' AND lad24cd = '{lad}' "
            "AND category_code = '{cat}'",
        "category_group of the refreshed period":
            "UPDATE public.{live} SET category_group = 'duty_total' "
            "WHERE period = '2025Q3' AND lad24cd = '{lad}' "
            "AND category_code = '{cat}'",
    }
    fails, seen = [], []
    for what, sql in cases.items():
        def body(cur, sql=sql):
            _fake_edition(cur, "2025Q3", _bump())
            return _halts(lambda: refresh_latest(
                cur, _after_update_hook=lambda c: c.execute(
                    sql.format(live=LIVE, lad=LAD, cat=CAT))))
        try:
            halted, msg = _in_savepoint(cur, body)
        except psycopg2.Error as e:
            return report(10, name, False, str(e).splitlines()[0])
        seen.append(msg[:70])
        if not (halted and "guard:" in msg):
            fails.append(f"{what}: {msg[:80]}")
    report(10, name, not fails, "; ".join(fails) if fails else
           f"{len(cases)} tamper kinds caught by the guard")


def gate_10b_noop_changes_nothing(cur):
    name = "refresh with nothing to do writes nothing (today's database)"

    def body(cur):
        full_b, kept_b = _hashes(cur)
        res = refresh_latest(cur)
        full_a, kept_a = _hashes(cur)
        return res, full_b == full_a and kept_b == kept_a, refresh_counts(cur)
    try:
        res, same, counts = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report("10b", name, False, str(e).splitlines()[0])
    report("10b", name, res["rows"] == 0 and same and counts == {},
           f"rows={res['rows']} hashes identical={same} plan={counts}")


def gate_10c_catches_what_the_snapshots_did(cur):
    name = ("seeded: the in-transaction guard catches the faults the retired "
            "snapshot gates caught")
    # The snapshot gates flagged: a changed value, loaded_at or non-refresh
    # column in a period outside the refresh; a change to non-refresh columns
    # inside it; a category_label moving in a period that was not refreshed;
    # and a refresh that changed nothing. Each is planted below.
    cases = {
        "value in a period outside the refresh":
            "UPDATE public.{live} SET value = COALESCE(value, 0) + 1, "
            "value_flag = NULL WHERE period = '2025Q4' AND lad24cd = '{lad}' "
            "AND category_code = '{cat}'",
        "category_label in a period outside the refresh":
            "UPDATE public.{live} SET category_label = 'x' "
            "WHERE period = '2025Q4' AND lad24cd = '{lad}' "
            "AND category_code = '{cat}'",
        "source_url of 2025Q2 (a two-edition period outside the refresh)":
            "UPDATE public.{live} SET source_url = 'x' WHERE period = '2025Q2' "
            "AND lad24cd = '{lad}' AND category_code = '{cat}'",
    }
    fails = []
    for what, sql in cases.items():
        def body(cur, sql=sql):
            _fake_edition(cur, "2025Q3", _bump())
            return _halts(lambda: refresh_latest(
                cur, _after_update_hook=lambda c: c.execute(
                    sql.format(live=LIVE, lad=LAD, cat=CAT))))
        try:
            halted, msg = _in_savepoint(cur, body)
        except psycopg2.Error as e:
            return report("10c", name, False, str(e).splitlines()[0])
        if not (halted and "guard:" in msg):
            fails.append(f"{what}: {msg[:80]}")

    report("10c", name, not fails, "; ".join(fails) if fails else
           f"{len(cases)} faults caught by the guard")


def gate_11_new_period(cur):
    name = ("seeded: a new live quarter -> status not ok; sync-new gives "
            "edition 1 'as loaded', idempotent")

    def body(cur):
        _fake_live_period(cur)
        s1 = status(cur)
        halted = _halts(lambda: refresh_latest(cur))
        done = sync_new(cur)
        tip = latest_edition(cur, "2099Q1")
        cur.execute(f"""SELECT COUNT(*), MIN(release_label), MIN(source_sha256),
                               MIN(source_file) FROM public.{TABLE}
                        WHERE period = '2099Q1' AND edition = 1""")
        rows, label, sha, src = cur.fetchone()
        cols = ", ".join(DATA_COLS)
        cur.execute(f"SELECT {cols} FROM public.{LIVE} "
                    "WHERE period = '2099Q1'")
        want = live_rows_sha256([dict(zip(DATA_COLS, r))
                                 for r in cur.fetchall()])
        again = sync_new(cur)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = '2099Q1'")
        total = cur.fetchone()[0]
        return (s1, halted, done, tip, rows, label, sha, want, src, again,
                total, status(cur))
    try:
        (s1, halted, done, tip, rows, label, sha, want, src, again, total,
         s2) = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(11, name, False, str(e).splitlines()[0])
    cur.execute(f"""SELECT COUNT(*) FROM public.{LIVE} WHERE period =
                    (SELECT MAX(period) FROM public.{LIVE})""")
    n_rows = cur.fetchone()[0]
    # the convention is the one the backfill used: a real edition 1 of an
    # untouched quarter carries the canonical hash of its own rows
    cols = ", ".join(DATA_COLS)
    cur.execute(f"""SELECT {cols}, source_sha256 FROM public.{TABLE}
                    WHERE period = '2025Q3' AND edition = 1""")
    real = cur.fetchall()
    conv = {r[-1] for r in real} == {live_rows_sha256(
        [dict(zip(DATA_COLS, r[:-1])) for r in real])}
    ok = (conv and s1["new_periods"] == ["2099Q1"] and not s1["ok"]
          and halted[0] and "sync-new" in halted[1]
          and done == ["2099Q1"] and tip == 1 and rows == n_rows
          and label == LIVE_LABEL and sha == want and again == []
          and total == n_rows and s2["ok"])
    report(11, name, ok, f"new={s1['new_periods']} refresh halted={halted[0]} "
           f"synced={done} edition rows={rows} label={label!r} sha matches "
           f"canonical hash={sha == want} (convention matches a real edition 1: "
           f"{conv}) source_file={src!r} second sync="
           f"{again} status after ok={s2['ok']}")


def gate_12_drift(cur):
    name = ("seeded: live changed directly -> status reports drift; refresh "
            "halts unless accepted")

    def body(cur, period):
        cur.execute(f"""UPDATE public.{LIVE} SET value = COALESCE(value, 0)
                        + 1000, value_flag = NULL WHERE period = %s
                        AND lad24cd = %s AND category_code = %s""",
                    (period, LAD, CAT))
        st = status(cur)
        halted = _halts(lambda: refresh_latest(cur))
        stray = _halts(lambda: refresh_latest(cur, accept_drift=("2024Q1",)))
        res = refresh_latest(cur, accept_drift=(period,))
        cur.execute(f"""SELECT value FROM public.{LIVE} WHERE period = %s
                        AND lad24cd = %s AND category_code = %s""",
                    (period, LAD, CAT))
        back = cur.fetchone()[0]
        cur.execute(f"""SELECT value FROM public.{TABLE} WHERE period = %s
                        AND edition = %s AND lad24cd = %s
                        AND category_code = %s""",
                    (period, latest_edition(cur, period), LAD, CAT))
        tip_value = cur.fetchone()[0]
        return st, halted, stray, res, status(cur), back == tip_value
    oks, notes = [], []
    for period in ("2025Q3", "2025Q2"):  # one edition; two editions
        try:
            st, halted, stray, res, st2, restored = _in_savepoint(
                cur, lambda c, p=period: body(c, p))
        except (psycopg2.Error, SystemExit) as e:
            return report(12, name, False, str(e).splitlines()[0])
        oks.append(st["drift_periods"] == [period] and not st["ok"]
                   and halted[0] and "--accept-drift" in halted[1]
                   and stray[0] and period in res["drift_accepted"]
                   and list(res["updated"]) == [period] and st2["ok"]
                   and restored)
        notes.append(f"{period}: drift={st['drift_periods']} halted="
                     f"{halted[0]} accepted->{list(res['updated'])} "
                     f"status after ok={st2['ok']}")
    report(12, name, all(oks), "; ".join(notes))


def gate_13_today(cur):
    name = ("today's database: status ok, nothing to refresh, chain valid for "
            "every period")
    try:
        st = status(cur)
        plan = refresh_counts(cur)
    except (SystemExit, ValueError, LookupError) as e:
        return report(13, name, False, str(e))
    report(13, name, st["ok"] and plan == {},
           f"periods={st['periods']} status ok={st['ok']} plan={plan}")


def gate_14_short_quarter_and_categories(cur):
    name = ("seeded: a short quarter or a changed category set is refused by "
            "sync-new, not absorbed (counts derived)")
    auths = expected_authorities(cur)
    cats = {p: c for p, c in edition1_category_counts(cur).items()}
    top = max(cats)
    n_cats = cats[top]

    def case(drop):
        def body(cur):
            _fake_live_period(cur, drop)
            st = status(cur)
            return st, _halts(lambda: sync_new(cur))
        return _in_savepoint(cur, body)
    try:
        st_a, h_a = case("authority")
        st_c, h_c = case("cell")
        st_g, h_g = case("category")

        def accepted(cur):
            _fake_live_period(cur, "category")
            stray = _halts(lambda: sync_new(cur, accept_categories=("2025Q1",)))
            done = sync_new(cur, accept_categories=("2099Q1",))
            return stray, done, edition1_category_counts(cur).get("2099Q1")
        stray, done, c_new = _in_savepoint(cur, accepted)
    except (psycopg2.Error, SystemExit) as e:
        return report(14, name, False, str(e).splitlines()[0])
    want = auths * n_cats
    ok = (st_a["bad_counts"].get("2099Q1") == (want - n_cats, want)
          and h_a[0] and f"{auths - 1} authorities" in h_a[1]
          and st_c["bad_counts"].get("2099Q1") == (want - 1, want)
          and h_c[0] and f"{want - 1} live rows" in h_c[1]
          and h_g[0] and "category set differs" in h_g[1]
          and "--accept-categories 2099Q1" in h_g[1]
          and stray[0] and "not periods awaiting" in stray[1]
          and done == ["2099Q1"] and c_new == n_cats - 1)
    report(14, name, ok, f"one authority short: bad_counts="
           f"{st_a['bad_counts']}, halt={h_a[1][:60]!r}; one cell short: "
           f"halt={h_c[1][:60]!r}; one category short: halt={h_g[1][:70]!r}; "
           f"accepted -> {done} with {c_new} categories")


def gate_15_chain_error_reported(cur):
    name = "seeded: a forked supersedes chain shows up in status as a chain error"

    def body(cur):
        _fake_edition(cur, "2025Q3", _bump())
        # a third edition that also supersedes edition 1 forks the chain
        cur.execute(f"""INSERT INTO public.{TABLE} ({', '.join(DATA_COLS)},
                        edition, supersedes, source_sha256)
                        SELECT {', '.join(DATA_COLS)}, 3, 1, 'gate-fork'
                        FROM public.{TABLE}
                        WHERE period = '2025Q3' AND edition = 1""")
        return status(cur)
    try:
        st = _in_savepoint(cur, body)
    except psycopg2.Error as e:
        return report(15, name, False, str(e).splitlines()[0])
    report(15, name, "2025Q3" in st["chain_errors"] and not st["ok"],
           str(st["chain_errors"])[:120])


def gate_16_bootstrap_needs_explicit_count(cur):
    name = ("seeded: --expected-authorities only when no count is derivable "
            "(or equal to it)")
    import s1b_editions as m
    n = expected_authorities(cur)

    def body(cur):
        _fake_live_period(cur, "authority")
        # derivable count (real periods have editions): a different N halts
        differs = _halts(lambda: sync_new(cur, n - 1))
        # derivable and equal to N: accepted, the short quarter still halts
        equal = _halts(lambda: sync_new(cur, n))
        # no period has editions: simulate the bootstrap
        saved = m.latest_map, m.authority_counts
        m.latest_map = lambda cur, *a, **k: ({}, ["2099Q1"], {})
        m.authority_counts = lambda cur, *a, **k: {"2099Q1": n - 1}
        try:
            no_arg = _halts(lambda: sync_new(cur))
            short = _halts(lambda: sync_new(cur, n))
            explicit = sync_new(cur, n - 1)
        finally:
            m.latest_map, m.authority_counts = saved
        return differs, no_arg, short, explicit, equal
    try:
        differs, no_arg, short, explicit, equal = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(16, name, False, str(e).splitlines()[0])
    ok = (differs[0] and "differs from the count" in differs[1]
          and no_arg[0] and "--expected-authorities" in no_arg[1]
          and short[0] and f"{n - 1} authorities" in short[1]
          and explicit == ["2099Q1"]
          and equal[0] and f"{n - 1} authorities" in equal[1])
    report(16, name, ok, f"derivable, N differs: {differs[1][:50]}; bootstrap "
           f"no N: {no_arg[1][:40]}; explicit {n - 1}: {explicit}")


# Retired literals: the snapshot table names, the typed row totals, the fixed
# period list and a typed authority or row count.
RETIRED_B = RETIRED + ("LIVE_ROWS_TOTAL", "EDITION_ROWS_TOTAL",
                       "BACKFILL_ROWS_TOTAL", "101232", "110408", "9176",
                       "9472")
# Functions allowed to mention them, by name, each with the reason.
ALLOWED_S1B = {
    # one-off historical markdown report of the 2026-10 registry files: it
    # iterates the seven restated quarters on purpose; writes no table
    "cmd_dryrun_all",
}
ALLOWED_S1B_VERIFY = {
    # gates 6-8 check the seven quarters restated in the 2026-10 reload, which
    # is historical by definition
    "_revised_editions",
    # this gate has to name the literals it looks for (and plants them)
    "gate_17_no_snapshot_dependency",
}
ALLOWED_MODULE_B = {
    # imported for cmd_dryrun_all (historical report) only
    "STALE_PERIODS",
    # the scanner's own literal list and the allow-lists (this file only)
    "RETIRED_B", "ALLOWED_S1B", "ALLOWED_S1B_VERIFY", "ALLOWED_MODULE_B",
    "REFRESH_PATH_B",
}
REFRESH_PATH_B = ("status", "sync_new", "refresh_latest", "refresh_counts",
                  "_plan", "_record_as_loaded", "expected_rows",
                  "check_coverage", "check_loaded", "check_flag_integrity",
                  "check_s1_cross", "run_backfill_gates")


def gate_17_no_snapshot_dependency(cur):
    name = ("no code path depends on the retired snapshot tables, fixed counts "
            "or period lists")
    here = Path(__file__).resolve().parent
    b_text = (here / "s1b_editions.py").read_text(encoding="utf-8")
    vf_text = (here / "s1b_editions_verify.py").read_text(encoding="utf-8")
    sv_text = (here / "s1b_support_needs_verify.py").read_text(encoding="utf-8")
    missing = [f for f in REFRESH_PATH_B if f not in _defined_functions(b_text)]
    hits = [f"s1b_editions.{fn}: {w}"
            for fn, w in scan_retired(b_text, ALLOWED_S1B, ALLOWED_MODULE_B,
                                      RETIRED_B)]
    hits += [f"s1b_editions_verify.{fn}: {w}"
             for fn, w in scan_retired(vf_text, ALLOWED_S1B_VERIFY,
                                       ALLOWED_MODULE_B, RETIRED_B)]
    # the independent support-needs suite must not read the snapshots either
    # (its own 296-authority universe is a deliberate independent check)
    hits += [f"s1b_support_needs_verify.{fn}: {w}"
             for fn, w in scan_retired(sv_text, set(), ALLOWED_MODULE_B,
                                       ("_bak_", "BAK", "STALE_PERIODS"))]
    # seeded: the scanner reports a planted literal in a function that is not
    # allow-listed, ignores it in an allow-listed one, and sees module level
    planted = ("def refresh_x(cur):\n    cur.execute('SELECT 1 FROM "
               "la_homelessness_support_needs_bak_20261006b')\n"
               "def old_report(cur):\n    return 101232\n"
               "def gate_x(cur):\n    return BACKFILL_ROWS_TOTAL\n"
               "X = 296\nSTALE_PERIODS = ('2023Q2',)\n")
    got = scan_retired(planted, {"old_report"}, ALLOWED_MODULE_B, RETIRED_B)
    seeded = (("refresh_x", "_bak_") in got
              and ("gate_x", "BACKFILL_ROWS_TOTAL") in got
              and not any(f == "old_report" for f, _ in got)
              and ("<module>", "296") in got
              and not any(w == "STALE_PERIODS" for _, w in got))
    report(17, name, not hits and not missing and seeded,
           f"missing={missing} hits={hits[:6]} seeded scanner ok={seeded}"
           if hits or missing or not seeded else
           f"three files scanned (functions and module level); allow-listed: "
           f"{sorted(ALLOWED_S1B | ALLOWED_S1B_VERIFY)}; planted literal "
           f"reported={got}")


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
            gate_6_loaded(cur)
            gate_7_flag_integrity(cur)
            gate_8_s1_cross(cur)
            gate_6s_seeded(cur)
            gate_7s_seeded(cur)
            gate_8s_seeded(cur)
            gate_9_refresh_updates_only_the_revised_period(cur)
            gate_10_tamper_rolls_back(cur)
            gate_10b_noop_changes_nothing(cur)
            gate_10c_catches_what_the_snapshots_did(cur)
            gate_11_new_period(cur)
            gate_12_drift(cur)
            gate_13_today(cur)
            gate_14_short_quarter_and_categories(cur)
            gate_15_chain_error_reported(cur)
            gate_16_bootstrap_needs_explicit_count(cur)
            gate_17_no_snapshot_dependency(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
