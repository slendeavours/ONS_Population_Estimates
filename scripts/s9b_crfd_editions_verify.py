"""Gates for the S9b (NHS England MHSDS MHS26, delayed discharge by local
authority) edition-history table.

Mirrors scripts/s9a_drd_editions_verify.py. Prints `GATE n name: PASS|FAIL`;
exits 1 on any FAIL. Gates 2 to 7 and 24 read the real tables (nhs_mh_crfd,
nhs_mh_crfd_editions and its file-check ledger) and gate 6 adds real data to
its seeded scenario. Until the editions table exists they print
`FAIL (pending load)` with the words "not yet migrated", so the script exits
non-zero until then, like the S1, S1b, S8b, S9a, S15, S18 and S19 verifiers;
nothing is created to make them pass. The seeded gates never need the real
editions table: they run on throwaway tables zz_s9b_live, zz_s9b_editions and
zz_s9b_editions_file_checks (a copy of the loader's SPEC named zz_s9b),
created inside the transaction, and call the loader's own writer and commands
(s9b_crfd_editions.apply_period and insert_live, pe.load_periods,
core.refresh_latest, and main/cmd_load with SPEC pointed at the copy and the
publisher's pages and downloads stubbed; the data files are built here as
zips and CSVs) rather than a re-implementation. The real tables are only
ever read.

Gates:
  1  table and file-check ledger shape, append-only triggers (UPDATE, DELETE,
     TRUNCATE blocked on both), on the throwaway copy; also the shape of the
     real tables once they exist (with the la_boundaries key)       (real)
  2  edition 1 present for every live month; live source names a file of the
     month                                                          (real)
  3  latest edition equals live (values and labels); live source is ONE
     value per month and is the tip's source_file (refresh sets the source of
     the whole month, so a mixed month fails)                       (real)
  4  coverage: the held area count (296) in every edition of every month,
     every code in la_boundaries                                    (real)
  5  no negative measure_value                                      (real)
  6  NULL versus 0 never conflated, both ways (seeded); '*' and a blank in a
     file are stored NULL, never 0, through the loader      (real data too)
  7  suppression listed per month: NULL counts equal in live and the latest
     edition, no zero value held (a suppressed value is NULL, never 0)(real)
  8  seeded revision: refresh_latest changes only that month's changed rows
     and moves the source of the whole month
  9  seeded fork and second root refused (chain_tip, and the loader's compare)
  10 parse rules: breakdown exact, SECONDARY_LEVEL NONE, E10 and UNKNOWN
     dropped and counted, markers NULL (a published 0 stays 0), unknown text,
     a Welsh code, a missing column halt; large files streamed (a zip is
     never read whole, memory stays flat, a download is chunked)
  11 planner: window, ledger skip, --recheck-all, never a month before the
     earliest held; Final preferred over Performance, then the version
  12 preview default: the load command with no flag issues no commit and
     stores nothing
  13 no network (a socket attempt fails the gate), no secret in source or
     output
  14 revert stored as a new edition (A, B, A -> edition 3; A again stores
     nothing)
  15 new month reaches live in one transaction (a failing live insert rolls
     the edition and the ledger row back; preview writes nothing)
  16 file identity: REPORTING_PERIOD_START (the three date forms, and the odd
     ones: 01/06/24, 1/6/2024) against the month, REPORTING_PERIOD_END its
     last day, STATUS (stripped) against the link's kind, the link's name
     against the month; caught before any write
  17 older-file guard: a Performance file after a Final, v1 after v2, or an
     earlier page halts unless --allow-older-file, before any download
  18 month checks: a short month, a swapped area, extra areas, a negative
     value, an unresolved code are rejected; NULL for a number above 5 areas,
     a revision above 50% in more than 10 areas (limits pass); 0/NULL change
     needs --acknowledge
  19 Barnsley and Sheffield (declared 'mixed'): the new codes resolve to the
     canonical ones, the old form loads, forms may differ between months, both
     forms of one area in one file halt before any write, no private recode
     dict in the loader (AST)
  20 rerun idempotent: the same files twice store nothing
  21 stranded month repair: a month with editions and no live rows gets its
     live rows from load, with no new edition
  22 file-check ledger: a v2 reissue with the same figures is recorded once as
     unchanged and skipped on the next run; an older link taken with
     --allow-older-file is recorded in the ledger; --recheck-all reads again
  23 transition-aware stop rules: a Performance-to-Final refresh changing 150
     areas is stored (not stopped); the same changes on a reissue stop; the
     national total moving over 50% stops a Final; 0/NULL always stops;
     --acknowledge releases
  24 on-disk reproduction: every data file in data/raw/s9b_mhsds/ whose name
     is a held tip's file parses to that tip exactly (read-only)    (real)

Usage:
    python scripts/s9b_crfd_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The commands get a stand-in
connection whose commit only counts. No network, no real-table writes, no
backend is ever terminated.
"""
import ast
import contextlib
import csv
import dataclasses
import io
import itertools
import os
import re
import socket
import sys
import tempfile
import tracemalloc
import zipfile
from contextlib import contextmanager
from datetime import date
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
import s9b_crfd_editions as m  # noqa: E402
from test_s9b_crfd_pure import (HEADER, MNAME, data_lines, final_name,  # noqa: E402
                                html_of, perf_name, url, write_data_file)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC, LIVE, TABLE = m.SPEC, m.LIVE, m.TABLE
LEDGER = pe.file_checks_table(m.PROFILE)
ZZ = dataclasses.replace(
    SPEC, name="zz_s9b", live_table="zz_s9b_live",
    editions_table="zz_s9b_editions", fk_la_boundaries=False)
ZL, ZE = ZZ.live_table, ZZ.editions_table
ZLEDGER = pe.file_checks_table(m._profile(ZZ))
EXPECTED_AREAS = 296                      # the held count of the live table
FETCHED = date(2026, 10, 9)
# 160 areas: the Performance-to-Final scenario changes 150 of them. 158
# English areas plus Barnsley and Sheffield (the old form, as the early files
# publish them).
CODES = [f"E06000{i:03d}" for i in range(1, 159)] + ["E08000016", "E08000019"]
N = len(CODES)
SPARE = "E06000159"
P1, P2, P3, P4 = "2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"
THIS_MONTH = "2026-05-01"
VALUE_COLS = m.VALUE_COLUMNS
NOT_YET = f"{TABLE} does not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (13)
_TAGS = {}


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

def link(name):
    """A stable files.digital.nhs.uk URL per file name (its own path)."""
    tag = _TAGS.setdefault(name, f"{len(_TAGS) % 100:02d}/{len(_TAGS):06X}")
    return url(name, tag)


def perf(p, v=1):
    return link(perf_name(p, v))


def final(p, v=1):
    return link(final_name(p, v))


def base_value(i, period):
    return 100 + 5 * i + int(period[5:7])


def recs(period, n=N, over=None, codes=CODES, source="gate"):
    """The month's records (n areas). over {(area index, column): value}
    (None allowed) replaces cells."""
    over = over or {}
    out = []
    for i in range(n):
        r = {"lad24cd": codes[i], "measure_id": m.MEASURE,
             "reporting_period": period, "la_name": f"Area {codes[i]}",
             "measure_name": MNAME, "measure_value": base_value(i, period),
             "source": source}
        for (a, col), v in over.items():
            if a == i:
                r[col] = v
        out.append(r)
    return out


def with_cell(records, area, col, new):
    return [dict(r, **{col: new}) if r["lad24cd"] == CODES[area] else r
            for r in records]


def setup_throwaway(cur):
    cur.execute("SELECT to_regclass('public.zz_s9b_live'), "
                "to_regclass('public.zz_s9b_editions'), "
                f"to_regclass('public.{ZLEDGER}')")
    if cur.fetchone() != (None, None, None):
        sys.exit("HARD STOP: a zz_s9b table already exists as a real table; "
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
    return [(m._name(s), o, e) for s, o, e in cur.fetchall()]


def live_rows(cur, period):
    cur.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE reporting_period = "
                "%s", (period,))
    return cur.fetchone()[0]


def live_vals(cur, period):
    cur.execute(f"SELECT lad24cd, measure_value FROM public.{ZL} WHERE "
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
    cur.execute(f"""INSERT INTO public.{ZE} (lad24cd, measure_id,
                    reporting_period, edition, measure_value, supersedes,
                    source_sha256) VALUES (%s, %s, %s, %s, 1, %s, %s)""",
                (CODES[0], m.MEASURE, period, edition, supersedes, sha))


def prof(expected=N, required=()):
    """The loader's own per-run profile on the throwaway spec."""
    return m.run_profile(ZZ, expected, required)


def apply(cur, period, records, link_=None, label="gate file", expected=N):
    """The loader's own writer for one month: s9b_crfd_editions.apply_period
    (edition rows, live rows through the loader's insert_live, then the
    file-check ledger row)."""
    link_ = link_ or perf(period)
    rr = [dict(r, source=link_) for r in records]
    info = m.FileInfo(link_, Path("gate.zip"), "ab" * 32, label)
    return m.apply_period(cur, prof(expected), period, rr,
                          fetched_on=FETCHED, info=info)


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
    the copy, a stand-in connection, the publication pages and downloads
    stubbed (the data files are built here), valid codes stubbed and the run
    log stubbed."""

    def __init__(self, cur, tmp):
        self.cur = cur
        self.tmp = Path(tmp)
        self.files = {}
        self.fetched = []
        self.n = 0

    def make(self, lnk, period, *, values=None, codes=None, status=None,
             transform=None, **kw):
        """Build the data file for `lnk` and register it for the download
        stub. values {code: value or None for '*'}; transform(i, code, v)."""
        vals = []
        for i, c in enumerate(codes or CODES):
            v = base_value(i, period)
            if transform:
                v = transform(i, c, v)
            v = (values or {}).get(c, v)
            vals.append((c, "*" if v is None else v))
        kind = m.classify_link(lnk)[0]
        self.n += 1
        d = self.tmp / str(self.n)
        d.mkdir()
        path = write_data_file(d / m._name(lnk), period, vals,
                               status=status or kind, **kw)
        self.files[lnk] = path
        return path

    def run(self, argv, *, page=(), table=True, file=None,
            this_month=THIS_MONTH):
        """Returns (rc or 'halt', text, connection stand-in, run-log mock)."""
        borrowed = _Borrowed(self.cur)
        args = list(argv)
        if file:
            args += ["--file", str(file)]
        pages = {}
        for lnk in page:
            pages.setdefault(m.period_from_link(lnk), []).append(lnk)

        def fetch_page(period, session=None):
            return html_of(*pages[period]) if period in pages else None

        def fetch_month(lnk, dest=None, session=None):
            self.fetched.append(lnk)
            return self.files[lnk]

        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "valid_codes", return_value=(
                    set(CODES) | {SPARE, "E08000038", "E08000039"})), \
                mock.patch.object(m, "fetch_page", side_effect=fetch_page), \
                mock.patch.object(m, "fetch_month", side_effect=fetch_month), \
                mock.patch.object(m, "_this_month", return_value=this_month), \
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

    def seed(self, periods=(P1, P2, P3), lnk=perf, values=None):
        page = []
        for p in periods:
            self.make(lnk(p), p, values=(values or {}).get(p))
            page.append(lnk(p))
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


def scenario(cur, fn, periods=(P1, P2, P3), lnk=perf, values=None):
    """Seed through the loader, run fn(env, page) and roll all of it back."""
    def body(c):
        with env(c) as e:
            page = e.seed(periods, lnk, values)
            return fn(e, page)
    return _in_savepoint(cur, body)


def final_like(i, c, v):
    """150 of 160 areas change: 20 by +60%, 130 by +5% (the national total
    moves about +10%): the publisher's year-end refresh."""
    if i < 20:
        return int(v * 1.6)
    if i < 150:
        return v + 5
    return v


# ---------------------------------------------------------------- gate 1

def shape_problems(cur, spec, ledger_table, expect_fk=False):
    """Problems with the editions table and its file-check ledger."""
    t = spec.editions_table
    cur.execute("""SELECT column_name, data_type, character_maximum_length,
                          numeric_precision, numeric_scale, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (t,))
    cols = {r[0]: r[1:] for r in cur.fetchall()}
    need = {*spec.key_cols, spec.period_col, "edition",
            *(c for c, _ in spec.value_cols), "la_name", "measure_name",
            "release_label", "published_date", "source_file",
            "source_sha256", "supersedes", "loaded_at"}
    bad = []
    if need - set(cols):
        bad.append(f"missing columns {sorted(need - set(cols))}")
    for c in ("lad24cd", "measure_id"):
        if cols.get(c, ("",))[0] != "character varying":
            bad.append(f"{c} is {cols.get(c)}")
        if cols.get(c, (0, 0, 0, 0, "YES"))[4] != "NO":
            bad.append(f"{c} is nullable")
    if cols.get(spec.period_col, ("",))[0] != "date":
        bad.append(f"{spec.period_col} is {cols.get(spec.period_col)}")
    for c, sql in spec.value_cols:
        got = cols.get(c, ("",))[0]
        want = "integer" if sql == "integer" else "numeric"
        if got != want:
            bad.append(f"{c} is {got}, expected {want}")
    if cols.get("la_name", ("",))[0] != "character varying":
        bad.append(f"la_name is {cols.get('la_name')}")
    if cols.get("measure_name", ("",))[0] != "text":
        bad.append(f"measure_name is {cols.get('measure_name')}")
    for c in ("edition", "supersedes"):
        if cols.get(c, ("",))[0] != "integer":
            bad.append(f"{c} is {cols.get(c)}")
    cur.execute("""SELECT a.attname FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid
                    AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = %s::regclass AND i.indisprimary""",
                (f"public.{t}",))
    if {r[0] for r in cur.fetchall()} != {"lad24cd", "measure_id",
                                          "reporting_period", "edition"}:
        bad.append("primary key is not (lad24cd, measure_id, "
                   "reporting_period, edition)")
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass AND contype = 'f'""",
                (f"public.{t}",))
    fks = " ".join(r[0] for r in cur.fetchall())
    if expect_fk and "la_boundaries" not in fks:
        bad.append("no lad24cd -> la_boundaries foreign key")
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
        cur, ZE, {"UPDATE": f"UPDATE public.{ZE} SET measure_value = 2",
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
        bad += [f"real: {p}" for p in shape_problems(cur, SPEC, LEDGER,
                                                     expect_fk=True)]
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


def _live_source(cur, spec, period):
    cur.execute(f"SELECT DISTINCT source FROM public.{spec.live_table} "
                f"WHERE {spec.period_col} = %s", (period,))
    return {r[0] for r in cur.fetchall()}


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
        src = _live_source(cur, spec, p)
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
    month carries ONE live source, the tip's source_file, on every row (a
    refresh sets the source of the whole month, so a month left partly on an
    older file fails)."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live periods to check (an empty state is not a pass)"
    bad = load_checks.check_latest_equals_live(cur, spec)
    for p in periods:
        try:
            files, tip = _source_files(cur, spec, p)
        except (LookupError, ValueError):
            continue          # already reported by check_latest_equals_live
        src = _live_source(cur, spec, p)
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


def real_coverage(cur, spec=SPEC, valid=None, expected=EXPECTED_AREAS):
    """Every edition of every month holds the held area count (296) with
    distinct keys, the tip too, and every code is in la_boundaries."""
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
    if expected is not None and want_n != expected:
        bad.append(f"the held area count is {want_n}, expected {expected}")
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
    if valid is None:
        valid = m.valid_codes(cur)
    for table in (spec.editions_table, spec.live_table):
        cur.execute(f"SELECT DISTINCT lad24cd FROM public.{table}")
        stray = sorted(r[0] for r in cur.fetchall() if r[0] not in valid)
        if stray:
            bad.append(f"{table}: {stray[:4]} not in la_boundaries")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} months, {n_ed} editions, each {want_n} areas (held "
        "count), every code in la_boundaries")


def real_no_negative(cur, spec=SPEC):
    """No negative measure_value (NULL is a suppressed value, not a bad
    one), in the editions and in live."""
    counts = {}
    for label, table in (("editions", spec.editions_table),
                         ("live", spec.live_table)):
        cur.execute(f"SELECT COUNT(*) FILTER (WHERE measure_value < 0) FROM "
                    f"public.{table}")
        counts[label] = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table}")
    if not cur.fetchone()[0]:
        return False, "no live rows to check (an empty state is not a pass)"
    return not any(counts.values()), (
        f"rows with a negative value: editions {counts['editions']}, live "
        f"{counts['live']}")


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
            diffs.append(f"{p}: live (NULL, 0) {ln} vs ed{ed} {en}")
    return not bad and not diffs, "; ".join((bad + diffs)[:3]) if (
        bad or diffs) else (
        "NULL and 0 counts per month identical in live and the latest "
        "edition; EXCEPT comparison clean")


def real_suppression(cur, spec=SPEC):
    """Suppression listed per month: the NULL count (a '*' or a blank) of
    live and of the latest edition must be equal, and no zero value is held
    (a suppressed value is NULL, never 0)."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live periods to check (an empty state is not a pass)"
    bad, listing, zeros = [], [], 0
    pc = spec.period_col
    for p in periods:
        try:
            ed = core.chain_tip(cur, spec, p)
        except (LookupError, ValueError) as e:
            bad.append(str(e))
            continue
        ln, lz = null_zero_counts(cur, spec.live_table, f"WHERE {pc} = %s",
                                  (p,))
        en, ez = null_zero_counts(cur, spec.editions_table,
                                  f"WHERE {pc} = %s AND edition = %s",
                                  (p, ed))
        listing.append(f"{_s(p)[:7]} {en}")
        if ln != en:
            bad.append(f"{p}: NULL count live {ln} vs ed{ed} {en}")
        if lz or ez:
            zeros += lz + ez
            bad.append(f"{p}: zero value(s) held (live {lz}, ed{ed} {ez}); a "
                       "suppressed value is NULL, never 0")
    return not bad, "; ".join(bad[:3]) if bad else (
        "NULL (suppressed) per month: " + ", ".join(listing)
        + "; no zero value held")


def real_on_disk(cur, spec=SPEC, raw_dir=None, recodes=None):
    """Every data file in data/raw/s9b_mhsds/ whose name is the file of a
    month's held tip parses (parse_file + build_records, the loader's own
    functions, streamed) to exactly that tip. Read-only."""
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} ORDER BY 1")
    periods = [r[0] for r in cur.fetchall()]
    if not periods:
        return False, "no editions to check (an empty state is not a pass)"
    if recodes is None:
        recodes = geography.load_recodes(cur)
    ok_n, bad, skipped = 0, [], 0
    for p in periods:
        files, tip = _source_files(cur, spec, p)
        name = m._name(files[tip]) if files.get(tip) else None
        path = raw / name if name else None
        if not path or not path.is_file():
            skipped += 1
            continue
        rows, _ = m.parse_file(path)
        got = {r["lad24cd"]: (r["la_name"], r["measure_name"],
                              r["measure_value"])
               for r in m.build_records(rows, recodes, _s(p))}
        cur.execute(f"SELECT lad24cd, la_name, measure_name, measure_value "
                    f"FROM public.{spec.editions_table} WHERE "
                    f"{spec.period_col} = %s AND edition = %s", (p, tip))
        held = {r[0]: tuple(r[1:]) for r in cur.fetchall()}
        if got != held:
            n = sum(1 for k in set(got) | set(held)
                    if got.get(k) != held.get(k))
            bad.append(f"{_s(p)} ed{tip}: {name} differs from the tip in {n} "
                       "area(s)")
        else:
            ok_n += 1
    if not ok_n and not bad:
        return False, (f"no data file in {raw} is the file of a held tip "
                       f"({len(periods)} months; an empty comparison is not "
                       "a pass)")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{ok_n} on-disk data file(s) parse to their tip exactly; {skipped} "
        "month(s) whose tip file is not on disk skipped")


def gate_2_edition1(cur):
    real_gate(cur, 2, "edition 1 present for every live month; live source "
              "names a file of the month", real_edition1_present)


def gate_3_latest_equals_live(cur):
    real_gate(cur, 3, "latest edition equals live (values and labels) and "
              "live source is one value per month, the tip's file",
              real_latest_equals_live)


def gate_4_coverage(cur):
    real_gate(cur, 4, "coverage: the held area count (296) in every "
              "edition, every code in la_boundaries", real_coverage)


def gate_5_negative(cur):
    name = "no negative measure_value"
    neg = recs(P1)
    neg[0]["measure_value"] = -1
    try:
        m.check_period(neg, P1, N, ())
        loud = False
    except ValueError as e:
        loud = "negative" in str(e)
    zero_ok = True
    try:
        m.check_period(recs(P1, over={(0, "measure_value"): 0,
                                      (1, "measure_value"): None}), P1, N, ())
    except ValueError:
        zero_ok = False
    if not (loud and zero_ok):
        return report(5, name, False, f"check_period: negative refused="
                      f"{loud}, 0 and NULL accepted={zero_ok}")
    real_gate(cur, 5, name, real_no_negative)


# ---------------------------------------------------------------- gate 6

def _null_zero_variants():
    """(label, column, area index, new value): the base month holds NULL at
    area 0 and 0 at area 1; area 0 becomes 0 and area 1 becomes NULL."""
    out = []
    for c in VALUE_COLS:
        out.append((f"{c} NULL->0", c, 0, 0))
        out.append((f"{c} 0->NULL", c, 1, None))
    return tuple(out)


VARIANTS = _null_zero_variants()


def null_zero_base(col):
    return recs(P1, over={(0, col): None, (1, col): 0})


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
            apply(cur, P1, changed, link_=perf(P1, 2))
            flagged = load_checks.check_latest_equals_live(cur, ZZ)
            stored = editions_of(cur, P1)
            cur.execute(f"""SELECT {col} IS NULL, {col} FROM public.{ZE}
                            WHERE reporting_period = %s AND edition = 2
                              AND lad24cd = %s""", (P1, CODES[area]))
            return clean, kind, flagged, stored, cur.fetchone()
        out[label] = _in_savepoint(cur, variant)
    return out


def variant_ok(res, new):
    clean, kind, flagged, stored, cell = res
    return (clean == [] and kind == "revised" and len(stored) == 2
            and cell == (new is None, new)
            and len(flagged) == 1 and "1 edition-only" in flagged[0])


def seeded_marker_through_loader(cur):
    """A file whose first area carries '*', the second a blank, the third a
    published 0, loaded through the loader: '*' and blank are stored NULL in
    the editions and live tables, never 0; the 0 stays 0. Returns (rc, NULL
    count and zero count of the three areas in the editions, then in live)."""
    def body(cur):
        with env(cur) as e:
            e.make(perf(P1), P1, values={CODES[0]: None, CODES[1]: "",
                                         CODES[2]: 0})
            rc, text, _, _ = e.run(
                ["load", "--commit", "--min-period", P1,
                 "--expected-areas", str(N)], page=[perf(P1)])
            out = []
            for table, where in ((ZE, "edition = 1"), (ZL, "TRUE")):
                row = []
                for c in CODES[:3]:
                    cur.execute(f"SELECT measure_value IS NULL, measure_value"
                                f" FROM public.{table} WHERE {where} AND "
                                "lad24cd = %s", (c,))
                    row.append(cur.fetchone())
                out.append(row)
            return rc, out
    return _in_savepoint(cur, body)


def gate_6_null_vs_zero(cur):
    name = ("NULL versus 0 never conflated (both ways; '*' and a blank are "
            "NULL through the loader, a published 0 stays 0)")
    try:
        s = seeded_null_zero(cur)
        rc, held = seeded_marker_through_loader(cur)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(6, name, False, f"seeded scenario: {e}")
    bad = [f"{label}: {s[label][1]}, edition 2 holds {s[label][4]}"
           for label, _c, _a, new in VARIANTS
           if not variant_ok(s[label], new)]
    want = [(True, None), (True, None), (False, 0)]
    if rc != 0 or held != [want, want]:
        bad.append(f"markers through the loader: rc {rc}, (NULL?, value) of "
                   f"the three areas {held}")
    notes = (f"{len(VARIANTS)} single-cell changes stored as edition 2 with "
             "the cell read back exactly; '*' and a blank are NULL (not 0) in "
             "editions and live, a published 0 stays 0")
    if bad:
        return report(6, name, False, "seeded scenario: " + "; ".join(bad[:4]))
    if not table_exists(cur):
        return report(6, name, False, f"{NOT_YET}; seeded scenario passes: "
                      + notes, pending=True)
    real_gate(cur, 6, name, lambda c: (lambda r: (r[0], f"{r[1]}; seeded "
              f"scenario: {notes}"))(real_null_vs_zero(c)))


# ---------------------------------------------------------------- gate 7

def gate_7_suppression(cur):
    name = ("suppression listed per month: NULL counts equal in live and the "
            "latest edition, no zero value held")

    def one(zero):
        def body(cur):
            with env(cur) as e:
                vals = {c: None for c in CODES[:5]}
                vals[CODES[5]] = ""
                if zero:
                    vals[CODES[6]] = 0
                e.make(perf(P1), P1, values=vals)
                rc, _, _, _ = e.run(
                    ["load", "--commit", "--min-period", P1,
                     "--expected-areas", str(N)], page=[perf(P1)])
                return rc, real_suppression(cur, ZZ)
        return _in_savepoint(cur, body)
    try:
        rc_a, (ok_a, det_a) = one(False)
        rc_b, (ok_b, det_b) = one(True)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(7, name, False, f"seeded scenario: {e}")
    seeded = (rc_a == 0 and ok_a and "2026-01 6" in det_a
              and rc_b == 0 and not ok_b and "zero value(s) held" in det_b)
    notes = (f"seeded: 5 '*' and a blank are listed as 6 NULL for the month "
             f"and pass ({ok_a}); a held 0 is refused ({not ok_b})")
    if not seeded:
        return report(7, name, False, f"seeded scenario wrong: {det_a}; "
                      f"{det_b}")
    if not table_exists(cur):
        return report(7, name, False, f"{NOT_YET}; {notes}", pending=True)
    real_gate(cur, 7, name, lambda c: (lambda r: (r[0], f"{r[1]}; {notes}"))(
        real_suppression(c)))


# ------------------------------------------------------------ seeded gates

def gate_8_seeded_revision(cur):
    name = ("seeded revision: edition 2 with one changed cell, refresh_latest "
            "writes only that month's changed value, sets the source of the "
            "whole month, keeps loaded_at")

    def snapshot(cur, period):
        cur.execute(f"""SELECT lad24cd, la_name, measure_name, measure_value,
                               source, loaded_at
                        FROM public.{ZL} WHERE reporting_period = %s
                        ORDER BY 1""", (period,))
        return cur.fetchall()

    def body(cur):
        ra, rb = recs(P1), recs(P2)
        for p, rr in ((P1, ra), (P2, rb)):
            apply(cur, p, rr)
        before_ok = load_checks.check_latest_equals_live(cur, ZZ) == []
        changed = with_cell(ra, 5, "measure_value",
                            ra[5]["measure_value"] + 1000)
        kind = apply(cur, P1, changed, link_=perf(P1, 2))
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
    iv, isrc, ilo = 3, 4, 5
    all_src = (len(diff) == N
               and all(y[isrc] == perf(P1, 2) and x[isrc] == perf(P1)
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
           f"{len(valued)}; only measure_value and source, loaded_at "
           f"kept)={only}; latest equals live after={after == []}")


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


# --------------------------------------------------------------- gate 10

def _big_zip(path, rows, other_rows):
    """A zip whose one CSV member holds `rows` MHS01 rows (kept out of the
    result) and a few MHS26 rows, written a line at a time."""
    d = "2026-04-01"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open("big.csv", "w") as raw:
            out = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            w = csv.writer(out, lineterminator="\n")
            w.writerow(HEADER)
            for i in range(rows):
                w.writerow([d, "2026-04-30", "Performance", m.BREAKDOWN,
                            f"E06{i % 1000:06d}", "x", "NONE", "NONE",
                            "MHS01", "Other measure padded out a little",
                            str(i)])
            for i, c in enumerate(other_rows):
                w.writerow([d, "2026-04-30", "Performance", m.BREAKDOWN, c,
                            f"Area {c}", "NONE", "NONE", m.MEASURE, MNAME,
                            str(10 + i)])
            out.flush()
            out.detach()
    return path


def gate_10_parse_rules(cur):
    name = ("parse rules: breakdown exact, SECONDARY_LEVEL NONE, E10 and "
            "UNKNOWN dropped and counted, '*' and blank NULL (a published 0 "
            "stays 0), unknown text, a Welsh code and a missing column "
            "halt; large files are streamed")
    samples = [("E06000001", "*"), ("E07000008", ""), ("E08000012", "0"),
               ("E09000001", 35)]
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        f = write_data_file(t / "a.zip", "2026-04-01", samples)
        got, meta = m.parse_file(f)
        by = {r["lad24cd"]: r["measure_value"] for r in got}

        def bad_value(text):
            return _raises(lambda: m.parse_file(write_data_file(
                t / "b.zip", "2026-04-01", [("E06000001", text)])),
                ValueError)
        unknown = [bad_value(x) for x in ("abc", "12.5", "1,200", "-", "..")]
        welsh = _raises(lambda: m.parse_file(write_data_file(
            t / "w.zip", "2026-04-01", [("W06000001", 5)])), ValueError)
        short = t / "h.csv"
        short.write_text("REPORTING_PERIOD_START,STATUS\n2026-04-01,x\n")
        hdr = _raises(lambda: m.parse_file(short), ValueError)
        dup = _raises(lambda: m.build_records(
            got, {"E06000001": "E07000008"}, "2026-04-01"), ValueError)
        # streaming: the zip is never read whole, memory stays flat, and a
        # plain CSV works the same way
        csvf = write_data_file(t / "p.csv", "2026-04-01", samples,
                               as_zip=False, form="iso")
        plain, _ = m.parse_file(csvf)
        with mock.patch.object(zipfile.ZipFile, "read",
                               side_effect=AssertionError("ZipFile.read")):
            streamed, _ = m.parse_file(f)
        bigf = _big_zip(t / "big.zip", 40000, ["E06000001", "E06000002"])
        raw_size = zipfile.ZipFile(bigf).infolist()[0].file_size
        tracemalloc.start()
        try:
            big_rows, big_meta = m.parse_file(bigf)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        it = m.iter_csv_rows(bigf)
        first = next(it)
        it.close()
    ok = (sorted(by) == ["E06000001", "E07000008", "E08000012", "E09000001"]
          and by == {"E06000001": None, "E07000008": None, "E08000012": 0,
                     "E09000001": 35}
          and by["E08000012"] is not None and by["E06000001"] is None
          and meta["dropped"] == {"E10": 1, "UNKNOWN": 1}
          and meta["mhs26_rows"] == 4 + 5
          and not any(r["lad24cd"] in ("E10000003", "UNKNOWN") for r in got)
          and all(u[0] and "MEASURE_VALUE" in u[1] for u in unknown)
          and welsh[0] and "W06000001" in welsh[1]
          and hdr[0] and "header missing" in hdr[1]
          and dup[0] and "duplicate" in dup[1]
          and [r["measure_value"] for r in plain] == [None, None, 0, 35]
          and streamed == got
          and len(big_rows) == 2 and big_meta["mhs26_rows"] == 2
          and raw_size > 4_000_000 and peak < raw_size // 4
          and first["MEASURE_ID"] == "MHS01")
    report(10, name, ok, f"4 areas kept, dropped {meta['dropped']}, the "
           "England/other-measure/A1 noise rows not kept; '*' and blank -> "
           "NULL, 0 stays 0; bad text, W code, missing column, duplicate "
           f"all refused; ZipFile.read never called; a {raw_size:,}-byte CSV "
           f"member parsed with a memory peak of {peak:,} bytes")


# --------------------------------------------------------------- gate 11

def gate_11_planner(cur):
    name = ("planner: window, ledger skip, --recheck-all, never a month "
            "before the earliest held; Final preferred over Performance, "
            "then the version")
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

    # Final over Performance, then the version
    pure = (m.choose_link([perf(P2), perf(P2, 3), final(P2)]) == final(P2)
            and m.choose_link([perf(P2), perf(P2, 2)]) == perf(P2, 2)
            and m.choose_link([final(P2), final(P2, 2), perf(P2, 4)])
            == final(P2, 2)
            and _raises(lambda: m.choose_link([perf(P2), link(
                "MHSDS Data_FebPerf_2026_v1.zip")]), ValueError)[0])

    def body(e, page):
        e.make(perf(P4), P4)
        e.make(perf(P4, 2), P4)
        e.make(final(P4), P4)
        e.make(final(P2), P2)
        pg = [perf(P1), perf(P2), final(P2), perf(P3), perf(P4),
              perf(P4, 2), final(P4)]
        rc, text, _, _ = e.run(["load", "--commit"], page=pg)
        cur = e.cur
        cur.execute(f"SELECT DISTINCT edition, source_file FROM public.{ZE} WHERE "
                    "reporting_period = %s", (P4,))
        return rc, sorted(e.fetched), cur.fetchall(), live_sources(cur, P4)
    try:
        rc, fetched, s4, src4 = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(11, name, False, str(ex).splitlines()[0])
    chose = (rc == 0 and fetched == sorted([final(P2), final(P4)])
             and s4 == [(1, final(P4))] and src4 == [(final(P4),)])
    ok = not bad and nothing == ([], [], periods) and pure and chose
    report(11, name, ok, f"{runs} held/page/ledger/recheck combinations "
           f"checked (and the --min-period widening); nothing held -> new "
           f"{nothing[0]}, earlier {len(nothing[2])}; breaches {bad[:2]}; "
           f"Final above Performance above v1={pure}; a page offering "
           f"Performance, v2 and Final loads only the Final ({chose})")


def gate_12_preview_default(cur):
    name = ("preview default: the load command with no flag issues no commit "
            "and stores nothing")

    def body(e, page):
        e.make(perf(P4), P4)
        e.make(final(P2), P2, transform=final_like)
        pg = [perf(P1), final(P2), perf(P3), perf(P4)]
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
    """A requests-like response: body only through iter_content (reading
    .content whole is a failure of the streaming rule)."""

    def __init__(self, body, status=200):
        self._body = body if isinstance(body, bytes) else body.encode()
        self.text = self._body.decode("utf-8", errors="replace")
        self.status_code = status
        self.chunk_sizes = []

    @property
    def content(self):
        raise AssertionError("response.content read whole")

    def iter_content(self, chunk_size=1):
        self.chunk_sizes.append(chunk_size)
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        pass


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.resps = []

    def get(self, lnk, headers=None, timeout=None, **kw):
        self.calls.append((lnk, kw))
        for prefix, body in self.pages.items():
            if lnk.startswith(prefix):
                r = _Resp(body)
                self.resps.append(r)
                return r
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
    files = [Path(__file__).resolve(), HERE / "s9b_crfd_editions.py",
             HERE / "period_editions.py", HERE / "geography.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")

    def fetch_stub(e):
        lnk = perf(P1)
        e.make(lnk, P1)
        body = e.files[lnk].read_bytes()
        s = _Session({m.PAGE_BASE: html_of(lnk), lnk: body})
        with _quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 1000):
            html = m.fetch_page(P1, session=s)
            path = m.fetch_month(lnk, Path(e.tmp) / "raw", session=s)
        return html, path, s

    def body(cur):
        with env(cur) as e:
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.seed()
                e.make(perf(P4), P4)
                rc, text, _, _ = e.run(
                    ["load", "--commit"],
                    page=[perf(P1), perf(P2), perf(P3), perf(P4)])
                html, path, s = fetch_stub(e)
            return rc, html, path, s
    try:
        rc, html, path, s = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(13, name, False, f"{type(ex).__name__}: {ex}")
    calls = [c[0] for c in s.calls]
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    literal = [n for n, t in texts.items() if pat.search(t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    chunked = (s.resps[-1].chunk_sizes == [m.CHUNK]
               and s.calls[-1][1].get("stream") is True)
    ok = (not attempts and rc == 0 and path.name == m._name(perf(P1))
          and all(u.startswith("https://") for u in calls) and len(calls) == 2
          and ".zip" in path.name and chunked
          and not literal and not in_src and not in_out)
    report(13, name, ok, f"socket attempts {len(attempts)}; load through "
           f"stubs returned {rc}; fetch through a stub session requested "
           f"{len(calls)} URLs, the file in {m.CHUNK}-byte chunks with "
           f"stream=True ({chunked}); secret-like literal in source "
           f"{literal or 'none'}; {len(secrets)} secret-named settings "
           f"checked against {len(files)} files and {len(OUTPUT)} output "
           f"lines: in source {in_src or 'none'}, in output "
           f"{in_out or 'none'}")


# ---------------------------------------------------------- gates 14 and 15

def gate_14_revert_new_edition(cur):
    name = "revert stored as a new edition"
    a = recs(P3)
    b = with_cell(a, 3, "measure_value", a[3]["measure_value"] + 7)

    def body(cur):
        seq = [apply(cur, P3, x, link_=lk) for x, lk in (
            (a, perf(P3)), (b, perf(P3, 2)), (a, perf(P3, 3)))]
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
        e.make(perf(P4), P4)
        rc, text, _, logged = e.run(["load", "--commit"],
                                    page=page + [perf(P4)])
        cur = e.cur
        cur.execute(f"SELECT COUNT(*), COUNT(loaded_at), COUNT(*) FILTER "
                    f"(WHERE source = %s), COUNT(DISTINCT measure_id) FROM "
                    f"public.{ZL} WHERE reporting_period = %s",
                    (perf(P4), P4))
        live_n, stamped, labelled, ids = cur.fetchone()
        return (rc, editions_of(cur, P4), live_n, stamped, labelled, ids,
                core.rows_differing(cur, ZZ, P4, 1), ledger_of(cur, P4),
                logged.called, m.status(cur, ZZ))

    def failed(e, page):
        e.make(perf(P4), P4)
        real_insert = m.insert_live

        def boom(c, profile, period, records):
            seen["editions_when_live_failed"] = sum(
                r[2] for r in editions_of(c, period))
            real_insert(c, profile, period, records[:10])
            raise RuntimeError("live insert failed (planted)")
        with mock.patch.object(m, "insert_live", side_effect=boom):
            rc, text, _, logged = e.run(["load", "--commit"],
                                        page=page + [perf(P4)])
        return (rc, editions_of(e.cur, P4), ledger_of(e.cur, P4),
                live_rows(e.cur, P4), logged.called)

    def preview(e, page):
        e.make(perf(P4), P4)
        rc, text, _, _ = e.run(["load"], page=page + [perf(P4)])
        return (rc, text, editions_of(e.cur, P4), ledger_of(e.cur, P4),
                live_rows(e.cur, P4))
    try:
        (rc, eds, live_n, stamped, labelled, ids, diff, led, logged,
         st) = scenario(cur, stored)
        f_rc, f_eds, f_led, f_live, f_logged = scenario(cur, failed)
        p_rc, p_out, p_eds, p_led, p_live = scenario(cur, preview)
    except (psycopg2.Error, SystemExit, RuntimeError) as e:
        return report(15, name, False, str(e).splitlines()[0])
    ok = (rc == 0 and eds == [(1, None, N)] and live_n == N
          and stamped == N and labelled == N and ids == 1 and diff == 0
          and led == [(perf_name(P4), "new", 1)] and logged
          and st["live_missing"] == []
          and f_rc == 1 and seen.get("editions_when_live_failed") == N
          and f_eds == [] and f_led == [] and f_live == 0 and not f_logged
          and p_rc == 0 and p_eds == [] and p_led == [] and p_live == 0
          and f"{P4}: new" in p_out)
    report(15, name, ok, f"editions {[(e, s) for e, s, _ in eds]}, live "
           f"rows {live_n} (loaded_at set {stamped}, source = the file URL "
           f"on {labelled}), cells differing {diff}, ledger {led}; planted "
           f"live failure: rc {f_rc}, edition rows written first="
           f"{seen.get('editions_when_live_failed') == N}, afterwards "
           f"editions {f_eds} ledger {f_led} live {f_live}; preview writes "
           f"editions {p_eds} ledger {p_led} live {p_live}")


# ------------------------------------------------------------------- gate 16

def write_odd_dates(path, period, values, fmt, status="Performance"):
    """A zip of a data file whose REPORTING_PERIOD dates are written by
    fmt(date) (an odd form such as 01/06/24 or 1/6/2024)."""
    lines = data_lines(period, values, form="iso", status=status)
    for row in lines[1:]:
        row[0] = fmt(date.fromisoformat(row[0]))
        row[1] = fmt(date.fromisoformat(row[1]))
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(lines)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{Path(path).stem}.csv", buf.getvalue())
    return path


def gate_16_file_identity(cur):
    name = ("file identity: REPORTING_PERIOD_START (all date forms) against "
            "the month, REPORTING_PERIOD_END its last day, STATUS against "
            "the link's kind, the link's name against the month; caught "
            "before any write")

    def body(e, page):
        tmp = e.tmp
        vals = [(c, 10) for c in CODES]
        wrong = {}

        def odd(label, fmt, p=P4, status="Performance"):
            d = tmp / f"odd{len(list(tmp.glob('odd*')))}"
            d.mkdir()
            return write_odd_dates(d / m._name(perf(p)), p, vals, fmt,
                                   status)
        # files that must be caught (loaded through the page, by link)
        e.make(perf(P1, 2), P1, start=date(2025, 12, 1))
        wrong["START is another month"] = ("page", perf(P1, 2), P1)
        e.make(perf(P1, 3), P1, end=date(2026, 1, 29))
        wrong["END is not the last day"] = ("page", perf(P1, 3), P1)
        e.make(perf(P1, 4), P1, status="Final")
        wrong["STATUS Final in a Performance link"] = ("page", perf(P1, 4),
                                                       P1)
        e.make(final(P1), P1, status="Performance")
        wrong["STATUS Performance in a Final link"] = ("page", final(P1), P1)
        out = {}
        for label, (_k, lnk, p) in wrong.items():
            with mock.patch.object(m, "apply_period",
                                   side_effect=AssertionError("wrote")), \
                    mock.patch.object(pe, "load_periods") as lp:
                rc, text, conn, logged = e.run(
                    ["load", "--commit", "--allow-older-file"],
                    page=[perf(P1), perf(P2), perf(P3), lnk])
            out[label] = (rc, text, lp.called, conn.cur.connection.commits,
                          logged.called)
        # offline files whose own contents disagree with their name
        offline = {
            "a --file named for March holding April": e.make(perf(P3), P4),
            "a --file with no MHS26 rows": write_data_file(
                tmp / "empty.zip", P4, [], noise=False),
        }
        for label, f in offline.items():
            with mock.patch.object(m, "apply_period",
                                   side_effect=AssertionError("wrote")), \
                    mock.patch.object(pe, "load_periods") as lp:
                rc, text, conn, logged = e.run(["load", "--commit"], file=f)
            out[label] = (rc, text, lp.called, conn.cur.connection.commits,
                          logged.called)
        before = state(e.cur)
        # odd date forms and the stripped STATUS are accepted ...
        accepted = {}
        for label, fmt in (("01/04/26", lambda d: d.strftime("%d/%m/%y")),
                           ("1/4/2026", lambda d: f"{d.day}/{d.month}/"
                                                  f"{d.year}"),
                           ("01-04-2026", lambda d: d.strftime("%d-%m-%Y")),
                           ("2026-04-01", lambda d: d.isoformat())):
            f = odd(label, fmt)
            rc, text, _, _ = e.run(["load"], file=f)
            accepted[label] = (rc, f"{P4}: new" in text)
        f = e.make(perf(P4), P4, status="Performance ")
        rc, text, _, _ = e.run(["load"], file=f)
        accepted["STATUS 'Performance '"] = (rc, f"{P4}: new" in text)
        # ... but are still checked against the month
        f = odd("wrong month", lambda d: d.replace(month=3).strftime(
            "%d/%m/%y"))
        rc_w, text_w, _, _ = e.run(["load", "--commit"], file=f)
        return (out, accepted, (rc_w, text_w), before == state(e.cur),
                editions_of(e.cur, P4), live_rows(e.cur, P4),
                ledger_of(e.cur, P4))
    try:
        out, accepted, wrong_month, unchanged, e4, l4, led4 = scenario(
            cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(16, name, False, str(ex).splitlines()[0])
    good = {"periods": [P4], "period_ends": ["2026-04-30"],
            "statuses": ["Performance"], "mhs26_rows": 4}
    K = m.PERFORMANCE
    unit_bad = [m.check_identity(P4, K, dict(good, periods=[P3]),
                                 perf_name(P4)),
                m.check_identity(P4, K, dict(good, periods=[P3, P4]),
                                 perf_name(P4)),
                m.check_identity(P4, K, dict(good, period_ends=["2026-04-29"]),
                                 perf_name(P4)),
                m.check_identity(P4, K, dict(good, statuses=["Final"]),
                                 perf_name(P4)),
                m.check_identity(P4, K, dict(good, statuses=["Final",
                                                             "Performance"]),
                                 perf_name(P4)),
                m.check_identity(P4, K, good, perf_name(P3)),
                m.check_identity(P4, K, good, final_name(P4)),
                m.check_identity(P4, K, dict(good, mhs26_rows=0),
                                 perf_name(P4))]
    unit_ok = m.check_identity(P4, K, good, perf_name(P4, 2)) == []
    caught = {k: (v[0] == "halt" and not v[2] and v[3] == 0 and not v[4]
                  and ("identity" in v[1] or "expected one" in v[1]
                       or "one of each" in v[1] or "carries" in v[1]))
              for k, v in out.items()}
    ok = (all(caught.values()) and len(caught) == 6
          and all(a == (0, True) for a in accepted.values())
          and wrong_month[0] == "halt" and "identity" in wrong_month[1]
          and unchanged and e4 == [] and l4 == 0 and led4 == []
          and unit_ok and all(len(u) >= 1 for u in unit_bad))
    report(16, name, ok, "; ".join(
        f"{k}: {'halted before any write' if v else 'NOT CAUGHT'}"
        for k, v in caught.items())
        + f"; accepted (preview rc 0): {sorted(accepted)}; an odd-form date "
        f"for the wrong month halts={wrong_month[0] == 'halt'}; nothing "
        "stored")


# ------------------------------------------------------------------- gate 17

def gate_17_older_file_guard(cur):
    name = ("older-file guard: a Performance file after a Final, v1 after "
            "v2, or an earlier page halts unless --allow-older-file, before "
            "anything is downloaded")

    def body(e, page):
        # P2 is held from the Final, P3 from the v2; the pages offer older
        e.make(perf(P2), P2)
        e.make(perf(P3), P3)
        old_page = [perf(P1), perf(P2), perf(P3)]
        before = state(e.cur)
        halts = {}
        for argv in (["load"], ["load", "--commit"], ["load", "--simulate"]):
            rc, text, _, logged = e.run(argv, page=old_page)
            halts[" ".join(argv)] = (
                rc == "halt" and "latest file" in text
                and "--allow-older-file" in text and e.fetched == []
                and not logged.called)
        unchanged = state(e.cur) == before
        rc_allow, t_allow, _, _ = e.run(["load", "--allow-older-file"],
                                        page=old_page)
        # v1 after v2 on its own
        rc_v, t_v, _, _ = e.run(["load"], page=[perf(P1), final(P2),
                                                perf(P3)])
        rc_same, t_same, _, _ = e.run(["load", "--commit"],
                                      page=[perf(P1), final(P2), perf(P3, 2)])
        # the page's latest month is earlier than the held latest
        rc_early, t_early, _, _ = e.run(["load"], page=[perf(P1), final(P2)])
        rc_early_ok, t_early_ok, _, _ = e.run(
            ["load", "--allow-older-file"], page=[perf(P1), final(P2)])
        return (halts, unchanged, rc_allow, t_allow, rc_v, t_v, rc_same,
                t_same, rc_early, t_early, rc_early_ok, t_early_ok)
    try:
        (halts, unchanged, rc_allow, t_allow, rc_v, t_v, rc_same, t_same,
         rc_early, t_early, rc_early_ok, t_early_ok) = scenario(
            cur, body, lnk=lambda p: (final(p) if p == P2 else
                                      perf(p, 2) if p == P3 else perf(p)))
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(17, name, False, str(ex).splitlines()[0])
    pure = (m.link_older(perf(P2), final(P2))
            and not m.link_older(final(P2), perf(P2))
            and m.link_older(perf(P2), perf(P2, 2))
            and not m.link_older(perf(P2, 2), perf(P2, 2))
            and m.link_older(final(P2), final(P2, 2))
            and not m.link_older(perf(P1), None)
            and not m.link_older(perf(P1), "plain-name.csv"))
    ok = (all(halts.values()) and unchanged and rc_allow == 0
          and "--allow-older-file given" in t_allow
          and rc_v == "halt" and "latest file" in t_v
          and rc_same == 0 and "--allow-older-file" not in t_same
          and rc_early == "halt" and "2026-03 is already held" in t_early
          and rc_early_ok == 0 and "--allow-older-file given" in t_early_ok
          and pure)
    report(17, name, ok, f"older link in preview/commit/simulate halted "
           f"before any download or write={all(halts.values())}; stores "
           f"unchanged={unchanged}; --allow-older-file proceeds ({rc_allow}); "
           f"a v1 after a v2 halts={rc_v == 'halt'}; the current link is "
           f"unaffected ({rc_same}); an earlier page "
           f"halts={rc_early == 'halt'} and is overridden ({rc_early_ok}); "
           f"pure rule={pure}")


# ------------------------------------------------------------------- gate 18

def gate_18_month_checks(cur):
    name = ("month checks: short month, swapped area, extra areas, negative "
            "value, unresolved code, NULL for a number above 5 areas, "
            "revision above 50% above 10 areas, 0/NULL needs --acknowledge")
    problems = []

    def run(label, setup, want_rc, want_text, check, flags=(), values=None):
        def one(e, page):
            pg = setup(e, page)
            with mock.patch.object(pe, "load_periods",
                                   wraps=pe.load_periods) as lp:
                rc, text, _, logged = e.run(["load", "--commit", *flags],
                                            page=pg)
            return rc, text, logged.called, lp.called, check(e.cur)
        try:
            rc, text, logged, called, extra = scenario(cur, one,
                                                       values=values)
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
            e.make(perf(P4), P4, **kw)
            return page + [perf(P4)]
        return setup

    def revised_p3(**kw):
        def setup(e, page):
            e.make(perf(P3, 2), P3, **kw)
            return [perf(P1), perf(P2), perf(P3, 2)]
        return setup

    nothing_p4 = lambda c: (editions_of(c, P4) == [] and live_rows(c, P4) == 0
                            and ledger_of(c, P4) == [])
    held_p3 = lambda c: editions_of(c, P3) == [(1, None, N)]
    run("short month", new_month(codes=CODES[:-1]), 1,
        f"{P4}: REJECTED, not stored", nothing_p4)
    run("short month text", new_month(codes=CODES[:-1]), 1,
        f"{N - 1} areas, expected {N}", nothing_p4)
    run("area swapped for another", new_month(codes=CODES[:-1] + [SPARE]), 1,
        CODES[-1], nothing_p4)
    run("more areas than held", new_month(codes=CODES + [SPARE]), 1,
        f"{N + 1} areas, expected {N}", nothing_p4)
    run("negative value", new_month(values={CODES[0]: -1}), 1, "negative",
        nothing_p4)
    run("code not in la_boundaries", new_month(
        codes=CODES[:-1] + ["E06999999"]), "halt",
        "unresolved local authority codes", nothing_p4)
    run("NULL for a number in 6 areas", revised_p3(
        values={c: None for c in CODES[:6]}), 1,
        "NULL replaces a number in 6 areas", held_p3)
    run("NULL for a number in 5 areas (the limit)", revised_p3(
        values={c: None for c in CODES[:5]}), 0, "",
        lambda c: editions_of(c, P3)[-1][0] == 2)
    run("revised above 50% in 11 areas", revised_p3(
        transform=lambda i, c, v: v * 3 if i < 11 else v), 1,
        "revised above 50% in 11 areas", held_p3)
    run("revised above 50% in 10 areas (the limit)", revised_p3(
        transform=lambda i, c, v: v * 3 if i < 10 else v), 0, "",
        lambda c: editions_of(c, P3)[-1][0] == 2)
    run("0 to NULL without --acknowledge", revised_p3(
        values={CODES[2]: None}), 1, "0 to NULL", held_p3,
        values={P3: {CODES[2]: 0}})
    run("0 to NULL with --acknowledge", revised_p3(values={CODES[2]: None}),
        0, "ACKNOWLEDGED", lambda c: editions_of(c, P3)[-1][0] == 2,
        flags=("--acknowledge", P3), values={P3: {CODES[2]: 0}})
    run("NULL to 0 without --acknowledge", revised_p3(
        values={CODES[2]: 0}), 1, "NULL to 0", held_p3,
        values={P3: {CODES[2]: None}})
    report(18, name, not problems, "; ".join(problems[:3]) if problems else
           "13 scenarios: each rejected month stored nothing (no edition, "
           "live row or ledger row) and wrote no run log row, and the limits "
           "themselves (5 NULL areas, 10 revised areas) pass")


# ------------------------------------------------------------------- gate 19

def _literal_recode_codes(tree):
    """Dict keys and any string constant equal to a new Barnsley/Sheffield
    code outside a docstring, in a parsed module."""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)):
                docs.add(id(body[0].value))
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs
                and node.value in geography.NEW_CODES | geography.OLD_CODES):
            found.add(node.value)
    return found


def gate_19_barnsley_sheffield(cur):
    name = ("Barnsley and Sheffield (declared 'mixed'): new codes resolve, "
            "forms may differ between months, both forms of one area in one "
            "file halt before any write, no private recode dict")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("none",))[0]
    new_form = {"E08000016": "E08000038", "E08000019": "E08000039"}

    def body(e, page):
        new_codes = [new_form.get(c, c) for c in CODES]
        # (a) the new form in a new month: stored under the canonical codes
        e.make(perf(P4), P4, codes=new_codes)
        rc_new, t_new, _, _ = e.run(["load", "--commit"],
                                    page=page + [perf(P4)])
        got = live_vals(e.cur, P4)
        # (b) the form changes between months of one run: P3 reissued in the
        # new form (same figures), P5 new in the old form
        e.make(perf(P3, 2), P3, codes=new_codes)
        e.make(perf("2026-05-01"), "2026-05-01")
        rc_mix, t_mix, _, _ = e.run(
            ["load", "--commit"], this_month="2026-05-01",
            page=[perf(P1), perf(P2), perf(P3, 2), perf(P4),
                  perf("2026-05-01")])
        # (c) both forms of one area in one file
        res = {}
        for label, codes in (
                ("E08000016 beside E08000038", CODES + ["E08000038"]),
                ("E08000019 beside E08000039", CODES + ["E08000039"])):
            e.make(perf("2026-06-01"), "2026-06-01", codes=codes)
            with mock.patch.object(pe, "load_periods") as lp, \
                    mock.patch.object(m, "apply_period",
                                      side_effect=AssertionError("wrote")):
                rc, text, conn, logged = e.run(
                    ["load", "--commit"], this_month="2026-06-01",
                    page=[perf(P1), perf(P2), perf(P3, 2), perf(P4),
                          perf("2026-05-01"), perf("2026-06-01")])
            res[label] = (rc, text, lp.called, conn.cur.connection.commits,
                          editions_of(e.cur, "2026-06-01"),
                          live_rows(e.cur, "2026-06-01"),
                          ledger_of(e.cur, "2026-06-01"), logged.called)
        rm, probs = geography.resolve(
            e.cur, m.RUN_SOURCE, set(new_form.values()) | set(new_form))
        return (rc_new, t_new, got, rc_mix, t_mix, ledger_of(e.cur, P3),
                res, rm, probs)
    try:
        (rc_new, t_new, got, rc_mix, t_mix, led3, res, rm,
         probs) = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, ValueError) as ex:
        return report(19, name, False, str(ex).splitlines()[0])
    halted = {k: (v[0] == "halt" and "both" in v[1] and "E080000" in v[1]
                  and not v[2] and v[3] == 0 and v[4] == [] and v[5] == 0
                  and v[6] == [] and not v[7])
              for k, v in res.items()}
    src = Path(m.__file__).read_text(encoding="utf-8")
    private = _literal_recode_codes(ast.parse(src))
    no_dict = not hasattr(m, "HARD_RECODES") and not private
    ok = (decl == "mixed" and rc_new == 0 and "2026-04 38/39" in t_new
          and "E08000016" in got and "E08000019" in got
          and "E08000038" not in got and "E08000039" not in got
          and len(got) == N
          and rc_mix == 0 and led3[-1] == (perf_name(P3, 2), "unchanged", 1)
          and all(halted.values()) and probs == []
          and rm == {"E08000038": "E08000016", "E08000039": "E08000019",
                     "E08000016": "E08000016", "E08000019": "E08000019"}
          and no_dict)
    report(19, name, ok, f"9b declared {decl!r}; the new form (38/39) "
           f"loaded as {N} areas under E08000016/19 (rc {rc_new}); a month in "
           f"the new form and one in the old form in one run (rc {rc_mix}); "
           + "; ".join(f"{k}: halted before any write={v}"
                       for k, v in halted.items())
           + f"; resolve map {sorted(rm.items())[:2]}..., problems {probs}; "
           f"private recode codes in the loader: {sorted(private) or 'none'}")


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
          and fetched == [perf(P2)] and eds == [(1, None, N)] and lv == N
          and src == [(perf(P2),)] and led[-1][1] == "live-missing"
          and after["live_missing"] == [])
    report(21, name, ok, f"status flagged {st['live_missing']} (exit "
           f"{rc_status}); load rc {rc}, fetched only that month; editions "
           f"{eds} (no new edition), live rows {lv}, live source {src}, "
           f"ledger {led[-1:]}; live_missing afterwards "
           f"{after['live_missing']}")


# ------------------------------------------------------------------- gate 22

def gate_22_ledger(cur):
    name = ("file-check ledger: a v2 reissue with the same figures is "
            "recorded once and skipped on the next run; an older link taken "
            "with --allow-older-file is recorded too; --recheck-all reads "
            "again")

    def body(e, page):
        e.make(perf(P2, 2), P2)                   # identical figures, new file
        pg = [perf(P1), perf(P2, 2), perf(P3)]
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
        checked = pe.checked_files(e.cur, m._profile(ZZ))
        eds_before = editions_of(e.cur)
        # an older link never seen before (v2 after a v3 that is the tip),
        # taken on purpose
        bump = {CODES[0]: base_value(0, P3) + 1}
        e.make(perf(P3, 3), P3, values=bump)
        e.make(perf(P3, 2), P3, values=bump)
        pg3 = [perf(P1), perf(P2, 2), perf(P3, 3)]
        e.run(["load", "--commit"], page=pg3)
        e.fetched.clear()
        pg_old = [perf(P1), perf(P2, 2), perf(P3, 2)]
        rc_h, t_h, _, _ = e.run(["load", "--commit"], page=pg_old)
        halted = (rc_h == "halt" and "--allow-older-file" in t_h
                  and e.fetched == [])
        n_before = len(ledger_of(e.cur, P3))
        rc4, t4, _, _ = e.run(["load", "--commit", "--allow-older-file"],
                              page=pg_old)
        f4 = e.fetched[:]
        l4 = ledger_of(e.cur, P3)
        grew = len(l4) == n_before + 1
        e.fetched.clear()
        # the ledger now holds it: the guard no longer applies, nothing is
        # fetched
        rc5, t5, _, _ = e.run(["load", "--commit"], page=pg_old)
        return (rc1, t1, f1, e1, l1, rc2, t2, f2, l2, rc3, f3,
                eds_before, checked, rc4, f4, l4, rc5, t5,
                list(e.fetched), ledger_of(e.cur, P3), halted and grew)
    try:
        (rc1, t1, f1, e1, l1, rc2, t2, f2, l2, rc3, f3, eds_all, checked,
         rc4, f4, l4, rc5, t5, f5, l5, halted) = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(22, name, False, str(ex).splitlines()[0])
    want = [(perf_name(P2), "new", 1), (perf_name(P2, 2), "unchanged", 1)]
    ok = (rc1 == 0 and f1 == [perf(P2, 2)] and f"{P2}: unchanged" in t1
          and e1 == [(1, None, N)] and l1 == want
          and rc2 == 0 and f2 == [] and "nothing to fetch" in t2 and l2 == want
          and rc3 == 0 and f3 == sorted([perf(P1), perf(P2, 2), perf(P3)])
          and eds_all == [(1, None, 3 * N)]
          and perf(P2, 2) in checked.get(P2, set())
          and halted and rc4 == 0 and f4 == [perf(P3, 2)] and len(l4) >= 3
          and l4[-1] == (perf_name(P3, 2), "unchanged", 2)
          and rc5 == 0 and f5 == [] and "nothing to fetch" in t5
          and l5 == l4)
    report(22, name, ok, f"v2 reissue fetched once ({len(f1)}), recorded "
           f"'unchanged' once ({l1[-1:]}), next run fetched {len(f2)} "
           f"('nothing to fetch'={'nothing to fetch' in t2}), ledger still "
           f"{len(l2)} rows for the month; --recheck-all fetched {len(f3)}; "
           f"older link under --allow-older-file recorded as {l4[-1:]} and "
           f"not fetched again ({len(f5)}); editions {eds_all}")


# ------------------------------------------------------------------- gate 23

def _summary(**kw):
    d = {"new": False, "transition": ("Performance", "Final"),
         "total_old": 1000, "total_new": 1100, "null_areas": 9,
         "big_areas": 36, "zero_null": 0}
    d.update(kw)
    return {"per_period": {P2: d}}


def gate_23_transition_stop_rules(cur):
    name = ("transition-aware stop rules: a Performance-to-Final refresh of "
            "150 areas is stored; the same on a reissue stops; a Final "
            "moving the national total over 50% stops; 0/NULL always stops; "
            "--acknowledge releases")

    def final_ok(e, page):
        before = live_vals(e.cur, P2)
        e.make(final(P2), P2, transform=final_like)
        pg = [perf(P1), final(P2), perf(P3)]
        rc, text, _, logged = e.run(["load", "--commit"], page=pg)
        eds = editions_of(e.cur, P2)
        e.cur.execute(f"SELECT release_label FROM public.{ZE} WHERE "
                      "reporting_period = %s AND edition = 2 LIMIT 1", (P2,))
        lab = e.cur.fetchone()
        same_live = live_vals(e.cur, P2) == before
        rc_r, _, _, _ = e.run(["refresh-latest", "--commit"])
        e.cur.execute(f"SELECT source, COUNT(*) FROM public.{ZL} WHERE "
                      "reporting_period = %s GROUP BY 1", (P2,))
        srcs = e.cur.fetchall()
        return (rc, text, eds, lab, same_live, ledger_of(e.cur, P2)[-1],
                logged.called, rc_r, srcs, e.fetched[:])

    def reissue(e, page, kind):
        lnk = perf(P2, 2) if kind == "perf" else final(P2, 2)
        e.make(lnk, P2, transform=final_like)
        pg = [perf(P1), lnk, perf(P3)]
        rc, text, _, logged = e.run(["load", "--commit"], page=pg)
        out = (rc, text, editions_of(e.cur, P2), ledger_of(e.cur, P2),
               logged.called)
        rc_a, t_a, _, _ = e.run(["load", "--commit", "--acknowledge", P2],
                                page=pg)
        return out + (rc_a, t_a, editions_of(e.cur, P2))

    def doubled(e, page):
        e.make(final(P2), P2, transform=lambda i, c, v: v * 2)
        pg = [perf(P1), final(P2), perf(P3)]
        rc, text, _, _ = e.run(["load", "--commit"], page=pg)
        out = (rc, text, editions_of(e.cur, P2))
        rc_a, t_a, _, _ = e.run(["load", "--commit", "--acknowledge", P2],
                                page=pg)
        return out + (rc_a, editions_of(e.cur, P2))

    def zero_null(e, page):
        e.make(final(P2), P2, values={CODES[2]: None})
        rc, text, _, _ = e.run(["load", "--commit"],
                               page=[perf(P1), final(P2), perf(P3)])
        return rc, text, editions_of(e.cur, P2)
    try:
        ok_run = scenario(cur, final_ok)
        perf_re = scenario(cur, lambda e, p: reissue(e, p, "perf"))
        final_re = scenario(cur, lambda e, p: reissue(e, p, "final"),
                            lnk=lambda p: final(p) if p == P2 else perf(p))
        dbl = scenario(cur, doubled)
        zn = scenario(cur, zero_null, values={P2: {CODES[2]: 0}})
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(23, name, False, str(ex).splitlines()[0])
    rc, text, eds, lab, same_live, led, logged, rc_r, srcs, fetched = ok_run
    stored = (rc == 0 and fetched == [final(P2)]
              and f"{P2}: Performance to Final: 150 areas changed" in text
              and "REJECTED" not in text and "STOP CONDITION" not in text
              and eds == [(1, None, N), (2, 1, N)] and same_live
              and lab and "Final data file" in lab[0]
              and "year-end Final" in lab[0]
              and led == (final_name(P2), "revised", 2) and logged
              and rc_r == 0 and srcs == [(final(P2), N)])

    def stopped(res, limit):
        rc, text, eds, led, logged, rc_a, t_a, eds_a = res
        return (rc == 1 and f"{P2}: REJECTED, not stored" in text
                and limit in text and eds == [(1, None, N)] and not logged
                and led[-1][1] == "new" and rc_a == 0
                and "ACKNOWLEDGED" in t_a
                and eds_a == [(1, None, N), (2, 1, N)])
    perf_stop = stopped(perf_re, "revised above 50% in 20 areas")
    final_stop = stopped(final_re, "revised above 50% in 20 areas")
    d_rc, d_text, d_eds, d_rca, d_eds_a = dbl
    total_stop = (d_rc == 1 and "national total" in d_text
                  and f"{P2}: REJECTED, not stored" in d_text
                  and d_eds == [(1, None, N)] and d_rca == 0
                  and d_eds_a == [(1, None, N), (2, 1, N)])
    z_rc, z_text, z_eds = zn
    zero_stop = (z_rc == 1 and "0 to NULL" in z_text
                 and z_eds == [(1, None, N)])
    t = {P2: ("Performance", "Performance")}
    rules = (m.stop_problems(_summary()) == {}
             and P2 in m.stop_problems(_summary(), t)
             and m.stop_problems(_summary(total_new=1500)) == {}
             and P2 in m.stop_problems(_summary(total_new=1501))
             and P2 in m.stop_problems(_summary(total_new=400))
             and P2 in m.stop_problems(_summary(zero_null=1))
             and m.stop_problems(_summary(), t, acknowledged={P2}) == {}
             and m.stop_problems(_summary(new=True, null_areas=99,
                                          big_areas=99)) == {}
             and m.stop_problems(_summary(null_areas=5, big_areas=10), t)
             == {}
             and P2 in m.stop_problems(_summary(null_areas=6, big_areas=0), t)
             and P2 in m.stop_problems(_summary(null_areas=0, big_areas=11),
                                       t))
    ok = stored and perf_stop and final_stop and total_stop and zero_stop \
        and rules
    report(23, name, ok, f"Performance to Final with 150 areas changed "
           f"stored as edition 2 and refreshed to live with one source "
           f"({stored}); the same on a Performance v2 reissue stops and "
           f"--acknowledge releases ({perf_stop}); on a Final v2 reissue "
           f"({final_stop}); a Final doubling the national total stops "
           f"({total_stop}); 0 to NULL stops even as a Final ({zero_stop}); "
           f"pure rules and limits={rules}")


# ------------------------------------------------------------------- gate 24

def gate_24_on_disk(cur):
    real_gate(cur, 24, "on-disk reproduction: every data file in "
              "data/raw/s9b_mhsds/ that is a held tip's file parses to that "
              "tip exactly", real_on_disk)


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1,
         gate_3_latest_equals_live, gate_4_coverage, gate_5_negative,
         gate_6_null_vs_zero, gate_7_suppression,
         gate_8_seeded_revision, gate_9_fork_and_second_root,
         gate_10_parse_rules, gate_11_planner, gate_12_preview_default,
         gate_13_no_network_no_secrets, gate_14_revert_new_edition,
         gate_15_new_month_reaches_live, gate_16_file_identity,
         gate_17_older_file_guard, gate_18_month_checks,
         gate_19_barnsley_sheffield, gate_20_rerun_idempotent,
         gate_21_stranded_repair, gate_22_ledger,
         gate_23_transition_stop_rules, gate_24_on_disk)


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
