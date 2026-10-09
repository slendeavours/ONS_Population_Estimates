"""Gates for the S9a (NHS England Discharge Ready Date) edition-history table.

Mirrors scripts/s18_pipr_editions_verify.py. Prints `GATE n name: PASS|FAIL`;
exits 1 on any FAIL. Gates 2 to 5, 7 and 24 read the real tables
(nhs_drd_discharge_delays, nhs_drd_discharge_delays_editions and its
file-check ledger) and gate 6 adds real data to its seeded scenario. Until the
editions table exists they print `FAIL (pending load)` with the words "not yet
migrated", so the script exits non-zero until then, like the S1, S1b, S8b,
S15, S18 and S19 verifiers; nothing is created to make them pass. The seeded
gates never need the real editions table: they run on throwaway tables
zz_s9a_live, zz_s9a_editions and zz_s9a_editions_file_checks (a copy of the
loader's SPEC named zz_s9a), created inside the transaction, and call the
loader's own writer and commands (s9a_drd_editions.apply_period and
insert_live, pe.load_periods, core.refresh_latest, and main/cmd_load with SPEC
pointed at the copy and the publisher's page and downloads stubbed) rather than
a re-implementation. The real tables are only ever read.

Gates:
  1  table and file-check ledger shape, append-only triggers (UPDATE, DELETE,
     TRUNCATE blocked on both), on the throwaway copy; also the shape of the
     real tables once they exist                                   (real)
  2  edition 1 present for every live month; live source names a file of the
     month                                                          (real)
  3  latest edition equals live (values and labels); live source equals the
     tip's source_file (a month whose tip is edition 1 carries it on every
     row)                                                           (real)
  4  coverage: the held area count (153) in every edition of every month,
     every UTLA in utla_lad_mapping                                 (real)
  5  no negative count, every percentage within 0..1                (real)
  6  NULL versus 0 never conflated on every value column, both ways (seeded);
     a '-' in a file is stored NULL, never 0, through the loader   (real data)
  7  total_discharges NULL everywhere: documented, listed if a later file
     fills it, never a failure                                      (real)
  8  seeded revision: refresh_latest changes only that month's changed rows
     and moves their source
  9  seeded fork and second root refused (chain_tip, and the loader's compare)
  10 parse rules: markers, #N/A and NULL rows dropped and counted (nothing
     stored as 0), a published 0 stays 0, unknown text, a fraction in a count,
     a non-English code and a shifted header all halt
  11 planner: window, ledger skip, --recheck-all, never a month before the
     earliest held
  12 preview default: the load command with no flag issues no commit and
     stores nothing
  13 no network (a socket attempt fails the gate), no secret in source or
     output
  14 revert stored as a new edition (A, B, A -> edition 3; A again stores
     nothing)
  15 new month reaches live in one transaction (a failing live insert rolls
     the edition and the ledger row back; preview writes nothing)
  16 file identity: link month, Cover Sheet and UTLA sheet Period:, Revised:
     against the -Revised name and the title must agree, caught before any
     write
  17 older-file guard: an older file (page link or --file) or an earlier page
     halts unless --allow-older-file, before anything is downloaded
  18 month checks: a short month, a missing UTLA, extra areas, a negative
     count, a percentage above 1, NULL for a number above 5 areas, a bed-days
     revision above 50% in more than 10 areas are rejected (limits pass); a
     0/NULL change needs --acknowledge
  19 Barnsley and Sheffield through geography.resolve: the 'old' declaration
     for 9a halts on the new form before any write
  20 rerun idempotent: the same files twice store nothing and leave live
     untouched
  21 stranded month repair: a month with editions and no live rows gets its
     live rows from load, with no new edition
  22 file-check ledger: an unchanged reissue is recorded once and skipped on
     the next run; --recheck-all reads it again
  23 precision-only changes (below 1e-8) are stored, labelled and reported
     apart, and never trip a stop
  24 on-disk reproduction: every webfile in data/raw/s9a_drd/ whose name is a
     held tip's file parses to that tip exactly (read-only)       (real)

Usage:
    python scripts/s9a_drd_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The commands get a stand-in
connection whose commit only counts. No network, no real-table writes, no
backend is ever terminated.
"""
import contextlib
import dataclasses
import io
import itertools
import os
import re
import socket
import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
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
import s9a_drd_editions as m  # noqa: E402
from test_s9a_drd_pure import (BASE, data_row, header_row, html_of,  # noqa: E402
                               url, write_workbook)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC, LIVE, TABLE = m.SPEC, m.LIVE, m.TABLE
LEDGER = pe.file_checks_table(m.PROFILE)
ZZ = dataclasses.replace(
    SPEC, name="zz_s9a", live_table="zz_s9a_live",
    editions_table="zz_s9a_editions")
ZL, ZE = ZZ.live_table, ZZ.editions_table
ZLEDGER = pe.file_checks_table(m._profile(ZZ))
FETCHED = date(2026, 10, 9)
N = 12                                   # areas in the seeded data
# ten English UTLAs plus Barnsley and Sheffield (the old form, as 9a publishes)
CODES = [f"E060000{i:02d}" for i in range(1, 11)] + ["E08000016", "E08000019"]
SPARE = "E06000077"
COUNTY = "E10000003"
P1, P2, P3, P4 = "2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01"
NAME = {P1: ("April-2026", 6), P2: ("May-2026", 7), P3: ("June-2026", 8),
        P4: ("July-2026", 9)}
VALUE_COLS = m.VALUE_COLUMNS
NOT_YET = f"{TABLE} does not exist yet (not yet migrated)"
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

def recs(period, n=N, over=None, codes=CODES, source="gate"):
    """The month's records (n areas). over {(area index, column): value}
    (None allowed) replaces cells. total_discharges is NULL, as published."""
    over = over or {}
    month = int(period[5:7])
    out = []
    for i in range(n):
        r = {"utla_code": codes[i], "utla_name": f"Area {codes[i]}",
             "reporting_period": period, "source": source}
        for c in m.COUNT_COLUMNS:
            r[c] = 100 + 10 * i + month
        r["total_discharges"] = None
        for c in m.RATIO_COLUMNS:
            r[c] = Decimal("0.25") + Decimal(i) / 100
        for (a, col), v in over.items():
            if a == i:
                r[col] = v
        out.append(r)
    return out


def with_cell(records, area, col, new):
    return [dict(r, **{col: new}) if r["utla_code"] == CODES[area] else r
            for r in records]


def setup_throwaway(cur):
    cur.execute("SELECT to_regclass('public.zz_s9a_live'), "
                "to_regclass('public.zz_s9a_editions'), "
                f"to_regclass('public.{ZLEDGER}')")
    if cur.fetchone() != (None, None, None):
        sys.exit("HARD STOP: a zz_s9a table already exists as a real table; "
                 "refusing to run")
    cur.execute(f"CREATE TABLE public.{ZL} (LIKE public.{LIVE} "
                "INCLUDING ALL)")
    core.create_schema(cur, ZZ)
    pe.create_file_checks(cur, m._profile(ZZ))


def editions_of(cur, period=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{ZE}"
                + (" WHERE reporting_period = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def ledger_of(cur, period=None):
    """[(file name, outcome, edition)] of the throwaway ledger, in order."""
    cur.execute(f"SELECT source_file, outcome, edition FROM public.{ZLEDGER}"
                + (" WHERE reporting_period = %s" if period else "")
                + " ORDER BY id", (period,) if period else None)
    return [(s.rsplit("/", 1)[-1], o, e) for s, o, e in cur.fetchall()]


def live_rows(cur, period):
    cur.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE reporting_period = "
                "%s", (period,))
    return cur.fetchone()[0]


def live_vals(cur, period, col="total_bed_days_lost"):
    cur.execute(f"SELECT utla_code, {col} FROM public.{ZL} WHERE "
                "reporting_period = %s", (period,))
    return dict(cur.fetchall())


def live_sources(cur, period):
    cur.execute(f"SELECT DISTINCT source FROM public.{ZL} WHERE "
                "reporting_period = %s", (period,))
    return cur.fetchall()


def state(cur):
    cur.execute(f"SELECT COUNT(*), MAX(loaded_at) FROM public.{ZL}")
    return cur.fetchone(), editions_of(cur), ledger_of(cur)


def raw_edition(cur, period, edition, supersedes, sha):
    """One throwaway-table row planted directly (the loader's writer refuses a
    fork or a second root, so a fault has to be planted by hand)."""
    cur.execute(f"""INSERT INTO public.{ZE} (utla_code, reporting_period,
                    edition, total_bed_days_lost, supersedes, source_sha256)
                    VALUES (%s, %s, %s, 1, %s, %s)""",
                (CODES[0], period, edition, supersedes, sha))


def prof(expected=N, required=()):
    """The loader's own per-run profile on the throwaway spec."""
    return m.run_profile(ZZ, expected, required)


def link_of(period, revised=False):
    nm, up = NAME[period]
    return (url(2026, up + 2, nm + "-Revised") if revised
            else url(2026, up, nm))


def orig(p):
    return link_of(p)


def rev(p):
    return link_of(p, True)


def apply(cur, period, records, link=None, label="gate file", expected=N):
    """The loader's own writer for one month: s9a_drd_editions.apply_period
    (edition rows, live rows through the loader's insert_live, then the
    file-check ledger row)."""
    link = link or orig(period)
    rr = [dict(r, source=link) for r in records]
    info = m.FileInfo(link, Path("gate.xlsx"), "ab" * 32, label)
    return m.apply_period(cur, prof(expected), period, rr,
                          fetched_on=FETCHED,
                          info=info)


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


class Env:
    """The loader's main(argv) on the throwaway tables, with SPEC pointed at
    the copy, a stand-in connection, the publisher's page and downloads
    stubbed (the workbooks are built here), valid UTLA codes stubbed and the
    run log stubbed."""

    def __init__(self, cur, tmp):
        self.cur = cur
        self.tmp = Path(tmp)
        self.files = {}
        self.fetched = []
        self.n = 0

    def make(self, link, period, *, bump=None, codes=None, overrides=None,
             revised=None, **kw):
        """Build the workbook for `link`; register it for the download stub.
        overrides {utla_code: {field: value}}; bump {utla_code: +x} on
        total_bed_days_lost."""
        rows = []
        for i, c in enumerate(codes or CODES):
            v = {"total_bed_days_lost": 300 + 10 * i + int(period[5:7])
                 + (bump or {}).get(c, 0)}
            v.update((overrides or {}).get(c, {}))
            rows.append(data_row(c, f"Area {c}", v))
        self.n += 1
        d = self.tmp / f"f{self.n}"
        d.mkdir()
        path = write_workbook(
            d / m._name(link), date.fromisoformat(period), rows,
            revised=revised if revised is not None
            else ("9th July 2026" if "Revised" in link else None), **kw)
        self.files[link] = path
        return path

    def run(self, argv, *, page=(), table=True, file=None):
        """Returns (rc or 'halt', text, connection stand-in, run-log mock)."""
        borrowed = _Borrowed(self.cur)
        args = list(argv)
        if file:
            args += ["--file", str(file)]

        def fetch_month(link, dest=None, session=None):
            self.fetched.append(link)
            return self.files[link]

        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "valid_codes", return_value=(
                    set(CODES) | {SPARE, COUNTY, "E08000038", "E08000039"})), \
                mock.patch.object(m, "fetch_page",
                                  return_value=html_of(*page)), \
                mock.patch.object(m, "fetch_month", side_effect=fetch_month), \
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

    def seed(self, periods=(P1, P2, P3), link=orig):
        page = []
        for p in periods:
            self.make(link(p), p)
            page.append(link(p))
        rc, text, _, _ = self.run(
            ["load", "--commit", "--min-period", periods[0],
             "--expected-areas", str(N)], page=page)
        if rc != 0:
            raise RuntimeError(f"seed load failed: {text[-300:]}")
        self.fetched.clear()
        return page


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp)


def scenario(cur, fn, periods=(P1, P2, P3), link=orig):
    """Seed through the loader, run fn(env, page) and roll all of it back."""
    def body(c):
        with env(c) as e:
            page = e.seed(periods, link)
            return fn(e, page)
    return _in_savepoint(cur, body)


# ---------------------------------------------------------------- gate 1

def shape_problems(cur, spec, ledger_table):
    """Problems with the editions table and its file-check ledger."""
    t = spec.editions_table
    cur.execute("""SELECT column_name, data_type, character_maximum_length,
                          numeric_precision, numeric_scale, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (t,))
    cols = {r[0]: r[1:] for r in cur.fetchall()}
    need = {*spec.key_cols, spec.period_col, "edition",
            *(c for c, _ in spec.value_cols), "utla_name", "release_label",
            "published_date", "source_file", "source_sha256", "supersedes",
            "loaded_at"}
    bad = []
    if need - set(cols):
        bad.append(f"missing columns {sorted(need - set(cols))}")
    if cols.get("utla_code", ("",))[:2] != ("character varying", None):
        bad.append(f"utla_code is {cols.get('utla_code')}")
    if cols.get("utla_code", (0, 0, 0, 0, "YES"))[4] != "NO":
        bad.append("utla_code is nullable")
    if cols.get(spec.period_col, ("",))[0] != "date":
        bad.append(f"{spec.period_col} is {cols.get(spec.period_col)}")
    for c, sql in spec.value_cols:
        got = cols.get(c, ("",))[0]
        want = "integer" if sql == "integer" else "numeric"
        if got != want:
            bad.append(f"{c} is {got}, expected {want}")
        if sql == "numeric" and cols.get(c, (0, 0, 0, 0))[2:4] != (None, None):
            bad.append(f"{c} has a precision/scale {cols.get(c)}")
    for c in ("edition", "supersedes"):
        if cols.get(c, ("",))[0] != "integer":
            bad.append(f"{c} is {cols.get(c)}")
    cur.execute("""SELECT a.attname FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid
                    AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = %s::regclass AND i.indisprimary""",
                (f"public.{t}",))
    if {r[0] for r in cur.fetchall()} != {"utla_code", "reporting_period",
                                          "edition"}:
        bad.append("primary key is not (utla_code, reporting_period, "
                   "edition)")
    cur.execute("""SELECT contype FROM pg_constraint
                   WHERE conrelid = %s::regclass""", (f"public.{t}",))
    if any(r[0] == "f" for r in cur.fetchall()):
        bad.append("a foreign key is present (E10 counties are not in "
                   "la_boundaries)")
    bad += _triggers(cur, t, spec.trigger, spec.truncate_trigger)
    # the ledger
    if not pe.table_exists(cur, ledger_table):
        bad.append(f"{ledger_table} does not exist")
        return bad
    cur.execute("""SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""",
                (ledger_table,))
    lc = {r[0]: r[1:] for r in cur.fetchall()}
    for c, ty, nullable in (("id", "bigint", "NO"),
                            (spec.period_col, "date", "NO"),
                            ("source_file", "text", "NO"),
                            ("file_sha256", "text", "NO"),
                            ("outcome", "text", "NO"),
                            ("edition", "integer", "YES"),
                            ("checked_at", "timestamp with time zone", "NO")):
        if lc.get(c) != (ty, nullable):
            bad.append(f"ledger {c} is {lc.get(c)}, expected "
                       f"{(ty, nullable)}")
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass AND contype = 'c'""",
                (f"public.{ledger_table}",))
    chk = " ".join(r[0] for r in cur.fetchall())
    if not all(f"'{o}'" in chk for o in pe.FILE_CHECK_OUTCOMES):
        bad.append(f"ledger outcome check is {chk!r}")
    bad += [f"ledger {p}" for p in _triggers(
        cur, ledger_table, f"{spec.name}_file_checks_immutable",
        f"{spec.name}_file_checks_no_truncate")]
    return bad


def _triggers(cur, table, row_trigger, trunc_trigger):
    cur.execute("""SELECT t.tgname, t.tgtype FROM pg_trigger t
                   WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal""",
                (f"public.{table}",))
    trg = dict(cur.fetchall())
    # tgtype bits: 1 row, 2 before, 4 insert, 8 delete, 16 update, 32 truncate
    ud, tr = trg.get(row_trigger, 0), trg.get(trunc_trigger, 0)
    if not (ud & 1 and ud & 2 and ud & 8 and ud & 16 and not ud & 4
            and tr & 2 and tr & 32):
        return [f"append-only triggers wrong: {sorted(trg)}"]
    return []


def immutability(cur, table, stmts, seed_row):
    """{UPDATE, DELETE, TRUNCATE: error text or None} on a table holding
    rows; all in savepoints."""
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
    name = ("table and file-check ledger shape (UPDATE, DELETE and TRUNCATE "
            "are blocked on both)")
    bad = shape_problems(cur, ZZ, ZLEDGER)

    def seed(cur):
        apply(cur, P1, recs(P1, n=1), expected=1)
    res = immutability(
        cur, ZE, {"UPDATE": f"UPDATE public.{ZE} SET total_bed_days_lost = 2",
                  "DELETE": f"DELETE FROM public.{ZE}",
                  "TRUNCATE": f"TRUNCATE public.{ZE}"}, seed)
    res_l = immutability(
        cur, ZLEDGER, {"UPDATE": f"UPDATE public.{ZLEDGER} SET outcome = "
                       "'new'", "DELETE": f"DELETE FROM public.{ZLEDGER}",
                       "TRUNCATE": f"TRUNCATE public.{ZLEDGER}"}, seed)
    for what, r in (("editions", res), ("ledger", res_l)):
        for k, msg in r.items():
            if not (msg and "append-only" in msg):
                bad.append(f"{what} {k} not blocked ({msg})")
    scope = "throwaway copy"
    if table_exists(cur):
        scope += " and real tables"
        bad += [f"real: {p}" for p in shape_problems(cur, SPEC, LEDGER)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise append-only "
           "on the editions table and the ledger")


# ------------------------------------------------- real-table gates

def real_gate(cur, n, name, fn):
    """Run fn(cur) -> (ok, detail) on the real tables, or report pending when
    the editions table has not been created."""
    if not table_exists(cur):
        return report(n, name, False, NOT_YET, pending=True)
    try:
        ok, detail = fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(n, name, False, str(e).splitlines()[0])
    report(n, name, ok, detail)


def _live_periods(cur, spec=SPEC):
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _source_files(cur, spec, period):
    """({edition: source_file}, tip) of a month."""
    cur.execute(f"SELECT edition, source_file FROM public.{spec.editions_table}"
                f" WHERE {spec.period_col} = %s ORDER BY 1", (period,))
    files = {}
    for ed, sf in cur.fetchall():
        files.setdefault(ed, sf)
    return files, core.chain_tip(cur, spec, period)


def real_edition1_present(cur, spec=SPEC):
    """Edition 1 for every live month; the live source of the month names a
    file of the month (edition 1's, or a later edition's after a refresh)."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live periods to check (an empty state is not a pass)"
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE edition = 1")
    have = {r[0] for r in cur.fetchall()}
    missing = [_s(x) for x in periods if x not in have]
    if missing:
        return False, (f"no edition 1 for {missing[:6]} ({len(missing)} of "
                       f"{len(periods)})")
    bad = []
    for p in periods:
        files, tip = _source_files(cur, spec, p)
        if not files.get(1):
            bad.append(f"{p}: edition 1 has no source_file")
            continue
        cur.execute(f"SELECT DISTINCT source FROM public.{spec.live_table} "
                    f"WHERE {spec.period_col} = %s", (p,))
        src = {r[0] for r in cur.fetchall()}
        if tip == 1 and src != {files[1]}:
            bad.append(f"{p}: live source {sorted(map(str, src))[:2]} is not "
                       f"edition 1's source_file")
        elif not src <= set(files.values()):
            bad.append(f"{p}: live source {sorted(map(str, src))[:2]} names "
                       "no file of the month")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} live months each have edition 1; live source names "
        "a file of the month")


def real_latest_equals_live(cur, spec=SPEC):
    """The latest edition equals live (values and labels, row for row); a
    month carries the tip's source_file on every row, whatever its tip (a
    refresh sets the source of the whole month, so a mixed month fails)."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live periods to check (an empty state is not a pass)"
    bad = load_checks.check_latest_equals_live(cur, spec)
    for p in periods:
        try:
            files, tip = _source_files(cur, spec, p)
        except (LookupError, ValueError):
            continue          # already reported by check_latest_equals_live
        cur.execute(f"SELECT DISTINCT source FROM public.{spec.live_table} "
                    f"WHERE {spec.period_col} = %s", (p,))
        src = {r[0] for r in cur.fetchall()}
        if tip == 1 and src != {files.get(1)}:
            bad.append(f"{p}: live source {sorted(map(str, src))[:2]} is not "
                       f"the tip's source_file {files.get(1)!r}")
        elif tip > 1 and not src <= set(files.values()):
            bad.append(f"{p}: live source names no file of the month")
        elif tip > 1 and src != {files[tip]}:
            bad.append(f"{p}: live source {sorted(map(str, src))[:3]} is not "
                       f"uniformly the tip's file {files[tip]!r}")
    return not bad, "; ".join(bad[:4]) if bad else (
        f"{len(periods)} months match row for row; every row of every month "
        "carries the tip's source_file")


def real_coverage(cur, spec=SPEC, valid=None):
    """Every edition of every month holds the held area count (153) with
    distinct keys, the tip too, and every UTLA is in utla_lad_mapping."""
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
        cur.execute(f"""SELECT COUNT(DISTINCT utla_code)
                        FROM public.{spec.editions_table}
                        WHERE {spec.period_col} = %s AND edition = %s""",
                    (p, tip))
        got = cur.fetchone()[0]
        if got != want_n:
            bad.append(f"{p} ed{tip}: {got} areas")
    if not n_ed:
        return False, "no editions to check (an empty state is not a pass)"
    if valid is None:
        valid = m.valid_codes(cur)
    for table in (spec.editions_table, spec.live_table):
        cur.execute(f"SELECT DISTINCT utla_code FROM public.{table}")
        stray = sorted(r[0] for r in cur.fetchall() if r[0] not in valid)
        if stray:
            bad.append(f"{table}: {stray[:4]} not in utla_lad_mapping")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} months, {n_ed} editions, each {want_n} areas (held "
        "count), every UTLA in utla_lad_mapping")


def real_no_negative(cur, spec=SPEC):
    """No negative count and every percentage within 0..1 (NULL is a
    suppressed value, not a bad one), in the editions and in live."""
    counts, pcts = {}, {}
    neg = " OR ".join(f"{c} < 0" for c in m.COUNT_COLUMNS)
    out = " OR ".join(f"{c} < 0 OR {c} > 1" for c in m.PCT_COLUMNS)
    for label, table in (("editions", spec.editions_table),
                         ("live", spec.live_table)):
        cur.execute(f"SELECT COUNT(*) FILTER (WHERE {neg}), "
                    f"COUNT(*) FILTER (WHERE {out}) FROM public.{table}")
        counts[label], pcts[label] = cur.fetchone()
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table}")
    if not cur.fetchone()[0]:
        return False, "no live rows to check (an empty state is not a pass)"
    return not any(counts.values()) and not any(pcts.values()), (
        f"rows with a negative count: editions {counts['editions']}, live "
        f"{counts['live']}; with a percentage outside 0..1: editions "
        f"{pcts['editions']}, live {pcts['live']}")


def null_zero_counts(cur, table, where="", params=()):
    """(NULL count, zero count) per value column as a flat tuple."""
    parts = []
    for c in VALUE_COLS:
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
        "NULL and 0 counts per month and column identical in live and the "
        "latest edition; EXCEPT comparison clean")


def real_total_discharges(cur, spec=SPEC):
    """total_discharges is NULL everywhere ('-' at UTLA level): documented.
    A later file that fills it is listed, never a failure."""
    out, filled = {}, []
    for label, table in (("editions", spec.editions_table),
                         ("live", spec.live_table)):
        cur.execute(f"SELECT COUNT(*), COUNT(total_discharges) FROM "
                    f"public.{table}")
        out[label] = cur.fetchone()
        cur.execute(f"SELECT DISTINCT {spec.period_col} FROM public.{table} "
                    "WHERE total_discharges IS NOT NULL ORDER BY 1")
        filled += [f"{label} {_s(r[0])}" for r in cur.fetchall()]
    if not out["live"][0]:
        return False, "no live rows to check (an empty state is not a pass)"
    return True, (f"non-NULL total_discharges: editions {out['editions'][1]} "
                  f"of {out['editions'][0]} rows, live {out['live'][1]} of "
                  f"{out['live'][0]}"
                  + (f"; LISTED (a later file fills it): {filled[:6]}"
                     if filled else "; NULL everywhere, as documented"))


def real_on_disk(cur, spec=SPEC, raw_dir=None):
    """Every webfile in data/raw/s9a_drd/ whose name is the file of a month's
    held tip parses (parse_workbook + build_records, the loader's own
    functions) to exactly that tip. Read-only."""
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} ORDER BY 1")
    periods = [r[0] for r in cur.fetchall()]
    if not periods:
        return False, "no editions to check (an empty state is not a pass)"
    cols = ("utla_code", "utla_name") + tuple(VALUE_COLS)
    ok_n, bad, skipped = 0, [], 0
    for p in periods:
        files, tip = _source_files(cur, spec, p)
        name = m._name(files[tip]) if files.get(tip) else None
        path = raw / name if name else None
        if not path or not path.is_file():
            skipped += 1
            continue
        rows, _ = m.parse_workbook(path)
        got = {r["utla_code"]: tuple(r[c] for c in cols)
               for r in m.build_records(rows, {}, _s(p))}
        cur.execute(f"SELECT {', '.join(cols)} FROM "
                    f"public.{spec.editions_table} WHERE "
                    f"{spec.period_col} = %s AND edition = %s", (p, tip))
        held = {r[0]: tuple(r) for r in cur.fetchall()}
        if got != held:
            n = sum(1 for k in set(got) | set(held)
                    if got.get(k) != held.get(k))
            bad.append(f"{_s(p)} ed{tip}: {name} differs from the tip in {n} "
                       "area(s)")
        else:
            ok_n += 1
    if not ok_n and not bad:
        return False, (f"no webfile in {raw} is the file of a held tip "
                       f"({len(periods)} months; an empty comparison is not "
                       "a pass)")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{ok_n} on-disk webfile(s) parse to their tip exactly; {skipped} "
        "month(s) whose tip file is not on disk skipped")


def gate_2_edition1(cur):
    real_gate(cur, 2, "edition 1 present for every live month; live source "
              "names a file of the month", real_edition1_present)


def gate_3_latest_equals_live(cur):
    real_gate(cur, 3, "latest edition equals live (values and labels) and "
              "live source is the tip's file", real_latest_equals_live)


def gate_4_coverage(cur):
    real_gate(cur, 4, "coverage: the held area count in every edition, "
              "every UTLA in utla_lad_mapping", real_coverage)


def gate_5_negative(cur):
    name = "no negative count, every percentage within 0..1"
    neg = recs(P1)
    neg[0]["total_bed_days_lost"] = -1
    big = recs(P1)
    big[0]["pct_same_day_discharge"] = Decimal("1.5")
    loud = []
    for bad_recs, word in ((neg, "negative"), (big, "outside 0..1")):
        try:
            m.check_period(bad_recs, P1, N, ())
            loud.append(False)
        except ValueError as e:
            loud.append(word in str(e))
    if not all(loud):
        return report(5, name, False, f"check_period accepts a bad month "
                      f"{loud}")
    real_gate(cur, 5, name, real_no_negative)


# ---------------------------------------------------------------- gate 6

def _null_zero_variants():
    """(label, column, area index, new value, base): per value column the
    base month holds NULL at area 0 and 0 at area 1; area 0 becomes 0 and
    area 1 becomes NULL, one cell at a time."""
    out = []
    for c in VALUE_COLS:
        zero = 0 if c in m.COUNT_COLUMNS else Decimal("0")
        out.append((f"{c} NULL->0", c, 0, zero))
        out.append((f"{c} 0->NULL", c, 1, None))
    return tuple(out)


VARIANTS = _null_zero_variants()


def null_zero_base(col):
    zero = 0 if col in m.COUNT_COLUMNS else Decimal("0")
    return recs(P1, over={(0, col): None, (1, col): zero})


def seeded_null_zero(cur):
    """Gate 6's scenario on throwaway tables through the loader's writer:
    each value column changed NULL to 0 and 0 to NULL, one cell at a time."""
    out = {}
    for label, col, area, new in VARIANTS:
        base = null_zero_base(col)

        def variant(cur, base=base, col=col, area=area, new=new):
            apply(cur, P1, base)
            clean = load_checks.check_latest_equals_live(cur, ZZ)
            changed = with_cell(base, area, col, new)
            kind = pe.classify_period(cur, prof(), P1, changed)
            apply(cur, P1, changed, link=rev(P1))
            flagged = load_checks.check_latest_equals_live(cur, ZZ)
            stored = editions_of(cur, P1)
            cur.execute(f"""SELECT {col} IS NULL, {col} FROM public.{ZE}
                            WHERE reporting_period = %s AND edition = 2
                              AND utla_code = %s""", (P1, CODES[area]))
            return clean, kind, flagged, stored, cur.fetchone()
        out[label] = _in_savepoint(cur, variant)
    return out


def variant_ok(res, new):
    clean, kind, flagged, stored, cell = res
    return (clean == [] and kind == "revised" and len(stored) == 2
            and cell == (new is None, new)
            and len(flagged) == 1 and "1 edition-only" in flagged[0])


def seeded_dash_through_loader(cur):
    """A file whose first area carries '-' in every value column, loaded
    through the loader: stored NULL in the editions and live tables, never
    0; a published 0 beside it stays 0."""
    def body(cur):
        with env(cur) as e:
            ov = {CODES[0]: {c: "-" for c in VALUE_COLS},
                  CODES[1]: {c: 0 for c in VALUE_COLS}}
            e.make(orig(P1), P1, overrides=ov)
            rc, text, _, _ = e.run(
                ["load", "--commit", "--min-period", P1,
                 "--expected-areas", str(N)], page=[orig(P1)])
            nulls, zeros = [], []
            for table, where in ((ZE, "edition = 1"), (ZL, "TRUE")):
                cols = ", ".join(
                    f"COUNT(*) FILTER (WHERE {c} IS NULL), "
                    f"COUNT(*) FILTER (WHERE {c} = 0)" for c in VALUE_COLS)
                cur.execute(f"SELECT {cols} FROM public.{table} WHERE "
                            f"{where} AND utla_code = %s", (CODES[0],))
                row = cur.fetchone()
                nulls.append(row[0::2])
                zeros.append(row[1::2])
                cur.execute(f"SELECT {cols} FROM public.{table} WHERE "
                            f"{where} AND utla_code = %s", (CODES[1],))
                row = cur.fetchone()
                zeros.append(row[1::2])
            return rc, nulls, zeros
    return _in_savepoint(cur, body)


def gate_6_null_vs_zero(cur):
    name = ("NULL versus 0 never conflated (every value column, both ways; "
            "'-' is NULL through the loader)")
    try:
        s = seeded_null_zero(cur)
        rc, nulls, zeros = seeded_dash_through_loader(cur)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(6, name, False, f"seeded scenario: {e}")
    bad = [f"{label}: {s[label][1]}, edition 2 holds {s[label][4]}"
           for label, _c, _a, new in VARIANTS
           if not variant_ok(s[label], new)]
    k = len(VALUE_COLS)
    dash_ok = (rc == 0 and all(n == (1,) * k for n in nulls)
               and zeros[0] == (0,) * k and zeros[2] == (0,) * k
               and zeros[1] == (1,) * k and zeros[3] == (1,) * k)
    if not dash_ok:
        bad.append(f"'-' through the loader: rc {rc}, NULL per column "
                   f"{nulls}, zeros {zeros}")
    notes = (f"{len(VARIANTS)} single-cell changes over {k} columns each "
             "stored as edition 2 with the cell read back exactly; '-' in "
             f"all {k} columns is NULL (not 0) in editions and live, a "
             "published 0 stays 0")
    if bad:
        return report(6, name, False, "seeded scenario: " + "; ".join(bad[:4]))
    if not table_exists(cur):
        return report(6, name, False, f"{NOT_YET}; seeded scenario passes: "
                      + notes, pending=True)
    real_gate(cur, 6, name, lambda c: (lambda r: (r[0], f"{r[1]}; seeded "
              f"scenario: {notes}"))(real_null_vs_zero(c)))


def gate_7_total_discharges(cur):
    name = ("total_discharges is NULL everywhere (documented); a later file "
            "that fills it is listed, not failed")
    ov = {CODES[0]: {"total_discharges": 5000}}
    with tempfile.TemporaryDirectory() as tmp:
        e = Env(cur, tmp)
        path = e.make(orig(P1), P1, overrides=ov)
        rows, _ = m.parse_workbook(path)
    filled = {r["utla_code"]: r["total_discharges"] for r in rows}
    seeded = (filled[CODES[0]] == 5000 and filled[CODES[1]] is None)
    if not seeded:
        return report(7, name, False, f"seeded parse wrong: {filled}")
    notes = ("seeded: '-' parses to NULL and a filled value to a whole "
             "number (so a later file filling it is stored, not refused)")
    if not table_exists(cur):
        return report(7, name, False, f"{NOT_YET}; {notes}", pending=True)
    real_gate(cur, 7, name, lambda c: (lambda r: (r[0], f"{r[1]}; {notes}"))(
        real_total_discharges(c)))


# ------------------------------------------------------------ seeded gates

def gate_8_seeded_revision(cur):
    name = ("seeded revision: edition 2 with one changed cell, refresh_latest "
            "writes only that month's changed value, sets the source of the whole "
            "month, keeps loaded_at")

    def snapshot(cur, period):
        cur.execute(f"""SELECT utla_code, utla_name, {', '.join(VALUE_COLS)},
                               source, loaded_at
                        FROM public.{ZL} WHERE reporting_period = %s
                        ORDER BY 1""", (period,))
        return cur.fetchall()

    def body(cur):
        ra, rb = recs(P1), recs(P2)
        for p, rr in ((P1, ra), (P2, rb)):
            apply(cur, p, rr)
        before_ok = load_checks.check_latest_equals_live(cur, ZZ) == []
        changed = with_cell(ra, 5, "total_bed_days_lost",
                            ra[5]["total_bed_days_lost"] + 1000)
        kind = apply(cur, P1, changed, link=rev(P1))
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
    iv, isrc, ilo = (2 + VALUE_COLS.index("total_bed_days_lost"),
                     2 + len(VALUE_COLS), 3 + len(VALUE_COLS))
    # source is set on EVERY row of the refreshed month; the value on one
    all_src = (len(diff) == N
               and all(y[isrc] == rev(P1) and x[isrc] == orig(P1)
                       for x, y in diff))
    valued = [(x, y) for x, y in diff if x[iv] != y[iv]]
    one = len(valued) == 1
    only = all_src and all(
        all(x[i] == y[i] for i in range(len(x)) if i not in (iv, isrc))
        for x, y in diff)
    ok = (before_ok and kind == "revised" and plan == {P1: 1}
          and updated == {P1: 1} and res["rows"] == 1 and p2_same and one
          and only and all(x[ilo] == y[ilo] for x, y in diff)
          and [e[0] for e in b_eds] == [1] and after == [])
    report(8, name, ok, f"edition 2 stored ({kind}); plan {plan}; updated "
           f"{updated} ({res['rows']} row); {P2} untouched={p2_same}; live "
           f"rows changed in {P1}: {len(diff)} (source on all, value on "
           f"{len(valued)}; only total_bed_days_lost and source, loaded_at "
           f"kept)={only}; "
           f"latest equals live after={after == []}")


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


def gate_10_parse_rules(cur):
    name = ("parse rules: '-' NULL, #N/A and NULL rows dropped and counted "
            "(never stored as 0), a published 0 stays 0, unknown text, a "
            "fractional count, a Welsh code and a shifted header halt")
    zeros = {c: 0 for c in m.COUNT_COLUMNS}
    zeros.update({c: 0.0 for c in m.RATIO_COLUMNS})
    rows = [data_row("E06000001", "A", {"discharged_14_20_days": 0,
                                        "avg_days_drd_to_discharge_exc_zero":
                                        "-", "pct_same_day_discharge": 0.1}),
            data_row("#N/A", "gone", zeros), data_row("NULL", "gone", zeros),
            data_row("NULL", "gone2", zeros),
            data_row(COUNTY, "County", {"total_bed_days_lost": "-"}),
            data_row("E08000019", "Sheffield")]
    d = date(2026, 4, 1)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        got, dropped = m.parse_workbook(write_workbook(t / "a.xlsx", d, rows))
        recoded = m.build_records(got, {}, "2026-04-01")

        def bad_file(label, rows_, **kw):
            return _raises(lambda: m.parse_workbook(
                write_workbook(t / f"{label}.xlsx", d, rows_, **kw)),
                ValueError)
        unknown = bad_file("u", [data_row("E06000001", "A",
                                          {"total_bed_days_lost": "n/a"})])
        frac = bad_file("f", [data_row("E06000001", "A",
                                       {"total_bed_days_lost": 1.5})])
        welsh = bad_file("w", [data_row("W06000001", "Wales")])
        hdr = header_row()
        hdr[m.COLUMNS[5][1]] = "something else"
        shifted = bad_file("h", [data_row("E06000001", "A")], header=hdr)
        dup = _raises(lambda: m.build_records(
            got, {"E06000001": "E08000019"}, "2026-04-01"), ValueError)
    by = {r["utla_code"]: r for r in recoded}
    a = by["E06000001"]
    ok = (dropped == {"#N/A": 1, "NULL": 2}
          and sorted(by) == ["E06000001", "E08000019", COUNTY]
          and not any(r["utla_code"] in ("#N/A", "NULL") for r in recoded)
          and a["total_discharges"] is None            # '-' is NULL
          and a["avg_days_drd_to_discharge_exc_zero"] is None
          and a["discharged_14_20_days"] == 0          # a published 0 stays 0
          and a["discharged_14_20_days"] is not None
          and a["pct_same_day_discharge"] == Decimal("0.1")
          and isinstance(a["total_bed_days_lost"], int)
          and by[COUNTY]["total_bed_days_lost"] is None
          and unknown[0] and "unknown marker" in unknown[1]
          and frac[0] and "whole number" in frac[1]
          and welsh[0] and "W06000001" in welsh[1]
          and shifted[0] and "header changed" in shifted[1]
          and dup[0] and "duplicate" in dup[1])
    report(10, name, ok, f"dropped {dropped}, 3 rows kept; '-' -> NULL; "
           "0 stays 0; floats as Decimal(repr(v)); unknown marker "
           f"halts={unknown[0]}, fraction halts={frac[0]}, W code "
           f"halts={welsh[0]}, shifted header halts={shifted[0]}, "
           f"duplicate refused={dup[0]}")


def gate_11_planner(cur):
    name = ("planner: window, ledger skip, --recheck-all, never a month "
            "before the earliest held")
    periods = [f"2026-{mm:02d}-01" for mm in range(1, 9)]
    bad, runs = [], 0
    for a_end in range(1, len(periods) + 1):
        avail = periods[:a_end]
        for h_start in range(0, len(periods), 2):
            for h_end in range(h_start, len(periods), 3):
                held = periods[h_start:h_end + 1]
                first = min(held)
                page = {p: f"L-{p}" for p in avail}
                inv = [p for p in held if p in page]
                # per held month on the page: its link is the tip's file (0),
                # differs and is unchecked (1) or differs and is checked (2)
                for states in itertools.product((0, 1, 2),
                                                repeat=min(len(inv), 3)):
                    st = dict(zip(inv, states))
                    tips = {p: (f"L-{p}" if st.get(p, 0) == 0
                                else f"old-{p}") for p in held}
                    checked = {p: {f"L-{p}"} for p in inv
                               if st.get(p) == 2}
                    for ra, rn in ((False, ()), (True, ()),
                                   (False, (held[0],))):
                        new, rc, earlier = m.plan_months(
                            held, page, tips, checked, recheck_all=ra,
                            recheck=rn)
                        runs += 1
                        want_rc = {p for p in inv
                                   if ra or p in rn or st.get(p, 0) == 1}
                        if (set(new) & set(held) or set(new) & set(rc)
                                or new != sorted(new) or rc != sorted(rc)
                                or any(x <= first for x in new)
                                or set(rc) != want_rc
                                or earlier != [x for x in avail
                                               if x not in held
                                               and x < first]
                                or any(x < first for x in set(new) | set(rc))):
                            bad.append((held[:1], avail[-1:], states, ra))
                        nw, rw, ew = m.plan_months(
                            held, page, tips, checked, recheck_all=ra,
                            recheck=rn, min_period=periods[0])
                        if (ew or set(nw) != {x for x in avail
                                              if x not in held}
                                or set(rw) != want_rc):
                            bad.append(("widen", held[:1], avail[-1:],
                                        states, ra))
    nothing = m.plan_months([], {p: "x" for p in periods}, {}, {})
    ok = not bad and nothing == ([], [], periods)
    report(11, name, ok, f"{runs} held/page/ledger/recheck combinations "
           f"checked (and the --min-period widening); nothing held -> new "
           f"{nothing[0]}, earlier {len(nothing[2])}; breaches {bad[:2]}")


def gate_12_preview_default(cur):
    name = ("preview default: the load command with no flag issues no commit "
            "and stores nothing")

    def body(e, page):
        e.make(orig(P4), P4)
        e.make(rev(P2), P2, bump={CODES[0]: 7})
        pg = [orig(P1), rev(P2), orig(P3), orig(P4)]
        base = state(e.cur)
        out = {}
        for word, flags in (("PREVIEW", []), ("SIMULATION", ["--simulate"])):
            rc, text, conn, logged = e.run(["load"] + flags, page=pg)
            out[word] = (rc, text, conn.commits, conn.cur.connection.commits,
                         logged.called, state(e.cur) == base,
                         editions_of(e.cur, P4), live_rows(e.cur, P4))
        rc, text, conn, logged = e.run(["load", "--commit"], page=pg)
        out["COMMIT"] = (rc, text, conn.commits, conn.cur.connection.commits,
                         logged.called, state(e.cur) == base,
                         editions_of(e.cur, P4), editions_of(e.cur, P2),
                         live_rows(e.cur, P4))
        return out
    try:
        r = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(12, name, False, str(ex).splitlines()[0])
    ok = True
    for word in ("PREVIEW", "SIMULATION"):
        rc, text, c1, c2, logged, same, e4, l4 = r[word]
        ok = ok and (rc == 0 and c1 == 0 and c2 == 0 and not logged and same
                     and e4 == [] and l4 == 0 and word in text)
    rc, text, c1, c2, logged, same, e4, e2, l4 = r["COMMIT"]
    ok = ok and (rc == 0 and c2 >= 1 and logged and not same
                 and e4 == [(1, None, N)] and l4 == N
                 and e2 == [(1, None, N), (2, 1, N)])
    pv, cm = r["PREVIEW"], r["COMMIT"]
    report(12, name, ok, f"no flag: commits {pv[3]}, run log {pv[4]}, tables "
           f"unchanged={pv[5]}; --simulate: commits {r['SIMULATION'][3]}, "
           f"tables unchanged={r['SIMULATION'][5]}; control --commit: commits "
           f"{cm[3]}, run log {cm[4]}, new month {cm[6]}")


# ------------------------------------------------------- secret/network gate

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

    def get(self, link, headers=None, timeout=None, **kw):
        self.calls.append(link)
        for prefix, body in self.pages.items():
            if link.startswith(prefix):
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
    files = [Path(__file__).resolve(), HERE / "s9a_drd_editions.py",
             HERE / "period_editions.py", HERE / "geography.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")

    def fetch_stub(e):
        link = orig(P1)
        e.make(link, P1)
        body = e.files[link].read_bytes()
        s = _Session({m.PAGE: html_of(link),
                      link: body})
        with _quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 1000):
            html = m.fetch_page(session=s)
            path = m.fetch_month(link, Path(e.tmp) / "raw", session=s)
        return html, path, s.calls

    def body(cur):
        with env(cur) as e:
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.seed()
                e.make(orig(P4), P4)
                rc, text, _, _ = e.run(
                    ["load", "--commit"],
                    page=[orig(P1), orig(P2), orig(P3), orig(P4)])
                html, path, calls = fetch_stub(e)
            return rc, html, path, calls
    try:
        rc, html, path, calls = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(13, name, False, f"{type(ex).__name__}: {ex}")
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    literal = [n for n, t in texts.items() if pat.search(t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    ok = (not attempts and rc == 0 and path.name == m._name(orig(P1))
          and all(u.startswith("https://") for u in calls) and len(calls) == 2
          and "webfile" in html and not literal and not in_src
          and not in_out)
    report(13, name, ok, f"socket attempts {len(attempts)}; load through "
           f"stubs returned {rc}; fetch through a stub session requested "
           f"{len(calls)} URLs; secret-like literal in source "
           f"{literal or 'none'}; {len(secrets)} secret-named settings "
           f"checked against {len(files)} files and {len(OUTPUT)} output "
           f"lines: in source {in_src or 'none'}, in output "
           f"{in_out or 'none'}")


# ---------------------------------------------------------- gates 14 and 15

def gate_14_revert_new_edition(cur):
    name = "revert stored as a new edition"
    a = recs(P3)
    b = with_cell(a, 3, "total_bed_days_lost", a[3]["total_bed_days_lost"] + 7)

    def body(cur):
        seq = [apply(cur, P3, x, link=lk) for x, lk in (
            (a, orig(P3)), (b, rev(P3)), (a, link_of(P3, True) + "?r=2"))]
        after_aba = editions_of(cur, P3)
        again = apply(cur, P3, a)
        after_abaa = editions_of(cur, P3)
        return (seq, after_aba, again, after_abaa, core.chain_tip(cur, ZZ, P3),
                [o for _, o, _ in ledger_of(cur, P3)])
    try:
        seq, after_aba, again, after_abaa, tip, outcomes = _in_savepoint(
            cur, body)
    except (psycopg2.Error, SystemExit) as e:
        return report(14, name, False, str(e))
    want = [(1, None, N), (2, 1, N), (3, 2, N)]
    ok = (seq == ["new", "revised", "revised"] and after_aba == want
          and again == "unchanged" and after_abaa == want and tip == 3
          and outcomes == ["new", "revised", "revised", "unchanged"])
    report(14, name, ok, f"A,B,A -> {seq}, editions (number, supersedes) "
           f"{[(e, s) for e, s, _ in after_aba]}; A again -> {again}, "
           f"editions still {[e for e, _, _ in after_abaa]}, tip {tip}; "
           f"ledger {outcomes}")


def gate_15_new_month_reaches_live(cur):
    name = ("new month reaches live (edition 1, live rows and ledger row in "
            "one transaction; live source is the file URL)")
    seen = {}

    def stored(e, page):
        e.make(orig(P4), P4)
        pg = page + [orig(P4)]
        rc, text, _, logged = e.run(["load", "--commit"], page=pg)
        cur = e.cur
        cur.execute(f"SELECT COUNT(*), COUNT(loaded_at), COUNT(*) FILTER "
                    f"(WHERE source = %s) FROM public.{ZL} WHERE "
                    "reporting_period = %s", (orig(P4), P4))
        live_n, stamped, labelled = cur.fetchone()
        return (rc, editions_of(cur, P4), live_n, stamped, labelled,
                core.rows_differing(cur, ZZ, P4, 1), ledger_of(cur, P4),
                logged.called, m.status(cur, ZZ))

    def failed(e, page):
        e.make(orig(P4), P4)
        real_insert = m.insert_live

        def boom(c, profile, period, records):
            seen["editions_when_live_failed"] = sum(
                r[2] for r in editions_of(c, period))
            real_insert(c, profile, period, records[:10])
            raise RuntimeError("live insert failed (planted)")
        with mock.patch.object(m, "insert_live", side_effect=boom):
            rc, text, _, logged = e.run(["load", "--commit"],
                                        page=page + [orig(P4)])
        return (rc, editions_of(e.cur, P4), ledger_of(e.cur, P4),
                live_rows(e.cur, P4), logged.called)

    def preview(e, page):
        e.make(orig(P4), P4)
        rc, text, _, _ = e.run(["load"], page=page + [orig(P4)])
        return (rc, text, editions_of(e.cur, P4), ledger_of(e.cur, P4),
                live_rows(e.cur, P4))
    try:
        (rc, eds, live_n, stamped, labelled, diff, led, logged,
         st) = scenario(cur, stored)
        f_rc, f_eds, f_led, f_live, f_logged = scenario(cur, failed)
        p_rc, p_out, p_eds, p_led, p_live = scenario(cur, preview)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(15, name, False, str(e).splitlines()[0])
    ok = (rc == 0 and eds == [(1, None, N)] and live_n == N
          and stamped == N and labelled == N and diff == 0
          and led == [(m._name(orig(P4)), "new", 1)] and logged
          and st["live_missing"] == []
          and f_rc == 1 and seen.get("editions_when_live_failed") == N
          and f_eds == [] and f_led == [] and f_live == 0 and not f_logged
          and p_rc == 0 and p_eds == [] and p_led == [] and p_live == 0
          and f"would store edition 1 and insert {N} live rows" in p_out)
    report(15, name, ok, f"editions {[(e, s) for e, s, _ in eds]}, live "
           f"rows {live_n} (loaded_at set {stamped}, source = the file URL "
           f"on {labelled}), cells differing {diff}, ledger {led}; planted "
           f"live failure: rc {f_rc}, edition rows written first="
           f"{seen.get('editions_when_live_failed') == N}, afterwards "
           f"editions {f_eds} ledger {f_led} live {f_live}; preview writes "
           f"editions {p_eds} ledger {p_led} live {p_live}")


# ------------------------------------------------------------------- gate 16

def gate_16_file_identity(cur):
    name = ("file identity: link month, Cover Sheet and UTLA sheet Period:, "
            "Revised: against the -Revised name, and the title must agree, "
            "caught before any write")

    def body(e, page):
        d = date(2026, 7, 1)
        rows = [data_row(c, f"Area {c}") for c in CODES]
        tmp = e.tmp

        def wb(link, **kw):
            p = tmp / f"id{len(list((tmp).glob('id*')))}"
            p.mkdir()
            return write_workbook(p / m._name(link),
                                  kw.pop("period", d), rows, **kw)
        cases = {
            "the Cover Period: is another month": wb(
                orig(P4), period=date(2026, 6, 1)),
            "the UTLA sheet Period: is another month": wb(
                orig(P4), utla_period=date(2026, 6, 1)),
            "Revised: is a date but the name is not -Revised": wb(
                orig(P4), revised="9th July 2026"),
            "the name says -Revised but there is no Revised: date": wb(
                rev(P4)),
            "the title is not the DRD title": wb(orig(P4), title="Other"),
            "an unrecognisable file name (no month)": wb(
                url(2026, 9, "prices"), ),
        }
        out = {}
        for label, f in cases.items():
            with mock.patch.object(m, "apply_period",
                                   side_effect=AssertionError("wrote")), \
                    mock.patch.object(pe, "load_periods") as lp:
                rc, text, conn, logged = e.run(["load", "--commit"], file=f)
            out[label] = (rc, text, lp.called, conn.cur.connection.commits,
                          logged.called)
        good = e.make(orig(P4), P4)
        rc, text, _, _ = e.run(["load"], file=good)
        return out, rc, editions_of(e.cur, P4), live_rows(e.cur, P4)
    try:
        out, good_rc, e4, l4 = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(16, name, False, str(ex).splitlines()[0])
    good = {"period": date(2026, 7, 1), "utla_period": date(2026, 7, 1),
            "revised": None, "utla_revised": None,
            "title": m.DRD_TITLE}
    unit = m.check_identity(P4, good, "x-July-2026.xlsx")
    unit_bad = [m.check_identity(P3, good, "x-July-2026.xlsx"),
                m.check_identity(P4, dict(good, utla_period=date(2026, 6, 1)),
                                 "x-July-2026.xlsx"),
                m.check_identity(P4, good, "x-July-2026-Revised.xlsx"),
                m.check_identity(P4, dict(good, revised=date(2026, 7, 9)),
                                 "x-July-2026.xlsx"),
                m.check_identity(P4, dict(good, title="Other"),
                                 "x-July-2026.xlsx")]
    caught = {k: v[0] == "halt" and not v[2] and v[3] == 0 and not v[4]
              for k, v in out.items()}
    ok = (all(caught.values()) and e4 == [] and l4 == 0
          and good_rc == 0 and unit == [] and all(len(u) >= 1
                                                  for u in unit_bad)
          and all("identity" in v[1] or "no month" in v[1]
                  for v in out.values()))
    report(16, name, ok, "; ".join(
        f"{k}: {'halted before any write' if v else 'NOT CAUGHT'}"
        for k, v in caught.items())
        + f"; a matching file previews (rc {good_rc}); nothing stored")


# ------------------------------------------------------------------- gate 17

def gate_17_older_file_guard(cur):
    name = ("older-file guard: an older file or an earlier page halts unless "
            "--allow-older-file, before anything is downloaded")

    def body(e, page):
        # P2 is held from the -Revised file; the page offers the original
        e.make(orig(P2), P2)
        old_page = [orig(P1), orig(P2), orig(P3)]
        before = state(e.cur)
        halts = {}
        for argv in (["load"], ["load", "--commit"], ["load", "--simulate"]):
            rc, text, _, logged = e.run(argv, page=old_page)
            halts[" ".join(argv)] = (
                rc == "halt" and "latest file" in text
                and "--allow-older-file" in text and e.fetched == []
                and not logged.called)
        unchanged = state(e.cur) == before
        # --file: the same guard, rank read from the file itself
        f_halts = {}
        for argv in (["load"], ["load", "--commit"], ["load", "--simulate"]):
            rc, text, _, logged = e.run(argv, file=e.files[orig(P2)])
            f_halts[" ".join(argv)] = (
                rc == "halt" and "the file given is" in text
                and "--allow-older-file" in text and not logged.called)
        f_unchanged = state(e.cur) == before
        rc_f_allow, t_f_allow, _, _ = e.run(
            ["load", "--allow-older-file"], file=e.files[orig(P2)])
        rc_f_same, t_f_same, _, _ = e.run(["load"], file=e.files[rev(P2)])
        rc_allow, t_allow, _, _ = e.run(["load", "--allow-older-file"],
                                        page=old_page)
        rc_same, t_same, _, _ = e.run(["load", "--commit"],
                                      page=[orig(P1), rev(P2), orig(P3)])
        # the page's latest month is earlier than the held latest
        rc_early, t_early, _, _ = e.run(["load"], page=[orig(P1), rev(P2)])
        rc_early_ok, t_early_ok, _, _ = e.run(
            ["load", "--allow-older-file"], page=[orig(P1), rev(P2)])
        return (halts, unchanged, rc_allow, t_allow, rc_same, t_same,
                rc_early, t_early, rc_early_ok, t_early_ok,
                (f_halts, f_unchanged, rc_f_allow, t_f_allow, rc_f_same,
                 t_f_same))
    try:
        (halts, unchanged, rc_allow, t_allow, rc_same, t_same, rc_early,
         t_early, rc_early_ok, t_early_ok, fl) = scenario(
            cur, body, link=lambda p: rev(p) if p == P2 else orig(p))
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(17, name, False, str(ex).splitlines()[0])
    pure = (m.link_older(orig(P2), rev(P2)) and not m.link_older(rev(P2),
                                                                 orig(P2))
            and not m.link_older(rev(P2), rev(P2))
            and not m.link_older(orig(P1), None)
            and m.link_older(url(2025, 7, "April-2025-Revised"),
                             url(2026, 7, "April-2025-Revised")))
    f_halts, f_unchanged, rc_f_allow, t_f_allow, rc_f_same, t_f_same = fl
    file_ok = (all(f_halts.values()) and f_unchanged and rc_f_allow == 0
               and "--allow-older-file given" in t_f_allow
               and rc_f_same == 0 and "--allow-older-file" not in t_f_same)
    ok = (all(halts.values()) and unchanged and rc_allow == 0
          and "--allow-older-file given" in t_allow and rc_same == 0
          and "--allow-older-file" not in t_same
          and rc_early == "halt" and "2026-06 is already held" in t_early
          and rc_early_ok == 0 and "--allow-older-file given" in t_early_ok
          and file_ok and pure)
    report(17, name, ok, f"older --file halted in preview/commit/simulate "
           f"with nothing written={file_ok}; older link in preview/commit/simulate halted "
           f"before any download or write={all(halts.values())}; stores "
           f"unchanged={unchanged}; --allow-older-file proceeds ({rc_allow}); "
           f"the current link is unaffected ({rc_same}); an earlier page "
           f"halts={rc_early == 'halt'} and is overridden ({rc_early_ok}); "
           f"pure rule={pure}")


# ------------------------------------------------------------------- gate 18

def gate_18_month_checks(cur):
    name = ("month checks: short month, missing UTLA, extra areas, negative "
            "count, percentage above 1, NULL for a number above 5 areas, "
            "revision above 50% above 10 areas, 0/NULL needs --acknowledge")
    problems = []

    def run(label, setup, want_rc, want_text, check, flags=()):
        def one(e, page):
            pg = setup(e, page)
            with mock.patch.object(pe, "load_periods",
                                   wraps=pe.load_periods) as lp:
                rc, text, _, logged = e.run(["load", "--commit", *flags],
                                            page=pg)
            return rc, text, logged.called, lp.called, check(e.cur)
        try:
            rc, text, logged, called, extra = scenario(cur, one)
        except (psycopg2.Error, SystemExit, RuntimeError) as ex:
            problems.append(f"{label}: {ex}")
            return
        if rc != want_rc or (want_text and want_text not in text) or not extra:
            problems.append(f"{label}: rc {rc}, expected {want_rc} "
                            f"({want_text!r} in text: {want_text in text}), "
                            f"state ok={extra}")
        if want_rc == 1 and logged:
            problems.append(f"{label}: a run log row was written")
        if want_rc == "halt" and called:
            problems.append(f"{label}: the engine ran")

    def new_month(**kw):
        def setup(e, page):
            e.make(orig(P4), P4, **kw)
            return page + [orig(P4)]
        return setup

    def revised_p3(**kw):
        def setup(e, page):
            e.make(rev(P3), P3, **kw)
            return [orig(P1), orig(P2), rev(P3)]
        return setup

    nothing_p4 = lambda c: (editions_of(c, P4) == [] and live_rows(c, P4) == 0
                            and ledger_of(c, P4) == [])
    run("short month", new_month(codes=CODES[:-1]), 1,
        f"{P4}: REJECTED, not stored", nothing_p4)
    run("short month text", new_month(codes=CODES[:-1]), 1,
        f"{N - 1} areas, expected {N}", nothing_p4)
    run("UTLA swapped for another", new_month(codes=CODES[:-1] + [SPARE]), 1,
        CODES[-1], nothing_p4)
    run("more areas than held", new_month(codes=CODES + [SPARE]), 1,
        f"{N + 1} areas, expected {N}", nothing_p4)
    run("negative count", new_month(
        overrides={CODES[0]: {"total_bed_days_lost": -1}}), 1, "negative",
        nothing_p4)
    run("percentage above 1", new_month(
        overrides={CODES[0]: {"pct_same_day_discharge": 1.5}}), 1,
        "outside 0..1", nothing_p4)
    run("NULL for a number in 6 areas", revised_p3(
        overrides={c: {"total_bed_days_lost": "-"} for c in CODES[:6]}), 1,
        "NULL replaces a number in 6 areas",
        lambda c: editions_of(c, P3) == [(1, None, N)])
    run("NULL for a number in 5 areas (the limit)", revised_p3(
        overrides={c: {"total_bed_days_lost": "-"} for c in CODES[:5]}), 0,
        "", lambda c: editions_of(c, P3)[-1][0] == 2)
    run("bed days revised above 50% in 11 areas",
        revised_p3(bump={c: 1000 for c in CODES[:11]}), 1,
        "revised above 50% in 11 areas",
        lambda c: editions_of(c, P3) == [(1, None, N)])
    run("bed days revised above 50% in 10 areas (the limit)",
        revised_p3(bump={c: 1000 for c in CODES[:10]}), 0, "",
        lambda c: editions_of(c, P3)[-1][0] == 2)
    zero_null = {CODES[2]: {"discharged_14_20_days": "-"}}
    run("0 to NULL without --acknowledge", revised_p3(overrides=zero_null), 1,
        "0 to NULL", lambda c: editions_of(c, P3) == [(1, None, N)])
    run("0 to NULL with --acknowledge", revised_p3(overrides=zero_null), 0,
        "ACKNOWLEDGED", lambda c: editions_of(c, P3)[-1][0] == 2,
        flags=("--acknowledge", P3))
    report(18, name, not problems, "; ".join(problems[:3]) if problems else
           "12 scenarios: each rejected month stored nothing (no edition, "
           "live row or ledger row) and wrote no run log row, and the limits "
           "themselves (5 NULL areas, 10 revised areas) pass")


# ------------------------------------------------------------------- gate 19

def gate_19_barnsley_sheffield(cur):
    name = ("Barnsley and Sheffield through geography.resolve: a form that "
            "disagrees with the 'old' declaration for 9a halts before any "
            "write")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("none",))[0]

    def body(e, page):
        new_form = {"E08000016": "E08000038", "E08000019": "E08000039"}
        res = {}
        for label, codes in (
                ("the new form (E08000038/39)",
                 [new_form.get(c, c) for c in CODES]),
                ("one new code beside the old (E08000016 + E08000039)",
                 ["E08000039" if c == "E08000019" else c for c in CODES])):
            e.make(orig(P4), P4, codes=codes)
            with mock.patch.object(pe, "load_periods") as lp, \
                    mock.patch.object(m, "apply_period",
                                      side_effect=AssertionError("wrote")):
                rc, text, conn, logged = e.run(["load", "--commit"],
                                               page=page + [orig(P4)])
            res[label] = (rc, text, lp.called, conn.cur.connection.commits,
                          editions_of(e.cur, P4), live_rows(e.cur, P4),
                          ledger_of(e.cur, P4), logged.called)
        # the old form (the seed itself carries it) resolves cleanly
        rm, probs = geography.resolve(e.cur, m.RUN_SOURCE, set(CODES))
        return res, rm, probs
    try:
        res, recode_map, probs = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, ValueError) as ex:
        return report(19, name, False, str(ex).splitlines()[0])
    halted = {k: (v[0] == "halt" and "declared 'old'" in v[1]
                  and "E080000" in v[1] and not v[2] and v[3] == 0
                  and v[4] == [] and v[5] == 0 and v[6] == [] and not v[7])
              for k, v in res.items()}
    ok = (decl == "old" and all(halted.values()) and probs == []
          and all(k == v for k, v in recode_map.items()))
    report(19, name, ok, f"9a declared {decl!r}; "
           + "; ".join(f"{k}: halted before any write={v}"
                       for k, v in halted.items())
           + f"; the seeded old form (E08000016/19, in all 3 held months) "
           f"loaded and resolves with problems {probs}, no code changed (map {recode_map})")


# ------------------------------------------------------------------- gate 20

def gate_20_rerun_idempotent(cur):
    name = ("rerun idempotent: the same files twice store nothing and leave "
            "live untouched")

    def body(e, page):
        base = state(e.cur)
        runs = []
        for flags in ([], [], ["--recheck-all"], ["--recheck-all"]):
            rc, text, _, _ = e.run(["load", "--commit", *flags], page=page)
            cur2, eds, led = state(e.cur)
            runs.append((rc, (cur2, eds) == (base[0], base[1]),
                         "nothing to fetch" in text if not flags else
                         f"{P1}: unchanged" in text
                         and f"{P3}: unchanged" in text))
        return base, runs
    try:
        base, runs = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(20, name, False, str(ex).splitlines()[0])
    ok = all(r == (0, True, True) for r in runs) and base[1] == [
        (1, None, 3 * N)]
    report(20, name, ok, f"two plain reruns and two --recheck-all reruns: "
           f"{runs}; editions {base[1]}, live rows and loaded_at unchanged")


# ------------------------------------------------------------------- gate 21

def gate_21_stranded_repair(cur):
    name = ("stranded month repair: editions with no live rows get live rows "
            "from load, no new edition")

    def body(e, page):
        ok0 = m.status(e.cur, ZZ)["ok"]
        e.cur.execute(f"DELETE FROM public.{ZL} WHERE reporting_period = %s",
                      (P2,))
        st = m.status(e.cur, ZZ)
        rc_status, _, _, _ = e.run(["status"])
        rc, text, _, _ = e.run(["load", "--commit"], page=page)
        return (ok0, st, rc_status, rc, text, e.fetched[:], editions_of(
            e.cur, P2), live_rows(e.cur, P2), live_sources(e.cur, P2),
            ledger_of(e.cur, P2), m.status(e.cur, ZZ))
    try:
        ok0, st, rc_status, rc, text, fetched, eds, lv, src, led, after = (
            scenario(cur, body))
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(21, name, False, str(ex).splitlines()[0])
    ok = (ok0 and st["live_missing"] == [P2] and not st["ok"]
          and rc_status == 1 and rc == 0 and "no new edition" in text
          and fetched == [orig(P2)] and eds == [(1, None, N)] and lv == N
          and src == [(orig(P2),)] and led[-1][1] == "live-missing"
          and after["live_missing"] == [])
    report(21, name, ok, f"status flagged {st['live_missing']} (exit "
           f"{rc_status}); load rc {rc}, fetched only that month; editions "
           f"{eds} (no new edition), live rows {lv}, live source {src}, "
           f"ledger {led[-1:]}; live_missing afterwards "
           f"{after['live_missing']}")


# ------------------------------------------------------------------- gate 22

def gate_22_ledger(cur):
    name = ("file-check ledger: an unchanged reissue is recorded once and "
            "skipped on the next run; --recheck-all reads it again")

    def body(e, page):
        e.make(rev(P2), P2)                   # identical figures, new file
        pg = [orig(P1), rev(P2), orig(P3)]
        rc1, t1, _, _ = e.run(["load", "--commit"], page=pg)
        f1 = e.fetched[:]
        e1, l1 = editions_of(e.cur, P2), ledger_of(e.cur, P2)
        e.fetched.clear()
        rc2, t2, _, _ = e.run(["load", "--commit"], page=pg)
        f2 = e.fetched[:]
        l2 = ledger_of(e.cur, P2)
        e.fetched.clear()
        rc3, t3, _, _ = e.run(["load", "--commit", "--recheck-all"], page=pg)
        f3 = sorted(e.fetched)
        # the ledger as the planner reads it
        checked = pe.checked_files(e.cur, m._profile(ZZ))
        return (rc1, t1, f1, e1, l1, rc2, t2, f2, l2, rc3, f3,
                editions_of(e.cur), checked)
    try:
        (rc1, t1, f1, e1, l1, rc2, t2, f2, l2, rc3, f3, eds_all,
         checked) = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(22, name, False, str(ex).splitlines()[0])
    want = [(m._name(orig(P2)), "new", 1), (m._name(rev(P2)), "unchanged", 1)]
    ok = (rc1 == 0 and f1 == [rev(P2)] and f"{P2}: unchanged" in t1
          and e1 == [(1, None, N)] and l1 == want
          and rc2 == 0 and f2 == [] and "nothing to fetch" in t2 and l2 == want
          and rc3 == 0 and f3 == sorted([orig(P1), rev(P2), orig(P3)])
          and eds_all == [(1, None, 3 * N)]
          and rev(P2) in checked.get(P2, set()))
    report(22, name, ok, f"reissue fetched once ({len(f1)}), recorded "
           f"'unchanged' once ({l1[-1:]}), next run fetched {len(f2)} "
           f"('nothing to fetch'={'nothing to fetch' in t2}), ledger still "
           f"{len(l2)} rows for the month; --recheck-all fetched {len(f3)}; "
           f"editions {eds_all}")


# ------------------------------------------------------------------- gate 23

def gate_23_precision_only(cur):
    name = ("precision-only changes (below 1e-8) are stored, labelled and "
            "reported apart, and never trip a stop")
    five = ("pct_same_day_discharge", "pct_delayed_1plus_days",
            "pct_acceptable_trust_coverage",
            "avg_days_drd_to_discharge_inc_zero",
            "avg_days_drd_to_discharge_exc_zero")
    base = {"pct_same_day_discharge": 0.9, "pct_delayed_1plus_days": 0.1,
            "pct_acceptable_trust_coverage": 0.5,
            "avg_days_drd_to_discharge_inc_zero": 1.25,
            "avg_days_drd_to_discharge_exc_zero": 2.5}

    def body(e, page):
        ov = {c: {k: v + 1e-9 for k, v in base.items()} for c in CODES}
        e.make(rev(P3), P3, overrides=ov)
        rc, text, _, _ = e.run(["load", "--commit"],
                               page=[orig(P1), orig(P2), rev(P3)])
        e.cur.execute(f"SELECT release_label FROM public.{ZE} WHERE "
                      "reporting_period = %s AND edition = 2 LIMIT 1", (P3,))
        lab = e.cur.fetchone()
        # the same month moved by 2e-8 in one cell is a real change
        ov2 = {CODES[0]: {"pct_same_day_discharge": 0.9 + 2e-8}}
        e.make(rev(P2), P2, overrides=ov2)
        rc2, text2, _, _ = e.run(["load"], page=[orig(P1), rev(P2),
                                                 rev(P3)])
        return rc, text, lab, editions_of(e.cur, P3), rc2, text2
    try:
        rc, text, lab, eds, rc2, text2 = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(23, name, False, str(ex).splitlines()[0])
    pure = (m.precision_only(Decimal("0.1"), Decimal("0.100000001"))
            and not m.precision_only(Decimal("0.1"), Decimal("0.10000002"))
            and not m.precision_only(None, Decimal("0.1"))
            and not m.precision_only(Decimal("0.1"), None)
            and not m.precision_only(Decimal("0"), None))
    ok = (rc == 0 and f"{P3}: revised" in text
          and "precision-only (below 1e-8, max" in text
          and f"{N * len(five)}" in text
          and "cells (compared with the latest editions): 0 changed" in text
          and "REJECTED" not in text and "STOP CONDITION" not in text
          and eds == [(1, None, N), (2, 1, N)]
          and lab and "precision-only" in lab[0]
          and rc2 == 0 and f"{P2}: revised, 1 areas changed" in text2
          and pure)
    report(23, name, ok, f"{N} areas x {len(five)} columns moved by 1e-9: "
           f"rc {rc}, stored as edition 2 ({[(a, b) for a, b, _ in eds]}), "
           "release label says precision-only, summary reports them apart "
           "with 0 changed cells and no stop; a 2e-8 move counts as a real "
           f"change ({f'{P2}: revised, 1 areas changed' in text2}); pure "
           f"rule={pure}")


# ------------------------------------------------------------------- gate 24

def gate_24_on_disk(cur):
    real_gate(cur, 24, "on-disk reproduction: every webfile in "
              "data/raw/s9a_drd/ that is a held tip's file parses to that "
              "tip exactly", real_on_disk)


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1,
         gate_3_latest_equals_live, gate_4_coverage, gate_5_negative,
         gate_6_null_vs_zero, gate_7_total_discharges,
         gate_8_seeded_revision, gate_9_fork_and_second_root,
         gate_10_parse_rules, gate_11_planner, gate_12_preview_default,
         gate_13_no_network_no_secrets, gate_14_revert_new_edition,
         gate_15_new_month_reaches_live, gate_16_file_identity,
         gate_17_older_file_guard, gate_18_month_checks,
         gate_19_barnsley_sheffield, gate_20_rerun_idempotent,
         gate_21_stranded_repair, gate_22_ledger, gate_23_precision_only,
         gate_24_on_disk)


def run_gate(cur, gate):
    """One gate in its own savepoint. A gate that raises (the loader halts
    inside a seeded scenario, a database error) is a FAIL of that gate, not
    the end of the run."""
    n = int(re.match(r"gate_(\d+)_", gate.__name__).group(1))
    seen = len(RESULTS)
    cur.execute("SAVEPOINT verify_gate")
    try:
        gate(cur)
        cur.execute("RELEASE SAVEPOINT verify_gate")
    except (Exception, SystemExit) as e:  # noqa: BLE001
        cur.execute("ROLLBACK TO SAVEPOINT verify_gate")
        cur.execute("RELEASE SAVEPOINT verify_gate")
        if len(RESULTS) == seen:
            report(n, gate.__name__, False,
                   f"gate raised {type(e).__name__}: "
                   f"{str(e).splitlines()[0] if str(e) else ''}")


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            setup_throwaway(cur)
            for gate in GATES:
                run_gate(cur, gate)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
