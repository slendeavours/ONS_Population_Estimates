"""Gates for the S8b (DWP Stat-Xplore Housing Benefit caseload by accommodation
type) edition-history table.

Mirrors scripts/ro4_editions_verify.py. Prints `GATE n name: PASS|FAIL`; exits 1
on any FAIL. Gates 2 to 7 read the real tables
(la_hb_accom_type_caseload and la_hb_accom_type_caseload_editions). Until the
editions table exists (Task 4 loads it) they print `FAIL (pending load)`, so the
script exits non-zero until then, like the S1 and S1b verifiers. The seeded
gates (1 and 6's scenario, 8 to 16) never need the real editions table: they run
on throwaway tables zz_s8b_live and zz_s8b_editions (a copy of the loader's SPEC
named zz_s8b), created inside the transaction, and call the loader's own
writers (apply_month, load_months, refresh_latest, chain_tip) rather than a
re-implementation.

Gates:
  1  table shape and immutability (UPDATE/DELETE/TRUNCATE blocked), on the
     throwaway copy; also on the real table once it exists
  2  edition 1 present for every live month                       (real)
  3  latest edition equals live for every month                   (real)
  4  every month has 296 areas x 4 types                          (real)
  5  no negative claimants                                        (real)
  6  NULL versus 0 never conflated (seeded scenario + real data)  (real)
  7  UNKNOWN zeros accepted; zeros in SA/TA/OTHER listed, not failed (real)
  8  seeded revision: refresh_latest updates only the changed row
  9  seeded fork and second root refused (chain_tip, and the loader's compare)
  10 merged-area rule: a null part gives NULL (build_records)
  11 months planner never lists a month beyond the available list
  12 preview default: the load path with no flag issues no commit
  13 the API key is not in the source files or any gate output
  14 revert stored as a new edition (A, B, A -> edition 3; A again stores nothing)
  15 no network and no key in output (stubs only; a socket attempt fails the gate)
  16 new month reaches live (edition 1 and live rows in one transaction; a
     planted live-insert failure leaves neither; preview writes nothing)

Usage:
    python scripts/s8b_hb_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. No network, no API calls, no
real-table writes, no backend is ever terminated. The Stat-Xplore key is read
from the environment only to check it is absent from the output, never printed.
"""
import contextlib
import dataclasses
import io
import os
import socket
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, get_conn  # noqa: E402
import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import s8b_hb_editions as m  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC, LIVE, TABLE = m.SPEC, m.LIVE, m.TABLE
ZZ = dataclasses.replace(
    SPEC, name="zz_s8b", live_table="zz_s8b_live",
    editions_table="zz_s8b_editions", fk_la_boundaries=False,
    key_types=m.key_types_for("zz_s8b_editions"))
ZL, ZE = ZZ.live_table, ZZ.editions_table
FETCHED = date(2026, 10, 8)
CODES = [f"E09{n:06d}" for n in range(m.EXPECTED_AREAS)]
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


def recs(month, value=lambda i, t: i, n=m.EXPECTED_AREAS, types=m.ACCOM_TYPES):
    return [{"lad24cd": CODES[i], "month": month, "accom_type": t,
             "claimants": value(i, t)} for t in types for i in range(n)]


def setup_throwaway(cur):
    cur.execute("SELECT to_regclass('public.zz_s8b_live'), "
                "to_regclass('public.zz_s8b_editions')")
    if cur.fetchone() != (None, None):
        sys.exit("HARD STOP: zz_s8b_live or zz_s8b_editions already exists as "
                 "a real table; refusing to run")
    cur.execute(f"""CREATE TABLE public.{ZL} (
        lad24cd text NOT NULL, month text NOT NULL, accom_type text NOT NULL,
        claimants integer, loaded_at timestamptz DEFAULT now(),
        PRIMARY KEY (lad24cd, month, accom_type))""")
    m.create_schema(cur, ZZ)


def seed_live(cur, records):
    cur.executemany(f"INSERT INTO public.{ZL} (lad24cd, month, accom_type, "
                    "claimants) VALUES (%(lad24cd)s, %(month)s, %(accom_type)s,"
                    " %(claimants)s)", records)


def editions_of(cur, month):
    """[(edition, supersedes, rows)] of one month in the throwaway table."""
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{ZE} "
                "WHERE month = %s GROUP BY 1, 2 ORDER BY 1", (month,))
    return cur.fetchall()


def raw_edition(cur, month, edition, supersedes, sha):
    """One throwaway-table row planted directly (the loader's writer refuses a
    fork or a second root, so a fault has to be planted by hand)."""
    cur.execute(f"""INSERT INTO public.{ZE} (lad24cd, accom_type, month,
                    edition, claimants, supersedes, source_sha256)
                    VALUES (%s, 'SA', %s, %s, 1, %s, %s)""",
                (CODES[0], month, edition, supersedes, sha))


# ---------------------------------------------------------------- gate 1

def shape_problems(cur, spec, fk):
    t = spec.editions_table
    cur.execute("""SELECT column_name, data_type, character_maximum_length
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (t,))
    cols = {r[0]: r[1:] for r in cur.fetchall()}
    need = {"lad24cd", "accom_type", "month", "edition", "claimants",
            "release_label", "published_date", "source_file", "source_sha256",
            "supersedes", "loaded_at"}
    bad = []
    if need - set(cols):
        bad.append(f"missing columns {sorted(need - set(cols))}")
    if cols.get("month") != ("character varying", 6):
        bad.append(f"month is {cols.get('month')}")
    if cols.get("accom_type") != ("character varying", 10):
        bad.append(f"accom_type is {cols.get('accom_type')}")
    if cols.get("claimants", ("",))[0] != "integer":
        bad.append(f"claimants is {cols.get('claimants')}")
    cur.execute("""SELECT a.attname FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid
                    AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = %s::regclass AND i.indisprimary""",
                (f"public.{t}",))
    if {r[0] for r in cur.fetchall()} != {"lad24cd", "accom_type", "month",
                                          "edition"}:
        bad.append("primary key is not (lad24cd, accom_type, month, edition)")
    cur.execute("""SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass""", (f"public.{t}",))
    cons = cur.fetchall()
    chk = " ".join(" ".join(d.split()) for ty, d in cons if ty == "c")
    if "month" not in chk or "accom_type" not in chk:
        bad.append(f"month / accom_type CHECK constraints absent ({chk[:80]})")
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
    """{UPDATE, DELETE, TRUNCATE, bad month, bad accom_type: error text or
    None} on a table holding one row; all in savepoints."""
    t = spec.editions_table
    stmts = {"UPDATE": f"UPDATE public.{t} SET claimants = 2",
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
            "blocked; bad month and accom_type refused)")
    bad = shape_problems(cur, ZZ, fk=False)

    def seed(cur):
        core.insert_edition(cur, ZZ, recs("202601", n=1, types=("SA",)),
                            "202601", release_label="t", published_date=None,
                            source_file="gate", source_sha256="h0",
                            supersedes=None)
    res = immutability(cur, ZZ, seed)
    for k, msg in res.items():
        if not (msg and "append-only" in msg):
            bad.append(f"{k} not blocked ({msg})")
    for month, accom in (("2026-4", "SA"), ("20260", "SA"), ("202604", "XX")):
        def body(cur, month=month, accom=accom):
            cur.execute("SAVEPOINT b")
            try:
                cur.execute(f"""INSERT INTO public.{ZE} (lad24cd, accom_type,
                    month, edition, claimants) VALUES (%s, %s, %s, 1, 1)""",
                            (CODES[0], accom, month))
                return False
            except psycopg2.errors.CheckViolation:
                cur.execute("ROLLBACK TO SAVEPOINT b")
                return True
        if not _in_savepoint(cur, body):
            bad.append(f"CHECK accepted month={month!r} accom_type={accom!r}")
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
    """Every edition of every month holds areas x types rows with distinct
    keys (load_checks.check_coverage), and the chain tip has the expected
    areas in each type."""
    want_n = m.EXPECTED_AREAS * len(m.ACCOM_TYPES)
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
        cur.execute(f"""SELECT accom_type, COUNT(DISTINCT lad24cd)
                        FROM public.{spec.editions_table}
                        WHERE month = %s AND edition = %s GROUP BY 1""",
                    (month, tip))
        got = dict(cur.fetchall())
        if got != {t: m.EXPECTED_AREAS for t in m.ACCOM_TYPES}:
            bad.append(f"{month} ed{tip}: areas per type {got}")
    if not months or not n_ed:
        return False, "no months or editions to check (an empty state is not a pass)"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(months)} months, {n_ed} editions, each {want_n} rows = "
        f"{m.EXPECTED_AREAS} areas x {len(m.ACCOM_TYPES)} types")


def real_no_negatives(cur, spec=SPEC):
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.editions_table} "
                "WHERE claimants < 0")
    ed = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} "
                "WHERE claimants < 0")
    live = cur.fetchone()[0]
    return ed == 0 and live == 0, (f"negative cells: editions {ed}, live "
                                   f"{live} (non-empty data is required by "
                                   "gate 2)")


def null_zero_counts(cur, table, where=""):
    cur.execute(f"""SELECT COUNT(*) FILTER (WHERE claimants IS NULL),
                           COUNT(*) FILTER (WHERE claimants = 0)
                    FROM public.{table} {where}""")
    return cur.fetchone()


def real_null_vs_zero(cur, spec=SPEC):
    months = _live_months(cur, spec)
    if not months:
        return False, "no live months to check (an empty state is not a pass)"
    bad = load_checks.check_latest_equals_live(cur, spec)
    diffs = []
    for month in months:
        ed = core.chain_tip(cur, spec, month)
        ln = null_zero_counts(cur, spec.live_table, f"WHERE month = '{month}'")
        en = null_zero_counts(cur, spec.editions_table,
                              f"WHERE month = '{month}' AND edition = {ed}")
        if ln != en:
            diffs.append(f"{month}: live (NULL, 0) {ln} vs ed{ed} {en}")
    return not bad and not diffs, "; ".join((bad + diffs)[:3]) if (
        bad or diffs) else (
        "NULL and 0 counts per month identical in live and the latest "
        "edition; EXCEPT comparison clean")


def zero_report(rows):
    """{accom_type: zero cells} from (accom_type, claimants) rows. A returned
    0 is a real value (rule 1); only NULL is missing."""
    out = {t: 0 for t in m.ACCOM_TYPES}
    for t, c in rows:
        if c is not None and c == 0:
            out[t] = out.get(t, 0) + 1
    return out


def real_zero_listing(cur, spec=SPEC):
    tips, new, errors = core.latest_map(cur, spec)
    if not tips:
        return False, "no months to list zeros for (an empty state is not a pass)"
    if errors or new:
        return False, f"cannot list zeros: new={new} errors={errors}"
    tot = {t: 0 for t in m.ACCOM_TYPES}
    months_with = {t: 0 for t in m.ACCOM_TYPES}
    for month, ed in tips.items():
        cur.execute(f"""SELECT accom_type, COUNT(*) FROM public.{spec.editions_table}
                        WHERE month = %s AND edition = %s AND claimants = 0
                        GROUP BY 1""", (month, ed))
        for t, n in cur.fetchall():
            tot[t] = tot.get(t, 0) + n
            months_with[t] = months_with.get(t, 0) + 1
    listed = ", ".join(f"{t} {tot[t]} zero cells in {months_with[t]} months"
                       for t in ("SA", "TA", "OTHER"))
    return True, (f"UNKNOWN zeros accepted ({tot['UNKNOWN']} cells in "
                  f"{months_with['UNKNOWN']} months); listed, not failed: "
                  f"{listed}")


def gate_2_edition1(cur):
    real_gate(cur, 2, "edition 1 present for every live month",
              real_edition1_present)


def gate_3_latest_equals_live(cur):
    real_gate(cur, 3, "latest edition equals live for every month",
              real_latest_equals_live)


def gate_4_coverage(cur):
    real_gate(cur, 4, "every month has 296 areas x 4 accommodation types",
              real_coverage)


def gate_5_negatives(cur):
    real_gate(cur, 5, "no negative claimants", real_no_negatives)


def seeded_null_zero(cur):
    """The scenario of gate 6, on throwaway tables, through the loader."""
    month = "202601"
    base = recs(month, value=lambda i, t: None if i == 0 else (
        0 if i == 1 else i + 1))

    def body(cur):
        seed_live(cur, base)
        apply_new = m.apply_month(cur, ZZ, month, base, fetched_on=FETCHED)
        clean = load_checks.check_latest_equals_live(cur, ZZ)
        out = {"first": apply_new, "clean": clean}
        for label, area, new in (("NULL->0", 0, 0), ("0->NULL", 1, None)):
            def variant(cur, area=area, new=new):
                changed = [dict(r, claimants=new) if (
                    r["lad24cd"] == CODES[area] and r["accom_type"] == "SA")
                    else r for r in base]
                kind = m.classify_month(cur, ZZ, month, changed)
                m.apply_month(cur, ZZ, month, changed, fetched_on=FETCHED)
                flagged = load_checks.check_latest_equals_live(cur, ZZ)
                stored = editions_of(cur, month)
                cur.execute(f"""SELECT claimants FROM public.{ZE}
                    WHERE month = %s AND edition = 2 AND lad24cd = %s
                    AND accom_type = 'SA'""", (month, CODES[area]))
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
    for label, want in (("NULL->0", 0), ("0->NULL", None)):
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


def gate_7_unknown_zeros(cur):
    name = ("UNKNOWN zeros accepted; zeros in SA/TA/OTHER listed with counts, "
            "not failed")
    sample = zero_report([("UNKNOWN", 0), ("UNKNOWN", 0), ("SA", 0), ("TA", 0),
                          ("TA", None), ("OTHER", 3)])
    if sample != {"SA": 1, "TA": 1, "OTHER": 0, "UNKNOWN": 2}:
        return report(7, name, False, f"zero_report wrong on a sample {sample}")
    real_gate(cur, 7, name, real_zero_listing)


# ------------------------------------------------------------ seeded gates

def gate_8_seeded_revision(cur):
    name = ("seeded revision: edition 2 with one changed cell, refresh_latest "
            "updates only that month's changed row")
    a, b = "202601", "202602"

    def body(cur):
        ra, rb = recs(a), recs(b)
        seed_live(cur, ra + rb)
        for mo, rr in ((a, ra), (b, rb)):
            m.apply_month(cur, ZZ, mo, rr, fetched_on=FETCHED)
        before_ok = load_checks.check_latest_equals_live(cur, ZZ) == []
        changed = [dict(r, claimants=r["claimants"] + 1000) if (
            r["lad24cd"] == CODES[5] and r["accom_type"] == "TA") else r
            for r in ra]
        kind = m.apply_month(cur, ZZ, a, changed, fetched_on=FETCHED)
        full_b = core.period_hashes(cur, ZZ, "live")
        kept_b = core.period_hashes(cur, ZZ, "live", exclude=ZZ.refresh_cols)
        plan = core.refresh_counts(cur, ZZ)
        res = core.refresh_latest(cur, ZZ)
        full_a = core.period_hashes(cur, ZZ, "live")
        kept_a = core.period_hashes(cur, ZZ, "live", exclude=ZZ.refresh_cols)
        cur.execute(f"""SELECT lad24cd, accom_type, claimants FROM public.{ZL}
                        WHERE month = %s ORDER BY 1, 2""", (a,))
        live_a = {(x, y): c for x, y, c in cur.fetchall()}
        want = {(r["lad24cd"], r["accom_type"]): r["claimants"] for r in changed}
        return (before_ok, kind, plan, res, full_b, full_a, kept_b, kept_a,
                live_a == want, editions_of(cur, b),
                load_checks.check_latest_equals_live(cur, ZZ))
    try:
        (before_ok, kind, plan, res, full_b, full_a, kept_b, kept_a, a_ok,
         b_eds, after) = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(8, name, False, str(e).splitlines()[0])
    ok = (before_ok and kind == "revised" and plan == {a: 1}
          and res["updated"] == {a: 1} and res["rows"] == 1
          and full_b[b] == full_a[b] and kept_b[b] == kept_a[b]
          and kept_b[a] == kept_a[a] and full_b[a] != full_a[a]
          and a_ok and [e[0] for e in b_eds] == [1] and after == [])
    report(8, name, ok, f"edition 2 stored ({kind}); plan {plan}; updated "
           f"{res['updated']} ({res['rows']} row); {b} untouched="
           f"{full_b[b] == full_a[b]}; loaded_at and other columns kept="
           f"{kept_b[a] == kept_a[a]}; latest equals live after={after == []}")


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
    raw = {"E1a": 3, "E1b": 4, "E2a": 5, "E2b": None, "E3a": 0, "E4a": 0,
           "E4b": 0}
    got = {r["lad24cd"]: r["claimants"]
           for r in m.build_records(raw, uris, "202601", "SA")}
    absent = {r["lad24cd"]: r["claimants"] for r in m.build_records(
        {"E1a": 1}, {"E1": ["u:E1a", "u:E1b"]}, "202601", "SA")}
    ok = (got == {"E1": 7, "E2": None, "E3": 0, "E4": 0}
          and absent == {"E1": None})
    report(10, name, ok, f"3+4 -> {got['E1']}; 5+null -> {got['E2']}; single "
           f"0 -> {got['E3']}; 0+0 -> {got['E4']}; a part absent from the "
           f"response -> {absent['E1']}")


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

    def run(argv):
        conn = mock.MagicMock()
        mcur = conn.cursor.return_value.__enter__.return_value
        mcur.connection = conn  # load_months commits through cur.connection
        with mock.patch.object(m, "_conn", return_value=conn), \
                mock.patch.object(m, "table_exists", return_value=True), \
                mock.patch.object(m, "held_months", return_value=[month]), \
                mock.patch.object(m, "get_available_months",
                                  return_value=[month]), \
                mock.patch.object(m, "get_english_la_members", return_value=[]), \
                mock.patch.object(m, "resolve_geography",
                                  return_value=({}, [])), \
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
          and "ROLLBACK TO SAVEPOINT s8b_month" in sql0
          and commits_s == 0 and applied_s == 1
          # --commit sees two commits: the month, then the run-log row
          and commits_c == 2 and applied_c == 1
          and "RELEASE SAVEPOINT s8b_month" in sql_c
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
    b = [dict(r, claimants=r["claimants"] + 7) if r["lad24cd"] == CODES[3]
         else r for r in a]

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
    n = m.EXPECTED_AREAS * len(m.ACCOM_TYPES)
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
    r = recs(month, value=lambda i, t: None if i == 0 else (0 if i == 1 else i))
    n = m.EXPECTED_AREAS * len(m.ACCOM_TYPES)

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
          and stamped == n and diff == 0 and chk == [] and st["ok"]
          and st["live_missing"] == []
          and f_rc == 1 and f_seen.get("editions_when_live_failed") == 1
          and f_eds == [] and f_live == 0
          and p_rc == 0 and p_eds == [] and p_live == 0
          and "would store edition 1 and insert 1,184 live rows" in p_out)
    report(16, name, ok, f"{kind}: editions {[(e, s) for e, s, _ in eds]}, "
           f"live rows {live_n} (loaded_at set {stamped}), cells differing "
           f"{diff}, status ok={st['ok']}; planted live failure: rc {f_rc}, "
           f"edition 1 written first={f_seen.get('editions_when_live_failed') == 1}"
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
    files = [Path(__file__).resolve(), here / "s8b_hb_editions.py",
             here / "statxplore_client.py"]
    key = api_key()
    if not key:
        # no key in this environment: the source check still has to hold, so
        # look for anything assigned to an APIKey / api_key literal instead
        import re
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


def gate_15_no_network_no_key(cur):
    name = "no network and no key in output"
    month = "202601"
    attempts, calls = [], []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")

    def cube(path, body):
        calls.append(path)
        geo = body["recodes"][m.GEO_FIELD_ID]["map"]
        items = [{"uris": [u[0]]} for u in geo]
        return {"cubes": {"c": {"values": [[1] for _ in geo]}},
                "fields": [{"items": items}]}
    lad_to_uris = {c: [f"str:value:hb_new:X:{c}"] for c in CODES}
    query = [u for v in lad_to_uris.values() for u in v]
    fetched, rc = None, None
    try:
        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse), \
                mock.patch.object(socket, "getaddrinfo", refuse), \
                mock.patch("statxplore_client.api_post", side_effect=cube), \
                mock.patch.object(m.time, "sleep"), _quiet():
            fetched = m.fetch_month(month, lad_to_uris, query)
            rc = _in_savepoint(cur, lambda c: m.load_months(
                c, ZZ, [month], lambda mo: fetched, FETCHED, False))
    except Exception as e:  # a blocked socket or any other failure
        return report(15, name, False, f"{type(e).__name__}: {e}")
    key = api_key()
    leaked = bool(key) and any(key in line for line in OUTPUT)
    ok = (not attempts and rc == 0 and len(fetched) == 4 * m.EXPECTED_AREAS
          and len(calls) > 0 and not leaked)
    report(15, name, ok, f"stub api_post called {len(calls)} times, socket "
           f"attempts {len(attempts)}, load_months returned {rc}; "
           f"statxplore_client imported only by this gate="
           f"{not LATE_IMPORT_AT_START}; key checked against "
           f"{len(OUTPUT)} output lines: "
           f"{'LEAKED' if leaked else 'not present'}"
           + ("" if key else " (no key in this environment)"))


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
            gate_7_unknown_zeros(cur)
            gate_8_seeded_revision(cur)
            gate_9_fork_and_second_root(cur)
            gate_10_merged_area_rule(cur)
            gate_11_planner(cur)
            gate_12_preview_default(cur)
            gate_13_key_not_in_source_or_output(cur)
            gate_14_revert_new_edition(cur)
            gate_15_no_network_no_key(cur)
            gate_16_new_month_reaches_live(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
