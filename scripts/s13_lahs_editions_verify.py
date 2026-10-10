"""Gates for the S13 (MHCLG Local Authority Housing Statistics, households on
the housing register) editions tables.

Mirrors scripts/s10_rough_sleeping_editions_verify.py,
s23_rsh_stock_editions_verify.py and s6_asylum_editions_verify.py.
Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes nothing, not
even a run-log row. The real-table gates read la_housing_register,
la_housing_register_editions and la_housing_register_editions_file_checks.
Until the editions table exists (ddl and migrate-legacy not yet run) they
print `FAIL (pending migration)` with the words "not yet migrated", so the
script exits non-zero until then, like the other verifiers; nothing is
created to make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s13_live / zz_s13_editions / zz_s13_editions_file_checks
(a copy of the loader's spec), created inside the transaction, and call the
loader's own writer and commands (s13_lahs_editions.main with the SPEC
pointed at the copy, the content API and the downloads stubbed; apply_year;
migrate_legacy; restore_edition) rather than a re-implementation. The real
tables are only ever read. Seeded parts use the loader tests' fixture
files (a handful of authorities, written to a temporary folder); they never
rely on a private constant of the loader.

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), live checks, no editions-only column on live  (throwaway + real)
  2  every live period has edition 1; the latest edition equals live cell
     for cell on the key, the year and the three values             (seeded + real)
  3  codes: every lad24cd in la_boundaries, no E08000038/39 stored, the held
     CSV's codes resolve through geography.resolve and la_code_lookup to the
     stored codes, source 13 declared 'old', Dorset resolves only through
     la_code_lookup (the five abolished district codes have rows there) and
     the loader holds no private recode dict or constant (AST)
  4  296 authorities in every live period (and in its tip edition)
  5  rule 1: no 0 where the latest edition's value_flag says NULL, no NULL
     without a flag, the [x] / [z] / [s] counts per column reported, and the
     only zero-to-NULL rules are the two named constants (AST: no other
     literal code compared with 0; no value coerced to 0)  (seeded + real)
  6  a successor total is NULL unless every predecessor is present
     (part_missing); cc2a only when every predecessor agrees (seeded + real)
  7  CSV-ODS cross-check re-run read-only on the files on disk
  8  identity from the files (seeded Cover, dictionary-definition, header,
     page and --release mismatches halt)
  9  older-file guard on the page, --release, --file and back-fill paths; the
     rank is the ODS Cover's own, never a file name
 10  stop conditions: each seeded REJECTED, stores nothing, no ledger row,
     partial run-log row
 11  a new period: edition, live rows and ledger row in one transaction
 12  ledger skip needs the (source, sha256) pair for every period the file
     states
 13  preview and simulate write nothing
 14  a byte-identical re-read is `unchanged` on every path (blanks, flags,
     predecessor sums included); after migrate-legacy the held file is
     skipped through the ledger                           (seeded + real)
 15  a revision, then refresh-latest: only that period and row change,
     loaded_at is copied, live source is not touched beyond the refreshed
     rows
 16  edition 1 equals the decision note's before-state lines  (seeded + real)
 17  2025 households_on_register in live equals the decision note's w1-read
     line (the map's year)                                    (seeded + real)
 18  the migration proof re-run read-only from the files on disk
 19  rerun idempotent
 20  stranded period repair
 21  restore-edition round trip
 22  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content
 23  the named correction: exact counts only, nothing else; --acknowledge
     never releases a 0/NULL change                           (seeded + real)
 24  the two zero rules (Telford and Wrekin, Allerdale) fire on their keys
     and years only; Cumberland is NULL while a part is NULL

The decision note's hash lines are scan-safe: a 64-hex run would be flagged by
the credential scan, so each sha256 is written as two 32-hex halves in two
labelled fields:
    before-state la_housing_register <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read la_housing_register 2025 rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
Both hashes are defined here (live_columns_sha, w1_read_sha); `python
scripts/s13_lahs_editions_verify.py --print-note-lines` prints the lines for
the live table as it stands (read-only), for the decision note.

Usage:
    python scripts/s13_lahs_editions_verify.py
    python scripts/s13_lahs_editions_verify.py --print-note-lines

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
import s13_lahs_editions as m  # noqa: E402
import test_s13_lahs_loader as tl  # noqa: E402
from test_s13_lahs_pure import (CSV_HEADER, PREDECESSORS, YEAR_BASE,  # noqa: E402
                                YEARS, _Cur, _Session, cell, collection,
                                csv_rows, dictionary_rows, ods_rows,
                                open_data_page, write_csv, write_ods,
                                year_page)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real spec and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPEC, which the seeded gates point
# at the copy)
REAL = m.SPEC
AREAS = m.EXPECTED_AREAS
ZZ = tl.ZZ
LEDGER = tl.LEDGER
PERIODS = tl.PERIODS
N = tl.N
P = "2025"
LOADER = HERE / "s13_lahs_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-10-s13-editions-first-load.md")
NOT_YET = "the S13 editions table does not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (22)
NEVER_CODES = ("E08000038", "E08000039")
DORSET = ("E07000049", "E07000050", "E07000051", "E07000052", "E07000053")
CORRECTION = "lahs-correction-2026-10"
# the migration proof on the real files, surveyed 2026-10-10 (Task 4): of the
# 3,256 held rows, 57 come from another predecessor's row of their key, 3 from
# the June 2026 file (set 2026-08-20) and 3 are NULL under TELFORD_NO_REGISTER
SURVEYED_PROOF = {"not_first": 57, "from_june": 3, "telford": 3}
FLAG_REASONS = ("not_available", "not_applicable", "suppressed",
                "part_missing", "parts_disagree", "not_loaded")
MARKER_OF = {"not_available": "[x]", "not_applicable": "[z]",
             "suppressed": "[s]"}
CODE_RE = re.compile(r"E0[6-9][0-9]{6}")


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


@contextmanager
def planted(cur, *sqls, editions=False):
    """Run `sqls` (a planted fault) inside a savepoint that is rolled back
    on exit. editions=True first disables the throwaway editions table's
    user triggers (DDL is transactional, so the rollback restores them);
    only ever used on the zz_s13 tables."""
    cur.execute("SAVEPOINT plant")
    try:
        if editions:
            cur.execute(f"ALTER TABLE public.{ZZ.editions_table} DISABLE "
                        "TRIGGER USER")
        for s in sqls:
            cur.execute(s)
        yield
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT plant")
        cur.execute("RELEASE SAVEPOINT plant")


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
        sys.exit("HARD STOP: a zz_s13 table already exists as a real table; "
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
    the downloads stubbed; every workbook is written here (the loader tests'
    writers)."""

    n8n_live = tl.Migrate.n8n_live
    legacy = tl.Migrate.legacy

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api = {m.COLLECTION_PATH: collection("2023-24", "2024-25")}
        self.urls, self.final, self.fetched = {}, {}, []
        self.k = 0
        self.tx = None

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, run-log
        mock); self.tx counts the commits."""
        borrowed = tl._Borrowed(cur)
        self.tx = borrowed.tx

        def fetch(url, dest, session=None, kind=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)
        out = io.StringIO()
        with tl.specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda p, s=None: self.api[p]), \
                mock.patch.object(m, "fetch", side_effect=fetch), \
                mock.patch.object(m, "log_run") as logged, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
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

    def named(self, csv_name, ods_name, years=YEARS, *, latest="25 June 2026",
              register=True, ods=None, **kw):
        """A CSV and its newest year's ODS whose NAMES are chosen (not
        derived from the content)."""
        self.k += 1
        d = self.root / f"t{self.k}"
        label = years[-1]
        csv_path = write_csv(d / csv_name, years, **kw)
        ods = dict({"cc2a": label < "2024-25"}, **(ods or {}))
        ods_path = write_ods(d / ods_name, label, cover={"latest": latest},
                             **ods)
        if register:
            cu = f"https://assets.example/media/c{self.k}/{csv_name}"
            ou = f"https://assets.example/media/o{self.k}/{ods_name}"
            self.urls[cu], self.urls[ou] = csv_path, ods_path
            self.api[m.OPEN_DATA_PATH] = open_data_page(label, cu)
            a = int(label[:4])
            self.api[YEAR_BASE.format(a, a + 1)] = year_page(label, ou)
            self.csv_url, self.ods_url = cu, ou
        return csv_path, ods_path


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


FEB = "12 February 2026"


def legacy_world(cur, fn, **kw):
    """A seeded legacy state (as the n8n load left it, from the February
    file); fn(env, feb csv, feb ods)."""
    def body(c):
        with env(c) as e, tl.specs():
            feb, feb_ods = e.files(latest=FEB, register=False, **kw)
            e.n8n_live(c, feb)
            return fn(e, feb, feb_ods)
    return _in_savepoint(cur, body)


def migrate_cmd(e, feb, *extra, files=None):
    """migrate-legacy FILE through main on the throwaway tables, with the
    surveyed state and file pointed at the seeded ones."""
    with mock.patch.object(m, "LEGACY_LIVE", m.live_state(e.cur)), \
            mock.patch.object(m, "LEGACY_FILE", files or e.legacy(feb)), \
            mock.patch.object(m, "LEGACY_JUNE", None):
        return e.cmd(["migrate-legacy", feb, *extra])


def cached_read(kind, path):
    """The held file read once per (path, size, mtime): reading the real
    ODS takes seconds."""
    p = Path(path)
    st = p.stat()
    key = (kind, str(p), st.st_size, st.st_mtime_ns)
    if key not in _CACHE:
        _CACHE[key] = (m.read_open_data(p) if kind == "csv"
                       else m.read_year_ods(p))
    return _CACHE[key]


_CACHE = {}
_SHAS = {}


def file_sha(path):
    p = Path(path)
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    if key not in _SHAS:
        _SHAS[key] = m.content_sha256(p)
    return _SHAS[key]


def find_by_sha(raw_dir, pattern, sha):
    for p in sorted(Path(raw_dir).glob(pattern)):
        if file_sha(p) == sha:
            return p
    return None


def held_csv(sha=None, raw_dir=None):
    """(path, open data read) of the held June 2026 CSV found by sha256, or
    (None, a problem text)."""
    sha = m.LEGACY_JUNE["sha256"] if sha is None else sha
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    p = find_by_sha(raw, "*.csv", sha)
    if p is None:
        return None, f"no .csv in {raw} with sha256 {sha[:16]}"
    try:
        return p, cached_read("csv", p)
    except ValueError as e:
        return None, f"{p.name}: {_first(e)}"


def held_ods(od, raw_dir=None):
    """[(path, year ODS read)] of the .ods files in the folder whose Cover
    year is the CSV's newest year; (None, a problem text) if none."""
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    out, problems = [], []
    for p in sorted(raw.glob("*.ods")):
        try:
            ods = cached_read("ods", p)
        except ValueError as e:
            problems.append(f"{p.name}: {_first(e)}")
            continue
        if ods["year"] == od["newest"]:
            out.append((p, ods))
    if not out:
        return None, (f"no .ods of {od['newest']} in {raw}"
                      + (f" ({problems[0]})" if problems else ""))
    return out, ""


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "varchar": "character varying",
          "text": "text", "date": "date", "boolean": "boolean"}


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
    for c in (m.KEY, m.PERIOD) + m.VALUES + ("source", "loaded_at"):
        if c not in lcols:
            bad.append(f"{spec.live_table}: {c} missing")
    for c in m.EXTRAS + ("live_source",):
        if c in lcols:
            bad.append(f"{spec.live_table}: has the editions-only column {c}")
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
            "are refused on the editions and the ledger; no editions-only "
            "column on live)")
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
        "edition equals live on lad24cd, the year and the three values, "
        "cell for cell")


def gate_2_edition1_and_latest(cur):
    name = ("every live period has edition 1; the latest edition equals "
            "live cell for cell on the three values")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_edition1_and_latest(e.cur, ZZ)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET households_on_register = "
             "households_on_register + 1 WHERE lad24cd = 'E06000002'",
             "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET jointly_managed_register = "
             "NOT jointly_managed_register WHERE lad24cd = 'E06000002' AND "
             "jointly_managed_register IS NOT NULL AND reporting_year = 2024",
             "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET reasonable_preference = "
             "NULL WHERE lad24cd = 'E06000002'", "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET households_on_register = 0 "
             "WHERE lad24cd = 'E06000063' AND reporting_year = 2024",
             "edition-only"),
            (f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
             "'E06000002'", "edition-only"),
            (f"UPDATE public.{ZZ.live_table} SET reporting_year = 2030 "
             "WHERE reporting_year = 2018", "no edition 1"),
        ):
            with planted(e.cur, sql):
                try:
                    ok, detail = real_edition1_and_latest(e.cur, ZZ)
                except (psycopg2.Error, ValueError, LookupError) as ex:
                    ok, detail = False, str(ex)
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(2, name, ok, "seeded complete state passes; six planted drifts "
          "(a value, a boolean, a NULL, a zero, a missing row, a year without "
          "edition 1) are each caught" if ok
          else f"{out['base']} {out['miss']}", cur,
          real_edition1_and_latest)


# ---------------------------------------------------------------- AST checks

def _docstring_ids(tree):
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None and \
                    node.body and isinstance(node.body[0], ast.Expr):
                doc.add(id(node.body[0].value))
    return doc


ALLOWED_RULE_NAMES = ("TELFORD_NO_REGISTER", "ALLERDALE_ZERO")
RECODE_NAME = re.compile(r"(?i)recode|lookup_gaps|_gaps\b|code_map|"
                         r"successor_map")


def _target_names(node):
    names = []
    for t in getattr(node, "targets", None) or [getattr(node, "target", None)]:
        if isinstance(t, ast.Name):
            names.append(t.id)
    return names


def _is_zero(node):
    return (isinstance(node, ast.Constant) and node.value == 0
            and not isinstance(node.value, bool))


def _source_problems(path, zeros=False):
    """Problems in a module's source (an AST check), each 'line N: ...':
      - a module-level assignment named like a recode table (LOOKUP_GAPS,
        *recode*) or a dict keyed by authority codes (a private recode);
      - an authority code string constant (E06-E09 + 6 digits) outside the
        two named zero-rule constants (TELFORD_NO_REGISTER, ALLERDALE_ZERO);
      - a Barnsley/Sheffield code (E08000016/19/38/39) inside any other
        non-docstring string;
      - a zero rule (a dict with first_year and column) other than the two
        named ones;
      - a function that compares with 0 (==, !=, is) and also holds an
        authority code constant (a literal code compared with 0).
    With zeros=True also a source value coerced to 0 (`x or 0`, `else 0`,
    COALESCE(x, 0), `.get(x, 0)`) on a line without `# not a source value`."""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines()
    doc = _docstring_ids(tree)
    bad = []
    bs = re.compile(r"E080000(16|19|38|39)(?![0-9])")
    flagged = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        for nm in _target_names(node):
            if RECODE_NAME.search(nm):
                bad.append(f"line {node.lineno}: {nm} is a private recode "
                           "table")
                flagged.update(id(x) for x in ast.walk(node))
            val = getattr(node, "value", None)
            if isinstance(val, ast.Dict) and nm not in ALLOWED_RULE_NAMES:
                keys = {k.value for k in val.keys if isinstance(
                    k, ast.Constant) and isinstance(k.value, str)}
                if {"first_year", "column"} <= keys:
                    bad.append(f"line {node.lineno}: {nm} is a zero rule "
                               "other than the two named ones")
                    flagged.update(id(x) for x in ast.walk(node))
    allowed = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                n in ALLOWED_RULE_NAMES for n in _target_names(node)):
            allowed.update(id(x) for x in ast.walk(node))
    for node in ast.walk(tree):
        if id(node) in flagged:
            continue
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(
                        k.value, str) and CODE_RE.fullmatch(k.value):
                    bad.append(f"line {k.lineno}: a dict keyed by the "
                               f"authority code {k.value!r} (a private "
                               "recode table)")
                    break
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc and id(node) not in allowed:
            if CODE_RE.fullmatch(node.value):
                bad.append(f"line {node.lineno}: authority code "
                           f"{node.value!r} outside the two named zero "
                           "rules")
            elif bs.search(node.value):
                bad.append(f"line {node.lineno}: a Barnsley/Sheffield code "
                           f"in {node.value[:40]!r}")
            if zeros and re.search(r"(?i)coalesce\([^)]*,\s*0\s*\)",
                                   node.value):
                bad.append(f"line {node.lineno}: COALESCE(x, 0)")
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        codes = [n for n in ast.walk(fn) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str) and id(n) not in doc
                 and CODE_RE.fullmatch(n.value)]
        cmp0 = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
                and any(isinstance(o, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot))
                        for o in n.ops)
                and any(_is_zero(x) for x in [n.left] + n.comparators)]
        if codes and cmp0:
            bad.append(f"line {cmp0[0].lineno}: {fn.name} compares with 0 "
                       f"and holds the literal code {codes[0].value!r}")
    if zeros:
        for node in ast.walk(tree):
            zero = (isinstance(node, ast.BoolOp)
                    and isinstance(node.op, ast.Or)
                    and any(_is_zero(v) for v in node.values[1:]))
            ifexp = isinstance(node, ast.IfExp) and _is_zero(node.orelse)
            getz = (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and len(node.args) == 2
                    and _is_zero(node.args[1]))
            if (zero or ifexp or getz) and "not a source value" not in \
                    lines[node.lineno - 1]:
                bad.append(f"line {node.lineno}: a value coerced to 0")
    return sorted(dict.fromkeys(bad), key=lambda s: int(
        re.match(r"line ([0-9]+)", s).group(1)))


def _lines_of(problems):
    """The line numbers named by _source_problems' 'line N: ...' texts."""
    return {int(re.match(r"line ([0-9]+)", x).group(1)) for x in problems}


DORSET_NOTES = tuple(DORSET) + ("E06000059", "LOOKUP_GAPS")


def pending_dorset_only(problems):
    """True if every problem is one the controller's ruling expects until the
    loader's LOOKUP_GAPS is removed (Task 6): the constant or the Dorset
    codes in it."""
    return all(any(w in p for w in DORSET_NOTES) for p in problems)


# ---------------------------------------------------------------- gate 3

def dorset_problems(cur):
    """Problems unless la_code_lookup itself has a new_unitary or merger row
    for each of the five abolished Dorset district codes, all to one target
    that is in la_boundaries (the only way the loader may resolve them)."""
    cur.execute("SELECT old_code, new_code, change_type FROM "
                "public.la_code_lookup WHERE old_code = ANY(%s)",
                (list(DORSET),))
    rows = cur.fetchall()
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    bad = []
    for c in DORSET:
        tg = {n for o, n, t in rows if o == c and t in ("new_unitary",
                                                        "merger")}
        if not tg:
            bad.append(f"la_code_lookup has no new_unitary or merger row for "
                       f"{c}")
        elif len(tg) != 1 or not tg <= valid:
            bad.append(f"la_code_lookup {c} -> {sorted(tg)} (one target in "
                       "la_boundaries expected)")
    return bad


class _DorsetCur:
    """Answers dorset_problems' two queries from a list of
    (old_code, new_code, change_type) rows; la_boundaries holds E06000059."""

    def __init__(self, rows):
        self.rows, self.result = rows, []

    def execute(self, sql, args=None):
        if "la_code_lookup" in sql:
            self.result = [r for r in self.rows if r[0] in set(args[0])]
        else:
            self.result = [("E06000059",)]

    def fetchall(self):
        return list(self.result)


def real_codes(cur, spec=REAL, loader=LOADER, form="old", csv_sha=None,
               raw_dir=None, dorset=True):
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
    periods = _live_periods(cur, spec)
    if not periods:
        bad.append(f"{spec.live_table}: {EMPTY}")
    path, od = held_csv(csv_sha, raw_dir)
    resolved = recoded = 0
    if path is None:
        bad.append(od)
    else:
        by = {}
        for r in od["rows"]:
            by.setdefault(r["period"], []).append(r)
        for p in periods:
            if p not in by:
                bad.append(f"{p}: not in {path.name}")
                continue
            rows = by[p]
            rmap, problems = m.resolve_codes(
                cur, {r["code"] for r in rows}, p,
                {r["code"]: r["lad24cd_pub"] for r in rows})
            bad += [f"{path.name} {p}: {x}" for x in problems[:2]]
            cur.execute(f"SELECT DISTINCT lad24cd FROM public."
                        f"{spec.editions_table} WHERE {spec.period_col} = %s "
                        "AND edition = 1", (p,))
            stored = {r[0] for r in cur.fetchall()}
            if set(rmap.values()) != stored:
                off = sorted(set(rmap.values()) ^ stored)
                bad.append(f"{path.name} {p}: its codes resolve to codes "
                           f"that are not edition 1's lad24cd ({off[:4]})")
            resolved += len(rmap)
            recoded += sum(1 for c, r in rmap.items() if c != r)
    if dorset:
        bad += dorset_problems(cur)
    src = _source_problems(loader)
    if src:
        bad.append(f"the loader: {src[:2]}")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("?",))[0]
    if decl != form:
        bad.append(f"source 13 is declared {decl!r}, expected {form!r}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{seen} distinct lad24cd over live and editions all in "
        f"la_boundaries; no E08000038/39 in lad24cd; {path.name}: "
        f"{resolved:,} publisher codes over {len(periods)} years resolve to "
        f"edition 1's lad24cd ({recoded:,} recoded: Barnsley, Sheffield and "
        "the reorganised authorities); Dorset's five district codes resolve "
        "through la_code_lookup rows; source 13 declared "
        f"{form!r}; no private recode dict or constant in the loader")


def gate_3_codes(cur):
    name = ("codes: lad24cd in la_boundaries, no E08000038/39 stored, the "
            "file's codes resolve through geography.resolve and "
            "la_code_lookup (Dorset only through la_code_lookup), no private "
            "recode dict or constant")
    out = {}

    def body(e):
        csv_path, _ = e.files()
        e.ok(e.cur, ["load", "--commit"])
        clean = Path(e.root) / "clean_loader.py"
        clean.write_text('"""A clean module."""\nX = 1\n', encoding="utf-8")
        kw = dict(csv_sha=m.content_sha256(csv_path),
                  raw_dir=csv_path.parent, loader=clean, dorset=False)
        out["base"] = real_codes(e.cur, ZZ, **kw)
        miss = []
        for sql, needle in (
            (f"UPDATE public.{ZZ.live_table} SET lad24cd = 'E08000038' "
             "WHERE lad24cd = 'E08000016'", "Barnsley/Sheffield"),
            (f"UPDATE public.{ZZ.live_table} SET lad24cd = 'E07999999' "
             "WHERE lad24cd = 'E06000001'", "not in la_boundaries"),
        ):
            with planted(e.cur, sql):
                ok, detail = real_codes(e.cur, ZZ, **kw)
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:90]}")
        ok, detail = real_codes(e.cur, ZZ, **dict(kw, form="new"))
        if ok or "declared" not in detail:
            miss.append(f"a wrong declared form passed: {detail[:80]}")
        ok, detail = real_codes(e.cur, ZZ, **dict(kw, csv_sha="0" * 64))
        if ok or "no .csv" not in detail:
            miss.append(f"a missing held file passed: {detail[:80]}")
        # dorset_problems on stand-in cursors (the real la_code_lookup now
        # holds the five rows, so the real table cannot show the failure)
        none = dorset_problems(_DorsetCur([]))
        full = dorset_problems(_DorsetCur(
            [(c, "E06000059", "new_unitary") for c in DORSET]))
        split = dorset_problems(_DorsetCur(
            [(c, "E06000059" if c != DORSET[0] else "E06000058",
              "new_unitary") for c in DORSET]))
        if (len(none) != 5 or "E07000049" not in " ".join(none)
                or full or not split):
            miss.append(f"Dorset rows check: none {none[:1]} full {full} "
                        f"split {split[:1]}")
        out["miss"] = miss
        # a file carrying the old Barnsley code as a publisher code halts
        # against the declared form 'old' and stores nothing
        before = state(e.cur)
        rows = [dict(r, local_authority_code="E08000038")
                if r["local_authority_code"] == "E08000016" else r
                for r in csv_rows()]
        e.files(rows=rows)
        halted, text = e.halts(["load", "--commit", "--recheck", P], before,
                               "E08000038")
        out["new_code"] = halted
    scenario(cur, body)
    # Dorset: with the abolished codes in a throwaway la_code_lookup and no
    # private table (LOOKUP_GAPS emptied, or absent), they resolve; without
    # the rows they are UNEXPLAINED; a disagreeing LAD24CD is a problem
    pub = {c: "E06000059" for c in DORSET}
    rows = _Cur.LOOKUP + [(c, "E06000059", "new_unitary") for c in DORSET]
    with mock.patch.object(m, "LOOKUP_GAPS", {}, create=True):
        rmap, probs = m.resolve_codes(_Cur(lookup=rows), set(DORSET), "2018",
                                      pub)
        out["dorset_via_lookup"] = (not probs
                                    and set(rmap.values()) == {"E06000059"})
        _, probs = m.resolve_codes(_Cur(), set(DORSET), "2018", pub)
        out["dorset_unexplained"] = (len(probs) == 5 and all(
            "UNEXPLAINED" in p for p in probs))
        _, probs = m.resolve_codes(_Cur(lookup=rows), {"E07000049"}, "2018",
                                   {"E07000049": "E06000058"})
        out["dorset_pub_disagrees"] = bool(probs)
    # the AST check: planted constructs are caught, docstrings and the two
    # named rules are not
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text(
            '"""E08000038 and E07000049 in a docstring"""\n'
            'TELFORD_NO_REGISTER = {"code": "E06000020", "first_year": 2022,'
            ' "column": "c"}\n'
            'LOOKUP_GAPS = {"E07000049": ("E06000059", "x")}\n'
            'MY_MAP = {"E07000050": "E06000059"}\n'
            'OTHER_RULE = {"code": "E06000001", "first_year": 1, '
            '"column": "c"}\n'
            'MSG = "E08000016 in a message"\n'
            'def f(r):\n    return r == "E06000001" and r["h"] == 0\n'
            'def g(r):\n    return r["h"] == 0\n', encoding="utf-8")
        ast_bad = _source_problems(p)
    seen = _lines_of(ast_bad)
    joined = " ".join(ast_bad)
    ast_ok = (seen >= {3, 4, 5, 6, 8} and not seen & {1, 2, 7, 9}
              and "LOOKUP_GAPS" in joined and "compares with 0" in joined)
    ok = (out["base"][0] and not out["miss"] and ast_ok
          and out.get("new_code") and out["dorset_via_lookup"]
          and out["dorset_unexplained"] and out["dorset_pub_disagrees"])
    detail = ("Barnsley and Sheffield stay E08000016/19; two planted "
              "breaks, a wrong declared form, a missing file and Dorset "
              "without la_code_lookup rows are each caught; a file carrying "
              "the new Barnsley code halts against the declared 'old' and "
              "stores nothing; the five Dorset codes resolve through a "
              "throwaway la_code_lookup with no private table, are "
              "UNEXPLAINED without its rows and a disagreeing LAD24CD is a "
              "problem; the AST check catches a recode table, a code "
              "outside the two named rules and a literal code compared "
              "with 0, and ignores a docstring and the named rules" if ok
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
        f"{len(periods)} period(s) ({periods[0]} to {periods[-1]}): {n} "
        "authorities, one row each, in live and in the tip edition")


def gate_4_row_counts(cur):
    name = "296 authorities in every period (live and its tip edition)"
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_row_counts(e.cur, ZZ, n=N)
        out["wrong_n"] = real_row_counts(e.cur, ZZ, n=N + 1)
        e.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
                      "'E08000016' AND reporting_year = 2018")
        out["short"] = real_row_counts(e.cur, ZZ, n=N)
    scenario(cur, body)
    ok = out["base"][0] and not out["wrong_n"][0] and not out["short"][0]
    mixed(4, name, ok, ("the expected count passes in every year; one more "
                        "authority, or a missing authority in an earlier "
                        "year of live, fails" if ok else f"{out}"), cur,
          real_row_counts)


# ---------------------------------------------------------------- gate 5

def parse_flag(text):
    """[(column, reason)] of a value_flag; ValueError on a malformed one."""
    out = []
    for part in (text or "").split("; "):
        if not part:
            continue
        col, sep, reason = part.partition("=")
        if not sep or not reason:
            raise ValueError(f"malformed value_flag item {part!r}")
        out.append((col, reason))
    return out


def real_flags(cur, spec=REAL):
    """In the tip edition of every period: a NULL value has a reason in
    value_flag for that column, a value (a published 0 included) has none;
    every reason is one the loader writes and used where it can be
    (part_missing and parts_disagree only on a record built from several
    predecessors; not_loaded only for reasonable_preference). Returns the
    counts per column of [x] / [z] / [s] and of published zeros."""
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, EMPTY
    bad = []
    counts = {c: {r: 0 for r in FLAG_REASONS} for c in m.VALUES}
    zeros = {c: 0 for c in m.VALUES}
    rows = 0
    for p in periods:
        tip = _tip(cur, spec, p)
        cur.execute(f"SELECT lad24cd, {', '.join(m.VALUES)}, value_flag, "
                    f"predecessor_codes FROM public.{spec.editions_table} "
                    f"WHERE {spec.period_col} = %s AND edition = %s",
                    (p, tip))
        for row in cur.fetchall():
            rows += 1
            k, vals, flag, preds = row[0], row[1:4], row[4], row[5] or ""
            try:
                items = parse_flag(flag)
            except ValueError as e:
                bad.append(f"{p} {k}: {e}")
                continue
            flagged = {}
            for col, reason in items:
                if col not in m.VALUES or reason not in FLAG_REASONS \
                        or col in flagged:
                    bad.append(f"{p} {k}: value_flag item {col}={reason} is "
                               "not one the loader writes")
                    continue
                flagged[col] = reason
                counts[col][reason] += 1
                if reason in ("part_missing", "parts_disagree") \
                        and ";" not in preds:
                    bad.append(f"{p} {k}: {col}={reason} on a record of one "
                               "source code")
                if reason == "parts_disagree" and col != m.JOINTLY:
                    bad.append(f"{p} {k}: parts_disagree on {col}")
                if reason == "part_missing" and col == m.JOINTLY:
                    bad.append(f"{p} {k}: part_missing on {col}")
                if reason == "not_loaded" and col != m.REASONABLE:
                    bad.append(f"{p} {k}: not_loaded on {col}")
            for col, v in zip(m.VALUES, vals):
                if v is None and col not in flagged:
                    bad.append(f"{p} {k}: {col} is NULL without a reason in "
                               "value_flag")
                elif v is not None and col in flagged:
                    bad.append(f"{p} {k}: {col} is {v!r} where value_flag "
                               f"says {flagged[col]} (a 0 where the flag "
                               "says NULL)")
                if v is not None and not isinstance(v, bool) and v == 0:
                    zeros[col] += 1
    cols = ", ".join(
        f"{c}: " + " ".join(f"{MARKER_OF[r]} {counts[c][r]}"
                            for r in MARKER_OF)
        + (f", part_missing {counts[c]['part_missing']}"
           if c != m.JOINTLY else f", parts_disagree "
           f"{counts[c]['parts_disagree']}")
        + f", not_loaded {counts[c]['not_loaded']}, published 0: {zeros[c]}"
        for c in m.VALUES)
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{rows:,} tip rows over {len(periods)} years: every NULL has its "
        f"reason, no flagged value is set; per column ([x]/[z]/[s] = "
        f"not_available/not_applicable/suppressed): {cols}")


def zero_rule_state():
    """Problems unless ZERO_RULES is exactly the two named constants."""
    names = [r["name"] for r in m.ZERO_RULES]
    bad = []
    if names != list(ALLOWED_RULE_NAMES):
        bad.append(f"ZERO_RULES is {names}")
    for r in m.ZERO_RULES:
        if r["column"] != m.HOUSEHOLDS or not r.get("evidence"):
            bad.append(f"{r['name']}: column or evidence wrong")
    return bad


def gate_5_blanks_and_zeros(cur):
    name = ("rule 1: no 0 where the latest edition's value_flag says NULL, no "
            "NULL without a flag, [x]/[z]/[s] counts per column reported; "
            "the only zero-to-NULL rules are the two named constants (AST)")
    problems = []
    out = {}

    def body(e):
        over = {("E06000002", "2017-18", "cc1a"): "[x]",
                ("E06000002", "2023-24", "cc2a"): "[x]",
                ("E06000001", "2023-24", "cc5a"): "[z]",
                ("E06000002", "2024-25", "cc5a"): "[s]"}
        e.files(overrides=over, ods={"overrides": {("E06000002", "cc5a"):
                                                   "[s]"}})
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0:
            problems.append(f"a file with [x], [z] and [s] cells did not "
                            f"load (rc {rc}): {text[-160:]}")
            out["base"] = (False, "not loaded")
            return
        out["base"] = real_flags(e.cur, ZZ)
        det = out["base"][1]
        for want in ("households_on_register: [x] 2 [z] 1 [s] 0",
                     "jointly_managed_register: [x] 1 [z] 5 [s] 0",
                     "reasonable_preference: [x] 0 [z] 1 [s] 1"):
            if want not in det:
                problems.append(f"counts: {want!r} not in {det[:260]}")
        flag = tl.ed_val
        if flag(e.cur, "E06000002", "value_flag", "2018") != \
                "households_on_register=not_available":
            problems.append("[x] in cc1a did not give NULL with "
                            "not_available")
        if tl.live_val(e.cur, "E06000002", period="2018") is not None:
            problems.append("[x] was stored as a value in live")
        if flag(e.cur, "E06000001", "value_flag", "2024") != \
                "reasonable_preference=not_applicable":
            problems.append("[z] in cc5a did not give not_applicable")
        if flag(e.cur, "E06000002", "value_flag", "2025") != (
                "jointly_managed_register=not_applicable; "
                "reasonable_preference=suppressed") or tl.live_val(
                    e.cur, "E06000002", "reasonable_preference") is not None:
            problems.append("[s] in cc5a did not give NULL suppressed")
        if tl.live_val(e.cur, "E06000001", period="2024") != 0 or flag(
                e.cur, "E06000001", "value_flag", "2024") != \
                "reasonable_preference=not_applicable":
            problems.append("a published 0 did not stay 0")
        if flag(e.cur, "E06000002", "value_flag", "2024") != \
                "jointly_managed_register=not_available":
            problems.append("[x] in cc2a did not give not_available")
        if tl.live_val(e.cur, "E06000020") is not None or flag(
                e.cur, "E06000020", "value_flag", "2025") != \
                "households_on_register=not_applicable; " \
                "jointly_managed_register=not_applicable":
            problems.append("Telford's 2025 zero is not NULL under the rule")
        miss = []
        for label, sql, needle in (
            ("a 0 set where the flag says NULL",
             f"UPDATE public.{ZZ.editions_table} SET households_on_register "
             "= 0 WHERE lad24cd = 'E06000002' AND reporting_year = 2018",
             "a 0 where the flag says NULL"),
            ("a NULL without a flag",
             f"UPDATE public.{ZZ.editions_table} SET value_flag = NULL "
             "WHERE lad24cd = 'E06000002' AND reporting_year = 2018",
             "NULL without a reason"),
            ("a flag the loader does not write",
             f"UPDATE public.{ZZ.editions_table} SET value_flag = "
             "'households_on_register=blank' WHERE lad24cd = 'E06000002' "
             "AND reporting_year = 2018", "not one the loader writes"),
            ("part_missing on a single-source record",
             f"UPDATE public.{ZZ.editions_table} SET value_flag = "
             "'households_on_register=part_missing' WHERE lad24cd = "
             "'E06000002' AND reporting_year = 2018",
             "one source code"),
            ("a malformed flag",
             f"UPDATE public.{ZZ.editions_table} SET value_flag = 'oops' "
             "WHERE lad24cd = 'E06000002' AND reporting_year = 2018",
             "malformed"),
        ):
            with planted(e.cur, sql, editions=True):
                ok, detail = real_flags(e.cur, ZZ)
            if ok or needle not in detail:
                miss.append(f"{label}: {detail[:90]}")
        out["miss"] = miss
    scenario(cur, body)
    problems += out.get("miss", [])
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text(
            'RULE = {"code": "E06000001", "first_year": 2020, "column": "c"}'
            '\n'
            'def f(r):\n    return r["code"] == "E06000001" and r["h"] == 0\n'
            'def g(v):\n    return v or 0\n'
            'def h(v):\n    return v or 0  # not a source value\n'
            'def i(d):\n    return d.get("x", 0)\n'
            'def j(v):\n    return v if v else 0\n'
            'Q = "SELECT COALESCE(x, 0) FROM t"\n', encoding="utf-8")
        planted_bad = _lines_of(_source_problems(p, zeros=True))
        plain = _lines_of(_source_problems(p))
    if planted_bad != {1, 3, 5, 9, 11, 12} or plain != {1, 3}:
        problems.append(f"the AST check: {planted_bad} / {plain}")
    src = _source_problems(LOADER, zeros=True)
    src = [s for s in src if not any(w in s for w in DORSET_NOTES)]
    if src:
        problems.append(f"the loader: {src[:2]}")
    problems += zero_rule_state()
    seeded = not problems
    sd = ("seeded: [x], [z] and [s] in cc1a, cc2a and cc5a each give NULL "
          "with their own reason, a published 0 stays 0, Telford's zero is "
          "NULL under its rule; a 0 where a flag says NULL, a NULL without "
          "a flag, an unknown reason, part_missing on one source code and a "
          "malformed flag are each caught; the loader has no literal code "
          "compared with 0, no value coerced to 0 and exactly the two "
          "named zero rules" if seeded else "; ".join(problems[:3]))
    mixed(5, name, seeded, sd, cur, real_flags)


# ---------------------------------------------------------------- gate 6

def real_successors(cur, spec=REAL):
    """In the tip edition of every period, a record built from several
    predecessors (';' in predecessor_codes): households_on_register and
    reasonable_preference are NULL exactly when value_flag says
    part_missing for that column, and jointly_managed_register NULL exactly
    when it says parts_disagree or carries the parts' common marker."""
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, EMPTY
    bad, multi, nulls = [], 0, 0
    for p in periods:
        tip = _tip(cur, spec, p)
        cur.execute(f"SELECT lad24cd, {', '.join(m.VALUES)}, value_flag, "
                    f"predecessor_codes FROM public.{spec.editions_table} "
                    f"WHERE {spec.period_col} = %s AND edition = %s "
                    "AND predecessor_codes LIKE '%%;%%'", (p, tip))
        for row in cur.fetchall():
            multi += 1
            k, vals, flag = row[0], dict(zip(m.VALUES, row[1:4])), row[4]
            flags = dict(parse_flag(flag))
            for c in (m.HOUSEHOLDS, m.REASONABLE):
                if (vals[c] is None) != (flags.get(c) == "part_missing"):
                    bad.append(f"{p} {k}: {c} is {vals[c]!r} but value_flag "
                               f"says {flags.get(c)!r} (a total is NULL "
                               "exactly when a part is missing)")
                nulls += vals[c] is None
            if flags.get(m.JOINTLY) == "parts_disagree" and \
                    vals[m.JOINTLY] is not None:
                bad.append(f"{p} {k}: parts_disagree with a value")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{multi} multi-predecessor record(s) over {len(periods)} years "
        f"({nulls} NULL total(s)): each total is NULL exactly when a "
        "part is missing")


def gate_6_successor_sums(cur):
    name = ("a successor total is NULL unless every predecessor is present "
            "(part_missing); cc2a only when every predecessor agrees")
    problems = []
    y3 = ("2017-18", "2018-19", "2024-25")

    def body(e):
        def pre(label, col):
            return [int(cell(c, label, col).replace(",", ""))
                    for c in PREDECESSORS]

        def run(over=None):
            """Load a fresh world with these cell overrides; return the
            2019 Cumberland edition row."""
            def one(c):
                with env(c) as e2, tl.specs():
                    e2.files(years=y3, overrides=over or {})
                    rc, text, _ = e2.cmd(["load", "--commit"])
                    if rc != 0:
                        problems.append(f"the load halted (rc {rc}): "
                                        f"{text[-120:]}")
                        return (None,) * 6
                    e2.cur.execute(
                        f"SELECT households_on_register, "
                        f"jointly_managed_register, reasonable_preference, "
                        f"value_flag, predecessor_codes, "
                        f"(SELECT households_on_register FROM public."
                        f"{ZZ.live_table} WHERE lad24cd = 'E06000063' AND "
                        f"reporting_year = 2019) FROM public."
                        f"{ZZ.editions_table} WHERE lad24cd = 'E06000063' "
                        "AND reporting_year = 2019")
                    return e2.cur.fetchone()
            return _in_savepoint(e.cur, one)
        h, jm, rp, fl, codes, live = run({(c, "2018-19", "cc2a"): "No"
                                          for c in PREDECESSORS})
        if h != sum(pre("2018-19", "cc1a")) or live != h:
            problems.append(f"all parts present: {h} is not the sum "
                            f"{sum(pre('2018-19', 'cc1a'))}")
        if rp != sum(pre("2018-19", "cc5a")):
            problems.append("all parts present: cc5a is not the sum")
        if codes != ";".join(PREDECESSORS) or fl is not None:
            problems.append(f"all parts present: codes {codes}, flag {fl}")
        for marker in ("[x]", "[z]", "[s]"):
            h, _, rp, fl, _, live = run({
                ("E07000028", "2018-19", "cc1a"): marker})
            if h is not None or live is not None or \
                    "households_on_register=part_missing" not in (fl or ""):
                problems.append(f"{marker} in one part: total {h!r}, live "
                                f"{live!r}, flag {fl!r} (NULL part_missing "
                                "expected)")
            if rp != sum(pre("2018-19", "cc5a")):
                problems.append(f"{marker} in a cc1a part changed cc5a")
        h, _, rp, fl, _, _ = run({("E07000029", "2018-19", "cc5a"): "[x]"})
        if rp is not None or "reasonable_preference=part_missing" not in (
                fl or "") or h != sum(pre("2018-19", "cc1a")):
            problems.append(f"[x] in a cc5a part: cc5a {rp!r}, flag {fl!r}")
        h, *_ = run({("E07000028", "2018-19", "cc1a"): "0"})
        want = sum(pre("2018-19", "cc1a")) - pre("2018-19", "cc1a")[1]
        if h != want:
            problems.append(f"a published 0 in a part: total {h!r}, "
                            f"expected the sum of the others {want}")
        _, jm, _, fl, _, _ = run({("E07000026", "2018-19", "cc2a"): "Yes",
                                  ("E07000028", "2018-19", "cc2a"): "No",
                                  ("E07000029", "2018-19", "cc2a"): "No"})
        if jm is not None or "jointly_managed_register=parts_disagree" not in (
                fl or ""):
            problems.append(f"parts disagreeing on cc2a: {jm!r}, {fl!r}")
        _, jm, _, fl, _, _ = run({(c, "2018-19", "cc2a"): "Yes"
                                  for c in PREDECESSORS})
        if jm is not True or fl is not None:
            problems.append(f"parts agreeing on cc2a: {jm!r}, {fl!r}")
        # a part missing in the records given to the builder directly
        rows = [{"code": c, "lad24cd_pub": "E06000063", "period": "2019",
                 "line": i + 2, "status": "Submitted", m.HOUSEHOLDS: v,
                 m.JOINTLY: False, m.REASONABLE: 5,
                 "flags": ({} if v is not None else {
                     m.HOUSEHOLDS: "not_available"}), "rule_note": None}
                for i, (c, v) in enumerate(zip(PREDECESSORS,
                                               (100, None, 300)))]
        (r,) = m.successor_records(rows, {c: "E06000063"
                                          for c in PREDECESSORS}, "2019")
        if r[m.HOUSEHOLDS] is not None or r["value_flag"] != \
                "households_on_register=part_missing":
            problems.append(f"successor_records with a missing part: {r}")
        rows[1][m.HOUSEHOLDS] = 0
        rows[1]["flags"] = {}
        (r,) = m.successor_records(rows, {c: "E06000063"
                                          for c in PREDECESSORS}, "2019")
        if r[m.HOUSEHOLDS] != 400:
            problems.append(f"a part of 0 is a value: {r[m.HOUSEHOLDS]}")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: Cumberland's total is the sum of its three predecessors "
          "when all are present (cc1a and cc5a), NULL with part_missing when "
          "any part is [x], [z] or [s] (in live too), a published 0 part "
          "counts as a value, cc2a is taken only when the parts agree "
          "(parts_disagree otherwise)" if seeded else "; ".join(problems[:3]))
    mixed(6, name, seeded, sd, cur, real_successors)


# ---------------------------------------------------------------- gate 7

def real_cross_check(june=None, raw_dir=None):
    """The CSV's newest year against its year ODS, re-run read-only on the
    files on disk (the held June 2026 CSV found by sha256, and every .ods in
    the folder whose Cover year is the CSV's newest): same authorities, cc1a,
    cc5a and cc2a equal (or, with no cc2a column, every CSV cc2a [z])."""
    sha = (m.LEGACY_JUNE or {}).get("sha256") if june is None \
        else june["sha256"]
    path, od = held_csv(sha, raw_dir)
    if path is None:
        return False, od
    pairs, why = held_ods(od, raw_dir)
    if pairs is None:
        return False, why
    for p, ods in pairs:
        try:
            m.cross_check(od, ods)
        except ValueError as e:
            return False, f"{path.name} against {p.name}: {_first(e)}"
    p, ods = pairs[0]
    return True, (f"{path.name} {od['newest']} equals {p.name} for all "
                  f"{len(ods['las'])} authorities on cc1a, cc5a and "
                  + ("cc2a" if ods["has_cc2a"] else "cc2a ([z] throughout: "
                     "the ODS has no cc2a column)"))


def gate_7_cross_check(cur):
    name = ("CSV-ODS cross-check re-run read-only on the files on disk; "
            "seeded mismatches halt")
    problems = []

    def check(label, needle, over_csv=None, ods=None, rows=None, years=YEARS,
              ods_years=None):
        def body(e):
            csv_path, ods_path = e.files(overrides=over_csv or {}, ods=ods,
                                         rows=rows, years=years)
            if ods_years:
                _, ods_path = e.files(years=ods_years, register=False)
                raw = Path(e.root) / "mix"
                raw.mkdir()
                (raw / csv_path.name).write_bytes(csv_path.read_bytes())
                (raw / ods_path.name).write_bytes(ods_path.read_bytes())
                csv_dir = raw
            else:
                csv_dir = csv_path.parent
            ok, detail = real_cross_check({"sha256": m.content_sha256(
                csv_path)}, csv_dir)
            if needle is None:
                if not ok:
                    problems.append(f"{label}: {detail[:100]}")
            elif ok or needle not in detail:
                problems.append(f"{label}: passed or wrong text: "
                                f"{detail[:100]}")
            if needle:
                # the same fault through the command halts and stores nothing
                before = state(e.cur)
                halted, text = e.halts(["load", "--commit"], before)
                if not halted and not ods_years:
                    problems.append(f"{label}: the load did not halt")
        scenario(cur, body)
    check("equal", None)
    check("cc1a differs", "cc1a", ods={"overrides": {("E06000002", "cc1a"):
                                                     5}})
    check("cc5a differs", "cc5a", ods={"overrides": {("E06000001", "cc5a"):
                                                     1}})
    check("marker against a number", "cc1a", over_csv={(
        "E06000002", "2024-25", "cc1a"): "[x]"})
    check("an authority only in the CSV", "only in the CSV", rows=[
        r for r in csv_rows() if not (r["local_authority_code"] ==
                                      "E06000002" and r["Year"] == "2024-25")
        ] + [dict(csv_rows()[0], local_authority_code="E06000099",
                  local_authority="Nowhere", LAD24CD="E06000099",
                  Year="2024-25")])
    check("an ODS of another year", "no .ods of 2024-25",
          ods_years=YEARS[:-1])
    check("no cc2a column and a CSV cc2a that is not [z]", "cc2a",
          over_csv={("E06000002", "2024-25", "cc2a"): "Yes"})

    def cc2a_ok(e):
        e.files(ods={"cc2a": True, "overrides": {("E06000002", "cc2a"):
                                                 "Yes"}})
        before = state(e.cur)
        halted, text = e.halts(["load", "--commit"], before)
        if not halted:
            problems.append("an ODS with a cc2a column that disagrees "
                            "with the CSV's [z] did not halt")
    scenario(cur, cc2a_ok)
    seeded = not problems
    sd = ("seeded: a matching pair passes; cc1a, cc5a, a marker against a "
          "number, an authority only in the CSV, an ODS of another year, a "
          "cc2a that is not [z] where the ODS has no cc2a column, and a cc2a "
          "column that disagrees with [z] each fail the re-run and halt the "
          "load" if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(7, name, False, sd)
    try:
        ok, detail = real_cross_check()
    except (SystemExit, ValueError, OSError) as ex:
        return report(7, name, False, _first(ex))
    report(7, name, ok, f"{sd}; held files: {detail}")


# ---------------------------------------------------------------- gate 8

def real_identity(cur=None, june=None, raw_dir=None, spec=REAL, rank=None):
    """The held ODS's identity read from the file itself: its Cover year is
    the CSV's newest, it defines [x], [z] and [s], its Latest update date is
    the rank, and its Data_Dictionary defines the variables read as the
    loader surveyed (read_year_ods raises otherwise). Once the editions
    table exists, every period's tip names a rank in its source_file."""
    sha = (m.LEGACY_JUNE or {}).get("sha256") if june is None \
        else june["sha256"]
    path, od = held_csv(sha, raw_dir)
    if path is None:
        return False, od
    pairs, why = held_ods(od, raw_dir)
    if pairs is None:
        return False, why
    p, ods = pairs[0]
    bad = []
    if ods["title"] != f"Local Authority Housing Statistics (LAHS) Data: " \
            f"{od['newest']}":
        bad.append(f"{p.name}: Cover title {ods['title']!r}")
    if sorted(ods["markers"]) != ["s", "x", "z"]:
        bad.append(f"{p.name}: markers {sorted(ods['markers'])}")
    if rank is not None and ods["rank"] != rank:
        bad.append(f"{p.name}: Latest update {ods['rank']}, expected {rank}")
    if ods["rank"] != ods["latest_update"] or not isinstance(
            ods["rank"], date):
        bad.append(f"{p.name}: the rank is not the Cover's date")
    tips = ""
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        info = m.tip_info(cur, spec)
        off = [pp for pp, i in info.items() if i["rank"] is None]
        if off:
            bad.append(f"{off[:3]}: the tip's source_file names no "
                       "'latest update' date")
        tips = f"; {len(info)} stored year(s), each tip names its rank"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{p.name}: {ods['title']!r}, latest update {ods['rank']}, markers "
        f"{sorted(ods['markers'])} defined, the Data_Dictionary defines "
        f"cc1a{', cc2a' if ods['has_cc2a'] else ''} and cc5a as surveyed"
        f"{tips}")


def gate_8_identity(cur):
    name = ("identity from the files: a seeded Cover, dictionary-definition, "
            "header, page or --release mismatch halts")
    problems = []

    def body(e):
        d = Path(e.root) / "ident"
        d.mkdir()
        n = [0]

        def raises(label, year="2024-25", **kw):
            n[0] += 1
            try:
                p = write_ods(d / f"{n[0]}.ods", "2024-25", **kw)
                m.read_year_ods(p, year)
                problems.append(f"{label} was read")
            except ValueError:
                pass
        raises("title of another year", cover={"title_year": "2023-24"})
        raises("no symbols line", cover={"symbols": None})
        raises("symbols without [s]", cover={
            "symbols": "[x] = not available, [z] = not applicable"})
        raises("no Latest update", cover={"latest": None})
        raises("a Latest update that is not a date", cover={"latest": "soon"})
        raises("no Next Update", cover={"next_update": None})
        raises("cc1a defined differently",
               dictionary=dictionary_rows(defs={"cc1a": "Something else"}))
        raises("cc5a defined differently",
               dictionary=dictionary_rows(defs={"cc5a": "Something else"}))
        raises("cc2a column defined differently", cc2a=True,
               dictionary=dictionary_rows(cc2a=True,
                                          defs={"cc2a": "Something else"}))
        raises("cc2a column without a definition", cc2a=True,
               dictionary=dictionary_rows(cc2a=False))
        raises("no cc5a definition", dictionary=[
            r for r in dictionary_rows() if r[0] != "cc5a"])
        la = ods_rows("2024-25")
        col = la[2].index("cc5a")
        raises("a header without cc5a", la=[r[:col] + r[col + 1:]
                                            for r in la])
        raises("an Imputations header that changed",
               imputations_header=("A", "B", "C", "D"))
        raises("a year of another label", year="2023-24")
        la = ods_rows("2024-25")
        la.append(list(la[3]))
        raises("a repeated authority", la=la)
        # the CSV
        n[0] += 1
        for label, kw in (
                ("a CSV header without cc5a", dict(header=[
                    h for h in CSV_HEADER if h != "cc5a"])),
                ("a Year that is not yyyy-yy", dict(rows=[dict(
                    csv_rows()[0], Year="2024/25")])),
                ("a code that is not an authority code", dict(rows=[dict(
                    csv_rows()[0], local_authority_code="E99000001")]))):
            try:
                m.read_open_data(write_csv(d / f"c{n[0]}.csv", **kw))
                problems.append(f"{label} was read")
            except ValueError:
                pass
            n[0] += 1
        ok = m.read_year_ods(write_ods(d / "ok.ods", "2024-25"), "2024-25")
        if ok["year"] != "2024-25" or ok["rank"] != date(2026, 6, 25):
            problems.append("a good ODS was not read")
        # through the command
        e.files()
        before = state(e.cur)
        for label, mutate, argv, needle in (
            ("a year page of another year",
             lambda: e.api[YEAR_BASE.format(2024, 2025)].update(
                 title="Local Authority Housing Statistics data returns "
                       "for 2023 to 2024"), ["load", "--commit"], None),
            ("--release of another year", lambda: None,
             ["load", "--release", "2023-24", "--commit"], "--release"),
        ):
            e.files()
            mutate()
            ok_, text = e.halts(argv, before, needle)
            if not ok_:
                problems.append(f"{label} did not halt: {text[-100:]}")
        csv_path, _ = e.files(register=False)
        _, old_ods = e.files(years=YEARS[:-1], register=False)
        ok_, text = e.halts(["load", "--file", csv_path, "--ods", old_ods,
                             "--no-page", "--commit"], before, "2023-24")
        if not ok_:
            problems.append(f"an ODS of another year did not halt: "
                            f"{text[-100:]}")
        e.files()
        e.api[m.OPEN_DATA_PATH]["title"] = "LAHS open data"
        ok_, text = e.halts(["load", "--commit"], before, "LAHS open data")
        if not ok_:
            problems.append("a renamed open data page did not halt")
        e.files()
        e.api[m.OPEN_DATA_PATH]["details"]["attachments"] = []
        ok_, text = e.halts(["load", "--commit"], before, "0 attachments")
        if not ok_:
            problems.append("an open data page with no attachment did not "
                            "halt")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a Cover title of another year, no or short symbols line, "
          "no or non-date Latest update, no Next Update, a changed "
          "definition of cc1a, cc2a or cc5a, a missing cc2a definition, a "
          "header without cc5a, a changed Imputations header, a CSV header "
          "without cc5a, a bad Year or code, a year page or --release of "
          "another year, an ODS of another year and a renamed or empty open "
          "data page each halt and store nothing" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(8, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(8, name, False, _first(ex))
    report(8, name, ok, f"{sd}; held files: {detail}")


# ---------------------------------------------------------------- gate 9

def gate_9_older_file(cur):
    name = ("older-file guard on the page, --release, --file and back-fill "
            "paths; the rank is the ODS Cover's own, never a file name")
    problems = []
    # the content is a February 2026 file; the NAMES carry a later date
    cname = "LAHS_open_data_1978-79_to_2024-25_2026-09-30.csv"
    oname = "LAHS_accessible_2024-25_tables_2026-09-30.ods"

    def older(e, how):
        e.seed(e.cur)
        csv_path, ods_path = e.named(
            cname, oname, latest=FEB, register=how != "file",
            overrides={("E06000002", "2024-25", "cc1a"): "99"},
            ods={"overrides": {("E06000002", "cc1a"): 99}})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2024-25"]
        if how == "file":
            argv += ["--file", csv_path, "--ods", ods_path]
        if how == "nopage":
            argv += ["--file", csv_path, "--ods", ods_path, "--no-page"]
        return argv

    def body(e):
        for how in ("page", "release", "file", "nopage"):
            def one(c, how=how):
                with env(c) as e2, tl.specs():
                    argv = older(e2, how)
                    before = state(c)
                    rc, text, logged = e2.cmd(argv)
                    if not (rc == "halt" and "2025 (older" in text
                            and "older file" in text
                            and state(c) == before and not logged.called):
                        problems.append(f"{how}: rc {rc}: {text[-160:]}")
            _in_savepoint(e.cur, one)

        def backfill(c):
            with env(c) as e2, tl.specs():
                e2.seed(c, years=YEARS[1:])
                before = state(c)
                csv_path, ods_path = e2.files(register=False)
                rc, text, logged = e2.cmd(["load", "--file", csv_path,
                                           "--ods", ods_path, "--no-page",
                                           "--commit"])
                if not (rc == 0 and "2018 (older" in text
                        and "back-filling" in text
                        and tl.editions(c, "2018") == []
                        and tl.count(c, ZZ.live_table,
                                     "WHERE reporting_year = 2018") == 0
                        and tl.editions(c, "2024") == [(1, None, N)]):
                    problems.append(f"back-fill of an earlier year: rc "
                                    f"{rc}: {text[-160:]}")
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
                    problems.append(f"--allow-older-file: rc {rc}, {eds}: "
                                    f"{text[-120:]}")
        _in_savepoint(e.cur, override)

        def equal_rank(c):
            with env(c) as e2, tl.specs():
                e2.seed(c)
                e2.files(overrides={("E06000002", "2023-24", "cc5a"): "1"})
                before = rejected_state(c, "2024")
                rc, text, logged = e2.cmd(["load", "--commit"])
                if not (rc == 1 and "two files claim the same release"
                        in text and "--accept-reissue 2024" in text
                        and rejected_state(c, "2024") == before):
                    problems.append(f"equal rank, other content: rc {rc}: "
                                    f"{text[-120:]}")
                rc, text, logged = e2.cmd(["load", "--commit",
                                           "--accept-reissue", "2024"])
                if not (rc == 0 and "ACCEPTED REISSUE" in text
                        and tl.editions(c, "2024") == [(1, None, N),
                                                       (2, 1, N)]
                        and "--accept-reissue" in logged.call_args[0][2]):
                    problems.append(f"--accept-reissue: rc {rc}")
        _in_savepoint(e.cur, equal_rank)
    scenario(cur, body)
    report(9, name, not problems, "a February 2026 file named like a "
           "later one, offered by the page, --release, --file and --file "
           "--no-page against a held file of 2026-06-25, is skipped and "
           "halts with nothing stored or logged; an earlier year offered "
           "after later ones is not back-filled; --allow-older-file "
           "overrides and is logged; equal rank with other content stops "
           "unless --accept-reissue" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 10

def rejected_state(cur, period):
    """The editions and live tables, and the number of ledger rows of the
    period (a rejected year stores nothing and writes no ledger row; a
    restated earlier year may still get its 'unchanged' row)."""
    tables = tables_state(cur)
    cur.execute(f"SELECT COUNT(*) FROM public.{LEDGER} WHERE "
                "reporting_year = %s", (period,))
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


def _rec(key, h, rp=1, j=False, preds=None):
    return {m.KEY: key, m.PERIOD: "2025", m.HOUSEHOLDS: h, m.JOINTLY: j,
            m.REASONABLE: rp, "value_flag": None, "return_status": "S",
            "imputed_cc1a": None, "predecessor_codes": preds or key}


def gate_10_stop_conditions(cur):
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

    def rev(e, over, ods=None):
        e.seed(e.cur, latest=FEB)
        e.files(overrides=over, ods=ods)

    case("revision: a value to NULL needs --acknowledge",
         lambda e: rev(e, {("E06000002", "2023-24", "cc5a"): "[x]"}),
         "--acknowledge 2024", period="2024")
    case("revision: England households move more than 2%",
         lambda e: rev(e, {("E06000002", "2023-24", "cc1a"): "90,000"}),
         "England households_on_register", period="2024")
    case("revision: a 0 to NULL change needs a named correction",
         lambda e: rev(e, {("E06000001", "2023-24", "cc1a"): "[x]"}),
         "--acknowledge-correction", period="2024")
    case("revision: a 0 to NULL change is not released by --acknowledge",
         lambda e: rev(e, {("E06000001", "2023-24", "cc1a"): "[x]"}),
         "0 to NULL or NULL to 0", period="2024",
         extra=("--acknowledge", "2024"))
    case("revision: a NULL to 0 change is not released by --acknowledge",
         lambda e: (e.seed(e.cur, latest=FEB, overrides={(
             "E06000001", "2023-24", "cc1a"): "[x]"}),
             e.files()), "0 to NULL or NULL to 0", period="2024",
         extra=("--acknowledge", "2024"))
    case("revision: the preview of the same writes no run-log row",
         lambda e: rev(e, {("E06000002", "2023-24", "cc5a"): "[x]"}),
         "--acknowledge 2024", commit=False, period="2024")
    case("new year: England households move more than 15%",
         lambda e: (e.seed(e.cur, years=YEARS[:-1], latest=FEB),
                    e.files(overrides={(c, "2024-25", "cc1a"): "90,000"
                                       for c in tl.LA if c in KEYS_ALL},
                            ods={"overrides": {(c, "cc1a"): 90000
                                               for c in tl.LA
                                               if c in KEYS_ALL}})),
         "England households_on_register")
    case("two files with the held file's rank and other content",
         lambda e: (e.seed(e.cur), e.files(overrides={(
             "E06000002", "2023-24", "cc5a"): "1"})),
         "two files claim the same release", period="2024")

    def partial(c):
        with env(c) as e, tl.specs():
            e.seed(c)
            tip = m.records(c, ZZ, "2024", 1)
            part = tip[:-1]
            out = m.period_problems(part, tip, None, kind="revised")
            if not any(x.startswith(m.PARTIAL) and "missing from the file"
                       in x for x in out):
                problems.append(f"a file missing an authority: {out}")
            out = m.period_problems(part, None, tip, kind="new")
            if not any(x.startswith(m.PARTIAL) and "authorities, expected"
                       in x for x in out):
                problems.append(f"a new year of fewer authorities: {out}")
            rows = [r for r in csv_rows() if not (
                r["local_authority_code"] == "E06000002"
                and r["Year"] == "2023-24")]
            e.files(rows=rows)
            before = rejected_state(c, "2024")
            rc, text, logged = e.cmd(["load", "--commit", "--acknowledge",
                                      "2024", "--acknowledge-correction",
                                      CORRECTION])
            if not (rc == 1 and m.PARTIAL in text
                    and "2024: REJECTED" in text
                    and rejected_state(c, "2024") == before):
                problems.append(f"--acknowledge released a partial file: rc "
                                f"{rc}")
    _in_savepoint(cur, partial)

    def limits(c):
        with tl.specs(), mock.patch.object(m, "EXPECTED_AREAS", 40):
            tip = [_rec(f"E06{i:06d}", 1000) for i in range(40)]
            # revised: England households, exactly 2% is not over
            new = [dict(r) for r in tip]
            new[0][m.HOUSEHOLDS] = 1000 + 800     # +800 of 40,000 = 2%
            if m.period_problems(new, tip, None, kind="revised"):
                problems.append("a 2% England move was stopped")
            new[0][m.HOUSEHOLDS] = 1000 + 801
            if not any("England" in x for x in m.period_problems(
                    new, tip, None, kind="revised")):
                problems.append("a move past 2% was not stopped")
            # authorities changing: 20 pass, 21 stop
            new = [dict(r) for r in tip]
            for r in new[:20]:
                r[m.REASONABLE] = 2
            if any("authorities change" in x for x in m.period_problems(
                    new, tip, None, kind="revised")):
                problems.append("20 authorities changing hit the limit")
            for r in new[:21]:
                r[m.REASONABLE] = 2
            if not any("authorities change" in x for x in m.period_problems(
                    new, tip, None, kind="revised")):
                problems.append("21 authorities changing did not stop")
            # a value to NULL is a soft stop; a 0/NULL change is the
            # correction's business (zero_null_flips), not listed there
            new = [dict(r) for r in tip]
            new[0][m.REASONABLE] = None
            if not any("go to or from NULL" in x for x in m.period_problems(
                    new, tip, None, kind="revised")):
                problems.append("a value to NULL was not stopped")
            z = [dict(r) for r in tip]
            z[0][m.HOUSEHOLDS] = 0
            tz = [dict(r) for r in z]
            tz[0][m.HOUSEHOLDS] = None
            if any("go to or from NULL" in x for x in m.period_problems(
                    tz, z, None, kind="revised")):
                problems.append("a 0/NULL change is a soft stop")
            if not m.zero_null_flips(tz, z) or not m.zero_null_flips(z, tz):
                problems.append("zero_null_flips missed a 0/NULL change")
            # new year: England exactly 15% is not over, +1 is
            prev = [dict(r, **{m.PERIOD: "2024"}) for r in tip]
            far = [dict(r) for r in tip]
            for r in far[:6]:
                r[m.HOUSEHOLDS] = 2000            # 6 x +1,000 = 15% of 40,000
            if any("England" in x for x in m.period_problems(
                    far, None, prev, kind="new")):
                problems.append("a 15% England move was stopped")
            far[6][m.HOUSEHOLDS] = 1001
            if not any("England" in x for x in m.period_problems(
                    far, None, prev, kind="new")):
                problems.append("a move past 15% was not stopped")
            # new year: authorities moving by more than 50%: 30 pass, 31 stop
            # (35 small authorities, 5 large so the England total is steady)
            base = [_rec(f"E06{i:06d}", 1000 if i < 35 else 100000)
                    for i in range(40)]
            prev = [dict(r, **{m.PERIOD: "2024"}) for r in base]
            mv = [dict(r) for r in base]
            for r in mv[:30]:
                r[m.HOUSEHOLDS] = 1500            # exactly +50%
            if m.period_problems(mv, None, prev, kind="new"):
                problems.append("authorities moving exactly 50% were "
                                f"stopped: {m.period_problems(mv, None, prev, kind='new')}")
            for r in mv[:30]:
                r[m.HOUSEHOLDS] = 1501            # just over +50%
            if m.period_problems(mv, None, prev, kind="new"):
                problems.append("30 authorities moving past 50% were "
                                "stopped (the limit is more than 30)")
            for r in mv[:31]:
                r[m.HOUSEHOLDS] = 1501
            if not any("more than 50%" in x for x in m.period_problems(
                    mv, None, prev, kind="new")):
                problems.append("31 authorities moving past 50% did not "
                                "stop")
    _in_savepoint(cur, limits)

    def within(c):
        with env(c) as e, tl.specs():
            over = {("E06000001", "2023-24", "cc1a"): "1,000"}
            e.seed(c, years=YEARS[:-1], latest=FEB, overrides=over,
                   ods={"overrides": {("E06000001", "cc1a"): 1000}})
            e.files(overrides=over)
            rc, text, _ = e.cmd(["load", "--commit"])
            if rc != 0 or tl.editions(c, "2025") != [(1, None, N)]:
                problems.append(f"a new year within the limits was refused: "
                                f"rc {rc}: {text[-200:]}")
    _in_savepoint(cur, within)

    def acknowledged(c):
        with env(c) as e, tl.specs():
            e.seed(c, latest=FEB)
            e.files(overrides={("E06000002", "2023-24", "cc5a"): "[x]"})
            rc, text, logged = e.cmd(["load", "--commit", "--acknowledge",
                                      "2024"])
            if not (rc == 0 and "ACKNOWLEDGED" in text
                    and tl.editions(c, "2024") == [(1, None, N), (2, 1, N)]
                    and "ACKNOWLEDGED 2024" in logged.call_args[0][2]):
                problems.append(f"--acknowledge of a NULL change: rc {rc}: "
                                f"{text[-140:]}")
            ok_, _ = e.halts(["load", "--commit", "--recheck", "2024",
                              "--acknowledge", "2019"], state(c))
            if not ok_:
                problems.append("an acknowledgement of a year the run does "
                                "not compare did not halt")
    _in_savepoint(cur, acknowledged)
    report(10, name, not problems, "a revision with a value going to NULL, "
           "England households moving more than 2%, a 0/NULL change (also "
           "with --acknowledge), a new year with England moving more than "
           "15% and two files of one rank are each REJECTED (exit 1) with "
           "nothing stored, no ledger row and one partial run-log row (none "
           "in a preview); a partial file is never released; the 2%, 15%, "
           "20-authority and 30-authority limits are exact; --acknowledge "
           "stores a NULL change and refuses a year not compared" if not
           problems else "; ".join(problems[:3]))


KEYS_ALL = tuple(tl.LA)


# --------------------------------------------------------------- gate 11

def gate_11_one_transaction(cur):
    name = ("a new period: edition, live rows and ledger row in one "
            "transaction")
    problems = []

    def good(e):
        e.files()
        before = state(e.cur)
        rc, text, logged = e.cmd(["load", "--commit"])
        led = tl.ledger(e.cur)
        if rc != 0 or [tl.editions(e.cur, p) for p in PERIODS] != [
                [(1, None, N)]] * 3 \
                or tl.count(e.cur, ZZ.live_table) != 3 * N \
                or [(o, ed) for _, _, o, ed in led] != [("new", 1)] * 3 \
                or state(e.cur) == before:
            problems.append(f"new periods did not store edition 1, {3 * N} "
                            f"live rows and three 'new' ledger rows: rc {rc} "
                            f"{led}")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the load")
        for p in PERIODS:
            if core.rows_differing(e.cur, ZZ, p, 1):
                problems.append(f"live differs from edition 1 in {p}")

    def failing(label, target, attr):
        def body(c):
            with env(c) as e, tl.specs():
                e.files()
                before = state(c)
                with mock.patch.object(target, attr,
                                       side_effect=RuntimeError("boom")):
                    rc, text, logged = e.cmd(["load", "--commit"])
                if not (rc == 1 and state(c) == before
                        and "2018: FAILED" in text and logged.called
                        and "PARTIAL RUN" in logged.call_args[0][2]):
                    problems.append(f"{label}: rc {rc}, state kept="
                                    f"{state(c) == before}")
        _in_savepoint(cur, body)
    scenario(cur, good)
    failing("a failing ledger insert", pe, "record_file_check")
    failing("a failing live insert", m, "insert_live")
    failing("a failing edition insert", core, "insert_edition")
    report(11, name, not problems, "edition 1, the live rows and one 'new' "
           "ledger row per year are stored together; a failing ledger "
           "insert, live insert or edition insert rolls the year back and "
           "logs a partial run" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 12

def gate_12_ledger_skip(cur):
    name = ("ledger skip needs the (source, sha256) pair for every period "
            "the file states")
    problems = []

    def one_period(e):
        e.seed(e.cur, latest=FEB)
        csv_path, _ = e.files(overrides={("E06000001", "2024-25", "cc5a"):
                                         "1"},
                              ods={"overrides": {("E06000001", "cc5a"): 1}})
        src = m.ledger_source(e.csv_url, tl.JUNE, "2024-25", PERIODS)
        # the file's (source, sha) recorded for 2018 only, not for 2024/2025
        e.cur.execute(f"INSERT INTO public.{LEDGER} (reporting_year, "
                      "source_file, file_sha256, outcome, edition) VALUES "
                      "(2018, %s, %s, 'unchanged', 1)",
                      (src, m.content_sha256(csv_path)))
        rc, text, _ = e.cmd(["load"])
        if rc != 0 or "nothing parsed" in text or "2025: revised" not in text:
            problems.append("a pair held for one of three periods skipped "
                            "the file")

    def same_url(e):
        csv0, _ = e.files()
        e.ok(e.cur, ["load", "--commit"])
        url0 = e.csv_url
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "nothing parsed" not in text or logged.called:
            problems.append("a held pair did not skip the read")
        # the same URL with other bytes: the sha differs, so it is read
        csv1, _ = e.files(overrides={("E06000002", "2023-24", "cc5a"): "1"})
        e.urls[url0] = csv1
        e.api[m.OPEN_DATA_PATH] = open_data_page("2024-25", url0)
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" in text or "2024: " not in text:
            problems.append("the same URL with other bytes was skipped")
        # the held pair again: skipped; --recheck reads it anyway
        e.urls[url0] = csv0
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" not in text:
            problems.append("the held pair no longer skips")
        rc, text, _ = e.cmd(["load", "--recheck", P])
        if f"{P}: unchanged" not in text:
            problems.append("--recheck did not read a ledger-held file")
    scenario(cur, one_period)
    scenario(cur, same_url)
    report(12, name, not problems, "a ledger pair held for only one of the "
           "periods the file states does not skip; the held pair skips with "
           "nothing parsed or logged; the same URL with other bytes is "
           "read; --recheck reads a held file" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 13

def gate_13_preview(cur):
    name = "preview and simulate write nothing"
    problems = []
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]

    def body(e):
        e.files(latest=FEB)
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"new periods {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.files(overrides={("E06000002", "2023-24", "cc5a"): "[x]"})
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"],
                     ["load", "--simulate", "--acknowledge", "2024"],
                     ["load", "--acknowledge", "2024"], ["status"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}, state kept="
                                f"{state(e.cur) == before}")
        e.ok(e.cur, ["load", "--commit", "--acknowledge", "2024"])
        before = state(e.cur)
        for argv in (["refresh-latest"], ["restore-edition", "2024", "1"],
                     ["refresh-latest", "--simulate"],
                     ["restore-edition", "2024", "1", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"{argv} after a revision: rc {rc}")
    scenario(cur, body)

    def legacy(e, feb, feb_ods):
        before = state(e.cur)
        for extra in ((), ("--simulate",)):
            rc, text, logged = migrate_cmd(e, feb, *extra)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"migrate-legacy {extra}: rc {rc}: "
                                f"{text[-100:]}")
    legacy_world(cur, legacy)
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    if cur.fetchone()[0] != runs:
        problems.append("the run log changed")
    report(13, name, not problems, "load (also with --acknowledge), status, "
           "refresh-latest, restore-edition and migrate-legacy in preview "
           "and --simulate leave the editions, the live table and the ledger "
           "as they were and log nothing" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 14

def reread_unchanged(e, csv_path, ods_path):
    """Problems (a list) when the same bytes are not `unchanged` on every
    path against the stored editions (the world of gate 14: years 2018, 2019
    and 2025)."""
    bad = []
    rc, text, logged = e.cmd(["load"])
    if rc != 0 or "nothing parsed" not in text or logged.called:
        bad.append(f"the page load: rc {rc}: {text[-100:]}")
    for argv in (["load", "--recheck", "2025"],
                 ["load", "--release", "2024-25", "--recheck", "2019"],
                 ["load", "--file", csv_path, "--recheck", "2018"],
                 ["load", "--file", csv_path, "--ods", ods_path, "--recheck",
                  "2025"],
                 ["load", "--file", csv_path, "--ods", ods_path, "--no-page",
                  "--recheck", "2019", "--commit"],
                 ["load", "--recheck", "2018", "--commit"]):
        rc, text, logged = e.cmd(argv)
        p = argv[argv.index("--recheck") + 1]
        if rc != 0 or f"{p}: unchanged" not in text or "REJECTED" in text                 or "two files claim" in text or ": revised" in text:
            bad.append(f"{' '.join(map(str, argv[1:]))}: rc {rc}: "
                       f"{text[-100:]}")
    return bad


def real_reread(cur, spec=REAL, june=None, raw_dir=None):
    """The held June CSV and its ODS, built now (the loader's own _build),
    equal period by period the edition stored from that file pair (found by
    the ODS's sha256 in its source_file and the CSV's in its label), so a
    re-read is unchanged."""
    _need(cur, spec)
    sha = (m.LEGACY_JUNE or {}).get("sha256") if june is None \
        else june["sha256"]
    path, od = held_csv(sha, raw_dir)
    if path is None:
        return False, od
    pairs, why = held_ods(od, raw_dir)
    if pairs is None:
        return False, why
    p, ods = pairs[0]
    ods_sha, csv_sha = file_sha(p), file_sha(path)
    with contextlib.redirect_stdout(io.StringIO()):
        built, _ = m._build(cur, od, ods)
    bad, equal = [], 0
    for per in sorted(built):
        cur.execute(f"SELECT edition FROM public.{spec.editions_table} WHERE "
                    f"{spec.period_col} = %s AND source_file LIKE %s AND "
                    "release_label LIKE %s GROUP BY edition ORDER BY "
                    "edition DESC", (per, f"%sha256 {ods_sha[:16]}%",
                                     f"%sha256 {csv_sha[:16]}%"))
        eds = [r[0] for r in cur.fetchall()]
        if not eds:
            bad.append(f"{per}: no edition was stored from {path.name} and "
                       f"{p.name} (not yet loaded)")
            continue
        stored = m.records(cur, spec, per, eds[0])
        byk = {r[m.KEY]: r for r in stored}
        recs = []
        for r in built[per]:
            r = dict(r)
            if r["imputed_cc1a"] is None and r[m.KEY] in byk:
                r["imputed_cc1a"] = byk[r[m.KEY]]["imputed_cc1a"]
            recs.append(r)
        if m.rows_content_sha(recs) != m.rows_content_sha(stored):
            diff = [k for k in sorted(byk) if any(
                byk[k][c] != next((x[c] for x in recs if x[m.KEY] == k),
                                  None) for c in m.CONTENT)]
            bad.append(f"{per}: edition {eds[0]} differs from the file read "
                       f"now in {len(diff)} authorit(ies), e.g. {diff[:3]}")
        else:
            equal += 1
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{path.name} with {p.name} read now builds {equal} year(s) whose "
        "content equals the edition stored from them, so a re-read is "
        "unchanged")


def gate_14_reread(cur):
    name = ("a byte-identical re-read is unchanged on every path (blanks, "
            "flags and predecessor sums included); the held migrated file is "
            "skipped through the ledger")
    problems = []
    y3 = ("2017-18", "2018-19", "2024-25")

    def loaded(e):
        over = {("E06000002", "2018-19", "cc5a"): "[s]",
                ("E06000002", "2017-18", "cc1a"): "[x]",
                ("E07000028", "2018-19", "cc1a"): "[x]",
                ("E06000001", "2024-25", "cc5a"): "[z]"}
        csv_path, ods_path = e.files(years=y3, overrides=over, ods={
            "overrides": {("E06000001", "cc5a"): "[z]"}})
        e.ok(e.cur, ["load", "--commit"])
        snap = tables_state(e.cur)
        problems.extend(f"loaded: {x}" for x in reread_unchanged(
            e, csv_path, ods_path))
        for p in ("2018", "2019", "2025"):
            if tl.editions(e.cur, p) != [(1, None, N)]:
                problems.append(f"a re-read stored an edition in {p}")
        if tables_state(e.cur) != snap:
            problems.append("a re-read changed the editions or live")
        if {o for _, _, o, _ in tl.ledger(e.cur)[3:]} - {"unchanged"}:
            problems.append("a re-read ledger row is not 'unchanged'")
        if tl.live_val(e.cur, "E06000002", period="2018") is not None or \
                tl.ed_val(e.cur, "E06000002", "value_flag", "2018") != \
                "households_on_register=not_available":
            problems.append("a published blank did not stay NULL with its "
                            "reason")
        if tl.live_val(e.cur, "E06000001", period="2019") is None:
            problems.append("a value became NULL on re-read")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("refresh-latest plans a write after the "
                            "re-reads")
    scenario(cur, loaded)

    def migrated(e, feb, feb_ods):
        rc, text, _ = migrate_cmd(e, feb, "--commit")
        if rc != 0:
            problems.append(f"migrate-legacy: rc {rc}: {text[-100:]}")
            return
        e.api[YEAR_BASE.format(2024, 2025)] = year_page("2024-25")
        for argv in (["load", "--file", feb, "--ods", feb_ods, "--no-page"],
                     ["load", "--file", feb, "--ods", feb_ods, "--no-page",
                      "--commit"],
                     ["load", "--file", feb, "--ods", feb_ods]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or "already in the ledger" not in text \
                    or "nothing to do" not in text or logged.called:
                problems.append(f"migrated file {' '.join(map(str, argv[3:]))}"
                                f": rc {rc}: {text[-100:]}")
        for p in PERIODS:
            if tl.editions(e.cur, p) != [(1, None, N)]:
                problems.append(f"re-reading the migrated file stored an "
                                f"edition in {p}")
        if {o for _, _, o, _ in tl.ledger(e.cur)} != {"unchanged"}:
            problems.append("a ledger row of the migrated file is not "
                            "'unchanged'")
    legacy_world(cur, migrated)
    seeded = not problems
    sd = ("seeded: after a load of a file with [s], [x] and [z] blanks and "
          "a predecessor sum, the page, --release, --file, --file --ods, "
          "--no-page and --recheck re-reads are unchanged, store no "
          "edition, leave every ledger row 'unchanged' and the blanks NULL "
          "with their reasons; after migrate-legacy the held February file "
          "is skipped through the ledger on every --file path" if seeded
          else "; ".join(problems[:3]))
    mixed(14, name, seeded, sd, cur, real_reread)


# --------------------------------------------------------------- gate 15

def gate_15_revision_and_refresh(cur):
    name = ("a revision, then refresh-latest: only that period and row "
            "change, loaded_at is copied, live source is untouched beyond "
            "the refreshed rows")
    problems = []

    def rows_hash(c, where):
        c.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                  f"FROM public.{ZZ.live_table} t WHERE {where}")
        return c.fetchone()[0]

    def by_key(c, period):
        c.execute(f"SELECT lad24cd, households_on_register, "
                  f"jointly_managed_register, reasonable_preference, source, "
                  f"loaded_at FROM public.{ZZ.live_table} WHERE "
                  "reporting_year = %s", (period,))
        return {r[0]: r for r in c.fetchall()}

    def body(e):
        e.seed(e.cur, latest=FEB)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        other_before = rows_hash(e.cur, "reporting_year <> 2024")
        before = by_key(e.cur, 2024)
        first = tl.live_val(e.cur, "E06000002", "reasonable_preference",
                            "2024")
        e.files(overrides={("E06000002", "2023-24", "cc5a"): str(first + 3)})
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0 or tl.editions(e.cur, "2024") != [(1, None, N),
                                                     (2, 1, N)] or \
                tl.editions(e.cur, "2018") != [(1, None, N)] or \
                tl.editions(e.cur, "2025") != [(1, None, N)]:
            problems.append(f"the revision did not store edition 2 of 2024 "
                            f"only: rc {rc}: {text[-100:]}")
        if by_key(e.cur, 2024) != before:
            problems.append("load changed the live table before "
                            "refresh-latest")
        if pe.status(e.cur, m.profile())["pending_refresh"] != ["2024"]:
            problems.append("status does not name the pending refresh")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called or \
                "2024=1" not in text:
            problems.append("the refresh-latest preview wrote or did not "
                            "plan exactly one row")
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if rc != 0:
            problems.append(f"refresh-latest --commit: rc {rc}: "
                            f"{text[-120:]}")
        after = by_key(e.cur, 2024)
        changed = [k for k in after if after[k] != before.get(k)]
        if changed != ["E06000002"] or after["E06000002"][3] != first + 3:
            problems.append(f"refresh-latest changed {changed[:3]}, "
                            "expected that one row")
        if rows_hash(e.cur, "reporting_year <> 2024") != other_before:
            problems.append("refresh-latest changed another year (values, "
                            "source or loaded_at)")
        unchanged_rows = [k for k in after if k != "E06000002"
                          and after[k][4:] != before[k][4:]]
        if unchanged_rows:
            problems.append(f"source or loaded_at moved on rows that did not "
                            f"change: {unchanged_rows[:3]}")
        e.cur.execute(f"SELECT loaded_at FROM public.{ZZ.editions_table} "
                      "WHERE reporting_year = 2024 AND edition = 2 LIMIT 1")
        ed_loaded = e.cur.fetchone()[0]
        e.cur.execute(f"SELECT lad24cd FROM public.{ZZ.live_table} WHERE "
                      "reporting_year = 2024 AND loaded_at = %s",
                      (ed_loaded,))
        if e.cur.fetchall() != [("E06000002",)]:
            problems.append("loaded_at was not copied to exactly the "
                            "refreshed row")
        if "2026-06-25" not in after["E06000002"][4]:
            problems.append("the refreshed row's source does not carry the "
                            "new edition's label")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("a second refresh-latest plans a write")
        # drift: a live row that equals no edition halts without
        # --accept-drift and nothing is written
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                      "households_on_register = households_on_register + 1 "
                      "WHERE lad24cd = 'E06000002' AND reporting_year = 2018")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if not (rc in ("halt", 1) and state(e.cur) == snap
                and not logged.called):
            problems.append(f"a drifted live year was refreshed: rc {rc}")
    scenario(cur, body)
    report(15, name, not problems, "a revised 2024 waits for refresh-latest "
           "(the preview writes nothing); the commit changes exactly the "
           "revised row, leaves every other row and year (values, source, "
           "loaded_at) alone and copies the edition's loaded_at to that "
           "row only; a second refresh plans nothing; a drifted live year is "
           "not overwritten" if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 16

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def _cellv(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def live_columns_sha(rows):
    """sha256 of a year's live columns: one line per row sorted by lad24cd,
    lad24cd|households_on_register|jointly_managed_register|
    reasonable_preference|source (NULL as '', booleans true/false), LF-joined,
    UTF-8. rows: records with the live source as live_source (m.records
    gives them for the live table and for an edition alike), so the live
    table before the migration and edition 1 after it hash the same."""
    lines = sorted("|".join([r[m.KEY]] + [_cellv(r[c]) for c in m.VALUES]
                            + [_cellv(r.get("live_source"))])
                   for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def w1_read_sha(rows):
    """sha256 of what W1 reads of a year: one line per row sorted by
    lad24cd, lad24cd|households_on_register (NULL as ''), LF-joined."""
    lines = sorted(f"{r[m.KEY]}|{_cellv(r[m.HOUSEHOLDS])}" for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _note_lines(note, label):
    """{year: (rows, sha256)} from lines `<label> la_housing_register
    <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the
    decision note (the two halves joined), or {}. A `before-state-all ...`
    line is a different record and does not match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                rf"(?m)^{re.escape(label)} la_housing_register ([0-9]{{4}}) "
                r"rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text)}


def note_hashes(note):
    return _note_lines(note, "before-state")


def w1_note_hashes(note):
    return _note_lines(note, "w1-read")


def note_lines_for(cur, spec=REAL):
    """The decision note's hash lines for the live table as it stands
    (read-only): a before-state line per year and the w1-read line of the
    newest year. Print them before the migration."""
    out = []
    periods = _live_periods(cur, spec)
    for p in periods:
        recs = m.records(cur, spec, p)
        out.append(f"before-state la_housing_register {p} rows={len(recs)} "
                   f"{hash_fields(live_columns_sha(recs))}")
    if periods:
        recs = m.records(cur, spec, periods[-1])
        out.append(f"w1-read la_housing_register {periods[-1]} "
                   f"rows={len(recs)} {hash_fields(w1_read_sha(recs))}")
    return out


def real_edition1_hash(cur, spec=REAL, note=NOTE, legacy_rows=None,
                       years=None):
    """Edition 1 'as loaded' of every migrated year equals the before-state
    line in the decision note: the row count and live_columns_sha (the three
    values and the live source), and the rows over all years are the
    surveyed live count."""
    _need(cur, spec)
    legacy_rows = m.LEGACY_LIVE[0] if legacy_rows is None else legacy_rows
    years = m.LEGACY_LIVE[3] if years is None else years
    recorded = note_hashes(note)
    bad, parts = [], []
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "release_label IN (%s, %s) ORDER BY 1",
                (core.AS_LOADED_LABEL, core.AS_LOADED_LATEST_LABEL))
    migrated = [str(r[0]) for r in cur.fetchall()]
    if not migrated:
        bad.append("no edition 1 'as loaded' (migrate-legacy not run)")
    elif len(migrated) != years:
        bad.append(f"{len(migrated)} migrated year(s), surveyed {years}")
    total = 0
    for p in migrated:
        recs = m.records(cur, spec, p, 1)
        n, h = len(recs), live_columns_sha(recs)
        total += n
        if Path(note).exists():
            if p not in recorded:
                bad.append(f"the decision note records no 'before-state "
                           f"la_housing_register {p} rows=.. sha256-first32="
                           "..  sha256-last32=..' line")
            elif recorded[p] != (n, h):
                bad.append(f"{p}: edition 1 {n} rows sha256 {h[:16]}.. "
                           f"differs from the note's {recorded[p][0]} rows "
                           f"{recorded[p][1][:16]}..")
        parts.append(f"{p} {n} rows {h[:8]}..")
    if migrated and total != legacy_rows:
        bad.append(f"edition 1 holds {total} rows, surveyed {legacy_rows}")
    for p in recorded:
        if p not in migrated:
            bad.append(f"the note has a line for {p}, which has no edition "
                       "1 as loaded")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"edition 1 of all {len(migrated)} migrated years ({total:,} rows) "
        "hashes to the before-state lines in the decision note ("
        + "; ".join(parts[:3]) + "; ...)")


def gate_16_before_state_hash(cur):
    name = ("edition 1 equals the before-state hashes recorded in the "
            "decision note")
    out = {}

    def body(e, feb, feb_ods):
        pre = {p: m.records(e.cur, ZZ, p) for p in PERIODS}
        before_lines = {p: (len(r), live_columns_sha(r))
                        for p, r in pre.items()}
        rc, text, _ = migrate_cmd(e, feb, "--commit")
        if rc != 0:
            out["error"] = text[-200:]
            return
        ed1 = {p: m.records(e.cur, ZZ, p, 1) for p in PERIODS}
        out["same"] = all((len(ed1[p]), live_columns_sha(ed1[p]))
                          == before_lines[p] for p in PERIODS)
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            lines = [f"before-state la_housing_register {p} rows={n} "
                     f"{hash_fields(h)}" for p, (n, h) in
                     sorted(before_lines.items())]
            kw = dict(legacy_rows=3 * N, years=3)
            note.write_text("intro\n" + "\n".join(lines) + "\n"
                            "before-state-all la_housing_register 2025 "
                            f"rows={N} {hash_fields('1' * 64)}\n",
                            encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            h = before_lines["2025"][1]
            note.write_text(note.read_text().replace(h[:32], "0" * 32),
                            encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            note.write_text("\n".join(lines[:2]) + "\nbefore-state-all "
                            + lines[2][len("before-state "):] + "\n",
                            encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            note.write_text("\n".join(lines).replace(f"rows={N}",
                                                     f"rows={N + 1}"),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                **kw)
            note.write_text("\n".join(lines) + "\n", encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(
                e.cur, ZZ, note, legacy_rows=3 * N + 5, years=3)
            out["wrong_years"] = real_edition1_hash(
                e.cur, ZZ, note, legacy_rows=3 * N, years=4)
            note.write_text("\n".join(lines + [lines[0].replace(
                PERIODS[0], "2019")]) + "\n", encoding="utf-8")
            out["extra_line"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            # a change to the live source shows in the hash
            h2 = live_columns_sha([dict(r, live_source="x")
                                   for r in ed1["2025"]])
            out["source_in_hash"] = h2 != before_lines["2025"][1]
            # no 64-hex run in a line (credential-scan safe)
            out["scan_safe"] = not re.search(r"[0-9a-f]{64}", "\n".join(
                lines))
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                      "households_on_register = households_on_register + 1 "
                      "WHERE lad24cd = 'E06000002' AND reporting_year = 2025")
        out["changed"] = live_columns_sha(m.records(e.cur, ZZ, "2025")) != \
            before_lines["2025"][1]
        with mock.patch.object(m, "LEGACY_LIVE", m.live_state(e.cur)), \
                mock.patch.object(m, "LEGACY_FILE", e.legacy(feb)):
            rc, text, _ = e.cmd(["migrate-legacy", feb, "--commit"])
        out["twice"] = rc == "halt" and "runs once" in text
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(16, name, False, _first(ex))
    ok = (out.get("same") and out.get("ok", (0,))[0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and not out["wrong_years"][0]
          and not out["extra_line"][0] and out.get("source_in_hash")
          and out.get("scan_safe") and out.get("changed")
          and out.get("twice"))
    mixed(16, name, ok,
          "a seeded migration stores an edition 1 whose hash (three "
          "values and live source) equals the live rows' before it and the "
          "note's lines; a wrong hash, a wrong row count, a missing line "
          "(a before-state-all line does not count), an extra line, a wrong "
          "survey and a missing note fail; a later change shows; a second "
          "migration is refused" if ok else f"seeded: {out}", cur,
          real_edition1_hash)


# --------------------------------------------------------------- gate 17

def real_w1_read(cur, spec=REAL, note=NOTE, year=P, rows=None):
    """The live rows of the map's year (2025): the count and w1_read_sha of
    (lad24cd, households_on_register) equal the decision note's w1-read line
    taken before any change, so no value W1 reads moved."""
    _need(cur, spec)
    rows = AREAS if rows is None else rows
    recorded = w1_note_hashes(note).get(year)
    if not Path(note).exists():
        return False, f"decision note {Path(note).name} not written"
    if recorded is None:
        return False, (f"the decision note records no 'w1-read "
                       f"la_housing_register {year} rows=.. sha256-first32=.."
                       " sha256-last32=..' line")
    recs = m.records(cur, spec, year)
    n, h = len(recs), w1_read_sha(recs)
    total = sum(r[m.HOUSEHOLDS] for r in recs if r[m.HOUSEHOLDS] is not None)
    bad = []
    if n != rows:
        bad.append(f"live {year} has {n} rows, expected {rows}")
    if recorded != (n, h):
        bad.append(f"live {year} households_on_register: {n} rows sha256 "
                   f"{h[:16]}.. differs from the note's {recorded[0]} rows "
                   f"{recorded[1][:16]}..")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live {year} households_on_register ({n} rows, England {total:,}) "
        f"hashes to the note's w1-read line ({h[:16]}..)")


def gate_17_w1_read(cur):
    name = ("2025 households_on_register in live equals the decision note's "
            "w1-read line (the map's year)")
    out = {}

    def body(e):
        e.seed(e.cur, latest=FEB)
        before = w1_read_sha(m.records(e.cur, ZZ, "2025"))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = (f"w1-read la_housing_register 2025 rows={N} "
                    f"{hash_fields(before)}")
            note.write_text("intro\n" + line + "\n", encoding="utf-8")
            kw = dict(note=note, rows=N)
            out["base"] = real_w1_read(e.cur, ZZ, **kw)
            # a revision of everything but 2025 households (cc5a, sums,
            # a 2018 change): the W1 read does not move
            e.files(overrides={("E06000002", "2023-24", "cc5a"): "9",
                               ("E06000002", "2024-25", "cc5a"): "9"},
                    ods={"overrides": {("E06000002", "cc5a"): 9}})
            e.ok(e.cur, ["load", "--commit"])
            e.ok(e.cur, ["refresh-latest", "--commit"])
            out["after_cc5a"] = real_w1_read(e.cur, ZZ, **kw)
            out["cc5a_moved"] = tl.live_val(
                e.cur, "E06000002", "reasonable_preference") == 9
            # a revised 2025 household figure moves it
            first = tl.live_val(e.cur, "E06000002")
            e.files(latest="2 July 2026",
                    overrides={("E06000002", "2024-25", "cc1a"):
                               str(first + 1)},
                    ods={"overrides": {("E06000002", "cc1a"): first + 1}})
            e.ok(e.cur, ["load", "--commit", "--acknowledge", "2025"])
            e.ok(e.cur, ["refresh-latest", "--commit"])
            out["after_households"] = real_w1_read(e.cur, ZZ, **kw)
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
    ok = (out["base"][0] and out["after_cc5a"][0] and out["cc5a_moved"]
          and not out["after_households"][0]
          and "differs" in out["after_households"][1]
          and not out["no_line"][0] and not out["other_label"][0]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and out["scan_safe"])
    mixed(17, name, ok, "seeded: the read passes on the live rows the note "
          "was taken from, stays equal after a revision and refresh of cc5a "
          "(W1 reads households_on_register only), changes with a revised "
          "household figure, and a missing or other-labelled line, a wrong "
          "row count and a missing note fail" if ok else f"{out}", cur,
          real_w1_read)


# --------------------------------------------------------------- gate 18

def real_migration_proof(cur, spec=REAL, files=None, june=None, repo=None,
                         expect=None):
    """The migration proof re-run read-only: the held February CSV (found at
    files['where'] under the repo, sha256 checked) read with the n8n rules
    (the loader's _n8n_view, on the codes resolved as load resolves them)
    reproduces every value, the source text, predecessor_codes, return_status
    and value_flag of edition 1 'as loaded' of every migrated year, taking
    the June 2026 file for the rows that came from it; 0 differences."""
    _need(cur, spec)
    files = files or m.LEGACY_FILE
    june = june if june is not None else m.LEGACY_JUNE
    base = Path(repo) if repo is not None else m.REPO
    path = Path(files["where"])
    path = path if path.is_absolute() else base / path
    if not path.is_file() or file_sha(path) != files["sha256"]:
        return False, (f"no file {files['where']} with sha256 "
                       f"{files['sha256'][:16]}")
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "release_label IN (%s, %s) ORDER BY 1",
                (core.AS_LOADED_LABEL, core.AS_LOADED_LATEST_LABEL))
    periods = [str(r[0]) for r in cur.fetchall()]
    if not periods:
        return False, "no edition 1 'as loaded'"
    cand = m._candidates(cur, cached_read("csv", path))
    cand_june = {}
    if june is not None:
        jp = Path(june["path"])
        if jp.is_file() and file_sha(jp) == june["sha256"]:
            cand_june = m._candidates(cur, cached_read("csv", jp))
    diffs, first, not_first, from_june, telford, rows = [], 0, 0, 0, 0, 0
    for p in periods:
        held = m.records(cur, spec, p, 1)
        keys_file = {k for k, q in cand if q == p}
        keys_held = {r[m.KEY] for r in held}
        diffs += [f"{k} {p}: only held" for k in sorted(
            keys_held - keys_file)]
        diffs += [f"{k} {p}: only in the file" for k in sorted(
            keys_file - keys_held)]
        cur.execute(f"SELECT DISTINCT source_file FROM public."
                    f"{spec.editions_table} WHERE {spec.period_col} = %s AND "
                    "edition = 1", (p,))
        (sf,), = cur.fetchall()
        for r in sorted(held, key=lambda x: x[m.KEY]):
            k = r[m.KEY]
            if k not in keys_file:
                continue
            rows += 1
            hit = None
            for src, tag in ((cand, "feb"), (cand_june, "june")):
                for i, s in enumerate(src.get((k, p), [])):
                    h, j, text, rule = m._n8n_view(s, p)
                    if (h, j, text) == (r[m.HOUSEHOLDS], r[m.JOINTLY],
                                        r["live_source"]) and \
                            m._num(h) == m._num(r[m.HOUSEHOLDS]) and \
                            r[m.REASONABLE] is None:
                        hit = (i, s, rule, tag)
                        break
                if hit:
                    break
            if hit is None:
                diffs.append(f"{k} {p}: no source row gives "
                             f"({r[m.HOUSEHOLDS]}, {r[m.JOINTLY]}, "
                             f"{r['live_source']!r})")
                continue
            i, s, rule, tag = hit
            if tag == "june":
                from_june += 1
            elif i == 0:
                first += 1
            else:
                not_first += 1
            telford += bool(rule)
            flags = {c: f for c, f in s["flags"].items() if r[c] is None}
            if rule:
                flags[rule["column"]] = rule["flag"]
            flags[m.REASONABLE] = m.NOT_LOADED
            if r["predecessor_codes"] != s["code"] or \
                    r["return_status"] != s["status"] or \
                    r["value_flag"] != m.value_flag(r, flags) or \
                    r["imputed_cc1a"] is not None:
                diffs.append(f"{k} {p}: predecessor_codes, return_status, "
                             "value_flag or imputed_cc1a differ")
        cur.execute(f"SELECT COUNT(*) FROM public.{ledger_name(spec)} WHERE "
                    f"{spec.period_col} = %s AND outcome = 'unchanged' AND "
                    "edition = 1 AND file_sha256 = %s", (p, files["sha256"]))
        if not cur.fetchone()[0]:
            diffs.append(f"{p}: no ledger row for the held file")
    got = {"not_first": not_first, "from_june": from_june, "telford": telford}
    if expect and got != expect:
        diffs.append(f"proof counts {got}, surveyed {expect}")
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{rows:,} rows of edition 1 over {len(periods)} "
                       f"years equal {path.name} read with the n8n rules "
                       f"(0 differences): {first:,} from the first row of "
                       f"their key, {not_first} from another predecessor's "
                       f"row, {from_june} from the June file, {telford} NULL "
                       "under TELFORD_NO_REGISTER")


def gate_18_migration_proof(cur):
    name = ("the migration proof re-run read-only from the files on disk; "
            "seeded: proof passes, a planted held difference stops "
            "migrate-legacy and stores nothing")
    problems = []

    def ok_world(e, feb, feb_ods):
        files = e.legacy(feb)
        rc, text, _ = migrate_cmd(e, feb, "--commit")
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        ok, detail = real_migration_proof(e.cur, ZZ, files, june=None)
        if not ok or "0 differences" not in detail:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(
            e.cur, ZZ, {**files, "sha256": "0" * 64}, june=None)
        if ok or "no file" not in detail:
            problems.append("a file with another sha256 was accepted")
        # a different, valid file stands in for the held one
        other, _ = e.files(register=False, latest=FEB, overrides={
            ("E06000002", "2023-24", "cc1a"): "7"})
        ok, detail = real_migration_proof(
            e.cur, ZZ, {**files, "where": str(other),
                        "sha256": m.content_sha256(other)}, june=None)
        if ok or "differences" not in detail:
            problems.append("a file that differs from edition 1 passed")
        for label, sql in (
            ("a value", f"UPDATE public.{ZZ.editions_table} SET "
             "households_on_register = households_on_register + 1 WHERE "
             "lad24cd = 'E06000002' AND reporting_year = 2024"),
            ("a code", f"UPDATE public.{ZZ.editions_table} SET "
             "predecessor_codes = 'E06000999' WHERE lad24cd = 'E06000002' "
             "AND reporting_year = 2024"),
            ("a flag", f"UPDATE public.{ZZ.editions_table} SET "
             "value_flag = NULL WHERE reporting_year = 2024 AND "
             "lad24cd = 'E06000002'"),
            ("a source text", f"UPDATE public.{ZZ.editions_table} SET "
             "live_source = 'x' WHERE lad24cd = 'E06000002' AND "
             "reporting_year = 2024"),
        ):
            with planted(e.cur, sql, editions=True):
                ok, detail = real_migration_proof(e.cur, ZZ, files,
                                                  june=None)
            if ok or "differences" not in detail:
                problems.append(f"edition 1 with {label} changed passed the "
                                f"re-run: {detail[:80]}")
        ok, detail = real_migration_proof(
            e.cur, ZZ, files, june=None, expect={"not_first": 99,
                                                 "from_june": 0,
                                                 "telford": 0})
        if ok or "surveyed" not in detail:
            problems.append("the surveyed counts are not enforced")

    def planted_world(sql, needle, surveyed_first=False):
        def f(e, feb, feb_ods):
            surveyed = m.live_state(e.cur) if surveyed_first else None
            e.cur.execute(sql)
            surveyed = surveyed or m.live_state(e.cur)
            before = state(e.cur)
            with mock.patch.object(m, "LEGACY_LIVE", surveyed), \
                    mock.patch.object(m, "LEGACY_FILE", e.legacy(feb)), \
                    mock.patch.object(m, "LEGACY_JUNE", None):
                rc, text, logged = e.cmd(["migrate-legacy", feb, "--commit"])
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    live = ZZ.live_table
    legacy_world(cur, ok_world)
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET households_on_register = "
        "households_on_register + 1 WHERE lad24cd = 'E06000002' AND "
        "reporting_year = 2024", "proof failed"))
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET jointly_managed_register = NOT "
        "jointly_managed_register WHERE lad24cd = 'E06000001' AND "
        "reporting_year = 2018 AND jointly_managed_register IS NOT NULL",
        "proof failed"))
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET reasonable_preference = 5 WHERE "
        "lad24cd = 'E06000002' AND reporting_year = 2024", "proof failed"))
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET lad24cd = 'E06000003' WHERE "
        "lad24cd = 'E06000001' AND reporting_year = 2018", "proof failed"))
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET source = 'x' WHERE lad24cd = "
        "'E06000002' AND reporting_year = 2024", "proof failed"))
    legacy_world(cur, planted_world(
        f"DELETE FROM public.{live} WHERE lad24cd = 'E06000002' AND "
        "reporting_year = 2024", "not as surveyed", surveyed_first=True))
    mixed(18, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the file by sha256; a changed or missing file, a file that is "
          "not edition 1 and a changed value, code, flag or source text in "
          "edition 1 are caught; planted differences in a value, cc2a, "
          "cc5a, a code and the source, and a dropped row, stop "
          "migrate-legacy with nothing stored" if not problems
          else "; ".join(problems[:3]), cur,
          lambda c: real_migration_proof(c, REAL, expect=SURVEYED_PROOF))


# ---------------------------------------------------------- gates 19 - 21

def gate_19_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        csv_path, ods_path = e.files()
        e.ok(e.cur, ["load", "--commit"])
        snap, led = tables_state(e.cur), len(tl.ledger(e.cur))
        for argv in (["load", "--commit"],
                     ["load", "--release", "2024-25", "--commit"],
                     ["load", "--file", csv_path, "--commit"],
                     ["load", "--file", csv_path, "--ods", ods_path,
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
        rc, text, logged = e.cmd(["load", "--recheck", P, "--commit"])
        if rc != 0 or [tl.editions(e.cur, p) for p in PERIODS] != [
                [(1, None, N)]] * 3:
            problems.append("a recheck stored an edition")
    scenario(cur, body)
    report(19, name, not problems, "a second load by the page, --release, "
           "--file and --file --ods --no-page and a refresh-latest stored no "
           "edition and changed no live row (re-reads add 'unchanged' "
           "ledger rows only); a ledger-skipped load logs nothing"
           if not problems else "; ".join(problems[:3]))


def gate_20_stranded(cur):
    name = "stranded period repair (edition held, live rows missing)"
    problems = []

    def body(e):
        e.seed(e.cur)
        e.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE "
                      "reporting_year = 2025")
        if pe.status(e.cur, m.profile())["live_missing"] != ["2025"]:
            problems.append("status does not name the stranded period")
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0 or "live-missing" not in text or \
                tl.editions(e.cur) != [(1, None, N)] or \
                tl.count(e.cur, ZZ.live_table) != 3 * N:
            problems.append(f"repair failed: rc {rc}: {text[-120:]}")
        if "live-missing" not in [o for _, _, o, _ in tl.ledger(e.cur)]:
            problems.append("no live-missing ledger row")
        if core.rows_differing(e.cur, ZZ, "2025", 1) or not pe.status(
                e.cur, m.profile())["ok"]:
            problems.append("the repaired rows do not equal the tip")
        if not tl.live_val(e.cur, "E06000020", "source").endswith("]"):
            problems.append("the repaired Telford row lost its rule note in "
                            "source")
    scenario(cur, body)
    report(20, name, not problems, "a stranded 2025 was rebuilt from its "
           "edition 1 with no new edition, a live-missing ledger row and the "
           "rule note kept in source" if not problems
           else "; ".join(problems[:3]))


def gate_21_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur, latest=FEB)
        first = tl.live_val(e.cur, "E06000002", "reasonable_preference")
        e.files(overrides={("E06000002", "2024-25", "cc5a"): str(first + 3)},
                ods={"overrides": {("E06000002", "cc5a"): first + 3}})
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
        if tl.live_val(e.cur, "E06000002", "reasonable_preference") != \
                first + 3:
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.live_val(e.cur, "E06000002", "reasonable_preference") != \
                first or core.rows_differing(e.cur, ZZ, P, 3):
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
                e.files()
                rc, _, _ = e.cmd(["load", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load"])
                raw = e.root / "raw"
                body_a = write_csv(e.root / "dl" / "a.csv").read_bytes()
                body_b = write_csv(e.root / "dl" / "b.csv", overrides={(
                    "E06000002", "2023-24", "cc5a"): "3"}).read_bytes()
                url = "https://assets.example/media/3/f.csv"
                with contextlib.redirect_stdout(io.StringIO()), \
                        mock.patch.object(m, "MIN_FILE_BYTES", 10):
                    p1, _ = m.fetch(url, raw, _Session(body_a, url))
                    sha1 = m.content_sha256(p1)
                    p2, _ = m.fetch(url, raw, _Session(body_b, url))
                    p3, _ = m.fetch(url, raw, _Session(body_a, url))
                kept = (p1 == raw / "f.csv" and m.content_sha256(p1) == sha1
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
    ok = (not attempts and rc == rc_s == rc_r == rc_l == 0 and kept
          and runs == runs_after and real_before == real_after
          and not literal and not in_src and not in_out)
    report(22, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned "
           f"{rc}/{rc_s}/{rc_r}/{rc_l}; a same-named different download "
           f"was saved beside it, the first untouched={kept}; run-log rows "
           f"{runs}->{runs_after}; real editions table and ledger "
           f"unchanged={real_before == real_after}; secret-like literal in "
           f"source {literal or 'none'}; {len(secrets)} secret-named "
           f"settings checked against {len(files)} files and "
           f"{len(OUTPUT)} output lines: in source {in_src or 'none'}, in "
           f"output {in_out or 'none'}")


# --------------------------------------------------------------- gate 23

def correction_problems_static():
    """Problems unless the real named correction is well formed: decided and
    explained, lists exactly the reporting years 2015 to 2025, only the three
    groups, and names only cc5a_filled for 2025 (the map's year: no
    households_on_register change)."""
    bad = []
    c = m.ACKNOWLEDGED_CORRECTIONS.get(CORRECTION)
    if c is None:
        return [f"{CORRECTION} is not in ACKNOWLEDGED_CORRECTIONS"]
    for k in ("decided", "why", "periods"):
        if not c.get(k):
            bad.append(f"{CORRECTION} has no {k}")
    pers = c.get("periods", {})
    if sorted(pers) != [str(y) for y in range(2015, 2026)]:
        bad.append(f"{CORRECTION} years are {sorted(pers)}")
    for p, groups in pers.items():
        extra = set(groups) - {"cc5a_filled", "predecessor_sum", "allerdale"}
        if extra or not all(isinstance(v, int) and v > 0
                            for v in groups.values()):
            bad.append(f"{CORRECTION} {p}: groups {groups}")
    if set(pers.get("2025", {})) != {"cc5a_filled"}:
        bad.append(f"{CORRECTION} 2025 names {pers.get('2025')}: only "
                   "cc5a_filled may change the map's year")
    return bad


def real_correction(cur, spec=REAL):
    """The correction was applied: every year it names has an edition whose
    release label records ACKNOWLEDGED <name>, and no edition outside its
    years does."""
    _need(cur, spec)
    bad = correction_problems_static()
    names = m.ACKNOWLEDGED_CORRECTIONS.get(CORRECTION, {}).get("periods", {})
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} WHERE release_label LIKE %s",
                (f"%ACKNOWLEDGED {CORRECTION}:%",))
    got = sorted(str(r[0]) for r in cur.fetchall())
    if got != sorted(names):
        bad.append(f"editions labelled ACKNOWLEDGED {CORRECTION} exist for "
                   f"{got}, the correction names {sorted(names)} (not yet "
                   "loaded?)")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{CORRECTION} is well formed and recorded in the label of an "
        f"edition of each of its {len(got)} years")


def gate_23_correction(cur):
    name = ("the named correction releases exactly the changes it counts "
            "and nothing else; --acknowledge never releases a 0/NULL change")
    problems = []
    groups = {"2018": {"cc5a_filled": N, "allerdale": 1},
              "2024": {"cc5a_filled": N}, "2025": {"cc5a_filled": N}}
    fix = {"periods": groups, "decided": "Scott (verify fixture)",
           "why": "verify fixture"}

    def world(c, fn):
        with env(c) as e, tl.specs():
            feb, feb_ods = e.files(latest=FEB, register=False)
            e.n8n_live(c, feb)
            rc, text, _ = migrate_cmd(e, feb, "--commit")
            if rc != 0:
                problems.append(f"migrate-legacy: {text[-100:]}")
                return
            e.api[YEAR_BASE.format(2024, 2025)] = year_page("2024-25")
            fn(e)

    def stage(e, extra=None):
        e.files(**(extra or {}))

    def period_state(c, p):
        """The editions, live rows and ledger rows of one reporting year."""
        out = []
        for t in (ZZ.editions_table, ZZ.live_table, LEDGER):
            c.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{t} t WHERE reporting_year "
                      "= %s", (p,))
            out.append(c.fetchone()[0])
        return tuple(out)

    def reject(label, argv, needle, period, extra=None, fixture=fix,
               whole=False):
        """The run refuses (exit 1 or a halt), names `needle`, and leaves
        `period` (or, whole=True, everything) as it was."""
        def one(e):
            stage(e, extra)
            snap = (lambda c: state(c)) if whole else (
                lambda c: period_state(c, period))
            before = snap(e.cur)
            with mock.patch.dict(m.ACKNOWLEDGED_CORRECTIONS, {"t": fixture}):
                rc, text, logged = e.cmd(argv)
            if rc not in (1, "halt") or needle not in text or                     before != snap(e.cur):
                problems.append(f"{label}: rc {rc}, unchanged="
                                f"{before == snap(e.cur)}: {text[-140:]}")
        _in_savepoint(cur, lambda c: world(c, one))
    commit = ["load", "--commit"]
    reject("no correction named", commit, "--acknowledge-correction", "2018")
    reject("--acknowledge for every year does not release 0/NULL",
           commit + ["--acknowledge", "2018", "--acknowledge", "2024",
                     "--acknowledge", "2025"], "0 to NULL or NULL to 0",
           "2018")
    reject("a correction whose count is one short",
           commit + ["--acknowledge-correction", "t"], "does not cover",
           "2024", fixture=dict(fix, periods=dict(groups, **{
               "2024": {"cc5a_filled": N - 1}})))
    reject("a correction that lists no group the changes need",
           commit + ["--acknowledge-correction", "t"], "does not cover",
           "2018", fixture=dict(fix, periods=dict(groups, **{
               "2018": {"cc5a_filled": N}})))
    reject("a year the correction does not list gets no release",
           commit + ["--acknowledge-correction", "t"], "--acknowledge 2025",
           "2025", fixture=dict(fix, periods={"2018": groups["2018"],
                                              "2024": groups["2024"]}))
    reject("a change outside the named groups blocks the correction",
           commit + ["--acknowledge-correction", "t"], "outside the named",
           "2024", extra={"overrides": {("E06000002", "2023-24", "cc1a"):
                                        "1,111"}})
    reject("a correction that names none of the years compared",
           commit + ["--acknowledge-correction", "t"], "none of which",
           "2018", fixture=dict(fix, periods={"2030": {"cc5a_filled": 1}}),
           whole=True)
    # an unknown correction name is refused by the command line
    def unknown(e):
        stage(e)
        rc, text, _ = e.cmd(commit + ["--acknowledge-correction", "nope"])
        if rc != "halt":
            problems.append("an unknown correction name was accepted")
    _in_savepoint(cur, lambda c: world(c, unknown))

    def stores(e):
        stage(e)
        with mock.patch.dict(m.ACKNOWLEDGED_CORRECTIONS, {"t": fix}):
            rc, text, logged = e.cmd(commit + ["--acknowledge-correction",
                                               "t"])
        if rc != 0 or "ACKNOWLEDGED (--acknowledge-correction)" not in text:
            problems.append(f"the exact correction was not released: rc "
                            f"{rc}: {text[-140:]}")
            return
        for p in PERIODS:
            if tl.editions(e.cur, p) != [(1, None, N), (2, 1, N)]:
                problems.append(f"{p}: edition 2 not stored")
        if "ACKNOWLEDGED t" not in logged.call_args[0][2]:
            problems.append("the run-log notes do not record the correction")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE edition = 2")
        if not all("ACKNOWLEDGED t:" in r[0] for r in e.cur.fetchall()):
            problems.append("the edition label does not record the "
                            "correction")
        if tl.live_val(e.cur, "E06000063", period="2018") != tl.n(
                "E07000026", "2017-18"):
            problems.append("load changed live before refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.live_val(e.cur, "E06000063", period="2018") is not None:
            problems.append("Cumberland 2018 is not NULL after the refresh")
        if tl.live_val(e.cur, "E06000002", "reasonable_preference") is None:
            problems.append("cc5a was not filled")
        if tl.live_val(e.cur, "E06000002") != tl.n("E06000002", "2024-25"):
            problems.append("a 2025 household figure moved")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
        # the same file again is unchanged against edition 2
        rc, text, _ = e.cmd(commit + ["--recheck", "2018"])
        if rc != 0 or "2018: unchanged" not in text:
            problems.append(f"the corrected file read again: rc {rc}")
    _in_savepoint(cur, lambda c: world(c, stores))
    problems += correction_problems_static()
    seeded = not problems
    sd = ("a load of a file changing every year needs the named "
          "correction; --acknowledge does not release a 0/NULL change; a "
          "correction one short, listing no needed group, naming no changed "
          "year or none of the compared years, an unknown name and a change "
          "outside the named groups are each refused with nothing stored; "
          "the exact counts store edition 2 (label and run-log record the "
          "correction), refresh-latest brings live to it, no 2025 household "
          "figure moves and the same file read again is unchanged; the "
          f"real {CORRECTION} lists 2015 to 2025, three groups, and only "
          "cc5a_filled for 2025" if seeded else "; ".join(problems[:3]))
    mixed(23, name, seeded, sd, cur, real_correction)


# --------------------------------------------------------------- gate 24

def gate_24_zero_rules(cur):
    name = ("the two zero rules fire on their key, years and a published 0 "
            "only; Cumberland is NULL while a part is NULL")
    problems = []

    def row(code, year, h, flags=None):
        return {"code": code, "lad24cd_pub": code, "period": str(year),
                "year": f"{year - 1}-{str(year)[2:]}", "line": 2,
                "status": "S", m.HOUSEHOLDS: h, m.JOINTLY: False,
                m.REASONABLE: 5, "flags": flags or {}, "rule_note": None}

    def fires(code, year, h=0):
        out, applied = m.zero_rules([row(code, year, h)])
        return bool(applied), out[0][m.HOUSEHOLDS]
    for code, year, want in (
            ("E06000020", 2021, False), ("E06000020", 2022, True),
            ("E06000020", 2025, True), ("E06000020", 2030, True),
            ("E07000026", 2014, False), ("E07000026", 2015, True),
            ("E07000026", 2018, True), ("E07000026", 2019, False),
            ("E07000028", 2016, False), ("E06000001", 2023, False),
            ("E06000063", 2016, False)):
        got, h = fires(code, year)
        if got != want or (h is None) != want:
            problems.append(f"{code} {year}: fired={got}, value {h!r}, "
                            f"expected fired={want}")
    for code, year in (("E06000020", 2023), ("E07000026", 2016)):
        got, h = fires(code, year, 5)
        if got or h != 5:
            problems.append(f"{code} {year}: a non-zero value was changed")
    rule = m.TELFORD_NO_REGISTER
    if (rule["code"], rule["first_year"], rule["last_year"],
            rule["column"], rule["flag"]) != ("E06000020", 2022, None,
                                              m.HOUSEHOLDS,
                                              "not_applicable"):
        problems.append("TELFORD_NO_REGISTER is not Telford and Wrekin "
                        "from 2022")
    rule = m.ALLERDALE_ZERO
    if (rule["code"], rule["successor"], rule["first_year"],
            rule["last_year"], rule["column"], rule["flag"]) != (
            "E07000026", "E06000063", 2015, 2018, m.HOUSEHOLDS,
            "not_counted"):
        problems.append("ALLERDALE_ZERO is not Allerdale 2015 to 2018")
    if "Scott" not in m.ALLERDALE_ZERO["evidence"] or \
            "2026-09-30-telford-no-housing-register.md" not in \
            m.TELFORD_NO_REGISTER["evidence"]:
        problems.append("a rule's evidence does not cite its decision")

    def body(e):
        e.files(years=("2017-18", "2018-19", "2024-25"))
        text_, _ = e.ok(e.cur, ["load"])
        if "zero rule TELFORD_NO_REGISTER" not in text_ or \
                "zero rule ALLERDALE_ZERO" not in text_:
            problems.append("the preview does not list both zero rules")
        e.ok(e.cur, ["load", "--commit"])
        if tl.live_val(e.cur, "E06000020", period="2018") != 0:
            problems.append("Telford's 2018 zero (before the rule) was "
                            "changed")
        if tl.live_val(e.cur, "E06000020") is not None or tl.ed_val(
                e.cur, "E06000020", "value_flag", "2025") != \
                "households_on_register=not_applicable; " \
                "jointly_managed_register=not_applicable":
            problems.append("Telford's 2025 zero is not NULL not_applicable")
        src = tl.live_val(e.cur, "E06000020", "source")
        if not src.endswith("[" + m.TELFORD_NO_REGISTER["note"] + "]"):
            problems.append("Telford's live source lacks the rule note")
        if tl.live_val(e.cur, "E06000001", "source").endswith("]"):
            problems.append("a row with no rule has a note in source")
        if tl.live_val(e.cur, "E06000063", period="2018") is not None or \
                "households_on_register=part_missing" not in tl.ed_val(
                    e.cur, "E06000063", "value_flag", "2018"):
            problems.append("Cumberland 2018 is not NULL part_missing while "
                            "Allerdale's zero is NULL")
        if tl.live_val(e.cur, "E06000063", period="2019") != sum(
                tl.n(c, "2018-19") for c in PREDECESSORS):
            problems.append("Cumberland 2019 (Allerdale reports a number) "
                            "is not the sum")
        if tl.live_val(e.cur, "E06000001", period="2019") is None:
            problems.append("an unrelated authority is NULL")
    scenario(cur, body)

    def after_window(e):
        e.files(years=("2017-18", "2018-19", "2024-25"), overrides={
            ("E07000026", "2018-19", "cc1a"): "0"})
        e.ok(e.cur, ["load", "--commit"])
        want = sum(tl.n(c, "2018-19") for c in PREDECESSORS[1:])
        if tl.live_val(e.cur, "E06000063", period="2019") != want:
            problems.append("Allerdale's 2019 published zero was read as "
                            "NULL (the rule ends in 2018)")
    scenario(cur, after_window)
    report(24, name, not problems, "Telford and Wrekin's zero is NULL "
           "(not_applicable) from 2022 and its rule note is kept in source, "
           "Allerdale's 2015 to 2018 zeros are NULL so Cumberland is NULL "
           "(part_missing), and nothing else changes: not another year, "
           "code or non-zero value, and Allerdale's 2019 zero counts as a "
           "value" if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_codes, gate_4_row_counts, gate_5_blanks_and_zeros,
         gate_6_successor_sums, gate_7_cross_check, gate_8_identity,
         gate_9_older_file, gate_10_stop_conditions, gate_11_one_transaction,
         gate_12_ledger_skip, gate_13_preview, gate_14_reread,
         gate_15_revision_and_refresh, gate_16_before_state_hash,
         gate_17_w1_read, gate_18_migration_proof, gate_19_rerun,
         gate_20_stranded, gate_21_restore, gate_22_no_network_no_secrets,
         gate_23_correction, gate_24_zero_rules)


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
        sys.exit("usage: s13_lahs_editions_verify.py [--print-note-lines]")
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
    mine = [t for t in left if t.startswith("zz_s13")]
    if mine:
        print(f"LEFTOVER: committed zz_s13 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s13 table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
