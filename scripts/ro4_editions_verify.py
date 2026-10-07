"""Gates for the RO4 housing-expenditure edition-history table.

Mirrors scripts/s1b_editions_verify.py. Prints `GATE n name: PASS/FAIL`; exits 1
on any FAIL. Gates 1-3 cover table shape, immutability and the supersedes-chain
helpers (the shared s1_editions.latest_edition, called with table= and
period_col='financial_year'); 4-5 cover the backfill (coverage, edition 1 equals
the live table row for row, NULL not 0); 6-7 cover provenance (manifest, file
hashes) and an independent re-parse of the local ods files; 8 is today's status;
9 a seeded newer edition; 10 the retired-literal scan; 11-19 the load,
refresh-latest and sync-new paths, all seeded in rolled-back savepoints (refresh
updates exactly the revised year, tamper halts, new year, drift, today, short
year, one-sided rows, fork, bootstrap count, load gates, load guards). Gates
ending in `s` or numbered 11 and above are seeded: they prove the check itself
flags a planted fault.

Usage:
    python scripts/ro4_editions_verify.py

Everything that writes runs inside a savepoint that is always rolled back and
the run ends in a rollback, so no table is left changed. Nothing is written to
the live table.
"""
import inspect
import sys
from datetime import date
from decimal import Decimal
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
             and cols.get("la_name", (None, None))[:2] == ("character varying", 100)
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
    t0 = tip(cur, fy)
    nxt = t0 + 1

    def short(cur):
        lad = _first_lad(cur)
        cols = ", ".join(m.DATA_COLS)
        cur.execute(f"""INSERT INTO public.{TABLE} ({cols}, edition, supersedes,
                        source_sha256) SELECT {cols}, %s, %s, 'seed-short'
                        FROM public.{TABLE} WHERE {PERIOD} = %s AND edition = %s
                        AND lad24cd <> %s""", (nxt, t0, fy, t0, lad))
        return m.check_coverage(cur)[0]
    sh = _in_savepoint(cur, short)

    def orphan(cur):
        m.insert_edition(cur, [_rec(_first_lad(cur), 1)], FY, **_kw("t", None, "o"))
        return m.check_coverage(cur)[0]
    orph = _in_savepoint(cur, orphan)
    ok = (any(f"{fy} ed{nxt}" in x for x in sh)
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
                                            ELSE {col} END, %s, %s, 'seed'
                        FROM public.{TABLE} WHERE {PERIOD} = %s AND edition = %s""",
                    (lad, tip(cur, fy) + 1, tip(cur, fy), fy, tip(cur, fy)))
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


# ------------------------------------------------ load, refresh, sync-new
#
# Seeded gates: each plants a fault or a newer edition inside a savepoint that
# is always rolled back, and proves the code does what it should. They revise
# the latest live financial year; the earliest is the one that must not move.
# Nothing here refreshes or loads a real second edition.

def _years(cur):
    cur.execute(f"SELECT DISTINCT {PERIOD} FROM public.{LIVE} ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _hashes(cur):
    return (period_hashes(cur, LIVE, m.HASH_KEY, period_col=PERIOD),
            period_hashes(cur, LIVE, m.HASH_KEY, exclude=m.REFRESH_COLS,
                          period_col=PERIOD))


def _halts(fn):
    """(True, message) if fn raised SystemExit (a halt)."""
    try:
        fn()
    except SystemExit as e:
        return True, str(e)
    return False, "no halt"


def _live_row(cur, fy, lad):
    cols = ", ".join(m.REFRESH_COLS)
    cur.execute(f"SELECT {cols} FROM public.{LIVE} WHERE {PERIOD} = %s "
                "AND lad24cd = %s", (fy, lad))
    return dict(zip(m.REFRESH_COLS, cur.fetchone()))


def _pick(cur, fy):
    """Authorities of the year's latest edition to plant changes on: three
    that reported a non-zero hostels net figure, one that did not report."""
    cur.execute(f"""SELECT lad24cd, data_missing, hostels_net_exp_000
                    FROM public.{TABLE} WHERE {PERIOD} = %s AND edition = %s
                    ORDER BY lad24cd""", (fy, tip(cur, fy)))
    rows = cur.fetchall()
    rep = [r[0] for r in rows if not r[1] and r[2] not in (None, 0)]
    miss = [r[0] for r in rows if r[1]]
    return rep[:3], miss[:1]


def _seed_edition_two(cur, fy):
    """Fake edition 2 of the year changing four authorities: NULL -> value with
    data_missing true -> false (and a new source text); value -> NULL with
    data_missing false -> true; value -> value with a new la_name; value -> a
    real 0. Returns (down, bump, zero, gone)."""
    rep, miss = _pick(cur, fy)
    if len(rep) < 3 or not miss:
        raise LookupError(f"{fy}: need 3 reported and 1 missing authority to "
                          f"seed against, have {len(rep)} and {len(miss)}")
    down, bump, zero = rep
    gone, = miss

    def mutate(rows):
        r = rows[gone]
        r["total_homelessness_gross_exp_000"] = 100.5
        r["hostels_gross_exp_000"] = 7
        r["data_missing"] = False
        r["source"] = "gate-fake source"
        r = rows[down]
        r["total_homelessness_gross_exp_000"] = None
        r["data_missing"] = True
        r = rows[bump]
        r["bb_gross_exp_000"] = (r["bb_gross_exp_000"] or 0) + 1
        r["la_name"] = "Gate fake name"
        rows[zero]["hostels_net_exp_000"] = 0
    _fake_edition(cur, fy, mutate)
    return down, bump, zero, gone


def gate_11_refresh_updates_only_the_revised_year(cur):
    name = ("seeded: edition 2 of one year (NULL<->value, data_missing flipped "
            "both ways, a real zero, name, source) -> refresh updates exactly "
            "that year")
    ys = _years(cur)
    if len(ys) < 2 or not table_exists(cur):
        return report(11, name, False, f"need two live years, have {ys}")
    fy, other = ys[-1], ys[0]

    def body(cur):
        down, bump, zero, gone = _seed_edition_two(cur, fy)
        before = m.status(cur)
        full_b, kept_b = _hashes(cur)
        res = m.refresh_latest(cur)
        full_a, kept_a = _hashes(cur)
        after = m.status(cur)
        g, d, b, z = (_live_row(cur, fy, x) for x in (gone, down, bump, zero))
        return (before, res, after, full_b, kept_b, full_a, kept_a, g, d, b, z,
                m.refresh_counts(cur))
    try:
        (before, res, after, full_b, kept_b, full_a, kept_a, g, d, b, z,
         plan) = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, LookupError) as e:
        return report(11, name, False, str(e).splitlines()[0])
    others = [p for p in full_b if p != fy]
    ok = (before["pending_refresh"] == [fy] and not before["ok"]
          and res["updated"] == {fy: 4} and res["rows"] == 4
          and all(full_b[p] == full_a[p] and kept_b[p] == kept_a[p]
                  for p in others) and other in others
          and kept_b[fy] == kept_a[fy] and full_b[fy] != full_a[fy]
          and g["data_missing"] is False
          and g["total_homelessness_gross_exp_000"] == 100.5
          and g["source"] == "gate-fake source"
          and d["data_missing"] is True
          and d["total_homelessness_gross_exp_000"] is None
          and b["la_name"] == "Gate fake name"
          and z["hostels_net_exp_000"] is not None
          and z["hostels_net_exp_000"] == 0
          and after["ok"] and plan == {})
    report(11, name, ok, f"pending={before['pending_refresh']} updated="
           f"{res['updated']} other year unchanged="
           f"{all(full_b[p] == full_a[p] for p in others)} loaded_at and "
           f"non-refresh columns of {fy} unchanged={kept_b[fy] == kept_a[fy]}; "
           f"NULL->value {g['total_homelessness_gross_exp_000']} flag "
           f"{g['data_missing']}; value->NULL "
           f"{d['total_homelessness_gross_exp_000']} flag {d['data_missing']}; "
           f"real zero kept {z['hostels_net_exp_000']}; status after ok="
           f"{after['ok']}")


def gate_11b_tamper_rolls_back(cur):
    name = "seeded: tampering outside the refresh inside the transaction halts"
    ys = _years(cur)
    if len(ys) < 2 or not table_exists(cur):
        return report("11b", name, False, f"need two live years, have {ys}")
    fy, other = ys[-1], ys[0]
    lad = _first_lad(cur)
    cases = {
        "another year's measure":
            f"UPDATE public.{LIVE} SET hostels_gross_exp_000 = "
            f"COALESCE(hostels_gross_exp_000, 0) + 1 WHERE {PERIOD} = '{other}' "
            f"AND lad24cd = '{lad}'",
        "another year's loaded_at":
            f"UPDATE public.{LIVE} SET loaded_at = loaded_at + interval "
            f"'1 day' WHERE {PERIOD} = '{other}' AND lad24cd = '{lad}'",
        "another year's la_name":
            f"UPDATE public.{LIVE} SET la_name = 'x' WHERE {PERIOD} = "
            f"'{other}' AND lad24cd = '{lad}'",
        "another year's row count":
            f"DELETE FROM public.{LIVE} WHERE {PERIOD} = '{other}' "
            f"AND lad24cd = '{lad}'",
        "loaded_at of the refreshed year":
            f"UPDATE public.{LIVE} SET loaded_at = loaded_at + interval "
            f"'1 day' WHERE {PERIOD} = '{fy}' AND lad24cd = '{lad}'",
        "financial_year key of the refreshed year":
            f"UPDATE public.{LIVE} SET {PERIOD} = '{FY}' WHERE "
            f"{PERIOD} = '{fy}' AND lad24cd = '{lad}'",
    }
    fails = []
    for what, sql in cases.items():
        def body(cur, sql=sql):
            _seed_edition_two(cur, fy)
            return _halts(lambda: m.refresh_latest(
                cur, _after_update_hook=lambda c: c.execute(sql)))
        try:
            halted, msg = _in_savepoint(cur, body)
        except (psycopg2.Error, LookupError) as e:
            return report("11b", name, False, str(e).splitlines()[0])
        if not (halted and "guard:" in msg):
            fails.append(f"{what}: {msg[:80]}")
    report("11b", name, not fails, "; ".join(fails) if fails else
           f"{len(cases)} tamper kinds caught by the guard")


def gate_11c_noop_changes_nothing(cur):
    name = "refresh with nothing to do writes nothing (today's database)"
    if not table_exists(cur):
        return report("11c", name, False, f"{TABLE} absent")

    def body(cur):
        full_b, kept_b = _hashes(cur)
        res = m.refresh_latest(cur)
        full_a, kept_a = _hashes(cur)
        return res, full_b == full_a and kept_b == kept_a, m.refresh_counts(cur)
    try:
        res, same, counts = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report("11c", name, False, str(e).splitlines()[0])
    report("11c", name, res["rows"] == 0 and same and counts == {},
           f"rows={res['rows']} hashes identical={same} plan={counts}")


def _fake_live_year(cur, drop=False):
    """Copy the latest live year into a brand-new financial year FY (drop: one
    authority fewer)."""
    cols = [c for c in m.DATA_COLS if c != PERIOD]
    cur.execute(f"""INSERT INTO public.{LIVE} ({', '.join(cols)}, {PERIOD},
                        loaded_at)
        SELECT {', '.join(cols)}, %s, loaded_at FROM public.{LIVE}
        WHERE {PERIOD} = (SELECT MAX({PERIOD}) FROM public.{LIVE}
                          WHERE {PERIOD} <> %s)""", (FY, FY))
    if drop:
        cur.execute(f"DELETE FROM public.{LIVE} WHERE {PERIOD} = %s "
                    "AND lad24cd = %s", (FY, _first_lad(cur)))


def gate_12_new_year(cur):
    name = ("seeded: a new live financial year -> status not ok, refresh "
            "halts; sync-new gives edition 1 'as loaded', idempotent")
    if not table_exists(cur):
        return report(12, name, False, f"{TABLE} absent")

    def body(cur):
        _fake_live_year(cur)
        s1 = m.status(cur)
        halted = _halts(lambda: m.refresh_latest(cur))
        done = m.sync_new(cur)
        cur.execute(f"""SELECT COUNT(*), MIN(release_label), MIN(source_sha256),
                               MIN(source_file), MIN(supersedes::text)
                        FROM public.{TABLE} WHERE {PERIOD} = %s AND edition = 1""",
                    (FY,))
        rows, label, sha, src, sup = cur.fetchone()
        recs, _, _ = m.live_recs(cur, FY)
        again = m.sync_new(cur)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE {PERIOD} = %s",
                    (FY,))
        return (s1, halted, done, rows, label, sha, m.rows_sha256(recs), src,
                sup, again, cur.fetchone()[0], m.status(cur),
                m.check_provenance(cur)[0])
    try:
        (s1, halted, done, rows, label, sha, want, src, sup, again, total, s2,
         prov) = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(12, name, False, str(e).splitlines()[0])
    cur.execute(f"""SELECT COUNT(*) FROM public.{LIVE} WHERE {PERIOD} =
                    (SELECT MAX({PERIOD}) FROM public.{LIVE})""")
    n_rows = cur.fetchone()[0]
    ok = (s1["new_periods"] == [FY] and not s1["ok"] and halted[0]
          and "sync-new" in halted[1] and done == [FY] and rows == n_rows
          and label.startswith("as loaded") and sha == want and src is None
          and sup is None and again == [] and total == n_rows and s2["ok"]
          and not prov)
    report(12, name, ok, f"new={s1['new_periods']} refresh halted={halted[0]} "
           f"synced={done} edition rows={rows} label={label[:30]!r} sha is the "
           f"canonical hash={sha == want} second sync={again} status after "
           f"ok={s2['ok']} provenance problems={prov}")


def gate_13_drift(cur):
    name = ("seeded: live changed directly -> status reports drift; refresh "
            "halts unless accepted")
    ys = _years(cur)
    if not ys or not table_exists(cur):
        return report(13, name, False, f"need live years, have {ys}")
    lad = _first_lad(cur)
    oks, notes = [], []
    for fy in ys:
        def body(cur, fy=fy):
            cur.execute(f"""UPDATE public.{LIVE} SET hostels_gross_exp_000 =
                            COALESCE(hostels_gross_exp_000, 0) + 1000,
                            la_name = 'drifted' WHERE {PERIOD} = %s
                            AND lad24cd = %s""", (fy, lad))
            st = m.status(cur)
            halted = _halts(lambda: m.refresh_latest(cur))
            stray = _halts(lambda: m.refresh_latest(cur, accept_drift=(FY,)))
            res = m.refresh_latest(cur, accept_drift=(fy,))
            back = _live_row(cur, fy, lad)
            cur.execute(f"""SELECT hostels_gross_exp_000, la_name FROM
                            public.{TABLE} WHERE {PERIOD} = %s AND edition = %s
                            AND lad24cd = %s""", (fy, tip(cur, fy), lad))
            want = cur.fetchone()
            return (st, halted, stray, res, m.status(cur),
                    (back["hostels_gross_exp_000"], back["la_name"]) == want)
        try:
            st, halted, stray, res, st2, restored = _in_savepoint(cur, body)
        except (psycopg2.Error, SystemExit) as e:
            return report(13, name, False, str(e).splitlines()[0])
        oks.append(st["drift_periods"] == [fy] and not st["ok"] and halted[0]
                   and "--accept-drift" in halted[1] and stray[0]
                   and fy in res["drift_accepted"]
                   and list(res["updated"]) == [fy] and st2["ok"] and restored)
        notes.append(f"{fy}: drift={st['drift_periods']} halted={halted[0]} "
                     f"accepted->{list(res['updated'])} status after "
                     f"ok={st2['ok']}")
    report(13, name, all(oks), "; ".join(notes))


def gate_14_today(cur):
    name = ("today's database: status ok, nothing to refresh, chain valid for "
            "every financial year")
    if not table_exists(cur):
        return report(14, name, False, f"{TABLE} absent")
    try:
        st = m.status(cur)
        plan = m.refresh_counts(cur)
    except (SystemExit, ValueError, LookupError) as e:
        return report(14, name, False, str(e))
    report(14, name, st["ok"] and plan == {},
           f"financial years={st['periods']} status ok={st['ok']} plan={plan}")


def gate_15_short_year(cur):
    name = ("seeded: a short financial year is refused by sync-new, not "
            "absorbed (count derived)")
    if not table_exists(cur):
        return report(15, name, False, f"{TABLE} absent")
    auths = m.expected_authorities(cur)

    def body(cur):
        _fake_live_year(cur, drop=True)
        st = m.status(cur)
        return st, _halts(lambda: m.sync_new(cur))
    try:
        st, halted = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(15, name, False, str(e).splitlines()[0])
    ok = (st["bad_counts"].get(FY) == (auths - 1, auths) and halted[0]
          and f"{auths - 1} live rows" in halted[1])
    report(15, name, ok, f"bad_counts={st['bad_counts']}, "
           f"halt={halted[1][:70]!r}")


def gate_15b_one_sided_rows(cur):
    name = ("seeded: an authority in only one of live and the latest edition "
            "halts the refresh (an UPDATE cannot repair it)")
    ys = _years(cur)
    if not ys or not table_exists(cur):
        return report("15b", name, False, f"need live years, have {ys}")
    fy = ys[-1]

    def body(cur):
        _seed_edition_two(cur, fy)
        cur.execute(f"""DELETE FROM public.{LIVE} WHERE {PERIOD} = %s AND
                        lad24cd = (SELECT MAX(lad24cd) FROM public.{LIVE}
                                   WHERE {PERIOD} = %s)""", (fy, fy))
        # a missing live row makes the year match no edition (drift); even once
        # the drift is accepted an UPDATE cannot create the row, so it halts
        return (_halts(lambda: m.refresh_latest(cur)),
                _halts(lambda: m.refresh_latest(cur, accept_drift=(fy,))))
    try:
        plain, accepted = _in_savepoint(cur, body)
    except (psycopg2.Error, LookupError) as e:
        return report("15b", name, False, str(e).splitlines()[0])
    report("15b", name, plain[0] and "--accept-drift" in plain[1]
           and accepted[0] and "only one of them" in accepted[1],
           f"halt={plain[1][:50]!r}; accepted: {accepted[1][:80]!r}")


def gate_16_fork_halts_refresh(cur):
    name = ("seeded: a forked supersedes chain shows in status as a chain "
            "error and halts the refresh")
    ys = _years(cur)
    if not ys or not table_exists(cur):
        return report(16, name, False, f"need live years, have {ys}")
    fy = ys[-1]

    def body(cur):
        t0 = tip(cur, fy)
        ed = _fake_edition(cur, fy)
        cols = ", ".join(m.DATA_COLS)
        cur.execute(f"""INSERT INTO public.{TABLE} ({cols}, edition, supersedes,
                        source_sha256) SELECT {cols}, %s, %s, 'gate-fork'
                        FROM public.{TABLE} WHERE {PERIOD} = %s
                        AND edition = %s""", (ed + 1, t0, fy, t0))
        return (m.status(cur), _halts(lambda: m.refresh_latest(cur)),
                _raises(lambda: m.latest_edition(cur, fy), ValueError))
    try:
        st, halted, tipped = _in_savepoint(cur, body)
    except psycopg2.Error as e:
        return report(16, name, False, str(e).splitlines()[0])
    report(16, name, fy in st["chain_errors"] and not st["ok"] and halted[0]
           and "invalid edition chain" in halted[1] and tipped[0],
           f"{str(st['chain_errors'])[:80]}; refresh halt={halted[1][:50]!r}")


def gate_17_bootstrap_needs_explicit_count(cur):
    name = ("seeded: --expected-authorities only when no count is derivable "
            "(or equal to it)")
    if not table_exists(cur):
        return report(17, name, False, f"{TABLE} absent")
    n = m.expected_authorities(cur)

    def body(cur):
        _fake_live_year(cur, drop=True)
        # a count can be derived (the real years have editions): a different N
        # halts, an equal one is accepted and the short year still halts
        differs = _halts(lambda: m.sync_new(cur, n - 1))
        equal = _halts(lambda: m.sync_new(cur, n))
        # no year has editions: simulate the bootstrap
        saved = m.latest_map, m.live_period_counts
        m.latest_map = lambda cur, *a, **k: ({}, [FY], {})
        m.live_period_counts = lambda cur, *a, **k: {FY: n - 1}
        try:
            no_arg = _halts(lambda: m.sync_new(cur))
            short = _halts(lambda: m.sync_new(cur, n))
            explicit = m.sync_new(cur, n - 1)
        finally:
            m.latest_map, m.live_period_counts = saved
        return differs, no_arg, short, explicit, equal
    try:
        differs, no_arg, short, explicit, equal = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(17, name, False, str(e).splitlines()[0])
    ok = (differs[0] and "differs from the count" in differs[1]
          and no_arg[0] and "--expected-authorities" in no_arg[1]
          and short[0] and f"{n - 1} live rows" in short[1]
          and explicit == [FY]
          and equal[0] and f"{n - 1} live rows" in equal[1])
    report(17, name, ok, f"derivable, N differs: {differs[1][:50]}; bootstrap "
           f"no N: {no_arg[1][:40]}; explicit {n - 1}: {explicit}")


def _recorded_file(cur, fy):
    """(entry, records, raw cells) of a manifest file of the year that is held
    locally and already recorded as one of its editions, so loading its
    records again as a new edition (inside a savepoint) is a faithful copy."""
    for e in m.manifest_entries(fy):
        cur.execute(f"""SELECT 1 FROM public.{TABLE} WHERE {PERIOD} = %s
                        AND source_sha256 = %s LIMIT 1""", (fy, e["sha256"]))
        if cur.fetchone() and (m.REF_DIR / e["file"]).exists():
            return (e, m.recs_from_df(m.parse_file(cur, fy, e), fy),
                    m.raw_cells(m.REF_DIR / e["file"], e["sheet"]))
    return None


def _load_variant(cur, fy, recs, raw, mutate=None, drop=None):
    """Insert the (mutated) records as the year's next edition and run both
    load gates on it; return (structural problems, raw re-read problems)."""
    recs = [dict(r) for r in recs if r["lad24cd"] != drop]
    if mutate:
        mutate({r["lad24cd"]: r for r in recs})
    t = tip(cur, fy)
    ed = m.insert_edition(cur, recs, fy, release_label="gate-load",
                          published_date=date(2099, 1, 1),
                          source_file="gate-load.ods",
                          source_sha256=f"gate-load-{fy}-{t}", supersedes=t)
    return (m.check_loaded(cur, fy, ed),
            m.check_raw_integrity(cur, fy, ed, raw)[0])


def gate_18_load_gates_seeded(cur):
    name = ("seeded: the load gates pass a faithful edition and flag a NULL "
            "stored as 0, a real 0 stored as NULL, a flipped data_missing, a "
            "short edition, an empty measure column and a changed name")
    ys = _years(cur)
    if not ys or not table_exists(cur):
        return report(18, name, False, f"need live years, have {ys}")
    got = next((g for g in (_recorded_file(cur, fy) for fy in reversed(ys))
                if g), None)
    if got is None:
        return report(18, name, False, "no recorded local manifest file to "
                      "re-load as a seeded edition")
    entry, recs, raw = got
    fy = entry[PERIOD]
    by = {r["lad24cd"]: r for r in recs}
    nulls = [(lad, c) for lad, r in by.items() for c in m.MEASURES
             if r[c] is None]
    zeros = [(lad, c) for lad, r in by.items() for c in m.MEASURES
             if r[c] == 0]
    miss = [lad for lad, r in by.items() if r["data_missing"]]
    if not nulls or not zeros or not miss:
        return report(18, name, False, f"file has {len(nulls)} NULL cells, "
                      f"{len(zeros)} zeros, {len(miss)} missing authorities")
    (nl, nc), (zl, zc) = nulls[0], zeros[0]
    col = m.MEASURES[0]

    def run(**kw):
        return _in_savepoint(cur, lambda c: _load_variant(c, fy, recs, raw,
                                                          **kw))
    try:
        clean = run()
        null0 = run(mutate=lambda rows: rows[nl].__setitem__(nc, Decimal(0)))
        zero_null = run(mutate=lambda rows: rows[zl].__setitem__(zc, None))
        flip = run(mutate=lambda rows: rows[miss[0]].__setitem__(
            "data_missing", False))
        short = run(drop=_first_lad(cur))
        empty = run(mutate=lambda rows: [r.__setitem__(col, None)
                                         for r in rows.values()])
        renamed = run(mutate=lambda rows: rows[zl].__setitem__("la_name", "x"))
    except (psycopg2.Error, SystemExit) as e:
        return report(18, name, False, str(e).splitlines()[0])
    ok = (clean == ([], [])
          and any("stored 0" in x for x in null0[1])
          and any("stored None" in x for x in zero_null[1])
          and any("data_missing" in x for x in flip[0] + flip[1])
          and any("authorities/" in x for x in short[0])
          and any("no value for any" in x for x in empty[0])
          and any("la_name" in x for x in renamed[1]))
    report(18, name, ok, f"faithful={clean == ([], [])}; NULL as 0: "
           f"{null0[1][:1]}; 0 as NULL: {zero_null[1][:1]}; flip: "
           f"{(flip[0] + flip[1])[:1]}; short: {short[0][:1]}; empty column: "
           f"{empty[0][:1]}; name: {renamed[1][:1]}")


def gate_19_load_cli_guards(cur):
    name = ("load: a recorded file is 'already loaded'; --supersedes is checked "
            "against the chain tip; --commit with --simulate is refused; a "
            "label shared by two entries is refused as ambiguous; the table is "
            "untouched")
    ys = _years(cur)
    if not ys or not table_exists(cur):
        return report(19, name, False, f"need live years, have {ys}")
    import contextlib
    import io
    got = next((g for g in (_recorded_file(cur, fy) for fy in reversed(ys))
                if g), None)
    if got is None:
        return report(19, name, False, "no recorded local manifest file")
    entry = got[0]
    fy = entry[PERIOD]
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
    n0 = cur.fetchone()[0]
    args = ["load", "--financial-year", fy, "--manifest-file", entry["file"],
            "--simulate"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.main(args)
    both = _halts(lambda: m.main(args + ["--commit"]))
    t = tip(cur, fy)
    wrong = _halts(lambda: m.check_supersedes(t + 1, t, fy))
    right = _halts(lambda: m.check_supersedes(t, t, fy))
    default = _halts(lambda: m.check_supersedes(None, t, fy))
    twin = [dict(entry), dict(entry, file="gate-twin.ods")]
    amb = _halts(lambda: m.select_entry(twin, fy, label=entry[
        "release_label_stored"]))
    byfile = m.select_entry(twin, fy, file="gate-twin.ods")["file"]
    none_or_two = _halts(lambda: m.select_entry(twin, fy))
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
    n1 = cur.fetchone()[0]
    ok = ("already loaded" in buf.getvalue() and both[0]
          and wrong[0] and "chain tip" in wrong[1] and not right[0]
          and not default[0] and amb[0] and "ambiguous" in amb[1]
          and byfile == "gate-twin.ods" and none_or_two[0] and n0 == n1)
    report(19, name, ok, f"said={buf.getvalue().strip()[:50]!r}; --commit with "
           f"--simulate refused={both[0]}; supersedes tip+1: {wrong[1][:50]!r}; "
           f"ambiguous label: {amb[1][:40]!r}; table rows {n0}->{n1}")


# ---- cover sheet and stored-label provenance (the error that was missed)

def gate_20_front_pages(cur):
    name = ("every manifest file's own Front_Page says what the manifest's "
            "actual label and date say")
    bad = m.check_manifest_front_pages()
    held = [e["file"] for e in m.load_manifest() if (m.REF_DIR / e["file"]).exists()]
    report(20, name, not bad and bool(held), "; ".join(bad[:4]) if bad else
           f"{len(held)} files checked: " + ", ".join(
               f"{e['file']} = {e['release_label_actual']}"
               for e in m.load_manifest() if e["file"] in held))


def gate_20s_front_page_seeded(cur):
    name = ("seeded: a file filed under the wrong release (wrong ordinal, wrong "
            "date, inconsistent label, unreadable file) is flagged, and load "
            "refuses it")
    held = [e for e in m.load_manifest() if (m.REF_DIR / e["file"]).exists()]
    if not held:
        return report("20s", name, False, "no manifest file held locally")
    e = held[0]
    path = m.REF_DIR / e["file"]
    word = e["release_label_actual"].split()[0]
    wrong_word = "third" if word != "third" else "second"
    cases = {
        "wrong release ordinal": dict(e, release_label_actual=e[
            "release_label_actual"].replace(word, wrong_word, 1)),
        "wrong published date": dict(e, published_date_actual="2000-01-01"),
        "label and date disagree": dict(e, release_label_actual=e[
            "release_label_actual"].replace("20", "19", 1)),
        "label without a date": dict(e, release_label_actual=f"{word} release"),
    }
    flagged = {k: bool(m.check_front_page(path, v)) for k, v in cases.items()}
    unreadable = bool(m.check_front_page(Path(__file__), e))
    clean = m.check_front_page(path, e) == []
    saved = m.load_manifest
    m.load_manifest = lambda: [cases["wrong release ordinal"]]
    try:
        refused = _halts(lambda: m.main(
            ["load", "--financial-year", e[PERIOD], "--manifest-file",
             e["file"]]))
    finally:
        m.load_manifest = saved
    ok = (clean and all(flagged.values()) and unreadable and refused[0]
          and "cover sheet" in refused[1])
    report("20s", name, ok, f"genuine entry clean={clean}; flagged={flagged}; "
           f"unreadable flagged={unreadable}; load refused: {refused[1][:60]!r}")


def gate_21_stored_label_provenance(cur):
    name = ("seeded: provenance compares each edition with the STORED label and "
            "date, and flags a stored label that differs")
    if not table_exists(cur):
        return report(21, name, False, f"{TABLE} absent")
    clean = m.check_provenance(cur)[0]
    saved = m.load_manifest

    def doctored(field, value):
        def f():
            out = []
            for i, x in enumerate(saved()):
                out.append(dict(x, **{field: value}) if i == 0 else x)
            return out
        return f
    flagged = {}
    for field, value in (("release_label_stored", "second release, published "
                          "4 Dec 2025"), ("published_date_stored", "1999-01-01")):
        m.load_manifest = doctored(field, value)
        try:
            flagged[field] = bool(m.check_provenance(cur)[0])
        finally:
            m.load_manifest = saved
    report(21, name, not clean and all(flagged.values()),
           f"genuine manifest problems={clean[:2]}; doctored flagged={flagged}")


# Retired literals: a typed authority count and fixed year lists.
RETIRED_R = RETIRED + ("2023-24", "2024-25", "2025-26", "2026-27")
ALLOWED_R = {
    # this gate has to name the literals it looks for (and plants them)
    "gate_10_no_typed_counts_or_years",
}
ALLOWED_MODULE_R = {"RETIRED_R", "ALLOWED_R", "ALLOWED_MODULE_R", "FY",
                    "RETIRED"}
REFRESH_PATH_R = ("status", "sync_new", "check_coverage", "backfill",
                  "check_latest_equals_live", "expected_authorities",
                  "refresh_latest", "refresh_counts", "_plan", "_unrepairable",
                  "select_entry", "check_supersedes", "check_front_page",
                  "check_manifest_front_pages",
                  "_one_sided", "record_edition1", "check_loaded",
                  "check_raw_integrity", "run_load_gates", "diff_recs",
                  "raw_cells", "cmd_load", "cmd_sync_new",
                  "cmd_refresh_latest")


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
            gate_11_refresh_updates_only_the_revised_year(cur)
            gate_11b_tamper_rolls_back(cur)
            gate_11c_noop_changes_nothing(cur)
            gate_12_new_year(cur)
            gate_13_drift(cur)
            gate_14_today(cur)
            gate_15_short_year(cur)
            gate_15b_one_sided_rows(cur)
            gate_16_fork_halts_refresh(cur)
            gate_17_bootstrap_needs_explicit_count(cur)
            gate_18_load_gates_seeded(cur)
            gate_19_load_cli_guards(cur)
            gate_20_front_pages(cur)
            gate_20s_front_page_seeded(cur)
            gate_21_stored_label_provenance(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
