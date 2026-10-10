"""Gates for the S23 (RSH registered provider social housing stock by local
authority) editions tables.

Mirrors scripts/s22_ctb_editions_verify.py and
s4_care_leaver_editions_verify.py.
Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes nothing, not
even a run-log row. The real-table gates read rsh_rp_stock_by_la,
rsh_rp_stock_by_la_editions and rsh_rp_stock_by_la_editions_file_checks. Until
the editions table exists (ddl and migrate-legacy not yet run) they print
`FAIL (pending migration)` with the words "not yet migrated", so the script
exits non-zero until then, like the other verifiers; nothing is created to
make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s23_live / zz_s23_editions / zz_s23_editions_file_checks
(a copy of the loader's spec), created inside the transaction, and call the
loader's own writer and commands (s23_rsh_stock_editions.main with the SPEC
pointed at the copy, the content API and the downloads stubbed;
apply_stock_period; migrate_legacy; restore_edition) rather than a
re-implementation. The real tables are only ever read.

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), live checks                              (throwaway + real)
  2  every live period has edition 1; the latest edition equals live on the
     key, the stock date, the 11 compared columns and null_reasons, cell for
     cell                                                             (real)
  3  live provenance uniform per period and equal to the tip's file_*
     values (live column against its file_* column, as refresh-latest's
     own after-check maps them)                                      (real)
  4  codes: lad24cd in la_boundaries, no E08000038/39 in lad24cd,
     publisher_la_code resolves to lad24cd through geography.resolve, no
     private recode dict and no la_code_lookup query in the loader (AST)
  5  296 authorities in the latest period                            (real)
  6  no NULL in the four always-counted stock columns; an LCHO NULL only on
     a Small PRP row with the not-counted reason, and no Small PRP LCHO zero
     (LCHO zeros only for the covered types, LARPs and Large PRPs); total =
     components (a NULL LCHO is not a part) on every row; the zero-total
     rows all LARPs; edition 1 'as loaded' still holds the published values
     (no NULL, no reason); a blank stock cell in a file halts; no source
     value coerced to 0 in the loader (AST)                  (seeded + real)
  7  in-file reconciliation re-run read-only from the file on disk
  8  identity from the file (seeded title, source line, Version History,
     header and page-year mismatches halt)
  9  older-file guard on the page, --release and --file paths; the rank is
     the file's own, never its name
 10  stop conditions: each seeded REJECTED, stores nothing, no ledger row,
     partial run-log row
 11  a new period: edition, live rows and ledger row in one transaction
 12  ledger skip needs the (URL, sha256) pair for the period
 13  preview and simulate write nothing
 14  a byte-identical re-read carries the not-counted rule: read as
     published it equals edition 1 "as loaded"; with the rule it equals the
     tip, which differs from edition 1 in exactly the cells the named
     acknowledgement records (LCHO 0 -> NULL with the reason); seeded: on
     every path the 0/NULL stop rejects it without the acknowledgement or
     with another count, the acknowledgement stores the next edition,
     refresh-latest brings live to it, and every path is then unchanged
 15  a revision, then refresh-latest: only that period changes, provenance on
     every row, loaded_at copied; key changes only with --accept-key-changes
 16  edition 1 equals the before-state hash lines in
     docs/decisions/2026-10-09-s23-editions-first-load.md             (real)
 17  the migration proof re-run read-only from the file on disk
 18  rerun idempotent
 19  stranded period repair
 20  restore-edition round trip
 21  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content

Usage:
    python scripts/s23_rsh_stock_editions_verify.py

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
import s23_rsh_stock_editions as m  # noqa: E402
import test_s23_rsh_stock_loader as tl  # noqa: E402
from test_s23_rsh_stock_pure import (BASE, CODES, HEADERS,  # noqa: E402
                                     HEADERS_2024, _Session, write_tool)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real spec and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPEC, which the seeded gates point
# at the copy)
REAL = m.SPEC
REAL_PROFILE = m.PROFILE
AREAS = m.EXPECTED_AREAS
REGIONS = m.EXPECTED_REGIONS
ZZ = tl.ZZ
LEDGER = tl.LEDGER
P = tl.P
N = tl.N
LOADER = HERE / "s23_rsh_stock_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-09-s23-editions-first-load.md")
NOT_YET = ("the S23 editions table does not exist yet (not yet migrated)")
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (21)
NEVER_CODES = ("E08000038", "E08000039")
PRIOR = "2024-03-31"


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
def expected(areas=AREAS, regions=REGIONS):
    with mock.patch.object(m, "EXPECTED_AREAS", areas), \
            mock.patch.object(m, "EXPECTED_REGIONS", regions):
        yield


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    names = (ZZ.live_table, ZZ.editions_table, LEDGER)
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                      for n in names))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s23 table already exists as a real table; "
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
    the downloads stubbed; every workbook is written here (write_tool)."""

    URL = tl.Migrate.URL
    PAGE = tl.Migrate.PAGE
    seed_legacy = tl.Migrate.seed_legacy
    legacy = tl.Migrate.legacy
    migrate = tl.Migrate.migrate
    check_reread_after_migration = tl.Migrate.check_reread_after_migration

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api = {tl.COLL: {"links": {"documents": []}}}
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

    def revise(self, **kw):
        return self.tool(version="1.2", month="December 2025", **kw)

    def named(self, name, *, serve=True, **kw):
        """A tool file whose NAME is chosen (not derived from the content)."""
        self.n += 1
        path = write_tool(self.root / f"n{self.n}" / name, **kw)
        url = f"https://assets.example/media/n{self.n}/{name}"
        if serve:
            self.urls[url] = path
            self.page(kw.get("year", 2025) - 1, kw.get("year", 2025), url)
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
    for c in m.ROW_COLS + m.LIVE_PROVENANCE + ("loaded_at",):
        if c not in lcols:
            bad.append(f"{spec.live_table}: {c} missing")
    for c in m.STOCK_COLUMNS:
        want = "YES" if c == m.NOT_COUNTED_COLUMN else "NO"
        if lcols.get(c, (None, None))[1] != want:
            bad.append(f"{spec.live_table}: {c} nullable is "
                       f"{lcols.get(c, (None, None))[1]}, expected {want} "
                       "(rule 1: only the not-counted LCHO may be NULL)")
    lchk = _constraint_names(cur, spec.live_table, "c")
    for suffix in ("components_sum_chk", "lcho_null_reason_chk"):
        if f"{m.LIVE_PREFIX}_{suffix}" not in lchk:
            bad.append(f"{spec.live_table}: CHECK {m.LIVE_PREFIX}_{suffix} "
                       "missing")
    lc = _columns(cur, led)
    for c, ty, nullable in (("id", "bigint", "NO"), (spec.period_col, "date",
                                                     "NO"),
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
    cols = ", ".join(tuple(spec.key_cols) + (spec.period_col,) + m.ROW_COLS)
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
        "edition equals live on the key, the stock date, the 11 compared "
        "columns and null_reasons, cell for cell")


def real_provenance(cur, spec=REAL):
    """Live provenance uniform per period and equal to the tip's file_*
    values (the mapping m.PROVENANCE_PAIRS, as refresh-latest's own
    live_equals_tip uses)."""
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, f"{spec.live_table}: {EMPTY}"
    bad = []
    lv = ", ".join(m.LIVE_PROVENANCE)
    ed = ", ".join(m.PROVENANCE)
    for p in periods:
        tip = _tip(cur, spec, p)
        cur.execute(f"SELECT DISTINCT {lv} FROM public.{spec.live_table} "
                    f"WHERE {spec.period_col} = %s", (p,))
        got = cur.fetchall()
        cur.execute(f"SELECT DISTINCT {ed} FROM public.{spec.editions_table} "
                    f"WHERE {spec.period_col} = %s AND edition = %s",
                    (p, tip))
        want = cur.fetchall()
        if len(got) != 1:
            bad.append(f"{p}: live provenance is not uniform ({len(got)} "
                       "distinct values)")
        elif len(want) != 1:
            bad.append(f"{p} ed{tip}: {len(want)} distinct file_* values")
        elif got[0] != want[0]:
            off = [c for c, a, b in zip(m.LIVE_PROVENANCE, got[0], want[0])
                   if a != b]
            bad.append(f"{p}: live provenance differs from the tip's "
                       f"file_* ({off})")
        elif m.release_rank(_tip_source(cur, spec, p, tip)) is None:
            bad.append(f"{p} ed{tip}: source_file names no file date")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} period(s): every live row carries the tip's five "
        "provenance values, and every tip names its version and month")


def _tip_source(cur, spec, p, tip):
    cur.execute(f"SELECT DISTINCT source_file FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = %s", (p, tip))
    rows = cur.fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _source_problems(path):
    """Problems in a module's source (an AST check): a string constant, other
    than a docstring, holding a Barnsley/Sheffield code (a private recode
    dict); a string constant that queries la_code_lookup; a source value
    coerced to 0 (`x or 0`, COALESCE(x, 0), `else 0`) on a line without
    `# not a source value`."""
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
            if re.search(r"(?i)coalesce\([^)]*,\s*0\s*\)", node.value):
                bad.append(f"line {node.lineno}: COALESCE(x, 0)")
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


def real_codes(cur, spec=REAL, loader=LOADER, form="old"):
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
    resolved = 0
    for p in _live_periods(cur, spec):
        cur.execute(f"SELECT DISTINCT publisher_la_code, lad24cd FROM "
                    f"public.{spec.live_table} WHERE {spec.period_col} = %s",
                    (p,))
        pairs = cur.fetchall()
        rmap, problems = m.resolve_codes(cur, {a for a, _ in pairs}, p)
        bad += [f"{p}: {x}" for x in problems[:2]]
        off = [(a, b, rmap.get(a)) for a, b in pairs if rmap.get(a) != b]
        if off:
            bad.append(f"{p}: publisher_la_code does not resolve to "
                       f"lad24cd for {off[:3]}")
        resolved += len(pairs)
    src = _source_problems(loader)
    if src:
        bad.append(f"the loader: {src[:2]}")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("?",))[0]
    if decl != form:
        bad.append(f"source 23 is declared {decl!r}, expected {form!r}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{seen} distinct lad24cd over live and editions all in "
        f"la_boundaries; no E08000038/39 in lad24cd; {resolved} publisher "
        "code(s) resolve to their lad24cd through geography.resolve; source "
        f"23 declared {form!r}; no private dict, la_code_lookup query or "
        "value coerced to 0 in the loader")


def real_row_counts(cur, spec=REAL, n=AREAS):
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, EMPTY
    p = periods[-1]
    tip = _tip(cur, spec, p)
    bad = []
    for table, extra in ((spec.live_table, ""),
                         (spec.editions_table, f" AND edition = {tip}")):
        cur.execute(f"SELECT COUNT(DISTINCT lad24cd) FROM public.{table} "
                    f"WHERE {spec.period_col} = %s{extra}", (p,))
        got = cur.fetchone()[0]
        if got != n:
            bad.append(f"{table} {p}: {got} authorities, expected {n}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"latest period {p}: {n} authorities in live and in edition {tip}")


def real_stock_values(cur, spec=REAL):
    """In live and in each period's tip edition: no NULL in the four
    always-counted stock columns; an LCHO NULL only with the not-counted
    reason and only on a PRP that is not a Large (Long Form) one, i.e. a
    Small PRP; no Short Form (Small PRP) row with an LCHO 0 (LCHO zeros
    only for the covered types); total = components (a NULL LCHO is not a
    part, the published total then equals the other three) on every row;
    every zero-total row a LARP (a published zero). In each edition 1 'as
    loaded': no NULL stock value and no reason (the published values as
    loaded are kept)."""
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, EMPTY
    bad, zeros, rows, nulls_seen = [], 0, 0, 0
    lc = m.NOT_COUNTED_COLUMN
    three = " + ".join(c for c in m.PARTS if c != lc)
    counted = " OR ".join(f"{c} IS NULL" for c in m.STOCK_COLUMNS if c != lc)
    for p in periods:
        tip = _tip(cur, spec, p)
        for table, extra in ((spec.live_table, ""),
                             (spec.editions_table, f" AND edition = {tip}")):
            w = f"{spec.period_col} = %s{extra}"
            cur.execute(
                f"SELECT COUNT(*), COUNT(*) FILTER (WHERE {counted}), "
                f"COUNT(*) FILTER (WHERE {lc} IS NULL), "
                f"COUNT(*) FILTER (WHERE {lc} IS NULL AND (null_reasons IS "
                f"DISTINCT FROM %s OR provider_type <> 'PRP' OR rp_size_band "
                f"IS NOT DISTINCT FROM 'Long Form')), "
                f"COUNT(*) FILTER (WHERE {lc} IS NOT NULL AND null_reasons "
                f"IS NOT NULL), "
                f"COUNT(*) FILTER (WHERE {lc} = 0 AND rp_size_band = "
                f"'Short Form'), "
                f"COUNT(*) FILTER (WHERE NOT ((({lc} IS NULL) AND "
                f"total_social_stock = {three}) OR (({lc} IS NOT NULL) AND "
                f"total_social_stock = {three} + {lc}))), "
                f"COUNT(*) FILTER (WHERE total_social_stock = 0), "
                f"COUNT(*) FILTER (WHERE total_social_stock = 0 AND "
                f"provider_type <> 'LARP') FROM public.{table} WHERE {w}",
                (m.NOT_COUNTED_REASON, p))
            n, nn, ln, lbad, rstray, smallz, off, z, znl = cur.fetchone()
            rows += n
            zeros += z
            nulls_seen += ln
            if nn:
                bad.append(f"{table} {p}: {nn} row(s) with a NULL in an "
                           "always-counted stock column")
            if lbad:
                bad.append(f"{table} {p}: {lbad} LCHO NULL(s) without the "
                           "not-counted reason or not on a Small PRP row")
            if rstray:
                bad.append(f"{table} {p}: {rstray} row(s) with a null reason "
                           "but an LCHO value")
            if smallz:
                bad.append(f"{table} {p}: {smallz} Small PRP (Short Form) "
                           "LCHO zero(s): not counted is NULL (rule 1)")
            if off:
                bad.append(f"{table} {p}: {off} row(s) whose total is not "
                           "the sum of the components")
            if znl:
                bad.append(f"{table} {p}: {znl} zero-total row(s) that are "
                           "not LARPs")
    cur.execute(f"SELECT {spec.period_col}, COUNT(*), COUNT(*) FILTER (WHERE "
                f"{counted} OR {lc} IS NULL OR null_reasons IS NOT NULL) "
                f"FROM public.{spec.editions_table} WHERE edition = 1 AND "
                "release_label = %s GROUP BY 1", (core.AS_LOADED_LABEL,))
    loaded = cur.fetchall()
    for p, n, nn in loaded:
        if nn:
            bad.append(f"{spec.editions_table} {p} edition 1 (as loaded): "
                       f"{nn} row(s) with a NULL stock value or a reason; "
                       "the published values as loaded must be kept")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{rows:,} rows over live and the tip editions: no NULL in the four "
        f"always-counted columns; {nulls_seen:,} LCHO NULL(s), each a Small "
        "PRP row with the not-counted reason, and no Small PRP LCHO zero; "
        f"total = components on every row; {zeros} zero-total row(s) all "
        f"LARPs (published zeros); {len(loaded)} edition 1 'as loaded' "
        "period(s) keep the published values (no NULL, no reason)")


def gate_2_edition1_and_latest(cur):
    name = ("every live period has edition 1; the latest edition equals "
            "live cell for cell on the 11 compared columns and null_reasons")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_edition1_and_latest(e.cur, ZZ)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET general_needs_bedspaces = "
             "general_needs_bedspaces + 1, total_social_stock = "
             "total_social_stock + 1 WHERE rp_code = 'L0055' AND "
             "lad24cd = 'E06000001'", "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET rp_size_band = NULL WHERE "
             "rp_code = '4865' AND lad24cd = 'E06000001'", "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET la_name = 'x' WHERE "
             "rp_code = '4865' AND lad24cd = 'E06000001'", "edition-only"),
            (f"DELETE FROM public.{ZZ.live_table} WHERE rp_code = '4865' "
             "AND lad24cd = 'E06000001'", "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET stock_date = "
             "'2030-03-31'", "no edition 1"),
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
          "(a stock cell, a NULLed band, a name, a missing row, a period "
          "without edition 1) are each caught" if ok else
          f"{out['base']} {out['miss']}", cur, real_edition1_and_latest)


def gate_3_provenance(cur):
    name = ("live provenance uniform per period and equal to the tip's "
            "file_* values")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_provenance(e.cur, ZZ)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET source_file = 'other' "
             "WHERE rp_code = '4865' AND lad24cd = 'E06000001'",
             "not uniform"),
            (f"UPDATE public.{ZZ.live_table} SET source_url = 'other'",
             "differs from the tip"),
            (f"UPDATE public.{ZZ.live_table} SET edition = '2023 to 2024'",
             "differs from the tip"),
            (f"UPDATE public.{ZZ.live_table} SET publication_date = "
             "'2020-01-01'", "differs from the tip"),
            (f"UPDATE public.{ZZ.live_table} SET release_page_url = 'x'",
             "differs from the tip"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_provenance(e.cur, ZZ)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(3, name, ok, "seeded provenance passes; a non-uniform row and each "
          "of the five columns drifting from the tip are caught" if ok else
          f"{out['base']} {out['miss']}", cur, real_provenance)


def gate_4_codes(cur):
    name = ("codes: lad24cd in la_boundaries, no E08000038/39 in lad24cd, "
            "publisher_la_code resolves through geography.resolve, no "
            "private recode dict or la_code_lookup query")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_codes(e.cur, ZZ)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET lad24cd = 'E08000038' "
             "WHERE lad24cd = 'E08000016'", "Barnsley/Sheffield"),
            (f"UPDATE public.{ZZ.live_table} SET publisher_la_code = "
             "'E06000099' WHERE lad24cd = 'E06000001'",
             "does not resolve"),
            (f"UPDATE public.{ZZ.live_table} SET publisher_la_code = "
             "'E08000019' WHERE lad24cd = 'E08000016'", "does not resolve"),
        ):
            e.cur.execute("SAVEPOINT plant")
            try:
                e.cur.execute(sql)
                ok, detail = real_codes(e.cur, ZZ)
            except psycopg2.Error as ex:
                ok, detail = False, str(ex)
                e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:90]}")
        out["miss"] = miss
        # a new code in a file halts against the declared form 'old'
        e.tool(codes=["E06000001", "E06000002", "E08000038", "E08000019"],
               year=2026, month="November 2026")
        rc, text, _ = e.cmd(["load", "--release", "2025-2026"])
        out["new_code"] = rc == "halt" and "E08000038" in text
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "stock_date = '2026-03-31'")
        out["new_stored"] = e.cur.fetchone()[0]
    scenario(cur, body)
    # the AST checks: planted constructs are caught, a docstring is not
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text(
            '"""E08000038 in a docstring; la_code_lookup too"""\n'
            'X = {"a": "E08000038"}\n'
            'Q = "SELECT old_code FROM la_code_lookup"\n'
            'def f(v):\n    return v or 0\n'
            'def g(v):\n    return v or 0  # not a source value\n'
            'C = "SELECT COALESCE(x, 0) FROM t"\n', encoding="utf-8")
        ast_bad = _source_problems(p)
    ast_ok = (len(ast_bad) == 4
              and any("line 2" in b for b in ast_bad)
              and any("line 3" in b for b in ast_bad)
              and any("line 5" in b for b in ast_bad)
              and any("line 8" in b for b in ast_bad))
    ok = (out["base"][0] and not out["miss"] and ast_ok
          and out.get("new_code") and out.get("new_stored") == 0)
    detail = ("Barnsley and Sheffield stay E08000016/19; three planted "
              "breaks each caught; a 2026 file carrying E08000038 halts "
              "against the declared 'old' and stores nothing; the AST check "
              "catches a private dict, a la_code_lookup query and a value "
              "coerced to 0, and ignores a docstring and a marked counter"
              if ok else f"seeded: {out} ast {ast_bad}")
    mixed(4, name, ok, detail, cur, real_codes)


def gate_5_row_counts(cur):
    name = "296 authorities in the latest period (live and its tip edition)"
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_row_counts(e.cur, ZZ, n=len(CODES))
        out["wrong_n"] = real_row_counts(e.cur, ZZ, n=len(CODES) + 1)
        e.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
                      "'E08000019'")
        out["short"] = real_row_counts(e.cur, ZZ, n=len(CODES))
    scenario(cur, body)
    ok = (out["base"][0] and not out["wrong_n"][0] and not out["short"][0])
    mixed(5, name, ok, ("the expected count passes; one more authority or a "
                        "missing authority in live fails" if ok else
                        f"{out}"), cur, real_row_counts)


def gate_6_stock_values(cur):
    name = ("no NULL in the four always-counted columns; an LCHO NULL only "
            "on a Small PRP with the not-counted reason, no Small PRP LCHO "
            "zero; total = components, zero totals all LARPs; edition 1 as "
            "loaded keeps the published values; a blank stock cell halts; no "
            "source value coerced to 0")
    out = {}
    probe = (f"UPDATE public.{ZZ.live_table} SET provider_type = 'PRP' "
             "WHERE total_social_stock = 0")

    def body(e):
        e.seed(e.cur)
        out["base"] = real_stock_values(e.cur, ZZ)
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "total_social_stock = 0")
        out["zeros"] = e.cur.fetchone()[0]
        e.cur.execute("SAVEPOINT plant")
        try:
            e.cur.execute(probe)
        except psycopg2.Error:
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            out["pt_chk"] = True
        e.cur.execute("RELEASE SAVEPOINT plant")
        # a Small PRP LCHO zero, a NULL without the reason and an LCHO
        # NULL on a Large PRP each fail the gate (the CHECKs are dropped in
        # a savepoint so the gate itself is tested)
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET low_cost_home_ownership = 0,"
             " null_reasons = NULL WHERE rp_code = 'H0375'",
             "Small PRP (Short Form) LCHO zero"),
            (f"UPDATE public.{ZZ.live_table} SET null_reasons = 'x' WHERE "
             "rp_code = 'H0375'", "without the not-counted reason"),
            (f"UPDATE public.{ZZ.live_table} SET low_cost_home_ownership = "
             "NULL, null_reasons = %s, total_social_stock = "
             "general_needs_self_contained + general_needs_bedspaces + "
             "supported_housing_and_older_people WHERE rp_code = '4865'",
             "not on a Small PRP row")):
            e.cur.execute("SAVEPOINT plant")
            for suffix in ("components_sum_chk", "lcho_null_reason_chk"):
                e.cur.execute(f"ALTER TABLE public.{ZZ.live_table} DROP "
                              f"CONSTRAINT {m.LIVE_PREFIX}_{suffix}")
            e.cur.execute(sql, (m.NOT_COUNTED_REASON,) if "%s" in sql
                          else None)
            ok_, detail = real_stock_values(e.cur, ZZ)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok_ or needle not in detail:
                out.setdefault("missed", []).append(needle)
        # the table's own constraints refuse a NULL in a counted column, an
        # LCHO NULL without its reason, a reason without a NULL and a broken
        # sum
        for sql in (f"UPDATE public.{ZZ.live_table} SET low_cost_home_"
                    "ownership = NULL",
                    f"UPDATE public.{ZZ.live_table} SET general_needs_"
                    "bedspaces = NULL",
                    f"UPDATE public.{ZZ.live_table} SET null_reasons = 'x' "
                    "WHERE low_cost_home_ownership IS NOT NULL",
                    f"UPDATE public.{ZZ.live_table} SET total_social_stock "
                    "= total_social_stock + 1"):
            e.cur.execute("SAVEPOINT plant")
            try:
                e.cur.execute(sql)
                out.setdefault("not_refused", []).append(sql[:60])
            except psycopg2.Error:
                pass
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
        # a blank, a string, a negative and a fraction in a file halt
        for what, cell in (("blank", None), ("string", "[x]"),
                           ("negative", -1), ("fraction", 2.5)):
            e.tool(values={("L0055", "E06000002"): {"LA_GN_SC_Own": cell}},
                   year=2026, month="November 2026")
            before = state(e.cur)
            halted, text = e.halts(["load", "--commit", "--release",
                                    "2025-2026"], before)
            out.setdefault("cells", {})[what] = halted
        # stock_cell: None has no path to 0
        try:
            m.stock_cell(None, "x")
            out["none_to_zero"] = True
        except ValueError:
            out["none_to_zero"] = False
        out["zero_kept"] = m.stock_cell(0, "x") == 0
    scenario(cur, body)
    src = _source_problems(LOADER)
    ok = (out["base"][0] and out["zeros"] == 1 and not out.get("not_refused")
          and not out.get("missed") and all(out["cells"].values())
          and not out["none_to_zero"] and out["zero_kept"] and not src)
    detail = ("the seeded state passes (its one zero total is a LARP; its "
              "Small PRP LCHO cells are NULL with the reason); a Small PRP "
              "LCHO zero, a NULL without the reason and an LCHO NULL on a "
              "Large PRP each fail; a blank, a text marker, a negative and a "
              "fractional cell each halt and store nothing; stock_cell(None) "
              "raises and a published 0 stays 0; the table refuses a NULL in "
              "a counted column, a NULL without its reason, a reason without "
              "a NULL and a broken sum; no source value is coerced to 0 in "
              "the loader" if ok else f"seeded: {out} {src[:2]}")
    mixed(6, name, ok, detail, cur, real_stock_values)


# --------------------------------------------------------- file gates (7, 8)

def held_file(files=None, raw_dir=None):
    """The file on disk whose sha256 is the held file's, or None."""
    files = m.LEGACY_FILE if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    for pat in ("*.xlsx",):
        for p in sorted(raw.glob(pat)):
            if m.content_sha256(p) == files["sha256"]:
                return p
    return None


def real_reconciliation(files=None, raw_dir=None, areas=AREAS,
                        regions=REGIONS):
    """The in-file reconciliation re-run read-only from the held file on
    disk: provider sums = LA subtotals on all five measures, subtotals =
    regions, the expected number of authorities and regions."""
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, (f"no file in {raw_dir or m.RAW_DIR} with sha256 "
                       f"{files['sha256'][:16]}")
    with expected(areas, regions):
        tool = m.read_tool(p)
        bad = m.reconcile(tool)
    if bad:
        return False, f"{p.name}: reconciliation failed: {bad[0]}"
    eng = m.england_totals(tool)
    return True, (f"{p.name}: {len(tool['providers']):,} provider rows "
                  f"reconcile to {len(tool['subtotals'])} LA subtotals and "
                  f"{len(tool['regions'])} regions on all five measures; "
                  "England " + " / ".join(f"{eng[c]:,}"
                                          for c in m.STOCK_COLUMNS))


def gate_7_reconciliation(cur):
    name = ("in-file reconciliation re-run read-only from the file on disk; "
            "a planted cell, subtotal or region off by one is caught")
    problems = []

    def body(e):
        t = m.read_tool(e.tool(register=False))
        if m.reconcile(t):
            problems.append(f"the clean fixture does not reconcile: "
                            f"{m.reconcile(t)[:1]}")
        p = [x for x in t["providers"] if x["rp_code"] == "L0055"
             and x["la_code"] == "E06000002"][0]
        p["values"]["general_needs_self_contained"] += 1
        out = m.reconcile(t)
        if not any("E06000002" in x for x in out):
            problems.append("a provider cell off by one was not caught")
        t = m.read_tool(e.tool(register=False,
                               subtotal={"E08000019": {"LA_SHHOP": 1}}))
        out = m.reconcile(t)
        if not (any("E08000019" in x for x in out)
                and any("Yorkshire and The Humber" in x for x in out)):
            problems.append(f"an LA subtotal off by one: {out[:2]}")
        t = m.read_tool(e.tool(register=False,
                               region={"North East": {"LA_SHHOP": 1}}))
        if not any("North East" in x for x in m.reconcile(t)):
            problems.append("a region row off by one was not caught")
        t = m.read_tool(e.tool(register=False, codes=CODES[:3]))
        if not any("authorit" in x or "subtotal" in x
                   for x in m.reconcile(t)):
            problems.append("a missing authority was not caught")
        # the real-file part, on a fixture standing in for the held file
        d = Path(e.root) / "disk"
        path = write_tool(d / "held.xlsx")
        fx = {"sha256": m.content_sha256(path)}
        ok, detail = real_reconciliation(fx, d, areas=len(CODES), regions=2)
        if not ok:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_reconciliation({"sha256": "0" * 64}, d,
                                         areas=len(CODES), regions=2)
        if ok or "no file" not in detail:
            problems.append("a missing file was accepted")
        bad = write_tool(d / "bad.xlsx", subtotal={"E06000001":
                                                   {"LA_SHHOP": 1}})
        ok, detail = real_reconciliation(
            {"sha256": m.content_sha256(bad)}, d, areas=len(CODES),
            regions=2)
        if ok or "reconciliation failed" not in detail:
            problems.append("a file that does not reconcile was accepted")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a clean file reconciles; a provider cell, an LA subtotal, "
          "a region and a missing authority each off are caught; the "
          "re-run finds the file by sha256" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(7, name, False, sd)
    ok, detail = real_reconciliation()
    report(7, name, ok, f"{sd}; held file: {detail}")


def real_identity(cur=None, files=None, raw_dir=None, spec=REAL):
    """The held file's identity read from the file itself equals what the
    loader surveyed (stock date, rank), and, once the editions table
    exists, every period's tip names a version and a month."""
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, f"no file with sha256 {files['sha256'][:16]}"
    with expected():
        tool = m.read_tool(p)
    bad = []
    if tool["rank"] != m.LEGACY_RANK:
        bad.append(f"{p.name}: rank {tool['rank']}, surveyed "
                   f"{m.LEGACY_RANK}")
    if (tool["y1"], tool["y2"]) != (int(tool["stock_date"][:4]) - 1,
                                    int(tool["stock_date"][:4])):
        bad.append("source years and stock date disagree")
    if tool["history"][-1][0] != tool["version"]:
        bad.append("Version History last row is not the Introduction's "
                   "version")
    tips = ""
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        info = m.tip_info(cur, spec)
        off = [pp for pp, i in info.items() if i["rank"] is None]
        if off:
            bad.append(f"{off[:3]}: the tip's source_file names no version "
                       "and month")
        tips = f"; {len(info)} stored period(s), each tip names its file"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{p.name}: stock date {tool['stock_date']}, edition "
        f"{tool['edition']!r}, version {tool['version']}, "
        f"{tool['published']:%B %Y}, Version History agrees{tips}")


def gate_8_identity(cur):
    name = ("identity from the file: a seeded title, source line, Version "
            "History, header or page-year mismatch halts")
    problems = []

    def body(e):
        tmpd = str(e.root)

        def raises(label, **kw):
            try:
                m.read_tool(write_tool(Path(tmpd) / f"{label}.xlsx", **kw))
                problems.append(f"{label} was read")
            except ValueError:
                pass
        raises("title", title="RP social housing by local authority area "
               "(SDR and LADR data) 2024")
        raises("source", source="Statistical Data Return (SDR)/Local "
               "Authority Data Return (LADR) 1 April 2023 to 31 March 2025")
        raises("history", history=[(1, "October 2025", "Original.")])
        raises("tail", history_tail=[["Publication date: November 2025"],
                                     ["Version: 1.2"]])
        raises("header", header=["x"] + list(HEADERS[1:]))
        raises("old_header", header=HEADERS_2024)
        t = m.read_tool(write_tool(Path(tmpd) / "ok.xlsx"))
        if m.identity_problems(t, (2023, 2024)) == []:
            problems.append("a page of other years was accepted")
        if m.identity_problems(t, None, (2023, 2024)) == []:
            problems.append("--release of other years was accepted")
        if m.identity_problems(t, (2024, 2025), (2024, 2025)):
            problems.append("a matching page was refused")
        # through the command: a page title of other years, and a file of
        # other years under --release, halt and store nothing
        e.tool()
        before = state(e.cur)
        e.api[BASE.format(2024, 2025)]["title"] = (
            tl.TITLE.format(2023, 2024))
        ok, _ = e.halts(["load", "--commit"], before)
        if not ok:
            problems.append("a page of other years did not halt the load")
        path = e.tool(register=False)
        ok, text = e.halts(["load", "--file", path, "--release", "2023-2024",
                            "--commit"], before, "--release 2023-2024")
        if not ok:
            problems.append("--file with another --release did not halt")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a title year, source line, Version History row, Version "
          "History tail, unknown header, 2024-style header, page years and "
          "--release years that disagree with the file each halt, and "
          "nothing is stored" if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(8, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(8, name, False, _first(ex))
    report(8, name, ok, f"{sd}; held file: {detail}")


# ---------------------------------------------------------------- gate 9-13

def gate_9_older_file(cur):
    name = ("older-file guard on the page, --release and --file paths; the "
            "rank is the file's own, never its name")
    problems = []

    def older(e, how):
        e.seed(e.cur)
        # the content is version 1.0 of October 2025; the NAME looks newer
        path, url = e.named("RP_COMBINED_TOOL_2025_FINAL_V1.3.xlsx",
                            serve=how != "file", version="1.0",
                            month="October 2025",
                            values={("L0055", "E06000002"):
                                    {"LA_GN_SC_Own": 110}})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2024-2025"]
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

        def override(c):
            with env(c) as e2, tl.specs():
                argv = older(e2, "file") + ["--allow-older-file"]
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
                e2.tool(values={("L0055", "E06000002"):
                                {"LA_GN_SC_Own": 140}})
                before = state(c)
                rc, text, logged = e2.cmd(["load", "--commit"])
                if not (rc == 1 and "two files claim the same version"
                        in text and state(c) == before):
                    problems.append(f"equal rank, other content: rc {rc}: "
                                    f"{text[-120:]}")
        _in_savepoint(e.cur, equal_rank)
    scenario(cur, body)
    report(9, name, not problems, "a version 1.0 (October 2025) file named "
           "like V1.3, offered by the page, --release and --file against a "
           "held 1.1 (November 2025), is skipped and halts with nothing "
           "stored or logged; --allow-older-file overrides and is logged; "
           "equal rank with other content stops" if not problems
           else "; ".join(problems[:3]))


def rejects(e, argv, needle, before, *, commit=True):
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
                f"rejected ['{P}']" not in notes:
            return f"run-log notes: {notes[:120]}"
    elif logged.called:
        return "a preview wrote a run-log row"
    return None


def gate_10_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row")
    problems = []

    def case(label, setup, needle, commit=True):
        def body(c):
            with env(c) as e, tl.specs():
                setup(e)
                before = state(c)
                argv = ["load"] + (["--commit"] if commit else [])
                bad = rejects(e, argv, needle, before, commit=commit)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)

    def seed_rev(e, **kw):
        e.seed(e.cur)
        e.revise(**kw)

    def seed_new(e, **kw):
        e.seed(e.cur, year=2024, month="November 2024")
        e.tool(**kw)
    case("revision: one authority's total +4000",
         lambda e: seed_rev(e, values={("L0055", "E06000002"):
                                       {"LA_GN_SC_Own": 5000}}),
         "E06000002")
    case("revision: national total moves more than 1%",
         lambda e: seed_rev(e, values={("L0055", "E06000002"):
                                       {"LA_GN_SC_Own": 5000}}),
         "national total_social_stock")
    case("revision: providers removed beyond 5%",
         lambda e: seed_rev(e, drop=[("H0375", "E06000001"),
                                     ("H0375", "E06000002")]),
         "provider rows removed")
    case("revision: preview of the same writes no run-log row",
         lambda e: seed_rev(e, values={("L0055", "E06000002"):
                                       {"LA_GN_SC_Own": 5000}}),
         "E06000002", commit=False)
    case("new period: national total moves more than 5%",
         lambda e: seed_new(e, values={(f"00C{i}", c): {"LA_GN_SC_Own": 5000}
                                       for i, c in enumerate(CODES)}),
         "national total_social_stock")
    case("new period: supported housing moves more than 10%",
         lambda e: seed_new(e, values={(f"00C{i}", c): {"LA_SHHOP": 400}
                                       for i, c in enumerate(CODES)}),
         "supported_housing_and_older_people")

    def partial(c):
        with env(c) as e, tl.specs():
            e.seed(c)
            tip = m.records(c, ZZ, P, 1)
            drop = {r["lad24cd"] for r in tip[:1]}
            part = [r for r in tip if r["lad24cd"] not in drop]
            out = m.period_problems(part, tip, None, kind="revised")
            if not any("missing from the file" in x for x in out):
                problems.append(f"a file missing an authority: {out}")
            out = m.period_problems(part, None, tip, kind="new")
            if not any("authorities, expected" in x for x in out):
                problems.append(f"a new period of fewer authorities: {out}")
            # both sides of a limit: +1 on one cell is within every limit
            ok = [dict(r) for r in tip]
            ok[0]["general_needs_self_contained"] += 1
            ok[0]["total_social_stock"] += 1
            if m.period_problems(ok, tip, None, kind="revised"):
                problems.append("a one-cell revision was rejected")
    _in_savepoint(cur, partial)

    def within(c):
        with env(c) as e, tl.specs():
            e.seed(c)
            before = tl.live_val(c, "L0055", "E06000002")
            e.revise(values={("L0055", "E06000002"):
                             {"LA_GN_SC_Own": before + 1}})
            rc, text, _ = e.cmd(["load", "--commit"])
            if rc != 0 or tl.editions(c) != [(1, None, N), (2, 1, N)]:
                problems.append(f"a revision within the limits was refused: "
                                f"rc {rc}")
    _in_savepoint(cur, within)
    report(10, name, not problems, "a revision with a large area move, a "
           "national move or providers removed, and a new period with a "
           "national total or supported-housing move, are each REJECTED "
           "(exit 1) with nothing stored, no ledger row and one partial "
           "run-log row (none in a preview); a partial file is caught by "
           "period_problems; a revision within the limits is stored"
           if not problems else "; ".join(problems[:3]))


def gate_11_one_transaction(cur):
    name = ("a new period: edition, live rows and ledger row in one "
            "transaction")
    problems = []

    def good(e):
        e.tool()
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
                e.tool()
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
    failing("a failing live insert", m, "insert_live")
    failing("a failing edition insert", core, "insert_edition")
    report(11, name, not problems, "edition 1, the live rows and one 'new' "
           "ledger row are stored together; a failing ledger insert, live "
           "insert or edition insert rolls all three back and logs a "
           "partial run" if not problems else "; ".join(problems[:3]))


def gate_12_ledger_skip(cur):
    name = "ledger skip needs the (URL, sha256) pair for the period"
    problems = []

    def body(e):
        path = e.tool()
        rank = (date(2025, 11, 1), (1, 1))
        sha = m.content_sha256(path)
        src = m.ledger_source(e.last_url, rank, "1.1", P)
        # (URL, sha) recorded, but under another stock date: read
        e.cur.execute(f"INSERT INTO public.{LEDGER} (stock_date, "
                      "source_file, file_sha256, outcome, edition) VALUES "
                      "(%s, %s, %s, 'new', 1)", (PRIOR, src, sha))
        rc, text, _ = e.cmd(["load"])
        if rc != 0 or "nothing parsed" in text or f"{P}: new" not in text:
            problems.append("a pair held for another period skipped the "
                            "file")
    scenario(cur, body)

    def same_name(e):
        path0 = e.tool()
        e.ok(e.cur, ["load", "--commit"])
        url0 = e.last_url
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "nothing parsed" not in text or logged.called:
            problems.append("a held pair did not skip the read")
        # the same URL with other bytes: the sha differs, so it is read
        e.tool(version="1.2", month="December 2025", url=url0,
               values={("L0055", "E06000002"): {"LA_GN_SC_Own": 140}})
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" in text or f"{P}: revised" not in text:
            problems.append("the same URL with other bytes was skipped")
        # the pair is held for the period: --recheck reads it anyway
        e.urls[url0] = path0
        e.page(2024, 2025, url0)
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" not in text:
            problems.append("the held pair no longer skips")
        rc, text, _ = e.cmd(["load", "--recheck", P])
        if f"{P}: unchanged" not in text:
            problems.append("--recheck did not read a ledger-held file")
    scenario(cur, same_name)
    report(12, name, not problems, "a ledger pair held under another stock "
           "date does not skip; the held pair skips with nothing parsed or "
           "logged; the same URL with other bytes is read; --recheck reads "
           "a held file" if not problems else "; ".join(problems[:3]))


def gate_13_preview(cur):
    name = "preview and simulate write nothing"
    problems = []
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]

    def body(e):
        e.tool()
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"new period {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.revise(values={("L0055", "E06000002"): {"LA_GN_SC_Own": 140}})
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
    report(13, name, not problems, "load, refresh-latest, restore-edition "
           "and migrate-legacy in preview and --simulate leave the editions, "
           "the live table and the ledger as they were and log nothing"
           if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 14

def real_held_reread(cur, spec=REAL, files=None, raw_dir=None):
    """The held file read again carries the not-counted rule. Read as
    published (the rule off) it has the content sha256 of edition 1 'as
    loaded' (key + the 11 compared columns; provenance is not in it). Read
    with the rule (as every load reads it) it has the content sha256 of the
    period's tip, and its 0/NULL changes against edition 1 are exactly those
    a named acknowledgement for the period records (each LCHO 0 -> NULL with
    the reason, the recorded count). So a re-read is unchanged against the
    tip and never turns the cells back into 0."""
    _need(cur, spec)
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, f"no file with sha256 {files['sha256'][:16]}"
    with expected():
        tool = m.read_tool(p)
    period = tool["stock_date"]
    cur.execute(f"SELECT DISTINCT release_label, source_file FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (period,))
    rows = cur.fetchall()
    if len(rows) != 1:
        return False, f"{period}: edition 1 missing or not single"
    label, sf = rows[0]
    held = m.records(cur, spec, period, 1)
    prov = dict(zip(m.PROVENANCE, m._provenance_of(held, period)))
    with contextlib.redirect_stdout(io.StringIO()):
        plain = m._build(cur, tool, prov, rule=False)
        ruled = m._build(cur, tool, prov)
    tip = _tip(cur, spec, period)
    tiprecs = m.records(cur, spec, period, tip)
    a, b = m.rows_content_sha(held), m.rows_content_sha(plain)
    t, r = m.rows_content_sha(tiprecs), m.rows_content_sha(ruled)
    bad = []
    if a != b:
        bad.append(f"{period}: edition 1 content sha {a[:12]} differs from "
                   f"the held file read now as published {b[:12]}")
    if t != r:
        bad.append(f"{period}: the tip (edition {tip}) content sha {t[:12]} "
                   f"differs from the held file read now with the not-"
                   f"counted rule {r[:12]}")
    fl = m._flips(ruled, held)
    names = [n for n, x in m.ACKNOWLEDGED_FLIPS.items()
             if x["stock_date"] == period]
    covered = [n for n in names if not m.ack_problems(n, period, fl, ruled)]
    if fl and not covered:
        bad.append(f"{period}: {len(fl):,} 0/NULL change(s) against edition "
                   "1 that no named acknowledgement covers ("
                   + ("; ".join(f"{n}: {m.ack_problems(n, period, fl, ruled)}"
                                for n in names) or "none recorded") + ")")
    if label != core.AS_LOADED_LABEL:
        bad.append(f"{period}: edition 1 is not 'as loaded' ({label!r})")
    if m.release_rank(sf) != tool["rank"]:
        bad.append(f"{period}: edition 1 source_file ranks "
                   f"{m.release_rank(sf)}, the file {tool['rank']}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{p.name} read now: as published it has edition 1's content "
        f"sha256 ({a[:12]}, {len(held):,} rows); with the not-counted rule "
        f"it has the tip's (edition {tip}, {t[:12]}), {len(fl):,} LCHO "
        "cell(s) 0 -> NULL against edition 1"
        + (f", exactly as {covered[0]} records" if covered else "")
        + "; so a re-read by the page, --release or --file is unchanged "
        "against the tip")


def gate_14_held_file_recheck(cur):
    name = ("a byte-identical re-read carries the not-counted rule: as "
            "published it equals edition 1, with the rule it equals the tip; "
            "0/NULL changes only through the named acknowledgement")
    problems = []
    out = {}

    def run(label, **kw):
        def body(e, path, files):
            plan, text = e.migrate(e.cur, path, files)
            d = Path(e.root) / "disk"
            d.mkdir()
            (d / path.name).write_bytes(path.read_bytes())
            # before the rule's edition: the tip (edition 1) is not what a
            # load reads now
            with expected(len(CODES), 2), tl.ack_cells():
                out[label + " before"] = real_held_reread(e.cur, ZZ, files, d)
            try:
                e.check_reread_after_migration(e.cur, path, files)
            except AssertionError as ex:
                problems.append(f"{label}: {_first(ex)}")
                return
            with expected(len(CODES), 2), tl.ack_cells():
                out[label] = real_held_reread(e.cur, ZZ, files, d)
            with expected(len(CODES), 2), tl.ack_cells(tl.SMALL + 1):
                out[label + " count"] = real_held_reread(e.cur, ZZ, files, d)
        legacy_world(cur, body, **kw)
    run("held")
    run("blank", values={("H0375", "E06000002"): {"Survey_Status": None,
                                                  "SDR_Size": None}})
    for label, (ok, detail) in out.items():
        if label.endswith(" before"):
            if ok or "not-counted rule" not in detail:
                problems.append(f"{label}: the as-loaded tip passed: "
                                f"{detail[:100]}")
        elif label.endswith(" count"):
            if ok or "no named acknowledgement covers" not in detail:
                problems.append(f"{label}: another count passed: "
                                f"{detail[:100]}")
        elif not ok:
            problems.append(f"{label}: {detail}")

    def changed(e, path, files):
        e.migrate(e.cur, path, files)
        other = write_tool(Path(e.root) / "disk2" / path.name,
                           values={("L0055", "E06000002"):
                                   {"LA_GN_SC_Own": 140}})
        with expected(len(CODES), 2):
            ok, detail = real_held_reread(
                e.cur, ZZ, {"sha256": m.content_sha256(other)},
                Path(other).parent)
        if ok or "differs" not in detail:
            problems.append("a file with other content was called equal")
    legacy_world(cur, changed)
    seeded = not problems
    sd = ("seeded: after migrate-legacy the same bytes read by the page, "
          "--release, --file, --file --no-page, --recheck and with --commit "
          "are REJECTED by the 0/NULL stop (and with the acknowledgement at "
          "another count), store nothing, then the named acknowledgement "
          "stores edition 2 (exactly the Small PRP LCHO cells 0 -> NULL with "
          "the reason, provenance kept), refresh-latest brings live to it and "
          "every path is then unchanged (also with a published blank band "
          "and status); the as-loaded tip and another count fail the real "
          "check; a file with other content is not equal" if seeded
          else "; ".join(problems[:3]))
    mixed(14, name, seeded, sd, cur, real_held_reread)


# ---------------------------------------------------------------- gate 15

def gate_15_revision_and_refresh(cur):
    name = ("a revision, then refresh-latest: only that period changes, "
            "provenance on every row, loaded_at copied; key changes only "
            "with --accept-key-changes")
    problems = []

    def rows_hash(c, period):
        c.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                  f"FROM public.{ZZ.live_table} t WHERE stock_date = %s",
                  (period,))
        return c.fetchone()[0]

    def by_key(c, period):
        cols = ", ".join(m.KEY + m.COMPARED)
        c.execute(f"SELECT {cols} FROM public.{ZZ.live_table} WHERE "
                  "stock_date = %s", (period,))
        return {r[:2]: r for r in c.fetchall()}

    def body(e):
        e.seed(e.cur, year=2024, month="November 2024")
        e.seed(e.cur)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        prior_before = rows_hash(e.cur, PRIOR)
        before = by_key(e.cur, P)
        first = tl.live_val(e.cur, "L0055", "E06000002")
        e.revise(values={("L0055", "E06000002"):
                         {"LA_GN_SC_Own": first + 3}})
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N), (2, 1, N)] or \
                tl.editions(e.cur, PRIOR) != [(1, None, N)]:
            problems.append(f"the revision did not store edition 2 of {P} "
                            f"only: rc {rc}")
        if by_key(e.cur, P) != before:
            problems.append("load changed the live table before "
                            "refresh-latest")
        if pe.status(e.cur, m.profile())["pending_refresh"] != [P]:
            problems.append("status does not name the pending refresh")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("the refresh-latest preview wrote")
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if rc != 0:
            problems.append(f"refresh-latest --commit: rc {rc}: "
                            f"{text[-120:]}")
        after = by_key(e.cur, P)
        changed = [k for k in after if after[k] != before.get(k)]
        if changed != [("L0055", "E06000002")]:
            problems.append(f"refresh-latest changed {changed[:3]}, "
                            "expected that one row")
        if rows_hash(e.cur, PRIOR) != prior_before:
            problems.append("refresh-latest changed the other period")
        e.cur.execute(f"SELECT DISTINCT {', '.join(m.LIVE_PROVENANCE)} FROM "
                      f"public.{ZZ.live_table} WHERE stock_date = %s", (P,))
        live = e.cur.fetchall()
        e.cur.execute(f"SELECT DISTINCT {', '.join(m.PROVENANCE)}, loaded_at"
                      f" FROM public.{ZZ.editions_table} WHERE stock_date = "
                      "%s AND edition = 2", (P,))
        tip = e.cur.fetchall()
        if len(live) != 1 or len(tip) != 1 or live[0] != tip[0][:5]:
            problems.append("live provenance is not the tip's on every row "
                            f"({len(live)} distinct)")
        elif live[0][1] != date(2025, 12, 1):
            problems.append(f"provenance publication_date {live[0][1]}")
        # loaded_at: the refreshed row takes the edition's, the rest keep
        # theirs (the engine writes loaded_at where it updates a row)
        if len(tip) == 1:
            e.cur.execute(f"SELECT rp_code, lad24cd FROM public."
                          f"{ZZ.live_table} WHERE stock_date = %s AND "
                          "loaded_at = %s", (P, tip[0][5]))
            if e.cur.fetchall() != [("L0055", "E06000002")]:
                problems.append("loaded_at was not copied to exactly the "
                                "refreshed row")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "stock_date = %s AND loaded_at <> '2026-01-01' AND "
                      "NOT (rp_code = 'L0055' AND lad24cd = 'E06000002')",
                      (P,))
        if e.cur.fetchone()[0]:
            problems.append("an unchanged row's loaded_at moved")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} WHERE "
                      "stock_date = %s AND loaded_at <> '2026-01-01'",
                      (PRIOR,))
        if e.cur.fetchone()[0]:
            problems.append("the other period's loaded_at moved")
        if m.live_equals_tip(e.cur, ZZ, [P, PRIOR]) or \
                not pe.status(e.cur, m.profile())["ok"]:
            problems.append("live does not equal the tips after the "
                            "refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("a second refresh-latest plans a write")
        # key changes
        e.tool(version="1.3", month="January 2026",
               drop=[("H0375", "E06000002")],
               add=[["New Small Limited", "H9999", "Small", "Short Form",
                     "Signed_Off", "E06000001", (1, 0, 0, 0)]])
        rc, text, _ = e.cmd(["load", "--commit"])
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if "keys added 1 (H9999/E06000001); removed 1 " \
                "(H0375/E06000002)" not in text:
            problems.append(f"the plan does not name the key changes: "
                            f"{text[-200:]}")
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if not (rc == "halt" and "not named" in text
                and state(e.cur) == snap):
            problems.append(f"key changes applied without "
                            f"--accept-key-changes: rc {rc}")
        rc, text, logged = e.cmd(["refresh-latest", "--commit",
                                  "--accept-key-changes", P])
        if rc != 0 or "1 inserted, 1 deleted" not in text or \
                tl.live_val(e.cur, "H9999", "E06000001") != 1 or \
                tl.live_val(e.cur, "H0375", "E06000002") is not None or \
                tl.count(e.cur, ZZ.live_table, "WHERE stock_date = %s",
                         (P,)) != N:
            problems.append(f"--accept-key-changes: rc {rc}: {text[-160:]}")
        if len(tl.provenance(e.cur)) != 1 or m.live_equals_tip(
                e.cur, ZZ, [P, PRIOR]):
            problems.append("provenance not uniform after the key changes")
    scenario(cur, body)
    report(15, name, not problems, "a revised 2025 stock date waits for "
           "refresh-latest (preview writes nothing); the commit changes "
           "exactly the revised row, leaves the 2024 rows alone, sets the "
           "five provenance columns on every row and copies the edition's "
           "loaded_at to that row; "
           "added or removed providers halt without --accept-key-changes "
           "and then insert and delete exactly the planned keys"
           if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 16

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def note_hashes(note):
    """{period: (rows, sha256)} from lines `before-state rsh_rp_stock_by_la
    <period> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the
    decision note (the two halves joined), or {}. (The `before-state-all`
    lines are a different record and do not match.)"""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                r"before-state rsh_rp_stock_by_la ([0-9]{4}-[0-9]{2}-"
                r"[0-9]{2}) rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})", text)}


def real_edition1_hash(cur, spec=REAL, note=NOTE, legacy_rows=None):
    """Edition 1 'as loaded' of every migrated period equals the before-
    state line recorded in the decision note: the row count and the
    rows_content_sha256 (key + the 11 compared columns), and the row count
    is the surveyed live count."""
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
                           f"rsh_rp_stock_by_la {p} rows=.. sha256-first32=.. sha256-last32=..' line")
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
        "edition 1 of every migrated period hashes to the before-state "
        "line in the decision note (" + "; ".join(parts) + ")")


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
            line = (f"before-state rsh_rp_stock_by_la {P} rows={N} "
                    f"{hash_fields(h)}")
            note.write_text(f"intro\n{line}\nbefore-state-all "
                            f"rsh_rp_stock_by_la {P} rows={N} "
                            f"{hash_fields('1' * 64)}\n", encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(note.read_text().replace(h[:32], "0" * 32),
                            encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(f"before-state-all rsh_rp_stock_by_la {P} "
                            f"rows={N} {hash_fields(h)}\n", encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}"),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, N)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                N)
            note.write_text(line, encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(e.cur, ZZ, note, N + 5)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET total_social_"
                      "stock = total_social_stock + 1, "
                      "general_needs_self_contained = general_needs_self_"
                      "contained + 1 WHERE rp_code = 'L0055' AND lad24cd = "
                      "'E06000002'")
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

def real_migration_proof(cur, spec=REAL, files=None, raw_dir=None,
                         areas=AREAS, regions=REGIONS):
    """The migration proof re-run read-only from the file on disk (found by
    sha256): the parser on the held file reproduces every cell of edition 1
    'as loaded' (the key, the stock date and the 11 compared columns; 0
    differences) and the file reconciles."""
    _need(cur, spec)
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, (f"no file in {raw_dir or m.RAW_DIR} with sha256 "
                       f"{files['sha256'][:16]}")
    with expected(areas, regions):
        tool = m.read_tool(p)
        bad = m.reconcile(tool)
    if bad:
        return False, f"{p.name}: reconciliation failed: {bad[0]}"
    period = tool["stock_date"]
    held = m.records(cur, spec, period, 1)
    if not held:
        return False, f"{period}: no edition 1"
    prov = dict(zip(m.PROVENANCE, m._provenance_of(held, period)))
    with contextlib.redirect_stdout(io.StringIO()):
        built = m._build(cur, tool, prov, rule=False)   # as published
    cells, diffs = m._proof(held, built)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (period,))
    if cur.fetchall() != [(core.AS_LOADED_LABEL,)]:
        return False, f"{period}: edition 1 is not the 'as loaded' one"
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{cells:,} cells of edition 1 ({len(held):,} rows) "
                       f"equal {p.name} read now (0 differences)")


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
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw,
                                          areas=len(CODES), regions=2)
        if not ok or "0 differences" not in detail:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(e.cur, ZZ, {**files,
                                                      "sha256": "0" * 64},
                                          raw, areas=len(CODES), regions=2)
        if ok or "no file" not in detail:
            problems.append("a file with another sha256 was accepted")
        # a file changed on disk is not found by sha256
        (raw / path.name).write_bytes(path.read_bytes() + b"\0")
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw,
                                          areas=len(CODES), regions=2)
        if ok or "no file" not in detail:
            problems.append("a changed file on disk was accepted")
        # a different, valid file stands in for the held one: edition 1 is
        # not that file
        other = write_tool(Path(e.root) / "raw2" / path.name,
                           values={("L0055", "E06000002"):
                                   {"LA_GN_SC_Own": 140}})
        ok, detail = real_migration_proof(
            e.cur, ZZ, {**files, "sha256": m.content_sha256(other)},
            other.parent, areas=len(CODES), regions=2)
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
        f"UPDATE public.{ZZ.live_table} SET general_needs_self_contained = "
        "general_needs_self_contained + 1, total_social_stock = "
        "total_social_stock + 1 WHERE rp_code = 'L0055' AND lad24cd = "
        "'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZZ.live_table} SET la_name = 'Other' WHERE "
        "rp_code = '4865' AND lad24cd = 'E06000001'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZZ.live_table} SET rp_size_band = NULL WHERE "
        "rp_code = '4865' AND lad24cd = 'E06000001'", "proof failed"))
    legacy_world(cur, planted(
        f"DELETE FROM public.{ZZ.live_table} WHERE rp_code = 'L0055' AND "
        "lad24cd = 'E06000002'", "not as surveyed", surveyed_first=True))
    mixed(17, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the file by sha256; a changed or missing file and a file that "
          "is not edition 1 are caught; planted differences in a stock "
          "cell, a name and a NULLed band, and a dropped row stop "
          "migrate-legacy with nothing stored" if not problems
          else "; ".join(problems[:3]), cur, real_migration_proof)


# ---------------------------------------------------------- gates 18 - 20

def gate_18_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        path = e.tool()
        e.ok(e.cur, ["load", "--commit"])
        snap, led = tables_state(e.cur), len(tl.ledger(e.cur))
        for argv in (["load", "--commit"],
                     ["load", "--release", "2024-2025", "--commit"],
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
        if len(tl.provenance(e.cur)) != 1 or not pe.status(
                e.cur, m.profile())["ok"] or m.live_equals_tip(
                e.cur, ZZ, [P]):
            problems.append("the repaired rows do not equal the tip")
    scenario(cur, body)
    report(19, name, not problems, f"a stranded {P} was rebuilt from its "
           "edition 1 with no new edition, the tip's provenance and a "
           "live-missing ledger row" if not problems
           else "; ".join(problems[:3]))


def gate_20_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.live_val(e.cur, "L0055", "E06000002")
        e.revise(values={("L0055", "E06000002"): {"LA_GN_SC_Own": first + 3}})
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
        if tl.live_val(e.cur, "L0055", "E06000002") != first + 3:
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.live_val(e.cur, "L0055", "E06000002") != first or \
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
                e.tool()
                rc, _, _ = e.cmd(["load", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load"])
                raw = e.root / "raw"
                body_a = write_tool(e.root / "dl" / "a.xlsx").read_bytes()
                body_b = write_tool(e.root / "dl" / "b.xlsx",
                                    version="1.2", month="December 2025",
                                    ).read_bytes()
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
         gate_3_provenance, gate_4_codes, gate_5_row_counts,
         gate_6_stock_values, gate_7_reconciliation, gate_8_identity,
         gate_9_older_file, gate_10_stop_conditions,
         gate_11_one_transaction, gate_12_ledger_skip, gate_13_preview,
         gate_14_held_file_recheck, gate_15_revision_and_refresh,
         gate_16_before_state_hash, gate_17_migration_proof, gate_18_rerun,
         gate_19_stranded, gate_20_restore, gate_21_no_network_no_secrets)


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
    mine = [t for t in left if t.startswith("zz_s23")]
    if mine:
        print(f"LEFTOVER: committed zz_s23 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s23 table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
