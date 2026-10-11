"""Gates for the S5 (English indices of deprivation, File 10, la_imd_2025)
editions tables.

Mirrors scripts/s23_rsh_stock_editions_verify.py and
s13_lahs_editions_verify.py. Prints `GATE n name: PASS|FAIL`; exits 1 on any
FAIL. It writes nothing, not even a run-log row. The real-table gates read
la_imd_2025, la_imd_2025_editions and la_imd_2025_editions_file_checks. Until
the editions table exists (ddl and migrate-legacy not yet run) they print
`FAIL (pending migration)` with the words "not yet migrated", so the script
exits non-zero until then, like the other verifiers; nothing is created to
make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s5_live / zz_s5_editions / zz_s5_editions_file_checks (a
copy of the loader's spec), created inside the transaction, and call the
loader's own writer and commands (s5_imd_editions.main with the SPEC pointed
at the copy, the content API and the downloads stubbed; apply_vintage;
migrate_legacy; restore_edition) rather than a re-implementation. The real
tables are only ever read.

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), shape                                       (throwaway + real)
  2  every live vintage has edition 1; the latest edition equals live cell
     for cell (key, vintage, la_name, the ten measures)               (real)
  3  live holds exactly one vintage: the live `vintage` column (text, NOT
     NULL, DEFAULT 'IoD2025', CHECK = 'IoD2025'; pending until ddl) and the
     single-vintage rule: a second vintage never reaches live through load,
     refresh-latest or --accept-drift; it is listed as held, not live
  4  identity from the file: the sheet set, the exact IMD header, the row
     count, docProps modified, the page title year and --release (seeded
     mismatches halt)
  5  older-source guard on the page, --release and --file paths; the rank is
     the file's docProps modified, never its name
  6  stop conditions: each seeded REJECTED (equal docProps with other
     content; a partial key set; another vintage into an empty live), stores
     nothing, no ledger row, partial run-log row; blank and text cells halt
  7  ledger rule: the skip needs the (where, sha256) pair declared for the
     vintage; a failing ledger, live or edition insert rolls the load back
  8  preview and simulate write nothing
  9  a byte-identical re-read is `unchanged` on every path against edition 1
     'as loaded'; the held file read now equals edition 1          (seeded +
     real)
 10  a reissue stores edition 2; refresh-latest changes only the live
     vintage, sets source on every row and copies loaded_at
 11  edition 1 equals the decision note's before-state line     (seeded + real)
 12  the migration proof re-run read-only from the file on disk (seeded + real)
 13  rerun idempotent
 14  restore-edition round trip
 15  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content
 16  the file's own checks on live and the tip: each rank column a ranking of
     1..n following its measure (shared ranks allowed), proportions and
     extents in [0, 1], published zeros kept            (seeded + real)
 17  imd_rank_of_average_rank in live equals the decision note's w1-read line
     (the map's IMD layer: sql/w1/05_la_signals.sql)         (seeded + real)

The decision note's hash lines are scan-safe: a 64-hex run would be flagged by
the credential scan, so each sha256 is written as two 32-hex halves in two
labelled fields:
    before-state la_imd_2025 IoD2025 rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read la_imd_2025 IoD2025 rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
`python scripts/s5_imd_editions_verify.py --print-note-lines` prints them for
the live table as it stands (read-only; also a before-state-all line over
every stored column but vintage), for the decision note. Take them before
the migration.

Usage:
    python scripts/s5_imd_editions_verify.py
    python scripts/s5_imd_editions_verify.py --print-note-lines

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The cursor handed to the gates
refuses commit and rollback on its connection, and the commands get a
stand-in connection whose commit only counts. No network, no real-table
writes, no backend is ever terminated. After the rollback a second,
read-only connection checks that no zz% table was left behind.
"""

import contextlib
import hashlib
import io
import os
import re
import socket
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
from _db import ENV, get_conn, get_readonly_conn  # noqa: E402
import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s5_imd_editions as m  # noqa: E402
import test_s5_imd_loader as tl  # noqa: E402
import test_s5_imd_pure as tp  # noqa: E402
from test_s5_imd_pure import (CODES, HEADER_2030, _Session,  # noqa: E402
                              write_file10)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real spec and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPEC, which the seeded gates point
# at the copy)
REAL = m.SPEC
AREAS = m.EXPECTED_AREAS
ZZ = tl.ZZ
LEDGER = tl.LEDGER
P = tl.P
P30 = tl.P30
N = tl.N
UTC = timezone.utc
LOADER = HERE / "s5_imd_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-10-reference-trio-editions-first-load.md")
NOT_YET = ("the S5 editions table does not exist yet (not yet migrated)")
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (15)
TABLE = "la_imd_2025"
W1_COL = "imd_rank_of_average_rank"
EARLIER = datetime(2025, 10, 28, 9, 0, 0, tzinfo=UTC)
LATER = datetime(2025, 12, 1, 10, 0, 0, tzinfo=UTC)


def report(n, name, ok, detail="", pending=False):
    RESULTS.append(ok)
    verdict = "PASS" if ok else ("FAIL (pending migration)" if pending
                                 else "FAIL")
    line = f"GATE {n} {name}: {verdict}" + (f"  [{detail}]" if detail else "")
    OUTPUT.append(line)
    print(line)


def _first(e):
    return str(e).splitlines()[0] if str(e) else type(e).__name__


class _NoCommit:
    """The .connection of a gate cursor: it has an encoding (psycopg2's
    helpers read it) but commit and rollback are refused, so nothing run
    from a gate can reach the real transaction."""

    def __init__(self, conn):
        self.encoding = conn.encoding

    def commit(self):
        raise RuntimeError("commit refused by the verify script")

    def rollback(self):
        raise RuntimeError("rollback refused by the verify script")


class SafeCur:
    def __init__(self, cur):
        self._cur = cur
        self.connection = _NoCommit(cur.connection)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def _in_savepoint(cur, fn):
    cur.execute("SAVEPOINT h")
    try:
        return fn(cur)
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT h")
        cur.execute("RELEASE SAVEPOINT h")


def exists(cur, spec=REAL):
    return pe.table_exists(cur, spec.editions_table)


def ledger_name(spec):
    return f"{spec.editions_table}_file_checks"


def _need(cur, spec):
    miss = [t for t in (spec.editions_table, ledger_name(spec))
            if not pe.table_exists(cur, t)]
    if miss:
        raise ValueError(f"{', '.join(miss)} does not exist")


@contextmanager
def specs():
    """The loader pointed at the throwaway copy, with the 2030 header
    declared (the editions-only path)."""
    with tl.specs({P30: tuple(HEADER_2030)}):
        yield


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    names = (ZZ.live_table, ZZ.editions_table, LEDGER)
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                      for n in names))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s5 table already exists as a real table; "
                 "refusing to run")
    cur.execute(f"CREATE TABLE public.{ZZ.live_table} (LIKE "
                f"public.{REAL.live_table} INCLUDING ALL)")
    with specs():
        m.create_all(cur)


def state(cur):
    """Everything a refused or previewed run must leave alone."""
    out = []
    for t in (ZZ.live_table, ZZ.editions_table, LEDGER):
        cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                    f"FROM public.{t} t")
        out.append(cur.fetchone()[0])
    return tuple(out)


def tables_state(cur):
    """state() without the ledger (a re-read adds 'unchanged' ledger rows)."""
    return state(cur)[:2]


# ------------------------------------------------------------ files and Env

class Env(tl.Migrate):
    """The loader's main(argv) on the throwaway tables, the content API and
    the downloads stubbed; every workbook is written here (write_file10)."""

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api = {m.COLLECTION_PATH: tp.collection()}
        self.urls, self.final, self.fetched = {}, {}, []
        self.n = 0
        self.last_url = None
        self.tx = None

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, run-log
        mock)."""
        borrowed = tl._Borrowed(cur)
        self.tx = borrowed.tx

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)
        out = io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda p, s=None: self.api[p]), \
                mock.patch.object(m, "fetch", side_effect=fetch), \
                mock.patch.object(m, "log_run") as logged, \
                contextlib.redirect_stdout(out):
            try:
                rc = m.main([str(a) for a in argv])
            except SystemExit as e:
                rc = "halt"
                out.write(f"\n{e.code}")
            finally:
                borrowed.tx.done()
        OUTPUT.extend(out.getvalue().splitlines())
        return rc, out.getvalue(), logged

    def cmd(self, argv):
        return self.run_main(self.cur, argv)

    def halts(self, argv, before, needle=None, rc_ok=("halt", 1)):
        """(True, text) if the command halted or exited 1 and left
        everything as `before`, with no run-log row."""
        rc, text, logged = self.cmd(argv)
        ok = (rc in rc_ok and state(self.cur) == before
              and not logged.called
              and (needle is None or needle in text))
        return ok, text

    def reissue(self, **kw):
        """The held vintage again, a later docProps modified."""
        return self.file(modified=LATER, **kw)

    def named(self, name, *, serve=True, vintage=P, **kw):
        """A File 10 whose NAME is chosen (not derived from the content)."""
        self.n += 1
        path = write_file10(self.root / f"n{self.n}" / name, vintage=vintage,
                            **kw)
        url = f"https://assets.example/media/n{self.n}/{name}"
        if serve:
            self.urls[url] = path
            self.page(int(vintage[3:]), url)
            self.last_url = url
        return path, url


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp)


def scenario(cur, fn):
    """Run fn(env) in a savepoint that is rolled back."""
    def body(c):
        with env(c) as e, specs():
            return fn(e)
    return _in_savepoint(cur, body)


def legacy_world(cur, fn, **kw):
    """A seeded legacy state (as the old build left it, the vintage column
    already added by ddl) and the fixture file; fn(env, path, files)."""
    def body(c):
        with env(c) as e, specs():
            path, files = e.seed_legacy(c, **kw)
            return fn(e, path, files)
    return _in_savepoint(cur, body)


def plant(cur, sql, check, args=None):
    """check() after one statement, in a savepoint that is rolled back."""
    cur.execute("SAVEPOINT plant")
    try:
        cur.execute(sql, args)
        return check()
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT plant")
        cur.execute("RELEASE SAVEPOINT plant")


def drop_live_vintage_check(cur, spec):
    """Drop the live vintage CHECK (in a caller's savepoint) so the gate's
    own detection can be tested."""
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = "
                "%s::regclass AND contype = 'c' AND "
                "pg_get_constraintdef(oid) LIKE %s",
                (f"public.{spec.live_table}", "%vintage%"))
    for (name,) in cur.fetchall():
        cur.execute(f"ALTER TABLE public.{spec.live_table} DROP CONSTRAINT "
                    f"{name}")


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "varchar": "character varying",
          "text": "text", "date": "date", "numeric": "numeric"}


def _family(sql):
    return _DTYPE[sql.split()[0].split("(")[0].lower()]


def _columns(cur, table):
    cur.execute("""SELECT column_name, data_type, is_nullable
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


def _constraint_defs(cur, table, kind):
    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                "conrelid = %s::regclass AND contype = %s",
                (f"public.{table}", kind))
    return [r[0] for r in cur.fetchall()]


def shape_problems(cur, spec):
    """Problems with the editions table, the live table and the ledger of a
    spec."""
    bad = []
    led = ledger_name(spec)
    for t in (spec.editions_table, spec.live_table, led):
        if not pe.table_exists(cur, t):
            bad.append(f"{t} does not exist")
    if bad:
        return bad
    meta = {"edition": ("integer", "NO"), "supersedes": ("integer", "YES"),
            "release_label": ("text", None), "published_date": ("date", None),
            "source_file": ("text", None), "source_sha256": ("text", None),
            "loaded_at": ("timestamp with time zone", None)}
    cols, lcols = _columns(cur, spec.editions_table), _columns(
        cur, spec.live_table)
    need = dict(meta)
    for c, ty in tuple(spec.key_types):
        need[c] = (_family(ty), "NO")
    for c, ty in spec.value_cols + spec.extra_cols:
        need[c] = (_family(ty), "NO" if "NOT NULL" in ty.upper() else "YES")
    for c, (ty, nullable) in need.items():
        got = cols.get(c)
        if got is None:
            bad.append(f"{spec.editions_table}: {c} missing")
        elif got[0] != ty or (nullable and got[1] != nullable):
            bad.append(f"{spec.editions_table}: {c} is {got}, expected "
                       f"{ty} {nullable}")
    if _pk(cur, spec.editions_table) != set(spec.key_cols) | {
            spec.period_col, "edition"}:
        bad.append(f"{spec.editions_table}: primary key is not key + "
                   "period + edition")
    fks = len(_constraint_defs(cur, spec.editions_table, "f"))
    if fks != (1 if spec.fk_la_boundaries else 0):
        bad.append(f"{spec.editions_table}: {fks} foreign key(s)")
    checks = " ".join(_constraint_defs(cur, spec.editions_table, "c"))
    if "IoD" not in checks:
        bad.append(f"{spec.editions_table}: the vintage CHECK (IoD<year>) "
                   "is missing")
    bad += _triggers(cur, spec.editions_table, spec.trigger,
                     spec.truncate_trigger)
    for c in (m.KEY, m.NAME) + m.VALUES + ("source", "loaded_at"):
        if c not in lcols:
            bad.append(f"{spec.live_table}: {c} missing")
    lc = _columns(cur, led)
    ptype = _family(dict(spec.key_types)[spec.period_col])
    for c, ty, nullable in (("id", "bigint", "NO"), (spec.period_col, ptype,
                                                     "NO"),
                            ("source_file", "text", "NO"),
                            ("file_sha256", "text", "NO"),
                            ("outcome", "text", "NO"),
                            ("edition", "integer", "YES"),
                            ("checked_at", "timestamp with time zone", "NO")):
        got = lc.get(c)
        if got is None or tuple(got) != (ty, nullable):
            bad.append(f"{led}: {c} is {got}, expected {(ty, nullable)}")
    chk = " ".join(_constraint_defs(cur, led, "c"))
    want = pe.FILE_CHECK_OUTCOMES + ((pe.HELD_NOT_LIVE,)
                                     if spec.live_single_period else ())
    if not all(f"'{o}'" in chk for o in want):
        bad.append(f"{led}: outcome check is {chk!r}")
    bad += _triggers(cur, led, f"{spec.name}_file_checks_immutable",
                     f"{spec.name}_file_checks_no_truncate")
    return bad


def immutability(cur, stmts, seed_fn):
    """{UPDATE, DELETE, TRUNCATE: error text or None}; all in savepoints."""
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
    name = ("editions table, ledger, live shape (UPDATE, DELETE and TRUNCATE "
            "are refused on the editions and the ledger)")
    bad = shape_problems(cur, ZZ)

    def seed(cur):
        with env(cur) as e, specs():
            e.seed(cur)
    targets = [(f"editions {ZZ.editions_table}", {
        "UPDATE": f"UPDATE public.{ZZ.editions_table} SET edition = edition",
        "DELETE": f"DELETE FROM public.{ZZ.editions_table}",
        "TRUNCATE": f"TRUNCATE public.{ZZ.editions_table}"}),
        (f"ledger {LEDGER}", {
            "UPDATE": f"UPDATE public.{LEDGER} SET outcome = 'new'",
            "DELETE": f"DELETE FROM public.{LEDGER}",
            "TRUNCATE": f"TRUNCATE public.{LEDGER}"})]
    for what, stmts in targets:
        for k, msg in immutability(cur, stmts, seed).items():
            if not (msg and "append-only" in msg):
                bad.append(f"{what} {k} not refused ({msg})")
    scope = "throwaway copy"
    if exists(cur):
        scope += " and real tables"
        bad += [f"real: {p}" for p in shape_problems(cur, REAL)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise "
           "append-only on the editions table and the ledger")


# ------------------------------------------------------- real-table gates

def mixed(n, name, seeded_ok, seeded_detail, cur, real_fn):
    """A gate with a seeded part (always) and a real part (once the editions
    table exists, else pending)."""
    if not seeded_ok:
        return report(n, name, False, f"seeded: {seeded_detail}")
    if not exists(cur):
        return report(n, name, False, f"seeded part passes; real: {NOT_YET}",
                      pending=True)
    try:
        ok, detail = real_fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(n, name, False, _first(e))
    report(n, name, ok, f"{seeded_detail}; real: {detail}")


EMPTY = "no live vintages to check (an empty state is not a pass)"


def _live_periods(cur, spec):
    if not m.live_has_vintage(cur, spec):
        raise ValueError(f"{spec.live_table} has no vintage column (ddl not "
                         "yet run)")
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [str(r[0]) for r in cur.fetchall()]


def _tip(cur, spec, p):
    return core.chain_tip(cur, spec, str(p))


def _both_ways(cur, a, aargs, b, bargs):
    counts = []
    for x, xa, y, ya in ((a, aargs, b, bargs), (b, bargs, a, aargs)):
        cur.execute(f"SELECT COUNT(*) FROM (({x}) EXCEPT ALL ({y})) q",
                    xa + ya)
        counts.append(cur.fetchone()[0])
    return counts


def real_edition1_and_latest(cur, spec=REAL):
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, f"{spec.live_table}: {EMPTY}"
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "source_file IS NOT NULL")
    have = {str(r[0]) for r in cur.fetchall()}
    missing = [p for p in periods if p not in have]
    if missing:
        return False, (f"no edition 1 (with a source_file) for "
                       f"{missing[:6]} ({len(missing)} of {len(periods)})")
    cols = ", ".join((m.KEY, spec.period_col, m.NAME) + m.VALUES)
    bad = []
    for p in periods:
        ed = _tip(cur, spec, p)
        a, b = _both_ways(
            cur, f"SELECT {cols} FROM public.{spec.editions_table} WHERE "
            f"{spec.period_col} = %s AND edition = %s", (p, ed),
            f"SELECT {cols} FROM public.{spec.live_table} WHERE "
            f"{spec.period_col} = %s", (p,))
        if a or b:
            bad.append(f"{p} ed{ed}: {a} edition-only, {b} live-only rows")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} live vintage(s) each have edition 1 and the latest "
        "edition equals live on the key, the vintage, la_name and the ten "
        "measures, cell for cell")


def gate_2_edition1_and_latest(cur):
    name = ("every live vintage has edition 1; the latest edition equals "
            "live cell for cell")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_edition1_and_latest(e.cur, ZZ)
        miss = []
        live = ZZ.live_table
        for sql, needle in (
            (f"UPDATE public.{live} SET imd_average_score = "
             "imd_average_score + 1 WHERE lad24cd = 'E06000001'",
             "edition-only"),
            (f"UPDATE public.{live} SET imd_rank_of_average_rank = 99 WHERE "
             "lad24cd = 'E06000001'", "edition-only"),
            (f"UPDATE public.{live} SET la_name = 'x' WHERE lad24cd = "
             "'E06000001'", "edition-only"),
            (f"DELETE FROM public.{live} WHERE lad24cd = 'E06000001'",
             "edition-only"),
        ):
            ok, detail = plant(e.cur, sql, lambda: real_edition1_and_latest(
                e.cur, ZZ))
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")

        def other_vintage():
            drop_live_vintage_check(e.cur, ZZ)
            e.cur.execute(f"UPDATE public.{live} SET vintage = 'IoD2030'")
            return real_edition1_and_latest(e.cur, ZZ)
        ok, detail = plant(e.cur, "SELECT 1", other_vintage)
        if ok or "no edition 1" not in detail:
            miss.append(f"a vintage without edition 1: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(2, name, ok, "seeded complete state passes; five planted drifts "
          "(a measure, a rank, a name, a missing row, a vintage without "
          "edition 1) are each caught" if ok else
          f"{out['base']} {out['miss']}", cur, real_edition1_and_latest)


# ---------------------------------------------------------------- gate 3

def vintage_column_problems(cur, spec):
    """Problems with the live `vintage` column: text, NOT NULL, DEFAULT
    'IoD2025', and the CHECK vintage = 'IoD2025' (the live table can hold no
    second vintage)."""
    if not m.live_has_vintage(cur, spec):
        return [f"{spec.live_table}.vintage does not exist (ddl not yet "
                "run)"]
    cur.execute("SELECT data_type, is_nullable, column_default FROM "
                "information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = %s AND column_name = 'vintage'",
                (spec.live_table,))
    row = tuple(cur.fetchone())
    bad = []
    want = ("text", "NO", f"'{m.LIVE_VINTAGE}'::text")
    if row != want:
        bad.append(f"{spec.live_table}.vintage is {row}, expected {want}")
    checks = " ".join(_constraint_defs(cur, spec.live_table, "c"))
    if f"(vintage = '{m.LIVE_VINTAGE}'::text)" not in checks:
        bad.append(f"{spec.live_table}: CHECK vintage = '{m.LIVE_VINTAGE}' "
                   "is missing")
    return bad


def real_single_vintage(cur, spec=REAL, n=AREAS):
    """The live vintage column as planned; live holds exactly one vintage
    (IoD2025) of n rows; any other vintage is in the editions only and
    status lists it as held, not live (information, not a fault)."""
    _need(cur, spec)
    bad = vintage_column_problems(cur, spec)
    if not m.live_has_vintage(cur, spec):
        return False, "; ".join(bad[:3])
    cur.execute(f"SELECT {spec.period_col}, COUNT(*) FROM "
                f"public.{spec.live_table} GROUP BY 1")
    live = {str(a): b for a, b in cur.fetchall()}
    if set(live) != {m.LIVE_VINTAGE}:
        bad.append(f"live holds vintage(s) {sorted(live)}, expected only "
                   f"{m.LIVE_VINTAGE}")
    elif live[m.LIVE_VINTAGE] != n:
        bad.append(f"live {m.LIVE_VINTAGE} has {live[m.LIVE_VINTAGE]} rows, "
                   f"expected {n}")
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} ORDER BY 1")
    stored = [str(r[0]) for r in cur.fetchall()]
    held = [p for p in stored if p != m.LIVE_VINTAGE]
    try:
        st = pe.status(cur, m.profile())
    except SystemExit as e:
        bad.append(f"status halts: {_first(e)}"[:120])
    else:
        if sorted(st.get("held_not_live", [])) != held:
            bad.append(f"status lists held-not-live "
                       f"{st.get('held_not_live')}, the editions hold {held}")
    if bad:
        return False, "; ".join(bad[:3])
    return True, (f"vintage text NOT NULL DEFAULT '{m.LIVE_VINTAGE}' with its "
                  f"CHECK; live holds {m.LIVE_VINTAGE} only ({n} rows); "
                  f"held, not live: {held or 'none'}")


def gate_3_single_vintage(cur):
    name = ("live holds exactly one vintage (the vintage column and its "
            "CHECK); a second vintage never reaches live; others are "
            "held, not live")
    out = {}
    problems = []

    def body(e):
        e.seed(e.cur)
        out["base"] = real_single_vintage(e.cur, ZZ, n=N)
        live = ZZ.live_table

        def md5():
            return tl.other_cols_md5(e.cur)
        before = md5()
        e.file(P30, modified=datetime(2030, 11, 1, 9, 0, 0, tzinfo=UTC),
               overrides={("E06000001", "imd_average_rank"): 26000.5})
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0 or f"{P30}: held-not-live" not in text \
                or tl.editions(e.cur, P30) != [(1, None, N)] \
                or md5() != before:
            problems.append(f"a second vintage did not stop at editions: "
                            f"rc {rc}: {text[-140:]}")
        out["held"] = real_single_vintage(e.cur, ZZ, n=N)
        if not out["held"][0] or P30 not in out["held"][1]:
            problems.append(f"held vintage not reported: {out['held']}")
        rc, text, _ = e.cmd(["refresh-latest", "--commit"])
        if rc != 0 or md5() != before:
            problems.append("refresh-latest moved live after a second "
                            "vintage")
        snap = state(e.cur)
        for flag in (P30, "IoD2019"):
            ok, _ = e.halts(["refresh-latest", "--commit", "--accept-drift",
                             flag], snap)
            if not ok:
                problems.append(f"refresh-latest --accept-drift {flag} did "
                                "not halt")
        rc, text, _ = e.cmd(["load", "--recheck", P30, "--commit"])
        if md5() != before:
            problems.append("a recheck of the second vintage touched live")
        # the CHECK refuses a second vintage in live
        e.cur.execute("SAVEPOINT chk")
        try:
            e.cur.execute(f"UPDATE public.{live} SET vintage = 'IoD2030' "
                          "WHERE lad24cd = 'E06000001'")
            problems.append("the live CHECK let a second vintage in")
        except psycopg2.Error:
            pass
        e.cur.execute("ROLLBACK TO SAVEPOINT chk")
        e.cur.execute("RELEASE SAVEPOINT chk")
        # planted breaks are each caught
        miss = []

        def two_vintages():
            drop_live_vintage_check(e.cur, ZZ)
            e.cur.execute(f"UPDATE public.{live} SET vintage = 'IoD2030' "
                          "WHERE lad24cd = 'E06000001'")
            return real_single_vintage(e.cur, ZZ, n=N)
        for sql, fn, needle in (
            ("SELECT 1", two_vintages, "live holds vintage"),
            (f"ALTER TABLE public.{live} DROP COLUMN vintage",
             lambda: real_single_vintage(e.cur, ZZ, n=N),
             "does not exist"),
            (f"ALTER TABLE public.{live} ALTER COLUMN vintage DROP DEFAULT",
             lambda: real_single_vintage(e.cur, ZZ, n=N), "expected"),
            (f"ALTER TABLE public.{live} ALTER COLUMN vintage DROP NOT NULL",
             lambda: real_single_vintage(e.cur, ZZ, n=N), "expected"),
        ):
            ok, detail = plant(e.cur, sql, fn)
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")

        def no_check():
            drop_live_vintage_check(e.cur, ZZ)
            return real_single_vintage(e.cur, ZZ, n=N)
        ok, detail = plant(e.cur, "SELECT 1", no_check)
        if ok or "CHECK" not in detail:
            miss.append(f"a missing CHECK: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not problems and not out["miss"]
    mixed(3, name, ok, "seeded: the column and its CHECK pass; IoD2030 "
          "loaded after IoD2025 is stored as editions only (live byte for "
          "byte as before), reported as held, not live, and left alone by "
          "load --recheck and refresh-latest; --accept-drift of it halts; "
          "the CHECK refuses it in live; a second vintage in live, a "
          "dropped column, default, NOT NULL or CHECK are each caught"
          if ok else f"{out} {problems[:3]}", cur, real_single_vintage)


# ---------------------------------------------------------- file gates (4, 5)

def held_file(files=None, raw_dir=None):
    """The file on disk whose sha256 is the held file's, or None."""
    files = m.LEGACY_FILE if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    for p in sorted(raw.glob("*.xlsx")):
        if m.content_sha256(p) == files["sha256"]:
            return p
    return None


def real_identity(cur=None, files=None, raw_dir=None, spec=REAL, areas=None):
    """The held file's identity read from the file itself (the sheet set,
    the exact IoD2025 header, the row count, docProps modified) equals what
    the loader surveyed, and, once the editions table exists, every
    vintage's tip names its file's docProps modified."""
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, f"no file with sha256 {files['sha256'][:16]}"
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_file10(p)
    bad = []
    if f["vintage"] != m.LIVE_VINTAGE:
        bad.append(f"{p.name}: vintage {f['vintage']}")
    if f["sheets"] != list(m.SHEETS):
        bad.append(f"{p.name}: sheets {f['sheets']}")
    if f["modified"] != m.LEGACY_RANK:
        bad.append(f"{p.name}: docProps modified {f['modified']}, surveyed "
                   f"{m.LEGACY_RANK}")
    tips = ""
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        info = m.tip_info(cur, spec)
        off = [v for v, i in info.items() if i["rank"] is None]
        if off:
            bad.append(f"{off[:3]}: the tip's source_file names no docProps "
                       "modified")
        tips = f"; {len(info)} stored vintage(s), each tip names its file"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{p.name}: vintage {f['vintage']}, the {len(f['sheets'])} declared "
        f"sheets, exact IMD header, {len(f['rows'])} authorities, docProps "
        f"{m.rank_text(f['modified'])}{tips}")


def gate_4_identity(cur):
    name = ("identity from the file: sheet set, IMD header, row count, "
            "docProps modified, page title year and --release; seeded "
            "mismatches halt")
    problems = []

    def body(e):
        def raises(label, **kw):
            try:
                m.read_file10(write_file10(e.root / f"{label}.xlsx", **kw))
                problems.append(f"{label} was read")
            except ValueError:
                pass
        raises("sheet", sheets=[s for s in m.SHEETS if s != "Income"])
        raises("order", sheets=list(reversed(m.SHEETS)))
        hdr = list(tp.HEADER_2025)
        hdr[2] = "IMD - Mean rank "
        raises("header", header=hdr)
        raises("other_vintage", header=[h.replace("IMD25", "IMD99")
                                        for h in tp.HEADER_2025])
        raises("rows", codes=CODES[:-1])
        raises("blank", overrides={("E06000002", "imd_extent"): None})
        raises("text", overrides={("E06000002", "imd_extent"): "[x]"})
        raises("no_modified", modified=None)
        # through the command: a page of another year, a page of 2030 over
        # an IoD2025 file, and --release 2019 over this file halt
        e.file()
        before = state(e.cur)
        e.api[tp.BASE.format(2025)]["title"] = tp.TITLE.format(2019)
        ok, _ = e.halts(["load", "--commit"], before)
        if not ok:
            problems.append("a page titled for 2019 did not halt the load")
        path = e.file(register=False)
        ok, _ = e.halts(["load", "--file", path, "--release", "2019",
                         "--commit"], before, "--release 2019")
        if not ok:
            problems.append("--file with another --release did not halt")
        url = "https://assets.example/media/q/F10.xlsx"
        e.urls[url] = path
        e.page(2030, url)
        ok, _ = e.halts(["load", "--commit"], before, "IoD2030")
        if not ok:
            problems.append("a page of 2030 over an IoD2025 file did not "
                            "halt")
        # and a 2030 header, once declared, is not an IoD2025 identity
        f30 = m.read_file10(write_file10(e.root / "f30.xlsx", vintage=P30))
        if m.identity_problems(f30, 2025) == []:
            problems.append("an IoD2030 file under a 2025 page was "
                            "accepted")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a missing or reordered sheet, a renamed or other-"
          "vintage header, 295 rows, a blank or text cell and a missing "
          "docProps modified are each refused by the reader; a page "
          "title, a page year and --release that disagree with the file "
          "each halt and store nothing" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(4, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(4, name, False, _first(ex))
    report(4, name, ok, f"{sd}; held file: {detail}")


def real_rank(cur=None, files=None, raw_dir=None, spec=REAL, areas=None):
    """The held file's rank is its docProps modified (never its name), and
    equals the rank edition 1 records."""
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, f"no file with sha256 {files['sha256'][:16]}"
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_file10(p)
    if f["rank"] != m.docprops_modified(p):
        return False, f"{p.name}: the rank is not docProps modified"
    detail = f"{p.name}: rank {m.rank_text(f['rank'])} (docProps modified)"
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = 1", (f["vintage"],))
        rows = cur.fetchall()
        if len(rows) != 1 or m.release_rank(rows[0][0]) != f["rank"]:
            return False, (f"{p.name}: edition 1 records rank "
                           f"{m.release_rank(rows[0][0]) if rows else None},"
                           f" the file {f['rank']}")
        detail += "; edition 1 records the same rank"
    return True, detail


def gate_5_older_file(cur):
    name = ("older-source guard on the page, --release and --file paths; "
            "the rank is the file's docProps modified, never its name")
    problems = []

    def older(e, how):
        e.seed(e.cur)
        # the content is modified 2025-10-28; the NAME looks newer
        path, url = e.named("File_10_-_IoD2025_Local_Authority_District_"
                            "Summaries__lower-tier__v9_FINAL.xlsx",
                            serve=how != "file", modified=EARLIER,
                            overrides={("E06000002", "imd_average_score"):
                                       35.6})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2025"]
        if how == "file":
            argv += ["--file", path]
        return argv

    def body(e):
        for how in ("page", "release", "file"):
            def one(c, how=how):
                with env(c) as e2, specs():
                    argv = older(e2, how)
                    before = state(c)
                    rc, text, logged = e2.cmd(argv)
                    if not (rc == "halt" and f"{P} (older" in text
                            and "older file" in text
                            and state(c) == before and not logged.called):
                        problems.append(f"{how}: rc {rc}: {text[-160:]}")
            _in_savepoint(e.cur, one)

        def override(c):
            with env(c) as e2, specs():
                argv = older(e2, "file") + ["--allow-older-file"]
                rc, text, logged = e2.cmd(argv)
                eds = tl.editions(c)
                if not (rc == 0 and eds == [(1, None, N), (2, 1, N)]
                        and logged.called and "--allow-older-file given"
                        in logged.call_args[0][2]):
                    problems.append(f"--allow-older-file: rc {rc}, {eds}")
        _in_savepoint(e.cur, override)

        def name_is_not_evidence(c):
            # a file named like an older one but newer by docProps is newer
            with env(c) as e2, specs():
                e2.seed(c)
                p, _ = e2.named("File_10_-_IoD2025_Local_Authority_District"
                                "_Summaries__lower-tier__v1_OLD.xlsx",
                                modified=LATER,
                                overrides={("E06000002", "imd_average_score"):
                                           35.6})
                rc, text, _ = e2.cmd(["load", "--commit"])
                if rc != 0 or tl.editions(c) != [(1, None, N), (2, 1, N)]:
                    problems.append(f"a newer file with an older-looking "
                                    f"name was refused: rc {rc}")
        _in_savepoint(e.cur, name_is_not_evidence)
    scenario(cur, body)
    seeded = not problems
    sd = ("a file modified 2025-10-28 named like v9, offered by the page, "
          "--release and --file against a held 2025-11-14 file, is skipped "
          "and halts with nothing stored or logged; --allow-older-file "
          "overrides and is logged; a newer file named v1 is newer"
          if seeded else "; ".join(problems[:3]))
    mixed(5, name, seeded, sd, cur, real_rank)


# ---------------------------------------------------------------- gate 6-8

def rejects(e, argv, needle, before, *, commit=True, vintage=P):
    """None if the command was REJECTED (exit 1) naming `needle`, stored
    nothing, wrote no ledger row, and (with --commit) wrote one partial
    run-log row; else the problem."""
    rc, text, logged = e.cmd(argv)
    if rc != 1 or "REJECTED" not in text or needle not in text:
        return f"rc {rc}, not REJECTED naming {needle!r}: {text[-140:]}"
    if state(e.cur) != before:
        return "something was stored or a ledger row written"
    if commit:
        if logged.call_count != 1:
            return "no partial run-log row"
        notes = logged.call_args[0][2]
        if "PARTIAL RUN (exit 1)" not in notes or \
                f"rejected ['{vintage}'" not in notes:
            return f"run-log notes: {notes[:120]}"
    elif logged.called:
        return "a preview wrote a run-log row"
    return None


def gate_6_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row; blank and text cells halt")
    problems = []

    def case(label, setup, needle, commit=True, vintage=P):
        def body(c):
            with env(c) as e, specs():
                setup(e)
                before = state(c)
                argv = ["load"] + (["--commit"] if commit else [])
                bad = rejects(e, argv, needle, before, commit=commit,
                              vintage=vintage)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)

    def equal_rank(e):
        e.seed(e.cur)
        e.file(overrides={("E06000002", "imd_average_score"): 35.6})

    def partial_keys(e):
        e.seed(e.cur)
        e.file(modified=LATER, codes=CODES[:-1] + ["E09000001"])
    case("equal docProps, other content", equal_rank,
         "two files claim the same release")
    case("equal docProps, preview exits 1 and logs nothing", equal_rank,
         "two files claim the same release", commit=False)
    case("a reissue with another key set", partial_keys, m.PARTIAL)
    case("another vintage into an empty live table",
         lambda e: e.file(P30, modified=datetime(2030, 11, 1, tzinfo=UTC)),
         "empty live table", vintage=P30)

    def accepted(c):
        with env(c) as e, specs():
            e.seed(c)
            e.file(overrides={("E06000002", "imd_average_score"): 35.6})
            rc, text, logged = e.cmd(["load", "--commit", "--accept-reissue",
                                      P])
            if not (rc == 0 and "ACCEPTED REISSUE" in text
                    and tl.editions(c) == [(1, None, N), (2, 1, N)]
                    and "--accept-reissue" in logged.call_args[0][2]):
                problems.append(f"--accept-reissue: rc {rc}: {text[-120:]}")
            # an acceptance naming a vintage that is not compared halts
            e.file(modified=datetime(2026, 1, 5, tzinfo=UTC),
                   overrides={("E06000002", "imd_average_score"): 35.7})
            ok, _ = e.halts(["load", "--commit", "--accept-reissue",
                             "IoD2019"], state(c), "IoD2019")
            if not ok:
                problems.append("--accept-reissue of a vintage not compared "
                                "did not halt")
    _in_savepoint(cur, accepted)

    def cells(c):
        with env(c) as e, specs():
            e.seed(c)
            for what, cell in (("blank", None), ("string", "[x]")):
                e.file(modified=LATER,
                       overrides={("E06000002", "imd_extent"): cell})
                halted, text = e.halts(["load", "--commit"], state(c))
                if not halted:
                    problems.append(f"a {what} cell did not halt")
            e.file(modified=LATER, overrides={
                ("E06000002", "imd_rank_of_average_rank"): 1})
            halted, text = e.halts(["load", "--commit"], state(c),
                                   "imd_rank_of_average_rank")
            if not halted:
                problems.append("a rank that follows no measure did not "
                                "halt")
    _in_savepoint(cur, cells)

    def within(c):
        with env(c) as e, specs():
            e.seed(c)
            e.reissue(overrides={("E06000002", "imd_average_score"): 35.6})
            rc, text, _ = e.cmd(["load", "--commit"])
            if rc != 0 or tl.editions(c) != [(1, None, N), (2, 1, N)]:
                problems.append(f"a newer reissue was refused: rc {rc}")
    _in_savepoint(cur, within)
    report(6, name, not problems, "equal docProps with other content, a "
           "reissue changing the key set and another vintage into an empty "
           "live table are each REJECTED (exit 1) with nothing stored, no "
           "ledger row and one partial run-log row (none in a preview); "
           "--accept-reissue stores edition 2 and is logged; a blank cell, "
           "a text cell and a rank that follows no measure halt; a newer "
           "reissue is stored" if not problems else "; ".join(problems[:3]))


def gate_7_ledger_rule(cur):
    name = ("ledger rule: the skip needs the (where, sha256) pair for the "
            "vintage; a failing insert rolls the load back")
    problems = []

    def pair(e):
        path = e.file(modified=LATER)
        rank = LATER
        sha = m.content_sha256(path)
        src = m.ledger_source(e.last_url, P, rank)
        # (URL, sha) recorded, but under another vintage: read
        e.cur.execute(f"INSERT INTO public.{LEDGER} (vintage, source_file, "
                      "file_sha256, outcome, edition) VALUES (%s, %s, %s, "
                      "'unchanged', 1)", (P30, src, sha))
        rc, text, _ = e.cmd(["load"])
        if rc != 0 or "nothing parsed" in text or f"{P}: new" not in text:
            problems.append("a pair held for another vintage skipped the "
                            "file")
    scenario(cur, pair)

    def same_name(e):
        path0 = e.file()
        e.ok(e.cur, ["load", "--commit"])
        url0 = e.last_url
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "nothing parsed" not in text or logged.called:
            problems.append("a held pair did not skip the read")
        other, _ = e.named("File_10_-_IoD2025_Local_Authority_District_"
                           "Summaries__lower-tier__v2.xlsx", modified=LATER,
                           serve=False,
                           overrides={("E06000002", "imd_average_score"):
                                      35.6})
        e.urls[url0] = other
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" in text or f"{P}: revised" not in text:
            problems.append("the same URL with other bytes was skipped")
        e.urls[url0] = path0
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" not in text:
            problems.append("the held pair no longer skips")
        rc, text, _ = e.cmd(["load", "--recheck", P])
        if f"{P}: unchanged" not in text:
            problems.append("--recheck did not read a ledger-held file")
    scenario(cur, same_name)

    def failing(label, target, attr):
        def body(c):
            with env(c) as e, specs():
                e.file()
                before = state(c)
                with mock.patch.object(target, attr,
                                       side_effect=RuntimeError("boom")):
                    rc, text, logged = e.cmd(["load", "--commit"])
                if not (rc == 1 and state(c) == before
                        and f"{P}: FAILED" in text and logged.called
                        and "PARTIAL RUN" in logged.call_args[0][2]):
                    problems.append(f"{label}: rc {rc}, state kept="
                                    f"{state(c) == before}")
        _in_savepoint(cur, body)
    failing("a failing ledger insert", pe, "record_file_check")
    failing("a failing live insert", m, "insert_live")
    failing("a failing edition insert", core, "insert_edition")
    report(7, name, not problems, "a pair held under another vintage does "
           "not skip; the held pair skips with nothing parsed or logged; "
           "the same URL with other bytes is read; --recheck reads a held "
           "file; a failing ledger, live or edition insert rolls back all "
           "three and logs a partial run" if not problems
           else "; ".join(problems[:3]))


def gate_8_preview(cur):
    name = "preview and simulate write nothing"
    problems = []
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]

    def body(e):
        e.file()
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"new vintage {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.reissue(overrides={("E06000002", "imd_average_score"): 35.6})
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"], ["status"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}, state kept="
                                f"{state(e.cur) == before}")
        e.ok(e.cur, ["load", "--commit"])
        before = state(e.cur)
        for argv in (["refresh-latest"], ["restore-edition", P, "1"],
                     ["refresh-latest", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"{argv} after a reissue: rc {rc}")
    scenario(cur, body)

    def legacy(e, path, files):
        before = state(e.cur)
        with mock.patch.object(m, "LEGACY_LIVE", e.legacy(e.cur)), \
                mock.patch.object(m, "LEGACY_FILE", files):
            for argv in (["migrate-legacy", path],
                         ["migrate-legacy", path, "--simulate"],
                         ["ddl"]):
                rc, text, logged = e.cmd(argv)
                if rc != 0 or state(e.cur) != before or logged.called:
                    problems.append(f"{argv[0]} {argv[2:]}: rc {rc}")
    legacy_world(cur, legacy)
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    if cur.fetchone()[0] != runs:
        problems.append("the run log changed")
    report(8, name, not problems, "load, refresh-latest, restore-edition, "
           "migrate-legacy and ddl in preview and --simulate leave the "
           "editions, the live table and the ledger as they were and log "
           "nothing" if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 9

def real_held_reread(cur, spec=REAL, files=None, raw_dir=None, areas=None):
    """The held file read again has the content sha256 of edition 1 'as
    loaded' (la_name and the ten measures at their live scales) and of live,
    and the tip's when no reissue has been stored: so a re-read by the page,
    --release or --file is unchanged, never a reissue."""
    _need(cur, spec)
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, f"no file with sha256 {files['sha256'][:16]}"
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_file10(p)
    with contextlib.redirect_stdout(io.StringIO()):
        built = m._build(cur, f)
    period = f["vintage"]
    held = m.records(cur, spec, period, 1)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (period,))
    label = cur.fetchall()
    bad = []
    if label != [(core.AS_LOADED_LABEL,)]:
        bad.append(f"{period}: edition 1 is not the 'as loaded' one")
    a, b = m.rows_content_sha(held), m.rows_content_sha(built)
    if a != b:
        bad.append(f"{period}: edition 1 content sha {a[:12]} differs from "
                   f"the held file read now {b[:12]}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{p.name} read now has edition 1's content sha256 ({a[:12]}.., "
        f"{len(held):,} rows): every re-read is unchanged")


def gate_9_byte_identical_reread(cur):
    name = ("a byte-identical re-read is unchanged on every path against "
            "edition 1 'as loaded'")
    problems = []
    out = {}

    def body(e, path, files):
        legacy = e.legacy(e.cur)
        plan, text = e.migrate(e.cur, path, files, legacy=legacy)
        if "0 differences" not in text:
            problems.append(f"the migration proof: {text[-120:]}")
            return
        d = Path(e.root) / "disk"
        d.mkdir()
        (d / path.name).write_bytes(path.read_bytes())
        with mock.patch.object(m, "EXPECTED_AREAS", N):
            out["real"] = real_held_reread(e.cur, ZZ, files, d, areas=N)
        e.urls[e.URL] = path
        e.page(2025, e.URL)
        src_before = tl.live_val(e.cur, "E06000001", "source")
        snap = tables_state(e.cur)
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" not in text:
            problems.append("the held file was parsed again by the page")
        for argv in (["load", "--recheck", P],
                     ["load", "--release", "2025", "--recheck", P],
                     ["load", "--file", path],
                     ["load", "--file", path, "--no-page"],
                     ["load", "--file", path, "--commit"],
                     ["load", "--file", path, "--no-page", "--recheck", P,
                      "--commit"],
                     ["load", "--recheck", P, "--commit"]):
            rc, text, _ = e.cmd(argv)
            if rc != 0 or f"{P}: unchanged" not in text \
                    or "REJECTED" in text or "two files claim" in text:
                problems.append(f"{' '.join(map(str, argv[:4]))}: rc {rc}: "
                                f"{text[-120:]}")
        if tables_state(e.cur) != snap or tl.editions(e.cur) != [
                (1, None, N)]:
            problems.append("a re-read stored an edition or moved live")
        if [o for _, _, o, _ in tl.ledger(e.cur)] != ["unchanged"] * 4:
            problems.append("the re-reads did not add 'unchanged' rows only")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text or tl.live_val(
                e.cur, "E06000001", "source") != src_before:
            problems.append("refresh-latest planned a write after a re-read")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
        # published zeros stay 0 on every path
        if tl.live_val(e.cur, "E08000019",
                       "imd_proportion_most_deprived_10") != 0:
            problems.append("a published zero is not 0")
        # a file with other content is not the held file
        other = write_file10(Path(e.root) / "disk2" / path.name,
                             overrides={("E06000002", "imd_average_score"):
                                        35.6})
        ok, detail = real_held_reread(
            e.cur, ZZ, {"sha256": m.content_sha256(other)}, other.parent,
            areas=N)
        if ok or "differs" not in detail:
            problems.append("a file with other content was called equal")
    legacy_world(cur, body)
    seeded = not problems and out.get("real", (False,))[0]
    sd = ("seeded: after migrate-legacy the same bytes read by the page, "
          "--release, --file, --file --no-page, --recheck and with "
          "--commit are unchanged against edition 1, store no edition, add "
          "'unchanged' ledger rows only and leave live and status alone; "
          "the held file read now has edition 1's content sha"
          if seeded else f"{out} {problems[:3]}")
    mixed(9, name, seeded, sd, cur, real_held_reread)


# --------------------------------------------------------------- gate 10

def gate_10_reissue_and_refresh(cur):
    name = ("a reissue stores edition 2; refresh-latest changes only the "
            "live vintage, sets source on every row and copies loaded_at")
    problems = []

    def body(e):
        e.seed(e.cur)
        e.file(P30, modified=datetime(2030, 11, 1, 9, 0, 0, tzinfo=UTC))
        e.ok(e.cur, ["load", "--commit"])
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        e.cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{ZZ.editions_table} t WHERE "
                      "vintage = %s", (P30,))
        held_before = e.cur.fetchone()[0]
        first = tl.live_val(e.cur, "E06000002", "imd_average_score")
        e.reissue(overrides={("E06000002", "imd_average_score"): 35.6})
        rc, text, _ = e.cmd(["load", "--commit", "--release", "2025"])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N), (2, 1, N)] or \
                tl.editions(e.cur, P30) != [(1, None, N)]:
            problems.append(f"the reissue did not store edition 2 of {P} "
                            f"only: rc {rc}")
        if tl.live_val(e.cur, "E06000002", "imd_average_score") != first:
            problems.append("the reissue reached live before refresh-latest")
        if pe.status(e.cur, m.profile())["pending_refresh"] != [P]:
            problems.append("status does not show the refresh pending")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("the refresh-latest preview wrote")
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if rc != 0:
            problems.append(f"refresh-latest --commit: rc {rc}: "
                            f"{text[-120:]}")
        if str(tl.live_val(e.cur, "E06000002", "imd_average_score")) \
                != "35.600":
            problems.append("live does not hold the reissued value")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "loaded_at <> '2026-01-01'")
        moved = e.cur.fetchone()[0]
        e.cur.execute(f"SELECT DISTINCT loaded_at FROM public."
                      f"{ZZ.editions_table} WHERE vintage = %s AND edition "
                      "= 2", (P,))
        ed2 = {r[0] for r in e.cur.fetchall()}
        e.cur.execute(f"SELECT DISTINCT loaded_at FROM public.{ZZ.live_table}"
                      " WHERE lad24cd = 'E06000002'")
        if moved < 1 or e.cur.fetchall() != [(next(iter(ed2)),)]:
            problems.append(f"loaded_at was not copied from edition 2 "
                            f"({moved} row(s) moved)")
        # source: one label on every row, the tip's
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE vintage = %s AND edition "
                      "= 2", (P,))
        label = e.cur.fetchone()[0]
        e.cur.execute(f"SELECT DISTINCT source FROM public.{ZZ.live_table}")
        if e.cur.fetchall() != [(label,)]:
            problems.append("live source is not the tip's label on every "
                            "row")
        # only the revised cell changed among the measures
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} l JOIN "
                      f"public.{ZZ.editions_table} d ON d.lad24cd = "
                      "l.lad24cd AND d.vintage = l.vintage AND d.edition = 1 "
                      "WHERE l.imd_average_score IS DISTINCT FROM "
                      "d.imd_average_score")
        if e.cur.fetchone()[0] != 1:
            problems.append("refresh-latest changed more than the revised "
                            "row")
        e.cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{ZZ.editions_table} t WHERE "
                      "vintage = %s", (P30,))
        if e.cur.fetchone()[0] != held_before:
            problems.append("the held vintage's editions changed")
        e.cur.execute(f"SELECT DISTINCT vintage FROM public.{ZZ.live_table}")
        if e.cur.fetchall() != [(P,)]:
            problems.append("live holds a vintage other than IoD2025")
        if not pe.status(e.cur, m.profile())["ok"] or core.rows_differing(
                e.cur, ZZ, P, 2):
            problems.append("live does not equal the tip after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("a second refresh-latest plans a write")
        # a drifted live row is not overwritten without --accept-drift
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET imd_extent = "
                      "0.9 WHERE lad24cd = 'E06000001'")
        snap = state(e.cur)
        ok, _ = e.halts(["refresh-latest", "--commit"], snap)
        if not ok:
            problems.append("a drifted live vintage was overwritten without "
                            "--accept-drift")
    scenario(cur, body)
    report(10, name, not problems, "a reissue (later docProps modified) "
           "waits in edition 2 for refresh-latest (preview writes "
           "nothing); the commit changes exactly the revised row, sets one "
           "source label on every row, copies the edition's loaded_at, "
           "leaves the held IoD2030 editions and the one-vintage live table "
           "as they were; a second refresh plans nothing; a drifted live "
           "vintage is not overwritten without --accept-drift"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 11

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def _note_lines(note, label, vintage=m.LIVE_VINTAGE):
    """{vintage: (rows, sha256)} from lines `<label> la_imd_2025 <vintage>
    rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the decision
    note (the two halves joined), or {}. A `before-state-all ...` line is a
    different record and does not match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                rf"(?m)^{re.escape(label)} {TABLE} (IoD[0-9]{{4}}) "
                r"rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text)}


def note_hashes(note):
    return _note_lines(note, "before-state")


def w1_note_hashes(note):
    return _note_lines(note, "w1-read")


def w1_read_sha(rows):
    """sha256 of what W1 reads of the IMD layer: one line per row sorted by
    lad24cd, lad24cd|imd_rank_of_average_rank, LF-joined, UTF-8."""
    lines = sorted(f"{r[m.KEY]}|{r[W1_COL]}" for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def live_all_sha(cur, spec=REAL):
    """sha256 of every stored column of the live table but the added
    `vintage`: lad24cd|la_name|the ten measures|source|loaded_at (UTC ISO,
    NULL as ''), sorted by lad24cd, LF-joined."""
    names = (m.KEY, m.NAME) + m.VALUES + ("source",)
    cur.execute(f"SELECT {', '.join(names)}, loaded_at AT TIME ZONE 'UTC' "
                f"FROM public.{spec.live_table}")
    lines = sorted("|".join("" if v is None else str(v) for v in r)
                   for r in cur.fetchall())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def note_lines_for(cur, spec=REAL):
    """The decision note's hash lines for the live table as it stands
    (read-only): before-state (rows_content_sha, the loader's), the
    before-state-all line over every stored column but vintage, and the
    w1-read line. Print them before the migration."""
    recs = m.records(cur, spec, m.LIVE_VINTAGE)
    n = len(recs)
    return [
        f"before-state {TABLE} {m.LIVE_VINTAGE} rows={n} "
        f"{hash_fields(m.rows_content_sha(recs))}",
        f"before-state-all {TABLE} {m.LIVE_VINTAGE} rows={n} "
        f"{hash_fields(live_all_sha(cur, spec))}",
        f"w1-read {TABLE} {m.LIVE_VINTAGE} rows={n} "
        f"{hash_fields(w1_read_sha(recs))}"]


def real_edition1_hash(cur, spec=REAL, note=NOTE, legacy_rows=None):
    """Edition 1 'as loaded' of the migrated vintage equals the before-state
    line in the decision note: the row count and rows_content_sha, and the
    row count is the surveyed live count."""
    _need(cur, spec)
    legacy_rows = m.LEGACY_LIVE[0] if legacy_rows is None else legacy_rows
    recorded = note_hashes(note)
    bad, parts = [], []
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "release_label = %s ORDER BY 1", (core.AS_LOADED_LABEL,))
    migrated = [str(r[0]) for r in cur.fetchall()]
    if not migrated:
        bad.append("no edition 1 'as loaded' (migrate-legacy not run)")
    for p in migrated:
        recs = m.records(cur, spec, p, 1)
        n, h = len(recs), m.rows_content_sha(recs)
        if n != legacy_rows:
            bad.append(f"{p}: edition 1 has {n} rows, surveyed "
                       f"{legacy_rows}")
        if Path(note).exists():
            if p not in recorded:
                bad.append(f"the decision note records no 'before-state "
                           f"{TABLE} {p} rows=.. sha256-first32=.. "
                           "sha256-last32=..' line")
            elif recorded[p] != (n, h):
                bad.append(f"{p}: edition 1 {n} rows sha256 {h[:16]}.. "
                           f"differs from the note's {recorded[p][0]} rows "
                           f"{recorded[p][1][:16]}..")
        parts.append(f"{p} {n:,} rows {h[:16]}..")
    for p in recorded:
        if p not in migrated:
            bad.append(f"the note has a line for {p}, which has no edition "
                       "1 as loaded")
    return not bad, "; ".join(bad[:3]) if bad else (
        "edition 1 of every migrated vintage hashes to the before-state "
        "line in the decision note (" + "; ".join(parts) + ")")


def gate_11_before_state_hash(cur):
    name = ("edition 1 equals the before-state line recorded in the "
            "decision note")
    out = {}

    def body(e, path, files):
        legacy = e.legacy(e.cur)
        lines = note_lines_for(e.cur, ZZ)
        e.migrate(e.cur, path, files, legacy=legacy)
        recs = m.records(e.cur, ZZ, P, 1)
        h = m.rows_content_sha(recs)
        out["same"] = h == m.rows_content_sha(m.records(e.cur, ZZ, P))
        out["printed"] = lines
        out["scan_safe"] = not any(re.search(r"[0-9a-f]{64}", x)
                                   for x in lines)
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = lines[0]
            note.write_text("intro\n" + "\n".join(lines) + "\n",
                            encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(note.read_text().replace(h[:32], "0" * 32),
                            encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(lines[1] + "\n", encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}"),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, N)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                N)
            note.write_text(line, encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(e.cur, ZZ, note, N + 5)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET imd_average_score "
                      "= imd_average_score + 1 WHERE lad24cd = 'E06000001'")
        out["changed"] = m.live_state(e.cur, ZZ)[:2] != legacy
        with mock.patch.object(m, "LEGACY_LIVE", legacy), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = e.cmd(["migrate-legacy", path, "--commit"])
        out["twice"] = rc == "halt" and ("runs once" in text
                                         or "not as surveyed" in text
                                         or "already holds" in text)
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(11, name, False, _first(ex))
    ok = (out.get("same") and out.get("scan_safe")
          and out.get("ok", (0,))[0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and out.get("changed")
          and out.get("twice"))
    mixed(11, name, ok,
          "a seeded migration stores an edition 1 whose hash equals the "
          "live rows' and the printed note line (scan-safe: no 64-hex run); "
          "a wrong hash, a wrong row count, a missing line (a "
          "before-state-all line does not count) and a missing note fail; "
          "a later change shows; a second migration is refused" if ok
          else f"seeded: {out}", cur, real_edition1_hash)


# --------------------------------------------------------------- gate 12

def real_migration_proof(cur, spec=REAL, files=None, raw_dir=None,
                         areas=None):
    """The migration proof re-run read-only from the file on disk (found by
    sha256): the parser on the held file reproduces every value of edition 1
    'as loaded' (lad24cd, la_name and the ten measures; 0 differences) and
    the file's own checks pass."""
    _need(cur, spec)
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, (f"no file in {raw_dir or m.RAW_DIR} with sha256 "
                       f"{files['sha256'][:16]}")
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_file10(p)
    with contextlib.redirect_stdout(io.StringIO()):
        built = m._build(cur, f)
    period = f["vintage"]
    held = m.records(cur, spec, period, 1)
    if not held:
        return False, f"{period}: no edition 1"
    cells, diffs = m._proof(held, built)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (period,))
    if cur.fetchall() != [(core.AS_LOADED_LABEL,)]:
        return False, f"{period}: edition 1 is not the 'as loaded' one"
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{cells:,} values of edition 1 ({len(held):,} rows) "
                       f"equal {p.name} read now (0 differences)")


def gate_12_migration_proof(cur):
    name = ("the migration proof re-run read-only from the file on disk; "
            "seeded: proof passes, a planted held difference stops "
            "migrate-legacy and stores nothing")
    problems = []

    def ok_world(e, path, files):
        with mock.patch.object(m, "LEGACY_LIVE", e.legacy(e.cur)), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = e.cmd(["migrate-legacy", path, "--commit"])
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        raw = Path(e.root) / "raw"
        raw.mkdir()
        (raw / path.name).write_bytes(path.read_bytes())
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw, areas=N)
        if not ok or "0 differences" not in detail:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(e.cur, ZZ, {**files,
                                                      "sha256": "0" * 64},
                                          raw, areas=N)
        if ok or "no file" not in detail:
            problems.append("a file with another sha256 was accepted")
        (raw / path.name).write_bytes(path.read_bytes() + b"\0")
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw, areas=N)
        if ok or "no file" not in detail:
            problems.append("a changed file on disk was accepted")
        other = write_file10(Path(e.root) / "raw2" / path.name,
                             overrides={("E06000002", "imd_average_score"):
                                        35.6})
        ok, detail = real_migration_proof(
            e.cur, ZZ, {**files, "sha256": m.content_sha256(other)},
            other.parent, areas=N)
        if ok or "differences" not in detail:
            problems.append("a file that differs from edition 1 passed")

    def planted(sql, needle, surveyed_first=False):
        def f(e, path, files):
            surveyed = e.legacy(e.cur) if surveyed_first else None
            e.cur.execute(sql)
            surveyed = surveyed or e.legacy(e.cur)
            before = state(e.cur)
            with mock.patch.object(m, "LEGACY_LIVE", surveyed), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = e.cmd(["migrate-legacy", path,
                                          "--commit"])
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    live = ZZ.live_table
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET imd_average_score = imd_average_score + 1"
        " WHERE lad24cd = 'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET la_name = 'Other' WHERE lad24cd = "
        "'E06000001'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET imd_rank_of_extent = 9 WHERE lad24cd = "
        "'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"DELETE FROM public.{live} WHERE lad24cd = 'E06000002'",
        "not as surveyed", surveyed_first=True))
    mixed(12, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the file by sha256; a changed or missing file and a file that "
          "is not edition 1 are caught; planted differences in a measure, "
          "a name and a rank, and a dropped row stop migrate-legacy with "
          "nothing stored" if not problems else "; ".join(problems[:3]),
          cur, real_migration_proof)


# ------------------------------------------------------ gates 13 and 14

def gate_13_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        path = e.file()
        e.ok(e.cur, ["load", "--commit"])
        snap, led = tables_state(e.cur), len(tl.ledger(e.cur))
        for argv in (["load", "--commit"],
                     ["load", "--release", "2025", "--commit"],
                     ["load", "--file", path, "--commit"],
                     ["load", "--file", path, "--no-page", "--commit"],
                     ["refresh-latest", "--commit"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or tables_state(e.cur) != snap:
                problems.append(f"{' '.join(map(str, argv[:3]))}: changed "
                                f"the editions or live: rc {rc}")
        if any(o != "unchanged" for _, _, o, _ in tl.ledger(e.cur)[led:]):
            problems.append("a re-read ledger row is not 'unchanged'")
        before = state(e.cur)
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or state(e.cur) != before or logged.called:
            problems.append("a ledger-skipped load changed state or logged")
        rc, text, logged = e.cmd(["load", "--recheck", P, "--commit"])
        eds = tl.editions(e.cur)
        if rc != 0 or eds != [(1, None, N)]:
            problems.append(f"a recheck stored an edition: {eds}")
    scenario(cur, body)
    report(13, name, not problems, "a second load by the page, --release, "
           "--file and --file --no-page and a refresh-latest stored no "
           "edition and changed no live row (re-reads add 'unchanged' "
           "ledger rows only); a ledger-skipped load logs nothing"
           if not problems else "; ".join(problems[:3]))


def gate_14_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.live_val(e.cur, "E06000002", "imd_average_score")
        e.reissue(overrides={("E06000002", "imd_average_score"): 35.6})
        e.ok(e.cur, ["load", "--commit"])
        e.ok(e.cur, ["refresh-latest", "--commit"])
        snap = state(e.cur)
        rc, text, logged = e.cmd(["restore-edition", P, "1"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("restore-edition preview wrote")
        rc, text, logged = e.cmd(["restore-edition", P, "1", "--commit"])
        if rc != 0 or tl.editions(e.cur)[-1] != (3, 2, N) or \
                not logged.called:
            problems.append(f"restore --commit: rc {rc}: {text[-120:]}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE edition = 3")
        if e.cur.fetchall() != [("restored from edition 1",)]:
            problems.append("the restored edition is not labelled")
        if str(tl.live_val(e.cur, "E06000002", "imd_average_score")) != \
                "35.600":
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.live_val(e.cur, "E06000002", "imd_average_score") != first \
                or core.rows_differing(e.cur, ZZ, P, 3):
            problems.append("refresh-latest did not apply the restored "
                            "edition")
        ed1 = {tuple(sorted(r.items())) for r in m.records(e.cur, ZZ, P, 1)}
        ed3 = {tuple(sorted(r.items())) for r in m.records(e.cur, ZZ, P, 3)}
        if ed1 != ed3:
            problems.append("edition 3 does not hold edition 1's rows")
        for argv in (["restore-edition", P, "3", "--commit"],
                     ["restore-edition", P, "9", "--commit"]):
            ok, _ = e.halts(argv, state(e.cur))
            if not ok:
                problems.append(f"{' '.join(argv[:3])} did not halt")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
    scenario(cur, body)
    report(14, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') and applied by refresh-latest; the "
           "tip and a missing edition are refused; a preview writes nothing"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 15

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


def gate_15_no_network_no_secrets(cur):
    name = ("no network, no secret in source or output, nothing written "
            "outside the rolled-back transaction; a download never "
            "overwrites a same-named file with different content")
    files = [Path(__file__).resolve(), LOADER, HERE / "period_editions.py",
             HERE / "geography.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]
    cur.execute("SELECT to_regclass(%s), to_regclass(%s)",
                (f"public.{REAL.editions_table}",
                 f"public.{ledger_name(REAL)}"))
    real_before = cur.fetchone()

    def body(c):
        with env(c) as e, specs():
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.file()
                rc, _, _ = e.cmd(["load", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load"])
                raw = e.root / "raw"
                body_a = write_file10(e.root / "dl" / "a.xlsx").read_bytes()
                body_b = write_file10(e.root / "dl" / "b.xlsx",
                                      modified=LATER).read_bytes()
                url = "https://assets.example/media/3/f.xlsx"
                with contextlib.redirect_stdout(io.StringIO()), \
                        mock.patch.object(m, "MIN_FILE_BYTES", 10):
                    p1, _ = m.fetch(url, raw, _Session(body_a, url))
                    sha1 = m.content_sha256(p1)
                    p2, _ = m.fetch(url, raw, _Session(body_b, url))
                    p3, _ = m.fetch(url, raw, _Session(body_a, url))
                kept = (p1 == raw / "f.xlsx" and m.content_sha256(p1) == sha1
                        and p2 != p1 and p2.name.startswith("f-")
                        and p3 == p1)
            return rc, rc_s, rc_r, rc_l, kept
    try:
        rc, rc_s, rc_r, rc_l, kept = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(15, name, False, f"{type(ex).__name__}: {ex}")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs_after = cur.fetchone()[0]
    cur.execute("SELECT to_regclass(%s), to_regclass(%s)",
                (f"public.{REAL.editions_table}",
                 f"public.{ledger_name(REAL)}"))
    real_after = cur.fetchone()
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    literal = [n for n, t in texts.items() if pat.search(t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    ok = (not attempts and rc == rc_s == rc_r == rc_l == 0 and kept
          and runs == runs_after and real_before == real_after
          and not literal and not in_src and not in_out)
    report(15, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned "
           f"{rc}/{rc_s}/{rc_r}/{rc_l}; a same-named different download "
           f"was saved beside it, the first untouched={kept}; run-log rows "
           f"{runs}->{runs_after}; real editions table and ledger "
           f"unchanged={real_before == real_after}; secret-like literal in "
           f"source {literal or 'none'}; {len(secrets)} secret-named "
           f"settings checked against {len(files)} files and "
           f"{len(OUTPUT)} output lines: in source {in_src or 'none'}, in "
           f"output {in_out or 'none'}")


# --------------------------------------------------------------- gate 16

def real_rank_ranges(cur, spec=REAL, n=AREAS):
    """In live and in each vintage's tip edition: the file's own checks
    (m.checks): each rank column a ranking of 1..n following its measure
    (shared ranks allowed where the measure ties), proportions and extents
    in [0, 1]; and n authorities."""
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, EMPTY
    bad, lines = [], []
    for p in periods:
        tip = _tip(cur, spec, p)
        for what, recs in ((f"{spec.live_table} {p}", m.records(cur, spec, p)),
                           (f"{spec.editions_table} {p} ed{tip}",
                            m.records(cur, spec, p, tip))):
            if len(recs) != n:
                bad.append(f"{what}: {len(recs)} rows, expected {n}")
                continue
            try:
                lines = m.checks(recs)
            except ValueError as e:
                bad.append(f"{what}: {_first(e)}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} vintage(s), live and tip: {n} authorities; "
        + "; ".join(lines[:3]) + "; and the other ranks and ranges pass")


def gate_16_rank_ranges(cur):
    name = ("rank columns are rankings of 1..n following their measure; "
            "proportions and extents in [0, 1]")
    out = {}
    miss = []

    def body(e):
        e.seed(e.cur)
        out["base"] = real_rank_ranges(e.cur, ZZ, n=N)
        live = ZZ.live_table
        for sql, needle in (
            (f"UPDATE public.{live} SET imd_rank_of_average_rank = 1 WHERE "
             "lad24cd = 'E06000002'", "imd_rank_of_average_rank"),
            (f"UPDATE public.{live} SET imd_rank_of_extent = 9 WHERE "
             "lad24cd = 'E06000002'", "imd_rank_of_extent"),
            (f"UPDATE public.{live} SET imd_proportion_most_deprived_10 = "
             "1.5 WHERE lad24cd = 'E06000002'",
             "imd_proportion_most_deprived_10"),
            (f"UPDATE public.{live} SET imd_extent = -0.1 WHERE lad24cd = "
             "'E06000002'", "imd_extent"),
            (f"UPDATE public.{live} SET imd_average_score = 99 WHERE "
             "lad24cd = 'E06000002'", "imd_rank_of_average_score"),
        ):
            ok, detail = plant(e.cur, sql, lambda: real_rank_ranges(
                e.cur, ZZ, n=N))
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        ok, detail = real_rank_ranges(e.cur, ZZ, n=N + 1)
        if ok or "rows, expected" not in detail:
            miss.append("a wrong authority count passed")
        # published zeros kept: two ties at rank 4 in the proportion column
        out["zeros"] = (tl.live_val(e.cur, "E08000019",
                                    "imd_proportion_most_deprived_10") == 0
                        and tl.live_val(e.cur, "E08000019",
                                        "imd_rank_of_proportion") == 4)
        # a file whose rank follows no measure, or a proportion above 1,
        # halts the load
        for label, ov in (("rank", {("E06000002", "imd_rank_of_extent"): 9}),
                          ("range", {("E06000002",
                                      "imd_proportion_most_deprived_10"):
                                     1.5})):
            e.file(modified=LATER, overrides=ov)
            ok, _ = e.halts(["load", "--commit"], state(e.cur))
            if not ok:
                miss.append(f"a file with a bad {label} did not halt")
    scenario(cur, body)
    ok = out["base"][0] and not miss and out.get("zeros")
    mixed(16, name, ok, "the seeded state passes (shared ranks where the "
          "published zeros tie); a rank that follows no measure, a rank "
          "outside 1..n, a proportion above 1, a negative extent and an "
          "average score out of rank order each fail; a published zero "
          "stays 0; a file with a bad rank or range halts the load"
          if ok else f"seeded: {out} {miss[:3]}", cur, real_rank_ranges)


# --------------------------------------------------------------- gate 17

def real_w1_read(cur, spec=REAL, note=NOTE, vintage=m.LIVE_VINTAGE,
                 rows=None):
    """The live rows of the live vintage: the count and w1_read_sha of
    (lad24cd, imd_rank_of_average_rank) equal the decision note's w1-read
    line taken before any change, so the map's IMD layer reads what it did."""
    _need(cur, spec)
    rows = AREAS if rows is None else rows
    recorded = w1_note_hashes(note).get(vintage)
    if not Path(note).exists():
        return False, f"decision note {Path(note).name} not written"
    if recorded is None:
        return False, (f"the decision note records no 'w1-read {TABLE} "
                       f"{vintage} rows=.. sha256-first32=.. "
                       "sha256-last32=..' line")
    recs = m.records(cur, spec, vintage)
    n, h = len(recs), w1_read_sha(recs)
    bad = []
    if n != rows:
        bad.append(f"live {vintage} has {n} rows, expected {rows}")
    if recorded != (n, h):
        bad.append(f"live {vintage} {W1_COL}: {n} rows sha256 {h[:16]}.. "
                   f"differs from the note's {recorded[0]} rows "
                   f"{recorded[1][:16]}..")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live {vintage} {W1_COL} ({n} rows) hashes to the note's w1-read "
        f"line ({h[:16]}..)")


def gate_17_w1_read(cur):
    name = ("imd_rank_of_average_rank in live equals the decision note's "
            "w1-read line (the map's IMD layer)")
    out = {}

    def body(e):
        e.seed(e.cur)
        before = w1_read_sha(m.records(e.cur, ZZ, P))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = f"w1-read {TABLE} {P} rows={N} {hash_fields(before)}"
            note.write_text("intro\n" + line + "\n", encoding="utf-8")
            kw = dict(note=note, rows=N)
            out["base"] = real_w1_read(e.cur, ZZ, **kw)
            # a reissue of everything but the rank of average rank (a score)
            # does not move what W1 reads
            e.reissue(overrides={("E06000002", "imd_average_score"): 35.6})
            e.ok(e.cur, ["load", "--commit"])
            e.ok(e.cur, ["refresh-latest", "--commit"])
            out["after_score"] = real_w1_read(e.cur, ZZ, **kw)
            out["score_moved"] = str(tl.live_val(
                e.cur, "E06000002", "imd_average_score")) == "35.600"
            # a reissued rank moves it
            e.file(modified=datetime(2026, 1, 5, tzinfo=UTC),
                   overrides={("E06000002", "imd_average_score"): 35.6,
                              ("E06000001", "imd_average_rank"): 24000.5,
                              ("E06000002", "imd_average_rank"): 25000.12,
                              ("E06000001", "imd_rank_of_average_rank"): 2,
                              ("E06000002", "imd_rank_of_average_rank"): 1})
            e.ok(e.cur, ["load", "--commit"])
            e.ok(e.cur, ["refresh-latest", "--commit"])
            out["after_rank"] = real_w1_read(e.cur, ZZ, **kw)
            note.write_text("intro\n", encoding="utf-8")
            out["no_line"] = real_w1_read(e.cur, ZZ, **kw)
            note.write_text(line.replace("w1-read", "w1-read-all") + "\n",
                            encoding="utf-8")
            out["other_label"] = real_w1_read(e.cur, ZZ, **kw)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}")
                            + "\n", encoding="utf-8")
            out["bad_rows"] = real_w1_read(e.cur, ZZ, **kw)
            out["no_note"] = real_w1_read(e.cur, ZZ, note=Path(tmp) / "x",
                                          rows=N)
            out["scan_safe"] = not re.search(r"[0-9a-f]{64}", line)
    scenario(cur, body)
    ok = (out["base"][0] and out["after_score"][0] and out["score_moved"]
          and not out["after_rank"][0]
          and "differs" in out["after_rank"][1]
          and not out["no_line"][0] and not out["other_label"][0]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and out["scan_safe"])
    mixed(17, name, ok, "seeded: the read passes on the live rows the note "
          "was taken from, stays equal after a reissue and refresh of a "
          "score (W1 reads imd_rank_of_average_rank only), changes with a "
          "reissued rank, and a missing or other-labelled line, a wrong row "
          "count and a missing note fail" if ok else f"{out}", cur,
          real_w1_read)


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_single_vintage, gate_4_identity, gate_5_older_file,
         gate_6_stop_conditions, gate_7_ledger_rule, gate_8_preview,
         gate_9_byte_identical_reread, gate_10_reissue_and_refresh,
         gate_11_before_state_hash, gate_12_migration_proof, gate_13_rerun,
         gate_14_restore, gate_15_no_network_no_secrets, gate_16_rank_ranges,
         gate_17_w1_read)


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
                   f"gate raised {type(e).__name__}: {_first(e)}")


def leftovers():
    """zz% relations in the real database, read on a separate read-only
    connection after the rollback."""
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT relname FROM pg_class WHERE relname LIKE "
                        "'zz%%' AND relnamespace = 'public'::regnamespace")
            return sorted(r[0] for r in cur.fetchall())
    finally:
        conn.rollback()
        conn.close()


def print_note_lines():
    """The decision note's hash lines for the live table as it stands, read
    on a read-only connection (nothing is written)."""
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            for line in note_lines_for(cur, REAL):
                print(line)
    finally:
        conn.rollback()
        conn.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--print-note-lines"]:
        return print_note_lines()
    if argv:
        sys.exit("usage: s5_imd_editions_verify.py [--print-note-lines]")
    conn = get_conn()
    try:
        with conn.cursor() as raw:
            cur = SafeCur(raw)
            setup_throwaway(cur)
            for gate in GATES:
                run_gate(cur, gate)
    finally:
        conn.rollback()
        conn.close()
    left = leftovers()
    mine = [t for t in left if t.startswith("zz_s5")]
    if mine:
        print(f"LEFTOVER: committed zz_s5 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s5 table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
