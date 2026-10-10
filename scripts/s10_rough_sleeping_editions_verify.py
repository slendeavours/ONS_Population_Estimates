"""Gates for the S10 (MHCLG rough sleeping snapshot in England) editions
tables.

Mirrors scripts/s23_rsh_stock_editions_verify.py and
s6_asylum_editions_verify.py.
Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes nothing, not
even a run-log row. The real-table gates read la_rough_sleeping,
la_rough_sleeping_editions and la_rough_sleeping_editions_file_checks. Until
the editions table exists (ddl and migrate-legacy not yet run) they print
`FAIL (pending migration)` with the words "not yet migrated", so the script
exits non-zero until then, like the other verifiers; nothing is created to
make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s10_live / zz_s10_editions / zz_s10_editions_file_checks
(a copy of the loader's spec), created inside the transaction, and call the
loader's own writer and commands (s10_rough_sleeping_editions.main with the
SPEC pointed at the copy, the content API and the downloads stubbed;
apply_year; migrate_legacy; restore_edition) rather than a re-implementation.
The real tables are only ever read.

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), live checks                              (throwaway + real)
  2  every live period has edition 1; the latest edition equals live cell
     for cell on the key, the year and both values                  (real)
  3  codes: every lad24cd in la_boundaries, no E08000038/39 stored, the
     held file's codes resolve through geography.resolve to the stored
     codes, source 10 declared 'new', no private recode dict and no
     la_code_lookup query in the loader (AST)
  4  296 authorities in every live period (and in its tip edition)  (real)
  5  no NULL (or negative) in either value, NOT NULL in the editions table;
     the edition 1 national sums equal the file's England row, read from
     the file on disk                                       (seeded + real)
  6  identity from the file (seeded Cover, title, range, publication date,
     table title, shorthand, header, page and --release mismatches halt)
  7  a marker, blank, text, negative or fraction in an LA cell halts and is
     never read as 0; no value coerced to 0 in the loader (AST); every LA
     cell of the held file, every year, is a count
  8  older-file guard on the page, --release, --file and back-fill paths;
     the rank is the file's own, never its name
  9  stop conditions: each seeded REJECTED, stores nothing, no ledger row,
     partial run-log row
 10  a new period: edition, live rows and ledger row in one transaction
 11  ledger skip needs the (URL, sha256) pair for every period the file
     states
 12  preview and simulate write nothing
 13  a byte-identical re-read is `unchanged` on every path against edition 1
     "as loaded", published zeros stay 0
 14  a restated back year is compared, needs --acknowledge naming it, and an
     acknowledgement must name a compared year
 15  refresh-latest changes only the revised period and copies loaded_at
 16  edition 1 equals the decision note's before-state hash line   (real)
 17  the migration proof re-run read-only from the file on disk
 18  rerun idempotent
 19  stranded period repair
 20  restore-edition round trip
 21  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content

The decision note's hash lines are scan-safe: a 64-hex run would be flagged
by the credential scan, so the sha256 is written as two 32-hex halves in two
labelled fields:
    before-state la_rough_sleeping <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>

Usage:
    python scripts/s10_rough_sleeping_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The cursor handed to the gates
refuses commit and rollback on its connection, and the commands get a
stand-in connection whose commit only counts. No network, no real-table
writes, no backend is ever terminated. After the rollback a second,
read-only connection checks that no zz% table was left behind.
"""
import ast
import contextlib
import io
import os
import re
import socket
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
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
import s10_rough_sleeping_editions as m  # noqa: E402
import test_s10_rough_sleeping_loader as tl  # noqa: E402
from test_s10_rough_sleeping_pure import (BASE, CODES, TITLE,  # noqa: E402
                                          _Session, collection, value,
                                          write_2024_layout, write_file)

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
LOADER = HERE / "s10_rough_sleeping_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-10-s10-editions-first-load.md")
NOT_YET = "the S10 editions table does not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (21)
NEVER_CODES = ("E08000038", "E08000039")
PUB = date(2026, 2, 26)


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
        sys.exit("HARD STOP: a zz_s10 table already exists as a real table; "
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

class Env(tl.Fixture):
    """The loader's main(argv) on the throwaway tables, the content API and
    the downloads stubbed; every workbook is written here (write_file)."""

    URL = tl.Migrate.URL
    seed_legacy = tl.Migrate.seed_legacy
    legacy = tl.Migrate.legacy
    migrate = tl.Migrate.migrate

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api = {tl.COLL: collection()}
        self.urls, self.final, self.fetched = {}, {}, []
        self.n = 0
        self.tx = None

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, run-log
        mock); self.tx counts the commits."""
        borrowed = tl._Borrowed(cur)
        self.tx = borrowed.tx

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)
        out = io.StringIO()
        with tl.specs(), \
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

    def named(self, name, year=2025, *, serve=True, **kw):
        """A snapshot file whose NAME is chosen (not derived from the
        content)."""
        self.n += 1
        path = write_file(self.root / f"n{self.n}" / name, year, **kw)
        url = f"https://assets.example/media/n{self.n}/{name}"
        if serve:
            self.urls[url] = path
            self.page(year, url)
            self.last_url = url
        return path, url


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
    """A seeded legacy state (as the old build left it) and the fixture
    file; fn(env, path, files)."""
    def body(c):
        with env(c) as e, tl.specs():
            path, files = e.seed_legacy(c, **kw)
            return fn(e, path, files)
    return _in_savepoint(cur, body)


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


def held_file(files=None, raw_dir=None):
    """The file on disk whose sha256 is the held file's, or None."""
    files = m.LEGACY_FILE if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    for p in sorted(raw.glob("*.ods")):
        if m.content_sha256(p) == files["sha256"]:
            return p
    return None


def _read_held(files=None, raw_dir=None):
    """(path, file facts) of the held file, or (None, a problem text)."""
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return None, (f"no file in {raw_dir or m.RAW_DIR} with sha256 "
                      f"{files['sha256'][:16]}")
    try:
        return p, m.read_file(p)
    except ValueError as e:
        return None, f"{p.name}: {_first(e)}"


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
        "edition equals live on lad24cd, the year, rough_sleeping and "
        "rough_sleeping_prev_year, cell for cell")


def gate_2_edition1_and_latest(cur):
    name = ("every live period has edition 1; the latest edition equals "
            "live cell for cell on both values")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_edition1_and_latest(e.cur, ZZ)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET rough_sleeping = "
             "rough_sleeping + 1 WHERE lad24cd = 'E06000002'",
             "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET rough_sleeping_prev_year = "
             "rough_sleeping_prev_year + 1 WHERE lad24cd = 'E06000002'",
             "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET rough_sleeping = 0 WHERE "
             "lad24cd = 'E09000033'", "edition-only"),
            (f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
             "'E06000002'", "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET snapshot_year = 2030",
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
    mixed(2, name, ok, "seeded complete state passes; five planted drifts "
          "(either value, a zero, a missing row, a year without edition 1) "
          "are each caught" if ok else f"{out['base']} {out['miss']}", cur,
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


def real_codes(cur, spec=REAL, loader=LOADER, form="new", files=None,
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
                       "new codes resolve to E08000016/19)")
    if not _live_periods(cur, spec):
        bad.append(f"{spec.live_table}: {EMPTY}")
    path, tool = _read_held(files, raw_dir)
    resolved = recoded = 0
    if path is None:
        bad.append(tool)
    else:
        rmap, problems = m.resolve_codes(cur, set(tool["las"]), tool["year"])
        bad += [f"{path.name}: {x}" for x in problems[:2]]
        cur.execute(f"SELECT DISTINCT lad24cd FROM public."
                    f"{spec.editions_table} WHERE {spec.period_col} = %s AND "
                    "edition = 1", (tool["year"],))
        stored = {r[0] for r in cur.fetchall()}
        if set(rmap.values()) != stored:
            off = sorted(set(rmap.values()) ^ stored)
            bad.append(f"{path.name}: its codes resolve to codes that are "
                       f"not edition 1's lad24cd ({off[:4]})")
        resolved = len(rmap)
        recoded = sum(1 for c, r in rmap.items() if c != r)
    src = _source_problems(loader)
    if src:
        bad.append(f"the loader: {src[:2]}")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("?",))[0]
    if decl != form:
        bad.append(f"source 10 is declared {decl!r}, expected {form!r}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{seen} distinct lad24cd over live and editions all in "
        f"la_boundaries; no E08000038/39 in lad24cd; the held file's "
        f"{resolved} codes resolve through geography.resolve to edition 1's "
        f"lad24cd ({recoded} recoded); source 10 declared {form!r}; no "
        "private dict or la_code_lookup query in the loader")


def gate_3_codes(cur):
    name = ("codes: lad24cd in la_boundaries, no E08000038/39 stored, the "
            "file's codes resolve through geography.resolve, no private "
            "recode dict or la_code_lookup query")
    out = {}

    def body(e):
        path = e.file()
        e.ok(e.cur, ["load", "--commit"])
        files = {"sha256": m.content_sha256(path)}
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
        ok, detail = real_codes(e.cur, ZZ, form="old", files=files,
                                raw_dir=raw)
        if ok or "declared" not in detail:
            miss.append(f"a wrong declared form passed: {detail[:80]}")
        ok, detail = real_codes(e.cur, ZZ, files={"sha256": "0" * 64},
                                raw_dir=raw)
        if ok or "no file" not in detail:
            miss.append(f"a missing held file passed: {detail[:80]}")
        out["miss"] = miss
        # a file carrying the old Barnsley/Sheffield codes halts against the
        # declared form 'new' and stores nothing
        before = state(e.cur)
        e.file(2026, codes=["E06000001", "E06000002", "E08000016",
                            "E08000039", "E09000033"])
        halted, text = e.halts(["load", "--commit"], before, "E08000016")
        out["old_code"] = halted
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
          and out.get("old_code"))
    detail = ("Barnsley and Sheffield stay E08000016/19; two planted "
              "breaks, a wrong declared form and a missing file are each "
              "caught; a file carrying the old Barnsley code halts against "
              "the declared 'new' and stores nothing; the AST check catches "
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
        out["base"] = real_row_counts(e.cur, ZZ, n=len(CODES))
        out["wrong_n"] = real_row_counts(e.cur, ZZ, n=len(CODES) + 1)
        e.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
                      "'E08000019' AND snapshot_year = 2025")
        out["short"] = real_row_counts(e.cur, ZZ, n=len(CODES))
    scenario(cur, body)
    ok = out["base"][0] and not out["wrong_n"][0] and not out["short"][0]
    mixed(4, name, ok, ("the expected count passes in both years; one more "
                        "authority, or a missing authority in an earlier "
                        "year of live, fails" if ok else f"{out}"), cur,
          real_row_counts)


# ---------------------------------------------------------------- gate 5

def real_values(cur, spec=REAL, files=None, raw_dir=None):
    """In live and in each period's tip edition: no NULL and no negative in
    either value; the editions table holds both NOT NULL. The file on disk
    (found by sha256): edition 1 of its year sums to the file's England row
    for the year and the year before, and so does the tip when the tip comes
    from that file."""
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
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE rough_sleeping IS "
                "NULL OR rough_sleeping_prev_year IS NULL), COUNT(*) FILTER "
                "(WHERE rough_sleeping < 0 OR rough_sleeping_prev_year < 0) "
                f"FROM public.{table} WHERE {spec.period_col} = %s{extra}",
                (p,))
            n, nulls, neg = cur.fetchone()
            rows += n
            if nulls:
                bad.append(f"{table} {p}: {nulls} row(s) with a NULL value")
            if neg:
                bad.append(f"{table} {p}: {neg} row(s) with a negative value")
    path, tool = _read_held(files, raw_dir)
    sums = ""
    if path is None:
        bad.append(tool)
    else:
        y = tool["year"]
        problems = m.reconcile(tool, [y - 1, y])
        if problems:
            bad.append(f"{path.name}: reconciliation failed: {problems[0]}")
        else:
            checked = []
            cur.execute(f"SELECT COUNT(*) FROM public.{spec.editions_table} "
                        f"WHERE {spec.period_col} = %s AND edition = 1",
                        (y,))
            if not cur.fetchone()[0]:
                bad.append(f"{y}: no edition 1 to sum against {path.name}")
            tip = (_tip(cur, spec, y) if str(y) in periods else None)
            cur.execute(f"SELECT DISTINCT source_file FROM public."
                        f"{spec.editions_table} WHERE {spec.period_col} = %s "
                        "AND edition = %s", (y, tip))
            tsrc = cur.fetchall()
            eds = [1]
            if tip not in (None, 1) and len(tsrc) == 1 and \
                    m.release_rank(tsrc[0][0]) == tool["rank"]:
                eds.append(tip)
            for ed in eds:
                cur.execute(
                    "SELECT SUM(rough_sleeping), SUM(rough_sleeping_prev_year)"
                    f" FROM public.{spec.editions_table} WHERE "
                    f"{spec.period_col} = %s AND edition = %s", (y, ed))
                a, b = cur.fetchone()
                wa, wb = m.england_total(tool, y), m.england_total(tool,
                                                                   y - 1)
                if (a, b) != (wa, wb):
                    bad.append(f"{y} edition {ed} sums to {a:,} / {b:,}, the "
                               f"England row of {path.name} says {wa:,} / "
                               f"{wb:,}")
                checked.append(f"edition {ed} {a:,} / {b:,}")
            sums = (f"; {path.name}: " + ", ".join(checked) + f" = England "
                    f"{m.england_total(tool, y):,} ({y}) and "
                    f"{m.england_total(tool, y - 1):,} ({y - 1})")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{rows:,} rows over live and the tip editions: no NULL, no negative "
        f"value, both editions columns NOT NULL{sums}")


def gate_5_values(cur):
    name = ("no NULL or negative in either value (NOT NULL in the editions "
            "table); edition 1 national sums equal the file's England row")
    out = {}

    def body(e):
        path = e.file()
        e.ok(e.cur, ["load", "--commit"])
        files = {"sha256": m.content_sha256(path)}
        raw = path.parent
        out["base"] = real_values(e.cur, ZZ, files, raw)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET rough_sleeping = NULL "
             "WHERE lad24cd = 'E06000002'", "NULL value"),
            (f"UPDATE public.{ZZ.live_table} SET rough_sleeping_prev_year = "
             "NULL WHERE lad24cd = 'E06000002'", "NULL value"),
            (f"UPDATE public.{ZZ.live_table} SET rough_sleeping = -1 WHERE "
             "lad24cd = 'E06000002'", "negative"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_values(e.cur, ZZ, files, raw)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        # a valid file whose England row differs from edition 1 is caught
        other = write_file(e.root / "other" / path.name, 2025,
                           values={("E06000002", 2025): 99})
        ok, detail = real_values(e.cur, ZZ, {"sha256": m.content_sha256(
            other)}, other.parent)
        if ok or "England row" not in detail:
            miss.append(f"a file with other sums passed: {detail[:80]}")
        # a file that does not reconcile is caught
        bad = write_file(e.root / "bad" / path.name, 2025,
                         england={2025: 1})
        ok, detail = real_values(e.cur, ZZ, {"sha256": m.content_sha256(bad)},
                                 bad.parent)
        if ok or "reconciliation failed" not in detail:
            miss.append(f"a file that does not reconcile passed: "
                        f"{detail[:80]}")
        ok, detail = real_values(e.cur, ZZ, {"sha256": "0" * 64}, raw)
        if ok or "no file" not in detail:
            miss.append("a missing file passed")
        # a NULL cannot be stored in the editions table at all
        e.cur.execute("SAVEPOINT plant")
        try:
            e.cur.execute(f"INSERT INTO public.{ZZ.editions_table} (lad24cd,"
                          "snapshot_year, edition, rough_sleeping, "
                          "rough_sleeping_prev_year, release_label, "
                          "published_date, source_file, source_sha256) "
                          "VALUES ('E06000001', 2030, 1, NULL, 1, 'x', "
                          "now(), 'x', 'x')")
            miss.append("the editions table accepted a NULL value")
        except psycopg2.Error:
            pass
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(5, name, ok, "the seeded state passes (edition 1 sums to the "
          "file's England row for both years); a NULL in either value, a "
          "negative, a file of other sums, a file that does not reconcile "
          "and a missing file are each caught; the editions table refuses a "
          "NULL" if ok else f"{out['base']} {out['miss']}", cur,
          real_values)


# --------------------------------------------------------- file gates (6, 7)

def real_identity(cur=None, files=None, raw_dir=None, spec=REAL):
    """The held file's identity read from the file itself equals what the
    loader surveyed (rank, year), and, once the editions table exists, every
    period's tip names its file's year and publication date."""
    path, tool = _read_held(files, raw_dir)
    if path is None:
        return False, tool
    bad = []
    if tool["rank"] != m.LEGACY_RANK:
        bad.append(f"{path.name}: rank {tool['rank']}, surveyed "
                   f"{m.LEGACY_RANK}")
    if tool["table_title"].split()[-1] != str(tool["year"]) or \
            tool["header"][-1] != tool["year"]:
        bad.append("the Table_1_Total title or header disagree with the "
                   "Cover's year")
    tips = ""
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        info = m.tip_info(cur, spec)
        off = [pp for pp, i in info.items() if i["rank"] is None]
        if off:
            bad.append(f"{off[:3]}: the tip's source_file names no autumn "
                       "year and publication date")
        tips = f"; {len(info)} stored year(s), each tip names its file"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{path.name}: {tool['title']!r}, published {tool['published']}, "
        f"table title and header agree on {tool['year']}{tips}")


def gate_6_identity(cur):
    name = ("identity from the file: a seeded Cover, range, publication "
            "date, table title, shorthand, header, page or --release "
            "mismatch halts")
    problems = []
    years = list(range(2010, 2026))
    hdr = ["Local Authority Code", "Local Authority Name", "Region Code",
           "Region Name"] + years

    def body(e):
        tmpd = Path(e.root)

        def raises(label, **kw):
            try:
                m.read_file(write_file(tmpd / f"{label}.ods", 2025, **kw))
                problems.append(f"{label} was read")
            except ValueError:
                pass
        raises("title", cover={"title_year": 2024})
        raises("range_end", cover={"last": 2024})
        raises("range_start", cover={"first": 2009})
        raises("no_pub", cover={"pub_line": False})
        raises("date_cell", cover={"date_cell": datetime(2020, 1, 1)})
        raises("no_next", cover={"next_release": None})
        raises("no_cover", cover=False)
        raises("table_title", title_year=2024)
        raises("shorthand", shorthand=None)
        raises("header", header=["x"] + hdr[1:])
        raises("header_year", header=hdr[:-1] + [2026])
        try:
            m.read_file(write_2024_layout(tmpd / "l2024.ods"))
            problems.append("the autumn 2024 layout was read")
        except ValueError as ex:
            if "Local authority" not in str(ex):
                problems.append("the 2024 layout did not name its header")
        t = m.read_file(write_file(tmpd / "ok.ods", 2025))
        if not m.identity_problems(t, 2024):
            problems.append("a page of another year was accepted")
        if not m.identity_problems(t, None, 2024):
            problems.append("--release of another year was accepted")
        if m.identity_problems(t, 2025, 2025):
            problems.append("a matching page was refused")
        # through the command: a page title of another year, a file of
        # another year under --release and a Cover of another year halt and
        # store nothing
        e.file()
        before = state(e.cur)
        e.api[BASE.format(2025)]["title"] = TITLE.format(2024)
        ok, _ = e.halts(["load", "--commit"], before)
        if not ok:
            problems.append("a page of another year did not halt the load")
        path = e.file(register=False)
        ok, _ = e.halts(["load", "--file", path, "--release", "2024",
                         "--commit"], before, "--release 2024")
        if not ok:
            problems.append("--file with another --release did not halt")
        path = e.file(register=False, cover={"title_year": 2024})
        ok, _ = e.halts(["load", "--file", path, "--no-page", "--commit"],
                        before)
        if not ok:
            problems.append("a Cover of another year did not halt")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a Cover title year, range start and end, missing "
          "publication date, date cell, missing Next Release, missing Cover, "
          "table title year, missing shorthand line, header and header year, "
          "the autumn 2024 layout, a page year and --release that disagree "
          "with the file each halt, and nothing is stored" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(6, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(6, name, False, _first(ex))
    report(6, name, ok, f"{sd}; held file: {detail}")


def real_cells(files=None, raw_dir=None):
    """Every LA cell of every year of the held file is a count (no marker,
    blank or text anywhere in 2010 to the file's year), and the file
    reconciles for every year."""
    path, tool = _read_held(files, raw_dir)
    if path is None:
        return False, tool
    bad = m.reconcile(tool)
    if bad:
        return False, f"{path.name}: {len(bad)} problem(s), e.g. {bad[0]}"
    y = tool["year"]
    zeros = {yy: sum(1 for r in tool["las"].values()
                     if m.la_cell(r["values"][yy], "x") == 0)
             for yy in (y - 1, y)}
    return True, (f"{path.name}: {len(tool['las'])} authorities x "
                  f"{len(tool['years'])} years, every cell a count, "
                  f"reconciled in every year; published zeros {zeros[y]} in "
                  f"{y} and {zeros[y - 1]} in {y - 1}; markers defined "
                  f"{sorted(tool['markers'])} and none at LA level")


def gate_7_markers(cur):
    name = ("a marker, blank, text, negative or fraction in an LA cell halts "
            "and is never read as 0; no value coerced to 0")
    problems = []

    def body(e):
        cases = (("[x]", "Not Available"), ("[z]", "Not Applicable"),
                 ("[n]", "created through reorganisation"),
                 (None, "blank"), ("see note", "text"), (-1, "negative"),
                 (2.5, "non-integer"))
        for cell, needle in cases:
            for yr in (2025, 2024):
                e.file(values={("E06000002", yr): cell})
                before = state(e.cur)
                ok, text = e.halts(["load", "--commit"], before, needle)
                if not ok:
                    problems.append(f"{cell!r} in {yr}: not halted naming "
                                    f"{needle!r}: {text[-100:]}")
        for cell in ("[x]", "[z]", "[n]", None, "", "  ", "see note", -1, 2.5,
                     True, float("nan")):
            try:
                got = m.la_cell(cell, "x")
                problems.append(f"la_cell({cell!r}) returned {got}")
            except ValueError:
                pass
        if m.la_cell(0, "x") != 0 or m.la_cell(3.0, "x") != 3:
            problems.append("a published 0 or a whole float is not a count")
        # the marker meaning comes from the file's own shorthand line
        try:
            m.la_cell("[x]", "x", {"x": "Not Available"})
        except ValueError as ex:
            if "Not Available" not in str(ex):
                problems.append("the marker's meaning is not named")
        # the file read as published: a marker in a year the run does not
        # read does not halt, one in a year it reads does (reconcile years)
        t = m.read_file(e.file(register=False,
                               values={("E06000002", 2012): "[x]"}))
        if not m.reconcile(t):
            problems.append("reconcile over every year missed a marker")
        if m.reconcile(t, [2024, 2025]):
            problems.append("a marker in an unread year halted the load")
    scenario(cur, body)
    src = _source_problems(LOADER, zeros=True)
    if src:
        problems.append(f"the loader: {src[:2]}")
    seeded = not problems
    sd = ("seeded: [x], [z], [n] (named with their meanings from the "
          "file), a blank, text, a negative and a fraction in either column "
          "of the loaded year each halt and store nothing; la_cell never "
          "returns a default and keeps a published 0; no value is coerced "
          "to 0 in the loader" if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(7, name, False, sd)
    ok, detail = real_cells()
    report(7, name, ok, f"{sd}; held file: {detail}")


# ---------------------------------------------------------------- gates 8-12

def gate_8_older_file(cur):
    name = ("older-file guard on the page, --release, --file and back-fill "
            "paths; the rank is the file's own, never its name")
    problems = []
    # the content is published 2026-01-15; the NAME carries a later date
    fake = "Rough_sleeping_snapshot_in_England__autumn_2025_2026-03-30.ods"

    def older(e, how):
        e.seed(e.cur)
        path, url = e.named(fake, 2025, serve=how != "file",
                            published=date(2026, 1, 15),
                            values={("E06000002", 2025): 99})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2025"]
        if how == "file":
            argv += ["--file", path]
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
                    problems.append(f"back-fill of autumn 2024: rc {rc}: "
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
    report(8, name, not problems, "an autumn 2025 file published 2026-01-15 "
           "but named like a later one, offered by the page, --release and "
           "--file against a held file of 2026-02-26, is skipped and halts "
           "with nothing stored or logged; an autumn 2024 file offered "
           "after 2025 is not back-filled; --allow-older-file overrides and "
           "is logged; equal rank with other content stops" if not problems
           else "; ".join(problems[:3]))


def rejected_state(cur, period):
    """The editions and live tables, and the number of ledger rows of the
    period (a rejected year stores nothing and writes no ledger row; a
    restated earlier year may still get its 'unchanged' row)."""
    tables = tables_state(cur)
    cur.execute(f"SELECT COUNT(*) FROM public.{LEDGER} WHERE "
                "snapshot_year = %s", (period,))
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
                f"rejected ['{period}']" not in notes:
            return f"run-log notes: {notes[:120]}"
    elif logged.called:
        return "a preview wrote a run-log row"
    return None


def gate_9_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row")
    problems = []
    base = {c: value(c, 2026) for c in CODES}

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
        e.file(published=date(2026, 3, 5), **kw)

    def seed_new(e, **kw):
        e.seed(e.cur)
        e.file(2026, **kw)
    first = value("E06000002", 2025)
    case("revision: any change, listed, needs --acknowledge",
         lambda e: seed_rev(e, values={("E06000002", 2025): first + 1}),
         "--acknowledge 2025")
    case("revision: one authority moves a lot (national total +1%)",
         lambda e: seed_rev(e, values={("E09000033", 2025): 385}),
         "national rough_sleeping")
    case("revision: only the prev-year column changes",
         lambda e: seed_rev(e, values={("E06000002", 2024): value(
             "E06000002", 2024) + 1}),
         "change against the held edition")
    case("revision: preview of the same writes no run-log row",
         lambda e: seed_rev(e, values={("E06000002", 2025): first + 1}),
         "--acknowledge 2025", commit=False)
    case("new year: national total moves more than 30%",
         lambda e: seed_new(e, values={(c, 2026): base[c] + 40
                                       for c in CODES if c != "E09000033"}),
         "national rough_sleeping", period="2026")
    case("new year: one authority moves by more than 150",
         lambda e: seed_new(e, values={("E09000033", 2026): 900}),
         "E09000033", period="2026")
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
                                      "2025", "--acknowledge", "2026"])
            if not (rc == 1 and m.PARTIAL in text
                    and "2026: REJECTED" in text and state(c) == before):
                problems.append(f"--acknowledge released a partial file: rc "
                                f"{rc}")
            # both sides of a limit: +1 on one cell is within the numeric
            # limits (only the any-change stop, which --acknowledge releases)
            ok = [dict(r) for r in tip]
            ok[0]["rough_sleeping"] += 1
            out = m.period_problems(ok, tip, None, kind="revised")
            if len(out) != 1 or "change against the held edition" not in out[
                    0]:
                problems.append(f"a one-cell revision: {out}")
            if m.period_problems([dict(r) for r in tip], tip, None,
                                 kind="revised"):
                problems.append("an identical revision was stopped")
    _in_savepoint(cur, partial)

    def many(c):
        with tl.specs(), mock.patch.object(m, "EXPECTED_AREAS", 12):
            tip = [{m.KEY: f"E06{i:06d}", m.PERIOD: P, "rough_sleeping": 50,
                    "rough_sleeping_prev_year": 50} for i in range(12)]
            new = [dict(r) for r in tip]
            for r in new[:11]:
                r["rough_sleeping"] += 1
            out = m.period_problems(new, tip, None, kind="revised")
            if not any("limit 10" in x for x in out):
                problems.append(f"more than 10 authorities changing: {out}")
            new = [dict(r) for r in tip]
            for r in new[:10]:
                r["rough_sleeping"] += 1
            if any("limit 10" in x for x in m.period_problems(
                    new, tip, None, kind="revised")):
                problems.append("10 authorities changing hit the limit")
            prev = [dict(r, rough_sleeping=50) for r in tip]
            far = [dict(r, rough_sleeping=50) for r in tip]
            far[0]["rough_sleeping"] = 50 + m.NEW_AREA_ABS
            if m.period_problems(far, None, prev, kind="new"):
                problems.append("a move of exactly the area limit was "
                                "stopped")
            far[0]["rough_sleeping"] += 1
            if not any("more than" in x for x in m.period_problems(
                    far, None, prev, kind="new")):
                problems.append("a move past the area limit was not stopped")
    _in_savepoint(cur, many)

    def within(c):
        with env(c) as e, tl.specs():
            e.seed(c)
            e.file(2026)
            rc, text, _ = e.cmd(["load", "--commit"])
            if rc != 0 or tl.editions(c, "2026") != [(1, None, N)]:
                problems.append(f"a new year within the limits was refused: "
                                f"rc {rc}")
    _in_savepoint(cur, within)
    report(9, name, not problems, "a revision (any change; a national move "
           "of more than 1%), a new year with a national move of more than "
           "30% or an authority move of more than 150, and two files of one "
           "rank are each REJECTED (exit 1) with nothing stored, no ledger "
           "row and one partial run-log row (none in a preview); a partial "
           "file is never released by --acknowledge; the area-count and "
           "authority limits are exact; a new year within the limits is "
           "stored" if not problems else "; ".join(problems[:3]))


def gate_10_one_transaction(cur):
    name = ("a new period: edition, live rows and ledger row in one "
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
            problems.append(f"a new period did not store edition 1, {N} "
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
        rank = (date(2027, 2, 26), 2026)
        # the file's (URL, sha) recorded for 2025 only, not for 2026
        e.cur.execute(f"INSERT INTO public.{LEDGER} (snapshot_year, "
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
        e.file(published=date(2026, 3, 5), url=url0,
               values={("E06000002", 2025): 40})
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" in text or "2025: REJECTED" not in text:
            problems.append("the same URL with other bytes was skipped")
        # the held pair again: skipped; --recheck reads it anyway
        e.urls[url0] = path0
        e.page(2025, url0)
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
                problems.append(f"new period {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.file(published=date(2026, 3, 5),
               values={("E06000002", 2025): 40})
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

    def legacy(e, path, files):
        before = state(e.cur)
        with mock.patch.object(m, "LEGACY_LIVE", e.legacy(e.cur)), \
                mock.patch.object(m, "LEGACY_FILE", files):
            for argv in (["migrate-legacy", path],
                         ["migrate-legacy", path, "--simulate"]):
                rc, text, logged = e.cmd(argv)
                if rc != 0 or state(e.cur) != before or logged.called:
                    problems.append(f"{argv[0]} {argv[2:]}: rc {rc}")
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

def real_held_reread(cur, spec=REAL, files=None, raw_dir=None):
    """The held file read again equals edition 1 'as loaded': the content
    sha256 of the rebuilt rows (every authority, both values, Barnsley and
    Sheffield through the recode) is edition 1's, its published zeros are
    the stored zeros, and edition 1 names this file's rank. So a re-read by
    the page, --release or --file is unchanged against edition 1 (and
    against the tip while the tip is edition 1)."""
    _need(cur, spec)
    path, tool = _read_held(files, raw_dir)
    if path is None:
        return False, tool
    period = str(tool["year"])
    cur.execute(f"SELECT DISTINCT release_label, source_file FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (period,))
    rows = cur.fetchall()
    if len(rows) != 1:
        return False, f"{period}: edition 1 missing or not single"
    label, sf = rows[0]
    held = m.records(cur, spec, period, 1)
    with contextlib.redirect_stdout(io.StringIO()):
        built = m._build(cur, tool, [period])[period]
    a, b = m.rows_content_sha(held), m.rows_content_sha(built)
    bad = []
    if a != b:
        bad.append(f"{period}: edition 1 content sha {a[:12]} differs from "
                   f"the held file read now {b[:12]}")
    if label != core.AS_LOADED_LABEL:
        bad.append(f"{period}: edition 1 is not 'as loaded' ({label!r})")
    if m.release_rank(sf) != tool["rank"]:
        bad.append(f"{period}: edition 1 source_file ranks "
                   f"{m.release_rank(sf)}, the file {tool['rank']}")
    zeros_file = sum(1 for r in tool["las"].values()
                     if m.la_cell(r["values"][tool["year"]], "x") == 0)
    zeros_held = sum(1 for r in held if r["rough_sleeping"] == 0)
    if zeros_file != zeros_held:
        bad.append(f"{period}: {zeros_file} published zeros in the file, "
                   f"{zeros_held} stored")
    tip = _tip(cur, spec, period)
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{path.name} read now has edition 1's content sha256 ({a[:12]}, "
        f"{len(held)} rows), {zeros_held} published zeros kept as 0, edition "
        f"1 names the file's rank {tool['rank'][0]}/{tool['rank'][1]}; the "
        f"tip is edition {tip}, so a re-read is "
        + ("unchanged against the tip" if tip == 1
           else "compared with edition 1 and the tip"))


def reread_after_migration(e, path):
    """Problems (a list) when the migrated state does not treat the same
    bytes as unchanged on every path."""
    bad = []
    e.urls[e.URL] = path
    e.page(2025, e.URL)
    rc, text, logged = e.cmd(["load"])
    if rc != 0 or "nothing parsed" not in text or logged.called:
        bad.append(f"the page load after migrate-legacy: rc {rc}: "
                   f"{text[-100:]}")
    for argv in (["load", "--recheck", P],
                 ["load", "--release", "2025", "--recheck", P],
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
    if tl.live_val(e.cur, "E06000001") != 0 or tl.live_val(
            e.cur, "E06000001", "rough_sleeping_prev_year") != 0:
        bad.append("a published zero is not 0")
    rc, text, _ = e.cmd(["refresh-latest"])
    if "would write: none" not in text:
        bad.append("refresh-latest plans a write after the re-reads")
    return bad


def gate_13_held_file_recheck(cur):
    name = ("a byte-identical re-read is unchanged on every path against "
            "edition 1 'as loaded'; published zeros stay 0")
    problems = []
    out = {}

    def run(label, **kw):
        def body(e, path, files):
            e.migrate(e.cur, path, files)
            d = Path(e.root) / "disk"
            d.mkdir()
            (d / path.name).write_bytes(path.read_bytes())
            out[label] = real_held_reread(e.cur, ZZ, files, d)
            problems.extend(f"{label}: {x}"
                            for x in reread_after_migration(e, path))
        legacy_world(cur, body, **kw)
    run("held")
    run("zeros", values={("E06000002", 2025): 0, ("E06000002", 2024): 0})
    for label, (ok, detail) in out.items():
        if not ok:
            problems.append(f"{label}: {detail}")

    def changed(e, path, files):
        e.migrate(e.cur, path, files)
        other = write_file(Path(e.root) / "disk2" / path.name, 2025,
                           values={("E06000002", 2025): 40})
        ok, detail = real_held_reread(
            e.cur, ZZ, {"sha256": m.content_sha256(other)}, other.parent)
        if ok or "differs" not in detail:
            problems.append("a file with other content was called equal")
        ok, detail = real_held_reread(e.cur, ZZ, {"sha256": "0" * 64},
                                      other.parent)
        if ok or "no file" not in detail:
            problems.append("a missing file was called equal")
    legacy_world(cur, changed)
    seeded = not problems
    sd = ("seeded: after migrate-legacy the same bytes read by the page, "
          "--release, --file, --file --no-page, --recheck and with --commit "
          "are unchanged, store no edition, leave every ledger row "
          "'unchanged' and published zeros at 0; a file with other content "
          "or none is not equal" if seeded else "; ".join(problems[:3]))
    mixed(13, name, seeded, sd, cur, real_held_reread)


# ---------------------------------------------------------------- gate 14

def gate_14_restated_year(cur):
    name = ("a restated back year is compared and needs --acknowledge "
            "naming it; an acknowledgement must name a compared year")
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
        rc, text, logged = e.cmd(["load", "--commit", "--acknowledge", "2025"])
        eds = tl.editions(e.cur)
        if rc != 0 or "2025: revised" not in text or "ACKNOWLEDGED" not in                 text or eds != [(1, None, N), (2, 1, N)] or                 tl.editions(e.cur, "2026") != [(1, None, N)] or                 tl.live_val(e.cur, "E06000002") != first or                 "ACKNOWLEDGED 2025" not in logged.call_args[0][2]:
            problems.append(f"--acknowledge 2025: rc {rc}, {eds}: "
                            f"{text[-160:]}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE snapshot_year = 2025 AND "
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
    scenario(cur, body)
    report(14, name, not problems, "an identical restated year is unchanged; "
           "a changed one is REJECTED naming --acknowledge 2025 with "
           "nothing stored; an acknowledgement of a year the file does not "
           "state halts; --acknowledge 2025 stores edition 2 (superseding "
           "1, labelled ACKNOWLEDGED, live waiting for refresh-latest) and "
           "the same file read again is unchanged" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 15

def gate_15_revision_and_refresh(cur):
    name = ("a revision, then refresh-latest: only that period and row "
            "change, loaded_at is copied")
    problems = []

    def rows_hash(c, period):
        c.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                  f"FROM public.{ZZ.live_table} t WHERE snapshot_year = %s",
                  (period,))
        return c.fetchone()[0]

    def by_key(c, period):
        c.execute(f"SELECT lad24cd, rough_sleeping, rough_sleeping_prev_year "
                  f"FROM public.{ZZ.live_table} WHERE snapshot_year = %s",
                  (period,))
        return {r[0]: r for r in c.fetchall()}

    def body(e):
        e.seed(e.cur, year=2024)
        e.seed(e.cur)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        prior_before = rows_hash(e.cur, 2024)
        before = by_key(e.cur, 2025)
        first = tl.live_val(e.cur, "E06000002")
        e.file(published=date(2026, 3, 5),
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
                      "WHERE snapshot_year = 2025 AND edition = 2 LIMIT 1")
        ed_loaded = e.cur.fetchone()[0]
        e.cur.execute(f"SELECT lad24cd FROM public.{ZZ.live_table} WHERE "
                      "snapshot_year = 2025 AND loaded_at = %s", (ed_loaded,))
        if e.cur.fetchall() != [("E06000002",)]:
            problems.append("loaded_at was not copied to exactly the "
                            "refreshed row")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "snapshot_year = 2025 AND loaded_at <> '2026-01-01' "
                      "AND lad24cd <> 'E06000002'")
        if e.cur.fetchone()[0]:
            problems.append("an unchanged row's loaded_at moved")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "snapshot_year = 2024 AND loaded_at <> '2026-01-01'")
        if e.cur.fetchone()[0]:
            problems.append("the other year's loaded_at moved")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("a second refresh-latest plans a write")
        # drift: a live row that equals no edition halts without
        # --accept-drift and the guard names the year
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET rough_sleeping = "
                      "rough_sleeping + 1 WHERE lad24cd = 'E09000033' AND "
                      "snapshot_year = 2024")
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


def note_hashes(note):
    """{year: (rows, sha256)} from lines `before-state la_rough_sleeping
    <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the
    decision note (the two halves joined), or {}. (The `before-state-all`
    lines are a different record and do not match.)"""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                r"before-state la_rough_sleeping ([0-9]{4}) rows=([0-9]+) "
                r"sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})", text)}


def real_edition1_hash(cur, spec=REAL, note=NOTE, legacy_rows=None):
    """Edition 1 'as loaded' of every migrated year equals the before-state
    line recorded in the decision note: the row count and the
    rows_content_sha (lad24cd + both values), and the row count is the
    surveyed live count."""
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
                           f"la_rough_sleeping {p} rows=.. sha256-first32=.. "
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
        "edition 1 of every migrated year hashes to the before-state line "
        "in the decision note (" + "; ".join(parts) + ")")


def gate_16_before_state_hash(cur):
    name = ("edition 1 equals the before-state hash recorded in the "
            "decision note")
    out = {}

    def body(e, path, files):
        pre = m.live_state(e.cur, ZZ)
        legacy = (pre[0], pre[1])
        with mock.patch.object(m, "LEGACY_LIVE", legacy), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = e.cmd(["migrate-legacy", path, "--commit"])
        if rc != 0:
            out["error"] = text[-200:]
            return
        recs = m.records(e.cur, ZZ, P, 1)
        h = m.rows_content_sha(recs)
        out["same"] = h == m.rows_content_sha(m.records(e.cur, ZZ, P))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = (f"before-state la_rough_sleeping {P} rows={N} "
                    f"{hash_fields(h)}")
            note.write_text(f"intro\n{line}\nbefore-state-all "
                            f"la_rough_sleeping {P} rows={N} "
                            f"{hash_fields('1' * 64)}\n", encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(note.read_text().replace(h[:32], "0" * 32),
                            encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(f"before-state-all la_rough_sleeping {P} "
                            f"rows={N} {hash_fields(h)}\n", encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}"),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, N)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                N)
            note.write_text(line, encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(e.cur, ZZ, note, N + 5)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET rough_sleeping = "
                      "rough_sleeping + 1 WHERE lad24cd = 'E06000002'")
        out["changed"] = m.live_state(e.cur, ZZ)[:2] != legacy
        with mock.patch.object(m, "LEGACY_LIVE", legacy), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = e.cmd(["migrate-legacy", path, "--commit"])
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
          and not out["wrong_survey"][0] and out.get("changed")
          and out.get("twice"))
    mixed(16, name, ok,
          "a seeded migration stores an edition 1 whose hash equals the "
          "live rows' and the note's line; a wrong hash, a wrong row "
          "count, a missing line (a before-state-all line does not count) "
          "and a missing note fail; a later change shows; a second "
          "migration is refused" if ok else f"seeded: {out}", cur,
          real_edition1_hash)


# ---------------------------------------------------------------- gate 17

def real_migration_proof(cur, spec=REAL, files=None, raw_dir=None):
    """The migration proof re-run read-only from the file on disk (found by
    sha256): the parser on the held file reproduces every value of edition 1
    'as loaded' (lad24cd, rough_sleeping, rough_sleeping_prev_year; 0
    differences) and the file reconciles for the years it states."""
    _need(cur, spec)
    path, tool = _read_held(files, raw_dir)
    if path is None:
        return False, tool
    period = str(tool["year"])
    bad = m.reconcile(tool, m._years_read([period]))
    if bad:
        return False, f"{path.name}: reconciliation failed: {bad[0]}"
    held = m.records(cur, spec, period, 1)
    if not held:
        return False, f"{period}: no edition 1"
    with contextlib.redirect_stdout(io.StringIO()):
        built = m._build(cur, tool, [period])[period]
    cells, diffs = m._proof(held, built)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (period,))
    if cur.fetchall() != [(core.AS_LOADED_LABEL,)]:
        return False, f"{period}: edition 1 is not the 'as loaded' one"
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{cells:,} values of edition 1 ({len(held)} rows) "
                       f"equal {path.name} read now (0 differences)")


def gate_17_migration_proof(cur):
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
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw)
        if not ok or "0 differences" not in detail:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(e.cur, ZZ, {**files,
                                                      "sha256": "0" * 64},
                                          raw)
        if ok or "no file" not in detail:
            problems.append("a file with another sha256 was accepted")
        # a file changed on disk is not found by sha256
        (raw / path.name).write_bytes(path.read_bytes() + b"\0")
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw)
        if ok or "no file" not in detail:
            problems.append("a changed file on disk was accepted")
        # a different, valid file stands in for the held one: edition 1 is
        # not that file
        other = write_file(Path(e.root) / "raw2" / path.name, 2025,
                           values={("E06000002", 2025): 40})
        ok, detail = real_migration_proof(
            e.cur, ZZ, {**files, "sha256": m.content_sha256(other)},
            other.parent)
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
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        f"UPDATE public.{ZZ.live_table} SET rough_sleeping = rough_sleeping "
        "+ 1 WHERE lad24cd = 'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZZ.live_table} SET rough_sleeping_prev_year = "
        "rough_sleeping_prev_year + 1 WHERE lad24cd = 'E09000033'",
        "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZZ.live_table} SET lad24cd = 'E06000003' WHERE "
        "lad24cd = 'E06000001'", "proof failed"))
    legacy_world(cur, planted(
        f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = 'E06000002'",
        "not as surveyed", surveyed_first=True))
    mixed(17, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the file by sha256; a changed or missing file and a file that "
          "is not edition 1 are caught; planted differences in either "
          "value and in a code, and a dropped row, stop migrate-legacy with "
          "nothing stored" if not problems else "; ".join(problems[:3]),
          cur, real_migration_proof)


# ---------------------------------------------------------- gates 18 - 20

def gate_18_rerun(cur):
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
    report(18, name, not problems, "a second load by the page, --release, "
           "--file and --file --no-page and a refresh-latest stored no "
           "edition and changed no live row (re-reads add 'unchanged' "
           "ledger rows only); a ledger-skipped load logs nothing"
           if not problems else "; ".join(problems[:3]))


def gate_19_stranded(cur):
    name = "stranded period repair (edition held, live rows missing)"
    problems = []

    def body(e):
        e.seed(e.cur)
        e.cur.execute(f"DELETE FROM public.{ZZ.live_table}")
        if pe.status(e.cur, m.profile())["live_missing"] != [P]:
            problems.append("status does not name the stranded period")
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
    report(19, name, not problems, f"a stranded {P} was rebuilt from its "
           "edition 1 with no new edition and a live-missing ledger row"
           if not problems else "; ".join(problems[:3]))


def gate_20_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.live_val(e.cur, "E06000002")
        e.file(published=date(2026, 3, 5),
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
    report(20, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') and applied by refresh-latest; the "
           "tip and a missing edition are refused; a preview writes nothing"
           if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 21

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


def gate_21_no_network_no_secrets(cur):
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
                body_a = write_file(e.root / "dl" / "a.ods").read_bytes()
                body_b = write_file(e.root / "dl" / "b.ods", published=date(
                    2026, 3, 5)).read_bytes()
                url = "https://assets.example/media/3/f.ods"
                with contextlib.redirect_stdout(io.StringIO()), \
                        mock.patch.object(m, "MIN_FILE_BYTES", 10):
                    p1, _ = m.fetch(url, raw, _Session(body_a, url))
                    sha1 = m.content_sha256(p1)
                    p2, _ = m.fetch(url, raw, _Session(body_b, url))
                    p3, _ = m.fetch(url, raw, _Session(body_a, url))
                kept = (p1 == raw / "f.ods" and m.content_sha256(p1) == sha1
                        and p2 != p1 and p2.name.startswith("f-")
                        and p3 == p1)
            return rc, rc_s, rc_r, rc_l, kept
    try:
        rc, rc_s, rc_r, rc_l, kept = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(21, name, False, f"{type(ex).__name__}: {ex}")
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
    report(21, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned "
           f"{rc}/{rc_s}/{rc_r}/{rc_l}; a same-named different download "
           f"was saved beside it, the first untouched={kept}; run-log rows "
           f"{runs}->{runs_after}; real editions table and ledger "
           f"unchanged={real_before == real_after}; secret-like literal in "
           f"source {literal or 'none'}; {len(secrets)} secret-named "
           f"settings checked against {len(files)} files and "
           f"{len(OUTPUT)} output lines: in source {in_src or 'none'}, in "
           f"output {in_out or 'none'}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_codes, gate_4_row_counts, gate_5_values, gate_6_identity,
         gate_7_markers, gate_8_older_file, gate_9_stop_conditions,
         gate_10_one_transaction, gate_11_ledger_skip, gate_12_preview,
         gate_13_held_file_recheck, gate_14_restated_year,
         gate_15_revision_and_refresh, gate_16_before_state_hash,
         gate_17_migration_proof, gate_18_rerun, gate_19_stranded,
         gate_20_restore, gate_21_no_network_no_secrets)


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


def main():
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
    mine = [t for t in left if t.startswith("zz_s10")]
    if mine:
        print(f"LEFTOVER: committed zz_s10 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s10 table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
