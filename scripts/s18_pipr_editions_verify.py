"""Gates for the S18 (ONS Price Index of Private Rents) edition-history table.

Mirrors scripts/s15_hpi_editions_verify.py. Prints `GATE n name: PASS|FAIL`;
exits 1 on any FAIL. Gates 2 to 7 read the real tables (la_private_rents and
la_private_rents_editions). Until the editions table exists they print
`FAIL (pending load)` with the words "not yet migrated", so the script exits
non-zero until then, like the S1, S1b, S8b, S15 and S19 verifiers; nothing is
created to make them pass. The seeded gates never need the real editions
table: they run on throwaway tables zz_s18_live and zz_s18_editions (a copy
of the loader's SPEC named zz_s18), created inside the transaction, and call
the loader's own writers and commands (s18_pipr_editions.apply_period and
insert_live, pe.load_periods, core.refresh_latest, and main/cmd_load with SPEC
pointed at the copy, load_file_records) rather than a re-implementation. The
real tables are only ever read.

Gates:
  1  table shape and immutability (UPDATE/DELETE/TRUNCATE blocked), on the
     throwaway copy; also the shape of the real table once it exists
  2  edition 1 present for every live period                      (real)
  3  latest edition equals live for every period                  (real)
  4  no area and no breakdown block dropped: every edition of every period
     holds the held area count x nine blocks                      (real)
  5  no zero or negative rent where present                      (real)
  6  NULL versus 0 never conflated, on each of the three numeric columns
     (seeded; plus real data)                                     (real)
  7  provisional is compared: a provisional-to-final flip is a revision,
     stored, and reaches live through refresh-latest (seeded); real data has
     no provisional month but the latest                          (real)
  8  seeded revision: refresh_latest updates only that month's changed rows
     and never touches live source or loaded_at
  9  seeded fork and second root refused (chain_tip, and the loader's compare)
  10 parse and record rules: half-up rounding, markers and blanks are NULL, a
     published 0 stays 0, provisional only on the file's latest month,
     out-of-window and non-English rows dropped, a duplicate key refused
  11 window planner never lists a month before the earliest held
  12 preview default: the load command with no flag issues no commit
  13 no network (a socket attempt fails the gate), no secret in source or output
  14 revert stored as a new edition (A, B, A -> edition 3; A again stores nothing)
  15 new month reaches live in one transaction, live source names the edition
     (a planted live-insert failure leaves neither; preview writes nothing)
  16 file identity: file name, Cover sheet and latest month must agree, caught
     before any write
  17 older-file guard: an older edition or an earlier latest month halts
     unless --allow-older-file; the live source column counts as held
  18 per-month checks: a short month, a missing area, a missing block, a zero
     rent, NULL replacing a number in more than 5 areas, a revision above 50%
     in more than 10 areas are rejected (limits themselves pass)
  19 Barnsley and Sheffield through geography.resolve: a form that disagrees
     with S18's declaration ('new') halts before any write
  20 rerun idempotent: the same file twice stores nothing and leaves live
     untouched
  21 stranded month repair: a month with editions and no live rows gets its
     live rows from load, with no new edition
  22 sync-new records edition 1 from live, taking the latest load date
  23 an unresolved area code halts the load before any write

Usage:
    python scripts/s18_pipr_editions_verify.py

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
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
from _db import ENV, get_conn  # noqa: E402
import editions_core as core  # noqa: E402
import geography  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
import s18_pipr_editions as m  # noqa: E402
from test_s18_pipr_pure import write_workbook  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC, LIVE, TABLE = m.SPEC, m.LIVE, m.TABLE
ZZ = dataclasses.replace(
    SPEC, name="zz_s18", live_table="zz_s18_live",
    editions_table="zz_s18_editions", fk_la_boundaries=False)
ZL, ZE = ZZ.live_table, ZZ.editions_table
FETCHED = date(2026, 10, 8)
N = 12                                   # areas in the seeded data
CODES = [f"E060000{i:02d}" for i in range(1, N + 1)]
SPARE = "E06000099"
P1, P2, P3, P4 = "2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01"
NB = len(m.BLOCKS)                       # nine breakdown blocks
ROWS = N * NB
COLS = m.VALUE_COLUMNS
NUM_COLS = m.NUMERIC_COLUMNS
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

def recs(period, n=N, over=None, prov=False, codes=CODES):
    """The month's records (n areas x nine blocks). over {(area index,
    column): value} (None allowed) replaces cells of the all/all block."""
    over = over or {}
    month = int(period[5:7])
    out = []
    for i in range(n):
        for b, (_, bt, cat) in enumerate(m.BLOCKS):
            r = {"lad24cd": codes[i], "period": period, "breakdown_type": bt,
                 "category": cat,
                 "mean_rent": Decimal(1000 + 100 * month + 10 * i + b),
                 "rent_index": Decimal(100 + i + b),
                 "annual_pct_change": Decimal("1.50") + b,
                 "provisional": prov}
            if b == 0:
                for c in COLS:
                    if (i, c) in over:
                        r[c] = over[(i, c)]
            out.append(r)
    return out


def setup_throwaway(cur):
    cur.execute("SELECT to_regclass('public.zz_s18_live'), "
                "to_regclass('public.zz_s18_editions')")
    if cur.fetchone() != (None, None):
        sys.exit("HARD STOP: zz_s18_live or zz_s18_editions already exists as "
                 "a real table; refusing to run")
    cur.execute(f"""CREATE TABLE public.{ZL} (
        lad24cd varchar(9) NOT NULL, period date NOT NULL,
        breakdown_type varchar(20) NOT NULL, category varchar(30) NOT NULL,
        mean_rent numeric(8,2), rent_index numeric(8,2),
        annual_pct_change numeric(6,2), provisional boolean DEFAULT false,
        source text, loaded_at timestamptz DEFAULT now(),
        PRIMARY KEY (lad24cd, period, breakdown_type, category))""")
    core.create_schema(cur, ZZ)


def seed_live(cur, records, source="seed", loaded_at=None):
    cols = ("lad24cd", "period", "breakdown_type", "category") + COLS
    for r in records:
        cur.execute(
            f"INSERT INTO public.{ZL} ({', '.join(cols)}, source"
            + (", loaded_at" if loaded_at else "") + ") VALUES ("
            + ", ".join(["%s"] * (len(cols) + 1)) + (", %s" if loaded_at else "")
            + ")", [r[c] for c in cols] + [source]
            + ([loaded_at(r)] if loaded_at else []))


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


def live_cell(cur, period, col, area, bt="all", cat="all"):
    cur.execute(f"SELECT {col} FROM public.{ZL} WHERE period = %s AND "
                "lad24cd = %s AND breakdown_type = %s AND category = %s",
                (period, area, bt, cat))
    return cur.fetchone()[0]


def raw_edition(cur, period, edition, supersedes, sha):
    """One throwaway-table row planted directly (the loader's writer refuses a
    fork or a second root, so a fault has to be planted by hand)."""
    cur.execute(f"""INSERT INTO public.{ZE} (lad24cd, period, breakdown_type,
                    category, edition, mean_rent, supersedes, source_sha256)
                    VALUES (%s, %s, 'all', 'all', %s, 1, %s, %s)""",
                (CODES[0], period, edition, supersedes, sha))


def prof(edition="22july2026", n=N, required=()):
    """The loader's own per-run profile on the throwaway spec."""
    return m.run_profile(ZZ, edition, n, required)


def apply(cur, period, records, edition="22july2026"):
    """The loader's own writer for one month: s18_pipr_editions.apply_period
    (edition rows, then the live rows through the loader's insert_live)."""
    return m.apply_period(cur, prof(edition), period, records,
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


def cover(edition):
    d = m.edition_date(edition)
    return ("The data tables in this spreadsheet were originally published "
            f"at 9:30am on {d.day} {d.strftime('%B')} {d.year}. Crown "
            "copyright.")


def file_rent(period, i, bump=None):
    base = 1000 + 100 * int(period[5:7]) + 10 * i
    return base + (bump or {}).get((period, CODES[i] if i < N else SPARE), 0)


def write_edition(d, edition, periods, *, codes=None, bump=None, extra=(),
                  overrides=None, drop=(), name=None, text=None):
    """pipr_<edition>.xlsx in directory d. codes {period: [codes]} (default
    CODES); bump {(period, code): +x} on every block's rent; extra
    [(period, code)] rows added; overrides {(period, code): {column: value}}.
    A Welsh row and a pre-window row are always there (both out of scope)."""
    codes = codes or {}
    rows = [(datetime(2021, 12, 1), CODES[0]),
            (datetime.fromisoformat(periods[-1]), "W06000015")]
    for p in periods:
        for c in codes.get(p, CODES):
            i = CODES.index(c) if c in CODES else N
            kw = {"rent": file_rent(p, i, bump), "index": 100.0 + i,
                  "change": 1.5}
            if overrides and (p, c) in overrides:
                kw["overrides"] = overrides[(p, c)]
            rows.append((datetime.fromisoformat(p), c, kw))
    for p, c in extra:
        rows.append((datetime.fromisoformat(p), c))
    path = Path(d) / (name or f"pipr_{edition}.xlsx")
    write_workbook(path, rows, drop=drop,
                   cover_text=text if text is not None else cover(edition))
    return path


def run_main(cur, argv, *, file=None, table=True, fetch=None):
    """The loader's main(argv) on the throwaway tables, with SPEC pointed at
    the copy, a stand-in connection, stubbed reference codes (the throwaway
    areas are not in la_boundaries) and a stubbed run log. file is passed as
    --file. Returns (rc or 'halt', text, connection stand-in, run-log mock)."""
    borrowed = _Borrowed(cur)
    args = list(argv)
    if file:
        args += ["--file", str(file)]
    out = io.StringIO()
    known = set(CODES) | {SPARE, "E08000016", "E08000019"}
    with mock.patch.object(m, "SPEC", ZZ), \
            mock.patch.object(m, "_conn", return_value=borrowed), \
            mock.patch.object(m, "reference_codes",
                              return_value=(known, {})), \
            mock.patch.object(m, "fetch_latest_file",
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


def _mk(tmp, name):
    p = Path(tmp) / name
    p.mkdir(exist_ok=True)
    return p


_COUNTER = [0]


def fresh_dir(tmp):
    _COUNTER[0] += 1
    return _mk(tmp, f"d{_COUNTER[0]}")


def seed_via_load(cur, tmp, periods=(P1, P2, P3), edition="22july2026"):
    """First run through the loader itself: nothing held, --min-period and
    --expected-areas (the stand-in connection never commits)."""
    f = write_edition(fresh_dir(tmp), edition, list(periods))
    rc, text, _, _ = run_main(
        cur, ["load", "--commit", "--min-period", periods[0],
              "--expected-areas", str(N)], file=f)
    if rc != 0:
        raise RuntimeError(f"seed load failed: {text[-300:]}")
    return f


def scenario(cur, tmp, fn):
    """Seed through the loader, run fn(cur) and roll all of it back."""
    def body(c):
        seed_via_load(c, tmp)
        return fn(c)
    return _in_savepoint(cur, body)


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
    if cols.get("breakdown_type") != ("character varying", 20, None, None):
        bad.append(f"breakdown_type is {cols.get('breakdown_type')}")
    if cols.get("category") != ("character varying", 30, None, None):
        bad.append(f"category is {cols.get('category')}")
    if cols.get(spec.period_col, ("",))[0] != "date":
        bad.append(f"{spec.period_col} is {cols.get(spec.period_col)}")
    for c, sql in spec.value_cols:
        mt = re.fullmatch(r"numeric\((\d+),(\d+)\)", sql)
        want = (("numeric", None, int(mt.group(1)), int(mt.group(2)))
                if mt else (sql,))
        got = cols.get(c)
        if mt and got != want or (not mt and (got or ("",))[0] != sql):
            bad.append(f"{c} is {got}, expected {sql}")
    for c in ("edition", "supersedes"):
        if cols.get(c, ("",))[0] != "integer":
            bad.append(f"{c} is {cols.get(c)}")
    cur.execute("""SELECT a.attname FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid
                    AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = %s::regclass AND i.indisprimary""",
                (f"public.{t}",))
    if {r[0] for r in cur.fetchall()} != {
            "lad24cd", "period", "breakdown_type", "category", "edition"}:
        bad.append("primary key is not (lad24cd, period, breakdown_type, "
                   "category, edition)")
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
    """{UPDATE, DELETE, TRUNCATE: error text or None} on a table holding rows;
    all in savepoints."""
    t = spec.editions_table
    stmts = {"UPDATE": f"UPDATE public.{t} SET mean_rent = 2",
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
    """Every edition of every month holds the held area count x nine blocks
    with distinct keys (load_checks.check_coverage) and every area holds all
    nine blocks; the chain tip has that many distinct areas."""
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
            bad += load_checks.check_coverage(cur, spec, p, ed, want_n * NB)
            cur.execute(f"""SELECT COUNT(*) FROM (
                SELECT lad24cd FROM public.{spec.editions_table}
                WHERE {spec.period_col} = %s AND edition = %s
                GROUP BY lad24cd
                HAVING COUNT(DISTINCT (breakdown_type, category)) <> %s) q""",
                        (p, ed, NB))
            short = cur.fetchone()[0]
            if short:
                bad.append(f"{p} ed{ed}: {short} area(s) lack a breakdown "
                           "block")
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
        f"{len(periods)} months, {n_ed} editions, each {want_n} areas x "
        f"{NB} blocks = {want_n * NB} rows (held count)")


def real_no_nonpositive(cur, spec=SPEC):
    """No zero or negative rent where one is present (NULL is a suppressed
    rent, not a bad one)."""
    counts = {}
    for label, table in (("editions", spec.editions_table),
                         ("live", spec.live_table)):
        cur.execute(f"SELECT COUNT(*) FROM public.{table} WHERE mean_rent <= 0")
        counts[label] = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table}")
    if not cur.fetchone()[0]:
        return False, "no live rows to check (an empty state is not a pass)"
    return not any(counts.values()), (
        f"zero or negative rent cells: editions {counts['editions']}, live "
        f"{counts['live']}")


def null_zero_counts(cur, table, where="", params=()):
    """(NULL count, zero count) per numeric column plus the NULL count of
    provisional, as a flat tuple."""
    parts = []
    for c in NUM_COLS:
        parts += [f"COUNT(*) FILTER (WHERE {c} IS NULL)",
                  f"COUNT(*) FILTER (WHERE {c} = 0)"]
    parts.append("COUNT(*) FILTER (WHERE provisional IS NULL)")
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
        "NULL and 0 counts per month and column identical in live and the "
        "latest edition; EXCEPT comparison clean")


def real_provisional(cur, spec=SPEC):
    """In each month's latest edition provisional is never NULL and is true
    only for the latest month (the workbook marks no other)."""
    tips, new, errors = core.latest_map(cur, spec)
    if not tips:
        return False, "no months to check (an empty state is not a pass)"
    if errors or new:
        return False, f"cannot check: new={new} errors={errors}"
    last = max(tips)
    bad, n_prov = [], 0
    for p, ed in tips.items():
        cur.execute(f"""SELECT COUNT(*) FILTER (WHERE provisional),
                               COUNT(*) FILTER (WHERE provisional IS NULL)
                        FROM public.{spec.editions_table}
                        WHERE {spec.period_col} = %s AND edition = %s""",
                    (p, ed))
        t, nul = cur.fetchone()
        n_prov += 1 if t else 0
        if nul:
            bad.append(f"{p} ed{ed}: {nul} NULL provisional")
        if t and p != last:
            bad.append(f"{p} ed{ed}: {t} provisional rows in a month that is "
                       "not the latest")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{n_prov} month(s) provisional in the latest editions (latest month "
        f"{_s(last)}); none NULL")


def gate_2_edition1(cur):
    real_gate(cur, 2, "edition 1 present for every live period",
              real_edition1_present)


def gate_3_latest_equals_live(cur):
    real_gate(cur, 3, "latest edition equals live for every period",
              real_latest_equals_live)


def gate_4_coverage(cur):
    real_gate(cur, 4, "no area or breakdown block dropped: every edition has "
              "the held area count x nine blocks", real_coverage)


def gate_5_nonpositive(cur):
    name = "no zero or negative rent where present"
    bad = [r for r in recs(P1) if r["mean_rent"] is not None]
    for r in bad:
        r["mean_rent"] = Decimal("0.00")
    try:
        m.check_period(bad, P1, N, ())
        loud = False
    except ValueError as e:
        loud = "zero or negative rent" in str(e)
    if not loud:
        return report(5, name, False, "check_period accepts a zero rent")
    real_gate(cur, 5, name, real_no_nonpositive)


# (label, column, area index, new value): the base month holds NULL at area
# 2j and 0 at area 2j+1 for the j-th numeric column (all/all block)
def _variants():
    out = []
    for j, c in enumerate(NUM_COLS):
        out.append((f"{c} NULL->0", c, 2 * j, Decimal("0.00")))
        out.append((f"{c} 0->NULL", c, 2 * j + 1, None))
    return tuple(out)


VARIANTS = _variants()


def null_zero_base():
    over = {}
    for j, c in enumerate(NUM_COLS):
        over[(2 * j, c)] = None
        over[(2 * j + 1, c)] = Decimal("0.00")
    return over


def _with(records, area, col, new):
    return [dict(r, **{col: new})
            if (r["lad24cd"], r["breakdown_type"], r["category"])
            == (CODES[area], "all", "all") else r for r in records]


def seeded_null_zero(cur):
    """The scenario of gate 6, on throwaway tables, through the loader's
    writer: each numeric column changed from NULL to 0 and from 0 to NULL,
    one cell at a time."""
    base = recs(P1, over=null_zero_base())

    def body(cur):
        first = apply(cur, P1, base)
        clean = load_checks.check_latest_equals_live(cur, ZZ)
        out = {"first": first, "clean": clean}
        for label, col, area, new in VARIANTS:
            def variant(cur, col=col, area=area, new=new):
                changed = _with(base, area, col, new)
                kind = pe.classify_period(cur, prof(), P1, changed)
                apply(cur, P1, changed)
                flagged = load_checks.check_latest_equals_live(cur, ZZ)
                stored = editions_of(cur, P1)
                cur.execute(f"""SELECT {col} IS NULL, {col}
                    FROM public.{ZE}
                    WHERE period = %s AND edition = 2 AND lad24cd = %s
                      AND breakdown_type = 'all' AND category = 'all'""",
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
    name = "NULL versus 0 never conflated (all three numeric columns)"
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
    notes = (f"{len(VARIANTS)} single-cell changes over {len(NUM_COLS)} "
             "columns each stored as edition 2 with the cell read back "
             "exactly")
    if not ok:
        return report(6, name, False, "seeded scenario: " + "; ".join(bad[:4]
                      or ["first load or clean state wrong"]))
    if not table_exists(cur):
        return report(6, name, False, f"{PENDING}; seeded scenario passes: "
                      + notes, pending=True)
    real_gate(cur, 6, name, lambda c: (lambda r: (r[0], f"{r[1]}; seeded "
              f"scenario: {notes}"))(real_null_vs_zero(c)))


def seeded_provisional(cur):
    """A month first loaded as the file's latest (provisional) and then
    published final, numbers unchanged: a revision, stored as edition 2, and
    live changes only through refresh_latest."""
    def body(cur):
        apply(cur, P1, recs(P1, prov=True))
        final = recs(P1, prov=False)
        kind = pe.classify_period(cur, prof(), P1, final)
        apply(cur, P1, final)
        eds = editions_of(cur, P1)
        flagged = load_checks.check_latest_equals_live(cur, ZZ)
        cur.execute(f"SELECT DISTINCT provisional FROM public.{ZL} "
                    "WHERE period = %s", (P1,))
        before = cur.fetchall()
        plan = {_s(k): v for k, v in core.refresh_counts(cur, ZZ).items()}
        core.refresh_latest(cur, ZZ)
        cur.execute(f"SELECT DISTINCT provisional FROM public.{ZL} "
                    "WHERE period = %s", (P1,))
        after = cur.fetchall()
        again = pe.classify_period(cur, prof(), P1, final)
        return (kind, eds, flagged, before, plan, after, again,
                load_checks.check_latest_equals_live(cur, ZZ))
    return _in_savepoint(cur, body)


def provisional_ok(e):
    kind, eds, flagged, before, plan, after, again, clean = e
    return (kind == "revised" and [x[0] for x in eds] == [1, 2]
            and len(flagged) == 1 and before == [(True,)]
            and plan == {P1: ROWS} and after == [(False,)]
            and again == "unchanged" and clean == [])


def gate_7_provisional_compared(cur):
    name = ("provisional is a compared value: provisional-to-final is a "
            "revision")
    try:
        e = seeded_provisional(cur)
    except (psycopg2.Error, SystemExit, ValueError) as ex:
        return report(7, name, False, f"seeded scenario: {ex}")
    if not provisional_ok(e):
        return report(7, name, False, f"seeded scenario wrong: {e}")
    notes = (f"flip stored as edition 2 ({e[0]}), live still provisional until "
             f"refresh-latest ({e[4]}), then final")
    if not table_exists(cur):
        return report(7, name, False, f"{PENDING}; seeded scenario passes: "
                      + notes, pending=True)
    real_gate(cur, 7, name, lambda c: (lambda r: (r[0], f"{r[1]}; seeded "
              f"scenario: {notes}"))(real_provisional(c)))


# ------------------------------------------------------------ seeded gates

def gate_8_seeded_revision(cur):
    name = ("seeded revision: edition 2 with one changed cell, refresh_latest "
            "updates only that month's changed row, live source and loaded_at "
            "kept")

    def snapshot(cur, period):
        cur.execute(f"""SELECT lad24cd, breakdown_type, category, mean_rent,
                               rent_index, annual_pct_change, provisional,
                               source, loaded_at
                        FROM public.{ZL} WHERE period = %s ORDER BY 1, 2, 3""",
                    (period,))
        return cur.fetchall()

    def body(cur):
        ra, rb = recs(P1), recs(P2)
        for p, rr in ((P1, ra), (P2, rb)):
            apply(cur, p, rr)
        before_ok = load_checks.check_latest_equals_live(cur, ZZ) == []
        changed = _with(ra, 5, "mean_rent",
                        ra[5 * NB]["mean_rent"] + 1000)
        kind = apply(cur, P1, changed)
        p1_b, p2_b = snapshot(cur, P1), snapshot(cur, P2)
        plan = {_s(k): v for k, v in core.refresh_counts(cur, ZZ).items()}
        res = core.refresh_latest(cur, ZZ)
        p1_a, p2_a = snapshot(cur, P1), snapshot(cur, P2)
        diff = [(x, y) for x, y in zip(p1_b, p1_a) if x != y]
        return (before_ok, kind, plan, res, p2_b == p2_a, diff,
                editions_of(cur, P2),
                load_checks.check_latest_equals_live(cur, ZZ))
    try:
        before_ok, kind, plan, res, p2_same, diff, b_eds, after = (
            _in_savepoint(cur, body))
    except (psycopg2.Error, SystemExit) as e:
        return report(8, name, False, str(e).splitlines()[0])
    updated = {_s(k): v for k, v in res["updated"].items()}
    one = len(diff) == 1
    only_rent = one and all(
        x[i] == y[i] for x, y in diff for i in (0, 1, 2, 4, 5, 6, 7, 8))
    ok = (before_ok and kind == "revised" and plan == {P1: 1}
          and updated == {P1: 1} and res["rows"] == 1 and p2_same and one
          and only_rent and diff[0][0][3] != diff[0][1][3]
          and [e[0] for e in b_eds] == [1] and after == [])
    report(8, name, ok, f"edition 2 stored ({kind}); plan {plan}; updated "
           f"{updated} ({res['rows']} row); {P2} untouched={p2_same}; live "
           f"cells changed in {P1}: {len(diff)}, only mean_rent (source and "
           f"loaded_at kept)={only_rent}; latest equals live after="
           f"{after == []}")


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


def gate_10_parse_and_record_rules(cur):
    name = ("parse and record rules: half-up rounding, markers NULL, a "
            "published 0 stays 0, provisional on the latest month only")
    D = Decimal
    ov = {"Rental price": 5.845, "Annual change": -1.505, "Index": 0,
          "Rental price one bed": "[x]", "Index one bed": None,
          "Annual change one bed": "[z]", "Rental price two bed": 1234.565}
    rows = [(datetime(2021, 12, 1), "E06000001"),             # pre-window
            (datetime(2026, 4, 1), "E06000001", {"overrides": ov}),
            (datetime(2026, 4, 1), "E08000038"),
            (datetime(2026, 4, 1), "W06000015"),               # Wales
            (datetime(2026, 5, 1), "E06000001"),
            (datetime(2026, 5, 1), "E08000038")]
    with tempfile.TemporaryDirectory() as tmp:
        path = write_workbook(Path(tmp) / "t.xlsx", rows)
        got = m.parse_workbook(path)
        recoded = m.build_records(got, {"E08000038": "E08000016"},
                                  min_period=date(2026, 4, 1))
        dup = _raises(lambda: m.build_records(
            got, {"E08000038": "E06000001"}, min_period=date(2026, 4, 1)),
            ValueError)
    by = {(r["lad24cd"], r["period"], r["breakdown_type"], r["category"]): r
          for r in recoded}
    a = by[("E06000001", "2026-04-01", "all", "all")]
    one = by[("E06000001", "2026-04-01", "bedroom", "1_bed")]
    two = by[("E06000001", "2026-04-01", "bedroom", "2_bed")]
    ok = (a["mean_rent"] == D("5.85") and a["annual_pct_change"] == D("-1.51")
          and a["rent_index"] == D("0.00") and a["rent_index"] is not None
          and one["mean_rent"] is None and one["rent_index"] is None
          and one["annual_pct_change"] is None
          and two["mean_rent"] == D("1234.57")
          and all(not r["lad24cd"].startswith("W") for r in recoded)
          and all(r["period"] >= "2026-04-01" for r in recoded)
          and {r["lad24cd"] for r in recoded} == {"E06000001", "E08000016"}
          and len(recoded) == 2 * 2 * NB
          and all(r["provisional"] == (r["period"] == "2026-05-01")
                  for r in recoded)
          and dup[0] and "duplicate" in dup[1])
    report(10, name, ok, "5.845 -> 5.85, -1.505 -> -1.51, 1234.565 -> "
           "1234.57; [x]/[z]/blank -> NULL; published 0 index stays 0; "
           "Welsh and pre-window rows dropped; E08000038 recoded; only the "
           f"latest month provisional; duplicate key refused={dup[0]}")


def gate_11_planner(cur):
    name = "window planner never lists a month before the earliest held"
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
            f = write_edition(fresh_dir(tmp), "19august2026", [P1, P2, P3, P4],
                              bump={(P2, CODES[0]): 7})
            out = {}
            for word, flags in (("PREVIEW", []), ("SIMULATION", ["--simulate"])):
                rc, text, conn, logged = run_main(cur, ["load"] + flags,
                                                  file=f)
                out[word] = (rc, text, conn.commits, conn.cur.connection.commits,
                             logged.called, editions_of(cur, P4),
                             editions_of(cur, P2), live_rows(cur, P4))
            rc, text, conn, logged = run_main(cur, ["load", "--commit"],
                                              file=f)
            out["COMMIT"] = (rc, text, conn.commits,
                             conn.cur.connection.commits, logged.called,
                             editions_of(cur, P4), editions_of(cur, P2),
                             live_rows(cur, P4))
            return out
    try:
        r = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(12, name, False, str(e).splitlines()[0])
    ok = True
    for word in ("PREVIEW", "SIMULATION"):
        rc, text, c1, c2, logged, e4, e2, l4 = r[word]
        ok = ok and (rc == 0 and c1 == 0 and c2 == 0 and not logged
                     and e4 == [] and e2 == [(1, None, ROWS)] and l4 == 0
                     and word in text)
    rc, text, c1, c2, logged, e4, e2, l4 = r["COMMIT"]
    ok = ok and (rc == 0 and c2 >= 1 and logged and e4 == [(1, None, ROWS)]
                 and l4 == ROWS and e2 == [(1, None, ROWS), (2, 1, ROWS)])
    pv, cm = r["PREVIEW"], r["COMMIT"]
    report(12, name, ok, f"no flag: commits {pv[3]}, run log {pv[4]}, "
           f"editions of the new month {pv[5]}; --simulate: commits "
           f"{r['SIMULATION'][3]}; control --commit: commits {cm[3]}, run log "
           f"{cm[4]}, new month {cm[5]}")


# ------------------------------------------------------- secret/network gate

LINK_PATH = ("/file?uri=/economy/inflationandpriceindices/datasets/"
             "priceindexofprivaterentsukmonthlypricestatistics/"
             "19august2026/priceindexofprivaterentsukmonthlypricestatistics13"
             ".xlsx")
LANDING_HTML = f'<html><a href="{LINK_PATH}">Download</a></html>'


class _Resp:
    def __init__(self, body, status=200):
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text = self.content.decode("utf-8", errors="replace")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append(url)
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return _Resp(body)
        return _Resp(b"not found", 404)


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
    files = [Path(__file__).resolve(), HERE / "s18_pipr_editions.py",
             HERE / "period_editions.py", HERE / "geography.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")

    def fetch_stub(tmp):
        body = write_edition(fresh_dir(tmp), "19august2026", [P1, P2]
                             ).read_bytes()
        s = _Session({m.LANDING: LANDING_HTML, m.BASE_URL + LINK_PATH: body})
        with _quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 1000):
            got = m.fetch_latest_file(session=s, dest=_mk(tmp, "raw"))
        return got, s.calls

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                seed_via_load(cur, _mk(tmp, "seed"))
                f = write_edition(fresh_dir(tmp), "19august2026",
                                  [P1, P2, P3, P4])
                rc, text, _, _ = run_main(cur, ["load", "--commit"], file=f)
                got, calls = fetch_stub(tmp)
            return rc, got[1], calls
    try:
        rc, edition, pages = _in_savepoint(cur, body)
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
    ok = (not attempts and rc == 0 and edition == "19august2026"
          and all(u.startswith("https://") for u in pages) and not literal
          and not in_src and not in_out)
    report(13, name, ok, f"socket attempts {len(attempts)}; load through a "
           f"file returned {rc}; fetch through a stub session requested "
           f"{len(pages)} pages; secret-like literal in source "
           f"{literal or 'none'}; {len(secrets)} secret-named settings "
           f"checked against {len(files)} files and {len(OUTPUT)} output "
           f"lines: in source {in_src or 'none'}, in output "
           f"{in_out or 'none'}")


# ---------------------------------------------------------- gates 14 and 15

def gate_14_revert_new_edition(cur):
    name = "revert stored as a new edition"
    a = recs(P3)
    b = _with(a, 3, "mean_rent", a[3 * NB]["mean_rent"] + 7)

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
    want = [(1, None, ROWS), (2, 1, ROWS), (3, 2, ROWS)]
    ok = (seq == ["new", "revised", "revised"] and after_aba == want
          and again == "unchanged" and after_abaa == want and tip == 3)
    report(14, name, ok, f"A,B,A -> {seq}, editions (number, supersedes) "
           f"{[(e, s) for e, s, _ in after_aba]}; A again -> {again}, "
           f"editions still {[e for e, _, _ in after_abaa]}, tip {tip}")


def gate_15_new_period_reaches_live(cur):
    name = ("new month reaches live (edition 1 and live rows in one "
            "transaction, live source names the edition)")
    r = recs(P4, over={(0, "mean_rent"): None, (1, "rent_index"): Decimal("0.00")},
             prov=True)
    label = m.release_label("19august2026")

    def stored(cur):
        kind = apply(cur, P4, r, "19august2026")
        cur.execute(f"""SELECT COUNT(*), COUNT(loaded_at),
                               COUNT(*) FILTER (WHERE source = %s)
                        FROM public.{ZL} WHERE period = %s""", (label, P4))
        live_n, stamped, labelled = cur.fetchone()
        return (kind, editions_of(cur, P4), live_n, stamped, labelled,
                core.rows_differing(cur, ZZ, P4, 1),
                pe.check_live_equals_edition(cur, prof("19august2026"), P4, 1),
                m.status(cur, ZZ))

    def apply_s18(*a, **k):
        return m.apply_period(*a, **k)

    def failed(cur):
        """A failure while inserting live rolls the whole month back, run
        through the engine's own per-month loop (load_periods)."""
        seen = {}
        real_insert = m.insert_live

        def boom(c, profile, period, records):
            seen["editions_when_live_failed"] = len(editions_of(c, period))
            real_insert(c, profile, period, records[:10])
            raise RuntimeError("live insert failed (planted)")
        with mock.patch.object(m, "insert_live", side_effect=boom), _quiet():
            rc = pe.load_periods(cur, prof("19august2026"), [P4], lambda p: r,
                                 FETCHED, False, simulate=True,
                                 apply=apply_s18)
        return rc, seen, editions_of(cur, P4), live_rows(cur, P4)

    def preview(cur):
        with _quiet() as buf:
            rc = pe.load_periods(cur, prof("19august2026"), [P4], lambda p: r,
                                 FETCHED, False, apply=apply_s18)
        return rc, buf.getvalue(), editions_of(cur, P4), live_rows(cur, P4)
    try:
        kind, eds, live_n, stamped, labelled, diff, chk, st = _in_savepoint(
            cur, stored)
        f_rc, f_seen, f_eds, f_live = _in_savepoint(cur, failed)
        p_rc, p_out, p_eds, p_live = _in_savepoint(cur, preview)
    except (psycopg2.Error, SystemExit) as e:
        return report(15, name, False, str(e).splitlines()[0])
    ok = (kind == "new" and eds == [(1, None, ROWS)] and live_n == ROWS
          and stamped == ROWS and labelled == ROWS and diff == 0 and chk == []
          and st["live_missing"] == []
          and f_rc == 1 and f_seen.get("editions_when_live_failed") == 1
          and f_eds == [] and f_live == 0
          and p_rc == 0 and p_eds == [] and p_live == 0
          and f"would store edition 1 and insert {ROWS} live rows" in p_out)
    report(15, name, ok, f"{kind}: editions {[(e, s) for e, s, _ in eds]}, "
           f"live rows {live_n} (loaded_at set {stamped}, source = "
           f"{label!r} on {labelled}), cells differing {diff}, live_missing "
           f"{st['live_missing']}; planted live failure: rc {f_rc}, edition 1 "
           f"written first={f_seen.get('editions_when_live_failed') == 1}, "
           f"afterwards editions {f_eds} live {f_live}; preview writes "
           f"editions {p_eds} live {p_live}")


# ------------------------------------------------------------------- gate 16

def gate_16_file_identity(cur):
    name = ("file identity: file name, Cover sheet and latest month must "
            "agree, caught before any write")
    ed = "19august2026"
    allp = [P1, P2, P3, P4]

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            cases = {
                "the Cover sheet names another edition": write_edition(
                    fresh_dir(tmp), ed, allp, text=cover("22july2026")),
                "the Cover sheet names no publication date": write_edition(
                    fresh_dir(tmp), ed, allp, text="Crown copyright."),
                "the latest month is not the one before publication":
                    write_edition(fresh_dir(tmp), "16september2026", allp),
                "an unrecognisable file name": write_edition(
                    fresh_dir(tmp), ed, allp, name="prices.xlsx"),
            }
            out = {}
            for label, f in cases.items():
                with mock.patch.object(pe, "apply_period",
                                       side_effect=AssertionError("wrote")), \
                        mock.patch.object(pe, "load_periods") as lp:
                    rc, text, conn, logged = run_main(
                        cur, ["load", "--commit"], file=f)
                out[label] = (rc, text, lp.called,
                              conn.cur.connection.commits, logged.called)
            good = write_edition(fresh_dir(tmp), ed, allp)
            rc, text, _, _ = run_main(cur, ["load"], file=good)
            return out, rc, editions_of(cur, P4), live_rows(cur, P4)
    try:
        out, good_rc, e4, l4 = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(16, name, False, str(e).splitlines()[0])
    unit = m.check_identity(ed, P4, "pipr_19august2026.xlsx", ed)
    unit_bad = [m.check_identity(ed, P4, "pipr_22july2026.xlsx", ed),
                m.check_identity(ed, P3, "pipr_19august2026.xlsx", ed),
                m.check_identity(ed, P4, "pipr_19august2026.xlsx", None),
                m.check_identity(ed, P4, "pipr_19august2026.xlsx",
                                 "22july2026"),
                m.check_identity("nonsense", P4, "x")]
    caught = {k: v[0] == "halt" and not v[2] and v[3] == 0 and not v[4]
              for k, v in out.items()}
    ok = (all(caught.values()) and e4 == [] and l4 == 0 and good_rc == 0
          and unit == [] and all(len(u) >= 1 for u in unit_bad)
          and all("identity" in v[1] for k, v in out.items()
                  if "unrecognisable" not in k))
    report(16, name, ok, "; ".join(
        f"{k}: {'halted before any write' if v else 'NOT CAUGHT'}"
        for k, v in caught.items())
        + f"; a matching file previews (rc {good_rc}); nothing stored")


# ------------------------------------------------------------------- gate 17

def gate_17_older_file_guard(cur):
    name = ("older-file guard: an older edition or an earlier latest month "
            "halts unless --allow-older-file")
    ed8 = "19august2026"

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            f8 = write_edition(fresh_dir(tmp), ed8, [P1, P2, P3, P4],
                               bump={(P2, CODES[0]): 7})
            rc8, t8, _, _ = run_main(cur, ["load", "--commit"], file=f8)
            before = (editions_of(cur), live_rows(cur, P4))
            old = write_edition(fresh_dir(tmp), "22july2026", [P1, P2, P3],
                                bump={(P2, CODES[1]): 9})
            halts = {}
            for argv in (["load"], ["load", "--commit"], ["load", "--simulate"]):
                with mock.patch.object(pe, "load_periods") as lp:
                    rc, text, _, logged = run_main(cur, argv, file=old)
                halts[" ".join(argv)] = (
                    rc == "halt" and "already held" in text
                    and "--allow-older-file" in text and not lp.called
                    and not logged.called)
            unchanged = (editions_of(cur), live_rows(cur, P4)) == before
            rc_allow, t_allow, _, _ = run_main(
                cur, ["load", "--allow-older-file"], file=old)
            rc_same, t_same, _, _ = run_main(cur, ["load", "--commit"],
                                             file=f8)
            f9 = write_edition(fresh_dir(tmp), "16september2026",
                               [P1, P2, P3, P4, "2026-08-01"])
            rc_new, t_new, _, _ = run_main(cur, ["load"], file=f9)
            # the live source column alone names the held edition
            cur.execute(f"UPDATE public.{ZL} SET source = %s",
                        ("ONS PIPR 16september2026 edition",))
            held = m.latest_held_edition(cur, ZZ, True)
            rc_src, t_src, _, _ = run_main(cur, ["load"], file=f8)
            return (rc8, halts, unchanged, rc_allow, t_allow, rc_same, t_same,
                    rc_new, t_new, held, rc_src, t_src)
    try:
        (rc8, halts, unchanged, rc_allow, t_allow, rc_same, t_same, rc_new,
         t_new, held, rc_src, t_src) = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(17, name, False, str(e).splitlines()[0])
    f = m.older_file_problem
    pure = (f("22july2026", ["2026-06-01"], ["2026-06-01"], "22july2026")
            is None
            and f("19august2026", ["2026-07-01"], ["2026-06-01"], None) is None
            and "22july2026" in (f("17june2026", ["2026-05-01"],
                                   ["2026-05-01"], "22july2026") or "")
            and "already held" in (f("17june2026", ["2026-05-01"],
                                     ["2026-06-01"], None) or ""))
    ok = (rc8 == 0 and all(halts.values()) and unchanged and rc_allow == 0
          and "--allow-older-file given" in t_allow and rc_same == 0
          and "--allow-older-file" not in t_same and rc_new == 0
          and "--allow-older-file" not in t_new
          and held == "16september2026" and rc_src == "halt"
          and "16september2026" in t_src and pure)
    report(17, name, ok, f"older file in preview/commit/simulate halted "
           f"before any write={all(halts.values())}; stores unchanged="
           f"{unchanged}; --allow-older-file proceeds ({rc_allow}); same "
           f"edition rerun and a newer file are unaffected; live source "
           f"names {held!r} and halts an older file={rc_src == 'halt'}; "
           f"pure rule={pure}")


# ------------------------------------------------------------------- gate 18

def gate_18_month_checks(cur):
    name = ("per-month checks: short month, missing area or block, zero rent, "
            "NULL for a number above 5 areas, revision above 50% above 10 "
            "areas")
    ed = "19august2026"
    allp = [P1, P2, P3, P4]
    problems = []

    def run(label, kwargs, want_rc, want_text, check):
        def one(cur):
            with tempfile.TemporaryDirectory() as tmp:
                seed_via_load(cur, _mk(tmp, "seed"))
                f = write_edition(fresh_dir(tmp), ed, allp, **kwargs)
                with mock.patch.object(pe, "load_periods",
                                       wraps=pe.load_periods) as lp:
                    rc, text, _, logged = run_main(cur, ["load", "--commit"],
                                                   file=f)
                return (rc, text, logged.called, lp.called, check(cur))
        try:
            rc, text, logged, called, extra = _in_savepoint(cur, one)
        except (psycopg2.Error, SystemExit, RuntimeError) as e:
            problems.append(f"{label}: {e}")
            return
        if rc != want_rc or (want_text and want_text not in text) or not extra:
            problems.append(f"{label}: rc {rc}, expected {want_rc} "
                            f"({want_text!r} in text: {want_text in text}), "
                            f"state ok={extra}")
        if want_rc == 1 and logged:
            problems.append(f"{label}: a run log row was written")
        if want_rc == "halt" and called:
            problems.append(f"{label}: the engine ran")

    def only_first(*periods):
        return lambda c: all(editions_of(c, p) == [(1, None, ROWS)]
                             for p in periods)

    run("short month", {"codes": {P4: CODES[:-1]}, "bump": {(P2, CODES[0]): 1}},
        1, f"{P4}: REJECTED",
        lambda c: editions_of(c, P4) == [] and live_rows(c, P4) == 0
        and editions_of(c, P2)[-1][0] == 2)
    run("short month text", {"codes": {P4: CODES[:-1]}}, 1,
        f"{N - 1} areas, expected {N}", lambda c: editions_of(c, P4) == [])
    run("area swapped for another", {"codes": {P4: CODES[:-1] + [SPARE]}}, 1,
        CODES[-1], lambda c: editions_of(c, P4) == [])
    run("block missing", {"drop": ("Annual change terraced",)}, "halt",
        "Annual change terraced", lambda c: editions_of(c, P4) == [])
    run("zero rent", {"overrides": {(P4, CODES[2]): {"Rental price": 0}}}, 1,
        "zero or negative rent", lambda c: editions_of(c, P4) == [])
    null = lambda n: {(P2, c): {"Rental price": None} for c in CODES[:n]}
    run("NULL for a number in 6 areas", {"overrides": null(6)}, 1,
        "NULL replaces a number in 6 areas",
        lambda c: editions_of(c, P2) == [(1, None, ROWS)]
        and editions_of(c, P4) == [(1, None, ROWS)])
    run("NULL for a number in 5 areas (the limit)", {"overrides": null(5)}, 0,
        "", lambda c: editions_of(c, P2)[-1][0] == 2)
    run("revision above 50% in 11 areas",
        {"bump": {(P3, c): 2000 for c in CODES[:11]}}, 1,
        "revised above 50% in 11 areas",
        lambda c: editions_of(c, P3) == [(1, None, ROWS)])
    run("revision above 50% in 10 areas (the limit)",
        {"bump": {(P3, c): 2000 for c in CODES[:10]}}, 0, "",
        lambda c: editions_of(c, P3)[-1][0] == 2)
    report(18, name, not problems, "; ".join(problems[:3]) if problems else
           "9 scenarios: each rejected month stored nothing and wrote no run "
           "log row, the other months of the run went through, and the "
           "limits themselves (5 NULL areas, 10 revised areas) pass")


# ------------------------------------------------------------------- gate 19

def gate_19_barnsley_sheffield(cur):
    name = ("Barnsley and Sheffield through geography.resolve: a form that "
            "disagrees with S18's declaration halts before any write")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("none",))[0]

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            f = write_edition(fresh_dir(tmp), "19august2026",
                              [P1, P2, P3, P4], extra=[(P4, "E08000016")])
            with mock.patch.object(pe, "load_periods") as lp, \
                    mock.patch.object(pe, "apply_period",
                                      side_effect=AssertionError("wrote")):
                rc, text, conn, logged = run_main(cur, ["load", "--commit"],
                                                  file=f)
            halt = (rc, text, lp.called, conn.cur.connection.commits,
                    editions_of(cur, P4), live_rows(cur, P4))
            g = write_edition(fresh_dir(tmp), "19august2026", [P4],
                              extra=[(P4, "E08000038")])
            stub = (set(CODES) | {"E08000016"}, {})
            with _quiet(), mock.patch.object(m, "reference_codes",
                                             return_value=stub), \
                    mock.patch.object(m.geography, "resolve",
                                      wraps=m.geography.resolve) as rs:
                by = m.load_file_records(g, _Borrowed(cur),
                                         min_period=date(2026, 4, 1))[0]
            return halt, rs.call_args, [r["lad24cd"] for r in by[P4]]
    try:
        halt, call, lads = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, ValueError) as e:
        return report(19, name, False, str(e).splitlines()[0])
    rc, text, called, commits, eds, lv = halt
    ok = (decl == "new" and rc == "halt"
          and all(x in text for x in ("declared 'new'", "E08000016",
                                      "geography.py"))
          and not called and commits == 0 and eds == [] and lv == 0
          and call.args[1] == "18" and "E08000038" in call.args[2]
          and "E08000016" in lads and "E08000038" not in lads)
    report(19, name, ok, f"S18 declared {decl!r}; a file carrying "
           f"E08000016 halted={rc == 'halt'} with commits {commits}, "
           f"editions {eds}, live rows {lv}; a file carrying E08000038 is "
           "keyed to E08000016 via resolve(cur, '18', ...)="
           f"{'E08000016' in lads}")


# ------------------------------------------------------------------- gate 20

def gate_20_rerun_idempotent(cur):
    name = ("rerun idempotent: the same file twice stores nothing and leaves "
            "live untouched")

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            f = seed_via_load(cur, _mk(tmp, "seed"))
            cur.execute(f"SELECT COUNT(*), MAX(loaded_at) FROM public.{ZL}")
            before = (cur.fetchone(), editions_of(cur))
            runs = []
            for _ in range(2):
                rc, text, _, _ = run_main(cur, ["load", "--commit"], file=f)
                cur.execute(f"SELECT COUNT(*), MAX(loaded_at) FROM "
                            f"public.{ZL}")
                runs.append((rc, f"{P1}: unchanged" in text
                             and f"{P3}: unchanged" in text,
                             (cur.fetchone(), editions_of(cur)) == before))
            return before, runs
    try:
        before, runs = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(20, name, False, str(e).splitlines()[0])
    ok = (all(r == (0, True, True) for r in runs)
          and before[1] == [(1, None, 3 * ROWS)])
    report(20, name, ok, f"two reruns of the seed file: {runs}; editions "
           f"{before[1]}")


# ------------------------------------------------------------------- gate 21

def gate_21_stranded_repair(cur):
    name = ("stranded month repair: editions with no live rows get live rows "
            "from load, no new edition")

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            ok0 = m.status(cur, ZZ)["ok"]
            f = write_edition(fresh_dir(tmp), "19august2026",
                              [P1, P2, P3, P4])
            with _quiet():
                rows = m.parse_workbook(f)
            by = m.group_by_period(m.build_records(rows, {}))
            core.insert_edition(cur, ZZ, by[P4], P4, release_label="t",
                                published_date=FETCHED, source_file="t",
                                source_sha256="h", supersedes=None)
            st = m.status(cur, ZZ)
            rc_status, _, _, _ = run_main(cur, ["status"])
            rc, text, _, _ = run_main(cur, ["load", "--commit"], file=f)
            cur.execute(f"SELECT DISTINCT source FROM public.{ZL} "
                        "WHERE period = %s", (P4,))
            src = cur.fetchall()
            return (ok0, st, rc_status, rc, text, editions_of(cur, P4),
                    live_rows(cur, P4), src, m.status(cur, ZZ))
    try:
        ok0, st, rc_status, rc, text, eds, lv, src, after = _in_savepoint(
            cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(21, name, False, str(e).splitlines()[0])
    ok = (ok0 and st["live_missing"] == [P4] and not st["ok"]
          and rc_status == 1 and rc == 0 and eds == [(1, None, ROWS)]
          and lv == ROWS and src == [("ONS PIPR 19august2026 edition",)]
          and after["live_missing"] == [])
    report(21, name, ok, f"status flagged {st['live_missing']} (exit "
           f"{rc_status}); load rc {rc}; editions of the month {eds} (no new "
           f"edition), live rows {lv}, live source {src}; live_missing "
           f"afterwards {after['live_missing']}")


# ------------------------------------------------------------------- gate 22

def gate_22_sync_new_latest(cur):
    name = ("sync-new records edition 1 for a month with several load dates, "
            "taking the latest")
    d_old, d_new = "2026-09-01 10:00:00+00", "2026-10-02 12:00:00+00"

    def loaded(r):
        if r["period"] == P1 and r["lad24cd"] == CODES[0]:
            return d_new
        return d_old

    def body(cur):
        seed_live(cur, recs(P1) + recs(P2), loaded_at=loaded)
        single = dataclasses.replace(ZZ, as_loaded_date="single")
        ctrl = _in_savepoint(cur, lambda c: _halts(
            lambda: core.sync_new(c, single, N)))
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
        return report(22, name, False, str(e).splitlines()[0])
    want = [(date.fromisoformat(P1), 1, None, ROWS, date(2026, 10, 2),
             date(2026, 10, 2), core.AS_LOADED_LATEST_LABEL),
            (date.fromisoformat(P2), 1, None, ROWS, date(2026, 9, 1),
             date(2026, 9, 1), core.AS_LOADED_LATEST_LABEL)]
    ok = (rc == 0 and rows == want and logged and equal == []
          and ctrl[0] and "loaded_at" in ctrl[1])
    report(22, name, ok, f"sync-new rc {rc}; {P1} (rows loaded on two days) "
           f"-> edition 1 published_date {rows[0][4] if rows else None}, "
           f"{P2} (one day) -> {rows[1][4] if len(rows) > 1 else None}; "
           f"latest equals live={equal == []}; the 'single' control halts="
           f"{ctrl[0]}")


# ------------------------------------------------------------------- gate 23

def gate_23_unresolved_code(cur):
    name = "an unresolved area code halts the load before any write"

    def body(cur):
        with tempfile.TemporaryDirectory() as tmp:
            seed_via_load(cur, _mk(tmp, "seed"))
            f = write_edition(fresh_dir(tmp), "19august2026",
                              [P1, P2, P3, P4], extra=[(P4, "E06999997")])
            with mock.patch.object(pe, "load_periods") as lp:
                rc, text, conn, logged = run_main(cur, ["load", "--commit"],
                                                  file=f)
            return (rc, text, lp.called, logged.called, editions_of(cur, P4),
                    live_rows(cur, P4))
    try:
        rc, text, called, logged, eds, lv = _in_savepoint(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(23, name, False, str(e).splitlines()[0])
    ok = (rc == "halt" and "UNEXPLAINED E06999997" in text
          and "la_code_lookup" in text and not called and not logged
          and eds == [] and lv == 0)
    report(23, name, ok, f"halted={rc == 'halt'} naming UNEXPLAINED "
           f"E06999997, engine ran={called}, editions {eds}, live rows {lv}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1,
         gate_3_latest_equals_live, gate_4_coverage, gate_5_nonpositive,
         gate_6_null_vs_zero, gate_7_provisional_compared,
         gate_8_seeded_revision, gate_9_fork_and_second_root,
         gate_10_parse_and_record_rules, gate_11_planner,
         gate_12_preview_default, gate_13_no_network_no_secrets,
         gate_14_revert_new_edition, gate_15_new_period_reaches_live,
         gate_16_file_identity, gate_17_older_file_guard,
         gate_18_month_checks, gate_19_barnsley_sheffield,
         gate_20_rerun_idempotent, gate_21_stranded_repair,
         gate_22_sync_new_latest, gate_23_unresolved_code)


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
