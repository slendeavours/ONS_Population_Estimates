"""Gates for the RO4 housing-expenditure edition-history table.

Mirrors scripts/s1b_editions_verify.py. Prints `GATE n name: PASS/FAIL`; exits 1
on any FAIL. Gates 1-3 cover table shape, immutability and the supersedes-chain
helpers (the shared s1_editions.latest_edition, called with table= and
period_col='financial_year'); 4-5 cover the backfill (coverage, edition 1 equals
the live table row for row, NULL not 0); 6-7 cover provenance (manifest, file
hashes) and an independent re-parse of the local ods files; 8 is today's status;
9 a seeded newer edition; 10 the retired-literal scan. Gates ending in `s` are
seeded: they prove the check itself flags a planted fault.

Usage:
    python scripts/ro4_editions_verify.py

Everything that writes runs inside a savepoint that is always rolled back and
the run ends in a rollback, so no table is left changed. Nothing is written to
the live table.
"""
import inspect
import sys
from datetime import date
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402
import ro4_editions as m  # noqa: E402
import s1_editions  # noqa: E402
import s1b_editions  # noqa: E402
from s1_editions import (EDITION_TABLES, classify_period, latest_edition,  # noqa: E402
                         latest_map, period_hashes, rows_differing)
from s1_editions_verify import (RETIRED, _defined_functions,  # noqa: E402
                                scan_retired)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = []
TABLE, LIVE, PERIOD = m.TABLE, m.LIVE, m.PERIOD
FY = "2099-00"  # a throwaway financial year, only ever inside a savepoint


def report(n, name, ok, detail=""):
    RESULTS.append(ok)
    print(f"GATE {n} {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))


def tip(cur, fy):
    return latest_edition(cur, fy, table=TABLE, period_col=PERIOD)


def table_exists(cur):
    cur.execute("SELECT to_regclass(%s)", (f"public.{TABLE}",))
    return cur.fetchone()[0] is not None


def _norm(defn):
    return " ".join(defn.split())


def gate_1_table_shape(cur):
    name = "table exists with columns, PK, FK, financial_year CHECK and three triggers"
    if not table_exists(cur):
        return report(1, name, False, f"{TABLE} absent")
    cur.execute("""SELECT column_name, data_type, character_maximum_length,
                          numeric_precision, numeric_scale
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (TABLE,))
    cols = {r[0]: r[1:] for r in cur.fetchall()}
    need = {"edition", "release_label", "published_date", "source_file",
            "source_sha256", "supersedes", "loaded_at", *m.DATA_COLS}
    typed = (all(cols.get(c) == ("numeric", None, 12, 2) for c in m.MEASURES)
             and cols.get(PERIOD, (None, None))[:2] == ("character varying", 7)
             and cols.get("data_missing", ("",))[0] == "boolean")
    cur.execute("""SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass""", (f"public.{TABLE}",))
    cons = cur.fetchall()
    pk = [d for t, d in cons if t == "p"]
    fk = [d for t, d in cons if t == "f"]
    chk = [_norm(d) for t, d in cons if t == "c"]
    fy_chk = [d for d in chk if PERIOD in d and "d{4}-" in d.replace("\\", "")
              .replace("\\\\", "")]
    cur.execute("""SELECT t.tgname, t.tgtype FROM pg_trigger t
                   WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal""",
                (f"public.{TABLE}",))
    trg = dict(cur.fetchall())
    # tgtype bits: 1 row, 2 before, 4 insert, 8 delete, 16 update, 32 truncate
    ud, tr = trg.get(m.TRIGGER, 0), trg.get(m.TRUNCATE_TRIGGER, 0)
    trig_ok = bool(ud & 1 and ud & 2 and ud & 8 and ud & 16 and not ud & 4
                   and tr & 2 and tr & 32)
    ok = (need <= set(cols) and typed and len(pk) == 1
          and f"lad24cd, {PERIOD}, edition" in pk[0]
          and len(fk) == 1 and "la_boundaries" in fk[0] and len(fy_chk) == 1
          and trig_ok)
    report(1, name, ok, f"missing_cols={sorted(need - set(cols))} typed={typed} "
           f"pk={pk} fk={len(fk)} checks={chk} triggers={sorted(trg)}")


def _first_lad(cur):
    cur.execute("SELECT lad24cd FROM la_boundaries ORDER BY lad24cd LIMIT 1")
    return cur.fetchone()[0]


def _rec(lad, v, fy=FY):
    r = {c: None for c in m.DATA_COLS}
    r.update({"lad24cd": lad, PERIOD: fy, "la_name": "t", "data_missing": False,
              "source": "t", "total_homelessness_gross_exp_000": v})
    return r


def _kw(label, d, sha):
    return dict(release_label=label, published_date=d, source_file="gate-throwaway",
                source_sha256=sha, supersedes=None)


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


def _expect_sql_error(cur, n, name, stmt, needle, fy=FY):
    if not table_exists(cur):
        return report(n, name, False, f"{TABLE} absent")

    def body(cur):
        m.insert_edition(cur, [_rec(_first_lad(cur), 1, fy)], fy,
                         **_kw("t", None, "h0"))
        cur.execute("SAVEPOINT stmt")
        try:
            cur.execute(stmt)
            return False, "no error raised"
        except psycopg2.Error as e:
            cur.execute("ROLLBACK TO SAVEPOINT stmt")
            return True, str(e).splitlines()[0]
    raised, msg = _in_savepoint(cur, body)
    report(n, name, raised and needle in msg, msg)


def gate_1b_bad_financial_year(cur):
    name = "CHECK refuses a financial_year that is not NNNN-NN"
    if not table_exists(cur):
        return report("1b", name, False, f"{TABLE} absent")
    msgs = []
    for bad in ("2024/25", "2024-2025", "24-25", "2024-5"):
        def body(cur, bad=bad):
            cur.execute("SAVEPOINT s")
            try:
                m.insert_edition(cur, [_rec(_first_lad(cur), 1, bad)], bad,
                                 **_kw("t", None, "hb"))
                return False, "inserted"
            except psycopg2.Error as e:
                cur.execute("ROLLBACK TO SAVEPOINT s")
                return True, str(e).splitlines()[0]
        msgs.append(_in_savepoint(cur, body))
    # a value longer than 7 characters is stopped by the column type, the rest
    # by the CHECK; either way nothing is stored
    report("1b", name, all(r and ("check" in t.lower() or "too long" in t.lower())
                           for r, t in msgs),
           f"{msgs[0][1][:70]} | {msgs[1][1][:50]}")


def gate_2_update(cur):
    _expect_sql_error(cur, 2, "UPDATE raises",
                      f"UPDATE public.{TABLE} SET total_homelessness_gross_exp_000"
                      f" = 2 WHERE {PERIOD} = '{FY}'", "append-only")


def gate_3_delete(cur):
    _expect_sql_error(cur, 3, "DELETE raises",
                      f"DELETE FROM public.{TABLE} WHERE {PERIOD} = '{FY}'",
                      "append-only")


def gate_3t_truncate(cur):
    _expect_sql_error(cur, "3t", "TRUNCATE raises", f"TRUNCATE public.{TABLE}",
                      "append-only")


def _chain(cur, specs):
    """Insert editions of FY; specs = [(supersedes, published, value)]."""
    lad = _first_lad(cur)
    for i, (sup, d, v) in enumerate(specs):
        m.insert_edition(cur, [_rec(lad, v)], FY,
                         **dict(_kw("t", d, f"h{i}"), supersedes=sup))


def _raw_chain(cur, rows):
    """Editions of FY inserted directly with arbitrary supersedes values;
    rows = [(edition, supersedes)]."""
    lad = _first_lad(cur)
    cols = list(m.DATA_COLS) + ["edition", "supersedes", "source_sha256"]
    for ed, sup in rows:
        r = _rec(lad, 1)
        cur.execute(f"INSERT INTO public.{TABLE} ({', '.join(cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))})",
                    [r[c] for c in m.DATA_COLS] + [ed, sup, f"r{ed}"])


def _chain_gate(cur, n, name, fn):
    if not table_exists(cur):
        report(n, name, False, f"{TABLE} absent")
        return None
    return _in_savepoint(cur, fn)


def gate_3_allow_list(cur):
    name = "shared latest_edition: RO4 table is allow-listed, an unknown table is refused"
    ok_listed = TABLE in EDITION_TABLES
    r = _raises(lambda: latest_edition(cur, FY, table="pg_class",
                                       period_col=PERIOD), ValueError)
    r2 = _raises(lambda: latest_edition(cur, FY, table=TABLE,
                                        period_col="period; drop"), ValueError)
    report("3z", name, ok_listed and r[0] and "unknown editions table" in r[1]
           and r2[0], f"listed={ok_listed}; {r[1][:60]}; bad col refused={r2[0]}")


def gate_3p_defaults_unchanged(cur):
    name = "S1/S1b behaviour unchanged: period_col defaults to 'period' in every shared helper"
    fns = (s1_editions.latest_edition, s1_editions.latest_map,
           s1_editions.rows_differing, s1_editions.classify_period,
           s1_editions.live_period_counts, s1_editions.status,
           s1_editions.period_hashes)
    got = {f.__name__: inspect.signature(f).parameters.get("period_col")
           for f in fns}
    ok = all(p is not None and p.default == "period" for p in got.values())
    report("3p", name, ok, f"{len(fns)} helpers checked")


def gate_3a_chain_tip(cur):
    name = "latest_edition: chain tip wins even with an older published_date"

    def body(cur):
        _chain(cur, [(None, date(2099, 3, 1), 1), (1, date(2099, 1, 1), 2)])
        return tip(cur, FY)
    got = _chain_gate(cur, "3a", name, body)
    if got is not None:
        report("3a", name, got == 2, f"latest = edition {got}, expected 2")


def gate_3b_fork_raises(cur):
    name = "latest_edition: fork (two editions supersede one) raises"

    def body(cur):
        _chain(cur, [(None, date(2099, 1, 1), 1), (1, date(2099, 2, 1), 2),
                     (1, date(2099, 3, 1), 3)])
        return _raises(lambda: tip(cur, FY), ValueError)
    r = _chain_gate(cur, "3b", name, body)
    if r is not None:
        report("3b", name, r[0] and "tips" in r[1], r[1][:90])


def gate_3e_superseded_never_returned(cur):
    name = "latest_edition: a superseded edition is never returned"

    def body(cur):
        _chain(cur, [(None, date(2099, 1, 1), 1), (1, date(2099, 6, 1), 2),
                     (2, date(2099, 2, 1), 3)])
        return tip(cur, FY)
    got = _chain_gate(cur, "3e", name, body)
    if got is not None:
        report("3e", name, got == 3, f"latest = edition {got}, expected 3")


def gate_3f_two_roots_raise(cur):
    name = "latest_edition: two roots with no supersedes raise"

    def body(cur):
        _chain(cur, [(None, date(2099, 1, 1), 1), (None, date(2099, 1, 1), 1)])
        return _raises(lambda: tip(cur, FY), ValueError)
    r = _chain_gate(cur, "3f", name, body)
    if r is not None:
        report("3f", name, r[0] and "no single root" in r[1], r[1][:90])


def gate_3g_single_edition(cur):
    name = "latest_edition: a single edition returns itself"

    def body(cur):
        _chain(cur, [(None, None, 1)])
        return tip(cur, FY)
    got = _chain_gate(cur, "3g", name, body)
    if got is not None:
        report("3g", name, got == 1, f"latest = edition {got}, expected 1")


def gate_3h_cycle_raises(cur):
    name = "latest_edition: a cycle (no root, no tip) raises"

    def body(cur):
        _raw_chain(cur, [(1, 2), (2, 1)])
        return _raises(lambda: tip(cur, FY), ValueError)
    r = _chain_gate(cur, "3h", name, body)
    if r is not None:
        report("3h", name, r[0], r[1][:90])


def gate_3j_mutual_supersede(cur):
    name = "latest_edition: 1 root plus two editions superseding each other raises"

    def body(cur):
        _raw_chain(cur, [(1, None), (2, 3), (3, 2)])
        return _raises(lambda: tip(cur, FY), ValueError)
    r = _chain_gate(cur, "3j", name, body)
    if r is not None:
        report("3j", name, r[0], r[1][:90])


def gate_3k_self_supersede(cur):
    name = "latest_edition: an edition superseding itself raises"

    def body(cur):
        _raw_chain(cur, [(1, None), (2, 1), (3, 3)])
        return _raises(lambda: tip(cur, FY), ValueError)
    r = _chain_gate(cur, "3k", name, body)
    if r is not None:
        report("3k", name, r[0], r[1][:90])


def gate_3c_empty_recs(cur):
    name = "insert_edition: empty rows halts and writes nothing"

    def body(cur):
        r = _raises(lambda: m.insert_edition(cur, [], FY, **_kw("t", None, "h0")),
                    SystemExit)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE {PERIOD} = %s",
                    (FY,))
        return r, cur.fetchone()[0]
    out = _chain_gate(cur, "3c", name, body)
    if out is not None:
        (ok, msg), n = out
        report("3c", name, ok and n == 0, f"{msg[:60]}; rows={n}")


def gate_3d_bad_supersedes(cur):
    name = "insert_edition: supersedes must name an existing edition"

    def body(cur):
        lad = _first_lad(cur)
        m.insert_edition(cur, [_rec(lad, 1)], FY, **_kw("t", date(2099, 1, 1), "h0"))
        bad = dict(_kw("t", date(2099, 2, 1), "h1"), supersedes=7)
        r = _raises(lambda: m.insert_edition(cur, [_rec(lad, 2)], FY, **bad),
                    SystemExit)
        good = dict(_kw("t", date(2099, 2, 1), "h1"), supersedes=1)
        ed = m.insert_edition(cur, [_rec(lad, 2)], FY, **good)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE {PERIOD} = %s",
                    (FY,))
        return r, ed, cur.fetchone()[0]
    out = _chain_gate(cur, "3d", name, body)
    if out is not None:
        (ok, msg), ed, n = out
        report("3d", name, ok and "supersedes" in msg and ed == 2 and n == 2,
               f"{msg[:60]}; valid supersedes -> edition {ed}, rows={n}")


def gate_3i_idempotent(cur):
    name = "insert_edition: same (financial_year, sha256) twice adds nothing"

    def body(cur):
        lad = _first_lad(cur)
        a = m.insert_edition(cur, [_rec(lad, 1)], FY, **_kw("t", None, "h0"))
        b = m.insert_edition(cur, [_rec(lad, 1)], FY, **_kw("t", None, "h0"))
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE {PERIOD} = %s",
                    (FY,))
        return a, b, cur.fetchone()[0]
    out = _chain_gate(cur, "3i", name, body)
    if out is not None:
        report("3i", name, out == (1, 1, 1),
               f"editions {out[0]},{out[1]}; rows={out[2]}")


def gate_4_coverage(cur):
    name = "every financial year: edition 1, the derived authority count in rows"
    if not table_exists(cur):
        return report(4, name, False, f"{TABLE} absent")
    bad, n1, total = m.check_coverage(cur)
    cur.execute(f"""SELECT {PERIOD}, COUNT(*) FROM public.{LIVE} GROUP BY 1
                    ORDER BY 1""")
    yrs = ", ".join(f"{p}={n}" for p, n in cur.fetchall())
    report(4, name, not bad, "; ".join(bad[:5]) if bad else
           f"edition-1 rows={n1}; live rows per year {yrs}")


def gate_4t_total(cur):
    name = "editions table row count equals the derived rows of every edition"
    if not table_exists(cur):
        return report("4t", name, False, f"{TABLE} absent")
    auths = m.expected_authorities(cur)
    cur.execute(f"SELECT COUNT(DISTINCT ({PERIOD}, edition)) FROM public.{TABLE}")
    eds = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
    total = cur.fetchone()[0]
    report("4t", name, auths is not None and total == auths * eds,
           f"table holds {total}, derived {auths} x {eds} editions")


def gate_4s_seeded(cur):
    name = "seeded: coverage flags a short edition and a missing edition 1"
    if not table_exists(cur):
        return report("4s", name, False, f"{TABLE} absent")
    cur.execute(f"SELECT MIN({PERIOD}) FROM public.{LIVE}")
    fy = cur.fetchone()[0]

    def short(cur):
        lad = _first_lad(cur)
        cols = ", ".join(m.DATA_COLS)
        cur.execute(f"""INSERT INTO public.{TABLE} ({cols}, edition, supersedes,
                        source_sha256) SELECT {cols}, 2, 1, 'seed-short'
                        FROM public.{TABLE} WHERE {PERIOD} = %s AND edition = 1
                        AND lad24cd <> %s""", (fy, lad))
        return m.check_coverage(cur)[0]
    sh = _in_savepoint(cur, short)

    def orphan(cur):
        m.insert_edition(cur, [_rec(_first_lad(cur), 1)], FY, **_kw("t", None, "o"))
        return m.check_coverage(cur)[0]
    orph = _in_savepoint(cur, orphan)
    ok = (any(f"{fy} ed2" in x for x in sh)
          and any(FY in x for x in orph))
    report("4s", name, ok, f"short={sh[:1]}; orphan={orph[:1]}")


def gate_5_latest_equals_live(cur):
    name = "latest edition equals the live table (every refresh column, NULL-safe)"
    if not table_exists(cur):
        return report(5, name, False, f"{TABLE} absent")
    try:
        bad = m.check_latest_equals_live(cur)
    except (ValueError, LookupError) as e:
        return report(5, name, False, str(e))
    cur.execute(f"SELECT COUNT(DISTINCT {PERIOD}) FROM public.{LIVE}")
    report(5, name, not bad, "; ".join(bad[:5]) if bad else
           f"{cur.fetchone()[0]} financial years match row-for-row")


def gate_5s_seeded(cur):
    name = ("seeded: gate 5 flags a NULL held as 0, a 0 held as NULL and a "
            "data_missing flip in the latest edition")
    if not table_exists(cur):
        return report("5s", name, False, f"{TABLE} absent")
    cur.execute(f"""SELECT {PERIOD}, lad24cd FROM public.{LIVE}
                    WHERE data_missing ORDER BY 1 DESC, 2 LIMIT 1""")
    row = cur.fetchone()
    cur.execute(f"""SELECT {PERIOD}, lad24cd FROM public.{LIVE}
                    WHERE hostels_gross_exp_000 = 0 ORDER BY 1, 2 LIMIT 1""")
    zero = cur.fetchone()
    if row is None or zero is None:
        return report("5s", name, False, f"need a data_missing row ({row}) and "
                      f"a stored zero ({zero}) to seed against")
    fy, lad = row

    def planted(cur, fy, lad, col, expr):
        cols = ", ".join(c for c in m.DATA_COLS if c != col)
        cur.execute(f"""INSERT INTO public.{TABLE} ({cols}, {col}, edition,
                        supersedes, source_sha256)
                        SELECT {cols}, CASE WHEN lad24cd = %s THEN {expr}
                                            ELSE {col} END, 2, 1, 'seed'
                        FROM public.{TABLE} WHERE {PERIOD} = %s AND edition = 1""",
                    (lad, fy))
        return m.check_latest_equals_live(cur, [fy])
    try:
        null_as_zero = _in_savepoint(cur, lambda c: planted(
            c, fy, lad, "total_homelessness_gross_exp_000", "0"))
        zero_as_null = _in_savepoint(cur, lambda c: planted(
            c, zero[0], zero[1], "hostels_gross_exp_000", "NULL"))
        flip = _in_savepoint(cur, lambda c: planted(
            c, fy, lad, "data_missing", "NOT data_missing"))
    except psycopg2.Error as e:
        return report("5s", name, False, str(e).splitlines()[0])
    ok = all(len(x) == 1 and "1 live-only" in x[0] and "1 edition-only" in x[0]
             for x in (null_as_zero, zero_as_null, flip))
    report("5s", name, ok, f"NULL->0: {null_as_zero[:1]}; 0->NULL: "
           f"{zero_as_null[:1]}; flip: {flip[:1]}")


def gate_5n_null_not_zero(cur):
    name = ("NULL not 0: data_missing is exactly 'total homelessness gross is "
            "NULL'; stored zeros are kept")
    if not table_exists(cur):
        return report("5n", name, False, f"{TABLE} absent")
    bad = m.check_null_not_zero(cur)
    cur.execute(f"""SELECT {PERIOD}, COUNT(*) FILTER (WHERE data_missing),
                           COUNT(*) FILTER (WHERE hostels_gross_exp_000 = 0)
                    FROM public.{TABLE} WHERE edition = 1 GROUP BY 1 ORDER BY 1""")
    det = "; ".join(f"{p}: {a} data_missing, {z} zero hostels cells"
                    for p, a, z in cur.fetchall())
    report("5n", name, not bad, "; ".join(bad[:4]) if bad else det)


def gate_6_provenance(cur):
    name = ("edition 1 provenance: one label/date/sha per year; a file sha only "
            "where the file's values equal the rows")
    if not table_exists(cur):
        return report(6, name, False, f"{TABLE} absent")
    bad, info = m.check_provenance(cur)
    report(6, name, not bad, "; ".join(bad[:5]) if bad else "; ".join(info))


def gate_7_reparse(cur):
    name = ("independent re-parse of each local ods (s2_ro4_load.parse) vs "
            "edition 1 is consistent with how edition 1 was recorded")
    if not table_exists(cur):
        return report(7, name, False, f"{TABLE} absent")
    res = m.reparse_report(cur)
    bad = [r["problem"] for r in res if r["problem"]]
    report(7, name, not bad, "; ".join(bad[:4]) if bad else
           "; ".join(r["summary"] for r in res))


def gate_8_status_today(cur):
    name = "today's database: status ok, chains valid for every financial year"
    if not table_exists(cur):
        return report(8, name, False, f"{TABLE} absent")
    try:
        st = m.status(cur)
    except (SystemExit, ValueError, LookupError) as e:
        return report(8, name, False, str(e))
    report(8, name, st["ok"], m.format_status(st).replace("\n", " | ")[:200])


def _fake_edition(cur, fy, mutate=None):
    """Append a fake next edition of a real year: a copy of the chain tip with
    mutate({lad: row dict}) applied (inside a savepoint the caller rolls back)."""
    t = tip(cur, fy)
    cols = ", ".join(m.DATA_COLS)
    cur.execute(f"SELECT {cols} FROM public.{TABLE} WHERE {PERIOD} = %s "
                "AND edition = %s", (fy, t))
    rows = {r[0]: dict(zip(m.DATA_COLS, r)) for r in cur.fetchall()}
    if mutate:
        mutate(rows)
    return m.insert_edition(cur, list(rows.values()), fy, release_label="gate-fake",
                            published_date=date(2099, 1, 1),
                            source_file="gate-fake.ods",
                            source_sha256=f"gate-fake-{fy}-{t}", supersedes=t)


def gate_9_seeded_newer_edition(cur):
    name = ("seeded: an edition 2 of one year -> status shows exactly that year "
            "pending, the other year untouched; a fork is a chain error")
    if not table_exists(cur):
        return report(9, name, False, f"{TABLE} absent")
    cur.execute(f"SELECT DISTINCT {PERIOD} FROM public.{LIVE} ORDER BY 1")
    years = [r[0] for r in cur.fetchall()]
    if len(years) < 2:
        return report(9, name, False, f"need two live years, have {years}")
    fy = years[-1]

    def bump(rows):
        r = next(iter(rows.values()))
        r["total_homelessness_gross_exp_000"] = (
            r["total_homelessness_gross_exp_000"] or 0) + 1
        r["data_missing"] = False

    def body(cur):
        before = period_hashes(cur, LIVE, ("lad24cd",), period_col=PERIOD)
        ed = _fake_edition(cur, fy, bump)
        st = m.status(cur)
        pending, _ = classify_period(cur, LIVE, TABLE, ("lad24cd",),
                                     m.REFRESH_COLS, fy, ed, PERIOD)
        other = [y for y in years if y != fy][0]
        other_kind = classify_period(cur, LIVE, TABLE, ("lad24cd",),
                                     m.REFRESH_COLS, other, tip(cur, other),
                                     PERIOD)[0]
        n = rows_differing(cur, LIVE, TABLE, ("lad24cd",), m.REFRESH_COLS, fy,
                           ed, PERIOD)
        cols = ", ".join(m.DATA_COLS)
        cur.execute(f"""INSERT INTO public.{TABLE} ({cols}, edition, supersedes,
                        source_sha256) SELECT {cols}, %s, 1, 'gate-fork'
                        FROM public.{TABLE} WHERE {PERIOD} = %s AND edition = 1""",
                    (ed + 1, fy))
        forked = m.status(cur)
        after = period_hashes(cur, LIVE, ("lad24cd",), period_col=PERIOD)
        return st, pending, other_kind, n, forked, before == after
    try:
        st, pend, other_kind, n, forked, live_same = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(9, name, False, str(e).splitlines()[0])
    ok = (st["pending_refresh"] == [fy] and not st["ok"] and pend == "pending"
          and other_kind == "current" and n == 2 and fy in forked["chain_errors"]
          and live_same)
    report(9, name, ok, f"pending={st['pending_refresh']} symmetric row diff "
           f"={n} other year {other_kind}; fork reported="
           f"{fy in forked['chain_errors']}; live unchanged={live_same}")


# Retired literals: a typed authority count and fixed year lists.
RETIRED_R = RETIRED + ("2023-24", "2024-25", "2025-26", "2026-27")
ALLOWED_R = {
    # this gate has to name the literals it looks for (and plants them)
    "gate_10_no_typed_counts_or_years",
}
ALLOWED_MODULE_R = {"RETIRED_R", "ALLOWED_R", "ALLOWED_MODULE_R", "FY",
                    "RETIRED"}
REFRESH_PATH_R = ("status", "sync_new", "check_coverage", "backfill",
                  "check_latest_equals_live", "expected_authorities")


def gate_10_no_typed_counts_or_years(cur):
    name = "no fixed authority count or financial-year list in the editions code"
    here = Path(__file__).resolve().parent
    mod = (here / "ro4_editions.py").read_text(encoding="utf-8")
    vf = (here / "ro4_editions_verify.py").read_text(encoding="utf-8")
    missing = [f for f in REFRESH_PATH_R if f not in _defined_functions(mod)]
    hits = [f"ro4_editions.{fn}: {w}"
            for fn, w in scan_retired(mod, set(), ALLOWED_MODULE_R, RETIRED_R)]
    hits += [f"ro4_editions_verify.{fn}: {w}"
             for fn, w in scan_retired(vf, ALLOWED_R, ALLOWED_MODULE_R,
                                       RETIRED_R)]
    planted = ("def refresh_x(cur):\n    return 296\nX = '2025-26'\n"
               "def old(cur):\n    return '2024-25'\n")
    got = scan_retired(planted, {"old"}, ALLOWED_MODULE_R, RETIRED_R)
    seeded = (("refresh_x", "296") in got and ("<module>", "2025-26") in got
              and not any(f == "old" for f, _ in got))
    report(10, name, not hits and not missing and seeded,
           f"missing={missing} hits={hits[:6]} seeded scanner ok={seeded}")


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            gate_1_table_shape(cur)
            gate_1b_bad_financial_year(cur)
            gate_2_update(cur)
            gate_3_delete(cur)
            gate_3t_truncate(cur)
            gate_3_allow_list(cur)
            gate_3p_defaults_unchanged(cur)
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
            gate_4s_seeded(cur)
            gate_5_latest_equals_live(cur)
            gate_5s_seeded(cur)
            gate_5n_null_not_zero(cur)
            gate_6_provenance(cur)
            gate_7_reparse(cur)
            gate_8_status_today(cur)
            gate_9_seeded_newer_edition(cur)
            gate_10_no_typed_counts_or_years(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
