"""Gates for the S12 (MHCLG Exceptional Financial Support and section 114
notices) editions tables.

Mirrors scripts/s22_ctb_editions_verify.py (several specs),
s13_lahs_editions_verify.py and s10_rough_sleeping_editions_verify.py.
Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes nothing, not
even a run-log row. The real-table gates read la_efs_support,
la_s114_notices, their editions tables la_efs_support_editions and
la_s114_notices_editions, the two <editions>_file_checks ledgers, the seven
saved year pages in data/raw/s12_efs and the decision note. Until the editions
tables exist (ddl and migrate-legacy not yet run) the gates that need them
print `FAIL (pending migration)` with the words "not yet migrated", so the
script exits non-zero until then, like the other verifiers; nothing is
created to make them pass.

The seeded gates never need the real editions tables: they run on the
throwaway tables zz_s12_efs_live / zz_s12_efs_editions /
zz_s12_efs_editions_file_checks and the same three for S.114 (copies of the
loader's two specs), created inside the transaction, and call the loader's
own writer and commands (s12_financial_stress_editions.main with the SPECS
pointed at the copies, the content API stubbed, the page and register files
written into a temporary directory that is the only allowed root;
migrate_legacy; restore_edition) rather than a re-implementation. The real
tables are only ever read.

Gates:
  1  both editions tables, both ledgers, append-only triggers (UPDATE,
     DELETE, TRUNCATE refused), the CHECKs, live columns   (seeded + real)
  2  every live period has edition 1; the latest edition equals live, both
     tables                                                (seeded + real)
  3  names: no fuzzy matching in the loader (AST), every alias in
     s12_efs_names.json names a real lad24cd and was seen on a held page,
     match_name is exact; Haringey is E09000014
  4  the misattribution regression: no E09000013 in 2025-26 or 2026-27
     unless a page names Hammersmith and Fulham; no alias maps there
  5  every page row is stored, excluded by name or halts (the saved pages
     re-read read-only); a halt stores nothing             (seeded + real)
  6  grammar coverage: an unknown cell form halts, never read as its first
     amount; every cell of the held pages parses; every stored cell_text
     re-parses to the stored values                        (seeded + real)
  7  amount_m is NULL exactly for the statuses withdrawn and other-years-only
     (rule 1), in the editions CHECK, the loader and the rows; a published 0
     stays 0                                               (seeded + real)
  8  S.114: the attribution CHECK; the predecessor rows unchanged between
     editions (gate 14 semantics); the unevidenced count reported
                                                           (seeded + real)
  9  the map sets: distinct lad24cd in la_efs_support (the before-state set
     less the two WITHDRAWN_ONLY_NOT_SUPPORT codes, E06000058 and
     E06000061; Bexley E09000004 is kept) and in la_s114_notices
     equal the decision note's w1-read lines               (seeded + real)
 10  older-page guard: the rank is the page's own (never the file name); an
     older register halts; --allow-older-file is logged
 11  stop conditions: each seeded REJECTED (or halted), stores nothing, no
     ledger row, partial run-log row
 12  a new year: edition, live rows and ledger row in one transaction (both
     tables)
 13  ledger rule: a skip needs the (page or register, sha256) pair
 14  preview and simulate write nothing, every writing command
 15  a byte-identical re-read is `unchanged` on every path, against edition 1
     "as loaded" too
 16  refresh-latest changes only the revised years, copies loaded_at and
     source; key changes only with --accept-key-changes
 17  edition 1 equals the decision note's before-state lines (seeded + real)
 18  rerun idempotent
 19  stranded year repair
 20  restore-edition round trip, both tables
 21  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content

The decision note's hash lines are scan-safe: a 64-hex run would be flagged
by the credential scan, so a sha256 is written as two 32-hex halves in two
labelled fields:
    before-state la_efs_support <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    before-state la_s114_notices <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-before la_efs_support lad24cd-set rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read la_efs_support lad24cd-set rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
    w1-read la_s114_notices lad24cd-set rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
A before-state line hashes a year's live rows before the migration (EFS:
lad24cd|amount_m|status|hra_only; S.114: lad24cd|notice_date|financial_year|
reason|date_confirmed|attribution|successor_codes|attribution_note; rows
sorted, LF-joined, NULL as ''; not the live source, which edition 1 does not
carry). w1-before is the set of lad24cd with any EFS row before the
migration; w1-read for EFS is that set less the codes of
WITHDRAWN_ONLY_NOT_SUPPORT (Scott, 2026-10-10: the EFS flag is dropped for
Bournemouth, Christchurch and Poole E06000058 and North Northamptonshire
E06000061; Bexley E09000004 is kept, in the rule's 'kept' list), and for
S.114 the set of lad24cd with any notice, less the authorities whose every notice is a named
removal in S114_REMOVALS (Hillingdon E09000017 after Task 7: 10 authorities). The set hash is sha256 of the sorted codes, LF-joined.
`python scripts/s12_financial_stress_editions_verify.py --print-note-lines`
prints them for the live tables as they stand (read-only); run it before
migrate-legacy and paste the lines into the decision note.

Usage:
    python scripts/s12_financial_stress_editions_verify.py
    python scripts/s12_financial_stress_editions_verify.py --print-note-lines

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
import html
import io
import json
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
from _db import ENV, get_conn, get_readonly_conn  # noqa: E402
import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s12_financial_stress_editions as m  # noqa: E402
import test_s12_loader as tl  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real specs and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPECS, which the seeded gates point
# at the copies)
REAL_EFS = m.SPEC_EFS
REAL_S114 = m.SPEC_S114
RULE = m.WITHDRAWN_ONLY_NOT_SUPPORT
RULE_CODES = frozenset(RULE["codes"])
ZZ_EFS = tl.ZZ_EFS
ZZ_S114 = tl.ZZ_S114
LEDGER_EFS = tl.LEDGER_EFS
LEDGER_S114 = tl.LEDGER_S114
TABLES = tl.TABLES
LOADER = HERE / "s12_financial_stress_editions.py"
NAMES = HERE / "s12_efs_names.json"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-10-s12-editions-first-load.md")
EFS_T = "la_efs_support"
S114_T = "la_s114_notices"
NOT_YET = "the S12 editions tables do not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (21)
R1, R2, R3 = tl.R1, tl.R2, tl.R3
P25, P26 = "2025-26", "2026-27"
EMPTY = "no live periods to check (an empty state is not a pass)"


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


def exists(cur, efs=REAL_EFS, s114=REAL_S114):
    return all(pe.table_exists(cur, t) for s in (efs, s114)
               for t in (s.editions_table, ledger_name(s)))


def _need(cur, *specs):
    miss = [t for s in specs for t in (s.editions_table, ledger_name(s))
            if not pe.table_exists(cur, t)]
    if miss:
        raise ValueError(f"{', '.join(miss)} does not exist")


def is_s114(spec):
    return tuple(spec.key_cols) == m.S114_KEY


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{t}')"
                                      for t in TABLES))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s12 table already exists as a real table; "
                 "refusing to run")
    cur.execute(f"CREATE TABLE public.{ZZ_EFS.live_table} (LIKE "
                f"public.{REAL_EFS.live_table} INCLUDING ALL)")
    cur.execute(f"CREATE TABLE public.{ZZ_S114.live_table} (LIKE "
                f"public.{REAL_S114.live_table} INCLUDING ALL)")
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
    return s[:2] + s[3:5]


def cnt(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


# ------------------------------------------------------------ files and Env

class Env(tl.MigrateBase):
    """The loader's main(argv) on the throwaway tables, the content API
    stubbed, every page and register written here into a temporary
    directory."""

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api = {}
        self.fetched = []
        self.register = self.root / "la_s114_notices.csv"
        self._stack = contextlib.ExitStack()

    def addCleanup(self, fn, *a, **k):  # noqa: N802 (unittest's name)
        self._stack.callback(fn, *a, **k)

    def close(self):
        self._stack.close()

    def run_main(self, cur, argv):
        res = super().run_main(cur, argv)
        OUTPUT.extend(res[1].splitlines())
        return res

    def cmd(self, argv):
        return self.run_main(self.cur, argv)

    def halts(self, argv, before, needle=None):
        """(True, text) if the command halted (SystemExit) with the state as
        `before` and no run-log row."""
        rc, text, logged = self.cmd(argv)
        ok = (rc == "halt" and state(self.cur) == before
              and not logged.called
              and (needle is None or needle in text))
        return ok, text

    def migrated(self):
        self.legacy(self.cur)
        self.ok(self.cur, ["migrate-legacy", "--commit"])


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        e = Env(cur, tmp)
        try:
            yield e
        finally:
            e.close()


def scenario(cur, fn):
    """Run fn(env) in a savepoint that is rolled back."""
    def body(c):
        with env(c) as e, tl.specs():
            return fn(e)
    return _in_savepoint(cur, body)


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


def counts_of(cur, part, period):
    """(live, editions, ledger) row counts of a part's year."""
    spec = ZZ_EFS if part == "efs" else ZZ_S114
    led = LEDGER_EFS if part == "efs" else LEDGER_S114
    w = "WHERE financial_year = %s"
    return tuple(cnt(cur, t, w, (period,))
                 for t in (spec.live_table, spec.editions_table, led))


def rejects(e, argv, needle, part, period, *, commit=True):
    """None if the command was REJECTED (exit 1) naming `needle`, stored
    nothing for the year, wrote no ledger row for it, and (with --commit)
    wrote one partial run-log row; else the problem."""
    before = counts_of(e.cur, part, period)
    rc, text, logged = e.cmd(argv)
    if rc != 1 or "REJECTED" not in text or needle not in text:
        return f"rc {rc}, not REJECTED naming {needle!r}: {text[-160:]}"
    if counts_of(e.cur, part, period) != before:
        return "something was stored or a ledger row written"
    if commit:
        if logged.call_count != 1:
            return "no partial run-log row"
        notes = logged.call_args[0][2]
        if "PARTIAL RUN (exit 1)" not in notes or \
                f"rejected ['{period}']" not in notes:
            return f"run-log notes: {notes[:140]}"
    elif logged.called:
        return "a preview wrote a run-log row"
    return None


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "varchar": "character varying",
          "text": "text", "date": "date", "numeric": "numeric",
          "boolean": "boolean", "text[]": "ARRAY",
          "timestamptz": "timestamp with time zone"}


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


S114_ACK = ["--acknowledge", "2017-18", "--acknowledge", "2020-21",
            "--acknowledge", "2021-22"]


def seed_both(e, *, efs=True, s114=True):
    """A loaded EFS (2025-26 and 2026-27) and a loaded S.114 register, as the
    loader's own load writes them."""
    if efs:
        e.seed(e.cur, tl.p25(updated=R1), tl.p26(updated=R1))
    if s114:
        e.write_register(tl.ROWS_AS_HELD)
        e.ok(e.cur, ["load", "--only", "s114"] + S114_ACK + ["--commit"])


def gate_1_table_shape_and_immutability(cur):
    name = ("both editions tables, both ledgers, live shape (UPDATE, DELETE "
            "and TRUNCATE are refused on the editions and the ledgers)")
    bad = shape_problems(cur, ZZ_EFS) + shape_problems(cur, ZZ_S114)

    def seed(cur):
        with env(cur) as e, tl.specs():
            seed_both(e)
    targets = []
    for spec, led in ((ZZ_EFS, LEDGER_EFS), (ZZ_S114, LEDGER_S114)):
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
        bad += [f"real: {p}" for s in (REAL_EFS, REAL_S114)
                for p in shape_problems(cur, s)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise "
           "append-only on both editions tables and both ledgers")


# ------------------------------------------------------- real-table gates

def _live_periods(cur, spec):
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [str(r[0]) for r in cur.fetchall()]


def _edition_periods(cur, spec):
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} ORDER BY 1")
    return [str(r[0]) for r in cur.fetchall()]


def real_edition1_and_latest(cur, efs=REAL_EFS, s114=REAL_S114):
    """Every live year has an edition 1, every editions year has live rows,
    and the latest edition equals live (the key, the year and the live
    value columns, NULL-safe, both ways), for both tables. Run after
    refresh-latest: a revision not yet refreshed shows here."""
    _need(cur, efs, s114)
    bad, parts = [], []
    for spec in (efs, s114):
        periods = _live_periods(cur, spec)
        if not periods:
            bad.append(f"{spec.live_table}: {EMPTY}")
            continue
        for p in periods:
            if not cnt(cur, spec.editions_table,
                       f"WHERE {spec.period_col} = %s AND edition = 1", (p,)):
                bad.append(f"{spec.live_table} {p}: no edition 1")
        extra = sorted(set(_edition_periods(cur, spec)) - set(periods))
        if extra:
            bad.append(f"{spec.editions_table}: years with no live rows "
                       f"{extra}")
        bad += m.live_equals_tip(cur, spec, periods)
        parts.append(f"{spec.live_table} {len(periods)} years")
    return not bad, "; ".join(bad[:3]) if bad else (
        "every live year has edition 1 and equals its latest edition ("
        + ", ".join(parts) + ")")


def gate_2_edition1_and_latest(cur):
    name = ("every live year has edition 1; the latest edition equals live, "
            "both tables")
    problems = []

    def body(e):
        ok, d = real_edition1_and_latest(e.cur, ZZ_EFS, ZZ_S114)
        if ok or "no live periods" not in d:
            problems.append(f"empty state: {ok}, {d[:80]}")
        seed_both(e)
        ok, d = real_edition1_and_latest(e.cur, ZZ_EFS, ZZ_S114)
        if not ok:
            problems.append(f"complete state fails: {d}")
        e.cur.execute("SAVEPOINT p")
        e.cur.execute(f"UPDATE public.{ZZ_EFS.live_table} SET amount_m = "
                      "amount_m + 1 WHERE lad24cd = 'E09000008' AND "
                      "financial_year = %s", (P25,))
        ok, d = real_edition1_and_latest(e.cur, ZZ_EFS, ZZ_S114)
        if ok or "live-only" not in d:
            problems.append(f"a drifted live cell passes: {d[:80]}")
        e.cur.execute("ROLLBACK TO SAVEPOINT p")
        e.cur.execute(f"DELETE FROM public.{ZZ_S114.live_table} WHERE "
                      "financial_year = '2017-18'")
        ok, d = real_edition1_and_latest(e.cur, ZZ_EFS, ZZ_S114)
        if ok or "no live rows" not in d:
            problems.append(f"an editions year with no live rows passes: "
                            f"{d[:80]}")
        e.cur.execute("ROLLBACK TO SAVEPOINT p")
    scenario(cur, body)
    mixed(2, name, not problems,
          "an empty state is not a pass; a loaded state passes; a drifted "
          "live cell and an editions year with no live rows fail"
          if not problems else "; ".join(problems[:3]), cur,
          real_edition1_and_latest)


# ---------------------------------------------------------- held pages

def held_pages():
    """{year: path} of the seven held year pages on disk whose sha256 is the
    one listed in LEGACY_PAGES, and a list of problems (a missing or changed
    file)."""
    out, bad = {}, []
    for y, f in sorted(m.LEGACY_PAGES.items()):
        p = Path(f["path"])
        if not p.exists():
            bad.append(f"{p.name} is not on disk")
        elif m.content_sha256(p) != f["sha256"]:
            bad.append(f"{p.name}: sha256 is not the held page's")
        else:
            out[y] = p
    return out, bad


def read_page(cur, path, book=None, excl=None):
    """One saved year page read as the loader reads it: (year, Records,
    raw_rows) where raw_rows is the number of rows in the page's authority
    tables (main, Housing Revenue Account, Police Force, Capitalisation
    support). ValueError when the loader would halt."""
    pg = json.loads(Path(path).read_text(encoding="utf-8"))
    ident = m.page_identity(pg)
    y = ident["year"]
    if book is None:
        boundaries = m._boundaries(cur)
        aliases, excl = m.load_names()
        book = m.NameBook(boundaries, aliases)
    body = (pg.get("details") or {}).get("body")
    m.resolve_directions(m.section_names(body), book, excl)
    dirs = m.resolve_directions(m.capitalisation_directions(body, y), book,
                                excl)
    recs = m.efs_records(pg, book, excl, dirs)
    raw = sum(len(rows) for heading, rows in m.tables(body)
              if m.table_kind(heading)[0] != "other-years")
    return y, recs, raw


def read_all_pages(cur):
    """({year: Records}, {year: raw rows}, problems) for the held pages."""
    files, bad = held_pages()
    boundaries = m._boundaries(cur)
    aliases, excl = m.load_names()
    book = m.NameBook(boundaries, aliases)
    parsed, raw = {}, {}
    for y, p in files.items():
        try:
            yy, recs, n = read_page(cur, p, book, excl)
        except ValueError as e:
            bad.append(f"{p.name}: {_first(e)}")
            continue
        if yy != y:
            bad.append(f"{p.name}: the page is for {yy}, expected {y}")
        parsed[y], raw[y] = recs, n
    return parsed, raw, bad


# ---------------------------------------------------------------- gate 3

FUZZY_MODULES = {"difflib", "rapidfuzz", "fuzzywuzzy", "thefuzz", "jellyfish",
                 "Levenshtein", "leven", "textdistance", "fuzzy"}
FUZZY_NAMES = {"get_close_matches", "SequenceMatcher", "levenshtein",
               "edit_distance", "jaro_winkler"}
CASE_CALLS = {"lower", "upper", "casefold", "title", "swapcase"}
SUBSTRING_CALLS = {"startswith", "endswith", "find", "rfind", "index",
                   "rindex", "search", "match", "fnmatch", "fnmatchcase",
                   "partition", "removeprefix", "removesuffix"}
NAME_FUNCS = {"match_name", "resolve_directions", "load_names",
              "check_aliases", "alias_predecessors",
              "capitalisation_directions", "direction_titles",
              "section_names", "efs_records"}


def fuzzy_problems(source=None):
    """Problems in the loader source (AST): any fuzzy-matching import or call;
    any case-folding call in a name-matching function; any substring test in
    match_name other than membership of its own parameters (the exclusion
    dict)."""
    src = (Path(LOADER).read_text(encoding="utf-8") if source is None
           else source)
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in FUZZY_MODULES:
                    bad.append(f"imports {a.name}")
        if isinstance(node, ast.ImportFrom) and node.module and \
                node.module.split(".")[0] in FUZZY_MODULES:
            bad.append(f"imports from {node.module}")
        if isinstance(node, ast.Attribute) and node.attr in FUZZY_NAMES:
            bad.append(f"uses .{node.attr}")
        if isinstance(node, ast.Name) and node.id in FUZZY_NAMES:
            bad.append(f"uses {node.id}")
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}
    for fname in sorted(NAME_FUNCS & set(funcs)):
        for node in ast.walk(funcs[fname]):
            if isinstance(node, ast.Call) and \
                    isinstance(node.func, ast.Attribute) and \
                    node.func.attr in CASE_CALLS:
                bad.append(f"{fname} calls .{node.func.attr}()")
    mn = funcs.get("match_name")
    if mn is None:
        bad.append("no match_name function")
    else:
        params = {a.arg for a in mn.args.args}
        for node in ast.walk(mn):
            if isinstance(node, ast.Call) and \
                    isinstance(node.func, ast.Attribute) and \
                    node.func.attr in SUBSTRING_CALLS:
                bad.append(f"match_name calls .{node.func.attr}()")
            if isinstance(node, ast.Compare):
                for op, right in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.In, ast.NotIn)) and not (
                            isinstance(right, ast.Name)
                            and right.id in params):
                        bad.append("match_name tests 'in' something other "
                                   "than its own parameters")
    return sorted(set(bad))


def _page_text(path):
    pg = json.loads(Path(path).read_text(encoding="utf-8"))
    body = (pg.get("details") or {}).get("body") or ""
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def seen_problems(files, names_path=None):
    """Every alias and exclusion of the names file names a page it was seen
    on, and that page's text holds the name exactly."""
    raw = json.loads(Path(names_path or m.NAMES_FILE).read_text(
        encoding="utf-8"))
    bad, texts = [], {}
    for kind in ("aliases", "exclusions"):
        for nm, ent in (raw.get(kind) or {}).items():
            if not ent.get("seen"):
                bad.append(f"{nm}: no page it was seen on")
            for s in ent.get("seen") or []:
                y = s.get("year")
                if y not in files:
                    bad.append(f"{nm}: seen on {y}, which is not a held page")
                    continue
                if y not in texts:
                    texts[y] = _page_text(files[y])
                if nm not in texts[y]:
                    bad.append(f"{nm}: not found on the {y} page")
    return bad


def names_problems(cur, names_path=None):
    """Problems with the names file: aliases that are not real lad24cd, are
    lad24nm themselves, or have no la_code_lookup row; and exact matching of
    the names that matter."""
    bad = []
    boundaries = m._boundaries(cur)
    try:
        aliases, excl = m.load_names(names_path)
    except ValueError as e:
        return [_first(e)]
    try:
        m.check_aliases(cur, aliases, boundaries)
    except SystemExit as e:
        bad.append(str(e).splitlines()[0] if str(e) else "check_aliases halted")
    for nm, code in aliases.items():
        if code not in set(boundaries.values()):
            bad.append(f"alias {nm} names {code}, not in la_boundaries")
    for probe, want in (("Haringey", "E09000014"),
                        ("Hammersmith and Fulham", "E09000013")):
        try:
            got = m.match_name(probe, boundaries, aliases, excl)
        except ValueError as e:
            got = _first(e)
        if got != want:
            bad.append(f"{probe} matched {got}, expected {want}")
    for probe in ("Gloucester Council", "Wokin", "haringey", "Woking "):
        try:
            got = m.match_name(probe, boundaries, aliases, excl)
        except ValueError:
            continue
        bad.append(f"{probe!r} matched {got} (a near miss must halt)")
    for probe in sorted(excl):
        if m.match_name(probe, boundaries, aliases, excl) is not None:
            bad.append(f"exclusion {probe} is not skipped")
    return bad


def gate_3_names(cur):
    name = ("names: no fuzzy matching in the loader (AST); every alias names "
            "a real lad24cd and was seen on a held page; match_name is exact")
    problems = fuzzy_problems()
    # the AST check itself: a fuzzy module or a case-folding match must fail
    fake = ("import difflib\n\ndef match_name(name, boundaries, aliases, "
            "exclusions):\n    return [n for n in boundaries if "
            "name.lower() in n.lower()]\n")
    if len(fuzzy_problems(fake)) < 2:
        problems.append("the AST check does not catch a fuzzy match")
    files, bad = held_pages()
    problems += bad
    problems += names_problems(cur)
    problems += seen_problems(files)
    n_alias = len(json.loads(NAMES.read_text(encoding="utf-8"))["aliases"])
    report(3, name, not problems, "; ".join(problems[:3]) if problems else
           "no difflib, no case-folding or substring matching in the name "
           f"functions; the {n_alias} aliases and the exclusions name a real "
           "code (or a body) and appear on a held page; Haringey is "
           "E09000014; near misses halt")


# ---------------------------------------------------------------- gate 4

def page_names(path):
    pg = json.loads(Path(path).read_text(encoding="utf-8"))
    body = (pg.get("details") or {}).get("body")
    return {n for heading, rows in m.tables(body)
            if m.table_kind(heading)[0] != "other-years" for n, _ in rows}


def real_misattribution(cur, efs=REAL_EFS, files=None):
    """No E09000013 row in 2025-26 or 2026-27 (live and the editions) unless
    a held page of that year names Hammersmith and Fulham."""
    files = held_pages()[0] if files is None else files
    named = {y for y, p in files.items()
             if "Hammersmith and Fulham" in page_names(p)}
    bad = []
    for y in (P25, P26):
        if y in named:
            continue
        for t in (efs.live_table, efs.editions_table):
            if pe.table_exists(cur, t) and cnt(
                    cur, t, "WHERE lad24cd = 'E09000013' AND "
                    "financial_year = %s", (y,)):
                bad.append(f"{t} holds E09000013 for {y}, whose page does "
                           "not name Hammersmith and Fulham")
    return not bad, "; ".join(bad) if bad else (
        "no E09000013 row in 2025-26 or 2026-27 in live or the editions (no "
        "held page names Hammersmith and Fulham)")


def gate_4_misattribution(cur):
    name = ("misattribution regression: Haringey is E09000014, never "
            "E09000013; no E09000013 in 2025-26 or 2026-27 unless a page "
            "names Hammersmith and Fulham")
    problems = []
    raw = json.loads(m.NAMES_FILE.read_text(encoding="utf-8"))
    for nm, a in (raw.get("aliases") or {}).items():
        if a.get("lad24cd") == "E09000013" and "Hammersmith" not in nm:
            problems.append(f"alias {nm} maps to E09000013")
    problems += names_problems(cur)
    live_ok, live_detail = real_misattribution(cur)
    if not live_ok:
        problems.append(live_detail)

    def body(e):
        e.site(tl.p25(updated=R1), tl.p26(updated=R1))
        e.ok(e.cur, ["load", "--only", "efs", "--commit"])
        e.cur.execute(f"SELECT lad24cd FROM public.{ZZ_EFS.live_table} "
                      "WHERE financial_year = %s ORDER BY 1", (P26,))
        got = [r[0] for r in e.cur.fetchall()]
        if "E09000014" not in got or "E09000013" in got:
            problems.append(f"Haringey 2026-27 stored as {got}")
        e.site(tl.p25(updated=R2, extra_rows=[(
            "Hammersmith and Fulham", "£1.0m (support agreed in-principle)")]),
            tl.p26(updated=R1))
        e.ok(e.cur, ["load", "--only", "efs", "--acknowledge", P25,
                     "--commit"])
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ_EFS.editions_table} "
                      "WHERE lad24cd = 'E09000013' AND financial_year = %s "
                      "AND edition = 2", (P25,))
        if e.cur.fetchone()[0] != 1:
            problems.append("a page that names Hammersmith and Fulham does "
                            "not store E09000013")
    try:
        scenario(cur, body)
    except (AssertionError, SystemExit) as ex:
        problems.append(f"seeded load: {_first(ex)}")
    mixed(4, name, not problems,
          "no alias maps to E09000013; Haringey stores as E09000014 and "
          "Hammersmith and Fulham as E09000013 only when the page names it"
          if not problems else "; ".join(problems[:3]), cur,
          lambda c: real_misattribution(c))


# ---------------------------------------------------------------- gate 5

def real_page_rows(cur, efs=REAL_EFS, parsed=None, raw=None, bad=None):
    """Every row of every held page is stored (a record), excluded by name
    (counted) or the page halts: records + excluded == raw rows; with the
    editions, every record outside the withdrawn-only rule is in an edition
    of its year."""
    if parsed is None:
        parsed, raw, bad = read_all_pages(cur)
    problems = list(bad)
    if not parsed:
        problems.append("no held page could be read")
    for y, recs in sorted(parsed.items()):
        n = len(recs) + len(recs.excluded)
        if n != raw[y]:
            problems.append(f"{y}: {raw[y]} page rows, {len(recs)} stored + "
                            f"{len(recs.excluded)} excluded")
    dropped = set()
    if parsed:
        try:
            _, dr = m.apply_withdrawn_rule(parsed)
            dropped = {(c, y) for c, y, _ in dr}
        except ValueError as e:
            problems.append(_first(e))
    if parsed and pe.table_exists(cur, efs.editions_table):
        for y, recs in sorted(parsed.items()):
            cur.execute(f"SELECT DISTINCT lad24cd FROM public."
                        f"{efs.editions_table} WHERE financial_year = %s",
                        (y,))
            stored = {r[0] for r in cur.fetchall()}
            lost = sorted(r["lad24cd"] for r in recs
                          if r["lad24cd"] not in stored
                          and (r["lad24cd"], y) not in dropped)
            if lost:
                problems.append(f"{y}: page rows {lost} are in no edition")
    tot = sum(raw.values()) if raw else 0
    exc = sum(len(r.excluded) for r in parsed.values())
    return not problems, "; ".join(problems[:3]) if problems else (
        f"{tot} rows on {len(parsed)} held pages: {tot - exc} stored, "
        f"{exc} excluded by name, none unread")


def gate_5_page_rows(cur):
    name = ("every page row is stored, excluded by name or halts; a halt "
            "stores nothing")
    problems = []

    def body(e):
        e.site(tl.p25(updated=R1))
        e.ok(e.cur, ["load", "--only", "efs", "--commit"])
        e.cur.execute(f"SELECT lad24cd FROM public.{ZZ_EFS.editions_table}")
        stored = {r[0] for r in e.cur.fetchall()}
        # p25: Barnet, Bradford, Croydon (main), Lambeth (hra); one police
        if stored != {"E09000003", "E08000032", "E09000008", "E09000022"}:
            problems.append(f"stored {sorted(stored)}")
        rc, text, _ = e.cmd(["load", "--only", "efs", "--recheck", P25])
        if "South Yorkshire Mayoral Combined Authority" not in text:
            problems.append("an excluded body is not listed")
        for label, nm in (("an unknown name", "Gloucestershire"),
                          ("a case change", "haringey"),
                          ("a longer name", "Haringey London Borough")):
            e.site(tl.p25(updated=R3, extra_rows=[(
                nm, "£5.0m (support agreed in-principle)")]))
            ok, t = e.halts(["load", "--only", "efs", "--commit"],
                            state(e.cur))
            if not ok:
                problems.append(f"{label} did not halt cleanly: {t[-100:]}")
    scenario(cur, body)
    try:
        parsed, raw, bad = read_all_pages(cur)
        ok_real, detail = real_page_rows(cur, REAL_EFS, parsed, raw, bad)
    except (psycopg2.Error, SystemExit, ValueError) as ex:
        ok_real, detail = False, _first(ex)
    if not ok_real:
        problems.append(f"held pages: {detail}")
    mixed(5, name, not problems,
          f"{detail}; an unknown, case-changed or longer name halts and "
          "stores nothing; an excluded body is listed"
          if not problems else "; ".join(problems[:3]), cur,
          lambda c: real_page_rows(c))


# ---------------------------------------------------------------- gate 6

GOOD_FORMS = (
    ("£55.7m (support agreed in-principle)", P25, Decimal("55.7"),
     "agreed-in-principle"),
    ("£113.0m", P25, Decimal("113.0"), "capitalisation-direction"),
    ("£70.0m (in the form of grant)", "2020-21", Decimal("70.0"), "grant"),
    ("£125.0m (original capitalisation of £100.0m in 2017-18 was extended "
     "by £25.0m)", "2020-21", Decimal("125.0"), "capitalisation-extended"),
    ("£136.0m (support agreed in-principle)\nThis was subsequently revised "
     "to £110.3m (support agreed in-principle)", P25, Decimal("110.3"),
     "agreed-in-principle"),
    ("£26.9m for 2024-25", P25, None, "other-years-only"),
    (m.WITHDRAWN, "2021-22", None, "withdrawn"),
)
UNKNOWN_FORMS = (
    "£5.0m and then something odd",
    "£5.0m (pending)",
    "Agreed, amount to be confirmed",
    "£5.0m (support agreed in-principle) plus £1.0m extra",
    "£12bn",
    "withdrew £5.0m",
    "This was subsequently revised to:",
    "£5.0m agreed in-principle covering:",
)


def recompute(cell_text, year):
    """(amount, status) a stored cell_text gives: the same reading the loader
    makes (a figure with no qualifier is a capitalisation direction)."""
    c = m.parse_cell(cell_text, f"cell_text of {year}")
    if c.withdrawn:
        return None, "withdrawn"
    f = c.figure(year)
    if f is None:
        return None, "other-years-only"
    if f.qual in m.QUAL_STATUS:
        return f.amount, m.QUAL_STATUS[f.qual]
    return f.amount, "capitalisation-direction"


def grammar_problems():
    bad = []
    for text, year, amount, status in GOOD_FORMS:
        try:
            got = recompute(text, year)
        except ValueError as e:
            bad.append(f"a known form halts: {text[:40]!r}: {_first(e)}")
            continue
        if got != (amount, status):
            bad.append(f"{text[:40]!r} read as {got}, expected "
                       f"{(amount, status)}")
    for text in UNKNOWN_FORMS:
        try:
            got = recompute(text, P25)
        except ValueError:
            continue
        bad.append(f"an unknown form is read, not halted: {text!r} -> {got}")
    return bad


def real_grammar(cur, efs=REAL_EFS, bad=None):
    """Every cell of the held pages parses (read_all_pages halts otherwise)
    and every stored cell_text re-parses to the stored amount and status;
    only an edition 1 'as loaded' may have no cell_text."""
    if bad is None:
        _, _, bad = read_all_pages(cur)
    problems = list(bad)
    _need(cur, efs)
    cur.execute(f"SELECT lad24cd, financial_year, edition, amount_m, status, "
                f"cell_text, release_label FROM public.{efs.editions_table}")
    n = nocell = 0
    for la, y, ed, amount, status, cell, label in cur.fetchall():
        if cell is None:
            nocell += 1
            if not (ed == 1 and str(label).startswith("as loaded")):
                problems.append(f"{la} {y} edition {ed} has no cell_text")
            continue
        n += 1
        try:
            got = recompute(cell, str(y))
        except ValueError as e:
            problems.append(f"{la} {y} edition {ed}: {_first(e)}")
            continue
        if got != (amount, status):
            problems.append(f"{la} {y} edition {ed}: cell_text reads "
                            f"{got}, stored {(amount, status)}")
    return not problems, "; ".join(problems[:3]) if problems else (
        f"every cell of the held pages parses; {n} stored cell_text values "
        f"re-parse to the stored amount and status ({nocell} 'as loaded' "
        "rows keep none)")


def gate_6_grammar(cur):
    name = ("grammar coverage: an unknown cell form halts, never read as its "
            "first amount; stored cell_text re-parses to the stored values")
    problems = grammar_problems()

    def body(e):
        e.site(tl.p25(updated=R1, croydon=(
            "£136.0m (support agreed in-principle)<br><br>This was "
            "subsequently revised to £110.3m (support agreed "
            "in-principle)")), tl.p26(updated=R1))
        e.ok(e.cur, ["load", "--only", "efs", "--commit"])
        ok, d = real_grammar(e.cur, ZZ_EFS, [])
        if not ok:
            problems.append(f"stored cell_text: {d}")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ_EFS.editions_table} "
                      "WHERE cell_text IS NULL")
        if e.cur.fetchone()[0]:
            problems.append("a loaded row has no cell_text")
        e.site(tl.p25(updated=R2, extra_rows=[(
            "Slough", "£5.0m (support agreed in-principle) plus £1.0m extra")]),
            tl.p26(updated=R1))
        ok, t = e.halts(["load", "--only", "efs", "--year", P25, "--commit"],
                        state(e.cur), "Slough")
        if not ok:
            problems.append(f"an unknown cell form did not halt: {t[-120:]}")
        e.cur.execute(f"UPDATE public.{ZZ_EFS.live_table} SET amount_m = 1 "
                      "WHERE lad24cd = 'E09000008' AND financial_year = %s",
                      (P25,))
    scenario(cur, body)
    mixed(6, name, not problems,
          f"{len(GOOD_FORMS)} known forms read as written; "
          f"{len(UNKNOWN_FORMS)} unknown forms halt; a loaded year's "
          "cell_text re-parses to its stored values; a page with an "
          "unknown form halts and stores nothing" if not problems
          else "; ".join(problems[:3]), cur,
          lambda c: real_grammar(c))


# ---------------------------------------------------------------- gate 7

def try_insert(cur, spec, data):
    """None if the editions row was accepted, else the error text (a
    savepoint is rolled back either way)."""
    row = {"edition": 1, "supersedes": None, "release_label": "verify",
           "published_date": date(2026, 1, 1), "source_file": "verify",
           "source_sha256": "0" * 8}
    row.update(data)
    cols = ", ".join(row)
    cur.execute("SAVEPOINT ti")
    try:
        cur.execute(f"INSERT INTO public.{spec.editions_table} ({cols}) "
                    f"VALUES ({', '.join(['%s'] * len(row))})",
                    tuple(row.values()))
        return None
    except psycopg2.Error as e:
        return str(e).splitlines()[0]
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT ti")
        cur.execute("RELEASE SAVEPOINT ti")


def efs_row(amount, status, year="1999-00"):
    return {"lad24cd": "E09000008", "financial_year": year,
            "amount_m": amount, "status": status, "hra_only": False}


def real_null_rule(cur, efs=REAL_EFS):
    """In the editions and live: amount_m IS NULL exactly for the statuses
    withdrawn and other-years-only (rule 1); a stored 0 has '£0' in its
    cell_text (it was published, not coerced)."""
    _need(cur, efs)
    nulls = ", ".join(f"'{s}'" for s in m.NULL_STATUSES)
    bad = []
    for t in (efs.editions_table, efs.live_table):
        cur.execute(f"SELECT COUNT(*) FROM public.{t} WHERE (amount_m IS "
                    f"NULL) <> (status IN ({nulls}))")
        n = cnt_ = cur.fetchone()[0]
        if n:
            bad.append(f"{t}: {n} rows where amount_m IS NULL disagrees "
                       f"with the status (NULL only for {nulls})")
        del cnt_
    cur.execute(f"SELECT COUNT(*) FROM public.{efs.editions_table} WHERE "
                "amount_m = 0 AND (cell_text IS NULL OR cell_text !~ "
                "'£0(\\.0+)?m')")
    z = cur.fetchone()[0]
    if z:
        bad.append(f"{z} edition rows hold 0 with no £0 in their cell_text")
    cur.execute(f"SELECT COUNT(*) FROM public.{efs.editions_table} WHERE "
                "status = 'withdrawn' AND cell_text IS NOT NULL AND "
                "cell_text !~ 'withdr'")
    w = cur.fetchone()[0]
    if w:
        bad.append(f"{w} withdrawn rows whose cell_text does not say so")
    cur.execute(f"SELECT COUNT(*) FROM public.{efs.editions_table}")
    total = cur.fetchone()[0]
    return not bad, "; ".join(bad[:3]) if bad else (
        f"amount_m is NULL exactly for {', '.join(m.NULL_STATUSES)} in all "
        f"{total} edition rows and in live; no published 0 was coerced")


def gate_7_null_rule(cur):
    name = ("amount_m is NULL exactly for withdrawn and other-years-only "
            "(rule 1); a published 0 stays 0")
    problems = []

    def body(e):
        for amount, status, refused in (
                (None, "withdrawn", False), (None, "other-years-only", False),
                (Decimal("1.0"), "withdrawn", True),
                (Decimal("1.0"), "other-years-only", True),
                (None, "agreed-in-principle", True),
                (None, "grant", True), (Decimal("0"), "grant", False)):
            msg = try_insert(e.cur, ZZ_EFS, efs_row(amount, status))
            if (msg is not None) != refused:
                problems.append(f"CHECK: amount {amount} with {status}: "
                                f"{'accepted' if msg is None else 'refused'}")
        e.site(tl.p21(), tl.p25(updated=R1, extra_rows=[
            ("Luton", "£1.0m (support agreed in-principle)"),
            ("Shropshire", "£26.9m for 2024-25"),
            ("Slough", "£0.0m (support agreed in-principle)")]))
        e.ok(e.cur, ["load", "--only", "efs", "--commit"])
        e.cur.execute(f"SELECT lad24cd, financial_year, amount_m, status "
                      f"FROM public.{ZZ_EFS.editions_table} WHERE lad24cd IN "
                      "('E06000032', 'E06000051', 'E06000039') ORDER BY 1, 2")
        got = e.cur.fetchall()
        want = [("E06000032", "2021-22", None, "withdrawn"),
                ("E06000032", P25, Decimal("1.000"), "agreed-in-principle"),
                ("E06000039", P25, Decimal("0.000"), "agreed-in-principle"),
                ("E06000051", P25, None, "other-years-only")]
        if got != want:
            problems.append(f"loaded {got}")
        ok, d = real_null_rule(e.cur, ZZ_EFS)
        if not ok:
            problems.append(f"rows: {d}")
        e.cur.execute(f"UPDATE public.{ZZ_EFS.live_table} SET amount_m = 0 "
                      "WHERE lad24cd = 'E06000051'")
    scenario(cur, body)
    mixed(7, name, not problems,
          "the editions CHECK refuses a figure on a withdrawn or "
          "other-years-only row and a NULL on any other; a withdrawn cell "
          "stores NULL, a cell with figures for other years only stores "
          "NULL, a published £0.0m stays 0" if not problems
          else "; ".join(problems[:3]), cur, lambda c: real_null_rule(c))


# ---------------------------------------------------------------- gate 8

def s114_row(attr, succ, note, conf="exact"):
    return {"lad24cd": "E10000021", "notice_date": date(1999, 1, 1),
            "financial_year": "1998-99", "reason": "x",
            "date_confirmed": conf, "attribution": attr,
            "successor_codes": succ, "attribution_note": note}


def real_s114(cur, efs=REAL_EFS, s114=REAL_S114):
    """The gate 14 rule on live and every edition: a code in la_boundaries is
    'direct' with no successors and no note; a code not in it is a
    'predecessor' with successors and a note; a predecessor row's three
    attribution fields are the same in every edition of its key. The
    unevidenced count of the latest editions is reported."""
    _need(cur, s114)
    boundaries = set(m._boundaries(cur).values())
    bad = []
    cur.execute(f"SELECT COUNT(*) FROM pg_constraint WHERE conrelid = "
                f"'public.{s114.live_table}'::regclass AND conname = "
                "'la_s114_notices_attribution_chk'")
    if s114.live_table == REAL_S114.live_table and not cur.fetchone()[0]:
        bad.append("la_s114_notices_attribution_chk is missing on live")
    for t, extra in ((s114.live_table, ""), (s114.editions_table, ", edition")):
        cur.execute(f"SELECT lad24cd, notice_date, attribution, "
                    f"successor_codes, attribution_note{extra} FROM "
                    f"public.{t}")
        for la, d, attr, succ, note, *ed in cur.fetchall():
            direct = la in boundaries
            if direct and (attr != "direct" or succ or note):
                bad.append(f"{t} {la} {d}: in la_boundaries but "
                           f"attribution {attr!r}")
            if not direct and not (attr == "predecessor" and succ and note):
                bad.append(f"{t} {la} {d}: not in la_boundaries but "
                           f"attribution {attr!r} without successors/note")
    cur.execute(f"SELECT lad24cd, notice_date, attribution, successor_codes, "
                f"attribution_note, edition FROM public.{s114.editions_table} "
                "WHERE attribution = 'predecessor' ORDER BY 1, 2, 6")
    first = {}
    for la, d, attr, succ, note, ed in cur.fetchall():
        k = (la, d)
        first.setdefault(k, (attr, succ, note))
        if first[k] != (attr, succ, note):
            bad.append(f"{la} {d}: the predecessor attribution changed in "
                       f"edition {ed}")
    unev = m.unevidenced_count(cur, s114)
    total = sum(len(m.records(cur, s114, p, t["edition"]))
                for p, t in m.tip_info(cur, s114).items())
    return not bad, "; ".join(bad[:3]) if bad else (
        "live and every edition follow the attribution rule and the "
        "predecessor rows are unchanged between editions; unevidenced "
        f"notices in the latest editions: {unev} of {total} (a count, not a "
        "failure until Task 7 has added the evidence; then 0 or each listed "
        "in the decision note)")


def gate_8_s114(cur):
    name = ("S.114: the attribution CHECK; predecessor rows unchanged "
            "between editions (gate 14); the unevidenced count reported")
    problems = []

    def body(e):
        for attr, succ, note, conf, refused in (
                ("predecessor", ["E06000061", "E06000062"], "n", "exact",
                 False),
                ("predecessor", None, "n", "exact", True),
                ("predecessor", ["E06000061"], None, "exact", True),
                ("direct", None, None, "exact", False),
                ("other", None, None, "exact", True),
                ("direct", None, None, "sometime", True)):
            msg = try_insert(e.cur, ZZ_S114, s114_row(attr, succ, note, conf))
            if (msg is not None) != refused:
                problems.append(f"CHECK {attr} succ={succ} note={note} "
                                f"{conf}: "
                                f"{'accepted' if msg is None else 'refused'}")
        e.migrated()
        ok, d = real_s114(e.cur, ZZ_EFS, ZZ_S114)
        if not ok or "unevidenced notices in the latest editions: 3 of 3" \
                not in d:
            problems.append(f"after migration: {ok} {d[:120]}")
        rows = list(tl.ROWS_AS_HELD)
        rows[0] = tl.register_row(
            "Croydon", "E09000008", "2020-11-11", "2020-21", "Overspend",
            "exact", url="https://example.gov.uk/s114.pdf",
            title="Section 114 notice", checked="2026-10-10")
        e.write_register(rows)
        e.ok(e.cur, ["load", "--only", "s114", "--commit"])
        ok, d = real_s114(e.cur, ZZ_EFS, ZZ_S114)
        if not ok or "2 of 3" not in d:
            problems.append(f"after the evidence edition: {ok} {d[:120]}")
        e.cur.execute("SAVEPOINT x")
        e.cur.execute(f"UPDATE public.{ZZ_S114.live_table} SET attribution = "
                      "'direct', successor_codes = NULL, attribution_note = "
                      "NULL WHERE lad24cd = 'E10000021'")
        ok, d = real_s114(e.cur, ZZ_EFS, ZZ_S114)
        if ok or "not in la_boundaries" not in d:
            problems.append(f"a predecessor made direct passes: {d[:100]}")
        e.cur.execute("ROLLBACK TO SAVEPOINT x")
        # a register that changes a predecessor row's attribution is a
        # change against the held edition: it stops
        rows[2] = rows[2].replace("E06000061;E06000062", "E06000061")
        e.write_register(rows, as_at="2026-10-11")
        rc, text, _ = e.cmd(["load", "--only", "s114", "--commit"])
        if rc != 1 or "REJECTED" not in text or "2017-18" not in text:
            problems.append(f"a changed predecessor row was not stopped: "
                            f"rc {rc}")
    scenario(cur, body)
    mixed(8, name, not problems,
          "the CHECK refuses a predecessor without successors or note, an "
          "unknown attribution and an unknown date_confirmed; the rule "
          "holds after the migration and after an evidence edition; a "
          "predecessor made direct fails; a register that changes a "
          "predecessor row is stopped; the unevidenced count is reported"
          if not problems else "; ".join(problems[:3]), cur,
          lambda c: real_s114(c))


# ---------------------------------------------------------------- gate 9

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def set_sha(codes):
    return hashlib.sha256("\n".join(sorted(codes)).encode("utf-8")).hexdigest()


def code_set(cur, table, edition1=False):
    cur.execute(f"SELECT DISTINCT lad24cd FROM public.{table}"
                + (" WHERE edition = 1" if edition1 else ""))
    return {r[0] for r in cur.fetchall()}


def named_removal_codes(cur, editions_table):
    """The authorities whose every edition 1 notice is named in the loader's
    S114_REMOVALS (a removal that leaves the authority with no notice, so its
    s114_flag goes); a named removal of one of several notices loses none."""
    named = {(c, d) for c, d in m.S114_REMOVALS}
    if not named:
        return set()
    cur.execute(f"SELECT lad24cd, notice_date FROM public.{editions_table} "
                "WHERE edition = 1")
    keys = {}
    for code, d in cur.fetchall():
        keys.setdefault(code, set()).add((code, d.isoformat()))
    return {c for c, ks in keys.items() if ks <= named}


def set_line(label, table, codes):
    return (f"{label} {table} lad24cd-set rows={len(codes)} "
            f"{hash_fields(set_sha(codes))}")


def note_set_line(note, label, table):
    """(rows, sha256) of the note line `<label> <table> lad24cd-set rows=<n>
    sha256-first32=<32 hex> sha256-last32=<32 hex>` (halves joined), or None."""
    p = Path(note)
    if not p.exists():
        return None
    mt = re.search(rf"(?m)^{re.escape(label)} {re.escape(table)} "
                   r"lad24cd-set rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
                   r"sha256-last32=([0-9a-f]{32})\s*$",
                   p.read_text(encoding="utf-8"))
    return (int(mt.group(1)), mt.group(2) + mt.group(3)) if mt else None


def real_map_sets(cur, efs=REAL_EFS, s114=REAL_S114, note=NOTE):
    """The map sets: the distinct lad24cd of the live EFS table hash to the
    note's w1-read line (the before-state set less the
    WITHDRAWN_ONLY_NOT_SUPPORT codes, E06000058 and E06000061), contain
    neither, and equal edition 1's set less them, whose own hash is the
    w1-before line (Bexley, in the rule's 'kept' list, stays); the
    distinct lad24cd of live S.114 hash to its w1-read line and equal edition
    1's set less the authorities whose only notices are named S114_REMOVALS
    (a notice is otherwise never removed)."""
    _need(cur, efs, s114)
    if not Path(note).exists():
        return False, f"decision note {Path(note).name} not written"
    bad = []
    live = code_set(cur, efs.live_table)
    ed1 = code_set(cur, efs.editions_table, True)
    s_live = code_set(cur, s114.live_table)
    s_ed1 = code_set(cur, s114.editions_table, True)
    s_expect = s_ed1 - named_removal_codes(cur, s114.editions_table)
    for label, table, codes in (
            ("w1-before", EFS_T, ed1), ("w1-read", EFS_T, live),
            ("w1-read", S114_T, s_live)):
        rec = note_set_line(note, label, table)
        if rec is None:
            bad.append(f"the decision note records no '{label} {table} "
                       "lad24cd-set rows=.. sha256-first32=.. "
                       "sha256-last32=..' line")
        elif rec != (len(codes), set_sha(codes)):
            bad.append(f"{label} {table}: {len(codes)} codes sha256 "
                       f"{set_sha(codes)[:16]}.. differ from the note's "
                       f"{rec[0]} codes {rec[1][:16]}..")
    gone = sorted(RULE_CODES & live)
    if gone:
        bad.append(f"live EFS still holds {gone} (WITHDRAWN_ONLY_NOT_SUPPORT;"
                   " run refresh-latest --accept-key-changes for their years)")
    if live != ed1 - RULE_CODES:
        bad.append(f"live EFS codes differ from edition 1 less the rule "
                   f"codes: live-only {sorted(live - (ed1 - RULE_CODES))}, "
                   f"missing {sorted((ed1 - RULE_CODES) - live)}")
    if s_live != s_expect:
        bad.append(f"live S.114 codes differ from edition 1 less the named "
                   f"removals: live-only {sorted(s_live - s_expect)}, missing "
                   f"{sorted(s_expect - s_live)}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"live la_efs_support has {len(live)} authorities with a row (edition "
        f"1 {len(ed1)} less {len(RULE_CODES)} under the rule) and "
        f"la_s114_notices {len(s_live)}, as the note's w1-read lines")


def gate_9_map_sets(cur):
    name = ("the map sets: distinct lad24cd in la_efs_support (before-state "
            "less the withdrawn-only codes) and in la_s114_notices equal the "
            "note's w1-read lines")
    problems = []

    def body(e):
        e.migrated()
        e.site(tl.p21(), tl.p25(extra_rows=[("Luton", "£1.0m (support "
                                             "agreed in-principle)")]))
        e.ok(e.cur, ["load", "--only", "efs", "--acknowledge", "2021-22",
                     "--acknowledge", P25, "--commit"])
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            ed1 = code_set(e.cur, ZZ_EFS.editions_table, True)
            s_live = code_set(e.cur, ZZ_S114.live_table)

            def write(live, drop=None, extra=""):
                lines = [set_line("w1-before", EFS_T, ed1),
                         set_line("w1-read", EFS_T, live),
                         set_line("w1-read", S114_T, s_live)]
                if drop is not None:
                    del lines[drop]
                note.write_text("intro\n" + "\n".join(lines) + "\n" + extra,
                                encoding="utf-8")
            reduced = ed1 - RULE_CODES
            write(reduced)
            ok, d = real_map_sets(e.cur, ZZ_EFS, ZZ_S114, note)
            if ok or "still holds" not in d:
                problems.append(f"live before the refresh passes: {d[:100]}")
            e.ok(e.cur, ["refresh-latest", "--accept-key-changes", "2021-22",
                         "--commit"])
            ok, d = real_map_sets(e.cur, ZZ_EFS, ZZ_S114, note)
            if not ok:
                problems.append(f"the reduced set fails: {d}")
            write(ed1)       # the before set recorded as the w1-read line
            ok, d = real_map_sets(e.cur, ZZ_EFS, ZZ_S114, note)
            if ok:
                problems.append("a w1-read line holding the rule codes passes")
            for i, what in ((0, "w1-before"), (1, "w1-read EFS"),
                            (2, "w1-read S.114")):
                write(reduced, drop=i)
                ok, d = real_map_sets(e.cur, ZZ_EFS, ZZ_S114, note)
                if ok or "records no" not in d:
                    problems.append(f"a missing {what} line passes")
            write(reduced)
            text = note.read_text(encoding="utf-8")
            note.write_text(text.replace(
                set_sha(reduced)[:32], "0" * 32), encoding="utf-8")
            ok, d = real_map_sets(e.cur, ZZ_EFS, ZZ_S114, note)
            if ok:
                problems.append("a wrong hash passes")
            note.write_text(text, encoding="utf-8")
            ok, _ = real_map_sets(e.cur, ZZ_EFS, ZZ_S114,
                                  Path(tmp) / "missing.md")
            if ok:
                problems.append("a missing note passes")
            e.cur.execute(f"INSERT INTO public.{ZZ_EFS.live_table} (lad24cd, "
                          "financial_year, amount_m, status, hra_only) VALUES "
                          "('E06000051', '2025-26', 1, 'grant', false)")
            ok, d = real_map_sets(e.cur, ZZ_EFS, ZZ_S114, note)
            if ok:
                problems.append("an extra live authority passes")
            ok = note.read_text(encoding="utf-8")
            if re.search(r"[0-9a-f]{64}", ok):
                problems.append("a note line holds a 64-hex run")
    scenario(cur, body)
    mixed(9, name, not problems,
          "a seeded migration, load and refresh leaves the EFS set at the "
          "before-state set less the rule's codes and the S.114 set "
          "unchanged; the gate passes only on the note's lines; a live set "
          "that still holds a rule code, a missing or wrong line, a missing "
          "note and an extra authority fail" if not problems
          else "; ".join(problems[:3]), cur, lambda c: real_map_sets(c))


# ---------------------------------------------------------------- gate 10

def real_ranks(cur, efs=REAL_EFS, s114=REAL_S114):
    """Every year whose latest edition is not edition 1 'as loaded' has a
    rank read from its own source_file (the page's 'page updated' or the
    register's 'register_as_at'), never from a file name."""
    _need(cur, efs, s114)
    bad, n = [], 0
    for spec in (efs, s114):
        for p, t in sorted(m.tip_info(cur, spec).items()):
            if str(t["label"] or "").startswith("as loaded"):
                continue
            n += 1
            if t["rank"] is None:
                bad.append(f"{spec.editions_table} {p}: tip edition "
                           f"{t['edition']} has no rank in its source_file")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{n} latest editions past 'as loaded' all carry their page's or "
        "register's own rank")


def gate_10_older(cur):
    name = ("older-page guard: the rank is the page's or register's own, "
            "never the file name; --allow-older-file is logged")
    problems = []
    later = "efs_2025-26_content_2099-01-01.json"

    def efs(e):
        e.seed(e.cur, tl.p25(updated=R2))
        older = tl.p25(updated=R1, croydon="£130.0m (support agreed "
                                           "in-principle)")
        e.site(older)
        before = state(e.cur)
        for how, argv in (
                ("the collection", ["load", "--only", "efs", "--commit"]),
                ("a file named with a later date",
                 ["load", "--only", "efs", "--page-file",
                  e.save(older, name=later), "--no-page", "--commit"])):
            ok, t = e.halts(argv, before, "older")
            if not ok:
                problems.append(f"{how}: not halted as older: {t[-100:]}")
        rc, text, logged = e.cmd(["load", "--only", "efs",
                                  "--allow-older-file", "--acknowledge", P25,
                                  "--commit"])
        eds = tl.editions(e.cur, P25)
        if not (rc == 0 and eds == [(1, None, 4), (2, 1, 4)]
                and logged.called and "--allow-older-file given"
                in logged.call_args[0][2]):
            problems.append(f"--allow-older-file: rc {rc}, {eds}")
    scenario(cur, efs)

    def s114(e):
        e.migrated()
        rows = list(tl.ROWS_AS_HELD)
        rows[0] = tl.register_row(
            "Croydon", "E09000008", "2020-11-11", "2020-21", "Overspend",
            "exact", url="https://example.gov.uk/a.pdf", title="A",
            checked="2026-10-10")
        e.write_register(rows, as_at="2026-10-10")
        e.ok(e.cur, ["load", "--only", "s114", "--commit"])
        rows[0] = rows[0].replace("/a.pdf", "/b.pdf")
        e.write_register(rows, as_at="2026-10-01")
        ok, t = e.halts(["load", "--only", "s114", "--commit"], state(e.cur),
                        "older")
        if not ok:
            problems.append(f"an older register did not halt: {t[-100:]}")
    scenario(cur, s114)
    mixed(10, name, not problems,
          "an older EFS page offered by the collection or as a file named "
          "with a later date halts with nothing stored or logged; "
          "--allow-older-file stores it, logged; an older register halts"
          if not problems else "; ".join(problems[:3]), cur,
          lambda c: real_ranks(c))


# ---------------------------------------------------------------- gate 11

def gate_11_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED (or halted), stores "
            "nothing, no ledger row, partial run-log row")
    problems = []
    ack25 = ["--acknowledge", P25]
    c_in = "£{}m (support agreed in-principle)"

    def case(label, setup, argv, needle, part="efs", period=P25,
             halts=False):
        def body(c):
            with env(c) as e, tl.specs():
                setup(e)
                if halts:
                    ok, t = e.halts(argv, state(c), needle)
                    bad = None if ok else f"not halted naming {needle!r}: " \
                                          f"{t[-120:]}"
                else:
                    bad = rejects(e, argv, needle, part, period)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)

    load = ["load", "--only", "efs", "--commit"]

    def revised(**kw):
        def setup(e):
            e.seed(e.cur, tl.p25(updated=R1))
            e.site(tl.p25(updated=R2, **kw))
        return setup
    case("revision: any change, listed, needs --acknowledge",
         revised(croydon=c_in.format("110.3")), load, "--acknowledge 2025-26")
    case("revision: a held authority missing is never released",
         revised(drop=("Barnet",)), load + ack25, "PARTIAL PAGE")
    case("revision: an amount going to NULL needs a named flip",
         lambda e: (e.seed(e.cur, tl.p25(updated=R1, extra_rows=[(
             "Shropshire", c_in.format("26.9"))])),
             e.site(tl.p25(updated=R2, extra_rows=[(
                 "Shropshire", "£26.9m for 2024-25")]))),
         load + ack25, "--acknowledge-flips")

    def reissue(e):
        e.seed(e.cur, tl.p25(updated=R2))
        e.site(tl.p25(updated=R2, croydon=c_in.format("130.0")))
    case("revision: equal rank, other content", reissue, load + ack25,
         "--accept-reissue 2025-26")
    case("another page disagrees with the year's own page",
         lambda e: e.site(tl.p25(), tl.p26(croydon_note="110.3")), load,
         "differs")
    case("withdrawn-only rule: a named authority now has support",
         lambda e: e.site(tl.p25(extra_rows=[(
             "Bournemouth, Christchurch and Poole", c_in.format("5.0"))]),
             tl.p21()), load, "no longer holds", halts=True)
    case("withdrawn-only rule: an unnamed authority is withdrawn only",
         lambda e: e.site(tl.p21()), load, "does not name them", halts=True)
    case("an unknown name",
         lambda e: e.site(tl.p25(extra_rows=[("Gloucestershire",
                                              c_in.format("5.0"))])),
         load, "Gloucestershire", halts=True)

    def s114_setup(rows):
        def setup(e):
            e.migrated()
            e.write_register(rows)
        return setup
    s114 = ["load", "--only", "s114", "--commit"]
    case("S.114: a held notice missing is never removed",
         s114_setup([tl.ROWS_AS_HELD[0], tl.ROWS_AS_HELD[2],
                     tl.register_row("Croydon", "E09000008", "2022-02-15",
                                     "2021-22", "Unlawful expenditure",
                                     "exact")]),
         s114 + ["--acknowledge", "2021-22"], "never removes", part="s114",
         period="2021-22")
    case("S.114: a notice added needs --acknowledge",
         s114_setup(tl.ROWS_AS_HELD + [tl.register_row(
             "Woking", "E07000217", "2023-06-07", "2023-24",
             "Risky investments", "exact")]),
         s114, "--acknowledge 2023-24", part="s114", period="2023-24")

    def s114_reissue(e):
        e.migrated()
        rows = list(tl.ROWS_AS_HELD)
        rows[0] = tl.register_row(
            "Croydon", "E09000008", "2020-11-11", "2020-21", "Overspend",
            "exact", url="https://example.gov.uk/a.pdf", title="A",
            checked="2026-10-10")
        e.write_register(rows)
        e.ok(e.cur, ["load", "--only", "s114", "--commit"])
        e.write_register([rows[0].replace("/a.pdf", "/b.pdf")] + rows[1:])
    case("S.114: equal register date, other content", s114_reissue,
         s114 + ["--acknowledge", "2020-21"], "--accept-reissue 2020-21",
         part="s114", period="2020-21")
    report(11, name, not problems,
           "a changed amount, a missing authority, a value going to NULL, an "
           "equal-rank reissue, a disagreeing page, a missing, added or "
           "reissued notice are REJECTED: nothing stored, no ledger row, one "
           "partial run-log row; the withdrawn-only rule breaking, an "
           "unnamed withdrawn-only authority and an unknown name halt"
           if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 12

def gate_12_one_transaction(cur):
    name = ("a new year: edition, live rows and ledger row in one "
            "transaction (both tables)")
    problems = []

    def efs_ok(e):
        e.site(tl.p25(updated=R1), tl.p26(updated=R1))
        e.ok(e.cur, ["load", "--only", "efs", "--commit"])
        for p, n in ((P25, 4), (P26, 4)):
            if tl.editions(e.cur, p) != [(1, None, n)]:
                problems.append(f"EFS {p} edition: {tl.editions(e.cur, p)}")
            if core.rows_differing(e.cur, ZZ_EFS, p, 1):
                problems.append(f"EFS {p}: live differs from edition 1")
            if cnt(e.cur, ZZ_EFS.live_table, "WHERE financial_year = %s",
                   (p,)) != n:
                problems.append(f"EFS {p}: live rows missing")
        led = tl.ledger(e.cur)
        if [(p, o, ed) for p, _, o, ed in led] != [
                (P25, "new", 1), (P26, "new", 1)]:
            problems.append(f"EFS ledger: {led}")
    scenario(cur, efs_ok)

    def efs_fail(e):
        e.site(tl.p25(updated=R1), tl.p26(updated=R1))
        with mock.patch.object(pe, "record_file_check",
                               side_effect=RuntimeError("boom")):
            rc, text, logged = e.cmd(["load", "--only", "efs", "--commit"])
        if rc != 1 or any(counts_of(e.cur, "efs", p) != (0, 0, 0)
                          for p in (P25, P26)):
            problems.append(f"EFS: a failing ledger insert left rows: rc {rc}")
        if not logged.called or "PARTIAL RUN" not in logged.call_args[0][2]:
            problems.append("EFS: no partial run-log row")
    scenario(cur, efs_fail)

    def s114_ok(e):
        e.write_register(tl.ROWS_AS_HELD)
        e.ok(e.cur, ["load", "--only", "s114"] + S114_ACK + ["--commit"])
        for p in ("2017-18", "2020-21", "2021-22"):
            if counts_of(e.cur, "s114", p) != (1, 1, 1) or core.rows_differing(
                    e.cur, ZZ_S114, p, 1):
                problems.append(f"S.114 {p}: {counts_of(e.cur, 's114', p)}")
        if {o for _, _, o, _ in tl.ledger(e.cur, LEDGER_S114)} != {"new"}:
            problems.append("S.114 ledger rows are not 'new'")
    scenario(cur, s114_ok)

    def s114_fail(e):
        e.write_register(tl.ROWS_AS_HELD)
        with mock.patch.object(pe, "record_file_check",
                               side_effect=RuntimeError("boom")):
            rc, _, _ = e.cmd(["load", "--only", "s114"] + S114_ACK
                             + ["--commit"])
        if rc != 1 or any(counts_of(e.cur, "s114", p) != (0, 0, 0)
                          for p in ("2017-18", "2020-21", "2021-22")):
            problems.append(f"S.114: a failing ledger insert left rows: "
                            f"rc {rc}")
    scenario(cur, s114_fail)
    report(12, name, not problems,
           "each new year's edition, live rows and ledger row (outcome "
           "'new') are present together for both tables; a failing ledger "
           "insert leaves nothing and logs a partial run"
           if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 13

def gate_13_ledger_rule(cur):
    name = ("ledger rule: a skip needs the (page or register, sha256) pair "
            "for the year")
    problems = []
    w, sha = "https://x/p", "ab" * 32
    src = m.ledger_source(w, P25, m._when(R2))
    chk = {P25: {(src, sha)}}
    for args, want in (((chk, w, sha, P25), True),
                       ((chk, w, "cd" * 32, P25), False),
                       ((chk, "https://x/q", sha, P25), False),
                       ((chk, w, sha, P26), False),
                       (({}, w, sha, P25), False)):
        if m.ledger_complete(*args) is not want:
            problems.append(f"ledger_complete{args[1:]} is not {want}")

    def efs(e):
        e.seed(e.cur, tl.p25(updated=R2))
        before = state(e.cur)
        text, logged = e.ok(e.cur, ["load", "--only", "efs", "--commit"])
        if "already in the ledger" not in text or logged.called or \
                state(e.cur) != before:
            problems.append("a page in the ledger was not skipped silently")
        e.site(tl.p25(updated=R2, croydon="£130.0m (support agreed "
                                          "in-principle)"))
        rc, text, _ = e.cmd(["load", "--only", "efs", "--acknowledge", P25,
                             "--commit"])
        if rc != 1 or "--accept-reissue" not in text:
            problems.append("a page with the same date and other content "
                            f"was skipped or stored: rc {rc}")
        e.site(tl.p25(updated=R2))
        text, _ = e.ok(e.cur, ["load", "--only", "efs", "--recheck", P25,
                               "--commit"])
        if f"{P25}: unchanged" not in text or {
                o for _, _, o, _ in tl.ledger(e.cur)[1:]} != {"unchanged"}:
            problems.append("--recheck did not read the page again")
    scenario(cur, efs)

    def s114(e):
        seed_both(e, efs=False)
        before = state(e.cur)
        text, logged = e.ok(e.cur, ["load", "--only", "s114", "--commit"])
        if "already in the ledger" not in text or logged.called or \
                state(e.cur) != before:
            problems.append("a register in the ledger was not skipped")
        e.write_register(tl.ROWS_AS_HELD + [tl.register_row(
            "Woking", "E07000217", "2023-06-07", "2023-24",
            "Risky investments", "exact")])
        rc, text, _ = e.cmd(["load", "--only", "s114", "--commit"])
        if rc != 1 or "--acknowledge 2023-24" not in text:
            problems.append("a changed register was skipped by the ledger")
    scenario(cur, s114)
    report(13, name, not problems,
           "a skip needs this exact page or register and sha256 for the "
           "year; a page with the same date and other content, and a "
           "register with another row, are read; --recheck reads again"
           if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 14

def gate_14_preview(cur):
    name = ("preview and simulate write nothing: every writing command")
    problems = []
    acks = ["--acknowledge", "2021-22", "--acknowledge", P25]

    def nothing(e, argv):
        before = state(e.cur)
        for extra in (([], ["--simulate"]) if argv[0] != "status" else ([],)):
            rc, text, logged = e.cmd(argv + extra)
            if (rc != 0 and argv[0] != "status") or state(e.cur) != before or logged.called:
                problems.append(f"{' '.join(map(str, argv[:3]))} "
                                f"{' '.join(extra) or 'preview'}: rc {rc}, "
                                "changed state or logged")

    def body(e):
        e.legacy(e.cur)
        nothing(e, ["migrate-legacy"])
        e.ok(e.cur, ["migrate-legacy", "--commit"])
        e.site(tl.p21(), tl.p25(extra_rows=[("Luton", "£1.0m (support "
                                             "agreed in-principle)")]))
        nothing(e, ["load", "--only", "efs"] + acks)
        nothing(e, ["ddl"])
        nothing(e, ["status"])
        e.ok(e.cur, ["load", "--only", "efs"] + acks + ["--commit"])
        nothing(e, ["refresh-latest", "--accept-key-changes", "2021-22"])
        nothing(e, ["restore-edition", "s12_efs", P25, "1"])
        rows = list(tl.ROWS_AS_HELD)
        rows[0] = tl.register_row(
            "Croydon", "E09000008", "2020-11-11", "2020-21", "Overspend",
            "exact", url="https://example.gov.uk/a.pdf", title="A",
            checked="2026-10-10")
        e.write_register(rows)
        nothing(e, ["load", "--only", "s114"])
    scenario(cur, body)
    report(14, name, not problems,
           "migrate-legacy, load (both parts), ddl, status, refresh-latest "
           "and restore-edition, previewed and simulated, change no table "
           "and write no run-log row" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 15

def gate_15_reread(cur):
    name = ("a byte-identical re-read is unchanged on every path, against "
            "edition 1 'as loaded' too")
    problems = []

    def plain(e):
        pg = tl.p25(updated=R1)
        e.seed(e.cur, pg)
        path = e.save(pg)
        for argv in (["load", "--only", "efs"],
                     ["load", "--only", "efs", "--recheck", P25],
                     ["load", "--only", "efs", "--page-file", path],
                     ["load", "--only", "efs", "--page-file", path,
                      "--no-page", "--commit"],
                     ["load", "--only", "efs", "--recheck", P25, "--commit"]):
            text, _ = e.ok(e.cur, argv)
            if "REJECTED" in text or ": revised" in text:
                problems.append(f"{' '.join(map(str, argv[2:5]))}: not "
                                "unchanged")
        if tl.editions(e.cur, P25) != [(1, None, 4)]:
            problems.append("a re-read stored an edition")
    scenario(cur, plain)

    def as_loaded(e):
        tl.seed_live_efs(e.cur, [
            ("E09000003", P25, Decimal("55.700"), "agreed-in-principle"),
            ("E08000032", P25, Decimal("113.000"), "capitalisation-direction"),
            ("E09000008", P25, Decimal("136.000"), "agreed-in-principle")])
        e.cur.execute(f"INSERT INTO public.{ZZ_EFS.live_table} (lad24cd, "
                      "financial_year, amount_m, status, hra_only, source, "
                      "loaded_at) VALUES ('E09000022', %s, 40, "
                      "'agreed-in-principle', true, %s, %s)",
                      (P25, tl.N8N_EFS.format(P25), tl.LOADED))
        tl.seed_live_s114(e.cur)
        pg = tl.p25()
        f25 = e.save(pg)
        csv_path = e.root / "legacy.csv"
        csv_path.write_text(tl.LEGACY_CSV, encoding="utf-8")
        efs_state = m.live_state(e.cur, ZZ_EFS)
        s114_state = m.live_state(e.cur, ZZ_S114)
        with mock.patch.object(m, "LEGACY_EFS", efs_state), \
                mock.patch.object(m, "LEGACY_S114", s114_state), \
                mock.patch.object(m, "LEGACY_PAGES", {P25: {
                    "path": f25, "sha256": m.content_sha256(f25)}}), \
                mock.patch.object(m, "LEGACY_S114_FILE", {
                    "path": csv_path, "sha256": m.content_sha256(csv_path)}):
            e.ok(e.cur, ["migrate-legacy", "--commit"])
        e.site(pg)
        for argv in (["load", "--only", "efs", "--page-file", f25,
                      "--no-page"],
                     ["load", "--only", "efs"],
                     ["load", "--only", "efs", "--commit"],
                     ["load", "--only", "efs", "--recheck", P25, "--commit"]):
            text, _ = e.ok(e.cur, argv)
            if f"{P25}: unchanged" not in text or "REJECTED" in text:
                problems.append(f"edition 1 as loaded, "
                                f"{' '.join(map(str, argv[2:4]))}: not "
                                "unchanged")
        if tl.editions(e.cur, P25) != [(1, None, 4)]:
            problems.append("a page reproducing edition 1 stored an edition")
        e.write_register(tl.ROWS_AS_HELD)
        text, _ = e.ok(e.cur, ["load", "--only", "s114", "--commit"])
        for p in ("2017-18", "2020-21", "2021-22"):
            if f"{p}: unchanged" not in text:
                problems.append(f"S.114 {p} as held is not unchanged")
            if tl.editions(e.cur, p, "zz_s12_s114_editions") != [(1, None, 1)]:
                problems.append(f"S.114 {p} stored an edition")
    scenario(cur, as_loaded)
    report(15, name, not problems,
           "a page read again by the collection, --recheck, --page-file and "
           "--page-file --no-page, in preview and with --commit, and a "
           "page or register that reproduces edition 1 as loaded, are "
           "unchanged and store no edition" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 16

def live_snapshot(cur, spec, period):
    cols = ", ".join(tuple(spec.key_cols) + tuple(
        c for c, _ in spec.value_cols) + ("source", "loaded_at"))
    cur.execute(f"SELECT {cols} FROM public.{spec.live_table} WHERE "
                f"{spec.period_col} = %s ORDER BY 1, 2", (period,))
    return cur.fetchall()


def gate_16_refresh(cur):
    name = ("refresh-latest changes only the revised years, copies "
            "loaded_at and source; key changes only with "
            "--accept-key-changes")
    problems = []

    def efs(e):
        seed_both(e)
        other = live_snapshot(e.cur, ZZ_EFS, P26)
        s114 = live_snapshot(e.cur, ZZ_S114, "2020-21")
        e.site(tl.p25(updated=R2, croydon="£110.3m (support agreed "
                                          "in-principle)"),
               tl.p26(updated=R1))
        e.ok(e.cur, ["load", "--only", "efs", "--acknowledge", P25,
                     "--commit"])
        if pe.status(e.cur, m.profile("s12_efs"))["pending_refresh"] != [P25]:
            problems.append("status does not name the revised year")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if live_snapshot(e.cur, ZZ_EFS, P26) != other or \
                live_snapshot(e.cur, ZZ_S114, "2020-21") != s114:
            problems.append("refresh-latest changed a year that was not "
                            "revised")
        e.cur.execute(f"SELECT loaded_at, release_label FROM public."
                      f"{ZZ_EFS.editions_table} WHERE financial_year = %s "
                      "AND edition = 2 LIMIT 1", (P25,))
        loaded, label = e.cur.fetchone()
        e.cur.execute(f"SELECT amount_m, loaded_at, source FROM public."
                      f"{ZZ_EFS.live_table} WHERE lad24cd = 'E09000008' AND "
                      "financial_year = %s", (P25,))
        if e.cur.fetchone() != (Decimal("110.300"), loaded, label):
            problems.append("the refreshed row is not the edition's amount, "
                            "loaded_at and release_label")
        # a key added by a later page
        e.site(tl.p25(updated=R3, croydon="£110.3m (support agreed "
                                          "in-principle)",
                      extra_rows=[("Haringey", "£40.6m (support agreed "
                                               "in-principle)")]),
               tl.p26(updated=R1))
        e.ok(e.cur, ["load", "--only", "efs", "--acknowledge", P25,
                     "--commit"])
        snap = state(e.cur)
        ok, t = e.halts(["refresh-latest", "--commit"], snap,
                        "--accept-key-changes")
        if not ok:
            problems.append(f"a key added was refreshed without "
                            f"--accept-key-changes: {t[-100:]}")
        e.ok(e.cur, ["refresh-latest", "--accept-key-changes", P25,
                     "--commit"])
        if live_snapshot(e.cur, ZZ_EFS, P25)[0][0] is None or not any(
                r[0] == "E09000014" for r in live_snapshot(e.cur, ZZ_EFS,
                                                           P25)):
            problems.append("the accepted key change was not applied")
        # drift is reported, not overwritten
        e.cur.execute(f"UPDATE public.{ZZ_EFS.live_table} SET amount_m = "
                      "amount_m + 1 WHERE lad24cd = 'E09000003' AND "
                      "financial_year = %s", (P26,))
        drifted = live_snapshot(e.cur, ZZ_EFS, P26)
        rc, _, _ = e.cmd(["refresh-latest", "--commit"])
        if live_snapshot(e.cur, ZZ_EFS, P26) != drifted or \
                P26 not in pe.status(e.cur, m.profile("s12_efs"))[
                    "drift_periods"]:
            problems.append("a drifted live year was overwritten or not "
                            f"reported (rc {rc})")
    scenario(cur, efs)

    def s114(e):
        seed_both(e, efs=False)
        rows = list(tl.ROWS_AS_HELD)
        rows[1] = tl.register_row(
            "Croydon", "E09000008", "2022-01-18", "2021-22",
            "Unlawful expenditure", "exact", url="https://x.gov.uk/r.pdf",
            title="Report", checked="2026-10-10")
        e.write_register(rows, as_at="2026-10-11")
        text, _ = e.ok(e.cur, ["load", "--only", "s114", "--commit"])
        if "date correction" not in text:
            problems.append("a date correction is not listed")
        ok, t = e.halts(["refresh-latest", "--commit"], state(e.cur),
                        "--accept-key-changes")
        if not ok:
            problems.append("a date correction was refreshed without "
                            "--accept-key-changes")
        e.ok(e.cur, ["refresh-latest", "--accept-key-changes", "2021-22",
                     "--commit"])
        e.cur.execute(f"SELECT notice_date FROM public.{ZZ_S114.live_table} "
                      "WHERE financial_year = '2021-22' AND lad24cd = "
                      "'E09000008'")
        if e.cur.fetchall() != [(date(2022, 1, 18),)]:
            problems.append("the corrected date did not reach live")
        if not m.status_ok(e.cur, "s12_s114"):
            problems.append("S.114 status is not clean after the refresh")
    scenario(cur, s114)
    report(16, name, not problems,
           "refresh-latest changed only the revised year, copied the "
           "edition's amount, loaded_at and release_label; an added key and "
           "a corrected notice date need --accept-key-changes; a drifted "
           "year is reported, never overwritten" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 17

def _cellv(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ";".join(str(x) for x in v)
    return str(v)


def columns_sha(spec, rows):
    """sha256 of a year's live columns, one line per row sorted: EFS
    lad24cd|amount_m|status|hra_only; S.114 lad24cd|notice_date|
    financial_year|reason|date_confirmed|attribution|successor_codes (joined
    by ';')|attribution_note; NULL as '', booleans true/false, LF-joined,
    UTF-8. Not the live source (edition 1 does not carry it), not loaded_at.
    rows: m.records of the live table or of an edition alike."""
    if is_s114(spec):
        cols = ("lad24cd", "notice_date", "financial_year") + m.S114_VALUES
    else:
        cols = ("lad24cd",) + m.EFS_VALUES
    lines = sorted("|".join(_cellv(r[c]) for c in cols) for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def table_of(spec):
    return S114_T if is_s114(spec) else EFS_T


def note_hashes(note, table):
    """{year: (rows, sha256)} from the lines `before-state <table> <year>
    rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the note (the
    halves joined), or {}."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                rf"(?m)^before-state {re.escape(table)} "
                r"([0-9]{4}-[0-9]{2}) rows=([0-9]+) "
                r"sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text)}


def note_lines_for(cur, efs=REAL_EFS, s114=REAL_S114):
    """The decision note's hash lines for the live tables as they stand
    (read-only): a before-state line per live year of each table, the
    w1-before set of lad24cd with an EFS row, the w1-read EFS set (that set
    less the WITHDRAWN_ONLY_NOT_SUPPORT codes) and the w1-read S.114 set.
    Print them before migrate-legacy."""
    out = []
    for spec in (efs, s114):
        for p in _live_periods(cur, spec):
            recs = m.records(cur, spec, p)
            out.append(f"before-state {table_of(spec)} {p} rows={len(recs)} "
                       f"{hash_fields(columns_sha(spec, recs))}")
    live = code_set(cur, efs.live_table)
    out.append(set_line("w1-before", EFS_T, live))
    out.append(set_line("w1-read", EFS_T, live - RULE_CODES))
    out.append(set_line("w1-read", S114_T, code_set(cur, s114.live_table)))
    return out


def real_edition1_hash(cur, efs=REAL_EFS, s114=REAL_S114, note=NOTE,
                       survey=None):
    """Edition 1 'as loaded' of every migrated year of both tables equals the
    before-state line in the decision note (row count and columns_sha), the
    rows over all years are the surveyed live count, and the note has no
    line for a year that was not migrated. survey: ((efs rows, efs years),
    (s114 rows, s114 years))."""
    _need(cur, efs, s114)
    if survey is None:
        survey = ((m.LEGACY_EFS[0], len(m.LEGACY_EFS[3])),
                  (m.LEGACY_S114[0], len(m.LEGACY_S114[3])))
    bad, parts = [], []
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    for spec, (rows, years) in zip((efs, s114), survey):
        table = table_of(spec)
        recorded = note_hashes(note, table)
        cur.execute(f"SELECT DISTINCT {spec.period_col} FROM public."
                    f"{spec.editions_table} WHERE edition = 1 AND "
                    "release_label IN (%s, %s) ORDER BY 1",
                    (core.AS_LOADED_LABEL, core.AS_LOADED_LATEST_LABEL))
        migrated = [str(r[0]) for r in cur.fetchall()]
        if not migrated:
            bad.append(f"{table}: no edition 1 'as loaded' (migrate-legacy "
                       "not run)")
        elif len(migrated) != years:
            bad.append(f"{table}: {len(migrated)} migrated year(s), "
                       f"surveyed {years}")
        total = 0
        for p in migrated:
            recs = m.records(cur, spec, p, 1)
            n, h = len(recs), columns_sha(spec, recs)
            total += n
            if Path(note).exists():
                if p not in recorded:
                    bad.append(f"the decision note records no 'before-state "
                               f"{table} {p} rows=.. sha256-first32=.. "
                               "sha256-last32=..' line")
                elif recorded[p] != (n, h):
                    bad.append(f"{table} {p}: edition 1 {n} rows sha256 "
                               f"{h[:16]}.. differs from the note's "
                               f"{recorded[p][0]} rows {recorded[p][1][:16]}..")
            parts.append(f"{p} {n} rows {h[:8]}..")
        if migrated and total != rows:
            bad.append(f"{table}: edition 1 holds {total} rows, surveyed "
                       f"{rows}")
        for p in recorded:
            if p not in migrated:
                bad.append(f"the note has a {table} line for {p}, which has "
                           "no edition 1 as loaded")
    return not bad, "; ".join(bad[:3]) if bad else (
        "edition 1 of every migrated year of both tables hashes to the "
        "before-state lines in the decision note (" + "; ".join(parts[:3])
        + "; ...)")


def gate_17_before_state(cur):
    name = ("edition 1 equals the before-state hashes recorded in the "
            "decision note")
    out = {}

    def body(e):
        e.legacy(e.cur)
        before = {}
        for spec in (ZZ_EFS, ZZ_S114):
            for p in _live_periods(e.cur, spec):
                recs = m.records(e.cur, spec, p)
                before[(table_of(spec), p)] = (len(recs),
                                               columns_sha(spec, recs))
        lines = [f"before-state {t} {p} rows={n} {hash_fields(h)}"
                 for (t, p), (n, h) in sorted(before.items())]
        e.ok(e.cur, ["migrate-legacy", "--commit"])
        out["same"] = all(
            (len(m.records(e.cur, s, p, 1)), columns_sha(
                s, m.records(e.cur, s, p, 1))) == before[(table_of(s), p)]
            for s in (ZZ_EFS, ZZ_S114) for p in _live_periods(e.cur, s))
        survey = ((m.LEGACY_EFS[0], len(m.LEGACY_EFS[3])),
                  (m.LEGACY_S114[0], len(m.LEGACY_S114[3])))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"

            def run(text, sv=survey, path=None):
                note.write_text(text, encoding="utf-8")
                return real_edition1_hash(e.cur, ZZ_EFS, ZZ_S114,
                                          path or note, sv)
            full = "intro\n" + "\n".join(lines) + "\n"
            out["ok"] = run(full)
            efs_line = next(x for x in lines if f"{EFS_T} {P25}" in x)
            h25 = before[(EFS_T, P25)][1]
            out["bad_hash"] = run(full.replace(h25[:32], "0" * 32))
            out["no_line"] = run(full.replace(efs_line + "\n", ""))
            out["bad_rows"] = run(full.replace(
                f"{EFS_T} {P25} rows={before[(EFS_T, P25)][0]}",
                f"{EFS_T} {P25} rows=99"))
            out["no_note"] = run(full, path=Path(tmp) / "x.md")
            out["wrong_survey"] = run(full, ((survey[0][0] + 5, survey[0][1]),
                                             survey[1]))
            out["wrong_years"] = run(full, (survey[0], (survey[1][0],
                                                        survey[1][1] + 1)))
            out["extra_line"] = run(full + f"before-state {EFS_T} 2019-20 "
                                    f"rows=1 {hash_fields('1' * 64)}\n")
            out["scan_safe"] = not re.search(r"[0-9a-f]{64}", full)
        e.cur.execute(f"UPDATE public.{ZZ_EFS.live_table} SET amount_m = "
                      "amount_m + 1 WHERE lad24cd = 'E09000003'")
        out["changed"] = columns_sha(
            ZZ_EFS, m.records(e.cur, ZZ_EFS, P25)) != before[(EFS_T, P25)][1]
        rc, text, _ = e.cmd(["migrate-legacy", "--commit"])
        out["twice"] = rc == "halt" and "runs once" in text
        out["helper"] = note_lines_for(e.cur, ZZ_EFS, ZZ_S114)
    scenario_legacy_error = None
    try:
        scenario(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        scenario_legacy_error = _first(ex)
        return report(17, name, False, scenario_legacy_error)
    helper = out.get("helper", [])
    ok = (out.get("same") and out["ok"][0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and not out["wrong_years"][0]
          and not out["extra_line"][0] and out.get("scan_safe")
          and out.get("changed") and out.get("twice")
          and len(helper) == 8
          and any(x.startswith("w1-read " + EFS_T) for x in helper)
          and not any(re.search(r"[0-9a-f]{64}", x) for x in helper))
    mixed(17, name, bool(ok),
          "a seeded migration stores an edition 1 whose hash equals the "
          "live rows' before it and the note's lines (both tables); a wrong "
          "hash, a wrong row count, a missing line, an extra line, a wrong "
          "survey and a missing note fail; a later change shows; a second "
          "migration is refused; --print-note-lines output is scan-safe"
          if ok else f"seeded: { {k: v for k, v in out.items() if k != 'helper'} }",
          cur, lambda c: real_edition1_hash(c))


# ---------------------------------------------------------------- gate 18

def gate_18_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        seed_both(e)
        pg = tl.p25(updated=R1)
        path = e.save(pg)
        snap, led = tables_state(e.cur), (
            cnt(e.cur, LEDGER_EFS), cnt(e.cur, LEDGER_S114))
        for argv in (["load", "--commit"],
                     ["load", "--only", "efs", "--page-file", path,
                      "--no-page", "--commit"],
                     ["load", "--only", "efs", "--recheck", P25, "--commit"],
                     ["refresh-latest", "--commit"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or tables_state(e.cur) != snap:
                problems.append(f"{' '.join(map(str, argv[:3]))}: changed "
                                f"the editions or live: rc {rc}")
        for t, n in ((LEDGER_EFS, led[0]), (LEDGER_S114, led[1])):
            cur_rows = tl.ledger(e.cur, t)[n:]
            if any(o != "unchanged" for _, _, o, _ in cur_rows):
                problems.append(f"{t}: a re-read ledger row is not "
                                "'unchanged'")
        before = state(e.cur)
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or state(e.cur) != before or logged.called:
            problems.append("a ledger-skipped load changed state or logged")
        if not (m.status_ok(e.cur, "s12_efs") and m.status_ok(e.cur,
                                                              "s12_s114")):
            problems.append("status not clean")
    scenario(cur, body)
    report(18, name, not problems,
           "a second load by the collection, --page-file --no-page and "
           "--recheck and a refresh-latest stored no edition and changed no "
           "live row (re-reads add 'unchanged' ledger rows only); a "
           "ledger-skipped load logs nothing" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 19

def gate_19_stranded(cur):
    name = "stranded year repair (edition held, live rows missing)"
    problems = []

    def body(e):
        seed_both(e)
        eds = (tl.editions(e.cur, P25), tl.editions(
            e.cur, "2020-21", "zz_s12_s114_editions"))
        e.cur.execute(f"DELETE FROM public.{ZZ_EFS.live_table} WHERE "
                      "financial_year = %s", (P25,))
        e.cur.execute(f"DELETE FROM public.{ZZ_S114.live_table} WHERE "
                      "financial_year = '2020-21'")
        if pe.status(e.cur, m.profile("s12_efs"))["live_missing"] != [P25] or \
                pe.status(e.cur, m.profile("s12_s114"))["live_missing"] != [
                    "2020-21"]:
            problems.append("status does not name the stranded years")
        text, _ = e.ok(e.cur, ["load", "--commit"])
        if "live-missing" not in text:
            problems.append("load did not repair: " + text[-120:])
        if (tl.editions(e.cur, P25), tl.editions(
                e.cur, "2020-21", "zz_s12_s114_editions")) != eds:
            problems.append("the repair stored a new edition")
        if core.rows_differing(e.cur, ZZ_EFS, P25, 1) or core.rows_differing(
                e.cur, ZZ_S114, "2020-21", 1):
            problems.append("the repaired rows do not equal the tip")
        if not (m.status_ok(e.cur, "s12_efs") and m.status_ok(e.cur,
                                                              "s12_s114")):
            problems.append("status not clean after the repair")
        outs = {o for _, _, o, _ in tl.ledger(e.cur)}
        outs |= {o for _, _, o, _ in tl.ledger(e.cur, LEDGER_S114)}
        if "live-missing" not in outs:
            problems.append("no live-missing ledger row")
    scenario(cur, body)
    report(19, name, not problems,
           "a stranded EFS year and a stranded notice year were rebuilt "
           "from their edition with no new edition and a live-missing "
           "ledger row" if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 20

def gate_20_restore(cur):
    name = "restore-edition round trip (both tables)"
    problems = []

    def efs(e):
        e.seed(e.cur, tl.p25(updated=R1))
        e.site(tl.p25(updated=R2, croydon="£110.3m (support agreed "
                                          "in-principle)"))
        e.ok(e.cur, ["load", "--only", "efs", "--acknowledge", P25,
                     "--commit"])
        e.ok(e.cur, ["refresh-latest", "--commit"])
        snap = state(e.cur)
        rc, text, logged = e.cmd(["restore-edition", "s12_efs", P25, "1"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("EFS preview wrote")
        rc, text, logged = e.cmd(["restore-edition", "s12_efs", P25, "1",
                                  "--commit"])
        if rc != 0 or tl.editions(e.cur, P25)[-1] != (3, 2, 4) or \
                not logged.called:
            problems.append(f"EFS restore --commit: rc {rc}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ_EFS.editions_table} WHERE edition = 3")
        if e.cur.fetchall() != [("restored from edition 1",)]:
            problems.append("the restored edition is not labelled")
        if tl.live(e.cur, "E09000008") != Decimal("110.300"):
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.live(e.cur, "E09000008") != Decimal("136.000") or \
                core.rows_differing(e.cur, ZZ_EFS, P25, 3):
            problems.append("refresh-latest did not apply the restored "
                            "edition")
        key = lambda rs: sorted(tuple(sorted((k, v) for k, v in r.items() if k != "live_source")) for r in rs)  # noqa: E731
        if key(m.records(e.cur, ZZ_EFS, P25, 1)) != key(
                m.records(e.cur, ZZ_EFS, P25, 3)):
            problems.append("edition 3 does not hold edition 1's rows")
        for argv in (["restore-edition", "s12_efs", P25, "3", "--commit"],
                     ["restore-edition", "s12_efs", P25, "9", "--commit"]):
            ok, _ = e.halts(argv, state(e.cur))
            if not ok:
                problems.append(f"{' '.join(argv[:4])} did not halt")
    scenario(cur, efs)

    def s114(e):
        seed_both(e, efs=False)
        rows = list(tl.ROWS_AS_HELD)
        rows[0] = tl.register_row(
            "Croydon", "E09000008", "2020-11-11", "2020-21", "Overspend",
            "exact", url="https://example.gov.uk/a.pdf", title="A",
            checked="2026-10-10")
        e.write_register(rows, as_at="2026-10-11")
        e.ok(e.cur, ["load", "--only", "s114", "--acknowledge", "2020-21",
                     "--commit"])
        e.ok(e.cur, ["refresh-latest", "--commit"])
        e.ok(e.cur, ["restore-edition", "s12_s114", "2020-21", "1",
                     "--commit"])
        if tl.editions(e.cur, "2020-21", "zz_s12_s114_editions")[-1] != (
                3, 2, 1):
            problems.append("S.114 restore did not store edition 3")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        e.cur.execute(f"SELECT evidence_url FROM public."
                      f"{ZZ_S114.editions_table} WHERE financial_year = "
                      "'2020-21' AND edition = 3")
        if e.cur.fetchall() != [(None,)]:
            problems.append("the restored notice carries edition 2's "
                            "evidence")
        if not m.status_ok(e.cur, "s12_s114"):
            problems.append("S.114 status not clean after the restore")
    scenario(cur, s114)
    report(20, name, not problems,
           "edition 1 stored as edition 3 ('restored from edition 1') and "
           "applied by refresh-latest for EFS and for the notices; the tip "
           "and a missing edition are refused; a preview writes nothing"
           if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 21

class _Resp:
    def __init__(self, body, url):
        self.content, self.url = body, url
        self.text = body.decode("utf-8")

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, body):
        self.body = body

    def get(self, url, **kw):
        return _Resp(self.body, url)


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
    files = [Path(__file__).resolve(), LOADER, HERE / "manual_input.py",
             HERE / "period_editions.py", HERE / "geography.py", NAMES,
             HERE / "tests" / "test_s12_verify_gates.py"]
    files = [f for f in files if f.exists()]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]
    probe = [REAL_EFS.editions_table, REAL_S114.editions_table,
             ledger_name(REAL_EFS), ledger_name(REAL_S114)]
    cur.execute("SELECT " + ", ".join("to_regclass(%s)" for _ in probe),
                [f"public.{t}" for t in probe])
    real_before = cur.fetchone()

    def body(c):
        with env(c) as e, tl.specs():
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.site(tl.p25(updated=R1), tl.p26(updated=R1))
                e.write_register(tl.ROWS_AS_HELD)
                rcs = [e.cmd(a)[0] for a in (
                    ["load", "--commit"] + S114_ACK, ["status"],
                    ["refresh-latest"], ["load"])]
                raw = e.root / "raw"
                body_a = json.dumps(tl.p25(updated=R1)).encode("utf-8")
                body_b = json.dumps(tl.p25(updated=R2)).encode("utf-8")
                base = tl.YEAR_BASE.format(P25)
                with contextlib.redirect_stdout(io.StringIO()):
                    p1, _ = m.fetch_page(base, _Session(body_a), raw)
                    sha1 = m.content_sha256(Path(raw) / "efs_2025-26_content_"
                                            f"{date.today():%Y-%m-%d}.json")
                    m.fetch_page(base, _Session(body_b), raw)
                    m.fetch_page(base, _Session(body_a), raw)
                saved = sorted(Path(raw).glob("*.json"))
                kept = (len(saved) == 2 and sha1 in (
                    m.content_sha256(saved[0]), m.content_sha256(saved[1]))
                    and (Path(raw) / f"efs_2025-26_content_{date.today():%Y-%m-%d}.json").exists()
                    and m.content_sha256(Path(raw) / "efs_2025-26_content_"
                                         f"{date.today():%Y-%m-%d}.json")
                    == sha1)
            return rcs, kept
    try:
        rcs, kept = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(21, name, False, f"{type(ex).__name__}: {ex}")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs_after = cur.fetchone()[0]
    cur.execute("SELECT " + ", ".join("to_regclass(%s)" for _ in probe),
                [f"public.{t}" for t in probe])
    real_after = cur.fetchone()
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    literal = [n for n, t in texts.items() if pat.search(t)]
    longhex = [n for n, t in texts.items()
               if re.search(r"(?<![0-9A-Za-z])[0-9a-f]{40,}(?![0-9A-Za-z])", t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    ok = (not attempts and rcs == [0, 0, 0, 0] and kept
          and runs == runs_after and real_before == real_after
          and not literal and not longhex and not in_src and not in_out)
    report(21, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned {rcs}; a "
           f"same-named different download was saved beside it, the first "
           f"untouched={kept}; run-log rows {runs}->{runs_after}; real "
           f"editions tables and ledgers unchanged="
           f"{real_before == real_after}; secret-like literal in source "
           f"{literal or 'none'}; 40+ hex run in source {longhex or 'none'}; "
           f"{len(secrets)} secret-named settings checked against "
           f"{len(files)} files and {len(OUTPUT)} output lines: in source "
           f"{in_src or 'none'}, in output {in_out or 'none'}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_names, gate_4_misattribution, gate_5_page_rows,
         gate_6_grammar, gate_7_null_rule, gate_8_s114, gate_9_map_sets,
         gate_10_older, gate_11_stop_conditions, gate_12_one_transaction,
         gate_13_ledger_rule, gate_14_preview, gate_15_reread,
         gate_16_refresh, gate_17_before_state, gate_18_rerun,
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


def print_note_lines():
    """The decision note's hash lines for the live tables as they stand,
    read on a read-only connection (nothing is written)."""
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            for line in note_lines_for(cur, REAL_EFS, REAL_S114):
                print(line)
    finally:
        conn.rollback()
        conn.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--print-note-lines"]:
        return print_note_lines()
    if argv:
        sys.exit("usage: s12_financial_stress_editions_verify.py "
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
    mine = [t for t in left if t.startswith("zz_s12")]
    if mine:
        print(f"LEFTOVER: committed zz_s12 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s12 table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
