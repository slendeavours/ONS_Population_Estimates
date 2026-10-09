"""Gates for the S11 (CQC Care directory with filters) snapshot tables.

Mirrors scripts/s9a_drd_editions_verify.py and s9b_crfd_editions_verify.py.
Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. Gates 2 to 5, 18 and 21
read the real tables (cqc_location_snapshots, cqc_location_snapshots_editions,
its file-check ledger and cqc_location_snapshots_unresolved) and gates 6, 7,
16 and 17 add real data to their seeded scenarios. Until the editions table
exists (migrate-legacy not yet run) they print `FAIL (pending migration)` with
the words "not yet migrated", so the script exits non-zero until then, like the
S1, S1b, S8b, S9, S15, S18 and S19 verifiers; nothing is created to make them
pass. The seeded gates never need the real tables: they run on throwaway
tables zz_s11_live, zz_s11_editions, zz_s11_editions_file_checks and
zz_s11_live_unresolved (a copy of the loader's SPEC named zz_s11), a seeded
legacy table zz_s11_legacy (and its _old and _unresolved companions), all
created inside the transaction, and call the loader's own writer and commands
(s11_cqc_editions.main with SPEC pointed at the copy, the CQC page, the
download and postcodes.io stubbed; apply_snapshot; migrate_legacy;
core.refresh_latest) rather than a re-implementation. The real tables are
only ever read.

Gates:
  1  table, file-check ledger, unresolved table and live table shape; append-
     only triggers (UPDATE, DELETE, TRUNCATE refused on the editions, the
     ledger and the unresolved table); on the throwaway copy, and on the real
     tables once they exist                                         (real)
  2  every live snapshot has edition 1; the latest edition equals live cell
     for cell                                                        (real)
  3  live source_file uniform per snapshot and equal to the tip's    (real)
  4  every lad24cd in la_boundaries, 296 authorities in every snapshot,
     no NULL lad24cd                                                 (real)
  5  one row per location per snapshot, no blank location_id         (real)
  6  NULL versus 0 never conflated (seeded: blank beds NULL, 0 beds 0, both
     recheck directions need --acknowledge; an unparseable value halts)
                                                                (real data)
  7  markers: flags, dormant, inherited, dual, brand '-'           (real data)
  8  identity: README title and as-at date against the file name; seeded
     mismatches halt before any write
  9  older-file guard on the page and --file paths, ranked by the README date
 10  stop conditions: a seeded snapshot over each threshold is REJECTED and
     stores nothing; the limits themselves pass
 11  a new snapshot in one transaction: a failing live insert, or a failing
     ledger write after the unresolved rows, rolls all four back
 12  ledger: an unchanged recheck recorded once and skipped on the next run
 13  preview and --simulate write nothing
 14  revision then refresh-latest changes only that snapshot and moves its
     source_file; a changed location set is REJECTED
 15  Barnsley/Sheffield: form 'none', a file cell with E08000038 halts, API
     E08000039 maps to E08000019, no private dict in the module (AST check)
 16  the view: same columns and types as cqc_locations_legacy; equals legacy
     for the migrated state; seeded leave-and-return behaves as legacy
                                                                (real data)
 17  W1's S11 subquery on the view equals the latest snapshot's count per
     authority                                                  (real data)
 18  unresolved: the latest snapshot's unresolved rows are absent from live
                                                                     (real)
 19  rerun idempotent
 20  stranded snapshot repair
 21  on-disk reproduction: each file in data/raw whose sha is a tip's parses
     to that tip exactly (read-only)                                 (real)
 22  no network, no secret in source or output

Usage:
    python scripts/s11_cqc_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The commands get a stand-in
connection whose commit only counts. No network, no real-table writes, no
backend is ever terminated.
"""
import ast
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
import s11_cqc_editions as m  # noqa: E402
from test_s11_cqc_pure import (Api, loc, ods_name, page_of,  # noqa: E402
                               square, write_ods)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC, LIVE, TABLE = m.SPEC, m.LIVE, m.TABLE
LEDGER = pe.file_checks_table(m.PROFILE)
UNRES = m.unresolved_table(SPEC)
ZZ = dataclasses.replace(
    SPEC, name="zz_s11", live_table="zz_s11_live",
    editions_table="zz_s11_editions",
    expected_rows_per_period=m.tip_row_count("zz_s11_editions"))
ZL, ZE = ZZ.live_table, ZZ.editions_table
ZLEDGER = pe.file_checks_table(m._profile(ZZ))
ZUNRES = m.unresolved_table(ZZ)
ZLEG, ZREN, ZLEGU = ("zz_s11_legacy", "zz_s11_legacy_old",
                     "zz_s11_legacy_unresolved")
ZALL = (ZL, ZE, ZLEDGER, ZUNRES, ZLEG, ZREN, ZLEGU)
FETCHED = date(2026, 10, 9)

A, B, C = "E06000001", "E06000002", "E06000003"
BOUNDS = [(A, square(-1.6, 53.30, -1.4, 53.45)),
          (B, square(-1.6, 53.45, -1.4, 53.60)),
          (C, square(-1.4, 53.30, -1.2, 53.45))]
AUTH = {A, B, C}                       # the authorities of the seeded data
POINT = {0: ("53.380000", "-1.500000"), 1: ("53.500000", "-1.500000"),
         2: ("53.380000", "-1.300000")}
N = 60                                 # mapped locations per seeded snapshot
D0, D1, D2, D3 = "2026-07-01", "2026-08-04", "2026-09-01", "2026-10-01"
NOXY = "1-NOXY"                        # no coordinates, unknown postcode
MIGRATED_AT = "2026-09-01"             # the latest snapshot migrate-legacy holds
NOT_YET = f"{TABLE} does not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (22)


def report(n, name, ok, detail="", pending=False):
    RESULTS.append(ok)
    verdict = "PASS" if ok else ("FAIL (pending migration)" if pending
                                 else "FAIL")
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


def _raises(fn, exc=ValueError):
    try:
        fn()
    except exc as e:
        return True, str(e)
    return False, "no error raised"


def url(d, folder=None):
    return (f"https://www.cqc.org.uk/system/files/{folder or d[:7]}/"
            + ods_name(date.fromisoformat(d)))


def tail(u):
    return u.rsplit("/", 2)[-2] + "/" + u.rsplit("/", 1)[-1]


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{t}')"
                                      for t in ZALL))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s11 table already exists as a real table; "
                 "refusing to run")
    m.create_all(cur, ZZ)
    pe.create_file_checks(cur, m._profile(ZZ))


def count(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


def editions_of(cur, period=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{ZE}"
                + (" WHERE snapshot_date = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def ledger_of(cur, period=None):
    """[(folder/file name, outcome, edition)] of the throwaway ledger."""
    cur.execute(f"SELECT source_file, outcome, edition FROM public.{ZLEDGER}"
                + (" WHERE snapshot_date = %s" if period else "")
                + " ORDER BY id", (period,) if period else None)
    return [(tail(s) if s.startswith("http") else s, o, e)
            for s, o, e in cur.fetchall()]


def unresolved_of(cur, period):
    cur.execute(f"SELECT edition, location_id FROM public.{ZUNRES} WHERE "
                "snapshot_date = %s ORDER BY 1, 2", (period,))
    return cur.fetchall()


def live_vals(cur, period, col="latest_overall_rating"):
    cur.execute(f"SELECT location_id, {col} FROM public.{ZL} WHERE "
                "snapshot_date = %s", (period,))
    return dict(cur.fetchall())


def state(cur):
    """Everything a refused or previewed run must leave alone."""
    cur.execute(f"SELECT COUNT(*), MAX(loaded_at) FROM public.{ZL}")
    return (cur.fetchone(), editions_of(cur), ledger_of(cur),
            count(cur, ZUNRES))


# ------------------------------------------------------------ files and Env

class _FakeConn:
    """cur.connection for the engine: commit and rollback only count."""

    def __init__(self, real):
        self.encoding = real.encoding
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _Cur:
    """The real cursor with a stand-in .connection."""

    def __init__(self, cur):
        self._cur = cur
        self.connection = _FakeConn(cur.connection)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _Borrowed:
    """A connection stand-in for the commands: hands out the proxy cursor,
    counts commits, and (when savepoint is set) rolls the real transaction
    back to it on rollback, as the command's own rollback would."""

    def __init__(self, cur, savepoint=None):
        self.cur = _Cur(cur)
        self.commits = 0
        self.savepoint = savepoint
        if savepoint:
            cur.execute(f"SAVEPOINT {savepoint}")

    @contextmanager
    def cursor(self):
        yield self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        if self.savepoint:
            self.cur.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint}")

    def close(self):
        pass


class Env:
    """The loader's main(argv) on the throwaway tables: SPEC pointed at the
    copy, a stand-in connection, the CQC page and the download stubbed (the
    files are built here as .ods), la_boundaries replaced by three test
    squares, postcodes.io stubbed and the run log stubbed."""

    def __init__(self, cur, tmp):
        self.cur = cur
        self.tmp = Path(tmp)
        self.files = {}
        self.fetched = []
        self.n = 0
        self.api = Api()

    def locations(self, ids=None, over=None, extra=()):
        out = []
        for i in (range(N) if ids is None else ids):
            lat, lon = POINT[i % 3]
            v = loc(f"1-{i}", lat, lon, rating="Good",
                    supported_living="Y" if i % 10 == 0 else "")
            for k, val in (over or {}).get(i, {}).items():
                v[k] = val
            out.append(v)
        out.append(loc(NOXY, "", "", postcode="ZZ9 9ZZ"))
        out += list(extra)
        return out

    def make(self, d, link=None, *, name=None, as_at=None, **kw):
        """Write the .ods of snapshot d; register it under link."""
        self.n += 1
        folder = self.tmp / str(self.n)
        folder.mkdir()
        dd = date.fromisoformat(d)
        path = write_ods(
            folder / (name or ods_name(dd)),
            as_at=as_at or f"{dd.day:02d} {m._MONTHS[dd.month - 1].title()}"
                           f" {dd.year}",
            locations=self.locations(**{k: v for k, v in kw.items()
                                        if k in ("ids", "over", "extra")}),
            **{k: v for k, v in kw.items() if k in ("title",)})
        self.files[link or url(d)] = path
        return path

    def run(self, argv, *, page=None, file=None, table=True, savepoint=None,
            runs=None):
        """Returns (rc or 'halt', text, connection stand-in, run-log mock)."""
        borrowed = _Borrowed(self.cur, savepoint)
        args = list(argv) + (["--file", str(file)] if file else [])

        def fetch_file(link, dest=None, session=None):
            self.fetched.append(link)
            return self.files[link]

        html = (page_of((page, "Care directory with filters (02 October "
                               "2026)")) if page else page_of())
        out = io.StringIO()
        with contextlib.ExitStack() as st:
            for name, val in (("SPEC", ZZ), ("LEGACY", ZLEG),
                              ("LEGACY_RENAMED", ZREN),
                              ("LEGACY_UNRESOLVED", ZLEGU)):
                st.enter_context(mock.patch.object(m, name, val))
            st.enter_context(mock.patch.object(m, "_conn",
                                               return_value=borrowed))
            st.enter_context(mock.patch.object(m, "load_boundaries",
                                               return_value=BOUNDS))
            st.enter_context(mock.patch.object(m, "_api",
                                               return_value=self.api))
            st.enter_context(mock.patch.object(m, "fetch_page",
                                               return_value=html))
            st.enter_context(mock.patch.object(m, "fetch_file",
                                               side_effect=fetch_file))
            logged = st.enter_context(mock.patch.object(m, "log_run"))
            st.enter_context(mock.patch.object(
                m, "table_exists",
                side_effect=lambda c, t: table and pe.table_exists(c, t)))
            if runs is not None:
                st.enter_context(mock.patch.object(
                    m, "legacy_run_dates", side_effect=lambda c: runs))
            st.enter_context(contextlib.redirect_stdout(out))
            assert m.SPEC is ZZ, "SPEC was not pointed at the throwaway copy"
            try:
                rc = m.main(args)
            except SystemExit as e:
                text = f"{out.getvalue()}\n{e.code}"
                OUTPUT.extend(text.splitlines())
                return "halt", text, borrowed, logged
        OUTPUT.extend(out.getvalue().splitlines())
        return rc, out.getvalue(), borrowed, logged

    def seed(self, dates=(D1, D2)):
        for d in dates:
            self.make(d)
            rc, text, _, _ = self.run(["load", "--commit"], page=url(d))
            if rc != 0:
                raise RuntimeError(f"seed load of {d} failed: {text[-300:]}")
        self.fetched.clear()


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp)


def scenario(cur, fn, dates=(D1, D2)):
    """Seed through the loader, run fn(env) and roll all of it back."""
    def body(c):
        with env(c) as e:
            e.seed(dates)
            return fn(e)
    return _in_savepoint(cur, body)


def apply(cur, e, d, over=None, ids=None, link=None):
    """The loader's own writer for one snapshot (apply_snapshot): the file
    is built, parsed and mapped as the loader does, then stored with its
    unresolved rows and ledger row. Returns the outcome."""
    path = e.make(d, link, ids=ids, over=over)
    rows, _ = m.parse_locations(path)
    mapped, unres = m.map_locations(rows, BOUNDS, {}, {}, api=Api())
    src = link or url(d)
    recs = [{**r, "snapshot_date": d, "source_file": src} for r in mapped]
    info = m.FileInfo(src, path, m.content_sha256(path),
                      m.release_label(d, None, m.content_sha256(path),
                                      "gate file"))
    return m.apply_snapshot(cur, m._profile(ZZ), d, recs, fetched_on=FETCHED,
                            info=info, unresolved=unres)


# ---------------------------------------------------------------- gate 1

_TYPE = {"text": ("text", None, None, None),
         "boolean": ("boolean", None, None, None),
         "integer": ("integer", None, 32, 0),
         "date": ("date", None, None, None)}


def _want_type(sql):
    base = sql.replace(" NOT NULL", "")
    mt = re.fullmatch(r"varchar\((\d+)\)", base)
    if mt:
        return ("character varying", int(mt.group(1)), None, None)
    mn = re.fullmatch(r"numeric\((\d+),(\d+)\)", base)
    if mn:
        return ("numeric", None, int(mn.group(1)), int(mn.group(2)))
    return _TYPE[base]


def _columns(cur, table):
    cur.execute("""SELECT column_name, data_type, character_maximum_length,
                          numeric_precision, numeric_scale, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (table,))
    return {r[0]: r[1:] for r in cur.fetchall()}


def _pk(cur, table):
    cur.execute("""SELECT a.attname FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid
                    AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = %s::regclass AND i.indisprimary""",
                (f"public.{table}",))
    return {r[0] for r in cur.fetchall()}


def _triggers(cur, table, row_trigger, trunc_trigger):
    cur.execute("""SELECT t.tgname, t.tgtype FROM pg_trigger t
                   WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal""",
                (f"public.{table}",))
    trg = dict(cur.fetchall())
    # tgtype bits: 1 row, 2 before, 4 insert, 8 delete, 16 update, 32 truncate
    ud, tr = trg.get(row_trigger, 0), trg.get(trunc_trigger, 0)
    if not (ud & 1 and ud & 2 and ud & 8 and ud & 16 and not ud & 4
            and tr & 2 and tr & 32):
        return [f"append-only triggers wrong on {table}: {sorted(trg)}"]
    return []


def _value_problems(cols, spec, what):
    bad = []
    for c, sql in spec.value_cols:
        got = cols.get(c)
        if got is None:
            bad.append(f"{what}: {c} missing")
            continue
        if tuple(got[:4]) != _want_type(sql):
            bad.append(f"{what}: {c} is {got[:4]}, expected "
                       f"{_want_type(sql)}")
        if ("NOT NULL" in sql) != (got[4] == "NO"):
            bad.append(f"{what}: {c} nullability {got[4]}, expected "
                       f"{'NOT NULL' if 'NOT NULL' in sql else 'NULL'}")
    return bad


def shape_problems(cur, spec):
    """Problems with the editions table, the live table, the unresolved table
    and the file-check ledger of a spec."""
    bad = []
    t, lv, un = spec.editions_table, spec.live_table, m.unresolved_table(spec)
    led = pe.file_checks_table(m._profile(spec))
    for table in (t, lv, un, led):
        if not pe.table_exists(cur, table):
            bad.append(f"{table} does not exist")
    if bad:
        return bad
    # editions
    cols = _columns(cur, t)
    need = {"location_id", "snapshot_date", "edition", "release_label",
            "published_date", "source_file", "source_sha256", "supersedes",
            "loaded_at"}
    if need - set(cols):
        bad.append(f"editions: missing columns {sorted(need - set(cols))}")
    if cols.get("location_id", ("",))[:2] != ("character varying", 20):
        bad.append(f"editions: location_id is {cols.get('location_id')}")
    if cols.get("location_id", (0, 0, 0, 0, "YES"))[4] != "NO":
        bad.append("editions: location_id is nullable")
    if cols.get("snapshot_date", ("",))[0] != "date":
        bad.append(f"editions: snapshot_date is {cols.get('snapshot_date')}")
    for c in ("edition", "supersedes"):
        if cols.get(c, ("",))[0] != "integer":
            bad.append(f"editions: {c} is {cols.get(c)}")
    bad += _value_problems(cols, spec, "editions")
    if _pk(cur, t) != {"location_id", "snapshot_date", "edition"}:
        bad.append("editions: primary key is not (location_id, "
                   "snapshot_date, edition)")
    cur.execute("SELECT contype FROM pg_constraint WHERE conrelid = "
                "%s::regclass", (f"public.{t}",))
    if any(r[0] == "f" for r in cur.fetchall()):
        bad.append("editions: a foreign key is present")
    bad += _triggers(cur, t, spec.trigger, spec.truncate_trigger)
    # live
    cols = _columns(cur, lv)
    if _pk(cur, lv) != {"snapshot_date", "location_id"}:
        bad.append("live: primary key is not (snapshot_date, location_id)")
    if cols.get("source_file", ("", 0, 0, 0, ""))[0] != "text" or cols.get(
            "source_file", (0,) * 5)[4] != "NO":
        bad.append(f"live: source_file is {cols.get('source_file')}, "
                   "expected text NOT NULL")
    if cols.get("snapshot_date", ("",))[0] != "date":
        bad.append("live: snapshot_date is not a date")
    bad += _value_problems(cols, spec, "live")
    cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = %s", (lv,))
    if not any("location_id" in r[0] and "snapshot_date DESC" in r[0]
               for r in cur.fetchall()):
        bad.append("live: no index on (location_id, snapshot_date DESC)")
    # unresolved
    cols = _columns(cur, un)
    for c, ty, nullable in (("snapshot_date", "date", "NO"),
                            ("edition", "integer", "NO"),
                            ("location_id", "character varying", "NO"),
                            ("location_name", "text", "YES"),
                            ("postcode", "text", "YES"),
                            ("reason", "text", "NO"),
                            ("source_file", "text", "NO")):
        if cols.get(c, (None,) * 6)[0] != ty or cols[c][4] != nullable:
            bad.append(f"unresolved: {c} is {cols.get(c)}, expected "
                       f"{(ty, nullable)}")
    if _pk(cur, un) != {"snapshot_date", "edition", "location_id"}:
        bad.append("unresolved: primary key is not (snapshot_date, edition, "
                   "location_id)")
    bad += _triggers(cur, un, f"{spec.name}_unresolved_immutable",
                     f"{spec.name}_unresolved_no_truncate")
    # the ledger
    lc = _columns(cur, led)
    for c, ty, nullable in (("id", "bigint", "NO"),
                            ("snapshot_date", "date", "NO"),
                            ("source_file", "text", "NO"),
                            ("file_sha256", "text", "NO"),
                            ("outcome", "text", "NO"),
                            ("edition", "integer", "YES"),
                            ("checked_at", "timestamp with time zone", "NO")):
        got = lc.get(c)
        if got is None or (got[0], got[4]) != (ty, nullable):
            bad.append(f"ledger: {c} is {got}, expected {(ty, nullable)}")
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass AND contype = 'c'""",
                (f"public.{led}",))
    chk = " ".join(r[0] for r in cur.fetchall())
    if not all(f"'{o}'" in chk for o in pe.FILE_CHECK_OUTCOMES):
        bad.append(f"ledger: outcome check is {chk!r}")
    bad += [f"ledger {p}" for p in _triggers(
        cur, led, f"{spec.name}_file_checks_immutable",
        f"{spec.name}_file_checks_no_truncate")]
    return bad


def immutability(cur, stmts, seed_fn):
    """{UPDATE, DELETE, TRUNCATE: error text or None} on tables holding rows;
    all in savepoints."""
    def body(cur):
        seed_fn(cur)
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
    name = ("table, ledger, unresolved and live table shape (UPDATE, DELETE "
            "and TRUNCATE are refused on the editions, the ledger and the "
            "unresolved table)")
    bad = shape_problems(cur, ZZ)

    def seed(cur):
        with env(cur) as e:
            path = e.make(D1, ids=range(2))
            rows, _ = m.parse_locations(path)
            mapped, unres = m.map_locations(rows, BOUNDS, {}, {}, api=Api())
            recs = [{**r, "snapshot_date": D1, "source_file": url(D1)}
                    for r in mapped]
            info = m.FileInfo(url(D1), path, "ab" * 32, "gate")
            m.apply_snapshot(cur, m._profile(ZZ), D1, recs,
                             fetched_on=FETCHED, info=info, unresolved=unres)
    for what, table, stmts in (
            ("editions", ZE, {"UPDATE": f"UPDATE public.{ZE} SET "
                              "provider_name = 'x'",
                              "DELETE": f"DELETE FROM public.{ZE}",
                              "TRUNCATE": f"TRUNCATE public.{ZE}"}),
            ("ledger", ZLEDGER, {"UPDATE": f"UPDATE public.{ZLEDGER} SET "
                                 "outcome = 'new'",
                                 "DELETE": f"DELETE FROM public.{ZLEDGER}",
                                 "TRUNCATE": f"TRUNCATE public.{ZLEDGER}"}),
            ("unresolved", ZUNRES, {"UPDATE": f"UPDATE public.{ZUNRES} SET "
                                    "reason = 'x'",
                                    "DELETE": f"DELETE FROM public.{ZUNRES}",
                                    "TRUNCATE": f"TRUNCATE public.{ZUNRES}"})
    ):
        for k, msg in immutability(cur, stmts, seed).items():
            if not (msg and "append-only" in msg):
                bad.append(f"{what} {k} not refused ({msg})")
    scope = "throwaway copy"
    if table_exists(cur):
        scope += " and real tables"
        bad += [f"real: {p}" for p in shape_problems(cur, SPEC)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise append-only "
           "on the editions table, the ledger and the unresolved table")


# ------------------------------------------------------- real-table gates

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


def _tip_source(cur, spec, period):
    """(tip edition, its source_file(s))."""
    tip = core.chain_tip(cur, spec, period)
    cur.execute(f"SELECT DISTINCT source_file FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = %s", (period, tip))
    return tip, {r[0] for r in cur.fetchall()}


def real_edition1_and_latest(cur, spec=SPEC):
    """Edition 1 for every live snapshot (with its source_file); the latest
    edition equals live, cell for cell (every data column, NULL-safe)."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live snapshots to check (an empty state is not a pass)"
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "source_file IS NOT NULL")
    have = {r[0] for r in cur.fetchall()}
    missing = [_s(p) for p in periods if p not in have]
    if missing:
        return False, (f"no edition 1 (with a source_file) for {missing[:6]} "
                       f"({len(missing)} of {len(periods)})")
    bad = load_checks.check_latest_equals_live(cur, spec)
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} live snapshots each have edition 1; the latest "
        "edition equals live cell for cell")


def real_source_uniform(cur, spec=SPEC):
    """Every live row of a snapshot carries one source_file: the latest
    edition's."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live snapshots to check (an empty state is not a pass)"
    bad = []
    for p in periods:
        try:
            tip, want = _tip_source(cur, spec, p)
        except (LookupError, ValueError) as e:
            bad.append(str(e))
            continue
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.live_table} WHERE {spec.period_col} = %s",
                    (p,))
        got = {r[0] for r in cur.fetchall()}
        if len(want) != 1:
            bad.append(f"{_s(p)}: tip ed{tip} has source_file {sorted(want)}")
        elif got != want:
            bad.append(f"{_s(p)}: live source_file {sorted(got)[:3]} is not "
                       f"uniformly the tip's {sorted(want)[0]!r}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} snapshots: every live row carries the tip "
        "edition's source_file")


def real_coverage(cur, spec=SPEC, valid=None, want_n=296):
    """lad24cd in la_boundaries, want_n (296) authorities in every snapshot
    (live and every edition), no NULL lad24cd."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live snapshots to check (an empty state is not a pass)"
    if valid is None:
        cur.execute("SELECT lad24cd FROM public.la_boundaries")
        valid = {r[0] for r in cur.fetchall()}
    bad = []
    for table in (spec.live_table, spec.editions_table):
        cur.execute(f"SELECT COUNT(*) FROM public.{table} WHERE lad24cd IS "
                    "NULL")
        if cur.fetchone()[0]:
            bad.append(f"{table}: NULL lad24cd")
        cur.execute(f"SELECT DISTINCT lad24cd FROM public.{table}")
        stray = sorted(r[0] for r in cur.fetchall()
                       if r[0] is not None and r[0] not in valid)
        if stray:
            bad.append(f"{table}: {stray[:4]} not in la_boundaries")
        extra = "" if table == spec.live_table else ", edition"
        cur.execute(f"SELECT {spec.period_col}{extra}, COUNT(DISTINCT "
                    f"lad24cd) FROM public.{table} GROUP BY 1{', 2' if extra else ''}"
                    " ORDER BY 1")
        for row in cur.fetchall():
            if row[-1] != want_n:
                bad.append(f"{table} {_s(row[0])}"
                           + (f" ed{row[1]}" if extra else "")
                           + f": {row[-1]} authorities, expected {want_n}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} snapshots, each {want_n} authorities in live and in "
        "every edition, all in la_boundaries, none NULL")


def real_one_row(cur, spec=SPEC):
    """One row per location per snapshot (per edition in the editions
    table), no blank location_id in any table."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live snapshots to check (an empty state is not a pass)"
    pc, bad = spec.period_col, []
    for table, grp in ((spec.live_table, pc),
                       (spec.editions_table, f"{pc}, edition")):
        cur.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM public.{table} "
                    f"GROUP BY {grp}, location_id HAVING COUNT(*) > 1) q")
        if cur.fetchone()[0]:
            bad.append(f"{table}: a location twice in one snapshot")
    for table in (spec.live_table, spec.editions_table,
                  m.unresolved_table(spec)):
        cur.execute(f"SELECT COUNT(*) FROM public.{table} WHERE "
                    "btrim(location_id) = ''")
        if cur.fetchone()[0]:
            bad.append(f"{table}: blank location_id")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} snapshots: one row per location per snapshot and "
        "edition, no blank location_id")


NULLABLE_VALUES = tuple(c for c, sql in m.COLUMN_TYPES if "NOT NULL" not in sql)
NUMERIC_VALUES = ("care_homes_beds", "latitude", "longitude")


def null_zero_counts(cur, table, where="", params=()):
    """(NULL count per nullable value column, zero count per numeric column)
    as a flat tuple."""
    parts = [f"COUNT(*) FILTER (WHERE {c} IS NULL)" for c in NULLABLE_VALUES]
    parts += [f"COUNT(*) FILTER (WHERE {c} = 0)" for c in NUMERIC_VALUES]
    cur.execute(f"SELECT {', '.join(parts)} FROM public.{table} {where}",
                params)
    return cur.fetchone()


def real_null_vs_zero(cur, spec=SPEC):
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live snapshots to check (an empty state is not a pass)"
    bad = load_checks.check_latest_equals_live(cur, spec)
    pc = spec.period_col
    for p in periods:
        try:
            ed = core.chain_tip(cur, spec, p)
        except (LookupError, ValueError) as e:
            bad.append(str(e))
            continue
        ln = null_zero_counts(cur, spec.live_table, f"WHERE {pc} = %s", (p,))
        en = null_zero_counts(cur, spec.editions_table,
                              f"WHERE {pc} = %s AND edition = %s", (p, ed))
        if ln != en:
            bad.append(f"{_s(p)}: live (NULL, 0) counts {ln} vs ed{ed} {en}")
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.editions_table} WHERE "
                "care_homes_beds < 0")
    if cur.fetchone()[0]:
        bad.append("negative care_homes_beds")
    return not bad, "; ".join(bad[:3]) if bad else (
        "NULL and 0 counts per snapshot and column identical in live and the "
        "latest edition; EXCEPT comparison clean")


METHODS = ("point_in_polygon", "nearest_fallback", "postcode_api_fallback",
           "postcode_terminated_fallback")


def real_markers(cur, spec=SPEC):
    """Stored values obey the marker rules: brand '-' is NULL, no blank text
    stored as '', care homes with beds >= 0, coordinates inside England, a
    known mapping method, flags non-NULL (the column types)."""
    bad = []
    lo_la, hi_la = m.LAT_RANGE
    lo_lo, hi_lo = m.LON_RANGE
    for table in (spec.live_table, spec.editions_table):
        cur.execute(f"""SELECT
            COUNT(*) FILTER (WHERE brand_name = '-'),
            COUNT(*) FILTER (WHERE btrim(provider_name) = '' OR
                btrim(location_name) = '' OR btrim(brand_name) = '' OR
                btrim(postcode) = ''),
            COUNT(*) FILTER (WHERE latitude NOT BETWEEN %s AND %s OR
                longitude NOT BETWEEN %s AND %s),
            COUNT(*) FILTER (WHERE mapping_method <> ALL(%s)),
            COUNT(*) FILTER (WHERE dormant IS NULL OR care_home IS NULL OR
                supported_living IS NULL OR dual_registered IS NULL),
            COUNT(*) FILTER (WHERE inspection_directorate <> %s),
            COUNT(*)
            FROM public.{table}""", (lo_la, hi_la, lo_lo, hi_lo,
                                     list(METHODS), m.SCOPE))
        dash, blank, out, meth, nulls, scope, n = cur.fetchone()
        for label, v in (("brand '-' stored", dash), ("blank text stored "
                         "as ''", blank), ("coordinates outside England",
                                           out), ("unknown mapping_method",
                                                  meth), ("NULL flag", nulls),
                         ("not Adult social care", scope)):
            if v:
                bad.append(f"{table}: {v} rows with {label}")
        if not n:
            bad.append(f"{table}: no rows (an empty state is not a pass)")
    return not bad, "; ".join(bad[:3]) if bad else (
        "no brand '-', no blank text, coordinates inside England, known "
        "mapping methods, no NULL flag, only the Adult social care "
        "directorate, in live and the editions")


def real_unresolved_absent(cur, spec=SPEC):
    """The unresolved rows of every snapshot's tip edition are absent from
    live and from that edition; the latest snapshot has its unresolved rows
    recorded (a snapshot with none is listed, not failed)."""
    periods = _live_periods(cur, spec)
    if not periods:
        return False, "no live snapshots to check (an empty state is not a pass)"
    un, pc, bad, counts = m.unresolved_table(spec), spec.period_col, [], {}
    for p in periods:
        try:
            tip = core.chain_tip(cur, spec, p)
        except (LookupError, ValueError) as e:
            bad.append(str(e))
            continue
        cur.execute(f"SELECT location_id FROM public.{un} WHERE "
                    f"{pc} = %s AND edition = %s", (p, tip))
        ids = [r[0] for r in cur.fetchall()]
        counts[_s(p)] = len(ids)
        if ids:
            cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} WHERE "
                        f"{pc} = %s AND location_id = ANY(%s)", (p, ids))
            in_live = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM public.{spec.editions_table} "
                        f"WHERE {pc} = %s AND edition = %s AND location_id = "
                        "ANY(%s)", (p, tip, ids))
            in_ed = cur.fetchone()[0]
            if in_live or in_ed:
                bad.append(f"{_s(p)} ed{tip}: {in_live} unresolved "
                           f"location(s) in live, {in_ed} in the edition")
    cur.execute(f"SELECT COUNT(*) FROM public.{un} u WHERE NOT EXISTS "
                f"(SELECT 1 FROM public.{spec.editions_table} e WHERE "
                f"e.{pc} = u.{pc} AND e.edition = u.edition)")
    if cur.fetchone()[0]:
        bad.append("unresolved rows for an edition that does not exist")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"unresolved per snapshot (tip edition) {counts}: none is in live or "
        "in the edition")


def real_on_disk(cur, spec=SPEC, raw_dir=None):
    """Each file in data/raw/ whose sha256 is the file of a snapshot's tip
    edition (the ledger's new/revised row of that edition) parses, with the
    loader's own parse_locations, to that tip exactly on every publisher
    column; the in-scope locations the tip lacks are its unresolved rows.
    Read-only."""
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    pc = spec.period_col
    led = pe.file_checks_table(m._profile(spec))
    cur.execute(f"SELECT DISTINCT {pc} FROM public.{spec.editions_table} "
                "ORDER BY 1")
    periods = [r[0] for r in cur.fetchall()]
    if not periods:
        return False, "no editions to check (an empty state is not a pass)"
    tips = {}
    for p in periods:
        tip = core.chain_tip(cur, spec, p)
        cur.execute(f"SELECT DISTINCT file_sha256 FROM public.{led} WHERE "
                    f"{pc} = %s AND edition = %s AND outcome IN ('new', "
                    "'revised')", (p, tip))
        for (sha,) in cur.fetchall():
            tips[sha] = (p, tip)
    cols = ("location_id",) + m.PUBLISHER_COLUMNS
    files = sorted(f for f in raw.glob("*HSCA_Active_Locations*")
                   if f.suffix.lower() in (".ods", ".xlsx"))
    ok_n, bad, skipped = 0, [], 0
    for f in files:
        hit = tips.get(m.content_sha256(f))
        if hit is None:
            skipped += 1
            continue
        p, tip = hit
        rows, _ = m.parse_locations(f)
        got = {r["location_id"]: tuple(r[c] for c in cols) for r in rows}
        cur.execute(f"SELECT {', '.join(cols)} FROM "
                    f"public.{spec.editions_table} WHERE {pc} = %s AND "
                    "edition = %s", (p, tip))
        held = {r[0]: tuple(r) for r in cur.fetchall()}
        cur.execute(f"SELECT location_id FROM public.{m.unresolved_table(spec)}"
                    f" WHERE {pc} = %s AND edition = %s", (p, tip))
        unres = {r[0] for r in cur.fetchall()}
        diff = [k for k in held if got.get(k) != held[k]]
        extra = set(got) - set(held)
        if diff or extra != unres:
            bad.append(f"{_s(p)} ed{tip}: {f.name} differs from the tip in "
                       f"{len(diff)} location(s); {len(extra - unres)} file "
                       f"location(s) neither stored nor unresolved, "
                       f"{len(unres - extra)} unresolved not in the file")
        else:
            ok_n += 1
    if not ok_n and not bad:
        return False, (f"no file in {raw} is the file of a held tip "
                       f"({len(periods)} snapshots, {len(files)} files; an "
                       "empty comparison is not a pass)")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{ok_n} on-disk file(s) parse to their tip exactly; {skipped} "
        "other file(s) are not a held tip's, skipped")


def gate_2_edition1_and_latest(cur):
    real_gate(cur, 2, "every live snapshot has edition 1; the latest edition "
              "equals live cell for cell", real_edition1_and_latest)


def gate_3_source_uniform(cur):
    real_gate(cur, 3, "live source_file uniform per snapshot and equal to "
              "the tip's", real_source_uniform)


def gate_4_coverage(cur):
    real_gate(cur, 4, "every lad24cd in la_boundaries, 296 authorities in "
              "every snapshot, no NULL lad24cd", real_coverage)


def gate_5_one_row(cur):
    real_gate(cur, 5, "one row per location per snapshot, no blank "
              "location_id", real_one_row)


# ---------------------------------------------------------------- gate 6

def gate_6_null_vs_zero(cur):
    name = ("NULL versus 0 never conflated (blank beds NULL, 0 beds 0, "
            "either direction on a recheck needs --acknowledge; an "
            "unparseable value halts)")
    bad = []

    def first(e):
        e.make(D3, ids=range(8), over={5: {"Care homes beds": ""},
                                       6: {"Care homes beds": "0"},
                                       7: {"Care homes beds": "12"}})
        rc, text, _, _ = e.run(["load", "--commit"], page=url(D3))
        out = {}
        for table, extra in ((ZE, "AND edition = 1"), (ZL, "")):
            cur2 = e.cur
            cur2.execute(f"SELECT location_id, care_homes_beds IS NULL, "
                         f"care_homes_beds FROM public.{table} WHERE "
                         f"snapshot_date = %s {extra} AND location_id IN "
                         "('1-5', '1-6', '1-7') ORDER BY 1", (D3,))
            out[table] = cur2.fetchall()
        return rc, text, out

    def direction(frm, to):
        """A recheck of D3 flipping location 1-5 from frm to to."""
        def body(e):
            e.make(D3, ids=range(8), over={5: {"Care homes beds": frm}})
            rc0, _, _, _ = e.run(["load", "--commit"], page=url(D3))
            before = state(e.cur)
            re = url(D3, "2026-10-reissue")
            e.make(D3, re, ids=range(8), over={5: {"Care homes beds": to}})
            rc1, t1, _, l1 = e.run(["load", "--commit"], page=re)
            held = state(e.cur) == before
            rc2, t2, _, _ = e.run(["load", "--commit", "--acknowledge", D3],
                                  page=re)
            eds = editions_of(e.cur, D3)
            e.cur.execute(f"SELECT care_homes_beds FROM public.{ZE} WHERE "
                          "snapshot_date = %s AND edition = 2 AND "
                          "location_id = '1-5'", (D3,))
            return (rc0, rc1, t1, held, l1.called, rc2, t2, eds,
                    e.cur.fetchone())
        return scenario(cur, body, dates=())

    def unparseable(e):
        res = {}
        for label, over in (("beds 3.5", {"Care homes beds": "3.5"}),
                            ("latitude abc", {"Location Latitude": "abc"}),
                            ("date 31/12/2020", {
                                "Location HSCA start date": "31/12/2020"})):
            e.make(D3, ids=range(8), over={2: over})
            before = state(e.cur)
            rc, text, conn, logged = e.run(["load", "--commit"], page=url(D3))
            res[label] = (rc == "halt" and state(e.cur) == before
                          and not logged.called)
        return res
    try:
        rc, text, got = scenario(cur, first, dates=())
        z2n = direction("0", "")
        n2z = direction("", "0")
        un = scenario(cur, unparseable, dates=())
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(6, name, False, f"seeded scenario: {ex}")
    want = [("1-5", True, None), ("1-6", False, 0), ("1-7", False, 12)]
    if rc != 0 or got[ZE] != want or got[ZL] != want:
        bad.append(f"blank/0/12 beds stored as {got} (rc {rc}); expected "
                   f"{want}")
    for label, r, cell in (("0 to NULL", z2n, (None,)),
                           ("NULL to 0", n2z, (0,))):
        rc0, rc1, t1, held, logged, rc2, t2, eds, c = r
        if not (rc0 == 0 and rc1 == 1 and "(rule 1.10)" in t1 and held
                and not logged and rc2 == 0 and "ACKNOWLEDGED" in t2
                and eds == [(1, None, 8), (2, 1, 8)] and c == cell):
            bad.append(f"{label}: rc {rc1}, stored nothing={held}, "
                       f"acknowledged rc {rc2}, editions {eds}, cell {c}")
    if not all(un.values()):
        bad.append(f"an unparseable value did not halt cleanly: {un}")
    notes = ("blank beds stored NULL and 0 beds stored 0 in editions and "
             "live; 0 to NULL and NULL to 0 on a recheck REJECTED without "
             "--acknowledge and stored (cell read back) with it; an "
             "unparseable bed count, latitude and date each halt with "
             "nothing stored")
    if bad:
        return report(6, name, False, "seeded scenario: " + "; ".join(bad[:3]))
    if not table_exists(cur):
        return report(6, name, False, f"{NOT_YET}; seeded scenario passes: "
                      + notes, pending=True)
    real_gate(cur, 6, name, lambda c: (lambda r: (r[0], f"{r[1]}; seeded "
              f"scenario: {notes}"))(real_null_vs_zero(c)))


# ---------------------------------------------------------------- gate 7

def gate_7_markers(cur):
    name = ("markers: Y/blank service flags, Y/N care home and dormant, "
            "inherited Y/N/blank, dual, brand '-' is NULL; anything else "
            "halts")
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)

        def parse(label, lv):
            p = write_ods(t / f"{label}.ods", locations=[lv])
            return m.parse_locations(p)[0][0]

        rec = parse("ok", loc("1-1", brand="-", inherited="",
                              **{"Location Dual Registered":
                                 "Dual Registration",
                                 "Primary ID (Dual registration locations)":
                                 "1-9",
                                 "Service type - Supported living service":
                                 "Y"}))
        yes = parse("y", loc("1-1", inherited="Y"))
        no = parse("n", loc("1-1", inherited="N"))

        def bad_file(label, **over):
            return _raises(lambda: parse(label, loc("1-1", **over)))
        cases = {
            "flag X": bad_file("a", supported_living="X"),
            "N in a band column": bad_file(
                "b", **{"Service user band - Dementia": "N"}),
            "blank dormant": bad_file("c", dormant=""),
            "blank care home": bad_file("d", care_home=""),
            "inherited maybe": bad_file("e", inherited="maybe"),
            "dual marker other": bad_file(
                "f", **{"Location Dual Registered": "Yes"}),
        }
    ok = (rec["brand_name"] is None and rec["inherited_rating"] is None
          and rec["dual_registered"] is True and rec["supported_living"]
          is True and rec["band_dementia"] is False and rec["dormant"]
          is False and rec["care_home"] is False
          and yes["inherited_rating"] is True
          and no["inherited_rating"] is False
          and all(v[0] for v in cases.values()))
    notes = ("seeded parse: brand '-' NULL, inherited blank NULL / Y true / "
             "N false, dual marker true, blank service flag false; halts: "
             + ", ".join(f"{k}={v[0]}" for k, v in cases.items()))
    if not ok:
        return report(7, name, False, "seeded parse: " + notes)
    if not table_exists(cur):
        return report(7, name, False, f"{NOT_YET}; {notes}", pending=True)
    real_gate(cur, 7, name, lambda c: (lambda r: (r[0], f"{r[1]}; {notes}"))(
        real_markers(c)))


# ---------------------------------------------------------------- gate 8

def gate_8_identity(cur):
    name = ("file identity: README title and as-at date against the file "
            "name, caught before any write")

    def body(e):
        out = {}
        d = Path(e.tmp)

        def file(label, fname, **kw):
            (d / label).mkdir()
            return write_ods(d / label / fname, locations=e.locations(),
                             **kw)
        cases = {
            "the README title is another file's": file(
                "a", ods_name(date(2026, 10, 1)), title="Deactivated "
                "locations"),
            "the README says another as-at date": file(
                "b", ods_name(date(2026, 10, 1)), as_at="02 October 2026"),
            "the README has no as-at line": file(
                "c", ods_name(date(2026, 10, 1)), as_at="sometime"),
            "the file name has no date": file("d", "HSCA_Active_Locations"
                                              ".ods", as_at="01 October 2026"),
        }
        for label, f in cases.items():
            before = state(e.cur)
            with mock.patch.object(m, "apply_snapshot",
                                   side_effect=AssertionError("wrote")), \
                    mock.patch.object(pe, "load_periods") as lp:
                rc, text, conn, logged = e.run(["load", "--commit"], file=f)
            out[label] = (rc == "halt" and "identity" in text
                          and not lp.called and conn.cur.connection.commits
                          == 0 and not logged.called
                          and state(e.cur) == before)
        good = file("e", ods_name(date(2026, 10, 1)),
                    as_at="01 October 2026")
        rc, text, _, _ = e.run(["load"], file=good)
        return out, rc, state(e.cur)[1:3]
    try:
        out, good_rc, (eds, led) = scenario(cur, body, dates=(D1,))
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(8, name, False, str(ex).splitlines()[0])
    rd = {"title": m.TITLE, "as_at": date(2026, 10, 1)}
    unit = (m.check_identity(rd, ods_name(date(2026, 10, 1))) == []
            and len(m.check_identity(dict(rd, title="x"),
                                     ods_name(date(2026, 10, 1)))) == 1
            and len(m.check_identity(dict(rd, as_at=date(2026, 10, 2)),
                                     ods_name(date(2026, 10, 1)))) == 1
            and len(m.check_identity(dict(rd, as_at=None),
                                     ods_name(date(2026, 10, 1)))) == 1)
    ok = all(out.values()) and good_rc == 0 and unit
    report(8, name, ok, "; ".join(
        f"{k}: {'halted before any write' if v else 'NOT CAUGHT'}"
        for k, v in out.items()) + f"; a matching file previews (rc "
        f"{good_rc}); pure check_identity rules={unit}")


# ---------------------------------------------------------------- gate 9

def gate_9_older_file_guard(cur):
    name = ("older-file guard on the page and --file paths, ranked by the "
            "README date (never the name or the link); --allow-older-file "
            "overrides")

    def body(e):
        f0 = e.make(D0)
        before = state(e.cur)
        halts, f_halts = {}, {}
        for argv in (["load"], ["load", "--commit"], ["load", "--simulate"]):
            rc, text, _, logged = e.run(argv, page=url(D0))
            halts[" ".join(argv)] = (
                rc == "halt" and "earlier than the latest held snapshot"
                in text and "--allow-older-file" in text
                and not logged.called)
            rc, text, _, logged = e.run(argv, file=f0)
            f_halts[" ".join(argv)] = (
                rc == "halt" and f"as at {D0}, earlier than the latest "
                f"held snapshot {D2}" in text and "--allow-older-file"
                in text and not logged.called)
        unchanged = state(e.cur) == before
        # ranked by the README date: a file NAMED as the newest whose README
        # says July, with the identity check out of the way
        (Path(e.tmp) / "n").mkdir()
        sneaky = write_ods(Path(e.tmp) / "n" / ods_name(date.fromisoformat(
            D3)), as_at="01 July 2026", locations=e.locations(ids=range(8)))
        with mock.patch.object(m, "check_identity", return_value=[]):
            rc_s, t_s, _, _ = e.run(["load", "--commit"], file=sneaky)
        sneaky_ok = (rc_s == "halt" and f"as at {D0}, earlier than the "
                     f"latest held snapshot {D2}" in t_s
                     and state(e.cur) == before)
        # the current file and a recheck of a held older one are unaffected
        rc_cur, t_cur, _, _ = e.run(["load", "--commit"], page=url(D2))
        e.make(D1, url(D1, "2026-08-reissue"))
        rc_rc, t_rc, _, _ = e.run(["load", "--recheck", D1],
                                  page=url(D1, "2026-08-reissue"))
        rc_allow, t_allow, _, logged = e.run(
            ["load", "--commit", "--allow-older-file"], file=f0)
        e.cur.execute(f"SELECT release_label FROM public.{ZE} WHERE "
                      "snapshot_date = %s", (D0,))
        label = e.cur.fetchone()
        return (halts, f_halts, unchanged, sneaky_ok, rc_cur, t_cur, rc_rc,
                rc_allow, t_allow, logged.called, editions_of(e.cur, D0),
                label)
    try:
        (halts, f_halts, unchanged, sneaky_ok, rc_cur, t_cur, rc_rc,
         rc_allow, t_allow, logged, e0, label) = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(9, name, False, str(ex).splitlines()[0])
    held = {D1: "x", D2: "x"}
    tips = held
    plan_ok = _halts_value(lambda: m.plan_load(
        D2, D0, tips, {}, "u", "sha", recheck=None, allow_older=False))
    plan_allow = m.plan_load(D2, D0, tips, {}, "u", "sha", recheck=None,
                             allow_older=True)
    plan_same = m.plan_load(D2, D1, tips, {}, "u", "sha", recheck=D1,
                            allow_older=False)
    pure = (plan_ok and plan_allow[0] == "new"
            and "--allow-older-file given" in plan_allow[1]
            and plan_same[0] == "recheck"
            and m.plan_load(D2, D3, tips, {}, "u", "sha", recheck=None,
                            allow_older=False)[0] == "new")
    ok = (all(halts.values()) and all(f_halts.values()) and unchanged
          and sneaky_ok and rc_cur == 0 and "already in the ledger" in t_cur
          and rc_rc == 0 and rc_allow == 0
          and "--allow-older-file given" in t_allow and logged
          and e0 == [(1, None, N)] and label
          and "--allow-older-file" in label[0] and pure)
    report(9, name, ok, f"older page link in preview/commit/simulate halted="
           f"{all(halts.values())}; older --file halted="
           f"{all(f_halts.values())}; stores unchanged={unchanged}; a file "
           f"named October whose README says July halts on the README "
           f"date={sneaky_ok}; the current file ({rc_cur}) and a recheck of "
           f"a held older snapshot ({rc_rc}) are unaffected; "
           f"--allow-older-file stores it ({rc_allow}) and labels it="
           f"{bool(label and '--allow-older-file' in label[0])}; plan_load "
           f"rules={pure}")


def _halts_value(fn):
    return _raises(fn)[0]


# --------------------------------------------------------------- gate 10

def _synthetic(ids, auth="E06000001", sl=False, authorities=None):
    return [{"location_id": f"x{i}",
             "lad24cd": (authorities[i % len(authorities)] if authorities
                         else auth),
             "supported_living": sl, "dormant": False} for i in ids]


def gate_10_stop_conditions(cur):
    name = ("stop conditions: a seeded snapshot over each threshold is "
            "REJECTED and stores nothing; the limits themselves pass")
    problems = []

    def rejected(label, snap_kw, expect, *, ok_at_limit=None):
        def body(e):
            e.make(D3, **snap_kw)
            before = state(e.cur)
            res = {}
            for flag in ("--commit", "--simulate"):
                rc, text, _, logged = e.run(["load", flag], page=url(D3))
                res[flag] = (rc == 1 and expect in text and "REJECTED "
                             f"{D3}, nothing stored; exit 1" in text
                             and state(e.cur) == before
                             and not logged.called)
            return res
        try:
            res = scenario(cur, body)
        except (psycopg2.Error, SystemExit, RuntimeError) as ex:
            problems.append(f"{label}: {ex}")
            return
        if not all(res.values()):
            problems.append(f"{label}: {res}")

    def passes(label, snap_kw):
        def body(e):
            e.make(D3, **snap_kw)
            rc, text, _, _ = e.run(["load"], page=url(D3))
            return rc == 0 and "REJECTED" not in text
        try:
            if not scenario(cur, body):
                problems.append(f"{label}: the limit itself was rejected")
        except (psycopg2.Error, SystemExit, RuntimeError) as ex:
            problems.append(f"{label}: {ex}")

    unres = lambda k: [loc(f"1-X{i}", "", "", postcode=f"ZZ{i} 1ZZ")
                       for i in range(k)]
    rejected("11 unresolved", {"extra": unres(10)},
             "11 unresolved locations (limit 10)")
    passes("10 unresolved", {"extra": unres(9)})
    rejected("rows +3.3%", {"ids": range(N + 2)}, "in-scope rows 60 -> 62")
    passes("rows +1.7%", {"ids": range(N + 1)})
    rejected("an authority with no location",
             {"ids": [i for i in range(N + 30) if i % 3 != 2]},
             f"authorities with no location ['{C}']")

    def too_far(e):
        e.make(D3, extra=[loc("1-FAR", "54.500000", "-1.500000")])
        before = state(e.cur)
        rc, text, _, logged = e.run(["load", "--commit"], page=url(D3))
        return (rc == "halt" and "over 2000 m" in text
                and state(e.cur) == before and not logged.called)
    try:
        if not scenario(cur, too_far):
            problems.append("a location over 2 km from every polygon was "
                            "not refused")
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        problems.append(f"nearest over 2 km: {ex}")
    # thresholds that need thousands of rows, on the loader's own rule
    auth = ["E06000001"]
    base = _synthetic(range(50000), authorities=auth)
    def probs(new, unresolved=0):
        return m.snapshot_problems(
            {"records": new, "unresolved": unresolved, "authorities": auth},
            {"records": base})
    gone_1001 = probs(_synthetic(range(1001, 50000), authorities=auth))
    gone_1000 = probs(_synthetic(range(1000, 50000), authorities=auth))
    new_1001 = probs(base + _synthetic(range(60000, 61001),
                                       authorities=auth))
    new_1000 = probs(base + _synthetic(range(60000, 61000),
                                       authorities=auth))
    if not (any("1,001 locations gone" in p for p in gone_1001)
            and not any("gone" in p for p in gone_1000)
            and any("1,001 locations new" in p for p in new_1001)
            and not any("locations new" in p for p in new_1000)):
        problems.append("gone/new limit of 1,000 not enforced exactly")
    codes = [f"E0600{i:04d}" for i in range(6)]

    def sl_probs(changed):
        old = [{"location_id": f"{c}-{k}", "lad24cd": c,
                "supported_living": True, "dormant": False}
               for c in codes for k in range(20)]
        new = [dict(r, supported_living=not (r["lad24cd"] in codes[:changed]
                                             and int(r["location_id"]
                                                     .rsplit("-", 1)[1]) < 15))
               for r in old]
        return m.snapshot_problems(
            {"records": new, "unresolved": 0, "authorities": codes},
            {"records": old})
    if not (any("supported-living" in p for p in sl_probs(6))
            and not any("supported-living" in p for p in sl_probs(5))):
        problems.append("supported-living limit (more than 5 authorities) "
                        "not enforced exactly")
    report(10, name, not problems, "; ".join(problems[:3]) if problems else
           "REJECTED with nothing stored (and no run log row) in preview-"
           "commit and simulate: 11 unresolved, rows +3.3%, an authority "
           "with no location; a location over 2 km from every polygon "
           "halts; the limits pass (10 unresolved, +1.7%); gone/new 1,001 "
           "over and 1,000 pass; supported living in 6 authorities over, 5 "
           "pass")


# --------------------------------------------------------------- gate 11

def gate_11_one_transaction(cur):
    name = ("new snapshot in one transaction: edition 1, live, unresolved "
            "and ledger rows commit or roll back together")
    seen = {}

    def stored(e):
        e.make(D3)
        rc, text, _, logged = e.run(["load", "--commit"], page=url(D3))
        return (rc, editions_of(e.cur, D3), count(
            e.cur, ZL, "WHERE snapshot_date = %s", (D3,)),
            unresolved_of(e.cur, D3), ledger_of(e.cur, D3), logged.called)

    def live_fails(e):
        e.make(D3)
        real = m.insert_live

        def boom(c, profile, period, records):
            seen["editions"] = sum(r[2] for r in editions_of(c, period))
            real(c, profile, period, records[:10])
            raise RuntimeError("live insert failed (planted)")
        with mock.patch.object(m, "insert_live", side_effect=boom):
            rc, text, _, logged = e.run(["load", "--commit"], page=url(D3))
        return (rc, text, editions_of(e.cur, D3), count(
            e.cur, ZL, "WHERE snapshot_date = %s", (D3,)),
            unresolved_of(e.cur, D3), ledger_of(e.cur, D3), logged.called)

    def ledger_fails(e):
        e.make(D3)

        def boom(*a, **k):
            seen["unresolved"] = count(e.cur, ZUNRES,
                                       "WHERE snapshot_date = %s", (D3,))
            seen["live"] = count(e.cur, ZL, "WHERE snapshot_date = %s", (D3,))
            raise RuntimeError("ledger write failed (planted)")
        with mock.patch.object(pe, "record_file_check", side_effect=boom):
            rc, text, _, logged = e.run(["load", "--commit"], page=url(D3))
        return (rc, text, editions_of(e.cur, D3), count(
            e.cur, ZL, "WHERE snapshot_date = %s", (D3,)),
            unresolved_of(e.cur, D3), ledger_of(e.cur, D3), logged.called)
    try:
        rc, eds, live_n, un, led, logged = scenario(cur, stored)
        f = scenario(cur, live_fails)
        l = scenario(cur, ledger_fails)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(11, name, False, str(ex).splitlines()[0])
    ok = (rc == 0 and eds == [(1, None, N)] and live_n == N
          and un == [(1, NOXY)] and led == [(tail(url(D3)), "new", 1)]
          and logged
          and f[0] == 1 and "planted" in f[1] and f[2:6] == ([], 0, [], [])
          and not f[6] and seen.get("editions") == N
          and l[0] == 1 and "planted" in l[1] and l[2:6] == ([], 0, [], [])
          and not l[6] and seen.get("unresolved") == 1
          and seen.get("live") == N)
    report(11, name, ok, f"stored: editions {[(e, s) for e, s, _ in eds]}, "
           f"live {live_n}, unresolved {un}, ledger {led}; planted live "
           f"failure (edition rows written first={seen.get('editions') == N})"
           f": rc {f[0]}, afterwards editions {f[2]} live {f[3]} unresolved "
           f"{f[4]} ledger {f[5]}; planted ledger failure (live {seen.get('live')} "
           f"and unresolved {seen.get('unresolved')} rows written first): "
           f"rc {l[0]}, afterwards editions {l[2]} live {l[3]} unresolved "
           f"{l[4]} ledger {l[5]}")


# --------------------------------------------------------------- gate 12

def gate_12_ledger(cur):
    name = ("ledger: an unchanged recheck is recorded once and skipped on "
            "the next run; --recheck reads it again")

    def body(e):
        reissue = url(D2, "2026-09-reissue")
        # same in-scope rows, different file bytes (a hospital row)
        e.make(D2, reissue, extra=[loc("1-H1", directorate="Hospitals")])
        rc1, t1, _, _ = e.run(["load", "--commit"], page=reissue)
        f1 = e.fetched[:]
        e1, l1 = editions_of(e.cur, D2), ledger_of(e.cur, D2)
        e.fetched.clear()
        rc2, t2, _, logged2 = e.run(["load", "--commit"], page=reissue)
        f2, l2 = e.fetched[:], ledger_of(e.cur, D2)
        e.fetched.clear()
        rc3, t3, _, _ = e.run(["load", "--commit", "--recheck", D2],
                              page=reissue)
        f3, l3 = e.fetched[:], ledger_of(e.cur, D2)
        checked = m.ledger_checks(e.cur, m._profile(ZZ))
        return (rc1, t1, f1, e1, l1, rc2, t2, f2, l2, logged2.called, rc3,
                f3, l3, checked)
    try:
        (rc1, t1, f1, e1, l1, rc2, t2, f2, l2, lg2, rc3, f3, l3,
         checked) = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(12, name, False, str(ex).splitlines()[0])
    re_ = url(D2, "2026-09-reissue")
    want = [(tail(url(D2)), "new", 1), (tail(re_), "unchanged", 1)]
    ok = (rc1 == 0 and f1 == [re_] and f"{D2}: unchanged" in t1
          and e1 == [(1, None, N)] and l1 == want
          and rc2 == 0 and f2 == [] and "nothing to fetch" in t2
          and l2 == want and not lg2
          and rc3 == 0 and f3 == [re_] and l3[-1] == (tail(re_), "unchanged", 1)
          and len(l3) == 3
          and any(s == re_ for s, _ in checked.get(D2, set())))
    report(12, name, ok, f"reissue fetched once ({len(f1)}), recorded "
           f"'unchanged' once ({l1[-1:]}), next run fetched {len(f2)} "
           f"('nothing to fetch'={'nothing to fetch' in t2}), ledger still "
           f"{len(l2)} rows; --recheck fetched {len(f3)} and recorded a "
           f"third row; editions stayed {e1}")


# --------------------------------------------------------------- gate 13

def gate_13_preview_writes_nothing(cur):
    name = ("preview and --simulate write nothing (no edition, live, ledger, "
            "unresolved or run log row)")

    def body(e):
        e.make(D3)
        re = url(D2, "2026-09-reissue")
        e.make(D2, re, over={3: {"Location Latest Overall Rating":
                                 "Outstanding"}})
        base = state(e.cur)
        out = {}
        for page in (url(D3), re):
            for word, flags in (("PREVIEW", []), ("SIMULATION",
                                                  ["--simulate"])):
                rc, text, conn, logged = e.run(["load"] + flags, page=page)
                out[(tail(page), word)] = (
                    rc == 0 and word in text and conn.commits == 0
                    and logged.called is False and state(e.cur) == base
                    and (": new" in text or ": revised" in text))
        rc, text, conn, logged = e.run(["load", "--commit"], page=url(D3))
        control = (rc == 0 and state(e.cur) != base and logged.called)
        return out, control
    try:
        out, control = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(13, name, False, str(ex).splitlines()[0])
    ok = all(out.values()) and control
    report(13, name, ok, f"new snapshot and changed reissue, each in "
           f"preview and --simulate: nothing written, no commit, no run log "
           f"row ({sum(out.values())} of {len(out)}); control --commit "
           f"stores={control}")


# --------------------------------------------------------------- gate 14

def gate_14_revision_and_refresh(cur):
    name = ("revision then refresh-latest changes only that snapshot and "
            "moves its source_file; a changed location set is REJECTED")

    def snap(cur2, d):
        cur2.execute(f"SELECT * FROM public.{ZL} WHERE snapshot_date = %s "
                     "ORDER BY location_id", (d,))
        return cur2.fetchall()

    def body(e):
        d1b = snap(e.cur, D1)
        d2b = snap(e.cur, D2)
        reissue = url(D2, "2026-09-reissue")
        e.make(D2, reissue, over={
            3: {"Location Latest Overall Rating": "Outstanding"},
            7: {"Location Latest Overall Rating": "Inadequate"}})
        rc, text, _, _ = e.run(["load", "--commit"], page=reissue)
        eds = editions_of(e.cur, D2)
        untouched = snap(e.cur, D2) == d2b
        pending = m.status(e.cur, ZZ)["pending_refresh"]
        plan = {_s(k): v for k, v in core.refresh_counts(e.cur, ZZ).items()}
        rc_r, text_r, _, _ = e.run(["refresh-latest", "--commit"])
        d2a = snap(e.cur, D2)
        cols = [d.name for d in e.cur.description]
        isrc, irat, ilo = (cols.index("source_file"),
                           cols.index("latest_overall_rating"),
                           cols.index("loaded_at"))
        changed_rating = sum(1 for x, y in zip(d2b, d2a) if x[irat] != y[irat])
        only = all(all(x[i] == y[i] for i in range(len(x))
                       if i not in (isrc, irat)) for x, y in zip(d2b, d2a))
        sources = {r[isrc] for r in d2a}
        before = state(e.cur)
        short = url(D1, "2026-08-reissue")
        e.make(D1, short, ids=range(N - 1))
        rc_s, text_s, _, logged = e.run(
            ["load", "--commit", "--recheck", D1], page=short)
        rejected = (rc_s == 1 and "location set differs" in text_s
                    and "REJECTED" in text_s and state(e.cur) == before
                    and not logged.called)
        return (rc, eds, untouched, pending, plan, rc_r, len(d2a),
                changed_rating, only, sources, snap(e.cur, D1) == d1b,
                all(x[ilo] == y[ilo] for x, y in zip(d2b, d2a)),
                reissue, rejected, m.status(e.cur, ZZ)["ok"])
    try:
        (rc, eds, untouched, pending, plan, rc_r, n_rows, changed, only,
         sources, d1_same, lo_kept, reissue, rejected, st_ok) = scenario(
            cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(14, name, False, str(ex).splitlines()[0])
    ok = (rc == 0 and eds == [(1, None, N), (2, 1, N)] and untouched
          and pending == [D2] and plan == {D2: 2} and rc_r == 0
          and n_rows == N and changed == 2 and only
          and sources == {reissue} and d1_same and lo_kept and rejected
          and st_ok)
    report(14, name, ok, f"edition 2 stored, live untouched until refresh "
           f"({untouched}); plan {plan}; after refresh-latest {changed} "
           f"ratings changed in {D2}, only latest_overall_rating and "
           f"source_file differ={only}, source_file is the reissue on all "
           f"{n_rows} rows={sources == {reissue}}, loaded_at kept="
           f"{lo_kept}; {D1} untouched={d1_same}; a reissue with a changed "
           f"location set REJECTED, nothing stored={rejected}; status ok="
           f"{st_ok}")


# --------------------------------------------------------------- gate 15

def _private_dicts(path):
    """Problems: a dict literal keyed by a Barnsley/Sheffield code, or such
    a code in a string constant other than a docstring."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d is not None and node.body and isinstance(
                    node.body[0], ast.Expr):
                doc.add(id(node.body[0].value))
    pat = re.compile(r"E080000(16|19|38|39)(?![0-9])")
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc and pat.search(node.value):
            bad.append(f"line {node.lineno}: {node.value[:40]!r}")
    return bad


def gate_15_barnsley_sheffield(cur):
    name = ("Barnsley and Sheffield: form 'none', a file cell with "
            "E08000038 halts, API E08000039 maps to E08000019, no private "
            "dict in the module")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("?",))[0]
    srcs = _private_dicts(HERE / "s11_cqc_editions.py")
    recodes = geography.load_recodes(cur)
    bounds = [("E08000016", square(-1.6, 53.45, -1.4, 53.6)),
              ("E08000019", square(-1.6, 53.30, -1.4, 53.45))]
    rows = [{"location_id": "1-1", "latitude": None, "longitude": None,
             "postcode": "S1 2AB", "location_name": "x"}]
    api = Api(codes={"S1 2AB": "E08000039"})
    mapped, unres = m.map_locations(rows, bounds, recodes, {}, api=api)
    barns = Api(codes={"S1 2AB": "E08000038"})
    mapped_b, _ = m.map_locations(rows, bounds, recodes, {}, api=barns)
    unknown = _raises(lambda: m.map_locations(
        rows, bounds, recodes, {}, api=Api(codes={"S1 2AB": "E06999999"})))

    def body(e):
        res = {}
        for code in ("E08000038", "E08000039", "E08000016", "E08000019"):
            # plant the code in a cell the loader reads
            e.make(D3, over={4: {"Provider ID": code}})
            f = e.files[url(D3)]
            before = state(e.cur)
            with mock.patch.object(m, "apply_snapshot",
                                   side_effect=AssertionError("wrote")):
                rc, text, conn, logged = e.run(["load", "--commit"], file=f)
            res[code] = (rc == "halt" and code in text
                         and "declared 'none'" in text
                         and state(e.cur) == before and not logged.called
                         and conn.cur.connection.commits == 0)
        return res
    try:
        res = scenario(cur, body, dates=(D1,))
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(15, name, False, str(ex).splitlines()[0])
    ok = (decl == "none" and not srcs and all(res.values())
          and mapped[0]["lad24cd"] == "E08000019"
          and mapped[0]["mapping_method"] == "postcode_api_fallback"
          and mapped_b[0]["lad24cd"] == "E08000016" and not unres
          and unknown[0] and "UNEXPLAINED" in unknown[1])
    report(15, name, ok, f"source 11 declared {decl!r}; a file cell holding "
           f"E08000038/39/16/19 halts before any write="
           f"{all(res.values())}; API E08000039 -> {mapped[0]['lad24cd']} "
           f"({mapped[0]['mapping_method']}), API E08000038 -> "
           f"{mapped_b[0]['lad24cd']}; an API code that resolves nowhere "
           f"halts={unknown[0]}; Barnsley/Sheffield codes as strings in "
           f"s11_cqc_editions.py outside docstrings: {srcs or 'none'}")


# ------------------------------------------------------- migration world

MIG_FILES = {D0: dict(ids=[i for i in range(N) if i != 61] + [62]),
             D1: dict(ids=range(1, N), over={
                 4: {"Location Latest Overall Rating": "Outstanding"}}),
             D2: dict(ids=[0] + list(range(2, N)) + [61])}
D3_RETURN = dict(ids=list(range(0, 59)) + [61])


def _legacy_like(cur):
    """The relation to copy the legacy columns from: the real base table,
    or, once it has been swapped for the view, the renamed table."""
    for name in (m.LEGACY, m.LEGACY_RENAMED):
        if m.relkind(cur, name) == "r":
            return name
    raise RuntimeError("neither cqc_locations nor cqc_locations_legacy is a "
                       "base table")


def seed_legacy(cur, e):
    """The legacy table as the retired loader left it from the three files
    (each location's latest snapshot), its unresolved table, and the run
    log dict migrate_legacy reads. Returns (files, runs, by)."""
    paths, by = {}, {}
    for d, kw in MIG_FILES.items():
        paths[d] = e.make(d, **kw)
        rows, _ = m.parse_locations(paths[d])
        mapped, _ = m.map_locations(rows, BOUNDS, {}, {}, api=Api())
        by[d] = {r["location_id"]: r for r in mapped}
    cur.execute(f"CREATE TABLE public.{ZLEG} (LIKE public.{_legacy_like(cur)}"
                " INCLUDING ALL)")
    cur.execute(f"CREATE TABLE public.{ZLEGU} (LIKE "
                "public.cqc_unresolved_locations INCLUDING ALL)")
    dates = sorted(by)
    last = {}
    for d in dates:
        for k in by[d]:
            last[k] = d
    cols = ("location_id",) + m.VALUE_COLUMNS
    for k, d in last.items():
        i = dates.index(d)
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        r = by[d][k]
        cur.execute(
            f"INSERT INTO public.{ZLEG} ({', '.join(cols)}, is_active, "
            "deregistered_seen_date, source_file_date) VALUES ("
            + ", ".join(["%s"] * (len(cols) + 3)) + ")",
            [r[c] for c in cols] + [nxt is None, nxt, d])
    cur.execute(f"INSERT INTO public.{ZLEGU} (location_id, location_name, "
                "postcode, reason, first_seen_edition, last_seen_edition, "
                "editions_seen) VALUES (%s, 'x', 'ZZ9 9ZZ', 'postcode "
                "absent', %s, %s, 2)", (NOXY, D1, D2))
    runs = {d: [(d, len(by[d]))] for d in dates}
    return [paths[D2], paths[D0], paths[D1]], runs, by


def migrate(e, files, runs, argv=("--commit",)):
    return e.run(["migrate-legacy", *map(str, files), *argv],
                 savepoint="zz_mig", runs=runs)


def view_columns(cur, a, b):
    """Differences between the columns (order, name, type) of two
    relations."""
    def cols(t):
        cur.execute("""SELECT column_name, data_type,
                              character_maximum_length, numeric_precision,
                              numeric_scale
                       FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = %s
                       ORDER BY ordinal_position""", (t,))
        return cur.fetchall()
    ca, cb = cols(a), cols(b)
    if not ca or not cb:
        return [f"{a if not ca else b} has no columns"]
    if ca != cb:
        return [f"{x} vs {y}" for x, y in zip(ca, cb) if x != y][:3] or [
            f"{len(ca)} vs {len(cb)} columns"]
    return []


def w1_subquery(table):
    """The S11 subquery of sql/w1/05_la_signals.sql, read from the file, with
    cqc_locations replaced by `table`."""
    text = (HERE.parent / "sql" / "w1" / "05_la_signals.sql").read_text(
        encoding="utf-8")
    mt = re.search(r"LEFT JOIN \(\s*(SELECT lad24cd, COUNT\(\*\) AS sl_count"
                   r"\s+FROM cqc_locations.*?GROUP BY lad24cd)\s*\)\s*s11",
                   text, re.S)
    if not mt:
        raise ValueError("the S11 subquery was not found in "
                         "sql/w1/05_la_signals.sql")
    return mt.group(1).replace("FROM cqc_locations", f"FROM public.{table}")


def latest_counts(cur, live_table):
    cur.execute(f"""SELECT lad24cd, COUNT(*) FROM public.{live_table}
                    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM
                          public.{live_table})
                      AND supported_living AND NOT dormant
                    GROUP BY lad24cd""")
    return dict(cur.fetchall())


def migrated_world(cur):
    """migrate-legacy through the loader's real command on the seeded
    legacy table, then a fourth snapshot in which one location leaves and
    one returns. Returns what gates 16 and 17 check."""
    out = {}

    def body(c):
        with env(c) as e:
            files, runs, by = seed_legacy(c, e)
            rc, text, _, logged = migrate(e, files, runs)
            out["rc"], out["text"] = rc, text
            if rc != 0:
                return out
            out["cols"] = view_columns(c, ZLEG, ZREN)
            out["kinds"] = (m.relkind(c, ZLEG), m.relkind(c, ZREN))
            out["diff"] = m.view_differences(c, ZLEG, ZREN)
            wrong = m.VIEW_SQL.replace("ELSE (SELECT MIN(s.snapshot_date)",
                                       "ELSE (SELECT MAX(s.snapshot_date)")
            c.execute("SAVEPOINT wrong")
            c.execute(f"DROP VIEW public.{ZLEG}")
            c.execute(wrong.format(view=ZLEG, live=ZL, cols=", ".join(
                f"r.{x}" for x in ("location_id",) + m.VALUE_COLUMNS)))
            out["wrong_view_diff"] = m.view_differences(c, ZLEG, ZREN)
            c.execute("ROLLBACK TO SAVEPOINT wrong")
            out["w1_swap"] = (m.w1_counts(c, ZLEG), m.w1_counts(c, ZREN))
            c.execute(f"SELECT COUNT(*) FROM public.{ZLEG} WHERE is_active")
            out["active_after_migration"] = c.fetchone()[0]
            # the fourth snapshot: 1-59 leaves, 1-1 returns
            e.make(D3, **D3_RETURN)
            rc3, t3, _, _ = e.run(["load", "--commit"], page=url(D3))
            out["rc3"] = rc3
            c.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE "
                      "snapshot_date = %s", (D3,))
            out["d3_rows"] = c.fetchone()[0]
            c.execute(f"SELECT COUNT(*) FROM public.{ZLEG} WHERE is_active")
            out["active_after_d3"] = c.fetchone()[0]
            c.execute(f"""SELECT location_id, is_active,
                                 deregistered_seen_date, source_file_date
                          FROM public.{ZLEG}""")
            out["view"] = {r[0]: r[1:] for r in c.fetchall()}
            # the legacy upsert's result computed from the four files
            files4 = {D0: e.files[url(D0)], D1: e.files[url(D1)],
                      D2: e.files[url(D2)], D3: e.files[url(D3)]}
            seen = {}
            for d in sorted(files4):
                rows, _ = m.parse_locations(files4[d])
                mapped, _ = m.map_locations(rows, BOUNDS, {}, {}, api=Api())
                for r in mapped:
                    seen[r["location_id"]] = d
            ds = sorted(files4)
            out["expected"] = {
                k: (d == D3, None if d == D3 else date.fromisoformat(
                    ds[ds.index(d) + 1]), date.fromisoformat(d))
                for k, d in seen.items()}
            c.execute(w1_subquery(ZLEG))
            out["w1_view"] = dict(c.fetchall())
            out["w1_latest"] = latest_counts(c, ZL)
        return out
    return _in_savepoint(cur, body)


def gate_16_the_view(cur):
    name = ("the view: same columns and types as cqc_locations_legacy; "
            "equals legacy for the migrated state; seeded leave-and-return "
            "behaves as legacy")
    try:
        w = migrated_world(cur)
    except (psycopg2.Error, SystemExit, RuntimeError, ValueError) as ex:
        return report(16, name, False, f"seeded migration: {ex}")
    if w.get("rc") != 0:
        return report(16, name, False, "seeded migration failed: "
                      f"{w.get('text', '')[-300:]}")
    exp = {k: tuple(v) for k, v in w["expected"].items()}
    got = {k: tuple(v) for k, v in w["view"].items()}
    ok = (w["kinds"] == ("v", "r") and w["cols"] == [] and w["diff"] == (0, 0)
          and w["wrong_view_diff"] != (0, 0)
          and w["w1_swap"][0] == w["w1_swap"][1]
          and w["rc3"] == 0 and got == exp
          and w["active_after_d3"] == w["d3_rows"])
    returning = got.get("1-1")
    leaving = got.get("1-59")
    notes = (f"seeded: after migrate-legacy the view has the legacy columns "
             f"and types and equals the table on every column but loaded_at "
             f"(EXCEPT ALL {w['diff']}); a wrong definition differs "
             f"({w['wrong_view_diff']}); after a fourth snapshot is_active "
             f"count {w['active_after_d3']} = its {w['d3_rows']} rows; "
             f"returning 1-1 {returning}, leaving 1-59 {leaving}; all "
             f"{len(exp)} locations equal the legacy upsert's result "
             f"computed from the files={got == exp}")
    if not ok:
        return report(16, name, False, notes)
    if not table_exists(cur):
        return report(16, name, False, f"{NOT_YET}; {notes}", pending=True)
    real_gate(cur, 16, name, lambda c: (lambda r: (r[0], f"{r[1]}; {notes}"))(
        real_view(c)))


def real_view(cur, view=m.LEGACY, old=m.LEGACY_RENAMED, spec=SPEC):
    """The real view: same columns and types as the renamed legacy table;
    equal to it (EXCEPT ALL both ways, all columns but loaded_at) while no
    snapshot newer than 2026-09-01 exists; afterwards its is_active count is
    the latest snapshot's row count."""
    if m.relkind(cur, view) != "v":
        return False, (f"{view} is not the view yet "
                       f"({m.relkind(cur, view)!r}): not yet migrated")
    if m.relkind(cur, old) != "r":
        return False, f"{old} (the kept legacy table) is missing"
    bad = [f"columns: {p}" for p in view_columns(cur, view, old)]
    cur.execute(f"SELECT MAX({spec.period_col}) FROM "
                f"public.{spec.live_table}")
    latest = cur.fetchone()[0]
    if latest is None:
        return False, "the live table holds no snapshot"
    if _s(latest) <= MIGRATED_AT:
        diff = m.view_differences(cur, view, old)
        if diff != (0, 0):
            bad.append(f"view vs {old}: {diff[0]} rows only in the view, "
                       f"{diff[1]} only in the table")
        what = f"equals {old} on every column but loaded_at (0 and 0 rows)"
    else:
        cur.execute(f"SELECT COUNT(*) FROM public.{view} WHERE is_active")
        n = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} WHERE "
                    f"{spec.period_col} = %s", (latest,))
        rows = cur.fetchone()[0]
        if n != rows:
            bad.append(f"is_active count {n} is not the latest snapshot's "
                       f"{rows} rows")
        what = (f"snapshot {latest} is newer than {MIGRATED_AT}: is_active "
                f"count {n} = its row count")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{view} has the columns and types of {old}; {what}")


def gate_17_w1_subquery(cur):
    name = ("W1's S11 subquery on the view equals the count from the latest "
            "snapshot per authority")
    try:
        w = migrated_world(cur)
        sub = w1_subquery(m.LEGACY)
    except (psycopg2.Error, SystemExit, RuntimeError, ValueError) as ex:
        return report(17, name, False, f"seeded: {ex}")
    ok = (w.get("rc") == 0 and w["w1_view"] == w["w1_latest"]
          and w["w1_view"] and w["w1_swap"][0] == w["w1_swap"][1])
    notes = (f"seeded: the subquery read from sql/w1/05_la_signals.sql on "
             f"the view gives {sum(w.get('w1_view', {}).values())} "
             "supported-living locations in "
             f"{len(w.get('w1_view', {}))} authorities, equal to the latest "
             "snapshot's count; the swap itself changed no count")
    if not ok:
        return report(17, name, False, notes)

    def real(c):
        if m.relkind(c, m.LEGACY) != "v":
            return False, f"{m.LEGACY} is not the view yet: not yet migrated"
        c.execute(sub)
        a = dict(c.fetchall())
        b = latest_counts(c, LIVE)
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        return not diff, (f"{len(diff)} authorities differ, e.g. {diff[:4]}"
                          if diff else
                          f"{sum(a.values())} locations in {len(a)} "
                          "authorities equal on the view and the latest "
                          f"snapshot; {notes}")
    if not table_exists(cur):
        return report(17, name, False, f"{NOT_YET}; {notes}", pending=True)
    real_gate(cur, 17, name, real)


# --------------------------------------------------------------- gate 18

def gate_18_unresolved(cur):
    name = ("unresolved: the latest snapshot's unresolved rows are absent "
            "from live and the edition")

    def body(e):
        cur2 = e.cur
        ok, detail = real_unresolved_absent(cur2, ZZ)
        un = unresolved_of(cur2, D2)
        cur2.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE location_id "
                     "= %s", (NOXY,))
        cur2.execute(f"SELECT reason FROM public.{ZUNRES} WHERE "
                     "location_id = %s AND snapshot_date = %s", (NOXY, D2))
        reason = cur2.fetchone()
        # a planted live row for an unresolved location is caught
        cur2.execute("SELECT column_name FROM information_schema.columns "
                     "WHERE table_schema = 'public' AND table_name = %s "
                     "AND column_name NOT IN ('location_id', 'loaded_at')",
                     (ZL,))
        others = ", ".join(r[0] for r in cur2.fetchall())
        cur2.execute(f"INSERT INTO public.{ZL} (location_id, {others}) "
                     f"SELECT %s, {others} FROM public.{ZL} WHERE "
                     "snapshot_date = %s LIMIT 1", (NOXY, D2))
        caught, _ = real_unresolved_absent(cur2, ZZ)
        return ok, detail, un, reason, caught
    try:
        ok, detail, un, reason, caught = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(18, name, False, str(ex).splitlines()[0])
    seeded = (ok and un == [(1, NOXY)] and reason and "ZZ9 9ZZ" in reason[0]
              and not caught)
    notes = (f"seeded: {detail}; the reason names the postcode; a planted "
             f"live row for an unresolved location is caught="
             f"{not caught}")
    if not seeded:
        return report(18, name, False, notes)
    if not table_exists(cur):
        return report(18, name, False, f"{NOT_YET}; {notes}", pending=True)
    real_gate(cur, 18, name, lambda c: (lambda r: (r[0], f"{r[1]}; {notes}"))(
        real_unresolved_absent(c)))


# --------------------------------------------------------------- gate 19

def gate_19_rerun_idempotent(cur):
    name = ("rerun idempotent: the same files twice store nothing and leave "
            "live untouched")

    def body(e):
        base = state(e.cur)
        runs = []
        for kw in ({"page": url(D2)}, {"page": url(D2)},
                   {"file": e.files[url(D2)]}, {"file": e.files[url(D1)]},
                   {"page": url(D1)}):
            rc, text, _, logged = e.run(["load", "--commit"], **kw)
            runs.append((rc == 0 and state(e.cur) == base
                         and not logged.called
                         and ("already" in text or "nothing" in text)))
        rc, text, _, _ = e.run(["status"])
        return runs, rc, base
    try:
        runs, st_rc, base = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(19, name, False, str(ex).splitlines()[0])
    ok = all(runs) and st_rc == 0 and base[1] == [(1, None, 2 * N)]
    report(19, name, ok, f"five reruns (page and --file, twice each, both "
           f"snapshots): unchanged={runs}; status exit {st_rc}; editions "
           f"{base[1]}, live rows and loaded_at unchanged")


# --------------------------------------------------------------- gate 20

def gate_20_stranded_repair(cur):
    name = ("stranded snapshot repair: editions with no live rows get live "
            "rows from load (the page for the latest snapshot, --file with "
            "--recheck for an older one), no new edition")

    def repair(e, d, **run_kw):
        e.cur.execute(f"DELETE FROM public.{ZL} WHERE snapshot_date = %s",
                      (d,))
        st = m.status(e.cur, ZZ)
        rc_status, _, _, _ = e.run(["status"])
        argv = ["load", "--commit"] + (["--recheck", d]
                                       if "file" in run_kw else [])
        e.fetched.clear()
        rc, text, _, _ = e.run(argv, **run_kw)
        e.cur.execute(f"SELECT DISTINCT source_file FROM public.{ZL} WHERE "
                      "snapshot_date = %s", (d,))
        src = e.cur.fetchall()
        return (st, rc_status, rc, text, e.fetched[:], editions_of(e.cur, d),
                count(e.cur, ZL, "WHERE snapshot_date = %s", (d,)), src,
                ledger_of(e.cur, d)[-1], unresolved_of(e.cur, d),
                m.status(e.cur, ZZ))

    def body(e):
        ok0 = m.status(e.cur, ZZ)["ok"]
        return (ok0, repair(e, D2, page=url(D2)),
                repair(e, D1, file=e.files[url(D1)]))
    try:
        ok0, latest, older = scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(20, name, False, str(ex).splitlines()[0])
    bad = []
    for d, (st, rc_s, rc, text, fetched, eds, lv, src, led, un,
            after), want_src, want_fetch in (
            (D2, latest, url(D2), [url(D2)]),
            (D1, older, None, [])):
        if not (st["live_missing"] == [d] and not st["ok"] and rc_s == 1
                and rc == 0 and "no new edition" in text
                and fetched == want_fetch and eds == [(1, None, N)]
                and lv == N and led[1] == "live-missing"
                and un == [(1, NOXY)] and after["live_missing"] == []
                and after["ok"] and (want_src is None or src == [(want_src,)])):
            bad.append(f"{d}: status {st['live_missing']} exit {rc_s}, load "
                       f"rc {rc}, editions {eds}, live {lv}, ledger {led}, "
                       f"unresolved {un}, after {after['live_missing']}")
    report(20, name, ok0 and not bad, "; ".join(bad[:2]) if bad else (
        f"status flagged each stranded snapshot (exit 1); the latest "
        f"({D2}) was repaired from the page, fetching only it, and an older "
        f"one ({D1}) from --file with --recheck; editions stayed one each "
        f"(no new edition), live rows {N}, ledger row 'live-missing', "
        "unresolved rows not duplicated, live_missing empty afterwards"))


# --------------------------------------------------------------- gate 21

def gate_21_on_disk(cur):
    real_gate(cur, 21, "on-disk reproduction: each file in data/raw/ whose "
              "sha256 is a held tip's parses to that tip exactly", real_on_disk)


# --------------------------------------------------------------- gate 22

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


def gate_22_no_network_no_secrets(cur):
    name = "no network, and no secret in the source files or any gate output"
    files = [Path(__file__).resolve(), HERE / "s11_cqc_editions.py",
             HERE / "period_editions.py", HERE / "geography.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")

    def body(c):
        with env(c) as e:
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.seed((D1,))
                e.make(D2)
                rc, text, _, _ = e.run(["load", "--commit"], page=url(D2))
                rc_s, _, _, _ = e.run(["status"])
                rc_r, _, _, _ = e.run(["refresh-latest"])
                # the real download code against a stub session
                body_bytes = e.files[url(D2)].read_bytes()
                s = _Session(body_bytes)
                with contextlib.redirect_stdout(io.StringIO()), \
                        mock.patch.object(m, "MIN_FILE_BYTES", 100):
                    path = m.fetch_file(url(D3), Path(e.tmp) / "raw", s)
            return rc, rc_s, rc_r, path, s.calls
    try:
        rc, rc_s, rc_r, path, calls = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(22, name, False, f"{type(ex).__name__}: {ex}")
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    literal = [n for n, t in texts.items() if pat.search(t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    ok = (not attempts and rc == 0 and rc_s == 0 and rc_r == 0
          and path.name == ods_name(date(2026, 10, 1))
          and calls and all(u.startswith("https://") for u in calls)
          and not literal and not in_src and not in_out)
    report(22, name, ok, f"socket attempts {len(attempts)}; load, status and "
           f"refresh-latest through stubs returned {rc}/{rc_s}/{rc_r}; "
           f"download through a stub session requested {len(calls)} URL(s); "
           f"secret-like literal in source {literal or 'none'}; "
           f"{len(secrets)} secret-named settings checked against "
           f"{len(files)} files and {len(OUTPUT)} output lines: in source "
           f"{in_src or 'none'}, in output {in_out or 'none'}")


class _Resp:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, link, headers=None, timeout=None, **kw):
        self.calls.append(link)
        return _Resp(self.body)


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_source_uniform, gate_4_coverage, gate_5_one_row,
         gate_6_null_vs_zero, gate_7_markers, gate_8_identity,
         gate_9_older_file_guard, gate_10_stop_conditions,
         gate_11_one_transaction, gate_12_ledger,
         gate_13_preview_writes_nothing, gate_14_revision_and_refresh,
         gate_15_barnsley_sheffield, gate_16_the_view, gate_17_w1_subquery,
         gate_18_unresolved, gate_19_rerun_idempotent,
         gate_20_stranded_repair, gate_21_on_disk,
         gate_22_no_network_no_secrets)


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
