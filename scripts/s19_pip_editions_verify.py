"""Gates for the S19 (DWP Stat-Xplore PIP claimants) edition-history table.

Mirrors scripts/s8b_hb_editions_verify.py. Prints `GATE n name: PASS|FAIL`;
exits 1 on any FAIL. Gates 2 to 7 and 17 read the real tables
(la_pip_claimants and la_pip_claimants_editions). Until the editions table
exists they print `FAIL (pending load)`, so the script exits non-zero until
then, like the S1, S1b and S8b verifiers; gate 17 is also pending while the
live table still holds month labels such as 'Apr-26'. The seeded gates never
need the real editions table: they run on throwaway tables zz_s19_live and
zz_s19_editions (a copy of the loader's SPEC named zz_s19), created inside the
transaction, and call the loader's own writers (apply_month, load_months,
migrate_months, build_records, refresh_latest) rather than a re-implementation.

Gates:
  1  table shape and immutability (UPDATE/DELETE/TRUNCATE blocked), on the
     throwaway copy; also on the real table once it exists
  2  edition 1 present for every live month                       (real)
  3  latest edition equals live for every month                   (real)
  4  every edition of every month has 296 areas                   (real)
  5  no negative claimants                                        (real)
  6  NULL versus 0 never conflated (seeded scenario + real data)  (real)
  7  zero counts listed, not failed                               (real)
  8  seeded revision: refresh_latest updates only the changed month's row
  9  seeded fork and second root refused (chain_tip, and the loader's compare)
  10 merged-area rule: a null part gives NULL (build_records)
  11 months planner never lists a month beyond the available list
  12 preview default: the load path with no flag issues no commit
  13 the API key is not in the source files or any gate output
  14 revert stored as a new edition (A, B, A -> edition 3; A again stores nothing)
  15 no network and no key in output (stubs only; a socket attempt fails the gate)
  16 new month reaches live (edition 1 and live rows in one transaction; a
     planted live-insert failure leaves neither; preview writes nothing)
  17 period keys sort chronologically: every live and edition month is yyyymm
     and MAX(month) is the chronologically latest; the relabel mapping
     round-trips for 2018-2030 (real, plus the mapping)
  18 migration preserves values (seeded, through migrate_months)
  19 discovery guard: another database or measure halts before any fetch

Why gate 17: with text labels, MAX(month) (how the live table's "latest
month" is chosen) sorts alphabetically, so 'Sep-25' beats 'Apr-26'. That
defect is documented and asserted below, and is why months are yyyymm keys.

Usage:
    python scripts/s19_pip_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. No network, no API calls, no
real-table writes, no backend is ever terminated. The Stat-Xplore key is read
from the environment only to check it is absent from the output, never printed.
"""
import contextlib
import dataclasses
import io
import json
import os
import re
import socket
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, get_conn  # noqa: E402
import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import s19_pip_editions as m  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC, LIVE, TABLE = m.SPEC, m.LIVE, m.TABLE
ZZ = dataclasses.replace(
    SPEC, name="zz_s19", live_table="zz_s19_live",
    editions_table="zz_s19_editions", fk_la_boundaries=False,
    key_types=m.key_types_for("zz_s19_editions"))
ZL, ZE = ZZ.live_table, ZZ.editions_table
FETCHED = date(2026, 10, 8)
CODES = [f"E09{n:06d}" for n in range(m.EXPECTED_AREAS)]
MEASURES = ("pip_total_claimants", "pip_enhanced_daily_living")
RESULTS = []
OUTPUT = []  # every line the gates print, for the key-leak gates (13, 15)
LATE_IMPORT_AT_START = "statxplore_client" in sys.modules


def report(n, name, ok, detail="", pending=False):
    RESULTS.append(ok)
    verdict = "PASS" if ok else ("FAIL (pending load)" if pending else "FAIL")
    line = f"GATE {n} {name}: {verdict}" + (f"  [{detail}]" if detail else "")
    OUTPUT.append(line)
    print(line)


def table_exists(cur, table=TABLE):
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def _in_savepoint(cur, fn):
    cur.execute("SAVEPOINT h")
    try:
        return fn(cur)
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT h")
        cur.execute("RELEASE SAVEPOINT h")


def _halts(fn):
    """(True, message) if fn raised SystemExit (a halt of the loader)."""
    try:
        fn()
    except SystemExit as e:
        return True, str(e)
    return False, "no halt"


GUARD_TEXT = "schema discovery chose"


def _guard_halts(fn, text=GUARD_TEXT):
    """True only if fn halted (SystemExit) with the discovery guard's own
    message. Any other outcome, including an unrelated halt or an error after
    an unguarded start, means the guard did not stop it."""
    try:
        fn()
    except SystemExit as e:
        return text in str(e.code)
    except Exception:
        return False
    return False


def _raises(fn, exc):
    try:
        fn()
    except exc as e:
        return True, str(e)
    return False, "no error raised"


@contextlib.contextmanager
def _quiet():
    """Swallow what the loader prints, but keep it for the key-leak gates."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf
    OUTPUT.extend(buf.getvalue().splitlines())


def recs(month, total=lambda i: i + 1, enhanced=lambda i: i,
         n=m.EXPECTED_AREAS):
    return [{"lad24cd": CODES[i], "month": month,
             "pip_total_claimants": total(i),
             "pip_enhanced_daily_living": enhanced(i)} for i in range(n)]


def setup_throwaway(cur):
    cur.execute("SELECT to_regclass('public.zz_s19_live'), "
                "to_regclass('public.zz_s19_editions')")
    if cur.fetchone() != (None, None):
        sys.exit("HARD STOP: zz_s19_live or zz_s19_editions already exists as "
                 "a real table; refusing to run")
    cur.execute(f"""CREATE TABLE public.{ZL} (
        lad24cd text NOT NULL, month text NOT NULL,
        pip_total_claimants integer, pip_enhanced_daily_living integer,
        loaded_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (lad24cd, month))""")
    m.create_schema(cur, ZZ)


def seed_live(cur, records):
    cur.executemany(f"INSERT INTO public.{ZL} (lad24cd, month, "
                    "pip_total_claimants, pip_enhanced_daily_living) VALUES "
                    "(%(lad24cd)s, %(month)s, %(pip_total_claimants)s, "
                    "%(pip_enhanced_daily_living)s)", records)


def editions_of(cur, month):
    """[(edition, supersedes, rows)] of one month in the throwaway table."""
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{ZE} "
                "WHERE month = %s GROUP BY 1, 2 ORDER BY 1", (month,))
    return cur.fetchall()


def raw_edition(cur, month, edition, supersedes, sha):
    """One throwaway-table row planted directly (the loader's writer refuses a
    fork or a second root, so a fault has to be planted by hand)."""
    cur.execute(f"""INSERT INTO public.{ZE} (lad24cd, month, edition,
                    pip_total_claimants, pip_enhanced_daily_living,
                    supersedes, source_sha256)
                    VALUES (%s, %s, %s, 1, 1, %s, %s)""",
                (CODES[0], month, edition, supersedes, sha))


# ---------------------------------------------------------------- gate 1

def shape_problems(cur, spec, fk):
    t = spec.editions_table
    cur.execute("""SELECT column_name, data_type, character_maximum_length
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (t,))
    cols = {r[0]: r[1:] for r in cur.fetchall()}
    need = {"lad24cd", "month", "edition", *MEASURES, "release_label",
            "published_date", "source_file", "source_sha256", "supersedes",
            "loaded_at"}
    bad = []
    if need - set(cols):
        bad.append(f"missing columns {sorted(need - set(cols))}")
    if cols.get("month") != ("character varying", 6):
        bad.append(f"month is {cols.get('month')}")
    for c in MEASURES:
        if cols.get(c, ("",))[0] != "integer":
            bad.append(f"{c} is {cols.get(c)}")
    cur.execute("""SELECT a.attname FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid
                    AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = %s::regclass AND i.indisprimary""",
                (f"public.{t}",))
    if {r[0] for r in cur.fetchall()} != {"lad24cd", "month", "edition"}:
        bad.append("primary key is not (lad24cd, month, edition)")
    cur.execute("""SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass""", (f"public.{t}",))
    cons = cur.fetchall()
    chk = " ".join(" ".join(d.split()) for ty, d in cons if ty == "c")
    if "month" not in chk:
        bad.append(f"month CHECK constraint absent ({chk[:80]})")
    nfk = sum(1 for ty, d in cons if ty == "f" and "la_boundaries" in d)
    if fk and nfk != 1:
        bad.append("lad24cd -> la_boundaries foreign key absent")
    cur.execute("""SELECT t.tgname, t.tgtype FROM pg_trigger t
                   WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal""",
                (f"public.{t}",))
    trg = dict(cur.fetchall())
    # tgtype bits: 1 row, 2 before, 4 insert, 8 delete, 16 update, 32 truncate
    ud, tr = trg.get(spec.trigger, 0), trg.get(spec.truncate_trigger, 0)
    if not (ud & 1 and ud & 2 and ud & 8 and ud & 16 and not ud & 4
            and tr & 2 and tr & 32):
        bad.append(f"append-only triggers wrong: {sorted(trg)}")
    return bad


def immutability(cur, spec, seed_row):
    """{UPDATE, DELETE, TRUNCATE: error text or None} on a table holding one
    row; all in savepoints."""
    t = spec.editions_table
    stmts = {"UPDATE": f"UPDATE public.{t} SET pip_total_claimants = 2",
             "DELETE": f"DELETE FROM public.{t}",
             "TRUNCATE": f"TRUNCATE public.{t}"}

    def body(cur):
        seed_row(cur)
        out = {}
        for k, stmt in stmts.items():
            cur.execute("SAVEPOINT stmt")
            try:
                cur.execute(stmt)
                out[k] = None
            except psycopg2.Error as e:
                cur.execute("ROLLBACK TO SAVEPOINT stmt")
                out[k] = str(e).splitlines()[0]
        return out
    return _in_savepoint(cur, body)


def gate_1_table_shape_and_immutability(cur):
    name = ("table shape and immutability (UPDATE, DELETE and TRUNCATE are "
            "blocked; bad month refused)")
    bad = shape_problems(cur, ZZ, fk=False)

    def seed(cur):
        core.insert_edition(cur, ZZ, recs("202601", n=1), "202601",
                            release_label="t", published_date=None,
                            source_file="gate", source_sha256="h0",
                            supersedes=None)
    res = immutability(cur, ZZ, seed)
    for k, msg in res.items():
        if not (msg and "append-only" in msg):
            bad.append(f"{k} not blocked ({msg})")
    for month in ("2026-4", "20260", "Apr-26"):
        def body(cur, month=month):
            cur.execute("SAVEPOINT b")
            try:
                cur.execute(f"""INSERT INTO public.{ZE} (lad24cd, month,
                    edition, pip_total_claimants) VALUES (%s, %s, 1, 1)""",
                            (CODES[0], month))
                return False
            except (psycopg2.errors.CheckViolation,
                    psycopg2.errors.StringDataRightTruncation):
                cur.execute("ROLLBACK TO SAVEPOINT b")
                return True
        if not _in_savepoint(cur, body):
            bad.append(f"CHECK accepted month={month!r}")
    scope = "throwaway copy"
    if table_exists(cur):
        scope += " and real table"
        bad += [f"real: {p}" for p in shape_problems(cur, SPEC, fk=True)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise append-only")


# ------------------------------------------------- real-table gates (2 to 7)

PENDING = f"{TABLE} does not exist yet (pending load)"


def real_gate(cur, n, name, fn):
    """Run fn(cur) -> (ok, detail) on the real tables, or report pending."""
    if not table_exists(cur):
        return report(n, name, False, PENDING, pending=True)
    try:
        ok, detail = fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(n, name, False, str(e).splitlines()[0])
    report(n, name, ok, detail)


def _live_months(cur, spec=SPEC):
    cur.execute(f"SELECT DISTINCT month FROM public.{spec.live_table} "
                "ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def real_edition1_present(cur, spec=SPEC):
    months = _live_months(cur, spec)
    cur.execute(f"SELECT DISTINCT month FROM public.{spec.editions_table} "
                "WHERE edition = 1")
    have = {r[0] for r in cur.fetchall()}
    missing = [x for x in months if x not in have]
    return (not missing and bool(months),
            ("no live months to check" if not months else
             f"{len(months)} live months" if not missing else
             f"no edition 1 for {missing[:6]} ({len(missing)} of "
             f"{len(months)})"))


def real_latest_equals_live(cur, spec=SPEC):
    months = _live_months(cur, spec)
    bad = load_checks.check_latest_equals_live(cur, spec)
    if not months:
        return False, "no live months to check (an empty state is not a pass)"
    return not bad, "; ".join(bad[:4]) if bad else (
        f"{len(months)} months match row for row")


def real_coverage(cur, spec=SPEC):
    """Every edition of every month holds one row per area, 296 in all, with
    distinct keys (load_checks.check_coverage), and the chain tip has the
    expected areas."""
    want_n = m.EXPECTED_AREAS
    cur.execute(f"SELECT DISTINCT month FROM public.{spec.live_table} UNION "
                f"SELECT DISTINCT month FROM public.{spec.editions_table} "
                "ORDER BY 1")
    months = [r[0] for r in cur.fetchall()]
    bad, n_ed = [], 0
    for month in months:
        cur.execute(f"SELECT DISTINCT edition FROM public.{spec.editions_table}"
                    " WHERE month = %s ORDER BY 1", (month,))
        eds = [r[0] for r in cur.fetchall()]
        if not eds:
            bad.append(f"{month}: no editions")
        for ed in eds:
            n_ed += 1
            bad += load_checks.check_coverage(cur, spec, month, ed, want_n)
        if not eds:
            continue
        try:
            tip = core.chain_tip(cur, spec, month)
        except (LookupError, ValueError) as e:
            bad.append(str(e))
            continue
        cur.execute(f"""SELECT COUNT(DISTINCT lad24cd)
                        FROM public.{spec.editions_table}
                        WHERE month = %s AND edition = %s""", (month, tip))
        got = cur.fetchone()[0]
        if got != m.EXPECTED_AREAS:
            bad.append(f"{month} ed{tip}: {got} areas")
    if not months or not n_ed:
        return False, "no months or editions to check (an empty state is not a pass)"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(months)} months, {n_ed} editions, each {want_n} rows = "
        f"{m.EXPECTED_AREAS} areas")


def real_no_negatives(cur, spec=SPEC):
    counts = {}
    for label, table in (("editions", spec.editions_table),
                         ("live", spec.live_table)):
        cur.execute(f"""SELECT COUNT(*) FROM public.{table}
                        WHERE pip_total_claimants < 0
                           OR pip_enhanced_daily_living < 0""")
        counts[label] = cur.fetchone()[0]
    return not any(counts.values()), (
        f"negative cells: editions {counts['editions']}, live "
        f"{counts['live']} (non-empty data is required by gate 2)")


def null_zero_counts(cur, table, where=""):
    cur.execute(f"""SELECT COUNT(*) FILTER (WHERE pip_total_claimants IS NULL),
                           COUNT(*) FILTER (WHERE pip_total_claimants = 0),
                           COUNT(*) FILTER (WHERE pip_enhanced_daily_living
                                            IS NULL),
                           COUNT(*) FILTER (WHERE pip_enhanced_daily_living
                                            = 0)
                    FROM public.{table} {where}""")
    return cur.fetchone()


def real_null_vs_zero(cur, spec=SPEC):
    months = _live_months(cur, spec)
    if not months:
        return False, "no live months to check (an empty state is not a pass)"
    bad = load_checks.check_latest_equals_live(cur, spec)
    diffs = []
    for month in months:
        try:
            ed = core.chain_tip(cur, spec, month)
        except (LookupError, ValueError) as e:
            diffs.append(str(e))
            continue
        ln = null_zero_counts(cur, spec.live_table, f"WHERE month = '{month}'")
        en = null_zero_counts(cur, spec.editions_table,
                              f"WHERE month = '{month}' AND edition = {ed}")
        if ln != en:
            diffs.append(f"{month}: live (NULL, 0) x2 {ln} vs ed{ed} {en}")
    return not bad and not diffs, "; ".join((bad + diffs)[:3]) if (
        bad or diffs) else (
        "NULL and 0 counts per month and measure identical in live and the "
        "latest edition; EXCEPT comparison clean")


def zero_report(rows):
    """{measure: zero cells} from (total, enhanced) rows. A returned 0 is a
    real value (rule 1); only NULL is missing."""
    out = {c: 0 for c in MEASURES}
    for row in rows:
        for c, v in zip(MEASURES, row):
            if v is not None and v == 0:
                out[c] += 1
    return out


def real_zero_listing(cur, spec=SPEC):
    tips, new, errors = core.latest_map(cur, spec)
    if not tips:
        return False, "no months to list zeros for (an empty state is not a pass)"
    if errors or new:
        return False, f"cannot list zeros: new={new} errors={errors}"
    tot = {c: 0 for c in MEASURES}
    months_with = {c: 0 for c in MEASURES}
    filters = ", ".join(f"COUNT(*) FILTER (WHERE {c} = 0)" for c in MEASURES)
    for month, ed in tips.items():
        cur.execute(f"""SELECT {filters}
                        FROM public.{spec.editions_table}
                        WHERE month = %s AND edition = %s""", (month, ed))
        for c, n in zip(MEASURES, cur.fetchone()):
            tot[c] += n
            months_with[c] += 1 if n else 0
    listed = ", ".join(f"{c} {tot[c]} zero cells in {months_with[c]} months"
                       for c in MEASURES)
    return True, f"zeros are values, listed not failed: {listed}"


def gate_2_edition1(cur):
    real_gate(cur, 2, "edition 1 present for every live month",
              real_edition1_present)


def gate_3_latest_equals_live(cur):
    real_gate(cur, 3, "latest edition equals live for every month",
              real_latest_equals_live)


def gate_4_coverage(cur):
    real_gate(cur, 4, "every edition of every month has 296 areas",
              real_coverage)


def gate_5_negatives(cur):
    real_gate(cur, 5, "no negative claimants", real_no_negatives)


# (label, column, area index, new value): the base month holds NULL total at
# area 0, 0 total at area 1, NULL enhanced at area 2, 0 enhanced at area 3
VARIANTS = (("NULL->0", "pip_total_claimants", 0, 0),
            ("0->NULL", "pip_total_claimants", 1, None),
            ("enhanced NULL->0", "pip_enhanced_daily_living", 2, 0),
            ("enhanced 0->NULL", "pip_enhanced_daily_living", 3, None))


def seeded_null_zero(cur):
    """The scenario of gate 6, on throwaway tables, through the loader."""
    month = "202601"
    base = recs(month, total=lambda i: None if i == 0 else (
        0 if i == 1 else i + 1),
        enhanced=lambda i: None if i == 2 else (0 if i == 3 else i))

    def body(cur):
        apply_new = m.apply_month(cur, ZZ, month, base, fetched_on=FETCHED)
        clean = load_checks.check_latest_equals_live(cur, ZZ)
        out = {"first": apply_new, "clean": clean}
        for label, col, area, new in VARIANTS:
            def variant(cur, col=col, area=area, new=new):
                changed = [dict(r, **{col: new})
                           if r["lad24cd"] == CODES[area] else r for r in base]
                kind = m.classify_month(cur, ZZ, month, changed)
                m.apply_month(cur, ZZ, month, changed, fetched_on=FETCHED)
                flagged = load_checks.check_latest_equals_live(cur, ZZ)
                stored = editions_of(cur, month)
                cur.execute(f"""SELECT {col} FROM public.{ZE}
                    WHERE month = %s AND edition = 2 AND lad24cd = %s""",
                            (month, CODES[area]))
                return kind, flagged, stored, cur.fetchone()
            out[label] = _in_savepoint(cur, variant)
        return out
    return _in_savepoint(cur, body)


def gate_6_null_vs_zero(cur):
    name = "NULL versus 0 never conflated"
    try:
        s = seeded_null_zero(cur)
    except (psycopg2.Error, SystemExit) as e:
        return report(6, name, False, f"seeded scenario: {e}")
    seeded_ok = s["first"] == "new" and s["clean"] == []
    notes = []
    for label, _col, _area, want in VARIANTS:
        kind, flagged, stored, cell = s[label]
        good = (kind == "revised" and len(stored) == 2 and cell == (want,)
                and len(flagged) == 1 and "1 edition-only" in flagged[0])
        seeded_ok = seeded_ok and good
        notes.append(f"{label} {kind}, edition 2 holds {cell}")
    if not seeded_ok:
        return report(6, name, False, "seeded scenario: " + "; ".join(notes))
    if not table_exists(cur):
        return report(6, name, False, f"{PENDING}; seeded scenario passes: "
                      + "; ".join(notes), pending=True)
    real_gate(cur, 6, name, lambda c: (lambda r: (r[0], f"{r[1]}; seeded "
              f"scenario: {'; '.join(notes)}"))(real_null_vs_zero(c)))


def gate_7_zero_counts(cur):
    name = "zero counts listed, not failed"
    sample = zero_report([(0, 0), (0, 5), (None, 0), (3, None), (None, None)])
    if sample != {"pip_total_claimants": 2, "pip_enhanced_daily_living": 2}:
        return report(7, name, False, f"zero_report wrong on a sample {sample}")
    real_gate(cur, 7, name, real_zero_listing)


# ------------------------------------------------------------ seeded gates

def seeded_enhanced_only(cur):
    """The second measure on its own, through the loader: a change in
    pip_enhanced_daily_living alone (total unchanged) is a revision stored as
    edition 2 and refreshed into live (only that month's changed row); a NULL
    in the enhanced measure alone is stored and read back as NULL."""
    a, b, c = "202601", "202602", "202603"

    def body(cur):
        ra = recs(a)
        rb = recs(b)
        for mo, rr in ((a, ra), (b, rb)):
            m.apply_month(cur, ZZ, mo, rr, fetched_on=FETCHED)
        changed = [dict(r, pip_enhanced_daily_living=r[
            "pip_enhanced_daily_living"] + 500) if r["lad24cd"] == CODES[7]
            else r for r in ra]
        kind = m.classify_month(cur, ZZ, a, changed)
        m.apply_month(cur, ZZ, a, changed, fetched_on=FETCHED)
        eds = editions_of(cur, a)
        pre = load_checks.check_latest_equals_live(cur, ZZ)
        plan = core.refresh_counts(cur, ZZ)
        full_b = core.period_hashes(cur, ZZ, "live")
        res = core.refresh_latest(cur, ZZ)
        full_a = core.period_hashes(cur, ZZ, "live")
        cur.execute(f"""SELECT pip_total_claimants, pip_enhanced_daily_living
                        FROM public.{ZL} WHERE month = %s AND lad24cd = %s""",
                    (a, CODES[7]))
        live_row = cur.fetchone()
        after = load_checks.check_latest_equals_live(cur, ZZ)
        # (c) NULL in the enhanced measure only, one area
        rc = recs(c, enhanced=lambda i: None if i == 9 else i)
        m.apply_month(cur, ZZ, c, rc, fetched_on=FETCHED)
        got = {}
        for table, extra in ((ZE, " AND edition = 1"), (ZL, "")):
            cur.execute(f"""SELECT pip_total_claimants,
                                   pip_enhanced_daily_living
                            FROM public.{table} WHERE month = %s
                            AND lad24cd = %s{extra}""", (c, CODES[9]))
            got[table] = cur.fetchone()
        return {"kind": kind, "eds": eds, "pre": pre, "plan": plan,
                "res": res, "b_same": full_b[b] == full_a[b],
                "a_changed": full_b[a] != full_a[a], "live_row": live_row,
                "want_live": (ra[7]["pip_total_claimants"],
                              ra[7]["pip_enhanced_daily_living"] + 500),
                "after": after, "null_only": got,
                "want_null": (10, None)}
    return _in_savepoint(cur, body)


def enhanced_only_ok(e):
    return (e["kind"] == "revised" and [x[0] for x in e["eds"]] == [1, 2]
            and len(e["pre"]) == 1 and e["plan"] == {"202601": 1}
            and e["res"]["updated"] == {"202601": 1} and e["res"]["rows"] == 1
            and e["b_same"] and e["a_changed"]
            and e["live_row"] == e["want_live"] and e["after"] == []
            and e["null_only"] == {ZE: e["want_null"], ZL: e["want_null"]})


def gate_8_seeded_revision(cur):
    name = ("seeded revision: edition 2 with one changed cell, refresh_latest "
            "updates only that month's changed row")
    a, b = "202601", "202602"

    def body(cur):
        ra, rb = recs(a), recs(b)
        for mo, rr in ((a, ra), (b, rb)):
            m.apply_month(cur, ZZ, mo, rr, fetched_on=FETCHED)
        before_ok = load_checks.check_latest_equals_live(cur, ZZ) == []
        changed = [dict(r, pip_total_claimants=r["pip_total_claimants"] + 1000)
                   if r["lad24cd"] == CODES[5] else r for r in ra]
        kind = m.apply_month(cur, ZZ, a, changed, fetched_on=FETCHED)
        full_b = core.period_hashes(cur, ZZ, "live")
        kept_b = core.period_hashes(cur, ZZ, "live", exclude=ZZ.refresh_cols)
        plan = core.refresh_counts(cur, ZZ)
        res = core.refresh_latest(cur, ZZ)
        full_a = core.period_hashes(cur, ZZ, "live")
        kept_a = core.period_hashes(cur, ZZ, "live", exclude=ZZ.refresh_cols)
        cur.execute(f"""SELECT lad24cd, pip_total_claimants,
                               pip_enhanced_daily_living FROM public.{ZL}
                        WHERE month = %s ORDER BY 1""", (a,))
        live_a = {x: (t, e) for x, t, e in cur.fetchall()}
        want = {r["lad24cd"]: (r["pip_total_claimants"],
                               r["pip_enhanced_daily_living"]) for r in changed}
        return (before_ok, kind, plan, res, full_b, full_a, kept_b, kept_a,
                live_a == want, editions_of(cur, b),
                load_checks.check_latest_equals_live(cur, ZZ))
    try:
        (before_ok, kind, plan, res, full_b, full_a, kept_b, kept_a, a_ok,
         b_eds, after) = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(8, name, False, str(e).splitlines()[0])
    try:
        enh = seeded_enhanced_only(cur)
    except (psycopg2.Error, SystemExit, KeyError) as e:
        return report(8, name, False, f"enhanced-only scenario: {e}")
    ok = (enhanced_only_ok(enh) and before_ok and kind == "revised"
          and plan == {a: 1}
          and res["updated"] == {a: 1} and res["rows"] == 1
          and b in full_b and a in full_b
          and full_b[b] == full_a[b] and kept_b[b] == kept_a[b]
          and kept_b[a] == kept_a[a] and full_b[a] != full_a[a]
          and a_ok and [e[0] for e in b_eds] == [1] and after == [])
    report(8, name, ok, f"edition 2 stored ({kind}); plan {plan}; updated "
           f"{res['updated']} ({res['rows']} row); {b} untouched="
           f"{full_b.get(b) == full_a.get(b)}; loaded_at and other columns "
           f"kept={kept_b.get(a) == kept_a.get(a)}; latest equals live "
           f"after={after == []}; enhanced measure alone: {enh['kind']}, editions "
           f"{[x[0] for x in enh['eds']]}, refreshed {enh['res']['updated']}, "
           f"NULL in enhanced only reads back {enh['null_only'][ZL]}")


def gate_9_fork_and_second_root(cur):
    name = "seeded fork and second root are refused (chain_tip)"
    month = "202601"

    def fork(cur):
        raw_edition(cur, month, 1, None, "r1")
        raw_edition(cur, month, 2, 1, "r2")
        raw_edition(cur, month, 3, 1, "r3")
        return (_raises(lambda: core.chain_tip(cur, ZZ, month), ValueError),
                _halts(lambda: m.compare_month(cur, ZZ, month, recs(month))))

    def roots(cur):
        raw_edition(cur, month, 1, None, "r1")
        raw_edition(cur, month, 2, None, "r2")
        return (_raises(lambda: core.chain_tip(cur, ZZ, month), ValueError),
                _halts(lambda: m.compare_month(cur, ZZ, month, recs(month))))

    def good(cur):
        raw_edition(cur, month, 1, None, "r1")
        raw_edition(cur, month, 2, 1, "r2")
        return core.chain_tip(cur, ZZ, month)
    try:
        f_tip, f_cmp = _in_savepoint(cur, fork)
        r_tip, r_cmp = _in_savepoint(cur, roots)
        tip = _in_savepoint(cur, good)
    except psycopg2.Error as e:
        return report(9, name, False, str(e).splitlines()[0])
    ok = (f_tip[0] and "fork" in f_tip[1] and f_cmp[0]
          and r_tip[0] and "no single root" in r_tip[1] and r_cmp[0] and tip == 2)
    report(9, name, ok, f"fork: {f_tip[1][:60]!r}, loader halts={f_cmp[0]}; "
           f"second root: {r_tip[1][:60]!r}, loader halts={r_cmp[0]}; "
           f"clean chain tip={tip}")


def gate_10_merged_area_rule(cur):
    name = "merged-area rule: a null part gives NULL, never a partial sum"
    uris = {"E1": ["u:E1a", "u:E1b"], "E2": ["u:E2a", "u:E2b"],
            "E3": ["u:E3a"], "E4": ["u:E4a", "u:E4b"]}
    total = {"E1a": 3, "E1b": 4, "E2a": 5, "E2b": None, "E3a": 0, "E4a": 0,
             "E4b": 0}
    enhanced = {"E1a": 1, "E1b": None, "E2a": 2, "E2b": 2, "E3a": None,
                "E4a": 0, "E4b": 0}
    got = {r["lad24cd"]: (r["pip_total_claimants"],
                          r["pip_enhanced_daily_living"])
           for r in m.build_records(total, enhanced, uris, "202601")}
    absent = {r["lad24cd"]: (r["pip_total_claimants"],
                             r["pip_enhanced_daily_living"])
              for r in m.build_records({"E1a": 1}, {"E1a": 1},
                                       {"E1": ["u:E1a", "u:E1b"]}, "202601")}
    ok = (got == {"E1": (7, None), "E2": (None, 4), "E3": (0, None),
                  "E4": (0, 0)}
          and absent == {"E1": (None, None)})
    report(10, name, ok, f"(total, enhanced): 3+4/1+null -> {got['E1']}; "
           f"5+null/2+2 -> {got['E2']}; single 0/single null -> {got['E3']}; "
           f"0+0 -> {got['E4']}; a part absent from the response -> "
           f"{absent['E1']}")


def gate_11_planner(cur):
    name = "months planner never lists a month beyond the available list"
    months = [f"{y}{mm:02d}" for y in (2025, 2026) for mm in range(1, 13)]
    bad, runs = [], 0
    for a_end in range(1, len(months) + 1):
        avail = months[:a_end]
        for h_start in range(0, len(months), 3):
            for h_end in range(h_start, len(months), 4):
                held = months[h_start:h_end + 1]
                for kw in ({}, {"recheck_n": 0}, {"recheck_n": 3},
                           {"recheck_all": True}):
                    new, rc = m.plan_months(held, avail, **kw)
                    runs += 1
                    listed = set(new) | set(rc)
                    if (not listed <= set(avail) or set(new) & set(rc)
                            or new != sorted(new) or rc != sorted(rc)
                            or any(x in held for x in new)
                            or any(x not in held for x in rc)):
                        bad.append((held[:1], avail[-1:], kw))
    nothing = m.plan_months([], months)
    beyond = m.plan_months(["202612"], ["202601", "202606"])
    ok = not bad and nothing == ([], []) and beyond == ([], [])
    report(11, name, ok, f"{runs} held/available/recheck combinations "
           f"checked; nothing held -> {nothing}; held beyond available -> "
           f"{beyond}; breaches {bad[:2]}")


def gate_12_preview_default(cur):
    name = ("preview default: the load path with no flag issues no commit and "
            "stores nothing")
    month = "202601"
    full = recs(month)
    kinds = {"kind": "new", "changed": len(full), "examples": [],
             "against": "edition"}
    discovery = {"database": {"id": m.EXPECTED_DATABASE_ID},
                 "measure": {"id": m.EXPECTED_MEASURE_ID}}

    def run(argv):
        conn = mock.MagicMock()
        mcur = conn.cursor.return_value.__enter__.return_value
        mcur.connection = conn  # load_months commits through cur.connection
        with mock.patch.object(m, "_conn", return_value=conn), \
                mock.patch.object(m, "months_migrated", return_value=True), \
                mock.patch.object(m, "table_exists", return_value=True), \
                mock.patch.object(m, "held_months", return_value=[month]), \
                mock.patch.object(m, "get_discovery", return_value=discovery), \
                mock.patch.object(m, "get_available_months",
                                  return_value=[month]), \
                mock.patch.object(m, "resolve_geography", return_value={}), \
                mock.patch.object(m, "fetch_month", return_value=full), \
                mock.patch.object(core, "latest_map",
                                  return_value=({}, [], {})), \
                mock.patch.object(m, "compare_month", return_value=kinds), \
                mock.patch.object(m, "apply_month", return_value="new") as ap, \
                _quiet():
            rc = m.main(argv)
        sql = [str(c.args[0]) for c in mcur.execute.call_args_list]
        return rc, conn.commit.call_count, ap.call_count, sql
    rc0, commits0, applied0, sql0 = run(["load", "--months", month])
    rcs, commits_s, applied_s, sql_s = run(["load", "--months", month,
                                            "--simulate"])
    rcc, commits_c, applied_c, sql_c = run(["load", "--months", month,
                                            "--commit"])
    ok = (rc0 == 0 and commits0 == 0 and applied0 == 0
          and "ROLLBACK TO SAVEPOINT s19_month" in sql0
          and commits_s == 0 and applied_s == 1
          # --commit sees two commits: the month, then the run-log row
          and commits_c == 2 and applied_c == 1
          and "RELEASE SAVEPOINT s19_month" in sql_c
          # the run log is written on --commit only
          and any("INSERT INTO pipeline_run_log" in x for x in sql_c)
          and not any("pipeline_run_log" in x for x in sql0 + sql_s))
    report(12, name, ok, f"no flag: commits {commits0}, apply_month calls "
           f"{applied0}, savepoint rolled back; --simulate: commits "
           f"{commits_s}; control --commit: commits {commits_c}")


def gate_14_revert_new_edition(cur):
    name = "revert stored as a new edition"
    month = "202603"
    a = recs(month)
    b = [dict(r, pip_total_claimants=r["pip_total_claimants"] + 7)
         if r["lad24cd"] == CODES[3] else r for r in a]

    def body(cur):
        seq = [m.apply_month(cur, ZZ, month, x, fetched_on=FETCHED)
               for x in (a, b, a)]
        after_aba = editions_of(cur, month)
        again = m.apply_month(cur, ZZ, month, a, fetched_on=FETCHED)
        after_abaa = editions_of(cur, month)
        return seq, after_aba, again, after_abaa, core.chain_tip(cur, ZZ, month)
    try:
        seq, after_aba, again, after_abaa, tip = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(14, name, False, str(e))
    n = m.EXPECTED_AREAS
    want = [(1, None, n), (2, 1, n), (3, 2, n)]
    ok = (seq == ["new", "revised", "revised"] and after_aba == want
          and again == "unchanged" and after_abaa == want and tip == 3)
    report(14, name, ok, f"A,B,A -> {seq}, editions (number, supersedes) "
           f"{[(e, s) for e, s, _ in after_aba]}; A again -> {again}, "
           f"editions still {[e for e, _, _ in after_abaa]}, tip {tip}")


def gate_16_new_month_reaches_live(cur):
    name = ("new month reaches live (edition 1 and live rows in one "
            "transaction)")
    month = "202604"
    r = recs(month, total=lambda i: None if i == 0 else (0 if i == 1 else i))
    n = m.EXPECTED_AREAS

    def stored(cur):
        kind = m.apply_month(cur, ZZ, month, r, fetched_on=FETCHED)
        cur.execute(f"SELECT COUNT(*), COUNT(loaded_at) FROM public.{ZL} "
                    "WHERE month = %s", (month,))
        live_n, stamped = cur.fetchone()
        return (kind, editions_of(cur, month), live_n, stamped,
                core.rows_differing(cur, ZZ, month, 1),
                m.check_live_equals_edition(cur, ZZ, month, 1),
                m.status(cur, ZZ))

    def failed(cur):
        """A failure while inserting live rolls the whole month back."""
        seen = {}
        real_insert = m.insert_live

        def boom(c, spec, mo, records):
            seen["editions_when_live_failed"] = len(editions_of(c, mo))
            real_insert(c, spec, mo, records[:10])
            raise RuntimeError("live insert failed (planted)")
        with mock.patch.object(m, "insert_live", side_effect=boom), _quiet():
            rc = m.load_months(cur, ZZ, [month], lambda mo: r, FETCHED, False,
                               simulate=True)
        cur.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE month = %s",
                    (month,))
        live_left = cur.fetchone()[0]
        return rc, seen, editions_of(cur, month), live_left

    def preview(cur):
        with _quiet() as buf:
            rc = m.load_months(cur, ZZ, [month], lambda mo: r, FETCHED, False)
        cur.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE month = %s",
                    (month,))
        live_left = cur.fetchone()[0]
        return rc, buf.getvalue(), editions_of(cur, month), live_left
    try:
        kind, eds, live_n, stamped, diff, chk, st = _in_savepoint(cur, stored)
        f_rc, f_seen, f_eds, f_live = _in_savepoint(cur, failed)
        p_rc, p_out, p_eds, p_live = _in_savepoint(cur, preview)
    except (psycopg2.Error, SystemExit) as e:
        return report(16, name, False, str(e).splitlines()[0])
    ok = (kind == "new" and eds == [(1, None, n)] and live_n == n
          and stamped == n and diff == 0 and chk == []
          and st["live_missing"] == []
          and f_rc == 1 and f_seen.get("editions_when_live_failed") == 1
          and f_eds == [] and f_live == 0
          and p_rc == 0 and p_eds == [] and p_live == 0
          and f"would store edition 1 and insert {n} live rows" in p_out)
    report(16, name, ok, f"{kind}: editions {[(e, s) for e, s, _ in eds]}, "
           f"live rows {live_n} (loaded_at set {stamped}), cells differing "
           f"{diff}, live_missing {st['live_missing']}; planted live failure:"
           f" rc {f_rc}, edition 1 written first="
           f"{f_seen.get('editions_when_live_failed') == 1}"
           f", afterwards editions {f_eds} live {f_live}; preview writes "
           f"editions {p_eds} live {p_live}")


# ------------------------------------------------------- key-leak gates

def api_key():
    """The Stat-Xplore key, for the leak check only. Never printed."""
    return (os.environ.get("StatXplore_API_Key", "")
            or os.environ.get("STATXPLORE_API_KEY", "")
            or ENV.get("StatXplore_API_Key", "")
            or ENV.get("STATXPLORE_API_KEY", "")).strip()


def gate_13_key_not_in_source_or_output(cur):
    name = "the API key does not appear in the source files or in any gate output"
    here = Path(__file__).resolve().parent
    files = [Path(__file__).resolve(), here / "s19_pip_editions.py",
             here / "statxplore_client.py", here / "statxplore_months.py"]
    key = api_key()
    if not key:
        # no key in this environment: the source check still has to hold, so
        # look for anything assigned to an APIKey / api_key literal instead
        pat = re.compile(r"""(?i)api[_-]?key['"]?\s*[:=]\s*['"][A-Za-z0-9]{16,}""")
        hits = [f.name for f in files if pat.search(f.read_text(
            encoding="utf-8"))]
        return report(13, name, not hits, f"no key in the environment to "
                      f"compare; key-like literal in source: {hits or 'none'}")
    in_src = [f.name for f in files if key in f.read_text(encoding="utf-8")]
    in_out = [i for i, line in enumerate(OUTPUT) if key in line]
    report(13, name, not in_src and not in_out,
           f"checked {len(files)} files and {len(OUTPUT)} output lines; "
           f"found in source: {in_src or 'no'}; in output: "
           f"{'yes' if in_out else 'no'}")


GEO_FIELD, DATE_FIELD, DL_FIELD = "str:field:G", "str:field:D", "str:field:DL"


def _discovery_for(month):
    return {"database": {"id": m.EXPECTED_DATABASE_ID, "label": "x"},
            "measure": {"id": m.EXPECTED_MEASURE_ID},
            "la_english_members": [], "geography_field_id": GEO_FIELD, "date_field_id": DATE_FIELD,
            "daily_living_field_id": DL_FIELD,
            "enhanced_member_id": "str:value:DL:enhanced",
            "date_members": [{"id": f"str:value:DATE:{month}"}]}


def gate_15_no_network_no_key(cur):
    name = "no network and no key in output"
    month = "202601"
    attempts, calls = [], []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")

    def cube(path, body):
        calls.append(path)
        geo = body["recodes"][GEO_FIELD]["map"]
        items = [{"uris": [u[0]]} for u in geo]
        return {"cubes": {"c": {"values": [[1] for _ in geo]}},
                "fields": [{"items": items}]}
    lad_to_uris = {c: [f"str:value:LA:{c}"] for c in CODES}
    geo = {"lad_to_uris": lad_to_uris,
           "all_query_uris": [u for v in lad_to_uris.values() for u in v]}
    fetched, rc = None, None
    try:
        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse), \
                mock.patch.object(socket, "getaddrinfo", refuse), \
                mock.patch("statxplore_client.api_post", side_effect=cube), \
                mock.patch.object(m.time, "sleep"), _quiet():
            fetched = m.fetch_month(month, _discovery_for(month), geo)
            rc = _in_savepoint(cur, lambda c: m.load_months(
                c, ZZ, [month], lambda mo: fetched, FETCHED, False))
    except Exception as e:  # a blocked socket or any other failure
        return report(15, name, False, f"{type(e).__name__}: {e}")
    key = api_key()
    leaked = bool(key) and any(key in line for line in OUTPUT)
    ok = (not attempts and rc == 0 and len(fetched) == m.EXPECTED_AREAS
          and len(calls) > 0 and not leaked)
    report(15, name, ok, f"stub api_post called {len(calls)} times, socket "
           f"attempts {len(attempts)}, load_months returned {rc}; "
           f"statxplore_client imported only by this gate="
           f"{not LATE_IMPORT_AT_START}; key checked against "
           f"{len(OUTPUT)} output lines: "
           f"{'LEAKED' if leaked else 'not present'}"
           + ("" if key else " (no key in this environment)"))


# ------------------------------------------- gates 17 to 19 (month keys)

KEY_RE = re.compile(r"^[0-9]{6}$")


def _chrono(key):
    return (int(key[:4]), int(key[4:]))


def real_period_keys(cur, spec=SPEC):
    """Every live and edition month is a yyyymm key and MAX(month) over each
    table is the chronologically latest. A label such as 'Apr-26' fails
    (the migration has not run)."""
    problems, seen = [], 0
    tables = [("live", spec.live_table)]
    if table_exists(cur, spec.editions_table):
        tables.append(("editions", spec.editions_table))
    for kind, table in tables:
        cur.execute(f"SELECT DISTINCT month FROM public.{table} ORDER BY 1")
        months = [r[0] for r in cur.fetchall()]
        seen += len(months)
        bad = [x for x in months if not KEY_RE.fullmatch(x)]
        if bad:
            problems.append(f"{kind}: {len(bad)} of {len(months)} months are "
                            f"not yyyymm keys, e.g. {bad[:3]}")
            try:
                latest = max((m.label_to_key(x), x) for x in bad)[1]
            except ValueError:
                latest = None
            cur.execute(f"SELECT MAX(month) FROM public.{table}")
            picked = cur.fetchone()[0]
            if latest and picked != latest:
                problems.append(f"{kind}: MAX(month) selects {picked!r}, the "
                                f"latest is {latest!r}")
            continue
        if months:
            cur.execute(f"SELECT MAX(month) FROM public.{table}")
            picked = cur.fetchone()[0]
            if picked != max(months, key=_chrono):
                problems.append(f"{kind}: MAX(month) selects {picked}, the "
                                f"chronologically latest is "
                                f"{max(months, key=_chrono)}")
    if not seen:
        return False, "no live or edition months to check (an empty state is not a pass)"
    return not problems, "; ".join(problems[:3]) if problems else (
        f"{seen} distinct months across live and editions, all yyyymm; "
        "MAX(month) is the latest in each table")


def period_key_mapping():
    """(ok, detail) with no real table: label <-> key round-trips for
    2018-2030, max over keys is chronological, max over labels is not (the
    defect that makes the keys necessary)."""
    keys = [f"{y}{mo:02d}" for y in range(2018, 2031) for mo in range(1, 13)]
    try:
        labels = [m.key_to_label(k) for k in keys]
        back = [m.label_to_key(lb) for lb in labels]
    except ValueError as e:
        return False, f"mapping failed: {e}"
    if back != keys or len(set(labels)) != len(keys):
        n = sum(1 for a, b in zip(back, keys) if a != b)
        return False, f"round trip broke for {n} of {len(keys)} months"
    if not all(KEY_RE.fullmatch(b) for b in back):
        return False, "label_to_key did not return yyyymm keys"
    latest = keys[-1]
    if max(keys) != latest or max(keys, key=_chrono) != latest:
        return False, "max over keys is not chronological"
    label_max = max(labels)
    if label_max == m.key_to_label(latest):
        return False, ("the label defect is not demonstrated: max over "
                       "labels is chronological")
    return True, (f"{len(keys)} months 2018-2030 round-trip; max over keys = "
                  f"{max(keys)} (latest); max over labels = {label_max!r}, "
                  f"not the latest {m.key_to_label(latest)!r}: the defect "
                  "the keys remove")


def _is_label(x):
    try:
        m.label_to_key(x)
        return True
    except ValueError:
        return False


def period_key_status(cur, spec=SPEC):
    """(ok, detail, pending) of the real-table half of gate 17. Pending only
    while the migration has plainly not run: every live month is a label, or
    the editions table is absent and no live month is malformed. A mixed
    table (keys and labels) or any month that is neither a key nor a label is
    a plain failure naming the offending months."""
    months = _live_months(cur, spec)
    keys = [x for x in months if KEY_RE.fullmatch(x)]
    labels = [x for x in months if _is_label(x)]
    other = [x for x in months if x not in keys and x not in labels]
    all_labels = bool(months) and len(labels) == len(months)
    if (keys and labels) or other:
        ok, detail = real_period_keys(cur, spec)
        return False, ("corrupt or half-migrated live months (keys "
                       f"{keys[:3]}, labels {labels[:3]}, other {other[:3]}): "
                       + detail), False
    if all_labels:
        return False, (f"{spec.live_table} still holds {len(labels)} month "
                       f"labels, e.g. {labels[:3]} (pending migration)"), True
    if not table_exists(cur, spec.editions_table):
        return False, (f"{spec.editions_table} does not exist yet (pending "
                       "load)"), True
    ok, detail = real_period_keys(cur, spec)
    return ok, detail, False


def gate_17_period_keys(cur):
    name = "period keys sort chronologically"
    map_ok, map_detail = period_key_mapping()
    if not map_ok:
        return report(17, name, False, map_detail)
    try:
        ok, detail, pending = period_key_status(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(17, name, False, str(e).splitlines()[0])
    report(17, name, ok, f"{detail}; mapping: {map_detail}", pending=pending)


# the independent expectation for gate 18: labels spelled out, not computed
SEED_LABELS = {"Sep-25": "202509", "Oct-25": "202510", "Dec-25": "202512",
               "Jan-26": "202601", "Apr-26": "202604"}


def _live_dump(cur):
    cur.execute(f"""SELECT month, lad24cd, pip_total_claimants,
                           pip_enhanced_daily_living, loaded_at
                    FROM public.{ZL} ORDER BY lad24cd, month""")
    return cur.fetchall()


def gate_18_migration_preserves_values(cur):
    name = "migration preserves values"
    seed = []
    for j, label in enumerate(SEED_LABELS):
        seed += recs(label, total=lambda i, j=j: (
            None if i == 0 else 0 if i == 1 else i * (j + 2)),
            enhanced=lambda i, j=j: None if i == 2 else i + j)

    def totals(cur):
        cur.execute(f"""SELECT month, SUM(pip_total_claimants),
                               SUM(pip_enhanced_daily_living), COUNT(*)
                        FROM public.{ZL} GROUP BY 1""")
        return {r[0]: r[1:] for r in cur.fetchall()}

    def body(cur):
        seed_live(cur, seed)
        before = _live_dump(cur)
        tot_b = totals(cur)
        rep = m.migrate_months(cur, ZL)
        after = _live_dump(cur)
        tot_a = totals(cur)
        cur.execute(f"SELECT MAX(month) FROM public.{ZL}")
        picked = cur.fetchone()[0]
        again = m.migrate_months(cur, ZL)
        return rep, before, after, tot_b, tot_a, picked, again

    def mixed(cur):
        seed_live(cur, recs("Apr-26") + recs("202601"))
        before = _live_dump(cur)
        halted = _halts(lambda: m.migrate_months(cur, ZL))
        return halted, _live_dump(cur) == before
    try:
        rep, before, after, tot_b, tot_a, picked, again = _in_savepoint(
            cur, body)
        (halted, msg), untouched = _in_savepoint(cur, mixed)
    except (psycopg2.Error, SystemExit, ValueError) as e:
        return report(18, name, False, str(e).splitlines()[0])
    mapped_before = sorted((SEED_LABELS.get(mo, mo), *rest)
                           for mo, *rest in before)
    mapped_after = sorted(after)
    totals_ok = ({SEED_LABELS.get(k, k): v for k, v in tot_b.items()} == tot_a)
    latest_b = sorted(r[1:] for r in before if r[0] == "Apr-26")
    latest_a = sorted(r[1:] for r in after if r[0] == "202604")
    ok = (rep["status"] == "migrated" and rep["mapping"] == SEED_LABELS
          and len(before) == len(after) == len(SEED_LABELS) * m.EXPECTED_AREAS
          and all(KEY_RE.fullmatch(r[0]) for r in after)
          and mapped_before == mapped_after   # only the month column changed
          and totals_ok and picked == "202604" and rep["latest_key"] == "202604"
          and latest_b == latest_a and len(latest_a) == m.EXPECTED_AREAS
          and again["status"] == "already migrated"
          and halted and untouched)
    report(18, name, ok, f"{len(before)} rows, {len(SEED_LABELS)} months "
           f"relabelled to {sorted(SEED_LABELS.values())}; per-month totals "
           f"and NULL/0 cells identical={totals_ok}; every other column "
           f"(incl. loaded_at) identical={mapped_before == mapped_after}; "
           f"MAX(month) -> {picked}, latest month's per-area values "
           f"identical={latest_b == latest_a}; second run "
           f"{again['status']!r}; mixed forms halt={halted}, table "
           f"untouched={untouched}")


def gate_19_discovery_guard(cur):
    name = ("discovery guard: another database or measure halts before any "
            "fetch")
    month = "202601"
    other = {"db": ("str:database:PIP_Monthly_other", m.EXPECTED_MEASURE_ID),
             "measure": (m.EXPECTED_DATABASE_ID,
                         "str:count:PIP_Monthly_new:V_OTHER"),
             "both": ("str:database:X", "str:count:X:Y"),
             "none": (None, None)}
    calls = []

    def stub(*a, **k):
        calls.append(a[:1])
        raise AssertionError("the client was called")

    def with_ids(db, measure):
        d = _discovery_for(month)
        d["database"] = {"id": db, "label": "x"}
        d["measure"] = {"id": measure}
        d["la_english_members"] = []
        return d
    halts = {}
    try:
        with mock.patch("statxplore_client.api_post", side_effect=stub), \
                mock.patch("statxplore_client.api_get", side_effect=stub), \
                mock.patch("statxplore_client.api_get_all_pages",
                           side_effect=stub), _quiet():
            for k, (db, ms) in other.items():
                d = with_ids(db, ms)
                halts[k] = (
                    _guard_halts(lambda d=d: m.check_discovery(d)),
                    _guard_halts(lambda d=d: m.fetch_month(
                        month, d, {"all_query_uris": [],
                                   "lad_to_uris": {}})))
            # the cache path: a checkpoint naming another database halts
            with tempfile.TemporaryDirectory() as tmp:
                bad_cache = Path(tmp) / "discovery.json"
                bad_cache.write_text(json.dumps(with_ids(*other["db"])),
                                     encoding="utf-8")
                with mock.patch.object(m, "DISCOVERY_CACHE", bad_cache):
                    cache_halts = _guard_halts(m.get_discovery)
                good_cache = Path(tmp) / "good.json"
                good_cache.write_text(json.dumps(
                    _discovery_for(month)),
                    encoding="utf-8")
                with mock.patch.object(m, "DISCOVERY_CACHE", good_cache):
                    got = m.get_discovery()
            expected_passes = (m.check_discovery(_discovery_for(month)) is None
                               and got["database"]["id"]
                               == m.EXPECTED_DATABASE_ID)
    except AssertionError as e:
        return report(19, name, False, str(e))
    ok = (all(a and b for a, b in halts.values()) and cache_halts
          and expected_passes and not calls
          and m.source_file_for(_discovery_for(month)) == m.SOURCE_FILE)
    report(19, name, ok, f"halts (check_discovery, fetch_month) by case: "
           f"{halts}; a checkpoint naming another database halts="
           f"{cache_halts}; the expected ids pass={expected_passes}; client "
           f"calls made: {len(calls)}")


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            setup_throwaway(cur)
            gate_1_table_shape_and_immutability(cur)
            gate_2_edition1(cur)
            gate_3_latest_equals_live(cur)
            gate_4_coverage(cur)
            gate_5_negatives(cur)
            gate_6_null_vs_zero(cur)
            gate_7_zero_counts(cur)
            gate_8_seeded_revision(cur)
            gate_9_fork_and_second_root(cur)
            gate_10_merged_area_rule(cur)
            gate_11_planner(cur)
            gate_12_preview_default(cur)
            gate_13_key_not_in_source_or_output(cur)
            gate_14_revert_new_edition(cur)
            gate_15_no_network_no_key(cur)
            gate_16_new_month_reaches_live(cur)
            gate_17_period_keys(cur)
            gate_18_migration_preserves_values(cur)
            gate_19_discovery_guard(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
