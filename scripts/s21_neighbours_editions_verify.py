"""Gates for the S21 (ONS statistical nearest neighbours, Table 7a,
la_statistical_neighbours) editions tables.

Mirrors scripts/s5_imd_editions_verify.py, s13_lahs_editions_verify.py,
s3_mye_editions_verify.py and s23_rsh_stock_editions_verify.py. Prints
`GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes nothing, not even
a run-log row. The real-table gates read la_statistical_neighbours,
la_statistical_neighbours_editions and its file-check ledger. Until the
editions table exists (ddl and migrate-legacy not yet run) they print
`FAIL (pending migration)` with the words "not yet migrated", so the
script exits non-zero until then, like the other verifiers; nothing is
created to make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s21_live / zz_s21_editions / zz_s21_editions_file_checks
(a copy of the loader's spec), created inside the transaction, and call the
loader's own writer and commands (s21_neighbours_editions.main with the SPEC
pointed at the copy, the ONS replies and the downloads stubbed; apply_release;
migrate_legacy; restore_edition) rather than a re-implementation. The real
tables are only ever read.

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), shape, the CHECKs refuse a bad row          (throwaway + real)
  2  live holds one release (the live release_date column, pending until
     ddl); edition 1 exists for it; live equals LIVE_RULE applied to the
     latest edition for all 296 subjects (1,480 pairs, ranks 1 to 5);
     LIVE_RULE is one named constant and nothing else decides what is live
     (AST)                                                 (seeded + real)
  3  codes: every live code in la_boundaries; no E08000038/39 stored in live
     or in a resolved editions column; geography.resolve only, no private
     recode dict and no la_code_lookup query (AST)         (seeded + real)
  4  the editions hold the publisher's table: 296 subjects x 20 rows with
     publisher ranks 1..20, and 5 live rows each           (seeded + real)
  5  every NULL england_rank is a non-English neighbour (stored verbatim)
     with its reason; no other NULL                        (seeded + real)
  6  identity from the file: Cover title and date, Contents and sheet
     titles, the exact header, notes 2 and 12, docProps modified (seeded
     mismatches halt)                                      (seeded + real)
  7  a blank or [x] cell halts (seeded)
  8  older-file guard on the page, --file and --file --no-page paths; the
     rank is the Cover date, never the file name           (seeded + real)
  9  stop conditions: each seeded REJECTED stores nothing, no ledger row,
     partial run-log row
 10  the first release is stored in one transaction (edition 1, live rows
     and the ledger row); a failing insert rolls all back
 11  ledger rule: the skip needs the (where, sha256) pair for the release
 12  preview and simulate write nothing
 13  a byte-identical re-read is `unchanged` on every path against edition 1
     'as loaded'; the held file read now equals edition 1  (seeded + real)
 14  a second ONS edition (another Cover date) is stored as editions only
     and can never reach live through load or refresh-latest
                                                           (seeded + real)
 15  a reissue stores edition 2; refresh-latest changes only live rows,
     needs --acknowledge, sets source and copies loaded_at
 16  edition 1 equals the decision note's before-state line (seeded + real)
 17  live equals the decision note's live-read line and before-state-all
     line (live rows identical before and after)           (seeded + real)
 18  the migration proof re-run read-only from the file on disk
                                                           (seeded + real)
 19  rerun idempotent
 20  restore-edition round trip
 21  no network, no secret in source or output, nothing left committed, no
     real write, no old build imported or run (AST)

The decision note's hash lines are scan-safe: a 64-hex run would be flagged
by the credential scan, so each sha256 is written as two 32-hex halves in
two labelled fields:
    before-state la_statistical_neighbours <release> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    before-state-all la_statistical_neighbours <release> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    live-read la_statistical_neighbours rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
before-state hashes the held live rows as (lad24cd, neighbour, release,
neighbour_rank) and is compared with LIVE_RULE applied to edition 1;
before-state-all hashes every stored live column but release_date (key,
neighbour_rank, source, loaded_at); live-read hashes (lad24cd,
statistical_neighbour_lad24cd, neighbour_rank), what the council briefing
and the Liverpool paper read. `python scripts/s21_neighbours_editions_verify.py
--print-note-lines` prints the three lines for the live table as it stands
(read-only), for the decision note. Take them before the migration (they
read the same after it while live is untouched).

Usage:
    python scripts/s21_neighbours_editions_verify.py
    python scripts/s21_neighbours_editions_verify.py --print-note-lines

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The cursor handed to the gates
refuses commit and rollback on its connection, and the commands get a
stand-in connection whose commit only counts. No network, no real-table
writes, no backend is ever terminated. After the rollback a second,
read-only connection checks that no zz% relation or function was left
behind and that the real tables are as they were. The old build
(scripts/s21_statistical_neighbours_build.py) is never imported or run.
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
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
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
import s21_neighbours_editions as m  # noqa: E402
import test_s21_neighbours_loader as tl  # noqa: E402
import test_s21_neighbours_pure as tp  # noqa: E402
from test_s21_neighbours_pure import ENGLISH, FILE, write_s21  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real spec and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPEC, which the seeded gates point
# at the copy)
REAL = m.SPEC
SUBJECTS = m.EXPECTED_SUBJECTS
ZZ = tl.ZZ
LEDGER = tl.LEDGER
P = tl.P
P27 = tl.P27
P25 = tl.P25
N = len(ENGLISH)
N_ED = tl.N_ED
N_LIVE = tl.N_LIVE
CHANGED = tl.CHANGED
UTC = timezone.utc
LOADER = HERE / "s21_neighbours_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-11-s21-editions-first-load.md")
NOT_YET = "the S21 editions table does not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (21)
REAL_BEFORE = {}
TABLE = "la_statistical_neighbours"
ENGLISH_PREFIXES = ("E06", "E07", "E08", "E09")
# the held file: its nations and its docProps modified (Task 1's read-only
# survey of 2026-10-11)
EXPECTED_NATIONS = {"England": 296, "Scotland": 32, "Wales": 22,
                    "Northern Ireland": 11}
EXPECTED_BOUNDARIES = "March 2026"
# built so that this file never names the old build as a string literal
OLD_BUILD = "s21_statistical_" + "neighbours_build"
EMPTY = "no live release to check (an empty state is not a pass)"


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


def profile_for(spec):
    import dataclasses
    return dataclasses.replace(m.PROFILE, spec=spec)


@contextmanager
def specs():
    """The loader pointed at the throwaway copy (5 English subjects)."""
    with tl.specs():
        yield


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    names = (ZZ.live_table, ZZ.editions_table, LEDGER)
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                      for n in names))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s21 table already exists as a real table; "
                 "refusing to run")
    cur.execute(f"CREATE TABLE public.{ZZ.live_table} (LIKE "
                f"public.{REAL.live_table} INCLUDING ALL)")
    # the copy starts from the held shape, before the ddl under test adds
    # the release_date column
    cur.execute(f"ALTER TABLE public.{ZZ.live_table} DROP COLUMN IF EXISTS "
                f"{m.PERIOD}")
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


def live_md5(cur):
    return tl.live_md5(cur)


# ------------------------------------------------------------ files and Env

class Env(tl.Migrate):
    """The loader's main(argv) on the throwaway tables, the ONS replies and
    the downloads stubbed; every workbook is written here (write_s21)."""

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api = {m.LANDING: tp.landing()}
        self.urls, self.final, self.fetched = {}, {}, []
        self.n = 0
        self.last_url = None

    def run_main(self, cur, argv):
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

    def migrated(self, **kw):
        """A seeded legacy state migrated through the real command:
        (path, files)."""
        path, files = self.seed_legacy(self.cur, **kw)
        with mock.patch.object(m, "LEGACY_LIVE", self.legacy(self.cur)), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = self.cmd(["migrate-legacy", path, "--commit"])
        if rc != 0:
            raise AssertionError(f"migrate-legacy: {text[-200:]}")
        return path, files


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
    """A seeded legacy state (as the old build left it: LIVE_RULE's rows of
    a fixture file, one loaded_at and one source) and the fixture file;
    fn(env, path, files)."""
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


def loosen(cur, spec):
    """(in a caller's savepoint) switch off the append-only triggers and the
    CHECKs of the throwaway editions table so a defect can be planted."""
    cur.execute(f"ALTER TABLE public.{spec.editions_table} DISABLE TRIGGER "
                "USER")
    for name in _constraint_names(cur, spec.editions_table, "c"):
        cur.execute(f"ALTER TABLE public.{spec.editions_table} DROP "
                    f"CONSTRAINT {name}")


def plant_ed(cur, spec, sqls, check):
    """check() after the statements run on the loosened editions table, in
    a savepoint that is rolled back."""
    cur.execute("SAVEPOINT plant")
    try:
        loosen(cur, spec)
        for sql in ([sqls] if isinstance(sqls, str) else sqls):
            cur.execute(sql)
        return check()
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT plant")
        cur.execute("RELEASE SAVEPOINT plant")


def drop_live_release_check(cur, spec):
    """Drop the live release_date CHECK (in a caller's savepoint) so the
    gate's own detection can be tested."""
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = "
                "%s::regclass AND contype = 'c' AND "
                "pg_get_constraintdef(oid) LIKE %s",
                (f"public.{spec.live_table}", f"%{m.PERIOD}%"))
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


def _constraint_names(cur, table, kind):
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = "
                "%s::regclass AND contype = %s", (f"public.{table}", kind))
    return [r[0] for r in cur.fetchall()]


def shape_problems(cur, spec, ref=None):
    """Problems with the editions table, the live table and the ledger of a
    spec. ref: a spec whose editions CHECK constraints the real ones must
    equal (the throwaway copy, built from the same spec)."""
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
    fks = _constraint_defs(cur, spec.editions_table, "f")
    if len(fks) != (1 if spec.fk_la_boundaries else 0):
        bad.append(f"{spec.editions_table}: {len(fks)} foreign key(s)")
    elif fks and not (fks[0].startswith("FOREIGN KEY (lad24cd) REFERENCES")
                      and "la_boundaries" in fks[0]):
        bad.append(f"{spec.editions_table}: the foreign key is {fks[0]!r}, "
                   "expected the subject (lad24cd) to la_boundaries only")
    checks = _constraint_defs(cur, spec.editions_table, "c")
    if len(checks) < len(spec.table_constraints) + 2:
        bad.append(f"{spec.editions_table}: {len(checks)} CHECK "
                   "constraints, fewer than the spec's")
    if ref is not None and sorted(checks) != sorted(
            _constraint_defs(cur, ref.editions_table, "c")):
        bad.append(f"{spec.editions_table}: its CHECK constraints differ "
                   f"from the spec's ({ref.editions_table})")
    bad += _triggers(cur, spec.editions_table, spec.trigger,
                     spec.truncate_trigger)
    for c in m.KEY + (m.RANK, "source", "loaded_at"):
        if c not in lcols:
            bad.append(f"{spec.live_table}: {c} missing")
    lc = _columns(cur, led)
    ptype = _family(dict(spec.key_types)[spec.period_col])
    for c, ty, nullable in (("id", "bigint", "NO"),
                            (spec.period_col, ptype, "NO"),
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


def _insert_copy(cur, spec, where, overrides, edition=9):
    """INSERT ... SELECT of one editions row as edition `edition` with some
    columns replaced (SQL expressions). Raises psycopg2.Error when a
    constraint refuses it."""
    cols = list(_columns(cur, spec.editions_table))
    exprs = []
    for c in cols:
        if c == "edition":
            exprs.append(str(edition))
        elif c in overrides:
            exprs.append(overrides[c])
        else:
            exprs.append(c)
    cur.execute(f"INSERT INTO public.{spec.editions_table} "
                f"({', '.join(cols)}) SELECT {', '.join(exprs)} FROM "
                f"public.{spec.editions_table} WHERE {where} LIMIT 1")
    return cur.rowcount


def constraint_refusals(cur, spec):
    """Problems (empty = fine) with the editions CHECKs of a seeded
    throwaway: a control copy of a row is stored, and each defect (a
    subject listing itself, an English row without england_rank, a
    non-English row with one or with a different stored code or a live
    rank, a NULL without its reason, a publisher rank of 21, an unknown
    nation, a malformed published code) is refused."""
    eng = "neighbour_nation = 'England' AND neighbour_rank IS NOT NULL"
    non = "neighbour_nation <> 'England'"
    cases = (
        ("a subject listing itself", eng, {
            "statistical_neighbour_lad24cd": "lad24cd"}),
        ("an English row without england_rank", eng, {
            "england_rank": "NULL"}),
        ("a non-English row with an england_rank", non, {
            "england_rank": "3"}),
        ("a non-English row stored under another code", non, {
            "statistical_neighbour_lad24cd": "'E06000003'"}),
        ("a non-English row with a live rank", non, {
            "neighbour_rank": "1"}),
        ("a NULL england_rank without its reason", non, {
            "null_reasons": "NULL"}),
        ("a publisher rank of 21", eng, {"publisher_rank": "21"}),
        ("an unknown nation", eng, {"neighbour_nation": "'Ireland'"}),
        ("a malformed published code", eng, {
            "neighbour_code_published": "'X123'"}))
    bad = []
    cur.execute("SAVEPOINT refuse")
    try:
        if _insert_copy(cur, spec, eng, {}) != 1:
            bad.append("the control copy of a row was not stored")
    except psycopg2.Error as e:
        bad.append(f"the control copy of a row was refused: {_first(e)}")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT refuse")
        cur.execute("RELEASE SAVEPOINT refuse")
    for label, where, over in cases:
        cur.execute("SAVEPOINT refuse")
        try:
            if _insert_copy(cur, spec, where, over) != 1:
                bad.append(f"{label}: no row to copy")
            else:
                bad.append(f"{label} was accepted")
        except psycopg2.Error:
            pass
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT refuse")
            cur.execute("RELEASE SAVEPOINT refuse")
    return bad


def gate_1_table_shape_and_immutability(cur):
    name = ("editions table, ledger, live shape (UPDATE, DELETE and TRUNCATE "
            "are refused on the editions and the ledger; the CHECKs refuse "
            "a bad row)")
    bad = shape_problems(cur, ZZ)

    def seed(cur):
        with env(cur) as e, specs():
            e.seed(e.cur)
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
    bad += scenario(cur, lambda e: (e.seed(e.cur),
                                    constraint_refusals(e.cur, ZZ))[1])
    scope = "throwaway copy"
    if exists(cur):
        scope += " and real tables"
        bad += [f"real: {p}" for p in shape_problems(cur, REAL, ref=ZZ)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise "
           "append-only on the editions table and the ledger; nine bad "
           "rows are refused by the CHECKs")


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


def _live_releases(cur, spec):
    if not m.live_has_release(cur, spec):
        raise ValueError(f"{spec.live_table} has no {m.PERIOD} column (ddl "
                         "not yet run)")
    cur.execute(f"SELECT DISTINCT {spec.period_col}::text FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _tip(cur, spec, p):
    return core.chain_tip(cur, spec, str(p))


def release_column_problems(cur, spec):
    """Problems with the live release_date column: date, NOT NULL, DEFAULT
    the live release, and the CHECK release_date = that release (the live
    table can hold no second release)."""
    if not m.live_has_release(cur, spec):
        return [f"{spec.live_table}.{m.PERIOD} does not exist (ddl not yet "
                "run)"]
    cur.execute("SELECT data_type, is_nullable, column_default FROM "
                "information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = %s AND column_name = %s",
                (spec.live_table, m.PERIOD))
    row = tuple(cur.fetchone())
    bad = []
    want = ("date", "NO", f"'{m.LIVE_RELEASE}'::date")
    if row != want:
        bad.append(f"{spec.live_table}.{m.PERIOD} is {row}, expected {want}")
    checks = " ".join(_constraint_defs(cur, spec.live_table, "c"))
    if f"({m.PERIOD} = '{m.LIVE_RELEASE}'::date)" not in checks:
        bad.append(f"{spec.live_table}: CHECK {m.PERIOD} = "
                   f"'{m.LIVE_RELEASE}' is missing")
    return bad


def derive_live(recs, rule=None):
    """{(subject, neighbour): rank}: the rule's live rows derived from a
    release's records here, independently of the loader's live_rows (the
    same fields of m.LIVE_RULE, read separately)."""
    rule = rule or m.LIVE_RULE
    by = {}
    for r in recs:
        by.setdefault(r["lad24cd"], []).append(r)
    out = {}
    for subj, rows in by.items():
        eng = sorted((r for r in rows
                      if str(r["neighbour_code_published"]).startswith(
                          tuple(rule.english_prefixes))
                      and (rule.within_publisher_rank is None
                           or r["publisher_rank"] <= rule.within_publisher_rank
                           )), key=lambda r: r["publisher_rank"])
        for i, r in enumerate(eng[:rule.take], start=1):
            out[(subj, r["statistical_neighbour_lad24cd"])] = (
                i if rule.renumber else r["publisher_rank"])
    return out


def real_live_rule(cur, spec=REAL, subjects=SUBJECTS):
    """Live holds exactly one release (the planned release_date column);
    it has edition 1; live equals LIVE_RULE applied to its latest edition,
    pair for pair and rank for rank, for every subject; and, whatever the
    rule says, every live neighbour is an English authority of the
    publisher's table and every rank is 1 to 5."""
    _need(cur, spec)
    bad = release_column_problems(cur, spec)
    if not m.live_has_release(cur, spec):
        return False, "; ".join(bad[:3])
    cur.execute(f"SELECT {spec.period_col}::text, COUNT(*) FROM "
                f"public.{spec.live_table} GROUP BY 1")
    live = dict(cur.fetchall())
    if not live:
        return False, f"{spec.live_table}: {EMPTY}"
    if set(live) != {m.LIVE_RELEASE}:
        bad.append(f"live holds release(s) {sorted(live)}, expected only "
                   f"{m.LIVE_RELEASE}")
    cur.execute(f"SELECT DISTINCT {spec.period_col}::text FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "source_file IS NOT NULL")
    have = {r[0] for r in cur.fetchall()}
    missing = sorted(p for p in live if p not in have)
    if missing:
        bad.append(f"no edition 1 (with a source_file) for {missing}")
    pairs = 0
    for p in sorted(set(live) & have):
        ed = _tip(cur, spec, p)
        recs = m.records(cur, spec, p, ed)
        got = m.live_pairs(cur, spec, p)
        want = derive_live(recs)
        added = sorted(k for k in want if k not in got)
        removed = sorted(k for k in got if k not in want)
        moved = sorted(k for k in want if k in got and got[k] != want[k])
        if added or removed or moved:
            bad.append(f"{p} ed{ed}: live differs from LIVE_RULE applied to "
                       f"the latest edition ({len(added)} missing, "
                       f"{len(removed)} extra, {len(moved)} with another "
                       f"rank; e.g. {(added or removed or moved)[:2]})")
        bad += [f"{p}: {x}" for x in m.live_equals_tip(cur, spec, [p])]
        subj = {r["lad24cd"] for r in recs}
        if len(subj) != subjects:
            bad.append(f"{p} ed{ed}: {len(subj)} subjects, expected "
                       f"{subjects}")
        per = Counter(a for a, _ in got)
        short = sorted(s for s in subj if per.get(s) != m.LIVE_RULE.take)
        if short:
            bad.append(f"{len(short)} subject(s) without exactly "
                       f"{m.LIVE_RULE.take} live rows, e.g. {short[:3]}")
        by = {(r["lad24cd"], r["statistical_neighbour_lad24cd"]): r
              for r in recs}
        foreign = sorted(k for k in got if k not in by
                         or by[k]["neighbour_nation"] != "England"
                         or not str(by[k]["neighbour_code_published"])
                         .startswith(ENGLISH_PREFIXES))
        if foreign:
            bad.append(f"{len(foreign)} live pair(s) are not English "
                       f"authorities of the publisher's table, e.g. "
                       f"{foreign[:2]}")
        outside = sorted(k for k, r in got.items() if r not in range(1, 6))
        if outside:
            bad.append(f"{len(outside)} live rank(s) outside 1..5")
        pairs += len(got)
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live holds {m.LIVE_RELEASE} only, with edition 1; its "
        f"{pairs:,} pairs equal LIVE_RULE ({m.LIVE_RULE.name}) applied to "
        f"the latest edition for all {subjects} subjects, every one an "
        f"English authority, ranks 1 to {m.LIVE_RULE.take}")


# the rule's fields and who may read them: the functions that apply or
# describe the rule. Nothing else may decide what is live.
RULE_FIELDS = ("name", "english_prefixes", "within_publisher_rank", "take",
               "renumber")
RULE_ATTRS = {"english_prefixes", "within_publisher_rank", "take",
              "renumber"}
RULE_READERS = {"live_rows", "_is_english", "rule_counts", "rule_text",
                "migrate_legacy", "cmd_migrate_legacy"}
LIVE_WRITERS = ("insert_live", "_want")


def rule_source_problems(text):
    """Problems in the loader's source (an AST check): LIVE_RULE assigned
    exactly once, at module level, as LiveRule(<the five fields>) of
    literals whose english_prefixes are English; no `global LIVE_RULE` and
    no assignment through globals(); the rule's fields read only by the
    functions that apply or describe it; the functions that write the live
    rows (insert_live, _want) call live_rows; no SQL string selects live
    rows by england_rank or by a stored neighbour_rank."""
    tree = ast.parse(text)
    bad = []
    stores = [n for n in ast.walk(tree) if isinstance(n, ast.Name)
              and n.id == "LIVE_RULE" and isinstance(n.ctx, (ast.Store,
                                                              ast.Del))]
    top = [n for n in tree.body if isinstance(n, ast.Assign)
           and any(isinstance(t, ast.Name) and t.id == "LIVE_RULE"
                   for t in n.targets)]
    if len(top) != 1 or len(stores) != 1:
        bad.append(f"LIVE_RULE is assigned {len(stores)} time(s) "
                   f"({len(top)} at module level), expected exactly once at "
                   "module level")
    elif not (isinstance(top[0].value, ast.Call)
              and getattr(top[0].value.func, "id", None) == "LiveRule"
              and [k.arg for k in top[0].value.keywords] == list(RULE_FIELDS)
              and not top[0].value.args):
        bad.append("LIVE_RULE is not LiveRule(" + ", ".join(RULE_FIELDS)
                   + ") with keyword arguments")
    else:
        try:
            vals = {k.arg: ast.literal_eval(k.value)
                    for k in top[0].value.keywords}
        except ValueError:
            bad.append("LIVE_RULE has a field that is not a literal")
        else:
            pre = vals["english_prefixes"]
            if not pre or not all(p in ENGLISH_PREFIXES for p in pre):
                bad.append(f"LIVE_RULE.english_prefixes {pre!r} is not a "
                           f"non-empty subset of {ENGLISH_PREFIXES}")
            if not (isinstance(vals["take"], int) and vals["take"] >= 1
                    and isinstance(vals["renumber"], bool)):
                bad.append("LIVE_RULE.take or renumber is not an int or bool")
    for node in ast.walk(tree):
        if isinstance(node, ast.Global) and "LIVE_RULE" in node.names:
            bad.append(f"line {node.lineno}: global LIVE_RULE")
        if (isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "globals"):
            bad.append(f"line {node.lineno}: an assignment through "
                       "globals()")
    scopes = [(n.name, n) for n in tree.body if isinstance(n, ast.FunctionDef)]
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        scopes += [(f.name, f) for f in cls.body
                   if isinstance(f, ast.FunctionDef)]
    inside = {id(x) for _, f in scopes for x in ast.walk(f)}
    for name, fn in scopes:
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and node.attr in RULE_ATTRS \
                    and name not in RULE_READERS:
                bad.append(f"line {node.lineno}: {name} reads .{node.attr} "
                           "(only the functions that apply or describe the "
                           "rule may)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in RULE_ATTRS \
                and id(node) not in inside:
            bad.append(f"line {node.lineno}: .{node.attr} read outside a "
                       "function")
    for want in LIVE_WRITERS:
        fn = [f for n, f in scopes if n == want]
        if not fn:
            bad.append(f"{want} is missing")
        elif not any(isinstance(x, ast.Call)
                     and getattr(x.func, "id", None) == "live_rows"
                     for x in ast.walk(fn[0])):
            bad.append(f"{want} does not take its rows from live_rows")
    doc = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc:
            s = node.value
            if re.search(r"(?is)\b(select|insert|update|delete|where)\b", s) \
                    and (re.search(r"(?i)\bengland_rank\s*(<=|>=|<|>|=|"
                                   r"between\b|in\b)", s)
                         or re.search(r"(?i)\bneighbour_rank\s+is\s+not\s+"
                                      r"null", s)):
                bad.append(f"line {node.lineno}: SQL that picks rows by "
                           "england_rank or a stored neighbour_rank")
    return bad


def _docstring_ids(tree):
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None and \
                    node.body and isinstance(node.body[0], ast.Expr):
                doc.add(id(node.body[0].value))
    return doc


def gate_2_live_rule(cur):
    name = ("live holds one release; edition 1 exists for it; live equals "
            "LIVE_RULE applied to the latest edition for all subjects; "
            "LIVE_RULE is one named constant and nothing else decides what "
            "is live")
    out = {}
    miss = []
    src = rule_source_problems(LOADER.read_text(encoding="utf-8"))

    def body(e):
        e.seed(e.cur)
        out["base"] = real_live_rule(e.cur, ZZ, subjects=N)
        live = ZZ.live_table
        # Hartlepool's neighbour 3 is Welsh: its neighbour 6 is live 5; the
        # Welsh, Scottish and Northern Irish neighbours are never live
        hl = tl.live(e.cur, "E06000001")
        out["promoted"] = (hl.get(("E06000001", "E06000008")) == 5
                           and ("E06000001", "W06000022") not in hl)
        every = tl.live(e.cur)
        out["english"] = not [k for k in every if k[1][0] != "E"]
        per = Counter(a for a, _ in every)
        out["five"] = len(per) == N and set(per.values()) == {5}
        for sql, needle in (
            (f"UPDATE public.{live} SET neighbour_rank = 5 WHERE lad24cd = "
             "'E06000002' AND neighbour_rank = 4", "another rank"),
            (f"DELETE FROM public.{live} WHERE lad24cd = 'E06000002' AND "
             "neighbour_rank = 3", "missing"),
            (f"UPDATE public.{live} SET statistical_neighbour_lad24cd = "
             "'W06000022' WHERE lad24cd = 'E06000002' AND neighbour_rank "
             "= 1", "not English authorities"),
            (f"INSERT INTO public.{live} (lad24cd, "
             "statistical_neighbour_lad24cd, neighbour_rank, source) VALUES "
             "('E06000002', 'E06000009', 6, 'x')", "extra"),
        ):
            ok, detail = plant(e.cur, sql, lambda: real_live_rule(
                e.cur, ZZ, subjects=N))
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:90]}")

        def other_release():
            drop_live_release_check(e.cur, ZZ)
            e.cur.execute(f"UPDATE public.{live} SET {m.PERIOD} = "
                          "'2030-01-01'")
            return real_live_rule(e.cur, ZZ, subjects=N)
        ok, detail = plant(e.cur, "SELECT 1", other_release)
        if ok or "no edition 1" not in detail or "holds release" not in detail:
            miss.append(f"a release without edition 1: {detail[:90]}")
        ok, detail = real_live_rule(e.cur, ZZ, subjects=N + 1)
        if ok or "subjects, expected" not in detail:
            miss.append("a wrong subject count passed")
        ok, detail = plant(e.cur, f"ALTER TABLE public.{live} DROP COLUMN "
                           f"{m.PERIOD}", lambda: real_live_rule(
                               e.cur, ZZ, subjects=N))
        if ok or "does not exist" not in detail:
            miss.append(f"a missing release_date column: {detail[:90]}")
        out["miss"] = miss

        def no_check():
            drop_live_release_check(e.cur, ZZ)
            return release_column_problems(e.cur, ZZ)
        out["nocheck"] = plant(e.cur, "SELECT 1", no_check)
    scenario(cur, body)
    # the AST check catches planted constructs
    text = LOADER.read_text(encoding="utf-8")
    planted = {
        "a second assignment": text + "\nLIVE_RULE = None\n",
        "a global rebind": text + ("\ndef _x():\n    global LIVE_RULE\n"
                                   "    LIVE_RULE = 1\n"),
        "the rule read elsewhere": text + ("\ndef _y(r):\n    return "
                                           "r.take\n"),
        "a non-English prefix": text.replace('"E06", "E07", "E08", "E09"',
                                             '"E06", "W06"', 1),
        "a SQL england_rank filter": text + (
            '\n_Q = "SELECT * FROM t WHERE england_rank <= 5"\n'),
        "a live writer without live_rows": text.replace(
            "rows = live_rows(records)", "rows = []", 1),
    }
    for label, t in planted.items():
        if t == text or not rule_source_problems(t):
            miss.append(f"AST missed {label}")
    ok = bool(out["base"][0] and not miss and not src
              and out.get("promoted") and out.get("english")
              and out.get("five") and out["nocheck"] is not None
              and any("CHECK" in x for x in out["nocheck"]))
    mixed(2, name, ok, "seeded: the complete state passes (Hartlepool's "
          "neighbour 6 is live 5 past its Welsh neighbour 3; no Welsh, "
          "Scottish or Northern Irish neighbour is live; five live rows "
          "each); a changed rank, a missing, an extra and a non-English "
          "live pair, a second release, a dropped column and a dropped "
          "CHECK are each caught; the loader's LIVE_RULE is assigned once "
          "as English-only literals, read only by the functions that apply "
          "it, and the live writers take their rows from live_rows (AST, "
          "six planted breaks caught)" if ok
          else f"{out} {miss[:3]} {src[:3]}", cur, real_live_rule)


# ---------------------------------------------------------------- gate 3

BOUNDARY_NEW = ("E08000038", "E08000039")
PRIVATE_CODE = re.compile(r"E080000(16|19|38|39)(?![0-9])")
QUERY_WORDS = re.compile(r"(?is)\b(select|from|join|update|insert)\b")


def geography_source_problems(text):
    """Problems in a module's source (an AST check): a string constant,
    other than a docstring, holding a Barnsley/Sheffield code (a private
    recode dict); a string constant that queries la_code_lookup; no call to
    geography.resolve."""
    tree = ast.parse(text)
    doc = _docstring_ids(tree)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc:
            if PRIVATE_CODE.search(node.value):
                bad.append(f"line {node.lineno}: private code "
                           f"{node.value[:40]!r}")
            if "la_code_lookup" in node.value and \
                    QUERY_WORDS.search(node.value):
                bad.append(f"line {node.lineno}: la_code_lookup query")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "resolve"
             and getattr(n.func.value, "id", None) == "geography"]
    if not calls:
        bad.append("no call to geography.resolve")
    return bad


def real_codes(cur, spec=REAL, loader=LOADER):
    """Every live code (subject and neighbour) is in la_boundaries; no
    E08000038/39 is stored in live or in the key columns of any edition;
    every English neighbour of the editions is in la_boundaries and a code
    that differs from the published one is what geography.resolve says; the
    loader resolves through geography.resolve only (AST)."""
    _need(cur, spec)
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    bad = []
    codes = 0
    for table in (spec.live_table, spec.editions_table):
        for col in m.KEY:
            cur.execute(f"SELECT DISTINCT {col} FROM public.{table} WHERE "
                        f"{col} IS NOT NULL"
                        + ("" if table == spec.live_table else
                           " AND neighbour_nation = 'England'"))
            got = {r[0] for r in cur.fetchall()}
            codes += len(got)
            stray = sorted(got - valid)
            if stray:
                bad.append(f"{table}.{col}: {len(stray)} code(s) not in "
                           f"la_boundaries, e.g. {stray[:3]}")
            old = sorted(got & set(BOUNDARY_NEW))
            if old:
                bad.append(f"{table}.{col} stores {old} (the retired "
                           "Barnsley/Sheffield codes)")
    cur.execute(f"SELECT DISTINCT {spec.period_col}::text, "
                "neighbour_code_published, statistical_neighbour_lad24cd "
                f"FROM public.{spec.editions_table} WHERE neighbour_nation = "
                "'England' AND neighbour_code_published <> "
                "statistical_neighbour_lad24cd")
    moved = cur.fetchall()
    by = {}
    for p, pub, stored in moved:
        by.setdefault(p, {})[pub] = stored
    for p, mp in by.items():
        recode, problems = geography.resolve(cur, m.RUN_SOURCE, set(mp),
                                             by_period={p: set(mp)})
        for pub, stored in mp.items():
            if geography.canonical(pub, recode) != stored:
                bad.append(f"{p}: {pub} is stored as {stored}, "
                           f"geography.resolve says "
                           f"{geography.canonical(pub, recode)}")
        bad += [f"{p}: {x}" for x in problems]
    bad += [f"loader: {x}" for x in geography_source_problems(
        Path(loader).read_text(encoding="utf-8"))]
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{codes} distinct live and editions codes are all in "
        f"la_boundaries; none is E08000038/39 (the {sum(len(x) for x in by.values())}"
        " published codes that differ resolve through geography.resolve); "
        "the loader has no private recode dict and no la_code_lookup query")


def gate_3_codes(cur):
    name = ("codes: every live code in la_boundaries; no E08000038/39 "
            "stored; geography.resolve only (no private recode dict, no "
            "la_code_lookup query)")
    out = {}
    miss = []

    def body(e):
        # Barnsley and Sheffield are published on the new codes as subject
        # and as neighbour: stored on the canonical codes, published code
        # kept in the editions-only column
        e.seed(e.cur)
        out["base"] = real_codes(e.cur, ZZ)
        out["subject"] = tl.count(
            e.cur, "zz_s21_editions", "WHERE lad24cd = 'E08000016'") == 20
        out["neighbour"] = tl.count(
            e.cur, "zz_s21_editions", "WHERE lad24cd = 'E06000001' AND "
            "statistical_neighbour_lad24cd = 'E08000016' AND "
            "neighbour_code_published = 'E08000038'") == 1
        out["none_stored"] = tl.count(
            e.cur, "zz_s21_editions", "WHERE lad24cd IN ('E08000038', "
            "'E08000039') OR statistical_neighbour_lad24cd IN "
            "('E08000038', 'E08000039')") == 0
        live, ed = ZZ.live_table, ZZ.editions_table
        for sqls, needle, in_ed in (
            (f"UPDATE public.{live} SET statistical_neighbour_lad24cd = "
             "'E08000038' WHERE lad24cd = 'E06000002' AND neighbour_rank = "
             "1", "E08000038", False),
            (f"UPDATE public.{live} SET statistical_neighbour_lad24cd = "
             "'E06009999' WHERE lad24cd = 'E06000002' AND neighbour_rank = "
             "1", "not in la_boundaries", False),
            (f"UPDATE public.{ed} SET statistical_neighbour_lad24cd = "
             "'E08000039' WHERE lad24cd = 'E06000002' AND publisher_rank = "
             "2 AND neighbour_nation = 'England'", "E08000039", True),
            (f"UPDATE public.{ed} SET statistical_neighbour_lad24cd = "
             "'E06009999' WHERE lad24cd = 'E06000002' AND publisher_rank = "
             "2 AND neighbour_nation = 'England'", "not in la_boundaries",
             True),
            (f"UPDATE public.{ed} SET statistical_neighbour_lad24cd = "
             "'E09000002' WHERE lad24cd = 'E06000001' AND "
             "neighbour_code_published = 'E08000038'", "geography.resolve",
             True),
        ):
            fn = lambda: real_codes(e.cur, ZZ)  # noqa: E731
            if in_ed:
                ok, detail = plant_ed(e.cur, ZZ, sqls, fn)
            else:
                ok, detail = plant(e.cur, sqls, fn)
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:90]}")
        # through the command: an old code in the file halts, an unknown
        # English code is UNEXPLAINED; nothing stored
        e.file(modified=datetime(2026, 4, 1, tzinfo=UTC),
               overrides={"E06000001": {4: "E08000016"}})
        ok, text = e.halts(["load", "--commit"], state(e.cur), "E08000016")
        if not ok:
            miss.append("a file carrying E08000016 did not halt")
        e.file(overrides={"E06000001": {4: "E06000099"}})
        ok, text = e.halts(["load", "--commit"], state(e.cur),
                           "UNEXPLAINED E06000099")
        if not ok:
            miss.append("an unknown English code did not halt")
    scenario(cur, body)
    text = LOADER.read_text(encoding="utf-8")
    src = geography_source_problems(text)
    planted = {
        "a private dict": text + ('\nCODE_REMAP = {"E08000038": '
                                  '"E08000016"}\n'),
        "a la_code_lookup query": text + (
            '\n_Q = "SELECT * FROM la_code_lookup"\n'),
        "no geography.resolve": text.replace("geography.resolve(",
                                             "geography.other(")
        .replace("geography.resolve", "geography.other"),
    }
    for label, t in planted.items():
        if t == text or not geography_source_problems(t):
            miss.append(f"AST missed {label}")
    docplant = text + '\n\ndef _z():\n    """E08000038 la_code_lookup"""\n'
    if geography_source_problems(docplant) != src:
        miss.append("AST flagged a docstring")
    ok = (out["base"][0] and out.get("subject") and out.get("neighbour")
          and out.get("none_stored") and not miss and not src)
    mixed(3, name, ok, "seeded: Barnsley and Sheffield published on "
          "E08000038/39 as subject and as neighbour are stored on "
          "E08000016/19 (the published code kept beside); a retired code or "
          "a code outside la_boundaries planted in live or in an edition, "
          "and an edition code geography.resolve would not give, are each "
          "caught; a file carrying E08000016 or an unknown code halts with "
          "nothing stored; the loader calls geography.resolve and has no "
          "private recode dict or la_code_lookup query (AST, three planted "
          "breaks caught, a docstring is not)" if ok
          else f"{out} {miss[:3]} {src[:3]}", cur, real_codes)


# ---------------------------------------------------------------- gate 4

def real_counts(cur, spec=REAL, subjects=SUBJECTS):
    """The editions hold the publisher's table: in every edition of every
    release each subject has m.NEIGHBOURS rows with publisher ranks 1..20
    once each; the live release's editions have `subjects` subjects; live
    has LIVE_RULE.take rows for each."""
    _need(cur, spec)
    periods = _live_releases(cur, spec)
    if not periods:
        return False, EMPTY
    cur.execute(f"""SELECT {spec.period_col}::text, edition,
                           COUNT(*), MIN(n), MAX(n), SUM(n), MIN(lo),
                           MAX(hi), MIN(d)
                    FROM (SELECT {spec.period_col}, edition, lad24cd,
                                 COUNT(*) n, MIN(publisher_rank) lo,
                                 MAX(publisher_rank) hi,
                                 COUNT(DISTINCT publisher_rank) d
                          FROM public.{spec.editions_table}
                          GROUP BY 1, 2, 3) q
                    GROUP BY 1, 2 ORDER BY 1, 2""")
    rows = cur.fetchall()
    if not rows:
        return False, f"{spec.editions_table} is empty"
    bad = []
    n = m.NEIGHBOURS
    for p, ed, subj, lo_n, hi_n, tot, lo, hi, d in rows:
        if (lo_n, hi_n, lo, hi, d) != (n, n, 1, n, n):
            bad.append(f"{p} ed{ed}: rows per subject {lo_n}..{hi_n}, "
                       f"publisher ranks {lo}..{hi} ({d} distinct), "
                       f"expected {n} rows and ranks 1..{n}")
        if p in periods and subj != subjects:
            bad.append(f"{p} ed{ed}: {subj} subjects, expected {subjects}")
    take = m.LIVE_RULE.take
    cur.execute(f"SELECT lad24cd, COUNT(*) FROM public.{spec.live_table} "
                "GROUP BY 1")
    per = dict(cur.fetchall())
    if len(per) != subjects or set(per.values()) != {take}:
        bad.append(f"live has {len(per)} subjects with {sorted(set(per.values()))} "
                   f"rows each, expected {subjects} with {take}")
    tip_rows = sum(r[5] for r in rows if r[0] in periods
                   and r[1] == _tip(cur, spec, r[0]))
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(rows)} edition(s) of {len({r[0] for r in rows})} release(s), "
        f"every subject with {n} rows and publisher ranks 1..{n}; the live "
        f"release's latest edition has {subjects} subjects x {n} = "
        f"{tip_rows:,} rows; live {sum(per.values()):,} rows, {take} each")


def gate_4_counts(cur):
    name = ("the editions hold the publisher's table: subjects x 20 rows "
            "with publisher ranks 1..20; 5 live rows each")
    out = {}
    miss = []

    def body(e):
        e.seed(e.cur)
        out["base"] = real_counts(e.cur, ZZ, subjects=N)
        live, ed = ZZ.live_table, ZZ.editions_table
        ok, detail = plant(e.cur, f"DELETE FROM public.{live} WHERE lad24cd "
                           "= 'E06000002' AND neighbour_rank = 5",
                           lambda: real_counts(e.cur, ZZ, subjects=N))
        if ok or "rows each" not in detail:
            miss.append(f"a missing live row: {detail[:90]}")
        ok, detail = plant_ed(
            e.cur, ZZ, f"DELETE FROM public.{ed} WHERE lad24cd = "
            "'E06000002' AND publisher_rank = 7",
            lambda: real_counts(e.cur, ZZ, subjects=N))
        if ok or "expected 20 rows" not in detail:
            miss.append(f"a missing editions row: {detail[:90]}")
        ok, detail = plant_ed(
            e.cur, ZZ, f"UPDATE public.{ed} SET publisher_rank = 7 WHERE "
            "lad24cd = 'E06000002' AND publisher_rank = 8",
            lambda: real_counts(e.cur, ZZ, subjects=N))
        if ok or "expected 20 rows" not in detail:
            miss.append(f"a repeated publisher rank: {detail[:90]}")
        ok, detail = real_counts(e.cur, ZZ, subjects=N + 1)
        if ok or "subjects, expected" not in detail:
            miss.append("a wrong subject count passed")
    scenario(cur, body)
    ok = out["base"][0] and not miss
    mixed(4, name, ok, "seeded: every subject has 20 editions rows with "
          "publisher ranks 1..20 and 5 live rows; a missing live row, a "
          "missing editions row, a repeated publisher rank and a wrong "
          "subject count are each caught" if ok
          else f"{out} {miss[:3]}", cur, real_counts)


# ---------------------------------------------------------------- gate 5

def real_nulls(cur, spec=REAL):
    """Every NULL england_rank is a neighbour of another nation, stored
    under its published code, with null_reasons naming it; an English
    neighbour has an england_rank; a NULL neighbour_rank says
    outside_live_rule and a live rank needs an england_rank; null_reasons is
    exactly what the rows' NULLs say; no other column is ever NULL."""
    _need(cur, spec)
    t = spec.editions_table
    bad, seen = [], {}

    def count(label, where):
        cur.execute(f"SELECT COUNT(*) FROM public.{t} WHERE {where}")
        k = cur.fetchone()[0]
        if k:
            bad.append(f"{k} row(s) {label}")
        return k
    count("with a NULL england_rank that is not a non-English neighbour "
          "stored verbatim with its reason",
          f"england_rank IS NULL AND NOT (neighbour_nation <> 'England' AND "
          "left(neighbour_code_published, 1) <> 'E' AND "
          "statistical_neighbour_lad24cd = neighbour_code_published AND "
          f"null_reasons LIKE '%england_rank={m.NOT_ENGLISH}%')")
    count("with an england_rank on a neighbour that is not English",
          "england_rank IS NOT NULL AND (neighbour_nation <> 'England' OR "
          "left(neighbour_code_published, 1) <> 'E')")
    count("with a NULL neighbour_rank that does not say it is outside the "
          "live rule",
          f"neighbour_rank IS NULL AND (null_reasons IS NULL OR "
          f"null_reasons NOT LIKE '%{m.RANK}={m.OUTSIDE_LIVE}%')")
    count("with a live rank but no england_rank",
          "neighbour_rank IS NOT NULL AND england_rank IS NULL")
    count("with null_reasons although nothing is NULL",
          "null_reasons IS NOT NULL AND england_rank IS NOT NULL AND "
          "neighbour_rank IS NOT NULL")
    count("with a non-English neighbour not stored under its published "
          "code", "neighbour_nation <> 'England' AND "
          "statistical_neighbour_lad24cd <> neighbour_code_published")
    cols = [c for c, (_, nul) in _columns(cur, t).items()
            if c not in ("england_rank", m.RANK, "null_reasons",
                         "supersedes")]
    for c in cols:
        count(f"with a NULL {c} (no NULL is allowed there)",
              f"{c} IS NULL")
    count("with a supersedes that is not NULL exactly for edition 1",
          "(edition = 1) <> (supersedes IS NULL)")
    cur.execute(f"SELECT DISTINCT {spec.period_col}::text, edition FROM "
                f"public.{t} ORDER BY 1, 2")
    nonenglish = 0
    for p, ed in cur.fetchall():
        recs = m.records(cur, spec, p, ed)
        wrong = [r for r in recs if m._reasons(r) != r["null_reasons"]]
        if wrong:
            bad.append(f"{p} ed{ed}: {len(wrong)} row(s) whose null_reasons "
                       f"is not what their NULLs say, e.g. "
                       f"{wrong[0]['lad24cd']}/"
                       f"{wrong[0]['statistical_neighbour_lad24cd']}")
        # england_rank counts 1..n over the English neighbours in publisher
        # order
        by = {}
        for r in recs:
            by.setdefault(r["lad24cd"], []).append(r)
        for s, rows in by.items():
            eng = [r["england_rank"] for r in sorted(
                rows, key=lambda x: x["publisher_rank"])
                if r["england_rank"] is not None]
            if eng != list(range(1, len(eng) + 1)):
                bad.append(f"{p} ed{ed} {s}: england_rank is {eng[:6]}.., "
                           "not 1..n in publisher order")
                break
        nonenglish = sum(1 for r in recs if r["england_rank"] is None)
        seen[(p, ed)] = nonenglish
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(seen)} edition(s): every NULL england_rank "
        f"({sum(seen.values()):,} rows) is a neighbour of another nation "
        "stored verbatim with its reason; england_rank counts 1..n over the "
        "English neighbours in publisher order; no other NULL")


def gate_5_nulls(cur):
    name = ("every NULL england_rank is a non-English neighbour with its "
            "reason; no other NULL")
    out = {}
    miss = []

    def body(e):
        e.seed(e.cur)
        out["base"] = real_nulls(e.cur, ZZ)
        ed = ZZ.editions_table
        out["welsh"] = tl.count(
            e.cur, "zz_s21_editions", "WHERE statistical_neighbour_lad24cd "
            "= 'W06000022' AND england_rank IS NULL AND null_reasons LIKE "
            "'%not_an_english_authority%' AND neighbour_code_published = "
            "'W06000022'") == 1
        for sqls, needle in (
            ((f"UPDATE public.{ed} SET england_rank = NULL WHERE lad24cd = "
              "'E06000002' AND publisher_rank = 2",), "NULL england_rank"),
            ((f"UPDATE public.{ed} SET england_rank = 1 WHERE lad24cd = "
              "'E06000001' AND publisher_rank = 3",), "not English"),
            ((f"UPDATE public.{ed} SET neighbour_rank = NULL, null_reasons = "
              "NULL WHERE lad24cd = 'E06000002' AND neighbour_rank = 2",),
             "outside the live rule"),
            ((f"UPDATE public.{ed} SET null_reasons = 'x' WHERE lad24cd = "
              "'E06000002' AND publisher_rank = 3 AND england_rank IS NOT "
              "NULL AND neighbour_rank IS NOT NULL",), "nothing is NULL"),
            ((f"ALTER TABLE public.{ed} ALTER COLUMN neighbour_name DROP "
              "NOT NULL",
              f"UPDATE public.{ed} SET neighbour_name = NULL WHERE lad24cd "
              "= 'E06000002' AND publisher_rank = 3",), "NULL neighbour_name"),
            ((f"UPDATE public.{ed} SET null_reasons = 'england_rank=other' "
              "WHERE lad24cd = 'E06000001' AND publisher_rank = 3",),
             "null_reasons"),
            ((f"UPDATE public.{ed} SET england_rank = england_rank + 1 "
              "WHERE lad24cd = 'E06000002' AND publisher_rank = 4",),
             "1..n in publisher order"),
        ):
            ok, detail = plant_ed(e.cur, ZZ, list(sqls),
                                  lambda: real_nulls(e.cur, ZZ))
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:90]}")
    scenario(cur, body)
    ok = out["base"][0] and out.get("welsh") and not miss
    mixed(5, name, ok, "seeded: the Welsh, Scottish and Northern Irish "
          "neighbours are stored verbatim with england_rank NULL and the "
          "reason; a NULL england_rank on an English neighbour, an "
          "england_rank on a Welsh one, a live-rule NULL without its "
          "reason, a reason with nothing NULL, another NULL column, a wrong "
          "reason and a broken england_rank run are each caught" if ok
          else f"{out} {miss[:3]}", cur, real_nulls)


# ---------------------------------------------------------- file gates (6-8)

def held_file(files=None, raw_dir=None):
    """The file on disk whose sha256 is the held file's, or None."""
    files = m.LEGACY_FILE if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    for p in sorted(raw.glob("*.xlsx")):
        if m.content_sha256(p) == files["sha256"]:
            return p
    return None


def real_identity(cur=None, files=None, raw_dir=None, spec=REAL,
                  subjects=SUBJECTS, nations=None):
    """The held file's identity read from the file itself equals what the
    loader surveyed: the Cover title, the release date (the period and the
    rank), the Contents and sheet titles, the exact header, notes 2 and 12,
    docProps modified, the English subjects and the nations; once the
    editions table exists, the tip of every release names its Cover date."""
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, f"no file with sha256 {files['sha256'][:16]}"
    f = m.read_file(p)
    nations = EXPECTED_NATIONS if nations is None else nations
    bad = []
    if f["title"] != m.COVER_TITLE:
        bad.append(f"{p.name}: Cover title {f['title']!r}")
    if f["release_date"] != m.LIVE_RELEASE or f["rank"] != m.LEGACY_RANK:
        bad.append(f"{p.name}: Cover date {f['release_date']}, surveyed "
                   f"{m.LIVE_RELEASE}")
    if f["contents_title"] != m.SHEET_TITLE or f["sheet_title"] != \
            m.SHEET_TITLE:
        bad.append(f"{p.name}: Contents or sheet title is not "
                   f"{m.SHEET_TITLE!r}")
    if tuple(f["header"]) != m.HEADER:
        bad.append(f"{p.name}: the header is not the exact 42 columns")
    if f["boundaries"] != EXPECTED_BOUNDARIES:
        bad.append(f"{p.name}: boundaries {f['boundaries']!r}")
    if f["nations"] != nations:
        bad.append(f"{p.name}: subjects by nation {f['nations']}, expected "
                   f"{nations}")
    if nations.get("England") != subjects:
        bad.append(f"expected {subjects} English subjects, the nations say "
                   f"{nations.get('England')}")
    tips = ""
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        info = m.tip_info(cur, spec)
        off = [v for v, i in info.items() if i["rank"] is None]
        if off:
            bad.append(f"{off[:3]}: the tip's source_file names no Cover "
                       "date")
        tips = f"; {len(info)} stored release(s), each tip names its Cover date"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{p.name}: Cover {m.COVER_TITLE!r}, release {f['release_date']}, "
        f"{f['sheet_title']!r}, the exact {len(f['header'])}-column header, "
        f"boundaries {f['boundaries']}, notes 2 and 12 captured, docProps "
        f"modified {f['modified']:%Y-%m-%dT%H:%M:%SZ}, subjects by nation "
        f"{f['nations']}{tips}")


def gate_6_identity(cur):
    name = ("identity from the file: Cover title and date, Contents and "
            "sheet titles, exact header, notes 2 and 12, docProps "
            "modified; seeded mismatches halt")
    problems = []

    def body(e):
        def raises(label, **kw):
            try:
                m.read_file(write_s21(e.root / f"{label}.xlsx", **kw))
                problems.append(f"{label} was read")
            except ValueError:
                pass
        raises("cover_title", cover_title="Other title")
        raises("no_date", published=None)
        raises("two_dates", cover_extra=("The data tables in this "
                                         "spreadsheet were originally "
                                         "published at 9.30am 1 May 2026.",))
        raises("sheet_title", sheet_title="Table 7b: something else")
        raises("contents_title", contents_title="Table 7b: something else")
        hdr = list(m.HEADER)
        hdr[2] = "Neighbour 1: Code"
        raises("header", header=hdr)
        raises("header_short", header=list(m.HEADER)[:-2])
        raises("note2", note2="Boundaries are not stated")
        raises("note12", note12=None)
        raises("sheets", sheets=("Cover sheet", "Contents", "Notes"))
        raises("no_modified", modified=None)
        raises("unreadable_modified", core="<not-xml")
        raises("twice", subjects=ENGLISH + ENGLISH[:1])
        # through the command: an edition label of another year, an API
        # releaseDate that is not the Cover date
        e.file(release="2026-03-11T00:00:00.000Z")
        before = state(e.cur)
        ok, _ = e.halts(["load", "--commit"], before, "2026-03-11")
        if not ok:
            problems.append("an API date that is not the Cover date did not "
                            "halt")
        e.api = {m.LANDING: tp.landing()}
        e.file(edition="2027")
        ok, _ = e.halts(["load", "--commit"], before, "2027")
        if not ok:
            problems.append("an edition of another year over a 2026 Cover "
                            "did not halt")
        if m.identity_problems(m.read_file(write_s21(
                e.root / "ok.xlsx")), "2026", "2026-03-17T23:30:00.000Z"):
            problems.append("the evening-before API date (British Summer "
                            "Time) was refused")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: a wrong Cover title, a missing or doubled release line, "
          "a wrong sheet or Contents title, a renamed or short header, "
          "unreadable notes 2 and 12, a missing sheet, a missing or "
          "unreadable docProps modified and a repeated subject are each "
          "refused by the reader; an API date that is not the Cover date "
          "and an edition of another year halt with nothing stored"
          if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(6, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(6, name, False, _first(ex))
    report(6, name, ok, f"{sd}; held file: {detail}")


def gate_7_blanks_and_markers(cur):
    name = ("a blank pair, the [x] marker or a malformed code halts before "
            "anything is stored")
    problems = []

    def body(e):
        e.seed(e.cur)
        for label, cells, needle in (
                ("marker", {("E06000001", 8): "[x]"}, "note 12"),
                ("blank pair", {("E06000001", 8): None,
                                ("E06000001", 9): None}, "blank"),
                ("blank name", {("E06000001", 9): None}, "blank"),
                ("text", {("E06000001", 8): "n/a"}, "E06000001"),
                ("short code", {("E06000001", 8): "E0600001"}, "E06000001"),
                ("blank subject", {("E06000001", 0): None}, "blank")):
            path = write_s21(e.root / f"{label}.xlsx".replace(" ", "_"),
                             cells=cells)
            try:
                m.read_file(path)
                problems.append(f"{label} was read")
            except ValueError as ex:
                if needle not in str(ex):
                    problems.append(f"{label}: {_first(ex)[:80]}")
            e.file(modified=datetime(2026, 4, 1, tzinfo=UTC), cells=cells)
            ok, text = e.halts(["load", "--commit"], state(e.cur), needle)
            if not ok:
                problems.append(f"{label} did not halt the load")
    scenario(cur, body)
    report(7, name, not problems, "a [x] marker (naming note 12), a blank "
           "pair, a blank name, free text, a short code and a blank subject "
           "each halt the reader and the load, naming the cell, with "
           "nothing stored, no ledger row and no run-log row; nothing is "
           "coerced" if not problems else "; ".join(problems[:3]))


def real_rank(cur=None, files=None, raw_dir=None, spec=REAL,
              subjects=SUBJECTS):
    """The held file's rank is its Cover date (never its name), and equals
    the rank edition 1 records."""
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, f"no file with sha256 {files['sha256'][:16]}"
    f = m.read_file(p)
    if f["rank"] != f["published"] or m.release_rank(f) != f["published"]:
        return False, f"{p.name}: the rank is not the Cover date"
    detail = f"{p.name}: rank {f['rank'].isoformat()} (the Cover date)"
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = 1", (f["release_date"],))
        rows = cur.fetchall()
        if len(rows) != 1 or m.release_rank(rows[0][0]) != f["rank"]:
            return False, (f"{p.name}: edition 1 records rank "
                           f"{m.release_rank(rows[0][0]) if rows else None},"
                           f" the file {f['rank']}")
        detail += "; edition 1 records the same rank"
    return True, detail


def gate_8_older_file(cur):
    name = ("older-file guard on the page, --file and --file --no-page "
            "paths; the rank is the Cover date, never the file name")
    problems = []

    def older(e, how):
        if how == "file":
            e.api = {m.LANDING: tp.landing()}
        e.seed(e.cur)
        if how == "page":
            e.api[m.LANDING] = tp.landing()
        # the Cover says 12 March 2025; the NAME looks newer
        path = e.file("12 March 2025", register=how == "page",
                      overrides=CHANGED, name="2027" + FILE)
        argv = ["load", "--commit"]
        if how != "page":
            argv += ["--file", path]
        if how == "file-no-page":
            argv += ["--no-page"]
        if how == "file":
            e.edition("2025", "x.xlsx", "2025-03-12T00:00:00.000Z")
        return argv

    def body(e):
        for how in ("page", "file", "file-no-page"):
            def one(c, how=how):
                with env(c) as e2, specs():
                    argv = older(e2, how)
                    before = state(c)
                    rc, text, logged = e2.cmd(argv)
                    if not (rc == "halt" and "older" in text
                            and "--allow-older-file" in text
                            and state(c) == before and not logged.called):
                        problems.append(f"{how}: rc {rc}: {text[-160:]}")
            _in_savepoint(e.cur, one)

        def override(c):
            with env(c) as e2, specs():
                argv = older(e2, "file-no-page") + ["--allow-older-file"]
                live_before = live_md5(c)
                rc, text, logged = e2.cmd(argv)
                eds = tl.editions(c, P25)
                if not (rc == 0 and eds == [(1, None, N_ED)]
                        and live_md5(c) == live_before and logged.called
                        and "--allow-older-file given"
                        in logged.call_args[0][2]):
                    problems.append(f"--allow-older-file: rc {rc}, {eds}")
        _in_savepoint(e.cur, override)

        def name_is_not_evidence(c):
            # a file named like an older one but newer by its Cover is not
            # older (it is another ONS edition: editions only)
            with env(c) as e2, specs():
                e2.seed(c)
                path = e2.file("4 March 2027", register=False,
                               overrides=CHANGED, name="2025old" + FILE)
                before = live_md5(c)
                rc, text, _ = e2.cmd(["load", "--file", path, "--no-page",
                                      "--commit"])
                if rc != 0 or "--allow-older-file" in text \
                        or tl.editions(c, P27) != [(1, None, N_ED)] \
                        or live_md5(c) != before:
                    problems.append(f"a newer file with an older-looking "
                                    f"name was refused: rc {rc}")
        _in_savepoint(e.cur, name_is_not_evidence)

        def reissue_named_older(c):
            # the Cover says 18 March 2026 (the held release); the NAME
            # says 2025. The rank is the Cover date, so this is a reissue
            # of the held release (two files claim it; with both flags it
            # is edition 2), never an older file
            with env(c) as e2, specs():
                e2.seed(c)
                path = e2.file(register=False, overrides=CHANGED,
                               name="2025old" + FILE)
                argv = ["load", "--file", path, "--no-page", "--commit"]
                before = state(c)
                rc, text, logged = e2.cmd(argv)
                if not (rc == 1 and "two files claim the same release" in text
                        and "older file" not in text
                        and state(c) == before):
                    problems.append("a reissue named 2025 was not treated as "
                                    f"the held release: rc {rc}: "
                                    f"{text[-120:]}")
                rc, text, _ = e2.cmd(argv + ["--accept-reissue", P,
                                             "--acknowledge", P])
                if rc != 0 or tl.editions(c) != [(1, None, N_ED),
                                                 (2, 1, N_ED)]:
                    problems.append("a reissue named 2025 was not stored as "
                                    f"edition 2: rc {rc}")
        _in_savepoint(e.cur, reissue_named_older)
    scenario(cur, body)
    seeded = not problems
    sd = ("a file whose Cover says 12 March 2025 but whose name says 2027, "
          "offered by the page, --file and --file --no-page against a held "
          "18 March 2026 release, is skipped and halts with nothing stored "
          "or logged; --allow-older-file stores it as editions only (live "
          "untouched) and is logged; a 4 March 2027 file named 2025old is "
          "newer; a reissue of 18 March 2026 named 2025old is the held "
          "release's reissue, not an older file" if seeded else "; ".join(problems[:3]))
    mixed(8, name, seeded, sd, cur, real_rank)


# ---------------------------------------------------------------- gate 9-12

def rejects(e, argv, needle, before, *, commit=True, release=P):
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
                f"rejected ['{release}'" not in notes:
            return f"run-log notes: {notes[:120]}"
    elif logged.called:
        return "a preview wrote a run-log row"
    return None


def gate_9_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row")
    problems = []

    def case(label, setup, needle, commit=True, release=P, extra=()):
        def body(c):
            with env(c) as e, specs():
                setup(e)
                before = state(c)
                argv = ["load"] + (["--commit"] if commit else []) \
                    + list(extra)
                bad = rejects(e, argv, needle, before, commit=commit,
                              release=release)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)

    def short(e):
        e.file(subjects=ENGLISH[:4] + ["W06000022"])

    def equal_cover(e):
        e.seed(e.cur)
        e.file(overrides=CHANGED)

    def missing_subject(e):
        e.seed(e.cur)
        e.file(subjects=ENGLISH[:-1] + ["E09000001", "W06000022",
                                        "S12000033"])
    case("fewer English subjects than expected", short, m.PARTIAL)
    case("fewer English subjects, preview exits 1 and logs nothing", short,
         m.PARTIAL, commit=False)
    case("another release into an empty live table",
         lambda e: e.file("4 March 2027"), "empty live table", release=P27)
    case("equal Cover date, other content", equal_cover,
         "two files claim the same release")
    case("equal Cover date, preview exits 1 and logs nothing", equal_cover,
         "two files claim the same release", commit=False)
    case("a reissue missing a subject of the held edition, even with both "
         "flags", missing_subject, m.PARTIAL,
         extra=("--accept-reissue", P, "--acknowledge", P))

    def accepted(c):
        with env(c) as e, specs():
            e.seed(c)
            e.file(overrides=CHANGED)
            before = state(c)
            rc, text, _ = e.cmd(["load", "--commit", "--accept-reissue", P])
            if rc != 1 or state(c) != before:
                problems.append("--accept-reissue without --acknowledge "
                                "stored a reissue that changes live pairs")
            rc, text, logged = e.cmd(["load", "--commit", "--accept-reissue",
                                      P, "--acknowledge", P])
            if not (rc == 0 and "ACCEPTED REISSUE" in text
                    and "ACKNOWLEDGED" in text
                    and tl.editions(c) == [(1, None, N_ED), (2, 1, N_ED)]
                    and "--accept-reissue" in logged.call_args[0][2]
                    and "ACKNOWLEDGED" in logged.call_args[0][2]):
                problems.append(f"--accept-reissue --acknowledge: rc {rc}: "
                                f"{text[-120:]}")
            # an acceptance naming a release that is not compared halts
            e.file("4 March 2027", overrides=CHANGED)
            for flag in ("--accept-reissue", "--acknowledge"):
                ok, _ = e.halts(["load", "--commit", flag, "2019-01-01"],
                                state(c), "2019-01-01")
                if not ok:
                    problems.append(f"{flag} of a release not compared did "
                                    "not halt")
    _in_savepoint(cur, accepted)

    def within(c):
        with env(c) as e, specs():
            e.seed(c)
            e.file("4 March 2027", overrides=CHANGED)
            rc, text, _ = e.cmd(["load", "--commit"])
            if rc != 0 or tl.editions(c, P27) != [(1, None, N_ED)]:
                problems.append(f"a newer ONS edition was refused: rc {rc}")
    _in_savepoint(cur, within)
    report(9, name, not problems, "a file with fewer English subjects than "
           "expected, another release into an empty live table, equal Cover "
           "dates with other content and a reissue missing a subject (even "
           "with both flags) are each REJECTED (exit 1) with nothing "
           "stored, no ledger row and one partial run-log row (none in a "
           "preview); --accept-reissue needs --acknowledge when pairs "
           "change, then stores edition 2 and is logged; an acceptance "
           "naming an uncompared release halts; a newer ONS edition is "
           "stored" if not problems else "; ".join(problems[:3]))


def gate_10_first_release(cur):
    name = ("the first release is stored in one transaction (edition 1, live "
            "rows, ledger row); a failing insert rolls all back")
    problems = []

    def first(e):
        e.file()
        rc, text, logged = e.cmd(["load", "--commit"])
        eds = tl.editions(e.cur)
        led = tl.ledger(e.cur)
        if not (rc == 0 and eds == [(1, None, N_ED)]
                and tl.count(e.cur, "zz_s21_live") == N_LIVE
                and len(led) == 1 and led[0][2:] == ("new", 1)
                and led[0][1] == f"{e.last_url} (release {P}; published "
                                 f"{P})"
                and logged.call_count == 1
                and m.live_equals_tip(e.cur, ZZ, [P]) == []):
            problems.append(f"the first release: rc {rc}, editions {eds}, "
                           f"ledger {led}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM "
                      f"public.{ZZ.editions_table}")
        label = e.cur.fetchone()[0]
        e.cur.execute(f"SELECT DISTINCT source, {m.PERIOD}::text FROM "
                      f"public.{ZZ.live_table}")
        if e.cur.fetchall() != [(label, P)] or m.LIVE_RULE.name not in label:
            problems.append("live source is not the edition's label naming "
                            "LIVE_RULE")
    scenario(cur, first)

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
    report(10, name, not problems, "the first load stores edition 1 "
           "(every English subject's 20 neighbours), LIVE_RULE's live rows "
           "(source = the label naming the rule, release_date the release) "
           "and one ledger row together, with one run-log row; a failing "
           "ledger, live or edition insert rolls back all three and logs a "
           "partial run" if not problems else "; ".join(problems[:3]))


def gate_11_ledger_rule(cur):
    name = ("ledger rule: the skip needs the (where, sha256) pair declared "
            "for the release")
    problems = []

    def pair(e):
        path = e.file()
        sha = m.content_sha256(path)
        src = m.ledger_source(e.last_url, date(2026, 3, 18), P)
        # (URL, sha) recorded, but under another release: read
        e.cur.execute(f"INSERT INTO public.{LEDGER} ({m.PERIOD}, "
                      "source_file, file_sha256, outcome, edition) VALUES "
                      "(%s, %s, %s, 'unchanged', 1)", (P27, src, sha))
        rc, text, _ = e.cmd(["load"])
        if rc != 0 or "nothing parsed" in text or f"{P}: new" not in text:
            problems.append("a pair held for another release skipped the "
                            "file")
    scenario(cur, pair)

    def same_name(e):
        path0 = e.file()
        e.ok(e.cur, ["load", "--commit"])
        url0 = e.last_url
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "nothing parsed" not in text or logged.called:
            problems.append("a held pair did not skip the read")
        other = e.file(register=False, overrides=CHANGED,
                       name="other" + FILE)
        e.urls[url0] = other
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" in text \
                or "two files claim the same release" not in text:
            problems.append("the same URL with other bytes was skipped")
        e.urls[url0] = path0
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" not in text:
            problems.append("the held pair no longer skips")
        rc, text, _ = e.cmd(["load", "--recheck", P])
        if f"{P}: unchanged" not in text:
            problems.append("--recheck did not read a ledger-held file")
    scenario(cur, same_name)

    report(11, name, not problems, "a (URL, sha256) pair held under another "
           "release does not skip; the held pair skips with nothing parsed "
           "or logged; the same URL with other bytes is read (and stops as "
           "two files claiming one release); --recheck reads a held file"
           if not problems else "; ".join(problems[:3]))


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
                problems.append(f"new release {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.file(overrides=CHANGED)
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"], ["status"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}, state kept="
                                f"{state(e.cur) == before}")
        e.ok(e.cur, ["load", "--commit", "--accept-reissue", P,
                     "--acknowledge", P])
        before = state(e.cur)
        for argv in (["refresh-latest"], ["restore-edition", P, "1"],
                     ["refresh-latest", "--simulate", "--acknowledge", P],
                     ["restore-edition", P, "1", "--simulate"]):
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
                         ["ddl"], ["ddl", "--simulate"]):
                rc, text, logged = e.cmd(argv)
                if rc != 0 or state(e.cur) != before or logged.called:
                    problems.append(f"{argv[0]} {argv[2:]}: rc {rc}")
    legacy_world(cur, legacy)
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    if cur.fetchone()[0] != runs:
        problems.append("the run log changed")
    report(12, name, not problems, "load, refresh-latest, restore-edition, "
           "migrate-legacy and ddl in preview and --simulate leave the "
           "editions, the live table and the ledger as they were and log "
           "nothing" if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 13

def _built(cur, f):
    with contextlib.redirect_stdout(io.StringIO()):
        return m._build(cur, f)


def real_held_reread(cur, spec=REAL, files=None, raw_dir=None,
                     subjects=SUBJECTS):
    """The held file read again has the content sha256 of edition 1 'as
    loaded' (and its LIVE_RULE rows are the rows of live while no reissue is
    stored): so a re-read by the page or --file is unchanged, never a
    reissue."""
    _need(cur, spec)
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, f"no file with sha256 {files['sha256'][:16]}"
    f = m.read_file(p)
    with mock.patch.object(m, "EXPECTED_SUBJECTS", subjects):
        built = _built(cur, f)
    period = f["release_date"]
    held = m.records(cur, spec, period, 1)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (period,))
    bad = []
    if cur.fetchall() != [(core.AS_LOADED_LABEL,)]:
        bad.append(f"{period}: edition 1 is not the 'as loaded' one")
    a, b = m.rows_content_sha(held), m.rows_content_sha(built)
    if a != b:
        bad.append(f"{period}: edition 1 content sha {a[:12]} differs from "
                   f"the held file read now {b[:12]}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{p.name} read now has edition 1's content sha256 ({a[:12]}.., "
        f"{len(held):,} rows): every re-read is unchanged")


def gate_13_byte_identical_reread(cur):
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
        out["real"] = real_held_reread(e.cur, ZZ, files, d, subjects=N)
        e.urls[files["url"]] = path
        e.edition("2026", FILE, "2026-03-18T00:00:00.000Z")
        src_before = tl.live(e.cur)
        snap = tables_state(e.cur)
        rc, text, _ = e.cmd(["load"])
        if "nothing parsed" not in text:
            problems.append("the held file was parsed again by the page")
        for argv in (["load", "--recheck", P],
                     ["load", "--recheck", P, "--commit"],
                     ["load", "--file", path],
                     ["load", "--file", path, "--no-page"],
                     ["load", "--file", path, "--commit"],
                     ["load", "--file", path, "--no-page", "--recheck", P,
                      "--commit"]):
            rc, text, _ = e.cmd(argv)
            if rc != 0 or f"{P}: unchanged" not in text \
                    or "REJECTED" in text or "two files claim" in text:
                problems.append(f"{' '.join(map(str, argv[:4]))}: rc {rc}: "
                                f"{text[-120:]}")
        if tables_state(e.cur) != snap or tl.editions(e.cur) != [
                (1, None, N_ED)]:
            problems.append("a re-read stored an edition or moved live")
        if {o for _, _, o, _ in tl.ledger(e.cur)} != {"unchanged"}:
            problems.append("the re-reads did not add 'unchanged' rows only")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text or tl.live(e.cur) != src_before:
            problems.append("refresh-latest planned a write after a re-read")
        if not m.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
        # a file with other content is not the held file
        other = write_s21(Path(e.root) / "disk2" / path.name,
                          overrides=CHANGED)
        ok, detail = real_held_reread(
            e.cur, ZZ, {"sha256": m.content_sha256(other)}, other.parent,
            subjects=N)
        if ok or "differs" not in detail:
            problems.append("a file with other content was called equal")
    legacy_world(cur, body)
    seeded = not problems and out.get("real", (False,))[0]
    sd = ("seeded: after migrate-legacy the same bytes read by the page, "
          "--file, --file --no-page, --recheck and with --commit are "
          "unchanged against edition 1, store no edition, add 'unchanged' "
          "ledger rows only and leave live and status alone; the held file "
          "read now has edition 1's content sha"
          if seeded else f"{out} {problems[:3]}")
    mixed(13, name, seeded, sd, cur, real_held_reread)


# --------------------------------------------------------------- gate 14

def real_not_live(cur, spec=REAL):
    """Live holds one release; any other release in the editions is held,
    not live: status lists exactly those, it is clean, and refresh-latest
    plans nothing for them (no edition of another release reaches live)."""
    _need(cur, spec)
    periods = _live_releases(cur, spec)
    cur.execute(f"SELECT DISTINCT {spec.period_col}::text FROM "
                f"public.{spec.editions_table} ORDER BY 1")
    stored = [r[0] for r in cur.fetchall()]
    held = [p for p in stored if p != m.LIVE_RELEASE]
    prof = profile_for(spec)
    bad = []
    if periods != [m.LIVE_RELEASE]:
        bad.append(f"live holds release(s) {periods}, expected only "
                   f"{m.LIVE_RELEASE}")
    try:
        st = m.status(cur, prof)
    except SystemExit as e:
        return False, f"status halts: {_first(e)}"[:140]
    if sorted(st.get("held_not_live", [])) != held:
        bad.append(f"status lists held-not-live {st.get('held_not_live')}, "
                   f"the editions hold {held}")
    if not st["ok"]:
        bad.append(f"status needs action: {[k for k in ('new_periods', 'pending_refresh', 'drift_periods', 'chain_errors', 'bad_counts', 'live_missing') if st.get(k)]}")
    try:
        plan = m.refresh_plan(cur, prof)
    except SystemExit as e:
        bad.append(f"refresh-latest halts: {_first(e)[:80]}")
    else:
        if plan is not None and plan["period"] != m.LIVE_RELEASE:
            bad.append(f"refresh-latest plans {plan['period']}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live holds {m.LIVE_RELEASE} only; held, not live: "
        f"{held or 'none'}; status is clean")


def gate_14_second_edition(cur):
    name = ("a second ONS edition is stored as editions only and never "
            "reaches live through load or refresh-latest")
    problems = []
    out = {}

    def body(e):
        e.seed(e.cur)
        before = live_md5(e.cur)
        e.file("4 March 2027", overrides=CHANGED)
        rc, text, logged = e.cmd(["load", "--commit"])
        if not (rc == 0 and "NEWER EDITION" in text
                and f"{P27}: held-not-live" in text
                and tl.editions(e.cur, P27) == [(1, None, N_ED)]
                and live_md5(e.cur) == before):
            problems.append(f"a second edition did not stop at editions: "
                            f"rc {rc}: {text[-140:]}")
        out["held"] = real_not_live(e.cur, ZZ)
        if not out["held"][0] or P27 not in out["held"][1]:
            problems.append(f"held release not reported: {out['held']}")
        for argv in (["refresh-latest", "--commit"],
                     ["load", "--commit"],
                     ["load", "--recheck", P27, "--commit"]):
            rc, text, _ = e.cmd(argv)
            if live_md5(e.cur) != before:
                problems.append(f"{' '.join(map(str, argv[:3]))} moved live "
                                "after a second edition")
        snap = state(e.cur)
        for flag in (P27, "2019-01-01"):
            ok, _ = e.halts(["refresh-latest", "--commit", "--acknowledge",
                             flag], snap)
            if not ok:
                problems.append(f"refresh-latest --acknowledge {flag} did "
                                "not halt")
        # a reissue of the second edition is stored as its next edition and
        # still never reaches live
        e.file("4 March 2027", overrides=dict(CHANGED, E06000001={4:
                                                                  "E09000002"}))
        rc, text, _ = e.cmd(["load", "--commit", "--accept-reissue", P27,
                             "--acknowledge", P27])
        if rc != 0 or tl.editions(e.cur, P27) != [(1, None, N_ED),
                                                  (2, 1, N_ED)]:
            problems.append(f"a reissue of the held edition: rc {rc}: "
                            f"{text[-100:]}")
        rc, text, _ = e.cmd(["refresh-latest", "--commit"])
        if live_md5(e.cur) != before:
            problems.append("refresh-latest moved live after a reissue of "
                            "the second edition")
        # the live CHECK refuses a second release
        e.cur.execute("SAVEPOINT chk")
        try:
            e.cur.execute(f"UPDATE public.{ZZ.live_table} SET {m.PERIOD} = "
                          "'2027-03-04' WHERE lad24cd = 'E06000001'")
            problems.append("the live CHECK let a second release in")
        except psycopg2.Error:
            pass
        e.cur.execute("ROLLBACK TO SAVEPOINT chk")
        e.cur.execute("RELEASE SAVEPOINT chk")
        # a planted break is caught: live stranded on the held release
        ok, detail = plant(
            e.cur, f"DELETE FROM public.{ZZ.live_table}",
            lambda: real_not_live(e.cur, ZZ))
        if ok:
            problems.append("an empty live table passed")
        # the held release in live (CHECK dropped) is caught
        def held_in_live():
            drop_live_release_check(e.cur, ZZ)
            e.cur.execute(f"UPDATE public.{ZZ.live_table} SET {m.PERIOD} = "
                          "'2027-03-04'")
            return real_not_live(e.cur, ZZ)
        ok, detail = plant(e.cur, "SELECT 1", held_in_live)
        if ok or "holds release" not in detail:
            problems.append(f"a held release in live: {detail[:80]}")
    scenario(cur, body)
    ok = not problems and out.get("held", (False,))[0]
    mixed(14, name, ok, "seeded: a 4 March 2027 edition loaded after the "
          "2026 one is stored as editions only (live byte for byte as "
          "before), reported as held, not live, and left alone by load, "
          "--recheck and refresh-latest; --acknowledge of it halts; its own "
          "reissue is stored as its edition 2 and still does not reach "
          "live; the CHECK refuses it in live; an empty live table and a "
          "held release in live are caught" if ok
          else f"{out} {problems[:3]}", cur, real_not_live)


# --------------------------------------------------------------- gate 15

def gate_15_reissue_and_refresh(cur):
    name = ("a reissue stores edition 2; refresh-latest changes only live "
            "rows, needs --acknowledge, sets source and copies loaded_at")
    problems = []

    def body(e):
        e.seed(e.cur)
        e.file("4 March 2027", overrides=CHANGED)
        e.ok(e.cur, ["load", "--commit"])
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        e.cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{ZZ.editions_table} t WHERE "
                      f"{m.PERIOD} = %s", (P27,))
        held_before = e.cur.fetchone()[0]
        first = tl.live(e.cur)
        want1 = derive_live(m.records(e.cur, ZZ, P, 1))
        reissue = ["--file", e.file(overrides=CHANGED, register=False),
                   "--no-page"]
        rc, text, _ = e.cmd(["load"] + reissue + ["--commit"])
        if rc != 1 or "--acknowledge" not in text:
            problems.append("a reissue changing pairs was stored without "
                            f"--acknowledge: rc {rc}")
        rc, text, _ = e.cmd(["load"] + reissue + ["--commit",
                                                  "--accept-reissue", P,
                                                  "--acknowledge", P])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N_ED), (2, 1, N_ED)]:
            problems.append(f"the reissue did not store edition 2 of {P}: "
                            f"rc {rc}")
        if tl.live(e.cur) != first:
            problems.append("the reissue reached live before refresh-latest")
        if m.status(e.cur, m.profile())["pending_refresh"] != [P]:
            problems.append("status does not show the refresh pending")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called \
                or "--acknowledge" not in text:
            problems.append("the refresh-latest preview wrote or did not "
                            "name --acknowledge")
        ok, _ = e.halts(["refresh-latest", "--commit"], snap, "--acknowledge")
        if not ok:
            problems.append("refresh-latest --commit without --acknowledge "
                            "changed live")
        rc, text, logged = e.cmd(["refresh-latest", "--commit",
                                  "--acknowledge", P])
        if rc != 0 or not logged.called \
                or "ACKNOWLEDGED" not in logged.call_args[0][2]:
            problems.append(f"refresh-latest --commit --acknowledge: rc "
                            f"{rc}: {text[-120:]}")
        want2 = derive_live(m.records(e.cur, ZZ, P, 2))
        now = tl.live(e.cur)
        if now != want2 or now == first:
            problems.append("live does not hold LIVE_RULE's rows of "
                            "edition 2")
        changed = {k for k in set(now) | set(first)
                   if now.get(k) != first.get(k)}
        expect = {k for k in set(want1) | set(want2)
                  if want1.get(k) != want2.get(k)}
        if changed != expect or not changed:
            problems.append(f"refresh-latest changed {len(changed)} pairs, "
                            f"the editions differ in {len(expect)}")
        e.cur.execute(f"SELECT DISTINCT loaded_at FROM public."
                      f"{ZZ.editions_table} WHERE {m.PERIOD} = %s AND "
                      "edition = 2", (P,))
        ed2 = [r[0] for r in e.cur.fetchall()]
        e.cur.execute(f"SELECT DISTINCT loaded_at FROM public.{ZZ.live_table}")
        if len(ed2) != 1 or e.cur.fetchall() != [(ed2[0],)]:
            problems.append("loaded_at was not copied from edition 2 onto "
                            "every live row")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE {m.PERIOD} = %s AND "
                      "edition = 2", (P,))
        label = e.cur.fetchone()[0]
        e.cur.execute(f"SELECT DISTINCT source FROM public.{ZZ.live_table}")
        if e.cur.fetchall() != [(label,)]:
            problems.append("live source is not the tip's label on every "
                            "row")
        e.cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{ZZ.editions_table} t WHERE "
                      f"{m.PERIOD} = %s", (P27,))
        if e.cur.fetchone()[0] != held_before:
            problems.append("the held edition's rows changed")
        e.cur.execute(f"SELECT DISTINCT {m.PERIOD}::text FROM public."
                      f"{ZZ.live_table}")
        if e.cur.fetchall() != [(P,)]:
            problems.append("live holds a release other than 2026-03-18")
        if not m.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("a second refresh-latest plans a write")
        # a drifted live row is never overwritten
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET neighbour_rank = "
                      "9 WHERE lad24cd = 'E06000001' AND neighbour_rank = 5")
        snap = state(e.cur)
        ok, _ = e.halts(["refresh-latest", "--commit", "--acknowledge", P],
                        snap, "drift")
        if not ok:
            problems.append("a drifted live release was overwritten")
    scenario(cur, body)
    report(15, name, not problems, "a reissue (equal Cover date) is stored "
           "as edition 2 only with --accept-reissue and --acknowledge and "
           "waits for refresh-latest (the preview writes nothing and names "
           "--acknowledge; the commit needs it); the commit changes exactly "
           "the pairs and ranks the editions differ in, sets one source "
           "label on every row, copies the edition's loaded_at, leaves the "
           "held 2027 edition and the one-release live table as they were; "
           "a second refresh plans nothing; a drifted live release is "
           "never overwritten" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 16

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


_HASH = (r"rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
         r"sha256-last32=([0-9a-f]{32})\s*$")


def _note_lines(note, label, release=True):
    """{release: (rows, sha256)} (or {None: ...} when the label carries no
    release) from lines `<label> la_statistical_neighbours [<release>]
    rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the decision
    note (the two halves joined), or {}. Another label does not match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    rel = r"([0-9]{4}-[0-9]{2}-[0-9]{2}) " if release else ""
    out = {}
    for mt in re.finditer(rf"(?m)^{re.escape(label)} {TABLE} {rel}{_HASH}",
                          text):
        g = mt.groups()
        out[g[0] if release else None] = (int(g[-3]), g[-2] + g[-1])
    return out


def note_hashes(note):
    return _note_lines(note, "before-state")


def all_note_hashes(note):
    return _note_lines(note, "before-state-all")


def live_read_hashes(note):
    return _note_lines(note, "live-read", release=False)


def pairs_sha(rows, with_release=None):
    """sha256 of (lad24cd, statistical_neighbour_lad24cd, [release,]
    neighbour_rank) lines sorted, LF-joined, UTF-8."""
    lines = sorted("|".join([r[0], r[1]] + ([with_release] if with_release
                                            else []) + [str(r[2])])
                   for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def live_rows_as_read(cur, spec):
    cur.execute(f"SELECT lad24cd, statistical_neighbour_lad24cd, "
                f"neighbour_rank FROM public.{spec.live_table}")
    return cur.fetchall()


def live_all_sha(cur, spec=REAL):
    """sha256 of every stored column of the live table but the added
    release_date: lad24cd|statistical_neighbour_lad24cd|neighbour_rank|
    source|loaded_at (UTC, NULL as ''), sorted, LF-joined."""
    cur.execute(f"SELECT lad24cd, statistical_neighbour_lad24cd, "
                f"neighbour_rank, source, loaded_at AT TIME ZONE 'UTC' "
                f"FROM public.{spec.live_table}")
    lines = sorted("|".join("" if v is None else str(v) for v in r)
                   for r in cur.fetchall())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def note_lines_for(cur, spec=REAL):
    """The decision note's hash lines for the live table as it stands
    (read-only): before-state (the held live rows with their release),
    before-state-all (every stored column but release_date) and live-read
    (what the readers read). Print them before the migration."""
    rows = live_rows_as_read(cur, spec)
    n = len(rows)
    return [
        f"before-state {TABLE} {m.LIVE_RELEASE} rows={n} "
        f"{hash_fields(pairs_sha(rows, m.LIVE_RELEASE))}",
        f"before-state-all {TABLE} {m.LIVE_RELEASE} rows={n} "
        f"{hash_fields(live_all_sha(cur, spec))}",
        f"live-read {TABLE} rows={n} {hash_fields(pairs_sha(rows))}"]


def real_edition1_hash(cur, spec=REAL, note=NOTE, legacy_rows=None):
    """Edition 1 'as loaded' of the migrated release, LIVE_RULE applied,
    equals the before-state line in the decision note: the row count and
    the hash of (lad24cd, neighbour, release, neighbour_rank), and the row
    count is the surveyed live count; the ranks stored in edition 1 are the
    same rows."""
    _need(cur, spec)
    legacy_rows = m.LEGACY_LIVE[0] if legacy_rows is None else legacy_rows
    recorded = note_hashes(note)
    bad, parts = [], []
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    cur.execute(f"SELECT DISTINCT {spec.period_col}::text FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "release_label = %s ORDER BY 1", (core.AS_LOADED_LABEL,))
    migrated = [r[0] for r in cur.fetchall()]
    if not migrated:
        bad.append("no edition 1 'as loaded' (migrate-legacy not run)")
    for p in migrated:
        recs = m.records(cur, spec, p, 1)
        rows = [(x["lad24cd"], x["statistical_neighbour_lad24cd"],
                 x[m.RANK]) for x in m.live_rows(recs)]
        n, h = len(rows), pairs_sha(rows, p)
        stored = [(x["lad24cd"], x["statistical_neighbour_lad24cd"],
                   x[m.RANK]) for x in recs if x[m.RANK] is not None]
        if pairs_sha(stored, p) != h:
            bad.append(f"{p}: the live ranks stored in edition 1 are not "
                       "LIVE_RULE's rows")
        if n != legacy_rows:
            bad.append(f"{p}: edition 1 gives {n} live rows, surveyed "
                       f"{legacy_rows}")
        if Path(note).exists():
            if p not in recorded:
                bad.append(f"the decision note records no 'before-state "
                           f"{TABLE} {p} rows=.. sha256-first32=.. "
                           "sha256-last32=..' line")
            elif recorded[p] != (n, h):
                bad.append(f"{p}: edition 1 {n} live rows sha256 {h[:16]}.. "
                           f"differs from the note's {recorded[p][0]} rows "
                           f"{recorded[p][1][:16]}..")
        parts.append(f"{p} {n:,} live rows {h[:16]}..")
    for p in recorded:
        if p not in migrated:
            bad.append(f"the note has a line for {p}, which has no edition "
                       "1 as loaded")
    return not bad, "; ".join(bad[:3]) if bad else (
        "edition 1 of every migrated release, LIVE_RULE applied, hashes to "
        "the before-state line in the decision note (" + "; ".join(parts)
        + ")")


def gate_16_before_state_hash(cur):
    name = ("edition 1 (LIVE_RULE applied) equals the before-state line "
            "recorded in the decision note")
    out = {}

    def body(e, path, files):
        legacy = e.legacy(e.cur)
        lines = note_lines_for(e.cur, ZZ)
        e.migrate(e.cur, path, files, legacy=legacy)
        recs = m.records(e.cur, ZZ, P, 1)
        rows = [(x["lad24cd"], x["statistical_neighbour_lad24cd"],
                 x[m.RANK]) for x in m.live_rows(recs)]
        h = pairs_sha(rows, P)
        out["same"] = h == pairs_sha(live_rows_as_read(e.cur, ZZ), P)
        out["printed"] = lines
        out["scan_safe"] = not any(re.search(r"[0-9a-f]{64}", x)
                                   for x in lines)
        out["three"] = len(lines) == 3 and len({
            x.split(" sha256-first32=")[1] for x in lines}) >= 2
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = lines[0]
            note.write_text("intro\n" + "\n".join(lines) + "\n",
                            encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZZ, note, N_LIVE)
            note.write_text(note.read_text().replace(h[:32], "0" * 32),
                            encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZZ, note, N_LIVE)
            note.write_text(lines[1] + "\n" + lines[2] + "\n",
                            encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, N_LIVE)
            note.write_text(line.replace(f"rows={N_LIVE}",
                                         f"rows={N_LIVE + 1}"),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, N_LIVE)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                N_LIVE)
            note.write_text(line, encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(e.cur, ZZ, note,
                                                     N_LIVE + 5)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET neighbour_rank = "
                      "9 WHERE lad24cd = 'E06000001' AND neighbour_rank = 5")
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
        return report(16, name, False, _first(ex))
    ok = (out.get("same") and out.get("scan_safe") and out.get("three")
          and out.get("ok", (0,))[0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and out.get("changed")
          and out.get("twice"))
    mixed(16, name, ok,
          "a seeded migration stores an edition 1 whose LIVE_RULE rows hash "
          "to the held live rows and the printed note line (scan-safe: no "
          "64-hex run); a wrong hash, a wrong row count, a missing line "
          "(before-state-all and live-read lines do not count) and a "
          "missing note fail; a later change shows; a second migration is "
          "refused" if ok else f"seeded: {out}", cur, real_edition1_hash)


# --------------------------------------------------------------- gate 17

def real_live_read(cur, spec=REAL, note=NOTE, rows=None):
    """Live as it stands equals the decision note's lines taken before any
    change: live-read (count and hash of lad24cd, statistical_neighbour_
    lad24cd, neighbour_rank: what the council briefing and the Liverpool
    paper read) and before-state-all (every stored column but
    release_date), so the live rows are identical before and after."""
    _need(cur, spec)
    rows = m.LEGACY_LIVE[0] if rows is None else rows
    if not Path(note).exists():
        return False, f"decision note {Path(note).name} not written"
    read, alls = live_read_hashes(note).get(None), all_note_hashes(note).get(
        m.LIVE_RELEASE)
    bad = []
    if read is None:
        bad.append(f"the decision note records no 'live-read {TABLE} "
                   "rows=.. sha256-first32=.. sha256-last32=..' line")
    if alls is None:
        bad.append(f"the decision note records no 'before-state-all {TABLE} "
                   f"{m.LIVE_RELEASE} rows=.. sha256-first32=.. "
                   "sha256-last32=..' line")
    live = live_rows_as_read(cur, spec)
    n, h = len(live), pairs_sha(live)
    if n != rows:
        bad.append(f"live has {n} rows, expected {rows}")
    if read is not None and read != (n, h):
        bad.append(f"live {n} rows sha256 {h[:16]}.. differs from the "
                   f"note's live-read {read[0]} rows {read[1][:16]}..")
    ha = live_all_sha(cur, spec)
    if alls is not None and alls != (n, ha):
        bad.append(f"live {n} rows (every column but release_date) sha256 "
                   f"{ha[:16]}.. differs from the note's before-state-all "
                   f"{alls[0]} rows {alls[1][:16]}..")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live ({n:,} rows) hashes to the note's live-read line ({h[:16]}..) "
        f"and, on every stored column, to its before-state-all line "
        f"({ha[:16]}..)")


def gate_17_live_read(cur):
    name = ("live equals the decision note's live-read and before-state-all "
            "lines (live rows identical before and after)")
    out = {}

    def body(e):
        e.seed(e.cur)
        lines = note_lines_for(e.cur, ZZ)
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            note.write_text("intro\n" + "\n".join(lines) + "\n",
                            encoding="utf-8")
            kw = dict(note=note, rows=N_LIVE)
            out["base"] = real_live_read(e.cur, ZZ, **kw)
            # the release_date column is not a stored column of the held
            # table: removing it leaves both hashes as they were
            e.cur.execute("SAVEPOINT x")
            e.cur.execute(f"UPDATE public.{ZZ.live_table} SET source = "
                          "'other'")
            out["source"] = real_live_read(e.cur, ZZ, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT x")
            e.cur.execute("SAVEPOINT x")
            e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                          "now() + interval '1 day'")
            out["loaded"] = real_live_read(e.cur, ZZ, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT x")
            e.cur.execute("SAVEPOINT x")
            e.cur.execute(f"UPDATE public.{ZZ.live_table} SET neighbour_rank "
                          "= 9 WHERE lad24cd = 'E06000001' AND "
                          "neighbour_rank = 5")
            out["rank"] = real_live_read(e.cur, ZZ, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT x")
            e.cur.execute("SAVEPOINT x")
            e.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                          "statistical_neighbour_lad24cd = 'E06000009' WHERE "
                          "lad24cd = 'E06000001' AND neighbour_rank = 5")
            out["pair"] = real_live_read(e.cur, ZZ, **kw)
            e.cur.execute("ROLLBACK TO SAVEPOINT x")
            for key, text in (("no_line", "intro\n"),
                              ("only_one", lines[2] + "\n"),
                              ("other_label", lines[2].replace(
                                  "live-read", "live-read-x") + "\n" + lines[1]),
                              ("bad_rows", lines[2].replace(
                                  f"rows={N_LIVE}", f"rows={N_LIVE + 1}")
                               + "\n" + lines[1])):
                note.write_text(text, encoding="utf-8")
                out[key] = real_live_read(e.cur, ZZ, **kw)
            out["no_note"] = real_live_read(e.cur, ZZ, note=Path(tmp) / "x",
                                            rows=N_LIVE)
            out["scan_safe"] = not any(re.search(r"[0-9a-f]{64}", x)
                                       for x in lines)
    scenario(cur, body)
    ok = (out["base"][0] and not out["source"][0] and not out["loaded"][0]
          and not out["rank"][0] and "live-read" in out["rank"][1]
          and not out["pair"][0] and not out["no_line"][0]
          and not out["only_one"][0] and not out["other_label"][0]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and out["scan_safe"])
    mixed(17, name, ok, "seeded: the read passes on the live rows the note "
          "was taken from; a changed rank or pair fails both lines, a "
          "changed source or loaded_at fails before-state-all only; a "
          "missing, single or other-labelled line, a wrong row count and a "
          "missing note fail" if ok else f"{out}", cur, real_live_read)


# --------------------------------------------------------------- gate 18

def _cell_diffs(held, built):
    """(cells, differences): every content column of every record."""
    h = {m._key(r): r for r in held}
    b = {m._key(r): r for r in built}
    diffs = [f"{'/'.join(k)} only in {'edition 1' if k in h else 'the file'}"
             for k in sorted(set(h) ^ set(b))]
    cells = 0
    for k in sorted(set(h) & set(b)):
        for c in m.CONTENT:
            cells += 1
            if h[k].get(c) != b[k].get(c):
                diffs.append(f"{'/'.join(k)} {c} {h[k].get(c)!r} != "
                             f"{b[k].get(c)!r}")
    return cells, diffs


def real_migration_proof(cur, spec=REAL, files=None, raw_dir=None,
                         subjects=SUBJECTS):
    """The migration proof re-run read-only from the file on disk (found by
    sha256): the parser on the held file reproduces every value of edition
    1 'as loaded' (every content column; 0 differences), and LIVE_RULE
    applied to the file's records and to edition 1 gives the same pairs and
    ranks."""
    _need(cur, spec)
    files = m.LEGACY_FILE if files is None else files
    p = held_file(files, raw_dir)
    if p is None:
        return False, (f"no file in {raw_dir or m.RAW_DIR} with sha256 "
                       f"{files['sha256'][:16]}")
    f = m.read_file(p)
    with mock.patch.object(m, "EXPECTED_SUBJECTS", subjects):
        built = _built(cur, f)
    period = f["release_date"]
    held = m.records(cur, spec, period, 1)
    if not held:
        return False, f"{period}: no edition 1"
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (period,))
    if cur.fetchall() != [(core.AS_LOADED_LABEL,)]:
        return False, f"{period}: edition 1 is not the 'as loaded' one"
    cells, diffs = _cell_diffs(held, built)
    if derive_live(held) != derive_live(built):
        diffs.append("LIVE_RULE gives other pairs or ranks for the file than "
                     "for edition 1")
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{cells:,} values of edition 1 ({len(held):,} rows) "
                       f"equal {p.name} read now (0 differences); LIVE_RULE "
                       "gives the same pairs and ranks for both")


def gate_18_migration_proof(cur):
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
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw, subjects=N)
        if not ok or "0 differences" not in detail:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(e.cur, ZZ, {**files,
                                                      "sha256": "0" * 64},
                                          raw, subjects=N)
        if ok or "no file" not in detail:
            problems.append("a file with another sha256 was accepted")
        (raw / path.name).write_bytes(path.read_bytes() + b"\0")
        ok, detail = real_migration_proof(e.cur, ZZ, files, raw, subjects=N)
        if ok or "no file" not in detail:
            problems.append("a changed file on disk was accepted")
        other = write_s21(Path(e.root) / "raw2" / path.name,
                          overrides=CHANGED)
        ok, detail = real_migration_proof(
            e.cur, ZZ, {**files, "sha256": m.content_sha256(other)},
            other.parent, subjects=N)
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
        f"UPDATE public.{live} SET neighbour_rank = 6 WHERE lad24cd = "
        "'E06000001' AND neighbour_rank = 5", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET statistical_neighbour_lad24cd = "
        "'E06000009' WHERE lad24cd = 'E06000002' AND neighbour_rank = 5",
        "proof failed"))
    legacy_world(cur, planted(
        f"DELETE FROM public.{live} WHERE lad24cd = 'E06000002' AND "
        "neighbour_rank = 5", "not as surveyed", surveyed_first=True))
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET loaded_at = now() WHERE lad24cd = "
        "'E06000002' AND neighbour_rank = 5", "loaded_at"))
    mixed(18, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the file by sha256; a changed or missing file and a file that "
          "is not edition 1 are caught; a planted changed rank or pair, a "
          "dropped row and a second loaded_at stop migrate-legacy with "
          "nothing stored" if not problems else "; ".join(problems[:3]),
          cur, real_migration_proof)


# ------------------------------------------------------ gates 19 and 20

def gate_19_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        path = e.file()
        e.ok(e.cur, ["load", "--commit"])
        snap, led = tables_state(e.cur), len(tl.ledger(e.cur))
        for argv in (["load", "--commit"],
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
        if rc != 0 or eds != [(1, None, N_ED)]:
            problems.append(f"a recheck stored an edition: {eds}")
        e.ok(e.cur, ["ddl", "--commit"])
        if tables_state(e.cur) != snap:
            problems.append("a second ddl --commit changed the tables")
    scenario(cur, body)
    report(19, name, not problems, "a second load by the page, --file and "
           "--file --no-page, a refresh-latest and a second ddl stored no "
           "edition and changed no live row (re-reads add 'unchanged' "
           "ledger rows only); a ledger-skipped load logs nothing"
           if not problems else "; ".join(problems[:3]))


def gate_20_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.live(e.cur)
        e.file(overrides=CHANGED)
        e.ok(e.cur, ["load", "--commit", "--accept-reissue", P,
                     "--acknowledge", P])
        e.ok(e.cur, ["refresh-latest", "--commit", "--acknowledge", P])
        second = tl.live(e.cur)
        snap = state(e.cur)
        rc, text, logged = e.cmd(["restore-edition", P, "1"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("restore-edition preview wrote")
        rc, text, logged = e.cmd(["restore-edition", P, "1", "--commit"])
        if rc != 0 or tl.editions(e.cur)[-1] != (3, 2, N_ED) or \
                not logged.called:
            problems.append(f"restore --commit: rc {rc}: {text[-120:]}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE edition = 3")
        if e.cur.fetchall() != [("restored from edition 1",)]:
            problems.append("the restored edition is not labelled")
        if tl.live(e.cur) != second:
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit", "--acknowledge", P])
        if tl.live(e.cur) != first:
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
        if not m.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
    scenario(cur, body)
    report(20, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') and applied by refresh-latest "
           "(live back to the first rows); the tip and a missing edition "
           "are refused; a preview writes nothing"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 21

def secret_values():
    """Values of environment/.env entries that look like secrets (names with
    PASSWORD, KEY, TOKEN or SECRET), for the leak check only. Never printed."""
    out = {}
    for src in (ENV, os.environ):
        for k, v in src.items():
            if (re.search(r"PASSWORD|KEY|TOKEN|SECRET", k.upper())
                    and isinstance(v, str) and len(v.strip()) >= 8):
                out[k.upper()] = v.strip()
    return out


def old_build_problems(text, strict=False):
    """Problems in a module's source (an AST check): an import of the old
    build, or a call (importlib, __import__, runpy, subprocess, os.system,
    exec, eval) with the old build's name among its arguments. strict (the
    verify script and the tests): also any string constant, other than a
    docstring, that names it."""
    tree = ast.parse(text)
    doc = _docstring_ids(tree)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if OLD_BUILD in a.name:
                    bad.append(f"line {node.lineno}: imports {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if OLD_BUILD in (node.module or "") or any(
                    OLD_BUILD in a.name for a in node.names):
                bad.append(f"line {node.lineno}: imports from {node.module}")
        elif isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            runs = re.search(r"(importlib|__import__|runpy|subprocess|"
                             r"os\.(system|exec|spawn|popen)|^exec$|^eval$|"
                             r"^compile$)", name)
            if runs and any(isinstance(c, ast.Constant)
                            and isinstance(c.value, str)
                            and OLD_BUILD in c.value
                            for c in ast.walk(node)):
                bad.append(f"line {node.lineno}: {name} names the old "
                           "build")
        elif strict and isinstance(node, ast.Constant) \
                and isinstance(node.value, str) and id(node) not in doc \
                and OLD_BUILD in node.value:
            bad.append(f"line {node.lineno}: a string names the old build")
    return bad


# the files whose source the gate reads: the verify script, the loader and
# the three test modules the gates run through
OLD_BUILD_FILES = (("s21_neighbours_editions_verify.py", True),
                   ("s21_neighbours_editions.py", False),
                   ("tests/test_s21_neighbours_pure.py", True),
                   ("tests/test_s21_neighbours_loader.py", True),
                   ("tests/test_s21_verify_gates.py", True))


def real_snapshot(cur):
    """Row counts of the real relations and a hash of the live rows, None
    where a relation does not exist."""
    out = {}
    for t in (REAL.live_table, REAL.editions_table, ledger_name(REAL)):
        cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
        if cur.fetchone()[0] is None:
            out[t] = None
            continue
        cur.execute(f"SELECT COUNT(*), md5(string_agg(x::text, ',' ORDER BY "
                    f"x::text)) FROM public.{t} x")
        out[t] = tuple(cur.fetchone())
    return out


def gate_21_no_network_no_secrets(cur):
    name = ("no network, no secret in source or output, nothing written "
            "outside the rolled-back transaction, no old build imported or "
            "run; a download never overwrites a same-named file with "
            "different content")
    files = [Path(__file__).resolve(), LOADER, HERE / "period_editions.py",
             HERE / "geography.py", HERE / "tests" / "test_s21_verify_gates.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]
    snap_before = real_snapshot(cur)

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
                body_a = write_s21(e.root / "dl" / "a.xlsx").read_bytes()
                body_b = write_s21(e.root / "dl" / "b.xlsx",
                                   overrides=CHANGED).read_bytes()
                url = "https://assets.example/media/3/f.xlsx"
                with contextlib.redirect_stdout(io.StringIO()), \
                        mock.patch.object(m, "MIN_FILE_BYTES", 10):
                    p1, _ = m.fetch(url, raw, tp._Session(body_a, url))
                    sha1 = m.content_sha256(p1)
                    p2, _ = m.fetch(url, raw, tp._Session(body_b, url))
                    p3, _ = m.fetch(url, raw, tp._Session(body_a, url))
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
    snap_after = real_snapshot(cur)
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files
             if f.exists()}
    literal = [n for n, t in texts.items() if pat.search(t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    old = []
    for rel, strict in OLD_BUILD_FILES:
        p = HERE / rel
        if p.exists():
            old += [f"{rel}: {x}" for x in old_build_problems(
                p.read_text(encoding="utf-8"), strict)]
    imported = [k for k in sys.modules if OLD_BUILD in k]
    ok = (not attempts and rc == rc_s == rc_r == rc_l == 0 and kept
          and runs == runs_after and snap_before == snap_after
          and (not REAL_BEFORE or REAL_BEFORE == snap_after)
          and not literal and not in_src and not in_out
          and not old and not imported)
    report(21, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned "
           f"{rc}/{rc_s}/{rc_r}/{rc_l}; a same-named different download "
           f"was saved beside it, the first untouched={kept}; run-log rows "
           f"{runs}->{runs_after}; real live, editions and ledger "
           f"unchanged={snap_before == snap_after and (not REAL_BEFORE or REAL_BEFORE == snap_after)}; "
           f"secret-like literal in source {literal or 'none'}; "
           f"{len(secrets)} secret-named settings checked against "
           f"{len(files)} files and {len(OUTPUT)} output lines: in source "
           f"{in_src or 'none'}, in output {in_out or 'none'}; the old "
           f"build imported or run (AST over {len(OLD_BUILD_FILES)} files, "
           f"and sys.modules): {(old + imported) or 'no'}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_live_rule,
         gate_3_codes, gate_4_counts, gate_5_nulls, gate_6_identity,
         gate_7_blanks_and_markers, gate_8_older_file,
         gate_9_stop_conditions, gate_10_first_release, gate_11_ledger_rule,
         gate_12_preview, gate_13_byte_identical_reread,
         gate_14_second_edition, gate_15_reissue_and_refresh,
         gate_16_before_state_hash, gate_17_live_read,
         gate_18_migration_proof, gate_19_rerun, gate_20_restore,
         gate_21_no_network_no_secrets)


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
    """zz% relations (tables, views, indexes, sequences) and functions in
    the real database, read on a separate read-only connection after the
    rollback."""
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 'relation ' || relname FROM pg_class WHERE "
                        "relname LIKE 'zz%%' AND relnamespace = "
                        "'public'::regnamespace UNION ALL SELECT "
                        "'function ' || proname FROM pg_proc WHERE proname "
                        "LIKE 'zz%%' AND pronamespace = 'public'::regnamespace"
                        " UNION ALL SELECT 'type ' || typname FROM pg_type "
                        "WHERE typname LIKE 'zz%%' AND typnamespace = "
                        "'public'::regnamespace AND typrelid = 0 AND "
                        "typelem = 0 ORDER BY 1")
            return sorted(r[0] for r in cur.fetchall())
    finally:
        conn.rollback()
        conn.close()


def committed_snapshot():
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            return real_snapshot(cur)
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
        sys.exit("usage: s21_neighbours_editions_verify.py "
                 "[--print-note-lines]")
    before = committed_snapshot()
    conn = get_conn()
    try:
        with conn.cursor() as raw:
            cur = SafeCur(raw)
            REAL_BEFORE.update(real_snapshot(cur))
            setup_throwaway(cur)
            for gate in GATES:
                run_gate(cur, gate)
    finally:
        conn.rollback()
        conn.close()
    left = leftovers()
    mine = [t for t in left if " zz_s21" in t]
    if mine:
        print(f"LEFTOVER: committed zz_s21 objects in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s21 table, view, index or function left in the "
              "database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% objects exist in the database (not made by "
              f"this run): {others}")
    if committed_snapshot() != before:
        print("REAL TABLES CHANGED: the committed state of the live table, "
              "the editions or the ledger differs from before this run")
        RESULTS.append(False)
    else:
        print("the committed live table, editions and ledger are as they "
              "were before this run")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
