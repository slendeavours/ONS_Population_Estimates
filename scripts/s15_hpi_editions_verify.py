"""Gates for the S15 (UK HPI house prices) edition-history table.

Mirrors scripts/s19_pip_editions_verify.py. Prints `GATE n name: PASS|FAIL`;
exits 1 on any FAIL. Gates 2 to 7 read the real tables (la_house_prices and
la_house_prices_editions). Until the editions table exists they print
`FAIL (pending load)` with the words "not yet migrated", so the script exits
non-zero until then, like the S1, S1b, S8b and S19 verifiers; nothing is
created to make them pass. The seeded gates never need the real editions
table: they run on throwaway tables zz_s15_live and zz_s15_editions (a copy
of the loader's SPEC named zz_s15), created inside the transaction, and call
the loader's own writers and commands (pe.apply_period, pe.load_periods,
core.refresh_latest, and cmd_load, cmd_sync_new and load_file_records of
s15_hpi_editions with SPEC pointed at the copy) rather than a
re-implementation. The real tables are only ever read.

Gates:
  1  table shape and immutability (UPDATE/DELETE/TRUNCATE blocked), on the
     throwaway copy; also the shape of the real table once it exists
  2  edition 1 present for every live period                      (real)
  3  latest edition equals live for every period                  (real)
  4  every edition of every period has the held area count        (real)
  5  no zero or negative prices where present                     (real)
  6  NULL versus 0 never conflated, on each of the seven value columns
     (seeded, including a property-type-only change; plus real data) (real)
  7  suppressed (NULL) property-type counts listed, not failed    (real)
  8  seeded revision: refresh_latest updates only that period's changed rows
  9  seeded fork and second root refused (chain_tip, and the loader's compare)
  10 build_records rules: suppressed is None, a missing property-type match is
     None and counted, unresolved codes returned
  11 window planner never lists a period before the earliest held
  12 preview default: the load command with no flag issues no commit
  13 no network (a socket attempt fails the gate), no secret in source or output
  14 revert stored as a new edition (A, B, A -> edition 3; A again stores nothing)
  15 new period reaches live in one transaction (a planted live-insert failure
     leaves neither; preview writes nothing)
  16 file identity: mismatched names or a stale latest period are caught
     before any write
  17 area-code reconciliation order (hard recode, valid, lookup, unresolved)
  18 the page parsers work on the sample pages and halt on a changed layout
  19 sync-new records edition 1 for a period with several load dates, taking
     the latest (as_loaded_date = 'latest'); the 'single' control halts
  20 Barnsley and Sheffield through geography.resolve: a form that disagrees
     with S15's declaration ('new') halts before any write

Usage:
    python scripts/s15_hpi_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The commands get a stand-in
connection whose commit only counts. No network, no real-table writes, no
backend is ever terminated.
"""
import contextlib
import dataclasses
import io
import os
import re
import socket
import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, get_conn  # noqa: E402
import editions_core as core  # noqa: E402
import geography  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
import s15_hpi_editions as m  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC, LIVE, TABLE = m.SPEC, m.LIVE, m.TABLE
ZZ = dataclasses.replace(
    SPEC, name="zz_s15", live_table="zz_s15_live",
    editions_table="zz_s15_editions", fk_la_boundaries=False)
ZL, ZE = ZZ.live_table, ZZ.editions_table
FETCHED = date(2026, 10, 8)
N = 16                                   # areas in the seeded data
CODES = [f"E06{n:06d}" for n in range(1, N + 1)]
SPARE = "E06000099"
P1, P2, P3 = "2026-05-01", "2026-06-01", "2026-07-01"
COLS = m.VALUE_COLUMNS
PRICE_COLS = m.PRICE_COLUMNS
PT_COLS = ("avg_price_detached", "avg_price_semi", "avg_price_terraced",
           "avg_price_flat")
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (13)


def report(n, name, ok, detail="", pending=False):
    RESULTS.append(ok)
    verdict = "PASS" if ok else ("FAIL (pending load)" if pending else "FAIL")
    line = f"GATE {n} {name}: {verdict}" + (f"  [{detail}]" if detail else "")
    OUTPUT.append(line)
    print(line)


def table_exists(cur, table=TABLE):
    return pe.table_exists(cur, table)


def _s(x):
    return str(x)


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
    """Swallow what the loader prints, but keep it for the leak gate."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf
    OUTPUT.extend(buf.getvalue().splitlines())


# ------------------------------------------------------------------ data

_OFFSET = {"avg_price_all": 0, "avg_price_all_sa": 5, "avg_price_detached": 100,
           "avg_price_semi": 50, "avg_price_terraced": -50,
           "avg_price_flat": -100}


def val(period, i, col):
    if col == "annual_change_pct":
        return Decimal("1.50")
    return Decimal(200000 + 1000 * i + 10 * int(period[5:7])
                   + _OFFSET[col]).quantize(Decimal("0.01"))


def recs(period, n=N, over=None, codes=CODES):
    """The period's records; over {(area index, column): value} (None
    allowed) replaces cells."""
    over = over or {}
    out = []
    for i in range(n):
        r = {"lad24cd": codes[i], "period": period}
        for c in COLS:
            r[c] = over[(i, c)] if (i, c) in over else val(period, i, c)
        out.append(r)
    return out


def setup_throwaway(cur):
    cur.execute("SELECT to_regclass('public.zz_s15_live'), "
                "to_regclass('public.zz_s15_editions')")
    if cur.fetchone() != (None, None):
        sys.exit("HARD STOP: zz_s15_live or zz_s15_editions already exists as "
                 "a real table; refusing to run")
    cur.execute(f"""CREATE TABLE public.{ZL} (
        lad24cd varchar(9) NOT NULL, period date NOT NULL,
        avg_price_all numeric(12,2), avg_price_all_sa numeric(12,2),
        annual_change_pct numeric(6,2), avg_price_detached numeric(12,2),
        avg_price_semi numeric(12,2), avg_price_terraced numeric(12,2),
        avg_price_flat numeric(12,2),
        loaded_at timestamptz DEFAULT now(),
        PRIMARY KEY (lad24cd, period))""")
    core.create_schema(cur, ZZ)


def seed_live(cur, records, loaded_at=None):
    cols = ("lad24cd", "period") + COLS
    for r in records:
        cur.execute(
            f"INSERT INTO public.{ZL} ({', '.join(cols)}"
            + (", loaded_at" if loaded_at else "") + ") VALUES ("
            + ", ".join(["%s"] * len(cols)) + (", %s" if loaded_at else "")
            + ")", [r[c] for c in cols] + ([loaded_at(r)] if loaded_at else []))


def editions_of(cur, period=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{ZE}"
                + (" WHERE period = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def live_rows(cur, period):
    cur.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE period = %s",
                (period,))
    return cur.fetchone()[0]


def raw_edition(cur, period, edition, supersedes, sha):
    """One throwaway-table row planted directly (the loader's writer refuses a
    fork or a second root, so a fault has to be planted by hand)."""
    cur.execute(f"""INSERT INTO public.{ZE} (lad24cd, period, edition,
                    avg_price_all, supersedes, source_sha256)
                    VALUES (%s, %s, %s, 1, %s, %s)""",
                (CODES[0], period, edition, supersedes, sha))


def prof(edition="2026-07", n=N, required=()):
    """The loader's own per-run profile on the throwaway spec."""
    return m.run_profile(ZZ, edition, n, required)


def apply(cur, period, records, edition="2026-07"):
    """The loader's own writer for one period: pe.apply_period."""
    return pe.apply_period(cur, prof(edition), period, records,
                           fetched_on=FETCHED, source_file="gate")


# ----------------------------------------------- stand-ins for the commands

class _FakeConn:
    """cur.connection for the engine: commit and rollback only count."""

    def __init__(self, real):
        self.encoding = real.encoding
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Cur:
    """The real cursor with a stand-in .connection."""

    def __init__(self, cur):
        self._cur = cur
        self.connection = _FakeConn(cur.connection)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _Borrowed:
    """A connection stand-in for the commands: hands out the proxy cursor and
    never commits, rolls back or closes the real connection."""

    def __init__(self, cur):
        self.cur = _Cur(cur)
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def cursor(self):
        yield self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


AVG_HEAD = ("Date,Region_Name,Area_Code,Average_Price,Monthly_Change,"
            "Annual_Change,Average_Price_SA")
PT_HEAD = ("Date,Region_Name,Area_Code,Detached_Average_Price,Detached_Index,"
           "Detached_Monthly_Change,Detached_Annual_Change,"
           "Semi_Detached_Average_Price,Semi_Detached_Index,"
           "Semi_Detached_Monthly_Change,Semi_Detached_Annual_Change,"
           "Terraced_Average_Price,Terraced_Index,Terraced_Monthly_Change,"
           "Terraced_Annual_Change,Flat_Average_Price,Flat_Index,"
           "Flat_Monthly_Change,Flat_Annual_Change")


def write_files(d, edition, periods, *, codes=None, bump=None, extra=(),
                avg_name=None, pt_name=None):
    """The two CSVs in directory d. codes {period: [codes]} (default CODES),
    bump {(period, code): +x} on the average price, extra (Area_Code, Date)
    rows added to the average-prices file. A Welsh row and a pre-window row
    are always there (both out of scope)."""
    codes, bump = codes or {}, bump or {}
    avg = [AVG_HEAD, "2021-12-01,Hartlepool,E06000001,1,,1.0,1",
           f"{periods[-1]},Cardiff,W06000015,5,,1.0,5"]
    pt = [PT_HEAD]
    for p in periods:
        for c in codes.get(p, CODES):
            i = CODES.index(c) if c in CODES else N
            v = 200000 + 1000 * i + 10 * int(p[5:7]) + bump.get((p, c), 0)
            avg.append(f"{p},Area {c},{c},{v},0.1,1.5,{v + 5}")
            pt.append(f"{p},Area {c},{c},{v + 100},1,0,0,{v + 50},1,0,0,"
                      f"{v - 50},1,0,0,{v - 100},1,0,0")
    for c, p in extra:
        avg.append(f"{p},Area {c},{c},123,0,1.0,124")
    a = Path(d) / (avg_name or f"Average-prices-{edition}.csv")
    b = Path(d) / (pt_name or f"Average-prices-Property-Type-{edition}.csv")
    a.write_text("\n".join(avg) + "\n", encoding="utf-8")
    b.write_text("\n".join(pt) + "\n", encoding="utf-8")
    return a, b


def run_main(cur, argv, *, files=None, table=True, fetch=None):
    """The loader's main(argv) on the throwaway tables, with SPEC pointed at
    the copy, a stand-in connection, stubbed reference codes (the throwaway
    areas are not in la_boundaries) and a stubbed run log. files (avg, pt)
    are passed as --avg-prices/--property-type. Returns (rc or 'halt', text,
    connection stand-in, run-log mock)."""
    borrowed = _Borrowed(cur)
    args = list(argv)
    if files:
        args += ["--avg-prices", str(files[0]),
                 "--property-type", str(files[1])]
    out = io.StringIO()
    with mock.patch.object(m, "SPEC", ZZ), \
            mock.patch.object(m, "_conn", return_value=borrowed), \
            mock.patch.object(m, "reference_codes",
                              return_value=(set(CODES) | {SPARE}, {})), \
            mock.patch.object(m, "fetch_latest_files",
                              side_effect=fetch or AssertionError(
                                  "no download in the verify script")), \
            mock.patch.object(m, "log_run") as logged, \
            mock.patch.object(m, "table_exists",
                              side_effect=lambda c, t: table
                              and pe.table_exists(c, t)), \
            contextlib.redirect_stdout(out):
        assert m.SPEC is ZZ, "SPEC was not pointed at the throwaway copy"
        try:
            rc = m.main(args)
        except SystemExit as e:
            text = f"{out.getvalue()}\n{e.code}"
            OUTPUT.extend(text.splitlines())
            return "halt", text, borrowed, logged
    OUTPUT.extend(out.getvalue().splitlines())
    return rc, out.getvalue(), borrowed, logged


def seed_via_load(cur, tmp, periods=(P1, P2), edition="2026-06"):
    """First run through the loader itself: nothing held, --min-period and
    --expected-areas (the stand-in connection never commits)."""
    f = write_files(tmp, edition, list(periods))
    rc, text, _, _ = run_main(
        cur, ["load", "--commit", "--min-period", periods[0],
              "--expected-areas", str(N)], files=f)
    if rc != 0:
        raise RuntimeError(f"seed load failed: {text[-300:]}")
    return f


# ---------------------------------------------------------------- gate 1

def shape_problems(cur, spec, fk):
    t = spec.editions_table
    cur.execute("""SELECT column_name, data_type, character_maximum_length,
                          numeric_precision, numeric_scale
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (t,))
    cols = {r[0]: r[1:] for r in cur.fetchall()}
    need = {*spec.key_cols, spec.period_col, "edition",
            *(c for c, _ in spec.value_cols), "release_label",
            "published_date", "source_file", "source_sha256", "supersedes",
            "loaded_at"}
    bad = []
    if need - set(cols):
        bad.append(f"missing columns {sorted(need - set(cols))}")
    if cols.get("lad24cd") != ("character varying", 9, None, None):
        bad.append(f"lad24cd is {cols.get('lad24cd')}")
    if cols.get(spec.period_col, ("",))[0] != "date":
        bad.append(f"{spec.period_col} is {cols.get(spec.period_col)}")
    for c, sql in spec.value_cols:
        p, s = re.fullmatch(r"numeric\((\d+),(\d+)\)", sql).groups()
        if cols.get(c) != ("numeric", None, int(p), int(s)):
            bad.append(f"{c} is {cols.get(c)}, expected {sql}")
    for c in ("edition", "supersedes"):
        if cols.get(c, ("",))[0] != "integer":
            bad.append(f"{c} is {cols.get(c)}")
    cur.execute("""SELECT a.attname FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid
                    AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = %s::regclass AND i.indisprimary""",
                (f"public.{t}",))
    if {r[0] for r in cur.fetchall()} != {"lad24cd", "period", "edition"}:
        bad.append("primary key is not (lad24cd, period, edition)")
    cur.execute("""SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass""", (f"public.{t}",))
    cons = cur.fetchall()
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
    stmts = {"UPDATE": f"UPDATE public.{t} SET avg_price_all = 2",
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
            "blocked)")
    bad = shape_problems(cur, ZZ, fk=False)

    def seed(cur):
        core.insert_edition(cur, ZZ, recs(P1, n=1), P1, release_label="t",
                            published_date=None, source_file="gate",
                            source_sha256="h0", supersedes=None)
    res = immutability(cur, ZZ, seed)
    for k, msg in res.items():
        if not (msg and "append-only" in msg):
            bad.append(f"{k} not blocked ({msg})")
    scope = "throwaway copy"
    if table_exists(cur):
        scope += " and real table"
        bad += [f"real: {p}" for p in shape_problems(cur, SPEC, fk=True)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise append-only")


# ------------------------------------------------- real-table gates (2 to 7)

PENDING = f"{TABLE} does not exist yet (not yet migrated)"


def real_gate(cur, n, name, fn):
    """Run fn(cur) -> (ok, detail) on the real tables, or report pending when
    the editions table has not been created."""
    if not table_exists(cur):
        return report(n, name, False, PENDING, pending=True)
    try:
        ok, detail = fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(n, name, False, str(e).splitlines()[0])
    report(n, name, ok, detail)


def _live_periods(cur, spec=SPEC):
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def real_edition1_present(cur, spec=SPEC):
    periods = _live_periods(cur, spec)
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE edition = 1")
    have = {r[0] for r in cur.fetchall()}
    missing = [_s(x) for x in periods if x not in have]
    return (not missing and bool(periods),
            ("no live periods to check" if not periods else
             f"{len(periods)} live periods" if not missing else
             f"no edition 1 for {missing[:6]} ({len(missing)} of "
             f"{len(periods)})"))


def real_latest_equals_live(cur, spec=SPEC):
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live periods to check (an empty state is not a pass)"
    bad = load_checks.check_latest_equals_live(cur, spec)
    return not bad, "; ".join(bad[:4]) if bad else (
        f"{len(periods)} periods match row for row")


def real_coverage(cur, spec=SPEC):
    """Every edition of every period holds the held area count (the live
    table's modal count) with distinct keys (load_checks.check_coverage), and
    the chain tip has that many distinct areas."""
    want_n = m.held_area_count(cur, spec)
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} UNION SELECT DISTINCT "
                f"{spec.period_col} FROM public.{spec.editions_table} "
                "ORDER BY 1")
    periods = [r[0] for r in cur.fetchall()]
    if want_n is None or not periods:
        return False, ("no periods or held area count to check (an empty "
                       "state is not a pass)")
    bad, n_ed = [], 0
    for p in periods:
        cur.execute(f"SELECT DISTINCT edition FROM public.{spec.editions_table}"
                    f" WHERE {spec.period_col} = %s ORDER BY 1", (p,))
        eds = [r[0] for r in cur.fetchall()]
        if not eds:
            bad.append(f"{p}: no editions")
            continue
        for ed in eds:
            n_ed += 1
            bad += load_checks.check_coverage(cur, spec, p, ed, want_n)
        try:
            tip = core.chain_tip(cur, spec, p)
        except (LookupError, ValueError) as e:
            bad.append(str(e))
            continue
        cur.execute(f"""SELECT COUNT(DISTINCT lad24cd)
                        FROM public.{spec.editions_table}
                        WHERE {spec.period_col} = %s AND edition = %s""",
                    (p, tip))
        got = cur.fetchone()[0]
        if got != want_n:
            bad.append(f"{p} ed{tip}: {got} areas")
    if not n_ed:
        return False, "no editions to check (an empty state is not a pass)"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} periods, {n_ed} editions, each {want_n} rows = "
        f"{want_n} areas (held count)")


def real_no_nonpositive(cur, spec=SPEC):
    """No zero or negative price where one is present (NULL is a suppressed
    price, not a bad one). annual_change_pct is a rate and may be negative."""
    counts = {}
    cond = " OR ".join(f"{c} <= 0" for c in PRICE_COLS)
    for label, table in (("editions", spec.editions_table),
                         ("live", spec.live_table)):
        cur.execute(f"SELECT COUNT(*) FROM public.{table} WHERE {cond}")
        counts[label] = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table}")
    if not cur.fetchone()[0]:
        return False, "no live rows to check (an empty state is not a pass)"
    return not any(counts.values()), (
        f"zero or negative price cells: editions {counts['editions']}, live "
        f"{counts['live']}")


def nonpositive_cells(rows):
    """Cells <= 0 among price values (None skipped) of rows of price tuples."""
    return sum(1 for row in rows for v in row if v is not None and v <= 0)


def null_zero_counts(cur, table, where="", params=()):
    """(NULL count, zero count) per value column, as a flat tuple."""
    parts = []
    for c in COLS:
        parts += [f"COUNT(*) FILTER (WHERE {c} IS NULL)",
                  f"COUNT(*) FILTER (WHERE {c} = 0)"]
    cur.execute(f"SELECT {', '.join(parts)} FROM public.{table} {where}",
                params)
    return cur.fetchone()


def real_null_vs_zero(cur, spec=SPEC):
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live periods to check (an empty state is not a pass)"
    bad = load_checks.check_latest_equals_live(cur, spec)
    diffs = []
    pc = spec.period_col
    for p in periods:
        try:
            ed = core.chain_tip(cur, spec, p)
        except (LookupError, ValueError) as e:
            diffs.append(str(e))
            continue
        ln = null_zero_counts(cur, spec.live_table, f"WHERE {pc} = %s", (p,))
        en = null_zero_counts(cur, spec.editions_table,
                              f"WHERE {pc} = %s AND edition = %s", (p, ed))
        if ln != en:
            diffs.append(f"{p}: live (NULL, 0) per column {ln} vs ed{ed} {en}")
    return not bad and not diffs, "; ".join((bad + diffs)[:3]) if (
        bad or diffs) else (
        "NULL and 0 counts per period and column identical in live and the "
        "latest edition; EXCEPT comparison clean")


def suppressed_report(rows):
    """{property-type column: NULL cells} from rows of the four
    property-type values. A suppressed low-volume price is NULL (a value that
    is missing, not zero)."""
    out = {c: 0 for c in PT_COLS}
    for row in rows:
        for c, v in zip(PT_COLS, row):
            if v is None:
                out[c] += 1
    return out


def real_suppressed_listing(cur, spec=SPEC):
    tips, new, errors = core.latest_map(cur, spec)
    if not tips:
        return False, "no periods to list (an empty state is not a pass)"
    if errors or new:
        return False, f"cannot list: new={new} errors={errors}"
    tot = {c: 0 for c in PT_COLS}
    periods_with = {c: 0 for c in PT_COLS}
    filters = ", ".join(f"COUNT(*) FILTER (WHERE {c} IS NULL)" for c in PT_COLS)
    for p, ed in tips.items():
        cur.execute(f"""SELECT {filters} FROM public.{spec.editions_table}
                        WHERE {spec.period_col} = %s AND edition = %s""",
                    (p, ed))
        for c, n in zip(PT_COLS, cur.fetchone()):
            tot[c] += n
            periods_with[c] += 1 if n else 0
    listed = ", ".join(f"{c} {tot[c]} suppressed cells in {periods_with[c]} "
                       "periods" for c in PT_COLS)
    return True, f"suppressed (NULL) counts are listed, not failed: {listed}"


def gate_2_edition1(cur):
    real_gate(cur, 2, "edition 1 present for every live period",
              real_edition1_present)


def gate_3_latest_equals_live(cur):
    real_gate(cur, 3, "latest edition equals live for every period",
              real_latest_equals_live)


def gate_4_coverage(cur):
    real_gate(cur, 4, "every edition of every period has the held area count",
              real_coverage)


def gate_5_nonpositive(cur):
    name = "no zero or negative prices where present"
    sample = nonpositive_cells([(None, Decimal("1.00")), (Decimal("0.00"), 5),
                                (Decimal("-3.00"), None)])
    if sample != 2:
        return report(5, name, False, f"nonpositive_cells wrong on a sample "
                      f"{sample}")
    real_gate(cur, 5, name, real_no_nonpositive)


# (label, column, area index, new value): the base period holds NULL at area
# 2j and 0 at area 2j+1 for the j-th value column
def _variants():
    out = []
    for j, c in enumerate(COLS):
        out.append((f"{c} NULL->0", c, 2 * j, Decimal("0.00")))
        out.append((f"{c} 0->NULL", c, 2 * j + 1, None))
    return tuple(out)


VARIANTS = _variants()


def null_zero_base():
    over = {}
    for j, c in enumerate(COLS):
        over[(2 * j, c)] = None
        over[(2 * j + 1, c)] = Decimal("0.00")
    return over


def seeded_null_zero(cur):
    """The scenario of gate 6, on throwaway tables, through the loader's
    writer: each of the seven value columns changed from NULL to 0 and from 0
    to NULL, one cell at a time."""
    base = recs(P1, over=null_zero_base())

    def body(cur):
        first = apply(cur, P1, base)
        clean = load_checks.check_latest_equals_live(cur, ZZ)
        out = {"first": first, "clean": clean}
        for label, col, area, new in VARIANTS:
            def variant(cur, col=col, area=area, new=new):
                changed = [dict(r, **{col: new})
                           if r["lad24cd"] == CODES[area] else r for r in base]
                kind = pe.classify_period(cur, prof(), P1, changed)
                apply(cur, P1, changed)
                flagged = load_checks.check_latest_equals_live(cur, ZZ)
                stored = editions_of(cur, P1)
                cur.execute(f"""SELECT {col} IS NULL, {col}
                    FROM public.{ZE}
                    WHERE period = %s AND edition = 2 AND lad24cd = %s""",
                            (P1, CODES[area]))
                return kind, flagged, stored, cur.fetchone()
            out[label] = _in_savepoint(cur, variant)
        return out
    return _in_savepoint(cur, body)


def variant_ok(res, new):
    kind, flagged, stored, cell = res
    return (kind == "revised" and len(stored) == 2
            and cell == (new is None, new)
            and len(flagged) == 1 and "1 edition-only" in flagged[0])


def gate_6_null_vs_zero(cur):
    name = "NULL versus 0 never conflated (all seven value columns)"
    try:
        s = seeded_null_zero(cur)
    except (psycopg2.Error, SystemExit) as e:
        return report(6, name, False, f"seeded scenario: {e}")
    ok = s["first"] == "new" and s["clean"] == []
    bad = []
    for label, _col, _area, new in VARIANTS:
        if not variant_ok(s[label], new):
            ok = False
            bad.append(f"{label}: {s[label][0]}, edition 2 holds {s[label][3]}")
    pt = [v for v in VARIANTS if v[1] in PT_COLS]
    notes = (f"{len(VARIANTS)} single-cell changes over {len(COLS)} columns "
             f"({len(pt)} on property-type columns alone) each stored as "
             "edition 2 with the cell read back exactly")
    if not ok:
        return report(6, name, False, "seeded scenario: " + "; ".join(bad[:4]
                      or ["first load or clean state wrong"]))
    if not table_exists(cur):
        return report(6, name, False, f"{PENDING}; seeded scenario passes: "
                      + notes, pending=True)
    real_gate(cur, 6, name, lambda c: (lambda r: (r[0], f"{r[1]}; seeded "
              f"scenario: {notes}"))(real_null_vs_zero(c)))


def gate_7_suppressed_listing(cur):
    name = "suppressed (NULL) property-type counts listed, not failed"
    sample = suppressed_report([(None, 5, None, 0), (None, None, None, None),
                                (1, 2, 3, 4)])
    if sample != {"avg_price_detached": 2, "avg_price_semi": 1,
                  "avg_price_terraced": 2, "avg_price_flat": 1}:
        return report(7, name, False, f"suppressed_report wrong on a sample "
                      f"{sample}")
    real_gate(cur, 7, name, real_suppressed_listing)


# ------------------------------------------------------------ seeded gates

def seeded_property_type_only(cur):
    """A change in one property-type column alone (flat), the other six
    columns unchanged, is a revision stored as edition 2 and refreshed into
    live (that period's one changed row only)."""
    def body(cur):
        for p in (P1, P2):
            apply(cur, p, recs(p))
        changed = recs(P1, over={(4, "avg_price_flat"): val(P1, 4, "avg_price_flat") + 500})
        kind = pe.classify_period(cur, prof(), P1, changed)
        apply(cur, P1, changed)
        eds = editions_of(cur, P1)
        plan = {_s(k): v for k, v in core.refresh_counts(cur, ZZ).items()}
        res = core.refresh_latest(cur, ZZ)
        cur.execute(f"SELECT avg_price_flat, avg_price_all FROM public.{ZL} "
                    "WHERE period = %s AND lad24cd = %s", (P1, CODES[4]))
        row = cur.fetchone()
        return {"kind": kind, "eds": eds, "plan": plan,
                "updated": {_s(k): v for k, v in res["updated"].items()},
                "rows": res["rows"], "row": row,
                "want": (val(P1, 4, "avg_price_flat") + 500,
                         val(P1, 4, "avg_price_all")),
                "after": load_checks.check_latest_equals_live(cur, ZZ)}
    return _in_savepoint(cur, body)


def property_type_only_ok(e):
    return (e["kind"] == "revised" and [x[0] for x in e["eds"]] == [1, 2]
            and e["plan"] == {P1: 1} and e["updated"] == {P1: 1}
            and e["rows"] == 1 and e["row"] == e["want"] and e["after"] == [])


def gate_8_seeded_revision(cur):
    name = ("seeded revision: edition 2 with one changed cell, refresh_latest "
            "updates only that period's changed row")

    def body(cur):
        ra, rb = recs(P1), recs(P2)
        for p, rr in ((P1, ra), (P2, rb)):
            apply(cur, p, rr)
        before_ok = load_checks.check_latest_equals_live(cur, ZZ) == []
        changed = [dict(r, avg_price_all=r["avg_price_all"] + 1000)
                   if r["lad24cd"] == CODES[5] else r for r in ra]
        kind = apply(cur, P1, changed)
        full_b = {_s(k): v for k, v in
                  core.period_hashes(cur, ZZ, "live").items()}
        kept_b = {_s(k): v for k, v in core.period_hashes(
            cur, ZZ, "live", exclude=ZZ.refresh_cols).items()}
        plan = {_s(k): v for k, v in core.refresh_counts(cur, ZZ).items()}
        res = core.refresh_latest(cur, ZZ)
        full_a = {_s(k): v for k, v in
                  core.period_hashes(cur, ZZ, "live").items()}
        kept_a = {_s(k): v for k, v in core.period_hashes(
            cur, ZZ, "live", exclude=ZZ.refresh_cols).items()}
        cur.execute(f"""SELECT lad24cd, avg_price_all FROM public.{ZL}
                        WHERE period = %s ORDER BY 1""", (P1,))
        live_a = dict(cur.fetchall())
        want = {r["lad24cd"]: r["avg_price_all"] for r in changed}
        return (before_ok, kind, plan, res, full_b, full_a, kept_b, kept_a,
                live_a == want, editions_of(cur, P2),
                load_checks.check_latest_equals_live(cur, ZZ))
    try:
        (before_ok, kind, plan, res, full_b, full_a, kept_b, kept_a, a_ok,
         b_eds, after) = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(8, name, False, str(e).splitlines()[0])
    try:
        pto = seeded_property_type_only(cur)
    except (psycopg2.Error, SystemExit, KeyError) as e:
        return report(8, name, False, f"property-type-only scenario: {e}")
    updated = {_s(k): v for k, v in res["updated"].items()}
    ok = (property_type_only_ok(pto) and before_ok and kind == "revised"
          and plan == {P1: 1} and updated == {P1: 1} and res["rows"] == 1
          and P1 in full_b and P2 in full_b
          and full_b[P2] == full_a[P2] and kept_b[P2] == kept_a[P2]
          and kept_b[P1] == kept_a[P1] and full_b[P1] != full_a[P1]
          and a_ok and [e[0] for e in b_eds] == [1] and after == [])
    report(8, name, ok, f"edition 2 stored ({kind}); plan {plan}; updated "
           f"{updated} ({res['rows']} row); {P2} untouched="
           f"{full_b.get(P2) == full_a.get(P2)}; loaded_at and other columns "
           f"kept={kept_b.get(P1) == kept_a.get(P1)}; latest equals live "
           f"after={after == []}; one property-type column alone: "
           f"{pto['kind']}, editions {[x[0] for x in pto['eds']]}, refreshed "
           f"{pto['updated']}")


def gate_9_fork_and_second_root(cur):
    name = "seeded fork and second root are refused (chain_tip, loader compare)"
    recs1 = recs(P1)

    def cmp_(cur):
        return _halts(lambda: pe.compare_period(cur, prof(), P1, recs1))

    def fork(cur):
        raw_edition(cur, P1, 1, None, "r1")
        raw_edition(cur, P1, 2, 1, "r2")
        raw_edition(cur, P1, 3, 1, "r3")
        return (_raises(lambda: core.chain_tip(cur, ZZ, P1), ValueError),
                cmp_(cur))

    def roots(cur):
        raw_edition(cur, P1, 1, None, "r1")
        raw_edition(cur, P1, 2, None, "r2")
        return (_raises(lambda: core.chain_tip(cur, ZZ, P1), ValueError),
                cmp_(cur))

    def good(cur):
        raw_edition(cur, P1, 1, None, "r1")
        raw_edition(cur, P1, 2, 1, "r2")
        return core.chain_tip(cur, ZZ, P1)
    try:
        f_tip, f_cmp = _in_savepoint(cur, fork)
        r_tip, r_cmp = _in_savepoint(cur, roots)
        tip = _in_savepoint(cur, good)
    except psycopg2.Error as e:
        return report(9, name, False, str(e).splitlines()[0])
    ok = (f_tip[0] and "fork" in f_tip[1] and f_cmp[0]
          and r_tip[0] and "no single root" in r_tip[1] and r_cmp[0]
          and tip == 2)
    report(9, name, ok, f"fork: {f_tip[1][:60]!r}, loader halts={f_cmp[0]}; "
           f"second root: {r_tip[1][:60]!r}, loader halts={r_cmp[0]}; "
           f"clean chain tip={tip}")


def _avg(code, date_, price="100000", sa="100001", change="1.5"):
    return {"Area_Code": code, "Date": date_, "Average_Price": price,
            "Average_Price_SA": sa, "Annual_Change": change}


def _pt(code, date_, d="1", s="2", t="3", f="4"):
    return {"Area_Code": code, "Date": date_, "Detached_Average_Price": d,
            "Semi_Detached_Average_Price": s,
            "Terraced_Average_Price": t, "Flat_Average_Price": f}


def gate_10_build_records_rules(cur):
    name = ("build_records rules: suppressed is None, a missing property-type "
            "match is None and counted, unresolved codes returned")
    d = "2026-07-01"
    avg = [_avg("E06000001", d, price="NA", sa="", change="0"),
           _avg("E06000002", d),                       # no property-type row
           _avg("E06000003", d),
           _avg("E06999998", d),                       # unresolvable
           _avg("E08000038", d),                       # hard recode, no pt row
           _avg("W06000015", d),                       # Welsh: out of scope
           _avg("E06000001", "2021-12-01")]            # before the window
    pt = [_pt("E06000001", d, d="NA", s="0"),
          _pt("E06000003", d, d="", s="7")]
    valid = {"E06000001", "E06000002", "E06000003", "E08000016"}
    by, unresolved, no_pt = m.build_records(
        avg, pt, valid_lads=valid, code_lookup={},
        hard_recodes={"E08000038": "E08000016"})
    got = {r["lad24cd"]: r for r in by.get(d, [])}
    one, two, three = (got.get("E06000001"), got.get("E06000002"),
                       got.get("E06000003"))
    dup_avg = _raises(lambda: m.build_records(
        [_avg("E06000001", d), _avg("E06000001", d)], [], valid_lads=valid,
        code_lookup={}), ValueError)
    dup_pt = _raises(lambda: m.build_records(
        [_avg("E06000001", d)], [_pt("E06000001", d), _pt("E06000001", d)],
        valid_lads=valid, code_lookup={}), ValueError)
    ok = (bool(one) and bool(two) and bool(three)
          and one["avg_price_all"] is None and one["avg_price_all_sa"] is None
          and one["annual_change_pct"] == 0
          and one["annual_change_pct"] is not None
          and one["avg_price_detached"] is None        # 'NA'
          and one["avg_price_semi"] == 0
          and one["avg_price_semi"] is not None        # a published 0 stays 0
          and three["avg_price_detached"] is None      # blank
          and all(two[c] is None for c in PT_COLS)
          and no_pt == 2 and unresolved == {"E06999998": 1}
          and sorted(got) == ["E06000001", "E06000002", "E06000003",
                              "E08000016"]
          and dup_avg[0] and dup_pt[0])
    report(10, name, ok, f"NA/blank -> None, published 0 stays 0; area with "
           f"no property-type row -> four None, counted {no_pt}; unresolved "
           f"{unresolved}; Welsh and pre-window rows dropped; duplicate keys "
           f"refused={dup_avg[0] and dup_pt[0]}")


def gate_11_planner(cur):
    name = "window planner never lists a period before the earliest held"
    periods = [f"{y}-{mm:02d}-01" for y in (2025, 2026) for mm in range(1, 13)]
    bad, runs = [], 0
    for a_end in range(1, len(periods) + 1):
        avail = periods[:a_end]
        for h_start in range(0, len(periods), 3):
            for h_end in range(h_start, len(periods), 4):
                held = periods[h_start:h_end + 1]
                for rn in (None, 0, 3):
                    new, rc, earlier = m.plan_window(held, avail, widen=False,
                                                     recheck_n=rn)
                    runs += 1
                    first = min(held)
                    listed = set(new) | set(rc)
                    if (not listed <= set(avail) or set(new) & set(rc)
                            or new != sorted(new) or rc != sorted(rc)
                            or any(x <= first or x in held for x in new)
                            or any(x not in held for x in rc)
                            or any(x < first for x in listed)
                            or earlier != [x for x in avail if x < first]):
                        bad.append((held[:1], avail[-1:], rn))
                    nw, rw, ew = m.plan_window(held, avail, widen=True,
                                               recheck_n=rn)
                    if (ew or set(nw) != {x for x in avail if x not in held}
                            or not set(rw) <= set(held) & set(avail)):
                        bad.append(("widen", held[:1], avail[-1:], rn))
    nothing = m.plan_window([], periods, widen=False)
    ok = not bad and nothing == ([], [], [])
    report(11, name, ok, f"{runs} held/available/recheck combinations "
           f"checked (and the --min-period widening); nothing held -> "
           f"{nothing}; breaches {bad[:2]}")


def gate_12_preview_default(cur):
    name = ("preview default: the load command with no flag issues no commit "
            "and stores nothing")

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            f = write_files(_mk(tmp, "run"), "2026-07", [P1, P2, P3],
                            bump={(P2, CODES[0]): 7})
            out = {}
            for word, flags in (("PREVIEW", []), ("SIMULATION", ["--simulate"])):
                rc, text, conn, logged = run_main(cur, ["load"] + flags,
                                                  files=f)
                out[word] = (rc, text, conn.commits, conn.cur.connection.commits,
                             logged.called, editions_of(cur, P3),
                             editions_of(cur, P2), live_rows(cur, P3))
            rc, text, conn, logged = run_main(cur, ["load", "--commit"],
                                              files=f)
            out["COMMIT"] = (rc, text, conn.commits,
                             conn.cur.connection.commits, logged.called,
                             editions_of(cur, P3), editions_of(cur, P2),
                             live_rows(cur, P3))
            return out
    try:
        r = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(12, name, False, str(e).splitlines()[0])
    ok = True
    for word in ("PREVIEW", "SIMULATION"):
        rc, text, c1, c2, logged, e3, e2, l3 = r[word]
        ok = ok and (rc == 0 and c1 == 0 and c2 == 0 and not logged
                     and e3 == [] and e2 == [(1, None, N)] and l3 == 0
                     and word in text)
    rc, text, c1, c2, logged, e3, e2, l3 = r["COMMIT"]
    ok = ok and (rc == 0 and c2 == 3 and logged and e3 == [(1, None, N)]
                 and l3 == N and e2 == [(1, None, N), (2, 1, N)])
    pv, sm, cm = r["PREVIEW"], r["SIMULATION"], r["COMMIT"]
    ok = ok and ("would store edition 1 and insert" in pv[1]
                 and "stored edition 1 and inserted" in sm[1])
    report(12, name, ok, f"no flag: commits {pv[3]}, run log {pv[4]}, "
           f"editions of the new period {pv[5]}; --simulate: commits {sm[3]}, "
           f"editions {sm[5]}; control --commit: commits {cm[3]}, run log "
           f"{cm[4]}, new period {cm[5]}")


def _mk(tmp, name):
    p = Path(tmp) / name
    p.mkdir(exist_ok=True)
    return p


# ------------------------------------------------------- secret/network gate

FETCH_PAGES = {}


def secret_values():
    """Values of environment/.env entries that look like secrets (names with
    PASSWORD, KEY, TOKEN or SECRET), for the leak check only. Never printed."""
    out = {}
    for src in (ENV, os.environ):
        for k, v in src.items():
            if (re.search(r"PASSWORD|KEY|TOKEN|SECRET", k.upper())
                    and isinstance(v, str) and len(v.strip()) >= 8):
                out[k] = v.strip()
    return out


def gate_13_no_network_no_secrets(cur):
    name = "no network, and no secret in the source files or any gate output"
    here = Path(__file__).resolve().parent
    files = [Path(__file__).resolve(), here / "s15_hpi_editions.py",
             here / "period_editions.py", here / "geography.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                seed_via_load(cur, _mk(tmp, "seed"))
                f = write_files(_mk(tmp, "run"), "2026-07", [P1, P2, P3])
                rc, text, _, _ = run_main(cur, ["load", "--commit"], files=f)
                s, out, pages = _fetch_with_stub(_mk(tmp, "raw"), {})
            return rc, text, s, out, pages
    try:
        rc, text, s, out, pages = _in_savepoint(cur, body)
    except Exception as e:  # a blocked socket or any other failure
        return report(13, name, False, f"{type(e).__name__}: {e}")
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    literal = [n for n, t in texts.items() if pat.search(t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    ok = (not attempts and rc == 0 and s[2] == "2026-07"
          and all(u.startswith("https://") for u in pages) and not literal
          and not in_src and not in_out)
    report(13, name, ok, f"socket attempts {len(attempts)}; load through "
           f"files returned {rc}; fetch through a stub session requested "
           f"{len(pages)} pages; secret-like literal in source "
           f"{literal or 'none'}; {len(secrets)} secret-named settings "
           f"checked against {len(files)} files and {len(OUTPUT)} output "
           f"lines: in source {in_src or 'none'}, in output "
           f"{in_out or 'none'}")


# ---------------------------------------------------------- gates 14 and 15

def gate_14_revert_new_edition(cur):
    name = "revert stored as a new edition"
    a = recs(P3)
    b = [dict(r, avg_price_all=r["avg_price_all"] + 7)
         if r["lad24cd"] == CODES[3] else r for r in a]

    def body(cur):
        seq = [apply(cur, P3, x) for x in (a, b, a)]
        after_aba = editions_of(cur, P3)
        again = apply(cur, P3, a)
        after_abaa = editions_of(cur, P3)
        return seq, after_aba, again, after_abaa, core.chain_tip(cur, ZZ, P3)
    try:
        seq, after_aba, again, after_abaa, tip = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(14, name, False, str(e))
    want = [(1, None, N), (2, 1, N), (3, 2, N)]
    ok = (seq == ["new", "revised", "revised"] and after_aba == want
          and again == "unchanged" and after_abaa == want and tip == 3)
    report(14, name, ok, f"A,B,A -> {seq}, editions (number, supersedes) "
           f"{[(e, s) for e, s, _ in after_aba]}; A again -> {again}, "
           f"editions still {[e for e, _, _ in after_abaa]}, tip {tip}")


def gate_15_new_period_reaches_live(cur):
    name = ("new period reaches live (edition 1 and live rows in one "
            "transaction)")
    r = recs(P3, over={(0, "avg_price_all"): None,
                       (1, "avg_price_all"): Decimal("0.00")})

    def stored(cur):
        kind = apply(cur, P3, r)
        cur.execute(f"SELECT COUNT(*), COUNT(loaded_at) FROM public.{ZL} "
                    "WHERE period = %s", (P3,))
        live_n, stamped = cur.fetchone()
        return (kind, editions_of(cur, P3), live_n, stamped,
                core.rows_differing(cur, ZZ, P3, 1),
                pe.check_live_equals_edition(cur, prof(), P3, 1),
                m.status(cur, ZZ))

    def failed(cur):
        """A failure while inserting live rolls the whole period back, run
        through the engine's own per-period loop (load_periods)."""
        seen = {}
        real_insert = pe.insert_live

        def boom(c, profile, period, records):
            seen["editions_when_live_failed"] = len(editions_of(c, period))
            real_insert(c, profile, period, records[:10])
            raise RuntimeError("live insert failed (planted)")
        with mock.patch.object(pe, "insert_live", side_effect=boom), _quiet():
            rc = pe.load_periods(cur, prof(), [P3], lambda p: r, FETCHED,
                                 False, simulate=True)
        return rc, seen, editions_of(cur, P3), live_rows(cur, P3)

    def preview(cur):
        with _quiet() as buf:
            rc = pe.load_periods(cur, prof(), [P3], lambda p: r, FETCHED,
                                 False)
        return rc, buf.getvalue(), editions_of(cur, P3), live_rows(cur, P3)
    try:
        kind, eds, live_n, stamped, diff, chk, st = _in_savepoint(cur, stored)
        f_rc, f_seen, f_eds, f_live = _in_savepoint(cur, failed)
        p_rc, p_out, p_eds, p_live = _in_savepoint(cur, preview)
    except (psycopg2.Error, SystemExit) as e:
        return report(15, name, False, str(e).splitlines()[0])
    ok = (kind == "new" and eds == [(1, None, N)] and live_n == N
          and stamped == N and diff == 0 and chk == []
          and st["live_missing"] == []
          and f_rc == 1 and f_seen.get("editions_when_live_failed") == 1
          and f_eds == [] and f_live == 0
          and p_rc == 0 and p_eds == [] and p_live == 0
          and f"would store edition 1 and insert {N} live rows" in p_out)
    report(15, name, ok, f"{kind}: editions {[(e, s) for e, s, _ in eds]}, "
           f"live rows {live_n} (loaded_at set {stamped}), cells differing "
           f"{diff}, live_missing {st['live_missing']}; planted live failure:"
           f" rc {f_rc}, edition 1 written first="
           f"{f_seen.get('editions_when_live_failed') == 1}, afterwards "
           f"editions {f_eds} live {f_live}; preview writes editions {p_eds} "
           f"live {p_live}")


# ------------------------------------------------------------------- gate 16

def gate_16_file_identity(cur):
    name = ("file identity: mismatched names or a stale latest period are "
            "caught before any write")

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            cases = {
                "the two file names name different editions": write_files(
                    _mk(tmp, "c1"), "2026-07", [P1, P2, P3],
                    pt_name="Average-prices-Property-Type-2026-06.csv"),
                "the named edition is not the latest period in the file":
                    write_files(_mk(tmp, "c2"), "2026-08", [P1, P2, P3]),
                "an unrecognisable file name": write_files(
                    _mk(tmp, "c3"), "2026-07", [P3], avg_name="prices.csv"),
                "the files are given the wrong way round": (
                    write_files(_mk(tmp, "c4"), "2026-07", [P3])[::-1]),
            }
            out = {}
            for label, f in cases.items():
                with mock.patch.object(pe, "apply_period",
                                       side_effect=AssertionError("wrote")), \
                        mock.patch.object(pe, "load_periods") as lp:
                    rc, text, conn, logged = run_main(
                        cur, ["load", "--commit"], files=f)
                out[label] = (rc, text, lp.called, conn.cur.connection.commits,
                              logged.called)
            good = write_files(_mk(tmp, "ok"), "2026-07", [P1, P2, P3])
            rc, text, _, _ = run_main(cur, ["load"], files=good)
            return out, rc, editions_of(cur, P3), live_rows(cur, P3)
    try:
        out, good_rc, e3, l3 = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(16, name, False, str(e).splitlines()[0])
    unit = m.check_identity("Average-prices-2026-07.csv",
                            "Average-prices-Property-Type-2026-07.csv",
                            {P1: [], P3: []})
    unit_bad = m.check_identity("Average-prices-2026-06.csv",
                                "Average-prices-Property-Type-2026-07.csv",
                                {P3: []})
    caught = {k: v[0] == "halt" and not v[2] and v[3] == 0 and not v[4]
              for k, v in out.items()}
    ok = (all(caught.values()) and e3 == [] and l3 == 0 and good_rc == 0
          and unit == [] and len(unit_bad) >= 1
          and all("identity" in v[1] for k, v in out.items()
                  if "unrecognisable" not in k and "round" not in k))
    report(16, name, ok, "; ".join(f"{k}: {'halted before any write' if v else 'NOT CAUGHT'}"
                                   for k, v in caught.items())
           + f"; a matching pair previews (rc {good_rc}); nothing stored")


# ------------------------------------------------------------------- gate 17

def gate_17_reconciliation_order(cur):
    name = ("area-code reconciliation order (hard recode, valid, lookup, "
            "unresolved)")
    valid = {"V1", "H1", "B1"}
    lookup = {"V1": "LV", "L1": "LL", "H1": "LH"}
    hard = {"H1": "HARD"}
    kw = dict(valid_lads=valid, code_lookup=lookup, hard_recodes=hard)
    order = {c: m.reconcile(c, **kw) for c in ("H1", "V1", "L1", "U1", "B1")}
    want = {"H1": "HARD", "V1": "V1", "L1": "LL", "U1": None, "B1": "B1"}
    problems = []
    # the real reference data, read only: the loader's own code sets
    try:
        rvalid, rlookup = m.reference_codes(cur)
        if not rvalid:
            problems.append("la_boundaries is empty")
        dangling = [o for o, n in rlookup.items() if n not in rvalid]
        if dangling:
            problems.append(f"lookup targets outside la_boundaries "
                            f"{dangling[:3]}")
        rmap, gprob = geography.resolve(cur, m.RUN_SOURCE,
                                        {"E08000038", "E08000039"})
        if rmap != {"E08000038": "E08000016", "E08000039": "E08000019"}:
            problems.append(f"geography.resolve gave {rmap}")
        if gprob:
            problems.append(f"new codes flagged for S15: {gprob}")
        if rmap.get("E08000038") not in rvalid:
            problems.append("a recode target is not in la_boundaries")
    except (psycopg2.Error, ValueError, KeyError) as e:
        return report(17, name, False, f"reference data: {e}")
    # an unresolved code in a file halts the load, explained by UNEXPLAINED
    def unresolved_halts(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            f = write_files(_mk(tmp, "u"), "2026-07", [P1, P2, P3],
                            extra=[("E06999997", P3)])
            with mock.patch.object(pe, "load_periods") as lp:
                rc, text, _, _ = run_main(cur, ["load", "--commit"], files=f)
            return rc, text, lp.called, editions_of(cur, P3)
    try:
        rc, text, called, e3 = _in_savepoint(cur, unresolved_halts)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(17, name, False, str(e).splitlines()[0])
    halted = (rc == "halt" and "UNEXPLAINED E06999997" in text
              and "la_code_lookup" in text and not called and e3 == [])
    ok = order == want and not problems and halted
    report(17, name, ok, f"order {order}; "
           + ("; ".join(problems) if problems else
              f"{len(rvalid)} valid codes, {len(rlookup)} lookup codes, "
              "Barnsley and Sheffield recodes resolve to la_boundaries")
           + f"; an unresolved code halts before any write={halted}")


# ------------------------------------------------------------------- gate 18

_LR = ("https://publicdata.landregistry.gov.uk/market-trend-data/"
       "house-price-index-data")
DL_URL = ("https://www.gov.uk/government/statistical-data-sets/"
          "uk-house-price-index-data-downloads-july-2026")
REAL_COLLECTIONS = f"""<html><head>
<script type="application/ld+json">{{"sameAs": "{DL_URL}"}}</script>
</head><body>
<div class="gem-c-document-list__item-title"><a class="govuk-link" href="/government/statistical-data-sets/uk-house-price-index-data-downloads-july-2026">UK House Price Index: data downloads July 2026</a>
</div></body></html>"""
REAL_DOWNLOADS = f"""<html><body>
    <p><a rel="external" href="{_LR}/Average-prices-2026-07.csv?utm_medium=GOV.UK&amp;utm_source=datadownload&amp;utm_campaign=average_price">Average price</a> (CSV, 7.4KB)</p>
    <p><a rel="external" href="{_LR}/Average-prices-Property-Type-2026-07.csv?utm_medium=GOV.UK&amp;utm_source=datadownload&amp;utm_campaign=average_price_property_price">Average price by property type</a> (CSV, 16KB)</p>
    <p><a rel="external" href="{_LR}/Average-price-seasonally-adjusted-2026-07.csv?utm_medium=GOV.UK">Average price seasonally adjusted</a></p>
</body></html>"""


class _Resp:
    def __init__(self, body, status=200):
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.text = self.content.decode("utf-8")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return _Resp(body)
        return _Resp(b"not found", 404)


def csv_body(head, n=60):
    rows = [head] + [f"2026-07-01,Area,E0600{i:04d},{200000 + i},0,1.0,1"
                     for i in range(n)]
    return ("\n".join(rows) + "\n").encode("utf-8")


def _stub_pages(**over):
    pages = {m.COLLECTIONS_URL: REAL_COLLECTIONS, DL_URL: REAL_DOWNLOADS,
             f"{_LR}/Average-prices-2026-07.csv": csv_body(AVG_HEAD),
             f"{_LR}/Average-prices-Property-Type-2026-07.csv":
                 csv_body(PT_HEAD)}
    pages.update(over)
    return pages


def _fetch_with_stub(dest, over):
    """(fetch_latest_files result or ('halt', text), printed text, urls
    requested) through a stub session, writing under dest."""
    session = _Session(_stub_pages(**over))
    with _quiet() as buf:
        try:
            got = m.fetch_latest_files(session=session, dest=Path(dest))
        except SystemExit as e:
            got = ("halt", str(e.code))
    if got[0] == "halt":
        return got + (None,), buf.getvalue(), session.calls
    return (got[0].name, got[1].name, got[2]), buf.getvalue(), session.calls


def gate_18_page_parsers(cur):
    name = ("the page parsers work on the sample pages and halt on a changed "
            "layout")
    a, p, ed = m.find_csv_links(REAL_DOWNLOADS)
    ok = (m.find_download_page(REAL_COLLECTIONS) == DL_URL and ed == "2026-07"
          and "&amp;" not in a and "&amp;" not in p
          and a.startswith(f"{_LR}/Average-prices-2026-07.csv?")
          and "/Average-prices-Property-Type-2026-07.csv?" in p
          and "Property-Type" not in a)
    notes = [f"sample pages parse (edition {ed})"]
    cases = {
        "collections page without the downloads link": (
            {m.COLLECTIONS_URL: "<html><body>New layout: see Reports</body></html>"},
            "New layout: see Reports"),
        "downloads page without the property-type link": (
            {DL_URL: REAL_DOWNLOADS.replace(
                "Average-prices-Property-Type-2026-07.csv", "PT.csv")},
            "Average price"),
        "an average-prices file that is too short": (
            {f"{_LR}/Average-prices-2026-07.csv": b"Date,Area_Code\n"},
            "bytes"),
        "a property-type file with another header": (
            {f"{_LR}/Average-prices-Property-Type-2026-07.csv":
                 csv_body("Period,Code,Price")}, "Period,Code"),
        "the downloads page names another month than its files": (
            {DL_URL: REAL_DOWNLOADS.replace("2026-07", "2026-06")},
            "july-2026"),
    }
    with tempfile.TemporaryDirectory() as tmp:
        good, _, calls = _fetch_with_stub(_mk(tmp, "good"), {})
        ok = (ok and good[0] == "Average-prices-2026-07.csv"
              and good[2] == "2026-07"
              and (_mk(tmp, "good") / good[0]).read_bytes()
              == csv_body(AVG_HEAD)
              and calls[:2] == [m.COLLECTIONS_URL, DL_URL])
        for label, (over, words) in cases.items():
            dest = _mk(tmp, "bad" + str(len(notes)))
            got, text, _ = _fetch_with_stub(dest, over)
            halted = got[0] == "halt" and words in got[1]
            wrote = list(dest.glob("*.csv"))
            ok = ok and halted and not wrote
            notes.append(f"{label}: " + ("halts, nothing written" if halted
                                         and not wrote else "NOT CAUGHT"))
    for page in ("<html>nothing to see</html>",):
        ok = ok and _raises(lambda: m.find_download_page(page), ValueError)[0]
    for page in ('<a href="https://h/Average-prices-2026-06.csv">x</a>',
                 "<html></html>"):
        ok = ok and _raises(lambda: m.find_csv_links(page), ValueError)[0]
    report(18, name, ok, "; ".join(notes))


# ------------------------------------------------------------------- gate 19

def gate_19_as_loaded_latest(cur):
    name = ("sync-new records edition 1 for a period with several load dates, "
            "taking the latest")
    d_old, d_new = "2026-09-01 10:00:00+00", "2026-10-02 12:00:00+00"
    multi, single = recs(P1), recs(P2)

    def loaded(r):
        if r["period"] == P1:
            return d_new if r["lad24cd"] == CODES[0] else d_old
        return d_old

    def body(cur):
        seed_live(cur, multi + single, loaded_at=loaded)
        # the control: one load date per period is required by 'single'
        single_spec = dataclasses.replace(ZZ, as_loaded_date="single")
        ctrl = _in_savepoint(cur, lambda c: _halts(
            lambda: core.sync_new(c, single_spec, N)))
        args = SimpleNamespace(expected_authorities=N, commit=False,
                               simulate=True)
        borrowed = _Borrowed(cur)
        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(pe, "log_run") as logged, \
                contextlib.redirect_stdout(out):
            assert m.SPEC is ZZ
            rc = m.cmd_sync_new(args)
        OUTPUT.extend(out.getvalue().splitlines())
        cur.execute(f"""SELECT period, edition, supersedes, COUNT(*),
                               MIN(published_date), MAX(published_date),
                               MIN(release_label)
                        FROM public.{ZE} GROUP BY 1, 2, 3 ORDER BY 1""")
        rows = cur.fetchall()
        return (ctrl, rc, rows, logged.called,
                load_checks.check_latest_equals_live(cur, ZZ))
    try:
        ctrl, rc, rows, logged, equal = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, AssertionError) as e:
        return report(19, name, False, str(e).splitlines()[0])
    want = [(date.fromisoformat(P1), 1, None, N, date(2026, 10, 2),
             date(2026, 10, 2), core.AS_LOADED_LATEST_LABEL),
            (date.fromisoformat(P2), 1, None, N, date(2026, 9, 1),
             date(2026, 9, 1), core.AS_LOADED_LATEST_LABEL)]
    ok = (rc == 0 and rows == want and logged and equal == []
          and ctrl[0] and "loaded_at" in ctrl[1])
    report(19, name, ok, f"sync-new rc {rc}; {P1} (rows loaded on two days) "
           f"-> edition 1 published_date {rows[0][4] if rows else None}, "
           f"{P2} (one day) -> {rows[1][4] if len(rows) > 1 else None}; "
           f"latest equals live={equal == []}; the 'single' control halts="
           f"{ctrl[0]}")


# ------------------------------------------------------------------- gate 20

def gate_20_barnsley_sheffield(cur):
    name = ("Barnsley and Sheffield through geography.resolve: a form that "
            "disagrees with S15's declaration halts before any write")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("none",))[0]

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            f = write_files(_mk(tmp, "old"), "2026-07", [P1, P2, P3],
                            extra=[("E08000016", P3)])
            with mock.patch.object(pe, "load_periods") as lp, \
                    mock.patch.object(pe, "apply_period",
                                      side_effect=AssertionError("wrote")):
                rc, text, conn, logged = run_main(cur, ["load", "--commit"],
                                                  files=f)
            halt = (rc, text, lp.called, conn.cur.connection.commits,
                    editions_of(cur, P3), live_rows(cur, P3))
            # the declared form resolves to the canonical codes
            g = write_files(_mk(tmp, "new"), "2026-07", [P3],
                            extra=[("E08000038", P3)])
            stub = (set(CODES) | {"E08000016"}, {})
            with _quiet(), mock.patch.object(m, "reference_codes",
                                             return_value=stub), \
                    mock.patch.object(m.geography, "resolve",
                                      wraps=m.geography.resolve) as rs:
                by = m.load_file_records(g[0], g[1], _Borrowed(cur))[0]
            return halt, rs.call_args, [r["lad24cd"] for r in by[P3]]
    try:
        halt, call, lads = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, ValueError) as e:
        return report(20, name, False, str(e).splitlines()[0])
    rc, text, called, commits, eds, lv = halt
    ok = (decl == "new" and rc == "halt"
          and all(x in text for x in ("declared 'new'", "E08000016",
                                      "geography.py"))
          and not called and commits == 0 and eds == [] and lv == 0
          and call.args[1] == "15" and "E08000038" in call.args[2]
          and "E08000016" in lads and "E08000038" not in lads)
    report(20, name, ok, f"S15 declared {decl!r}; a file carrying "
           f"E08000016 halted={rc == 'halt'} with commits {commits}, "
           f"editions {eds}, live rows {lv}; a file carrying E08000038 is "
           "keyed to E08000016 via resolve(cur, '15', ...)="
           f"{'E08000016' in lads}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1,
         gate_3_latest_equals_live, gate_4_coverage, gate_5_nonpositive,
         gate_6_null_vs_zero, gate_7_suppressed_listing,
         gate_8_seeded_revision, gate_9_fork_and_second_root,
         gate_10_build_records_rules, gate_11_planner,
         gate_12_preview_default, gate_13_no_network_no_secrets,
         gate_14_revert_new_edition, gate_15_new_period_reaches_live,
         gate_16_file_identity, gate_17_reconciliation_order,
         gate_18_page_parsers, gate_19_as_loaded_latest,
         gate_20_barnsley_sheffield)


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            setup_throwaway(cur)
            for gate in GATES:
                gate(cur)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
