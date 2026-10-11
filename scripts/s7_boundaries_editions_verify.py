"""Gates for the S7 (LAD May 2024 BGC boundaries, ONS ArcGIS, la_boundaries)
editions tables.

Mirrors scripts/s23_rsh_stock_editions_verify.py and
s5_imd_editions_verify.py. Prints `GATE n name: PASS|FAIL`; exits 1 on any
FAIL. It writes nothing, not even a run-log row. The real-table gates read
la_boundaries (the W1 spine), la_boundaries_editions and
la_boundaries_editions_file_checks. Until the editions table exists (ddl and
migrate-legacy not yet run) they print `FAIL (pending migration)` with the
words "not yet migrated", so the script exits non-zero until then, like the
other verifiers; nothing is created to make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s7_live / zz_s7_editions / zz_s7_editions_file_checks (a
copy of the loader's spec, with no foreign key), created inside the
transaction, and call the loader's own writer and commands
(s7_boundaries_editions.main with the SPEC pointed at the copy, the ArcGIS
replies and the downloads stubbed; apply_vintage; migrate_legacy;
restore_edition) rather than a re-implementation. The real tables are only
ever read; the foreign-key gate reads the catalogue and the referencing
tables.

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), shape (no foreign key: it holds other vintages' codes)
                                                           (throwaway + real)
  2  every live vintage has edition 1; the latest edition equals live cell
     for cell (key, source_date, lad24nm, longitude, latitude, shape_area,
     geojson)                                                         (real)
  3  live holds exactly one vintage (source_date 2024-05-01 only); a second
     vintage never reaches live through load, refresh-latest or
     --accept-drift; it is listed as held, not live
  4  identity from the source: the ArcGIS service list, the layer definition
     (name, exact fields, lastEditDate), the CRS, exceededTransferLimit,
     England only; seeded mismatches halt
  5  older-source guard on the API and --file paths; the rank is the layer's
     editingInfo.lastEditDate, never a service or file name
  6  stop conditions: any change to the live vintage's geometry or names is
     REJECTED without --acknowledge 2024-05-01; equal lastEditDate with other
     content; a changed key set is PARTIAL whatever the flags; an empty live
     table is never rebuilt; each stores nothing, no ledger row, partial
     run-log row
  7  ledger rule: the skip needs the (where, content sha256) pair for the
     vintage and the same lastEditDate; a failing ledger or edition insert
     rolls the load back
  8  preview and simulate write nothing
  9  a byte-identical re-read is `unchanged` on every path against edition 1
     'as loaded'; the held reply read now equals edition 1  (seeded + real)
 10  a reissue stores edition 2; refresh-latest changes only the live
     vintage, needs --acknowledge 2024-05-01, prints the map message and
     copies loaded_at
 11  edition 1 equals the decision note's before-state line     (seeded + real)
 12  the migration proof re-run read-only from the files on disk, geometry as
     canonical text and as jsonb                            (seeded + real)
 13  rerun idempotent
 14  restore-edition round trip
 15  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content
 16  no la_code_lookup write, no DELETE on la_boundaries, refresh_key_changes
     off (AST and spec); the real la_boundaries and la_code_lookup are
     unchanged by a full seeded run
 17  geojson in live equals the decision note's w1-read line (the map's
     geometry: data/boundaries/la_boundaries.geojson)       (seeded + real)
 18  every foreign key to la_boundaries still validates: the keys are counted
     from the database (pg_constraint, not assumed: 20 on 2026-10-11, after
     the S5 and S3b editions tables added theirs; 18 before) and
     the orphan rows across the referencing tables are 0
 19  the later vintages held on disk (attribute files, read only): codes
     added and removed, names unchanged; the attribute and geometry
     differences on the shared districts are a finding, not a failure

The decision note's hash lines are scan-safe: a 64-hex run would be flagged by
the credential scan, so each sha256 is written as two 32-hex halves in two
labelled fields:
    before-state la_boundaries 2024-05-01 rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read la_boundaries geometry rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
`python scripts/s7_boundaries_editions_verify.py --print-note-lines` prints
them for the live table as it stands (read-only; also a before-state-all line
over every stored column), for the decision note. Take them before the
migration.

Usage:
    python scripts/s7_boundaries_editions_verify.py
    python scripts/s7_boundaries_editions_verify.py --print-note-lines

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The cursor handed to the gates
refuses commit and rollback on its connection, and the commands get a
stand-in connection whose commit only counts. No network, no real-table
writes, no backend is ever terminated. After the rollback a second,
read-only connection checks that no zz% table was left behind.
"""
import ast
import contextlib
import hashlib
import io
import json
import os
import re
import socket
import sys
import tempfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest import mock

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
from _db import ENV, get_conn, get_readonly_conn  # noqa: E402
import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s7_boundaries_editions as m  # noqa: E402
import test_s7_boundaries_loader as tl  # noqa: E402
import test_s7_boundaries_pure as tp  # noqa: E402
from test_s7_boundaries_pure import (CODES, CODES_NEW, P24, P26,  # noqa: E402
                                     V24, V26, geometry, props, write_geojson,
                                     write_json, write_layer)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real spec and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPEC, which the seeded gates point
# at the copy)
REAL = m.SPEC
AREAS = m.EXPECTED_AREAS
ZZ = tl.ZZ
LEDGER = tl.LEDGER
N = tl.N
LOADER = HERE / "s7_boundaries_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-10-reference-trio-editions-first-load.md")
NOT_YET = ("the S7 editions table does not exist yet (not yet migrated)")
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (15)
TABLE = "la_boundaries"
LATER_MS = tl.LATER_MS
EARLIER_MS = tl.EARLIER_MS
LAST_26 = 1788960865457
NEW_CODES = ("E08000038", "E08000039")
OLD_CODES = ("E08000016", "E08000019")
MOVED = geometry(0, d=0.001)


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


specs = tl.specs


def plant(cur, sql, check, args=None):
    """check() after one statement, in a savepoint that is rolled back."""
    cur.execute("SAVEPOINT plant")
    try:
        cur.execute(sql, args)
        return check()
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT plant")
        cur.execute("RELEASE SAVEPOINT plant")


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    names = (ZZ.live_table, ZZ.editions_table, LEDGER)
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                      for n in names))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s7 table already exists as a real table; "
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

class Env(tl.Fixture):
    """The loader's main(argv) on the throwaway tables, the ArcGIS replies
    and the downloads stubbed; every file is written here (write_geojson,
    write_layer)."""

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api = {m.list_url(): tp.services_json()}
        self.urls, self.final, self.fetched = {}, {}, []
        self.n = 0
        self.tx = None

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, run-log
        mock)."""
        borrowed = tl._Borrowed(cur)
        self.tx = borrowed.tx

        def fetch_api(url, dest, session=None):
            self.fetched.append((url, Path(dest).name))
            return self.urls[url], self.final.get(url, url)
        out = io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda u, s=None: self.api[u]), \
                mock.patch.object(m, "fetch_api", side_effect=fetch_api), \
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

    def attribute_reissue(self, **kw):
        """The live vintage again, a later lastEditDate, one district's
        LAT changed (attributes only: no name or geometry)."""
        kw.setdefault("overrides", {("E06000002", "LAT"): 52.35})
        return self.release(last=LATER_MS, **kw)

    def moved_geometry(self, **kw):
        """A later lastEditDate with one district's geometry moved."""
        return self.release(last=LATER_MS, geoms={"E06000001": MOVED}, **kw)

    def later_vintage(self, register=True):
        return self.release(CODES_NEW, V26, last=LAST_26, register=register)


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
    """A seeded legacy state (the live rows as the old workflow left them,
    no editions yet) and the fixture files; fn(env, geojson, layer, files)."""
    def body(c):
        with env(c) as e, specs():
            gj, layer, files = e.seed_live(c, **kw)
            return fn(e, gj, layer, files)
    return _in_savepoint(cur, body)


@contextmanager
def surveyed(e, files):
    """The loader's surveyed live state pointed at the seeded legacy rows."""
    with mock.patch.object(m, "LEGACY_LIVE", e.legacy(e.cur)), \
            mock.patch.object(m, "LEGACY_LOADED", e.loaded(e.cur)), \
            mock.patch.object(m, "LEGACY_FILE", files):
        yield


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "varchar": "character varying",
          "text": "text", "date": "date", "numeric": "numeric",
          "jsonb": "jsonb"}


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
        bad.append(f"{spec.editions_table}: {fks} foreign key(s) (it holds "
                   "other vintages' codes, so none)")
    checks = " ".join(_constraint_defs(cur, spec.editions_table, "c"))
    if "lad24cd" not in checks:
        bad.append(f"{spec.editions_table}: the lad24cd CHECK is missing")
    bad += _triggers(cur, spec.editions_table, spec.trigger,
                     spec.truncate_trigger)
    for c in (m.KEY, m.PERIOD) + m.VALUES + ("loaded_at",):
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


EMPTY = "no live vintage to check (an empty state is not a pass)"


def _live_periods(cur, spec):
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [str(r[0]) for r in cur.fetchall()]


def _tip(cur, spec, p):
    return core.chain_tip(cur, spec, str(p))


def _both_ways(cur, a, aargs, b, bargs):
    counts_ = []
    for x, xa, y, ya in ((a, aargs, b, bargs), (b, bargs, a, aargs)):
        cur.execute(f"SELECT COUNT(*) FROM (({x}) EXCEPT ALL ({y})) q",
                    xa + ya)
        counts_.append(cur.fetchone()[0])
    return counts_


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
    cols = ", ".join((m.KEY, spec.period_col) + m.VALUES)
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
        "edition equals live on the key, source_date, lad24nm, longitude, "
        "latitude, shape_area and geojson, cell for cell")


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
            (f"UPDATE public.{live} SET lad24nm = 'x' WHERE lad24cd = "
             "'E06000001'", "edition-only"),
            (f"UPDATE public.{live} SET latitude = latitude + 1 WHERE "
             "lad24cd = 'E06000001'", "edition-only"),
            (f"UPDATE public.{live} SET geojson = %s::jsonb WHERE lad24cd = "
             "'E06000001'", "edition-only"),
            (f"DELETE FROM public.{live} WHERE lad24cd = 'E06000001'",
             "edition-only"),
            (f"UPDATE public.{live} SET source_date = '2026-05-01'",
             "no edition 1"),
        ):
            ok, detail = plant(
                e.cur, sql, lambda: real_edition1_and_latest(e.cur, ZZ),
                (json.dumps(MOVED),) if "%s" in sql else None)
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(2, name, ok, "seeded complete state passes; five planted drifts "
          "(a name, a latitude, a geometry, a missing row, a vintage "
          "without edition 1) are each caught" if ok else
          f"{out['base']} {out['miss']}", cur, real_edition1_and_latest)


# ---------------------------------------------------------------- gate 3

def real_single_vintage(cur, spec=REAL, n=AREAS):
    """Live holds source_date 2024-05-01 only (n rows); any other vintage is
    in the editions only and status lists it as held, not live (information,
    not a fault)."""
    _need(cur, spec)
    cur.execute(f"SELECT {spec.period_col}, COUNT(*) FROM "
                f"public.{spec.live_table} GROUP BY 1")
    live = {str(a): b for a, b in cur.fetchall()}
    bad = []
    if set(live) != {m.LIVE_PERIOD}:
        bad.append(f"live holds vintage(s) {sorted(live)}, expected only "
                   f"{m.LIVE_PERIOD}")
    elif live[m.LIVE_PERIOD] != n:
        bad.append(f"live {m.LIVE_PERIOD} has {live[m.LIVE_PERIOD]} rows, "
                   f"expected {n}")
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} ORDER BY 1")
    stored = [str(r[0]) for r in cur.fetchall()]
    held = [p for p in stored if p != m.LIVE_PERIOD]
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
    return True, (f"live holds {m.LIVE_PERIOD} only ({n} rows); held, not "
                  f"live: {held or 'none'}")


def gate_3_single_vintage(cur):
    name = ("live holds exactly one vintage (2024-05-01); a second vintage "
            "never reaches live; others are held, not live")
    out = {}
    problems = []

    def body(e):
        e.seed(e.cur)
        out["base"] = real_single_vintage(e.cur, ZZ, n=N)
        live = ZZ.live_table
        before = tl.live_md5(e.cur)
        e.later_vintage()
        rc, text, _ = e.cmd(["load", "--vintage", V26, "--commit"])
        if rc != 0 or f"{P26}: held-not-live" not in text \
                or tl.editions(e.cur, P26) != [(1, None, N)] \
                or tl.live_md5(e.cur) != before:
            problems.append(f"a second vintage did not stop at editions: "
                            f"rc {rc}: {text[-140:]}")
        out["held"] = real_single_vintage(e.cur, ZZ, n=N)
        if not out["held"][0] or P26 not in out["held"][1]:
            problems.append(f"held vintage not reported: {out['held']}")
        rc, text, _ = e.cmd(["refresh-latest", "--commit"])
        if rc != 0 or tl.live_md5(e.cur) != before:
            problems.append("refresh-latest moved live after a second "
                            "vintage")
        snap = state(e.cur)
        for argv in (["--accept-drift", P26], ["--acknowledge", P26],
                     ["--accept-drift", "2011-01-01"]):
            ok, _ = e.halts(["refresh-latest", "--commit"] + argv, snap)
            if not ok:
                problems.append(f"refresh-latest {' '.join(argv)} did not "
                                "halt")
        e.cmd(["load", "--vintage", V26, "--recheck", P26, "--commit"])
        if tl.live_md5(e.cur) != before:
            problems.append("a recheck of the second vintage touched live")
        miss = []
        ok, detail = plant(e.cur, f"UPDATE public.{live} SET source_date = "
                           "'2026-05-01' WHERE lad24cd = 'E06000001'",
                           lambda: real_single_vintage(e.cur, ZZ, n=N))
        if ok or "live holds vintage" not in detail:
            miss.append(f"two vintages in live: {detail[:80]}")
        ok, detail = plant(e.cur, f"DELETE FROM public.{live} WHERE "
                           "lad24cd = 'E06000001'",
                           lambda: real_single_vintage(e.cur, ZZ, n=N))
        if ok or "rows, expected" not in detail:
            miss.append(f"a missing row: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not problems and not out["miss"]
    mixed(3, name, ok, "seeded: May 2026 loaded after May 2024 is stored as "
          "editions only (live byte for byte as before), reported as held, "
          "not live, and left alone by load --recheck and refresh-latest; "
          "--accept-drift and --acknowledge of it halt; a second vintage in "
          "live and a missing row are each caught"
          if ok else f"{out} {problems[:3]}", cur, real_single_vintage)


# ---------------------------------------------------------- file gates (4, 5)

def held_files(files=None, raw_dir=None):
    """(geojson, layer JSON) on disk: the GeoJSON whose bytes sha256 is the
    held reply's, and a layer definition for May 2024 whose lastEditDate is
    the held one's (the layer is API output, so it is identified by its own
    content); either may be None."""
    files = m.LEGACY_FILE if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    gj = layer = None
    for p in sorted(raw.glob("*.geojson")):
        if m.content_sha256(p) == files["bytes_sha256"]:
            gj = p
            break
    for p in sorted(raw.glob("*layer*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"), parse_float=Decimal)
            ident = m.layer_identity(d, m.vintage_of_layer(d))
        except ValueError:
            continue
        if ident["vintage"] == m.LIVE_VINTAGE and \
                ident["last_edit_ms"] == m.LEGACY_RANK_MS:
            layer = p
            break
    return gj, layer


def _layer(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"),
                   parse_float=Decimal)
    return m.layer_identity(d, m.vintage_of_layer(d))


def real_identity(cur=None, files=None, raw_dir=None, spec=REAL, areas=None):
    """The held reply's identity read from the layer and the GeoJSON: the
    layer is LAD_MAY_2024_UK_BGC with exactly the declared fields and the
    surveyed lastEditDate; the GeoJSON is EPSG:4326, not truncated,
    England only, every district's attributes present, Polygon or
    MultiPolygon geometry; once the editions table exists, every vintage's
    tip names its layer's lastEditDate."""
    gj, layer = held_files(files, raw_dir)
    if gj is None or layer is None:
        return False, (f"no {'GeoJSON' if gj is None else 'layer JSON'} in "
                       f"{raw_dir or m.RAW_DIR} for the held reply")
    ident = _layer(layer)
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_geojson(gj, m.LIVE_VINTAGE)
    bad = []
    if ident["name"] != "LAD_MAY_2024_UK_BGC" or \
            ident["period"] != m.LIVE_PERIOD:
        bad.append(f"{layer.name}: {ident['name']} {ident['period']}")
    if len(f["rows"]) != (areas or AREAS):
        bad.append(f"{gj.name}: {len(f['rows'])} districts")
    tips = ""
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        info = m.tip_info(cur, spec)
        off = [v for v, i in info.items() if i["rank"] is None]
        if off:
            bad.append(f"{off[:3]}: the tip's source_file names no ArcGIS "
                       "lastEditDate")
        tips = f"; {len(info)} stored vintage(s), each tip names its file"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{layer.name}: {ident['name']} with the declared fields, "
        f"{m.rank_text(ident['rank'])}; {gj.name}: {f['features']} features, "
        f"{len(f['rows'])} English districts ({f['ignored']} other-nation "
        f"ignored), geometry types {dict(sorted(f['types'].items()))}{tips}")


def gate_4_identity(cur):
    name = ("identity from the source: service list, layer definition, "
            "CRS, truncation, England only; seeded mismatches halt")
    problems = []

    def body(e):
        e.seed(e.cur)
        before = state(e.cur)

        def stops(label, argv, needle, **kw):
            ok, text = e.halts(argv, before, needle)
            if not ok:
                problems.append(f"{label}: {text[-100:]}")
        # the service list: a renamed service, and two matches
        e.api[m.list_url()] = tp.services_json([
            ("Local_Authority_Districts_May_2024_Boundaries_UK_BGC_V2",
             "FeatureServer")])
        stops("a renamed service", ["load", "--commit"], "_UK_BGC_V2")
        e.api[m.list_url()] = tp.services_json([
            (tp.service_name(V24), "FeatureServer"),
            ("Local_Authority_Districts_MAY_2024_Boundaries_UK_BGC",
             "FeatureServer")])
        stops("two matching services", ["load", "--commit"], "2 FeatureServer")
        e.api[m.list_url()] = tp.services_json()
        # the layer: other fields, another vintage's layer for the file
        gj, _ = e.release(last=LATER_MS, register=False)
        bad_layer = e.root / "bad.json"
        write_json(bad_layer, tp.layer_json(drop=("LAT",)))
        stops("a layer without LAT", ["load", "--file", gj, "--layer",
                                      bad_layer, "--commit"], "LAT")
        l26 = write_layer(e.root / "l26.json", V26)
        stops("another vintage's layer", ["load", "--file", gj, "--layer",
                                          l26, "--vintage", V24, "--commit"],
              "LAD_MAY_2026_UK_BGC")
        stops("no --layer", ["load", "--file", gj, "--commit"], "--layer")
        no_edit = e.root / "noedit.json"
        write_json(no_edit, tp.layer_json(last=None))
        stops("no lastEditDate", ["load", "--file", gj, "--layer", no_edit,
                                  "--commit"], "lastEditDate")
        good_layer = write_layer(e.root / "good.json", last=LATER_MS)
        # the reply: truncated, another CRS, a district short, a missing
        # attribute, a duplicate code
        for label, kw, needle in (
            ("exceededTransferLimit", dict(exceeded=True),
             "exceededTransferLimit"),
            ("another CRS", dict(crs="EPSG:27700"), "EPSG:27700"),
            ("a district short", dict(codes=CODES[:-1]), f"{N - 1} English"),
            ("a missing attribute", dict(overrides={
                ("E06000002", "LONG"): m.MISSING}), "LONG"),
            ("a blank attribute", dict(overrides={
                ("E06000002", "LONG"): None}), "LONG"),
            ("a duplicate code", dict(codes=CODES[:4] + [CODES[0]]),
             "appears twice"),
        ):
            bad_gj, _ = e.release(last=LATER_MS, register=False, **kw)
            stops(label, ["load", "--file", bad_gj, "--layer", good_layer,
                          "--commit"], needle)
        # only England is stored: a Welsh feature is read and ignored
        extra = [{"type": "Feature", "geometry": geometry(9),
                  "properties": props("W06000001", 9)}]
        welsh, _ = e.release(last=LATER_MS, register=False, extra=extra)
        rc, text, _ = e.cmd(["load", "--file", welsh, "--layer", good_layer,
                             "--commit"])
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.editions_table} "
                      "WHERE lad24cd LIKE 'W%'")
        if rc != 0 or e.cur.fetchone()[0] or "1 other" not in text and \
                "ignored" not in text:
            problems.append(f"a Welsh feature was stored or not reported: "
                            f"rc {rc}")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a renamed or doubled service, a layer without a "
          "declared field or of another vintage, no --layer, no "
          "lastEditDate, a truncated reply, another CRS, a district short, "
          "a missing or blank attribute and a duplicate code each halt and "
          "store nothing; a Welsh feature is read and never stored"
          if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(4, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(4, name, False, _first(ex))
    report(4, name, ok, f"{sd}; held reply: {detail}")


def real_rank(cur=None, files=None, raw_dir=None, spec=REAL, areas=None):
    """The held reply's rank is its layer's editingInfo.lastEditDate (never
    a service or file name), and equals the rank edition 1 records."""
    gj, layer = held_files(files, raw_dir)
    if layer is None:
        return False, (f"no layer JSON in {raw_dir or m.RAW_DIR} for the "
                       "held reply")
    ident = _layer(layer)
    detail = f"{layer.name}: rank {m.rank_text(ident['rank'])}"
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = 1", (ident["period"],))
        rows = cur.fetchall()
        if len(rows) != 1 or m.release_rank(rows[0][0]) != ident["rank"]:
            return False, (f"{layer.name}: edition 1 records rank "
                           f"{m.release_rank(rows[0][0]) if rows else None},"
                           f" the layer {ident['rank']}")
        detail += "; edition 1 records the same rank"
    return True, detail


def gate_5_older_file(cur):
    name = ("older-source guard on the API and --file paths; the rank is "
            "the layer's lastEditDate, never a service or file name")
    problems = []

    def older(e, how):
        e.seed(e.cur)
        gj, layer = e.release(last=EARLIER_MS, register=how != "file")
        argv = ["load", "--commit"]
        if how == "file":
            named = gj.with_name("LAD_MAY_2099_UK_BGC_england_4326.geojson")
            gj.rename(named)
            argv += ["--file", named, "--layer", layer]
        return argv

    def body(e):
        for how in ("api", "file"):
            def one(c, how=how):
                with env(c) as e2, specs():
                    argv = older(e2, how)
                    before = state(c)
                    rc, text, logged = e2.cmd(argv)
                    if not (rc == "halt" and f"{P24} (older" in text
                            and state(c) == before and not logged.called):
                        problems.append(f"{how}: rc {rc}: {text[-160:]}")
            _in_savepoint(e.cur, one)

        def override(c):
            with env(c) as e2, specs():
                argv = older(e2, "file") + ["--allow-older-file"]
                rc, text, logged = e2.cmd(argv)
                if not (rc == 0 and f"{P24}: unchanged" in text
                        and tl.editions(c) == [(1, None, N)]
                        and logged.called and "--allow-older-file given"
                        in logged.call_args[0][2]):
                    problems.append(f"--allow-older-file: rc {rc}")
        _in_savepoint(e.cur, override)

        def name_is_not_evidence(c):
            for nm in ("LAD_MAY_2099_UK_BGC_england_4326.geojson",
                       "Local_Authority_Districts_May_2099_Boundaries_UK_BGC"
                       ):
                if m.release_rank(nm) is not None:
                    problems.append(f"a name gave a rank: {nm}")
            with env(c) as e2, specs():
                e2.seed(c)
                # the same content under a later lastEditDate is unchanged
                # and raises the tip's rank; the earlier layer is then older
                e2.release(last=LATER_MS)
                rc, text, _ = e2.cmd(["load", "--commit"])
                if rc != 0 or f"{P24}: unchanged" not in text:
                    problems.append(f"a later lastEditDate, same content: "
                                    f"rc {rc}: {text[-100:]}")
                e2.release()
                ok, _ = e2.halts(["load", "--commit"], state(c))
                if not ok:
                    problems.append("the earlier layer was accepted after a "
                                    "later one")
        _in_savepoint(e.cur, name_is_not_evidence)
    scenario(cur, body)
    seeded = not problems
    sd = ("a layer of an earlier lastEditDate (the reply named like May "
          "2099), offered by the API and --file against the held "
          "2024-07-02 layer, is skipped and halts with nothing stored or "
          "logged; --allow-older-file overrides and is logged; a name gives "
          "no rank; a later lastEditDate moves the tip's rank"
          if seeded else "; ".join(problems[:3]))
    mixed(5, name, seeded, sd, cur, real_rank)


# ---------------------------------------------------------------- gate 6-8

def rejects(e, argv, needle, before, *, commit=True, vintage=P24):
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
    name = ("stop conditions: a geometry or name change to the live vintage "
            "needs --acknowledge; each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row")
    problems = []

    def case(label, setup, needle, extra=(), commit=True):
        def body(c):
            with env(c) as e, specs():
                setup(e)
                before = state(c)
                argv = ["load"] + list(extra) + (["--commit"] if commit
                                                 else [])
                bad = rejects(e, argv, needle, before, commit=commit)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)

    def geometry_changed(e):
        e.seed(e.cur)
        e.moved_geometry()

    def name_changed(e):
        e.seed(e.cur)
        e.release(last=LATER_MS, overrides={
            ("E06000002", "LAD24NM"): "Middlesbrough Town"})

    def equal_rank(e):
        e.seed(e.cur)
        e.release(overrides={("E06000002", "LAT"): 52.35})

    def partial_keys(e):
        e.seed(e.cur)
        e.release(CODES[:4] + ["E09000001"], last=LATER_MS)
    case("a geometry change", geometry_changed,
         "geometry changed 1 (E06000001)")
    case("a geometry change, preview exits 1 and logs nothing",
         geometry_changed, f"--acknowledge {P24}", commit=False)
    case("a name change", name_changed, "Middlesbrough Town")
    case("equal lastEditDate, other content", equal_rank,
         "two sources claim the same release")
    case("a reissue with another key set, whatever the flags", partial_keys,
         m.PARTIAL, extra=["--acknowledge", P24, "--accept-reissue", P24])

    def acknowledged(c):
        with env(c) as e, specs():
            e.seed(c)
            before_live = tl.live_md5(c)
            e.moved_geometry()
            rc, text, logged = e.cmd(["load", "--commit", "--acknowledge",
                                      P24])
            if not (rc == 0 and "ACKNOWLEDGED" in text
                    and tl.editions(c) == [(1, None, N), (2, 1, N)]
                    and "--acknowledge" in logged.call_args[0][2]
                    and tl.live_md5(c) == before_live):
                problems.append(f"--acknowledge: rc {rc}: {text[-120:]}")
            for argv, needle in (
                    (["load", "--commit", "--acknowledge", "2011-01-01"],
                     "2011-01-01"),
                    (["load", "--vintage", V26, "--commit", "--acknowledge",
                      P26], "only the live vintage")):
                ok, _ = e.halts(argv, state(c), needle)
                if not ok:
                    problems.append(f"{' '.join(argv[:5])} did not halt")
    _in_savepoint(cur, acknowledged)

    def never_rebuilt(c):
        with env(c) as e, specs():
            e.release()
            ok, _ = e.halts(["load", "--commit"], state(c), "never rebuilds")
            if not ok:
                problems.append("an empty live table was not refused")
            with specs():
                cur.execute("SAVEPOINT hook")
                try:
                    m.refuse_live_insert(c, m.profile(), P24, [])
                    problems.append("the engine's live-insert hook inserted")
                except SystemExit:
                    pass
                cur.execute("ROLLBACK TO SAVEPOINT hook")
    _in_savepoint(cur, never_rebuilt)

    def attribute_only(c):
        with env(c) as e, specs():
            e.seed(c)
            e.attribute_reissue()
            rc, text, _ = e.cmd(["load", "--commit"])
            if rc != 0 or tl.editions(c) != [(1, None, N), (2, 1, N)]:
                problems.append(f"an attributes-only reissue was refused: "
                                f"rc {rc}: {text[-100:]}")
    _in_savepoint(cur, attribute_only)
    report(6, name, not problems, "a geometry change and a name change to "
           "the live vintage, equal lastEditDate with other content and a "
           "reissue changing the key set (whatever the flags) are each "
           "REJECTED (exit 1) with nothing stored, no ledger row and one "
           "partial run-log row (none in a preview); --acknowledge "
           f"{P24} stores edition 2 and leaves live alone; "
           "--acknowledge of another vintage halts; an empty live table is "
           "never rebuilt; an attributes-only later layer is stored"
           if not problems else "; ".join(problems[:3]))


def gate_7_ledger_rule(cur):
    name = ("ledger rule: the skip needs the (where, content sha256) pair "
            "for the vintage and the same lastEditDate; a failing insert "
            "rolls the load back")
    problems = []

    def pair(e):
        e.seed(e.cur)
        # a later vintage's (where, sha) recorded under 2024-05-01 only: the
        # ledger row declares 2026-05-01, so it is read, not skipped
        gj, layer = e.later_vintage()
        f = m.read_geojson(gj, V26)
        sha = m.rows_content_sha(m.build_records(f))
        where = m.query_url(tp.service_name(V26), "LAD26CD")
        rank = m.layer_identity(json.loads(layer.read_text(
            encoding="utf-8")), V26)["rank"]
        e.cur.execute(f"INSERT INTO public.{LEDGER} (source_date, "
                      "source_file, file_sha256, outcome, edition) VALUES "
                      "(%s, %s, %s, 'unchanged', 1)",
                      (P24, m.ledger_source(where, P26, rank), sha))
        rc, text, _ = e.cmd(["load", "--vintage", V26])
        if rc != 0 or "nothing compared" in text or \
                f"{P26}: held-not-live" not in text:
            problems.append(f"a pair held under another vintage skipped the "
                            f"reply: rc {rc}")
    scenario(cur, pair)

    def held(e):
        e.seed(e.cur)
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "nothing compared" not in text or logged.called:
            problems.append(f"a held pair did not skip: rc {rc}")
        # the same content under a later lastEditDate: another rank in the
        # ledger row, so it is compared (and found unchanged)
        e.release(last=LATER_MS)
        rc, text, _ = e.cmd(["load"])
        if "nothing compared" in text or f"{P24}: unchanged" not in text:
            problems.append("the same content under a later lastEditDate "
                            "was skipped")
        e.release()
        rc, text, _ = e.cmd(["load", "--recheck", P24])
        if f"{P24}: unchanged" not in text:
            problems.append("--recheck did not compare a ledger-held reply")
    scenario(cur, held)

    def failing(label, target, attr):
        def body(c):
            with env(c) as e, specs():
                e.seed(c)
                e.later_vintage()
                before = state(c)
                with mock.patch.object(target, attr,
                                       side_effect=RuntimeError("boom")):
                    rc, text, logged = e.cmd(["load", "--vintage", V26,
                                              "--commit"])
                if not (rc == 1 and state(c) == before
                        and f"{P26}: FAILED" in text and logged.called
                        and "PARTIAL RUN" in logged.call_args[0][2]):
                    problems.append(f"{label}: rc {rc}, state kept="
                                    f"{state(c) == before}")
        _in_savepoint(cur, body)
    failing("a failing ledger insert", pe, "record_file_check")
    failing("a failing edition insert", core, "insert_edition")
    report(7, name, not problems, "a held pair skips with nothing compared "
           "or logged; a pair recorded under another vintage does not skip; "
           "the same content under a later lastEditDate is compared; "
           "--recheck reads a held reply; a failing ledger or edition "
           "insert rolls back both and logs a partial run" if not problems
           else "; ".join(problems[:3]))


def gate_8_preview(cur):
    name = "preview and simulate write nothing"
    problems = []
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]

    def body(e):
        e.seed(e.cur)
        e.later_vintage()
        before = state(e.cur)
        for argv in (["load", "--vintage", V26],
                     ["load", "--vintage", V26, "--simulate"], ["status"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}")
        e.attribute_reissue()
        e.ok(e.cur, ["load", "--commit"])
        before = state(e.cur)
        for argv in (["refresh-latest"], ["restore-edition", P24, "1"],
                     ["refresh-latest", "--simulate", "--acknowledge", P24],
                     ["restore-edition", P24, "1", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv} after a reissue: rc {rc}")
    scenario(cur, body)

    def legacy(e, gj, layer, files):
        before = state(e.cur)
        with surveyed(e, files):
            for argv in (["migrate-legacy", "--file", gj, "--layer", layer],
                         ["migrate-legacy", "--file", gj, "--layer", layer,
                          "--simulate"], ["ddl"]):
                rc, text, logged = e.cmd(argv)
                if rc != 0 or state(e.cur) != before or logged.called:
                    problems.append(f"{argv[0]} {argv[-1]}: rc {rc}")
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
    """The held reply read again has the content sha256 of edition 1 'as
    loaded' (lad24nm, longitude, latitude, shape_area and the canonical
    geometry), so a re-read by the API or --file/--layer is unchanged, never
    a reissue."""
    _need(cur, spec)
    gj, layer = held_files(files, raw_dir)
    if gj is None:
        return False, f"no GeoJSON in {raw_dir or m.RAW_DIR} for the held " \
            "reply"
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_geojson(gj, m.LIVE_VINTAGE)
    built = m.build_records(f)
    held = m.records(cur, spec, m.LIVE_PERIOD, 1)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (m.LIVE_PERIOD,))
    label = cur.fetchall()
    bad = []
    if label != [(core.AS_LOADED_LABEL,)]:
        bad.append(f"{m.LIVE_PERIOD}: edition 1 is not the 'as loaded' one")
    a, b = m.rows_content_sha(held), m.rows_content_sha(built)
    if a != b:
        bad.append(f"{m.LIVE_PERIOD}: edition 1 content sha {a[:12]} differs"
                   f" from the held reply read now {b[:12]}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{gj.name} read now has edition 1's content sha256 ({a[:12]}.., "
        f"{len(held):,} rows): every re-read is unchanged")


def gate_9_byte_identical_reread(cur):
    name = ("a byte-identical re-read is unchanged on every path against "
            "edition 1 'as loaded'")
    problems = []
    out = {}

    def body(e, gj, layer, files):
        before_live = tl.live_md5(e.cur)
        plan, text = e.migrate(e.cur, gj, layer, files)
        if "0 differences" not in text:
            problems.append(f"the migration proof: {text[-120:]}")
            return
        d = Path(e.root) / "disk"
        d.mkdir()
        (d / gj.name).write_bytes(gj.read_bytes())
        (d / layer.name).write_bytes(layer.read_bytes())
        fx = {"bytes_sha256": m.content_sha256(gj)}
        out["real"] = real_held_reread(e.cur, ZZ, fx, d, areas=N)
        e.urls[m.layer_url(tp.service_name(V24))] = layer
        e.urls[files["url"]] = gj
        snap = tables_state(e.cur)
        rc, text, _ = e.cmd(["load"])
        if "nothing compared" not in text:
            problems.append("the held reply was compared again by the API")
        for argv in (["load", "--recheck", P24],
                     ["load", "--file", gj, "--layer", layer],
                     ["load", "--file", gj, "--layer", layer, "--commit"],
                     ["load", "--file", gj, "--layer", layer, "--recheck",
                      P24, "--commit"],
                     ["load", "--recheck", P24, "--commit"]):
            rc, text, _ = e.cmd(argv)
            if rc != 0 or f"{P24}: unchanged" not in text \
                    or "REJECTED" in text or "two sources claim" in text:
                problems.append(f"{' '.join(map(str, argv[:4]))}: rc {rc}: "
                                f"{text[-120:]}")
        if tables_state(e.cur) != snap or tl.editions(e.cur) != [
                (1, None, N)]:
            problems.append("a re-read stored an edition or moved live")
        if tl.live_md5(e.cur) != before_live:
            problems.append("live changed")
        if [o for _, _, o, _ in tl.ledger(e.cur)] != ["unchanged"] * 4:
            problems.append("the re-reads did not add 'unchanged' rows only")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text or "Scott's decision" in text:
            problems.append("refresh-latest planned a write after a re-read")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} l JOIN "
                      f"public.{ZZ.editions_table} e USING (lad24cd) WHERE "
                      "l.geojson::text = e.geojson::text")
        if e.cur.fetchone()[0] != N:
            problems.append("an edition's geometry is not the live jsonb")
        other = write_geojson(Path(e.root) / "disk2" / gj.name,
                              overrides={("E06000002", "LAT"): 52.35})
        ok, detail = real_held_reread(
            e.cur, ZZ, {"bytes_sha256": m.content_sha256(other)},
            other.parent, areas=N)
        if ok or "differs" not in detail:
            problems.append("a reply with other content was called equal")
    legacy_world(cur, body)
    seeded = not problems and out.get("real", (False,))[0]
    sd = ("seeded: after migrate-legacy the same reply read by the API, "
          "--file/--layer, --recheck and with --commit is unchanged against "
          "edition 1, stores no edition, adds 'unchanged' ledger rows only "
          "and leaves live and status alone; the held reply read now has "
          "edition 1's content sha" if seeded
          else f"{out} {problems[:3]}")
    mixed(9, name, seeded, sd, cur, real_held_reread)


# --------------------------------------------------------------- gate 10

def gate_10_reissue_and_refresh(cur):
    name = ("a reissue stores edition 2; refresh-latest needs "
            "--acknowledge, prints the map message, changes only the live "
            "vintage and copies loaded_at")
    problems = []

    def body(e):
        e.seed(e.cur)
        e.later_vintage()
        e.ok(e.cur, ["load", "--vintage", V26, "--commit"])
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        e.cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{ZZ.editions_table} t WHERE "
                      "source_date = %s", (P26,))
        held_before = e.cur.fetchone()[0]
        before = tl.live_md5(e.cur)
        e.moved_geometry()
        rc, text, _ = e.cmd(["load", "--commit", "--acknowledge", P24])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N), (2, 1, N)] or \
                tl.editions(e.cur, P26) != [(1, None, N)]:
            problems.append(f"the reissue did not store edition 2 of {P24} "
                            f"only: rc {rc}: {text[-120:]}")
        if tl.live_md5(e.cur) != before:
            problems.append("the reissue reached live before refresh-latest")
        if pe.status(e.cur, m.profile())["pending_refresh"] != [P24]:
            problems.append("status does not show the refresh pending")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called \
                or "this changes the map's geometry: Scott's decision" \
                not in text or "geometry changed 1 (E06000001)" not in text:
            problems.append("the refresh-latest preview wrote or did not "
                            "print the map message and the difference")
        ok, _ = e.halts(["refresh-latest", "--commit"], snap,
                        "Scott's decision")
        if not ok:
            problems.append("refresh-latest --commit without --acknowledge "
                            "did not halt")
        ok, _ = e.halts(["refresh-latest", "--commit", "--acknowledge",
                         P26], snap)
        if not ok:
            problems.append("refresh-latest --acknowledge of another "
                            "vintage did not halt")
        rc, text, logged = e.cmd(["refresh-latest", "--commit",
                                  "--acknowledge", P24])
        if rc != 0:
            problems.append(f"refresh-latest --commit --acknowledge: rc "
                            f"{rc}: {text[-120:]}")
        if json.loads(tl.live_val(e.cur, "E06000001", "geojson::text")) \
                != MOVED:
            problems.append("live does not hold the reissued geometry")
        e.cur.execute(f"SELECT lad24cd FROM public.{ZZ.live_table} WHERE "
                      "loaded_at <> '2026-01-01'")
        moved = [r[0] for r in e.cur.fetchall()]
        if moved != ["E06000001"]:
            problems.append(f"loaded_at moved on {moved}, not only on the "
                            "changed row")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} l JOIN "
                      f"public.{ZZ.editions_table} d USING (lad24cd) WHERE "
                      "d.edition = 2 AND l.loaded_at = d.loaded_at AND "
                      "l.lad24cd = 'E06000001'")
        if e.cur.fetchone()[0] != 1:
            problems.append("loaded_at was not copied from edition 2")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} l JOIN "
                      f"public.{ZZ.editions_table} d ON d.lad24cd = "
                      "l.lad24cd AND d.source_date = l.source_date AND "
                      "d.edition = 1 WHERE l.geojson::text IS DISTINCT FROM "
                      "d.geojson::text")
        if e.cur.fetchone()[0] != 1:
            problems.append("refresh-latest changed more than the moved "
                            "district")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table}")
        if e.cur.fetchone()[0] != N:
            problems.append("a live row was added or removed")
        e.cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{ZZ.editions_table} t WHERE "
                      "source_date = %s", (P26,))
        if e.cur.fetchone()[0] != held_before:
            problems.append("the held vintage's editions changed")
        e.cur.execute(f"SELECT DISTINCT source_date::text FROM public."
                      f"{ZZ.live_table}")
        if e.cur.fetchall() != [(P24,)]:
            problems.append("live holds a vintage other than 2024-05-01")
        if not pe.status(e.cur, m.profile())["ok"] or core.rows_differing(
                e.cur, ZZ, P24, 2):
            problems.append("live does not equal the tip after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("a second refresh-latest plans a write")
    scenario(cur, body)
    report(10, name, not problems, "a reissue with a moved geometry "
           "(--acknowledge) waits in edition 2 for refresh-latest; the "
           "preview prints the map message and the difference and writes "
           "nothing; the commit needs --acknowledge 2024-05-01, changes "
           "exactly the moved district, copies the edition's loaded_at, "
           "adds and removes no row and leaves the held May 2026 editions "
           "alone; a second refresh plans nothing"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 11

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def note_hashes(note):
    """{vintage: (rows, sha256)} from lines `before-state la_boundaries
    <source_date> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>`
    of the decision note (the two halves joined), or {}. A `before-state-all
    ...` line is a different record and does not match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                rf"(?m)^before-state {TABLE} ([0-9]{{4}}-[0-9]{{2}}-"
                r"[0-9]{2}) rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text)}


def w1_note_hash(note):
    """(rows, sha256) of the note's `w1-read la_boundaries geometry
    rows=<n> sha256-first32=.. sha256-last32=..` line, or None."""
    p = Path(note)
    if not p.exists():
        return None
    mt = re.search(
        rf"(?m)^w1-read {TABLE} geometry rows=([0-9]+) "
        r"sha256-first32=([0-9a-f]{32}) sha256-last32=([0-9a-f]{32})\s*$",
        p.read_text(encoding="utf-8"))
    return (int(mt.group(1)), mt.group(2) + mt.group(3)) if mt else None


def w1_read_sha(rows):
    """sha256 of what the map reads of the boundaries: one line per row
    sorted by lad24cd, lad24cd|the canonical geometry text, LF-joined,
    UTF-8."""
    lines = sorted(f"{r[m.KEY]}|{r['geojson']}" for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def live_all_sha(cur, spec=REAL):
    """sha256 of every stored column of the live table: lad24cd|lad24nm|
    longitude|latitude|shape_area|canonical geometry|source_date|loaded_at
    (UTC, NULL as ''), sorted by lad24cd, LF-joined."""
    cur.execute(f"SELECT lad24cd, lad24nm, longitude, latitude, shape_area, "
                f"geojson::text, source_date, loaded_at AT TIME ZONE 'UTC' "
                f"FROM public.{spec.live_table}")
    lines = []
    for r in cur.fetchall():
        cells = list(r)
        cells[5] = m.canonical_text(cells[5])
        lines.append("|".join("" if v is None else str(v) for v in cells))
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")
                          ).hexdigest()


def note_lines_for(cur, spec=REAL):
    """The decision note's hash lines for the live table as it stands
    (read-only): before-state (rows_content_sha, the loader's), the
    before-state-all line over every stored column and the w1-read line of
    the geometry. Print them before the migration."""
    recs = m.records(cur, spec, m.LIVE_PERIOD)
    n = len(recs)
    return [
        f"before-state {TABLE} {m.LIVE_PERIOD} rows={n} "
        f"{hash_fields(m.rows_content_sha(recs))}",
        f"before-state-all {TABLE} {m.LIVE_PERIOD} rows={n} "
        f"{hash_fields(live_all_sha(cur, spec))}",
        f"w1-read {TABLE} geometry rows={n} {hash_fields(w1_read_sha(recs))}"]


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

    def body(e, gj, layer, files):
        legacy, loaded = e.legacy(e.cur), e.loaded(e.cur)
        lines = note_lines_for(e.cur, ZZ)
        e.migrate(e.cur, gj, layer, files, legacy=legacy, loaded=loaded)
        recs = m.records(e.cur, ZZ, P24, 1)
        h = m.rows_content_sha(recs)
        out["same"] = h == m.rows_content_sha(m.records(e.cur, ZZ, P24))
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
            note.write_text(lines[1] + "\n" + lines[2] + "\n",
                            encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}"),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, N)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                N)
            note.write_text(line, encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(e.cur, ZZ, note, N + 5)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET lad24nm = 'x' "
                      "WHERE lad24cd = 'E06000001'")
        out["changed"] = m.live_state(e.cur, ZZ)[:2] != legacy
        with mock.patch.object(m, "LEGACY_LIVE", legacy), \
                mock.patch.object(m, "LEGACY_LOADED", loaded), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = e.cmd(["migrate-legacy", "--file", gj, "--layer",
                                 layer, "--commit"])
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
          "a wrong hash, a wrong row count, a missing line (the "
          "before-state-all and w1-read lines do not count) and a missing "
          "note fail; a later change shows; a second migration is refused"
          if ok else f"seeded: {out}", cur, real_edition1_hash)


# --------------------------------------------------------------- gate 12

def real_migration_proof(cur, spec=REAL, files=None, raw_dir=None,
                         areas=None):
    """The migration proof re-run read-only from the files on disk (the
    GeoJSON found by its bytes sha256, the layer by its lastEditDate): the
    parser on the held reply reproduces every value of edition 1 'as
    loaded' (296 x 5, the geometry as canonical text; 0 differences), and
    every edition 1 geometry equals the rebuilt one as jsonb."""
    _need(cur, spec)
    gj, layer = held_files(files, raw_dir)
    if gj is None or layer is None:
        return False, (f"no {'GeoJSON' if gj is None else 'layer JSON'} in "
                       f"{raw_dir or m.RAW_DIR} for the held reply")
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_geojson(gj, m.LIVE_VINTAGE)
    built = m.build_records(f)
    held = m.records(cur, spec, m.LIVE_PERIOD, 1)
    if not held:
        return False, f"{m.LIVE_PERIOD}: no edition 1"
    cells, diffs = m._proof(held, built)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (m.LIVE_PERIOD,))
    if cur.fetchall() != [(core.AS_LOADED_LABEL,)]:
        return False, f"{m.LIVE_PERIOD}: edition 1 is not the 'as loaded' one"
    from psycopg2.extras import execute_values
    got = execute_values(
        cur, f"SELECT COUNT(*) FROM public.{spec.editions_table} e JOIN "
        "(VALUES %s) v(k, g) ON e.lad24cd = v.k WHERE e.edition = 1 AND "
        f"e.{spec.period_col} = '{m.LIVE_PERIOD}' AND e.geojson = "
        "v.g::jsonb", [(r[m.KEY], r["geojson"]) for r in built],
        fetch=True, page_size=max(1, len(built)))
    same = sum(r[0] for r in got)
    if same != len(built):
        diffs.append(f"{len(built) - same} geometries differ as jsonb")
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{cells:,} cells of edition 1 ({len(held):,} rows) "
                       f"equal {gj.name} read now (0 differences); {same} of "
                       f"{len(built)} geometries identical (canonical text, "
                       "and equal as jsonb)")


def gate_12_migration_proof(cur):
    name = ("the migration proof re-run read-only from the files on disk; "
            "seeded: proof passes, a planted held difference stops "
            "migrate-legacy and stores nothing")
    problems = []

    def ok_world(e, gj, layer, files):
        with surveyed(e, files):
            rc, text, _ = e.cmd(["migrate-legacy", "--file", gj, "--layer",
                                 layer, "--commit"])
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        raw = Path(e.root) / "raw"
        raw.mkdir()
        (raw / gj.name).write_bytes(gj.read_bytes())
        (raw / layer.name).write_bytes(layer.read_bytes())
        fx = {"bytes_sha256": m.content_sha256(gj)}
        ok, detail = real_migration_proof(e.cur, ZZ, fx, raw, areas=N)
        if not ok or "0 differences" not in detail:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(
            e.cur, ZZ, {"bytes_sha256": "0" * 64}, raw, areas=N)
        if ok or "no GeoJSON" not in detail:
            problems.append("a file with another sha256 was accepted")
        (raw / gj.name).write_bytes(gj.read_bytes() + b"\n")
        ok, detail = real_migration_proof(e.cur, ZZ, fx, raw, areas=N)
        if ok or "no GeoJSON" not in detail:
            problems.append("a changed file on disk was accepted")
        other = write_geojson(Path(e.root) / "raw2" / gj.name,
                              geoms={"E06000001": MOVED})
        (Path(e.root) / "raw2" / layer.name).write_bytes(layer.read_bytes())
        ok, detail = real_migration_proof(
            e.cur, ZZ, {"bytes_sha256": m.content_sha256(other)},
            other.parent, areas=N)
        if ok or "differences" not in detail:
            problems.append("a reply that differs from edition 1 passed")

    def planted(sql, needle, args=None, surveyed_first=False):
        def f(e, gj, layer, files):
            sv = (e.legacy(e.cur), e.loaded(e.cur)) if surveyed_first \
                else None
            e.cur.execute(sql, args)
            sv = sv or (e.legacy(e.cur), e.loaded(e.cur))
            before = state(e.cur)
            with mock.patch.object(m, "LEGACY_LIVE", sv[0]), \
                    mock.patch.object(m, "LEGACY_LOADED", sv[1]), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = e.cmd(["migrate-legacy", "--file", gj,
                                          "--layer", layer, "--commit"])
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    live = ZZ.live_table
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET geojson = %s::jsonb WHERE lad24cd = "
        "'E06000001'", "proof failed", (json.dumps(MOVED),)))
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET lad24nm = 'Other' WHERE lad24cd = "
        "'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET shape_area = shape_area + 1 WHERE "
        "lad24cd = 'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"DELETE FROM public.{live} WHERE lad24cd = 'E06000002'",
        "not as surveyed", surveyed_first=True))
    mixed(12, name, not problems,
          "seeded migration proof passes (0 differences, geometry as "
          "canonical text and as jsonb) and re-runs from the files by "
          "sha256 and lastEditDate; a changed or missing file and a reply "
          "that is not edition 1 are caught; planted differences in a "
          "geometry, a name and an area, and a dropped row, stop "
          "migrate-legacy with nothing stored" if not problems
          else "; ".join(problems[:3]), cur, real_migration_proof)


# ------------------------------------------------------ gates 13 and 14

def gate_13_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        e.seed(e.cur)
        gj, layer = e.release()
        snap, led = tables_state(e.cur), len(tl.ledger(e.cur))
        for argv in (["load", "--commit"],
                     ["load", "--file", gj, "--layer", layer, "--commit"],
                     ["refresh-latest", "--commit", "--acknowledge", P24]):
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
        rc, text, logged = e.cmd(["load", "--recheck", P24, "--commit"])
        eds = tl.editions(e.cur)
        if rc != 0 or eds != [(1, None, N)]:
            problems.append(f"a recheck stored an edition: {eds}")
        e.later_vintage()
        e.ok(e.cur, ["load", "--vintage", V26, "--commit"])
        snap = tables_state(e.cur)
        e.ok(e.cur, ["load", "--vintage", V26, "--commit"])
        if tables_state(e.cur) != snap:
            problems.append("a second load of the held vintage changed "
                            "state")
    scenario(cur, body)
    report(13, name, not problems, "a second load by the API and by "
           "--file/--layer and a refresh-latest stored no edition and "
           "changed no live row (re-reads add 'unchanged' ledger rows "
           "only); a ledger-skipped load logs nothing; a held vintage "
           "loaded twice stores once" if not problems
           else "; ".join(problems[:3]))


def gate_14_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur)
        e.attribute_reissue()
        e.ok(e.cur, ["load", "--commit"])
        e.ok(e.cur, ["refresh-latest", "--commit", "--acknowledge", P24])
        snap = state(e.cur)
        rc, text, logged = e.cmd(["restore-edition", P24, "1"])
        if rc != 0 or state(e.cur) != snap or logged.called \
                or "--acknowledge" not in text:
            problems.append("restore-edition preview wrote or did not name "
                            "--acknowledge")
        rc, text, logged = e.cmd(["restore-edition", P24, "1", "--commit"])
        if rc != 0 or tl.editions(e.cur)[-1] != (3, 2, N) or \
                not logged.called:
            problems.append(f"restore --commit: rc {rc}: {text[-120:]}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE edition = 3")
        if e.cur.fetchall() != [("restored from edition 1",)]:
            problems.append("the restored edition is not labelled")
        if str(tl.live_val(e.cur, "E06000002", "latitude")) != "52.350000":
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit", "--acknowledge", P24])
        if str(tl.live_val(e.cur, "E06000002", "latitude")) != "52.100000" \
                or core.rows_differing(e.cur, ZZ, P24, 3):
            problems.append("refresh-latest did not apply the restored "
                            "edition")
        ed1 = {tuple(sorted(r.items())) for r in m.records(e.cur, ZZ, P24, 1)}
        ed3 = {tuple(sorted(r.items())) for r in m.records(e.cur, ZZ, P24, 3)}
        if ed1 != ed3:
            problems.append("edition 3 does not hold edition 1's rows")
        for argv in (["restore-edition", P24, "3", "--commit"],
                     ["restore-edition", P24, "9", "--commit"]):
            ok, _ = e.halts(argv, state(e.cur))
            if not ok:
                problems.append(f"{' '.join(argv[:3])} did not halt")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
    scenario(cur, body)
    report(14, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') and applied by refresh-latest "
           "--acknowledge; the tip and a missing edition are refused; a "
           "preview writes nothing" if not problems
           else "; ".join(problems[:3]))


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
            "outside the rolled-back transaction")
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
            e.seed(c)
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.later_vintage()
                rc, _, _ = e.cmd(["load", "--vintage", V26, "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load", "--vintage", V26])
            return rc, rc_s, rc_r, rc_l
    try:
        rc, rc_s, rc_r, rc_l = _in_savepoint(cur, body)
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
    ok = (not attempts and rc == rc_s == rc_r == rc_l == 0
          and runs == runs_after and real_before == real_after
          and not literal and not in_src and not in_out)
    report(15, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned "
           f"{rc}/{rc_s}/{rc_r}/{rc_l}; run-log rows "
           f"{runs}->{runs_after}; real editions table and ledger "
           f"unchanged={real_before == real_after}; secret-like literal in "
           f"source {literal or 'none'}; {len(secrets)} secret-named "
           f"settings checked against {len(files)} files and "
           f"{len(OUTPUT)} output lines: in source {in_src or 'none'}, in "
           f"output {in_out or 'none'}")


# --------------------------------------------------------------- gate 16

def module_problems(path, spec=REAL):
    """Problems in a module's source: a string constant (an f-string's parts
    included) that writes la_code_lookup (INSERT, UPDATE, DELETE, TRUNCATE,
    ALTER or DROP), a DELETE FROM, TRUNCATE or DROP TABLE anywhere (la_
    boundaries above all), or an INSERT or UPDATE of la_boundaries or of
    the spec's live table; and (the spec) refresh_key_changes on or
    live_single_period off."""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None and \
                    node.body and isinstance(node.body[0], ast.Expr):
                doc.add(id(node.body[0].value))
    write = r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?|" \
            r"ALTER\s+TABLE|DROP\s+TABLE)"
    bad = []

    def check(line, s):
        if re.search(rf"\b{write}\s+(ONLY\s+)?(public\.)?\{{?\w*\.?"
                     r"(la_code_lookup)\b", s, re.I):
            bad.append(f"line {line}: a write to la_code_lookup")
        if re.search(r"\b(DELETE\s+FROM|TRUNCATE\b|DROP\s+TABLE)", s, re.I):
            bad.append(f"line {line}: a DELETE, TRUNCATE or DROP TABLE "
                       f"({s.strip()[:40]!r})")
        if re.search(r"\b(INSERT\s+INTO|UPDATE)\s+(public\.)?"
                     r"(la_boundaries\b|\{[\w.]*live_table\})", s, re.I):
            bad.append(f"line {line}: an INSERT or UPDATE of the live table")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc:
            check(node.lineno, node.value)
        if isinstance(node, ast.JoinedStr):
            parts = [x.value if isinstance(x, ast.Constant) else
                     "{" + ast.unparse(x.value) + "}" for x in node.values]
            check(node.lineno, "".join(str(p) for p in parts))
    if spec.refresh_key_changes:
        bad.append("the spec has refresh_key_changes on")
    if not spec.live_single_period:
        bad.append("the spec has live_single_period off")
    return list(dict.fromkeys(bad))


def _table_md5(cur, table):
    cur.execute(f"SELECT COUNT(*), md5(string_agg(t::text, E'\\n' ORDER BY "
                f"t::text)) FROM public.{table} t")
    return cur.fetchone()


def gate_16_no_lookup_write_no_delete(cur):
    name = ("no la_code_lookup write, no DELETE on la_boundaries, "
            "refresh_key_changes off (AST and spec); the real spine and "
            "lookup unchanged by a full run")
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text(
            '"""DELETE FROM la_boundaries in a docstring"""\n'
            'A = "INSERT INTO la_code_lookup VALUES (1)"\n'
            'B = "DELETE FROM la_boundaries WHERE false"\n'
            'C = "TRUNCATE public.la_code_lookup"\n'
            'D = f"UPDATE public.{spec.live_table} SET x = 1"\n'
            'E = "UPDATE la_boundaries SET x = 1"\n'
            'F = "SELECT old_code FROM la_code_lookup"\n'
            'G = f"SELECT * FROM public.{spec.live_table}"\n',
            encoding="utf-8")
        planted = module_problems(p)
        flipped = module_problems(p, mock.Mock(refresh_key_changes=True,
                                               live_single_period=False))
    caught = (any("line 2" in b for b in planted)
              and any("line 3" in b for b in planted)
              and any("line 4" in b for b in planted)
              and any("line 5" in b for b in planted)
              and any("line 6" in b for b in planted)
              and not any("line 1:" in b or "line 7" in b or "line 8" in b
                          for b in planted)
              and any("refresh_key_changes" in b for b in flipped)
              and any("live_single_period" in b for b in flipped))
    if not caught:
        problems.append(f"the source check is wrong: {planted} {flipped}")
    src = module_problems(LOADER)
    if src:
        problems.append(f"the loader: {src[:3]}")

    def body(e):
        b = _table_md5(e.cur, "la_boundaries")
        lk = _table_md5(e.cur, "la_code_lookup")
        e.seed(e.cur)
        e.later_vintage()
        e.ok(e.cur, ["load", "--vintage", V26, "--commit"])
        e.moved_geometry()
        e.ok(e.cur, ["load", "--commit", "--acknowledge", P24])
        e.ok(e.cur, ["refresh-latest", "--commit", "--acknowledge", P24])
        e.ok(e.cur, ["restore-edition", P24, "1", "--commit"])
        e.ok(e.cur, ["refresh-latest", "--commit", "--acknowledge", P24])
        if _table_md5(e.cur, "la_boundaries") != b:
            problems.append("the real la_boundaries changed")
        if _table_md5(e.cur, "la_code_lookup") != lk:
            problems.append("the real la_code_lookup changed")
    scenario(cur, body)
    report(16, name, not problems, "no write to la_code_lookup, no "
           "DELETE, TRUNCATE or DROP TABLE and no INSERT or UPDATE of "
           "la_boundaries in the loader's strings (planted ones are "
           "caught, a docstring and a SELECT are not); refresh_key_changes "
           "off and live_single_period on in the spec; the real "
           "la_boundaries and la_code_lookup are byte for byte unchanged "
           "by a seeded load, acknowledged reissue, refresh and restore"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 17

def real_w1_geometry(cur, spec=REAL, note=NOTE, rows=None):
    """The live rows of the live vintage: the count and w1_read_sha of
    (lad24cd, canonical geojson) equal the decision note's w1-read line taken
    before any change, so the map's geometry (the exported GeoJSON) is what
    it was."""
    _need(cur, spec)
    rows = AREAS if rows is None else rows
    if not Path(note).exists():
        return False, f"decision note {Path(note).name} not written"
    recorded = w1_note_hash(note)
    if recorded is None:
        return False, (f"the decision note records no 'w1-read {TABLE} "
                       "geometry rows=.. sha256-first32=.. "
                       "sha256-last32=..' line")
    recs = m.records(cur, spec, m.LIVE_PERIOD)
    n, h = len(recs), w1_read_sha(recs)
    bad = []
    if n != rows:
        bad.append(f"live {m.LIVE_PERIOD} has {n} rows, expected {rows}")
    if recorded != (n, h):
        bad.append(f"live {m.LIVE_PERIOD} geometry: {n} rows sha256 "
                   f"{h[:16]}.. differs from the note's {recorded[0]} rows "
                   f"{recorded[1][:16]}..")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live {m.LIVE_PERIOD} geometry ({n} rows) hashes to the note's "
        f"w1-read line ({h[:16]}..)")


def gate_17_w1_geometry(cur):
    name = ("geojson in live equals the decision note's w1-read line (the "
            "map's geometry)")
    out = {}

    def body(e):
        e.seed(e.cur)
        before = w1_read_sha(m.records(e.cur, ZZ, P24))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = (f"w1-read {TABLE} geometry rows={N} "
                    f"{hash_fields(before)}")
            note.write_text("intro\n" + line + "\n", encoding="utf-8")
            kw = dict(note=note, rows=N)
            out["base"] = real_w1_geometry(e.cur, ZZ, **kw)
            # an attributes-only reissue (LAT) refreshed into live: the
            # geometry the map reads does not move
            e.attribute_reissue()
            e.ok(e.cur, ["load", "--commit"])
            e.ok(e.cur, ["refresh-latest", "--commit", "--acknowledge",
                         P24])
            out["after_lat"] = real_w1_geometry(e.cur, ZZ, **kw)
            out["lat_moved"] = str(tl.live_val(
                e.cur, "E06000002", "latitude")) == "52.350000"
            # a moved geometry moves it
            e.release(last=1900000000000, geoms={"E06000001": MOVED},
                      overrides={("E06000002", "LAT"): 52.35})
            e.ok(e.cur, ["load", "--commit", "--acknowledge", P24])
            e.ok(e.cur, ["refresh-latest", "--commit", "--acknowledge",
                         P24])
            out["after_geom"] = real_w1_geometry(e.cur, ZZ, **kw)
            note.write_text("intro\n", encoding="utf-8")
            out["no_line"] = real_w1_geometry(e.cur, ZZ, **kw)
            note.write_text(line.replace("w1-read", "w1-read-all") + "\n",
                            encoding="utf-8")
            out["other_label"] = real_w1_geometry(e.cur, ZZ, **kw)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}")
                            + "\n", encoding="utf-8")
            out["bad_rows"] = real_w1_geometry(e.cur, ZZ, **kw)
            out["no_note"] = real_w1_geometry(e.cur, ZZ,
                                              note=Path(tmp) / "x", rows=N)
            out["scan_safe"] = not re.search(r"[0-9a-f]{64}", line)
    scenario(cur, body)
    ok = (out["base"][0] and out["after_lat"][0] and out["lat_moved"]
          and not out["after_geom"][0]
          and "differs" in out["after_geom"][1]
          and not out["no_line"][0] and not out["other_label"][0]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and out["scan_safe"])
    mixed(17, name, ok, "seeded: the read passes on the live rows the note "
          "was taken from, stays equal after an attributes-only reissue "
          "and refresh (the map reads the geometry), changes with a moved "
          "geometry, and a missing or other-labelled line, a wrong row "
          "count and a missing note fail" if ok else f"{out}", cur,
          real_w1_geometry)


# --------------------------------------------------------------- gate 18

def fk_report(cur, parent=TABLE):
    """(foreign keys referencing `parent` counted from pg_constraint, the
    ones not validated, the orphan rows across the referencing tables,
    [(referencing table, constraint, orphans)])."""
    cur.execute("""
        SELECT c.conname, c.conrelid::regclass::text, c.convalidated,
               ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY
                     u(k, o) JOIN pg_attribute a ON a.attrelid = c.conrelid
                     AND a.attnum = u.k ORDER BY u.o),
               ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY
                     u(k, o) JOIN pg_attribute a ON a.attrelid = c.confrelid
                     AND a.attnum = u.k ORDER BY u.o)
        FROM pg_constraint c
        WHERE c.contype = 'f' AND c.confrelid = %s::regclass
        ORDER BY 2, 1""", (f"public.{parent}",))
    keys = cur.fetchall()
    unvalidated, per = [], []
    for name, child, valid, ccols, pcols in keys:
        if not valid:
            unvalidated.append(f"{child}.{name}")
        nonnull = " AND ".join(f"r.{c} IS NOT NULL" for c in ccols)
        match = " AND ".join(f"p.{p} = r.{c}" for c, p in zip(ccols, pcols))
        cur.execute(f"SELECT COUNT(*) FROM {child} r WHERE {nonnull} AND "
                    f"NOT EXISTS (SELECT 1 FROM public.{parent} p WHERE "
                    f"{match})")
        per.append((child, name, cur.fetchone()[0]))
    return len(keys), unvalidated, sum(n for _, _, n in per), per


def real_foreign_keys(cur, parent=TABLE):
    """Every foreign key referencing la_boundaries (counted from the
    database) is validated and has no orphan row."""
    n, unvalidated, orphans, per = fk_report(cur, parent)
    if n == 0:
        return False, f"no foreign key references {parent}"
    bad = []
    if unvalidated:
        bad.append(f"{len(unvalidated)} not validated: {unvalidated[:3]}")
    if orphans:
        bad.append(f"{orphans} orphan row(s): "
                   f"{[p for p in per if p[2]][:3]}")
    return not bad, "; ".join(bad) if bad else (
        f"{n} foreign keys reference {parent} (counted from pg_constraint, "
        f"over {len({c for c, _, _ in per})} tables), all validated; 0 "
        "orphan rows across the referencing tables")


def gate_18_foreign_keys(cur):
    name = ("every foreign key to la_boundaries (counted from the database) "
            "still validates; 0 orphan rows")
    problems = []
    t = ZZ.live_table
    cur.execute("SAVEPOINT fk")
    try:
        cur.execute(f"CREATE TABLE public.zz_s7_fk_ok (lad24cd varchar(9) "
                    f"REFERENCES public.{t}(lad24cd))")
        cur.execute(f"CREATE TABLE public.zz_s7_fk_bad (lad24cd "
                    "varchar(9))")
        cur.execute(f"INSERT INTO public.{t} (lad24cd, lad24nm, longitude, "
                    "latitude, shape_area, geojson, source_date) VALUES "
                    "('E06000001', 'x', 0, 0, 0, '{}', '2024-05-01')")
        cur.execute("INSERT INTO public.zz_s7_fk_ok VALUES ('E06000001')")
        cur.execute("INSERT INTO public.zz_s7_fk_bad VALUES ('E06000001'), "
                    "('E06000999'), (NULL)")
        ok, detail = real_foreign_keys(cur, t)
        if not ok or "1 foreign keys" not in detail:
            problems.append(f"one clean key: {detail}")
        cur.execute(f"ALTER TABLE public.zz_s7_fk_bad ADD CONSTRAINT "
                    f"zz_s7_fk_bad_fk FOREIGN KEY (lad24cd) REFERENCES "
                    f"public.{t}(lad24cd) NOT VALID")
        n, unvalidated, orphans, per = fk_report(cur, t)
        ok, detail = real_foreign_keys(cur, t)
        if ok or n != 2 or orphans != 1 or len(unvalidated) != 1 \
                or "not validated" not in detail or "orphan" not in detail:
            problems.append(f"a not-valid key with an orphan: n {n}, "
                            f"orphans {orphans}, {detail}")
        cur.execute(f"ALTER TABLE public.zz_s7_fk_bad DROP CONSTRAINT "
                    "zz_s7_fk_bad_fk")
        ok, detail = real_foreign_keys(cur, t)
        if not ok or "1 foreign keys" not in detail:
            problems.append(f"after dropping the bad key: {detail}")
        cur.execute("DROP TABLE public.zz_s7_fk_ok")
        ok, detail = real_foreign_keys(cur, t)
        if ok or "no foreign key" not in detail:
            problems.append("a table with no foreign key passed")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT fk")
        cur.execute("RELEASE SAVEPOINT fk")
    seeded = not problems
    if not seeded:
        return report(18, name, False, "; ".join(problems[:3]))
    try:
        ok, detail = real_foreign_keys(cur)
    except (psycopg2.Error, ValueError) as ex:
        return report(18, name, False, _first(ex))
    report(18, name, ok, f"seeded: a clean key passes, a key added NOT "
           f"VALID with an orphan row fails on both counts, a table with no "
           f"key fails; real: {detail}")


# --------------------------------------------------------------- gate 19

def _held_attributes(raw, stem):
    """The newest held attributes-only file for a vintage (by file name),
    or None."""
    hits = sorted(Path(raw).glob(f"*{stem}*attributes*.json"))
    return hits[-1] if hits else None


def real_later_vintages(cur=None, raw_dir=None, areas=None):
    """The later vintages held on disk, read only. Attribute files (the
    publisher's ArcGIS attribute queries): against the May 2024 attribute
    file the codes added and removed must be exactly E08000038/39 and
    E08000016/19 and no name may change (an unexpected code or name halts
    the gate). The LONG, LAT and Shape__Area differences on the shared
    districts, and (when the May 2026 GeoJSON is on disk and cur is given)
    the geometry differences against live, are reported as a finding: the
    later BGC vintages are redrawn, they are not the same boundaries with
    two codes recoded."""
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    base_p = _held_attributes(raw, "May_2024")
    if base_p is None:
        return False, f"no May 2024 attributes file in {raw}"
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        base = m.read_attributes(base_p, m.LIVE_VINTAGE)
        bad, finds = [], []
        for vintage in ("DEC_2025", "May_2026"):
            stem = vintage
            p = _held_attributes(raw, stem)
            if p is None:
                bad.append(f"no {vintage} attributes file in {raw}")
                continue
            d = m.vintage_diff(m.read_attributes(p, vintage), base)
            if tuple(d["added"]) != NEW_CODES or \
                    tuple(d["removed"]) != OLD_CODES or d["names"]:
                bad.append(f"{vintage}: codes added {d['added']}, removed "
                           f"{d['removed']}, names changed {d['names']}: "
                           "expected E08000038/39 added, E08000016/19 "
                           "removed, no name changed")
                continue
            finds.append(f"{vintage}: {len(d['attributes'])} of "
                         f"{d['common']} shared districts differ in LONG, "
                         "LAT or Shape__Area")
        g26 = sorted(raw.glob("LAD_MAY_2026*.geojson"))
        if g26 and cur is not None and not bad:
            try:
                f = m.read_geojson(g26[-1], "May_2026")
                d = m.vintage_diff(m.build_records(f),
                                   m.records(cur, REAL, m.LIVE_PERIOD))
                if d["geometry"] is not None:
                    finds.append(f"May_2026 GeoJSON: geometry differs on "
                                 f"{len(d['geometry'])} of {d['common']} "
                                 "shared districts against live")
            except ValueError as e:
                bad.append(f"{g26[-1].name}: {_first(e)}")
    if bad:
        return False, "; ".join(bad[:3])
    return True, ("FINDING (not a failure): " + "; ".join(finds)
                  + "; E08000016/19 removed, E08000038/39 added, no name "
                  "changed: the later vintages are redrawn, so adopting one "
                  "is Scott's decision")


def gate_19_later_vintages(cur):
    name = ("the later vintages held on disk (read only): E08000016/19 "
            "removed, E08000038/39 added, names unchanged; attribute and "
            "geometry differences are a finding, not a failure")
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp)

        def attrs(path, vintage, codes, overrides=None):
            write_json(path, {"features": [
                {"attributes": props(c, i, vintage, overrides)}
                for i, c in enumerate(codes)]})
        attrs(raw / "x_May_2024_attributes.json", V24, CODES)
        attrs(raw / "x_DEC_2025_attributes.json", "DEC_2025", CODES_NEW,
              {(c, "LAT"): 53.0 for c in CODES_NEW})
        attrs(raw / "x_May_2026_attributes.json", V26, CODES_NEW)
        ok, detail = real_later_vintages(None, raw, areas=N)
        if not ok or "FINDING (not a failure)" not in detail \
                or "3 of 3 shared" not in detail:
            problems.append(f"the expected pattern failed: {detail[:160]}")
        # an unexpected extra code, a changed name and a missing file fail
        attrs(raw / "x_May_2026_attributes.json", V26,
              CODES_NEW[:4] + ["E09000001"])
        ok, detail = real_later_vintages(None, raw, areas=N)
        if ok or "expected E08000038/39" not in detail:
            problems.append("an unexpected code was accepted")
        attrs(raw / "x_May_2026_attributes.json", V26, CODES_NEW,
              {("E06000001", "LAD26NM"): "Hartlepool Borough"})
        ok, detail = real_later_vintages(None, raw, areas=N)
        if ok or "names changed" not in detail:
            problems.append("a changed name was accepted")
        (raw / "x_May_2026_attributes.json").unlink()
        ok, detail = real_later_vintages(None, raw, areas=N)
        if ok or "no May_2026 attributes file" not in detail:
            problems.append("a missing file was accepted")
    if problems:
        return report(19, name, False, "seeded: " + "; ".join(problems[:3]))
    try:
        ok, detail = real_later_vintages(cur)
    except (psycopg2.Error, ValueError, OSError) as ex:
        return report(19, name, False, _first(ex))
    report(19, name, ok, f"seeded: the expected pattern passes with a "
           f"finding, an extra code, a changed name and a missing file "
           f"fail; held files: {detail}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_single_vintage, gate_4_identity, gate_5_older_file,
         gate_6_stop_conditions, gate_7_ledger_rule, gate_8_preview,
         gate_9_byte_identical_reread, gate_10_reissue_and_refresh,
         gate_11_before_state_hash, gate_12_migration_proof, gate_13_rerun,
         gate_14_restore, gate_15_no_network_no_secrets,
         gate_16_no_lookup_write_no_delete, gate_17_w1_geometry,
         gate_18_foreign_keys, gate_19_later_vintages)


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
        sys.exit("usage: s7_boundaries_editions_verify.py "
                 "[--print-note-lines]")
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
    mine = [t for t in left if t.startswith("zz_s7")]
    if mine:
        print(f"LEFTOVER: committed zz_s7 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s7 table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
