"""Gates for the S17 (SafeLives Marac data, by police force area) editions
tables.

Mirrors scripts/s13_lahs_editions_verify.py and s12_financial_stress_editions_
verify.py. Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes
nothing, not even a run-log row. The real-table gates read marac_cases,
marac_cases_editions, marac_cases_editions_file_checks, la_pfa_mapping, the
eight held SafeLives workbooks and the ONS lookup JSON in data/raw/s17_marac
and the decision note. Until the editions table exists (ddl and migrate-legacy
not yet run) the gates that need it print `FAIL (pending migration)` with the
words "not yet migrated", so the script exits non-zero until then, like the
other verifiers; nothing is created to make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s17_live / zz_s17_editions / zz_s17_editions_file_checks
(a copy of the loader's spec), created inside the transaction, and call the
loader's own writer and commands (s17_marac_editions.main with the SPEC
pointed at the copy, the data page and the downloads stubbed; apply_year;
migrate_legacy; restore_edition) rather than a re-implementation. The real
tables are only ever read. Seeded parts use the loader tests' fixture
workbooks (a handful of forces, written to a temporary folder, which is the
only allowed --file root); they never rely on a private constant of the
loader.

The rules this script holds the loader to (Scott, 2026-10-10):
  * exactly two named zero rules, NORFOLK_2023_24_NOT_SUBMITTED and
    WEST_MIDLANDS_2023_24_NOT_SUBMITTED; no Lancashire rule (Lancashire's
    housing zeros stay as published);
  * 39 English police force areas (la_pfa_mapping.pfa_name_safelives); edition
    1 'as loaded' holds the 38 the live table held, West Midlands arrives as a
    key added in edition 2 and refresh-latest needs --accept-key-changes;
  * the first load is released by the named entry s17-restored-zeros-2026-10
    (13 NULL-to-0 cells), and nothing goes from a value to NULL;
  * the map year 2025-26: the 38 held forces are unchanged in cases_discussed
    and cases_per_10k_adult_females, and the seven West Midlands councils
    gain values (cases 7,810; rate 65.903502).

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), the value_flag CHECK, live shape, no editions-only column on
     live                                               (throwaway + real)
  2  every live period has edition 1; the latest edition equals live cell for
     cell on the key, the year and the eight values         (seeded + real)
  3  geography: la_pfa_mapping covers 296 authorities and 39 forces and
     equals the ONS lookup file on disk on pfa_name (one documented spelling
     difference); every year's latest edition holds exactly the mapped
     forces, edition 1 the 38 held; a missing, unknown or Welsh force halts
                                                            (seeded + real)
  4  rule 1: every NULL in the latest edition carries a value_flag, a
     published 0 stays 0 except under the two named rules (the held files
     re-read), the named entry is NULL-to-0 only, the loader source (AST) has
     no other zero rule, no literal force compared with 0, no value coerced
     to 0, no Lancashire rule                               (seeded + real)
  5  reconciliation re-run read-only from the files on disk
  6  identity: the held files' titles and sha256; seeded title, header,
     sheet and link mismatches halt; Housing is found by its header text
  7  older-file and reissue guards, on every path; the rank is the file's own,
     never its name                                         (seeded + real)
  8  stop conditions: each seeded REJECTED, stores nothing, no ledger row,
     partial run-log row; the limits are exact; the eight held years pass
  9  a new period: edition, live rows and ledger row in one transaction
 10  ledger rule: a skip needs the (source, sha256) pair
 11  preview and simulate write nothing, every writing command
 12  a byte-identical re-read is `unchanged` on every path, against edition 1
     'as loaded' too                                        (seeded + real)
 13  refresh-latest changes only the revised years and rows, copies
     loaded_at; key changes only with --accept-key-changes
 14  edition 1 equals the decision note's before-state lines (seeded + real)
 15  the map year 2025-26 equals the decision note's w1-read line on the
     held forces, and the West Midlands force gains its values
                                                            (seeded + real)
 16  the migration proof re-run from the files on disk       (seeded + real)
 17  rerun idempotent
 18  stranded year repair
 19  restore-edition round trip
 20  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content

The decision note's hash lines are scan-safe: a 64-hex run would be flagged by
the credential scan, so a sha256 is written as two 32-hex halves in two
labelled fields:
    before-state marac_cases <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read marac_cases 2025-26 rows=38 sha256-first32=<32 hex> sha256-last32=<32 hex>
A before-state line hashes a year's live rows before the migration (one line
per force sorted by name: name|the eight values, NULL as ''; not the live
source, which edition 1 does not carry), LF-joined. The w1-read line hashes
what W1 reads of the map year over the 38 HELD forces (name|cases_discussed|
cases_per_10k_adult_females, NULL as ''), so it is the same before and after
West Midlands is added. `python scripts/s17_marac_editions_verify.py
--print-note-lines` prints them for the live table as it stands (read-only);
run it before migrate-legacy and paste the lines into the decision note.

Usage:
    python scripts/s17_marac_editions_verify.py
    python scripts/s17_marac_editions_verify.py --print-note-lines

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
import shutil
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
import s17_marac_editions as m  # noqa: E402
import test_s17_marac_loader as tl  # noqa: E402
from test_s17_marac_pure import (HEADER, PFAS, Resp, Session,  # noqa: E402
                                 fixture_values, write_marac)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real spec and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPEC, which the seeded gates point
# at the copy)
REAL = m.SPEC
ZZ = tl.ZZ
LEDGER = tl.LEDGER
N = tl.N
Y = tl.Y
Y0 = tl.Y0
LOADER = HERE / "s17_marac_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-10-s17-editions-first-load.md")
ONS_JSON = (HERE.parent / "data" / "raw" / "s17_marac"
            / "LAD24_CSP24_PFA24_EW_LU_2026-10-10.json")
NOT_YET = "the S17 editions table does not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (20)

MAP_YEAR = "2025-26"
ADDED_FORCES = ("West Midlands",)         # in the files, not in the held table
AUTHORITIES = 296
ENGLISH_FORCES = 39
HELD_FORCES = 38
# what the 2025-26 file publishes for West Midlands, and how many councils
# la_pfa_mapping maps to it (they gain values on the map)
WM_MAP = {"cases_discussed": Decimal("7810.00"),
          "cases_per_10k_adult_females": Decimal("65.903502")}
WM_COUNCILS = {"West Midlands": 7}
ZERO_RULE_NAMES = ("NORFOLK_2023_24_NOT_SUBMITTED",
                   "WEST_MIDLANDS_2023_24_NOT_SUBMITTED")
FIRST_LOAD_ENTRY = "s17-restored-zeros-2026-10"
RESTORED_CELLS = 13
# the migration proof on the real files, surveyed 2026-10-10 (Task 4)
SURVEYED_PROOF = {"rows": 304, "restored": 13, "keys_added": 8,
                  "rule_held": 0}
# the one documented spelling difference between la_pfa_mapping.pfa_name and
# the ONS lookup file
ONS_SPELLING = {"E09000001": ("City of London", "London, City of")}
NOT_SUBMITTED = {"marac_count": 0, "cases_discussed": 0,
                 "recommended_cases": 0,
                 "cases_per_10k_adult_females": "No population data",
                 "repeat_cases": 0, "repeat_cases_pct": "#DIV/0!",
                 "children_in_household": 0, "housing_referrals": 0}


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
    only ever used on the zz_s17 tables."""
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


def ledger_name(spec):
    return f"{spec.editions_table}_file_checks"


def exists(cur, spec=REAL):
    return all(pe.table_exists(cur, t)
               for t in (spec.editions_table, ledger_name(spec)))


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
        sys.exit("HARD STOP: a zz_s17 table already exists as a real table; "
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
    """The loader's main(argv) on the throwaway tables, the data page and the
    downloads stubbed; every workbook is written here (the loader tests'
    writers and fixtures)."""

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.links, self.urls, self.final, self.fetched = {}, {}, {}, []
        self.page_refused = False
        self.names = PFAS
        self.n = 0

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

    def register(self, path, label=Y):
        """List `path` on the data page as the link of `label`."""
        y2 = int(label[:4]) + 1
        url = tl.LINK.format(y2 - 1, y2)
        self.links[label] = url
        self.urls[url] = path
        self.last_url = url
        return url

    def revise(self, **kw):
        return self.file(modified="2026-09-01T10:00:00Z", **kw)


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp)


def scenario(cur, fn):
    """Run fn(env) in a savepoint that is rolled back."""
    def body(c):
        with env(c) as e, tl.specs(e.root, e.names):
            return fn(e)
    return _in_savepoint(cur, body)


def legacy_world(cur, fn, **kw):
    """A seeded legacy state (as the n8n load left it: published zeros held
    as NULL, no West Midlands row); fn(env, path, files, label)."""
    def body(c):
        with env(c) as e, tl.specs(e.root, e.names):
            path, files, label = e.seed_legacy(c, **kw)
            return fn(e, path, files, label)
    return _in_savepoint(cur, body)


def migrate_cmd(e, files, *extra, explained=None):
    """migrate-legacy through main on the throwaway tables, with the
    surveyed state and files pointed at the seeded ones."""
    with mock.patch.object(m, "LEGACY_LIVE", m.live_state(e.cur)), \
            mock.patch.object(m, "LEGACY_FILES", files), \
            mock.patch.object(m, "LEGACY_PFA_MAPPING",
                              m.mapping_state(e.cur)), \
            mock.patch.object(m, "LEGACY_EXPLAINED_KEYS",
                              {"West Midlands": "test"} if explained is None
                              else explained):
        return e.cmd(["migrate-legacy", *extra])


def entry_for(label, cells=None):
    """A named flips entry for the seeded legacy world: the restored zeros of
    the fixture (City of London and Lancashire housing referrals)."""
    return {"decided": "test", "why": "test", "periods": {label: cells or {
        "City of London/housing_referrals": (None, "0.00"),
        "Lancashire/housing_referrals": (None, "0.00")}}}


def go_live(e, path, label, entry="t"):
    """After migrate-legacy: load the file as edition 2 under a named flips
    entry, then refresh with its key change."""
    e.register(path, label)
    with mock.patch.dict(m.ACKNOWLEDGED_FLIPS, {entry: entry_for(label)}):
        e.ok(e.cur, ["load", "--commit", "--acknowledge", label,
                     "--acknowledge-flips", entry])
    e.ok(e.cur, ["refresh-latest", "--commit", "--accept-key-changes", label])


_CACHE = {}
_SHAS = {}


def file_sha(path):
    p = Path(path)
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    if key not in _SHAS:
        _SHAS[key] = m.content_sha256(p)
    return _SHAS[key]


def cached_read(path, year=None):
    """The workbook read once per (path, size, mtime, year): reading the
    .xls files takes a moment."""
    p = Path(path)
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns, year)
    if key not in _CACHE:
        _CACHE[key] = m.read_workbook(p, year)
    return _CACHE[key]


def held_file(files, y):
    """(path, workbook) of a held file by year, or (None, problem)."""
    f = files[y]
    p = Path(f["path"])
    if not p.is_file():
        return None, f"{y}: no file {p.name} in {p.parent}"
    if file_sha(p) != f["sha256"]:
        return None, (f"{y}: {p.name} has sha256 {file_sha(p)[:16]}, "
                      f"expected {f['sha256'][:16]}")
    try:
        return p, cached_read(p, y)
    except ValueError as e:
        return None, f"{y}: {p.name}: {_first(e)}"


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "varchar": "character varying",
          "text": "text", "date": "date", "numeric": "numeric",
          "boolean": "boolean"}


def _family(sql):
    return _DTYPE[sql.split()[0].split("(")[0].lower()]


def _columns(cur, table):
    cur.execute("""SELECT column_name, data_type, is_nullable,
                          numeric_precision, numeric_scale
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
            bad.append(f"{spec.editions_table}: {c} is {got[:2]}, expected "
                       f"{ty} {nullable}")
    # the value columns hold the live columns' precision and scale
    for c, ty in m.VALUE_TYPES:
        for table, have in ((spec.editions_table, cols), (spec.live_table,
                                                           lcols)):
            g = have.get(c)
            if g is None:
                continue
            if ty == "integer":
                if g[0] != "integer":
                    bad.append(f"{table}.{c} is {g[0]}, expected integer")
            else:
                p, s = map(int, re.fullmatch(r"numeric\((\d+),(\d+)\)",
                                             ty).groups())
                if g[0] != "numeric" or (g[2], g[3]) != (p, s):
                    bad.append(f"{table}.{c} is {g[0]}({g[2]},{g[3]}), "
                               f"expected {ty}")
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
    for c, _ in m.FLAG_TYPES:
        if c in lcols:
            bad.append(f"{spec.live_table}: has the editions-only column {c}")
    if "live_source" in lcols:
        bad.append(f"{spec.live_table}: has the editions-only column "
                   "live_source")
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
        if got is None or tuple(got[:2]) != (ty, nullable):
            bad.append(f"{led}: {c} is {got and got[:2]}, expected "
                       f"{(ty, nullable)}")
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


def flag_check_problems(cur, spec=ZZ):
    """Problems unless the editions table's value_flag CHECK refuses a flag
    on a row with no NULL value and an unknown flag, and accepts the two
    flags on a NULL row (each insert in a savepoint, rolled back)."""
    cols = [m.KEY, m.PERIOD, "edition"] + list(m.VALUES) + ["value_flag"]
    base = {c: 1 for c in m.VALUES}

    def insert(flag, nulls):
        vals = ["zz-force", "1999-00", 1] + [
            None if c in nulls else base[c] for c in m.VALUES] + [flag]
        cur.execute("SAVEPOINT chk")
        try:
            cur.execute(f"INSERT INTO public.{spec.editions_table} "
                        f"({', '.join(cols)}) VALUES "
                        f"({', '.join(['%s'] * len(cols))})", vals)
            return True
        except psycopg2.Error:
            return False
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT chk")
            cur.execute("RELEASE SAVEPOINT chk")
    bad = []
    if insert("not_submitted", ()):
        bad.append("a value_flag on a row with no NULL value was accepted")
    if insert("bogus", ("marac_count",)):
        bad.append("an unknown value_flag was accepted")
    for flag in m.FLAGS:
        if not insert(flag, ("marac_count",)):
            bad.append(f"value_flag {flag} on a NULL row was refused")
    if not insert(None, ("marac_count",)):
        bad.append("a NULL value without a flag was refused (rule 1.10 is "
                   "checked by gate 4, not by the CHECK)")
    return bad


def gate_1_table_shape_and_immutability(cur):
    name = ("editions table, ledger, live shape (UPDATE, DELETE and TRUNCATE "
            "are refused on the editions and the ledger; the value_flag "
            "CHECK; no editions-only column on live)")
    bad = shape_problems(cur, ZZ)

    def seed(cur):
        with env(cur) as e, tl.specs(e.root, e.names):
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
    bad += flag_check_problems(cur)
    scope = "throwaway copy"
    if exists(cur):
        scope += " and real tables"
        bad += [f"real: {p}" for p in shape_problems(cur, REAL)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise "
           "append-only on the editions table and the ledger; the CHECK "
           "refuses a flag without a NULL and an unknown flag")


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
    cur.execute(f"SELECT DISTINCT {m.PERIOD} FROM public.{spec.live_table} "
                "ORDER BY 1")
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
    cur.execute(f"SELECT DISTINCT {m.PERIOD} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "source_file IS NOT NULL")
    have = {str(r[0]) for r in cur.fetchall()}
    missing = [p for p in periods if p not in have]
    if missing:
        return False, (f"no edition 1 (with a source_file) for "
                       f"{missing[:6]} ({len(missing)} of {len(periods)})")
    cols = ", ".join((m.KEY, m.PERIOD) + m.VALUES)
    bad = []
    for p in periods:
        ed = _tip(cur, spec, p)
        a, b = _both_ways(
            cur, f"SELECT {cols} FROM public.{spec.editions_table} WHERE "
            f"{m.PERIOD} = %s AND edition = %s", (p, ed),
            f"SELECT {cols} FROM public.{spec.live_table} WHERE "
            f"{m.PERIOD} = %s", (p,))
        if a or b:
            bad.append(f"{p} ed{ed}: {a} edition-only, {b} live-only rows")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} live period(s) each have edition 1 and the latest "
        "edition equals live on the force, the year and the eight values, "
        "cell for cell")


def gate_2_edition1_and_latest(cur):
    name = ("every live period has edition 1; the latest edition equals "
            "live cell for cell on the eight values")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_edition1_and_latest(e.cur, ZZ)
        miss = []
        live = ZZ.live_table
        for sql, needle in (
            (f"UPDATE public.{live} SET cases_discussed = cases_discussed "
             "+ 1 WHERE pfa_name_safelives = 'Cumbria'", "edition-only"),
            (f"UPDATE public.{live} SET marac_count = marac_count + 1 "
             "WHERE pfa_name_safelives = 'Cumbria'", "edition-only"),
            (f"UPDATE public.{live} SET repeat_cases_pct = NULL WHERE "
             "pfa_name_safelives = 'Cumbria'", "edition-only"),
            (f"UPDATE public.{live} SET housing_referrals = 0 WHERE "
             "pfa_name_safelives = 'Cumbria'", "edition-only"),
            (f"DELETE FROM public.{live} WHERE pfa_name_safelives = "
             "'Cumbria'", "edition-only"),
            (f"UPDATE public.{live} SET financial_year = '2030-31'",
             "no edition 1"),
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
          "(an amount, an integer, a NULL, a zero, a missing row, a year "
          "without edition 1) are each caught" if ok
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


def _target_names(node):
    names = []
    for t in getattr(node, "targets", None) or [getattr(node, "target", None)]:
        if isinstance(t, ast.Name):
            names.append(t.id)
    return names


def _is_zero(node):
    return (isinstance(node, ast.Constant) and node.value == 0
            and not isinstance(node.value, bool))


# module-level constants that may name Lancashire (the force list and the
# named NULL-to-0 entry) or hold a zero rule
LANCASHIRE_OK = ("PFA_NAMES_2026_10", "ACKNOWLEDGED_FLIPS")


def source_problems(path, pfas, zeros=False):
    """Problems in a module's source (an AST check), each 'line N: ...':
      - a module-level dict naming a force and columns (the shape of a zero
        rule) whose name is not one of the two named rules;
      - 'Lancashire' in a non-docstring string outside PFA_NAMES_2026_10 and
        ACKNOWLEDGED_FLIPS (a Lancashire rule);
      - a function that compares with 0 (==, !=, is) and also holds a
        literal force name (or 'force/column') from `pfas` (a literal force
        compared with 0).
    With zeros=True also a source value coerced to 0 (`x or 0`, `else 0`,
    COALESCE(x, 0), `.get(x, 0)`) on a line without `# not a source value`."""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines()
    doc = _docstring_ids(tree)
    bad = []
    skip = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        names = _target_names(node)
        val = getattr(node, "value", None)
        if isinstance(val, ast.Dict) and not set(names) & set(ZERO_RULE_NAMES):
            keys = {k.value for k in val.keys if isinstance(
                k, ast.Constant) and isinstance(k.value, str)}
            if {"pfa", "columns"} <= keys:
                bad.append(f"line {node.lineno}: {names} is a zero rule "
                           "other than the two named ones")
        if set(names) & set(LANCASHIRE_OK):
            skip.update(id(x) for x in ast.walk(node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc and id(node) not in skip:
            if "lancashire" in node.value.lower():
                bad.append(f"line {node.lineno}: Lancashire named in "
                           f"{node.value[:40]!r} (Lancashire's zeros stay "
                           "as published: there is no Lancashire rule)")
            if zeros and re.search(r"(?i)coalesce\([^)]*,\s*0\s*\)",
                                   node.value):
                bad.append(f"line {node.lineno}: COALESCE(x, 0)")
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        forces = [n for n in ast.walk(fn) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str) and id(n) not in doc
                  and (n.value in pfas
                       or n.value.split("/")[0] in pfas)]
        cmp0 = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
                and any(isinstance(o, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot))
                        for o in n.ops)
                and any(_is_zero(x) for x in [n.left] + n.comparators)]
        if forces and cmp0:
            bad.append(f"line {cmp0[0].lineno}: {fn.name} compares with 0 "
                       f"and holds the literal force {forces[0].value!r}")
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
    return {int(re.match(r"line ([0-9]+)", x).group(1)) for x in problems}


def zero_rule_state():
    """Problems unless the loader's zero rules and named entry are what
    Scott decided (2026-10-10): exactly Norfolk and West Midlands 2023-24,
    each a not_submitted rule on all eight columns in the published form of a
    non-submission, with evidence and who decided; no Lancashire; and the one
    named entry lists only NULL-to-0 changes (13 cells), none to NULL."""
    bad = []
    rules = {r["name"]: r for r in m.ZERO_RULES}
    if tuple(r["name"] for r in m.ZERO_RULES) != ZERO_RULE_NAMES:
        bad.append(f"ZERO_RULES names are "
                   f"{[r['name'] for r in m.ZERO_RULES]}, expected "
                   f"{list(ZERO_RULE_NAMES)}")
    for name, pfa in zip(ZERO_RULE_NAMES, ("Norfolk", "West Midlands")):
        r = rules.get(name)
        if r is None:
            continue
        if (r["pfa"], tuple(r["years"]), r["flag"]) != (
                pfa, ("2023-24",), "not_submitted") \
                or tuple(r["columns"]) != m.VALUES:
            bad.append(f"{name} is not {pfa} 2023-24 not_submitted on all "
                       "eight columns")
        pub = r["published"]
        if pub.get("cases_per_10k_adult_females") != "No population data" \
                or pub.get("repeat_cases_pct") != "#DIV/0!" \
                or any(pub.get(c) != 0 for c in m.VALUES
                       if c not in ("cases_per_10k_adult_females",
                                    "repeat_cases_pct")):
            bad.append(f"{name}: the published form is not the "
                       "non-submission form")
        if not r.get("evidence") or not r.get("decided"):
            bad.append(f"{name} has no evidence or no decided")
    if any("lancashire" in str(r.get("pfa", "")).lower()
           for r in m.ZERO_RULES):
        bad.append("a Lancashire rule exists")
    if "Scott" not in rules.get(ZERO_RULE_NAMES[1], {}).get("decided", ""):
        bad.append("WEST_MIDLANDS_2023_24_NOT_SUBMITTED does not record "
                   "Scott's decision")
    ent = m.ACKNOWLEDGED_FLIPS
    if sorted(ent) != [FIRST_LOAD_ENTRY]:
        bad.append(f"ACKNOWLEDGED_FLIPS names {sorted(ent)}, expected only "
                   f"{FIRST_LOAD_ENTRY}")
    e = ent.get(FIRST_LOAD_ENTRY)
    if e:
        cells = [(p, k, v) for p, d in e["periods"].items()
                 for k, v in d.items()]
        if len(cells) != RESTORED_CELLS:
            bad.append(f"{FIRST_LOAD_ENTRY} lists {len(cells)} cells, "
                       f"expected {RESTORED_CELLS}")
        for p, k, (a, b) in cells:
            if a is not None or b is None or Decimal(b) != 0:
                bad.append(f"{FIRST_LOAD_ENTRY} {p} {k}: {a!r}->{b!r} is "
                           "not a NULL-to-0 change")
        if not e.get("decided") or not e.get("why"):
            bad.append(f"{FIRST_LOAD_ENTRY} has no decided or why")
    return bad


# ---------------------------------------------------------------- gate 3

def mapping_problems(cur, ons=ONS_JSON, rows=AUTHORITIES, forces=ENGLISH_FORCES,
                     spelling=None):
    """Problems unless la_pfa_mapping covers `rows` authorities and `forces`
    English forces (none Welsh), pfa_name_safelives equals pfa_name, and
    pfa_name equals the ONS LAD24_CSP24_PFA24 file on disk for every English
    authority except the documented spelling difference."""
    spelling = ONS_SPELLING if spelling is None else spelling
    cur.execute("SELECT lad24cd, pfa_name, pfa_name_safelives FROM "
                "public.la_pfa_mapping")
    got = cur.fetchall()
    bad = []
    if len(got) != rows or len({r[0] for r in got}) != rows:
        bad.append(f"la_pfa_mapping has {len(got)} rows over "
                   f"{len({r[0] for r in got})} authorities, expected {rows}")
    names = {r[2] for r in got}
    if len(names) != forces:
        bad.append(f"la_pfa_mapping has {len(names)} forces, expected "
                   f"{forces}")
    welsh = sorted(names & set(m.WELSH_FORCES))
    if welsh:
        bad.append(f"la_pfa_mapping maps to Welsh forces {welsh}")
    diff = [r[0] for r in got if r[1] != r[2]]
    if diff:
        bad.append(f"pfa_name_safelives differs from pfa_name for "
                   f"{diff[:3]}")
    p = Path(ons)
    if not p.is_file():
        bad.append(f"no ONS lookup file {p.name} in {p.parent}")
        return bad
    try:
        feats = json.loads(p.read_text(encoding="utf-8"))["features"]
    except (ValueError, KeyError) as e:
        return bad + [f"{p.name} is not the ONS lookup JSON: {_first(e)}"]
    ons_names = {}
    for f in feats:
        a = f["attributes"]
        if a["LAD24CD"].startswith("E"):
            ons_names.setdefault(a["LAD24CD"], set()).add(a["PFA24NM"])
    mine = {r[0]: r[1] for r in got}
    if set(ons_names) != set(mine):
        bad.append(f"authority codes differ from {p.name}: only in ONS "
                   f"{sorted(set(ons_names) - set(mine))[:3]}, only in "
                   f"la_pfa_mapping {sorted(set(mine) - set(ons_names))[:3]}")
    for code in sorted(set(mine) & set(ons_names)):
        if len(ons_names[code]) != 1:
            bad.append(f"{code}: ONS names {sorted(ons_names[code])}")
        elif (mine[code], next(iter(ons_names[code]))) != \
                spelling.get(code, (mine[code], mine[code])):
            bad.append(f"{code}: pfa_name {mine[code]!r} vs ONS "
                       f"{sorted(ons_names[code])}")
    return bad


def real_forces(cur, spec=REAL, names=None, added=ADDED_FORCES,
                held=HELD_FORCES):
    """Every year's latest edition holds exactly the English forces of
    la_pfa_mapping, edition 1 'as loaded' the held ones (less `added`), live
    those or all of them; each in la_pfa_mapping, none Welsh."""
    _need(cur, spec)
    names = frozenset(m.pfa_names(cur) if names is None else names)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, f"{spec.live_table}: {EMPTY}"
    bad = []
    held_names = names - set(added)
    if set(m.WELSH_FORCES) & names:
        bad.append("a Welsh force is among the English forces")
    for p in periods:
        tip = _tip(cur, spec, p)
        got = {r[m.KEY] for r in m.held_records(cur, spec, p, tip)}
        if got != names:
            bad.append(f"{p} ed{tip}: {len(got)} forces, expected "
                       f"{len(names)}; missing {sorted(names - got)[:3]}, "
                       f"extra {sorted(got - names)[:3]}")
        cur.execute(f"SELECT release_label FROM public.{spec.editions_table}"
                    f" WHERE {m.PERIOD} = %s AND edition = 1 LIMIT 1", (p,))
        r1 = cur.fetchone()
        if r1 and r1[0] == core.AS_LOADED_LABEL:
            got1 = {r[m.KEY] for r in m.held_records(cur, spec, p, 1)}
            if got1 != held_names:
                bad.append(f"{p} ed1 as loaded: {len(got1)} forces, "
                           f"expected the {len(held_names)} held ones")
        live = {r[m.KEY] for r in m.held_records(cur, spec, p)}
        if not held_names <= live <= names:
            bad.append(f"{p} live: {len(live)} forces, expected "
                       f"{len(held_names)} to {len(names)} of the mapped "
                       "ones")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} year(s): the latest edition holds the {len(names)} "
        f"mapped forces, edition 1 the {len(held_names)} held ones "
        f"(less {', '.join(added)}), live both or the held ones")


def gate_3_geography(cur):
    name = ("la_pfa_mapping covers 296 authorities and 39 forces and equals "
            "the ONS lookup on disk; every year holds exactly the mapped "
            "forces; a missing, unknown or Welsh force halts")
    problems = []
    if set(ADDED_FORCES) != set(m.LEGACY_EXPLAINED_KEYS):
        problems.append(f"the forces added after the held 38 "
                        f"{sorted(ADDED_FORCES)} are not the loader's "
                        f"explained keys {sorted(m.LEGACY_EXPLAINED_KEYS)}")
    try:
        problems += mapping_problems(cur)
    except psycopg2.Error as e:
        problems.append(f"la_pfa_mapping: {_first(e)}")
    # the mapping check itself catches a planted fault
    with tempfile.TemporaryDirectory() as tmp:
        bad_json = Path(tmp) / "ons.json"
        feats = json.loads(ONS_JSON.read_text(encoding="utf-8"))
        for f in feats["features"]:
            if f["attributes"]["LAD24CD"] == "E06000001":
                f["attributes"]["PFA24NM"] = "Durham"
        bad_json.write_text(json.dumps(feats), encoding="utf-8")
        if not any("E06000001" in x for x in mapping_problems(cur, bad_json)):
            problems.append("a changed ONS force name was not caught")
        if not mapping_problems(cur, rows=AUTHORITIES + 1):
            problems.append("a wrong authority count was not caught")
        if not mapping_problems(cur, spelling={}):
            problems.append("the documented spelling difference is not "
                            "needed (the file changed?)")
    seeded_problems = []

    def body(e):
        e.file()
        base = state(e.cur)
        for label, names, needle in (
                ("a force of the file not in the mapping", PFAS - {"Suffolk"},
                 "not an English police force area"),
                ("a mapped force missing from the file",
                 PFAS | {"Mystery Force"}, "not in the Cases sheet"),
                ("a Welsh force taken as English", PFAS | {"Gwent"},
                 "Welsh")):
            e.names = names
            ok, text = e.halts(["load", "--commit"], base, needle)
            if not ok:
                seeded_problems.append(f"{label}: {text[-120:]}")
        e.names = PFAS
        e.ok(e.cur, ["load", "--commit"])
        ok, detail = real_forces(e.cur, ZZ, names=PFAS, added=())
        if not ok:
            seeded_problems.append(f"complete state: {detail}")
        for sql, needle in (
                (f"DELETE FROM public.{ZZ.live_table} WHERE "
                 "pfa_name_safelives = 'Suffolk'", "live"),
                (f"UPDATE public.{ZZ.live_table} SET pfa_name_safelives = "
                 "'Gwent' WHERE pfa_name_safelives = 'Suffolk'", "live")):
            with planted(e.cur, sql):
                ok, detail = real_forces(e.cur, ZZ, names=PFAS, added=())
            if ok or needle not in detail:
                seeded_problems.append(f"planted {sql[:30]}: {detail[:80]}")
        ok, detail = real_forces(e.cur, ZZ, names=PFAS - {"Suffolk"},
                                 added=())
        if ok or "forces, expected" not in detail:
            seeded_problems.append("a mapping that lacks a stored force "
                                   "passed")
    scenario(cur, body)
    problems += seeded_problems
    mixed(3, name, not problems, "la_pfa_mapping: 296 authorities, 39 "
          "forces, equal to the ONS file but City of London's spelling; "
          "seeded: a force missing from the mapping, one missing from the "
          "file and a Welsh one each halt with nothing stored, and the "
          "stored forces are checked against the mapping" if not problems
          else "; ".join(problems[:3]), cur, real_forces)


# ---------------------------------------------------------------- gate 4

def _tip_rows(cur, spec, p):
    return m.held_records(cur, spec, p, _tip(cur, spec, p))


def real_blanks(cur, spec=REAL, files=None, names=None, rules=True):
    """Rule 1 on the real data: every row of the latest edition with a NULL
    value carries a value_flag (a NULL without a reason is a number the
    parser could not read); and, from the held files re-read, every cell the
    file publishes as 0 is 0 in the latest edition unless one of the two
    named rules turned it into NULL, every marker is NULL with its flag. The
    counts per flag are reported."""
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, f"{spec.live_table}: {EMPTY}"
    bad, flags, nulls = [], {}, 0
    for p in periods:
        for r in _tip_rows(cur, spec, p):
            miss = [c for c in m.VALUES if r[c] is None]
            nulls += len(miss)
            if miss and not r["value_flag"]:
                bad.append(f"{p} {r[m.KEY]}: NULL {', '.join(miss[:2])} with "
                           "no value_flag")
            if r["value_flag"]:
                flags[r["value_flag"]] = flags.get(r["value_flag"], 0) + 1
    if bad:
        bad = [f"{len(bad)} row(s) with a NULL and no value_flag (a "
               "published 0 held as NULL?): " + "; ".join(bad[:3])]
    if files is None and rules:
        files = m.LEGACY_FILES
    zeros = 0
    if files:
        names = m.pfa_names(cur) if names is None else names
        applied = set()
        for y in sorted(files):
            p, wb = held_file(files, y)
            if p is None:
                bad.append(wb)
                continue
            tips = {r[m.KEY]: r for r in _tip_rows(cur, spec, y)}
            recs = m.records(wb, y, names)
            cells = {(a["pfa"], a["column"]) for a in recs.applied}
            applied |= {a["rule"] for a in recs.applied}
            for r in recs:
                t = tips.get(r[m.KEY])
                if t is None:
                    bad.append(f"{y} {r[m.KEY]}: not in the latest edition")
                    continue
                for c in m.VALUES:
                    raw = r["_raw"][c]
                    if (r[m.KEY], c) in cells:
                        if t[c] is not None:
                            bad.append(f"{y} {r[m.KEY]} {c}: a named rule "
                                       "makes it NULL, the edition holds a "
                                       "value")
                    elif isinstance(raw, (int, float)) and raw == 0:
                        zeros += 1
                        if t[c] != 0:
                            bad.append(f"{y} {r[m.KEY]} {c}: published 0, "
                                       f"the latest edition holds {t[c]!r}")
                    elif isinstance(raw, str) and t[c] is not None:
                        bad.append(f"{y} {r[m.KEY]} {c}: published {raw!r}, "
                                   "the edition holds a value")
        if applied != set(ZERO_RULE_NAMES) and rules:
            bad.append(f"the rules applied to the held files are "
                       f"{sorted(applied)}, expected {list(ZERO_RULE_NAMES)}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} year(s): {nulls} NULL cell(s), each row with a "
        f"value_flag ({', '.join(f'{k} {v}' for k, v in sorted(flags.items()))}"
        f" row(s)); {zeros} published zeros in the held files stay 0; only "
        "the two named rules turn a published 0 into NULL")


def gate_4_blanks_and_zeros(cur):
    name = ("rule 1: every NULL carries a value_flag; a published 0 stays 0 "
            "except under the two named rules; the named entry is NULL-to-0 "
            "only; no other zero rule, literal force compared with 0 or "
            "value coerced to 0 in the loader; no Lancashire rule")
    problems = []
    problems += zero_rule_state()
    pfas = set(m.PFA_NAMES_2026_10) | {"Norfolk", "West Midlands"}
    src = source_problems(LOADER, pfas, zeros=True)
    problems += [f"loader {x}" for x in src]
    out = {}

    def not_submitted(extra=None):
        v = dict(NOT_SUBMITTED)
        v.update(extra or {})
        return v

    def body(e):
        # a year with the two rules, one with markers and published zeros,
        # and an earlier one with a published 0 for Norfolk (the rule is for
        # 2023-24 only)
        p24 = e.file(year=2024, modified="2024-07-11T13:57:05Z", values={
            "Norfolk": not_submitted(), "West Midlands": not_submitted(),
            "Suffolk": {"housing_referrals": 0},
            "City of London": {"housing_referrals": 0}})
        p26 = e.file(year=2026, values={
            "Norfolk": {c: "No Data" for c in m.CASES_COLUMNS
                        + ("housing_referrals",)},
            "Suffolk": {"cases_per_10k_adult_females": "No population data"},
            "City of London": {"housing_referrals": 0},
            "Lancashire": {"housing_referrals": 0}})
        p23 = e.file(year=2023, modified="2023-07-11T13:57:05Z", values={
            "Norfolk": {"housing_referrals": 0}})
        e.ok(e.cur, ["load", "--commit", "--file", p24, "--file", p26,
                     "--file", p23, "--no-page"])
        cur_ = e.cur
        out["rows"] = {(y, k): (tl.ed_val(cur_, k, "value_flag", 1, y),
                                tl.ed_val(cur_, k, "housing_referrals", 1, y),
                                tl.ed_val(cur_, k, "cases_discussed", 1, y))
                       for y in ("2022-23", "2023-24", "2025-26")
                       for k in ("Norfolk", "West Midlands", "Suffolk",
                                 "City of London", "Lancashire")}
        out["table"] = real_blanks(cur_, ZZ, files=None, rules=False)
        with planted(cur_, f"UPDATE public.{ZZ.editions_table} SET "
                     "marac_count = NULL, value_flag = NULL WHERE "
                     "pfa_name_safelives = 'Cumbria' AND financial_year = "
                     "'2025-26'", editions=True):
            out["plant_null"] = real_blanks(cur_, ZZ, files=None, rules=False)

    def no_match(e):
        # a rule that no longer matches the file halts, nothing stored
        p = e.file(year=2024, modified="2024-07-11T13:57:05Z", values={
            "Norfolk": not_submitted({"cases_discussed": 5}),
            "West Midlands": not_submitted()})
        ok_, text = e.halts(["load", "--commit", "--file", p, "--no-page"],
                            state(e.cur), "no longer matches")
        out["no_match"] = ok_
    scenario(cur, body)
    scenario(cur, no_match)
    try:
        rows = out["rows"]
        for k in ("Norfolk", "West Midlands"):
            if rows[("2023-24", k)] != ("not_submitted", None, None):
                problems.append(f"{k} 2023-24 is {rows[('2023-24', k)]}")
        if rows[("2023-24", "Suffolk")][1] != 0 or \
                rows[("2023-24", "Suffolk")][0] is not None:
            problems.append("Suffolk 2023-24's published 0 did not stay 0")
        if rows[("2025-26", "Norfolk")] != ("not_submitted", None, None):
            problems.append("a 'No Data' marker did not give NULL "
                            "not_submitted")
        if rows[("2025-26", "Suffolk")][0] != "no_population":
            problems.append("'No population data' did not give NULL "
                            "no_population")
        for k in ("City of London", "Lancashire"):
            if rows[("2025-26", k)][1] != 0 or rows[("2025-26", k)][0]:
                problems.append(f"{k} 2025-26 housing 0 did not stay a "
                                "plain 0")
        if not out["table"][0]:
            problems.append(f"table part: {out['table'][1]}")
        if out["plant_null"][0] or "no value_flag" not in \
                out["plant_null"][1]:
            problems.append("a NULL with no value_flag was not caught")
        if not out["no_match"]:
            problems.append("a rule that no longer matches the file did not "
                            "halt with nothing stored")
        if rows[("2022-23", "Norfolk")][:2] != (None, 0):
            problems.append("the Norfolk rule fired outside 2023-24")
    except KeyError as ex:
        problems.append(f"seeded scenario incomplete: {ex}")

    def real(c):
        return real_blanks(c, REAL)
    mixed(4, name, not problems, "the two named rules (Norfolk and West "
          "Midlands 2023-24, not_submitted) are the only zero rules and "
          "fire on their force and year only; the named entry lists 13 "
          "NULL-to-0 cells and nothing to NULL; markers give NULL with "
          "their flag, published zeros (Lancashire's included) stay 0; a "
          "NULL without a flag and a rule that no longer matches are caught; "
          "the loader's AST holds no other zero rule, no literal force "
          "compared with 0 and no coerced value" if not problems
          else "; ".join(problems[:3]), cur, real)


# ---------------------------------------------------------------- gate 5

def real_reconcile(files=None, names=None):
    """The reconciliation re-run read-only from the files on disk: each held
    file read (identity), its English forces summing to the England row on
    every measure whose parts are all published; the measures not checked are
    listed."""
    files = files or m.LEGACY_FILES
    bad, done, skipped = [], 0, {}
    for y in sorted(files):
        p, wb = held_file(files, y)
        if p is None:
            bad.append(wb)
            continue
        problems, not_checked = m.reconcile(wb)
        bad += [f"{y}: {x}" for x in problems]
        done += len(m.RECONCILED) - len(not_checked)
        if not_checked:
            skipped[y] = not_checked
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(files)} held files: the English forces sum to the England row "
        f"on {done} measure-years; not checked (a part is unpublished): "
        + (", ".join(f"{y} {'/'.join(v)}" for y, v in skipped.items())
           or "none"))


def gate_5_reconciliation(cur):
    name = ("reconciliation inside each file re-run read-only from the files "
            "on disk; a seeded mismatch halts and stores nothing")
    problems = []
    ok, detail = real_reconcile()
    if not ok:
        problems.append(detail)

    def body(e):
        e.file(england_delta=1)
        base = state(e.cur)
        okh, text = e.halts(["load", "--commit"], base, "reconciliation")
        if not okh:
            problems.append(f"an England row off by 1 did not halt: "
                            f"{text[-100:]}")
        # a measure with an unpublished part is not checked, the rest is
        path = e.file(values={"Norfolk": {"marac_count": "No Data"}})
        wb = m.read_workbook(path)
        probs, skipped = m.reconcile(wb)
        if probs or skipped != ["marac_count"]:
            problems.append(f"a 'No Data' part: problems {probs}, not "
                            f"checked {skipped}")
        wb = m.read_workbook(e.file(england_delta=1, values={
            "Norfolk": {"cases_discussed": "No Data"}}))
        probs, skipped = m.reconcile(wb)
        if "cases_discussed" in "".join(probs):
            problems.append("an unpublished part was summed as 0")
        e.ok(e.cur, ["load"])
        # a clean file reconciles on all five measures
        probs, skipped = m.reconcile(m.read_workbook(e.file()))
        if probs or skipped:
            problems.append(f"a clean fixture: {probs} {skipped}")
    scenario(cur, body)
    report(5, name, not problems, detail if not problems else
           "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 6

def real_identity(files=None):
    """Each held file by sha256 and by its own titles: Notes and Cases titles
    agree with the year (the 2022-23 file only through its named erratum),
    the Cases header is the eight columns, Housing is found by its header
    text."""
    files = files or m.LEGACY_FILES
    bad, errata = [], []
    for y in sorted(files):
        p, wb = held_file(files, y)
        if p is None:
            bad.append(wb)
            continue
        if wb["year"] != y:
            bad.append(f"{y}: {p.name} says {wb['year']}")
        if wb.get("erratum"):
            errata.append(y)
        if wb["sha256"] != files[y]["sha256"]:
            bad.append(f"{y}: sha256 differs")
    if errata and errata != ["2022-23"]:
        bad.append(f"a Notes-title erratum is used for {errata}, only "
                   "2022-23 is named")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(files)} held files: sha256, Notes and Cases titles and years "
        f"agree (erratum used for {', '.join(errata) or 'none'})")


def gate_6_identity(cur):
    name = ("identity from the files: the held files' titles and sha256; "
            "seeded title, header, sheet and link mismatches halt; Housing "
            "is found by its header text")
    problems = []
    ok, detail = real_identity()
    if not ok:
        problems.append(detail)
    errata = {v["file"]: v["year"] for v in m.NOTES_TITLE_ERRATA.values()}
    if errata != {"Marac-data-2022-2023.xls": "2022-23"}:
        problems.append(f"NOTES_TITLE_ERRATA names {errata}")

    def body(e):
        base = state(e.cur)

        def halts(label, argv, needle=None, **kw):
            e.file(**kw)
            ok_, text = e.halts(argv, base, needle)
            if not ok_:
                problems.append(f"{label}: {text[-110:]}")
        halts("the Notes title says another year", ["load", "--commit"],
              "Notes", notes_year=2025)
        halts("the Cases title says another year", ["load", "--commit"],
              None, cases_year=2025)
        hdr = list(HEADER)
        hdr[2] = "Number of cases reviewed"
        halts("a changed Cases header", ["load", "--commit"], None,
              header=hdr)
        halts("a missing sheet", ["load", "--commit"], None,
              sheets=("Notes", "Cases"))
        path = e.file(year=2025, register=False)
        e.urls[tl.LINK.format(2025, 2026)] = path
        e.links[Y] = tl.LINK.format(2025, 2026)
        ok_, text = e.halts(["load", "--commit"], base)
        if not ok_:
            problems.append(f"a link whose file says another year: "
                            f"{text[-100:]}")
        # an erratum is for its own bytes only: another file with the same
        # disagreement is not excused
        halts("an unnamed Notes-title disagreement", ["load", "--commit"],
              "Notes", notes_year=2024)
        # the named alias is accepted and reported; Housing found by header
        # text wherever the column sits
        hdr = list(HEADER)
        hdr[1] = "Number of Maracs"
        path = e.file(header=hdr, housing_col=30, values={
            "Cumbria": {"housing_referrals": 7.25}})
        text, _ = e.ok(e.cur, ["load", "--commit"])
        if "header alias" not in text:
            problems.append("the named header alias was not reported")
        suffolk = Decimal(str(fixture_values()["Suffolk"][
            "housing_referrals"])).quantize(Decimal("0.01"))
        if tl.live_val(e.cur, "Cumbria", "housing_referrals") != Decimal(
                "7.25") or tl.live_val(e.cur, "Suffolk",
                                       "housing_referrals") != suffolk:
            problems.append("Housing was not found by its header text at "
                            "column 30")
    scenario(cur, body)
    report(6, name, not problems, detail if not problems else
           "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 7

def real_ranks(cur, spec=REAL):
    """Each year's latest edition came from a file whose rank names the same
    year (its own titles), or is 'as loaded' (no rank); the ledger's sources
    parse."""
    _need(cur, spec)
    bad, parts = [], []
    for p, t in sorted(m.tip_info(cur, spec).items()):
        if t["label"] == core.AS_LOADED_LABEL:
            parts.append(f"{p} as loaded")
            continue
        r = t["rank"]
        if r is None:
            bad.append(f"{p}: the tip's source_file names no rank")
        elif r[0] != p:
            bad.append(f"{p}: the tip's rank says {r[0]}")
        else:
            parts.append(f"{p} {m.rank_text(r)[10:]}")
    cur.execute(f"SELECT DISTINCT source_file FROM public.{ledger_name(spec)}")
    for (s,) in cur.fetchall():
        if m.parse_ledger_source(s) is None:
            bad.append(f"ledger source does not parse: {s[:60]}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(parts)} year(s): " + "; ".join(parts[:3]) + "; ...")


def gate_7_older_and_reissue(cur):
    name = ("older-file and reissue guards on the page and --file paths; the "
            "rank is the file's own, never its name; --allow-older-file and "
            "--accept-reissue are logged")
    problems = []

    def older(e, how):
        e.seed(e.cur)
        name_ = "MARAC-DATA-2025-2026-published-2027-09-30.xlsx"
        path = e.file(modified="2026-06-01T10:00:00Z", register=how == "page",
                      values={"Cumbria": {"cases_discussed": 7}})
        if how != "page":
            dest = e.root / "named" / name_
            dest.parent.mkdir(exist_ok=True)
            shutil.copy(path, dest)
            return ["load", "--commit", "--file", dest, "--no-page"]
        return ["load", "--commit"]

    def body(e):
        for how in ("page", "file"):
            def one(c, how=how):
                with env(c) as e2, tl.specs(e2.root, e2.names):
                    argv = older(e2, how)
                    before = state(c)
                    rc, text, logged = e2.cmd(argv)
                    if not (rc == "halt" and "older" in text
                            and state(c) == before and not logged.called):
                        problems.append(f"{how}: rc {rc}: {text[-140:]}")
                    rc, text, logged = e2.cmd(argv + [
                        "--allow-older-file", "--accept-reissue", Y,
                        "--acknowledge", Y])
                    eds = tl.editions(c)
                    if not (rc == 0 and eds == [(1, None, N), (2, 1, N)]
                            and logged.called and "--allow-older-file given"
                            in logged.call_args[0][2]):
                        problems.append(f"--allow-older-file ({how}): rc "
                                        f"{rc}, {eds}")
            _in_savepoint(e.cur, one)

        def reissue(c):
            with env(c) as e2, tl.specs(e2.root, e2.names):
                e2.seed(c)
                e2.revise(values={"Cumbria": {"cases_discussed": 120.5}})
                before = rejected_state(c, Y)
                bad = rejects(e2, ["load", "--commit"], "--accept-reissue",
                              before)
                if bad:
                    problems.append(f"a later file with other content: {bad}")
                rc, text, logged = e2.cmd(["load", "--commit",
                                           "--accept-reissue", Y,
                                           "--acknowledge", Y])
                if not (rc == 0 and "ACCEPTED REISSUE" in text
                        and tl.editions(c) == [(1, None, N), (2, 1, N)]
                        and "--accept-reissue" in logged.call_args[0][2]):
                    problems.append(f"--accept-reissue: rc {rc}")
        _in_savepoint(e.cur, reissue)

        def equal_rank(c):
            with env(c) as e2, tl.specs(e2.root, e2.names):
                e2.seed(c)
                e2.file(values={"Cumbria": {"cases_discussed": 120.5}})
                before = rejected_state(c, Y)
                bad = rejects(e2, ["load", "--commit", "--acknowledge", Y],
                              "--accept-reissue", before)
                if bad:
                    problems.append(f"equal rank, other content: {bad}")
        _in_savepoint(e.cur, equal_rank)
    scenario(cur, body)

    def tip_as_loaded(e, path, files, label):
        rc, text, _ = migrate_cmd(e, files, "--commit")
        if rc != 0:
            problems.append(f"migrate-legacy: {text[-100:]}")
            return
        e.register(path, label)
        before = state(e.cur)
        rc, text, logged = e.cmd(["load", "--commit"])
        if not (rc == 1 and "--acknowledge" in text
                and "--accept-reissue" not in text
                and state(e.cur) == before):
            problems.append("a file against a tip 'as loaded' (no rank) "
                            f"asked for the wrong release: rc {rc}")
    legacy_world(cur, tip_as_loaded)
    if not hasattr(m, "_older") or m._older(("x", None), ("x", None)):
        problems.append("two unknown modified dates are ordered")
    t0 = m.release_rank("f.xlsx; SafeLives 2025-26; modified "
                        "2026-07-02T14:06:06Z; sha256 ab; x")
    t1 = m.release_rank("SafeLives 2025-26; modified unknown")
    if t0 is None or t1 is None or t1[1] is not None or \
            m.release_rank("MARAC-DATA-2027-2028.xlsx") is not None:
        problems.append("release_rank reads a rank from a file name or "
                        "fails on 'modified unknown'")
    mixed(7, name, not problems, "a file older than the held one is skipped "
          "and halts, from the page and from --file even when its name "
          "carries a later date, storing and logging nothing; "
          "--allow-older-file overrides and is logged; a later file with "
          "other content, and an equal-rank one, stop unless "
          "--accept-reissue; a tip 'as loaded' has no rank and asks for "
          "--acknowledge" if not problems else "; ".join(problems[:3]),
          cur, real_ranks)


# ---------------------------------------------------------------- gate 8

def rejected_state(cur, period):
    """The editions and live tables, and the number of ledger rows of the
    period (a rejected year stores nothing and writes no ledger row)."""
    tables = tables_state(cur)
    cur.execute(f"SELECT COUNT(*) FROM public.{LEDGER} WHERE "
                "financial_year = %s", (period,))
    return tables + (cur.fetchone()[0],)


def rejects(e, argv, needle, before, *, commit=True, period=Y):
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


def _prec(key, cases):
    return {m.KEY: key, m.PERIOD: "x", "cases_discussed": Decimal(cases)}


def limit_problems():
    """The soft limits are exact: England cases discussed exactly 25% is not
    over, +1 is; exactly 6 forces moving by more than 40% pass, 7 stop; a
    force moving exactly 40% is not over."""
    bad = []
    g = m.NEW_ENGLAND_PCT, m.NEW_PFA_PCT, m.NEW_MAX_PFAS
    if g != (25, 40, 6):
        bad.append(f"limits are {g}, calibrated as (25, 40, 6)")
    prev = [_prec(f"F{i:02d}", 1000) for i in range(40)]

    def new_with(changes):
        out = [dict(r) for r in prev]
        for i, v in changes.items():
            out[i]["cases_discussed"] = Decimal(v)
        return out

    def england(new):
        return any("England" in x for x in m.period_problems(
            new, None, prev, kind="new")[1])

    def forces(new):
        return any("police force areas move" in x for x in m.period_problems(
            new, None, prev, kind="new")[1])
    if england(new_with({0: 1000 + 10000})):
        bad.append("England +25% exactly was stopped")
    if not england(new_with({0: 1000 + 10001})):
        bad.append("England +25% and 1 was not stopped")
    if forces(new_with({i: 1400 for i in range(10)})):
        bad.append("forces moving exactly 40% were counted")
    if forces(new_with({**{i: 1401 for i in range(6)}})):
        bad.append("6 forces past 40% were stopped (the limit is more than "
                   "6)")
    if not forces(new_with({**{i: 1401 for i in range(7)}})):
        bad.append("7 forces past 40% were not stopped")
    # a revised year: a missing force is hard, a change is soft
    tip = [dict(r, **{c: Decimal(1) for c in m.COMPARED if c != "value_flag"},
                value_flag=None) for r in prev[:5]]
    hard, soft = m.period_problems(tip[:4], tip, None, kind="revised")
    if not any("PARTIAL FILE" in x for x in hard):
        bad.append("a force missing from a revised file is not a hard stop")
    changed = [dict(r) for r in tip]
    changed[0]["marac_count"] = Decimal(2)
    hard, soft = m.period_problems(changed, tip, None, kind="revised")
    if hard or not soft:
        bad.append("a changed value is not a soft stop")
    return bad


def real_calibration(cur):
    """The eight held years, read from the files, pass the new-year stop
    conditions against their predecessors (the limits are calibrated on
    them)."""
    names = m.pfa_names(cur)
    prev, bad = None, []
    for y in sorted(m.LEGACY_FILES):
        p, wb = held_file(m.LEGACY_FILES, y)
        if p is None:
            return False, wb
        recs = m.records(wb, y, names)
        if prev is not None:
            hard, soft = m.period_problems(recs, None, prev, kind="new")
            bad += [f"{y}: {x[:90]}" for x in hard + soft]
        prev = recs
    return not bad, "; ".join(bad[:2]) if bad else (
        "the eight held years pass the new-year limits against their "
        "predecessors")


def gate_8_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row; the limits are exact")
    problems = limit_problems()
    ok, detail = real_calibration(cur)
    if not ok:
        problems.append(f"calibration: {detail}")

    def case(label, setup, argv, needle, commit=True, names=None):
        def body(c):
            with env(c) as e, tl.specs(e.root, e.names):
                setup(e)
                if names is not None:
                    e.names = names
                before = rejected_state(c, Y)
                bad = rejects(e, argv, needle, before, commit=commit)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)
    accept = ["--commit", "--accept-reissue", Y]

    def rev(**kw):
        def f(e):
            e.seed(e.cur)
            e.revise(**kw)
        return f
    case("revised: a changed value needs --acknowledge",
         rev(values={"Cumbria": {"cases_discussed": 120.5}}),
         ["load"] + accept, f"--acknowledge {Y}")
    case("revised: the preview of the same writes no run-log row",
         rev(values={"Cumbria": {"cases_discussed": 120.5}}),
         ["load", "--accept-reissue", Y], f"--acknowledge {Y}", commit=False)
    case("revised: a value to NULL needs a named flips entry",
         rev(values={"City of London": {"housing_referrals": "No Data"}}),
         ["load"] + accept + ["--acknowledge", Y], "--acknowledge-flips")
    case("revised: a NULL to 0 needs a named flips entry",
         lambda e: (e.seed(e.cur, values={"Suffolk": {
             "repeat_cases": "No Data"}}),
             e.revise(values={"Suffolk": {"repeat_cases": 0}})),
         ["load"] + accept + ["--acknowledge", Y], "--acknowledge-flips")
    case("revised: a file lacking a held force is partial (nothing releases "
         "it)",
         lambda e: (e.seed(e.cur), e.revise(regions=(
             ("North West", ("Cumbria", "Lancashire")),
             ("West Midlands", ("Staffordshire", "West Midlands")),
             ("East", ("Norfolk",)),
             ("London", ("Metropolitan Police", "City of London"))))),
         ["load"] + accept + ["--acknowledge", Y], "PARTIAL FILE",
         names=PFAS - {"Suffolk"})
    case("new year: England cases discussed move more than 25%",
         lambda e: (e.seed(e.cur, year=2025, modified="2025-07-11T13:57:05Z"),
                    e.file(values={n: {"cases_discussed": 1000}
                                   for n in PFAS})),
         ["load", "--commit"], "England")

    def stray(e):
        e.seed(e.cur)
        base = state(e.cur)
        for argv in (["load", "--commit", "--acknowledge", "2019-20"],
                     ["load", "--commit", "--acknowledge-flips",
                      FIRST_LOAD_ENTRY]):
            ok_, text = e.halts(argv, base)
            if not ok_:
                problems.append(f"{argv[2:]} did not halt: {text[-100:]}")

    def acknowledged(e):
        e.seed(e.cur)
        e.revise(values={"Cumbria": {"cases_discussed": 120.5}})
        rc, text, logged = e.cmd(["load", "--commit", "--accept-reissue", Y,
                                  "--acknowledge", Y])
        if not (rc == 0 and "ACKNOWLEDGED" in text
                and tl.editions(e.cur) == [(1, None, N), (2, 1, N)]
                and "ACKNOWLEDGED" in logged.call_args[0][2]):
            problems.append(f"--acknowledge of a changed value: rc {rc}")
    scenario(cur, stray)
    scenario(cur, acknowledged)
    report(8, name, not problems, "a revised year with a changed value, a "
           "value to NULL, a NULL to 0 or a missing force, and a new year "
           "with England moving more than 25%, are each REJECTED (exit 1) "
           "with nothing stored, no ledger row and one partial run-log row "
           "(none in a preview); a partial file is never released; the "
           "25%, 40% and 6-force limits are exact and the eight held years "
           "pass them; --acknowledge stores a changed value and a year the "
           "run does not compare halts" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 9

def gate_9_one_transaction(cur):
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
                or state(e.cur) == before or not logged.called:
            problems.append(f"a new year did not store edition 1, {N} live "
                            f"rows and one 'new' ledger row: rc {rc} {led}")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the load")
        if core.rows_differing(e.cur, ZZ, Y, 1):
            problems.append("live differs from edition 1")
        src = tl.live_val(e.cur, "Cumbria", "source")
        if "sha256" not in (src or ""):
            problems.append("the live source does not carry the file's "
                            "sha256")

    def failing(label, target, attr):
        def body(c):
            with env(c) as e, tl.specs(e.root, e.names):
                e.file()
                before = state(c)
                with mock.patch.object(target, attr,
                                       side_effect=RuntimeError("boom")):
                    rc, text, logged = e.cmd(["load", "--commit"])
                if not (rc == 1 and state(c) == before
                        and f"{Y}: FAILED" in text and logged.called
                        and "PARTIAL RUN" in logged.call_args[0][2]):
                    problems.append(f"{label}: rc {rc}, state kept="
                                    f"{state(c) == before}")
        _in_savepoint(cur, body)
    scenario(cur, good)
    failing("a failing ledger insert", pe, "record_file_check")
    failing("a failing live insert", m, "insert_live")
    failing("a failing edition insert", core, "insert_edition")
    report(9, name, not problems, "edition 1, the live rows and one 'new' "
           "ledger row are stored together; a failing ledger insert, live "
           "insert or edition insert rolls the year back and logs a partial "
           "run" if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 10

def gate_10_ledger_rule(cur):
    name = "ledger skip needs the (source, sha256) pair"
    problems = []
    checked = {Y: {("https://x/a.xlsx (SafeLives 2025-26; modified "
                    "2026-07-02T14:06:06Z)", "h1")}}
    for where, sha, per, want in (
            ("https://x/a.xlsx", "h1", Y, True),
            ("https://x/a.xlsx", "h2", Y, False),
            ("https://x/b.xlsx", "h1", Y, False),
            ("https://x/a.xlsx", "h1", Y0, False)):
        if m.ledger_complete(checked, where, sha, per) != want:
            problems.append(f"ledger_complete({where}, {sha}, {per}) != "
                            f"{want}")

    def same_url(e):
        csv0 = e.file()
        e.ok(e.cur, ["load", "--commit"])
        url0 = e.last_url
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "already in the ledger" not in text or logged.called:
            problems.append("a held pair did not skip")
        # the same URL with other bytes: the sha differs, so it is read and
        # compared (here stopped as a reissue, nothing stored)
        csv1 = e.revise(values={"Cumbria": {"cases_discussed": 120.5}})
        e.urls[url0] = csv1
        rc, text, _ = e.cmd(["load"])
        if "already in the ledger" in text or "--accept-reissue" not in text:
            problems.append("the same URL with other bytes was skipped")
        # the held pair again: skipped; --recheck reads it anyway
        e.urls[url0] = csv0
        rc, text, _ = e.cmd(["load"])
        if "already in the ledger" not in text:
            problems.append("the held pair no longer skips")
        rc, text, _ = e.cmd(["load", "--recheck", Y])
        if f"{Y}: unchanged" not in text or "already in the ledger" in text:
            problems.append("--recheck did not read a ledger-held file")
        # the same bytes under another link: read, found unchanged
        other = ("https://safelives.org.uk/wp-content/uploads/2026/08/"
                 "MARAC-DATA-2025-2026.xlsx")
        e.urls[other] = csv0
        e.links[Y] = other
        rc, text, _ = e.cmd(["load"])
        if "already in the ledger" in text or f"{Y}: unchanged" not in text:
            problems.append("a held file under a new link was skipped by "
                            "the old pair")
    scenario(cur, same_url)
    report(10, name, not problems, "a ledger pair is the file's source and "
           "its sha256 for the year; the held pair skips with nothing logged; "
           "the same link with other bytes, and the same bytes under another "
           "link, are read; --recheck reads a held file" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 11

def gate_11_preview(cur):
    name = "preview and simulate write nothing, every writing command"
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
        e.revise(values={"Cumbria": {"cases_discussed": 120.5}})
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"],
                     ["load", "--simulate", "--accept-reissue", Y,
                      "--acknowledge", Y],
                     ["load", "--accept-reissue", Y, "--acknowledge", Y],
                     ["status"], ["ddl"], ["ddl", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}, state kept="
                                f"{state(e.cur) == before}")
        e.ok(e.cur, ["load", "--commit", "--accept-reissue", Y,
                     "--acknowledge", Y])
        before = state(e.cur)
        for argv in (["refresh-latest"], ["restore-edition", Y, "1"],
                     ["refresh-latest", "--simulate"],
                     ["restore-edition", Y, "1", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"{argv} after a revision: rc {rc}")
    scenario(cur, body)

    def legacy(e, path, files, label):
        before = state(e.cur)
        for extra in ((), ("--simulate",)):
            rc, text, logged = migrate_cmd(e, files, *extra)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"migrate-legacy {extra}: rc {rc}: "
                                f"{text[-100:]}")
    legacy_world(cur, legacy)
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    if cur.fetchone()[0] != runs:
        problems.append("the run log changed")
    report(11, name, not problems, "load (also with the acknowledgements), "
           "status, ddl, refresh-latest, restore-edition and migrate-legacy "
           "in preview and --simulate leave the editions, the live table and "
           "the ledger as they were and log nothing" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 12

def reread_unchanged(e, path):
    """Problems (a list) when the same bytes are not `unchanged` on every
    path against the stored edition."""
    bad = []
    rc, text, logged = e.cmd(["load"])
    if rc != 0 or "already in the ledger" not in text or logged.called:
        bad.append(f"the page load: rc {rc}: {text[-100:]}")
    for argv in (["load", "--recheck", Y],
                 ["load", "--year", Y, "--recheck", Y],
                 ["load", "--file", path],
                 ["load", "--file", path, "--no-page"],
                 ["load", "--file", path, "--no-page", "--recheck", Y,
                  "--commit"],
                 ["load", "--recheck", Y, "--commit"]):
        rc, text, logged = e.cmd(argv)
        if rc != 0 or f"{Y}: unchanged" not in text \
                or "REJECTED" in text or ": revised" in text:
            bad.append(f"{' '.join(map(str, argv[1:]))}: rc {rc}: "
                       f"{text[-100:]}")
    return bad


def stored_from(cur, spec, y, sha):
    """The highest edition of year y stored from the file with this sha256
    (its source_file names the sha256), else the edition of a ledger row
    for it, else None."""
    cur.execute(f"SELECT edition FROM public.{spec.editions_table} WHERE "
                f"{m.PERIOD} = %s AND source_file LIKE %s GROUP BY edition "
                "ORDER BY edition DESC", (y, f"%sha256 {sha[:16]}%"))
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute(f"SELECT edition FROM public.{ledger_name(spec)} WHERE "
                f"{m.PERIOD} = %s AND file_sha256 = %s AND edition IS NOT "
                "NULL ORDER BY id DESC LIMIT 1", (y, sha))
    r = cur.fetchone()
    return r[0] if r else None


def real_reread(cur, spec=REAL, files=None, names=None):
    """Each held file, built now (the loader's own records), equals the
    edition stored from it (found by the sha256 in its source_file, or by
    its ledger row), so a re-read is unchanged."""
    _need(cur, spec)
    files = files or m.LEGACY_FILES
    names = m.pfa_names(cur) if names is None else names
    bad, equal = [], 0
    for y in sorted(files):
        p, wb = held_file(files, y)
        if p is None:
            bad.append(wb)
            continue
        ed = stored_from(cur, spec, y, files[y]["sha256"])
        if ed is None:
            bad.append(f"{y}: no edition or ledger row records {p.name} "
                       "(not yet loaded)")
            continue
        recs = m.records(wb, y, names)
        stored = m.held_records(cur, spec, y, ed)
        if m.rows_content_sha(recs) != m.rows_content_sha(stored):
            sk = {r[m.KEY]: r for r in stored}
            diff = [r[m.KEY] for r in recs if r[m.KEY] not in sk or any(
                r[c] != sk[r[m.KEY]][c] for c in m.COMPARED)]
            bad.append(f"{y}: edition {ed} differs from {p.name} read now "
                       f"in {len(diff)} force(s), e.g. {diff[:3]}")
        else:
            equal += 1
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{equal} held files, read now, equal the edition stored from them "
        "(values and value_flag), so a re-read is unchanged")


def gate_12_reread(cur):
    name = ("a byte-identical re-read is unchanged on every path (markers, "
            "rules and zeros included); after migrate-legacy a faithful "
            "held file is skipped through the ledger")
    problems = []
    over = {"Norfolk": {c: "No Data" for c in m.CASES_COLUMNS
                        + ("housing_referrals",)},
            "Suffolk": {"cases_per_10k_adult_females": "No population data"}}

    def loaded(e):
        path = e.file(values=over)
        e.ok(e.cur, ["load", "--commit"])
        snap = tables_state(e.cur)
        problems.extend(f"loaded: {x}" for x in reread_unchanged(e, path))
        if tl.editions(e.cur) != [(1, None, N)]:
            problems.append("a re-read stored an edition")
        if tables_state(e.cur) != snap:
            problems.append("a re-read changed the editions or live")
        if {o for _, _, o, _ in tl.ledger(e.cur)[1:]} - {"unchanged"}:
            problems.append("a re-read ledger row is not 'unchanged'")
        if tl.live_val(e.cur, "Norfolk") is not None or tl.ed_val(
                e.cur, "Norfolk", "value_flag", 1) != "not_submitted":
            problems.append("a marker did not stay NULL with its reason")
        if tl.live_val(e.cur, "City of London", "housing_referrals") != 0:
            problems.append("a published 0 became NULL on re-read")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "none (total 0)" not in text:
            problems.append("refresh-latest plans a write after the "
                            "re-reads")
    scenario(cur, loaded)

    def migrated(e):
        path, files, label = e.seed_legacy(e.cur, defects=False,
                                           values=over)
        rc, text, _ = migrate_cmd(e, files, "--commit", explained={})
        if rc != 0 or "0 differences" not in text:
            problems.append(f"a faithful migration: rc {rc}: {text[-120:]}")
            return
        if len(tl.ledger(e.cur)) != 1:
            problems.append("a faithful year got no ledger row")
        e.register(path, label)
        text, _ = e.ok(e.cur, ["load"])
        if "already in the ledger" not in text:
            problems.append("the held file was not skipped through the "
                            "ledger")
        for argv in (["load", "--recheck", label],
                     ["load", "--file", path, "--no-page"],
                     ["load", "--file", path, "--recheck", label,
                      "--commit"]):
            text, _ = e.ok(e.cur, argv)
            if f"{label}: unchanged" not in text:
                problems.append(f"{argv[1:]} against edition 1 as loaded: "
                                f"{text[-100:]}")
        if tl.editions(e.cur, label) != [(1, None, N)]:
            problems.append("re-reading the migrated file stored an "
                            "edition")
    scenario(cur, migrated)
    mixed(12, name, not problems, "after a load of a file with markers, "
          "zeros and a rule-free year, the page, --year, --file, --file "
          "--no-page and --recheck re-reads are unchanged, store no edition, "
          "leave every ledger row 'unchanged' and the markers NULL with "
          "their reasons; after migrate-legacy a faithful held file is "
          "skipped through the ledger and re-reads unchanged on every path"
          if not problems else "; ".join(problems[:3]), cur, real_reread)


# --------------------------------------------------------------- gate 13

def gate_13_refresh(cur):
    name = ("refresh-latest changes only the revised years and rows, copies "
            "loaded_at, and applies a key change only with "
            "--accept-key-changes")
    problems = []

    def by_key(c, period):
        c.execute(f"SELECT pfa_name_safelives, cases_discussed, source, "
                  f"loaded_at FROM public.{ZZ.live_table} WHERE "
                  "financial_year = %s", (period,))
        return {r[0]: r for r in c.fetchall()}

    def rows_hash(c, where):
        c.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                  f"FROM public.{ZZ.live_table} t WHERE {where}")
        return c.fetchone()[0]

    def body(e):
        e.seed(e.cur, year=2025, modified="2025-07-11T13:57:05Z")
        e.seed(e.cur)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        other_before = rows_hash(e.cur, f"financial_year = '{Y0}'")
        before = by_key(e.cur, Y)
        e.revise(values={"Cumbria": {"cases_discussed": 120.5}})
        rc, text, _ = e.cmd(["load", "--commit", "--accept-reissue", Y,
                             "--acknowledge", Y])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N), (2, 1, N)] or \
                tl.editions(e.cur, Y0) != [(1, None, N)]:
            problems.append(f"the revision did not store edition 2 of {Y} "
                            f"only: rc {rc}")
        if by_key(e.cur, Y) != before:
            problems.append("load changed the live table before "
                            "refresh-latest")
        if pe.status(e.cur, m.profile())["pending_refresh"] != [Y]:
            problems.append("status does not name the pending refresh")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called or \
                f"{Y}=1" not in text:
            problems.append("the refresh-latest preview wrote or did not "
                            "plan exactly one row")
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if rc != 0:
            problems.append(f"refresh-latest --commit: rc {rc}: "
                            f"{text[-120:]}")
        after = by_key(e.cur, Y)
        changed = [k for k in after if after[k] != before.get(k)]
        if changed != ["Cumbria"] or after["Cumbria"][1] != Decimal("120.50"):
            problems.append(f"refresh-latest changed {changed[:3]}, "
                            "expected that one row")
        if rows_hash(e.cur, f"financial_year = '{Y0}'") != other_before:
            problems.append("refresh-latest changed another year")
        if [k for k in after if k != "Cumbria"
                and after[k][2:] != before[k][2:]]:
            problems.append("source or loaded_at moved on rows that did not "
                            "change")
        e.cur.execute(f"SELECT DISTINCT loaded_at FROM public."
                      f"{ZZ.editions_table} WHERE financial_year = %s AND "
                      "edition = 2", (Y,))
        (ed_loaded,), = e.cur.fetchall()
        e.cur.execute(f"SELECT pfa_name_safelives FROM public."
                      f"{ZZ.live_table} WHERE financial_year = %s AND "
                      "loaded_at = %s", (Y, ed_loaded))
        if e.cur.fetchall() != [("Cumbria",)]:
            problems.append("loaded_at was not copied to exactly the "
                            "refreshed row")
        if "SafeLives" not in after["Cumbria"][2] or \
                after["Cumbria"][2] == before["Cumbria"][2]:
            problems.append("the refreshed row's source does not carry the "
                            "new edition's label")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "none (total 0)" not in text:
            problems.append("a second refresh-latest plans a write")
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET cases_discussed = "
                      "cases_discussed + 1 WHERE pfa_name_safelives = "
                      f"'Suffolk' AND financial_year = '{Y0}'")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if not (rc in ("halt", 1) and state(e.cur) == snap
                and not logged.called):
            problems.append(f"a drifted live year was refreshed: rc {rc}")
    scenario(cur, body)

    def keys(e, path, files, label):
        rc, text, _ = migrate_cmd(e, files, "--commit")
        if rc != 0:
            problems.append(f"migrate-legacy: {text[-100:]}")
            return
        e.register(path, label)
        with mock.patch.dict(m.ACKNOWLEDGED_FLIPS, {"t": entry_for(label)}):
            e.ok(e.cur, ["load", "--commit", "--acknowledge", label,
                         "--acknowledge-flips", "t"])
        if tl.editions(e.cur, label) != [(1, None, N - 1), (2, 1, N)]:
            problems.append("West Midlands is not a key added in edition 2")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if not (rc == "halt" and "not named" in text
                and state(e.cur) == snap and not logged.called):
            problems.append(f"a key change without --accept-key-changes was "
                            f"applied: rc {rc}")
        e.ok(e.cur, ["refresh-latest", "--commit", "--accept-key-changes",
                     label])
        if tl.count(e.cur, ZZ.live_table) != N or \
                tl.live_val(e.cur, "West Midlands", period=label) == "absent":
            problems.append("--accept-key-changes did not add the force")
        e.cur.execute(f"SELECT column_name FROM information_schema.columns "
                      f"WHERE table_name = '{ZZ.live_table}' AND "
                      "column_name = 'value_flag'")
        if e.cur.fetchall():
            problems.append("value_flag reached the live table")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean after the key change")
    legacy_world(cur, keys)
    report(13, name, not problems, "a revised year waits for refresh-latest "
           "(the preview writes nothing); the commit changes exactly the "
           "revised row, leaves every other row and year alone and copies "
           "the edition's loaded_at to that row only; a second refresh plans "
           "nothing; a drifted year is not overwritten; West Midlands "
           "arrives in edition 2 and only --accept-key-changes adds it to "
           "live (value_flag stays editions-only)" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 14

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def _cellv(v):
    return "" if v is None else str(v)


def live_columns_sha(rows):
    """sha256 of a year's live columns: one line per force sorted by name,
    name|the eight values (NULL as ''), LF-joined, UTF-8. Not the live
    source, which edition 1 does not carry; rows: records (m.held_records
    gives them for the live table and for an edition alike), so the live
    table before the migration and edition 1 after it hash the same."""
    lines = sorted("|".join([r[m.KEY]] + [_cellv(r[c]) for c in m.VALUES])
                   for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def w1_read_sha(rows):
    """sha256 of what W1 reads of a year: one line per force sorted by name,
    name|cases_discussed|cases_per_10k_adult_females (NULL as ''),
    LF-joined."""
    lines = sorted("|".join([r[m.KEY]] + [_cellv(r[c])
                                          for c in m.MAP_COLUMNS])
                   for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _note_lines(note, label):
    """{year: (rows, sha256)} from lines `<label> marac_cases <year>
    rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the decision
    note (the two halves joined), or {}. A `before-state-all ...` line is a
    different record and does not match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                rf"(?m)^{re.escape(label)} marac_cases ([0-9]{{4}}-[0-9]{{2}})"
                r" rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text)}


def note_hashes(note):
    return _note_lines(note, "before-state")


def w1_note_hashes(note):
    return _note_lines(note, "w1-read")


def note_lines_for(cur, spec=REAL, added=ADDED_FORCES):
    """The decision note's hash lines for the live table as it stands
    (read-only): a before-state line per year and the w1-read line of the
    map year over the held forces (less `added`). Print them before the
    migration."""
    out = []
    periods = _live_periods(cur, spec)
    for p in periods:
        recs = m.held_records(cur, spec, p)
        out.append(f"before-state marac_cases {p} rows={len(recs)} "
                   f"{hash_fields(live_columns_sha(recs))}")
    if MAP_YEAR in periods:
        recs = [r for r in m.held_records(cur, spec, MAP_YEAR)
                if r[m.KEY] not in added]
        out.append(f"w1-read marac_cases {MAP_YEAR} rows={len(recs)} "
                   f"{hash_fields(w1_read_sha(recs))}")
    return out


def real_edition1_hash(cur, spec=REAL, note=NOTE, legacy_rows=None,
                       years=None):
    """Edition 1 'as loaded' of every migrated year equals the before-state
    line in the decision note: the row count and live_columns_sha (the eight
    values), and the rows over all years are the surveyed live count."""
    _need(cur, spec)
    legacy_rows = m.LEGACY_LIVE[0] if legacy_rows is None else legacy_rows
    years = len(m.LEGACY_LIVE[3]) if years is None else years
    recorded = note_hashes(note)
    bad, parts = [], []
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    cur.execute(f"SELECT DISTINCT {m.PERIOD} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "release_label = %s ORDER BY 1", (core.AS_LOADED_LABEL,))
    migrated = [str(r[0]) for r in cur.fetchall()]
    if not migrated:
        bad.append("no edition 1 'as loaded' (migrate-legacy not run)")
    elif len(migrated) != years:
        bad.append(f"{len(migrated)} migrated year(s), surveyed {years}")
    total = 0
    for p in migrated:
        recs = m.held_records(cur, spec, p, 1)
        n, h = len(recs), live_columns_sha(recs)
        total += n
        if Path(note).exists():
            if p not in recorded:
                bad.append(f"the decision note records no 'before-state "
                           f"marac_cases {p} rows=.. sha256-first32=.. "
                           "sha256-last32=..' line")
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


def gate_14_before_state(cur):
    name = ("edition 1 equals the before-state hashes recorded in the "
            "decision note")
    out = {}

    def body(e, path, files, label):
        pre = m.held_records(e.cur, ZZ, label)
        before = (len(pre), live_columns_sha(pre))
        rc, text, _ = migrate_cmd(e, files, "--commit")
        if rc != 0:
            out["error"] = text[-200:]
            return
        ed1 = m.held_records(e.cur, ZZ, label, 1)
        out["same"] = (len(ed1), live_columns_sha(ed1)) == before
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = (f"before-state marac_cases {label} rows={before[0]} "
                    f"{hash_fields(before[1])}")
            kw = dict(legacy_rows=before[0], years=1)
            note.write_text("intro\n" + line + "\n"
                            f"before-state-all marac_cases {label} "
                            f"rows={before[0]} {hash_fields('1' * 64)}\n",
                            encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            note.write_text(line.replace(before[1][:32], "0" * 32) + "\n",
                            encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            note.write_text(line.replace("before-state ",
                                         "before-state-all ") + "\n",
                            encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            note.write_text(line.replace(f"rows={before[0]}",
                                         f"rows={before[0] + 1}") + "\n",
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                **kw)
            note.write_text(line + "\n", encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(
                e.cur, ZZ, note, legacy_rows=before[0] + 5, years=1)
            out["wrong_years"] = real_edition1_hash(
                e.cur, ZZ, note, legacy_rows=before[0], years=2)
            note.write_text(line + "\n" + line.replace(label, "2019-20")
                            + "\n", encoding="utf-8")
            out["extra_line"] = real_edition1_hash(e.cur, ZZ, note, **kw)
            out["scan_safe"] = not re.search(r"[0-9a-f]{64}", line)
            # the hash sees each value and the name; not the source or flag
            rows = [dict(r) for r in ed1]
            base = live_columns_sha(rows)
            rows[0]["cases_discussed"] = (rows[0]["cases_discussed"]
                                          or 0) + 1
            out["sees_value"] = live_columns_sha(rows) != base
            rows = [dict(r, live_source="x", value_flag="not_submitted")
                    for r in ed1]
            out["blind_to_source"] = live_columns_sha(rows) == base
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET cases_discussed = "
                      "cases_discussed + 1 WHERE pfa_name_safelives = "
                      "'Suffolk'")
        out["changed"] = live_columns_sha(m.held_records(
            e.cur, ZZ, label)) != before[1]
        rc, text, _ = migrate_cmd(e, files, "--commit")
        out["twice"] = rc == "halt" and "runs once" in text
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(14, name, False, _first(ex))
    ok = (out.get("same") and out.get("ok", (0,))[0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and not out["wrong_years"][0]
          and not out["extra_line"][0] and out.get("sees_value")
          and out.get("blind_to_source") and out.get("scan_safe")
          and out.get("changed") and out.get("twice"))
    mixed(14, name, ok,
          "a seeded migration stores an edition 1 whose hash (the eight "
          "values) equals the live rows' before it and the note's line; a "
          "wrong hash, a wrong row count, a missing line (a "
          "before-state-all line does not count), an extra line, a wrong "
          "survey and a missing note fail; a later change shows; a second "
          "migration is refused" if ok else f"seeded: {out}", cur,
          real_edition1_hash)


# --------------------------------------------------------------- gate 15

def real_map(cur, spec=REAL, note=NOTE, year=MAP_YEAR, held=HELD_FORCES,
             added=ADDED_FORCES, expect=None, councils=None, mapping=True):
    """The map year in live: the held forces' cases_discussed and
    cases_per_10k_adult_females (count and w1_read_sha) equal the decision
    note's w1-read line taken before any change, so no value W1 read moved;
    each added force has its row with the values the 2025-26 file publishes
    (the councils mapped to it gain them); every authority of la_pfa_mapping
    has a value. W1 reads MAX(financial_year): the gate is pinned to `year`."""
    _need(cur, spec)
    expect = WM_MAP if expect is None else expect
    councils = WM_COUNCILS if councils is None else councils
    if not Path(note).exists():
        return False, f"decision note {Path(note).name} not written"
    recorded = w1_note_hashes(note).get(year)
    if recorded is None:
        return False, (f"the decision note records no 'w1-read marac_cases "
                       f"{year} rows=.. sha256-first32=.. sha256-last32=..' "
                       "line")
    cur.execute(f"SELECT MAX({m.PERIOD}) FROM public.{spec.live_table}")
    newest = cur.fetchone()[0]
    bad = []
    if newest != year:
        bad.append(f"W1 reads {newest}, this gate is pinned to {year}: "
                   "review the map change and the note")
    rows = m.held_records(cur, spec, year)
    heldrows = [r for r in rows if r[m.KEY] not in added]
    n, h = len(heldrows), w1_read_sha(heldrows)
    if n != held:
        bad.append(f"live {year} has {n} held forces, expected {held}")
    if recorded != (n, h):
        bad.append(f"live {year} cases_discussed and "
                   f"cases_per_10k_adult_females of the held forces: {n} "
                   f"rows sha256 {h[:16]}.. differs from the note's "
                   f"{recorded[0]} rows {recorded[1][:16]}..")
    by = {r[m.KEY]: r for r in rows}
    for a in added:
        r = by.get(a)
        if r is None:
            bad.append(f"{a} has no {year} row in live (refresh-latest "
                       "--accept-key-changes not yet run?)")
            continue
        for c in m.MAP_COLUMNS:
            if r[c] != expect[c]:
                bad.append(f"{a} {year} {c} is {r[c]}, expected {expect[c]}")
    gain = 0
    if mapping:
        cur.execute("SELECT pfa_name_safelives, COUNT(*) FROM "
                    "public.la_pfa_mapping GROUP BY 1")
        per = dict(cur.fetchall())
        miss = sorted(k for k in per if k not in by)
        if miss:
            bad.append(f"forces {miss[:3]} of la_pfa_mapping have no {year} "
                       "row")
        for a in added:
            if per.get(a) != councils.get(a):
                bad.append(f"la_pfa_mapping maps {per.get(a)} councils to "
                           f"{a}, expected {councils.get(a)}")
            gain += per.get(a, 0)
        total = sum(per.values())
        detail_map = (f"{total - gain} of {total} authorities read "
                      f"unchanged values, {gain} gain {', '.join(added)}")
    else:
        detail_map = "mapping not checked"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live {year}: the {n} held forces' cases_discussed and "
        f"cases_per_10k_adult_females hash to the note's w1-read line "
        f"({h[:16]}..); " + "; ".join(
            f"{a} {by[a]['cases_discussed']} cases, "
            f"{by[a]['cases_per_10k_adult_females']} per 10k"
            for a in added) + f"; {detail_map}")


def gate_15_map(cur):
    name = ("the map year 2025-26: the held forces equal the decision note's "
            "w1-read line, and West Midlands gains its values")
    out = {}
    wm = {"cases_discussed": 7810, "cases_per_10k_adult_females": 65.903502}
    exp = {"cases_discussed": Decimal("7810.00"),
           "cases_per_10k_adult_females": Decimal("65.903502")}

    def body(e, path, files, label):
        held = [r for r in m.held_records(e.cur, ZZ, label)
                if r[m.KEY] not in ADDED_FORCES]
        before = w1_read_sha(held)
        n = len(held)
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = (f"w1-read marac_cases {label} rows={n} "
                    f"{hash_fields(before)}")
            note.write_text("intro\n" + line + "\n", encoding="utf-8")
            kw = dict(note=note, year=label, held=n, expect=exp,
                      councils={"West Midlands": 0}, mapping=False)
            out["before_refresh"] = real_map(e.cur, ZZ, **kw)
            rc, text, _ = migrate_cmd(e, files, "--commit")
            if rc != 0:
                out["error"] = text[-200:]
                return
            go_live(e, path, label)
            out["base"] = real_map(e.cur, ZZ, **kw)
            out["scan_safe"] = not re.search(r"[0-9a-f]{64}", line)
            note.write_text("intro\n", encoding="utf-8")
            out["no_line"] = real_map(e.cur, ZZ, **kw)
            note.write_text(line.replace("w1-read", "w1-read-all") + "\n",
                            encoding="utf-8")
            out["other_label"] = real_map(e.cur, ZZ, **kw)
            note.write_text(line.replace(f"rows={n}", f"rows={n + 1}")
                            + "\n", encoding="utf-8")
            out["bad_rows"] = real_map(e.cur, ZZ, **kw)
            out["no_note"] = real_map(e.cur, ZZ, **dict(
                kw, note=Path(tmp) / "x"))
            note.write_text("intro\n" + line + "\n", encoding="utf-8")
            live = ZZ.live_table
            plants = {
                "a held force's cases": (
                    f"UPDATE public.{live} SET cases_discussed = "
                    "cases_discussed + 1 WHERE pfa_name_safelives = "
                    "'Cumbria'", "differs from the note"),
                "a held force's rate": (
                    f"UPDATE public.{live} SET cases_per_10k_adult_females "
                    "= NULL WHERE pfa_name_safelives = 'Cumbria'",
                    "differs from the note"),
                "a held force removed": (
                    f"DELETE FROM public.{live} WHERE pfa_name_safelives = "
                    "'Suffolk'", "held forces, expected"),
                "West Midlands' cases": (
                    f"UPDATE public.{live} SET cases_discussed = 1 WHERE "
                    "pfa_name_safelives = 'West Midlands'", "expected"),
                "West Midlands' rate": (
                    f"UPDATE public.{live} SET cases_per_10k_adult_females "
                    "= NULL WHERE pfa_name_safelives = 'West Midlands'",
                    "expected"),
                "West Midlands removed": (
                    f"DELETE FROM public.{live} WHERE pfa_name_safelives = "
                    "'West Midlands'", "has no"),
            }
            out["plants"] = {}
            for k, (sql, needle) in plants.items():
                with planted(e.cur, sql):
                    ok_, detail = real_map(e.cur, ZZ, **kw)
                out["plants"][k] = (not ok_) and needle in detail
            with planted(e.cur, f"UPDATE public.{live} SET financial_year "
                         "= '2030-31' WHERE financial_year = '%s'" % label):
                ok_, detail = real_map(e.cur, ZZ, **kw)
            out["pinned"] = (not ok_)
    try:
        legacy_world(cur, body, values={"West Midlands": wm})
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(15, name, False, _first(ex))
    ok = (out.get("base", (0,))[0] and out.get("scan_safe")
          and not out["no_line"][0] and not out["other_label"][0]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and all(out["plants"].values()) and out["pinned"]
          and not out["before_refresh"][0]
          and "West Midlands has no" in out["before_refresh"][1])
    mixed(15, name, ok, "seeded: before refresh-latest the held forces "
          "already equal the note and West Midlands has no row; after the "
          "load and refresh-latest --accept-key-changes the held forces "
          "still equal the note's w1-read line and West Midlands carries its "
          "values; a changed held cases or rate, a removed force, a wrong or "
          "missing West Midlands row, a missing or other-labelled line, a "
          "wrong row count, a missing note and a newer year each fail"
          if ok else f"seeded: {out}", cur, real_map)


# --------------------------------------------------------------- gate 16

def _eq(a, b):
    return (a is None and b is None) or (
        a is not None and b is not None and Decimal(str(a)) == Decimal(str(b)))


def real_migration_proof(cur, spec=REAL, files=None, names=None, expect=None,
                         entry="default", added=ADDED_FORCES):
    """The migration proof re-run read-only: each held file (found by path
    and sha256) read by the loader's own records reproduces every value of
    edition 1 'as loaded' except (a) cells the held table has as NULL and the
    file publishes as 0 with no rule, which are exactly the cells of the
    named entry; (b) cells a named rule turns to NULL from a held value; and
    forces only in the files, which are exactly `added`. The value_flag of
    edition 1 is the file's reason where every NULL of the row is a NULL of
    the file, else NULL; a year gets a ledger row for its file exactly when
    it has no difference. `expect` pins the counts."""
    _need(cur, spec)
    files = files or m.LEGACY_FILES
    names = m.pfa_names(cur) if names is None else names
    if entry == "default":
        entry = m.ACKNOWLEDGED_FLIPS.get(FIRST_LOAD_ENTRY)
    cur.execute(f"SELECT DISTINCT {m.PERIOD} FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "release_label = %s ORDER BY 1", (core.AS_LOADED_LABEL,))
    periods = [str(r[0]) for r in cur.fetchall()]
    if not periods:
        return False, "no edition 1 'as loaded'"
    if periods != sorted(files):
        return False, (f"edition 1 'as loaded' is held for {periods}, the "
                       f"files are {sorted(files)}")
    diffs, restored, rule_held, keys_added, rows = [], [], [], 0, 0
    for y in periods:
        p, wb = held_file(files, y)
        if p is None:
            diffs.append(wb)
            continue
        recs = m.records(wb, y, names)
        held = {r[m.KEY]: r for r in m.held_records(cur, spec, y, 1)}
        got = {r[m.KEY]: r for r in recs}
        cells = {(a["pfa"], a["column"]) for a in recs.applied}
        n_diff = 0
        diffs += [f"{y} {k}: held, not in the file" for k in sorted(
            set(held) - set(got))]
        for k in sorted(set(got) - set(held)):
            if k in added:
                keys_added += 1
                n_diff += 1
            else:
                diffs.append(f"{y} {k}: in the file, not held")
        for k in sorted(set(held) & set(got)):
            rows += 1
            nulls = [c for c in m.VALUES if held[k][c] is None]
            for c in m.VALUES:
                hv, fv = held[k][c], got[k][c]
                if _eq(hv, fv):
                    continue
                if hv is None and fv is not None and fv == 0 \
                        and (k, c) not in cells:
                    restored.append((y, f"{k}/{c}", m._text(fv)))
                    n_diff += 1
                elif fv is None and (k, c) in cells:
                    rule_held.append((y, f"{k}/{c}"))
                    n_diff += 1
                else:
                    diffs.append(f"{y} {k} {c}: held {hv!r}, file {fv!r}")
            want = got[k]["value_flag"] if nulls and all(
                got[k][c] is None for c in nulls) else None
            if held[k]["live_source"] != core.AS_LOADED_LABEL:
                diffs.append(f"{y} {k}: edition 1 is not 'as loaded'")
            cur.execute(f"SELECT value_flag FROM public.{spec.editions_table}"
                        f" WHERE {m.KEY} = %s AND {m.PERIOD} = %s AND "
                        "edition = 1", (k, y))
            if cur.fetchone()[0] != want:
                diffs.append(f"{y} {k}: edition 1 value_flag is not the "
                             "file's reason")
        cur.execute(f"SELECT COUNT(*) FROM public.{ledger_name(spec)} WHERE "
                    f"{m.PERIOD} = %s AND outcome = 'unchanged' AND edition "
                    "= 1 AND file_sha256 = %s", (y, files[y]["sha256"]))
        has = cur.fetchone()[0] > 0
        if has != (n_diff == 0):
            diffs.append(f"{y}: {n_diff} difference(s) but "
                         f"{'a' if has else 'no'} ledger row for the file")
    if entry is not None:
        want_cells = {(y, k, new) for y, d in entry["periods"].items()
                      for k, (old, new) in d.items()}
        if set(restored) != want_cells:
            diffs.append(
                f"restored cells differ from the named entry: only in the "
                f"files {sorted(set(restored) - want_cells)[:2]}, only in "
                f"the entry {sorted(want_cells - set(restored))[:2]}")
    got_counts = {"rows": sum(len(m.held_records(cur, spec, y, 1))
                              for y in periods),
                  "restored": len(restored), "keys_added": keys_added,
                  "rule_held": len(rule_held)}
    if expect and got_counts != expect:
        diffs.append(f"proof counts {got_counts}, surveyed {expect}")
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{rows:,} rows of edition 1 over {len(periods)} "
                       f"years equal the held files read now (0 "
                       f"differences): {len(restored)} NULL-to-0 cells that "
                       f"are exactly the named entry, {keys_added} "
                       f"{'/'.join(added)} key(s) only in the files, "
                       f"{len(rule_held)} held value(s) a rule would turn "
                       "to NULL")


def gate_16_migration_proof(cur):
    name = ("the migration proof re-run read-only from the files on disk; "
            "seeded: the proof passes, a planted held difference stops "
            "migrate-legacy and stores nothing")
    problems = []

    def ok_world(e, path, files, label):
        rc, text, _ = migrate_cmd(e, files, "--commit")
        if rc != 0 or "differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        ent = entry_for(label)
        ok, detail = real_migration_proof(e.cur, ZZ, files, PFAS, entry=ent)
        if not ok:
            problems.append(f"re-run from disk: {detail}")
        ok, detail = real_migration_proof(
            e.cur, ZZ, {label: dict(files[label], sha256="0" * 64)}, PFAS,
            entry=ent)
        if ok or "sha256" not in detail:
            problems.append("a file with another sha256 was accepted")
        ok, detail = real_migration_proof(
            e.cur, ZZ, files, PFAS, entry=entry_for(label, {
                "City of London/housing_referrals": (None, "0.00")}))
        if ok or "named entry" not in detail:
            problems.append("a restored cell outside the named entry passed")
        other = e.file(register=False, values={
            "Cumbria": {"cases_discussed": 7}})
        ok, detail = real_migration_proof(
            e.cur, ZZ, {label: {"path": other,
                                "sha256": m.content_sha256(other)}}, PFAS,
            entry=ent)
        if ok or "differences" not in detail:
            problems.append("a file that differs from edition 1 passed")
        ed = ZZ.editions_table
        for what, sql in (
            ("a value", f"UPDATE public.{ed} SET cases_discussed = "
             "cases_discussed + 1 WHERE pfa_name_safelives = 'Cumbria'"),
            ("a NULL", f"UPDATE public.{ed} SET marac_count = NULL WHERE "
             "pfa_name_safelives = 'Cumbria'"),
            ("a flag", f"UPDATE public.{ed} SET repeat_cases = NULL, "
             "value_flag = 'not_submitted' WHERE pfa_name_safelives = "
             "'Cumbria'"),
            ("a deleted row", f"DELETE FROM public.{ed} WHERE "
             "pfa_name_safelives = 'Suffolk'"),
            ("a label", f"UPDATE public.{ed} SET release_label = 'x' WHERE "
             "pfa_name_safelives = 'Cumbria'"),
        ):
            with planted(e.cur, sql, editions=True):
                ok, detail = real_migration_proof(e.cur, ZZ, files, PFAS,
                                                  entry=ent)
            if ok or "differences" not in detail:
                problems.append(f"edition 1 with {what} changed passed the "
                                f"re-run: {detail[:80]}")
        ok, detail = real_migration_proof(
            e.cur, ZZ, files, PFAS, entry=ent,
            expect={"rows": 1, "restored": 0, "keys_added": 0,
                    "rule_held": 0})
        if ok or "surveyed" not in detail:
            problems.append("the surveyed counts are not enforced")

    def planted_world(sql, needle, surveyed_first=False, explained=None):
        def f(e, path, files, label):
            surveyed = m.live_state(e.cur) if surveyed_first else None
            e.cur.execute(sql)
            surveyed = surveyed or m.live_state(e.cur)
            before = state(e.cur)
            with mock.patch.object(m, "LEGACY_LIVE", surveyed), \
                    mock.patch.object(m, "LEGACY_FILES", files), \
                    mock.patch.object(m, "LEGACY_PFA_MAPPING",
                                      m.mapping_state(e.cur)), \
                    mock.patch.object(
                        m, "LEGACY_EXPLAINED_KEYS",
                        {"West Midlands": "test"} if explained is None
                        else explained):
                rc, text, logged = e.cmd(["migrate-legacy", "--commit"])
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    live = ZZ.live_table
    legacy_world(cur, ok_world)
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET cases_discussed = cases_discussed + 1 "
        "WHERE pfa_name_safelives = 'Suffolk'", "proof failed"))
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET marac_count = marac_count + 1 WHERE "
        "pfa_name_safelives = 'Cumbria'", "proof failed"))
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET housing_referrals = 5 WHERE "
        "pfa_name_safelives = 'City of London'", "proof failed"))
    legacy_world(cur, planted_world(
        f"UPDATE public.{live} SET pfa_name_safelives = 'Mystery' WHERE "
        "pfa_name_safelives = 'Suffolk'", "proof failed"))
    legacy_world(cur, planted_world(
        f"DELETE FROM public.{live} WHERE pfa_name_safelives = 'Suffolk'",
        "not as surveyed", surveyed_first=True))
    legacy_world(cur, planted_world(
        "SELECT 1", "West Midlands", explained={}))
    mixed(16, name, not problems,
          "seeded migration proof passes and re-runs from the file by "
          "sha256; a changed or missing file, a restored cell outside the "
          "named entry, a file that is not edition 1 and a changed value, "
          "NULL, flag, label or row in edition 1 are caught; planted "
          "differences in a value, an integer, a restored zero and a force, "
          "an unexplained key and a dropped row stop migrate-legacy with "
          "nothing stored" if not problems else "; ".join(problems[:3]),
          cur, lambda c: real_migration_proof(c, REAL,
                                               expect=SURVEYED_PROOF))


# ---------------------------------------------------------- gates 17 - 19

def gate_17_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        path = e.file(values={"Norfolk": {c: "No Data" for c in
                                          m.CASES_COLUMNS}})
        e.ok(e.cur, ["load", "--commit"])
        snap, led = tables_state(e.cur), len(tl.ledger(e.cur))
        for argv in (["load", "--commit"],
                     ["load", "--year", Y, "--commit"],
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
        rc, text, logged = e.cmd(["load", "--recheck", Y, "--commit"])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N)]:
            problems.append("a recheck stored an edition")
    scenario(cur, body)
    report(17, name, not problems, "a second load by the page, --year, "
           "--file and --file --no-page and a refresh-latest stored no "
           "edition and changed no live row (re-reads add 'unchanged' ledger "
           "rows only); a ledger-skipped load logs nothing" if not problems
           else "; ".join(problems[:3]))


def gate_18_stranded(cur):
    name = "stranded year repair (edition held, live rows missing)"
    problems = []

    def body(e):
        e.seed(e.cur)
        e.cur.execute(f"DELETE FROM public.{ZZ.live_table}")
        if pe.status(e.cur, m.profile())["live_missing"] != [Y]:
            problems.append("status does not name the stranded year")
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0 or "live-missing" not in text or \
                tl.editions(e.cur) != [(1, None, N)] or \
                tl.count(e.cur, ZZ.live_table) != N:
            problems.append(f"repair failed: rc {rc}: {text[-120:]}")
        if "live-missing" not in [o for _, _, o, _ in tl.ledger(e.cur)]:
            problems.append("no live-missing ledger row")
        if core.rows_differing(e.cur, ZZ, Y, 1) or not pe.status(
                e.cur, m.profile())["ok"]:
            problems.append("the repaired rows do not equal the tip")
        if "sha256" not in (tl.live_val(e.cur, "Cumbria", "source") or ""):
            problems.append("the repaired rows lost the edition's label as "
                            "their source")
    scenario(cur, body)
    report(18, name, not problems, "a stranded year was rebuilt from its "
           "edition 1 with no new edition and a live-missing ledger row"
           if not problems else "; ".join(problems[:3]))


def gate_19_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.live_val(e.cur, "Cumbria", "cases_discussed")
        e.revise(values={"Cumbria": {"cases_discussed": 120.5}})
        e.ok(e.cur, ["load", "--commit", "--accept-reissue", Y,
                     "--acknowledge", Y])
        e.ok(e.cur, ["refresh-latest", "--commit"])
        snap = state(e.cur)
        rc, text, logged = e.cmd(["restore-edition", Y, "1"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("restore-edition preview wrote")
        rc, text, logged = e.cmd(["restore-edition", Y, "1", "--commit"])
        if rc != 0 or tl.editions(e.cur)[-1] != (3, 2, N) or \
                not logged.called:
            problems.append(f"restore --commit: rc {rc}: {text[-120:]}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE edition = 3")
        if e.cur.fetchall() != [("restored from edition 1",)]:
            problems.append("the restored edition is not labelled")
        if tl.live_val(e.cur, "Cumbria", "cases_discussed") == first:
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.live_val(e.cur, "Cumbria", "cases_discussed") != first or \
                core.rows_differing(e.cur, ZZ, Y, 3):
            problems.append("refresh-latest did not apply the restored "
                            "edition")
        ed1 = {tuple(sorted(r.items())) for r in m.held_records(
            e.cur, ZZ, Y, 1)}
        ed3 = {tuple(sorted(r.items())) for r in m.held_records(
            e.cur, ZZ, Y, 3)}
        # the label differs by design; the values and flags must not
        strip = lambda s: {tuple((k, v) for k, v in r if k != "live_source")
                           for r in s}
        if strip(ed1) != strip(ed3):
            problems.append("edition 3 does not hold edition 1's rows")
        for argv in (["restore-edition", Y, "3", "--commit"],
                     ["restore-edition", Y, "9", "--commit"]):
            ok_, _ = e.halts(argv, state(e.cur))
            if not ok_:
                problems.append(f"{' '.join(argv[:3])} did not halt")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
    scenario(cur, body)
    report(19, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') and applied by refresh-latest; the "
           "tip and a missing edition are refused; a preview writes nothing"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 20

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


def gate_20_no_network_no_secrets(cur):
    name = ("no network, no secret in source or output, nothing written "
            "outside the rolled-back transaction; a download never "
            "overwrites a same-named file with different content")
    files = [Path(__file__).resolve(), LOADER, HERE / "period_editions.py",
             HERE / "manual_input.py"]
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
        with env(c) as e, tl.specs(e.root, e.names):
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.file()
                rc, _, _ = e.cmd(["load", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load"])
                raw = e.root / "raw"
                a = write_marac(e.root / "dl" / "a.xlsx").read_bytes()
                b = write_marac(e.root / "dl" / "b.xlsx", values={
                    "Cumbria": {"cases_discussed": 7}}).read_bytes()
                url = ("https://safelives.org.uk/wp-content/uploads/"
                       "MARAC-DATA-2025-2026.xlsx")
                with contextlib.redirect_stdout(io.StringIO()):
                    p1, _ = m.fetch(url, raw, Session(Resp(a)))
                    sha1 = m.content_sha256(p1)
                    p2, _ = m.fetch(url, raw, Session(Resp(b)))
                    p3, _ = m.fetch(url, raw, Session(Resp(a)))
                kept = (p1 == raw / "MARAC-DATA-2025-2026.xlsx"
                        and m.content_sha256(p1) == sha1 and p2 != p1
                        and p2.name.startswith("MARAC-DATA-2025-2026-")
                        and p3 == p1)
            return rc, rc_s, rc_r, rc_l, kept
    try:
        rc, rc_s, rc_r, rc_l, kept = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(20, name, False, f"{type(ex).__name__}: {ex}")
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
    hex64 = [n for n, t in texts.items() if re.search(r"[0-9a-f]{64}", t)]
    ok = (not attempts and rc == rc_s == rc_r == rc_l == 0 and kept
          and runs == runs_after and real_before == real_after
          and not literal and not in_src and not in_out and not hex64)
    report(20, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned "
           f"{rc}/{rc_s}/{rc_r}/{rc_l}; a same-named different download "
           f"was saved beside it, the first untouched={kept}; run-log rows "
           f"{runs}->{runs_after}; real editions table and ledger "
           f"unchanged={real_before == real_after}; secret-like literal in "
           f"source {literal or 'none'}; 64-hex run in source "
           f"{hex64 or 'none'}; {len(secrets)} secret-named settings checked "
           f"against {len(files)} files and {len(OUTPUT)} output lines: in "
           f"source {in_src or 'none'}, in output {in_out or 'none'}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_geography, gate_4_blanks_and_zeros, gate_5_reconciliation,
         gate_6_identity, gate_7_older_and_reissue, gate_8_stop_conditions,
         gate_9_one_transaction, gate_10_ledger_rule, gate_11_preview,
         gate_12_reread, gate_13_refresh, gate_14_before_state, gate_15_map,
         gate_16_migration_proof, gate_17_rerun, gate_18_stranded,
         gate_19_restore, gate_20_no_network_no_secrets)


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
        sys.exit("usage: s17_marac_editions_verify.py [--print-note-lines]")
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
    mine = [t for t in left if t.startswith("zz_s17")]
    if mine:
        print(f"LEFTOVER: committed zz_s17 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s17 table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
