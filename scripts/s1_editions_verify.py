"""Gates for the S1 edition-history table.

Prints `GATE n name: PASS/FAIL`; exits 1 on any FAIL. Gates 1-3 cover the
table shape and immutability, 4-5 the backfill, 6-8 the revised 2023Q2-2024Q4
editions (registry source, no suppressed value stored as 0, support-need totals
equal to an independent S1b-reader re-read of the same file). Gates 6-8 print `FAIL (pending load)` while any of those seven
quarters has no edition 2 or later, so the exit code is 1 until they are loaded.
Gates 9-17 cover the generalised refresh, each as a seeded scenario inside a
rolled-back savepoint (the editions table is append-only, so a seeded edition
can only exist inside one): an edition 2 of one real period is refreshed and
nothing else moves (9); tampering outside the refresh inside the transaction
halts it (10, 10b); the W1 outputs move only where a current-quarter or
prior-year TA figure changed (11, 11s); a new live quarter makes status fail
and sync-new records it as edition 1, idempotently (12); a live row changed
directly is reported as drift and refresh halts unless accepted (13); today's
database is clean (14); a short quarter is flagged because counts are derived
(15); a forked chain is reported (16); nothing depends on the retired snapshot
tables, fixed counts or period lists (17); sync-new needs an explicit
count when no period has editions (18). The per-period before/after content hash
(excluding loaded_at) replaces the old snapshot comparison.

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
                         NATIONAL_TA_COLS, REFRESH_COLS, TA_SIGNAL_COLS,
                         check_no_suppressed_zero, check_registry,
                         check_support_needs, check_w1_equivalence,
                         expected_authorities, insert_edition, latest_edition,
                         LIVE_LABEL, live_text_sha256, modal_count,
                         period_hashes,
                         refresh_counts, refresh_latest, reproduction_update,
                         reproduction_verdict, status, sync_new,
                         w1_snapshot, LIVE)

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


def _raw_chain(cur, rows):
    """Insert editions of 2099Q1 directly with arbitrary supersedes values
    (insert_edition would refuse them); rows = [(edition, supersedes)]."""
    lad = _first_lad(cur)
    for ed, sup in rows:
        cur.execute(f"""INSERT INTO public.{TABLE} (lad24cd, period, edition,
                        total_assessments, source_file, source_sha256,
                        supersedes) VALUES (%s, '2099Q1', %s, 1,
                        'gate-throwaway', %s, %s)""",
                    (lad, ed, f"r{ed}", sup))


def gate_3h_mutual_supersede(cur):
    name = "latest_edition: 1 root plus two editions superseding each other raises"
    if not table_exists(cur):
        return report("3h", name, False, f"{TABLE} absent")

    def body(cur):
        _raw_chain(cur, [(1, None), (2, 3), (3, 2)])
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    ok, msg = _in_savepoint(cur, body)
    report("3h", name, ok, msg)


def gate_3i_self_supersede(cur):
    name = "latest_edition: an edition superseding itself raises"
    if not table_exists(cur):
        return report("3i", name, False, f"{TABLE} absent")

    def body(cur):
        _raw_chain(cur, [(1, None), (2, 1), (3, 3)])
        return _raises(lambda: latest_edition(cur, "2099Q1"), ValueError)
    ok, msg = _in_savepoint(cur, body)
    report("3i", name, ok, msg)


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
    name = "every quarter has edition 1 and the derived authority count per edition"
    if not table_exists(cur):
        return report(4, name, False, f"{TABLE} absent")
    cur.execute("SELECT DISTINCT period FROM public.la_statutory_homelessness")
    periods = sorted(r[0] for r in cur.fetchall())
    want = expected_authorities(cur)
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
                for k, v in e.items() if v != (want, want)]
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
    hart = dict(cur.fetchall())
    cur.execute(f"""SELECT total_assessments FROM public.{LIVE}
                    WHERE period = '2025Q2' AND lad24cd = 'E06000001'""")
    live_h = cur.fetchone()[0]
    latest_h = hart.get(latest_edition(cur, "2025Q2"))
    if hart.get(1) != 138 or hart.get(2) != 159 or latest_h != live_h:
        bad.append(f"Hartlepool 2025Q2 total_assessments by edition {hart}, "
                   f"live {live_h}: expected edition 1 = 138, edition 2 = 159 "
                   "and live = the latest edition")
    report(5, name, bool(periods) and not bad,
           "; ".join(bad[:5]) if bad else f"{len(periods)} periods match; "
           "Hartlepool ed1 138, ed2 159, live = latest")


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
    name = "2023Q2-2024Q4 have a registry edition >= 2, derived authority count"
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
    name = "support_needs_total equals independent S1b-reader A3 re-read, every authority"
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
        report(8, name, True, f"{done} periods {expected_authorities(cur)}/{expected_authorities(cur)}")
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
    name = "seeded: gate 8 check flags one authority short of the derived count"
    if not table_exists(cur):
        return report("8s", name, False, f"{TABLE} absent")
    cur.execute(f"""SELECT lad24cd, support_needs_total FROM public.{TABLE}
                    WHERE period = '2023Q2' AND edition = 1""")
    exp = dict(cur.fetchall())
    clean = check_support_needs(cur, "2023Q2", 1, expected=dict(exp))
    lad = min(exp)
    exp[lad] = -1 if exp[lad] is None else exp[lad] + 1
    bad = check_support_needs(cur, "2023Q2", 1, expected=exp)
    n = expected_authorities(cur)
    report("8s", name, not clean and any(f"{n - 1}/{n}" in x for x in bad),
           f"clean={clean}; seeded={bad}")


LAD = "E06000001"  # a real authority present in every period


def _fake_edition(cur, period, mutate=None, *, file="gate-fake.ods") -> int:
    """Append a fake next edition of a real period, a copy of the chain tip
    with `mutate({lad: {measure: value}})` applied. The editions table is
    append-only, so the caller must be inside a savepoint it rolls back."""
    tip = latest_edition(cur, period)
    cols = ", ".join(("lad24cd",) + MEASURES)
    cur.execute(f"SELECT {cols} FROM public.{TABLE} "
                "WHERE period = %s AND edition = %s", (period, tip))
    maps = {r[0]: dict(zip(MEASURES, r[1:])) for r in cur.fetchall()}
    if mutate:
        mutate(maps)
    recs = [dict(lad24cd=k, **v) for k, v in maps.items()]
    return insert_edition(cur, recs, period, release_label="gate-fake",
                          published_date=date(2099, 1, 1), source_url=None,
                          source_file=file,
                          source_sha256=f"gate-fake-{period}-{tip}",
                          supersedes=tip)


def _bump(col, by=1, lad=LAD):
    def f(maps):
        maps[lad][col] = (maps[lad][col] or 0) + by
    return f


def _fake_live_period(cur, drop_one=False):
    """Copy the current quarter's live rows into a fake 2099Q1 (a brand-new
    quarter)."""
    cur.execute(f"""INSERT INTO public.{LIVE}
        (lad24cd, period, {', '.join(MEASURES)}, loaded_at, source_file,
         extracted_at)
        SELECT lad24cd, '2099Q1', {', '.join(MEASURES)}, loaded_at,
               source_file, extracted_at
        FROM public.{LIVE} WHERE period = (SELECT MAX(period)
                                           FROM public.{LIVE})""")
    if drop_one:
        cur.execute(f"DELETE FROM public.{LIVE} WHERE period = '2099Q1' "
                    "AND lad24cd = %s", (LAD,))


def _hashes(cur):
    key = ("period", "lad24cd")
    return (period_hashes(cur, LIVE, key),
            period_hashes(cur, LIVE, key, exclude=REFRESH_COLS))


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
        _fake_edition(cur, "2025Q3", _bump("owed_duty"))
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
          and list(res["updated"]) == ["2025Q3"] and others_same
          and kept_same and changed and after["ok"])
    report(9, name, ok, f"pending={before['pending_refresh']} "
           f"updated={res['updated']} other periods unchanged={others_same} "
           f"suspect/loaded_at unchanged={kept_same} status after ok="
           f"{after['ok']}")


def gate_10_tamper_rolls_back(cur):
    name = "seeded: tampering outside the refresh inside the transaction halts"
    cases = {
        "another period's measure":
            "UPDATE public.{live} SET total_assessments = "
            "COALESCE(total_assessments, 0) + 1 "
            "WHERE period = '2024Q1' AND lad24cd = '{lad}'",
        "another period's loaded_at":
            "UPDATE public.{live} SET loaded_at = loaded_at + interval '1 day' "
            "WHERE period = '2024Q1' AND lad24cd = '{lad}'",
        "another period's row count":
            "DELETE FROM public.{live} WHERE period = '2024Q1' "
            "AND lad24cd = '{lad}'",
        "a *_suspect column of an untouched period":
            "UPDATE public.{live} SET drug_dependency_suspect = "
            "COALESCE(drug_dependency_suspect, 0) + 1 "
            "WHERE period = '2024Q2' AND lad24cd = '{lad}'",
        "a *_suspect column of the refreshed period":
            "UPDATE public.{live} SET mental_health_suspect = "
            "COALESCE(mental_health_suspect, 0) + 1 "
            "WHERE period = '2025Q3' AND lad24cd = '{lad}'",
    }
    fails, seen = [], []
    for what, sql in cases.items():
        def body(cur, sql=sql):
            _fake_edition(cur, "2025Q3", _bump("owed_duty"))
            return _halts(lambda: refresh_latest(
                cur, _after_update_hook=lambda c: c.execute(
                    sql.format(live=LIVE, lad=LAD))))
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


def gate_11_w1_equivalence(cur):
    name = "W1 outputs move only for authorities whose current/prior-year TA changed"
    out = []

    def run(period, col, by, lad=LAD):
        def body(cur):
            _fake_edition(cur, period, _bump(col, by, lad))
            return refresh_latest(cur)
        return _in_savepoint(cur, body)
    try:
        snap = w1_snapshot(cur)  # derives current/prior quarter as W1 does
        top, prev = snap["top"], snap["prev"]
        cur_res = run(top, "households_in_ta", 7)
        prev_res = run(prev, "households_in_ta", 5, "E06000002")
        non_ta = run(top, "owed_duty", 3)
    except (psycopg2.Error, SystemExit) as e:
        return report(11, name, False, str(e).splitlines()[0])
    w = cur_res["w1"]
    ok = (w["ta_changed_authorities"] == [LAD]
          and set(w["authorities_differing"]) <= set(w["ta_changed_authorities"])
          and w["authorities_differing"]
          and set(w["columns"]) <= TA_SIGNAL_COLS
          and w["national_columns_changed"]
          and set(w["national_columns_changed"]) <= NATIONAL_TA_COLS)
    out.append(f"{top} (current) TA +7 -> signals differ for "
               f"{w['authorities_differing']} in {sorted(w['columns'])}, "
               f"national {w['national_columns_changed']}")
    w = prev_res["w1"]
    ok = ok and (w["ta_changed_authorities"] == ["E06000002"]
                 and set(w["authorities_differing"]) <= {"E06000002"}
                 and set(w["columns"]) <= TA_SIGNAL_COLS)
    out.append(f"{prev} (prior year) TA +5 -> {w['authorities_differing']}")
    w = non_ta["w1"]
    ok = ok and not w["authorities_differing"] and not w["national_columns_changed"]
    out.append("non-TA measure change -> no signals move")
    report(11, name, ok, "; ".join(out))


def gate_11s_w1_negative(cur):
    name = "seeded: W1 check flags signals moving where TA did not, and non-TA columns"

    def body(cur):
        before = w1_snapshot(cur)
        cur.execute(f"""UPDATE public.{LIVE} SET households_in_ta =
                        COALESCE(households_in_ta, 0) + 7
                        WHERE period = %s AND lad24cd = %s""",
                    (before["top"], LAD))
        after = w1_snapshot(cur)
        return before, after
    try:
        before, after = _in_savepoint(cur, body)
    except psycopg2.Error as e:
        return report("11s", name, False, str(e).splitlines()[0])
    truthful, _ = check_w1_equivalence(before, after)
    # claim the TA figure did not change: the same signals movement is wrong
    denied = dict(after, ta=dict(before["ta"]))
    untrue, _ = check_w1_equivalence(before, denied)
    # a non-TA signal column moving
    j = before["cols"].index("la_name")
    row = list(after["signals"][LAD])
    row[j] = "changed"
    tampered = dict(after, signals=dict(after["signals"], **{LAD: tuple(row)}))
    non_ta, _ = check_w1_equivalence(before, tampered)
    ok = (not truthful and any("did not" in x for x in untrue)
          and any("non-TA signal columns" in x for x in non_ta))
    report("11s", name, ok, f"truthful={truthful}; denied={untrue[:1]}; "
           f"non-TA={non_ta[:1]}")


def gate_12_new_period(cur):
    name = "seeded: a new live quarter -> status not ok; sync-new gives edition 1, idempotent"

    def body(cur):
        _fake_live_period(cur)
        s1 = status(cur)
        halted = _halts(lambda: refresh_latest(cur))
        done = sync_new(cur)
        tip = latest_edition(cur, "2099Q1")
        cur.execute(f"""SELECT COUNT(*), MIN(release_label), MIN(source_sha256)
                        FROM public.{TABLE} WHERE period = '2099Q1'
                        AND edition = 1""")
        rows, label, sha = cur.fetchone()
        again = sync_new(cur)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = '2099Q1'")
        total = cur.fetchone()[0]
        return s1, halted, done, tip, rows, label, sha, again, total, status(cur)
    try:
        s1, halted, done, tip, rows, label, sha, again, total, s2 = \
            _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(12, name, False, str(e).splitlines()[0])
    n_auth = expected_authorities(cur)
    cur.execute(f"SELECT MAX(period) FROM public.{LIVE}")
    newest = cur.fetchone()[0]
    cur.execute(f"SELECT {', '.join(('lad24cd',) + MEASURES)} "
                f"FROM public.{TABLE} WHERE period = %s AND edition = %s",
                (newest, latest_edition(cur, newest)))
    want = live_text_sha256([dict(zip(("lad24cd",) + MEASURES, r))
                             for r in cur.fetchall()])
    ok = (s1["new_periods"] == ["2099Q1"] and not s1["ok"]
          and halted[0] and "sync-new" in halted[1]
          and done == ["2099Q1"] and tip == 1 and rows == n_auth
          and label == LIVE_LABEL and sha == want
          and again == [] and total == n_auth and s2["ok"])
    report(12, name, ok, f"new={s1['new_periods']} refresh halted={halted[0]} "
           f"synced={done} edition rows={rows} label={label!r} sha matches "
           f"current quarter's canonical hash={sha == want} second sync={again} "
           f"status after ok={s2['ok']}")


def gate_13_drift(cur):
    name = "seeded: live changed directly -> status reports drift; refresh halts unless accepted"

    def body(cur, period):
        cur.execute(f"""UPDATE public.{LIVE} SET total_assessments =
                        COALESCE(total_assessments, 0) + 1000
                        WHERE period = %s AND lad24cd = %s""", (period, LAD))
        st = status(cur)
        halted = _halts(lambda: refresh_latest(cur))
        stray = _halts(lambda: refresh_latest(cur, accept_drift=("2024Q1",)))
        full_b, _ = _hashes(cur)
        res = refresh_latest(cur, accept_drift=(period,))
        st2 = status(cur)
        return st, halted, stray, res, st2
    oks, notes = [], []
    for period in ("2025Q3", "2025Q2"):  # one edition; two editions
        try:
            st, halted, stray, res, st2 = _in_savepoint(
                cur, lambda c, p=period: body(c, p))
        except (psycopg2.Error, SystemExit) as e:
            return report(13, name, False, str(e).splitlines()[0])
        oks.append(st["drift_periods"] == [period] and not st["ok"]
                   and halted[0] and "--accept-drift" in halted[1]
                   and stray[0] and period in res["drift_accepted"]
                   and list(res["updated"]) == [period] and st2["ok"])
        notes.append(f"{period}: drift={st['drift_periods']} halted="
                     f"{halted[0]} accepted->{list(res['updated'])} "
                     f"status after ok={st2['ok']}")
    report(13, name, all(oks), "; ".join(notes))


def gate_14_today(cur):
    name = "today's database: status ok, nothing to refresh, chain valid for every period"
    try:
        st = status(cur)
        plan = refresh_counts(cur)
    except (SystemExit, ValueError, LookupError) as e:
        return report(14, name, False, str(e))
    report(14, name, st["ok"] and plan == {},
           f"periods={st['periods']} status ok={st['ok']} plan={plan}")


def gate_15_derived_counts(cur):
    name = "seeded: a quarter one authority short is flagged, not absorbed (counts derived)"

    def body(cur):
        _fake_live_period(cur, drop_one=True)
        st = status(cur)
        halted = _halts(lambda: sync_new(cur))
        return st, halted
    try:
        st, halted = _in_savepoint(cur, body)
    except psycopg2.Error as e:
        return report(15, name, False, str(e).splitlines()[0])
    n = expected_authorities(cur)
    unit = (modal_count({"a": 7, "b": 7, "c": 6}) == 7
            and modal_count({}) is None)
    ok = (st["bad_counts"].get("2099Q1") == (n - 1, n) and not st["ok"]
          and halted[0] and f"{n - 1} live rows, expected {n}" in halted[1]
          and unit)
    report(15, name, ok, f"bad_counts={st['bad_counts']} sync-new: "
           f"{halted[1][:80]}")


def gate_16_chain_error_reported(cur):
    name = "seeded: a forked supersedes chain shows up in status as a chain error"

    def body(cur):
        tip0 = latest_edition(cur, "2025Q3")
        ed = _fake_edition(cur, "2025Q3", _bump("owed_duty"))
        # another edition that also supersedes the old tip forks the chain
        cur.execute(f"""INSERT INTO public.{TABLE} (lad24cd, period, edition,
                        total_assessments, source_file, source_sha256,
                        supersedes) VALUES (%s, '2025Q3', %s, 1, 'gate-fake',
                        'gate-fork', %s)""", (LAD, ed + 1, tip0))
        return status(cur), ed
    try:
        st, _ = _in_savepoint(cur, body)
    except psycopg2.Error as e:
        return report(16, name, False, str(e).splitlines()[0])
    report(16, name, "2025Q3" in st["chain_errors"] and not st["ok"],
           str(st["chain_errors"])[:120])


def gate_19_url_mismatch_not_a_failure(cur):
    name = "reproduction verdict rests on cells only; a file_url mismatch is a note"
    same = reproduction_verdict(0, "u", "u")
    differ = reproduction_verdict(0, "page", "registry")
    cells = reproduction_verdict(3, "u", "u")
    both = reproduction_verdict(3, "page", "registry")
    ok = (same == (True, "")
          and differ[0] is True and "file_url differs" in differ[1]
          and cells == (False, "")
          and both[0] is False and "file_url differs" in both[1])

    # end to end where a manifest file is available: 2025Q1-Q4 and the
    # revised quarters carry URLs that differ from the edition's source_url
    cur.execute("""SELECT h.period FROM public.homelessness_quarter_urls h
                   JOIN (SELECT DISTINCT period, source_url FROM
                         public.{t}) e USING (period)
                   WHERE h.file_url IS DISTINCT FROM e.source_url""".format(
        t=TABLE))
    mism = [r[0] for r in cur.fetchall()]

    def body(cur):
        return reproduction_update(cur, mism[:1]) if mism else []
    try:
        res = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(19, name, False, str(e).splitlines()[0])
    e2e = all(r[2] is not False for r in res)
    report(19, name, ok and e2e,
           f"verdicts ok={ok}; periods whose file_url differs from the "
           f"edition URL: {len(mism)}; reproduction_update on one -> {res}")


def gate_20_missing_row_halts(cur):
    name = "seeded: an accepted drift that is only a missing live row halts, not 'nothing to refresh'"

    def body(cur):
        cur.execute(f"DELETE FROM public.{LIVE} WHERE period = '2025Q3' "
                    "AND lad24cd = %s", (LAD,))
        st = status(cur)
        plan_halt = _halts(lambda: refresh_latest(cur, accept_drift=("2025Q3",)))
        return st, plan_halt
    try:
        st, h = _in_savepoint(cur, body)
    except psycopg2.Error as e:
        return report(20, name, False, str(e).splitlines()[0])
    ok = (not st["ok"] and h[0] and "only one of them" in h[1])
    report(20, name, ok, f"status ok={st['ok']}; refresh: {h[1][:90]}")


# Retired literals: the snapshot table names, the fixed live row count, the
# fixed period list and a typed authority count.
RETIRED = ("_bak_", "BAK", "3256", "STALE_PERIODS", "296")
# Functions allowed to mention them, by name, each with the reason.
ALLOWED_S1 = {
    # one-off historical markdown report of the 2026-10 reload candidates:
    # iterates the seven restated quarters on purpose; writes no table
    "cmd_dryrun_all",
}
ALLOWED_VERIFY = {
    # gates 6-8 check the seven quarters restated in the 2026-10 reload, which
    # is historical by definition
    "_revised_editions",
    # this gate has to name the literals it looks for (and plants them)
    "gate_17_no_snapshot_dependency",
}
# Module-level assignments allowed to carry a retired literal:
ALLOWED_MODULE = {
    # defined for the historical dryrun-all reports and gates 6-8 (both tables)
    "STALE_PERIODS",
    # the scanner's own literal list and this allow-list (this file only)
    "RETIRED",
    "ALLOWED_MODULE",
}
REFRESH_PATH = ("refresh_latest", "refresh_counts", "status", "sync_new",
                "classify_period", "guard_problems", "check_w1_equivalence",
                "_plan", "reproduction_update", "latest_map",
                "w1_snapshot", "period_hashes")


def scan_retired(text, allowed_funcs, allowed_module=ALLOWED_MODULE,
                 literals=RETIRED) -> list:
    """[(function or '<module>', literal)] for every retired literal found in
    the source text: inside each top-level function not in allowed_funcs, and
    in module-level statements other than docstrings, imports of an allowed
    name, and assignments to an allowed name."""
    import ast
    tree = ast.parse(text)
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in allowed_funcs:
                continue
            where = node.name
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in allowed_module
                for t in node.targets):
            continue
        elif isinstance(node, ast.Expr) and isinstance(
                getattr(node, "value", None), ast.Constant):
            continue  # docstring / bare string
        elif isinstance(node, ast.ImportFrom) and all(
                a.name in allowed_module for a in node.names
                if a.name in literals):
            continue
        else:
            where = "<module>"
        src = ast.get_source_segment(text, node) or ""
        out += [(where, w) for w in literals if w in src]
    return out


def _defined_functions(text):
    import ast
    return {n.name for n in ast.parse(text).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def gate_17_no_snapshot_dependency(cur):
    name = "no code path depends on the retired snapshot tables, fixed counts or period lists"
    here = Path(__file__).resolve().parent
    s1_text = (here / "s1_editions.py").read_text(encoding="utf-8")
    vf_text = (here / "s1_editions_verify.py").read_text(encoding="utf-8")
    missing = [f for f in REFRESH_PATH if f not in _defined_functions(s1_text)]
    hits = [f"s1_editions.{fn}: {w}"
            for fn, w in scan_retired(s1_text, ALLOWED_S1)]
    hits += [f"s1_editions_verify.{fn}: {w}"
             for fn, w in scan_retired(vf_text, ALLOWED_VERIFY)]
    # seeded: the scanner reports a planted literal in a function that is not
    # allow-listed, ignores it in an allow-listed one, and sees module level
    planted = ("def refresh_x(cur):\n    cur.execute('SELECT 1 FROM "
               "la_statutory_homelessness_bak_1')\n"
               "def old_report(cur):\n    return 3256\n"
               "X = 296\nSTALE_PERIODS = ('2023Q2',)\n")
    got = scan_retired(planted, {"old_report"})
    seeded = (("refresh_x", "_bak_") in got
              and not any(f == "old_report" for f, _ in got)
              and ("<module>", "296") in got
              and not any(w == "STALE_PERIODS" for _, w in got))
    report(17, name, not hits and not missing and seeded,
           f"missing={missing} hits={hits[:6]} seeded scanner ok={seeded}"
           if hits or missing or not seeded else
           f"both files scanned (functions and module level); allow-listed: "
           f"{sorted(ALLOWED_S1 | ALLOWED_VERIFY)}; planted literal "
           f"reported={got}")


def gate_18_bootstrap_needs_explicit_count(cur):
    name = "seeded: --expected-authorities only when no count is derivable (or equal to it)"
    import s1_editions as m
    n = expected_authorities(cur)

    def body(cur):
        _fake_live_period(cur, drop_one=True)
        # derivable count (real periods have editions): a different N halts
        differs = _halts(lambda: sync_new(cur, n - 1))
        # derivable and equal to N: accepted, the short quarter still halts
        equal = _halts(lambda: sync_new(cur, n))
        # no period has editions: simulate the bootstrap
        saved = m.latest_map, m.live_period_counts
        m.latest_map = lambda cur, *a, **k: ({}, ["2099Q1"], {})
        m.live_period_counts = lambda cur, *a, **k: {"2099Q1": n - 1}
        try:
            no_arg = _halts(lambda: sync_new(cur))
            short = _halts(lambda: sync_new(cur, n))
            explicit = sync_new(cur, n - 1)
        finally:
            m.latest_map, m.live_period_counts = saved
        return differs, no_arg, short, explicit, equal
    try:
        differs, no_arg, short, explicit, equal = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(18, name, False, str(e).splitlines()[0])
    ok = (differs[0] and "differs from the count" in differs[1]
          and no_arg[0] and "--expected-authorities" in no_arg[1]
          and short[0] and f"{n - 1} live rows, expected {n}" in short[1]
          and explicit == ["2099Q1"]
          and equal[0] and f"{n - 1} live rows, expected {n}" in equal[1])
    report(18, name, ok, f"derivable, N differs: {differs[1][:50]}; bootstrap "
           f"no N: {no_arg[1][:40]}; explicit {n - 1}: {explicit}")


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
            gate_3h_mutual_supersede(cur)
            gate_3i_self_supersede(cur)
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
            gate_9_refresh_updates_only_the_revised_period(cur)
            gate_10_tamper_rolls_back(cur)
            gate_10b_noop_changes_nothing(cur)
            gate_11_w1_equivalence(cur)
            gate_11s_w1_negative(cur)
            gate_12_new_period(cur)
            gate_13_drift(cur)
            gate_14_today(cur)
            gate_15_derived_counts(cur)
            gate_16_chain_error_reported(cur)
            gate_17_no_snapshot_dependency(cur)
            gate_18_bootstrap_needs_explicit_count(cur)
            gate_19_url_mismatch_not_a_failure(cur)
            gate_20_missing_row_halts(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
