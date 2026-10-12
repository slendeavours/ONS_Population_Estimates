"""Gates for the S14 (DWP Universal Credit Local Housing Allowance rates, and
the BRMA to local authority crosswalk) editions tables.

Mirrors scripts/s12_financial_stress_editions_verify.py (two specs),
s13_lahs_editions_verify.py and s3_mye_editions_verify.py. Prints
`GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes nothing, not even a
run-log row. The real-table gates read brma_lha_rates, la_brma_mapping and
their editions tables and file-check ledgers. Until the editions tables exist
(ddl and migrate-legacy not yet run) they print `FAIL (pending migration)`
with the words "not yet migrated", so the script exits non-zero until then,
like the other verifiers; nothing is created to make them pass. Gates that
read only the live tables and the files on disk (6, 8, 24) run now.

The seeded gates never need the real editions tables: they run on the
throwaway tables zz_s14_rates_live / zz_s14_map_live / zz_s14_rates_editions /
zz_s14_map_editions and their ledgers (copies of the loader's specs), created
inside the transaction, and call the loader's own writer and commands
(s14_lha_editions.main with the SPECS pointed at the copies, the GOV.UK
replies and the downloads stubbed; migrate_legacy; restore_edition) rather
than a re-implementation. The real tables are only ever read. Seeded parts
use the loader tests' fixtures (five BRMAs, six authorities, a tiny BRMA
layer, all written to a temporary folder). No retired script is imported or
run by any gate (gate 22 checks this with the AST).

Gates:
  1  both editions tables, both ledgers, append-only triggers (UPDATE,
     DELETE, TRUNCATE refused), live checks            (throwaway + real)
  2  every live period has edition 1; the latest edition equals live cell
     for cell (rates: the five monthly and five weekly; crosswalk: la_name,
     brma_name, mapping_method)                          (seeded + real)
  3  152 BRMAs in every rate year (live and tip edition), no "&" name in
     the rates or the crosswalk                          (seeded + real)
  4  rule 1: no NULL, no zero, no negative in any rate; a blank, text, zero,
     negative or three-decimal amount halts and no row is skipped; the
     held CSV's every cell is a money amount and every row is stored
                                                         (seeded + real)
  5  every weekly rate equals monthly x 12 / 52 rounded half-up (Decimal, no
     float), computed independently here, in live, the tip editions and the
     held CSV; the loader's weekly() is Decimal half-up (AST)
                                                         (seeded + real)
  6  the W1 LHA join leaves 0 unmatched authorities: every authority of
     la_boundaries reaches a la_brma_mapping row and, through brma_name, a
     brma_lha_rates row of the newest year with all five weekly rates
                                                         (seeded + real)
  7  296 authorities mapped, codes in la_boundaries, brma_name reproduced by
     the check-mapping rebuild (read-only) from GML.zip and la_boundaries
                                                         (seeded + real)
  8  identity from the file (seeded header-year, layout, "&" name, encoding
     and page mismatches halt); the held CSV and layer's identity
  9  older-file guard on the page, --release and --file paths
 10  stop conditions: each seeded REJECTED, stores nothing, no ledger row,
     partial run-log row; both sides of each limit
 11  a new period: edition, live rows and ledger row in one transaction
 12  ledger skip needs the (source, sha256) pair
 13  preview and simulate write nothing
 14  a byte-identical re-read is `unchanged` on every path against edition 1
     "as loaded"                                         (seeded + real)
 15  the mapping spec never takes a second vintage into live (held, not
     live)                                               (seeded + real)
 16  edition 1 equals the decision note's before-state lines (seeded + real)
 17  the weekly rates at the newest year and brma_name per authority equal
     the decision note's w1-read lines                    (seeded + real)
 18  the migration proofs re-run read-only: the held CSV and the rebuilt
     crosswalk reproduce edition 1                        (seeded + real)
 19  rerun idempotent
 20  stranded period repair
 21  restore-edition round trip
 22  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content, and
     no retired script is imported or run by any gate or test (AST)
 23  refresh_map input discovery: brma_lha_rates and la_brma_mapping are
     inputs, the editions tables and ledgers are not     (stub + real)
 24  the crosswalk alias table is exact (18 "X & Y" to "X and Y" pairs, no
     regex, no fuzzy matching) and its targets are BRMAs of the held rates
 25  the named correction s14-mapping-method-2026-10: edition 2 differs
     from edition 1 only in mapping_method, 'NaN' to centroid_spatial_join
     on 295 rows                                         (seeded + real)

The decision note's hash lines are scan-safe: a 64-hex run would be flagged by
the credential scan, so each sha256 is written as two 32-hex halves in two
labelled fields:
    before-state brma_lha_rates <fy> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    before-state la_brma_mapping <vintage> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    before-state-all brma_lha_rates rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    before-state-all la_brma_mapping rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read brma_lha_rates <fy> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read la_brma_mapping rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
before-state is the loader's rows_content_sha (brma_name and the ten rates)
and mapping_content_sha (lad24cd, la_name, brma_name, mapping_method);
before-state-all adds the live source text (every column edition 1 keeps);
w1-read is what W1 reads: brma_name and the five weekly rates of the newest
year, and (lad24cd, brma_name) of the crosswalk. `python
scripts/s14_lha_editions_verify.py --print-note-lines` prints the lines for
the live tables as they stand (read-only), for the decision note; run it
before the migration.

Usage:
    python scripts/s14_lha_editions_verify.py
    python scripts/s14_lha_editions_verify.py --print-note-lines

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The cursor handed to the gates
refuses commit and rollback on its connection, and the commands get a
stand-in connection whose commit only counts. No network, no real-table
writes, no backend is ever terminated. After the rollback a second,
read-only connection checks that no zz% table, function or view was left
behind.
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
import unittest
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest import mock

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
from _db import ENV, get_conn, get_readonly_conn  # noqa: E402
import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s14_lha_editions as m  # noqa: E402
import test_s14_lha_loader as tl  # noqa: E402
from test_s14_lha_pure import (FY, RATES, _Session, polygons_for,  # noqa: E402
                               write_csv, write_gml)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real specs and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPECS, which the seeded gates
# point at the copies)
REAL_R = m.RATES
REAL_M = m.MAPPING
ZR, ZM = tl.ZR, tl.ZM
RL, ML = tl.RL, tl.ML
TABLES = tl.TABLES
N, NA = tl.N, tl.NA
V = m.LIVE_VINTAGE
CORRECTION = "s14-mapping-method-2026-10"
ALIAS_COUNT = 18
LOADER = HERE / "s14_lha_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-11-s14-editions-first-load.md")
RATES_T = "brma_lha_rates"
MAP_T = "la_brma_mapping"
NOT_YET = "the S14 editions tables do not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (22)
EMPTY = "no live periods to check (an empty state is not a pass)"
# the retired scripts, written in halves so this file never names them whole
RETIRED = ("s3_mye" + "_refresh", "s14_lha_rates" + "_build")
_GML = {}


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


def ledger_name(spec):
    return f"{spec.editions_table}_file_checks"


def exists(cur, rates=REAL_R, mapping=REAL_M):
    return all(pe.table_exists(cur, t) for s in (rates, mapping)
               for t in (s.editions_table, ledger_name(s)))


def _need(cur, *specs):
    miss = [t for s in specs for t in (s.editions_table, ledger_name(s))
            if not pe.table_exists(cur, t)]
    if miss:
        raise ValueError(f"{', '.join(miss)} does not exist")


def is_map(spec):
    return spec.period_col == m.VINTAGE_COL


def values_of(spec):
    return m.MAP_VALUES if is_map(spec) else m.RATE_VALUES


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{t}')"
                                      for t in TABLES))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s14 table already exists as a real table; "
                 "refusing to run")
    cur.execute(f"CREATE TABLE public.{ZR.live_table} (LIKE "
                f"public.{REAL_R.live_table} INCLUDING ALL)")
    cur.execute(f"CREATE TABLE public.{ZM.live_table} (LIKE "
                f"public.{REAL_M.live_table} INCLUDING ALL)")
    with tl.specs():
        m.create_all(cur)


def state(cur):
    """Everything a refused or previewed run must leave alone: the live and
    editions tables and the ledgers of both parts."""
    out = []
    for t in TABLES:
        cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                    f"FROM public.{t} t")
        out.append(cur.fetchone()[0])
    return tuple(out)


def tables_state(cur):
    """state() without the two ledgers (a re-read adds 'unchanged' rows)."""
    s = state(cur)
    return s[:4]


def cnt(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


# ------------------------------------------------------------ files and Env

class Env(tl.Migrate):
    """The loader's main(argv) on the throwaway tables, the GOV.UK replies
    and downloads stubbed, every CSV and layer written here into a
    temporary directory. Subclasses the loader tests' Migrate fixture so
    publish, local, seed_legacy and migrate are the tests' own."""

    def __init__(self, cur, root):
        unittest.TestCase.__init__(self)
        self.cur = cur
        self.root = Path(root)
        self.docs = [("2026 to 2027", tl.TS), ("2025 to 2026", "x"),
                     ("2014 to 2019", "x")]
        self.api, self.urls, self.final, self.fetched = {}, {}, {}, []
        self.n = 0
        self.gml = self.root / "gml" / "GML.zip"
        self.gml.parent.mkdir(parents=True, exist_ok=True)
        if "bytes" not in _GML:
            tmp = Path(tempfile.mkdtemp())
            try:
                _GML["bytes"] = write_gml(tmp / "GML.zip").read_bytes()
            finally:
                for f in tmp.iterdir():
                    f.unlink()
                tmp.rmdir()
        self.gml.write_bytes(_GML["bytes"])
        self.gml_sha = m.content_sha256(self.gml)

    def run_main(self, cur, argv):
        res = super().run_main(cur, argv)
        OUTPUT.extend(res[1].splitlines())
        return res

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

    def specs(self, extra=None):
        return tl.specs(self.gml_sha, extra)


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp)


def scenario(cur, fn):
    """Run fn(env) in a savepoint that is rolled back."""
    def body(c):
        with env(c) as e, e.specs():
            return fn(e)
    return _in_savepoint(cur, body)


def legacy_world(cur, fn, **kw):
    """A seeded legacy state (as the retired build left it) and the fixture
    CSV; fn(env, path, legacy)."""
    def body(c):
        with env(c) as e, e.specs():
            path, legacy = e.seed_legacy(c, **kw)
            return fn(e, path, legacy)
    return _in_savepoint(cur, body)


def patched_legacy(legacy, path):
    """The loader's held-state constants pointed at the seeded state."""
    stack = contextlib.ExitStack()
    for name, key in (("LEGACY_RATES", "rates"),
                      ("LEGACY_RATES_LOADED", "rates_loaded"),
                      ("LEGACY_MAPPING", "mapping"),
                      ("LEGACY_MAPPING_LOADED", "mapping_loaded"),
                      ("LEGACY_RATES_FILE", "rates_file"),
                      ("LEGACY_METHOD_FORM", "method_form")):
        stack.enter_context(mock.patch.object(m, name, legacy[key]))
    stack.enter_context(mock.patch.object(m, "RATES_FILE", path))
    return stack


def mixed(n, name, seeded_ok, seeded_detail, cur, real_fn):
    """A gate with a seeded part (always) and a real part (once the editions
    tables exist, else pending)."""
    if not seeded_ok:
        return report(n, name, False, f"seeded: {seeded_detail}")
    if not exists(cur):
        return report(n, name, False, f"seeded part passes; real: {NOT_YET}",
                      pending=True)
    try:
        ok, detail = real_fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError,
            AssertionError) as e:
        return report(n, name, False, _first(e))
    report(n, name, ok, f"{seeded_detail}; real: {detail}")


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


def _constraint_names(cur, table, kind):
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = "
                "%s::regclass AND contype = %s", (f"public.{table}", kind))
    return {r[0] for r in cur.fetchall()}


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
    fks = len(_constraint_names(cur, spec.editions_table, "f"))
    if fks != (1 if spec.fk_la_boundaries else 0):
        bad.append(f"{spec.editions_table}: {fks} foreign key(s)")
    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                "conrelid = %s::regclass AND contype = 'c'",
                (f"public.{spec.editions_table}",))
    defs = [r[0] for r in cur.fetchall()]
    for c in spec.table_constraints:
        col = re.match(r"CHECK \(\s*([a-z_0-9]+)", c)
        if not col or not any(col.group(1) in d for d in defs):
            bad.append(f"{spec.editions_table}: {c[:50]} missing")
    if len(defs) < len(spec.table_constraints):
        bad.append(f"{spec.editions_table}: {len(defs)} CHECK constraints, "
                   f"expected {len(spec.table_constraints)}")
    bad += _triggers(cur, spec.editions_table, spec.trigger,
                     spec.truncate_trigger)
    live_cols = (tuple(spec.key_cols) + (spec.period_col,)
                 + tuple(c for c, _ in spec.value_cols)
                 + ("source", "loaded_at"))
    for c in live_cols:
        if c not in lcols:
            bad.append(f"{spec.live_table}: {c} missing")
    lc = _columns(cur, led)
    ptype = dict(spec.key_types)[spec.period_col]
    for c, ty, nullable in (("id", "bigint", "NO"),
                            (spec.period_col, _family(ptype), "NO"),
                            ("source_file", "text", "NO"),
                            ("file_sha256", "text", "NO"),
                            ("outcome", "text", "NO"),
                            ("edition", "integer", "YES"),
                            ("checked_at", "timestamp with time zone", "NO")):
        got = lc.get(c)
        if got is None or tuple(got) != (ty, nullable):
            bad.append(f"{led}: {c} is {got}, expected {(ty, nullable)}")
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass AND contype = 'c'""",
                (f"public.{led}",))
    chk = " ".join(r[0] for r in cur.fetchall())
    if not all(f"'{o}'" in chk for o in pe.FILE_CHECK_OUTCOMES):
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
    name = ("both editions tables, both ledgers, live shape (UPDATE, DELETE "
            "and TRUNCATE are refused on the editions and the ledgers)")
    bad = shape_problems(cur, ZR) + shape_problems(cur, ZM)

    def seed(cur):
        with env(cur) as e, e.specs():
            e.seed(cur)
    targets = []
    for spec, led in ((ZR, RL), (ZM, ML)):
        targets += [(f"editions {spec.editions_table}", {
            "UPDATE": f"UPDATE public.{spec.editions_table} SET edition = "
                      "edition",
            "DELETE": f"DELETE FROM public.{spec.editions_table}",
            "TRUNCATE": f"TRUNCATE public.{spec.editions_table}"}),
            (f"ledger {led}", {
                "UPDATE": f"UPDATE public.{led} SET outcome = 'new'",
                "DELETE": f"DELETE FROM public.{led}",
                "TRUNCATE": f"TRUNCATE public.{led}"})]
    for what, stmts in targets:
        for k, msg in immutability(cur, stmts, seed).items():
            if not (msg and "append-only" in msg):
                bad.append(f"{what} {k} not refused ({msg})")
    scope = "throwaway copies"
    if exists(cur):
        scope += " and real tables"
        bad += [f"real: {p}" for s in (REAL_R, REAL_M)
                for p in shape_problems(cur, s)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise "
           "append-only on both editions tables and both ledgers")


# ------------------------------------------------------- real-table gates

def _periods(cur, spec):
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [str(r[0]) for r in cur.fetchall()]


def _live_periods(cur, spec):
    """Live periods; a crosswalk live table without the vintage column yet
    holds the one live vintage."""
    if is_map(spec) and not m.live_has_vintage(cur, spec):
        cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table}")
        return [V] if cur.fetchone()[0] else []
    return _periods(cur, spec)


def _tip(cur, spec, p):
    return core.chain_tip(cur, spec, str(p))


def _both_ways(cur, a, aargs, b, bargs):
    counts = []
    for x, xa, y, ya in ((a, aargs, b, bargs), (b, bargs, a, aargs)):
        cur.execute(f"SELECT COUNT(*) FROM (({x}) EXCEPT ALL ({y})) q",
                    xa + ya)
        counts.append(cur.fetchone()[0])
    return counts


def real_edition1_and_latest(cur, rates=REAL_R, mapping=REAL_M):
    _need(cur, rates, mapping)
    bad, done = [], []
    for spec in (rates, mapping):
        periods = _live_periods(cur, spec)
        if not periods:
            return False, f"{spec.live_table}: {EMPTY}"
        cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                    f"public.{spec.editions_table} WHERE edition = 1 AND "
                    "source_file IS NOT NULL")
        have = {str(r[0]) for r in cur.fetchall()}
        missing = [p for p in periods if p not in have]
        if missing:
            bad.append(f"{spec.live_table}: no edition 1 (with a "
                       f"source_file) for {missing[:6]}")
            continue
        cols = ", ".join(tuple(spec.key_cols) + (spec.period_col,)
                         + values_of(spec))
        for p in periods:
            ed = _tip(cur, spec, p)
            a, b = _both_ways(
                cur, f"SELECT {cols} FROM public.{spec.editions_table} WHERE "
                f"{spec.period_col} = %s AND edition = %s", (p, ed),
                f"SELECT {cols} FROM public.{spec.live_table} WHERE "
                f"{spec.period_col} = %s", (p,))
            if a or b:
                bad.append(f"{spec.live_table} {p} ed{ed}: {a} "
                           f"edition-only, {b} live-only rows")
        done.append(f"{spec.live_table} {len(periods)} period(s)")
    return not bad, "; ".join(bad[:3]) if bad else (
        "; ".join(done) + ": each has edition 1 and the latest edition "
        "equals live cell for cell")


def gate_2_edition1_and_latest(cur):
    name = ("every live period has edition 1; the latest edition equals "
            "live cell for cell (rates and crosswalk)")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_edition1_and_latest(e.cur, ZR, ZM)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZR.live_table} SET sar_monthly = sar_monthly "
             "+ 1 WHERE brma_name = 'Ashford'", "edition-only"),
            (f"UPDATE public.{ZR.live_table} SET four_bed_weekly = "
             "four_bed_weekly + 0.01 WHERE brma_name = 'Ashford'",
             "edition-only"),
            (f"DELETE FROM public.{ZR.live_table} WHERE brma_name = "
             "'Ashford'", "edition-only"),
            (f"UPDATE public.{ZR.live_table} SET financial_year = '2030-31'",
             "no edition 1"),
            (f"UPDATE public.{ZM.live_table} SET brma_name = 'Ashford' "
             "WHERE lad24cd = 'E06000010'", "edition-only"),
            (f"UPDATE public.{ZM.live_table} SET mapping_method = 'NaN' "
             "WHERE lad24cd = 'E06000010'", "edition-only"),
            (f"DELETE FROM public.{ZM.live_table} WHERE lad24cd = "
             "'E06000010'", "edition-only"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            try:
                ok, detail = real_edition1_and_latest(e.cur, ZR, ZM)
            except (psycopg2.Error, ValueError, LookupError) as ex:
                ok, detail = False, str(ex)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(2, name, ok, "seeded complete state passes; seven planted drifts "
          "(a monthly or weekly rate, a missing BRMA, a year without "
          "edition 1, a brma_name, a mapping_method, a missing authority) "
          "are each caught" if ok else f"{out['base']} {out['miss']}", cur,
          real_edition1_and_latest)


# ---------------------------------------------------------------- gate 3

def real_brma_sets(cur, rates=REAL_R, mapping=REAL_M, n=m.EXPECTED_POLYGONS):
    """Every rate year, in live and in its tip edition, has n BRMAs, one row
    each; no BRMA name in the rates or in the crosswalk contains '&'."""
    _need(cur, rates, mapping)
    periods = _live_periods(cur, rates)
    if not periods:
        return False, EMPTY
    bad = []
    for p in periods:
        tip = _tip(cur, rates, p)
        for table, extra in ((rates.live_table, ""),
                             (rates.editions_table, f" AND edition = {tip}")):
            cur.execute(f"SELECT COUNT(DISTINCT brma_name), COUNT(*), "
                        f"COUNT(*) FILTER (WHERE brma_name LIKE '%%&%%') "
                        f"FROM public.{table} WHERE financial_year = %s"
                        f"{extra}", (p,))
            k, rows, amp = cur.fetchone()
            if (k, rows) != (n, n):
                bad.append(f"{table} {p}: {k} BRMAs in {rows} rows, "
                           f"expected {n}")
            if amp:
                bad.append(f"{table} {p}: {amp} name(s) contain '&'")
    for table in (mapping.live_table, mapping.editions_table):
        cur.execute(f"SELECT COUNT(*) FROM public.{table} WHERE brma_name "
                    "LIKE '%%&%%'")
        if cur.fetchone()[0]:
            bad.append(f"{table}: brma_name contains '&'")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} rate year(s): {n} BRMAs, one row each, in live and "
        "in the tip edition; no '&' in any rate or crosswalk name")


def gate_3_brma_sets(cur):
    name = ("152 BRMAs in every rate year (live and tip edition), no '&' "
            "name in the rates or the crosswalk")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_brma_sets(e.cur, ZR, ZM, N)
        out["wrong_n"] = real_brma_sets(e.cur, ZR, ZM, N + 1)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZR.live_table} SET brma_name = 'Kernow & West'"
             " WHERE brma_name = 'Kernow West'", "contain '&'"),
            (f"DELETE FROM public.{ZR.live_table} WHERE brma_name = "
             "'Ashford'", "expected"),
            (f"UPDATE public.{ZM.live_table} SET brma_name = 'Hull & East "
             "Riding' WHERE lad24cd = 'E06000010'", "brma_name contains"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_brma_sets(e.cur, ZR, ZM, N)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["wrong_n"][0] and not out["miss"]
    mixed(3, name, ok, ("the expected count passes; one more BRMA, an '&' "
                        "name in the rates or the crosswalk, or a missing "
                        "BRMA fails" if ok else f"{out}"), cur,
          real_brma_sets)


# ---------------------------------------------------------------- gate 4

def zero_coercions(path):
    """Problems in a module's source (an AST check): a source value coerced
    to 0 (`x or 0`, COALESCE(x, 0), `else 0`) on a line without `# not a
    source value`."""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines()
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            if ast.get_docstring(node, clean=False) is not None and \
                    node.body and isinstance(node.body[0], ast.Expr):
                doc.add(id(node.body[0].value))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc \
                and re.search(r"(?i)coalesce\([^)]*,\s*0\s*\)", node.value):
            bad.append(f"line {node.lineno}: COALESCE(x, 0)")
        zero = (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                and any(isinstance(v, ast.Constant) and v.value == 0
                        and not isinstance(v.value, bool)
                        for v in node.values[1:]))
        ifexp = (isinstance(node, ast.IfExp)
                 and isinstance(node.orelse, ast.Constant)
                 and node.orelse.value == 0
                 and not isinstance(node.orelse.value, bool))
        if (zero or ifexp) and "not a source value" not in lines[
                node.lineno - 1]:
            bad.append(f"line {node.lineno}: a value coerced to 0")
    return bad


def held_csv(raw_dir=None, sha=None):
    """The rates CSV on disk whose sha256 is the held file's, or None."""
    sha = m.LEGACY_RATES_FILE["sha256"] if sha is None else sha
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    for p in sorted(raw.glob("*.csv")) if raw.is_dir() else []:
        if m.content_sha256(p) == sha:
            return p
    return None


def read_held_csv(raw_dir=None, sha=None):
    """(path, file facts) of the held rates CSV, or (None, a problem)."""
    p = held_csv(raw_dir, sha)
    if p is None:
        return None, (f"no CSV in {raw_dir or m.RAW_DIR} with sha256 "
                      f"{(sha or m.LEGACY_RATES_FILE['sha256'])[:16]}")
    try:
        return p, m.read_csv(p)
    except ValueError as e:
        return None, f"{p.name}: {_first(e)}"


def real_rule_1(cur, rates=REAL_R, raw_dir=None, sha=None):
    """In live and each year's tip edition: no NULL and no zero or negative
    in any of the ten rates (the editions table holds them NOT NULL and
    CHECKed > 0). The held CSV: every monthly cell is a money amount and
    every data row is a stored BRMA (no row skipped)."""
    _need(cur, rates)
    periods = _live_periods(cur, rates)
    if not periods:
        return False, EMPTY
    bad, rows = [], 0
    cols = _columns(cur, rates.editions_table)
    for c in m.RATE_VALUES:
        if cols.get(c, (None, "YES"))[1] != "NO":
            bad.append(f"{rates.editions_table}.{c} is nullable")
    null_any = " OR ".join(f"{c} IS NULL" for c in m.RATE_VALUES)
    zero_any = " OR ".join(f"{c} <= 0" for c in m.RATE_VALUES)
    for p in periods:
        tip = _tip(cur, rates, p)
        for table, extra in ((rates.live_table, ""),
                             (rates.editions_table, f" AND edition = {tip}")):
            cur.execute(f"SELECT COUNT(*), COUNT(*) FILTER (WHERE "
                        f"{null_any}), COUNT(*) FILTER (WHERE {zero_any}) "
                        f"FROM public.{table} WHERE financial_year = %s"
                        f"{extra}", (p,))
            n, nulls, zero = cur.fetchone()
            rows += n
            if nulls:
                bad.append(f"{table} {p}: {nulls} row(s) with a NULL rate")
            if zero:
                bad.append(f"{table} {p}: {zero} row(s) with a zero or "
                           "negative rate")
    path, facts = read_held_csv(raw_dir, sha)
    note = ""
    if path is None:
        bad.append(facts)
    else:
        data_rows = len(facts["rows"])
        cells = 0
        for n_, bname, cells_ in facts["rows"]:
            for c, head in zip(cells_, facts["header"][1:]):
                m.money(c, f"{path.name} row {n_} {bname} {head!r}")
                cells += 1
        year = facts["year"]
        cur.execute(f"SELECT COUNT(*) FROM public.{rates.editions_table} "
                    "WHERE financial_year = %s AND edition = 1", (year,))
        stored = cur.fetchone()[0]
        if stored != data_rows:
            bad.append(f"{path.name}: {data_rows} data rows, edition 1 of "
                       f"{year} stores {stored} (a row was skipped or "
                       "added)")
        note = (f"; {path.name}: {data_rows} data rows, {cells} cells, "
                f"every one a money amount, all {stored} stored")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{rows:,} rows over live and the tip editions: no NULL, no zero, "
        f"no negative; the editions columns NOT NULL{note}")


def gate_4_rule_1(cur):
    name = ("rule 1: no NULL, zero or negative rate; a blank, text, zero, "
            "negative or three-decimal amount halts and no row is skipped; "
            "no value coerced to 0")
    problems = []

    def body(e):
        cases = (("", "blank"), (None, "blank"), ("n/a", "money amount"),
                 ("0.00", "zero"), ("-5.00", "negative"),
                 ("395.123", "money amount"), ("RAW:  ", "blank"))
        for cell, needle in cases:
            e.publish(rates=dict(RATES, **{"Kernow West": (
                "392.50", cell, "695.00", "815.00", "1,100.00")}))
            before = state(e.cur)
            ok, text = e.halts(["load", "--only", "rates", "--commit"],
                               before, needle)
            if not ok:
                problems.append(f"{cell!r}: not halted naming {needle!r}: "
                                f"{text[-100:]}")
        for cell in ("", "  ", "n/a", "text", "0", "0.00", "-1", "£-1.00",
                     "1.234", None):
            try:
                got = m.money(cell, "x")
                problems.append(f"money({cell!r}) returned {got}")
            except ValueError:
                pass
        if m.money("£1,097.80", "x") != Decimal("1097.80"):
            problems.append("a money amount with '£' and a thousands "
                            "separator is not read")
        # no row is skipped: a table with one unreadable row builds nothing
        f = m.read_csv(e.local(rates=dict(RATES, **{"Kernow West": (
            "392.50", "oops", "695.00", "815.00", "1,100.00")})))
        try:
            m.build_rates(f, FY)
            problems.append("build_rates skipped an unreadable row")
        except ValueError as ex:
            if "Kernow West" not in str(ex):
                problems.append("the unreadable row is not named")
        ok = m.build_rates(m.read_csv(e.local()), FY)
        if len(ok) != len(RATES):
            problems.append("build_rates dropped a row")
        # the check on a stored state
        e.seed(e.cur)
        path = e.local()
        sha = m.content_sha256(path)
        base = real_rule_1(e.cur, ZR, path.parent, sha)
        if not base[0]:
            problems.append(f"seeded state: {base[1]}")
        for sql, needle in (
            (f"UPDATE public.{ZR.live_table} SET three_bed_monthly = 0 "
             "WHERE brma_name = 'Ashford'", "zero or negative"),
            (f"UPDATE public.{ZR.live_table} SET sar_weekly = NULL WHERE "
             "brma_name = 'Ashford'", "NULL"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok_, detail = real_rule_1(e.cur, ZR, path.parent, sha)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok_ or needle not in detail:
                problems.append(f"{needle!r}: {detail[:80]}")
        e.cur.execute("SAVEPOINT plant")
        e.cur.execute(f"ALTER TABLE public.{ZR.editions_table} DISABLE "
                      "TRIGGER USER")
        e.cur.execute(f"DELETE FROM public.{ZR.editions_table} WHERE "
                      "brma_name = 'Ashford'")
        ok_, detail = real_rule_1(e.cur, ZR, path.parent, sha)
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
        if ok_ or "data rows" not in detail:
            problems.append(f"a skipped row passed: {detail[:80]}")
        # the table refuses a NULL or zero at the schema level
        for sql in (f"INSERT INTO public.{ZR.editions_table} (brma_name, "
                    "financial_year, edition, release_label, source_file, "
                    "source_sha256, source_text, sar_monthly) VALUES ('x', "
                    "'2030-31', 1, 'x', 'x', 'x', 'x', NULL)",
                    ):
            e.cur.execute("SAVEPOINT plant")
            try:
                e.cur.execute(sql)
                problems.append("the editions table accepted a NULL rate")
            except psycopg2.Error:
                pass
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
    scenario(cur, body)
    src = zero_coercions(LOADER)
    if src:
        problems.append(f"the loader: {src[:2]}")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text('def f(v):\n    return v or 0\n'
                     'def g(v):\n    return v or 0  # not a source value\n',
                     encoding="utf-8")
        planted = zero_coercions(p)
    if len(planted) != 1 or "line 2" not in planted[0]:
        problems.append(f"the zero-coercion check is wrong: {planted}")
    seeded = not problems
    sd = ("seeded: a blank, an all-space cell, 'n/a', a zero, a negative and "
          "a three-decimal amount each halt and store nothing; money() never "
          "returns a default; build_rates never skips a row; planted zeros "
          "and NULLs and a skipped row are caught; no value is coerced to 0 "
          "in the loader" if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(4, name, False, sd)
    if not exists(cur):
        # the held CSV is read now whether or not the tables exist
        path, facts = read_held_csv()
        if path is None:
            return report(4, name, False, f"seeded part passes; {facts}")
        return report(4, name, False,
                      f"seeded part passes; real: {NOT_YET}", pending=True)
    try:
        ok, detail = real_rule_1(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(4, name, False, _first(ex))
    report(4, name, ok, f"{sd}; real: {detail}")


# ---------------------------------------------------------------- gate 5

def expected_weekly(monthly):
    """monthly x 12 / 52 in Decimal, rounded half-up to the penny; written
    here again, independent of the loader's weekly()."""
    return (Decimal(monthly) * Decimal(12) / Decimal(52)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def weekly_ast_problems(path=LOADER):
    """Problems (an AST check) in the loader's weekly(): it must use Decimal
    arithmetic and ROUND_HALF_UP, and never float() or round()."""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name == "weekly"]
    if len(fns) != 1:
        return [f"{len(fns)} functions named weekly"]
    fn = fns[0]
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    bad = []
    if "ROUND_HALF_UP" not in names | attrs:
        bad.append("weekly() does not round with ROUND_HALF_UP")
    if "Decimal" not in names:
        bad.append("weekly() does not use Decimal")
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ("float", "round"):
            bad.append(f"weekly() calls {n.func.id}()")
    seg = "\n".join(text.splitlines()[fn.lineno - 1:fn.end_lineno])
    if "not a source value" not in seg:
        bad.append("weekly() is not marked '# not a source value'")
    return bad


def real_weekly(cur, rates=REAL_R, raw_dir=None, sha=None):
    """Every weekly rate in live, in each year's tip edition and in the held
    CSV equals its monthly rate x 12 / 52, Decimal, half-up (computed here
    independently); the loader's weekly() is Decimal half-up (AST)."""
    _need(cur, rates)
    periods = _live_periods(cur, rates)
    if not periods:
        return False, EMPTY
    bad, checked = [], 0
    cols = ", ".join(("brma_name",) + m.MONTHLY + m.WEEKLY)
    for p in periods:
        tip = _tip(cur, rates, p)
        for table, extra in ((rates.live_table, ""),
                             (rates.editions_table, f" AND edition = {tip}")):
            cur.execute(f"SELECT {cols} FROM public.{table} WHERE "
                        f"financial_year = %s{extra}", (p,))
            for row in cur.fetchall():
                for mo, wk, v in zip(m.MONTHLY, m.WEEKLY, zip(
                        row[1:6], row[6:11])):
                    checked += 1
                    if v[1] != expected_weekly(v[0]):
                        bad.append(f"{table} {p} {row[0]} {wk} {v[1]} != "
                                   f"{v[0]} x 12 / 52 = "
                                   f"{expected_weekly(v[0])}")
    path, facts = read_held_csv(raw_dir, sha)
    if path is None:
        bad.append(facts)
    else:
        for r in m.build_rates(facts, facts["year"]):
            for mo, wk in zip(m.MONTHLY, m.WEEKLY):
                if r[wk] != expected_weekly(r[mo]):
                    bad.append(f"{path.name} {r['brma_name']} {wk} "
                               f"{r[wk]} != {expected_weekly(r[mo])}")
    bad += weekly_ast_problems()
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{checked:,} weekly values in live and the tip editions equal "
        "monthly x 12 / 52 half-up, and so do the held CSV's; weekly() is "
        "Decimal ROUND_HALF_UP with no float or round() (AST)")


def gate_5_weekly(cur):
    name = ("every weekly rate equals monthly x 12 / 52, Decimal half-up "
            "(live, editions, held CSV); weekly() has no float or round()")
    problems = []

    def body(e):
        e.seed(e.cur)
        path = e.local()
        sha = m.content_sha256(path)
        ok, detail = real_weekly(e.cur, ZR, path.parent, sha)
        if not ok:
            problems.append(f"seeded state: {detail}")
        for sql in (f"UPDATE public.{ZR.live_table} SET three_bed_weekly = "
                    "three_bed_weekly + 0.01 WHERE brma_name = 'Ashford'",):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_weekly(e.cur, ZR, path.parent, sha)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or "!=" not in detail:
                problems.append(f"a planted weekly passed: {detail[:80]}")
        # a stored weekly that is a float round of the monthly is caught:
        # find a monthly where half-up and float rounding differ
        e.cur.execute("SAVEPOINT plant")
        e.cur.execute(f"UPDATE public.{ZR.live_table} SET sar_weekly = "
                      "round((sar_monthly::float8 * 12 / 52)::numeric, 2) + "
                      "0.01 WHERE brma_name = 'Ashford'")
        ok, detail = real_weekly(e.cur, ZR, path.parent, sha)
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
        if ok:
            problems.append("a weekly one penny off passed")
    scenario(cur, body)
    # the loader's derivation, against ours and against float rounding
    for amount in ("393.24", "750.00", "850.00", "1097.80", "1440.00",
                   "0.01", "3000.00", "1234.56"):
        if m.weekly(Decimal(amount)) != expected_weekly(amount):
            problems.append(f"weekly({amount}) differs from the independent "
                            "half-up value")
    if m.weekly(Decimal("0.195")) != Decimal("0.05") or \
            round(0.195 * 12 / 52, 2) != 0.04:
        problems.append("the half-up probe (0.195 x 12 / 52 = 0.045 exactly: "
                        "half-up 0.05, float round 0.04) is wrong")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text("def weekly(monthly):\n    return round(float(monthly) "
                     "* 12 / 52, 2)\n", encoding="utf-8")
        planted = weekly_ast_problems(p)
    if len(planted) < 3:
        problems.append(f"the weekly AST check missed a float round: "
                        f"{planted}")
    problems += [f"loader: {x}" for x in weekly_ast_problems()]
    seeded = not problems
    sd = ("seeded: a planted weekly (one penny off) is caught; weekly() "
          "agrees with the independent Decimal half-up value on eight "
          "amounts, and 0.195 gives 0.05 where float rounding gives 0.04; "
          "the AST check catches a float round()" if seeded
          else "; ".join(problems[:3]))
    mixed(5, name, seeded, sd, cur, real_weekly)


# ---------------------------------------------------------------- gate 6

def w1_join_unmatched(cur, rates, mapping, driver="la_boundaries"):
    """[(lad24cd, why)] for every authority of `driver` (a table with
    lad24cd) that the W1 LHA join does not reach: no crosswalk row, no
    rate row for its brma_name in the newest rate year, or a NULL weekly
    rate. The join is that of sql/w1/05_la_signals.sql."""
    cur.execute(f"""
        SELECT b.lad24cd,
               CASE WHEN lbm.lad24cd IS NULL THEN 'no crosswalk row'
                    WHEN lha.brma_name IS NULL THEN 'no rate row for '
                         || lbm.brma_name
                    ELSE 'a NULL weekly rate' END
          FROM public.{driver} b
          LEFT JOIN public.{mapping.live_table} lbm ON lbm.lad24cd = b.lad24cd
          LEFT JOIN public.{rates.live_table} lha
            ON lha.brma_name = lbm.brma_name
           AND lha.financial_year = (SELECT MAX(financial_year) FROM
                                     public.{rates.live_table})
         WHERE lbm.lad24cd IS NULL OR lha.brma_name IS NULL
            OR lha.sar_weekly IS NULL OR lha.one_bed_weekly IS NULL
            OR lha.two_bed_weekly IS NULL OR lha.three_bed_weekly IS NULL
            OR lha.four_bed_weekly IS NULL
         ORDER BY b.lad24cd""")
    return cur.fetchall()


def real_w1_join(cur, rates=REAL_R, mapping=REAL_M, driver="la_boundaries",
                 n=m.EXPECTED_AUTHORITIES):
    """Reads the live tables only. The W1 LHA join leaves 0 unmatched
    authorities out of n (verify_source_registry gate 18 checks the same)."""
    cur.execute(f"SELECT COUNT(*) FROM public.{driver}")
    total = cur.fetchone()[0]
    if total != n:
        return False, f"{driver} has {total} authorities, expected {n}"
    cur.execute(f"SELECT MAX(financial_year) FROM public.{rates.live_table}")
    fy = cur.fetchone()[0]
    un = w1_join_unmatched(cur, rates, mapping, driver)
    if un:
        return False, (f"{len(un)} of {total} authorities are not reached by "
                       f"the W1 LHA join at {fy}: "
                       + ", ".join(f"{c} ({why})" for c, why in un[:4]))
    return True, (f"0 of {total} authorities unmatched: every authority has "
                  f"a crosswalk row and, through brma_name, all five weekly "
                  f"rates of {fy}")


def gate_6_w1_join(cur):
    name = ("the W1 LHA join leaves 0 unmatched authorities (crosswalk row, "
            "rate row at the newest year, five weekly rates)")
    problems = []

    def body(e):
        e.seed(e.cur)
        ok, detail = real_w1_join(e.cur, ZR, ZM, ZM.live_table, NA)
        if not ok:
            problems.append(f"seeded state: {detail}")
        for sql, needle in (
            (f"UPDATE public.{ZM.live_table} SET brma_name = 'Hull & East "
             "Riding' WHERE lad24cd = 'E06000010'", "no rate row for"),
            (f"DELETE FROM public.{ZR.live_table} WHERE brma_name = "
             "'Ashford'", "no rate row for Ashford"),
            (f"UPDATE public.{ZR.live_table} SET two_bed_weekly = NULL "
             "WHERE brma_name = 'Ashford'", "NULL weekly"),
            (f"INSERT INTO public.{ZR.live_table} (brma_name, "
             "financial_year, sar_monthly, one_bed_monthly, two_bed_monthly,"
             " three_bed_monthly, four_bed_monthly, sar_weekly, "
             "one_bed_weekly, two_bed_weekly, three_bed_weekly, "
             "four_bed_weekly) VALUES ('Z', '2099-00', 1, 1, 1, 1, 1, 1, 1, "
             "1, 1, 1)", "no rate row for"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_w1_join(e.cur, ZR, ZM, ZM.live_table, NA)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                problems.append(f"{needle!r}: {detail[:90]}")
        ok, detail = real_w1_join(e.cur, ZR, ZM, ZM.live_table, NA + 1)
        if ok:
            problems.append("a wrong authority count passed")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: the join reaches every authority; a '&' brma_name, a "
          "missing rate row, a NULL weekly rate and a newest year without "
          "the BRMAs are each caught" if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(6, name, False, sd)
    try:
        ok, detail = real_w1_join(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(6, name, False, _first(ex))
    report(6, name, ok, f"{sd}; real: {detail}")


# ---------------------------------------------------------------- gate 7

def real_mapping(cur, rates=REAL_R, mapping=REAL_M, gml=None,
                 n=m.EXPECTED_AUTHORITIES, all_boundaries=True):
    """Reads the live tables and the layer on disk: the crosswalk maps n
    authorities, every code in la_boundaries and every la_boundaries code
    mapped; the check-mapping rebuild (read-only, from GML.zip and
    la_boundaries) reproduces brma_name and la_name for every authority;
    mapping_method differences are listed."""
    cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT lad24cd) FROM public."
                f"{mapping.live_table}")
    rows, k = cur.fetchone()
    bad = []
    if (rows, k) != (n, n):
        bad.append(f"{mapping.live_table}: {k} authorities in {rows} rows, "
                   f"expected {n}")
    cur.execute(f"""SELECT (SELECT COUNT(*) FROM public.{mapping.live_table} x
                             WHERE x.lad24cd NOT IN (SELECT lad24cd FROM
                                                     public.la_boundaries)),
                           (SELECT COUNT(*) FROM public.la_boundaries b
                             WHERE b.lad24cd NOT IN (SELECT lad24cd FROM
                                                     public.{mapping.live_table}))""")
    stray, unmapped = cur.fetchone()
    if stray:
        bad.append(f"{stray} mapped code(s) not in la_boundaries")
    if unmapped and all_boundaries:
        bad.append(f"{unmapped} la_boundaries code(s) not mapped")
    fy, names = m.latest_rate_names(cur, rates, False)
    path = Path(gml) if gml else m.GML_FILE
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            v, recs, _ = m.rebuild_mapping(cur, path, names)
        except SystemExit as e:
            return False, f"check-mapping rebuild: {_first(e)}"
    held = {r["lad24cd"]: r for r in m.records(cur, mapping, v)}
    new = {r["lad24cd"]: r for r in recs}
    if set(held) != set(new):
        bad.append(f"authorities on one side only: "
                   f"{sorted(set(held) ^ set(new))[:4]}")
    both = sorted(set(held) & set(new))
    same = {c: sum(held[k][c] == new[k][c] for k in both)
            for c in m.MAP_VALUES}
    for c in ("brma_name", "la_name"):
        if same[c] != len(both):
            diff = [k for k in both if held[k][c] != new[k][c]]
            bad.append(f"check-mapping: {c} reproduced for {same[c]} of "
                       f"{len(both)}; differs at {diff[:4]}")
    methods = {}
    for k in both:
        if held[k]["mapping_method"] != new[k]["mapping_method"]:
            key = (held[k]["mapping_method"], new[k]["mapping_method"])
            methods[key] = methods.get(key, 0) + 1
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{rows} authorities mapped, all in la_boundaries and none left out; "
        f"the rebuild from {path.name} reproduces brma_name and la_name for "
        f"{same['brma_name']} of {len(both)}; mapping_method differences "
        f"(held -> rebuilt): "
        + (", ".join(f"{a!r} -> {b!r} x {c}"
                     for (a, b), c in sorted(methods.items())) or "none"))


def gate_7_mapping(cur):
    name = ("296 authorities mapped, codes in la_boundaries, brma_name "
            "reproduced by the check-mapping rebuild (read-only)")
    problems = []

    def body(e):
        e.seed(e.cur)
        ok, detail = real_mapping(e.cur, ZR, ZM, e.gml, NA, False)
        if not ok:
            problems.append(f"seeded state: {detail}")
        for sql, needle in (
            (f"UPDATE public.{ZM.live_table} SET brma_name = 'Ashford' "
             "WHERE lad24cd = 'E06000010'", "brma_name reproduced"),
            (f"UPDATE public.{ZM.live_table} SET la_name = 'x' WHERE "
             "lad24cd = 'E06000010'", "la_name reproduced"),
            (f"DELETE FROM public.{ZM.live_table} WHERE lad24cd = "
             "'E06000010'", "expected"),
            (f"UPDATE public.{ZM.live_table} SET lad24cd = 'E07999999' "
             "WHERE lad24cd = 'E06000010'", "not in la_boundaries"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_mapping(e.cur, ZR, ZM, e.gml, NA, False)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                problems.append(f"{needle!r}: {detail[:90]}")
        # the command itself, read-only, reproduces brma_name
        rc, text, logged = e.cmd(["check-mapping"])
        if rc != 0 or f"brma_name: reproduced for {NA} of {NA}" not in text \
                or logged.called:
            problems.append(f"check-mapping: rc {rc}: {text[-120:]}")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: the crosswalk is reproduced for every authority; a "
          "changed brma_name or la_name, a missing authority and a code "
          "outside la_boundaries are each caught; the check-mapping "
          "command reproduces brma_name and writes nothing" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(7, name, False, sd)
    try:
        ok, detail = real_mapping(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(7, name, False, _first(ex))
    report(7, name, ok, f"{sd}; real: {detail}")


# ---------------------------------------------------------------- gate 8

def real_identity(files_dir=None):
    """The held CSV and BRMA layer read now: the CSV's header names the year
    of the held file and the older-layout files on disk are refused by name;
    the layer is the known one (zip sha256 a known vintage, layer, field,
    polygon count, per-geometry srsName)."""
    path, facts = read_held_csv(files_dir)
    bad = []
    if path is None:
        bad.append(facts)
        year = None
    else:
        year = facts["year"]
        if year != m.LEGACY_RATES_FILE["period"]:
            bad.append(f"{path.name}: the header names {year}, the held "
                       f"file is {m.LEGACY_RATES_FILE['period']}")
        if m.header_year_problems(facts, year):
            bad.append(f"{path.name}: a header cell names another year")
    older = []
    raw = Path(files_dir) if files_dir is not None else m.RAW_DIR
    for p in sorted(raw.glob("england-rates-*.csv")):
        if path is not None and p == path:
            continue
        try:
            m.read_csv(p)
        except ValueError as e:
            older.append(f"{p.name}: {_first(e)[:60]}")
    layer = ""
    if files_dir is None:
        gml = m.GML_FILE
        if not gml.is_file():
            bad.append(f"{gml}: not on disk")
        else:
            v = m.gml_vintage(m.content_sha256(gml))
            if v is None:
                bad.append("GML.zip is not a known BRMA layer")
            else:
                g = m.read_gml(gml)
                if (g["layer"], g["field"], len(g["names"])) != (
                        m.GML_LAYER, m.GML_FIELD, m.EXPECTED_POLYGONS):
                    bad.append(f"the layer is {g['layer']!r}, field "
                               f"{g['field']!r}, {len(g['names'])} polygons")
                layer = (f"; GML.zip is vintage {v}: layer {g['layer']!r}, "
                         f"field {g['field']!r}, {len(g['names'])} polygons")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{path.name}: header names {year}, equals the page year of the "
        f"held file; {len(older)} older-layout file(s) on disk refused "
        f"by name{layer}")


def gate_8_identity(cur):
    name = ("identity from the file: a seeded header-year, layout, '&' name, "
            "encoding or page mismatch halts")
    problems = []

    def body(e):
        bad_head = ["BRMA", "Monthly UC LHA rates 2024 to 2025 - SAR",
                    f"1 Bed {tl.YEARS}", f"2 Bed {tl.YEARS}",
                    f"3 Bed {tl.YEARS}", f"4 Bed {tl.YEARS}"]
        other = ["BRMA", f"Monthly UC LHA rates {tl.YEARS} - SAR",
                 f"1 Bed {tl.YEARS}", f"2 Bed {tl.YEARS}",
                 f"3 Bed {tl.YEARS}"]
        for label, kw, needle in (
            ("header year", {"header": bad_head}, "names 2024-25"),
            ("older layout", {"old_layout": True}, "older layout"),
            ("a column missing", {"header": other}, "the header row is"),
            ("undecodable bytes", {"raw": b"BRMA,\x81\x8d\x8f\x90\x9d\n"
                                   + b"x" * 300}, "neither UTF-8"),
        ):
            e.publish(**kw)
            before = state(e.cur)
            ok, text = e.halts(["load", "--only", "rates", "--commit"],
                               before)
            if not ok:
                problems.append(f"{label} did not halt cleanly: "
                                f"{text[-100:]}")
            elif needle not in text:
                problems.append(f"{label} did not name {needle!r}: "
                                f"{text[-100:]}")
        rates = dict(RATES)
        rates["Medway & Swale"] = rates.pop("Medway and Swale")
        e.publish(rates=rates)
        ok, text = e.halts(["load", "--only", "rates", "--commit"],
                           state(e.cur), "Medway & Swale")
        if not ok:
            problems.append(f"an '&' name did not halt: {text[-100:]}")
        # --accept-header-year passes the year label and is logged; naming
        # it when the header agrees is refused
        e.publish(header=bad_head)
        rc, text, logged = e.cmd(["load", "--only", "rates",
                                  "--accept-header-year", FY, "--commit"])
        if rc != 0 or "ACCEPTED HEADER YEAR" not in text or not \
                logged.called or "--accept-header-year" not in \
                logged.call_args[0][2]:
            problems.append(f"--accept-header-year: rc {rc}: {text[-100:]}")
        e.publish()
        rc, text, _ = e.cmd(["load", "--only", "rates", "--recheck", FY,
                             "--accept-header-year", FY])
        if rc != "halt" or "nothing to accept" not in text:
            problems.append("--accept-header-year with an agreeing header "
                            "was not refused")
        # a --release that disagrees with the page halts
        before = state(e.cur)
        path = e.local()
        ok, text = e.halts(["load", "--only", "rates", "--file", path,
                            "--no-page", "--release", "2025-26",
                            "--commit"], before)
        if not ok:
            problems.append("--file with another --release did not halt")
        # a layer that is not a known vintage, and one that is not a layer
        gml2 = write_gml(Path(e.root) / "gml2" / "GML.zip",
                         polygons_for()[::-1])
        ok, text = e.halts(["load", "--only", "mapping", "--gml", str(gml2),
                            "--commit"], state(e.cur),
                           "not a known BRMA layer")
        if not ok:
            problems.append(f"an unknown layer did not halt: {text[-100:]}")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a header naming another year, the older layout, a "
          "missing column, undecodable bytes, an '&' name, a --release that "
          "disagrees and an unknown layer each halt and store nothing; "
          "--accept-header-year releases only a genuine mismatch and is "
          "logged" if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(8, name, False, sd)
    try:
        ok, detail = real_identity()
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(8, name, False, _first(ex))
    report(8, name, ok, f"{sd}; real: {detail}")


# ---------------------------------------------------------------- gates 9-13

def gate_9_older_file(cur):
    name = ("older-file guard on the page, --release and --file paths; "
            "--allow-older-file overrides and is logged")
    problems = []

    def older(e, how):
        e.seed(e.cur)
        argv = ["load", "--only", "rates", "--commit"]
        if how == "file":
            path = e.local("2025 to 2026")
            return argv + ["--file", path, "--no-page"]
        e.publish(rates=tl.bump(RATES, "Ashford"),
                  history=[{"note": "x",
                            "public_timestamp": "2026-01-30T09:30:00Z"}])
        if how == "release":
            argv += ["--release", FY]
        return argv

    def body(e):
        for how in ("page", "release", "file"):
            def one(c, how=how):
                with env(c) as e2, e2.specs():
                    argv = older(e2, how)
                    before = state(c)
                    rc, text, logged = e2.cmd(argv)
                    if not (rc == "halt" and "older file" in text
                            and "skipped (older" in text
                            and state(c) == before and not logged.called):
                        problems.append(f"{how}: rc {rc}: {text[-160:]}")
            _in_savepoint(e.cur, one)

        def override(c):
            with env(c) as e2, e2.specs():
                argv = older(e2, "page") + ["--allow-older-file",
                                            "--acknowledge", FY]
                rc, text, logged = e2.cmd(argv)
                eds = tl.editions(c)
                if not (rc == 0 and eds == [(1, None, N), (2, 1, N)]
                        and logged.called and "--allow-older-file given"
                        in logged.call_args[0][2]):
                    problems.append(f"--allow-older-file: rc {rc}, {eds}")
        _in_savepoint(e.cur, override)

        def equal_rank(c):
            with env(c) as e2, e2.specs():
                e2.seed(c)
                e2.publish(rates=tl.bump(RATES, "Ashford"), history=[
                    {"note": "x", "public_timestamp": tl.TS}])
                before = state(c)
                rc, text, logged = e2.cmd(["load", "--only", "rates",
                                           "--commit", "--acknowledge", FY])
                if not (rc == 1 and "two files claim the same release"
                        in text and state(c) == before):
                    problems.append(f"equal rank, other content: rc {rc}: "
                                    f"{text[-120:]}")
                rc, text, logged = e2.cmd(["load", "--only", "rates",
                                           "--commit", "--acknowledge", FY,
                                           "--accept-reissue", FY])
                if rc != 0 or "ACCEPTED REISSUE" not in text or \
                        tl.editions(c) != [(1, None, N), (2, 1, N)]:
                    problems.append(f"--accept-reissue: rc {rc}: "
                                    f"{text[-120:]}")
        _in_savepoint(e.cur, equal_rank)

        def no_page(c):
            with env(c) as e2, e2.specs():
                e2.seed(c)
                path = e2.local(rates=tl.bump(RATES, "Kernow West", 2))
                rc, text, _ = e2.cmd(["load", "--only", "rates", "--file",
                                      path, "--no-page", "--acknowledge",
                                      FY])
                if not (rc == 1 and "no within-year rank" in text
                        and f"--accept-reissue {FY}" in text):
                    problems.append(f"--file --no-page with other content: "
                                    f"rc {rc}: {text[-120:]}")
        _in_savepoint(e.cur, no_page)
    scenario(cur, body)
    report(9, name, not problems, "the same year's page with an earlier "
           "change, offered by the page, --release and --file (2025-26), is "
           "skipped and halts with nothing stored or logged; the rank is "
           "the header year and the page's change timestamp, never the "
           "media id; --allow-older-file overrides and is logged; equal "
           "rank with other content stops unless --accept-reissue; --file "
           "--no-page has no within-year rank" if not problems
           else "; ".join(problems[:3]))


def rejected_state(cur, period, part="rates"):
    """The editions and live tables of both parts, and the ledger row count
    of the period (a rejected period stores nothing and writes no ledger
    row)."""
    led = RL if part == "rates" else ML
    col = "financial_year" if part == "rates" else "vintage"
    return tables_state(cur) + (cnt(cur, led, f"WHERE {col} = %s",
                                    (period,)),)


def rejects(e, argv, needle, before, *, commit=True, period=FY,
            part="rates"):
    """None if the command was REJECTED (exit 1) naming `needle`, stored
    nothing, wrote no ledger row, and (with --commit) wrote one partial
    run-log row; else the problem."""
    rc, text, logged = e.cmd(argv)
    if rc != 1 or "REJECTED" not in text or needle not in text:
        return f"rc {rc}, not REJECTED naming {needle!r}: {text[-140:]}"
    if rejected_state(e.cur, period, part) != before:
        return "something was stored or a ledger row written"
    if commit:
        if logged.call_count != 1:
            return "no partial run-log row"
        notes = logged.call_args[0][2]
        if "PARTIAL RUN (exit 1)" not in notes or \
                f"rejected ['{period}'" not in notes:
            return f"run-log notes: {notes[:120]}"
    elif logged.called:
        return "a preview wrote a run-log row"
    return None


def scaled(rates, factor):
    """RATES with every monthly amount multiplied by `factor` (a Decimal)."""
    out = {}
    for k, vals in rates.items():
        out[k] = tuple(f"{(Decimal(v.replace(',', '')) * factor).quantize(Decimal('0.01')):,}"
                       for v in vals)
    return out


def gate_10_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row; both sides of each limit")
    problems = []
    hist_later = [{"note": "x", "public_timestamp": "2026-05-01T10:00:00Z"}]

    def case(label, setup, needle, argv, commit=True, period=FY,
             part="rates"):
        def body(c):
            with env(c) as e, e.specs():
                setup(e)
                before = rejected_state(c, period, part)
                bad = rejects(e, ["load"] + argv + (["--commit"] if commit
                                                    else []), needle,
                              before, commit=commit, period=period,
                              part=part)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)

    def new_year(e, rates):
        e.seed(e.cur)
        e.publish("2027 to 2028", rates=rates)
    case("reissue: any change needs --acknowledge",
         lambda e: (e.seed(e.cur), e.publish(
             rates=tl.bump(RATES, "Ashford"), history=hist_later)),
         f"--acknowledge {FY}", ["--only", "rates"])
    case("reissue: a preview writes no run-log row",
         lambda e: (e.seed(e.cur), e.publish(
             rates=tl.bump(RATES, "Ashford"), history=hist_later)),
         f"--acknowledge {FY}", ["--only", "rates"], commit=False)
    case("new year: one BRMA moves by more than 25%",
         lambda e: new_year(e, tl.bump(RATES, "Ashford", by="200.00")),
         "Ashford", ["--only", "rates"], period="2027-28")
    case("new year: a rate type's median moves by more than 10%",
         lambda e: new_year(e, scaled(RATES, Decimal("1.11"))),
         "median", ["--only", "rates"], period="2027-28")
    case("new year: the BRMA set differs (KEY CHANGES)",
         lambda e: new_year(e, dict(RATES, Bath=("400.00",) * 5)),
         m.KEYS, ["--only", "rates", "--acknowledge", "2027-28"],
         period="2027-28")
    case("new year: a crosswalk BRMA has no rate row (JOIN, released by no "
         "flag)",
         lambda e: new_year(e, {k: v for k, v in RATES.items()
                                if k != "Kernow West"}),
         m.JOIN, ["--only", "rates", "--acknowledge", "2027-28",
                  "--accept-key-changes", "2027-28"], period="2027-28")

    def limits(c):
        with e_specs(c):
            def recs(scale):
                out = []
                for k, vals in RATES.items():
                    r = {"brma_name": k, "financial_year": FY,
                         **{col: Decimal(v.replace(",", "")) * scale
                            for col, v in zip(m.MONTHLY, vals)}}
                    for mo, wk in zip(m.MONTHLY, m.WEEKLY):
                        r[wk] = m.weekly(r[mo])
                    out.append(r)
                return out
            prev = recs(Decimal(1))
            # exactly 25% on one BRMA's one rate is fine, one penny more
            # is stopped
            at = [dict(r) for r in prev]
            at[0][m.MONTHLY[0]] = prev[0][m.MONTHLY[0]] * Decimal("1.25")
            if any("move by more than" in x for x in m.period_problems(
                    at, None, prev, kind="new")):
                problems.append("a move of exactly 25% was stopped")
            past = [dict(r) for r in at]
            past[0][m.MONTHLY[0]] += Decimal("0.01")
            if not any("move by more than" in x for x in m.period_problems(
                    past, None, prev, kind="new")):
                problems.append("a move past 25% was not stopped")
            # the median: exactly 10% is fine, a penny past it is stopped
            one = recs(Decimal("1.10"))
            if any("median" in x for x in m.period_problems(
                    one, None, prev, kind="new")):
                problems.append("a median move of exactly 10% was stopped")
            two = recs(Decimal("1.10"))
            for r in two:
                for col in m.MONTHLY:
                    r[col] += Decimal("0.05")
            if not any("median" in x for x in m.period_problems(
                    two, None, prev, kind="new")):
                problems.append("a median move past 10% was not stopped")
            # a reissue: any value changed is stopped, an identical one is not
            same = [dict(r) for r in prev]
            if m.period_problems(same, prev, None, kind="revised"):
                problems.append("an identical reissue was stopped")
            same[0][m.MONTHLY[1]] += Decimal("0.01")
            if not m.period_problems(same, prev, None, kind="revised"):
                problems.append("a reissue changing one rate by a penny was "
                                "not stopped")
            # the crosswalk: an authority set difference is a hard stop
            tip = [{"lad24cd": "E06000001", "la_name": "A", "brma_name": "B",
                    "mapping_method": "centroid_spatial_join"},
                   {"lad24cd": "E06000002", "la_name": "C", "brma_name": "D",
                    "mapping_method": "centroid_spatial_join"}]
            out = m.mapping_problems(tip[:1], tip)
            if not any(x.startswith(m.KEYS) for x in out):
                problems.append("a crosswalk missing an authority was not "
                                "a KEY CHANGES stop")
            if m.mapping_problems([dict(r) for r in tip], tip):
                problems.append("an identical crosswalk was stopped")
    _in_savepoint(cur, limits)
    report(10, name, not problems, "a reissue (any change), a new year with "
           "a BRMA beyond 25% or a median beyond 10%, a BRMA-set change and "
           "a crosswalk name without a rate row are each REJECTED (exit 1) "
           "with nothing stored, no ledger row and one partial run-log row "
           "(none in a preview); JOIN is released by no flag; every limit is "
           "exact on both sides; a missing crosswalk authority is a hard "
           "stop" if not problems else "; ".join(problems[:3]))


@contextmanager
def e_specs(cur):
    """The throwaway specs and fixture boundaries without an Env."""
    with tl.specs():
        yield


def gate_11_one_transaction(cur):
    name = ("a new period: edition, live rows and ledger row in one "
            "transaction")
    problems = []

    def good(e):
        e.publish()
        before = state(e.cur)
        rc, text, logged = e.cmd(["load", "--only", "rates", "--commit"])
        led = tl.ledger(e.cur)
        if rc != 0 or tl.editions(e.cur) != [(1, None, N)] \
                or tl.count(e.cur, ZR.live_table) != N \
                or [(o, ed) for _, _, o, ed in led] != [("new", 1)] \
                or state(e.cur) == before:
            problems.append(f"a new year did not store edition 1, {N} live "
                            f"rows and one 'new' ledger row: rc {rc} {led}")
        if not pe.status(e.cur, m.profile("rates"))["ok"]:
            problems.append("status not clean after the load")
        if core.rows_differing(e.cur, ZR, FY, 1):
            problems.append("live differs from edition 1")
        rc, text, _ = e.cmd(["load", "--only", "mapping", "--commit"])
        if rc != 0 or tl.editions(e.cur, tl.V, "zz_s14_map_editions",
                                  "vintage") != [(1, None, NA)] or \
                len(tl.ledger(e.cur, ML, "vintage")) != 1:
            problems.append(f"the crosswalk did not store edition 1 and one "
                            f"ledger row together: rc {rc}")

    def failing(label, target, attr):
        def body(c):
            with env(c) as e, e.specs():
                e.publish()
                before = state(c)
                with mock.patch.object(target, attr,
                                       side_effect=RuntimeError("boom")):
                    rc, text, logged = e.cmd(["load", "--only", "rates",
                                              "--commit"])
                if not (rc == 1 and state(c) == before
                        and f"{FY}: FAILED" in text and logged.called
                        and "PARTIAL RUN" in logged.call_args[0][2]):
                    problems.append(f"{label}: rc {rc}, state kept="
                                    f"{state(c) == before}")
        _in_savepoint(cur, body)
    scenario(cur, good)
    failing("a failing ledger insert", pe, "record_file_check")
    failing("a failing live insert", m, "insert_live")
    failing("a failing edition insert", core, "insert_edition")
    report(11, name, not problems, "edition 1, the live rows and one 'new' "
           "ledger row are stored together (rates and crosswalk); a failing "
           "ledger insert, live insert or edition insert rolls all three "
           "back and logs a partial run" if not problems
           else "; ".join(problems[:3]))


def gate_12_ledger_skip(cur):
    name = ("ledger skip needs the (source, sha256) pair; a changed file or "
            "--recheck is read")
    problems = []

    def body(e):
        e.publish()
        e.ok(e.cur, ["load", "--commit"])
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "nothing parsed" not in text or logged.called or \
                f"{tl.V}: skipped (checked" not in text:
            problems.append(f"a held pair did not skip the read: {text[-140:]}")
        # the same URL with other bytes: the sha differs, so it is read
        e.publish(rates=tl.bump(RATES, "Ashford"), history=[
            {"note": "x", "public_timestamp": tl.TS}])
        rc, text, _ = e.cmd(["load", "--only", "rates"])
        if "nothing parsed" in text or f"{FY}: REJECTED" not in text:
            problems.append("the same URL with other bytes was skipped")
        # the held pair again: skipped; --recheck reads it anyway
        e.publish()
        rc, text, _ = e.cmd(["load", "--only", "rates"])
        if "nothing parsed" not in text:
            problems.append("the held pair no longer skips")
        rc, text, _ = e.cmd(["load", "--only", "rates", "--recheck", FY])
        if f"{FY}: unchanged" not in text:
            problems.append("--recheck did not read a ledger-held file")
        # the crosswalk: another layer is read, the held pair is skipped
        rc, text, _ = e.cmd(["load", "--only", "mapping"])
        if f"{tl.V}: skipped (checked" not in text:
            problems.append("the held crosswalk layer did not skip")
        rc, text, _ = e.cmd(["load", "--only", "mapping", "--recheck", tl.V])
        if "skipped (checked" in text:
            problems.append("--recheck did not read the held crosswalk")
        # a stranded period is always read again
        e.cur.execute(f"DELETE FROM public.{ZR.live_table}")
        rc, text, _ = e.cmd(["load", "--only", "rates"])
        if "nothing parsed" in text or "live-missing" not in text:
            problems.append("a stranded period was skipped by the ledger")
    scenario(cur, body)
    report(12, name, not problems, "a held (source, sha256) pair skips the "
           "read with nothing parsed or logged; the same URL with other "
           "bytes is read; --recheck reads a held file and a held layer; a "
           "stranded period is read again" if not problems
           else "; ".join(problems[:3]))


def gate_13_preview(cur):
    name = "preview and simulate write nothing"
    problems = []
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]

    def body(e):
        e.publish()
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"new year {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.publish(rates=tl.bump(RATES, "Ashford"), history=[
            {"note": "x", "public_timestamp": "2026-05-01T10:00:00Z"}])
        before = state(e.cur)
        for argv in (["load", "--only", "rates"],
                     ["load", "--only", "rates", "--simulate"],
                     ["load", "--only", "rates", "--simulate",
                      "--acknowledge", FY],
                     ["load", "--only", "rates", "--acknowledge", FY],
                     ["status"], ["check-mapping"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}, state kept="
                                f"{state(e.cur) == before}")
        e.ok(e.cur, ["load", "--only", "rates", "--commit", "--acknowledge",
                     FY])
        before = state(e.cur)
        for argv in (["refresh-latest"],
                     ["restore-edition", "brma_lha_rates", FY, "1"],
                     ["refresh-latest", "--simulate"],
                     ["restore-edition", "brma_lha_rates", FY, "1",
                      "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"{argv[0]} after a reissue: rc {rc}")
    scenario(cur, body)

    def legacy(e, path, lg):
        before = state(e.cur)
        with patched_legacy(lg, path):
            for argv in (["migrate-legacy"], ["migrate-legacy",
                                              "--simulate"]):
                rc, text, logged = e.cmd(argv)
                if rc != 0 or state(e.cur) != before or logged.called:
                    problems.append(f"{argv}: rc {rc}")
    legacy_world(cur, legacy)
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    if cur.fetchone()[0] != runs:
        problems.append("the run log changed")
    report(13, name, not problems, "load (also with --acknowledge), status, "
           "check-mapping, refresh-latest, restore-edition and "
           "migrate-legacy in preview and --simulate leave the editions, "
           "the live tables and the ledgers as they were and log nothing"
           if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 14

def real_held_reread(cur, rates=REAL_R, raw_dir=None, sha=None):
    """The held CSV read again equals edition 1 'as loaded' of its year: the
    content sha256 of the rebuilt rows (every BRMA, ten rates) is edition
    1's; edition 1 names the file's rank; and the ledger records the
    byte-identical file as 'unchanged' for edition 1. So a re-read by the
    page, --release or --file is unchanged against edition 1 (and against
    the tip while the tip is edition 1)."""
    _need(cur, rates)
    path, facts = read_held_csv(raw_dir, sha)
    if path is None:
        return False, facts
    year = facts["year"]
    cur.execute(f"SELECT DISTINCT release_label, source_file FROM "
                f"public.{rates.editions_table} WHERE financial_year = %s "
                "AND edition = 1", (year,))
    rows = cur.fetchall()
    if len(rows) != 1:
        return False, f"{year}: edition 1 missing or not single"
    label, sf = rows[0]
    held = m.records(cur, rates, year, 1)
    built = m.build_rates(facts, year)
    a, b = m.rows_content_sha(held), m.rows_content_sha(built)
    bad = []
    if a != b:
        bad.append(f"{year}: edition 1 content sha {a[:12]} differs from "
                   f"the held CSV read now {b[:12]}")
    if label != core.AS_LOADED_LABEL:
        bad.append(f"{year}: edition 1 is not 'as loaded' ({label!r})")
    want = m.LEGACY_RATES_FILE["rank"] if sha is None else None
    got = m.release_rank(sf)
    if want is not None and got != want:
        bad.append(f"{year}: edition 1 source_file ranks {got}, the file "
                   f"{want}")
    if got is None:
        bad.append(f"{year}: edition 1 names no rank")
    cur.execute(f"SELECT outcome, edition FROM public."
                f"{ledger_name(rates)} WHERE financial_year = %s AND "
                "file_sha256 = %s", (year, m.content_sha256(path)))
    if ("unchanged", 1) not in cur.fetchall():
        bad.append(f"{year}: no 'unchanged' ledger row for the held CSV")
    tip = _tip(cur, rates, year)
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{path.name} read now has edition 1's content sha256 ({a[:12]}.., "
        f"{len(held)} rows), edition 1 names the file's rank, the ledger "
        f"holds it 'unchanged'; the tip is edition {tip}, so a re-read is "
        + ("unchanged against the tip" if tip == 1
           else "compared with edition 1 and the tip"))


def reread_after_migration(e, path):
    """Problems (a list) when the migrated state does not treat the same
    bytes as unchanged on every path."""
    bad = []
    rc, text, logged = e.cmd(["load", "--only", "rates"])
    if rc != 0 or "nothing parsed" not in text or logged.called:
        bad.append(f"the page load after migrate-legacy: rc {rc}: "
                   f"{text[-100:]}")
    for argv in (["load", "--only", "rates", "--recheck", FY],
                 ["load", "--only", "rates", "--release", FY, "--recheck",
                  FY],
                 ["load", "--only", "rates", "--file", path],
                 ["load", "--only", "rates", "--file", path, "--no-page"],
                 ["load", "--only", "rates", "--file", path, "--commit"],
                 ["load", "--only", "rates", "--file", path, "--no-page",
                  "--recheck", FY, "--commit"],
                 ["load", "--only", "rates", "--recheck", FY, "--commit"]):
        rc, text, logged = e.cmd(argv)
        if rc != 0 or f"{FY}: unchanged" not in text or "REJECTED" in text:
            bad.append(f"{' '.join(map(str, argv[1:]))}: rc {rc}: "
                       f"{text[-100:]}")
    if tl.editions(e.cur) != [(1, None, N)]:
        bad.append(f"a re-read stored an edition: {tl.editions(e.cur)}")
    if {o for _, _, o, _ in tl.ledger(e.cur)} != {"unchanged"}:
        bad.append("a re-read ledger row is not 'unchanged'")
    rc, text, _ = e.cmd(["refresh-latest"])
    if "would write: none" not in text:
        bad.append("refresh-latest plans a write after the re-reads")
    return bad


def gate_14_held_file_recheck(cur):
    name = ("a byte-identical re-read is unchanged on every path against "
            "edition 1 'as loaded'")
    problems = []
    out = {}

    def held(e, path, legacy):
        with patched_legacy(legacy, path):
            rc, text, _ = e.cmd(["migrate-legacy", "--commit"])
        if rc != 0:
            problems.append(f"migrate-legacy: {text[-120:]}")
            return
        d = Path(e.root) / "disk"
        d.mkdir()
        (d / path.name).write_bytes(path.read_bytes())
        out["held"] = real_held_reread(e.cur, ZR, d, m.content_sha256(path))
        problems.extend(f"held: {x}"
                        for x in reread_after_migration(e, path))
    legacy_world(cur, held)
    if not out.get("held", (False,))[0]:
        problems.append(f"held: {out.get('held')}")

    def changed(e, path, legacy):
        with patched_legacy(legacy, path):
            e.cmd(["migrate-legacy", "--commit"])
        other = e.local(rates=tl.bump(RATES, "Ashford"))
        ok, detail = real_held_reread(e.cur, ZR, other.parent,
                                      m.content_sha256(other))
        if ok or "differs" not in detail:
            problems.append("a file with other content was called equal")
        ok, detail = real_held_reread(e.cur, ZR, other.parent, "0" * 64)
        if ok or "no CSV" not in detail:
            problems.append("a missing file was called equal")
    legacy_world(cur, changed)
    seeded = not problems
    sd = ("seeded: after migrate-legacy the same bytes read by the page, "
          "--release, --file, --file --no-page, --recheck and with --commit "
          "are unchanged, store no edition and leave every ledger row "
          "'unchanged'; a file with other content or none is not equal"
          if seeded else "; ".join(problems[:3]))
    mixed(14, name, seeded, sd, cur, real_held_reread)


# ---------------------------------------------------------------- gate 15

def real_vintages(cur, mapping=REAL_M):
    """Live holds exactly one vintage, LIVE_VINTAGE, and it has edition 1; a
    vintage that is in the editions table and not in live is held, not live
    (listed)."""
    _need(cur, mapping)
    if not m.live_has_vintage(cur, mapping):
        return False, f"{mapping.live_table}.vintage does not exist"
    cur.execute(f"SELECT DISTINCT vintage FROM public.{mapping.live_table}")
    live = sorted(r[0] for r in cur.fetchall())
    cur.execute(f"SELECT DISTINCT vintage FROM public."
                f"{mapping.editions_table}")
    held = sorted(r[0] for r in cur.fetchall())
    bad = []
    if live != [V]:
        bad.append(f"live holds vintages {live}, expected [{V!r}]")
    if V not in held:
        bad.append(f"the live vintage {V} has no edition")
    cur.execute(f"SELECT COUNT(*) FROM public.{mapping.live_table} WHERE "
                f"vintage <> %s", (V,))
    if cur.fetchone()[0]:
        bad.append("a live row has another vintage")
    other = [v for v in held if v not in live]
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live holds only {V}; held, not live: "
        f"{other or 'none'}; the live vintage column is CHECKed to {V}")


def gate_15_second_vintage(cur):
    name = ("the mapping spec never takes a second vintage into live (held, "
            "not live)")
    problems = []

    def body(e):
        e.seed(e.cur)
        other = "brma-2027-05_lad-2024-05"
        gml2 = write_gml(Path(e.root) / "gml2" / "GML.zip",
                         polygons_for()[::-1])
        borrowed = tl._Borrowed(e.cur)
        extra = {other: dict(m.VINTAGES[tl.V], gml_sha256=m.content_sha256(
            gml2), boundaries=tl._STATE(tl.boundaries()))}
        out = io.StringIO()
        with e.specs(extra), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "log_run"), \
                contextlib.redirect_stdout(out):
            try:
                rcs = [m.main(["load", "--only", "mapping", "--gml",
                               str(gml2), "--commit"]),
                       m.main(["refresh-latest", "--commit"]),
                       m.main(["status"])]
            finally:
                borrowed.tx.done()
        text = out.getvalue()
        OUTPUT.extend(text.splitlines())
        if rcs != [0, 0, 0] or "held-not-live" not in text or \
                f"held, not live: {other}" not in text:
            problems.append(f"the second vintage: rc {rcs}: {text[-160:]}")
        if tl.editions(e.cur, other, "zz_s14_map_editions", "vintage") != [
                (1, None, NA)]:
            problems.append("the second vintage was not stored as edition 1")
        e.cur.execute(f"SELECT DISTINCT vintage FROM public.{ZM.live_table}")
        if e.cur.fetchall() != [(tl.V,)]:
            problems.append("live took the second vintage")
        ok, detail = real_vintages(e.cur, ZM)
        if not ok or other not in detail:
            problems.append(f"real_vintages on the seeded state: {detail}")
        # a live row of another vintage is refused by the table itself
        e.cur.execute("SAVEPOINT plant")
        try:
            e.cur.execute(f"UPDATE public.{ZM.live_table} SET vintage = "
                          f"'{other}'")
            problems.append("live accepted another vintage")
        except psycopg2.Error:
            pass
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
    scenario(cur, body)
    mixed(15, name, not problems, "seeded: a layer of another vintage is "
          "stored as editions only ('held, not live'), refresh-latest and "
          "status leave live on the one vintage, and the live vintage "
          "column refuses another value" if not problems
          else "; ".join(problems[:3]), cur, real_vintages)


# ---------------------------------------------------------------- gate 16

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def _cellv(v):
    return "" if v is None else str(v)


def all_sha(records, cols):
    """sha256 of every column an edition keeps (the live source text
    included): the key, the period, `cols` and the source text, one line
    per row sorted, '|'-joined, LF, UTF-8 (the before-state-all record)."""
    lines = sorted("|".join(_cellv(r[c]) for c in cols) for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


RATE_ALL = (m.RATE_KEY, m.FY_COL) + m.RATE_VALUES + (m.SOURCE_TEXT,)
MAP_ALL = (m.MAP_KEY,) + m.MAP_VALUES + (m.SOURCE_TEXT,)


def w1_rates_sha(records):
    """sha256 of what W1 reads of the rates: brma_name and the five weekly
    rates (sar, 1 to 4 bed; two places), one line per BRMA sorted, LF,
    UTF-8."""
    lines = sorted("|".join([r["brma_name"]] + [
        str(Decimal(r[c]).quantize(m.CENT)) for c in m.WEEKLY])
        for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def w1_map_sha(records):
    """sha256 of what W1 reads of the crosswalk: lad24cd|brma_name, sorted,
    LF, UTF-8."""
    lines = sorted(f"{r['lad24cd']}|{r['brma_name']}" for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _note_lines(note, label, table):
    """{period or '': (rows, sha256)} from lines `<label> <table> [<period>]
    rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the decision
    note (the two halves joined), or {}. A line with another label (for
    example before-state-all) or table is a different record and does not
    match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {(mt.group(1) or "").strip(): (int(mt.group(2)),
                                          mt.group(3) + mt.group(4))
            for mt in re.finditer(
                rf"(?m)^{re.escape(label)} {re.escape(table)}"
                r"((?: [A-Za-z0-9_-]+)?) rows=([0-9]+) "
                r"sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text)}


def note_lines_for(cur, rates=REAL_R, mapping=REAL_M):
    """The decision note's hash lines for the live tables as they stand
    (read-only): before-state, before-state-all and w1-read of the rates
    (newest financial year) and of the crosswalk. Print them before the
    migration."""
    out = []
    periods = _live_periods(cur, rates)
    mperiods = _live_periods(cur, mapping)
    for p in periods:
        recs = m.records(cur, rates, p)
        out.append(f"before-state {RATES_T} {p} rows={len(recs)} "
                   f"{hash_fields(m.rows_content_sha(recs))}")
    for p in mperiods:
        recs = m.records(cur, mapping, p)
        out.append(f"before-state {MAP_T} {p} rows={len(recs)} "
                   f"{hash_fields(m.mapping_content_sha(recs))}")
    allr = [r for p in periods for r in m.records(cur, rates, p)]
    out.append(f"before-state-all {RATES_T} rows={len(allr)} "
               f"{hash_fields(all_sha(allr, RATE_ALL))}")
    allm = [r for p in mperiods for r in m.records(cur, mapping, p)]
    out.append(f"before-state-all {MAP_T} rows={len(allm)} "
               f"{hash_fields(all_sha(allm, MAP_ALL))}")
    if periods:
        recs = m.records(cur, rates, periods[-1])
        out.append(f"w1-read {RATES_T} {periods[-1]} rows={len(recs)} "
                   f"{hash_fields(w1_rates_sha(recs))}")
    if mperiods:
        out.append(f"w1-read {MAP_T} rows={len(allm)} "
                   f"{hash_fields(w1_map_sha(allm))}")
    return out


def real_edition1_hash(cur, rates=REAL_R, mapping=REAL_M, note=NOTE,
                       legacy=None):
    """Edition 1 'as loaded' of each table equals the before-state line of
    the decision note: the row count and the loader's content sha256, and
    the before-state-all line (every column edition 1 keeps, the live source
    text included); the row counts are the surveyed live counts."""
    _need(cur, rates, mapping)
    legacy = legacy or (m.LEGACY_RATES[0], m.LEGACY_MAPPING[0])
    bad, parts = [], []
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    for spec, table, sha_fn, cols, want_rows in (
            (rates, RATES_T, m.rows_content_sha, RATE_ALL, legacy[0]),
            (mapping, MAP_T, m.mapping_content_sha, MAP_ALL, legacy[1])):
        recorded = _note_lines(note, "before-state", table)
        cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                    f"public.{spec.editions_table} WHERE edition = 1 AND "
                    "release_label = %s ORDER BY 1", (core.AS_LOADED_LABEL,))
        migrated = [str(r[0]) for r in cur.fetchall()]
        if not migrated:
            bad.append(f"{table}: no edition 1 'as loaded' (migrate-legacy "
                       "not run)")
        total, allrecs = 0, []
        for p in migrated:
            recs = m.records(cur, spec, p, 1)
            n, h = len(recs), sha_fn(recs)
            total += n
            allrecs += recs
            if Path(note).exists():
                if p not in recorded:
                    bad.append(f"the decision note records no 'before-state "
                               f"{table} {p} rows=.. sha256-first32=.. "
                               "sha256-last32=..' line")
                elif recorded[p] != (n, h):
                    bad.append(f"{table} {p}: edition 1 {n} rows sha256 "
                               f"{h[:16]}.. differs from the note's "
                               f"{recorded[p][0]} rows {recorded[p][1][:16]}"
                               "..")
            parts.append(f"{table} {p} {n:,} rows {h[:16]}..")
        if migrated and total != want_rows:
            bad.append(f"{table}: edition 1 holds {total} rows, surveyed "
                       f"{want_rows}")
        for p in recorded:
            if p not in migrated:
                bad.append(f"the note has a before-state line for {table} "
                           f"{p}, which has no edition 1 as loaded")
        allrec = _note_lines(note, "before-state-all", table).get("")
        if Path(note).exists():
            if allrec is None:
                bad.append(f"the decision note records no 'before-state-all "
                           f"{table} rows=.. ..' line")
            elif migrated and allrec != (len(allrecs),
                                         all_sha(allrecs, cols)):
                bad.append(f"{table}: the note's before-state-all line "
                           "differs from edition 1")
    return not bad, "; ".join(bad[:3]) if bad else (
        "edition 1 of both tables hashes to the before-state lines in the "
        "decision note, source text included (" + "; ".join(parts) + ")")


def gate_16_before_state_hash(cur):
    name = ("edition 1 equals the before-state hashes recorded in the "
            "decision note")
    out = {}

    def body(e, path, legacy):
        lines = note_lines_for(e.cur, ZR, ZM)
        with patched_legacy(legacy, path):
            rc, text, _ = e.cmd(["migrate-legacy", "--commit"])
        if rc != 0:
            out["error"] = text[-200:]
            return
        legacy_n = (legacy["rates"][0], legacy["mapping"][0])
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            note.write_text("intro\n" + "\n".join(lines) + "\n",
                            encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZR, ZM, note, legacy_n)
            body_ = note.read_text()
            first = next(x for x in lines if x.startswith("before-state "))
            note.write_text(body_.replace(first.split()[-2][-12:],
                                          "0" * 12), encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZR, ZM, note,
                                                 legacy_n)
            note.write_text("\n".join(x for x in lines
                                     if not x.startswith("before-state ")),
                            encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZR, ZM, note,
                                                legacy_n)
            note.write_text("\n".join(x for x in lines
                                      if not x.startswith("before-state-all")
                                      ), encoding="utf-8")
            out["no_all"] = real_edition1_hash(e.cur, ZR, ZM, note, legacy_n)
            note.write_text(body_.replace(" rows=", " rows=1", 1),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZR, ZM, note,
                                                 legacy_n)
            out["no_note"] = real_edition1_hash(e.cur, ZR, ZM,
                                                Path(tmp) / "x", legacy_n)
            note.write_text(body_, encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(
                e.cur, ZR, ZM, note, (legacy_n[0] + 5, legacy_n[1]))
        # the note's lines hash the live rows exactly as edition 1
        out["same"] = (m.rows_content_sha(m.records(e.cur, ZR, FY))
                       == m.rows_content_sha(m.records(e.cur, ZR, FY, 1)))
        e.cur.execute(f"UPDATE public.{ZR.live_table} SET sar_monthly = "
                      "sar_monthly + 1 WHERE brma_name = 'Ashford'")
        out["changed"] = (m.rows_content_sha(m.records(e.cur, ZR, FY))
                          != m.rows_content_sha(m.records(e.cur, ZR, FY, 1)))
        with patched_legacy(legacy, path):
            rc, text, _ = e.cmd(["migrate-legacy", "--commit"])
        out["twice"] = rc == "halt" and ("runs once" in text
                                         or "not as surveyed" in text)
        out["lines"] = lines
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(16, name, False, _first(ex))
    lines = out.get("lines", [])
    scan_ok = bool(lines) and all(
        not re.search(r"[0-9a-f]{64}", x) and re.search(
            r"sha256-first32=[0-9a-f]{32} sha256-last32=[0-9a-f]{32}$", x)
        for x in lines)
    ok = (out.get("same") and out.get("ok", (0,))[0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["no_all"][0] and not out["bad_rows"][0]
          and not out["no_note"][0] and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and out.get("changed")
          and out.get("twice") and scan_ok)
    mixed(16, name, ok,
          "a seeded migration stores an edition 1 whose hashes equal the "
          "note's before-state and before-state-all lines (both tables, "
          "source text included); a wrong hash, a wrong row count, a "
          "missing line, a missing before-state-all line and a missing note "
          "fail; a later change shows; a second migration is refused; the "
          "lines are scan-safe" if ok else f"seeded: {out}", cur,
          real_edition1_hash)


# ---------------------------------------------------------------- gate 17

def real_w1_read(cur, rates=REAL_R, mapping=REAL_M, note=NOTE):
    """What W1 reads equals the decision note's w1-read lines: the live
    rates of the year in the line (the five weekly rates per BRMA) and the
    live crosswalk's brma_name per authority. The first load must move
    neither. (sql/w1/05_la_signals.sql reads lbm.brma_name and
    lha.*_weekly at MAX financial_year.)"""
    if not Path(note).exists():
        return False, f"decision note {Path(note).name} not written"
    bad, parts = [], []
    rl = _note_lines(note, "w1-read", RATES_T)
    if not rl:
        bad.append(f"the decision note records no 'w1-read {RATES_T} <fy> "
                   "rows=.. sha256-first32=.. sha256-last32=..' line")
    for fy, (rows, sha) in rl.items():
        recs = m.records(cur, rates, fy)
        n, h = len(recs), w1_rates_sha(recs)
        if (n, h) != (rows, sha):
            bad.append(f"live {RATES_T} {fy}: {n} rows sha256 {h[:16]}.. "
                       f"differs from the note's w1-read line {rows} rows "
                       f"{sha[:16]}..")
        else:
            parts.append(f"{RATES_T} {fy} {n} rows ({h[:16]}..)")
    cur.execute(f"SELECT MAX(financial_year) FROM public.{rates.live_table}")
    newest = cur.fetchone()[0]
    ml = _note_lines(note, "w1-read", MAP_T)
    if not ml:
        bad.append(f"the decision note records no 'w1-read {MAP_T} rows=.. "
                   "sha256-first32=.. sha256-last32=..' line")
    for _, (rows, sha) in ml.items():
        recs = m.records(cur, mapping, V)
        n, h = len(recs), w1_map_sha(recs)
        if (n, h) != (rows, sha):
            bad.append(f"live {MAP_T}: {n} rows sha256 {h[:16]}.. differs "
                       f"from the note's w1-read line {rows} rows "
                       f"{sha[:16]}..")
        else:
            parts.append(f"{MAP_T} {n} rows ({h[:16]}..)")
    return not bad, "; ".join(bad[:3]) if bad else (
        "what W1 reads hashes to the note's w1-read lines ("
        + "; ".join(parts) + f"); W1 reads the newest year, {newest}")


def gate_17_w1_read(cur):
    name = ("the weekly rates and brma_name W1 reads equal the decision "
            "note's w1-read lines")
    out = {}

    def body(e):
        e.seed(e.cur)
        lines = [x for x in note_lines_for(e.cur, ZR, ZM)
                 if x.startswith("w1-read")]
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            note.write_text("\n".join(lines) + "\n", encoding="utf-8")
            kw = {"note": note}
            out["base"] = real_w1_read(e.cur, ZR, ZM, **kw)
            # a monthly rate and the provenance text are not what W1 reads
            e.cur.execute(f"UPDATE public.{ZR.live_table} SET source = 'x', "
                          "sar_monthly = sar_monthly + 1")
            e.cur.execute(f"UPDATE public.{ZM.live_table} SET "
                          "mapping_method = 'NaN', source = 'x'")
            out["unread"] = real_w1_read(e.cur, ZR, ZM, **kw)
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(f"UPDATE public.{ZR.live_table} SET two_bed_weekly"
                          " = two_bed_weekly + 0.01 WHERE brma_name = "
                          "'Ashford'")
            out["weekly"] = real_w1_read(e.cur, ZR, ZM, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(f"UPDATE public.{ZM.live_table} SET brma_name = "
                          "'Ashford' WHERE lad24cd = 'E06000010'")
            out["brma"] = real_w1_read(e.cur, ZR, ZM, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(f"DELETE FROM public.{ZM.live_table} WHERE "
                          "lad24cd = 'E06000010'")
            out["gone"] = real_w1_read(e.cur, ZR, ZM, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            note.write_text("", encoding="utf-8")
            out["no_line"] = real_w1_read(e.cur, ZR, ZM, **kw)
            note.write_text("\n".join(x.replace("w1-read", "w1-read-all")
                                      for x in lines), encoding="utf-8")
            out["other_label"] = real_w1_read(e.cur, ZR, ZM, **kw)
            note.write_text("\n".join(lines[:1]) + "\n", encoding="utf-8")
            out["only_rates"] = real_w1_read(e.cur, ZR, ZM, **kw)
            out["no_note"] = real_w1_read(e.cur, ZR, ZM,
                                          note=Path(tmp) / "x")
        out["lines"] = lines
    scenario(cur, body)
    ok = (out["base"][0] and out["unread"][0]
          and not out["weekly"][0] and not out["brma"][0]
          and not out["gone"][0] and not out["no_line"][0]
          and not out["other_label"][0] and not out["only_rates"][0]
          and not out["no_note"][0] and "not written" in out["no_note"][1]
          and len(out["lines"]) == 2)
    mixed(17, name, ok, "the seeded lines match the live rates and "
          "crosswalk; a changed weekly rate, a changed brma_name, a missing "
          "authority, a missing or mislabelled line and a missing note "
          "fail; a changed monthly rate, source text or mapping_method does "
          "not move what W1 reads" if ok else f"seeded: {out}", cur,
          real_w1_read)


# ---------------------------------------------------------------- gate 18

def real_migration_proof(cur, rates=REAL_R, mapping=REAL_M, raw_dir=None,
                         sha=None, gml=None, method_form=None):
    """The migration proofs re-run read-only. Rates: the held CSV (found by
    sha256) read now reproduces every value of edition 1 'as loaded', the
    five monthly as published and the five weekly as derived (0
    differences). Crosswalk: the rebuild from GML.zip and la_boundaries
    reproduces edition 1's brma_name and la_name for every authority, and
    its mapping_method but for the named held form."""
    _need(cur, rates, mapping)
    path, facts = read_held_csv(raw_dir, sha)
    if path is None:
        return False, facts
    year = facts["year"]
    held = {r["brma_name"]: r for r in m.records(cur, rates, year, 1)}
    if not held:
        return False, f"{year}: no edition 1"
    built = {r["brma_name"]: r for r in m.build_rates(facts, year)}
    diffs = [("only held", k) for k in sorted(set(held) - set(built))]
    diffs += [("only file", k) for k in sorted(set(built) - set(held))]
    cells = 0
    for k in sorted(set(held) & set(built)):
        for c in m.RATE_VALUES:
            cells += 1
            if Decimal(held[k][c]) != built[k][c]:
                diffs.append((k, c, held[k][c], built[k][c]))
    if diffs:
        return False, f"{year}: {len(diffs)} differences, e.g. {diffs[:2]}"
    cur.execute(f"SELECT DISTINCT release_label FROM public."
                f"{rates.editions_table} WHERE financial_year = %s AND "
                "edition = 1", (year,))
    if cur.fetchall() != [(core.AS_LOADED_LABEL,)]:
        return False, f"{year}: edition 1 is not the 'as loaded' one"
    fy, names = year, set(built)
    path_g = Path(gml) if gml else m.GML_FILE
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            v, rebuilt, _ = m.rebuild_mapping(cur, path_g, names)
        except SystemExit as e:
            return False, f"crosswalk rebuild: {_first(e)}"
    mheld = {r["lad24cd"]: r for r in m.records(cur, mapping, v, 1)}
    mnew = {r["lad24cd"]: r for r in rebuilt}
    mdiffs = [("only held", k) for k in sorted(set(mheld) - set(mnew))]
    mdiffs += [("only rebuilt", k) for k in sorted(set(mnew) - set(mheld))]
    form = m.LEGACY_METHOD_FORM if method_form is None else method_form
    forms = 0
    mcells = 0
    for k in sorted(set(mheld) & set(mnew)):
        for c in m.MAP_VALUES:
            mcells += 1
            a, b = mheld[k][c], mnew[k][c]
            if a == b:
                continue
            if c == "mapping_method" and (a, b) == (form[0], form[1]):
                forms += 1
                continue
            mdiffs.append((k, c, a, b))
    if mdiffs:
        return False, (f"crosswalk: {len(mdiffs)} differences, e.g. "
                       f"{mdiffs[:2]}")
    if forms != form[2]:
        return False, (f"crosswalk: {forms} rows hold {form[0]!r} where the "
                       f"rebuild says {form[1]!r}, expected {form[2]}")
    return True, (f"{cells:,} rate values of edition 1 ({len(held)} BRMAs) "
                  f"equal {path.name} read now (0 differences); the "
                  f"crosswalk rebuild from {path_g.name} reproduces "
                  f"{mcells:,} values of edition 1 ({len(mheld)} "
                  f"authorities) but {forms} held {form[0]!r} "
                  "mapping_method(s), named")


def gate_18_migration_proof(cur):
    name = ("the migration proofs re-run read-only from the files on disk; "
            "seeded: proofs pass, planted held differences stop "
            "migrate-legacy and store nothing")
    problems = []

    def ok_world(e, path, legacy):
        with patched_legacy(legacy, path):
            rc, text, _ = e.cmd(["migrate-legacy", "--commit"])
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        raw = Path(e.root) / "raw"
        raw.mkdir()
        (raw / path.name).write_bytes(path.read_bytes())
        sha = m.content_sha256(path)
        form = legacy["method_form"]
        ok, detail = real_migration_proof(e.cur, ZR, ZM, raw, sha, e.gml,
                                          form)
        if not ok or "0 differences" not in detail:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(e.cur, ZR, ZM, raw, "0" * 64,
                                          e.gml, form)
        if ok or "no CSV" not in detail:
            problems.append("a file with another sha256 was accepted")
        ok, detail = real_migration_proof(e.cur, ZR, ZM, raw, sha, e.gml,
                                          (form[0], form[1], form[2] + 1))
        if ok or "expected" not in detail:
            problems.append("a wrong held-form count was accepted")
        other = e.local(rates=tl.bump(RATES, "Ashford"))
        ok, detail = real_migration_proof(
            e.cur, ZR, ZM, other.parent, m.content_sha256(other), e.gml,
            form)
        if ok or "differences" not in detail:
            problems.append("a CSV that differs from edition 1 passed")

    def planted(sql, needle):
        def f(e, path, legacy):
            e.cur.execute(sql)
            lg = e.seed_legacy_state(e.cur, path)[1]
            before = state(e.cur)
            with patched_legacy(lg, path):
                rc, text, logged = e.cmd(["migrate-legacy", "--commit"])
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        f"UPDATE public.{ZR.live_table} SET sar_weekly = sar_weekly + 0.01 "
        "WHERE brma_name = 'Ashford'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZR.live_table} SET one_bed_monthly = "
        "one_bed_monthly + 1 WHERE brma_name = 'Ashford'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZM.live_table} SET brma_name = 'Ashford' WHERE "
        "lad24cd = 'E06000010'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZM.live_table} SET la_name = 'x' WHERE lad24cd = "
        "'E06000010'", "proof failed"))
    mixed(18, name, not problems,
          "seeded migration proofs pass (0 differences) and re-run from the "
          "files by sha256; a missing CSV, a wrong held-form count and a "
          "CSV that is not edition 1 are caught; planted differences in a "
          "weekly or monthly rate, a brma_name and a la_name stop "
          "migrate-legacy with nothing stored" if not problems
          else "; ".join(problems[:3]), cur, real_migration_proof)


# ---------------------------------------------------------- gates 19 - 21

def gate_19_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        e.publish()
        path = e.local()
        e.ok(e.cur, ["load", "--commit"])
        snap, led = tables_state(e.cur), len(tl.ledger(e.cur))
        for argv in (["load", "--commit"],
                     ["load", "--release", FY, "--commit"],
                     ["load", "--only", "rates", "--file", path,
                      "--commit"],
                     ["load", "--only", "rates", "--file", path,
                      "--no-page", "--commit"],
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
        rc, text, logged = e.cmd(["load", "--only", "rates", "--recheck",
                                  FY, "--commit"])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N)]:
            problems.append(f"a recheck stored an edition: "
                            f"{tl.editions(e.cur)}")
    scenario(cur, body)
    report(19, name, not problems, "a second load by the page, --release, "
           "--file and --file --no-page and a refresh-latest stored no "
           "edition and changed no live row (re-reads add 'unchanged' "
           "ledger rows only); a ledger-skipped load logs nothing"
           if not problems else "; ".join(problems[:3]))


def gate_20_stranded(cur):
    name = "stranded period repair (edition held, live rows missing)"
    problems = []

    def body(e):
        e.seed(e.cur)
        e.cur.execute(f"DELETE FROM public.{ZM.live_table}")
        if pe.status(e.cur, m.profile("mapping"))["live_missing"] != [tl.V]:
            problems.append("status does not name the stranded crosswalk")
        rc, text, _ = e.cmd(["load", "--only", "mapping", "--commit"])
        if rc != 0 or "live-missing" not in text or \
                tl.count(e.cur, ZM.live_table) != NA:
            problems.append(f"crosswalk repair failed: rc {rc}: "
                            f"{text[-120:]}")
        e.cur.execute(f"DELETE FROM public.{ZR.live_table}")
        if pe.status(e.cur, m.profile("rates"))["live_missing"] != [FY]:
            problems.append("status does not name the stranded rates")
        rc, text, _ = e.cmd(["load", "--only", "rates", "--commit"])
        if rc != 0 or "live-missing" not in text or \
                tl.editions(e.cur) != [(1, None, N)] or \
                tl.count(e.cur, ZR.live_table) != N:
            problems.append(f"rates repair failed: rc {rc}: {text[-120:]}")
        if "live-missing" not in [o for _, _, o, _ in tl.ledger(e.cur)]:
            problems.append("no live-missing ledger row")
        if core.rows_differing(e.cur, ZR, FY, 1) or core.rows_differing(
                e.cur, ZM, tl.V, 1) or not pe.status(
                    e.cur, m.profile("rates"))["ok"] or not pe.status(
                    e.cur, m.profile("mapping"))["ok"]:
            problems.append("the repaired rows do not equal the tip")
    scenario(cur, body)
    report(20, name, not problems, f"a stranded crosswalk and a stranded "
           f"{FY} were each rebuilt from their edition 1 with no new "
           "edition and live-missing ledger rows" if not problems
           else "; ".join(problems[:3]))


def gate_21_restore(cur):
    name = "restore-edition round trip (rates and crosswalk)"
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.rate(e.cur)
        e.publish(rates=tl.bump(RATES, "Ashford"), history=[
            {"note": "x", "public_timestamp": "2026-05-01T10:00:00Z"}])
        e.ok(e.cur, ["load", "--only", "rates", "--commit", "--acknowledge",
                     FY])
        e.ok(e.cur, ["refresh-latest", "--commit"])
        snap = state(e.cur)
        rc, text, logged = e.cmd(["restore-edition", "brma_lha_rates", FY,
                                  "1"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("restore-edition preview wrote")
        rc, text, logged = e.cmd(["restore-edition", "brma_lha_rates", FY,
                                  "1", "--commit"])
        if rc != 0 or tl.editions(e.cur)[-1] != (3, 2, N) or \
                not logged.called:
            problems.append(f"restore --commit: rc {rc}: {text[-120:]}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZR.editions_table} WHERE edition = 3")
        if e.cur.fetchall() != [("restored from edition 1",)]:
            problems.append("the restored edition is not labelled")
        if tl.rate(e.cur) != first + 1:
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.rate(e.cur) != first or core.rows_differing(e.cur, ZR, FY, 3):
            problems.append("refresh-latest did not apply the restored "
                            "edition")
        if tl.rate(e.cur, col="sar_weekly") != m.weekly(first):
            problems.append("the restored weekly rate is not derived")
        ed1 = {tuple(sorted((k, str(v)) for k, v in r.items()))
               for r in m.records(e.cur, ZR, FY, 1)}
        ed3 = {tuple(sorted((k, str(v)) for k, v in r.items()))
               for r in m.records(e.cur, ZR, FY, 3)}
        if ed1 != ed3:
            problems.append("edition 3 does not hold edition 1's rows")
        for argv in (["restore-edition", "brma_lha_rates", FY, "3",
                      "--commit"],
                     ["restore-edition", "brma_lha_rates", FY, "9",
                      "--commit"],
                     ["restore-edition", "la_brma_mapping", FY, "1",
                      "--commit"]):
            ok, _ = e.halts(argv, state(e.cur))
            if not ok:
                problems.append(f"{' '.join(argv[:4])} did not halt")
        if not pe.status(e.cur, m.profile("rates"))["ok"]:
            problems.append("status not clean")
    scenario(cur, body)
    report(21, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') and applied by refresh-latest; the "
           "tip, a missing edition and a period of the other table are "
           "refused; a preview writes nothing" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 22

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


RUNNERS = {"subprocess.run", "subprocess.call", "subprocess.check_call",
           "subprocess.check_output", "subprocess.Popen", "os.system",
           "os.popen", "os.execv", "os.execl", "runpy.run_path",
           "runpy.run_module", "importlib.import_module", "__import__",
           "exec", "eval", "compile", "run_path", "run_module",
           "import_module", "Popen", "check_call", "check_output"}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def retired_problems(paths, names=RETIRED):
    """Problems (an AST check) in the given source files: an import of a
    retired script; a string constant that is, as a whole, a retired script's
    module or file name or a path to it (a command-line word, a path built
    for a runner); or a call to a runner (subprocess, os.system, runpy,
    importlib, exec, eval) with a retired script's name anywhere in its
    arguments. History text that merely mentions a script (a docstring or a
    message that says what it did) is not a use and is not flagged. The names
    are matched whole (the _v2 form included)."""
    alt = "|".join(map(re.escape, names))
    word = re.compile(r"(?<![A-Za-z0-9_])(" + alt + r")(_v2)?(?![A-Za-z0-9])")
    whole = re.compile(r"(?:.*[/\\])?(" + alt + r")(_v2)?(?:\.py)?")
    bad = []
    for path in paths:
        p = Path(path)
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] + [a.name for a in node.names]
            for mod in mods:
                if word.search(mod):
                    bad.append(f"{p.name} line {node.lineno}: imports "
                               f"{mod}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and whole.fullmatch(node.value.strip()):
                bad.append(f"{p.name} line {node.lineno}: a retired script "
                           f"named as {node.value[:50]!r}")
            if isinstance(node, ast.Call) and (
                    _dotted(node.func) in RUNNERS):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(
                            sub.value, str) and word.search(sub.value):
                        bad.append(f"{p.name} line {node.lineno}: a runner "
                                   f"is given a retired script "
                                   f"({sub.value[:40]!r})")
    return bad


def retired_files():
    """The scripts and tests that must never touch a retired script: the
    S3 and S14 verify scripts, their gate tests and the loaders and loader
    tests (test_s3_s14_retired.py, which proves the guards, is the one place
    that names them)."""
    names = ("s3_mye_editions_verify.py", "s14_lha_editions_verify.py",
             "s3_mye_editions.py", "s14_lha_editions.py",
             "tests/test_s3_verify_gates.py",
             "tests/test_s14_verify_gates.py",
             "tests/test_s3_mye_pure.py", "tests/test_s3_mye_loader.py",
             "tests/test_s14_lha_pure.py", "tests/test_s14_lha_loader.py")
    return [HERE / n for n in names if (HERE / n).exists()]


def gate_22_no_network_no_secrets(cur):
    name = ("no network, no secret in source or output, nothing written "
            "outside the rolled-back transaction; a download never "
            "overwrites a same-named file with different content; no "
            "retired script imported or run")
    files = [Path(__file__).resolve(), LOADER, HERE / "period_editions.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]
    reals = [f"public.{s.editions_table}" for s in (REAL_R, REAL_M)] + [
        f"public.{ledger_name(s)}" for s in (REAL_R, REAL_M)]

    def real_state():
        cur.execute("SELECT " + ", ".join("to_regclass(%s)" for _ in reals),
                    reals)
        return cur.fetchone()

    real_before = real_state()

    def body(c):
        with env(c) as e, e.specs():
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.publish()
                rc, _, _ = e.cmd(["load", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load"])
                rc_c, _, _ = e.cmd(["check-mapping"])
                raw = e.root / "raw"
                body_a = e.local().read_bytes()
                body_b = e.local(rates=tl.bump(RATES, "Ashford")).read_bytes()
                url = "https://assets.example/media/3/f.csv"
                with contextlib.redirect_stdout(io.StringIO()):
                    p1, _ = m.fetch(url, raw, _Session(body_a, url))
                    sha1 = m.content_sha256(p1)
                    p2, _ = m.fetch(url, raw, _Session(body_b, url))
                    p3, _ = m.fetch(url, raw, _Session(body_a, url))
                kept = (p1 == raw / "f.csv" and m.content_sha256(p1) == sha1
                        and p2 != p1 and p2.name.startswith("f-")
                        and p3 == p1)
            return rc, rc_s, rc_r, rc_l, rc_c, kept
    try:
        rc, rc_s, rc_r, rc_l, rc_c, kept = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(22, name, False, f"{type(ex).__name__}: {ex}")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs_after = cur.fetchone()[0]
    real_after = real_state()
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    literal = [n for n, t in texts.items() if pat.search(t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    retired = retired_problems(retired_files())
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.py"
        probe.write_text(
            f"import {RETIRED[1]}_v2\n"
            f"x = ['python', 'scripts/{RETIRED[0]}.py']\n"
            f"import subprocess\nsubprocess.run(['python -I {RETIRED[1]}"
            f".py'])\n"
            f"y = '{RETIRED[1]} ran by accident once'\n", encoding="utf-8")
        planted = retired_problems([probe])
    checker_ok = len(planted) == 3
    ok = (not attempts and rc == rc_s == rc_r == rc_l == rc_c == 0 and kept
          and runs == runs_after and real_before == real_after
          and not literal and not in_src and not in_out and not retired
          and checker_ok)
    report(22, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest, a preview and check-mapping through stubs "
           f"returned {rc}/{rc_s}/{rc_r}/{rc_l}/{rc_c}; a same-named "
           f"different download was saved beside it, the first "
           f"untouched={kept}; run-log rows {runs}->{runs_after}; real "
           f"editions tables and ledgers unchanged="
           f"{real_before == real_after}; secret-like literal in source "
           f"{literal or 'none'}; {len(secrets)} secret-named settings "
           f"checked against {len(files)} files and {len(OUTPUT)} output "
           f"lines: in source {in_src or 'none'}, in output "
           f"{in_out or 'none'}; retired scripts imported or run in "
           f"{len(retired_files())} gate/test/loader files: "
           f"{retired[:2] or 'none'} (the AST check catches a planted "
           f"import, path and runner: {checker_ok})")


# ---------------------------------------------------------------- gate 23

def discovery_problems(names):
    """Problems in refresh_map's input discovery against a catalogue of
    table names: brma_lha_rates and la_brma_mapping must be inputs, the
    editions tables, their ledgers and la_boundaries must not."""
    import refresh_map
    cur = mock.Mock()
    cur.fetchall.return_value = [(t, True) for t in names]
    got = {t for t, _ in refresh_map.input_tables(cur)}
    bad = [f"{t} is not an input" for t in (RATES_T, MAP_T) if t not in got]
    for t in ("brma_lha_rates_editions", "brma_lha_rates_editions_file_checks",
              "la_brma_mapping_editions", "la_brma_mapping_editions_file_"
              "checks", "la_boundaries"):
        if t in got:
            bad.append(f"{t} is an input")
    return bad, sorted(got)


def gate_23_refresh_map(cur):
    name = ("refresh_map input discovery: brma_lha_rates and la_brma_mapping "
            "are inputs, the editions tables and ledgers are not")
    names = {RATES_T, MAP_T, "brma_lha_rates_editions",
             "brma_lha_rates_editions_file_checks", "la_brma_mapping_editions",
             "la_brma_mapping_editions_file_checks", "la_boundaries",
             "staging_la_signals"}
    bad, got = discovery_problems(names)
    import refresh_map
    real = [t for t, _ in refresh_map.input_tables(cur)]
    real_bad = [f"{t} is not an input in the database"
                for t in (RATES_T, MAP_T) if t not in real]
    real_bad += [f"{t} is an input in the database" for t in real
                 if t.endswith("_editions") or "_editions_" in t
                 or t == "la_boundaries"]
    problems = bad + real_bad
    report(23, name, not problems, "; ".join(problems[:3]) if problems else
           f"against a stub catalogue the inputs are {got}; in the database "
           f"brma_lha_rates and la_brma_mapping are inputs ({len(real)} W1 "
           "inputs) and no editions table or ledger and not la_boundaries "
           "is one")


# ---------------------------------------------------------------- gate 24

def alias_problems(path=None, loader=LOADER, rate_names=None,
                   count=ALIAS_COUNT):
    """Problems with the crosswalk alias table: exactly `count` entries,
    each an exact "X & Y" key whose value is "X and Y" and says where it
    was seen; convert_name changes exactly those names, halts on any other
    "&" name and leaves every other name as it is; the loader's alias and
    conversion functions use a dict lookup and no regex, replace or fuzzy
    matching (AST); each target is a BRMA name of the held rates when
    given."""
    bad = []
    p = Path(path or m.NAMES_FILE)
    raw = json.loads(p.read_text(encoding="utf-8")).get("aliases") or {}
    if len(raw) != count:
        bad.append(f"{len(raw)} aliases, expected {count}")
    for k, v in raw.items():
        if " & " not in k or v.get("name") != k.replace(" & ", " and "):
            bad.append(f"{k!r} -> {v.get('name')!r} is not 'X & Y' -> "
                       "'X and Y'")
        if not v.get("seen"):
            bad.append(f"{k!r}: says nowhere it was seen")
    aliases = m.brma_aliases(p)
    for k, v in aliases.items():
        if m.convert_name(k, aliases) != v:
            bad.append(f"convert_name({k!r}) is not {v!r}")
    for stray in ("Hull & East Riding", "A & B", "Kernow & West"):
        if stray in aliases:
            continue
        try:
            m.convert_name(stray, aliases)
            bad.append(f"an unknown '&' name {stray!r} was converted")
        except ValueError:
            pass
    for plain in ("Ashford", "Hull and East Riding", "X and Y"):
        if m.convert_name(plain, aliases) != plain:
            bad.append(f"{plain!r} was changed")
    text = Path(loader).read_text(encoding="utf-8")
    tree = ast.parse(text)
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in (
                "brma_aliases", "convert_name"):
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr in (
                        "replace", "sub", "subn", "match", "search",
                        "fullmatch", "lower", "casefold", "translate",
                        "get_close_matches"):
                    bad.append(f"{fn.name}() uses .{n.attr}")
                if isinstance(n, ast.Name) and n.id in ("re", "difflib",
                                                        "fuzz", "process"):
                    bad.append(f"{fn.name}() uses {n.id}")
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] + [getattr(n, "module", "")
                                                or ""]
            if any(x in ("difflib", "rapidfuzz", "fuzzywuzzy", "thefuzz",
                         "Levenshtein") for x in mods):
                bad.append(f"the loader imports a fuzzy matcher {mods}")
    if rate_names is not None:
        gone = sorted(v for v in aliases.values() if v not in rate_names)
        if gone:
            bad.append(f"{len(gone)} alias target(s) are not BRMAs of the "
                       f"held rates: {gone[:3]}")
    return bad


def gate_24_aliases(cur):
    name = ("the crosswalk alias table is exact: 18 'X & Y' to 'X and Y' "
            "pairs, a dict lookup, no regex, no fuzzy matching")
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        # planted breaks are caught
        good = json.loads(m.NAMES_FILE.read_text(encoding="utf-8"))
        first = next(iter(good["aliases"]))
        planted = {
            "a missing pair": {k: v for k, v in list(
                good["aliases"].items())[1:]},
            "a wrong target": dict(good["aliases"], **{first: {
                "name": "Somewhere else", "seen": "x"}}),
            "an entry with no source": dict(good["aliases"], **{first: {
                "name": good["aliases"][first]["name"], "seen": ""}}),
        }
        for label, aliases in planted.items():
            p = Path(tmp) / "names.json"
            p.write_text(json.dumps({"aliases": aliases}), encoding="utf-8")
            try:
                if not alias_problems(p):
                    problems.append(f"{label} passed")
            except ValueError:
                pass
        fuzzy = Path(tmp) / "loader.py"
        fuzzy.write_text(
            "import re\n\ndef brma_aliases(path=None):\n    return {}\n\n"
            "def convert_name(name, aliases):\n"
            "    return re.sub(r' & ', ' and ', name)\n", encoding="utf-8")
        if len([x for x in alias_problems(loader=fuzzy)
                if "uses" in x]) < 2:
            problems.append("a regex conversion was not caught")
    seeded = not problems
    sd = ("planted breaks (a missing pair, a wrong target, an entry with no "
          "source, a regex conversion) are each caught" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(24, name, False, sd)
    names = None
    path, facts = read_held_csv()
    if path is not None:
        names = {r[1] for r in facts["rows"]}
    try:
        bad = alias_problems(rate_names=names)
    except (ValueError, OSError) as ex:
        return report(24, name, False, _first(ex))
    report(24, name, not bad, "; ".join(bad[:3]) if bad else
           f"{ALIAS_COUNT} exact pairs, each 'X & Y' -> 'X and Y' with its "
           "source; convert_name changes those names only and halts on any "
           "other '&' name; no regex, replace or fuzzy matching in the "
           f"loader (AST); {'every target is a BRMA of ' + path.name if path else 'held CSV not read'}; "
           + sd)


# ---------------------------------------------------------------- gate 25

def real_correction(cur, mapping=REAL_M, corr=None, name=CORRECTION,
                    vintage=V):
    """The named correction is stored as edition 2 of the live vintage and
    is nothing else: edition 2 supersedes edition 1 (the 'as loaded' one),
    its label names the correction, and it differs from edition 1 only in
    mapping_method, 'NaN' to centroid_spatial_join, on exactly the named
    count of authorities (la_name, brma_name and the authority set equal).
    Live then holds no 'NaN'."""
    _need(cur, mapping)
    corr = corr or m.MAPPING_CORRECTIONS[name]
    cur.execute(f"SELECT DISTINCT edition, supersedes FROM public."
                f"{mapping.editions_table} WHERE vintage = %s ORDER BY 1",
                (vintage,))
    chain = cur.fetchall()
    if (2, 1) not in chain:
        return False, (f"{vintage} has editions {chain}; edition 2 (the "
                       f"named correction {name}) is not loaded yet")
    cur.execute(f"SELECT DISTINCT release_label FROM public."
                f"{mapping.editions_table} WHERE vintage = %s AND edition "
                "= 2", (vintage,))
    labels = [r[0] for r in cur.fetchall()]
    bad = []
    if len(labels) != 1 or f"named correction {name}" not in labels[0]:
        bad.append(f"edition 2's label does not name {name}: {labels}")
    e1 = {r["lad24cd"]: r for r in m.records(cur, mapping, vintage, 1)}
    e2 = {r["lad24cd"]: r for r in m.records(cur, mapping, vintage, 2)}
    if set(e1) != set(e2):
        bad.append(f"the authority sets differ: "
                   f"{sorted(set(e1) ^ set(e2))[:4]}")
    cols = {c: [k for k in sorted(set(e1) & set(e2)) if e1[k][c] != e2[k][c]]
            for c in m.MAP_VALUES}
    for c in ("la_name", "brma_name"):
        if cols[c]:
            bad.append(f"edition 2 changes {c} at {len(cols[c])} "
                       f"authorities: {cols[c][:4]}")
    changed = cols["mapping_method"]
    if len(changed) != corr["count"]:
        bad.append(f"edition 2 changes mapping_method at {len(changed)} "
                   f"authorities, the correction names {corr['count']}")
    odd = [k for k in changed if (e1[k]["mapping_method"],
                                  e2[k]["mapping_method"]) != (corr["from"],
                                                               corr["to"])]
    if odd:
        bad.append(f"{len(odd)} change(s) are not {corr['from']!r} -> "
                   f"{corr['to']!r}: {odd[:4]}")
    if m.named_correction(list(e2.values()), list(e1.values())) != name:
        bad.append(f"the change is not exactly the named correction {name}")
    live = {r["lad24cd"]: r for r in m.records(cur, mapping, vintage)}
    nan = [k for k, r in live.items() if r["mapping_method"] == corr["from"]]
    if nan:
        bad.append(f"live still holds {corr['from']!r} at {len(nan)} "
                   "authorities (run refresh-latest)")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"edition 2 of {vintage} differs from edition 1 only in "
        f"mapping_method at {len(changed)} authorities, {corr['from']!r} -> "
        f"{corr['to']!r} (the named correction {name}); la_name, brma_name "
        f"and the {len(e1)} authorities are equal; live holds no "
        f"{corr['from']!r}")


def gate_25_correction(cur):
    name = ("the named correction s14-mapping-method-2026-10: edition 2 "
            "differs from edition 1 only in mapping_method, 'NaN' to "
            "centroid_spatial_join on 295 rows")
    problems = []
    corr = dict(m.MAPPING_CORRECTIONS[CORRECTION], count=NA - 1)

    def body(e, path, legacy):
        with patched_legacy(legacy, path):
            rc, text, _ = e.cmd(["migrate-legacy", "--commit"])
        if rc != 0:
            problems.append(f"migrate-legacy: {text[-120:]}")
            return
        ok, detail = real_correction(e.cur, ZM, corr)
        if ok or "not loaded yet" not in detail:
            problems.append(f"before edition 2: {detail}")
        # the correction needs --acknowledge and names itself
        with mock.patch.dict(m.MAPPING_CORRECTIONS, {CORRECTION: corr}):
            before = rejected_state(e.cur, tl.V, "mapping")
            bad_ = rejects(e, ["load", "--only", "mapping", "--commit"],
                           f"--acknowledge {tl.V}", before, period=tl.V,
                           part="mapping")
            if bad_:
                problems.append(f"the correction without --acknowledge: "
                                f"{bad_}")
            rc, text, logged = e.cmd(["load", "--only", "mapping",
                                      "--acknowledge", tl.V, "--commit"])
            if rc != 0 or f"named correction {CORRECTION}" not in text:
                problems.append(f"the correction: rc {rc}: {text[-140:]}")
            ok, detail = real_correction(e.cur, ZM, corr)
            if ok or "live still holds" not in detail:
                problems.append(f"before refresh-latest: {detail}")
            e.ok(e.cur, ["refresh-latest", "--commit"])
            ok, detail = real_correction(e.cur, ZM, corr)
            if not ok:
                problems.append(f"after refresh-latest: {detail}")
            # a change that is more than the correction is not it
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(f"ALTER TABLE public.{ZM.editions_table} DISABLE "
                          "TRIGGER USER")
            e.cur.execute(f"UPDATE public.{ZM.editions_table} SET "
                          "brma_name = 'Ashford' WHERE edition = 2 AND "
                          "lad24cd = 'E06000010'")
            ok, detail = real_correction(e.cur, ZM, corr)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or "brma_name" not in detail:
                problems.append(f"a brma_name change passed: {detail[:80]}")
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(f"ALTER TABLE public.{ZM.editions_table} DISABLE "
                          "TRIGGER USER")
            e.cur.execute(f"UPDATE public.{ZM.editions_table} SET "
                          "mapping_method = 'nearest_fallback' WHERE "
                          "edition = 2 AND lad24cd = 'E06000010'")
            ok, detail = real_correction(e.cur, ZM, corr)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok:
                problems.append("another mapping_method change passed")
        ok, detail = real_correction(e.cur, ZM, dict(corr, count=NA))
        if ok:
            problems.append("a wrong count passed")
    legacy_world(cur, body)
    mixed(25, name, not problems, "seeded: after migrate-legacy the "
          "crosswalk correction is refused without --acknowledge, stored as "
          "edition 2 naming s14-mapping-method-2026-10, applied by "
          "refresh-latest; a change to brma_name, another mapping_method "
          "change or a wrong count is not the correction" if not problems
          else "; ".join(problems[:3]), cur, real_correction)


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_brma_sets, gate_4_rule_1, gate_5_weekly, gate_6_w1_join,
         gate_7_mapping, gate_8_identity, gate_9_older_file,
         gate_10_stop_conditions, gate_11_one_transaction,
         gate_12_ledger_skip, gate_13_preview, gate_14_held_file_recheck,
         gate_15_second_vintage, gate_16_before_state_hash, gate_17_w1_read,
         gate_18_migration_proof, gate_19_rerun, gate_20_stranded,
         gate_21_restore, gate_22_no_network_no_secrets,
         gate_23_refresh_map, gate_24_aliases, gate_25_correction)


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
    """zz% relations, views and functions in the real database, read on a
    separate read-only connection after the rollback."""
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT relname FROM pg_class WHERE relname LIKE "
                        "'zz%%' AND relnamespace = 'public'::regnamespace")
            out = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT proname FROM pg_proc WHERE proname LIKE "
                        "'zz%%' AND pronamespace = 'public'::regnamespace")
            out += [r[0] for r in cur.fetchall()]
            return sorted(out)
    finally:
        conn.rollback()
        conn.close()


def print_note_lines():
    """The decision note's hash lines for the live tables as they stand,
    read on a read-only connection (nothing is written)."""
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            for line in note_lines_for(cur, REAL_R, REAL_M):
                print(line)
    finally:
        conn.rollback()
        conn.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--print-note-lines"]:
        return print_note_lines()
    if argv:
        sys.exit("usage: s14_lha_editions_verify.py [--print-note-lines]")
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
    mine = [t for t in left if t.startswith("zz_s14")]
    if mine:
        print(f"LEFTOVER: committed zz_s14 objects in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s14 table, view or function left in the database "
              "after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
