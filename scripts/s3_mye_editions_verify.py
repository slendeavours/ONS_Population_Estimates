"""Gates for the S3 (ONS mid-year population estimates) editions tables.

Mirrors scripts/s10_rough_sleeping_editions_verify.py and
s13_lahs_editions_verify.py. Prints `GATE n name: PASS|FAIL`; exits 1 on any
FAIL. It writes nothing, not even a run-log row. The real-table gates read
la_population, la_population_editions and la_population_editions_file_checks.
Until the editions table exists (ddl and migrate-legacy not yet run) they
print `FAIL (pending migration)` with the words "not yet migrated", so the
script exits non-zero until then, like the other verifiers; nothing is
created to make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s3_live / zz_s3_editions / zz_s3_editions_file_checks
(a copy of the loader's spec), created inside the transaction, and call the
loader's own writer and commands (s3_mye_editions.main with the SPEC pointed
at the copy, the ONS replies and the downloads stubbed; apply_year;
migrate_legacy; restore_edition) rather than a re-implementation. The real
tables are only ever read. Seeded parts use the loader tests' fixture
workbooks (five authorities, written to a temporary folder). No retired
script is imported or run by any gate (gate 22 checks this with the AST).

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), live checks                              (throwaway + real)
  2  every live period has edition 1; the latest edition equals live cell
     for cell on the key, the year, population and la_name  (seeded + real)
  3  codes: every lad24cd in la_boundaries, no E08000038/39 stored, the held
     workbooks' codes resolve through geography.resolve to edition 1's
     codes, source 3 declared 'old', no private recode dict and no
     la_code_lookup query in the loader (AST)               (seeded + real)
  4  296 authorities in every live period (and in its tip edition)
  5  no NULL, no zero (rule 1) in live and in the tip editions; edition 1
     (and a tip from a held file) sum to the England row of the workbook on
     disk, read-only                                        (seeded + real)
  6  identity from the workbook (seeded Cover, title, header, page and
     --release mismatches halt); the held workbooks' identity (seeded + real)
  7  a blank, marker or text in an LA cell halts and is never read as 0; no
     value coerced to 0 in the loader (AST); every LA cell of the held
     workbooks is a positive integer                         (seeded + real)
  8  older-file guard on the page, --release, --file and back-fill paths;
     the rank is the file's own, never its name
  9  stop conditions: each seeded REJECTED, stores nothing, no ledger row,
     partial run-log row
 10  a new year: edition, live rows and ledger row in one transaction
 11  ledger skip needs the (URL, sha256) pair for every period the file
     states
 12  preview and simulate write nothing
 13  a byte-identical re-read is `unchanged` on every path against edition 1
     "as loaded"; the held workbooks read now give edition 1 (2024: but for
     the four named name forms)                              (seeded + real)
 14  a restated back year is compared and needs --acknowledge naming it; an
     acknowledgement must name a compared year; --accept-large-revision
     needs --acknowledge
 15  refresh-latest changes only the revised year and copies loaded_at
 16  edition 1 equals the decision note's before-state lines (seeded + real)
 17  live 2025 equals the decision note's w1-read line (lad24cd + population,
     the map's year)                                         (seeded + real)
 18  the migration proof re-run read-only from the workbooks on disk
 19  rerun idempotent
 20  stranded year repair
 21  restore-edition round trip
 22  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content, and
     no retired script is imported or run by any gate or test (AST)
 23  refresh_map input discovery: la_population is an input, the editions
     tables and la_boundaries are not                        (stub + real)
 24  the July 2026 revision of mid-2024: the two workbooks on disk differ at
     295 authorities and England moves -12,931 (listed); once loaded, edition
     2 of 2024 equals the July 2026 workbook's MYE4 mid-2024 (seeded + real)

The decision note's hash lines are scan-safe: a 64-hex run would be flagged by
the credential scan, so each sha256 is written as two 32-hex halves in two
labelled fields:
    before-state la_population <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    before-state-all la_population rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read la_population 2025 rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
before-state is the loader's rows_content_sha (lad24cd, population, la_name);
before-state-all hashes every live row (lad24cd|reference_year|population|
la_name, sorted); w1-read hashes what W1 reads (lad24cd|population, sorted).
`python scripts/s3_mye_editions_verify.py --print-note-lines` prints the lines
for the live table as it stands (read-only), for the decision note; run it
before the migration.

Usage:
    python scripts/s3_mye_editions_verify.py
    python scripts/s3_mye_editions_verify.py --print-note-lines
    python scripts/s3_mye_editions_verify.py --print-revision

--print-revision lists every authority the July 2026 workbook restates for
mid-2024 (read-only, from the workbooks on disk), for the decision note.

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
import os
import re
import socket
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
from _db import ENV, get_conn, get_readonly_conn  # noqa: E402
import editions_core as core  # noqa: E402
import geography  # noqa: E402
import period_editions as pe  # noqa: E402
import s3_mye_editions as m  # noqa: E402
import test_s3_mye_loader as tl  # noqa: E402
from test_s3_mye_pure import (CODES, ED_URI, _Session, edition,  # noqa: E402
                              landing, value, write_file)

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
N = tl.N
LOADER = HERE / "s3_mye_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-11-s3-editions-first-load.md")
NOT_YET = "the S3 editions table does not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (22)
NEVER_CODES = ("E08000038", "E08000039")
# the retired scripts, written in halves so this file never names them whole
RETIRED = ("s3_mye" + "_refresh", "s14_lha_rates" + "_build")
REVISION_COUNT = 295          # authorities whose mid-2024 population changes
REVISION_ENGLAND = -12931     # England, mid-2024: July 2026 against July 2025
HELD_PLAIN = ("mye24tablesew.xlsx", "mye25tablesew.xlsx")


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


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    names = (ZZ.live_table, ZZ.editions_table, LEDGER)
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                      for n in names))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s3 table already exists as a real table; "
                 "refusing to run")
    cur.execute(f"CREATE TABLE public.{ZZ.live_table} (LIKE "
                f"public.{REAL.live_table} INCLUDING ALL)")
    with tl.specs():
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
    """The loader's main(argv) on the throwaway tables, the ONS replies and
    the downloads stubbed; every workbook is written here (write_file).
    Subclasses the loader tests' Migrate fixture so seed_legacy, legacy and
    migrate are the tests' own."""

    def __init__(self, cur, root):
        unittest.TestCase.__init__(self)
        self.cur = cur
        self.root = Path(root)
        self.api = {m.LANDING: landing()}
        self.urls, self.final, self.fetched = {}, {}, []
        self.n = 0

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, run-log
        mock). The output is kept for the secret-leak gate."""
        rc, text, logged = super().run_main(cur, argv)
        OUTPUT.extend(text.splitlines())
        return rc, text, logged

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


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp)


def scenario(cur, fn):
    """Run fn(env) in a savepoint that is rolled back."""
    def body(c):
        with env(c) as e, tl.specs():
            return fn(e)
    return _in_savepoint(cur, body)


def legacy_world(cur, fn, **kw):
    """A seeded legacy state (as the old loads left it) and the fixture
    workbooks; fn(env, paths, files, loaded)."""
    def body(c):
        with env(c) as e, tl.specs():
            paths, files, loaded = e.seed_legacy(c, **kw)
            return fn(e, paths, files, loaded)
    return _in_savepoint(cur, body)


def patched_legacy(e, loaded, files):
    """The loader's held-state constants pointed at the seeded state."""
    legacy = e.legacy(e.cur, loaded)
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(m, "LEGACY_LIVE", legacy[:2]))
    stack.enter_context(mock.patch.object(m, "LEGACY_LOADED", loaded))
    stack.enter_context(mock.patch.object(m, "LEGACY_FILES", files))
    return stack


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "varchar": "character varying",
          "text": "text", "date": "date"}


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
    have = _constraint_names(cur, spec.editions_table, "c")
    for c in spec.table_constraints:
        name = c.split()[1]
        if name not in have:
            bad.append(f"{spec.editions_table}: CHECK {name} missing")
    bad += _triggers(cur, spec.editions_table, spec.trigger,
                     spec.truncate_trigger)
    for c in (m.KEY, m.PERIOD) + m.VALUES + ("loaded_at",):
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
    name = ("editions table, ledger, live shape (UPDATE, DELETE and TRUNCATE "
            "are refused on the editions and the ledger)")
    bad = shape_problems(cur, ZZ)

    def seed(cur):
        with env(cur) as e, tl.specs():
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


EMPTY = "no live periods to check (an empty state is not a pass)"
_CACHE = {}


def _live_periods(cur, spec):
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


def read_cached(path):
    """m.read_file once per content (the workbooks are large)."""
    sha = m.content_sha256(path)
    if sha not in _CACHE:
        _CACHE[sha] = m.read_file(path)
    return _CACHE[sha]


def held_paths(files=None, raw_dir=None):
    """{year: the workbook on disk whose sha256 is the held one, or None}."""
    files = m.LEGACY_FILES if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    cands = sorted(raw.glob("*.xlsx")) if raw.is_dir() else []
    shas = {p: m.content_sha256(p) for p in cands}
    out = {}
    for year, spec in files.items():
        hit = [p for p, s in shas.items() if s == spec["sha256"]]
        out[str(year)] = hit[0] if hit else None
    return out


def read_held(files=None, raw_dir=None):
    """({year: (path, file facts)}, [problems]) for every held workbook."""
    found, problems = {}, []
    files = m.LEGACY_FILES if files is None else files
    for year, p in held_paths(files, raw_dir).items():
        if p is None:
            raw = raw_dir or m.RAW_DIR
            problems.append(f"no file in {raw} with sha256 "
                            f"{files[year]['sha256'][:16]} (mid-{year})")
            continue
        try:
            f = read_cached(p)
        except ValueError as e:
            problems.append(f"{p.name}: {_first(e)}")
            continue
        if str(f["year"]) != str(year):
            problems.append(f"{p.name}: the file says mid-{f['year']}, the "
                            f"held file is mid-{year}")
            continue
        found[str(year)] = (p, f)
    return found, problems


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
    cols = ", ".join(tuple(spec.key_cols) + (spec.period_col,) + m.VALUES)
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
        f"{len(periods)} live period(s) each have edition 1 and the latest "
        "edition equals live on lad24cd, the year, population and la_name, "
        "cell for cell")


def gate_2_edition1_and_latest(cur):
    name = ("every live period has edition 1; the latest edition equals "
            "live cell for cell on both values")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_edition1_and_latest(e.cur, ZZ)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET population = population + 1"
             " WHERE lad24cd = 'E06000002'", "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET la_name = 'x' WHERE lad24cd"
             " = 'E06000002'", "edition-only"),
            (f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
             "'E06000002'", "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET reference_year = 2030",
             "no edition 1"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            try:
                ok, detail = real_edition1_and_latest(e.cur, ZZ)
            except (psycopg2.Error, ValueError, LookupError) as ex:
                ok, detail = False, str(ex)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(2, name, ok, "seeded complete state passes; four planted drifts "
          "(either value, a missing row, a year without edition 1) are each "
          "caught" if ok else f"{out['base']} {out['miss']}", cur,
          real_edition1_and_latest)


# ---------------------------------------------------------------- gate 3

def _source_problems(path, zeros=False):
    """Problems in a module's source (an AST check): a string constant, other
    than a docstring, holding a Barnsley/Sheffield code (a private recode
    dict); a string constant that queries la_code_lookup. With zeros=True
    also a source value coerced to 0 (`x or 0`, COALESCE(x, 0), `else 0`) on
    a line without `# not a source value`."""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines()
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None and \
                    node.body and isinstance(node.body[0], ast.Expr):
                doc.add(id(node.body[0].value))
    pat = re.compile(r"E080000(16|19|38|39)(?![0-9])")
    query = re.compile(r"(?is)\b(select|from|join|update|insert)\b")
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc:
            if pat.search(node.value):
                bad.append(f"line {node.lineno}: private code "
                           f"{node.value[:40]!r}")
            if "la_code_lookup" in node.value and query.search(node.value):
                bad.append(f"line {node.lineno}: la_code_lookup query")
            if zeros and re.search(r"(?i)coalesce\([^)]*,\s*0\s*\)",
                                   node.value):
                bad.append(f"line {node.lineno}: COALESCE(x, 0)")
        if not zeros:
            continue
        zero = (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                and any(isinstance(v, ast.Constant) and v.value == 0
                        and not isinstance(v.value, bool)
                        for v in node.values[1:]))
        ifexp = (isinstance(node, ast.IfExp)
                 and isinstance(node.orelse, ast.Constant)
                 and node.orelse.value == 0
                 and not isinstance(node.orelse.value, bool))
        if zero or ifexp:
            if "not a source value" not in lines[node.lineno - 1]:
                bad.append(f"line {node.lineno}: a value coerced to 0")
    return bad


def real_codes(cur, spec=REAL, loader=LOADER, form="old", files=None,
               raw_dir=None):
    _need(cur, spec)
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    bad, seen = [], 0
    for table in (spec.live_table, spec.editions_table):
        cur.execute(f"SELECT DISTINCT lad24cd FROM public.{table} WHERE "
                    "lad24cd IS NOT NULL")
        codes = {r[0] for r in cur.fetchall()}
        seen += len(codes)
        stray = sorted(codes - valid)
        if stray:
            bad.append(f"{table}: {stray[:4]} not in la_boundaries")
        new = sorted(codes & set(NEVER_CODES))
        if new:
            bad.append(f"{table}: lad24cd holds {new} (Barnsley/Sheffield "
                       "are E08000016/19 in this series)")
    if not _live_periods(cur, spec):
        bad.append(f"{spec.live_table}: {EMPTY}")
    held, problems = read_held(files, raw_dir)
    bad += problems
    resolved = 0
    for year, (path, f) in held.items():
        rmap, probs = m.resolve_codes(cur, set(f["mye2"]["las"]), f["year"])
        bad += [f"{path.name}: {x}" for x in probs[:2]]
        cur.execute(f"SELECT DISTINCT lad24cd FROM public."
                    f"{spec.editions_table} WHERE {spec.period_col} = %s AND "
                    "edition = 1", (int(year),))
        stored = {r[0] for r in cur.fetchall()}
        if set(rmap.values()) != stored:
            off = sorted(set(rmap.values()) ^ stored)
            bad.append(f"{path.name}: its codes resolve to codes that are "
                       f"not edition 1's lad24cd ({off[:4]})")
        resolved += len(rmap)
    src = _source_problems(loader)
    if src:
        bad.append(f"the loader: {src[:2]}")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("?",))[0]
    if decl != form:
        bad.append(f"source 3 is declared {decl!r}, expected {form!r}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{seen} distinct lad24cd over live and editions all in "
        f"la_boundaries; no E08000038/39 in lad24cd; the held workbooks' "
        f"{resolved} codes resolve through geography.resolve to edition 1's "
        f"lad24cd; source 3 declared {form!r}; no private dict or "
        "la_code_lookup query in the loader")


def gate_3_codes(cur):
    name = ("codes: lad24cd in la_boundaries, no E08000038/39 stored, the "
            "workbooks' codes resolve through geography.resolve, no private "
            "recode dict or la_code_lookup query")
    out = {}

    def body(e):
        path = e.file()
        e.ok(e.cur, ["load", "--commit"])
        files = {"2025": {"sha256": m.content_sha256(path)}}
        raw = path.parent
        out["base"] = real_codes(e.cur, ZZ, files=files, raw_dir=raw)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET lad24cd = 'E08000038' "
             "WHERE lad24cd = 'E08000016'", "Barnsley/Sheffield"),
            (f"UPDATE public.{ZZ.live_table} SET lad24cd = 'E07999999' "
             "WHERE lad24cd = 'E06000001'", "not in la_boundaries"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_codes(e.cur, ZZ, files=files, raw_dir=raw)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:90]}")
        ok, detail = real_codes(e.cur, ZZ, form="new", files=files,
                                raw_dir=raw)
        if ok or "declared" not in detail:
            miss.append(f"a wrong declared form passed: {detail[:80]}")
        ok, detail = real_codes(e.cur, ZZ, files={"2025": {
            "sha256": "0" * 64}}, raw_dir=raw)
        if ok or "no file" not in detail:
            miss.append(f"a missing held file passed: {detail[:80]}")
        out["miss"] = miss
        # a file carrying the new Barnsley/Sheffield codes halts against the
        # declared form 'old' and stores nothing
        before = state(e.cur)
        e.file(2026, codes=["E06000001", "E06000002", "E08000038",
                            "E08000019", "E09000033"])
        halted, text = e.halts(["load", "--commit"], before, "E08000038")
        out["new_code"] = halted
    scenario(cur, body)
    # the AST checks: planted constructs are caught, a docstring is not
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text(
            '"""E08000038 in a docstring; la_code_lookup too"""\n'
            'X = {"a": "E08000038"}\n'
            'Q = "SELECT old_code FROM la_code_lookup"\n'
            'D = "a message about la_code_lookup recode"\n'
            'def f(v):\n    return v or 0\n', encoding="utf-8")
        ast_bad = _source_problems(p)
    ast_ok = (len(ast_bad) == 2 and any("line 2" in b for b in ast_bad)
              and any("line 3" in b for b in ast_bad))
    ok = (out["base"][0] and not out["miss"] and ast_ok
          and out.get("new_code"))
    detail = ("Barnsley and Sheffield stay E08000016/19; two planted "
              "breaks, a wrong declared form and a missing file are each "
              "caught; a file carrying the new Barnsley code halts against "
              "the declared 'old' and stores nothing; the AST check catches "
              "a private dict and a la_code_lookup query and ignores a "
              "docstring and a message" if ok
              else f"seeded: {out} ast {ast_bad}")
    mixed(3, name, ok, detail, cur, real_codes)


# ---------------------------------------------------------------- gate 4

def real_row_counts(cur, spec=REAL, n=AREAS):
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, EMPTY
    bad = []
    for p in periods:
        tip = _tip(cur, spec, p)
        for table, extra in ((spec.live_table, ""),
                             (spec.editions_table, f" AND edition = {tip}")):
            cur.execute(f"SELECT COUNT(DISTINCT lad24cd), COUNT(*) FROM "
                        f"public.{table} WHERE {spec.period_col} = %s{extra}",
                        (p,))
            got = tuple(cur.fetchone())
            if got != (n, n):
                bad.append(f"{table} {p}: {got[0]} authorities in {got[1]} "
                           f"rows, expected {n}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} period(s): {n} authorities, one row each, in live "
        "and in the tip edition")


def gate_4_row_counts(cur):
    name = "296 authorities in every period (live and its tip edition)"
    out = {}

    def body(e):
        e.seed(e.cur)
        e.seed(e.cur, year=2026)
        out["base"] = real_row_counts(e.cur, ZZ, n=N)
        out["wrong_n"] = real_row_counts(e.cur, ZZ, n=N + 1)
        e.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
                      "'E08000019' AND reference_year = 2025")
        out["short"] = real_row_counts(e.cur, ZZ, n=N)
    scenario(cur, body)
    ok = out["base"][0] and not out["wrong_n"][0] and not out["short"][0]
    mixed(4, name, ok, ("the expected count passes in both years; one more "
                        "authority, or a missing authority in an earlier "
                        "year of live, fails" if ok else f"{out}"), cur,
          real_row_counts)


# ---------------------------------------------------------------- gate 5

def _total_of(recs):
    return sum(r["population"] for r in recs)


def real_values(cur, spec=REAL, files=None, raw_dir=None):
    """In live and in each period's tip edition: no NULL and no zero (rule 1)
    in either value (the editions table holds both NOT NULL). The workbooks
    on disk (found by sha256): edition 1 of each held year sums to the
    England row of its own workbook, and a tip edition that comes from a
    held workbook (its source_file ranks as that file) sums to that file's
    England row for the year."""
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, EMPTY
    bad, rows = [], 0
    cols = _columns(cur, spec.editions_table)
    for c in m.VALUES:
        if cols.get(c, (None, "YES"))[1] != "NO":
            bad.append(f"{spec.editions_table}.{c} is nullable")
    for p in periods:
        tip = _tip(cur, spec, p)
        for table, extra in ((spec.live_table, ""),
                             (spec.editions_table, f" AND edition = {tip}")):
            cur.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE population IS NULL "
                "OR la_name IS NULL OR la_name = ''), COUNT(*) FILTER (WHERE "
                f"population <= 0) FROM public.{table} WHERE "
                f"{spec.period_col} = %s{extra}", (p,))
            n, nulls, zero = cur.fetchone()
            rows += n
            if nulls:
                bad.append(f"{table} {p}: {nulls} row(s) with a NULL value")
            if zero:
                bad.append(f"{table} {p}: {zero} row(s) with a zero or "
                           "negative population")
    held, problems = read_held(files, raw_dir)
    bad += problems
    sums = []
    for year, (path, f) in held.items():
        rec = m.reconcile(f, [int(year)])
        if rec:
            bad.append(f"{path.name}: reconciliation failed: {rec[0]}")
            continue
        want = m.england_total(f, year)
        cur.execute(f"SELECT SUM(population), COUNT(*) FROM public."
                    f"{spec.editions_table} WHERE {spec.period_col} = %s AND "
                    "edition = 1", (int(year),))
        got, n = cur.fetchone()
        if not n:
            bad.append(f"{year}: no edition 1 to sum against {path.name}")
        elif got != want:
            bad.append(f"{year} edition 1 sums to {got:,}, the England row "
                       f"of {path.name} says {want:,}")
        sums.append(f"{year} edition 1 {got or 0:,} = {path.name} England "
                    f"{want:,}")
    for p in periods:
        tip = _tip(cur, spec, p)
        if tip == 1:
            continue
        cur.execute(f"SELECT DISTINCT source_file FROM public."
                    f"{spec.editions_table} WHERE {spec.period_col} = %s "
                    "AND edition = %s", (int(p), tip))
        src = cur.fetchall()
        rank = m.release_rank(src[0][0]) if len(src) == 1 else None
        for year, (path, f) in held.items():
            if f["rank"] == rank and int(p) in f["mye4"]["years"]:
                want = m.england_total(f, p)
                cur.execute(f"SELECT SUM(population) FROM public."
                            f"{spec.editions_table} WHERE "
                            f"{spec.period_col} = %s AND edition = %s",
                            (int(p), tip))
                got = cur.fetchone()[0]
                if got != want:
                    bad.append(f"{p} edition {tip} sums to {got:,}, the "
                               f"England row of {path.name} says {want:,}")
                sums.append(f"{p} edition {tip} {got:,} = {path.name} "
                            f"England {want:,}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{rows:,} rows over live and the tip editions: no NULL, no zero, "
        "both editions columns NOT NULL; " + "; ".join(sums))


def gate_5_values(cur):
    name = ("no NULL or zero in either value (NOT NULL in the editions "
            "table); edition 1 national sums equal the workbook's England "
            "row")
    out = {}

    def body(e):
        path = e.file()
        e.ok(e.cur, ["load", "--commit"])
        files = {"2025": {"sha256": m.content_sha256(path)}}
        raw = path.parent
        out["base"] = real_values(e.cur, ZZ, files, raw)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET population = NULL WHERE "
             "lad24cd = 'E06000002'", "NULL value"),
            (f"UPDATE public.{ZZ.live_table} SET la_name = NULL WHERE "
             "lad24cd = 'E06000002'", "NULL value"),
            (f"UPDATE public.{ZZ.live_table} SET population = 0 WHERE "
             "lad24cd = 'E06000002'", "zero or negative"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_values(e.cur, ZZ, files, raw)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        # a valid workbook whose England row differs from edition 1 is caught
        other = write_file(e.root / "other" / path.name, 2025,
                           values={("E06000002", 2025): 99999})
        ok, detail = real_values(e.cur, ZZ, {"2025": {
            "sha256": m.content_sha256(other)}}, other.parent)
        if ok or "England row" not in detail:
            miss.append(f"a file with other sums passed: {detail[:80]}")
        # a workbook that does not reconcile is caught
        bad = write_file(e.root / "bad" / path.name, 2025,
                         england={("mye2", 2025): 1})
        ok, detail = real_values(e.cur, ZZ, {"2025": {
            "sha256": m.content_sha256(bad)}}, bad.parent)
        if ok or "reconciliation failed" not in detail:
            miss.append(f"a file that does not reconcile passed: "
                        f"{detail[:80]}")
        ok, detail = real_values(e.cur, ZZ, {"2025": {"sha256": "0" * 64}},
                                 raw)
        if ok or "no file" not in detail:
            miss.append("a missing file passed")
        # a NULL cannot be stored in the editions table at all
        e.cur.execute("SAVEPOINT plant")
        try:
            e.cur.execute(f"INSERT INTO public.{ZZ.editions_table} (lad24cd,"
                          "reference_year, edition, population, la_name, "
                          "release_label, published_date, source_file, "
                          "source_sha256) VALUES ('E06000001', 2030, 1, "
                          "NULL, 'x', 'x', now(), 'x', 'x')")
            miss.append("the editions table accepted a NULL value")
        except psycopg2.Error:
            pass
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(5, name, ok, "the seeded state passes (edition 1 sums to the "
          "workbook's England row); a NULL in either value, a zero, a "
          "workbook of other sums, one that does not reconcile and a missing "
          "file are each caught; the editions table refuses a NULL" if ok
          else f"{out['base']} {out['miss']}", cur, real_values)


# --------------------------------------------------------- file gates (6, 7)

def real_identity(cur=None, files=None, raw_dir=None, spec=REAL):
    """The held workbooks' identity read from the files themselves equals what
    the loader surveyed (year, rank), and, once the editions table exists,
    every period's tip names its file's mid-year and publication date."""
    held, problems = read_held(files, raw_dir)
    bad = list(problems)
    ranks = m.LEGACY_RANKS if files is None else {}
    for year, (path, f) in held.items():
        if ranks and f["rank"] != ranks.get(year):
            bad.append(f"{path.name}: rank {f['rank']}, surveyed "
                       f"{ranks.get(year)}")
        if not f["mye2"]["year"] == f["mye4"]["year"] == f["year"]:
            bad.append(f"{path.name}: MYE2 and MYE4 disagree with the Cover")
    tips = ""
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        info = m.tip_info(cur, spec)
        off = [pp for pp, i in info.items() if i["rank"] is None]
        if off:
            bad.append(f"{off[:3]}: the tip's source_file names no mid-year "
                       "and publication date")
        tips = f"; {len(info)} stored year(s), each tip names its file"
    return not bad, "; ".join(bad[:3]) if bad else (
        "; ".join(f"{p.name}: {f['title']!r}, published {f['published']}"
                  for p, f in held.values())
        + f"; Cover, MYE2 and MYE4 agree{tips}")


def gate_6_identity(cur):
    name = ("identity from the workbook: a seeded Cover, MYE2 or MYE4 title, "
            "header, publication line, page or --release mismatch halts")
    problems = []

    def body(e):
        tmpd = Path(e.root)

        def raises(label, **kw):
            try:
                m.read_file(write_file(tmpd / f"{label}.xlsx", 2025, **kw))
                problems.append(f"{label} was read")
            except ValueError:
                pass
        raises("cover_title", cover={"title_year": 2024})
        raises("no_pub", cover={"pub_line": False})
        raises("no_next", cover={"next_line": False})
        raises("date_cell", cover={"date_text": "Date published: 1 January "
                                   "2020"})
        raises("mye2_title", mye2_title_year=2024)
        raises("mye4_title", mye4_title_year=2024)
        raises("mye2_header", mye2_header=["Code", "Name", "Geography",
                                           "Total"])
        raises("mye4_header", mye4_header=["Code", "Name", "Geography",
                                           "Mid-2025"])
        raises("no_geography_note", notes=[])
        bad = tmpd / "not_a_workbook.xlsx"
        bad.write_bytes(b"this is not a workbook")
        try:
            m.read_file(bad)
            problems.append("a file that is not a workbook was read")
        except Exception:  # noqa: BLE001 (any refusal is the point)
            pass
        t = m.read_file(write_file(tmpd / "ok.xlsx", 2025))
        if not m.identity_problems(t, 2024):
            problems.append("a page of another year was accepted")
        if not m.identity_problems(t, None, 2024):
            problems.append("--release of another year was accepted")
        if m.identity_problems(t, 2025, 2025):
            problems.append("a matching page was refused")
        # through the command: a page edition of another year, a file of
        # another year under --release and a Cover of another year halt and
        # store nothing
        e.file()
        before = state(e.cur)
        e.api[ED_URI.format(2025)]["description"]["edition"] = (
            "Mid-2024: 2023 local authority boundaries")
        ok, _ = e.halts(["load", "--commit"], before, "Mid-2024")
        if not ok:
            problems.append("a page of another year did not halt the load")
        path = e.file(register=False)
        ok, _ = e.halts(["load", "--file", path, "--release", "2024",
                         "--no-page", "--commit"], before, "--release 2024")
        if not ok:
            problems.append("--file with another --release did not halt")
        path = e.file(register=False, cover={"title_year": 2024})
        ok, _ = e.halts(["load", "--file", path, "--no-page", "--commit"],
                        before)
        if not ok:
            problems.append("a Cover of another year did not halt")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a Cover title year, missing publication or next-publication "
          "line, a Date published line that differs, a MYE2 or MYE4 title "
          "year, a MYE2 or MYE4 header, a missing geography note, a file "
          "that is not a workbook, a page edition and --release that "
          "disagree with the file each halt, and nothing is stored"
          if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(6, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(6, name, False, _first(ex))
    report(6, name, ok, f"{sd}; held workbooks: {detail}")


def real_cells(files=None, raw_dir=None):
    """Every English LA cell of every year of the held workbooks is a
    positive integer (la_cell: no blank, marker, text or zero anywhere in
    MYE2 All ages and MYE4 mid-2011 to the file's year), and each workbook
    reconciles for every year."""
    held, problems = read_held(files, raw_dir)
    if problems:
        return False, problems[0]
    out = []
    for year, (path, f) in held.items():
        bad = m.reconcile(f, f["mye4"]["years"])
        if bad:
            return False, f"{path.name}: {len(bad)} problem(s), e.g. {bad[0]}"
        cells = 0
        for code, r in f["mye2"]["las"].items():
            m.la_cell(r["values"]["All ages"], f"{code} All ages")
            cells += 1
        for code, r in f["mye4"]["las"].items():
            for y in f["mye4"]["years"]:
                m.la_cell(r["values"][y], f"{code} Mid-{y}")
                cells += 1
        out.append(f"{path.name}: {len(f['mye2']['las'])} authorities, "
                   f"{cells:,} cells, every one a positive integer, "
                   f"reconciled in all {len(f['mye4']['years'])} years")
    return True, "; ".join(out)


def gate_7_markers(cur):
    name = ("a blank, marker or text in an LA cell halts and is never read "
            "as 0; no value coerced to 0")
    problems = []

    def body(e):
        cases = ((None, "blank"), ("", "blank"), ("see note", "text"),
                 ("[x]", "text"), (0, "zero"), (-1, "negative"),
                 (2.5, "non-integer"))
        for cell, needle in cases:
            e.file(mye2={"E06000002": cell},
                   values={("E06000002", 2025): cell})
            before = state(e.cur)
            ok, text = e.halts(["load", "--commit"], before, needle)
            if not ok:
                problems.append(f"{cell!r} in the newest year: not halted "
                                f"naming {needle!r}: {text[-100:]}")
            # a back year of MYE4 is read too (the year is stated by a file
            # that is the newest, so seed 2024 as held and bad 2024 in 2026)
        for cell in ("[x]", "[z]", None, "", "  ", "see note", -1, 2.5, True,
                     0, float("nan")):
            try:
                got = m.la_cell(cell, "x")
                problems.append(f"la_cell({cell!r}) returned {got}")
            except ValueError:
                pass
        if m.la_cell(3.0, "x") != 3 or m.la_cell(7, "x") != 7:
            problems.append("a whole number is not a population")
        # a bad cell in a year the run does not read does not halt it, one
        # in a year it reads does (reconcile over the years named)
        t = m.read_file(e.file(register=False, values={
            ("E06000002", 2012): None}))
        if not m.reconcile(t, t["mye4"]["years"]):
            problems.append("reconcile over every year missed a blank")
        if m.reconcile(t, [2024, 2025]):
            problems.append("a blank in an unread year halted the load")
    scenario(cur, body)
    src = _source_problems(LOADER, zeros=True)
    if src:
        problems.append(f"the loader: {src[:2]}")
    # the checker finds a planted coercion and honours the marker comment
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text('def f(v):\n    return v or 0\n'
                     'def g(v):\n    return v or 0  # not a source value\n',
                     encoding="utf-8")
        planted = _source_problems(p, zeros=True)
    if len(planted) != 1 or "line 2" not in planted[0]:
        problems.append(f"the zero-coercion check is wrong: {planted}")
    seeded = not problems
    sd = ("seeded: a blank, an empty string, text, a marker, a zero, a "
          "negative and a fraction in an LA cell of the newest year each "
          "halt and store nothing; la_cell never returns a default; no "
          "value is coerced to 0 in the loader" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(7, name, False, sd)
    try:
        ok, detail = real_cells()
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(7, name, False, _first(ex))
    report(7, name, ok, f"{sd}; held workbooks: {detail}")


# ---------------------------------------------------------------- gates 8-12

def gate_8_older_file(cur):
    name = ("older-file guard on the page, --release, --file and back-fill "
            "paths; the rank is the file's own, never its name")
    problems = []
    # the content is published 2026-07-01; the NAME looks like a later year's
    fake = "mye26tablesew.xlsx"

    def older(e, how):
        e.seed(e.cur)
        path = e.file(published=date(2026, 7, 1), register=how != "file",
                      name=fake, values={("E06000002", 2025): 123456})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2025"]
        if how == "file":
            argv += ["--file", path, "--no-page"]
        return argv

    def body(e):
        for how in ("page", "release", "file"):
            def one(c, how=how):
                with env(c) as e2, tl.specs():
                    argv = older(e2, how)
                    before = state(c)
                    rc, text, logged = e2.cmd(argv)
                    if not (rc == "halt" and f"{P} (older" in text
                            and "older file" in text
                            and state(c) == before and not logged.called):
                        problems.append(f"{how}: rc {rc}: {text[-160:]}")
            _in_savepoint(e.cur, one)

        def backfill(c):
            with env(c) as e2, tl.specs():
                e2.seed(c)
                path = e2.file(2024, register=False)
                before = state(c)
                rc, text, logged = e2.cmd(["load", "--file", path,
                                           "--no-page", "--commit"])
                if not (rc == "halt" and "2024 (older" in text
                        and state(c) == before and not logged.called):
                    problems.append(f"back-fill of mid-2024: rc {rc}: "
                                    f"{text[-160:]}")
        _in_savepoint(e.cur, backfill)

        def override(c):
            with env(c) as e2, tl.specs():
                argv = older(e2, "file") + ["--allow-older-file",
                                            "--acknowledge", P]
                rc, text, logged = e2.cmd(argv)
                eds = tl.editions(c)
                if not (rc == 0 and eds == [(1, None, N), (2, 1, N)]
                        and logged.called and "--allow-older-file given"
                        in logged.call_args[0][2]):
                    problems.append(f"--allow-older-file: rc {rc}, {eds}")
        _in_savepoint(e.cur, override)

        def equal_rank(c):
            with env(c) as e2, tl.specs():
                e2.seed(c)
                e2.file(values={("E06000002", 2025): 40})
                before = state(c)
                rc, text, logged = e2.cmd(["load", "--commit",
                                           "--acknowledge", P])
                if not (rc == 1 and "two files claim the same release"
                        in text and state(c) == before):
                    problems.append(f"equal rank, other content: rc {rc}: "
                                    f"{text[-120:]}")
        _in_savepoint(e.cur, equal_rank)
    scenario(cur, body)
    report(8, name, not problems, "a mid-2025 file published 2026-07-01 but "
           "named like a later one, offered by the page, --release and "
           "--file against a held file of 2026-07-29, is skipped and halts "
           "with nothing stored or logged; a mid-2024 file offered after "
           "2025 is not back-filled; --allow-older-file overrides and is "
           "logged; equal rank with other content stops" if not problems
           else "; ".join(problems[:3]))


def rejected_state(cur, period):
    """The editions and live tables, and the number of ledger rows of the
    period (a rejected year stores nothing and writes no ledger row; a
    restated earlier year may still get its 'unchanged' row)."""
    tables = tables_state(cur)
    cur.execute(f"SELECT COUNT(*) FROM public.{LEDGER} WHERE "
                "reference_year = %s", (int(period),))
    return tables + (cur.fetchone()[0],)


def rejects(e, argv, needle, before, *, commit=True, period=P):
    """None if the command was REJECTED (exit 1) naming `needle`, stored
    nothing, wrote no ledger row, and (with --commit) wrote one partial
    run-log row; else the problem."""
    rc, text, logged = e.cmd(argv)
    if rc != 1 or "REJECTED" not in text or needle not in text:
        return f"rc {rc}, not REJECTED naming {needle!r}: {text[-140:]}"
    if rejected_state(e.cur, period) != before:
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


def gate_9_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row")
    problems = []

    def case(label, setup, needle, commit=True, period=P, extra=()):
        def body(c):
            with env(c) as e, tl.specs():
                setup(e)
                before = rejected_state(c, period)
                argv = ["load"] + (["--commit"] if commit else []) + list(
                    extra)
                bad = rejects(e, argv, needle, before, commit=commit,
                              period=period)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)

    def seed_rev(e, **kw):
        e.seed(e.cur)
        e.file(published=date(2026, 8, 5), **kw)

    def seed_new(e, **kw):
        e.seed(e.cur)
        e.file(2026, **kw)
    first = value("E06000002", 2025)
    case("revision: any change, listed, needs --acknowledge",
         lambda e: seed_rev(e, values={("E06000002", 2025): first + 1}),
         "--acknowledge 2025")
    case("revision: an authority moves by more than the area limit",
         lambda e: seed_rev(e, values={("E06000001", 2025): 200000}),
         m.LARGE, extra=("--acknowledge", P))
    case("revision: the national total moves by more than the limit",
         lambda e: seed_rev(e, values={("E09000033", 2025): value(
             "E09000033", 2025) + 4500}),
         "national population", extra=("--acknowledge", P))
    case("revision: preview of the same writes no run-log row",
         lambda e: seed_rev(e, values={("E06000002", 2025): first + 1}),
         "--acknowledge 2025", commit=False)
    case("new year: the national total moves by more than the limit",
         lambda e: seed_new(e, values={("E09000033", 2026): value(
             "E09000033", 2026) + 20000}),
         "national population", period="2026")
    case("new year: one authority moves by more than the area limit",
         lambda e: seed_new(e, values={("E06000001", 2026): 150000}),
         "E06000001", period="2026")
    case("two files with the held file's rank and other content",
         lambda e: (e.seed(e.cur), e.file(values={("E06000002", 2025): 40})),
         "two files claim the same release", extra=("--acknowledge", P))

    def partial(c):
        with env(c) as e, tl.specs():
            e.seed(c)
            tip = m.records(c, ZZ, P, 1)
            part = tip[:-1]
            out = m.period_problems(part, tip, None, kind="revised")
            if not any(x.startswith(m.PARTIAL) and "missing from the file"
                       in x for x in out):
                problems.append(f"a file missing an authority: {out}")
            out = m.period_problems(part, None, tip, kind="new")
            if not any(x.startswith(m.PARTIAL) and "authorities, expected"
                       in x for x in out):
                problems.append(f"a new year of fewer authorities: {out}")
            # nothing releases a partial file
            e.file(2026, codes=CODES[:-1])
            before = state(c)
            rc, text, logged = e.cmd(["load", "--commit", "--acknowledge",
                                      "2025", "--acknowledge", "2026",
                                      "--accept-large-revision", "2025"])
            if not (rc == 1 and m.PARTIAL in text
                    and "2026: REJECTED" in text and state(c) == before):
                problems.append(f"--acknowledge released a partial file: rc "
                                f"{rc}")
            # both sides of a limit: +1 on one cell is within the numeric
            # limits (only the any-change stop, which --acknowledge releases)
            ok = [dict(r) for r in tip]
            ok[0]["population"] += 1
            out = m.period_problems(ok, tip, None, kind="revised")
            if len(out) != 1 or "the held edition is revised" not in out[0]:
                problems.append(f"a one-cell revision: {out}")
            if m.period_problems([dict(r) for r in tip], tip, None,
                                 kind="revised"):
                problems.append("an identical revision was stopped")
    _in_savepoint(cur, partial)

    def limits(c):
        # exact limits on synthetic records: a move of exactly the limit is
        # fine, one basis point past it is stopped
        with tl.specs():
            def recs(vals):
                return [{m.KEY: f"E06{i:06d}", m.PERIOD: P, "population": v,
                         "la_name": f"A{i}"} for i, v in enumerate(vals)]
            tip = recs([100000] * N)
            at = recs([100000 + 100000 * m.REV_AREA_BP // 10000] + [100000] * (
                N - 1))
            past = recs([100000 + 100000 * m.REV_AREA_BP // 10000 + 1]
                        + [100000] * (N - 1))
            if any(x.startswith(m.LARGE) and "authorit" in x
                   for x in m.period_problems(at, tip, None, kind="revised")):
                problems.append("a move of exactly the revised-area limit "
                                "was stopped as large")
            if not any(x.startswith(m.LARGE) and "authorit" in x
                       for x in m.period_problems(past, tip, None,
                                                  kind="revised")):
                problems.append("a move past the revised-area limit was not "
                                "stopped as large")
            big = recs([100000] * N)
            nat = [dict(r) for r in big]
            nat[0]["population"] += 100000 * N * m.REV_TOTAL_BP // 10000
            if any("national" in x for x in m.period_problems(
                    nat, big, None, kind="revised")):
                problems.append("a national move of exactly the limit was "
                                "stopped")
            nat[0]["population"] += 1
            if not any("national" in x for x in m.period_problems(
                    nat, big, None, kind="revised")):
                problems.append("a national move past the limit was not "
                                "stopped")
            prev = recs([100000] * N)
            far = [dict(r) for r in prev]
            far[0]["population"] = 100000 + 100000 * m.NEW_AREA_BP // 10000
            if any("population moves by more than" in x
                   for x in m.period_problems(far, None, prev, kind="new")):
                problems.append("a move of exactly the new-year area limit "
                                "was stopped")
            far[0]["population"] += 1
            if not any("population moves by more than" in x
                       for x in m.period_problems(far, None, prev,
                                                  kind="new")):
                problems.append("a move past the new-year area limit was "
                                "not stopped")
    _in_savepoint(cur, limits)

    def within(c):
        with env(c) as e, tl.specs():
            e.seed(c)
            e.file(2026)
            rc, text, _ = e.cmd(["load", "--commit"])
            if rc != 0 or tl.editions(c, "2026") != [(1, None, N)]:
                problems.append(f"a new year within the limits was refused: "
                                f"rc {rc}")
    _in_savepoint(cur, within)
    report(9, name, not problems, "a revision (any change; an authority "
           "beyond 7.68% or the national total beyond 0.5% only with "
           "--accept-large-revision), a new year past the national or "
           "authority limit and two files of one rank are each REJECTED "
           "(exit 1) with nothing stored, no ledger row and one partial "
           "run-log row (none in a preview); a partial file is never "
           "released by a flag; the limits are exact on both sides; a new "
           "year within the limits is stored" if not problems
           else "; ".join(problems[:3]))


def gate_10_one_transaction(cur):
    name = ("a new year: edition, live rows and ledger row in one "
            "transaction")
    problems = []

    def good(e):
        e.file()
        before = state(e.cur)
        rc, text, logged = e.cmd(["load", "--commit"])
        led = tl.ledger(e.cur)
        if rc != 0 or tl.editions(e.cur) != [(1, None, N)] \
                or tl.count(e.cur, ZZ.live_table) != N \
                or [(o, ed) for _, _, o, ed in led] != [("new", 1)] \
                or state(e.cur) == before:
            problems.append(f"a new year did not store edition 1, {N} "
                            f"live rows and one 'new' ledger row: rc {rc} "
                            f"{led}")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the load")
        if core.rows_differing(e.cur, ZZ, P, 1):
            problems.append("live differs from edition 1")

    def failing(label, target, attr):
        def body(c):
            with env(c) as e, tl.specs():
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
    scenario(cur, good)
    failing("a failing ledger insert", pe, "record_file_check")
    failing("a failing live insert", pe, "insert_live")
    failing("a failing edition insert", core, "insert_edition")
    report(10, name, not problems, "edition 1, the live rows and one 'new' "
           "ledger row are stored together; a failing ledger insert, live "
           "insert or edition insert rolls all three back and logs a "
           "partial run" if not problems else "; ".join(problems[:3]))


def gate_11_ledger_skip(cur):
    name = ("ledger skip needs the (URL, sha256) pair for every period the "
            "file states")
    problems = []

    def other_period(e):
        e.seed(e.cur)
        path = e.file(2026)
        rank = (date(2027, 7, 29), 2026)
        # the file's (URL, sha) recorded for 2025 only, not for 2026
        e.cur.execute(f"INSERT INTO public.{LEDGER} (reference_year, "
                      "source_file, file_sha256, outcome, edition) VALUES "
                      "(2025, %s, %s, 'unchanged', 1)",
                      (m.ledger_source(e.last_url, rank, [2025, 2026]),
                       m.content_sha256(path)))
        rc, text, _ = e.cmd(["load"])
        if rc != 0 or "nothing parsed" in text or "2026: new" not in text:
            problems.append("a pair held for one of two periods skipped the "
                            "file")
    scenario(cur, other_period)

    def same_name(e):
        path0 = e.file()
        e.ok(e.cur, ["load", "--commit"])
        url0 = e.last_url
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "nothing parsed" not in text or logged.called:
            problems.append("a held pair did not skip the read")
        # the same URL with other bytes: the sha differs, so it is read
        e.file(published=date(2026, 8, 5), values={("E06000002", 2025): 40})
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" in text or "2025: REJECTED" not in text:
            problems.append("the same URL with other bytes was skipped")
        # the held pair again: skipped; --recheck reads it anyway
        e.urls[url0] = path0
        e.page(2025, path0.name)
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" not in text:
            problems.append("the held pair no longer skips")
        rc, text, _ = e.cmd(["load", "--recheck", P])
        if f"{P}: unchanged" not in text:
            problems.append("--recheck did not read a ledger-held file")
    scenario(cur, same_name)
    report(11, name, not problems, "a ledger pair held for only one of the "
           "periods the file states does not skip; the held pair skips with "
           "nothing parsed or logged; the same URL with other bytes is "
           "read; --recheck reads a held file" if not problems
           else "; ".join(problems[:3]))


def gate_12_preview(cur):
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
                problems.append(f"new year {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.file(published=date(2026, 8, 5), values={
            ("E06000002", 2025): value("E06000002", 2025) + 1})
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"],
                     ["load", "--simulate", "--acknowledge", P],
                     ["load", "--acknowledge", P], ["status"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}, state kept="
                                f"{state(e.cur) == before}")
        e.ok(e.cur, ["load", "--commit", "--acknowledge", P])
        before = state(e.cur)
        for argv in (["refresh-latest"], ["restore-edition", P, "1"],
                     ["refresh-latest", "--simulate"],
                     ["restore-edition", P, "1", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"{argv} after a revision: rc {rc}")
    scenario(cur, body)

    def legacy(e, paths, files, loaded):
        before = state(e.cur)
        with patched_legacy(e, loaded, files):
            for argv in (["migrate-legacy", *paths],
                         ["migrate-legacy", *paths, "--simulate"]):
                rc, text, logged = e.cmd(argv)
                if rc != 0 or state(e.cur) != before or logged.called:
                    problems.append(f"{argv[0]} {argv[-1:]}: rc {rc}")
    legacy_world(cur, legacy)
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    if cur.fetchone()[0] != runs:
        problems.append("the run log changed")
    report(12, name, not problems, "load (also with --acknowledge), "
           "status, refresh-latest, restore-edition and migrate-legacy in "
           "preview and --simulate leave the editions, the live table and "
           "the ledger as they were and log nothing" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 13

def real_held_reread(cur, spec=REAL, files=None, raw_dir=None,
                     forms=None):
    """Each held workbook read again gives edition 1 'as loaded': the content
    sha256 of the rebuilt rows (every authority, population and la_name) is
    edition 1's, the held name forms named in LEGACY_NAME_FORMS apart (those
    four 2024 names are held in the NOMIS form, so the 2024 workbook is
    compared with its names swapped, and has no ledger row, which is why a
    re-read compares it); the ledger records a byte-identical file as
    'unchanged'; and edition 1 names the file's rank. So a re-read by the
    page, --release or --file is unchanged against edition 1 (and against
    the tip while the tip is edition 1)."""
    _need(cur, spec)
    forms = m.LEGACY_NAME_FORMS if forms is None else forms
    held, problems = read_held(files, raw_dir)
    bad = list(problems)
    parts = []
    for year, (path, f) in held.items():
        cur.execute(f"SELECT DISTINCT release_label, source_file FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = 1", (int(year),))
        rows = cur.fetchall()
        if len(rows) != 1:
            bad.append(f"{year}: edition 1 missing or not single")
            continue
        label, sf = rows[0]
        held_recs = m.records(cur, spec, year, 1)
        with contextlib.redirect_stdout(io.StringIO()):
            built = m._build(cur, f, [year])[year]
        swap = {c: v[0] for c, v in forms.get(year, {}).items()}
        a = m.rows_content_sha(held_recs)
        b = m.rows_content_sha([dict(r, la_name=swap.get(r["lad24cd"],
                                                         r["la_name"]))
                                for r in built])
        if a != b:
            bad.append(f"{year}: edition 1 content sha {a[:12]} differs from "
                       f"the held workbook read now {b[:12]}")
        if label != core.AS_LOADED_LABEL:
            bad.append(f"{year}: edition 1 is not 'as loaded' ({label!r})")
        if m.release_rank(sf) != f["rank"]:
            bad.append(f"{year}: edition 1 source_file ranks "
                       f"{m.release_rank(sf)}, the file {f['rank']}")
        cur.execute(f"SELECT outcome, edition FROM public."
                    f"{ledger_name(spec)} WHERE {spec.period_col} = %s AND "
                    "file_sha256 = %s", (int(year), m.content_sha256(path)))
        led = cur.fetchall()
        if forms.get(year):
            if led:
                bad.append(f"{year}: the workbook differs from edition 1 in "
                           "held name forms, so it should have no ledger row")
            parts.append(f"{year} {a[:12]}.. equal but for "
                         f"{len(forms[year])} named name form(s), no ledger "
                         "row")
        else:
            if ("unchanged", 1) not in led:
                bad.append(f"{year}: no 'unchanged' ledger row for the "
                           "held workbook")
            parts.append(f"{year} {a[:12]}.. ({len(held_recs)} rows) equal, "
                         "ledger 'unchanged'")
    return not bad, "; ".join(bad[:3]) if bad else (
        "the held workbooks read now give edition 1's content sha256: "
        + "; ".join(parts))


def reread_after_migration(e, paths, files):
    """Problems (a list) when the migrated state does not treat the same
    bytes as unchanged on every path. The newest held workbook is the one
    with a ledger row (a workbook with held name forms has none)."""
    bad = []
    path = paths[-1]
    url = e.page(int(path.name[3:5]) + 2000, path.name)
    e.urls[url] = path
    rc, text, logged = e.cmd(["load"])
    if rc != 0 or "nothing parsed" not in text or logged.called:
        bad.append(f"the page load after migrate-legacy: rc {rc}: "
                   f"{text[-100:]}")
    for argv in (["load", "--recheck", P],
                 ["load", "--release", P, "--recheck", P],
                 ["load", "--file", path],
                 ["load", "--file", path, "--no-page"],
                 ["load", "--file", path, "--commit"],
                 ["load", "--file", path, "--no-page", "--recheck", P,
                  "--commit"],
                 ["load", "--recheck", P, "--commit"]):
        rc, text, logged = e.cmd(argv)
        if rc != 0 or f"{P}: unchanged" not in text or "REJECTED" in text \
                or "two files claim" in text:
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


def gate_13_held_file_recheck(cur):
    name = ("a byte-identical re-read is unchanged on every path against "
            "edition 1 'as loaded'")
    problems = []
    out = {}

    def held(e, paths, files, loaded):
        with patched_legacy(e, loaded, files):
            rc, text, _ = e.cmd(["migrate-legacy", *paths, "--commit"])
        if rc != 0:
            problems.append(f"migrate-legacy: {text[-120:]}")
            return
        d = Path(e.root) / "disk"
        d.mkdir()
        for p in paths:
            (d / p.name).write_bytes(p.read_bytes())
        out["held"] = real_held_reread(
            e.cur, ZZ, files, d, forms={})
        problems.extend(f"held: {x}"
                        for x in reread_after_migration(e, paths, files))
    legacy_world(cur, held)
    if not out.get("held", (False,))[0]:
        problems.append(f"held: {out.get('held')}")

    def forms_world(e, paths, files, loaded):
        forms = {"2024": {"E06000002": ("Middlesbrough Borough",
                                        "Middlesbrough")}}
        with patched_legacy(e, loaded, files), \
                mock.patch.object(m, "LEGACY_NAME_FORMS", forms):
            rc, text, _ = e.cmd(["migrate-legacy", *paths, "--commit"])
        if rc != 0:
            problems.append(f"migrate-legacy with a name form: {text[-120:]}")
            return
        d = Path(e.root) / "disk"
        d.mkdir()
        for p in paths:
            (d / p.name).write_bytes(p.read_bytes())
        ok, detail = real_held_reread(e.cur, ZZ, files, d, forms=forms)
        if not ok:
            problems.append(f"with a named name form: {detail}")
        ok, detail = real_held_reread(e.cur, ZZ, files, d, forms={})
        if ok or "differs" not in detail:
            problems.append("an unnamed name form was called equal")
    legacy_world(cur, forms_world, years=(2024, 2025),
                 names={(2024, "E06000002"): "Middlesbrough Borough"})

    def changed(e, paths, files, loaded):
        with patched_legacy(e, loaded, files):
            e.cmd(["migrate-legacy", *paths, "--commit"])
        other = write_file(Path(e.root) / "disk2" / paths[0].name, 2025,
                           values={("E06000002", 2025): 40})
        ok, detail = real_held_reread(
            e.cur, ZZ, {"2025": {"sha256": m.content_sha256(other)}},
            other.parent, forms={})
        if ok or "differs" not in detail:
            problems.append("a file with other content was called equal")
        ok, detail = real_held_reread(e.cur, ZZ, {"2025": {
            "sha256": "0" * 64}}, other.parent, forms={})
        if ok or "no file" not in detail:
            problems.append("a missing file was called equal")
    legacy_world(cur, changed)
    seeded = not problems
    sd = ("seeded: after migrate-legacy the same bytes read by the page, "
          "--release, --file, --file --no-page, --recheck and with --commit "
          "are unchanged, store no edition and leave every ledger row "
          "'unchanged'; a workbook with a named held name form compares "
          "equal only with that form named; a file with other content or "
          "none is not equal" if seeded else "; ".join(problems[:3]))
    mixed(13, name, seeded, sd, cur, real_held_reread)


# ---------------------------------------------------------------- gate 14

def gate_14_restated_year(cur):
    name = ("a restated back year is compared and needs --acknowledge "
            "naming it; --accept-large-revision needs --acknowledge; an "
            "acknowledgement must name a compared year")
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.live_val(e.cur, "E06000002")
        # a 2026 file restating 2025 differently: 2026 is a new year and is
        # stored, 2025 is compared and REJECTED
        e.file(2026, values={("E06000002", 2025): first + 2})
        rc, text, logged = e.cmd(["load", "--commit"])
        if not (rc == 1 and "2025: REJECTED" in text
                and "--acknowledge 2025" in text and "2026: new" in text
                and tl.editions(e.cur) == [(1, None, N)]
                and tl.editions(e.cur, "2026") == [(1, None, N)]
                and logged.called and "rejected ['2025'"
                in logged.call_args[0][2]):
            problems.append(f"a restated year without --acknowledge: rc "
                            f"{rc}: {text[-160:]}")
        before = state(e.cur)
        ok, text = e.halts(["load", "--commit", "--acknowledge", "2019"],
                           before)
        if not ok:
            problems.append("an acknowledgement for a year the file does "
                            "not state did not halt")
        ok, text = e.halts(["load", "--commit", "--accept-large-revision",
                            "2019"], before)
        if not ok:
            problems.append("--accept-large-revision for a year the file "
                            "does not state did not halt")
        rc, text, logged = e.cmd(["load", "--commit", "--acknowledge", "2025"])
        eds = tl.editions(e.cur)
        if rc != 0 or "2025: revised" not in text or "ACKNOWLEDGED" not in \
                text or eds != [(1, None, N), (2, 1, N)] or \
                tl.editions(e.cur, "2026") != [(1, None, N)] or \
                tl.live_val(e.cur, "E06000002") != first or \
                "ACKNOWLEDGED 2025" not in logged.call_args[0][2]:
            problems.append(f"--acknowledge 2025: rc {rc}, {eds}: "
                            f"{text[-160:]}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE reference_year = 2025 AND "
                      "edition = 2")
        lab = e.cur.fetchall()
        if len(lab) != 1 or "ACKNOWLEDGED" not in lab[0][0]:
            problems.append("the edition's label does not record the "
                            "acknowledgement")
        if pe.status(e.cur, m.profile())["pending_refresh"] != [P]:
            problems.append("status does not name the pending refresh")
        # the same file read again is unchanged against edition 2
        rc, text, _ = e.cmd(["load", "--commit", "--recheck", P])
        if rc != 0 or f"{P}: unchanged" not in text or tl.editions(
                e.cur) != [(1, None, N), (2, 1, N)]:
            problems.append(f"the acknowledged file read again: rc {rc}: "
                            f"{text[-120:]}")
        # a large revision needs both flags
        e.file(2027, values={("E06000002", 2025): first + 2,
                             ("E06000002", 2026): value("E06000002", 2026)
                             * 2})
        before = state(e.cur)
        rc, text, _ = e.cmd(["load", "--commit", "--acknowledge", "2026"])
        if not (rc == 1 and m.LARGE in text
                and "--accept-large-revision 2026" in text
                and tl.editions(e.cur, "2026") == [(1, None, N)]):
            problems.append(f"a large revision with --acknowledge alone: rc "
                            f"{rc}: {text[-160:]}")
        rc, text, _ = e.cmd(["load", "--commit", "--acknowledge", "2026",
                             "--accept-large-revision", "2026"])
        if rc != 0 or tl.editions(e.cur, "2026") != [(1, None, N),
                                                     (2, 1, N)]:
            problems.append(f"a large revision with both flags: rc {rc}: "
                            f"{text[-160:]}")
    scenario(cur, body)
    report(14, name, not problems, "an identical restated year is "
           "unchanged; a changed one is REJECTED naming --acknowledge 2025 "
           "with nothing stored; an acknowledgement of a year the file does "
           "not state halts; --acknowledge 2025 stores edition 2 (superseding "
           "1, labelled ACKNOWLEDGED, live waiting for refresh-latest) and "
           "the same file read again is unchanged; a large revision needs "
           "--accept-large-revision as well" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 15

def gate_15_revision_and_refresh(cur):
    name = ("a revision, then refresh-latest: only that year and row "
            "change, loaded_at is copied")
    problems = []

    def rows_hash(c, period):
        c.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                  f"FROM public.{ZZ.live_table} t WHERE reference_year = %s",
                  (period,))
        return c.fetchone()[0]

    def by_key(c, period):
        c.execute(f"SELECT lad24cd, population, la_name FROM public."
                  f"{ZZ.live_table} WHERE reference_year = %s", (period,))
        return {r[0]: r for r in c.fetchall()}

    def body(e):
        e.seed(e.cur, year=2024)
        e.seed(e.cur)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        prior_before = rows_hash(e.cur, 2024)
        before = by_key(e.cur, 2025)
        first = tl.live_val(e.cur, "E06000002")
        e.file(published=date(2026, 8, 5),
               values={("E06000002", 2025): first + 3})
        rc, text, _ = e.cmd(["load", "--commit", "--acknowledge", P])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N), (2, 1, N)] or \
                tl.editions(e.cur, "2024") != [(1, None, N)]:
            problems.append(f"the revision did not store edition 2 of {P} "
                            f"only: rc {rc}")
        if by_key(e.cur, 2025) != before:
            problems.append("load changed the live table before "
                            "refresh-latest")
        if pe.status(e.cur, m.profile())["pending_refresh"] != [P]:
            problems.append("status does not name the pending refresh")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called or \
                "2025=1" not in text:
            problems.append("the refresh-latest preview wrote or did not "
                            "plan the change")
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if rc != 0:
            problems.append(f"refresh-latest --commit: rc {rc}: "
                            f"{text[-120:]}")
        after = by_key(e.cur, 2025)
        changed = [k for k in after if after[k] != before.get(k)]
        if changed != ["E06000002"] or after["E06000002"][1] != first + 3:
            problems.append(f"refresh-latest changed {changed[:3]}, "
                            "expected that one row")
        if rows_hash(e.cur, 2024) != prior_before:
            problems.append("refresh-latest changed the other year")
        e.cur.execute(f"SELECT loaded_at FROM public.{ZZ.editions_table} "
                      "WHERE reference_year = 2025 AND edition = 2 LIMIT 1")
        ed_loaded = e.cur.fetchone()[0]
        e.cur.execute(f"SELECT lad24cd FROM public.{ZZ.live_table} WHERE "
                      "reference_year = 2025 AND loaded_at = %s",
                      (ed_loaded,))
        if e.cur.fetchall() != [("E06000002",)]:
            problems.append("loaded_at was not copied to exactly the "
                            "refreshed row")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "reference_year = 2025 AND loaded_at <> '2026-01-01' "
                      "AND lad24cd <> 'E06000002'")
        if e.cur.fetchone()[0]:
            problems.append("an unchanged row's loaded_at moved")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "reference_year = 2024 AND loaded_at <> '2026-01-01'")
        if e.cur.fetchone()[0]:
            problems.append("the other year's loaded_at moved")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("a second refresh-latest plans a write")
        # drift: a live row that equals no edition halts without
        # --accept-drift and the guard names the year
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET population = "
                      "population + 1 WHERE lad24cd = 'E09000033' AND "
                      "reference_year = 2024")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if not (rc in ("halt", 1) and state(e.cur) == snap
                and not logged.called):
            problems.append(f"a drifted live year was refreshed: rc {rc}")
    scenario(cur, body)
    report(15, name, not problems, "a revised 2025 waits for refresh-latest "
           "(preview writes nothing); the commit changes exactly the "
           "revised row, leaves the 2024 rows alone and copies the edition's "
           "loaded_at to that row only; a second refresh plans nothing; a "
           "drifted live year is not overwritten" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 16

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def all_rows_sha(rows):
    """sha256 of every live row: lad24cd|reference_year|population|la_name,
    sorted, LF-joined, UTF-8 (the before-state-all record)."""
    lines = sorted(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}" for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def w1_read_sha(recs):
    """sha256 of what W1 reads of a year: lad24cd|population, sorted,
    LF-joined, UTF-8 (sql/w1/05_la_signals.sql reads p.population)."""
    lines = sorted(f"{r[m.KEY]}|{r['population']}" for r in recs)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _note_lines(note, label, period=True):
    """{year: (rows, sha256)} from lines `<label> la_population <year>
    rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the decision
    note (the two halves joined), or {}. A line with another label (for
    example before-state-all) is a different record and does not match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                rf"(?m)^{re.escape(label)} la_population ([0-9]{{4}}) "
                r"rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text)}


def note_hashes(note):
    return _note_lines(note, "before-state")


def w1_note_hashes(note):
    return _note_lines(note, "w1-read")


def note_all_hash(note):
    """(rows, sha256) of the `before-state-all la_population rows=.. ..`
    line, or None."""
    p = Path(note)
    if not p.exists():
        return None
    mt = re.search(r"(?m)^before-state-all la_population rows=([0-9]+) "
                   r"sha256-first32=([0-9a-f]{32}) "
                   r"sha256-last32=([0-9a-f]{32})\s*$",
                   p.read_text(encoding="utf-8"))
    return (int(mt.group(1)), mt.group(2) + mt.group(3)) if mt else None


def _live_rows(cur, spec):
    cur.execute(f"SELECT lad24cd, reference_year, population, la_name FROM "
                f"public.{spec.live_table}")
    return cur.fetchall()


def note_lines_for(cur, spec=REAL):
    """The decision note's hash lines for the live table as it stands
    (read-only): a before-state line per year, the before-state-all line and
    the w1-read line of the newest year. Print them before the migration."""
    out = []
    periods = _live_periods(cur, spec)
    for p in periods:
        recs = m.records(cur, spec, p)
        out.append(f"before-state la_population {p} rows={len(recs)} "
                   f"{hash_fields(m.rows_content_sha(recs))}")
    rows = _live_rows(cur, spec)
    out.append(f"before-state-all la_population rows={len(rows)} "
               f"{hash_fields(all_rows_sha(rows))}")
    if periods:
        recs = m.records(cur, spec, periods[-1])
        out.append(f"w1-read la_population {periods[-1]} rows={len(recs)} "
                   f"{hash_fields(w1_read_sha(recs))}")
    return out


def real_edition1_hash(cur, spec=REAL, note=NOTE, legacy_rows=None):
    """Edition 1 'as loaded' of every migrated year equals the before-state
    line in the decision note: the row count and rows_content_sha (lad24cd,
    population, la_name); the rows over all years are the surveyed live
    count; and, when the note has it, the before-state-all line equals the
    edition 1 rows of all years."""
    _need(cur, spec)
    legacy_rows = m.LEGACY_LIVE[0] if legacy_rows is None else legacy_rows
    recorded = note_hashes(note)
    bad, parts, total = [], [], 0
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "release_label = %s ORDER BY 1", (core.AS_LOADED_LABEL,))
    migrated = [str(r[0]) for r in cur.fetchall()]
    if not migrated:
        bad.append("no edition 1 'as loaded' (migrate-legacy not run)")
    all_rows = []
    for p in migrated:
        recs = m.records(cur, spec, p, 1)
        n, h = len(recs), m.rows_content_sha(recs)
        total += n
        all_rows += [(r["lad24cd"], p, r["population"], r["la_name"])
                     for r in recs]
        if Path(note).exists():
            if p not in recorded:
                bad.append(f"the decision note records no 'before-state "
                           f"la_population {p} rows=.. sha256-first32=.. "
                           "sha256-last32=..' line")
            elif recorded[p] != (n, h):
                bad.append(f"{p}: edition 1 {n} rows sha256 {h[:16]}.. "
                           f"differs from the note's {recorded[p][0]} rows "
                           f"{recorded[p][1][:16]}..")
        parts.append(f"{p} {n:,} rows {h[:16]}..")
    if migrated and total != legacy_rows:
        bad.append(f"edition 1 holds {total} rows over {migrated}, surveyed "
                   f"{legacy_rows}")
    for p in recorded:
        if p not in migrated:
            bad.append(f"the note has a line for {p}, which has no edition "
                       "1 as loaded")
    allrec = note_all_hash(note)
    if allrec is not None and allrec != (len(all_rows),
                                         all_rows_sha(all_rows)):
        bad.append("the note's before-state-all line differs from edition 1 "
                   "over all years")
    return not bad, "; ".join(bad[:3]) if bad else (
        "edition 1 of every migrated year hashes to the before-state line "
        "in the decision note (" + "; ".join(parts) + ")"
        + ("; before-state-all agrees" if allrec else ""))


def gate_16_before_state_hash(cur):
    name = ("edition 1 equals the before-state hash recorded in the "
            "decision note")
    out = {}

    def body(e, paths, files, loaded):
        pre = e.cur
        live_before = {p: m.records(pre, ZZ, p) for p in ("2025",)}
        with patched_legacy(e, loaded, files):
            rc, text, _ = e.cmd(["migrate-legacy", *paths, "--commit"])
        if rc != 0:
            out["error"] = text[-200:]
            return
        recs = m.records(e.cur, ZZ, P, 1)
        h = m.rows_content_sha(recs)
        out["same"] = h == m.rows_content_sha(live_before[P]) == \
            m.rows_content_sha(m.records(e.cur, ZZ, P))
        allr = [(r["lad24cd"], P, r["population"], r["la_name"])
                for r in recs]
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = (f"before-state la_population {P} rows={N} "
                    f"{hash_fields(h)}")
            allline = (f"before-state-all la_population rows={N} "
                       f"{hash_fields(all_rows_sha(allr))}")
            note.write_text(f"intro\n{line}\n{allline}\n", encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(note.read_text().replace(h[:32], "0" * 32, 1),
                            encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(allline.replace("before-state-all",
                                            "before-state-all")
                            + "\n", encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}"),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, N)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                N)
            note.write_text(line, encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(e.cur, ZZ, note, N + 5)
            note.write_text(line + "\n" + allline.replace(
                allline[-10:], "0" * 10) + "\n", encoding="utf-8")
            out["bad_all"] = real_edition1_hash(e.cur, ZZ, note, N)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET population = "
                      "population + 1 WHERE lad24cd = 'E06000002'")
        out["changed"] = (m.rows_content_sha(m.records(e.cur, ZZ, P))
                          != h)
        with patched_legacy(e, loaded, files):
            rc, text, _ = e.cmd(["migrate-legacy", *paths, "--commit"])
        out["twice"] = rc == "halt" and ("runs once" in text
                                         or "not as surveyed" in text)
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(16, name, False, _first(ex))
    ok = (out.get("same") and out.get("ok", (0,))[0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and not out["bad_all"][0]
          and out.get("changed") and out.get("twice"))
    mixed(16, name, ok,
          "a seeded migration stores an edition 1 whose hash equals the "
          "live rows' and the note's line; a wrong hash, a wrong row "
          "count, a missing line (a before-state-all line does not count), "
          "a wrong before-state-all line and a missing note fail; a later "
          "change shows; a second migration is refused" if ok
          else f"seeded: {out}", cur, real_edition1_hash)


# ---------------------------------------------------------------- gate 17

def real_w1_read(cur, spec=REAL, note=NOTE, year=P, rows=None):
    """The live rows of the map's year (2025): the count and w1_read_sha of
    (lad24cd, population) equal the decision note's w1-read line. This is
    what sql/w1/05_la_signals.sql reads (p.population at MAX
    reference_year); the first load must not move it."""
    recorded = w1_note_hashes(note).get(year)
    if not Path(note).exists():
        return False, f"decision note {Path(note).name} not written"
    if recorded is None:
        return False, (f"the decision note records no 'w1-read "
                       f"la_population {year} rows=.. sha256-first32=.. "
                       "sha256-last32=..' line")
    recs = m.records(cur, spec, year)
    n, h = len(recs), w1_read_sha(recs)
    if rows is not None and n != rows:
        return False, f"live {year} has {n} rows, expected {rows}"
    if (n, h) != recorded:
        return False, (f"live {year}: {n} rows sha256 {h[:16]}.. differs "
                       f"from the note's w1-read line {recorded[0]} rows "
                       f"{recorded[1][:16]}..")
    cur.execute(f"SELECT MAX({spec.period_col}) FROM public."
                f"{spec.live_table}")
    newest = cur.fetchone()[0]
    return True, (f"live {year}: {n} rows, (lad24cd, population) hashes to "
                  f"the note's w1-read line ({h[:16]}..); W1 reads the "
                  f"newest year, {newest}")


def gate_17_w1_read(cur):
    name = ("live population of the map's year equals the decision note's "
            "w1-read line (lad24cd + population)")
    out = {}

    def body(e):
        e.seed(e.cur)
        before = w1_read_sha(m.records(e.cur, ZZ, P))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = f"w1-read la_population {P} rows={N} {hash_fields(before)}"
            note.write_text(line + "\n", encoding="utf-8")
            kw = {"note": note, "year": P}
            out["base"] = real_w1_read(e.cur, ZZ, **kw)
            # la_name is not what W1 reads: a name change keeps the hash
            e.cur.execute(f"UPDATE public.{ZZ.live_table} SET la_name = 'x' "
                          "WHERE lad24cd = 'E06000002'")
            out["after_name"] = real_w1_read(e.cur, ZZ, **kw)
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(f"UPDATE public.{ZZ.live_table} SET population = "
                          "population + 1 WHERE lad24cd = 'E06000002'")
            out["after_population"] = real_w1_read(e.cur, ZZ, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd "
                          "= 'E06000002'")
            out["after_delete"] = real_w1_read(e.cur, ZZ, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            note.write_text("", encoding="utf-8")
            out["no_line"] = real_w1_read(e.cur, ZZ, **kw)
            note.write_text(line.replace("w1-read", "w1-read-all") + "\n",
                            encoding="utf-8")
            out["other_label"] = real_w1_read(e.cur, ZZ, **kw)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}")
                            + "\n", encoding="utf-8")
            out["bad_rows"] = real_w1_read(e.cur, ZZ, **kw)
            out["no_note"] = real_w1_read(e.cur, ZZ, note=Path(tmp) / "x",
                                          year=P)
    scenario(cur, body)
    ok = (out["base"][0] and out["after_name"][0]
          and not out["after_population"][0] and not out["after_delete"][0]
          and not out["no_line"][0] and not out["other_label"][0]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1])
    mixed(17, name, ok, "the seeded line matches the live year; a changed "
          "population or a missing authority, a missing or mislabelled line, "
          "a wrong row count and a missing note fail; a changed la_name does "
          "not move what W1 reads" if ok else f"seeded: {out}", cur,
          real_w1_read)


# ---------------------------------------------------------------- gate 18

def real_migration_proof(cur, spec=REAL, files=None, raw_dir=None,
                         forms=None):
    """The migration proof re-run read-only from the workbooks on disk
    (found by sha256): the parser on each held workbook reproduces every
    value of edition 1 'as loaded' (lad24cd, population, la_name; 0
    differences but the held name forms named in LEGACY_NAME_FORMS) and the
    workbook reconciles for the year."""
    _need(cur, spec)
    forms = m.LEGACY_NAME_FORMS if forms is None else forms
    held, problems = read_held(files, raw_dir)
    if problems:
        return False, problems[0]
    parts = []
    for year, (path, f) in held.items():
        bad = m.reconcile(f, [int(year)])
        if bad:
            return False, f"{path.name}: reconciliation failed: {bad[0]}"
        held_recs = m.records(cur, spec, year, 1)
        if not held_recs:
            return False, f"{year}: no edition 1"
        with contextlib.redirect_stdout(io.StringIO()):
            built = m._build(cur, f, [year])[year]
        cells, diffs, seen = m._proof(held_recs, built, forms.get(year, {}))
        cur.execute(f"SELECT DISTINCT release_label FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = 1", (int(year),))
        if cur.fetchall() != [(core.AS_LOADED_LABEL,)]:
            return False, f"{year}: edition 1 is not the 'as loaded' one"
        if diffs:
            return False, (f"{year}: {len(diffs)} differences, e.g. "
                           f"{diffs[:2]}")
        parts.append(f"{year}: {cells:,} values of edition 1 "
                     f"({len(held_recs)} rows) equal {path.name} read now "
                     f"(0 differences; {len(seen)} named name form(s))")
    return True, "; ".join(parts)


def gate_18_migration_proof(cur):
    name = ("the migration proof re-run read-only from the workbooks on "
            "disk; seeded: proof passes, a planted held difference stops "
            "migrate-legacy and stores nothing")
    problems = []

    def ok_world(e, paths, files, loaded):
        with patched_legacy(e, loaded, files):
            rc, text, _ = e.cmd(["migrate-legacy", *paths, "--commit"])
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        raw = Path(e.root) / "raw"
        raw.mkdir()
        for p in paths:
            (raw / p.name).write_bytes(p.read_bytes())
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw, forms={})
        if not ok or "0 differences" not in detail:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(
            e.cur, ZZ, {"2025": dict(files["2025"], sha256="0" * 64)}, raw,
            forms={})
        if ok or "no file" not in detail:
            problems.append("a file with another sha256 was accepted")
        # a file changed on disk is not found by sha256
        (raw / paths[0].name).write_bytes(paths[0].read_bytes() + b"\0")
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw, forms={})
        if ok or "no file" not in detail:
            problems.append("a changed file on disk was accepted")
        # a different, valid workbook stands in for the held one: edition 1
        # is not that file
        other = write_file(Path(e.root) / "raw2" / paths[0].name, 2025,
                           values={("E06000002", 2025): 40})
        ok, detail = real_migration_proof(
            e.cur, ZZ, {"2025": dict(files["2025"],
                                     sha256=m.content_sha256(other))},
            other.parent, forms={})
        if ok or "differences" not in detail:
            problems.append("a file that differs from edition 1 passed")

    def planted(sql, needle, surveyed_first=False):
        def f(e, paths, files, loaded):
            surveyed = e.legacy(e.cur, loaded) if surveyed_first else None
            e.cur.execute(sql)
            surveyed = surveyed or e.legacy(e.cur, loaded)
            before = state(e.cur)
            with mock.patch.object(m, "LEGACY_LIVE", surveyed[:2]), \
                    mock.patch.object(m, "LEGACY_LOADED", loaded), \
                    mock.patch.object(m, "LEGACY_FILES", files):
                rc, text, logged = e.cmd(["migrate-legacy", *paths,
                                          "--commit"])
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        f"UPDATE public.{ZZ.live_table} SET population = population + 1 "
        "WHERE lad24cd = 'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZZ.live_table} SET la_name = 'Mbro' WHERE lad24cd "
        "= 'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZZ.live_table} SET lad24cd = 'E06000003' WHERE "
        "lad24cd = 'E06000001'", "proof failed"))
    legacy_world(cur, planted(
        f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = 'E06000002'",
        "not as surveyed", surveyed_first=True))
    mixed(18, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the workbook by sha256; a changed or missing file and a file "
          "that is not edition 1 are caught; planted differences in either "
          "value and in a code, and a dropped row, stop migrate-legacy with "
          "nothing stored" if not problems else "; ".join(problems[:3]),
          cur, real_migration_proof)


# ---------------------------------------------------------- gates 19 - 21

def gate_19_rerun(cur):
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
    report(19, name, not problems, "a second load by the page, --release, "
           "--file and --file --no-page and a refresh-latest stored no "
           "edition and changed no live row (re-reads add 'unchanged' "
           "ledger rows only); a ledger-skipped load logs nothing"
           if not problems else "; ".join(problems[:3]))


def gate_20_stranded(cur):
    name = "stranded year repair (edition held, live rows missing)"
    problems = []

    def body(e):
        e.seed(e.cur)
        e.cur.execute(f"DELETE FROM public.{ZZ.live_table}")
        if pe.status(e.cur, m.profile())["live_missing"] != [P]:
            problems.append("status does not name the stranded year")
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0 or "live-missing" not in text or \
                tl.editions(e.cur) != [(1, None, N)] or \
                tl.count(e.cur, ZZ.live_table) != N:
            problems.append(f"repair failed: rc {rc}: {text[-120:]}")
        if "live-missing" not in [o for _, _, o, _ in tl.ledger(e.cur)]:
            problems.append("no live-missing ledger row")
        if core.rows_differing(e.cur, ZZ, P, 1) or not pe.status(
                e.cur, m.profile())["ok"]:
            problems.append("the repaired rows do not equal the tip")
    scenario(cur, body)
    report(20, name, not problems, f"a stranded {P} was rebuilt from its "
           "edition 1 with no new edition and a live-missing ledger row"
           if not problems else "; ".join(problems[:3]))


def gate_21_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.live_val(e.cur, "E06000002")
        e.file(published=date(2026, 8, 5),
               values={("E06000002", 2025): first + 3})
        e.ok(e.cur, ["load", "--commit", "--acknowledge", P])
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
        if tl.live_val(e.cur, "E06000002") != first + 3:
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.live_val(e.cur, "E06000002") != first or \
                core.rows_differing(e.cur, ZZ, P, 3):
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
    report(21, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') and applied by refresh-latest; the "
           "tip and a missing edition are refused; a preview writes nothing"
           if not problems else "; ".join(problems[:3]))


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
        with env(c) as e, tl.specs():
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.file()
                rc, _, _ = e.cmd(["load", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load"])
                raw = e.root / "raw"
                body_a = write_file(e.root / "dl" / "a.xlsx").read_bytes()
                body_b = write_file(e.root / "dl" / "b.xlsx", published=date(
                    2026, 8, 5)).read_bytes()
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
        return report(22, name, False, f"{type(ex).__name__}: {ex}")
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
    retired = retired_problems(retired_files())
    # the checker catches a planted import and a planted subprocess call
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.py"
        probe.write_text(
            f"import {RETIRED[0]}\n"
            f"x = ['python', 'scripts/{RETIRED[1]}_v2.py']\n"
            f"import subprocess\nsubprocess.run(['python -I {RETIRED[0]}"
            f".py'])\n"
            f"y = '{RETIRED[0]} did this once'\n", encoding="utf-8")
        planted = retired_problems([probe])
    checker_ok = len(planted) == 3
    ok = (not attempts and rc == rc_s == rc_r == rc_l == 0 and kept
          and runs == runs_after and real_before == real_after
          and not literal and not in_src and not in_out and not retired
          and checker_ok)
    report(22, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned "
           f"{rc}/{rc_s}/{rc_r}/{rc_l}; a same-named different download "
           f"was saved beside it, the first untouched={kept}; run-log rows "
           f"{runs}->{runs_after}; real editions table and ledger "
           f"unchanged={real_before == real_after}; secret-like literal in "
           f"source {literal or 'none'}; {len(secrets)} secret-named "
           f"settings checked against {len(files)} files and "
           f"{len(OUTPUT)} output lines: in source {in_src or 'none'}, in "
           f"output {in_out or 'none'}; retired scripts imported or named in "
           f"{len(retired_files())} gate/test/loader files: "
           f"{retired[:2] or 'none'} (the AST check catches a planted import "
           f"and path: {checker_ok})")


# ---------------------------------------------------------------- gate 23

def discovery_problems(names):
    """Problems in refresh_map's input discovery against a set of table
    names {name: has loaded_at}: la_population must be an input, the
    editions tables and la_boundaries must not."""
    import refresh_map
    cur = mock.Mock()
    cur.fetchall.return_value = [(t, True) for t in names]
    got = {t for t, _ in refresh_map.input_tables(cur)}
    bad = []
    if "la_population" not in got:
        bad.append("la_population is not an input")
    for t in ("la_population_editions", "la_population_editions_file_checks",
              "la_boundaries"):
        if t in got:
            bad.append(f"{t} is an input")
    return bad, sorted(got)


def gate_23_refresh_map(cur):
    name = ("refresh_map input discovery: la_population is an input, the "
            "editions tables and la_boundaries are not")
    names = {"la_population", "la_population_editions",
             "la_population_editions_file_checks", "la_boundaries",
             "staging_la_signals"}
    bad, got = discovery_problems(names)
    # read-only against the real catalogue as it stands
    import refresh_map
    real = [t for t, _ in refresh_map.input_tables(cur)]
    real_bad = []
    if "la_population" not in real:
        real_bad.append("la_population is not an input in the database")
    real_bad += [f"{t} is an input in the database" for t in real
                 if t.startswith("la_population_editions")
                 or t == "la_boundaries"]
    problems = bad + real_bad
    report(23, name, not problems, "; ".join(problems[:3]) if problems else
           f"against a stub catalogue the inputs are {got}; in the database "
           f"la_population is an input ({len(real)} W1 inputs) and no "
           "editions table or ledger and not la_boundaries is one")


# ---------------------------------------------------------------- gate 24

def revision_facts(files=None, raw_dir=None, old="2024", new="2025",
                   expect=(REVISION_COUNT, REVISION_ENGLAND)):
    """(ok, detail) of the July 2026 revision of mid-2024 read from the two
    workbooks on disk: the `new` workbook's MYE4 mid-<old> against the `old`
    workbook's MYE2: the authorities whose population changes and England's
    move, with the largest moves listed. expect: (authorities changed,
    England's move)."""
    held, problems = read_held(files, raw_dir)
    if problems:
        return False, problems[0]
    if old not in held or new not in held:
        return False, (f"both held workbooks (mid-{old} and mid-{new}) are "
                       "needed")
    (_, fo), (_, fn) = held[old], held[new]
    was = {c: m.la_cell(r["values"]["All ages"], c)
           for c, r in fo["mye2"]["las"].items()}
    now = {c: m.la_cell(r["values"][int(old)], c)
           for c, r in fn["mye4"]["las"].items()}
    if set(was) != set(now):
        return False, "the two workbooks list different authorities"
    moved = {c: now[c] - was[c] for c in was if now[c] != was[c]}
    eng = sum(now.values()) - sum(was.values())
    top = sorted(moved, key=lambda c: (-abs(moved[c]), c))[:3]
    detail = (f"mid-{new} workbook MYE4 mid-{old} against the mid-{old} "
              f"workbook MYE2: {len(moved)} of {len(was)} authorities "
              f"change, England {sum(was.values()):,} -> {sum(now.values()):,}"
              f" ({eng:+,}); largest "
              + ", ".join(f"{fn['mye4']['las'][c]['name']} {c} "
                          f"{moved[c]:+,}" for c in top))
    if (len(moved), eng) != tuple(expect):
        return False, (f"expected {expect[0]} authorities and England "
                       f"{expect[1]:+,}; {detail}")
    return True, detail


def real_revision(cur, spec=REAL, files=None, raw_dir=None, old="2024",
                  new="2025", expect=(REVISION_COUNT, REVISION_ENGLAND)):
    """Edition 2 of mid-2024 is the July 2026 workbook's MYE4 mid-2024:
    equal on every authority's population and la_name, superseding edition
    1, differing from edition 1 at the listed authorities and moving England
    by the listed amount."""
    _need(cur, spec)
    ok, detail = revision_facts(files, raw_dir, old, new, expect)
    if not ok:
        return False, detail
    held, _ = read_held(files, raw_dir)
    fn = held[new][1]
    cur.execute(f"SELECT DISTINCT edition, supersedes FROM public."
                f"{spec.editions_table} WHERE {spec.period_col} = %s "
                "ORDER BY 1", (int(old),))
    chain = cur.fetchall()
    if (2, 1) not in chain:
        return False, (f"{old} has editions {chain}; edition 2 (the revision "
                       "stored by load --acknowledge) is not loaded yet")
    e1 = {r["lad24cd"]: r for r in m.records(cur, spec, old, 1)}
    e2 = {r["lad24cd"]: r for r in m.records(cur, spec, old, 2)}
    codes, probs = m.resolve_codes(cur, set(fn["mye4"]["las"]), int(old))
    bad = list(probs[:1])
    for code, r in fn["mye4"]["las"].items():
        got = e2.get(codes.get(code))
        if got is None:
            bad.append(f"{code}: not in edition 2")
        elif got["population"] != m.la_cell(r["values"][int(old)], code) \
                or got["la_name"] != r["name"]:
            bad.append(f"{code}: edition 2 {got['population']:,} "
                       f"{got['la_name']!r}, workbook "
                       f"{r['values'][int(old)]:,} {r['name']!r}")
    diff = [k for k in e2 if k in e1
            and e1[k]["population"] != e2[k]["population"]]
    if len(diff) != expect[0]:
        bad.append(f"edition 2 differs from edition 1 at {len(diff)} "
                   f"authorities, expected {expect[0]}")
    moved = sum(r["population"] for r in e2.values()) - sum(
        r["population"] for r in e1.values())
    if moved != expect[1]:
        bad.append(f"edition 2 moves England by {moved:+,}, expected "
                   f"{expect[1]:+,}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"edition 2 of {old} equals the mid-{new} workbook's MYE4 mid-{old} "
        f"on {len(e2)} authorities (population and la_name) and differs "
        f"from edition 1 at {len(diff)} (England {moved:+,}); {detail}")


def gate_24_revision(cur):
    name = ("the July 2026 revision of mid-2024: 295 authorities, England "
            "-12,931, listed; stored as edition 2 and equal to the workbook")
    out = {}
    delta = (7, -5)

    def body(e, paths, files, loaded):
        # held: 2024 and 2025 as loaded; then a 2026 workbook restates 2024
        # at two authorities and is stored with --acknowledge 2024
        with patched_legacy(e, loaded, files):
            rc, text, _ = e.cmd(["migrate-legacy", *paths, "--commit"])
        if rc != 0:
            out["error"] = text[-200:]
            return
        newer = e.file(2026, values={
            ("E06000002", 2024): value("E06000002", 2024) + delta[0],
            ("E09000033", 2024): value("E09000033", 2024) + delta[1]})
        rc, text, _ = e.cmd(["load", "--commit", "--acknowledge", "2024"])
        out["load"] = (rc, text[-200:])
        out["editions"] = tl.editions(e.cur, "2024")
        d = Path(e.root) / "disk"
        d.mkdir()
        for p in (paths[0], newer):
            (d / p.name).write_bytes(p.read_bytes())
        sel = {"2024": {"sha256": m.content_sha256(paths[0])},
               "2026": {"sha256": m.content_sha256(newer)}}
        kw = dict(files=sel, raw_dir=d, old="2024", new="2026")
        out["ok"] = real_revision(e.cur, ZZ, expect=(2, sum(delta)), **kw)
        out["bad_count"] = real_revision(e.cur, ZZ, expect=(3, sum(delta)),
                                         **kw)
        out["bad_england"] = real_revision(e.cur, ZZ, expect=(2, 99), **kw)
        e.cur.execute("SAVEPOINT plant")
        e.cur.execute(f"ALTER TABLE public.{ZZ.editions_table} DISABLE "
                      "TRIGGER USER")
        e.cur.execute(f"UPDATE public.{ZZ.editions_table} SET population = "
                      "population + 1 WHERE reference_year = 2024 AND "
                      "edition = 2 AND lad24cd = 'E06000001'")
        out["planted"] = real_revision(e.cur, ZZ, expect=(2, sum(delta)),
                                       **kw)
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
        e.cur.execute("SAVEPOINT plant")
        e.cur.execute(f"ALTER TABLE public.{ZZ.editions_table} DISABLE "
                      "TRIGGER USER")
        e.cur.execute(f"DELETE FROM public.{ZZ.editions_table} WHERE "
                      "reference_year = 2024 AND edition = 2")
        out["no_edition_2"] = real_revision(e.cur, ZZ, expect=(2, sum(delta)),
                                            **kw)
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
    try:
        legacy_world(cur, body, years=(2024, 2025))
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(24, name, False, _first(ex))
    seeded_ok = (out.get("load", (1,))[0] == 0
                 and out.get("editions") == [(1, None, N), (2, 1, N)]
                 and out["ok"][0] and not out["bad_count"][0]
                 and not out["bad_england"][0] and not out["planted"][0]
                 and not out["no_edition_2"][0]
                 and "not loaded yet" in out["no_edition_2"][1])
    if not seeded_ok:
        return report(24, name, False, f"seeded: {out}")
    ok, detail = revision_facts()
    if not ok:
        return report(24, name, False, detail)
    sd = ("seeded: a workbook restating mid-2024 at two authorities is "
          "stored as edition 2 only with --acknowledge 2024 and equals "
          "the workbook; a wrong count or England move, a planted cell and "
          f"a missing edition 2 are each caught; the workbooks on disk: "
          f"{detail}")
    mixed(24, name, True, sd, cur, real_revision)


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_codes, gate_4_row_counts, gate_5_values, gate_6_identity,
         gate_7_markers, gate_8_older_file, gate_9_stop_conditions,
         gate_10_one_transaction, gate_11_ledger_skip, gate_12_preview,
         gate_13_held_file_recheck, gate_14_restated_year,
         gate_15_revision_and_refresh, gate_16_before_state_hash,
         gate_17_w1_read, gate_18_migration_proof, gate_19_rerun,
         gate_20_stranded, gate_21_restore, gate_22_no_network_no_secrets,
         gate_23_refresh_map, gate_24_revision)


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
    """zz% relations and functions in the real database, read on a separate
    read-only connection after the rollback."""
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


def print_revision():
    """Every authority whose mid-2024 population the July 2026 workbook
    restates (code, name, mid-2024 workbook, mid-2025 workbook, move), read
    from the workbooks on disk; for the decision note. Read-only."""
    held, problems = read_held()
    if problems:
        sys.exit(problems[0])
    fo, fn = held["2024"][1], held["2025"][1]
    rows = []
    for c, r in sorted(fn["mye4"]["las"].items()):
        was = m.la_cell(fo["mye2"]["las"][c]["values"]["All ages"], c)
        now = m.la_cell(r["values"][2024], c)
        if was != now:
            rows.append((c, r["name"], was, now, now - was))
    print(f"{len(rows)} authorities: mid-2024 population in "
          f"{held['2024'][0].name} (MYE2) against {held['2025'][0].name} "
          f"(MYE4); England {sum(r[4] for r in rows):+,}")
    for c, name, was, now, d in rows:
        print(f"{c} {name}: {was:,} -> {now:,} ({d:+,})")


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
    if argv == ["--print-revision"]:
        return print_revision()
    if argv:
        sys.exit("usage: s3_mye_editions_verify.py [--print-note-lines | "
                 "--print-revision]")
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
    mine = [t for t in left if t.startswith("zz_s3")]
    if mine:
        print(f"LEFTOVER: committed zz_s3 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s3 table or function left in the database after the "
              "rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
